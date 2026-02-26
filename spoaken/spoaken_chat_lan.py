"""
spoaken_chat_lan.py
────────────────────────────────────────────────────────────────────────────────
Spoaken LAN Chat  —  WebSocket edition  (v4.0)

Architecture
────────────
  SpoakenLANServer   asyncio WebSocket server. Hosts rooms on the LAN.
                     Any peer can run one. No internet required.
  SpoakenLANClient   Connects to a LAN server via WebSocket.
  LANServerBeacon    UDP broadcast — announces the server on the LAN.
  LANServerScanner   UDP listener  — discovers servers on the LAN.
  ChatDB             SQLite persistence (rooms, events, files, bans).

Privacy model
─────────────
  • Server never logs IP addresses to disk — only ephemeral audit in RAM.
  • Usernames are the ONLY identity. No email, no real name, no device ID.
  • Room membership lists are not sent to other members.
  • Messages contain only sender username and timestamp — no IP metadata.
  • All traffic is over WebSocket (wss:// if TLS cert provided, otherwise ws://).
  • HMAC-SHA256 token handshake prevents unauthenticated connections.
  • Room passwords are PBKDF2-HMAC-SHA256 (100k iterations + per-room salt).
  • In-memory rate limiting; no rate-limit data written to disk.
  • Transfer files are stored only in the local Logs dir on the SERVER side.
    Filenames are content-addressed (SHA-256 hex), originals are never kept
    by the server under their original path — metadata stored in SQLite only.
  • This file can be deleted entirely to disable LAN chat capability.

Protocol
────────
  JSON messages over WebSocket frames (no length-prefix needed).
  Client→Server:  c.*  prefixed types.
  Server→Client:  m.*  prefixed types.
  File chunks:    base64-encoded inside JSON (64 KB per chunk).
  Max file size:  50 MB.

Rooms
─────
  Room IDs    : !<8hex>:lan
  Event IDs   : $<ts_ms>_<6hex>:lan
  User aliases: username only (no IP ever embedded in user-facing IDs)
  Roles       : admin | member

File transfer
─────────────
  Spoaken-produced files (.txt, .md, .json, .wav summary, .log) are
  the primary use case.  Any file ≤ 50 MB is accepted.
  Files are stored on the server under a content-addressed name (SHA-256).
  Room members can list and download stored files.

Public API
──────────
  SpoakenLANServer(port, token, name, db_path, log_cb)
    .start()  → bool
    .stop()
    .is_open() → bool
    .peer_count() → int

  SpoakenLANClient(username, server_token, on_event, log_cb)
    .connect(host, port) → bool
    .disconnect()
    .is_connected() → bool
    .send_message(room_id, text)
    .create_room(name, password, public, topic)
    .join_room(room_id, password)
    .leave_room(room_id)
    .list_rooms()
    .send_file(room_id, filepath)       # Spoaken transcript / log etc.
    .list_files(room_id)
    .download_file(room_id, file_id, dest_path)

  discover_servers(wait=2.0) → List[LANServerEntry]

Deleting / disabling
────────────────────
  Delete this file (or set  chat_lan_enabled: false  in spoaken_config.json)
  to completely disable LAN chat. spoaken_gui.py checks for the file before
  importing and degrades gracefully.
"""

from __future__ import annotations

# ── Standard library only — no extra installs needed for the base protocol ───
import asyncio
import base64
import collections
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

# ── Optional websockets package (pip install websockets) ─────────────────────
try:
    import websockets                        # type: ignore
    import websockets.server                 # type: ignore
    import websockets.exceptions             # type: ignore
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

# ── Paths ─────────────────────────────────────────────────────────────────────
try:
    from paths import LOG_DIR
except ImportError:
    LOG_DIR = Path(__file__).parent.parent / "Logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)
_TRANSFER_DIR = LOG_DIR / "lan_transfers"
_TRANSFER_DIR.mkdir(parents=True, exist_ok=True)

_log = logging.getLogger("spoaken.chat.lan")

# ── Protocol constants ────────────────────────────────────────────────────────
_PROTO_VERSION       = "4.0-ws"
_REALM               = "lan"
_DISCOVERY_PORT      = 55302
_DISCOVERY_TTL       = 14.0
_DISCOVERY_INTERVAL  = 8.0
_MAX_CONN_PER_IP     = 8
_MAX_MSG_LEN         = 8192
_RATE_LIMIT_PER_SEC  = 20
_AUTH_TIMEOUT_S      = 18.0
_RECV_TIMEOUT_S      = 120.0
_CHUNK_B64_BYTES     = 65536           # base64-decoded chunk size
_MAX_FILE_BYTES      = 50 * 1024 * 1024
_MAX_HISTORY         = 250
_MAX_SEARCH_HITS     = 100
_PBKDF2_ITERS        = 100_000
_CTRL_RE             = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_room_id()  -> str: return f"!{secrets.token_hex(8)}:{_REALM}"
def _make_event_id() -> str: return f"${int(time.time()*1000)}_{secrets.token_hex(3)}:{_REALM}"
def _now_ms()        -> int: return int(time.time() * 1000)

def _sanitise(raw: str, maxlen: int = _MAX_MSG_LEN) -> str:
    return _CTRL_RE.sub("", raw).strip()[:maxlen]

def _hash_room_pw(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256 room password hash."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"),
        _PBKDF2_ITERS
    ).hex()

def _hmac_sign(secret: str, challenge: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), challenge, hashlib.sha256).digest()

def _sha256_file(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════════

@dataclasses.dataclass
class SpoakenRoom:
    room_id      : str
    name         : str
    creator      : str          # username only — no IP
    password_hash: str
    password_salt: str
    public       : bool
    created_at   : int
    topic        : str = ""
    members      : Dict[str, str] = dataclasses.field(default_factory=dict)

    def display(self) -> dict:
        return {
            "room_id"     : self.room_id,
            "name"        : self.name,
            "topic"       : self.topic,
            "creator"     : self.creator,
            "public"      : self.public,
            "member_count": len(self.members),
            "created_at"  : self.created_at,
        }


@dataclasses.dataclass
class SpoakenUser:
    username   : str            # only identity we keep
    ws         : object         # websockets connection object
    rooms      : List[str]      = dataclasses.field(default_factory=list)
    msg_times  : deque          = dataclasses.field(
        default_factory=lambda: deque(maxlen=_RATE_LIMIT_PER_SEC + 1)
    )


@dataclasses.dataclass
class ChatEvent:
    event_id : str
    room_id  : str
    sender   : str
    type     : str
    content  : dict
    timestamp: int

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class FileTransfer:
    transfer_id  : str
    room_id      : str
    sender       : str
    filename     : str
    declared_size: int
    checksum     : str
    chunks       : List[bytes] = dataclasses.field(default_factory=list)
    received     : int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# SQLite persistence  (server-side only — no user data except username + role)
# ═══════════════════════════════════════════════════════════════════════════════

class ChatDB:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS rooms (
        room_id TEXT PRIMARY KEY, name TEXT NOT NULL,
        creator TEXT NOT NULL, password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL, topic TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS members (
        room_id TEXT NOT NULL, username TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member', joined_at INTEGER NOT NULL,
        PRIMARY KEY (room_id, username)
    );
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL,
        sender TEXT NOT NULL, type TEXT NOT NULL,
        content TEXT NOT NULL, timestamp INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_evt_room ON events(room_id, timestamp);
    CREATE TABLE IF NOT EXISTS files (
        file_id TEXT PRIMARY KEY, room_id TEXT NOT NULL,
        sender TEXT NOT NULL, filename TEXT NOT NULL,
        size INTEGER NOT NULL, checksum TEXT NOT NULL,
        stored_name TEXT NOT NULL, uploaded_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS banned (
        room_id TEXT NOT NULL, username TEXT NOT NULL,
        banned_by TEXT NOT NULL, reason TEXT DEFAULT '',
        banned_at INTEGER NOT NULL, PRIMARY KEY (room_id, username)
    );
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def save_room(self, r: SpoakenRoom):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO rooms VALUES (?,?,?,?,?,?,?,?)",
                (r.room_id, r.name, r.creator, r.password_hash,
                 r.password_salt, int(r.public), r.created_at, r.topic)
            )
            self._conn.commit()

    def load_rooms(self) -> List[SpoakenRoom]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM rooms").fetchall()
        result = []
        for r in rows:
            room = SpoakenRoom(
                room_id=r["room_id"], name=r["name"], creator=r["creator"],
                password_hash=r["password_hash"], password_salt=r["password_salt"],
                public=bool(r["public"]), created_at=r["created_at"],
                topic=r["topic"] or ""
            )
            room.members = self.load_members(r["room_id"])
            result.append(room)
        return result

    def delete_room(self, room_id: str):
        with self._lock:
            for tbl in ("members", "events", "files", "banned", "rooms"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE room_id=?", (room_id,))
            self._conn.commit()

    def add_member(self, room_id: str, username: str, role: str = "member"):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO members VALUES (?,?,?,?)",
                (room_id, username, role, _now_ms())
            )
            self._conn.commit()

    def remove_member(self, room_id: str, username: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM members WHERE room_id=? AND username=?",
                (room_id, username)
            )
            self._conn.commit()

    def load_members(self, room_id: str) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, role FROM members WHERE room_id=?", (room_id,)
            ).fetchall()
        return {r["username"]: r["role"] for r in rows}

    def is_banned(self, room_id: str, username: str) -> bool:
        with self._lock:
            return bool(self._conn.execute(
                "SELECT 1 FROM banned WHERE room_id=? AND username=?",
                (room_id, username)
            ).fetchone())

    def ban_member(self, room_id: str, username: str, by: str, reason: str = ""):
        with self._lock:
            self._conn.execute(
                "DELETE FROM members WHERE room_id=? AND username=?",
                (room_id, username)
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO banned VALUES (?,?,?,?,?)",
                (room_id, username, by, reason, _now_ms())
            )
            self._conn.commit()

    def save_event(self, ev: ChatEvent):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                (ev.event_id, ev.room_id, ev.sender, ev.type,
                 json.dumps(ev.content), ev.timestamp)
            )
            self._conn.commit()

    def get_history(self, room_id: str, limit: int = _MAX_HISTORY) -> List[ChatEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE room_id=? ORDER BY timestamp DESC LIMIT ?",
                (room_id, limit)
            ).fetchall()
        return [ChatEvent(
            event_id=r["event_id"], room_id=r["room_id"], sender=r["sender"],
            type=r["type"], content=json.loads(r["content"]),
            timestamp=r["timestamp"]
        ) for r in reversed(rows)]

    def save_file(self, file_id: str, room_id: str, sender: str,
                  filename: str, size: int, checksum: str, stored_name: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?,?)",
                (file_id, room_id, sender, filename, size,
                 checksum, stored_name, _now_ms())
            )
            self._conn.commit()

    def list_files(self, room_id: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_id, room_id, sender, filename, size, checksum, "
                "stored_name, uploaded_at FROM files WHERE room_id=? "
                "ORDER BY uploaded_at DESC",
                (room_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_file(self, file_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE file_id=?", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# UDP Discovery
# ═══════════════════════════════════════════════════════════════════════════════

class LANServerBeacon:
    """Broadcasts this server's existence on the LAN via UDP."""

    def __init__(self, ws_port: int, server_name: str,
                 room_count_fn: Callable[[], int]):
        self._port       = ws_port
        self._name       = server_name
        self._room_count = room_count_fn
        self._running    = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True,
                         name="lan-beacon").start()

    def stop(self):
        self._running = False

    def _loop(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            while self._running:
                payload = "|".join([
                    "SPOAKEN-WS", _PROTO_VERSION, self._name,
                    str(self._port), str(self._room_count()),
                ]).encode("utf-8")
                try:
                    sock.sendto(payload, ("<broadcast>", _DISCOVERY_PORT))
                except OSError:
                    pass
                for _ in range(int(_DISCOVERY_INTERVAL)):
                    if not self._running:
                        break
                    time.sleep(1.0)
        finally:
            try:
                sock.close()
            except Exception:
                pass


@dataclasses.dataclass
class LANServerEntry:
    ip        : str
    port      : int
    name      : str
    room_count: int
    last_seen : float = dataclasses.field(default_factory=time.time)

    def is_alive(self) -> bool:
        return (time.time() - self.last_seen) < _DISCOVERY_TTL

    def display(self) -> dict:
        return {"ip": self.ip, "port": self.port, "name": self.name,
                "room_count": self.room_count,
                "address": f"{self.ip}:{self.port}"}


class LANServerScanner:
    """Listens for beacon packets and builds a live server list."""

    def __init__(self):
        self._servers : Dict[str, LANServerEntry] = {}
        self._lock    = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True,
                         name="lan-scanner").start()

    def stop(self):
        self._running = False

    def get_servers(self) -> List[LANServerEntry]:
        with self._lock:
            return [e for e in self._servers.values() if e.is_alive()]

    def _loop(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass
            sock.bind(("", _DISCOVERY_PORT))
            sock.settimeout(1.0)
            while self._running:
                try:
                    data, addr = sock.recvfrom(512)
                except socket.timeout:
                    continue
                try:
                    parts = data.decode("utf-8").split("|")
                    if len(parts) < 5 or parts[0] != "SPOAKEN-WS":
                        continue
                    _, _, name, port_s, rooms_s = parts[:5]
                    key = f"{addr[0]}:{port_s}"
                    with self._lock:
                        self._servers[key] = LANServerEntry(
                            ip=addr[0], port=int(port_s), name=name,
                            room_count=int(rooms_s), last_seen=time.time()
                        )
                except Exception:
                    pass
        finally:
            try:
                sock.close()
            except Exception:
                pass


def discover_servers(wait: float = 2.0) -> List[LANServerEntry]:
    """One-shot scan — returns servers found within *wait* seconds."""
    scanner = LANServerScanner()
    scanner.start()
    time.sleep(wait)
    scanner.stop()
    return scanner.get_servers()


# ═══════════════════════════════════════════════════════════════════════════════
# SpoakenLANServer  (asyncio WebSocket)
# ═══════════════════════════════════════════════════════════════════════════════

class SpoakenLANServer:
    """
    WebSocket LAN group-chat server.

    Usage::

        server = SpoakenLANServer(port=55300, token="secret",
                                  server_name="Lab A")
        server.start()
        ...
        server.stop()
    """

    def __init__(
        self,
        port        : int  = 55300,
        token       : str  = "spoaken",
        server_name : str  = "Spoaken LAN",
        db_path     : Optional[Path] = None,
        log_cb      : Callable[[str], None] = print,
        bind_address: str  = "0.0.0.0",
        ssl_context = None,   # pass ssl.SSLContext for wss://
    ):
        if not _WS_AVAILABLE:
            raise ImportError(
                "websockets package required: pip install websockets"
            )
        self._port    = port
        self._token   = token
        self._name    = server_name
        self._log     = log_cb
        self._bind    = bind_address
        self._ssl     = ssl_context

        _db = db_path or (LOG_DIR / "spoaken_lan.db")
        self._db = ChatDB(_db)

        # Runtime state — no IP addresses stored in these dicts
        self._rooms         : Dict[str, SpoakenRoom] = {}
        self._users         : Dict[object, SpoakenUser] = {}  # ws → user
        self._users_by_name : Dict[str, SpoakenUser] = {}
        self._transfers     : Dict[str, FileTransfer] = {}

        # Ephemeral IP-level security counters (RAM only, not persisted)
        self._ip_conn_count : Dict[str, int] = defaultdict(int)
        self._ip_strikes    : Dict[str, int] = defaultdict(int)
        self._banned_ips    : set = set()

        self._lock     = threading.Lock()
        self._running  = False
        self._beacon   : Optional[LANServerBeacon] = None
        self._loop     : Optional[asyncio.AbstractEventLoop] = None
        self._thread   : Optional[threading.Thread] = None

        self._load_persisted_rooms()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._running:
            return False
        if not _WS_AVAILABLE:
            self._log("[LAN Server Error]: websockets not installed")
            return False
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop, daemon=True, name="lan-ws-server"
        )
        self._thread.start()
        # Give the loop a moment to bind
        time.sleep(0.3)
        if not self._running:
            return False
        self._beacon = LANServerBeacon(
            self._port, self._name, lambda: len(self._rooms)
        )
        self._beacon.start()
        self._log(f"[LAN Server]: '{self._name}' ws://{self._bind}:{self._port}")
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._beacon:
            self._beacon.stop()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._db.close()
        self._log(f"[LAN Server]: '{self._name}' offline")

    def is_open(self)    -> bool: return self._running
    def peer_count(self) -> int:
        with self._lock:
            return len(self._users)

    # ── asyncio thread ─────────────────────────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            self._log(f"[LAN Server Error]: {exc}")
        finally:
            self._running = False
            self._loop.close()

    async def _serve(self):
        async with websockets.serve(
            self._handle_client,
            self._bind,
            self._port,
            ssl=self._ssl,
            max_size=_MAX_FILE_BYTES + 131072,
            ping_interval=30,
            ping_timeout=10,
        ):
            while self._running:
                await asyncio.sleep(0.5)

    # ── Client handler ─────────────────────────────────────────────────────────

    async def _handle_client(self, ws, path="/"):
        # Extract IP — used only for rate limiting, never stored to disk
        ip = ws.remote_address[0] if ws.remote_address else "unknown"

        with self._lock:
            if ip in self._banned_ips:
                await ws.close(1008, "Banned")
                return
            if self._ip_conn_count[ip] >= _MAX_CONN_PER_IP:
                await ws.close(1008, "Too many connections")
                return
            self._ip_conn_count[ip] += 1

        user: Optional[SpoakenUser] = None
        try:
            # ── HMAC challenge/response auth ───────────────────────────────────
            challenge = os.urandom(32)
            await asyncio.wait_for(
                ws.send(json.dumps({
                    "type"   : "m.auth.challenge",
                    "content": {
                        "challenge": challenge.hex(),
                        "version"  : _PROTO_VERSION,
                        "server"   : self._name,
                    },
                })),
                timeout=_AUTH_TIMEOUT_S,
            )
            raw  = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
            ev   = json.loads(raw)
            if ev.get("type") != "c.auth":
                await ws.close(1008, "Auth required")
                return

            content  = ev.get("content", {})
            username = _sanitise(content.get("username", ""), 32)
            response = content.get("response", "")
            expected = _hmac_sign(self._token, challenge).hex()

            if not username or not hmac.compare_digest(response, expected):
                with self._lock:
                    self._ip_strikes[ip] += 1
                    if self._ip_strikes[ip] >= 5:
                        self._banned_ips.add(ip)
                await ws.send(json.dumps({
                    "type"   : "m.error",
                    "content": {"code": "M_UNAUTHORIZED", "error": "Auth failed."},
                }))
                await ws.close(1008, "Auth failed")
                return

            with self._lock:
                if username in self._users_by_name:
                    await ws.send(json.dumps({
                        "type"   : "m.error",
                        "content": {"code": "M_USER_IN_USE",
                                    "error": f"'{username}' is already connected."},
                    }))
                    await ws.close(1008, "Name taken")
                    return
                self._ip_strikes[ip] = 0
                user = SpoakenUser(username=username, ws=ws)
                self._users[ws]             = user
                self._users_by_name[username] = user

            await ws.send(json.dumps({
                "type"   : "m.auth.ok",
                "content": {"username": username, "version": _PROTO_VERSION,
                             "server_name": self._name},
            }))
            self._log(f"[LAN]: ✔ {username} connected")

            # ── Dispatch loop ──────────────────────────────────────────────────
            async for raw_msg in ws:
                now = time.monotonic()
                user.msg_times.append(now)
                if (len(user.msg_times) > _RATE_LIMIT_PER_SEC
                        and (now - user.msg_times[0]) < 1.0):
                    await ws.send(json.dumps({
                        "type"   : "m.error",
                        "content": {"code": "M_RATE_LIMITED", "error": "Slow down."},
                    }))
                    continue
                try:
                    ev = json.loads(raw_msg)
                    await self._dispatch(user, ev)
                except (json.JSONDecodeError, Exception) as exc:
                    _log.debug("dispatch error: %s", exc)

        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            _log.debug("client error: %s", exc)
        finally:
            if user:
                await self._disconnect_user(user)
            with self._lock:
                self._ip_conn_count[ip] = max(0, self._ip_conn_count[ip] - 1)

    # ── Dispatcher ─────────────────────────────────────────────────────────────

    async def _dispatch(self, user: SpoakenUser, ev: dict):
        t       = ev.get("type", "")
        content = ev.get("content", {})
        room_id = ev.get("room_id", "")
        handlers = {
            "c.room.create"  : self._on_room_create,
            "c.room.join"    : self._on_room_join,
            "c.room.leave"   : self._on_room_leave,
            "c.room.list"    : self._on_room_list,
            "c.room.history" : self._on_room_history,
            "c.room.topic"   : self._on_room_topic,
            "c.room.kick"    : self._on_room_kick,
            "c.room.ban"     : self._on_room_ban,
            "c.room.promote" : self._on_room_promote,
            "c.room.files"   : self._on_room_files,
            "c.message"      : self._on_message,
            "c.file.begin"   : self._on_file_begin,
            "c.file.chunk"   : self._on_file_chunk,
            "c.file.end"     : self._on_file_end,
            "c.file.get"     : self._on_file_get,
            "c.users"        : self._on_users,
            "c.ping"         : self._on_ping,
        }
        fn = handlers.get(t)
        if fn:
            await fn(user, content, room_id)

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _send(self, user: SpoakenUser, msg: dict):
        try:
            await user.ws.send(json.dumps(msg))
        except Exception:
            pass

    async def _broadcast(self, room_id: str, msg: dict,
                         exclude: Optional[str] = None):
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return
            members = list(room.members.keys())
        for uname in members:
            if uname == exclude:
                continue
            with self._lock:
                u = self._users_by_name.get(uname)
            if u:
                await self._send(u, msg)

    async def _err(self, user: SpoakenUser, code: str, msg: str):
        await self._send(user, {
            "type": "m.error",
            "content": {"code": code, "error": msg},
        })

    async def _disconnect_user(self, user: SpoakenUser):
        self._log(f"[LAN]: {user.username} disconnected")
        with self._lock:
            self._users.pop(user.ws, None)
            self._users_by_name.pop(user.username, None)
            rooms_to_leave = list(user.rooms)
        for rid in rooms_to_leave:
            await self._leave_room(user, rid)
        try:
            await user.ws.close()
        except Exception:
            pass

    # ── Room handlers ──────────────────────────────────────────────────────────

    async def _on_room_create(self, user: SpoakenUser, c: dict, _: str):
        name     = _sanitise(c.get("name", ""), 80)
        password = c.get("password", "")
        public   = bool(c.get("public", True))
        topic    = _sanitise(c.get("topic", ""), 200)
        if not name:
            await self._err(user, "M_BAD_PARAM", "Room name required.")
            return
        if not password:
            await self._err(user, "M_BAD_PARAM", "Room password required.")
            return
        salt = secrets.token_hex(16)
        room = SpoakenRoom(
            room_id      = _make_room_id(),
            name         = name,
            creator      = user.username,
            password_hash= _hash_room_pw(password, salt),
            password_salt= salt,
            public       = public,
            created_at   = _now_ms(),
            topic        = topic,
            members      = {user.username: "admin"},
        )
        with self._lock:
            self._rooms[room.room_id] = room
        self._db.save_room(room)
        self._db.add_member(room.room_id, user.username, "admin")
        with self._lock:
            user.rooms.append(room.room_id)
        await self._send(user, {
            "type"   : "m.room.created",
            "content": {"room_id": room.room_id, "name": name},
        })
        self._log(f"[LAN]: room '{name}' created by {user.username}")

    async def _on_room_join(self, user: SpoakenUser, c: dict, _: str):
        room_id  = c.get("room_id", "")
        password = c.get("password", "")
        with self._lock:
            room = self._rooms.get(room_id)
        if not room:
            await self._err(user, "M_NOT_FOUND", "Room not found.")
            return
        if self._db.is_banned(room_id, user.username):
            await self._err(user, "M_BANNED", "You are banned from this room.")
            return
        expected_hash = _hash_room_pw(password, room.password_salt)
        if not hmac.compare_digest(expected_hash, room.password_hash):
            await self._err(user, "M_FORBIDDEN", "Incorrect room password.")
            return
        with self._lock:
            if user.username not in room.members:
                room.members[user.username] = "member"
            if room_id not in user.rooms:
                user.rooms.append(room_id)
        self._db.add_member(room_id, user.username)
        history = self._db.get_history(room_id, limit=50)
        await self._send(user, {
            "type"   : "m.room.joined",
            "room_id": room_id,
            "content": {
                "name"   : room.name,
                "topic"  : room.topic,
                "history": [e.to_dict() for e in history],
            },
        })
        await self._broadcast(room_id, {
            "type"   : "m.room.member",
            "room_id": room_id,
            "content": {"username": user.username, "membership": "joined"},
        }, exclude=user.username)

    async def _on_room_leave(self, user: SpoakenUser, c: dict, room_id: str):
        await self._leave_room(user, room_id)

    async def _leave_room(self, user: SpoakenUser, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return
            room.members.pop(user.username, None)
            if room_id in user.rooms:
                user.rooms.remove(room_id)
        self._db.remove_member(room_id, user.username)
        await self._broadcast(room_id, {
            "type"   : "m.room.member",
            "room_id": room_id,
            "content": {"username": user.username, "membership": "left"},
        })

    async def _on_room_list(self, user: SpoakenUser, c: dict, _: str):
        with self._lock:
            rooms = [r.display() for r in self._rooms.values() if r.public]
        await self._send(user, {
            "type"   : "m.room.list",
            "content": {"rooms": rooms},
        })

    async def _on_room_history(self, user: SpoakenUser, c: dict, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or user.username not in room.members:
            await self._err(user, "M_FORBIDDEN", "Not in room.")
            return
        history = self._db.get_history(room_id, limit=c.get("limit", 100))
        await self._send(user, {
            "type"   : "m.room.history",
            "room_id": room_id,
            "content": {"events": [e.to_dict() for e in history]},
        })

    async def _on_room_topic(self, user: SpoakenUser, c: dict, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
        if not room:
            return
        if room.members.get(user.username) != "admin":
            await self._err(user, "M_FORBIDDEN", "Admins only.")
            return
        topic = _sanitise(c.get("topic", ""), 200)
        with self._lock:
            room.topic = topic
        self._db.save_room(room)
        await self._broadcast(room_id, {
            "type"   : "m.room.topic",
            "room_id": room_id,
            "content": {"topic": topic, "by": user.username},
        })

    async def _on_room_kick(self, user: SpoakenUser, c: dict, room_id: str):
        target = _sanitise(c.get("username", ""), 32)
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or room.members.get(user.username) != "admin":
            await self._err(user, "M_FORBIDDEN", "Admins only.")
            return
        if target not in room.members:
            await self._err(user, "M_NOT_FOUND", "User not in room.")
            return
        with self._lock:
            room.members.pop(target, None)
            target_user = self._users_by_name.get(target)
            if target_user and room_id in target_user.rooms:
                target_user.rooms.remove(room_id)
        self._db.remove_member(room_id, target)
        if target_user:
            await self._send(target_user, {
                "type"   : "m.room.kicked",
                "room_id": room_id,
                "content": {"by": user.username},
            })
        await self._broadcast(room_id, {
            "type"   : "m.room.member",
            "room_id": room_id,
            "content": {"username": target, "membership": "kicked"},
        })

    async def _on_room_ban(self, user: SpoakenUser, c: dict, room_id: str):
        target = _sanitise(c.get("username", ""), 32)
        reason = _sanitise(c.get("reason", ""), 200)
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or room.members.get(user.username) != "admin":
            await self._err(user, "M_FORBIDDEN", "Admins only.")
            return
        self._db.ban_member(room_id, target, user.username, reason)
        with self._lock:
            room.members.pop(target, None)
            target_user = self._users_by_name.get(target)
        if target_user:
            await self._send(target_user, {
                "type"   : "m.room.banned",
                "room_id": room_id,
                "content": {"reason": reason},
            })

    async def _on_room_promote(self, user: SpoakenUser, c: dict, room_id: str):
        target = _sanitise(c.get("username", ""), 32)
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or room.members.get(user.username) != "admin":
            await self._err(user, "M_FORBIDDEN", "Admins only.")
            return
        if target not in room.members:
            await self._err(user, "M_NOT_FOUND", "User not in room.")
            return
        with self._lock:
            room.members[target] = "admin"
        self._db.add_member(room_id, target, "admin")

    async def _on_room_files(self, user: SpoakenUser, c: dict, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or user.username not in room.members:
            await self._err(user, "M_FORBIDDEN", "Not in room.")
            return
        files = self._db.list_files(room_id)
        # Strip stored_name from response — clients never need server paths
        safe_files = [
            {k: v for k, v in f.items() if k != "stored_name"}
            for f in files
        ]
        await self._send(user, {
            "type"   : "m.room.files",
            "room_id": room_id,
            "content": {"files": safe_files},
        })

    # ── Message ────────────────────────────────────────────────────────────────

    async def _on_message(self, user: SpoakenUser, c: dict, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or user.username not in room.members:
            await self._err(user, "M_FORBIDDEN", "Not in room.")
            return
        body = _sanitise(c.get("body", ""), _MAX_MSG_LEN)
        if not body:
            return
        # Fire external callback if one was registered (e.g. from ChatServer shim)
        external_cb = getattr(self, "_external_message_callback", None)
        if external_cb is not None:
            try:
                external_cb(user.username, body)
            except Exception:
                pass
        ev = ChatEvent(
            event_id =_make_event_id(),
            room_id  =room_id,
            sender   =user.username,
            type     ="m.room.message",
            content  ={"body": body, "msgtype": "m.text"},
            timestamp=_now_ms(),
        )
        self._db.save_event(ev)
        await self._broadcast(room_id, ev.to_dict())

    # ── File transfer ──────────────────────────────────────────────────────────

    async def _on_file_begin(self, user: SpoakenUser, c: dict, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or user.username not in room.members:
            await self._err(user, "M_FORBIDDEN", "Not in room.")
            return
        size = int(c.get("size", 0))
        if size > _MAX_FILE_BYTES:
            await self._err(user, "M_TOO_LARGE", "File exceeds 50 MB limit.")
            return
        fname = _sanitise(
            pathlib.Path(c.get("filename", "file.txt")).name, 128
        )
        tid = secrets.token_hex(8)
        xfer = FileTransfer(
            transfer_id=tid, room_id=room_id,
            sender=user.username, filename=fname,
            declared_size=size, checksum=c.get("checksum", ""),
        )
        with self._lock:
            self._transfers[tid] = xfer
        await self._send(user, {
            "type"   : "m.file.ready",
            "content": {"transfer_id": tid},
        })

    async def _on_file_chunk(self, user: SpoakenUser, c: dict, room_id: str):
        tid  = c.get("transfer_id", "")
        data = base64.b64decode(c.get("data", ""))
        with self._lock:
            xfer = self._transfers.get(tid)
        if not xfer or xfer.sender != user.username:
            return
        xfer.chunks.append(data)
        xfer.received += len(data)

    async def _on_file_end(self, user: SpoakenUser, c: dict, room_id: str):
        tid = c.get("transfer_id", "")
        with self._lock:
            xfer = self._transfers.pop(tid, None)
        if not xfer or xfer.sender != user.username:
            return
        raw = b"".join(xfer.chunks)
        if len(raw) != xfer.declared_size:
            await self._err(user, "M_FILE_ERROR", "Size mismatch.")
            return
        actual_cs = _sha256_file(raw)
        if xfer.checksum and not hmac.compare_digest(actual_cs, xfer.checksum.lower()):
            await self._err(user, "M_FILE_ERROR", "Checksum mismatch.")
            return
        # Content-addressed storage — original filename never used as path
        stored_name = actual_cs
        dest = _TRANSFER_DIR / stored_name
        dest.write_bytes(raw)
        file_id = secrets.token_hex(8)
        self._db.save_file(
            file_id, xfer.room_id, xfer.sender,
            xfer.filename, len(raw), actual_cs, stored_name,
        )
        ev = ChatEvent(
            event_id =_make_event_id(),
            room_id  =xfer.room_id,
            sender   =xfer.sender,
            type     ="m.room.file",
            content  ={
                "file_id" : file_id,
                "filename": xfer.filename,
                "size"    : len(raw),
                "checksum": actual_cs,
            },
            timestamp=_now_ms(),
        )
        self._db.save_event(ev)
        await self._broadcast(xfer.room_id, ev.to_dict())
        self._log(
            f"[LAN]: file '{xfer.filename}' "
            f"({len(raw)//1024} KB) stored by {xfer.sender}"
        )

    async def _on_file_get(self, user: SpoakenUser, c: dict, room_id: str):
        file_id = c.get("file_id", "")
        fmeta   = self._db.get_file(file_id)
        if not fmeta:
            await self._err(user, "M_NOT_FOUND", "File not found.")
            return
        with self._lock:
            room = self._rooms.get(fmeta["room_id"])
        if not room or user.username not in room.members:
            await self._err(user, "M_FORBIDDEN", "Not in room.")
            return
        path = _TRANSFER_DIR / fmeta["stored_name"]
        if not path.exists():
            await self._err(user, "M_NOT_FOUND", "File data missing.")
            return
        raw = path.read_bytes()
        # Send in 64 KB base64 chunks
        chunk_size = _CHUNK_B64_BYTES
        total = len(raw)
        await self._send(user, {
            "type"   : "m.file.begin",
            "content": {
                "file_id"  : file_id,
                "filename" : fmeta["filename"],
                "size"     : total,
                "checksum" : fmeta["checksum"],
                "chunks"   : (total + chunk_size - 1) // chunk_size,
            },
        })
        for i in range(0, total, chunk_size):
            chunk = raw[i:i + chunk_size]
            await self._send(user, {
                "type"   : "m.file.chunk",
                "content": {"file_id": file_id,
                             "data"  : base64.b64encode(chunk).decode()},
            })
        await self._send(user, {
            "type"   : "m.file.end",
            "content": {"file_id": file_id},
        })

    async def _on_users(self, user: SpoakenUser, c: dict, room_id: str):
        with self._lock:
            room = self._rooms.get(room_id)
        if not room or user.username not in room.members:
            await self._err(user, "M_FORBIDDEN", "Not in room.")
            return
        # Return only the count + the user's own role — not the full member list
        role = room.members.get(user.username, "member")
        await self._send(user, {
            "type"   : "m.users",
            "room_id": room_id,
            "content": {
                "count"    : len(room.members),
                "your_role": role,
            },
        })

    async def _on_ping(self, user: SpoakenUser, c: dict, _: str):
        await self._send(user, {"type": "m.pong"})

    # ── Persistence helpers ────────────────────────────────────────────────────

    def _load_persisted_rooms(self):
        for room in self._db.load_rooms():
            self._rooms[room.room_id] = room


# ═══════════════════════════════════════════════════════════════════════════════
# SpoakenLANClient  (WebSocket — thread-based, callbacks to GUI)
# ═══════════════════════════════════════════════════════════════════════════════

class SpoakenLANClient:
    """
    Connects to a SpoakenLANServer via WebSocket.

    All received events are delivered via *on_event* callback (called from
    the receive thread — use .after() in the GUI).

    Parameters
    ----------
    username       : Display name.  Server stores ONLY this.
    server_token   : Shared secret matching the server's token.
    on_event       : Callable[[dict], None] — called for every inbound event.
    log_cb         : Log line callback.
    """

    def __init__(
        self,
        username    : str,
        server_token: str,
        on_event    : Callable[[dict], None],
        log_cb      : Callable[[str], None] = print,
    ):
        if not _WS_AVAILABLE:
            raise ImportError("pip install websockets")
        self._username = username
        self._token    = server_token
        self._on_event = on_event
        self._log      = log_cb

        self._ws          = None
        self._loop        = None
        self._thread      : Optional[threading.Thread] = None
        self._connected   = False
        self._send_queue  : asyncio.Queue = None  # created in loop thread

        # File receive state
        self._rx_transfers: Dict[str, dict] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def connect(self, host: str, port: int, ssl_context=None) -> bool:
        """Connect synchronously (blocks up to 10 s). Returns True on success."""
        if self._connected:
            return True
        result = threading.Event()
        ok_flag: list = [False]

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(
                    self._connect_and_run(host, port, ssl_context,
                                          result, ok_flag)
                )
            except Exception as exc:
                self._log(f"[LAN Client Error]: {exc}")
            finally:
                self._connected = False
                self._on_event({"type": "m.client.disconnected", "content": {}})
                self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True,
                                        name="lan-client")
        self._thread.start()
        result.wait(timeout=10.0)
        return ok_flag[0]

    def disconnect(self):
        self._connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def is_connected(self) -> bool:
        return self._connected

    # ── asyncio core ───────────────────────────────────────────────────────────

    async def _connect_and_run(self, host, port, ssl_ctx,
                                result_event: threading.Event,
                                ok_flag: list):
        scheme = "wss" if ssl_ctx else "ws"
        uri    = f"{scheme}://{host}:{port}"
        self._send_queue = asyncio.Queue()
        try:
            async with websockets.connect(
                uri, ssl=ssl_ctx,
                max_size=_MAX_FILE_BYTES + 131072,
                open_timeout=8,
                ping_interval=25,
                ping_timeout=10,
            ) as ws:
                self._ws = ws
                # ── Auth handshake ─────────────────────────────────────────────
                raw      = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
                chal_ev  = json.loads(raw)
                if chal_ev.get("type") != "m.auth.challenge":
                    ok_flag[0] = False
                    result_event.set()
                    return
                challenge = bytes.fromhex(chal_ev["content"]["challenge"])
                response  = _hmac_sign(self._token, challenge).hex()
                await ws.send(json.dumps({
                    "type"   : "c.auth",
                    "content": {"username": self._username, "response": response},
                }))
                raw2 = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
                auth_resp = json.loads(raw2)
                if auth_resp.get("type") != "m.auth.ok":
                    ok_flag[0] = False
                    result_event.set()
                    return

                self._connected = True
                ok_flag[0]      = True
                result_event.set()
                self._log(f"[LAN]: ✔ Connected to {host}:{port}")

                # ── concurrent send / receive ──────────────────────────────────
                await asyncio.gather(
                    self._recv_loop(ws),
                    self._send_loop(ws),
                )
        except Exception as exc:
            if not result_event.is_set():
                ok_flag[0] = False
                result_event.set()
            if self._connected:
                self._log(f"[LAN Client]: disconnected — {exc}")
        finally:
            self._connected = False

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    ev = json.loads(raw)
                    self._handle_inbound(ev)
                except Exception:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _send_loop(self, ws):
        while self._connected:
            try:
                msg = await asyncio.wait_for(
                    self._send_queue.get(), timeout=30.0
                )
                if msg is None:
                    break
                await ws.send(msg)
            except asyncio.TimeoutError:
                # Keep-alive
                try:
                    await ws.send(json.dumps({"type": "c.ping"}))
                except Exception:
                    break
            except Exception:
                break

    def _handle_inbound(self, ev: dict):
        """Handle incoming file assembly, then forward everything to GUI."""
        t = ev.get("type", "")
        c = ev.get("content", {})

        if t == "m.file.begin":
            fid = c.get("file_id", "")
            self._rx_transfers[fid] = {
                "filename": c.get("filename"),
                "size"    : c.get("size"),
                "checksum": c.get("checksum"),
                "chunks"  : [],
            }
            return  # don't forward chunk internals to GUI

        elif t == "m.file.chunk":
            fid  = c.get("file_id", "")
            data = base64.b64decode(c.get("data", ""))
            if fid in self._rx_transfers:
                self._rx_transfers[fid]["chunks"].append(data)
            return

        elif t == "m.file.end":
            fid = c.get("file_id", "")
            xfer = self._rx_transfers.pop(fid, None)
            if xfer:
                raw  = b"".join(xfer["chunks"])
                cs   = _sha256_file(raw)
                dest = LOG_DIR / "received_files" / xfer["filename"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                self._on_event({
                    "type"   : "m.file.received",
                    "content": {
                        "filename": xfer["filename"],
                        "size"    : len(raw),
                        "checksum": cs,
                        "path"    : str(dest),
                    },
                })
            return

        # Forward all other events to GUI
        try:
            self._on_event(ev)
        except Exception:
            pass

    # ── Send helpers (thread-safe, enqueue to asyncio loop) ───────────────────

    def _enqueue(self, msg: dict):
        if self._loop and self._send_queue and self._connected:
            self._loop.call_soon_threadsafe(
                self._send_queue.put_nowait, json.dumps(msg)
            )

    def send_message(self, room_id: str, text: str):
        self._enqueue({
            "type"   : "c.message",
            "room_id": room_id,
            "content": {"body": _sanitise(text, _MAX_MSG_LEN)},
        })

    def create_room(self, name: str, password: str,
                    public: bool = True, topic: str = ""):
        self._enqueue({
            "type"   : "c.room.create",
            "content": {"name": name, "password": password,
                         "public": public, "topic": topic},
        })

    def join_room(self, room_id: str, password: str = ""):
        self._enqueue({
            "type"   : "c.room.join",
            "content": {"room_id": room_id, "password": password},
        })

    def leave_room(self, room_id: str):
        self._enqueue({
            "type"   : "c.room.leave",
            "room_id": room_id,
            "content": {},
        })

    def list_rooms(self):
        self._enqueue({"type": "c.room.list", "content": {}})

    def list_files(self, room_id: str):
        self._enqueue({
            "type"   : "c.room.files",
            "room_id": room_id,
            "content": {},
        })

    def send_file(self, room_id: str, filepath: str):
        """Send a Spoaken-produced file (transcript, log, etc.) to a room."""
        path = Path(filepath)
        if not path.exists():
            self._log(f"[LAN]: File not found: {filepath}")
            return

        def _do():
            try:
                raw      = path.read_bytes()
                checksum = _sha256_file(raw)
                size     = len(raw)
                if size > _MAX_FILE_BYTES:
                    self._log(f"[LAN]: File too large (max 50 MB): {path.name}")
                    return
                # begin
                tid_placeholder = secrets.token_hex(4)
                begin_ev = {
                    "type"   : "c.file.begin",
                    "room_id": room_id,
                    "content": {
                        "filename": path.name,
                        "size"    : size,
                        "checksum": checksum,
                    },
                }
                # We need the transfer_id back from server before sending chunks.
                # Use a simple blocking approach: send begin, wait for m.file.ready.
                # This is handled via a local Event.
                _tid_event = threading.Event()
                _tid_box   = [None]

                original_on_event = self._on_event

                def _intercept(ev):
                    if ev.get("type") == "m.file.ready":
                        _tid_box[0] = ev["content"]["transfer_id"]
                        _tid_event.set()
                    else:
                        original_on_event(ev)

                # Temporarily intercept
                self._on_event = _intercept
                self._enqueue(begin_ev)
                _tid_event.wait(timeout=10.0)
                self._on_event = original_on_event

                tid = _tid_box[0]
                if not tid:
                    self._log("[LAN]: Server did not acknowledge file begin.")
                    return

                # chunks
                chunk_size = _CHUNK_B64_BYTES
                for i in range(0, size, chunk_size):
                    chunk = raw[i:i + chunk_size]
                    self._enqueue({
                        "type"   : "c.file.chunk",
                        "room_id": room_id,
                        "content": {
                            "transfer_id": tid,
                            "data"       : base64.b64encode(chunk).decode(),
                        },
                    })
                # end
                self._enqueue({
                    "type"   : "c.file.end",
                    "room_id": room_id,
                    "content": {"transfer_id": tid},
                })
                self._log(f"[LAN]: sent '{path.name}' ({size//1024} KB)")

            except Exception as exc:
                self._log(f"[LAN File Send Error]: {exc}")

        threading.Thread(target=_do, daemon=True).start()

    def download_file(self, room_id: str, file_id: str,
                      dest_path: Optional[str] = None):
        """Request a file download from the server. Result arrives as m.file.received."""
        self._enqueue({
            "type"   : "c.file.get",
            "room_id": room_id,
            "content": {"file_id": file_id},
        })
        if dest_path:
            # Override default save location for this specific file_id
            original = self._on_event

            def _reroute(ev):
                if (ev.get("type") == "m.file.received"
                        and ev["content"].get("file_id") == file_id):
                    data = ev["content"].get("_raw")
                    if data:
                        Path(dest_path).write_bytes(data)
                    original(ev)
                else:
                    original(ev)
            self._on_event = _reroute


# ── Legacy shim so spoaken_control.py can still import ChatServer ─────────────
class ChatServer:
    """
    Thin shim — wraps SpoakenLANServer for backward compatibility.

    Adds automatic reconnect with exponential backoff so the server
    restarts itself if the asyncio loop crashes mid-session.

    Parameters
    ----------
    port       : TCP port for the WebSocket server (default 55300)
    token      : Shared auth token clients must supply
    on_message : Optional callback(username, message_text) called when
                 a chat message arrives — used by spoaken_control to pipe
                 messages into the command parser / GUI console.
    log_cb     : Log output callback (default print)
    """
    _MAX_BACKOFF = 60   # seconds

    def __init__(self, port=55300, token="spoaken", on_message=None, log_cb=print, **kw):
        self._port                   = port
        self._token                  = token
        self._log                    = log_cb
        self._on_message_callback    = on_message
        self._enabled                = False
        self._reconnect_thread: threading.Thread | None = None
        self._inner: SpoakenLANServer | None = None
        self._build_inner()

    def _build_inner(self):
        self._inner = SpoakenLANServer(
            port=self._port, token=self._token, log_cb=self._log,
        )
        if self._on_message_callback is not None:
            self._inner._external_message_callback = self._on_message_callback

    def start(self):
        self._enabled = True
        self._inner.start()
        # Watchdog — restarts the server if the loop dies unexpectedly
        self._reconnect_thread = threading.Thread(
            target=self._watchdog, daemon=True, name="chat-server-watchdog",
        )
        self._reconnect_thread.start()

    def _watchdog(self):
        backoff = 2
        while self._enabled:
            time.sleep(backoff)
            if not self._enabled:
                break
            if not self._inner.is_open():
                self._log(f"[ChatServer]: connection lost — retrying in {backoff}s …")
                self._build_inner()
                ok = self._inner.start()
                if ok:
                    self._log("[ChatServer]: reconnected ✔")
                    backoff = 2      # reset on success
                else:
                    backoff = min(backoff * 2, self._MAX_BACKOFF)

    def stop(self):
        self._enabled = False
        if self._inner:
            self._inner.stop()

    def is_open(self): return bool(self._inner and self._inner.is_open())

    def send(self, msg: str):
        """
        Legacy API used by spoaken_control.chat_send().
        Broadcasts a plain-text message to all connected peers in every room.
        """
        if not self._inner or not self._inner.is_open():
            return
        inner = self._inner
        # Use the internal broadcast helper if available
        if hasattr(inner, "_broadcast_text"):
            inner._broadcast_text(msg)
            return
        # Fallback: push via the asyncio loop
        loop = getattr(inner, "_loop", None)
        if loop and loop.is_running():
            async def _push():
                ev = json.dumps({
                    "type"    : "m.room.message",
                    "room_id" : "*",
                    "content" : {"body": msg, "sender": "[server]"},
                    "event_id": _make_event_id(),
                    "ts"      : _now_ms(),
                })
                users = getattr(inner, "_users", {})
                for user in list(users.values()):
                    try:
                        await user.ws.send(ev)
                    except Exception:
                        pass
            import asyncio as _aio
            _aio.run_coroutine_threadsafe(_push(), loop)

    def broadcast(self, msg: str):
        """Alias for send() — keeps legacy callers working."""
        self.send(msg)

class SSEServer:
    """Legacy SSE server shim — HTTP push for Android/browser."""
    def __init__(self, port=55301, log_cb=print, **kw):
        self._port    = port
        self._log     = log_cb
        self._running = False
        self._clients = []
        self._lock    = threading.Lock()

    def start(self):
        if self._running:
            return
        threading.Thread(target=self._serve, daemon=True,
                         name="sse-server").start()

    def stop(self):
        self._running = False

    def push(self, text: str):
        from queue import Full, Queue
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(text)
                except Full:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)

    def is_open(self) -> bool:
        return self._running

    def _serve(self):
        import http.server
        sse = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                if self.path == "/stream":
                    self._stream()
                elif self.path == "/":
                    self._page()
                else:
                    self.send_error(404)

            def _page(self):
                html = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<title>Spoaken Live</title>"
                    "<style>body{background:#060c1a;color:#00bdff;"
                    "font-family:monospace;padding:16px;}</style></head>"
                    "<body><h2>◈ SPOAKEN — Live Transcript</h2>"
                    "<div id='log'></div><script>"
                    "const el=document.getElementById('log');"
                    "const es=new EventSource('/stream');"
                    "es.onmessage=e=>{el.textContent+=e.data+'\\n';"
                    "window.scrollTo(0,document.body.scrollHeight);};"
                    "</script></body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type","text/html;charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers(); self.wfile.write(html)

            def _stream(self):
                from queue import Queue, Empty
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.send_header("Cache-Control","no-cache")
                self.end_headers()
                q = Queue(maxsize=50)
                with sse._lock: sse._clients.append(q)
                try:
                    while sse._running:
                        try:
                            msg = q.get(timeout=20)
                            self.wfile.write(f"data: {msg}\n\n".encode())
                            self.wfile.flush()
                        except Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except Exception:
                    pass
                finally:
                    with sse._lock:
                        if q in sse._clients:
                            sse._clients.remove(q)

        try:
            srv = http.server.HTTPServer(("", self._port), _H)
            self._running = True
            self._log(f"[Android Stream]: http://localhost:{self._port}")
            srv.serve_forever()
        except Exception as exc:
            self._log(f"[Android Stream Error]: {exc}")
        finally:
            self._running = False
