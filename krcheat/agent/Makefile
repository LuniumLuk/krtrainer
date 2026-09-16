# kr_agent — build rules (foundation §9.3, §13)
#
#   make                 the agent dylib, the probe dylib and the test harness
#   make ARCHS=...       override the agent's architectures
#   make clean
#
# The agent is built with `-undefined dynamic_lookup`, so its references to the Lua C API and
# to SDL stay unresolved until load time and bind to the copies the game already has. That is
# the whole point: the agent must not drag a second LuaJIT into the process. It is also why
# `codesign -s -` matters — an arm64 dylib must be signed to load at all, and an ad-hoc
# signature is enough (§9.3).
#
# The harness is different: it links Lua.framework the way LÖVE does, because its job is to
# prove that a plain call to `luaL_newstate` from another image reaches an injected agent.

CC ?= clang
FRAMEWORKS ?= $(HOME)/Library/Application Support/Steam/steamapps/common/Kingdom Rush/Kingdom Rush.app/Contents/Frameworks

WARNINGS = -Wall -Wextra -Wno-unused-parameter
CFLAGS ?= -O2 $(WARNINGS) -fPIC
HOST_ARCH := -arch $(shell uname -m)
# Universal by default: the bundle is, and an ad-hoc signature covers both slices.
ARCHS ?= -arch x86_64 -arch arm64

AGENT = kr_agent.dylib
PROBE = kr_probe.dylib
HARNESS = kr_harness

.PHONY: all clean check

all: $(AGENT) $(PROBE) $(HARNESS)

$(AGENT): kr_agent.c kr_agent.h
	$(CC) $(CFLAGS) -I. $(ARCHS) -dynamiclib -install_name @rpath/$(AGENT) \
		-undefined dynamic_lookup -o $@ kr_agent.c
	codesign -s - --force --timestamp=none $@
	@echo "built $@ ($(ARCHS))"

$(PROBE): probe_caller.c kr_agent.h
	$(CC) $(CFLAGS) -I. $(HOST_ARCH) -dynamiclib -undefined dynamic_lookup \
		-o $@ probe_caller.c

$(HARNESS): harness.c kr_agent.h
	$(CC) $(CFLAGS) -I. $(HOST_ARCH) -o $@ harness.c \
		-F"$(FRAMEWORKS)" -framework Lua -framework SDL2 -Wl,-rpath,"$(FRAMEWORKS)" \
		-undefined dynamic_lookup

# The agent is loaded with DYLD_INSERT_LIBRARIES; the harness drives frames and the test acts
# as the CLI. Run `python3 -m unittest tests.test_agent` for the whole thing.
check: all
	DYLD_INSERT_LIBRARIES="$(CURDIR)/$(AGENT)" ./$(HARNESS) --seconds 2 --report 'return "ok"'

clean:
	rm -f $(AGENT) $(PROBE) $(HARNESS)
