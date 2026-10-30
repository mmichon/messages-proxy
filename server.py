#!/usr/bin/env python3
"""messages-proxy — read and send iMessage/SMS through a LAN web page.

Reads ~/Library/Messages/chat.db (read-only) for history and incoming messages,
and drives Messages.app via AppleScript to send. Standard library only.

Env:
  IMSG_PORT   port to listen on (default 8787)
  IMSG_BIND   bind address (default 0.0.0.0)
  IMSG_TOKEN  if set, require ?t=<token> once, then a session cookie
"""

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
CHAT_DB = os.path.join(HOME, "Library", "Messages", "chat.db")
ATTACH_ROOT = os.path.join(HOME, "Library", "Messages", "Attachments")
ADDRESSBOOK_GLOB = os.path.join(
    HOME, "Library", "Application Support", "AddressBook", "Sources",
    "*", "AddressBook-v22.abcddb",
)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DB = os.path.join(APP_DIR, "state.db")
SEND_SCRIPT = os.path.join(APP_DIR, "send.applescript")
NEW_SCRIPT = os.path.join(APP_DIR, "new.applescript")

APPLE_EPOCH = 978307200  # 2001-01-01 UTC, in Unix seconds

PORT = int(os.environ.get("IMSG_PORT", "8787"))
BIND = os.environ.get("IMSG_BIND", "0.0.0.0")
TOKEN = os.environ.get("IMSG_TOKEN") or None

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


def chat_db():
    """Read-only connection to chat.db, falling back to a snapshot copy."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % CHAT_DB, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("select 1 from message limit 1").fetchone()
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


def _contact_label(first, last, org, nick):
    name = " ".join(p for p in (first, last) if p).strip()
    return name or nick or org or ""


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

        _contacts["map"] = out
        _contacts["at"] = now
        return out


def pretty_handle(handle):
    """Contact name for a handle, else the handle itself."""
    if not handle:
        return ""
    names = contact_map()
    if "@" in handle:
        return names.get(handle.strip().lower(), handle)
    return names.get(_phone_key(handle), handle)


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
    for r in rows:
        name = r["transfer_name"] or ""
        if name.endswith(".pluginPayloadAttachment"):
            continue  # link previews and app payloads, not real files
        mime = r["mime_type"] or mimetypes.guess_type(name)[0] or ""
        # Attachments can be iCloud-offloaded or purged; the row outlives the file.
        local = r["filename"] and os.path.isfile(os.path.expanduser(r["filename"]))
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


def attachment_blob(aid):
    con = chat_db()
    try:
        row = con.execute(
            "select filename, mime_type, transfer_name from attachment where ROWID = ?",
            (aid,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row["filename"]:
        return None
    path = os.path.realpath(os.path.expanduser(row["filename"]))
    root = os.path.realpath(ATTACH_ROOT)
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    name = row["transfer_name"] or os.path.basename(path)
    mime = row["mime_type"] or mimetypes.guess_type(name)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        return mime, name, fh.read()


# --------------------------------------------------------------------- sending

def send_message(guid, text):
    """Try the chat's own GUID, then per-service variants of its handle."""
    candidates = [guid]
    if ";" in guid:
        handle = guid.rsplit(";", 1)[-1]
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


# ------------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    server_version = "messages-proxy"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
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
            return True, {"Set-Cookie": "imsg=%s; Path=/; HttpOnly; SameSite=Lax" % TOKEN}
        return False, None

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
        ok, cookie = self._authorized(q)
        if not ok:
            return self._send(401, "Unauthorized\n", "text/plain")

        try:
            if url.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8", cookie)
            if url.path == "/favicon.ico":
                return self._send(200, FAVICON, "image/svg+xml")
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
            if url.path == "/api/poll":
                return self._send(200, {"max": max_rowid()}, extra=cookie)
            if url.path == "/api/attachment":
                got = attachment_blob(int((q.get("id") or [0])[0]))
                if not got:
                    return self._send(404, "not found\n", "text/plain")
                mime, name, data = got
                return self._send(
                    200, data, mime,
                    {"Content-Disposition": 'inline; filename="%s"' % name.replace('"', "")},
                )
            return self._send(404, {"error": "not found"})
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
                handle = (body.get("handle") or "").strip()
                text = body.get("text") or ""
                if not handle or not text.strip():
                    return self._send(400, {"error": "handle and text required"})
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


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#2b7cff"/>'
    '<path d="M32 14c-10 0-18 6.5-18 14.5 0 4.6 2.7 8.7 6.9 11.3-.4 2.6-1.6 5.3-3.4 7.4'
    ' 3.9-.6 7.3-2.3 9.8-4.3 1.5.3 3.1.5 4.7.5 10 0 18-6.5 18-14.9S42 14 32 14z"'
    ' fill="#fff"/></svg>'
)

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Messages</title>
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
    width: 300px; flex: 0 0 300px; border-right: 1px solid var(--line);
    background: var(--bg-side); display: flex; flex-direction: column;
  }
  #side h1 {
    font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--dim); margin: 0; padding: 14px 16px 10px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: center;
  }
  #new {
    border: 0; background: none; color: var(--accent); font: inherit;
    font-size: 12px; font-weight: 600; cursor: pointer; padding: 0;
    text-transform: none; letter-spacing: 0;
  }
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
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #head {
    padding: 12px 18px; border-bottom: 1px solid var(--line); font-weight: 600;
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
  .bubble .note { font-size: 12.5px; opacity: .72; font-style: italic; }
  .bubble a { color: inherit; }
  #composer { border-top: 1px solid var(--line); padding: 12px; display: flex; gap: 8px; }
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
    #side { width: 44vw; flex-basis: 44vw; }
    .row { max-width: 85%; }
  }
</style>
</head><body>
<aside id="side">
  <h1>Messages <button id="new">+ New</button></h1>
  <input id="to" placeholder="Phone number or email, then type below" autocomplete="off">
  <input id="search" placeholder="Filter conversations" autocomplete="off">
  <div id="chats"></div>
</aside>
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
  if (!r.ok) throw new Error(body.error || r.status);
  return body;
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
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/api/attachment?id=" + a.id;
        img.alt = a.name;
        img.onerror = () => {
          const n = document.createElement("div");
          n.className = "note";
          n.textContent = "\u{1F4CE} " + (a.name || "image") + " — unavailable";
          img.replaceWith(n);
        };
        bub.append(img);
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
}
let lastTs = 0;

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
  e.textContent = "Enter a phone number or email above";
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
  $("#sub").textContent = [c.service, c.handles.join(", ")].filter(Boolean).join(" · ");
  $("#err").textContent = "";
  $("#text").disabled = false;
  $("#send").disabled = false;
  drawChats();
  const { messages } = await api("/api/messages?chat=" + encodeURIComponent(c.guid));
  drawMessages(messages, false);
  if (messages.length) {
    lastId = messages[messages.length - 1].id;
    markSeen();
  }
  $("#text").focus();
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
      }
    }
    await refreshChats();
  } catch (e) { /* transient; next tick retries */ }
}

async function send() {
  const text = $("#text").value;
  if (!text.trim()) return;
  const handle = composing ? $("#to").value.trim() : null;
  if (composing && !handle) { $("#to").focus(); return; }
  if (!composing && !cur) return;
  $("#send").disabled = true;
  $("#err").textContent = "";
  try {
    if (composing) {
      const r = await api("/api/new", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({handle, text}),
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
    $("#err").textContent = "Send failed — " + e.message;
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
$("#text").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#text").addEventListener("input", e => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px";
});
$("#search").addEventListener("input", e => { filter = e.target.value; drawChats(); });

refreshChats().then(() => api("/api/poll")).then(r => { maxSeen = r.max; });
setInterval(tick, 3000);
</script>
</body></html>
"""


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
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write(
        "messages-proxy on http://%s:%d/%s\n"
        % (BIND, PORT, ("?t=" + TOKEN) if TOKEN else "")
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
