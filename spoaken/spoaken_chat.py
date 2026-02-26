"""
spoaken_chat.py
───────────────
Compatibility shim that re-exports the chat classes used by spoaken_control.py
and spoaken_commands.py.

  ChatServer   — LAN WebSocket server (wraps SpoakenLANServer)
  SSEServer    — HTTP Server-Sent Events server for Android / browser clients
  SpoakenLANServer  — full LAN server (room management, file transfer, auth)
  SpoakenLANClient  — LAN client (connect to a LAN server)
  SpoakenOnlineRelay  — online relay server (internet chat rooms)
  SpoakenOnlineClient — online client (connect to relay)

Import this module instead of importing spoaken_chat_lan or
spoaken_chat_online directly — it keeps the rest of the codebase clean and
makes it easy to swap implementations later.
"""

# ── LAN chat (local network, WebSocket + UDP discovery) ──────────────────────
from spoaken_chat_lan import (
    ChatServer,
    SSEServer,
    SpoakenLANServer,
    SpoakenLANClient,
    LANServerBeacon,
    LANServerScanner,
    LANServerEntry,
    SpoakenRoom,
    SpoakenUser,
    ChatDB,
    ChatEvent,
    FileTransfer,
)

# ── Online chat (internet relay, WebSocket) ───────────────────────────────────
from spoaken_chat_online import (
    SpoakenOnlineRelay,
    SpoakenOnlineClient,
    OnlineRoom,
    OnlineUser,
    FileRelay,
)

__all__ = [
    # Legacy / control.py API
    "ChatServer",
    "SSEServer",
    # LAN
    "SpoakenLANServer",
    "SpoakenLANClient",
    "LANServerBeacon",
    "LANServerScanner",
    "LANServerEntry",
    "SpoakenRoom",
    "SpoakenUser",
    "ChatDB",
    "ChatEvent",
    "FileTransfer",
    # Online
    "SpoakenOnlineRelay",
    "SpoakenOnlineClient",
    "OnlineRoom",
    "OnlineUser",
    "FileRelay",
]
