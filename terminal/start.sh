#!/bin/bash
# Two long-lived processes in one container: the interactive web terminal
# and the chat bridge the dashboard talks to. Both are just front-ends onto
# the same Claude Code install and the same ~/.claude login, which is the
# point - log in once in the terminal, the chat panel works too.
#
# bash, not sh: `wait -n` is a bash builtin and dash does not have it.
set -e

python3 -u /opt/bridge.py &
BRIDGE=$!

ttyd --writable --port 7681 --interface 0.0.0.0 \
     --credential "$TTYD_USER:$TTYD_PASS" \
     --max-clients 4 \
     -t titleFixed="opsdeck :: claude code" \
     -t fontSize=14 \
     bash -l &
TTYD=$!

# If either dies, drop the container so docker restarts it cleanly rather
# than leaving half the service up.
wait -n "$BRIDGE" "$TTYD"
kill "$BRIDGE" "$TTYD" 2>/dev/null || true
exit 1
