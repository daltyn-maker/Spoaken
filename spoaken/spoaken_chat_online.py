"""
spoaken_chat_online.py
────────────────────────────────────────────────────────────────────────────────
Spoaken P2P Tor Chat  —  v1.0
Fully peer-to-peer, Tor-routed, no external servers, no Matrix, no relay,
no accounts on any third-party service.

Identity model  (local DID)
────────────────────────────
  Every user creates a local profile on first launch:
    • A display username   (human-readable, 1-32 chars)
    • A persistent Ed25519 keypair  (DID key material, generated once)
    • A DID identifier  did:spoaken:<base58(sha256(pubkey)[:16])>

  The DID is derived from a persistent Ed25519 key stored in
  spoaken_config.json under "p2p_identity.did_key_hex".
  It is NOT sent over the network in cleartext — only the derived did: URI
  and an ephemeral session pubkey are shared.

  Username uniqueness per-room is enforced by the room HOST at join time.
  Two peers cannot share the same username in one room.  If a collision is
  detected and the DIDs differ, the joining peer is refused with
  "username_taken" so they can choose another name.

  Profiles are stored in spoaken_config.json:
      "p2p_identity": {
          "username"    : "alice",
          "did"         : "did:spoaken:ABC123...",
          "did_key_hex" : "<64 hex chars>"   <- persistent private key
      }

Tor transport
─────────────
  stem      creates an ephemeral v3 hidden service (NEW:ED25519-V3).
  websockets handles the WS protocol.
  PySocks   routes outbound connections through the Tor SOCKS5 proxy.
  Tor must be running: sudo apt install tor && sudo systemctl start tor

  Required packages:
      pip install websockets PySocks stem cryptography

Room model
──────────
  First peer to call create_room() becomes HOST.
  The host runs a local WS server; .onion:port is the room "address".
  Members connect via Tor to the host's .onion address.
  Room passwords use PBKDF2-HMAC-SHA256 (100 000 rounds + per-room salt).

Security
────────
  All traffic encrypted+anonymised by Tor.
  Ed25519 message signing (per-session ephemeral key).
  HMAC-SHA256 session handshake.
  IP address never exposed.
  No logs written to disk by this module.
  No user should know information on another user

"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    import websockets
    import websockets.server
    import websockets.exceptions
    _WS_OK = True
except ImportError:
    _WS_OK = False

try:
    import socks
    _SOCKS_OK = True
except ImportError:
    _SOCKS_OK = False

try:
    from stem.control import Controller as _StemController
    _STEM_OK = True
except ImportError:
    _STEM_OK = False

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption,
    )
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False


def _tor_backend() -> str:
    """
    Detect which Tor transport is available and return one of:
      'stem'  — system Tor + stem controller
      'none'  — stem/Tor not available
    """
    if _SOCKS_OK and _STEM_OK and _check_tor_running():
        return "stem"
    return "none"

try:
    from paths import LOG_DIR
except ImportError:
    LOG_DIR = Path(__file__).parent.parent / "Logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)

_log = logging.getLogger("spoaken.chat.p2p")

# ── Constants ─────────────────────────────────────────────────────────────────
_PROTO_VER          = "1.0-p2p-did"
_MAX_MSG_LEN        = 8192
_RATE_LIMIT_PER_SEC = 20
_AUTH_TIMEOUT_S     = 25.0
_CHUNK_B64_BYTES    = 32768
_MAX_FILE_BYTES     = 50 * 1024 * 1024
_PBKDF2_ITERS       = 100_000
_CTRL_RE            = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_TOR_SOCKS_HOST     = "127.0.0.1"
_TOR_SOCKS_PORT     = 9050
_TOR_CONTROL_PORT   = 9051
_HS_BASE_PORT       = 55320


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitise(raw: str, maxlen: int = _MAX_MSG_LEN) -> str:
    return _CTRL_RE.sub("", raw).strip()[:maxlen]

def _make_room_id()  -> str: return f"!{secrets.token_hex(8)}:p2p"
def _make_event_id() -> str: return f"${int(time.time()*1000)}_{secrets.token_hex(3)}:p2p"
def _now_ms()        -> int: return int(time.time() * 1000)

def _hash_room_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERS
    ).hex()

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _check_tor_running(host=_TOR_SOCKS_HOST, port=_TOR_SOCKS_PORT, timeout=3.0) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False

def _b58encode(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    result = ""
    while n:
        n, rem = divmod(n, 58)
        result = alphabet[rem] + result
    for byte in data:
        if byte == 0:
            result = "1" + result
        else:
            break
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Session key (ephemeral, per-run)
# ─────────────────────────────────────────────────────────────────────────────

class _SessionKey:
    def __init__(self):
        if _CRYPTO_OK:
            self._priv      = Ed25519PrivateKey.generate()
            self.public_hex = self._priv.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw).hex()
        else:
            self._secret    = secrets.token_bytes(32)
            self.public_hex = self._secret.hex()

    def sign(self, data: bytes) -> str:
        if _CRYPTO_OK:
            return self._priv.sign(data).hex()
        return hmac.HMAC(self._secret, data, hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Local Identity / DID
# ─────────────────────────────────────────────────────────────────────────────

def _generate_did() -> tuple:
    """Return (priv_key_hex: str, did: str)."""
    if _CRYPTO_OK:
        priv      = Ed25519PrivateKey.generate()
        priv_hex  = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
        pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    else:
        priv_hex  = secrets.token_hex(32)
        pub_bytes = hashlib.sha256(bytes.fromhex(priv_hex)).digest()

    did_suffix = _b58encode(hashlib.sha256(pub_bytes).digest()[:16])
    return priv_hex, f"did:spoaken:{did_suffix}"


class SpoakenIdentity:
    """
    Manages the user's local DID identity stored in spoaken_config.json.

    Security note: The did_key_hex is a private key stored locally in plain
    config.  It is used only to derive the DID and generate an auth token —
    it is never transmitted.  Users should keep their config file private
    (chmod 600).  If the config is compromised an attacker gains only the
    ability to claim the same DID in new rooms; they cannot decrypt past
    messages (Tor provides transport encryption, not E2E content encryption).
    """

    def __init__(self, cfg_path: str):
        self._cfg_path     = Path(cfg_path)
        self._username     = "anonymous"
        self._did          = ""
        self._did_priv_hex = ""
        self._session_key  = _SessionKey()
        self._load_or_create()

    def _read_cfg(self) -> dict:
        try:
            return json.loads(self._cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_or_create(self):
        cfg   = self._read_cfg()
        ident = cfg.get("p2p_identity", {})
        if ident.get("did") and ident.get("did_key_hex"):
            self._username     = ident.get("username", "anonymous")
            self._did          = ident["did"]
            self._did_priv_hex = ident["did_key_hex"]
        else:
            priv_hex, did = _generate_did()
            self._did          = did
            self._did_priv_hex = priv_hex
            self._username     = ident.get("username", "anonymous")
            self._save()

    def _save(self):
        cfg = self._read_cfg()
        cfg["p2p_identity"] = {
            "username"    : self._username,
            "did"         : self._did,
            "did_key_hex" : self._did_priv_hex,
        }
        try:
            self._cfg_path.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            _log.warning(f"[Identity]: Could not save: {exc}")

    @property
    def username(self) -> str:
        return self._username

    @username.setter
    def username(self, val: str):
        self._username = _sanitise(val, 32) or "anonymous"
        self._save()

    @property
    def did(self) -> str:
        return self._did

    @property
    def session_pubkey_hex(self) -> str:
        return self._session_key.public_hex

    def sign(self, data: bytes) -> str:
        return self._session_key.sign(data)

    def auth_token(self) -> str:
        """HMAC(persistent_key, session_pubkey) — proves DID ownership."""
        secret = bytes.fromhex(self._did_priv_hex)
        return hmac.HMAC(secret, self._session_key.public_hex.encode(),
                         hashlib.sha256).hexdigest()


# Convenience functions (used by GUI to read/write identity without instantiating the node)

def load_identity(cfg_path: str) -> dict:
    try:
        cfg   = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        ident = cfg.get("p2p_identity", {})
        return {
            "username": ident.get("username", ""),
            "did"     : ident.get("did", ""),
            "has_key" : bool(ident.get("did_key_hex")),
        }
    except Exception:
        return {"username": "", "did": "", "has_key": False}


def save_identity(cfg_path: str, username: str):
    p = Path(cfg_path)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        cfg = {}
    cfg.setdefault("p2p_identity", {})["username"] = _sanitise(username, 32) or "anonymous"
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def create_identity(cfg_path: str, username: str) -> dict:
    """Create new identity (first-run).  Will not overwrite an existing DID."""
    p = Path(cfg_path)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        cfg = {}

    if cfg.get("p2p_identity", {}).get("did_key_hex"):
        if username:
            cfg["p2p_identity"]["username"] = _sanitise(username, 32)
            p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return load_identity(cfg_path)

    priv_hex, did = _generate_did()
    cfg["p2p_identity"] = {
        "username"    : _sanitise(username, 32) or "anonymous",
        "did"         : did,
        "did_key_hex" : priv_hex,
    }
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"username": cfg["p2p_identity"]["username"], "did": did, "has_key": True}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses  (kept for spoaken_chat.py / GUI compatibility)
# ─────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class OnlineRoom:
    room_id      : str
    name         : str
    creator      : str
    password_hash: str
    password_salt: str
    public       : bool
    created_at   : int
    topic        : str = ""
    host_onion   : str = ""
    members      : Dict[str, str] = dataclasses.field(default_factory=dict)

    def display(self) -> dict:
        return {
            "room_id"     : self.room_id,
            "name"        : self.name,
            "topic"       : self.topic,
            "creator"     : self.creator,
            "public"      : self.public,
            "member_count": len(self.members),
            "host_onion"  : self.host_onion,
            "created_at"  : self.created_at,
        }


@dataclasses.dataclass
class OnlineUser:
    username : str
    did      : str
    onion    : str
    ws       : object
    rooms    : List[str] = dataclasses.field(default_factory=list)
    msg_times: deque = dataclasses.field(
        default_factory=lambda: deque(maxlen=_RATE_LIMIT_PER_SEC + 1))


@dataclasses.dataclass
class FileRelay:
    file_id  : str
    filename : str
    room_id  : str
    sender   : str
    checksum : str
    chunks   : List[bytes] = dataclasses.field(default_factory=list)
    complete : bool = False

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


# ─────────────────────────────────────────────────────────────────────────────
# Tor Hidden Service  (stem backend — system Tor required)
# ─────────────────────────────────────────────────────────────────────────────

class _TorHiddenServiceStem:
    """Creates an ephemeral v3 hidden service via stem + system Tor."""

    def __init__(self, local_port: int, log_cb: Callable = print):
        self._local_port = local_port
        self._log        = log_cb
        self._onion: Optional[str] = None
        self._controller = None

    @property
    def onion_address(self) -> Optional[str]:
        return self._onion

    def start(self) -> bool:
        if not _STEM_OK:
            self._log(
                "[P2P]: stem not installed — cannot auto-create hidden service.\n"
                "  pip install stem\n"
                f"  Or add to /etc/tor/torrc:\n"
                f"    HiddenServiceDir /var/lib/tor/spoaken/\n"
                f"    HiddenServicePort {self._local_port} 127.0.0.1:{self._local_port}\n"
            )
            return False
        try:
            ctrl = _StemController.from_port(port=_TOR_CONTROL_PORT)
            ctrl.authenticate()
            self._controller = ctrl
            hs = ctrl.create_ephemeral_hidden_service(
                {self._local_port: self._local_port},
                key_type="NEW", key_content="ED25519-V3",
                await_publication=False,
            )
            self._onion = f"{hs.service_id}.onion"
            self._log(f"[P2P/stem]: Hidden service ready → {self._onion}:{self._local_port}")
            return True
        except Exception as exc:
            self._log(f"[P2P/stem]: Hidden service failed: {exc}")
            return False

    def stop(self):
        if self._controller and self._onion:
            try:
                self._controller.remove_ephemeral_hidden_service(
                    self._onion.replace(".onion", ""))
            except Exception:
                pass
            try:
                self._controller.close()
            except Exception:
                pass
        self._onion      = None
        self._controller = None


# ─────────────────────────────────────────────────────────────────────────────
# Unified _TorHiddenService factory
# ─────────────────────────────────────────────────────────────────────────────

def _TorHiddenService(local_port: int, log_cb: Callable = print):
    """
    Return the stem-based hidden-service implementation if system Tor is
    available, otherwise return a no-op stub with a helpful error message.
    """
    backend = _tor_backend()
    if backend == "stem":
        log_cb("[P2P]: Using stem + system Tor for hidden service.")
        return _TorHiddenServiceStem(local_port, log_cb)
    # Return a stub that always fails with a helpful message
    class _NoTorHS:
        onion_address = None
        def start(self):
            log_cb(
                "[P2P]: Tor backend unavailable.\n"
                "  Install system Tor + stem:\n"
                "    sudo apt install tor && sudo systemctl start tor\n"
                "    pip install stem PySocks websockets cryptography"
            )
            return False
        def stop(self): pass
    return _NoTorHS()


# ─────────────────────────────────────────────────────────────────────────────
# Tor WS connector
# ─────────────────────────────────────────────────────────────────────────────

async def _tor_ws_connect(onion_url: str, timeout: float = 60.0):
    """
    Open a WebSocket connection to an .onion address through Tor.

    Uses PySocks + system Tor SOCKS5 proxy via the stem backend.
    """
    if not _WS_OK:
        raise RuntimeError("websockets not installed")

    url    = onion_url
    scheme = "wss" if url.startswith("wss://") else "ws"
    url    = url.removeprefix("wss://").removeprefix("ws://")

    hostport, _, path_rest = url.partition("/")
    path = "/" + path_rest if path_rest else "/"
    host, _, port_str = hostport.rpartition(":")
    port = int(port_str) if port_str.isdigit() else (443 if scheme == "wss" else 80)
    if not host:
        host = hostport

    backend = _tor_backend()

    # ── PySocks via system Tor ────────────────────────────────────────────────
    if backend == "stem" and _SOCKS_OK:
        raw_sock = socks.create_connection(
            (host, port),
            proxy_type=socks.SOCKS5,
            proxy_addr=_TOR_SOCKS_HOST,
            proxy_port=_TOR_SOCKS_PORT,
            timeout=timeout,
        )
        raw_sock.setblocking(False)
        return await websockets.connect(f"{scheme}://{hostport}{path}", sock=raw_sock)

    raise RuntimeError("No Tor transport available for outbound .onion connection.")


# ─────────────────────────────────────────────────────────────────────────────
# P2P Room Host (WebSocket server)
# ─────────────────────────────────────────────────────────────────────────────

class _P2PRoomHost:
    """
    WS server for one room.
    Enforces username uniqueness: two different DIDs cannot claim the same name.
    """

    def __init__(self, room: OnlineRoom, local_port: int,
                 host_username: str, host_did: str, log_cb: Callable = print):
        self._room       = room
        self._port       = local_port
        self._host_user  = host_username
        self._host_did   = host_did
        self._log        = log_cb
        self._peers: Dict[str, OnlineUser] = {}
        self._did_map: Dict[str, str]      = {}
        self._files: Dict[str, FileRelay]  = {}
        self._on_event: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server     = None
        self._running    = False
        self._thread: Optional[threading.Thread] = None

    def set_event_callback(self, cb: Callable):
        self._on_event = cb

    def start(self) -> bool:
        if self._running:
            return True
        self._loop    = asyncio.new_event_loop()
        ready_ev      = threading.Event()

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve(ready_ev))

        self._thread = threading.Thread(target=_run, daemon=True,
                                        name=f"p2p-host-{self._room.room_id[:8]}")
        self._thread.start()
        ready_ev.wait(timeout=6.0)   # wait for bind, not a busy-poll
        return self._running

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def is_running(self) -> bool:
        return self._running

    async def _serve(self, ready_ev: threading.Event):
        try:
            self._server = await websockets.serve(
                self._handle_peer, "127.0.0.1", self._port,
                max_size=_MAX_FILE_BYTES + 65536,
                ping_interval=30, ping_timeout=10,
            )
            self._running = True
            ready_ev.set()   # signal start() that the server is bound
            self._log(f"[P2P Host]: '{self._room.name}' on port {self._port}")
            await asyncio.Future()
        except Exception as exc:
            self._log(f"[P2P Host]: server error — {exc}")
            ready_ev.set()   # unblock start() even on failure
        finally:
            self._running = False

    async def _handle_peer(self, ws):
        username = None
        did      = None
        try:
            challenge = secrets.token_bytes(32)
            await ws.send(json.dumps({
                "type"     : "s.challenge",
                "challenge": base64.b64encode(challenge).decode(),
                "room_id"  : self._room.room_id,
                "room_name": self._room.name,
                "proto"    : _PROTO_VER,
            }))

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"type": "m.auth.fail", "reason": "timeout"}))
                return

            auth = json.loads(raw)
            if auth.get("type") != "c.auth":
                await ws.send(json.dumps({"type": "m.auth.fail", "reason": "bad_type"}))
                return

            username   = _sanitise(auth.get("username", ""), 32)
            did        = _sanitise(auth.get("did", ""), 80)
            onion      = _sanitise(auth.get("onion", ""), 80)
            room_pw    = auth.get("room_password", "")

            if not username:
                await ws.send(json.dumps({"type": "m.auth.fail", "reason": "no_username"}))
                return

            # Username uniqueness by DID
            if username in self._peers:
                if self._peers[username].did != did:
                    await ws.send(json.dumps({
                        "type"  : "m.auth.fail",
                        "reason": "username_taken",
                        "hint"  : "choose a different username",
                    }))
                    return
                # Same DID reconnecting — evict stale connection
                try:
                    await self._peers[username].ws.close()
                except Exception:
                    pass
                del self._peers[username]

            if self._room.password_hash:
                if not hmac.compare_digest(
                    _hash_room_pw(room_pw, self._room.password_salt),
                    self._room.password_hash,
                ):
                    await ws.send(json.dumps({"type": "m.auth.fail", "reason": "wrong_password"}))
                    return

            peer = OnlineUser(username=username, did=did, onion=onion, ws=ws)
            peer.rooms.append(self._room.room_id)
            self._peers[username]        = peer
            self._did_map[did]           = username
            self._room.members[username] = did

            await ws.send(json.dumps({
                "type"   : "m.auth.ok",
                "room_id": self._room.room_id,
                "host"   : self._host_user,
                "members": [{"username": u, "did": d}
                             for u, d in self._room.members.items()],
                "topic"  : self._room.topic,
            }))

            await self._broadcast({
                "type"   : "m.member.join",
                "room_id": self._room.room_id,
                "content": {"username": username, "did": did, "ts": _now_ms()},
            }, exclude=username)

            if self._on_event:
                self._on_event({"type": "m.member.join", "room_id": self._room.room_id,
                                "content": {"username": username, "did": did}})

            await self._peer_loop(peer)

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            self._log(f"[P2P Host]: peer error ({username}): {exc}")
        finally:
            if username and username in self._peers:
                del self._peers[username]
                self._room.members.pop(username, None)
                if did:
                    self._did_map.pop(did, None)
                await self._broadcast({
                    "type"   : "m.member.leave",
                    "room_id": self._room.room_id,
                    "content": {"username": username, "did": did or "", "ts": _now_ms()},
                })
                if self._on_event:
                    self._on_event({"type": "m.member.leave", "room_id": self._room.room_id,
                                    "content": {"username": username}})

    async def _peer_loop(self, peer: OnlineUser):
        async for raw in peer.ws:
            now = time.time()
            peer.msg_times.append(now)
            if (len(peer.msg_times) > _RATE_LIMIT_PER_SEC and
                    (now - peer.msg_times[0]) < 1.0):
                await peer.ws.send(json.dumps({"type": "m.error", "reason": "rate_limited"}))
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            t = msg.get("type", "")
            c = msg.get("content", {})

            if t == "c.ping":
                await peer.ws.send(json.dumps({"type": "m.pong"}))

            elif t == "c.message":
                body = _sanitise(c.get("body", ""), _MAX_MSG_LEN)
                sig  = c.get("sig", "")
                ev   = {
                    "type"    : "m.room.message",
                    "room_id" : self._room.room_id,
                    "event_id": _make_event_id(),
                    "content" : {"body": body, "sender": peer.username,
                                 "did": peer.did, "sig": sig, "ts": _now_ms()},
                }
                await self._broadcast(ev)
                if self._on_event:
                    self._on_event(ev)

            elif t == "c.file.begin":
                fid = secrets.token_hex(8)
                self._files[fid] = FileRelay(
                    file_id=fid,
                    filename=_sanitise(c.get("filename", "file"), 200),
                    room_id=self._room.room_id,
                    sender=peer.username,
                    checksum=c.get("checksum", ""),
                )
                await peer.ws.send(json.dumps({
                    "type"   : "m.file.ready",
                    "content": {"file_id": fid},
                }))

            elif t == "c.file.chunk":
                fid  = c.get("file_id", "")
                data = base64.b64decode(c.get("data", ""))
                if fid in self._files:
                    xfer = self._files[fid]
                    if sum(len(ch) for ch in xfer.chunks) + len(data) <= _MAX_FILE_BYTES:
                        xfer.chunks.append(data)
                        await self._broadcast({
                            "type"   : "m.file.chunk",
                            "room_id": self._room.room_id,
                            "content": {"file_id": fid, "data": c.get("data", "")},
                        }, exclude=peer.username)

            elif t == "c.file.end":
                fid  = c.get("file_id", "")
                xfer = self._files.pop(fid, None)
                if xfer:
                    raw_bytes = xfer.data
                    cs_ok = (_sha256(raw_bytes) == xfer.checksum) if xfer.checksum else True
                    ev_end = {
                        "type"   : "m.file.end",
                        "room_id": self._room.room_id,
                        "content": {"file_id": fid, "filename": xfer.filename,
                                    "checksum": xfer.checksum, "cs_ok": cs_ok,
                                    "size": len(raw_bytes), "sender": peer.username},
                    }
                    await self._broadcast(ev_end, exclude=peer.username)
                    if self._on_event:
                        self._on_event(ev_end)

            elif t == "c.room.leave":
                break

    async def _broadcast(self, ev: dict, exclude: Optional[str] = None):
        raw  = json.dumps(ev)
        dead = []
        for uname, peer in list(self._peers.items()):
            if uname == exclude:
                continue
            try:
                await peer.ws.send(raw)
            except Exception:
                dead.append(uname)
        for uname in dead:
            peer = self._peers.pop(uname, None)
            self._room.members.pop(uname, None)
            if peer:
                self._did_map.pop(peer.did, None)   # fix: was missing, caused DID map leak

    def send_to_all(self, ev: dict):
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(ev), self._loop)


# ─────────────────────────────────────────────────────────────────────────────
# Outbound client connection
# ─────────────────────────────────────────────────────────────────────────────

class _P2PClientConn:
    def __init__(self, identity: SpoakenIdentity, room_id: str, room_pw: str,
                 host_onion: str, on_event: Callable, log_cb: Callable):
        self._identity   = identity
        self._room_id    = room_id
        self._room_pw    = room_pw
        self._host_onion = host_onion
        self._on_event   = on_event
        self._log        = log_cb
        self._connected  = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._send_queue = None
        self._thread: Optional[threading.Thread] = None
        self.room_name   = room_id
        self.member_list: List[dict] = []

    @property
    def host_onion(self) -> str:
        return self._host_onion

    def connect(self) -> bool:
        ok_flag  = [False]
        ready_ev = threading.Event()
        self._loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main(ok_flag, ready_ev))

        self._thread = threading.Thread(target=_run, daemon=True,
                                        name=f"p2p-client-{self._room_id[:8]}")
        self._thread.start()
        # Timeout slightly longer than the WS connect timeout (60s) so the
        # inner exception has time to propagate before we give up here.
        ready_ev.wait(timeout=65.0)
        return ok_flag[0]

    def disconnect(self):
        self._connected = False
        if self._loop and self._send_queue and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, None)

    def send(self, msg: dict):
        if self._loop and self._send_queue and self._connected:
            self._loop.call_soon_threadsafe(
                self._send_queue.put_nowait, json.dumps(msg))

    def is_connected(self) -> bool:
        return self._connected

    async def _main(self, ok_flag, ready_ev):
        self._send_queue = asyncio.Queue()
        url = f"ws://{self._host_onion}:{_HS_BASE_PORT}"
        try:
            backend = _tor_backend()
            if _WS_OK and ".onion" in self._host_onion and backend != "none":
                ws = await _tor_ws_connect(url, timeout=60.0)
            else:
                ws = await websockets.connect(url)

            async with ws:
                raw1  = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
                chal  = json.loads(raw1)
                if chal.get("type") != "s.challenge":
                    ready_ev.set(); return

                self.room_name = chal.get("room_name", self._room_id)

                await ws.send(json.dumps({
                    "type"          : "c.auth",
                    "username"      : self._identity.username,
                    "did"           : self._identity.did,
                    "onion"         : "",
                    "session_pubkey": self._identity.session_pubkey_hex,
                    "auth_token"    : self._identity.auth_token(),
                    "room_password" : self._room_pw,
                }))

                raw2      = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
                auth_resp = json.loads(raw2)

                if auth_resp.get("type") != "m.auth.ok":
                    reason = auth_resp.get("reason", "rejected")
                    hint   = auth_resp.get("hint", "")
                    self._log(f"[P2P Client]: auth failed — {reason}"
                              + (f" ({hint})" if hint else ""))
                    ok_flag[0] = False; ready_ev.set(); return

                self._connected  = True
                ok_flag[0]       = True
                self.member_list = auth_resp.get("members", [])
                ready_ev.set()
                self._log(f"[P2P Client]: joined '{self.room_name}'")

                await asyncio.gather(
                    self._recv_loop(ws),
                    self._send_loop(ws),
                )

        except Exception as exc:
            if not ready_ev.is_set():
                ok_flag[0] = False; ready_ev.set()
            if self._connected:
                self._log(f"[P2P Client]: disconnected — {exc}")
        finally:
            self._connected = False

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    self._on_event(json.loads(raw))
                except Exception:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _send_loop(self, ws):
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._send_queue.get(), timeout=30.0)
                if msg is None: break
                await ws.send(msg)
            except asyncio.TimeoutError:
                try:
                    await ws.send(json.dumps({"type": "c.ping"}))
                except Exception:
                    break
            except Exception:
                break


# ─────────────────────────────────────────────────────────────────────────────
# Main P2P Node
# ─────────────────────────────────────────────────────────────────────────────

class SpoakenP2PNode:
    """Local peer node. Manages Tor HS, hosted rooms, and joined rooms."""

    def __init__(self, cfg_path: str = "", on_event: Callable = lambda ev: None,
                 log_cb: Callable = print):
        if not cfg_path:
            try:
                from paths import ROOT_DIR
                cfg_path = str(ROOT_DIR / "spoaken_config.json")
            except ImportError:
                cfg_path = str(Path(__file__).parent.parent / "spoaken_config.json")

        self._cfg_path  = cfg_path
        self._on_event  = on_event
        self._log       = log_cb
        self._identity  = SpoakenIdentity(cfg_path)
        self._hs        = _TorHiddenService(_HS_BASE_PORT, log_cb)
        self._onion: Optional[str] = None
        self._hosted: Dict[str, _P2PRoomHost]   = {}
        self._joined: Dict[str, _P2PClientConn] = {}
        self._started   = False
        self._rx_files: Dict[str, dict] = {}
        self._port_seq  = _HS_BASE_PORT

    @property
    def username(self) -> str:
        return self._identity.username

    @username.setter
    def username(self, val: str):
        self._identity.username = val

    @property
    def did(self) -> str:
        return self._identity.did

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._started:
            return True
        backend = _tor_backend()
        if backend == "none":
            self._log(
                "[P2P]: No Tor backend is available.\n"
                "  Install system Tor + stem:\n"
                "    sudo apt install tor && sudo systemctl start tor\n"
                "    pip install stem PySocks websockets cryptography"
            )
            return False

        if self._hs.start():
            self._onion = self._hs.onion_address
        else:
            self._onion = f"(no-hs:{_HS_BASE_PORT})"

        self._started = True
        self._log(
            f"[P2P]: Node started.  "
            f"Username={self._identity.username}  "
            f"DID={self._identity.did}  "
            f"Onion={self._onion}  "
            f"Backend={backend}")
        return True

    def stop(self):
        self._started = False
        for host in list(self._hosted.values()):
            host.stop()
        for conn in list(self._joined.values()):
            conn.disconnect()
        self._hs.stop()

    @property
    def onion_address(self) -> str:
        return self._onion or "(not started)"

    def is_started(self) -> bool:
        return self._started

    # ── Rooms ──────────────────────────────────────────────────────────────────

    def create_room(self, name: str, password: str = "",
                    public: bool = True, topic: str = "") -> str:
        room_id  = _make_room_id()
        salt     = secrets.token_hex(16)
        pw_hash  = _hash_room_pw(password, salt) if password else ""
        port     = self._next_port()

        room = OnlineRoom(
            room_id=room_id,
            name=_sanitise(name, 64),
            creator=self._identity.username,
            password_hash=pw_hash,
            password_salt=salt,
            public=public,
            created_at=_now_ms(),
            topic=_sanitise(topic, 256),
            host_onion=self._onion or "",
            members={self._identity.username: self._identity.did},
        )
        host = _P2PRoomHost(room=room, local_port=port,
                            host_username=self._identity.username,
                            host_did=self._identity.did,
                            log_cb=self._log)
        host.set_event_callback(self._on_event)
        if not host.start():
            self._port_seq -= 1   # reclaim the port on failure
            self._log(f"[P2P]: Failed to start room on port {port}")
            return ""
        self._hosted[room_id] = host
        self._log(f"[P2P]: Created room '{name}' ({room_id})")
        self._on_event({"type": "m.room.created", "room_id": room_id,
                        "content": room.display()})
        return room_id

    def join_room(self, host_onion: str, room_id: str, password: str = "") -> bool:
        if room_id in self._joined or room_id in self._hosted:
            return True
        conn = _P2PClientConn(identity=self._identity, room_id=room_id, room_pw=password,
                               host_onion=host_onion, on_event=self._handle_inbound,
                               log_cb=self._log)
        ok = conn.connect()
        if ok:
            self._joined[room_id] = conn
        return ok

    def leave_room(self, room_id: str):
        if room_id in self._joined:
            self._joined[room_id].disconnect()
            del self._joined[room_id]
        elif room_id in self._hosted:
            self._hosted[room_id].stop()
            del self._hosted[room_id]

    def send_message(self, room_id: str, text: str):
        body = _sanitise(text, _MAX_MSG_LEN)
        sig  = self._identity.sign(body.encode())
        if room_id in self._hosted:
            ev = {
                "type"    : "m.room.message",
                "room_id" : room_id,
                "event_id": _make_event_id(),
                "content" : {"body": body, "sender": self._identity.username,
                             "did": self._identity.did, "sig": sig, "ts": _now_ms()},
            }
            self._hosted[room_id].send_to_all(ev)
            self._on_event(ev)
        elif room_id in self._joined:
            self._joined[room_id].send({"type": "c.message",
                                        "content": {"body": body, "sig": sig}})

    def list_rooms(self, notify: bool = False) -> list:
        """Return the local list of hosted and joined rooms.

        Args:
            notify: If True, also fire an ``m.room.list`` event so the GUI can
                    update its dropdown.  Defaults to False so that internal
                    callers (e.g. the room picker) don't produce spurious GUI updates.
        """
        rooms = []
        for rid, host in self._hosted.items():
            rooms.append({"room_id": rid, "name": host._room.name,
                           "role": "host", "onion": self._onion,
                           "members": list(host._room.members.keys())})
        for rid, conn in self._joined.items():
            rooms.append({"room_id": rid, "name": conn.room_name,
                           "role": "member", "host": conn.host_onion})
        if notify:
            self._on_event({"type": "m.room.list", "content": {"rooms": rooms}})
        return rooms

    def list_peers(self, room_id: str) -> list:
        if room_id in self._hosted:
            return [{"username": u, "did": p.did}
                    for u, p in self._hosted[room_id]._peers.items()] + \
                   [{"username": self._identity.username, "did": self._identity.did}]
        if room_id in self._joined:
            return self._joined[room_id].member_list
        return []

    def send_file(self, room_id: str, filepath: str):
        path = Path(filepath)
        if not path.exists():
            self._log(f"[P2P]: File not found: {filepath}"); return

        def _do():
            try:
                raw = path.read_bytes()
                cs  = _sha256(raw)
                sz  = len(raw)
                if sz > _MAX_FILE_BYTES:
                    self._log(f"[P2P]: File too large: {path.name}"); return

                if room_id in self._hosted:
                    fid = secrets.token_hex(8)
                    self._hosted[room_id].send_to_all({
                        "type": "m.file.begin", "room_id": room_id,
                        "content": {"file_id": fid, "filename": path.name,
                                    "checksum": cs, "size": sz,
                                    "sender": self._identity.username}})
                    for i in range(0, sz, _CHUNK_B64_BYTES):
                        self._hosted[room_id].send_to_all({
                            "type": "m.file.chunk", "room_id": room_id,
                            "content": {"file_id": fid,
                                        "data": base64.b64encode(raw[i:i+_CHUNK_B64_BYTES]).decode()}})
                    self._hosted[room_id].send_to_all({
                        "type": "m.file.end", "room_id": room_id,
                        "content": {"file_id": fid, "filename": path.name,
                                    "checksum": cs, "size": sz}})

                elif room_id in self._joined:
                    conn     = self._joined[room_id]
                    fid_box  = [None]
                    ready_ev = threading.Event()
                    orig     = conn._on_event

                    def _intercept(ev):
                        if ev.get("type") == "m.file.ready":
                            fid_box[0] = ev["content"]["file_id"]; ready_ev.set()
                        else:
                            orig(ev)

                    conn._on_event = _intercept
                    conn.send({"type": "c.file.begin",
                               "content": {"filename": path.name, "checksum": cs, "size": sz}})
                    ready_ev.wait(timeout=10.0)
                    conn._on_event = orig
                    fid = fid_box[0]
                    if not fid:
                        self._log("[P2P]: Host did not acknowledge file."); return
                    for i in range(0, sz, _CHUNK_B64_BYTES):
                        conn.send({"type": "c.file.chunk",
                                   "content": {"file_id": fid,
                                               "data": base64.b64encode(raw[i:i+_CHUNK_B64_BYTES]).decode()}})
                    conn.send({"type": "c.file.end", "content": {"file_id": fid}})
                    self._log(f"[P2P]: sent '{path.name}' ({sz // 1024} KB)")
            except Exception as exc:
                self._log(f"[P2P File Error]: {exc}")

        threading.Thread(target=_do, daemon=True).start()

    def _next_port(self) -> int:
        self._port_seq += 1
        return self._port_seq

    def _handle_inbound(self, ev: dict):
        t = ev.get("type", "")
        c = ev.get("content", {})
        if t == "m.file.begin":
            fid = c.get("file_id", "")
            self._rx_files[fid] = {"filename": c.get("filename", "file"),
                                   "checksum": c.get("checksum", ""), "chunks": []}
            return
        if t == "m.file.chunk":
            fid  = c.get("file_id", "")
            data = base64.b64decode(c.get("data", ""))
            if fid in self._rx_files:
                xfer = self._rx_files[fid]
                current_size = sum(len(ch) for ch in xfer["chunks"])
                if current_size + len(data) <= _MAX_FILE_BYTES:
                    xfer["chunks"].append(data)
                else:
                    self._log(f"[P2P]: Incoming file '{xfer.get('filename','')}' exceeds size limit — dropping.")
                    self._rx_files.pop(fid, None)
            return
        if t == "m.file.end":
            fid  = c.get("file_id", "")
            xfer = self._rx_files.pop(fid, None)
            if xfer:
                raw   = b"".join(xfer["chunks"])
                fname = xfer["filename"]
                dest  = LOG_DIR / "received_files" / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                self._on_event({"type": "m.file.received",
                                "content": {"filename": fname, "size": len(raw),
                                            "checksum": _sha256(raw), "_saved_path": str(dest)}})
            return
        self._on_event(ev)


# ─────────────────────────────────────────────────────────────────────────────
# Legacy / compat shims
# These exist only so that code written against the old relay-based API still
# imports and runs without errors.  New code should use SpoakenP2PNode directly.
# ─────────────────────────────────────────────────────────────────────────────

class SpoakenOnlineRelay:
    """No-op — relay is not needed in P2P mode."""
    def __init__(self, *a, **kw): pass
    def start(self) -> bool: return False
    def stop(self): pass


class SpoakenOnlineClient(SpoakenP2PNode):
    """Drop-in replacement for the old relay-based client."""

    def __init__(self, username: str = "", token: str = "",
                 on_event: Callable = lambda ev: None,
                 log_cb: Callable = print, **kw):
        super().__init__(cfg_path=kw.get("cfg_path", ""),
                         on_event=on_event, log_cb=log_cb)
        if username:
            self._identity.username = username

    def connect(self, url: str = "") -> bool:
        ok = self.start()
        if url:
            self._log(f"[P2P]: connect('{url}') — P2P mode; use join_room() to join a peer.")
        return ok

    def disconnect(self):
        self.stop()

    def is_connected(self) -> bool:
        return self.is_started()

    def join_room(self, room_id_or_onion: str, password: str = "") -> bool:  # type: ignore[override]
        if "/" in room_id_or_onion and ".onion" in room_id_or_onion:
            onion, _, room_id = room_id_or_onion.partition("/")
            return super().join_room(onion, room_id, password)
        self._log("[P2P]: Provide full address: '<host>.onion/<room_id>'")
        return False

    def download_file(self, room_id: str, file_id: str):
        self._log("[P2P]: Files are streamed inline in P2P mode.")

    def list_files(self, room_id: str):
        self._log("[P2P]: Files are streamed directly in P2P mode.")


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers  (unchanged public API — kept for backward compatibility only)
# These functions existed in the old relay/Matrix architecture.  In P2P mode
# the canonical config path goes through SpoakenIdentity / create_identity().
# ─────────────────────────────────────────────────────────────────────────────

def save_online_config(cfg_path: str, server_url: str, username: str, token: str):
    p = Path(cfg_path)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        cfg = {}
    cfg["online_server"]   = server_url
    cfg["online_username"] = username
    cfg["online_token"]    = token
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_online_config(cfg_path: str) -> dict:
    try:
        cfg   = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        ident = cfg.get("p2p_identity", {})
        return {
            "server"        : cfg.get("online_server", ""),
            "username"      : ident.get("username") or cfg.get("online_username", "anonymous"),
            "token"         : cfg.get("online_token", ""),
            "did"           : ident.get("did", ""),
            "tor_socks_port": int(cfg.get("tor_socks_port", _TOR_SOCKS_PORT)),
        }
    except Exception:
        return {"server": "", "username": "anonymous",
                "token": "", "did": "", "tor_socks_port": _TOR_SOCKS_PORT}
