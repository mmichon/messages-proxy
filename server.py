#!/usr/bin/env python3
"""messages-proxy — read and send iMessage/SMS through a LAN web page.

Reads ~/Library/Messages/chat.db (read-only) for history and incoming messages,
and drives Messages.app via AppleScript to send. Standard library only.

Env:
  IMSG_PORT   port to listen on (default 8787)
  IMSG_BIND   bind address (default 0.0.0.0)
  IMSG_TOKEN  if set, require ?t=<token> once, then a session cookie
"""

import gzip
import ipaddress
import json
import mimetypes
import queue
import secrets
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HOME = os.path.expanduser("~")
CHAT_DB = os.path.join(HOME, "Library", "Messages", "chat.db")
# Stickers don't live under Attachments/, so both roots are allowed; a path
# from chat.db that resolves outside them is refused.
ATTACH_ROOTS = (
    os.path.join(HOME, "Library", "Messages", "Attachments"),
    os.path.join(HOME, "Library", "Messages", "StickerCache"),
)
ADDRESSBOOK_GLOB = os.path.join(
    HOME, "Library", "Application Support", "AddressBook", "Sources",
    "*", "AddressBook-v22.abcddb",
)
# Names people share over iMessage ("Name and Photo Sharing"), for handles that
# aren't in Contacts at all. Ordered: live shares first, then ones still pending
# acceptance. archivedNicknamesKeyStore.db is deliberately left out — those are
# shares that were withdrawn or superseded, so the name there can be stale.
NICKNAME_DBS = tuple(
    os.path.join(HOME, "Library", "Messages", "NickNameCache", name)
    for name in ("handledNicknamesKeyStore.db", "pendingNicknamesKeyStore.db")
)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DB = os.path.join(APP_DIR, "state.db")
CACHE_DIR = os.path.join(APP_DIR, "cache")
SEND_SCRIPT = os.path.join(APP_DIR, "send.applescript")
NEW_SCRIPT = os.path.join(APP_DIR, "new.applescript")
_REAL_ROOTS = tuple(os.path.realpath(r) for r in ATTACH_ROOTS)

APPLE_EPOCH = 978307200  # 2001-01-01 UTC, in Unix seconds

PORT = int(os.environ.get("IMSG_PORT", "8787"))
BIND = os.environ.get("IMSG_BIND", "0.0.0.0")
TOKEN = os.environ.get("IMSG_TOKEN") or None

# A year, so a home-screen app does not fall back to 401 once its webview is
# evicted and the session cookie goes with it.
COOKIE = "imsg=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
ICON_CACHE_HDR = {"Cache-Control": "public, max-age=604800"}

# Inline images are transcoded to JPEG and capped at this longest edge. Phone
# photos arrive as ~4 MB HEIC, which no browser but Safari can decode at all.
PREVIEW_MAX = 1200                # bubbles are <=68% wide; 2x DPR covers it
ATTACH_CACHE = "private, max-age=31536000, immutable"
CLIENT_GONE = (BrokenPipeError, ConnectionResetError)
PREVIEW_MIN_BYTES = 512 * 1024   # below this, serve the original untouched
# Chrome and Firefox decode none of these; Safari only handles HEIC.
TRANSCODE_MIMES = (
    "image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence",
    "image/tiff", "image/x-adobe-dng",
)
CACHE_MAX_BYTES = 300 * 1024 * 1024
PREWARM_IMAGES = 24              # newest images per thread converted ahead of time

# LAN mode: a browser on the home network that reached us through Funnel is
# making a round trip through Tailscale's relay for every request. When its
# public IP is ours, the page is told the LAN address and switches to it.
TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
NET_TTL = 300.0
HANDOFF_TTL = 120.0
# Lets the page check that the LAN address is really this server, not
# whatever else holds that IP on some other network.
INSTANCE = secrets.token_hex(8)
GZIP_TYPES = ("application/json", "text/", "image/svg+xml", "application/manifest+json")

# ---------------------------------------------------------------- chat.db access

_snap_lock = threading.Lock()
_snap = {"dir": None, "at": 0.0}
SNAP_TTL = 5.0


def _snapshot_path():
    """Copy the live DB (plus WAL/SHM) somewhere readable, for when it's locked."""
    with _snap_lock:
        now = time.time()
        if _snap["dir"] and now - _snap["at"] < SNAP_TTL:
            return os.path.join(_snap["dir"], "chat.db")
        d = _snap["dir"] or os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "messages-proxy-snap"
        )
        os.makedirs(d, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            src = CHAT_DB + suffix
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(d, "chat.db" + suffix))
                except OSError:
                    pass
        _snap["dir"] = d
        _snap["at"] = now
        return os.path.join(d, "chat.db")


def _drop_snapshot():
    """Delete the snapshot once the live DB is readable again — it's a full copy."""
    if not _snap["dir"]:
        return
    with _snap_lock:
        # Wait a minute past the last refresh so a reader that just got the
        # snapshot path from another thread isn't pulled out from under.
        if _snap["dir"] and time.time() - _snap["at"] > 60:
            shutil.rmtree(_snap["dir"], ignore_errors=True)
            _snap["dir"] = None
            _snap["at"] = 0.0


def chat_db():
    """Read-only connection to chat.db, falling back to a snapshot copy."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CHAT_DB, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("select 1 from message limit 1").fetchone()
        _drop_snapshot()
        return con
    except sqlite3.Error:
        con = sqlite3.connect("file:%s?mode=ro" % _snapshot_path(), uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con


def state_db():
    con = sqlite3.connect(STATE_DB, timeout=5)
    con.execute(
        "create table if not exists seen (chat_guid text primary key, rowid_seen integer not null)"
    )
    con.commit()
    return con


# -------------------------------------------------------- attributedBody decoding

_PRINTABLE = re.compile(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]{3,}")


def _read_tsint(data, pos):
    """Read a typedstream length/int: 0x81 -> int16, 0x82 -> int32, else inline."""
    b = data[pos]
    pos += 1
    if b == 0x81:
        return int.from_bytes(data[pos : pos + 2], "little"), pos + 2
    if b == 0x82:
        return int.from_bytes(data[pos : pos + 4], "little"), pos + 4
    return b, pos


def decode_attributed_body(blob):
    """Pull the plain string out of an NSAttributedString typedstream blob."""
    if not blob:
        return None
    data = bytes(blob)
    i = data.find(b"NSString")
    if i != -1:
        # The string payload follows a '+' (short) or '*' (shared) class marker.
        for p in range(i + 8, min(i + 24, len(data))):
            if data[p] in (0x2B, 0x2A):
                try:
                    n, q = _read_tsint(data, p + 1)
                    if 0 < n <= len(data) - q:
                        text = data[q : q + n].decode("utf-8", "replace")
                        if text.strip():
                            return text
                except (IndexError, ValueError):
                    pass
                break
    # Fallback: longest run that looks like text.
    runs = _PRINTABLE.findall(data)
    if not runs:
        return None
    best = max(runs, key=len).decode("utf-8", "replace")
    best = best.strip().lstrip("+*").strip()
    return best or None


def msg_text(row):
    text = row["text"] or decode_attributed_body(row["attributedBody"]) or ""
    # U+FFFC is the object-replacement placeholder standing in for an attachment.
    return text.replace("￼", "").strip()


# --------------------------------------------------------------- contact names

_contacts_lock = threading.Lock()
_contacts = {"map": None, "at": 0.0}
CONTACTS_TTL = 600.0


def _phone_key(value):
    """Last 10 digits, so +1-415-555-0123 and 4155550123 agree."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def handle_key(handle):
    """Comparison key for a handle: lowercased for emails, last 10 digits for phones.

    Everything that matches handles against each other goes through this, so 1:1
    lookups and group participant-set matching can't drift apart.
    """
    handle = (handle or "").strip()
    if not handle:
        return ""
    return handle.lower() if "@" in handle else _phone_key(handle)


def _contact_label(first, last, org, nick):
    name = " ".join(p for p in (first, last) if p).strip()
    return name or nick or org or ""


def _unarchive_root(blob):
    """Top-level dict of an NSKeyedArchiver plist, one level deep.

    The nickname rows are a flat NSMutableDictionary, so resolving the root's
    NS.keys/NS.objects against $objects is enough — no general unarchiver needed.
    """
    import plistlib

    plist = plistlib.loads(blob)
    objects = plist["$objects"]

    def deref(value):
        return objects[value.data] if isinstance(value, plistlib.UID) else value

    root = deref(plist["$top"]["root"])
    if not isinstance(root, dict):
        return {}
    keys = root.get("NS.keys") or []
    values = root.get("NS.objects") or []
    return {deref(k): deref(v) for k, v in zip(keys, values)}


def nickname_map():
    """handle key -> shared name, from the Messages nickname cache.

    Each kvtable row is keyed by the raw handle and holds an NSKeyedArchiver
    blob with 'dn' (display name) plus 'fn'/'ln'. Covers people who share their
    name over iMessage but aren't in Contacts; Contacts still wins when both have
    a name, so this only ever fills gaps.
    """
    out = {}
    for path in NICKNAME_DBS:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
        except sqlite3.Error:
            continue
        try:
            rows = con.execute("select key, value from kvtable").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for handle, blob in rows:
            key = handle_key(handle)
            if not key or key in out:
                continue
            try:
                row = _unarchive_root(blob)
            except Exception:
                # A malformed or re-versioned row must not cost us every name.
                continue
            label = row.get("dn") or " ".join(
                p for p in (row.get("fn"), row.get("ln")) if p
            ).strip()
            if isinstance(label, str) and label:
                out[key] = label
    return out


def contact_map():
    """handle (phone key or lowercased email) -> display name, from all Contacts sources."""
    import glob

    with _contacts_lock:
        now = time.time()
        if _contacts["map"] is not None and now - _contacts["at"] < CONTACTS_TTL:
            return _contacts["map"]

        out = {}
        for path in glob.glob(ADDRESSBOOK_GLOB):
            try:
                con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
            except sqlite3.Error:
                continue
            try:
                for table, col in (
                    ("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                    ("ZABCDEMAILADDRESS", "ZADDRESS"),
                ):
                    try:
                        rows = con.execute(
                            "select x.%s, r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, "
                            "r.ZNICKNAME from %s x join ZABCDRECORD r on r.Z_PK = x.ZOWNER"
                            % (col, table)
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    for value, first, last, org, nick in rows:
                        label = _contact_label(first, last, org, nick)
                        if not value or not label:
                            continue
                        key = (
                            _phone_key(value)
                            if table == "ZABCDPHONENUMBER"
                            else value.strip().lower()
                        )
                        if key:
                            out.setdefault(key, label)
            finally:
                con.close()

        # Shared names fill handles Contacts has nothing for; setdefault keeps
        # the user's own Contacts entry authoritative when both have a name.
        for key, label in nickname_map().items():
            out.setdefault(key, label)

        _contacts["map"] = out
        _contacts["at"] = now
        return out


def pretty_handle(handle):
    """Contact name for a handle, else the handle itself."""
    if not handle:
        return ""
    names = contact_map()
    return names.get(handle_key(handle), handle)


def apple_ts(value):
    """chat.db dates are nanoseconds since the Apple epoch (older rows: seconds)."""
    if not value:
        return 0
    if value > 1_000_000_000_000:
        value = value / 1_000_000_000
    return int(value + APPLE_EPOCH)


# ------------------------------------------------------------------------ queries

CHATS_SQL = """
with last as (
  select cmj.chat_id as cid, max(m.ROWID) as mid
  from chat_message_join cmj
  join message m on m.ROWID = cmj.message_id
  group by cmj.chat_id
)
select c.ROWID as chat_rowid, c.guid, c.display_name, c.chat_identifier,
       c.service_name, m.ROWID as mid, m.text, m.attributedBody, m.is_from_me,
       m.date, m.cache_has_attachments, m.item_type, m.associated_message_type
from last
join chat c on c.ROWID = last.cid
join message m on m.ROWID = last.mid
order by m.date desc
limit ?
"""

MESSAGES_SQL = """
select m.ROWID as mid, m.guid, m.text, m.attributedBody, m.is_from_me, m.date,
       m.service, m.is_sent, m.error, m.item_type, m.associated_message_type,
       m.cache_has_attachments, m.date_edited, m.date_retracted, h.id as sender
from chat_message_join cmj
join message m on m.ROWID = cmj.message_id
left join handle h on h.ROWID = m.handle_id
where cmj.chat_id = ? and m.ROWID > ?
order by m.ROWID desc
limit ?
"""


def chat_participants(con, chat_rowids):
    if not chat_rowids:
        return {}
    marks = ",".join("?" * len(chat_rowids))
    rows = con.execute(
        "select chj.chat_id as cid, h.id as handle "
        "from chat_handle_join chj join handle h on h.ROWID = chj.handle_id "
        "where chj.chat_id in (%s)" % marks,
        chat_rowids,
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["cid"], []).append(r["handle"])
    return out


def list_chats(limit=120):
    con = chat_db()
    try:
        rows = con.execute(CHATS_SQL, (limit,)).fetchall()
        parts = chat_participants(con, [r["chat_rowid"] for r in rows])
    finally:
        con.close()

    st = state_db()
    try:
        seen = dict(st.execute("select chat_guid, rowid_seen from seen").fetchall())
        known = [(r["guid"], seen[r["guid"]]) for r in rows if r["guid"] in seen]
        con = chat_db()
        try:
            unread_by_guid = unread_counts(con, known)
        finally:
            con.close()

        fresh = []
        out = []
        for r in rows:
            guid = r["guid"]
            handles = parts.get(r["chat_rowid"], [])
            pretty = [pretty_handle(h) for h in handles]
            name = (
                r["display_name"]
                or ", ".join(pretty)
                or pretty_handle(r["chat_identifier"])
            )
            if guid not in seen:
                # First sighting: treat everything already here as read.
                fresh.append((guid, r["mid"]))
                unread = 0
            else:
                unread = unread_by_guid.get(guid, 0)
            out.append(
                {
                    "guid": guid,
                    "name": name,
                    "handles": handles,
                    # Same order as handles, each resolved to a name where one
                    # exists; the thread subtitle shows these instead of digits.
                    "names": pretty,
                    "service": r["service_name"],
                    "preview": preview_of(r),
                    "ts": apple_ts(r["date"]),
                    "last_id": r["mid"],
                    "unread": unread,
                }
            )
        if fresh:
            st.executemany("insert or ignore into seen values (?,?)", fresh)
            st.commit()
    finally:
        st.close()
    return out


def preview_of(row):
    text = msg_text(row).replace("\n", " ").strip()
    if not text and row["cache_has_attachments"]:
        text = "\U0001f4ce Attachment"
    if row["item_type"]:
        text = text or "(system message)"
    prefix = "You: " if row["is_from_me"] else ""
    return (prefix + text)[:120]


def unread_counts(con, seen_pairs):
    """Incoming messages newer than each chat's last-seen rowid, in one query."""
    if not seen_pairs:
        return {}
    values = ",".join(["(?,?)"] * len(seen_pairs))
    args = [x for pair in seen_pairs for x in pair]
    rows = con.execute(
        "with s(guid, seen) as (values %s) "
        "select c.guid, count(m.ROWID) from s "
        "join chat c on c.guid = s.guid "
        "join chat_message_join cmj on cmj.chat_id = c.ROWID "
        "join message m on m.ROWID = cmj.message_id "
        "where m.ROWID > s.seen and m.is_from_me = 0 and m.item_type = 0 "
        "group by c.guid" % values,
        args,
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def attachments_for(con, message_ids):
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = con.execute(
        "select maj.message_id as mid, a.ROWID as aid, a.filename, a.mime_type, "
        "a.transfer_name, a.total_bytes "
        "from message_attachment_join maj join attachment a on a.ROWID = maj.attachment_id "
        "where maj.message_id in (%s)" % marks,
        message_ids,
    ).fetchall()
    out = {}
    convert = []
    for r in rows:
        name = r["transfer_name"] or ""
        if name.endswith(".pluginPayloadAttachment"):
            continue  # link previews and app payloads, not real files
        mime = r["mime_type"] or mimetypes.guess_type(name)[0] or ""
        # Attachments can be iCloud-offloaded or purged; the row outlives the file.
        local = attachment_path(r["filename"])
        if local and (mime in TRANSCODE_MIMES or wants_preview(mime, r["total_bytes"] or 0)):
            convert.append((r["aid"], local))
        out.setdefault(r["mid"], []).append(
            {
                "id": r["aid"],
                "name": name,
                "mime": mime,
                "bytes": r["total_bytes"] or 0,
                "image": mime.startswith("image/"),
                "available": bool(local),
            }
        )
    # The browser asks for these as soon as the thread renders; start the
    # sips conversions now so they're cache hits by then. Newest first.
    convert.sort(reverse=True)
    for item in convert[:PREWARM_IMAGES]:
        prewarm(*item)
    return out


def list_messages(guid, after=0, limit=200):
    con = chat_db()
    try:
        crow = con.execute("select ROWID from chat where guid = ?", (guid,)).fetchone()
        if not crow:
            return None
        rows = con.execute(MESSAGES_SQL, (crow[0], after, limit)).fetchall()
        rows = list(reversed(rows))
        atts = attachments_for(con, [r["mid"] for r in rows])
    finally:
        con.close()

    out = []
    for r in rows:
        out.append(
            {
                "id": r["mid"],
                "text": msg_text(r),
                "from_me": bool(r["is_from_me"]),
                "sender": pretty_handle(r["sender"]),
                "ts": apple_ts(r["date"]),
                "service": r["service"] or "",
                "failed": bool(r["error"]) or (bool(r["is_from_me"]) and not r["is_sent"]),
                "system": bool(r["item_type"]),
                "tapback": bool(r["associated_message_type"]),
                "edited": bool(r["date_edited"]),
                "unsent": bool(r["date_retracted"]),
                "attachments": atts.get(r["mid"], []),
            }
        )
    return out


def max_rowid():
    con = chat_db()
    try:
        row = con.execute("select max(ROWID) from message").fetchone()
        return row[0] or 0
    finally:
        con.close()


def attachment_path(filename):
    """Realpath of an attachment file, or None if it is gone or out of bounds.

    The one place the roots are enforced, so the listing's "available" flag and
    /api/attachment can't disagree -- they used to, for stickers: the flag said
    yes and the fetch 404'd, which the UI then drew as a text mention.
    """
    if not filename:
        return None
    path = os.path.realpath(os.path.expanduser(filename))
    for root in _REAL_ROOTS:
        if path.startswith(root + os.sep) and os.path.isfile(path):
            return path
    return None


def wants_preview(mime, nbytes):
    """Whether a big-but-displayable image is worth shrinking for the inline view."""
    if mime == "image/gif":
        return False                 # sips would flatten it to one frame
    return mime.startswith("image/") and nbytes > PREVIEW_MIN_BYTES


def _prune_cache():
    """Keep the preview cache under CACHE_MAX_BYTES, oldest-first."""
    try:
        files = []
        for n in os.listdir(CACHE_DIR):
            f = os.path.join(CACHE_DIR, n)
            try:
                st = os.stat(f)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, f))
    except OSError:
        return
    total = sum(f[1] for f in files)
    for _, size, f in sorted(files):
        if total <= CACHE_MAX_BYTES:
            return
        try:
            os.remove(f)
            total -= size
        except OSError:
            pass


def image_size(path):
    """(width, height) from sips, or (0, 0) if it won't say."""
    try:
        out = subprocess.run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    dims = {}
    for line in out.splitlines():
        k, _, v = line.strip().partition(": ")
        if k in ("pixelWidth", "pixelHeight") and v.isdigit():
            dims[k] = int(v)
    return (dims.get("pixelWidth", 0), dims.get("pixelHeight", 0))


def preview_jpeg(aid, path, cap=PREVIEW_MAX):
    """A browser-safe JPEG of `path`, converted by sips on first request.

    `cap` is the longest edge in pixels, or 0 to keep the original size.

    Returns the cached file's path, or None if conversion failed (the caller
    then falls back to the original bytes). Keyed by rowid plus the source's
    mtime and size, so each attachment is converted once, not on every poll.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    out = os.path.join(
        CACHE_DIR, "%d-%d-%d-%d.jpg" % (aid, int(st.st_mtime), st.st_size, cap))
    if os.path.isfile(out) and os.path.getsize(out):
        os.utime(out, None)          # keep hot previews out of the prune
        return out
    # Thread id in the temp name: two tabs can ask for the same image at once.
    tmp = "%s.%d.tmp" % (out, threading.get_ident())
    args = ["-s", "format", "jpeg", "-s", "formatOptions", "72"]
    if cap and max(image_size(path)) > cap:
        args += ["-Z", str(cap)]           # -Z would otherwise upscale small ones
    try:
        if not os.path.isdir(CACHE_DIR):
            os.makedirs(CACHE_DIR, 0o700)
        subprocess.run(
            ["/usr/bin/sips"] + args + [path, "--out", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=60, check=True,
        )
        os.replace(tmp, out)
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    _prune_cache()
    return out


_prewarm_q = queue.Queue()
_prewarm_pending = set()
_prewarm_lock = threading.Lock()


def _prewarm_worker():
    while True:
        aid, path = _prewarm_q.get()
        try:
            preview_jpeg(aid, path, PREVIEW_MAX)
        except Exception:
            pass                      # the request path will try again and report
        with _prewarm_lock:
            _prewarm_pending.discard(aid)


def prewarm(aid, path):
    """Queue a background preview conversion for an attachment, once."""
    with _prewarm_lock:
        if aid in _prewarm_pending:
            return
        _prewarm_pending.add(aid)
    _prewarm_q.put((aid, path))


for _ in range(2):
    threading.Thread(target=_prewarm_worker, daemon=True).start()


def attachment_blob(aid, preview=False):
    con = chat_db()
    try:
        row = con.execute(
            "select filename, mime_type, transfer_name from attachment where ROWID = ?",
            (aid,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    path = attachment_path(row["filename"])
    if not path:
        return None
    name = row["transfer_name"] or os.path.basename(path)
    mime = row["mime_type"] or mimetypes.guess_type(name)[0] or "application/octet-stream"
    # A HEIC is converted either way -- at full size for the click-through,
    # since no browser but Safari can open the original at all. Formats that do
    # display get shrunk for the inline view only.
    forced = mime in TRANSCODE_MIMES
    if forced or (preview and wants_preview(mime, os.path.getsize(path))):
        jpg = preview_jpeg(aid, path, PREVIEW_MAX if preview else 0)
        # A JPEG of an already-displayable image can come out bigger than the
        # original; those fall back to the file as it is.
        if jpg and (forced or os.path.getsize(jpg) < os.path.getsize(path)):
            with open(jpg, "rb") as fh:
                return "image/jpeg", os.path.splitext(name)[0] + ".jpg", fh.read()
    with open(path, "rb") as fh:
        return mime, name, fh.read()


def content_disposition(name):
    """RFC 6266 filename, ASCII plus a UTF-8 copy for everything else.

    Headers go out latin-1 encoded, so a macOS screenshot name -- they carry
    U+202F before AM/PM -- used to raise UnicodeEncodeError and 500 the request,
    which the UI then showed as a text mention instead of the image.
    """
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "")
    return "inline; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name, quote(name, safe=""),
    )


# --------------------------------------------------------------------- sending

def send_message(guid, text):
    """Try the chat's own GUID, then per-service variants of its handle."""
    candidates = [guid]
    handle = guid.rsplit(";", 1)[-1]
    # A group's tail is `chat<hex>`, not a handle — per-service variants of it are
    # meaningless and would only bury the real error.
    if ";" in guid and not handle.startswith("chat"):
        kind = ";-;" if ";-;" in guid else ";+;"
        for svc in ("iMessage", "SMS", "any"):
            alt = svc + kind + handle
            if alt not in candidates:
                candidates.append(alt)

    errors = []
    for cand in candidates:
        proc = subprocess.run(
            ["osascript", SEND_SCRIPT, cand, text],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if proc.returncode == 0:
            return True, cand
        errors.append("%s: %s" % (cand, (proc.stderr or "").strip()))
    return False, " | ".join(errors)


def start_conversation(handle, text):
    """Send to a handle with no existing chat. `chat id` can't create one, so this
    goes through `participant ... of account`, which does."""
    proc = subprocess.run(
        ["osascript", NEW_SCRIPT, handle, text],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or "send failed").strip(), None
    service = (proc.stdout or "").strip()
    # Messages creates the chat asynchronously; give it a moment to land.
    for _ in range(12):
        guid = find_chat_for_handle(handle)
        if guid:
            return True, service, guid
        time.sleep(0.5)
    return True, service, None


def find_chat_for_handle(handle):
    """Newest chat whose identifier matches this handle (phones compared by last 10)."""
    con = chat_db()
    try:
        rows = con.execute(
            "select c.guid, c.chat_identifier, max(m.ROWID) as mid "
            "from chat c join chat_message_join cmj on cmj.chat_id = c.ROWID "
            "join message m on m.ROWID = cmj.message_id "
            "group by c.ROWID order by mid desc limit 400"
        ).fetchall()
    finally:
        con.close()

    if "@" in handle:
        want = handle.strip().lower()
        for r in rows:
            if (r["chat_identifier"] or "").strip().lower() == want:
                return r["guid"]
        return None

    want = _phone_key(handle)
    if not want:
        return None
    for r in rows:
        if _phone_key(r["chat_identifier"] or "") == want:
            return r["guid"]
    return None


def parse_handles(value):
    """Accept a list or a comma-separated string; trim, drop blanks, de-dupe."""
    if isinstance(value, str):
        value = value.split(",")
    out, seen = [], set()
    for item in value or []:
        item = (item or "").strip()
        key = handle_key(item)
        if not item or not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


NO_GROUP_ERROR = (
    "No existing group thread with exactly those people. macOS won't let a "
    "script create a group chat (the Messages AppleScript suite has only "
    "send/login/logout), so start the group once from Messages.app or your "
    "iPhone and it'll work here from then on."
)


def find_chat_for_handles(handles):
    """Newest chat whose participant set is *exactly* these handles.

    Equality, not containment: a message addressed to three people must never
    land in a four-person thread that happens to include them.
    """
    want = set(k for k in (handle_key(h) for h in handles) if k)
    if not want:
        return None

    # Left join the messages: most group rows in chat.db are message-less shells
    # (7496 chats here, 246 with any history), and the thread you want may well
    # be one of them. Ordering puts chats with real history first.
    con = chat_db()
    try:
        rows = con.execute(
            "select c.guid, max(cmj.message_id) as mid, "
            "  group_concat(distinct h.id) as parts "
            "from chat c "
            "join chat_handle_join chj on chj.chat_id = c.ROWID "
            "join handle h on h.ROWID = chj.handle_id "
            "left join chat_message_join cmj on cmj.chat_id = c.ROWID "
            "group by c.ROWID order by mid is null, mid desc"
        ).fetchall()
    finally:
        con.close()

    for r in rows:
        parts = set(
            k for k in (handle_key(p) for p in (r["parts"] or "").split(",")) if k
        )
        if parts == want:
            return r["guid"]
    return None


def mark_unread(guid):
    """Rewind our seen marker to just before the last incoming message.

    Only affects this web UI's badge — chat.db is never written, so Messages.app
    and your other devices keep their own read state.
    """
    con = chat_db()
    try:
        row = con.execute(
            "select max(m.ROWID) from chat_message_join cmj "
            "join message m on m.ROWID = cmj.message_id "
            "where cmj.chat_id = (select ROWID from chat where guid = ?) "
            "and m.is_from_me = 0 and m.item_type = 0",
            (guid,),
        ).fetchone()
    finally:
        con.close()
    last_in = row[0] if row else None
    if not last_in:
        return False
    st = state_db()
    try:
        st.execute(
            "insert into seen values (?,?) on conflict(chat_guid) "
            "do update set rowid_seen = excluded.rowid_seen",
            (guid, last_in - 1),
        )
        st.commit()
    finally:
        st.close()
    return True


# ------------------------------------------------------------------- LAN mode

_net_lock = threading.Lock()
_net = {"at": 0.0, "public": frozenset(), "lan": None, "funnel": None}
CGNAT = ipaddress.ip_network("100.64.0.0/10")    # Tailscale's own addresses
_handoffs = {}
_handoff_lock = threading.Lock()


def tailscale_self():
    """This Mac's public IPs, LAN IP and Funnel URL, as Tailscale sees them.

    `Self.Addrs` holds the WireGuard endpoints Tailscale discovered: the public
    one is our NAT's address (no third-party "what's my IP" call), and the
    private one is the LAN interface. Asking the routing table instead gives
    the 100.x tunnel address whenever Tailscale owns the default route. Cached.
    """
    with _net_lock:
        if time.time() - _net["at"] < NET_TTL:
            return _net
        try:
            out = subprocess.run(
                [TAILSCALE, "status", "--json"],
                capture_output=True, timeout=5,
            ).stdout
            me = json.loads(out or b"{}").get("Self") or {}
        except (OSError, ValueError, subprocess.SubprocessError):
            me = {}
        public, lan = set(), None
        for addr in me.get("Addrs") or []:
            try:
                ip = ipaddress.ip_address(addr.rpartition(":")[0].strip("[]"))
            except ValueError:
                continue
            if ip.is_global:
                public.add(str(ip))
            elif (lan is None and ip.version == 4 and ip.is_private
                  and ip not in CGNAT):
                lan = str(ip)
        dns = (me.get("DNSName") or "").rstrip(".")
        _net.update(at=time.time(), public=frozenset(public), lan=lan,
                    funnel="https://" + dns if dns else None)
        return _net


def new_handoff():
    """One-time code the LAN origin trades for a cookie, so the token itself
    never rides in a plain-http URL."""
    now = time.time()
    code = secrets.token_urlsafe(18)
    with _handoff_lock:
        for k in [k for k, exp in _handoffs.items() if exp < now]:
            del _handoffs[k]
        _handoffs[code] = now + HANDOFF_TTL
    return code


def take_handoff(code):
    with _handoff_lock:
        exp = _handoffs.pop(code, 0)
    return exp > time.time()


# ------------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    server_version = "messages-proxy"
    protocol_version = "HTTP/1.1"

    # Every open tab polls every few seconds; logging those successes was most of
    # the log's growth. Failures on these paths are still logged.
    QUIET_PATHS = ("/api/poll", "/api/match")

    def log_request(self, code="-", size="-"):
        if isinstance(code, int) and code < 400 and self.path.startswith(self.QUIET_PATHS):
            return
        BaseHTTPRequestHandler.log_request(self, code, size)

    def log_message(self, fmt, *args):
        # The default format includes the request line, which carries ?t=<token>.
        msg = re.sub(r"([?&]t=)[^&\s\"]+", r"\1<token>", fmt % args)
        sys.stderr.write("%s %s\n" % (self.address_string(), msg))

    # -- helpers

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        extra = dict(extra or {})
        # Over Funnel bytes are what cost; JSON and the page shrink ~5x.
        if (len(body) > 1024 and ctype.startswith(GZIP_TYPES)
                and "gzip" in (self.headers.get("Accept-Encoding") or "")):
            body = gzip.compress(body, 6)
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, query):
        if not TOKEN:
            return True, None
        if ("imsg=" + TOKEN) in (self.headers.get("Cookie") or ""):
            return True, None
        if (query.get("t") or [None])[0] == TOKEN:
            return True, {"Set-Cookie": COOKIE % TOKEN}
        return False, None

    def _route(self):
        """How this request reached us: funnel, lan, tailnet or local, plus
        the client's real address."""
        ip = self.client_address[0]
        # Only tailscaled, on loopback, is trusted to say a request came via
        # Funnel; the last X-Forwarded-For hop is the one it added.
        if ip in ("127.0.0.1", "::1") and self.headers.get("Tailscale-Funnel-Request"):
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            return "funnel", fwd
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return "lan", ip
        if addr.is_loopback:
            return "local", ip
        if addr in CGNAT:
            return "tailnet", ip
        return "lan", ip

    def _net_info(self):
        via, client = self._route()
        net = tailscale_self()
        lan = net["lan"]
        same_lan = via == "funnel" and bool(lan) and client in net["public"]
        info = {
            "via": via,
            "same_lan": same_lan,
            "lan": "http://%s:%d" % (lan, PORT) if lan else None,
            "funnel": net["funnel"],
            "instance": INSTANCE,
        }
        if same_lan and TOKEN:
            info["handoff"] = new_handoff()
        # Once per page load; this is the line to read when LAN mode doesn't kick in.
        self.log_message("net: via=%s client=%s ours=%s lan=%s same_lan=%s",
                         via, client, ",".join(sorted(net["public"])) or "?",
                         lan, same_lan)
        return info

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # -- routes

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)

        # Icons and the manifest stay open: iOS and Android fetch them without
        # the session cookie, and a 401 there means a blank home-screen tile.
        path = ICON_ALIASES.get(url.path, url.path)
        if path in ICON_ROUTES:
            return self._send(200, icon_png(path), "image/png", ICON_CACHE_HDR)
        if url.path == "/favicon.ico":
            return self._send(200, FAVICON, "image/svg+xml", ICON_CACHE_HDR)
        if url.path == "/manifest.webmanifest":
            return self._send(200, MANIFEST, "application/manifest+json",
                              ICON_CACHE_HDR)
        if url.path == "/ping":
            # Reachability probe for switching between LAN and Funnel; the
            # other origin calls it, hence CORS. Says nothing but which process.
            return self._send(200, {"instance": INSTANCE}, extra={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Private-Network": "true",
            })
        handoff = (q.get("h") or [""])[0]
        if url.path == "/" and handoff and TOKEN:
            if not take_handoff(handoff):
                return self._send(401, "Link expired\n", "text/plain")
            # Drop the code from the address bar; the cookie carries on.
            return self._send(302, "", "text/plain",
                              {"Location": "/", "Set-Cookie": COOKIE % TOKEN})

        ok, cookie = self._authorized(q)
        if not ok:
            return self._send(401, "Unauthorized\n", "text/plain")

        try:
            if url.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8", cookie)
            if url.path == "/api/chats":
                limit = min(int((q.get("limit") or [120])[0]), 500)
                return self._send(200, {"chats": list_chats(limit)}, extra=cookie)
            if url.path == "/api/messages":
                guid = (q.get("chat") or [""])[0]
                after = int((q.get("after") or [0])[0])
                msgs = list_messages(guid, after)
                if msgs is None:
                    return self._send(404, {"error": "no such chat"})
                return self._send(200, {"messages": msgs}, extra=cookie)
            if url.path == "/api/match":
                handles = parse_handles((q.get("handles") or [""])[0])
                guid = (
                    find_chat_for_handles(handles) if len(handles) > 1
                    else find_chat_for_handle(handles[0]) if handles
                    else None
                )
                return self._send(
                    200,
                    {
                        "found": bool(guid),
                        "chat": guid,
                        "names": [pretty_handle(h) for h in handles],
                    },
                    extra=cookie,
                )
            if url.path == "/api/net":
                return self._send(200, self._net_info(), extra=cookie)
            if url.path == "/api/poll":
                return self._send(200, {"max": max_rowid()}, extra=cookie)
            if url.path == "/api/attachment":
                full = (q.get("full") or ["0"])[0] == "1"
                got = attachment_blob(int((q.get("id") or [0])[0]), preview=not full)
                if not got:
                    return self._send(404, "not found\n", "text/plain")
                mime, name, data = got
                # An attachment rowid's bytes never change, so the browser may
                # keep them. With no-store, reopening a thread re-pulled every
                # image over Funnel -- ~10s each from a remote browser.
                return self._send(
                    200, data, mime,
                    {"Content-Disposition": content_disposition(name),
                     "Cache-Control": ATTACH_CACHE},
                )
            return self._send(404, {"error": "not found"})
        except CLIENT_GONE:
            return                    # tab closed or image dropped mid-transfer
        except Exception as exc:  # keep the server up; surface the cause
            self.log_message("error on %s: %r", url.path, exc)
            return self._send(500, {"error": str(exc)})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        ok, cookie = self._authorized(parse_qs(url.query))
        if not ok:
            return self._send(401, "Unauthorized\n", "text/plain")
        try:
            body = self._body()
            if url.path == "/api/send":
                guid = (body.get("chat") or "").strip()
                text = body.get("text") or ""
                if not guid or not text.strip():
                    return self._send(400, {"error": "chat and text required"})
                sent, info = send_message(guid, text)
                if sent:
                    return self._send(200, {"ok": True, "via": info}, extra=cookie)
                return self._send(502, {"ok": False, "error": info})
            if url.path == "/api/new":
                handles = parse_handles(
                    body.get("handles") or body.get("handle") or ""
                )
                text = body.get("text") or ""
                if not handles or not text.strip():
                    return self._send(400, {"error": "handle and text required"})
                if len(handles) > 1:
                    guid = find_chat_for_handles(handles)
                    if not guid:
                        return self._send(409, {"ok": False, "error": NO_GROUP_ERROR})
                    sent, info = send_message(guid, text)
                    if not sent:
                        return self._send(502, {"ok": False, "error": info})
                    return self._send(200, {"ok": True, "chat": guid}, extra=cookie)
                handle = handles[0]
                existing = find_chat_for_handle(handle)
                if existing:
                    # Already have a thread with them; use the normal path.
                    sent, info = send_message(existing, text)
                    if not sent:
                        return self._send(502, {"ok": False, "error": info})
                    return self._send(200, {"ok": True, "chat": existing}, extra=cookie)
                sent, info, guid = start_conversation(handle, text)
                if not sent:
                    return self._send(502, {"ok": False, "error": info})
                return self._send(
                    200, {"ok": True, "chat": guid, "service": info}, extra=cookie
                )
            if url.path == "/api/unread":
                guid = (body.get("chat") or "").strip()
                if not guid:
                    return self._send(400, {"error": "chat required"})
                return self._send(
                    200, {"ok": mark_unread(guid)}, extra=cookie
                )
            if url.path == "/api/seen":
                guid = (body.get("chat") or "").strip()
                upto = int(body.get("upto") or 0)
                if guid and upto:
                    st = state_db()
                    try:
                        st.execute(
                            "insert into seen values (?,?) on conflict(chat_guid) "
                            "do update set rowid_seen = max(rowid_seen, excluded.rowid_seen)",
                            (guid, upto),
                        )
                        st.commit()
                    finally:
                        st.close()
                return self._send(200, {"ok": True}, extra=cookie)
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            self.log_message("error on %s: %r", url.path, exc)
            return self._send(500, {"error": str(exc)})


# -- app icon
#
# One glyph, rendered two ways: an SVG for browser tabs and PNGs for the
# home-screen/manifest sizes. iOS ignores SVG icons, and Pillow is not an
# option here (stdlib only), so the PNGs are rasterized by hand below --
# a gradient rounded square with a white speech bubble on it.

ICON_BG_TOP = (0x4c, 0x8d, 0xff)
ICON_BG_BOT = (0x1d, 0x6b, 0xff)

# Normalized glyph geometry, shared by the SVG and the rasterizer.
BUBBLE = (0.180, 0.200, 0.820, 0.655, 0.150)       # x0, y0, x1, y1, radius
TAIL = ((0.330, 0.600), (0.480, 0.628), (0.238, 0.812))
SQUARE_R = 0.2232                                   # iOS-ish corner radius

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#4c8dff"/><stop offset="1" stop-color="#1d6bff"/>'
    '</linearGradient></defs>'
    '<rect width="64" height="64" rx="14.3" fill="url(#g)"/>'
    '<path d="M38.4 38.4 15.2 52 21.1 41.9Z" fill="#fff"/>'
    '<rect x="11.5" y="12.8" width="41" height="29.1" rx="9.6" fill="#fff"/>'
    '</svg>'
)


def _round_rect_hit(u, v, x0, y0, x1, y1, r):
    if u < x0 or u > x1 or v < y0 or v > y1:
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    dx, dy = u - cx, v - cy
    return dx * dx + dy * dy <= r * r


def _tri_hit(u, v, tri):
    (ax, ay), (bx, by), (cx, cy) = tri
    d1 = (bx - ax) * (v - ay) - (by - ay) * (u - ax)
    d2 = (cx - bx) * (v - by) - (cy - by) * (u - bx)
    d3 = (ax - cx) * (v - cy) - (ay - cy) * (u - cx)
    return (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0)


def _png_bytes(size, rgba):
    """Pack a size x size RGBA buffer into a PNG."""
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)                               # filter type 0
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def render_icon(size, rounded=True, glyph=1.0):
    """Rasterize the icon. rounded=False is full-bleed (iOS and maskable both
    apply their own mask); glyph shrinks the bubble inside the safe zone."""
    ss = 4 if size <= 64 else 2                     # supersampling for edges
    n = float(ss * ss)
    bx0, by0, bx1, by1, br = BUBBLE
    sq_r = SQUARE_R if rounded else 0.0
    rgba = bytearray(size * size * 4)
    i = 0
    for py in range(size):
        for px in range(size):
            sq = bub = 0
            for sy in range(ss):
                v = (py + (sy + 0.5) / ss) / size
                for sx in range(ss):
                    u = (px + (sx + 0.5) / ss) / size
                    if sq_r and not _round_rect_hit(u, v, 0.0, 0.0, 1.0, 1.0, sq_r):
                        continue
                    sq += 1
                    gu, gv = (u - 0.5) / glyph + 0.5, (v - 0.5) / glyph + 0.5
                    if (_round_rect_hit(gu, gv, bx0, by0, bx1, by1, br)
                            or _tri_hit(gu, gv, TAIL)):
                        bub += 1
            if not sq:
                i += 4
                continue
            t = (py + 0.5) / size
            w = bub / float(sq)                     # white coverage, clipped
            for c in range(3):
                g = ICON_BG_TOP[c] + (ICON_BG_BOT[c] - ICON_BG_TOP[c]) * t
                rgba[i + c] = int(g + (255 - g) * w + 0.5)
            rgba[i + 3] = int(255 * sq / n + 0.5)
            i += 4
    return _png_bytes(size, rgba)


# path -> (size, rounded, glyph); rendered on first request, then cached.
ICON_ROUTES = {
    "/icon-32.png": (32, True, 1.0),
    "/icon-180.png": (180, False, 1.0),
    "/icon-192.png": (192, True, 1.0),
    "/icon-512.png": (512, True, 1.0),
    "/icon-maskable-512.png": (512, False, 0.74),
}
ICON_ALIASES = {
    "/apple-touch-icon.png": "/icon-180.png",
    "/apple-touch-icon-precomposed.png": "/icon-180.png",
    "/apple-touch-icon-180x180.png": "/icon-180.png",
}
_ICON_CACHE = {}
_ICON_LOCK = threading.Lock()


def icon_png(path):
    with _ICON_LOCK:
        if path not in _ICON_CACHE:
            _ICON_CACHE[path] = render_icon(*ICON_ROUTES[path])
        return _ICON_CACHE[path]


MANIFEST = {
    "name": "Messages",
    "short_name": "Messages",
    "description": "iMessage and SMS from this Mac, in a browser.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#1c1c1e",
    "theme_color": "#1c1c1e",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/icon-maskable-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "maskable"},
    ],
}

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Messages</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="icon" href="/icon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#f6f6f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#141416" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Messages">
<style>
  :root {
    --bg: #ffffff; --bg-side: #f6f6f7; --line: #e3e3e6; --fg: #1d1d1f;
    --dim: #76767e; --in: #ebebf0; --out: #2b7cff; --out-fg: #fff;
    --accent: #2b7cff; --err: #d1322c;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #1c1c1e; --bg-side: #141416; --line: #2f2f33; --fg: #f2f2f4;
      --dim: #8e8e96; --in: #2c2c30; --out: #2b7cff; --out-fg: #fff; --err: #ff6b63;
    }
  }
  :root[data-theme="dark"] {
    --bg: #1c1c1e; --bg-side: #141416; --line: #2f2f33; --fg: #f2f2f4;
    --dim: #8e8e96; --in: #2c2c30; --out: #2b7cff; --out-fg: #fff; --err: #ff6b63;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--fg); display: flex;
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  #side {
    flex: 0 0 var(--side-w, 300px); min-width: 0;
    background: var(--bg-side); display: flex; flex-direction: column;
  }
  #side h1 {
    padding-top: calc(14px + env(safe-area-inset-top));
    font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--dim); margin: 0; padding: 14px 16px 10px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: center;
  }
  #new {
    border: 0; background: none; color: var(--accent); font: inherit;
    font-size: 12px; font-weight: 600; cursor: pointer; padding: 0;
    text-transform: none; letter-spacing: 0;
  }
  #net {
    border: 0; background: none; font: inherit; font-size: 11px; cursor: pointer;
    padding: 0 0 0 8px; text-transform: none; letter-spacing: 0; color: var(--dim);
  }
  #net.lan { color: #2fa84f; }
  #net.offer { color: var(--accent); font-weight: 600; }
  #to {
    margin: 0 12px 10px; padding: 7px 10px; border: 1px solid var(--accent);
    border-radius: 8px; background: var(--bg); color: var(--fg); font: inherit;
    display: none;
  }
  #head button.ghost {
    border: 1px solid var(--line); background: none; color: var(--dim);
    border-radius: 7px; padding: 4px 9px; font: inherit; font-size: 12px;
    cursor: pointer;
  }
  #head button.ghost:hover { color: var(--fg); }
  #search {
    margin: 0 12px 10px; padding: 7px 10px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--bg); color: var(--fg); font: inherit;
  }
  #chats { overflow-y: auto; flex: 1; }
  .chat {
    padding: 9px 16px; border-bottom: 1px solid var(--line); cursor: pointer;
    display: flex; gap: 8px; align-items: baseline;
  }
  .chat:hover { background: rgba(127,127,140,.1); }
  .chat.sel { background: var(--accent); color: #fff; }
  .chat.sel .preview, .chat.sel .when { color: rgba(255,255,255,.8); }
  .chat .main { min-width: 0; flex: 1; }
  .chat .name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chat .preview {
    color: var(--dim); font-size: 13px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
  }
  .chat .when { color: var(--dim); font-size: 11px; white-space: nowrap; }
  .badge {
    background: var(--accent); color: #fff; border-radius: 10px; padding: 1px 7px;
    font-size: 11px; font-weight: 600;
  }
  .chat.sel .badge { background: #fff; color: var(--accent); }
  #split {
    flex: 0 0 5px; margin: 0 -2px; z-index: 1; cursor: col-resize; touch-action: none;
    background: linear-gradient(to right, transparent 2px, var(--line) 2px, var(--line) 3px, transparent 3px);
  }
  #split:hover, #split.drag { background: var(--accent); opacity: .6; }
  body.resizing { cursor: col-resize; user-select: none; -webkit-user-select: none; }
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #head {
    padding: calc(12px + env(safe-area-inset-top)) 18px 12px; border-bottom: 1px solid var(--line); font-weight: 600;
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
  }
  #head .sub { font-weight: 400; color: var(--dim); font-size: 12px; }
  #thread { flex: 1; overflow-y: auto; padding: 18px; display: flex; flex-direction: column; gap: 3px; }
  .row { display: flex; flex-direction: column; max-width: 68%; }
  .row.me { align-self: flex-end; align-items: flex-end; }
  .row.gap { margin-top: 12px; }
  .bubble {
    padding: 7px 12px; border-radius: 17px; background: var(--in);
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .row.me .bubble { background: var(--out); color: var(--out-fg); }
  .row.sys { align-self: center; max-width: 80%; }
  .row.sys .bubble { background: none; color: var(--dim); font-size: 12px; text-align: center; }
  .meta { font-size: 11px; color: var(--dim); margin: 2px 4px 0; }
  .meta .fail { color: var(--err); font-weight: 600; }
  .bubble img { max-width: 100%; border-radius: 10px; display: block; margin: 4px 0; }
  .note.retry { cursor: pointer; }
  .bubble .note { font-size: 12.5px; opacity: .72; font-style: italic; }
  .bubble a { color: inherit; }
  #composer {
    border-top: 1px solid var(--line); display: flex; gap: 8px;
    padding: 12px 12px calc(12px + env(safe-area-inset-bottom));
  }
  #text {
    flex: 1; resize: none; padding: 9px 12px; border: 1px solid var(--line);
    border-radius: 18px; background: var(--bg); color: var(--fg); font: inherit;
    max-height: 140px;
  }
  #send {
    border: 0; background: var(--accent); color: #fff; border-radius: 18px;
    padding: 0 18px; font: inherit; font-weight: 600; cursor: pointer;
  }
  #send:disabled { opacity: .45; cursor: default; }
  #err { color: var(--err); padding: 0 18px 10px; font-size: 12px; white-space: pre-wrap; }
  .empty { color: var(--dim); margin: auto; }
  @media (max-width: 720px) {
    #side { flex-basis: var(--side-w, 44vw); }
    .row { max-width: 85%; }
  }
</style>
</head><body>
<aside id="side">
  <h1><span>Messages<button id="net" style="display:none"></button></span><button id="new">+ New</button></h1>
  <input id="to" placeholder="Phone numbers or emails, comma-separated" autocomplete="off">
  <input id="search" placeholder="Filter conversations" autocomplete="off">
  <div id="chats"></div>
</aside>
<div id="split" title="Drag to resize, double-click to reset"></div>
<section id="main">
  <div id="head">
    <span id="title">Pick a conversation</span>
    <span class="sub" id="sub"></span>
    <button class="ghost" id="markunread" style="display:none">Mark unread</button>
  </div>
  <div id="thread"><div class="empty">Nothing selected</div></div>
  <div id="err"></div>
  <div id="composer">
    <textarea id="text" rows="1" placeholder="Message" disabled></textarea>
    <button id="send" disabled>Send</button>
  </div>
</section>
<script>
const $ = s => document.querySelector(s);
// Sidebar width: dragged via #split, remembered per browser. Storage can throw
// (private mode, blocked site data), and the layout must work without it.
(() => {
  const split = $("#split"), root = document.documentElement;
  const clamp = w => Math.round(Math.max(160, Math.min(w, innerWidth - 280)));
  const apply = w => root.style.setProperty("--side-w", clamp(w) + "px");
  let saved = null;
  try { saved = parseInt(localStorage.getItem("sideW"), 10); } catch (e) {}
  if (saved > 0) apply(saved);
  split.addEventListener("pointerdown", e => {
    e.preventDefault();
    split.setPointerCapture(e.pointerId);
    split.classList.add("drag"); document.body.classList.add("resizing");
  });
  split.addEventListener("pointermove", e => {
    if (split.classList.contains("drag")) apply(e.clientX);
  });
  const end = e => {
    if (!split.classList.contains("drag")) return;
    split.classList.remove("drag"); document.body.classList.remove("resizing");
    const w = parseInt(root.style.getPropertyValue("--side-w"), 10);
    try { localStorage.setItem("sideW", w); } catch (e) {}
  };
  split.addEventListener("pointerup", end);
  split.addEventListener("pointercancel", end);
  split.addEventListener("dblclick", () => {
    root.style.removeProperty("--side-w");
    try { localStorage.removeItem("sideW"); } catch (e) {}
  });
})();
let chats = [], cur = null, lastId = 0, maxSeen = 0, filter = "", composing = false;

const fmtWhen = ts => {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  if (now - d < 6 * 864e5) return d.toLocaleDateString([], {weekday: "short"});
  return d.toLocaleDateString([], {month: "numeric", day: "numeric"});
};
const fmtFull = ts => ts ? new Date(ts * 1000).toLocaleString([], {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "";

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(body.error || r.status);
    err.status = r.status;
    throw err;
  }
  return body;
}

function splitHandles(value) {
  return (value || "").split(",").map(s => s.trim()).filter(Boolean);
}

function drawChats() {
  const box = $("#chats");
  box.textContent = "";
  const f = filter.toLowerCase();
  for (const c of chats) {
    if (f && !(c.name + " " + c.preview).toLowerCase().includes(f)) continue;
    const el = document.createElement("div");
    el.className = "chat" + (cur && c.guid === cur.guid ? " sel" : "");
    const main = document.createElement("div");
    main.className = "main";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = c.name;
    const prev = document.createElement("div");
    prev.className = "preview";
    prev.textContent = c.preview || " ";
    main.append(name, prev);
    const right = document.createElement("div");
    right.style.textAlign = "right";
    const when = document.createElement("div");
    when.className = "when";
    when.textContent = fmtWhen(c.ts);
    right.append(when);
    if (c.unread) {
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = c.unread;
      right.append(document.createElement("br"), b);
    }
    el.append(main, right);
    el.onclick = () => open(c);
    el.oncontextmenu = e => { e.preventDefault(); markUnread(c.guid); };
    box.append(el);
  }
}

function drawMessages(msgs, append) {
  const box = $("#thread");
  if (!append) box.textContent = "";
  if (!append && !msgs.length) {
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = "No messages";
    box.append(e);
    return;
  }
  let prev = append ? lastTs : 0;
  for (const m of msgs) {
    const row = document.createElement("div");
    row.className = "row" + (m.from_me ? " me" : "") + (m.system ? " sys" : "");
    if (prev && m.ts - prev > 3600) row.classList.add("gap");
    prev = m.ts;
    const bub = document.createElement("div");
    bub.className = "bubble";
    if (m.text) bub.append(document.createTextNode(m.text));
    for (const a of m.attachments) {
      if (!a.available) {
        const note = document.createElement("div");
        note.className = "note";
        note.textContent = "\u{1F4CE} " + (a.name || "attachment") + " — not downloaded to this Mac";
        bub.append(note);
      } else if (a.image) {
        // The <img> gets the downscaled JPEG; the link has the original file.
        const wrap = document.createElement("a");
        wrap.href = "/api/attachment?id=" + a.id + "&full=1";
        wrap.target = "_blank";
        const img = document.createElement("img");
        img.loading = "lazy";
        img.decoding = "async";
        // Images arrive after the thread is scrolled to the bottom and push it
        // up; keep it pinned unless the reader has scrolled away.
        img.onload = () => { if (pinned) box.scrollTop = box.scrollHeight; };
        img.src = "/api/attachment?id=" + a.id;
        img.alt = a.name;
        // Over Funnel a request can be dropped before it reaches the server;
        // retry a couple of times before calling the image unavailable, and
        // leave the note clickable for one more go.
        let tries = 0;
        img.onerror = () => {
          if (tries < 2) {
            tries++;
            setTimeout(() => { img.src = "/api/attachment?id=" + a.id + "&try=" + tries; },
                       1500 * tries);
            return;
          }
          const n = document.createElement("div");
          n.className = "note retry";
          n.textContent = "\u{1F4CE} " + (a.name || "image") + " — unavailable, tap to retry";
          n.onclick = () => {
            tries = 1;
            img.src = "/api/attachment?id=" + a.id + "&try=" + Date.now();
            n.replaceWith(wrap);
          };
          wrap.replaceWith(n);
        };
        wrap.append(img);
        bub.append(wrap);
      } else {
        const link = document.createElement("a");
        link.href = "/api/attachment?id=" + a.id;
        link.textContent = "\u{1F4CE} " + (a.name || "attachment");
        link.target = "_blank";
        bub.append(document.createElement("br"), link);
      }
    }
    if (!bub.childNodes.length) {
      bub.append(Object.assign(document.createElement("div"),
        {className: "note", textContent: "\u{1F4CE} attachment unavailable"}));
    }
    row.append(bub);
    const bits = [fmtFull(m.ts)];
    if (!m.from_me && cur && cur.handles.length > 1 && m.sender) bits.unshift(m.sender);
    if (m.edited) bits.push("edited");
    if (m.unsent) bits.push("unsent");
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = bits.join(" · ");
    if (m.failed) {
      const f = document.createElement("span");
      f.className = "fail";
      f.textContent = " · not delivered";
      meta.append(f);
    }
    row.append(meta);
    box.append(row);
  }
  lastTs = prev;
  box.scrollTop = box.scrollHeight;
  pinned = true;
}
let lastTs = 0, pinned = true;
$("#thread").addEventListener("scroll", e => {
  const b = e.target;
  pinned = b.scrollHeight - b.scrollTop - b.clientHeight < 40;
});

async function markUnread(guid) {
  await api("/api/unread", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chat: guid}),
  }).catch(() => {});
  // Deselect, or the open thread would immediately mark itself seen again.
  if (cur && cur.guid === guid) closeThread();
  await refreshChats();
}

function closeThread() {
  cur = null;
  lastId = 0;
  lastTs = 0;
  composing = false;
  $("#to").style.display = "none";
  $("#title").textContent = "Pick a conversation";
  $("#sub").textContent = "";
  $("#markunread").style.display = "none";
  $("#thread").textContent = "";
  const e = document.createElement("div");
  e.className = "empty";
  e.textContent = "Nothing selected";
  $("#thread").append(e);
  $("#text").disabled = true;
  $("#send").disabled = true;
  drawChats();
}

function startCompose() {
  cur = null;
  composing = true;
  lastId = 0;
  $("#to").style.display = "block";
  $("#to").value = "";
  $("#title").textContent = "New message";
  $("#sub").textContent = "";
  $("#markunread").style.display = "none";
  $("#thread").textContent = "";
  const e = document.createElement("div");
  e.className = "empty";
  e.textContent = "Enter one or more recipients above, comma-separated";
  $("#thread").append(e);
  $("#text").disabled = false;
  $("#send").disabled = false;
  $("#err").textContent = "";
  drawChats();
  $("#to").focus();
}

async function open(c) {
  cur = c;
  composing = false;
  lastId = 0;
  lastTs = 0;
  $("#to").style.display = "none";
  $("#markunread").style.display = "";
  $("#title").textContent = c.name;
  const who = (c.names && c.names.length ? c.names : c.handles).join(", ");
  $("#sub").textContent = [c.service, who].filter(Boolean).join(" · ");
  $("#err").textContent = "";
  $("#text").disabled = false;
  $("#send").disabled = false;
  drawChats();
  // A cached copy draws at once; the fetch below still runs, since sends can
  // fail, messages get edited or unsent, and the cache may be behind.
  const cached = threads.get(c.guid);
  if (cached) showThread(c, cached.messages);
  $("#text").focus();
  const messages = await loadThread(c.guid);
  if (cur !== c || composing) return;           // switched away meanwhile
  if (!cached || cached.json !== threads.get(c.guid).json) showThread(c, messages);
}
function showThread(c, messages) {
  drawMessages(messages, false);
  lastId = messages.length ? messages[messages.length - 1].id : 0;
  if (lastId) markSeen();
}
// guid -> {messages, json}. Memory only: nothing about the threads is left on
// the disk of whatever machine the browser runs on.
const threads = new Map();
async function loadThread(guid) {
  const { messages } = await api("/api/messages?chat=" + encodeURIComponent(guid));
  threads.set(guid, {messages, json: JSON.stringify(messages)});
  return messages;
}
// Fetch the top threads in the background so opening one doesn't wait on a
// round trip (most of a second over Funnel). Only threads that changed since
// they were cached; on the LAN their images are warmed too, which is free.
let prefetching = false;
async function prefetch() {
  if (prefetching) return;
  prefetching = true;
  try {
    for (const c of chats.slice(0, 8)) {
      const t = threads.get(c.guid);
      const have = t && t.messages.length ? t.messages[t.messages.length - 1].id : 0;
      if (have >= c.last_id || (cur && cur.guid === c.guid)) continue;
      const msgs = await loadThread(c.guid).catch(() => null);
      if (msgs && net.fast) {
        for (const m of msgs.slice(-15)) for (const a of m.attachments)
          if (a.image && a.available) new Image().src = "/api/attachment?id=" + a.id;
      }
    }
  } finally { prefetching = false; }
}

function markSeen() {
  if (!cur || !lastId) return;
  fetch("/api/seen", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chat: cur.guid, upto: lastId}),
  }).catch(() => {});
  const c = chats.find(x => x.guid === cur.guid);
  if (c) { c.unread = 0; drawChats(); }
}

async function refreshChats() {
  const { chats: list } = await api("/api/chats");
  chats = list;
  drawChats();
}

async function tick() {
  try {
    const { max } = await api("/api/poll");
    if (max === maxSeen) return;
    maxSeen = max;
    if (cur && lastId) {
      const { messages } = await api(
        "/api/messages?chat=" + encodeURIComponent(cur.guid) + "&after=" + lastId);
      if (messages.length) {
        drawMessages(messages, true);
        lastId = messages[messages.length - 1].id;
        markSeen();
        const t = threads.get(cur.guid);
        if (t) {
          t.messages = t.messages.concat(messages).slice(-300);
          t.json = JSON.stringify(t.messages);
        }
      }
    }
    await refreshChats();
    prefetch();
  } catch (e) {
    failures++;
    if (failures >= 3 && net.via === "lan") fallBackToFunnel();
    return;
  }
  failures = 0;
}

// ---- LAN mode. Through Funnel every request goes out to Tailscale's relay
// and back even when this browser is in the same house. The server says when
// our public IPs match; then the page checks the LAN address answers (with the
// same server instance) and moves there, carrying a one-time login code.
let net = {}, failures = 0;
// An installed app on a phone or tablet would be thrown out of the app (iOS
// opens a Safari sheet) by moving to another origin; a desktop Chrome app
// window just shows the new address in a strip and carries on.
const pinnedApp = matchMedia("(display-mode: standalone)").matches && (
  /iPhone|iPad|iPod|Android/.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));
function prefersFunnel() {
  try { return localStorage.getItem("imsg-net") === "funnel"; } catch (e) { return false; }
}
async function reachable(base, instance) {
  try {
    const ctl = new AbortController();
    setTimeout(() => ctl.abort(), 2500);
    // targetAddressSpace lets Chrome reach a private http address from this
    // https page (asking once for local-network permission); browsers without
    // it refuse as mixed content, and the link is still there to click.
    const r = await fetch(base + "/ping", {signal: ctl.signal, targetAddressSpace: "local"});
    return r.ok && (await r.json()).instance === instance;
  } catch (e) { return false; }
}
async function goLan() {
  const r = await api("/api/net");          // a fresh one-time code
  location.href = r.lan + "/" + (r.handoff ? "?h=" + encodeURIComponent(r.handoff) : "");
}
async function fallBackToFunnel() {
  if (!net.funnel || fallBackToFunnel.busy) return;
  fallBackToFunnel.busy = true;
  try {
    const r = await fetch(net.funnel + "/ping").catch(() => null);
    // Only leave if Funnel answers: if both fail the server is just restarting.
    if (r && r.ok) location.href = net.funnel + "/";
  } finally { fallBackToFunnel.busy = false; }
}
function drawNet() {
  const b = $("#net");
  b.style.display = "none";
  b.className = "";
  if (net.via === "lan" && net.funnel) {
    b.textContent = "· LAN";
    b.className = "lan";
    b.title = "Connected directly over the home network. Click to use the internet address instead.";
    b.onclick = () => { location.href = net.funnel + "/?net=funnel"; };
    b.style.display = "";
  } else if (net.same_lan && !pinnedApp && !net.unreachable) {
    b.textContent = "· use LAN";
    b.className = "offer";
    b.title = "You're on the same network as the Mac: connect to it directly, skipping the internet round trip.";
    b.onclick = () => {
      try { localStorage.removeItem("imsg-net"); } catch (e) {}
      goLan();
    };
    b.style.display = "";
  }
}
async function checkNet() {
  if (new URLSearchParams(location.search).get("net") === "funnel") {
    try { localStorage.setItem("imsg-net", "funnel"); } catch (e) {}
    history.replaceState(null, "", "/");
  }
  try { net = await api("/api/net"); } catch (e) { return; }
  net.fast = net.via === "lan" || net.via === "local";
  drawNet();
  if (!net.same_lan || !net.lan || pinnedApp) return;
  if (await reachable(net.lan, net.instance)) {
    if (!prefersFunnel())
      location.href = net.lan + "/" + (net.handoff ? "?h=" + encodeURIComponent(net.handoff) : "");
    return;
  }
  // With local-network access granted, a failed probe means the LAN really
  // can't be reached from this browser -- a work Chrome sends even private
  // addresses through the corporate proxy, which errors -- so don't offer a
  // link that leads there. Safari can't probe at all and keeps the link.
  try {
    const p = await navigator.permissions.query({name: "local-network-access"});
    if (p.state === "granted") { net.unreachable = true; drawNet(); }
  } catch (e) { /* no such permission in this browser */ }
}

async function send() {
  const text = $("#text").value;
  if (!text.trim()) return;
  const handles = composing ? splitHandles($("#to").value) : [];
  if (composing && !handles.length) { $("#to").focus(); return; }
  if (!composing && !cur) return;
  $("#send").disabled = true;
  $("#err").textContent = "";
  try {
    if (composing) {
      const r = await api("/api/new", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({handles, text}),
      });
      $("#text").value = "";
      $("#text").style.height = "auto";
      await refreshChats();
      const c = chats.find(x => x.guid === r.chat);
      if (c) await open(c); else composing = false;
    } else {
      await api("/api/send", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({chat: cur.guid, text}),
      });
      $("#text").value = "";
      $("#text").style.height = "auto";
      setTimeout(tick, 900);
    }
  } catch (e) {
    // 409 is the "no such group thread" explanation; it speaks for itself.
    $("#err").textContent =
      e.status === 409 ? e.message : "Send failed — " + e.message;
  } finally {
    $("#send").disabled = false;
    $("#text").focus();
  }
}

$("#send").onclick = send;
$("#new").onclick = startCompose;
$("#markunread").onclick = () => { if (cur) markUnread(cur.guid); };
$("#to").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("#text").focus(); }
});
// Resolve recipients while they're typed: a group that doesn't exist can't be
// created from here, so say so before a message gets written.
let matchTimer = null;
$("#to").addEventListener("input", () => {
  clearTimeout(matchTimer);
  const handles = splitHandles($("#to").value);
  if (handles.length < 2) { $("#sub").textContent = ""; return; }
  matchTimer = setTimeout(async () => {
    try {
      const r = await api("/api/match?handles=" + encodeURIComponent(handles.join(",")));
      $("#sub").textContent = r.found
        ? "→ " + r.names.join(", ")
        : "no existing group with these " + handles.length + " people";
    } catch (e) { $("#sub").textContent = ""; }
  }, 250);
});
$("#text").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#text").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px";
});
$("#search").addEventListener("input", e => { filter = e.target.value; drawChats(); });

checkNet();
refreshChats().then(() => api("/api/poll")).then(r => { maxSeen = r.max; prefetch(); });
setInterval(tick, 3000);
</script>
</body></html>
"""


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A browser dropping a connection (Funnel resets, a thread switched
        # mid-load) is routine; only print tracebacks for real failures.
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        super().handle_error(request, client_address)


def main():
    if not os.path.exists(CHAT_DB):
        sys.exit("chat.db not found at %s" % CHAT_DB)
    try:
        chat_db().close()
    except sqlite3.Error as exc:
        sys.exit(
            "cannot read chat.db (%s)\n"
            "Grant Full Disk Access to this Python binary: %s" % (exc, sys.executable)
        )
    srv = Server((BIND, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(
        "messages-proxy on http://%s:%d/%s\n"
        % (BIND, PORT, "?t=<token>" if TOKEN else "")
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
