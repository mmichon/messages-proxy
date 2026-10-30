# imsg-web

Reads iMessage/SMS history from `~/Library/Messages/chat.db` and sends through
Messages.app, exposing both over a LAN web page. Built so a Mac that cannot run
Messages.app (a locked-down work machine) can still use iMessage in a browser.

## Layout

- `server.py` — everything: HTTP server, chat.db queries, osascript send, inline UI.
  Standard library only; no venv, no dependencies, no CDN assets. Keep it that way.
- `send.applescript` — `on run argv`, invoked as `osascript send.applescript <guid> <text>`.
  Message text is passed as an argv item, never interpolated into the script source.
- `state.db` — app-owned SQLite, one table `seen(chat_guid, rowid_seen)`. Gitignored.

## Hard rules

- **Never write to `chat.db`.** It's opened `mode=ro` and unread state lives in
  `state.db` instead of touching Apple's `is_read` column.
- Attachment paths from the DB are realpath'd and must resolve inside
  `~/Library/Messages/Attachments`, else the request 404s.
- `.pluginPayloadAttachment` rows are link previews/app payloads, not files — skipped.

## chat.db notes (verified on macOS 27.2)

- Dates are **nanoseconds** since the Apple epoch (2001-01-01); `apple_ts()` also
  handles the older seconds-based rows.
- Chat GUIDs look like `any;-;+14155550123` or `any;+;<hex>`. Services seen:
  `iMessage`, `SMS`, `RCS`. Older `iMessage;-;` style GUIDs still work for sending,
  so `send_message()` falls back through service variants.
- ~5% of rows have `text IS NULL` with content only in `attributedBody`, an
  NSAttributedString typedstream. `decode_attributed_body()` reads the length-prefixed
  UTF-8 payload after the `NSString` marker, with a printable-run fallback.
- AppleScript `send ... to chat id` works here, but `service type of account` raises
  -10000 — don't rely on account properties.

## Running

    python3 server.py                      # http://<host>:8787
    IMSG_TOKEN=secret python3 server.py    # then open /?t=secret

Auth: if a `token` file exists (mode 600, gitignored), `keepalive.sh` exports it as
`IMSG_TOKEN` and every request needs `?t=<token>` once, which sets a session cookie.
Delete the file to go back to open LAN access.

**Keep the token in place whenever the server is reachable from outside the LAN.**
A Tailscale Funnel hostname gets a public Let's Encrypt cert, so it appears in
Certificate Transparency logs and gets scanned within minutes of being created —
an unauthenticated Funnel would hand strangers the whole message history and the
ability to send.

Persistence is a **cron** entry running `keepalive.sh` every minute:

    * * * * * $HOME/src/imsg-web/keepalive.sh

## Permissions — why cron and not launchd

The process needs two TCC grants: **Full Disk Access** (read chat.db) and
**Automation → Messages** (send). Both are attributed to the *responsible process*,
which for a daemon is whatever spawned it.

On this Mac `/usr/sbin/cron` already holds both, so a cron-spawned server inherits
them and needs **no permission prompts at all**. This was verified empirically:
a cron-run script both read 7311 rows from chat.db and sent an iMessage (exit 0).

A **launchd** agent does *not* work: it gets its own TCC identity and dies with
`cannot read chat.db (unable to open database file)`. It would need Full Disk Access
granted to the resolved interpreter — which under launchd is
`/Applications/Xcode.app/Contents/Developer/usr/bin/python3`, a path that moves with
Xcode updates. Don't reintroduce a launchd agent without re-checking this.

Run under `/usr/bin/python3` (3.9.6), not Homebrew's — it's a stable path, and the
code is kept 3.9-compatible for that reason.
