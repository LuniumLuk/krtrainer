/*
 * harness.c — drive the agent without the game.
 *
 * M3's spikes (S3-S5) ask whether a dylib can be loaded, whether the frame hook fires and
 * whether the `lua_State` is captured. Only two of those need the game; everything else —
 * the channel, the protocol, the snippets, the override lifecycle, the encoding — can be
 * exercised against the *real* LuaJIT from the game's own bundle, which is what this does.
 *
 * It links Lua.framework the way LÖVE does, creates a state, builds a small world from a
 * setup script, then ticks the agent until told to stop, reporting the outcome. A Python
 * test plays the CLI: it writes requests into the channel and reads the responses.
 *
 * Usage:
 *   harness --setup FILE [--seconds 6] [--ticks-per-second 240] [--report 'return ...']
 *           [--probe-dylib PATH]
 *
 * Each `--report <lua chunk>` is evaluated after the loop and printed as `REPORT <value>`,
 * which is how a test asserts what the snippets actually did to the world.
 */

#include "kr_agent.h"

#include <dlfcn.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/*
 * A few SDL2 entry points, declared here for the same reason the Lua API is: there are no SDL
 * headers on this machine, and the harness needs only to make a window and present it.
 *
 * The window is deliberately created *without* SDL_WINDOW_OPENGL. `SDL_GL_SwapWindow` checks
 * the window's flags and returns an error immediately for a non-GL window, which means the
 * harness exercises the real call path — our interposer, then the real SDL function, then back
 * — with no GL context, no visible window and no chance of crashing the test run. The frame
 * *hook* is what is under test, not the present itself.
 */
#define SDL_INIT_VIDEO 0x00000020u
#define SDL_WINDOW_HIDDEN 0x00000008u

extern int SDL_Init(unsigned int flags);
extern void *SDL_CreateWindow(const char *title, int x, int y, int w, int h, unsigned int flags);
extern void SDL_DestroyWindow(void *window);
extern void SDL_Quit(void);
extern const char *SDL_GetError(void);
extern void SDL_GL_SwapWindow(void *window);

#define MAX_REPORTS 8

static int run_chunk(lua_State *L, const char *code, const char *name, char **out)
{
    int status;
    const char *text;
    size_t len = 0;
    char *error = NULL;

    if (out) {
        *out = NULL;
    }
    status = luaL_loadbuffer(L, code, strlen(code), name);
    if (status != LUA_OK) {
        text = lua_tolstring(L, -1, &len);
        fprintf(stderr, "harness: %s failed to load: %s\n", name, text ? text : "?");
        lua_pop(L, 1);
        return -1;
    }
    status = lua_pcall(L, 0, 1, 0);
    if (status != LUA_OK) {
        text = lua_tolstring(L, -1, &len);
        fprintf(stderr, "harness: %s failed: %s\n", name, text ? text : "?");
        lua_pop(L, 1);
        return -1;
    }
    text = lua_tolstring(L, -1, &len);
    if (out && text) {
        *out = (char *)malloc(len + 1);
        if (*out) {
            memcpy(*out, text, len);
            (*out)[len] = '\0';
        }
    }
    lua_pop(L, 1);
    (void)error;
    return 0;
}

static char *read_file(const char *path)
{
    FILE *f = fopen(path, "rb");
    long size;
    char *data;
    if (!f) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size <= 0) {
        fclose(f);
        return NULL;
    }
    data = (char *)malloc((size_t)size + 1);
    if (!data) {
        fclose(f);
        return NULL;
    }
    if (fread(data, 1, (size_t)size, f) != (size_t)size) {
        free(data);
        fclose(f);
        return NULL;
    }
    data[size] = '\0';
    fclose(f);
    return data;
}

static void sleep_seconds(double seconds)
{
    struct timespec ts;
    ts.tv_sec = (time_t)seconds;
    ts.tv_nsec = (long)((seconds - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
}

int main(int argc, char **argv)
{
    lua_State *L;
    void *window = NULL;
    const char *setup = NULL;
    const char *probe_dylib = NULL;
    const char *reports[MAX_REPORTS];
    int report_count = 0;
    int use_sdl = 1;
    double seconds = 6.0;
    double ticks_per_second = 240.0;
    double interval;
    double elapsed = 0.0;
    unsigned long ticks = 0;
    const char *capture = "none";
    int i;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--setup") == 0 && i + 1 < argc) {
            setup = argv[++i];
        } else if (strcmp(argv[i], "--probe-dylib") == 0 && i + 1 < argc) {
            probe_dylib = argv[++i];
        } else if (strcmp(argv[i], "--no-sdl") == 0) {
            use_sdl = 0;
        } else if (strcmp(argv[i], "--seconds") == 0 && i + 1 < argc) {
            seconds = atof(argv[++i]);
        } else if (strcmp(argv[i], "--ticks-per-second") == 0 && i + 1 < argc) {
            ticks_per_second = atof(argv[++i]);
        } else if (strcmp(argv[i], "--report") == 0 && i + 1 < argc) {
            if (report_count < MAX_REPORTS) {
                reports[report_count++] = argv[++i];
            }
        } else {
            fprintf(stderr, "harness: unknown argument %s\n", argv[i]);
            return 2;
        }
    }

    L = luaL_newstate();
    if (!L) {
        fprintf(stderr, "harness: luaL_newstate returned NULL\n");
        return 2;
    }
    if (kr_agent_state() == L) {
        capture = "interposed luaL_newstate (this image)";
    }
    luaL_openlibs(L);

    /*
     * If this image's own call was not interposed (dyld does not interpose the image that
     * defines the interpose table), try a *separate* image: a tiny dylib that calls
     * luaL_newstate. Interposition applies between images, so if that is caught, transport A's
     * mechanism works.
     */
    if (strcmp(capture, "none") == 0 && probe_dylib) {
        void *handle = dlopen(probe_dylib, RTLD_NOW);
        if (handle) {
            lua_State *(*probe)(void) = (lua_State * (*)(void)) dlsym(handle, "probe_make_state");
            if (probe) {
                lua_State *other = probe();
                if (kr_agent_state() == other && other != NULL) {
                    capture = "interposed from another image (probe dylib)";
                }
                if (other) {
                    lua_close(other); /* do not leave a second state around */
                }
            }
        }
    }
    if (strcmp(capture, "none") == 0) {
        capture = "direct attach (interposition not observed here)";
    }
    if (kr_agent_state() == NULL) {
        kr_agent_attach(L);
    }

    if (setup) {
        char *code = read_file(setup);
        if (!code) {
            fprintf(stderr, "harness: cannot read setup file %s\n", setup);
            return 2;
        }
        if (run_chunk(L, code, "=setup", NULL) != 0) {
            free(code);
            return 2;
        }
        free(code);
    }

    printf("HARNESS pid=%d capture=%s channel=%s\n", (int)getpid(), capture, kr_agent_channel_dir());
    fflush(stdout);

    /*
     * Prefer a real frame path: SDL_GL_SwapWindow is how the game presents, and calling it is
     * the only way to prove the interposer is wired up. A video driver may be unavailable
     * (no window server, a bare CI box), so fall back to a headless SDL driver and then to
     * calling the tick directly — the tests care which one was used, so the harness says.
     */
    if (use_sdl) {
        if (SDL_Init(SDL_INIT_VIDEO) != 0) {
            setenv("SDL_VIDEODRIVER", "dummy", 1);
            if (SDL_Init(SDL_INIT_VIDEO) != 0) {
                fprintf(stderr, "harness: no SDL video driver (%s)\n", SDL_GetError());
            }
        }
        /*
         * SDL_Init installs handlers for SIGINT and SIGTERM so it can turn them into
         * SDL_QUIT events. A harness that never polls events therefore ignores the signal
         * that a test uses to stop it, and the process outlives the test. Measured, not
         * assumed: this is what left harness processes behind while the tests passed.
         *
         * The same behaviour in the real game is worth knowing about: `krcheat play` cannot
         * end a game with SIGTERM, which is why the transport escalates to SIGKILL.
         */
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        window = SDL_CreateWindow("krcheat harness", 0, 0, 64, 64, SDL_WINDOW_HIDDEN);
        if (!window) {
            fprintf(stderr, "harness: SDL_CreateWindow failed: %s\n", SDL_GetError());
        }
    }
    printf("HARNESS frames=%s\n", window ? "sdl" : "direct");
    fflush(stdout);

    interval = 1.0 / (ticks_per_second > 0.0 ? ticks_per_second : 240.0);
    while (elapsed < seconds) {
        if (window) {
            SDL_GL_SwapWindow(window); /* through the agent's interposer, which ticks */
        } else {
            kr_agent_tick();
        }
        sleep_seconds(interval);
        elapsed += interval;
        ticks++;
    }

    for (i = 0; i < report_count; i++) {
        char *value = NULL;
        if (run_chunk(L, reports[i], "=report", &value) == 0) {
            printf("REPORT %s\n", value ? value : "<nil>");
            free(value);
        } else {
            printf("REPORT <error>\n");
        }
        fflush(stdout);
    }

    {
        char status[256];
        printf("HARNESS status %s\n", kr_agent_status_text(status, sizeof status));
    }
    printf("HARNESS done ticks=%lu\n", ticks);
    fflush(stdout);

    if (window) {
        SDL_DestroyWindow(window);
        SDL_Quit();
    }
    return 0;
}
