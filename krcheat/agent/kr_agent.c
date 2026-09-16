/*
 * kr_agent.c — the injected agent (foundation §9.3, §11).
 *
 * Loaded into the running game with DYLD_INSERT_LIBRARIES. It does three things and
 * deliberately nothing else:
 *
 *   1. captures the `lua_State *` by interposing `luaL_newstate` / `lua_newstate`;
 *   2. gets a once-per-frame callback by interposing `SDL_GL_SwapWindow`;
 *   3. answers a file channel in $TMPDIR/krcheat/<pid>/, evaluating snippets in the game's
 *      own VM and maintaining per-frame overrides.
 *
 * Hard rules, from §11.6 — a failure here is a crash or a hang in the user's game, which is
 * the one thing this project cannot undo:
 *
 *   - never block the main thread: plain non-blocking file I/O, no waiting, no locks held
 *     across a snippet;
 *   - never re-enter: a snippet that calls back into the hook is refused, not queued;
 *   - never let a snippet loop forever: an instruction-count debug hook turns a runaway
 *     snippet into a Lua error well before a human notices;
 *   - every failure is logged and swallowed, never raised into the game.
 *
 * The agent knows nothing about Kingdom Rush. All field names live in the CLI's snippet
 * library, and everything game-specific arrives as a Lua string.
 */

#include "kr_agent.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach-o/nlist.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* ---------------------------------------------------------------------- constants */

#define KR_MAX_REQUEST (64 * 1024)      /* §11.6: cmd.json cap */
#define KR_MAX_RESPONSE (1024 * 1024)   /* §11.3: out.json cap */
#define KR_MAX_OVERRIDES 8
#define KR_KEY_MAX 32
#define KR_MAX_ENTRIES 24               /* fields in one request object */

/* Instructions a single snippet may execute before the watchdog kills it. Generous for a
 * real snippet (a few thousand), tiny compared to a hang. */
#define KR_INSTRUCTION_BUDGET 4000000UL

/* Overrides are dropped this long after the last heartbeat (§11.7). */
#define KR_DEFAULT_HEARTBEAT_SECONDS 10.0

/* How often the frame counter is written to the log, in frames. */
#define KR_LOG_EVERY_FRAMES 600

#ifndef KR_AGENT_VERSION
#define KR_AGENT_VERSION "0.1.0"
#endif

/* ------------------------------------------------------------------- tiny utilities */

static char *kr_strdup(const char *s)
{
    size_t n;
    char *copy;
    if (!s) {
        return NULL;
    }
    n = strlen(s);
    copy = (char *)malloc(n + 1);
    if (copy) {
        memcpy(copy, s, n + 1);
    }
    return copy;
}

static void kr_free_str(char **slot)
{
    if (slot && *slot) {
        free(*slot);
        *slot = NULL;
    }
}

static double kr_now(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0.0;
    }
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* ---------------------------------------------------------------- the growing buffer */

typedef struct {
    char *data;
    size_t len;
    size_t cap;
    int failed;
} kr_buf;

static void kr_buf_init(kr_buf *b, size_t cap)
{
    b->data = (char *)malloc(cap);
    b->len = 0;
    b->cap = b->data ? cap : 0;
    b->failed = b->data ? 0 : 1;
    if (b->data) {
        b->data[0] = '\0';
    }
}

static void kr_buf_free(kr_buf *b)
{
    if (b->data) {
        free(b->data);
    }
    b->data = NULL;
    b->len = b->cap = 0;
}

static void kr_buf_append(kr_buf *b, const char *s, size_t n)
{
    if (b->failed || !s) {
        return;
    }
    if (b->len + n + 1 > b->cap) {
        size_t want = b->len + n + 1;
        size_t cap = b->cap ? b->cap : 256;
        char *grown;
        while (cap < want) {
            cap *= 2;
        }
        grown = (char *)realloc(b->data, cap);
        if (!grown) {
            b->failed = 1;
            return;
        }
        b->data = grown;
        b->cap = cap;
    }
    memcpy(b->data + b->len, s, n);
    b->len += n;
    b->data[b->len] = '\0';
}

static void kr_buf_puts(kr_buf *b, const char *s)
{
    if (s) {
        kr_buf_append(b, s, strlen(s));
    }
}

static void kr_buf_putc(kr_buf *b, char c)
{
    kr_buf_append(b, &c, 1);
}

/* JSON string escaping, for the values we emit. */
static void kr_buf_json_string(kr_buf *b, const char *s, size_t n)
{
    size_t i;
    char tmp[8];
    kr_buf_putc(b, '"');
    for (i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        switch (c) {
        case '"': kr_buf_puts(b, "\\\""); break;
        case '\\': kr_buf_puts(b, "\\\\"); break;
        case '\n': kr_buf_puts(b, "\\n"); break;
        case '\r': kr_buf_puts(b, "\\r"); break;
        case '\t': kr_buf_puts(b, "\\t"); break;
        default:
            if (c < 0x20) {
                snprintf(tmp, sizeof tmp, "\\u%04x", c);
                kr_buf_puts(b, tmp);
            } else {
                kr_buf_putc(b, (char)c);
            }
        }
    }
    kr_buf_putc(b, '"');
}

/* -------------------------------------------------------------- calling the originals */

/*
 * How does the replacement call the function it replaced?
 *
 * Two mistakes were made here before this comment was written, and both crashed the game, so the
 * reasoning is recorded rather than the conclusion alone.
 *
 * **Mistake 1: `dlsym`.** Measured on dyld 4 from a library injected with
 * DYLD_INSERT_LIBRARIES:
 *
 *   dlsym(RTLD_NEXT, "luaL_newstate")        -> NULL   (nothing "next" to us: inserted images
 *                                                        are first in the load order)
 *   dlsym(RTLD_DEFAULT, "luaL_newstate")     -> us     (a flat lookup applies interposition)
 *   dlsym(handle_of_Lua_framework, ...)      -> us     (interposition applies to handle lookups
 *                                                        too)
 *
 * So a direct call does the work: dyld does not interpose an interposing image's own references,
 * and that is the documented mechanism that lets a replacement call the original. It is what
 * `kr_call_*` below do, and it is verified in the harness *and* in the real game (frames ticked
 * through the interposed present).
 *
 * **Mistake 2: a single global recursion flag.** That flag was an `int`, shared by every thread.
 * LÖVE creates Lua states on `love.thread` worker threads, so two threads calling
 * `luaL_newstate` at the same time each saw the other's flag and each concluded it had recursed
 * itself. The guard "gave up" — by returning NULL — and the game then dereferenced a NULL
 * `lua_State`, registering `EXC_BAD_ACCESS at 0x10` inside `lua_pushcclosure` on a thread runner.
 * A crash in the user's game is the worst thing this project can do, and it came from a guard
 * that was meant to be a safety net.
 *
 * Hence the rules now:
 *
 *   1. the depth counter is **thread-local and per function** (`__thread`), so concurrency is
 *      never mistaken for recursion — and so that `luaL_newstate` legitimately reaching
 *      `lua_newstate` inside Lua.framework is not mistaken for it either;
 *   2. the guard is a **tripwire, not a policy**: it logs, and then still calls the original.
 *      "Give up" is not an option here, because the caller has no way to cope with a NULL state;
 *   3. the original is resolved explicitly as a last resort (`kr_resolve_symbol`), so that even a
 *      genuine routing-back has an answer that is not NULL.
 */

/* Primary path: the direct call. A replacement may call the original by name because dyld does
 * not interpose a library's references to the symbols it interposes. */
extern lua_State *luaL_newstate(void);
extern lua_State *lua_newstate(lua_Alloc f, void *ud);
extern void SDL_GL_SwapWindow(void *window);

static __thread int t_depth_luaL_newstate = 0;
static __thread int t_depth_lua_newstate = 0;
static __thread int t_depth_swap_window = 0;

/* Counted, not just logged, so `kr_agent_status_text` can report it: a non-zero value means a call
 * really was routed back to us, which is the condition the resolver exists for. */
static int g_reentry_reports = 0;

/* One-shot diagnostics for the tripwires. A benign race on a flag that only decides whether a log
 * line is written is preferable to a mutex in a path that must stay cheap. */
static int g_reported_luaL_newstate = 0;
static int g_reported_lua_newstate = 0;
static int g_reported_swap_window = 0;

static void *kr_resolve_symbol(const char *symbol, const char *preferred_image, const void *ours);

/* ------------------------------------------------------------------- symbol resolution */

/*
 * The last-resort way to find a function's real address: read the Mach-O symbol table of the
 * image that defines it and add the slide. This bypasses dyld's interposition entirely, because
 * nothing here goes through a binding — it is the file's own table plus the address dyld chose
 * for the image.
 *
 * It is the fallback rather than the primary path because it is the most code and the most
 * assumptions: it wants an unstripped `LC_SYMTAB`, which the shipped `Lua.framework` has
 * (verified with `nm -gU`: 87 `lua_*` symbols). It exists so that a genuine routing-back has an
 * answer that is not NULL, which is the only answer the caller cannot survive.
 */
static void *kr_symbol_in_image(const struct mach_header_64 *header, intptr_t slide,
                                const char *symbol)
{
    const struct load_command *command;
    const struct symtab_command *symtab = NULL;
    const struct nlist_64 *symbols;
    const char *strings;
    uint32_t index;

    if (!header || header->magic != MH_MAGIC_64) {
        return NULL;
    }
    command = (const struct load_command *)((const char *)header + sizeof(struct mach_header_64));
    for (index = 0; index < header->ncmds; index++) {
        if (command->cmd == LC_SYMTAB) {
            symtab = (const struct symtab_command *)command;
            break;
        }
        if (command->cmdsize == 0) {
            return NULL;
        }
        command = (const struct load_command *)((const char *)command + command->cmdsize);
    }
    if (!symtab || symtab->nsyms == 0) {
        return NULL;
    }
    symbols = (const struct nlist_64 *)((const char *)header + symtab->symoff);
    strings = (const char *)header + symtab->stroff;
    for (index = 0; index < symtab->nsyms; index++) {
        const struct nlist_64 *entry = &symbols[index];
        if ((entry->n_type & N_STAB) != 0 || (entry->n_type & N_TYPE) != N_SECT) {
            continue;
        }
        if (entry->n_un.n_strx == 0 || entry->n_un.n_strx >= symtab->strsize) {
            continue;
        }
        if (strcmp(strings + entry->n_un.n_strx, symbol) == 0) {
            return (void *)(slide + (intptr_t)entry->n_value);
        }
    }
    return NULL;
}

static void *kr_resolve_symbol(const char *symbol, const char *preferred_image, const void *ours)
{
    uint32_t count = _dyld_image_count();
    int pass;

    /* Pass 1 looks only at the library that should define it; pass 2 at everything else, in
     * case Lua is linked into the game some other way than the frameworks suggest. */
    for (pass = 0; pass < 2; pass++) {
        uint32_t index;
        for (index = 0; index < count; index++) {
            const char *name = _dyld_get_image_name(index);
            const struct mach_header *header = _dyld_get_image_header(index);
            void *found;

            if (!name || !header) {
                continue;
            }
            /* Never ourselves, and never a second copy of ourselves. */
            if (strstr(name, "kr_agent")) {
                continue;
            }
            if (pass == 0 && (!preferred_image || !strstr(name, preferred_image))) {
                continue;
            }
            found = kr_symbol_in_image((const struct mach_header_64 *)header,
                                       _dyld_get_image_vmaddr_slide(index), symbol);
            if (found && found != ours) {
                return found;
            }
        }
    }

    /* Pass 3: the documented lookups, rejected if they hand back our own replacement. */
    {
        void *found = dlsym(RTLD_NEXT, symbol);
        if (found && found != ours) {
            return found;
        }
        found = dlsym(RTLD_DEFAULT, symbol);
        if (found && found != ours) {
            return found;
        }
    }
    return NULL;
}

static lua_State *g_L = NULL;
static int g_attached = 0;
static unsigned long g_frames_hooked = 0;
static pthread_mutex_t g_reentry = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_attach_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned long g_frame = 0;
static char g_channel[768];
static char g_cmd_path[820];
static char g_out_path[820];
static char g_hb_path[820];
static char g_log_path[820];

static struct timespec g_cmd_time = {0, 0};
static off_t g_cmd_size = -1;
static int g_last_request_id = 0;
static double g_hb_last_touch = 0.0;
static double g_heartbeat_seconds = KR_DEFAULT_HEARTBEAT_SECONDS;
static time_t g_attached_at = 0;

#define KR_LOG_MAX (256 * 1024)

/* Defined with the interposers, below, but needed by kr_agent_attach. */
static int kr_ensure_channel(void);

/*
 * Report whether the last-resort symbol lookup works, once, at attach.
 *
 * The fallback only runs if a call is ever routed back to us, so without this it would be code
 * that has never executed on the machine where it matters most — and it is the difference between
 * a recovered call and a NULL `lua_State`, which is fatal for the host. Checking it here costs
 * three symbol lookups per attach, exercises the resolver on every real run, and means a stripped
 * or renamed symbol is visible in the log *before* anything needs it.
 */
static void kr_log_fallback_check(void);

static void kr_log(const char *fmt, ...)
{
    char line[1024];
    va_list ap;
    int n;
    int fd;
    struct stat st;

    if (!g_log_path[0]) {
        return;
    }
    va_start(ap, fmt);
    n = vsnprintf(line, sizeof line - 32, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    /* Keep the agent log bounded: it is diagnostic, never state. */
    if (stat(g_log_path, &st) == 0 && st.st_size > KR_LOG_MAX) {
        if (truncate(g_log_path, 0) != 0) {
            return;
        }
    }
    fd = open(g_log_path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) {
        return;
    }
    {
        size_t len = strlen(line);
        char suffix[32];
        int m = snprintf(suffix, sizeof suffix, "\n");
        if (write(fd, line, len) < 0) { /* ignored: logging must never fail loudly */
        }
        if (m > 0 && write(fd, suffix, (size_t)m) < 0) {
        }
    }
    close(fd);
}

/* ------------------------------------------------------------------ a flat JSON object */

/*
 * The request is a flat object of strings, numbers and booleans — the CLI writes it, so the
 * shape is ours to fix, and a full JSON parser in C would be more code than the agent. A
 * nested value, an unknown escape or a trailing comma is a parse error, and a parse error is
 * reported as a Lua error in the response rather than guessed at.
 */
typedef struct {
    char *key;
    char *value; /* NULL for null */
    double number;
    int is_number;
    int is_bool;
    int boolean;
    int is_null;
} kr_entry;

typedef struct {
    kr_entry entries[KR_MAX_ENTRIES];
    int count;
} kr_request;

static void kr_request_free(kr_request *req)
{
    int i;
    for (i = 0; i < req->count; i++) {
        kr_free_str(&req->entries[i].key);
        kr_free_str(&req->entries[i].value);
    }
    req->count = 0;
}

static const kr_entry *kr_request_get(const kr_request *req, const char *key)
{
    int i;
    for (i = 0; i < req->count; i++) {
        if (req->entries[i].key && strcmp(req->entries[i].key, key) == 0) {
            return &req->entries[i];
        }
    }
    return NULL;
}

static const char *kr_request_string(const kr_request *req, const char *key)
{
    const kr_entry *e = kr_request_get(req, key);
    return (e && e->value) ? e->value : NULL;
}

static int kr_request_int(const kr_request *req, const char *key, int fallback) __attribute__((unused));
static int kr_request_int(const kr_request *req, const char *key, int fallback)
{
    const kr_entry *e = kr_request_get(req, key);
    if (e && e->is_number) {
        return (int)e->number;
    }
    return fallback;
}

static void kr_skip_ws(const char **p, const char *end)
{
    while (*p < end && (**p == ' ' || **p == '\t' || **p == '\n' || **p == '\r')) {
        (*p)++;
    }
}

static char *kr_parse_string(const char **p, const char *end)
{
    kr_buf b;
    kr_buf_init(&b, 64);
    kr_skip_ws(p, end);
    if (*p >= end || **p != '"') {
        kr_buf_free(&b);
        return NULL;
    }
    (*p)++;
    while (*p < end && **p != '"') {
        if (**p == '\\') {
            (*p)++;
            if (*p >= end) {
                kr_buf_free(&b);
                return NULL;
            }
            switch (**p) {
            case '"': kr_buf_putc(&b, '"'); break;
            case '\\': kr_buf_putc(&b, '\\'); break;
            case '/': kr_buf_putc(&b, '/'); break;
            case 'b': kr_buf_putc(&b, '\b'); break;
            case 'f': kr_buf_putc(&b, '\f'); break;
            case 'n': kr_buf_putc(&b, '\n'); break;
            case 'r': kr_buf_putc(&b, '\r'); break;
            case 't': kr_buf_putc(&b, '\t'); break;
            case 'u': {
                /* Enough for the escapes a JSON encoder produces for control characters. */
                unsigned int code = 0;
                int i;
                if (end - *p < 5) {
                    kr_buf_free(&b);
                    return NULL;
                }
                for (i = 0; i < 4; i++) {
                    char c = (*p)[1 + i];
                    code <<= 4;
                    if (c >= '0' && c <= '9') {
                        code |= (unsigned int)(c - '0');
                    } else if (c >= 'a' && c <= 'f') {
                        code |= (unsigned int)(c - 'a' + 10);
                    } else if (c >= 'A' && c <= 'F') {
                        code |= (unsigned int)(c - 'A' + 10);
                    } else {
                        kr_buf_free(&b);
                        return NULL;
                    }
                }
                *p += 4;
                if (code < 0x80) {
                    kr_buf_putc(&b, (char)code);
                } else if (code < 0x800) {
                    kr_buf_putc(&b, (char)(0xC0 | (code >> 6)));
                    kr_buf_putc(&b, (char)(0x80 | (code & 0x3F)));
                } else {
                    kr_buf_putc(&b, (char)(0xE0 | (code >> 12)));
                    kr_buf_putc(&b, (char)(0x80 | ((code >> 6) & 0x3F)));
                    kr_buf_putc(&b, (char)(0x80 | (code & 0x3F)));
                }
                break;
            }
            default:
                kr_buf_free(&b);
                return NULL;
            }
            (*p)++;
        } else {
            kr_buf_putc(&b, **p);
            (*p)++;
        }
    }
    if (*p >= end || **p != '"' || b.failed) {
        kr_buf_free(&b);
        return NULL;
    }
    (*p)++;
    return b.data;
}

static int kr_parse_value(const char **p, const char *end, kr_entry *e)
{
    const char *start;
    kr_skip_ws(p, end);
    if (*p >= end) {
        return -1;
    }
    if (**p == '"') {
        e->value = kr_parse_string(p, end);
        return e->value ? 0 : -1;
    }
    if (end - *p >= 4 && strncmp(*p, "true", 4) == 0) {
        e->is_bool = 1;
        e->boolean = 1;
        *p += 4;
        return 0;
    }
    if (end - *p >= 5 && strncmp(*p, "false", 5) == 0) {
        e->is_bool = 1;
        e->boolean = 0;
        *p += 5;
        return 0;
    }
    if (end - *p >= 4 && strncmp(*p, "null", 4) == 0) {
        e->is_null = 1;
        *p += 4;
        return 0;
    }
    start = *p;
    {
        char tmp[64];
        size_t n;
        char *stop = NULL;
        while (*p < end && (strchr("-+.eE0123456789", **p) != NULL)) {
            (*p)++;
        }
        n = (size_t)(*p - start);
        if (n == 0 || n >= sizeof tmp) {
            return -1;
        }
        memcpy(tmp, start, n);
        tmp[n] = '\0';
        errno = 0;
        e->number = strtod(tmp, &stop);
        if (stop == tmp) {
            return -1;
        }
        e->is_number = 1;
        return 0;
    }
}

static int kr_parse_request(const char *text, size_t len, kr_request *req)
{
    const char *p = text;
    const char *end = text + len;
    req->count = 0;
    kr_skip_ws(&p, end);
    if (p >= end || *p != '{') {
        return -1;
    }
    p++;
    kr_skip_ws(&p, end);
    if (p < end && *p == '}') {
        return 0;
    }
    while (p < end) {
        kr_entry *e;
        if (req->count >= KR_MAX_ENTRIES) {
            return -1;
        }
        e = &req->entries[req->count];
        memset(e, 0, sizeof *e);
        e->key = kr_parse_string(&p, end);
        if (!e->key) {
            return -1;
        }
        kr_skip_ws(&p, end);
        if (p >= end || *p != ':') {
            return -1;
        }
        p++;
        if (kr_parse_value(&p, end, e) != 0) {
            return -1;
        }
        req->count++;
        kr_skip_ws(&p, end);
        if (p < end && *p == ',') {
            p++;
            continue;
        }
        if (p < end && *p == '}') {
            p++;
            break;
        }
        return -1;
    }
    return 0;
}

/* ----------------------------------------------------------------- running a snippet */

static int g_in_snippet = 0;

static void kr_watchdog(lua_State *L, lua_Debug *ar)
{
    (void)ar;
    luaL_error(L, "krcheat: snippet exceeded its instruction budget (possible loop)");
}

/*
 * Turn the JIT off for this chunk, recursively, before running it.
 *
 * This is not a micro-optimisation and it is not optional — it is the difference between the
 * watchdog working and not working. Measured on the game's own LuaJIT 2.1 (see
 * tests/test_agent_integration.py, which pins all of this):
 *
 *   `while i < 1000000000 do i = i + 1 end`   finished in 2.5 s and never hit the hook;
 *   `for i = 1, 1000000000 do end`            finished in 0.9 s and never hit the hook;
 *   `while true do end`                       never returned, and the hook never fired.
 *
 * The reason is that a compiled trace never returns to the interpreter's dispatch loop, and
 * the instruction-count hook is only consulted there. So a snippet that loops runs at full
 * JIT speed and is uninterruptible — precisely the failure §11.6 calls the one thing the
 * design cannot undo.
 *
 * `jit.off(chunk, true)` marks the chunk and the prototypes defined inside it, and nothing
 * else. The cost is that our own snippet runs interpreted; the game's code keeps its JIT, its
 * traces are not flushed, and a snippet is a few dozen bytecodes. Unlike a global
 * `jit.off()`, this is safe to call while the game has traces running underneath us, which it
 * does: we are reached from C, below a live Lua stack.
 *
 * Plain Lua 5.1 has no `jit` table; there the count hook works on its own, so this is a
 * no-op and is skipped silently.
 */
static void kr_jit_off_for_chunk(lua_State *L, int chunk_index)
{
    int top = lua_gettop(L);
    /* Resolve the index to an absolute one first. A relative index handed in by the caller is
     * no longer valid once this function pushes anything, and passing a stale -1 made this
     * mark itself instead of the chunk — the watchdog then looked broken while appearing to
     * be installed. */
    int chunk = chunk_index < 0 ? top + chunk_index + 1 : chunk_index;

    lua_getglobal(L, "jit");
    if (lua_type(L, -1) == KR_LUA_TTABLE) {
        lua_getfield(L, -1, "off");
        if (lua_type(L, -1) == KR_LUA_TFUNCTION) {
            lua_pushvalue(L, chunk);
            lua_pushboolean(L, 1); /* recursive */
            if (lua_pcall(L, 2, 0, 0) != LUA_OK) {
                const char *message = lua_tolstring(L, -1, NULL);
                kr_log("[krcheat] jit.off failed: %s", message ? message : "?");
            }
        }
    }
    lua_settop(L, top);
}

/*
 * Runs `code` once and returns its result as a heap string. The snippet is expected to
 * `return` a JSON string (§11.4 — the snippet encodes its own result, so nothing here has to
 * marshal Lua values). On failure, `*error` is set to a message and NULL is returned.
 */
static char *kr_run_snippet(const char *code, const char *name, char **error)
{
    lua_State *L = g_L;
    int status;
    char *result = NULL;
    const char *text;
    size_t len = 0;

    if (error) {
        *error = NULL;
    }
    if (!L || !code) {
        if (error) {
            *error = kr_strdup("no Lua state");
        }
        return NULL;
    }

    lua_sethook(L, kr_watchdog, LUA_MASKCOUNT, (int)KR_INSTRUCTION_BUDGET);
    status = luaL_loadbuffer(L, code, strlen(code), name ? name : "=(krcheat)");
    if (status != LUA_OK) {
        text = lua_tolstring(L, -1, &len);
        if (error) {
            *error = text ? kr_strdup(text) : kr_strdup("load error");
        }
        lua_pop(L, 1);
        lua_sethook(L, NULL, 0, 0);
        return NULL;
    }
    /* Before the chunk runs, and before the hook can fire: see kr_jit_off_for_chunk. */
    kr_jit_off_for_chunk(L, -1);
    status = lua_pcall(L, 0, 1, 0);
    if (status != LUA_OK) {
        text = lua_tolstring(L, -1, &len);
        if (error) {
            *error = text ? kr_strdup(text) : kr_strdup("runtime error");
        }
        lua_pop(L, 1);
        lua_sethook(L, NULL, 0, 0);
        return NULL;
    }
    text = lua_tolstring(L, -1, &len);
    if (text) {
        result = (char *)malloc(len + 1);
        if (result) {
            memcpy(result, text, len);
            result[len] = '\0';
        }
    }
    lua_pop(L, 1);
    lua_sethook(L, NULL, 0, 0);
    return result;
}

/* -------------------------------------------------------------------- the overrides */

typedef struct {
    int used;
    char key[KR_KEY_MAX];
    char *code;
    char *capture;
    char *restore;
    char *saved;
    int applied;
    unsigned long last_frame;
} kr_override;

static kr_override g_overrides[KR_MAX_OVERRIDES];

static kr_override *kr_override_find(const char *key)
{
    int i;
    for (i = 0; i < KR_MAX_OVERRIDES; i++) {
        if (g_overrides[i].used && strcmp(g_overrides[i].key, key) == 0) {
            return &g_overrides[i];
        }
    }
    return NULL;
}

static kr_override *kr_override_free_slot(void)
{
    int i;
    for (i = 0; i < KR_MAX_OVERRIDES; i++) {
        if (!g_overrides[i].used) {
            return &g_overrides[i];
        }
    }
    return NULL;
}

static void kr_override_clear(kr_override *o, const char *why)
{
    char *error = NULL;
    char *ignored;

    if (!o || !o->used) {
        return;
    }
    /*
     * §11.7.3: "clear restores the stored value and removes the override. It does not merely
     * stop enforcing — otherwise the last written value silently persists and `off` would
     * appear to do nothing."
     *
     * The captured originals never pass through JSON. They live in a Lua table keyed by
     * override key (`__krcheat_saved`) as real references, so a table or a function can be
     * restored exactly, and — more importantly — the restore path needs no `lib/json`, no
     * decoding step and no allocation beyond the snippet itself. Restore is the one operation
     * that must not fail for a mundane reason.
     *
     * `__krcheat_key` is how the restore snippet learns which key it is restoring.
     */
    if (o->restore && o->restore[0]) {
        lua_pushstring(g_L, o->key);
        lua_setglobal(g_L, "__krcheat_key");
        ignored = kr_run_snippet(o->restore, "=krcheat-restore", &error);
        kr_free_str(&ignored);
        lua_pushnil(g_L);
        lua_setglobal(g_L, "__krcheat_key");
        if (error) {
            kr_log("[krcheat] restore failed for key=%s: %s", o->key, error);
            kr_free_str(&error);
        }
    }
    kr_log("[krcheat] override cleared key=%s (%s)", o->key, why ? why : "requested");
    kr_free_str(&o->code);
    kr_free_str(&o->capture);
    kr_free_str(&o->restore);
    kr_free_str(&o->saved);
    memset(o, 0, sizeof *o);
    lua_gc(g_L, 2 /* LUA_GCCOLLECT */, 0);
}

static void kr_overrides_clear_all(const char *why)
{
    int i;
    for (i = 0; i < KR_MAX_OVERRIDES; i++) {
        if (g_overrides[i].used) {
            kr_override_clear(&g_overrides[i], why);
        }
    }
}

/* ------------------------------------------------------------------ the channel files */

static int kr_mkdir_p(const char *path)
{
    char tmp[768];
    size_t len;
    size_t i;

    len = strlen(path);
    if (len == 0 || len >= sizeof tmp) {
        return -1;
    }
    memcpy(tmp, path, len + 1);
    if (tmp[len - 1] == '/') {
        tmp[len - 1] = '\0';
    }
    for (i = 1; i < len; i++) {
        if (tmp[i] == '/') {
            tmp[i] = '\0';
            if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
                return -1;
            }
            tmp[i] = '/';
        }
    }
    if (mkdir(tmp, 0755) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

static void kr_join(char *dst, size_t cap, const char *dir, const char *name)
{
    snprintf(dst, cap, "%s/%s", dir, name);
}

static char *kr_read_file(const char *path, size_t cap, size_t *out_len)
{
    int fd;
    char *data;
    ssize_t n;
    struct stat st;

    *out_len = 0;
    if (stat(path, &st) != 0 || st.st_size <= 0 || (size_t)st.st_size >= cap) {
        return NULL;
    }
    data = (char *)malloc((size_t)st.st_size + 1);
    if (!data) {
        return NULL;
    }
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        free(data);
        return NULL;
    }
    n = read(fd, data, (size_t)st.st_size);
    close(fd);
    if (n <= 0) {
        free(data);
        return NULL;
    }
    data[n] = '\0';
    *out_len = (size_t)n;
    return data;
}

/* Write-and-rename, so a reader never sees half a file (§11.3). */
static int kr_write_atomic(const char *path, const char *data, size_t len)
{
    char tmp[860];
    int fd;
    ssize_t written;

    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return -1;
    }
    written = write(fd, data, len);
    if (written < 0 || (size_t)written != len) {
        close(fd);
        unlink(tmp);
        return -1;
    }
    if (fsync(fd) != 0) {
        /* not fatal */
    }
    close(fd);
    if (rename(tmp, path) != 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

static void kr_touch_heartbeat(void)
{
    if (!g_hb_path[0]) {
        return;
    }
    if (utimes(g_hb_path, NULL) != 0) {
        int fd = open(g_hb_path, O_WRONLY | O_CREAT, 0644);
        if (fd >= 0) {
            close(fd);
            utimes(g_hb_path, NULL);
        }
    }
    g_hb_last_touch = kr_now();
}

static int kr_write_response(int id, const char *key, int ok, const char *result, const char *error,
                             double ms, int truncated)
{
    kr_buf b;
    char num[64];
    int rc;

    kr_buf_init(&b, 1024);
    if (b.failed) {
        return -1;
    }
    kr_buf_puts(&b, "{\"id\":");
    snprintf(num, sizeof num, "%d", id);
    kr_buf_puts(&b, num);
    kr_buf_puts(&b, ",\"ok\":");
    kr_buf_puts(&b, ok ? "true" : "false");
    if (key) {
        kr_buf_puts(&b, ",\"key\":");
        kr_buf_json_string(&b, key, strlen(key));
    }
    if (result) {
        kr_buf_puts(&b, ",\"result\":");
        kr_buf_json_string(&b, result, strlen(result));
    }
    if (error) {
        kr_buf_puts(&b, ",\"error\":");
        kr_buf_json_string(&b, error, strlen(error));
    }
    if (truncated) {
        kr_buf_puts(&b, ",\"truncated\":true");
    }
    snprintf(num, sizeof num, "%.3f", ms);
    kr_buf_puts(&b, ",\"ms\":");
    kr_buf_puts(&b, num);
    kr_buf_puts(&b, "}\n");

    if (b.len > KR_MAX_RESPONSE) {
        /* Truncate rather than fill the channel: the marker tells the CLI what happened. */
        const char tail[] = ",\"truncated\":true,\"result\":null}\n";
        kr_buf_free(&b);
        kr_buf_init(&b, sizeof tail + 64);
        kr_buf_puts(&b, "{\"id\":");
        snprintf(num, sizeof num, "%d", id);
        kr_buf_puts(&b, num);
        kr_buf_puts(&b, ",\"ok\":false,\"error\":\"response exceeded the size cap\"");
        kr_buf_puts(&b, tail);
        truncated = 1;
    }
    rc = kr_write_atomic(g_out_path, b.data ? b.data : "", b.len);
    kr_buf_free(&b);
    return rc;
}

/* ------------------------------------------------------------------ request handling */

static void kr_handle_request(const char *text, size_t len)
{
    kr_request req;
    const kr_entry *id_entry;
    const char *mode;
    const char *key;
    const char *code;
    const char *capture;
    const char *restore;
    int id;
    double started = kr_now();
    char *result = NULL;
    char *error = NULL;
    int ok = 0;

    if (kr_parse_request(text, len, &req) != 0) {
        kr_request_free(&req);
        kr_log("[krcheat] bad request: parse error");
        kr_write_response(-1, NULL, 0, NULL, "bad request: expected a flat JSON object", 0.0, 0);
        return;
    }

    id_entry = kr_request_get(&req, "id");
    id = id_entry && id_entry->is_number ? (int)id_entry->number : 0;
    g_last_request_id = id;
    mode = kr_request_string(&req, "mode");
    key = kr_request_string(&req, "key");
    code = kr_request_string(&req, "code");
    capture = kr_request_string(&req, "capture");
    restore = kr_request_string(&req, "restore");
    {
        const kr_entry *hb = kr_request_get(&req, "heartbeat_seconds");
        if (hb && hb->is_number && hb->number > 0.5) {
            g_heartbeat_seconds = hb->number;
        }
    }

    if (!mode) {
        error = kr_strdup("request has no mode");
    } else if (strcmp(mode, "once") == 0) {
        if (!code) {
            error = kr_strdup("mode 'once' needs code");
        } else {
            result = kr_run_snippet(code, "=krcheat-once", &error);
            ok = (error == NULL);
        }
    } else if (strcmp(mode, "always") == 0) {
        kr_override *slot;
        if (!key) {
            error = kr_strdup("mode 'always' needs a key");
        } else if (strlen(key) >= KR_KEY_MAX) {
            error = kr_strdup("key is too long");
        } else if (!code) {
            error = kr_strdup("mode 'always' needs code");
        } else {
            /* Replace semantics (§11.2): clear the old one first, so the new capture reads
             * the values the game actually has rather than the ones we were forcing. */
            kr_override *existing = kr_override_find(key);
            if (existing) {
                kr_override_clear(existing, "replaced");
            }
            slot = kr_override_free_slot();
            if (!slot) {
                error = kr_strdup("too many overrides");
            } else {
                memset(slot, 0, sizeof *slot);
                slot->used = 1;
                snprintf(slot->key, sizeof slot->key, "%s", key);
                slot->code = kr_strdup(code);
                slot->capture = kr_strdup(capture ? capture : "");
                slot->restore = kr_strdup(restore ? restore : "");
                if (capture && capture[0]) {
                    /* §11.7.1: read the current values *before* the first write, or the
                     * capture records the value we forced and `off` restores the cheat. */
                    char *capture_error = NULL;
                    lua_pushstring(g_L, key);
                    lua_setglobal(g_L, "__krcheat_key");
                    slot->saved = kr_run_snippet(capture, "=krcheat-capture", &capture_error);
                    lua_pushnil(g_L);
                    lua_setglobal(g_L, "__krcheat_key");
                    if (capture_error) {
                        kr_log("[krcheat] capture failed key=%s: %s", key, capture_error);
                        kr_free_str(&capture_error);
                    }
                }
                result = kr_run_snippet(slot->code, "=krcheat-always", &error);
                ok = (error == NULL);
                slot->applied = ok;
                slot->last_frame = g_frame;
                if (ok) {
                    kr_log("[krcheat] override registered key=%s (%s)", key,
                           capture ? "captured" : "not captured");
                    kr_touch_heartbeat();
                }
            }
        }
    } else if (strcmp(mode, "clear") == 0) {
        kr_override *slot;
        if (!key) {
            error = kr_strdup("mode 'clear' needs a key");
        } else if (strcmp(key, "*") == 0) {
            kr_overrides_clear_all("requested");
            ok = 1;
        } else {
            slot = kr_override_find(key);
            if (!slot) {
                /* Not an error: clearing something that is not set is what `off` should be. */
                ok = 1;
                result = kr_strdup("{\"cleared\":false}");
            } else {
                kr_override_clear(slot, "requested");
                ok = 1;
                result = kr_strdup("{\"cleared\":true}");
            }
        }
    } else if (strcmp(mode, "status") == 0) {
        /* §11.7.5: "what is active right now" must be answerable. The agent owns the override
         * table, so it answers from its own state rather than being asked to introspect Lua. */
        int i;
        int first = 1;
        char number[32];
        kr_buf b;
        kr_buf_init(&b, 512);
        kr_buf_puts(&b, "{\"overrides\":[");
        for (i = 0; i < KR_MAX_OVERRIDES; i++) {
            kr_override *o = &g_overrides[i];
            if (!o->used) {
                continue;
            }
            if (!first) {
                kr_buf_putc(&b, ',');
            }
            first = 0;
            kr_buf_puts(&b, "{\"key\":");
            kr_buf_json_string(&b, o->key, strlen(o->key));
            kr_buf_puts(&b, ",\"has_capture\":");
            kr_buf_puts(&b, (o->capture && o->capture[0]) ? "true" : "false");
            kr_buf_puts(&b, ",\"applied\":");
            kr_buf_puts(&b, o->applied ? "true" : "false");
            snprintf(number, sizeof number, "%lu", g_frame - o->last_frame);
            kr_buf_puts(&b, ",\"frames_since_applied\":");
            kr_buf_puts(&b, number);
            if (o->saved) {
                kr_buf_puts(&b, ",\"saved\":");
                kr_buf_json_string(&b, o->saved, strlen(o->saved));
            }
            kr_buf_putc(&b, '}');
        }
        kr_buf_puts(&b, "]}");
        result = b.data;
        b.data = NULL; /* hand ownership to `result` */
        kr_buf_free(&b);
        ok = 1;
    } else {
        error = kr_strdup("unknown mode");
    }

    if (!ok && !error) {
        error = kr_strdup("snippet returned no result");
    }
    kr_write_response(id, key, ok, ok ? (result ? result : "{\"ok\":true}") : NULL, error,
                      (kr_now() - started) * 1000.0, 0);
    if (error) {
        kr_log("[krcheat] request id=%d mode=%s failed: %s", id, mode ? mode : "?", error);
    }
    kr_free_str(&result);
    kr_free_str(&error);
    kr_request_free(&req);
}

static void kr_apply_overrides(void)
{
    int i;
    for (i = 0; i < KR_MAX_OVERRIDES; i++) {
        kr_override *o = &g_overrides[i];
        char *error = NULL;
        char *ignored;
        if (!o->used || !o->code) {
            continue;
        }
        /* Re-registering every frame is the point: the game writes these fields itself. */
        ignored = kr_run_snippet(o->code, "=krcheat-frame", &error);
        kr_free_str(&ignored);
        if (error) {
            kr_log("[krcheat] override key=%s errored: %s", o->key, error);
            kr_free_str(&error);
            kr_override_clear(o, "error");
        }
        o->last_frame = g_frame;
    }
}

static void kr_check_heartbeat(void)
{
    int i;
    int any = 0;
    struct stat st;
    double age;

    for (i = 0; i < KR_MAX_OVERRIDES; i++) {
        if (g_overrides[i].used) {
            any = 1;
            break;
        }
    }
    if (!any) {
        return;
    }
    /* The age comes from the *file*, because whoever owns the override may not be this
     * process: a detached keeper renounces the heartbeat while the CLI has exited. */
    if (stat(g_hb_path, &st) == 0) {
        age = difftime(time(NULL), st.st_mtime);
    } else {
        age = difftime(time(NULL), g_attached_at);
    }
    if (age < 0.0) {
        age = 0.0;
    }
    if (age > g_heartbeat_seconds) {
        /* §11.7: a crashed or forgotten CLI must not leave the game modified forever. */
        kr_log("[krcheat] heartbeat stale (%.1fs > %.1fs): clearing every override",
               age, g_heartbeat_seconds);
        kr_overrides_clear_all("heartbeat lost");
    }
}

static void kr_poll_channel(void)
{
    struct stat st;
    char *text;
    size_t len = 0;

    if (stat(g_cmd_path, &st) != 0) {
        return;
    }
    /*
     * The change detector is (mtime, size) — with mtime in nanoseconds.
     *
     * Seconds are not enough, and the failure mode is silent: two requests written in the same
     * second whose bodies are the same length look identical, so the agent never reads the
     * second one and the caller times out. That is easy to hit in normal use (`live status`
     * twice in a row, or any command with a fixed payload) and it is what a test caught here.
     * Nanosecond timestamps on APFS make a false match effectively impossible.
     */
    if (st.st_mtimespec.tv_sec == g_cmd_time.tv_sec &&
        st.st_mtimespec.tv_nsec == g_cmd_time.tv_nsec &&
        st.st_size == g_cmd_size) {
        return;
    }
    g_cmd_time = st.st_mtimespec;
    g_cmd_size = st.st_size;
    if (st.st_size <= 0 || (size_t)st.st_size >= KR_MAX_REQUEST) {
        kr_log("[krcheat] request rejected: %lld bytes (cap %d)", (long long)st.st_size, KR_MAX_REQUEST);
        kr_write_response(-1, NULL, 0, NULL, "request too large or empty", 0.0, 0);
        return;
    }
    text = kr_read_file(g_cmd_path, KR_MAX_REQUEST, &len);
    if (!text) {
        return;
    }
    kr_handle_request(text, len);
    free(text);
}

/* ------------------------------------------------------------------------- the frame */

void kr_agent_tick(void)
{
    g_frame++;

    /* Re-entrancy guard: a snippet that calls back into the hook is dropped, never queued. */
    if (pthread_mutex_trylock(&g_reentry) != 0) {
        return;
    }
    if (!g_L) {
        pthread_mutex_unlock(&g_reentry);
        return;
    }
    if (g_in_snippet) {
        pthread_mutex_unlock(&g_reentry);
        return;
    }
    g_in_snippet = 1;

    kr_poll_channel();
    kr_apply_overrides();
    kr_check_heartbeat();

    if (g_frame % KR_LOG_EVERY_FRAMES == 0) {
        int count = 0;
        int i;
        for (i = 0; i < KR_MAX_OVERRIDES; i++) {
            if (g_overrides[i].used) {
                count++;
            }
        }
        kr_log("[krcheat] frame=%lu overrides=%d", g_frame, count);
    }

    g_in_snippet = 0;
    pthread_mutex_unlock(&g_reentry);
}

/* ------------------------------------------------------------------------- attach */

int kr_agent_attach(lua_State *L)
{
    if (!L || g_attached) {
        return 0;
    }
    /* LÖVE creates states on worker threads, so this can be reached concurrently. The mutex
     * makes "first one wins" true rather than probable; anything else would attach the agent to
     * a state that is about to be handed to a thread and then read frames on another. */
    pthread_mutex_lock(&g_attach_lock);
    if (g_attached) {
        pthread_mutex_unlock(&g_attach_lock);
        return 0;
    }
    g_L = L;
    g_attached = 1;

    if (kr_ensure_channel() != 0) {
        /* No channel, no agent: log nowhere and stay out of the way. */
        g_log_path[0] = '\0';
        pthread_mutex_unlock(&g_attach_lock);
        return -1;
    }
    g_hb_last_touch = kr_now();
    g_attached_at = time(NULL);
    lua_sethook(L, NULL, 0, 0); /* make sure no stale hook from a previous attach survives */
    kr_log("[krcheat] agent %s attached: pid=%d, state=%p, channel=%s",
           KR_AGENT_VERSION, (int)getpid(), (void *)L, g_channel);
    kr_log_fallback_check();
    pthread_mutex_unlock(&g_attach_lock);
    return 0;
}

lua_State *kr_agent_state(void)
{
    return g_L;
}

const char *kr_agent_channel_dir(void)
{
    return g_channel;
}

const char *kr_agent_status_text(char *dst, size_t cap)
{
    int overrides = 0;
    int i;
    for (i = 0; i < KR_MAX_OVERRIDES; i++) {
        if (g_overrides[i].used) {
            overrides++;
        }
    }
    snprintf(dst, cap, "attached=%s frames=%lu overrides=%d reentry_skips=%d",
             g_attached ? "yes" : "no", g_frames_hooked, overrides, g_reentry_reports);
    return dst;
}

void kr_agent_shutdown(void)
{
    kr_overrides_clear_all("shutdown");
    g_L = NULL;
    g_attached = 0;
}

/* --------------------------------------------------------------- the interposers */

/*
 * Every interposer here has the same shape, and the shape is the point:
 *
 *   - the depth counter is `__thread` and belongs to *one* function, so concurrent calls from
 *     different threads are ordinary calls, and `luaL_newstate` reaching `lua_newstate` inside
 *     Lua.framework is ordinary nesting;
 *   - the tripwire logs and then still calls the original. It never withholds the result,
 *     because the caller cannot survive a NULL `lua_State` — see the note above.
 */

/* The frame hook. Our part runs before the present so an `always` snippet takes effect in the
 * frame it was registered for; the original present is still called, always. */
static void kr_swap_window(void *window)
{
    if (t_depth_swap_window++ > 0) {
        /* Genuine routing-back: the frame must still be presented, so resolve the original
         * rather than returning (a window that never presents looks like a frozen game). */
        void (*real)(void *) =
            (void (*)(void *))kr_resolve_symbol("_SDL_GL_SwapWindow", "SDL2", (const void *)kr_swap_window);
        t_depth_swap_window--;
        if (!g_reported_swap_window) {
            g_reported_swap_window = 1;
            kr_log("[krcheat] SDL_GL_SwapWindow was routed back to us; using the resolved "
                   "original (%s)", real ? "found" : "NOT FOUND");
        }
        g_reentry_reports++;
        if (real) {
            real(window);
            return;
        }
        return;
    }
    g_frames_hooked++;
    kr_agent_tick();
    SDL_GL_SwapWindow(window);
    t_depth_swap_window--;
}

static lua_State *kr_newstate(void)
{
    lua_State *L;
    lua_State *(*real)(void);

    if (t_depth_luaL_newstate++ > 0) {
        real = (lua_State * (*)(void)) kr_resolve_symbol("_luaL_newstate", "Lua.framework",
                                                        (const void *)kr_newstate);
        t_depth_luaL_newstate--;
        if (!g_reported_luaL_newstate) {
            g_reported_luaL_newstate = 1;
            kr_log("[krcheat] luaL_newstate was routed back to us; using the resolved original "
                   "(%s)", real ? "found" : "NOT FOUND");
        }
        g_reentry_reports++;
        if (!real) {
            kr_log("[krcheat] luaL_newstate recursed and the original could not be found");
            return NULL; /* unreachable in practice; reported loudly if ever reached */
        }
        L = real();
    } else {
        L = luaL_newstate();
        t_depth_luaL_newstate--;
    }
    if (L && !g_attached) {
        kr_agent_attach(L);
    }
    return L;
}

static lua_State *kr_newstate_with_alloc(lua_Alloc alloc, void *ud)
{
    lua_State *L;
    lua_State *(*real)(lua_Alloc, void *);

    if (t_depth_lua_newstate++ > 0) {
        real = (lua_State * (*)(lua_Alloc, void *)) kr_resolve_symbol(
            "_lua_newstate", "Lua.framework", (const void *)kr_newstate_with_alloc);
        t_depth_lua_newstate--;
        if (!g_reported_lua_newstate) {
            g_reported_lua_newstate = 1;
            kr_log("[krcheat] lua_newstate was routed back to us; using the resolved original "
                   "(%s)", real ? "found" : "NOT FOUND");
        }
        g_reentry_reports++;
        if (!real) {
            kr_log("[krcheat] lua_newstate recursed and the original could not be found");
            return NULL; /* unreachable in practice; reported loudly if ever reached */
        }
        L = real(alloc, ud);
    } else {
        L = lua_newstate(alloc, ud);
        t_depth_lua_newstate--;
    }
    if (L && !g_attached) {
        kr_agent_attach(L);
    }
    return L;
}

/*
 * Report whether the last-resort symbol lookup works, once, at attach.
 *
 * The fallback only runs if a call is ever routed back to us, so without this it would be code
 * that has never executed on the machine where it matters most — and it is the difference between
 * a recovered call and a NULL `lua_State`, which is fatal for the host. Checking it here costs
 * three symbol lookups per attach, exercises the resolver on every real run, and means a stripped
 * or renamed symbol shows up in the log *before* anything needs it.
 */
static void kr_log_fallback_check(void)
{
    void *newstate = kr_resolve_symbol("_luaL_newstate", "Lua.framework", (const void *)kr_newstate);
    void *newstate_alloc = kr_resolve_symbol("_lua_newstate", "Lua.framework",
                                            (const void *)kr_newstate_with_alloc);
    void *swap = kr_resolve_symbol("_SDL_GL_SwapWindow", "SDL2", (const void *)kr_swap_window);
    kr_log("[krcheat] fallback resolution: luaL_newstate=%s lua_newstate=%s swap=%s",
           newstate ? "yes" : "NO", newstate_alloc ? "yes" : "NO", swap ? "yes" : "NO");
    if (!newstate || !newstate_alloc || !swap) {
        kr_log("[krcheat] WARNING: a call routed back to us may not be recoverable on this "
               "build; its symbols are not where they were expected");
    }
}

/*
 * `__DATA,__interpose` is what makes this work without patching anything: dyld rebinds the
 * game's references to these symbols to the functions above. Apple's own documentation
 * recommends it for exactly this, and it needs no root and no re-signing of the game.
 *
 * Deliberately small: two ways to catch the state, one way to catch a frame. Every extra
 * interposed symbol is another chance to break the host.
 */
__attribute__((used, section("__DATA,__interpose"))) static const struct {
    const void *replacement;
    const void *replacee;
} kr_interposers[] = {
    {(const void *)kr_newstate, (const void *)luaL_newstate},
    {(const void *)kr_newstate_with_alloc, (const void *)lua_newstate},
    {(const void *)kr_swap_window, (const void *)SDL_GL_SwapWindow},
};

/* Create the channel directory, and log the injection itself.
 *
 * Doing this at load rather than at state capture is deliberate: spike S3 asks "can we load
 * a dylib into the game, and does it run?", and the answer has to be observable even if the
 * state is never captured. */
static int kr_ensure_channel(void)
{
    char tmp[700];
    const char *env;

    if (g_log_path[0]) {
        return 0;
    }
    env = getenv("TMPDIR");
    snprintf(tmp, sizeof tmp, "%s", (env && env[0]) ? env : "/tmp");
    /* macOS sets TMPDIR with a trailing slash; drop it so the paths we build are byte-identical
     * to the ones the CLI builds from tempfile.gettempdir(). */
    {
        size_t len = strlen(tmp);
        while (len > 1 && tmp[len - 1] == '/') {
            tmp[--len] = '\0';
        }
    }
    snprintf(g_channel, sizeof g_channel, "%s/krcheat/%d", tmp, (int)getpid());
    if (kr_mkdir_p(g_channel) != 0) {
        return -1;
    }
    kr_join(g_cmd_path, sizeof g_cmd_path, g_channel, "cmd.json");
    kr_join(g_out_path, sizeof g_out_path, g_channel, "out.json");
    kr_join(g_hb_path, sizeof g_hb_path, g_channel, "hb");
    kr_join(g_log_path, sizeof g_log_path, g_channel, "log");
    return 0;
}

__attribute__((constructor)) static void kr_agent_loaded(void)
{
    if (kr_ensure_channel() == 0) {
        kr_log("[krcheat] agent %s injected into pid=%d, waiting for a lua_State",
               KR_AGENT_VERSION, (int)getpid());
    }
}
