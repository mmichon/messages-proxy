#!/bin/bash
# Start the server if it isn't running.
#
# Run from cron, NOT launchd: /usr/sbin/cron already holds Full Disk Access and
# Automation on this Mac, and a process it spawns inherits both, so the server
# reads chat.db and sends via Messages with no TCC prompts. A launchd agent gets
# its own TCC identity and fails with "unable to open database file".
#
# Liveness is checked via a pidfile, not `pgrep -f server.py` — that pattern
# matches any shell whose command line merely mentions the path.
cd "$HOME/src/imsg-web" || exit 1
PIDFILE=imsg-web.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	exit 0
fi
# token file (mode 600, gitignored) gates the server when it's reachable from
# outside the LAN via Tailscale Funnel. Remove the file to go back to open access.
[ -f token ] && export IMSG_TOKEN="$(cat token)"
nohup /usr/bin/python3 server.py >> imsg-web.log 2>&1 &
echo $! > "$PIDFILE"
