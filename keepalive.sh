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
cd "$HOME/src/messages-proxy" || exit 1
PIDFILE=messages-proxy.pid
LOG=messages-proxy.log
LOG_MAX=$((5 * 1024 * 1024))

# Cap the log at LOG_MAX plus one rotated copy. Copy-then-truncate rather than mv:
# the running server holds the fd open with O_APPEND, so after truncation it just
# carries on writing at the new end; a mv would leave it writing into the old file.
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt "$LOG_MAX" ]; then
	cp "$LOG" "$LOG.1"
	: > "$LOG"
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	exit 0
fi
# token file (mode 600, gitignored) gates the server when it's reachable from
# outside the LAN via Tailscale Funnel. Remove the file to go back to open access.
[ -f token ] && export IMSG_TOKEN="$(cat token)"
# Older logs had the token in them; keep the log private regardless.
umask 077
nohup /usr/bin/python3 server.py >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
