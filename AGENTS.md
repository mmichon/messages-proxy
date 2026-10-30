# messages-proxy

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
- Attachment paths from the DB are realpath'd by `attachment_path()` and must
  resolve inside `~/Library/Messages/Attachments` **or**
  `~/Library/Messages/StickerCache`, else the request 404s. Use that one helper
  everywhere: the listing's `available` flag and `/api/attachment` disagreeing is
  exactly how stickers ended up as "unavailable" notes.
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

## Images and attachments

`/api/attachment?id=<rowid>` serves a **browser-safe JPEG**; `&full=1` is the same
image at original resolution. Nothing is ever handed to the browser that Chrome
can't decode, because it couldn't: ~60% of incoming images are `image/heic`
(iPhone photos, ~4 MB each), which **only Safari renders**. In Chrome the `<img>`
fired `onerror` and the UI drew its "📎 name — unavailable" note instead — the
"images arrive as text attachment mentions" symptom, which never showed up
locally because Safari was fine.

- Conversion is `/usr/bin/sips` (no deps, and the cron-spawned server can run it
  with no extra TCC grant). `-Z` **upscales** smaller images, so `preview_jpeg()`
  checks `image_size()` first and passes it only when the source is bigger.
- Converted files are cached in `cache/` (gitignored), keyed by rowid + source
  mtime + size + cap, pruned oldest-first past 300 MB. A ~4 MB HEIC takes ~0.25s
  once, then ~1ms — worth having, since every open tab re-fetches on poll.
- Inline previews cap the longest edge at `PREVIEW_MAX` (1600): 3.6 MB → ~700 KB,
  which matters over Funnel. Clicking the image opens `&full=1`.
- GIFs are never converted (sips would flatten the animation), and formats the
  browser already handles are only shrunk if they're over `PREVIEW_MIN_BYTES`
  and the JPEG actually comes out smaller. HEIC/TIFF are converted regardless.
- **Headers are latin-1.** macOS screenshot names carry U+202F before AM/PM, so
  `Content-Disposition: filename="…"` raised `UnicodeEncodeError` and 500'd the
  request — another image that arrived as a text mention. `content_disposition()`
  sends an ASCII-folded name plus RFC 6266 `filename*=UTF-8''…`.
- Still open: a message whose only attachment is a `.pluginPayloadAttachment`
  (link previews, including iCloud photo-share links) has nothing to draw and
  falls back to "📎 attachment unavailable".

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

    * * * * * $HOME/src/messages-proxy/keepalive.sh

## New conversations and unread

**Starting a thread with someone you have no chat with** can't go through
`chat id` — that errors with -1728 for a GUID that doesn't exist. It has to go
through `participant`:

    send theText to participant theHandle of (1st account whose service type = iMessage)

That's `new.applescript`, with an SMS account as fallback for non-Apple numbers.
`POST /api/new {handle, text}` uses it, then polls `find_chat_for_handle()` for up
to 6s because Messages creates the chat row asynchronously. Handles are matched by
last-10-digits for phones and lowercased string for emails, so "+1 (415) 555-0123"
finds an existing thread instead of starting a duplicate.

Note `service type of account` raises -10000 when read directly, but the
`whose service type = iMessage` filter works — don't "simplify" it into a property read.

**Multiple recipients** (`POST /api/new {handles: [...], text}`; `handle` still
accepted, and a comma-separated string works for either). A **new group thread
cannot be created from a script on this macOS** — don't go looking for a way.
`sdef /System/Applications/Messages.app` has one suite with exactly three
commands: `send`, `login`, `logout`. There is no Standard Suite, so no `make`,
and the usual `make new text chat with properties {participants:…}` recipe is
dead. The `chat` class's "may be specified at time of creation" note is
vestigial. Creating one would mean UI-scripting Messages.app, which needs an
Accessibility TCC grant for `/usr/sbin/cron` and steals focus — deliberately not
done.

So multi-recipient sends only reach groups that **already exist**:
`find_chat_for_handles()` compares `handle_key()` sets for **exact equality**
(not containment — a message for three people must never land in a four-person
thread) and sends to the match through the normal `chat id` path. No match is a
**409** carrying `NO_GROUP_ERROR`, which the UI prints as-is. `GET
/api/match?handles=…` resolves recipients while they're typed so the compose box
can say up front whether a thread exists.

Two traps that cost real debugging here:

- **Most chat rows have no messages** — 7496 chats, 246 with any history. Group
  shells in particular. So `find_chat_for_handles()` **left** joins
  `chat_message_join` and orders `mid is null, mid desc`; an inner join (what
  `list_chats` and `find_chat_for_handle` use) silently misses most groups.
- **Duplicate shells are normal**: several chat rows can carry the identical
  participant set. The ordering makes the pick deterministic — the one with the
  most recent message wins.

`handle_key()` is the single normalization used by matching *and* `pretty_handle`;
keep it that way so 1:1 and group matching can't drift. Group GUIDs are
`…;+;chat<hex>`, so `send_message()` skips its per-service variant expansion when
the tail starts with `chat` — those variants are meaningless for a group and only
bury the real error.

**Mark unread** (`POST /api/unread`) rewinds this app's `seen` marker to one below
the chat's last incoming rowid. It is **web-UI only** — chat.db is never written, so
Messages.app on this Mac and your iPhone keep their own read state and will not show
the thread as unread. The UI deselects the thread afterwards, otherwise the open
thread would immediately re-mark itself seen.

## Remote access (Tailscale Funnel)

Reachable from anywhere at **https://macbook-air.tail823fbf.ts.net** — nothing is
installed on the client, it's an ordinary https page. Config:

    /Applications/Tailscale.app/Contents/MacOS/Tailscale funnel --bg 8787
    # off:  ... funnel --https=443 off

Notes:
- Use the CLI **inside the app bundle**. Tailscale here is the GUI app running as
  IPNExtension, so `/opt/homebrew/bin/tailscale` can't find its socket.
- Funnel needs `--shields-up=false`; it was on and had to be turned off. That also
  lets other tailnet devices reach this Mac's ports, so don't re-enable blindly.
- Funnel required a one-time enable on the tailnet in the admin console.
- First visit needs `?t=<token>`; after that an HttpOnly cookie carries it.

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
