#!/bin/sh
# Start a virtual X display, then hand off to the MCP server.
#
# We deliberately do NOT use `xvfb-run`: as PID 1 in a container its Xvfb-ready
# SIGUSR1 handshake races, and when the signal is missed it leaves Xvfb running
# but never execs the app — the server then never binds its port and the
# container sits "up" but dead. Starting Xvfb ourselves and polling for its
# socket is deterministic.
set -eu

DISPLAY_NUM="${GH_DISPLAY:-99}"
SCREEN="${GH_SCREEN:-1366x900x24}"

mkdir -p /tmp/.X11-unix 2>/dev/null || true
rm -f "/tmp/.X${DISPLAY_NUM}-lock" 2>/dev/null || true

Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN}" -nolisten tcp &

# Wait for the X socket to appear before launching Chromium.
i=0
while [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; do
    i=$((i + 1))
    if [ "${i}" -gt 100 ]; then
        echo "entrypoint: Xvfb :${DISPLAY_NUM} did not come up within 10s" >&2
        exit 1
    fi
    sleep 0.1
done

export DISPLAY=":${DISPLAY_NUM}"
# exec so the server becomes PID 1 and receives SIGTERM directly on stop.
exec geizhals-mcp
