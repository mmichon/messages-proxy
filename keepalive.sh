#!/bin/bash
# Start the server if it isn't running.
#
# Run from cron, NOT launchd: /usr/sbin/cron already holds Full Disk Access on
# this Mac, and a process it spawns inherits that, so the server can read
# chat.db without granting Full Disk Access to any Python binary. A launchd
# agent gets its own TCC identity and fails with "unable to open database file".
pgrep -f 'imsg-web/server.py' >/dev/null && exit 0
cd "$HOME/src/imsg-web" || exit 1
exec /usr/bin/python3 server.py >> imsg-web.log 2>&1
