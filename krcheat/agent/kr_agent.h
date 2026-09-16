/*
 * kr_agent.h — the injected agent's public seam, plus the Lua 5.1 API it needs.
 *
 * There is no `lua.h` on this machine (LuaJIT ships only the dylib), so the handful of
 * entry points the agent uses are declared here, matching Lua 5.1 / LuaJIT 2.1 exactly.
 * The three "missing" symbols from the framework are the classic macros — `lua_getglobal`,
 * `lua_setglobal` and `lua_pushcfunction` are macros over `lua_getfield`/`lua_setfield`/
 * `lua_pushcclosure` with `LUA_GLOBALSINDEX`, so they are defined here the same way.
 *
 * The seam exists so the agent can be driven without the game: `harness.c` links the real
 * Lua.framework, creates a state, and calls `kr_agent_tick()` in a loop while a test acts as
 * the CLI. That is how the channel, the snippets and the override lifecycle are verified.
 */

#ifndef KR_AGENT_H
#define KR_AGENT_H

#include <stddef.h>

/* ------------------------------------------------------------------ Lua types */

typedef struct lua_State lua_State;
typedef struct lua_Debug lua_Debug; /* opaque: the agent never inspects it */
typedef void (*lua_Hook)(lua_State *L, lua_Debug *ar);
typedef void *(*lua_Alloc)(void *ud, void *ptr, size_t osize, size_t nsize);
typedef int (*lua_CFunction)(lua_State *L);

/* Lua 5.1 pseudo-indices (LuaJIT is 5.1-compatible). */
#define LUA_GLOBALSINDEX (-10002)
#define LUA_REGISTRYINDEX (-10000)

/* Debug-hook masks (lua.h). */
#define LUA_MASKCOUNT (1 << 3)
#define LUA_MASKCALL (1 << 0)
#define LUA_MASKRET (1 << 1)
#define LUA_MASKLINE (1 << 2)

#define LUA_OK 0

/* Type tags. */
#define KR_LUA_TNIL 0
#define KR_LUA_TBOOLEAN 1
#define KR_LUA_TNUMBER 3
#define KR_LUA_TSTRING 4
#define KR_LUA_TTABLE 5
#define KR_LUA_TFUNCTION 6

/* ------------------------------------------------------------- Lua API (declared) */

extern lua_State *luaL_newstate(void);
extern void luaL_openlibs(lua_State *L);
extern lua_State *lua_newstate(lua_Alloc f, void *ud);
extern void lua_close(lua_State *L);
extern int luaL_loadbuffer(lua_State *L, const char *buff, size_t sz, const char *name);
extern int lua_pcall(lua_State *L, int nargs, int nresults, int errfunc);
extern int lua_gettop(lua_State *L);
extern void lua_settop(lua_State *L, int idx);
extern void lua_getfield(lua_State *L, int idx, const char *k);
extern void lua_setfield(lua_State *L, int idx, const char *k);
extern void lua_pushnil(lua_State *L);
extern void lua_pushstring(lua_State *L, const char *s);
extern void lua_pushnumber(lua_State *L, double n);
extern void lua_pushboolean(lua_State *L, int b);
extern void lua_pushvalue(lua_State *L, int idx);
extern void lua_pushcclosure(lua_State *L, lua_CFunction fn, int n);
extern int lua_type(lua_State *L, int idx);
extern int lua_toboolean(lua_State *L, int idx);
extern double lua_tonumber(lua_State *L, int idx);
extern const char *lua_tolstring(lua_State *L, int idx, size_t *len);
extern int lua_sethook(lua_State *L, lua_Hook f, int mask, int count);
extern int luaL_error(lua_State *L, const char *fmt, ...);
extern int lua_gc(lua_State *L, int what, int data);

/* Lua 5.1 macros. */
#define lua_getglobal(L, s) lua_getfield((L), LUA_GLOBALSINDEX, (s))
#define lua_setglobal(L, s) lua_setfield((L), LUA_GLOBALSINDEX, (s))
#define lua_pushcfunction(L, f) lua_pushcclosure((L), (f), 0)
#define lua_pop(L, n) lua_settop((L), -(n) - 1)

/* ---------------------------------------------------------------- the agent seam */

/* Attach to a state directly. The interposers call this; the harness may call it too. */
int kr_agent_attach(lua_State *L);

/* The captured state, or NULL before one has been seen. */
lua_State *kr_agent_state(void);

/* One frame's work: poll the channel, apply overrides, honour the heartbeat. */
void kr_agent_tick(void);

/* Release buffers and stop heartbeating. Safe to call twice. */
void kr_agent_shutdown(void);

/* Where the channel lives, for diagnostics: "<tmp>/krcheat/<pid>". */
const char *kr_agent_channel_dir(void);

/* One line of state, for `krcheat live status` and for the harness. Fills `dst` and returns
 * it: "attached=yes frames=57 overrides=1 reentry_skips=0". */
const char *kr_agent_status_text(char *dst, size_t cap);

#endif /* KR_AGENT_H */
