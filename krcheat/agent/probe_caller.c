/*
 * probe_caller.c — a second image that calls luaL_newstate, to prove interposition works.
 *
 * dyld does not interpose references made by the image that *defines* the interpose table, so
 * the harness cannot test the mechanism on its own calls. A separate dylib can: its call is an
 * ordinary bind, and if the agent's `__DATA,__interpose` section is doing its job, the
 * replacement runs and captures the state.
 *
 * This file is built into the same dylib as the agent (see the Makefile) and exports one
 * function, which the harness loads at runtime.
 */

#include "kr_agent.h"

lua_State *probe_make_state(void);

lua_State *probe_make_state(void)
{
    return luaL_newstate();
}
