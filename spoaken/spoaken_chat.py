"""







MODIFY AT YOUR OWN RISK









spoaken_chat.py
───────────────────────────────────────────────────────────────────────────────
Spoaken LAN Messaging System  —  v3.0
Matrix.org–inspired, fully offline, zero-internet group chat for classrooms,
labs, and local networks.

Architecture
────────────
  SpoakenLANServer  TCP server: rooms, users, history, file storage, search.
                    Any peer on the LAN can host one.
  SpoakenLANClient  Connects to a server, sends events, receives broadcasts.
  ServerBeacon      UDP broadcaster — lets servers announce themselves (~10 s).
  ServerScanner     UDP listener — builds a live list of servers on the LAN.
  ChatDB            SQLite persistence layer.

Event protocol  (Matrix-inspired, JSON over length-prefixed TCP)
────────────────────────────────────────────────────────────────
  Every action is a JSON "event" with a stable event_id.
  Client→Server commands use the  c.*  prefix.
  Server→Client events use the    m.*  prefix.
  History is replayed on join (up to 250 events).

Room model
──────────
  • Room IDs:    !<8hex>:spoaken
  • User IDs:    @<username>:<host_ip>
  • Event IDs:   $<ts_ms>_<6hex>:spoaken
  • Rooms have name, topic, SHA-256 password, public/private flag.
  • Roles: "admin" (creator, can kick/ban/promote) | "member"
  • Rooms persist across server restarts via SQLite.

File transfer
─────────────
  Chunked push (client → server) with SHA-256 integrity check.
  Stored in  Logs/room_transfers/<room_id>/<filename>
  Any room member can download stored files.
  Limit: 50 MB per file.

Cross-reference search (online-only, like Matrix)
─────────────────────────────────────────────────
  Searches message history AND transferred text-log files.
  Scope: "room" | "all" (all your rooms) | "messages" | "files"
  Returns paginated results with context snippets, timestamps, senders.

Legacy compatibility
────────────────────
  ChatServer and SSEServer from v2 are preserved at the bottom of this file
  so spoaken_control.py continues to work unchanged.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import math
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
import uuid
from collections import defaultdict, deque
from pathlib import Path
from queue import Full, Queue
from typing import Callable, Dict, List, Optional, Tuple

# ── Paths ─────────────────────────────────────────────────────────────────────
try:
    from paths import LOG_DIR
except ImportError:
    LOG_DIR = Path(__file__).parent.parent / "Logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)

_log = logging.getLogger("spoaken.chat")

# ── Protocol / security constants ─────────────────────────────────────────────
_PROTO_VERSION      = "1.0"
_REALM              = "spoaken"
_DISCOVERY_PORT     = 55302
_DISCOVERY_TTL      = 12.0
_DISCOVERY_INTERVAL = 8.0
_MAX_CONN_PER_IP    = 8
_MAX_MSG_LEN        = 4096
_RATE_LIMIT         = 20
_AUTH_TIMEOUT_S     = 15.0
_RECV_TIMEOUT_S     = 120.0
_CHUNK_BYTES        = 65536        # 64 KB per file chunk
_MAX_HISTORY        = 250
_MAX_FILE_BYTES     = 50 * 1024 * 1024   # 50 MB
_MAX_SEARCH_HITS    = 100
_AUDIT_LOG          = LOG_DIR / "chat_audit.log"
_TRANSFER_DIR       = LOG_DIR / "room_transfers"
_TRANSFER_DIR.mkdir(parents=True, exist_ok=True)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ═══════════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════════

@dataclasses.dataclass
class SpoakenRoom:
    room_id      : str
    name         : str
    creator      : str
    password_hash: str
    password_salt: str
    public       : bool
    created_at   : int
    topic        : str = ""
    members      : Dict[str, str] = dataclasses.field(default_factory=dict)
    # {username: "admin" | "member"}

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
    username : str
    user_id  : str
    ip       : str
    conn     : socket.socket
    rooms    : List[str] = dataclasses.field(default_factory=list)
    send_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    msg_window: deque = dataclasses.field(
        default_factory=lambda: deque(maxlen=_RATE_LIMIT + 1)
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
# Security / protocol helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_room_id()          -> str: return f"!{secrets.token_hex(8)}:{_REALM}"
def _make_event_id(rid: str) -> str: return f"${int(time.time()*1000)}_{secrets.token_hex(3)}:{_REALM}"
def _make_user_id(u, h)      -> str: return f"@{u}:{h}"
def _now_ms()                -> int: return int(time.time() * 1000)

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode()).hexdigest()

def _sanitise(raw: str, maxlen: int = _MAX_MSG_LEN) -> str:
    return _CTRL_RE.sub("", raw).strip()[:maxlen]

def _audit(msg: str):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def _hmac_sign(secret: str, challenge: bytes) -> bytes:
    return hmac.new(secret.encode(), challenge, hashlib.sha256).digest()


# ── Wire encoding  (4-byte big-endian length prefix + UTF-8 JSON + newline) ──

def _encode(ev: dict) -> bytes:
    payload = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
    return struct.pack(">I", len(payload)) + payload

def _send_event(conn: socket.socket, lock: threading.Lock, ev: dict) -> bool:
    try:
        data = _encode(ev)
        with lock:
            conn.sendall(data)
        return True
    except OSError:
        return False


class _FrameReader:
    """Reassembles length-prefixed JSON frames from a TCP stream."""
    def __init__(self, conn: socket.socket):
        self._conn = conn
        self._buf  = b""

    def read_event(self, timeout: float = _RECV_TIMEOUT_S) -> Optional[dict]:
        self._conn.settimeout(timeout)
        try:
            while len(self._buf) < 4:
                chunk = self._conn.recv(4096)
                if not chunk:
                    return None
                self._buf += chunk

            length = struct.unpack(">I", self._buf[:4])[0]
            self._buf = self._buf[4:]
            if length > _MAX_FILE_BYTES + 65536:   # sanity cap
                return None

            while len(self._buf) < length:
                need  = min(65536, length - len(self._buf))
                chunk = self._conn.recv(need)
                if not chunk:
                    return None
                self._buf += chunk

            payload   = self._buf[:length]
            self._buf = self._buf[length:]
            return json.loads(payload.decode("utf-8"))
        except (socket.timeout, json.JSONDecodeError, struct.error, OSError):
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# SQLite persistence layer
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
        stored_path TEXT NOT NULL, uploaded_at INTEGER NOT NULL
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

    # Rooms
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
        return [SpoakenRoom(
            room_id=r["room_id"], name=r["name"], creator=r["creator"],
            password_hash=r["password_hash"], password_salt=r["password_salt"],
            public=bool(r["public"]), created_at=r["created_at"],
            topic=r["topic"] or ""
        ) for r in rows]

    def delete_room(self, room_id: str):
        with self._lock:
            for tbl in ("members", "events", "files", "banned", "rooms"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE room_id=?", (room_id,))
            self._conn.commit()

    # Members
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

    # Events
    def save_event(self, ev: ChatEvent):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                (ev.event_id, ev.room_id, ev.sender, ev.type,
                 json.dumps(ev.content), ev.timestamp)
            )
            self._conn.commit()

    def get_history(self, room_id: str, limit: int = _MAX_HISTORY,
                    before_ts: int = 0) -> List[ChatEvent]:
        q = ("SELECT * FROM events WHERE room_id=? "
             + ("AND timestamp<? " if before_ts else "")
             + "ORDER BY timestamp DESC LIMIT ?")
        args = (room_id, before_ts, limit) if before_ts else (room_id, limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [ChatEvent(
            event_id=r["event_id"], room_id=r["room_id"], sender=r["sender"],
            type=r["type"], content=json.loads(r["content"]),
            timestamp=r["timestamp"]
        ) for r in reversed(rows)]

    def search_events(self, query: str, room_ids: List[str],
                      limit: int = _MAX_SEARCH_HITS) -> List[dict]:
        if not room_ids:
            return []
        like = f"%{query.replace('%','').replace('_','')}%"
        ph   = ",".join("?" * len(room_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE room_id IN ({ph}) "
                f"AND (content LIKE ? OR sender LIKE ?) "
                f"ORDER BY timestamp DESC LIMIT ?",
                (*room_ids, like, like, limit)
            ).fetchall()
        results = []
        for r in rows:
            try:
                body = json.loads(r["content"]).get("body", "")[:200]
            except Exception:
                body = ""
            results.append({
                "event_id" : r["event_id"], "room_id": r["room_id"],
                "sender"   : r["sender"],   "type"   : r["type"],
                "timestamp": r["timestamp"], "snippet": body,
            })
        return results

    # Files
    def save_file(self, file_id: str, room_id: str, sender: str,
                  filename: str, size: int, checksum: str, path: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?,?)",
                (file_id, room_id, sender, filename, size,
                 checksum, path, _now_ms())
            )
            self._conn.commit()

    def list_files(self, room_id: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE room_id=? ORDER BY uploaded_at DESC",
                (room_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_file(self, file_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE file_id=?", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def search_files(self, query: str, room_ids: List[str]) -> List[dict]:
        """Full text search inside stored text files."""
        results, q_low = [], query.lower()
        for room_id in room_ids:
            for fr in self.list_files(room_id):
                p = Path(fr["stored_path"])
                if not p.exists():
                    continue
                try:
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                except Exception:
                    continue
                for n, line in enumerate(lines, 1):
                    if q_low in line.lower():
                        results.append({
                            "file_id"    : fr["file_id"],
                            "room_id"    : room_id,
                            "filename"   : fr["filename"],
                            "sender"     : fr["sender"],
                            "lineno"     : n,
                            "snippet"    : line.strip()[:200],
                            "uploaded_at": fr["uploaded_at"],
                        })
                        if len(results) >= _MAX_SEARCH_HITS:
                            return results
        return results

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# LAN Discovery
# ═══════════════════════════════════════════════════════════════════════════════

class ServerBeacon:
    """UDP broadcaster — announces this server to the LAN."""

    def __init__(self, tcp_port: int, server_name: str,
                 room_count_fn: Callable[[], int]):
        self._port       = tcp_port
        self._name       = server_name
        self._room_count = room_count_fn
        self._running    = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True,
                         name="spoaken-beacon").start()

    def stop(self):
        self._running = False

    def _loop(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            while self._running:
                payload = "|".join([
                    "SPOAKEN", _PROTO_VERSION, self._name,
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
class ServerEntry:
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


class ServerScanner:
    """UDP listener — discovers SpoakenLANServer instances on the LAN."""

    def __init__(self):
        self._servers : Dict[str, ServerEntry] = {}
        self._lock    = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True,
                         name="spoaken-scanner").start()

    def stop(self):
        self._running = False

    def get_servers(self) -> List[ServerEntry]:
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
                    if len(parts) < 5 or parts[0] != "SPOAKEN":
                        continue
                    _, _, name, port_s, rooms_s = parts[:5]
                    key = f"{addr[0]}:{port_s}"
                    with self._lock:
                        self._servers[key] = ServerEntry(
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


# ═══════════════════════════════════════════════════════════════════════════════
# SpoakenLANServer
# ═══════════════════════════════════════════════════════════════════════════════

class SpoakenLANServer:
    """
    Full LAN group-chat server.

        server = SpoakenLANServer(
            port=55300, token="mytoken",
            server_name="Physics Lab",
            db_path=Path("Logs/chat.db"),
        )
        server.start()
        ...
        server.stop()
    """

    def __init__(
        self,
        port        : int  = 55300,
        token       : str  = "spoaken",
        server_name : str  = "Spoaken Server",
        db_path     : Optional[Path] = None,
        log_cb      : Callable[[str], None] = print,
        bind_address: str  = "0.0.0.0",
    ):
        self._port  = port
        self._token = token
        self._name  = server_name
        self._log   = log_cb
        self._bind  = bind_address

        _db = db_path or (LOG_DIR / "spoaken_lan.db")
        self._db = ChatDB(_db)

        self._rooms          : Dict[str, SpoakenRoom] = {}
        self._users          : Dict[socket.socket, SpoakenUser] = {}
        self._users_by_name  : Dict[str, SpoakenUser] = {}
        self._rooms_lock     = threading.Lock()
        self._users_lock     = threading.Lock()
        self._transfers      : Dict[str, FileTransfer] = {}
        self._xfer_lock      = threading.Lock()

        self._conn_counts    : Dict[str, int] = defaultdict(int)
        self._banned_ips     : set            = set()
        self._auth_strikes   : Dict[str, int] = defaultdict(int)
        self._sec_lock       = threading.Lock()

        self._srv_sock       : Optional[socket.socket] = None
        self._running        = False
        self._beacon         : Optional[ServerBeacon]  = None

        self._load_persisted_rooms()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._running:
            return False
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self._bind, self._port))
            srv.listen(64)
            srv.settimeout(1.0)
            self._srv_sock = srv
            self._running  = True
            threading.Thread(target=self._accept_loop, daemon=True,
                             name="spoaken-accept").start()
            self._beacon = ServerBeacon(
                self._port, self._name, lambda: len(self._rooms)
            )
            self._beacon.start()
            self._log(
                f"[LAN Server]: '{self._name}' online  "
                f"{self._bind}:{self._port}"
            )
            _audit(f"SERVER STARTED {self._bind}:{self._port} name={self._name}")
            return True
        except OSError as exc:
            self._log(f"[LAN Server Error]: cannot bind {self._port} — {exc}")
            return False

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._beacon:
            self._beacon.stop()
        with self._users_lock:
            for u in list(self._users.values()):
                _send_event(u.conn, u.send_lock, {
                    "type": "m.server.shutdown",
                    "content": {"message": "Server shutting down."},
                })
                try:
                    u.conn.close()
                except Exception:
                    pass
            self._users.clear()
            self._users_by_name.clear()
        if self._srv_sock:
            try:
                self._srv_sock.close()
            except Exception:
                pass
        self._db.close()
        self._log(f"[LAN Server]: '{self._name}' offline")
        _audit(f"SERVER STOPPED port={self._port}")

    def is_open(self)     -> bool: return self._running
    def peer_count(self)  -> int:
        with self._users_lock:
            return len(self._users)

    # ── Accept loop ────────────────────────────────────────────────────────────

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._srv_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            with self._sec_lock:
                if ip in self._banned_ips:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                if self._conn_counts[ip] >= _MAX_CONN_PER_IP:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                self._conn_counts[ip] += 1
            threading.Thread(target=self._handle_client, args=(conn, ip),
                             daemon=True, name=f"client-{ip}").start()

    # ── Client lifecycle ───────────────────────────────────────────────────────

    def _handle_client(self, conn: socket.socket, ip: str):
        user:   Optional[SpoakenUser] = None
        reader = _FrameReader(conn)
        try:
            # ── HMAC handshake ─────────────────────────────────────────────────
            challenge = os.urandom(32)
            _send_event(conn, threading.Lock(), {
                "type": "m.auth.challenge",
                "content": {
                    "challenge": challenge.hex(),
                    "version"  : _PROTO_VERSION,
                    "server"   : self._name,
                },
            })
            ev = reader.read_event(timeout=_AUTH_TIMEOUT_S)
            if not ev or ev.get("type") != "c.auth":
                conn.close()
                return

            username = _sanitise(ev.get("content", {}).get("username", ""), 32)
            response = ev.get("content", {}).get("response", "")
            expected = _hmac_sign(self._token, challenge).hex()

            if not username or not hmac.compare_digest(response, expected):
                with self._sec_lock:
                    self._auth_strikes[ip] += 1
                    if self._auth_strikes[ip] >= 3:
                        self._banned_ips.add(ip)
                        _audit(f"BANNED (auth) {ip}")
                _send_event(conn, threading.Lock(), {
                    "type": "m.error",
                    "content": {"code": "M_UNAUTHORIZED",
                                "error": "Authentication failed."},
                })
                conn.close()
                return

            with self._users_lock:
                if username in self._users_by_name:
                    _send_event(conn, threading.Lock(), {
                        "type": "m.error",
                        "content": {"code": "M_USER_IN_USE",
                                    "error": f"'{username}' is already connected."},
                    })
                    conn.close()
                    return

            user = SpoakenUser(
                username=username,
                user_id=_make_user_id(username, ip),
                ip=ip, conn=conn,
            )
            with self._users_lock:
                self._users[conn]            = user
                self._users_by_name[username] = user

            with self._sec_lock:
                self._auth_strikes[ip] = 0

            _send_event(conn, user.send_lock, {
                "type": "m.auth.ok",
                "content": {
                    "user_id"    : user.user_id,
                    "server_name": self._name,
                    "version"    : _PROTO_VERSION,
                },
            })
            _audit(f"CONNECTED {ip} user={username}")
            self._log(f"[LAN]: ✔  {username}@{ip} connected")

            # ── Dispatch loop ──────────────────────────────────────────────────
            while self._running:
                ev = reader.read_event()
                if ev is None:
                    break
                now = time.monotonic()
                user.msg_window.append(now)
                if (len(user.msg_window) > _RATE_LIMIT
                        and (now - user.msg_window[0]) < 1.0):
                    _send_event(conn, user.send_lock, {
                        "type": "m.error",
                        "content": {"code": "M_RATE_LIMITED",
                                    "error": "Slow down."},
                    })
                    continue
                self._dispatch(user, ev)

        except Exception as exc:
            _audit(f"CLIENT_ERROR {ip} {exc}")
        finally:
            if user:
                self._disconnect_user(user)
            else:
                try:
                    conn.close()
                except Exception:
                    pass
            with self._sec_lock:
                self._conn_counts[ip] = max(0, self._conn_counts[ip] - 1)

    # ── Central dispatcher ─────────────────────────────────────────────────────

    def _dispatch(self, user: SpoakenUser, ev: dict):
        t       = ev.get("type", "")
        content = ev.get("content", {})
        room_id = ev.get("room_id", "")
        fn = {
            "c.room.create" : self._on_room_create,
            "c.room.join"   : self._on_room_join,
            "c.room.leave"  : self._on_room_leave,
            "c.room.list"   : self._on_room_list,
            "c.room.history": self._on_room_history,
            "c.room.topic"  : self._on_room_topic,
            "c.room.kick"   : self._on_room_kick,
            "c.room.ban"    : self._on_room_ban,
            "c.room.promote": self._on_room_promote,
            "c.room.files"  : self._on_room_files,
            "c.message"     : self._on_message,
            "c.file.begin"  : self._on_file_begin,
            "c.file.chunk"  : self._on_file_chunk,
            "c.file.end"    : self._on_file_end,
            "c.file.get"    : self._on_file_get,
            "c.search"      : self._on_search,
            "c.users"       : self._on_users,
            "c.ping"        : lambda u, c, r: _send_event(
                u.conn, u.send_lock, {"type": "m.pong"}
            ),
        }.get(t)
        if fn:
            try:
                fn(user, content, room_id)
            except Exception as exc:
                _log.exception("dispatch error %s: %s", t, exc)
                self._err(user, "M_INTERNAL", str(exc))

    # ── Room handlers ──────────────────────────────────────────────────────────

    def _on_room_create(self, user: SpoakenUser, c: dict, _: str):
        name     = _sanitise(c.get("name", ""), 80)
        password = c.get("password", "")
        public   = bool(c.get("public", True))
        topic    = _sanitise(c.get("topic", ""), 200)

        if not name:
            return self._err(user, "M_INVALID", "Room name required.")
        if not password:
            return self._err(user, "M_INVALID", "Password required.")

        room_id  = _make_room_id()
        salt     = secrets.token_hex(16)
        room = SpoakenRoom(
            room_id=room_id, name=name, creator=user.username,
            password_hash=_hash_pw(password, salt), password_salt=salt,
            public=public, created_at=_now_ms(), topic=topic,
            members={user.username: "admin"},
        )
        with self._rooms_lock:
            self._rooms[room_id] = room
        self._db.save_room(room)
        self._db.add_member(room_id, user.username, "admin")
        with self._users_lock:
            user.rooms.append(room_id)

        self._persist_event(room_id, user.user_id, "m.room.create",
                            {"name": name, "topic": topic, "public": public})

        _send_event(user.conn, user.send_lock, {
            "type": "m.room.created",
            "content": {
                "room_id"  : room_id, "name": name, "topic": topic,
                "public"   : public,  "role": "admin",
                "share_id" : room_id,
            },
        })
        self._log(f"[LAN]: room '{name}' {room_id} created by {user.username}")
        _audit(f"ROOM_CREATE {room_id} name={name} by={user.username}")

    def _on_room_join(self, user: SpoakenUser, c: dict, _: str):
        room_id  = c.get("room_id", "")
        password = c.get("password", "")

        with self._rooms_lock:
            room = self._rooms.get(room_id)
        if not room:
            return self._err(user, "M_NOT_FOUND", "Room not found.")
        if self._db.is_banned(room_id, user.username):
            return self._err(user, "M_FORBIDDEN", "You are banned from this room.")
        if room_id in user.rooms:
            return self._err(user, "M_ALREADY_JOINED", "Already in room.")

        expected = _hash_pw(password, room.password_salt)
        if not hmac.compare_digest(expected, room.password_hash):
            _audit(f"JOIN_FAIL (pw) {room_id} user={user.username}")
            return self._err(user, "M_FORBIDDEN", "Incorrect room password.")

        role = "member"
        with self._rooms_lock:
            room.members[user.username] = role
        self._db.add_member(room_id, user.username, role)
        with self._users_lock:
            user.rooms.append(room_id)

        history = [e.to_dict() for e in self._db.get_history(room_id)]
        join_ev = self._persist_event(
            room_id, user.user_id, "m.room.member",
            {"membership": "join", "username": user.username, "role": role}
        )
        self._broadcast(room_id, join_ev, exclude=user.username)

        _send_event(user.conn, user.send_lock, {
            "type": "m.room.joined",
            "content": {
                "room_id"   : room_id, "name": room.name, "topic": room.topic,
                "role"      : role, "members": dict(room.members),
                "history"   : history,
                "file_count": len(self._db.list_files(room_id)),
            },
        })
        self._log(f"[LAN]: {user.username} joined '{room.name}'")
        _audit(f"ROOM_JOIN {room_id} user={user.username}")

    def _on_room_leave(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        with self._rooms_lock:
            room = self._rooms.get(room_id)
        if not room or room_id not in user.rooms:
            return
        with self._rooms_lock:
            room.members.pop(user.username, None)
        self._db.remove_member(room_id, user.username)
        with self._users_lock:
            if room_id in user.rooms:
                user.rooms.remove(room_id)
        leave_ev = self._persist_event(
            room_id, user.user_id, "m.room.member",
            {"membership": "leave", "username": user.username}
        )
        self._broadcast(room_id, leave_ev)
        _send_event(user.conn, user.send_lock,
                    {"type": "m.room.left", "content": {"room_id": room_id}})

    def _on_room_list(self, user: SpoakenUser, c: dict, _: str):
        with self._rooms_lock:
            snap = list(self._rooms.values())
        rooms = []
        for r in snap:
            if r.public:
                d = r.display()
                d["joined"] = r.room_id in user.rooms
                rooms.append(d)
            elif r.room_id in user.rooms:
                d = r.display()
                d["joined"] = True
                rooms.append(d)
        _send_event(user.conn, user.send_lock,
                    {"type": "m.room.list",
                     "content": {"rooms": rooms, "count": len(rooms)}})

    def _on_room_history(self, user: SpoakenUser, c: dict, room_id: str):
        room_id   = room_id or c.get("room_id", "")
        before_ts = int(c.get("before_ts", 0))
        limit     = min(int(c.get("limit", 50)), _MAX_HISTORY)
        if room_id not in user.rooms:
            return self._err(user, "M_FORBIDDEN", "Not in room.")
        history = [e.to_dict() for e in
                   self._db.get_history(room_id, limit, before_ts)]
        _send_event(user.conn, user.send_lock, {
            "type": "m.room.history", "room_id": room_id,
            "content": {"events": history, "count": len(history)},
        })

    def _on_room_topic(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        topic   = _sanitise(c.get("topic", ""), 200)
        with self._rooms_lock:
            room = self._rooms.get(room_id)
        if not room:
            return self._err(user, "M_NOT_FOUND", "Room not found.")
        if room.members.get(user.username) != "admin":
            return self._err(user, "M_FORBIDDEN", "Admin only.")
        room.topic = topic
        self._db.save_room(room)
        ev = self._persist_event(room_id, user.user_id,
                                 "m.room.topic", {"topic": topic})
        self._broadcast(room_id, ev)

    def _on_room_kick(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        target  = c.get("username", "")
        with self._rooms_lock:
            room = self._rooms.get(room_id)
        if not room:
            return
        if room.members.get(user.username) != "admin":
            return self._err(user, "M_FORBIDDEN", "Admin only.")
        if target == room.creator:
            return self._err(user, "M_FORBIDDEN", "Cannot kick room creator.")
        with self._rooms_lock:
            room.members.pop(target, None)
        self._db.remove_member(room_id, target)
        ev = self._persist_event(
            room_id, user.user_id, "m.room.member",
            {"membership": "kick", "username": target, "kicked_by": user.username}
        )
        self._broadcast(room_id, ev)
        with self._users_lock:
            kicked = self._users_by_name.get(target)
        if kicked:
            if room_id in kicked.rooms:
                kicked.rooms.remove(room_id)
            _send_event(kicked.conn, kicked.send_lock, {
                "type": "m.room.kicked", "room_id": room_id,
                "content": {"kicked_by": user.username},
            })

    def _on_room_ban(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        target  = c.get("username", "")
        reason  = _sanitise(c.get("reason", ""), 200)
        with self._rooms_lock:
            room = self._rooms.get(room_id)
        if not room:
            return
        if room.members.get(user.username) != "admin":
            return self._err(user, "M_FORBIDDEN", "Admin only.")
        if target == room.creator:
            return self._err(user, "M_FORBIDDEN", "Cannot ban creator.")
        self._db.ban_member(room_id, target, user.username, reason)
        with self._rooms_lock:
            room.members.pop(target, None)
        ev = self._persist_event(
            room_id, user.user_id, "m.room.member",
            {"membership": "ban", "username": target, "reason": reason}
        )
        self._broadcast(room_id, ev)
        with self._users_lock:
            banned = self._users_by_name.get(target)
        if banned:
            if room_id in banned.rooms:
                banned.rooms.remove(room_id)
            _send_event(banned.conn, banned.send_lock, {
                "type": "m.room.banned", "room_id": room_id,
                "content": {"reason": reason},
            })
        _audit(f"BAN {room_id} {target} by={user.username}")

    def _on_room_promote(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        target  = c.get("username", "")
        role    = c.get("role", "member")
        if role not in ("admin", "member"):
            return self._err(user, "M_INVALID", "Role must be admin or member.")
        with self._rooms_lock:
            room = self._rooms.get(room_id)
        if not room:
            return
        if room.members.get(user.username) != "admin":
            return self._err(user, "M_FORBIDDEN", "Admin only.")
        if target not in room.members:
            return self._err(user, "M_NOT_FOUND", "User not in room.")
        with self._rooms_lock:
            room.members[target] = role
        self._db.add_member(room_id, target, role)
        ev = self._persist_event(
            room_id, user.user_id, "m.room.member",
            {"membership": "promote", "username": target, "role": role}
        )
        self._broadcast(room_id, ev)

    def _on_room_files(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        if room_id not in user.rooms:
            return self._err(user, "M_FORBIDDEN", "Not in room.")
        files = self._db.list_files(room_id)
        _send_event(user.conn, user.send_lock, {
            "type": "m.room.files", "room_id": room_id,
            "content": {"files": files, "count": len(files)},
        })

    # ── Message handler ────────────────────────────────────────────────────────

    def _on_message(self, user: SpoakenUser, c: dict, room_id: str):
        if room_id not in user.rooms:
            return self._err(user, "M_FORBIDDEN", "Not in room.")
        body    = _sanitise(c.get("body", ""))
        msgtype = c.get("msgtype", "m.text")
        if not body:
            return
        if msgtype not in ("m.text", "m.notice", "m.emote"):
            msgtype = "m.text"
        ev = self._persist_event(
            room_id, user.user_id, "m.room.message",
            {"msgtype": msgtype, "body": body}
        )
        self._broadcast(room_id, ev)

    # ── File transfer ──────────────────────────────────────────────────────────

    def _on_file_begin(self, user: SpoakenUser, c: dict, room_id: str):
        if room_id not in user.rooms:
            return self._err(user, "M_FORBIDDEN", "Not in room.")
        filename = pathlib.Path(_sanitise(c.get("filename", "unknown"), 255)).name
        size     = int(c.get("size", 0))
        checksum = c.get("checksum", "")
        if size > _MAX_FILE_BYTES:
            return self._err(user, "M_TOO_LARGE",
                             f"File exceeds {_MAX_FILE_BYTES//1024//1024} MB.")
        if not filename or not checksum:
            return self._err(user, "M_INVALID", "filename and checksum required.")

        xfer_id = secrets.token_hex(8)
        with self._xfer_lock:
            self._transfers[xfer_id] = FileTransfer(
                transfer_id=xfer_id, room_id=room_id, sender=user.username,
                filename=filename, declared_size=size, checksum=checksum,
            )
        _send_event(user.conn, user.send_lock, {
            "type": "m.file.begin_ack",
            "content": {"transfer_id": xfer_id, "chunk_size": _CHUNK_BYTES},
        })
        self._broadcast_raw(room_id, {
            "type": "m.file.offer", "room_id": room_id, "sender": user.user_id,
            "content": {"transfer_id": xfer_id, "filename": filename, "size": size},
        })

    def _on_file_chunk(self, user: SpoakenUser, c: dict, _: str):
        xfer_id = c.get("transfer_id", "")
        with self._xfer_lock:
            xfer = self._transfers.get(xfer_id)
        if not xfer or xfer.sender != user.username:
            return self._err(user, "M_NOT_FOUND", "Unknown transfer.")
        try:
            chunk = base64.b64decode(c.get("data", ""))
        except Exception:
            return self._err(user, "M_INVALID", "Bad base64.")
        xfer.chunks.append(chunk)
        xfer.received += len(chunk)
        if len(xfer.chunks) % 5 == 0:
            _send_event(user.conn, user.send_lock, {
                "type": "m.file.progress",
                "content": {"transfer_id": xfer_id,
                            "received": xfer.received,
                            "total": xfer.declared_size},
            })

    def _on_file_end(self, user: SpoakenUser, c: dict, _: str):
        xfer_id = c.get("transfer_id", "")
        with self._xfer_lock:
            xfer = self._transfers.pop(xfer_id, None)
        if not xfer or xfer.sender != user.username:
            return self._err(user, "M_NOT_FOUND", "Unknown transfer.")

        data        = b"".join(xfer.chunks)
        actual_hash = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(actual_hash, xfer.checksum):
            return self._err(user, "M_CHECKSUM", "Checksum mismatch.")

        # Safe store
        room_dir = (_TRANSFER_DIR
                    / xfer.room_id.replace("!", "").replace(":", "_"))
        room_dir.mkdir(parents=True, exist_ok=True)
        dest = room_dir / xfer.filename
        if dest.exists():
            dest = room_dir / f"{dest.stem}_{secrets.token_hex(3)}{dest.suffix}"
        dest.write_bytes(data)

        file_id = secrets.token_hex(8)
        self._db.save_file(file_id, xfer.room_id, xfer.sender,
                           xfer.filename, len(data), actual_hash, str(dest))

        ev = self._persist_event(
            xfer.room_id, user.user_id, "m.file.complete",
            {"file_id": file_id, "filename": xfer.filename,
             "size": len(data), "checksum": actual_hash},
        )
        self._broadcast(xfer.room_id, ev)
        self._log(
            f"[LAN]: file stored '{xfer.filename}'  "
            f"{len(data)//1024} KB  room={xfer.room_id}"
        )
        _audit(f"FILE_STORED {xfer.room_id} {xfer.filename} {len(data)} bytes")

    def _on_file_get(self, user: SpoakenUser, c: dict, room_id: str):
        file_id = c.get("file_id", "")
        fr      = self._db.get_file(file_id)
        if not fr:
            return self._err(user, "M_NOT_FOUND", "File not found.")
        if fr["room_id"] not in user.rooms:
            return self._err(user, "M_FORBIDDEN", "Not in room.")
        p = Path(fr["stored_path"])
        if not p.exists():
            return self._err(user, "M_NOT_FOUND", "File missing on server.")

        data  = p.read_bytes()
        total = math.ceil(len(data) / _CHUNK_BYTES)
        _send_event(user.conn, user.send_lock, {
            "type": "m.file.download_begin",
            "content": {"file_id": file_id, "filename": fr["filename"],
                        "size": len(data), "chunks": total,
                        "checksum": fr["checksum"]},
        })
        for i in range(total):
            chunk = data[i * _CHUNK_BYTES: (i + 1) * _CHUNK_BYTES]
            _send_event(user.conn, user.send_lock, {
                "type": "m.file.download_chunk",
                "content": {"file_id": file_id, "index": i, "total": total,
                            "data": base64.b64encode(chunk).decode("ascii")},
            })
        _send_event(user.conn, user.send_lock, {
            "type": "m.file.download_end",
            "content": {"file_id": file_id},
        })

    # ── Cross-reference search ─────────────────────────────────────────────────

    def _on_search(self, user: SpoakenUser, c: dict, room_id: str):
        """
        Federated cross-reference search across message history and log files.

        content["scope"] options:
          "room"     — current room only (messages + files)
          "all"      — every room the user belongs to
          "messages" — messages only
          "files"    — log files only
        """
        query   = _sanitise(c.get("query", ""), 256)
        scope   = c.get("scope", "room")
        room_id = room_id or c.get("room_id", "")

        if not query:
            return self._err(user, "M_INVALID", "Query required.")

        if scope == "all":
            search_rooms = list(user.rooms)
        elif room_id and room_id in user.rooms:
            search_rooms = [room_id]
        else:
            return self._err(user, "M_FORBIDDEN", "No valid room scope.")

        msgs  = self._db.search_events(query, search_rooms) if scope != "files" else []
        files = self._db.search_files(query, search_rooms) if scope != "messages" else []

        _send_event(user.conn, user.send_lock, {
            "type": "m.search.results",
            "content": {
                "query"     : query,
                "scope"     : scope,
                "messages"  : msgs,
                "files"     : files,
                "total_hits": len(msgs) + len(files),
            },
        })
        self._log(
            f"[LAN]: search '{query}' scope={scope} "
            f"hits={len(msgs)+len(files)} user={user.username}"
        )

    # ── Users list ─────────────────────────────────────────────────────────────

    def _on_users(self, user: SpoakenUser, c: dict, room_id: str):
        room_id = room_id or c.get("room_id", "")
        if room_id:
            with self._rooms_lock:
                room = self._rooms.get(room_id)
            if not room or room_id not in user.rooms:
                return self._err(user, "M_FORBIDDEN", "Not in room.")
            with self._users_lock:
                online = [u.username for u in self._users.values()
                          if room_id in u.rooms]
            _send_event(user.conn, user.send_lock, {
                "type": "m.users", "room_id": room_id,
                "content": {"members": dict(room.members), "online": online},
            })
        else:
            with self._users_lock:
                all_users = list(self._users_by_name.keys())
            _send_event(user.conn, user.send_lock,
                        {"type": "m.users", "content": {"online": all_users}})

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _err(self, user: SpoakenUser, code: str, error: str):
        _send_event(user.conn, user.send_lock,
                    {"type": "m.error", "content": {"code": code, "error": error}})

    def _persist_event(self, room_id: str, sender: str,
                       ev_type: str, content: dict) -> dict:
        ev = ChatEvent(
            event_id=_make_event_id(room_id), room_id=room_id,
            sender=sender, type=ev_type, content=content, timestamp=_now_ms()
        )
        self._db.save_event(ev)
        return ev.to_dict()

    def _broadcast(self, room_id: str, ev_dict: dict,
                   exclude: Optional[str] = None):
        with self._users_lock:
            recipients = [
                u for u in self._users.values()
                if room_id in u.rooms and u.username != exclude
            ]
        for u in recipients:
            _send_event(u.conn, u.send_lock, ev_dict)

    def _broadcast_raw(self, room_id: str, ev_dict: dict,
                       exclude: Optional[str] = None):
        self._broadcast(room_id, ev_dict, exclude)

    def _disconnect_user(self, user: SpoakenUser):
        with self._users_lock:
            self._users.pop(user.conn, None)
            self._users_by_name.pop(user.username, None)
        for room_id in list(user.rooms):
            with self._rooms_lock:
                room = self._rooms.get(room_id)
            if room:
                leave_ev = self._persist_event(
                    room_id, user.user_id, "m.room.member",
                    {"membership": "leave", "username": user.username,
                     "reason": "disconnected"}
                )
                self._broadcast(room_id, leave_ev)
        try:
            user.conn.close()
        except Exception:
            pass
        _audit(f"DISCONNECTED {user.ip} user={user.username}")
        self._log(f"[LAN]: {user.username} disconnected")

    def _load_persisted_rooms(self):
        for room in self._db.load_rooms():
            room.members = self._db.load_members(room.room_id)
            self._rooms[room.room_id] = room
        self._log(f"[LAN]: loaded {len(self._rooms)} room(s) from DB")

    # ── Legacy broadcast API (used by spoaken_control.py) ─────────────────────
    def send(self, message: str):
        safe = _sanitise(message)
        ev   = {"type": "m.server.notice", "content": {"body": safe}}
        with self._users_lock:
            for u in self._users.values():
                _send_event(u.conn, u.send_lock, ev)


# ═══════════════════════════════════════════════════════════════════════════════
# SpoakenLANClient
# ═══════════════════════════════════════════════════════════════════════════════

class SpoakenLANClient:
    """
    Full-featured client for SpoakenLANServer.

        client = SpoakenLANClient(
            username="alice", server_token="mytoken",
            on_event=my_callback, log_cb=print,
        )
        ok = client.connect("192.168.1.10", 55300)
        client.create_room("Physics 101", password="letmein", public=True)
        client.send_message(room_id, "Hello class!")
        client.send_log_file(room_id, Path("Logs/session.txt"))
        client.search(room_id, "quantum", scope="all")
        client.disconnect()

    on_event(event: dict) is called in a background thread for every
    server event.  event["type"] is one of the m.* types.
    """

    def __init__(
        self,
        username    : str,
        server_token: str = "spoaken",
        on_event    : Optional[Callable[[dict], None]] = None,
        log_cb      : Callable[[str], None] = print,
    ):
        self._username = username
        self._token    = server_token
        self._on_event = on_event or (lambda e: None)
        self._log      = log_cb

        self._conn      : Optional[socket.socket] = None
        self._lock      = threading.Lock()
        self._connected = False
        self._reader    : Optional[_FrameReader]  = None
        self._user_id   = ""

        # Local room cache
        self.rooms       : Dict[str, dict]  = {}
        # In-progress downloads
        self._downloads  : Dict[str, dict]  = {}
        # Transfer ack slot
        self._pending_xfer_id: Optional[str] = None
        self._xfer_event = threading.Event()

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self, host: str, port: int, timeout: float = 10.0) -> bool:
        try:
            conn = socket.create_connection((host, port), timeout=timeout)
            conn.settimeout(timeout)
            self._conn   = conn
            self._reader = _FrameReader(conn)

            ev = self._reader.read_event(timeout=timeout)
            if not ev or ev.get("type") != "m.auth.challenge":
                self._log("[Client]: No auth challenge.")
                conn.close()
                return False

            challenge = bytes.fromhex(ev["content"]["challenge"])
            response  = _hmac_sign(self._token, challenge).hex()
            self._raw_send({"type": "c.auth",
                            "content": {"username": self._username,
                                        "response": response}})

            ev = self._reader.read_event(timeout=timeout)
            if not ev or ev.get("type") != "m.auth.ok":
                err = (ev or {}).get("content", {}).get("error", "unknown")
                self._log(f"[Client]: Auth failed — {err}")
                conn.close()
                return False

            self._user_id   = ev["content"].get("user_id", "")
            server_name     = ev["content"].get("server_name", host)
            self._connected = True

            threading.Thread(target=self._recv_loop, daemon=True,
                             name=f"recv-{host}").start()

            self._log(
                f"[Client]: ✔  connected to '{server_name}'  "
                f"as {self._user_id}"
            )
            return True
        except Exception as exc:
            self._log(f"[Client]: Connection failed — {exc}")
            return False

    def disconnect(self):
        self._connected = False
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def is_connected(self) -> bool:
        return self._connected

    # ── Room API ───────────────────────────────────────────────────────────────

    def create_room(self, name: str, password: str,
                    public: bool = True, topic: str = "") -> bool:
        return self._send({"type": "c.room.create",
                           "content": {"name": name, "password": password,
                                       "public": public, "topic": topic}})

    def join_room(self, room_id: str, password: str) -> bool:
        return self._send({"type": "c.room.join", "room_id": room_id,
                           "content": {"room_id": room_id,
                                       "password": password}})

    def leave_room(self, room_id: str) -> bool:
        return self._send({"type": "c.room.leave",
                           "room_id": room_id, "content": {}})

    def list_rooms(self) -> bool:
        return self._send({"type": "c.room.list", "content": {}})

    def get_history(self, room_id: str,
                    limit: int = 50, before_ts: int = 0) -> bool:
        return self._send({"type": "c.room.history", "room_id": room_id,
                           "content": {"limit": limit,
                                       "before_ts": before_ts}})

    def set_topic(self, room_id: str, topic: str) -> bool:
        return self._send({"type": "c.room.topic", "room_id": room_id,
                           "content": {"topic": topic}})

    def kick(self, room_id: str, username: str) -> bool:
        return self._send({"type": "c.room.kick", "room_id": room_id,
                           "content": {"username": username}})

    def ban(self, room_id: str, username: str, reason: str = "") -> bool:
        return self._send({"type": "c.room.ban", "room_id": room_id,
                           "content": {"username": username,
                                       "reason": reason}})

    def promote(self, room_id: str, username: str,
                role: str = "admin") -> bool:
        return self._send({"type": "c.room.promote", "room_id": room_id,
                           "content": {"username": username, "role": role}})

    def list_files(self, room_id: str) -> bool:
        return self._send({"type": "c.room.files",
                           "room_id": room_id, "content": {}})

    def list_users(self, room_id: str = "") -> bool:
        return self._send({"type": "c.users",
                           "room_id": room_id, "content": {}})

    # ── Messaging ──────────────────────────────────────────────────────────────

    def send_message(self, room_id: str, body: str,
                     msgtype: str = "m.text") -> bool:
        return self._send({"type": "c.message", "room_id": room_id,
                           "content": {"msgtype": msgtype, "body": body}})

    def send_emote(self, room_id: str, body: str) -> bool:
        return self.send_message(room_id, body, "m.emote")

    # ── File transfer ──────────────────────────────────────────────────────────

    def send_log_file(
        self,
        room_id    : str,
        filepath   : Path,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        timeout    : float = 30.0,
    ) -> bool:
        """
        Push a file to a room.  Blocks until transfer is complete.
        progress_cb(bytes_sent, total_bytes)
        """
        if not filepath.exists():
            self._log(f"[Client]: File not found — {filepath}")
            return False

        data     = filepath.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        size     = len(data)

        self._xfer_event.clear()
        self._pending_xfer_id = None
        self._send({"type": "c.file.begin", "room_id": room_id,
                    "content": {"filename": filepath.name,
                                "size": size, "checksum": checksum}})

        # Wait for begin_ack
        if not self._xfer_event.wait(timeout=timeout):
            self._log("[Client]: Transfer ack timeout.")
            return False
        xfer_id = self._pending_xfer_id
        if not xfer_id:
            return False

        # Send chunks
        total = math.ceil(size / _CHUNK_BYTES)
        for i in range(total):
            chunk = data[i * _CHUNK_BYTES: (i + 1) * _CHUNK_BYTES]
            self._send({"type": "c.file.chunk",
                        "content": {
                            "transfer_id": xfer_id,
                            "data": base64.b64encode(chunk).decode("ascii"),
                            "index": i, "total": total,
                        }})
            if progress_cb:
                progress_cb(min((i + 1) * _CHUNK_BYTES, size), size)

        self._send({"type": "c.file.end",
                    "content": {"transfer_id": xfer_id}})
        self._log(f"[Client]: file sent '{filepath.name}'  {size//1024} KB")
        return True

    def download_file(self, file_id: str) -> bool:
        return self._send({"type": "c.file.get",
                           "content": {"file_id": file_id}})

    # ── Cross-reference search ─────────────────────────────────────────────────

    def search(self, query: str, room_id: str = "",
               scope: str = "room") -> bool:
        return self._send({"type": "c.search", "room_id": room_id,
                           "content": {"query": query, "scope": scope,
                                       "room_id": room_id}})

    def ping(self) -> bool:
        return self._send({"type": "c.ping", "content": {}})

    # ── Internal ───────────────────────────────────────────────────────────────

    def _send(self, ev: dict) -> bool:
        return self._raw_send(ev)

    def _raw_send(self, ev: dict) -> bool:
        if not self._conn:
            return False
        return _send_event(self._conn, self._lock, ev)

    def _recv_loop(self):
        while self._connected and self._reader:
            ev = self._reader.read_event()
            if ev is None:
                break
            t = ev.get("type", "")

            # Local state
            if t == "m.room.created":
                self.rooms[ev["content"]["room_id"]] = ev["content"]
            elif t == "m.room.joined":
                self.rooms[ev["content"]["room_id"]] = ev["content"]
            elif t == "m.room.left":
                self.rooms.pop(ev["content"].get("room_id", ""), None)
            elif t == "m.file.begin_ack":
                self._pending_xfer_id = ev["content"].get("transfer_id")
                self._xfer_event.set()
            elif t == "m.file.download_begin":
                fid = ev["content"]["file_id"]
                self._downloads[fid] = {"meta": ev["content"], "chunks": []}
            elif t == "m.file.download_chunk":
                fid = ev["content"]["file_id"]
                dl  = self._downloads.get(fid)
                if dl:
                    dl["chunks"].append(
                        base64.b64decode(ev["content"]["data"])
                    )
            elif t == "m.file.download_end":
                fid = ev["content"]["file_id"]
                dl  = self._downloads.pop(fid, None)
                if dl:
                    data     = b"".join(dl["chunks"])
                    checksum = hashlib.sha256(data).hexdigest()
                    meta     = dl["meta"]
                    if hmac.compare_digest(checksum, meta.get("checksum", "")):
                        fname    = pathlib.Path(meta["filename"]).name
                        out      = LOG_DIR / "downloads" / fname
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(data)
                        ev["_local_path"] = str(out)
                        self._log(f"[Client]: download saved → {out}")
                    else:
                        ev["_checksum_error"] = True
                        self._log("[Client]: download checksum mismatch!")

            try:
                self._on_event(ev)
            except Exception as exc:
                _log.warning("on_event error: %s", exc)

        self._connected = False
        self._log("[Client]: disconnected.")
        try:
            self._on_event({"type": "m.client.disconnected"})
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience discovery helpers
# ═══════════════════════════════════════════════════════════════════════════════

_global_scanner: Optional[ServerScanner] = None
_scanner_lock   = threading.Lock()

def get_scanner() -> ServerScanner:
    """Return (and lazily start) the global LAN server scanner."""
    global _global_scanner
    with _scanner_lock:
        if _global_scanner is None:
            _global_scanner = ServerScanner()
            _global_scanner.start()
    return _global_scanner

def discover_servers(wait: float = 2.0) -> List[ServerEntry]:
    """Block for *wait* seconds collecting beacon packets, then return results."""
    s = get_scanner()
    time.sleep(wait)
    return s.get_servers()


# ═══════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
# LEGACY API — ChatServer and SSEServer from v2  (unchanged for compatibility)
# ─────────────────────────────────────────────────────────────────────────────
# spoaken_control.py imports these directly; they are preserved here verbatim.
# ═══════════════════════════════════════════════════════════════════════════════

_MAX_CONN_V2   = 3
_MAX_MSG_V2    = 512
_RATE_V2       = 8
_STRIKES_V2    = 3
_AUTH_TO_V2    = 10.0
_RECV_TO_V2    = 90.0
_CHAL_BYTES_V2 = 32
_SSE_QMAX      = 500
_SSE_KA        = 20

def _san_v2(raw: str) -> str:
    return _CTRL_RE.sub("", raw).strip()[:_MAX_MSG_V2]

def _hmac_v2(secret: str, challenge: bytes) -> bytes:
    return hmac.new(secret.encode(), challenge, hashlib.sha256).digest()


class ChatServer:
    """
    Legacy v2 simple TCP broadcast server.
    Preserved for spoaken_control.py compatibility.
    For new code, use SpoakenLANServer.
    """

    def __init__(self, port: int, token: str,
                 on_message=None, log_cb=None,
                 bind_address: str = "0.0.0.0"):
        self._port   = port
        self._token  = token
        self._on_msg = on_message or (lambda ip, msg: None)
        self._log    = log_cb or print
        self._bind   = bind_address

        self._srv_sock  : Optional[socket.socket]       = None
        self._clients   : List[socket.socket]            = []
        self._client_ips: Dict[socket.socket, str]       = {}
        self._clients_lock = threading.Lock()
        self._running   = False

        self._conn_counts  : Dict[str, int] = defaultdict(int)
        self._strike_counts: Dict[str, int] = defaultdict(int)
        self._banned_ips   : set            = set()
        self._rate_windows : Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_RATE_V2 + 1)
        )
        self._sec_lock = threading.Lock()

    def start(self) -> bool:
        if self._running:
            return False
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self._bind, self._port))
            srv.listen(16)
            srv.settimeout(1.0)
            self._srv_sock = srv
            self._running  = True
            threading.Thread(target=self._accept_loop, daemon=True,
                             name="chat-accept").start()
            self._log(f"[Chat]: port {self._port} open  bind={self._bind}")
            _audit(f"LEGACY_STARTED {self._bind}:{self._port}")
            return True
        except OSError as exc:
            self._log(f"[Chat Error]: port {self._port} — {exc}")
            return False

    def stop(self):
        if not self._running:
            return
        self._running = False
        with self._clients_lock:
            for c in list(self._clients):
                try:
                    c.sendall(b"SERVER_CLOSING\n")
                    c.close()
                except Exception:
                    pass
            self._clients.clear()
            self._client_ips.clear()
        try:
            if self._srv_sock:
                self._srv_sock.close()
        except Exception:
            pass
        self._srv_sock = None
        self._log(f"[Chat]: port {self._port} closed")

    def send(self, message: str):
        safe    = _san_v2(message)
        payload = (safe + "\n").encode("utf-8")
        dead    = []
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.sendall(payload)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._evict(c, "send failed")

    def peer_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def is_open(self) -> bool:
        return self._running

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._srv_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            with self._sec_lock:
                if ip in self._banned_ips:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                if self._conn_counts[ip] >= _MAX_CONN_V2:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                self._conn_counts[ip] += 1
            threading.Thread(target=self._handle_client, args=(conn, ip),
                             daemon=True, name=f"chat-{ip}").start()

    def _handle_client(self, conn: socket.socket, ip: str):
        try:
            conn.settimeout(_AUTH_TO_V2)
            chal = os.urandom(_CHAL_BYTES_V2)
            conn.sendall(chal.hex().encode("ascii") + b"\n")
            resp = conn.recv(128).decode("ascii", errors="ignore").strip()
            exp  = _hmac_v2(self._token, chal).hex()
            if not hmac.compare_digest(resp, exp):
                with self._sec_lock:
                    self._strike_counts[ip] += 1
                    if self._strike_counts[ip] >= _STRIKES_V2:
                        self._banned_ips.add(ip)
                conn.sendall(b"AUTH_FAIL\n")
                conn.close()
                return
            with self._sec_lock:
                self._strike_counts[ip] = 0
            conn.sendall(b"AUTH_OK\n")
            conn.settimeout(_RECV_TO_V2)
            with self._clients_lock:
                self._clients.append(conn)
                self._client_ips[conn] = ip
            _audit(f"LEGACY_CONNECTED {ip}")
            buf = b""
            while self._running:
                try:
                    chunk = conn.recv(1024)
                except socket.timeout:
                    try:
                        conn.sendall(b"PING\n")
                    except Exception:
                        break
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = _san_v2(line.decode("utf-8", errors="ignore"))
                    if not msg:
                        continue
                    now = time.monotonic()
                    with self._sec_lock:
                        w = self._rate_windows[ip]
                        w.append(now)
                        if len(w) > _RATE_V2 and (now - w[0]) < 1.0:
                            try:
                                conn.sendall(b"RATE_LIMITED\n")
                            except Exception:
                                pass
                            continue
                    try:
                        self._on_msg(ip, msg)
                    except Exception:
                        pass
        except Exception as exc:
            _audit(f"LEGACY_ERR {ip}: {exc}")
        finally:
            self._evict(conn, "disconnect")
            with self._sec_lock:
                self._conn_counts[ip] = max(0, self._conn_counts[ip] - 1)

    def _evict(self, conn: socket.socket, reason: str = ""):
        with self._clients_lock:
            ip = self._client_ips.pop(conn, "?")
            if conn in self._clients:
                self._clients.remove(conn)
        try:
            conn.close()
        except Exception:
            pass
        _audit(f"LEGACY_DISC {ip} ({reason})")


class SSEServer:
    """
    Legacy v2 HTTP SSE server.
    Preserved for spoaken_control.py compatibility.
    """

    def __init__(self, port: int, log_cb=None):
        self._port    = port
        self._log     = log_cb or print
        self._clients : List[Queue] = []
        self._lock    = threading.Lock()
        self._srv     = None
        self._running = False

    def start(self):
        if self._running:
            return
        threading.Thread(target=self._serve, daemon=True,
                         name="sse-server").start()

    def stop(self):
        self._running = False
        if self._srv:
            try:
                self._srv.shutdown()
            except Exception:
                pass
        self._srv = None
        self._log(f"[Android Stream]: port {self._port} closed")

    def push(self, text: str):
        safe = _san_v2(text)
        if not safe:
            return
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(safe)
                except Full:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)

    def is_open(self) -> bool:
        return self._running

    def _add_client(self) -> Queue:
        q = Queue(maxsize=_SSE_QMAX)
        with self._lock:
            self._clients.append(q)
        return q

    def _remove_client(self, q: Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _serve(self):
        import http.server
        sse = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/":
                    self._page()
                elif self.path == "/stream":
                    self._stream()
                else:
                    self.send_error(404)

            def _page(self):
                html = (
                    "<!doctype html><html><head>"
                    "<meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>Spoaken Live</title>"
                    "<style>body{background:#060c1a;color:#00bdff;font-family:monospace;"
                    "padding:16px;margin:0;}h2{color:#00e5cc;}"
                    "#log{white-space:pre-wrap;font-size:15px;line-height:1.5;}"
                    "</style></head><body>"
                    "<h2>◈ SPOAKEN — Live Transcript</h2>"
                    "<div id='log'></div><script>"
                    "const el=document.getElementById('log');"
                    "const es=new EventSource('/stream');"
                    "es.onmessage=e=>{el.textContent+=e.data+'\\n';"
                    "window.scrollTo(0,document.body.scrollHeight);};"
                    "es.onerror=()=>{el.textContent+="
                    "'[disconnected]\\n';};"
                    "</script></body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                origin = self.headers.get("Origin", "")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.end_headers()
                q = sse._add_client()
                try:
                    while sse._running:
                        try:
                            msg = q.get(timeout=_SSE_KA)
                            self.wfile.write(
                                f"data: {msg}\n\n".encode("utf-8")
                            )
                            self.wfile.flush()
                        except Exception:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except Exception:
                    pass
                finally:
                    sse._remove_client(q)

        try:
            import socket as _sock
            self._srv  = http.server.HTTPServer(("", self._port), _H)
            local_ip   = _sock.gethostbyname(_sock.gethostname())
            self._running = True
            self._log(
                f"[Android Stream]: http://{local_ip}:{self._port}"
            )
            self._srv.serve_forever()
        except Exception as exc:
            self._log(f"[Android Stream Error]: {exc}")
        finally:
            self._running = False
            
            
            
            
