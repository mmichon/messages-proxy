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

## Names (resolving handles to people)

`pretty_handle()` turns a handle into a name via `contact_map()`, keyed by
`handle_key()` so the normalization can't drift from the matching code. Two
sources, merged with `setdefault` so **Contacts always wins**:

1. **Contacts** — every `AddressBook-v22.abcddb` under `Sources/*`, read
   `mode=ro`. Nine sources exist here but only the Google CardDAV one has phone
   data (1768 numbers); the iCloud source holds 3 contacts, LinkedIn has names
   but no phones. Don't hard-code the UUID — sources are recreated when an
   account is re-added. `ZFULLNUMBER` is stored unnormalized (`415.503.7118`,
   `(213) 364-1300`, `+1847…`), and `ZCOUNTRYCODE`/`ZAREACODE`/`ZLOCALNUMBER` are
   empty on every row, so last-10-digits (`_phone_key`) is the only thing that
   works.
2. **Shared names** — `NickNameCache/{handled,pending}NicknamesKeyStore.db`, the
   names people share over iMessage ("Name and Photo Sharing"). This is how
   Messages.app labels someone who isn't in Contacts at all. ~137 handles here.
   `archivedNicknamesKeyStore.db` is **deliberately excluded** — withdrawn or
   superseded shares, so the name can be stale.

Each nickname row is `kvtable(key = raw handle, value = NSKeyedArchiver plist)`
with `dn` (display name) plus `fn`/`ln`. `_unarchive_root()` resolves the root's
`NS.keys`/`NS.objects` against `$objects` one level deep — the row is a flat
dictionary, so a general unarchiver isn't needed. A row that fails to decode is
skipped individually; one re-versioned blob must not cost every other name.

The whole map is built in ~0.05s and cached for `CONTACTS_TTL` (600s).

**`chat.db` cannot help here.** The `handle` table has no name column at all
(`id`, `country`, `service`, `uncanonicalized_id`, `person_centric_id`), and
`chat.display_name` is set on only 263 of 3687 group chats — so ~93% of groups
must be labeled from their participants.

**Not every number resolves, and that's not a bug.** A handle absent from both
sources has no name anywhere on this Mac; Messages.app shows it as digits too.
`pretty_handle()` returns the raw handle in that case.

`list_chats()` ships both `handles` (raw, in participant order) and `names` (the
same order, resolved). Both are needed: the thread subtitle renders `names`,
while sending and the group-sender gate (`cur.handles.length > 1`) need the raw
handles. A resolved name must never be fed back into a send path.

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
- Inline previews cap the longest edge at `PREVIEW_MAX` (1200, enough for a
  ≤68%-wide bubble at 2x): a 4032px HEIC → ~280 KB, which matters over Funnel.
  Clicking the image opens `&full=1`.
- **The bottleneck is Funnel, not sips.** Locally a preview is 0.2s cold, 1ms
  cached; from a remote browser it was ~10s per image. Successful attachment
  responses carry `ATTACH_CACHE` (`private, max-age=1y, immutable` — a rowid's
  bytes never change) so reopening a thread doesn't re-pull everything; it used
  to be the default `no-store`. 404s stay `no-store`.
- Funnel also drops connections before they reach the server (no log line at
  all for that image). The `<img>` retries twice with backoff before showing
  "unavailable, tap to retry". `BrokenPipeError`/`ConnectionResetError`
  (`CLIENT_GONE`) are swallowed rather than 500'd and traced into the log.
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

## App icon / installing it

The page is installable (Add to Home Screen on iOS, Install on Chrome). The icon
is **one glyph rendered two ways**: `FAVICON` is an SVG for browser tabs, and the
home-screen sizes are rasterized by `render_icon()` — pure stdlib (`zlib` +
`struct`), because iOS ignores SVG icons and Pillow is not a dependency here.
Geometry lives once in `BUBBLE` / `TAIL` / `SQUARE_R` as 0..1 coordinates, shared
by both; if you move the glyph, move it in both or they drift.

- `ICON_ROUTES` maps path → (size, rounded, glyph scale). `rounded=False` is
  full-bleed, for `/apple-touch-icon.png` (iOS applies its own mask — a rounded
  PNG there gets double-rounded corners) and for the maskable manifest icon,
  whose glyph is shrunk to 0.74 to stay inside the safe zone.
- Rendering is supersampled in Python: 512px takes ~0.5s, so results are cached
  in `_ICON_CACHE` on first request. Don't render eagerly at startup.
- **Icons and `/manifest.webmanifest` are served before the token check.** iOS and
  Android fetch them without the session cookie, and a 401 there is a blank
  home-screen tile. They expose nothing.
- The auth cookie carries `Max-Age` (1 year). A session cookie dies when the
  standalone webview is evicted, which showed up as a bare "Unauthorized" page in
  the installed app.
- `viewport-fit=cover` plus `env(safe-area-inset-*)` on `#side h1`, `#head` and
  `#composer` — standalone mode has no browser chrome, so without it the header
  sits under the notch and the composer under the home indicator.

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

## LAN mode and load speed

Server work is never the slow part (chats ~5–50 ms, a thread ≤6 ms, a cached
preview ~1 ms). The time is all in **round trips through Funnel**: a browser on
the home Wi-Fi that opens the `ts.net` URL goes out to Tailscale's relay and
back for every request. So the page moves itself onto the LAN when it can:

- Funnel requests reach the server from tailscaled on loopback carrying
  `Tailscale-Funnel-Request: ?1` and `X-Forwarded-For: <client public IP>`.
  `Handler._route()` trusts those headers only from loopback.
- `tailscale_self()` reads `status --json` (cached 5 min). `Self.Addrs` holds
  the endpoints Tailscale discovered: the global one is this NAT's public IP
  (no "what's my IP" service), the private one is the LAN address. **Don't**
  find the LAN IP from the routing table — with Tailscale owning the default
  route that returns the 100.x tunnel address.
- `GET /api/net` says `via` (funnel/lan/tailnet/local) and `same_lan` (Funnel
  client's public IP is ours), plus a **one-time handoff code**. `/?h=<code>`
  trades it for the cookie and 302s to `/`, so the token itself never rides in
  a plain-http URL. Codes are single-use and expire after 120 s.
- The Funnel page then probes `http://<lan>:8787/ping` (open, CORS `*`, returns
  the per-process `INSTANCE` so a different device at that IP doesn't count)
  and navigates there if it answers. An https page can only fetch a private
  http address in Chrome, via `fetch(..., {targetAddressSpace: "local"})`
  (Local Network Access; prompts once). Safari/iOS block it as mixed content,
  so the probe fails quietly and the header shows a **"use LAN"** link instead.
  Installed apps on phones/tablets (`pinnedApp`) are never switched — leaving
  the origin pops iOS out of the app. A desktop Chrome app window switches
  fine (it just shows an address strip), so those are not excluded.
- **The work Mac can't use LAN mode, and that's not a bug.** Its Chrome sends
  everything, private addresses included, through Google's corporate proxy
  (UberProxy/BeyondCorp), which errors on `10.0.0.89`. Its public IP still
  matches ours (`same_lan=True` in the log), so detection alone can't tell. If
  the local-network permission is granted and the probe fails anyway, the
  page hides "use LAN" (`net.unreachable`). Don't try to route around the
  proxy; Funnel is the path that machine is allowed.
- Every `/api/net` call logs `net: via=… client=… ours=… same_lan=…` — the
  first thing to read when LAN mode doesn't kick in.
- On the LAN origin, three failed polls make the page check Funnel's `/ping`
  and move back there — only if Funnel answers, so a server restart doesn't
  bounce it. Clicking "· LAN" goes back to Funnel and sets
  `localStorage imsg-net=funnel`, which stops auto-switching on that browser.
- The LAN origin is plain http, so its cookie crosses the home network in the
  clear. That's the trade for skipping the internet.

Other speedups, which help on either path:

- JSON, the page and SVG are **gzipped** when over 1 KB (page 18→8 KB, chat
  list 37→10 KB).
- The client keeps threads **in memory** (`threads` map, never localStorage —
  nothing is left on the disk of a work machine). Opening a cached thread draws
  immediately; the full fetch still runs and redraws only if the JSON differs,
  which covers failed sends, edits and unsends.
- `prefetch()` loads the top 8 threads that changed since cached, after the
  first load and after every poll that changed something. On a fast path
  (`via` lan/local) it also warms the newest images into the browser cache.
- `attachments_for()` queues the newest `PREWARM_IMAGES` previews of a thread
  for `sips` on two background workers, so the `<img>` requests that follow
  are cache hits rather than a 0.25 s conversion each.
- Images keep the thread pinned to the bottom as they load (`pinned`), unless
  the reader has scrolled up.

## Disk use

Nothing here may grow without bound:

- `messages-proxy.log` is capped by `keepalive.sh` (every minute from cron) at 5 MB
  plus one `messages-proxy.log.1`, copy-then-truncate so the running server's
  O_APPEND fd keeps working. Successful `/api/poll` and `/api/match` requests
  aren't logged at all — every open tab polls every few seconds and those lines
  were most of the growth.
- The token is never written to the log: `log_message()` rewrites `?t=…` to
  `?t=<token>`, and the startup line prints the placeholder. The log is mode 600.
- The `$TMPDIR/messages-proxy-snap/` copy of chat.db (only made while chat.db is
  locked) is deleted by `_drop_snapshot()` once the live DB is readable again.
- `cache/` holds transcoded image previews; `_prune_cache()` drops the
  least-recently-used files past 300 MB (`CACHE_MAX_BYTES`) on every write.
- `state.db` is one row per chat; original attachments are read straight from
  Apple's folder, never copied.

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
