"""
spoaken_commands.py
───────────────────
Command registry and parser for Spoaken v2.

All user-facing commands — whether typed into the sidebar prompt or spoken
aloud — pass through a single CommandParser instance.  Adding a new command
is one _register() call.

Usage pattern
─────────────
    In controller.set_objects():
        from spoaken_commands import CommandParser
        self._cmd_parser = CommandParser(self)

    In any entry point (sidebar, voice stream):
        handled, output = self._cmd_parser.parse(raw_text)
        if handled and output:
            self.view.thread_safety_console(output)

Invocation styles accepted for every command
────────────────────────────────────────────
  Full form:   spoaken.translate(french)
  Short form:  translate french
  Bare word:   help
  Aliases:     /help  :help  !help

Command catalogue
─────────────────
  ── Recording ────────────────────────────────────────────────────────────
  record / start             — start recording
  stop                       — stop recording

  ── Transcript ───────────────────────────────────────────────────────────
  copy                       — copy transcript to clipboard
  clear                      — wipe transcript, logs, and memory
  polish                     — run grammar-correction pass now
  translate <lang | off>     — enable / disable live translation
  summarize                  — summarise current transcript (LLM / extractive)

  ── Engine control ───────────────────────────────────────────────────────
  noise <on | off>           — toggle noise suppression
  llm <on|off|status|…>      — LLM engine control
  status                     — print current engine / model status

  ── LAN access ───────────────────────────────────────────────────────────
  lan <on | off>             — enable / disable LAN server (toggle button)
  port <on | off>            — alias for lan (legacy)

  ── Chat / rooms ─────────────────────────────────────────────────────────
  chat.connect <host[:port]> [user] [token]
  chat.disconnect
  chat.send <message>        — send a message to the current room
  chat.list                  — list rooms on the connected server
  chat.room                  — show currently joined room
  chat.leave                 — leave the current room
  chat.peers                 — list peers in the current room
  chat.create <n> <pw>       — create a new room

  ── File transfer ────────────────────────────────────────────────────────
  file.send <path>           — send a file to the current room
  file.list                  — list files available in the current room
  file.download <file_id> [dest]
                             — download a file from the current room

  ── Utilities ────────────────────────────────────────────────────────────
  logs                       — open the Logs folder
  update                     — open the Update & Repair window
  graph                      — waveform graph (preview)
  help                       — show this reference
"""

import re
import sys
import threading
from typing import Callable


# ─────────────────────────────────────────────────────────────────────────────
# Internal record type
# ─────────────────────────────────────────────────────────────────────────────

class _Cmd:
    __slots__ = ("name", "handler", "description", "usage", "aliases", "section")

    def __init__(
        self,
        name       : str,
        handler    : Callable,
        description: str,
        usage      : str,
        aliases    : tuple = (),
        section    : str   = "",
    ):
        self.name        = name
        self.handler     = handler
        self.description = description
        self.usage       = usage
        self.aliases     = aliases
        self.section     = section


# ─────────────────────────────────────────────────────────────────────────────
# CommandParser
# ─────────────────────────────────────────────────────────────────────────────

class CommandParser:
    """
    Single command bus for Spoaken.

    Parameters
    ----------
    controller : TranscriptionController — the live controller instance.
                 Must be fully initialised before parse() is called.
    """

    # Prefixes stripped before matching so "spoaken.cmd(arg)" == "cmd arg"
    _SPOAKEN_RE = re.compile(
        r"^(?:spoaken\.)?"              # optional  "spoaken."
        r"([a-z_.]+)"                   # command name (dots allowed: chat.send etc.)
        r"(?:[(\s]\s*([^)]*?)\s*\)?"   # optional (arg) or  arg
        r")?$",
        re.IGNORECASE,
    )

    # Strip leading/trailing punctuation shortcuts   /help  :help  !help
    _PREFIX_RE = re.compile(r"^[/!:]+")

    # Section display order
    _SECTION_ORDER = [
        "Help",
        "Recording",
        "Transcript",
        "Engines",
        "LAN Access",
        "Chat / Rooms",
        "File Transfer",
        "Utilities",
    ]

    def __init__(self, controller):
        self._ctrl    = controller
        self._cmds    : dict[str, _Cmd] = {}
        self._aliases : dict[str, str]  = {}
        self._register_all()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def parse(self, text: str) -> tuple:
        """
        Attempt to match text against the command registry.

        Returns
        -------
        (handled: bool, console_output: str | None)
        """
        raw  = text.strip()
        norm = self._PREFIX_RE.sub("", raw).lower().strip()

        m = self._SPOAKEN_RE.match(norm)
        if not m:
            return False, None

        cmd_name = m.group(1).strip()
        arg      = (m.group(2) or "").strip()

        canonical = self._aliases.get(cmd_name, cmd_name)
        entry     = self._cmds.get(canonical)

        if entry is None:
            return False, None

        try:
            result = entry.handler(arg)
            return True, result
        except Exception as exc:
            return True, f"[Command Error]: {cmd_name} — {exc}"

    def help_text(self) -> str:
        """Return a formatted help string grouped by section in display order."""
        # Bucket commands by section, preserving definition order within each
        buckets: dict[str, list[_Cmd]] = {s: [] for s in self._SECTION_ORDER}
        for cmd in self._cmds.values():
            sec = cmd.section or "Utilities"
            if sec not in buckets:
                buckets[sec] = []
            buckets[sec].append(cmd)

        w = 40   # usage column width

        lines = [
            "══════════════════════════════════════════════════",
            "  SPOAKEN COMMAND REFERENCE",
            "══════════════════════════════════════════════════",
        ]

        for sec in self._SECTION_ORDER:
            cmds = buckets.get(sec, [])
            if not cmds:
                continue
            bar  = "─" * max(0, 46 - len(sec))
            lines.append(f"  ── {sec} {bar}")
            for cmd in sorted(cmds, key=lambda c: c.name):
                alias_str = (
                    f"  [{', '.join(cmd.aliases)}]" if cmd.aliases else ""
                )
                lines.append(f"  {cmd.usage:<{w}}  {cmd.description}{alias_str}")

        lines += [
            "══════════════════════════════════════════════════",
            "  Tip: prefix with 'spoaken.'   e.g.  spoaken.clear()",
            "       or just type the bare command: clear",
            "       or use prefix shortcuts:  /help   !help   :help",
            "══════════════════════════════════════════════════",
        ]
        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────────────
    # Registration
    # ─────────────────────────────────────────────────────────────────────────

    def _register(
        self,
        name       : str,
        handler    : Callable,
        description: str,
        usage      : str,
        aliases    : tuple = (),
        section    : str   = "",
    ):
        cmd = _Cmd(name, handler, description, usage, aliases, section)
        self._cmds[name] = cmd
        for alias in aliases:
            self._aliases[alias] = name

    def _register_all(self):
        c = self._ctrl

        # ── Help ──────────────────────────────────────────────────────────────
        self._register(
            "help",
            lambda _: self.help_text(),
            "Show this command reference",
            "help",
            aliases=("?", "commands", "cmds"),
            section="Help",
        )

        # ── Recording ─────────────────────────────────────────────────────────
        self._register(
            "record",
            lambda _: self._safe_call(c.toggle_recording)
                      if not c.model.is_running else None,
            "Start recording",
            "record",
            aliases=("start", "rec", "listen"),
            section="Recording",
        )

        self._register(
            "stop",
            lambda _: self._safe_call(c.toggle_recording)
                      if c.model.is_running else None,
            "Stop recording",
            "stop",
            aliases=("pause", "end"),
            section="Recording",
        )

        # ── Transcript ────────────────────────────────────────────────────────
        self._register(
            "copy",
            lambda _: self._safe_call(c.copy_transcript),
            "Copy full transcript to clipboard",
            "copy",
            aliases=("clipboard",),
            section="Transcript",
        )

        self._register(
            "clear",
            lambda _: self._safe_call(c.clear_all_logs),
            "Wipe transcript display, data stores, and log files",
            "clear",
            aliases=("wipe", "reset"),
            section="Transcript",
        )

        self._register(
            "polish",
            lambda _: self._safe_call(c.swap_polishing),
            "Run grammar-correction pass on current transcript",
            "polish",
            aliases=("fix", "correct", "grammar"),
            section="Transcript",
        )

        self._register(
            "translate",
            self._cmd_translate,
            "Enable / disable live translation  (e.g. french / off)",
            "translate <lang | off>",
            section="Transcript",
        )

        self._register(
            "summarize",
            self._cmd_summarize,
            "Summarize current transcript  (LLM if available, else extractive)",
            "summarize",
            aliases=("summary", "tldr", "sum"),
            section="Transcript",
        )

        # ── Engines ───────────────────────────────────────────────────────────
        self._register(
            "noise",
            self._cmd_noise,
            "Toggle noise suppression  (on / off)",
            "noise <on | off>",
            aliases=("denoise",),
            section="Engines",
        )

        self._register(
            "llm",
            self._cmd_llm,
            "LLM control: on|off|status|model <n>|translate|summarize|install|pull",
            "llm <on|off|status|model <n>|…>",
            aliases=("ollama",),
            section="Engines",
        )

        self._register(
            "status",
            self._cmd_status,
            "Show current engine / model / connection status",
            "status",
            aliases=("info", "state"),
            section="Engines",
        )

        # ── LAN access ────────────────────────────────────────────────────────
        self._register(
            "lan",
            self._cmd_lan,
            "Enable / disable LAN server  (mirrors the LAN Access button)",
            "lan <on | off>",
            aliases=("port", "server", "host"),
            section="LAN Access",
        )

        # ── Chat / rooms ──────────────────────────────────────────────────────
        self._register(
            "chat.connect",
            self._cmd_chat_connect,
            "Connect to a LAN server",
            "chat.connect <host[:port]> [user] [token]",
            section="Chat / Rooms",
        )

        self._register(
            "chat.disconnect",
            self._cmd_chat_disconnect,
            "Disconnect from the current LAN server",
            "chat.disconnect",
            aliases=("disconnect",),
            section="Chat / Rooms",
        )

        self._register(
            "chat.send",
            self._cmd_chat_send,
            "Send a message to the current room",
            "chat.send <message>",
            section="Chat / Rooms",
        )

        self._register(
            "chat.list",
            self._cmd_chat_list,
            "List rooms on the connected LAN server",
            "chat.list",
            aliases=("rooms",),
            section="Chat / Rooms",
        )

        self._register(
            "chat.room",
            self._cmd_chat_room,
            "Show the currently joined room",
            "chat.room",
            aliases=("room",),
            section="Chat / Rooms",
        )

        self._register(
            "chat.leave",
            self._cmd_chat_leave,
            "Leave the current room",
            "chat.leave",
            aliases=("leave",),
            section="Chat / Rooms",
        )

        self._register(
            "chat.peers",
            self._cmd_chat_peers,
            "List peers connected in the current room",
            "chat.peers",
            aliases=("peers", "who"),
            section="Chat / Rooms",
        )

        self._register(
            "chat.create",
            self._cmd_chat_create,
            "Create a new room  (name  password  [topic])",
            "chat.create <name> <password> [topic]",
            aliases=("create",),
            section="Chat / Rooms",
        )

        # ── File transfer ─────────────────────────────────────────────────────
        self._register(
            "file.send",
            self._cmd_file_send,
            "Send a file to the current room  (LAN and P2P)",
            "file.send <path>",
            aliases=("send",),
            section="File Transfer",
        )

        self._register(
            "file.list",
            self._cmd_file_list,
            "List files stored in the current room  (LAN only)",
            "file.list",
            aliases=("files",),
            section="File Transfer",
        )

        self._register(
            "file.download",
            self._cmd_file_download,
            "Download a file by ID from the current room  (LAN only)",
            "file.download <file_id> [dest_path]",
            aliases=("download", "dl"),
            section="File Transfer",
        )

        # ── Utilities ─────────────────────────────────────────────────────────
        self._register(
            "logs",
            lambda _: self._safe_call(c.open_logs),
            "Open the Logs folder in the system file manager",
            "logs",
            aliases=("log",),
            section="Utilities",
        )

        self._register(
            "update",
            self._cmd_update,
            "Open the Spoaken Update & Repair window",
            "update",
            aliases=("upgrade", "repair"),
            section="Utilities",
        )

        self._register(
            "graph",
            lambda _: "[Graph]: waveform graph is in development — coming soon",
            "Waveform analysis graph  (preview — not yet available)",
            "graph",
            section="Utilities",
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Recording / Transcript implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_translate(self, arg: str) -> str:
        c = self._ctrl
        if not arg:
            current = c._translate_lang or "off"
            return f"[Translate]: currently → {current}  |  usage: translate <lang | off>"
        if arg.lower() in ("off", "stop", "none", "disable"):
            c._translate_lang = None
            return "[Translate]: translation disabled"
        c._translate_lang = arg
        return f"[Translate]: live translation → {arg}"

    def _cmd_summarize(self, arg: str) -> str:
        c = self._ctrl
        try:
            c.view.thread_safety_console("[Summarize]: running …")
            threading.Thread(target=c.run_summarize, daemon=True).start()
            return "[Summarize]: summary requested — see console"
        except Exception as exc:
            return f"[Summarize Error]: {exc}"

    # ─────────────────────────────────────────────────────────────────────────
    # Engine implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_noise(self, arg: str) -> str:
        c   = self._ctrl
        on  = arg.lower() in ("on", "1", "yes", "true", "enable")
        off = arg.lower() in ("off", "0", "no", "false", "disable")
        if not on and not off:
            return "[Noise]: usage: noise on  |  noise off"
        c.toggle_noise_suppression(on)
        try:
            c.view.after(0, lambda: c.view.btn_noise.configure(
                text=f"Noise: {'ON' if on else 'OFF'}",
                fg_color="#0d4040" if on else "#1a2640",
                hover_color="#156060" if on else "#253560",
            ))
        except Exception:
            pass
        return f"[Noise]: suppression {'ON' if on else 'OFF'}"

    def _cmd_status(self, _: str) -> str:
        c = self._ctrl
        try:
            vosk_ok    = c.model.small_model is not None
            whisper_ok = c.model.whisper_model is not None
            grammar_ok = c.model.tool is not None
            recording  = c.model.is_running
            writing    = c.writing_status
            port_open  = bool(c._chat_server and c._chat_server.is_open())
            lang       = c._translate_lang or "off"
            peers      = c._chat_server.peer_count() if c._chat_server else 0

            view        = getattr(c, "view", None)
            lan_conn    = bool(view and getattr(view, "_lan_client", None)
                               and view._lan_client.is_connected())
            lan_room    = (getattr(view, "_lan_current_room", None) or "none") if view else "n/a"
            p2p_mode    = bool(view and getattr(view, "_p2p_mode", False))
            p2p_up      = bool(view and getattr(view, "_p2p_node", None)
                               and view._p2p_node.is_started())

            lines = [
                "─────────────────────────── STATUS ─",
                f"  Recording   : {'● ON' if recording  else '○ OFF'}",
                f"  Writer      : {'● ON' if writing    else '○ OFF'}",
                f"  Vosk model  : {'✔' if vosk_ok    else '✗  not loaded'}",
                f"  Whisper     : {'✔' if whisper_ok else '✗  not loaded'}",
                f"  Grammar T5  : {'✔' if grammar_ok else '✗  not loaded'}",
                f"  LAN server  : {'● open' if port_open else '○ closed'}  ({peers} peer{'s' if peers != 1 else ''})",
                f"  LAN client  : {'● connected' if lan_conn else '○ disconnected'}  room: {lan_room[:20]}",
                f"  P2P mode    : {'● active' if p2p_mode else '○ inactive'}  node: {'up' if p2p_up else 'down'}",
                f"  Translation : {lang}",
                "────────────────────────────────────",
            ]
            return "\n".join(lines)
        except Exception as exc:
            return f"[Status Error]: {exc}"

    def _cmd_llm(self, arg: str) -> str:
        c   = self._ctrl
        arg = arg.strip().lower()

        if not arg or arg == "status":
            enabled = getattr(c, "_llm_enabled", False)
            mode    = getattr(c, "_llm_mode",    None) or "off"
            model   = getattr(c, "_llm_model",   None) or "auto"
            try:
                from spoaken_llm import is_ollama_running, list_ollama_models
                online  = is_ollama_running()
                models  = list_ollama_models()
                model_s = f"{len(models)} model(s) available" if online else "offline"
            except Exception:
                online  = False
                model_s = "ollama package not installed"
            return "\n".join([
                "──────────────── LLM STATUS ─────",
                f"  Enabled  : {'✔' if enabled else '✗'}",
                f"  Mode     : {mode}",
                f"  Model    : {model}",
                f"  Ollama   : {'● running' if online else '○ offline'}",
                f"  Models   : {model_s}",
                "─────────────────────────────────",
                "  Commands: llm on | off | translate | summarize",
                "            llm model <n> | install | pull | status",
            ])

        if arg in ("on", "enable"):
            c.set_llm_enabled(True)
            try:
                c.view.after(0, lambda: c.view._toggle_llm()
                             if not c.view._llm_enabled else None)
            except Exception:
                pass
            return "[LLM]: enabled"

        if arg in ("off", "disable"):
            c.set_llm_enabled(False)
            return "[LLM]: disabled"

        if arg == "translate":
            c.set_llm_mode("translate")
            return "[LLM]: mode → translate"

        if arg in ("summarize", "summary"):
            c.set_llm_mode("summarize")
            return "[LLM]: mode → summarize"

        if arg.startswith("model "):
            model_name = arg[6:].strip()
            c.set_llm_model(model_name)
            return f"[LLM]: model set → {model_name}"

        if arg == "install":
            try:
                from spoaken_llm import ensure_ollama_pkg
                ok = ensure_ollama_pkg(log_fn=c.view.thread_safety_console)
                return f"[LLM]: ollama package {'installed ✔' if ok else 'install failed ✗'}"
            except Exception as exc:
                return f"[LLM Install Error]: {exc}"

        if arg == "pull":
            return (
                "[LLM]: Pull models with Ollama CLI:\n"
                "  ollama pull mistral-small:24b\n"
                "  ollama pull deepseek-r1:14b\n"
                "  ollama pull huihui_ai/qwen2.5-1m-abliterated:14b"
            )

        return (f"[LLM]: unknown arg '{arg}'  |  "
                "usage: llm on|off|status|translate|summarize|model <n>|install|pull")

    # ─────────────────────────────────────────────────────────────────────────
    # LAN access
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_lan(self, arg: str) -> str:
        """
        Enable or disable the LAN server and sync the GUI "LAN Access" button.
        Mirrors exactly what clicking the button does so the UI stays consistent.
        """
        c       = self._ctrl
        on      = arg.lower() in ("on",  "1", "yes", "true",  "open",  "enable")
        off     = arg.lower() in ("off", "0", "no",  "false", "close", "disable")
        is_open = bool(c._chat_server and c._chat_server.is_open())

        if not on and not off:
            state = "On (open)" if is_open else "Off (closed)"
            return f"[LAN]: access is currently {state}  |  usage: lan on  |  lan off"

        if on and not is_open:
            # Delegate to the GUI toggle so update_chat_port_btn() fires correctly
            try:
                c.view.after(0, c.view._on_toggle_port)
                return "[LAN]: access enabling …"
            except Exception:
                c.toggle_chat_port()
                return "[LAN]: access enabled"

        if off and is_open:
            try:
                c.view.after(0, c.view._on_toggle_port)
                return "[LAN]: access disabling …"
            except Exception:
                c.toggle_chat_port()
                return "[LAN]: access disabled"

        return "[LAN]: already " + ("open" if is_open else "closed")

    # ─────────────────────────────────────────────────────────────────────────
    # Chat / room implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_chat_connect(self, arg: str) -> str:
        c = self._ctrl
        try:
            parts = arg.split()
            if not parts:
                return "[Chat]: Usage: chat.connect <host[:port]> [user] [token]"
            host_port = parts[0]
            host      = host_port.split(":")[0]
            port      = int(host_port.split(":")[1]) if ":" in host_port else 55300
            user      = parts[1] if len(parts) > 1 else "spoaken"
            token     = parts[2] if len(parts) > 2 else "spoaken"
            view      = c.view
            view.after(0, lambda: view._lan_host_entry.delete(0, "end"))
            view.after(0, lambda: view._lan_host_entry.insert(0, host))
            view.after(0, lambda: view._lan_port_entry.delete(0, "end"))
            view.after(0, lambda: view._lan_port_entry.insert(0, str(port)))
            view.after(0, lambda: view._lan_user_entry.delete(0, "end"))
            view.after(0, lambda: view._lan_user_entry.insert(0, user))
            view.after(0, lambda: view._lan_token_entry.delete(0, "end"))
            view.after(0, lambda: view._lan_token_entry.insert(0, token))
            view.after(100, view._on_lan_connect)
            return f"[Chat]: connecting to {host}:{port} as {user} …"
        except Exception as exc:
            return f"[Chat Connect Error]: {exc}"

    def _cmd_chat_disconnect(self, _: str) -> str:
        c = self._ctrl
        try:
            c.view.after(0, c.view._on_lan_disconnect)
            return "[Chat]: disconnecting …"
        except Exception as exc:
            return f"[Chat Error]: {exc}"

    def _cmd_chat_send(self, arg: str) -> str:
        c = self._ctrl
        try:
            view    = c.view
            client  = getattr(view, "_lan_client", None)
            room_id = getattr(view, "_lan_current_room", None)
            if not client or not client.is_connected():
                return "[Chat]: Not connected to a server."
            if not room_id:
                return "[Chat]: Not in a room — join one via the sidebar first."
            if not arg:
                return "[Chat]: Usage: chat.send <message>"
            client.send_message(room_id, arg)
            view.after(0, lambda: view.chat_receive(f"[Me]: {arg}"))
            return f"[Chat]: sent → {arg[:60]}"
        except Exception as exc:
            return f"[Chat Error]: {exc}"

    def _cmd_chat_list(self, _: str) -> str:
        c = self._ctrl
        try:
            view   = c.view
            client = getattr(view, "_lan_client", None)
            if not client or not client.is_connected():
                return "[Chat]: Not connected to a LAN server."
            client.list_rooms()
            return "[Chat]: room list requested — results in sidebar"
        except Exception as exc:
            return f"[Chat Error]: {exc}"

    def _cmd_chat_room(self, _: str) -> str:
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[Chat]: view not available"
        room_id   = getattr(view, "_lan_current_room", None) or \
                    getattr(view, "_p2p_current_room", None)
        room_name = view._room_var.get() if hasattr(view, "_room_var") else "?"
        p2p       = getattr(view, "_p2p_mode", False)
        if not room_id:
            return "[Chat]: Not currently in any room."
        return f"[Chat]: room — {room_name}  [{'P2P' if p2p else 'LAN'}]  id: {room_id}"

    def _cmd_chat_leave(self, _: str) -> str:
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[Chat]: view not available"
        try:
            if getattr(view, "_p2p_mode", False):
                room_id = getattr(view, "_p2p_current_room", None)
                if room_id and view._p2p_node:
                    view._p2p_node.leave_room(room_id)
                    view._p2p_current_room = None
                    return "[Chat]: left P2P room"
                return "[Chat]: not in a P2P room"
            else:
                room_id = getattr(view, "_lan_current_room", None)
                client  = getattr(view, "_lan_client",       None)
                if room_id and client and client.is_connected():
                    client.leave_room(room_id)
                    view._lan_current_room = None
                    return f"[Chat]: left room {room_id[:16]}"
                return "[Chat]: not in a room"
        except Exception as exc:
            return f"[Chat Leave Error]: {exc}"

    def _cmd_chat_peers(self, _: str) -> str:
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[Chat]: view not available"
        try:
            if getattr(view, "_p2p_mode", False):
                room_id = getattr(view, "_p2p_current_room", None)
                if not room_id or not view._p2p_node:
                    return "[Chat]: not in a P2P room"
                peers = view._p2p_node.list_peers(room_id)
                if not peers:
                    return "[Chat]: no peers listed  (you may be alone)"
                lines = ["[Chat]: peers —"]
                for p in peers:
                    lines.append(f"  • {p.get('username','?')}  [{p.get('did','')[:20]}]")
                return "\n".join(lines)
            return "[Chat]: peer list not available in LAN mode — see sidebar"
        except Exception as exc:
            return f"[Chat Peers Error]: {exc}"

    def _cmd_chat_create(self, arg: str) -> str:
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[Chat]: view not available"
        parts = arg.split(None, 2)
        if len(parts) < 2:
            return "[Chat]: Usage: chat.create <name> <password> [topic]"
        name, pw = parts[0], parts[1]
        topic    = parts[2] if len(parts) > 2 else ""
        try:
            if getattr(view, "_p2p_mode", False):
                if not view._p2p_node or not view._p2p_node.is_started():
                    return "[Chat]: Start P2P node first."
                def _do():
                    rid = view._p2p_node.create_room(
                        name, password=pw, public=True, topic=topic)
                    if rid:
                        view._p2p_rooms_cache[rid] = name
                        view._p2p_current_room = rid
                        view.after(0, lambda: view._room_var.set(name))
                threading.Thread(target=_do, daemon=True).start()
                return f"[Chat]: creating P2P room '{name}' …"
            else:
                client = getattr(view, "_lan_client", None)
                if not client or not client.is_connected():
                    return "[Chat]: Connect to a LAN server first."
                client.create_room(name, pw, public=True, topic=topic)
                return f"[Chat]: create request sent for room '{name}'"
        except Exception as exc:
            return f"[Chat Create Error]: {exc}"

    # ─────────────────────────────────────────────────────────────────────────
    # File transfer implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_file_send(self, arg: str) -> str:
        import pathlib as _pl
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[File]: view not available"

        path = arg.strip().strip("\"'")
        if not path:
            return "[File]: Usage: file.send <path>"

        p = _pl.Path(path)
        if not p.exists():
            return f"[File]: file not found — {path}"
        if p.stat().st_size > 50 * 1024 * 1024:
            return "[File]: too large (max 50 MB)"
        kb = p.stat().st_size // 1024

        def _bg():
            try:
                if getattr(view, "_p2p_mode", False):
                    room_id = getattr(view, "_p2p_current_room", None)
                    node    = getattr(view, "_p2p_node",          None)
                    if not room_id or not node:
                        view.after(0, lambda: view.thread_safety_console(
                            "[File]: join a P2P room first"))
                        return
                    node.send_file(room_id, str(p))
                else:
                    client  = getattr(view, "_lan_client",       None)
                    room_id = getattr(view, "_lan_current_room", None)
                    if not client or not client.is_connected() or not room_id:
                        view.after(0, lambda: view.thread_safety_console(
                            "[File]: connect to a room first"))
                        return
                    client.send_file(room_id, str(p))
                view.after(0, lambda n=p.name, k=kb: view.thread_safety_console(
                    f"[File]: sent '{n}'  ({k} KB)"))
            except Exception as exc:
                view.after(0, lambda e=exc: view.thread_safety_console(
                    f"[File Error]: {e}"))

        threading.Thread(target=_bg, daemon=True).start()
        return f"[File]: sending '{p.name}'  ({kb} KB) …"

    def _cmd_file_list(self, _: str) -> str:
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[File]: view not available"
        if getattr(view, "_p2p_mode", False):
            return "[File]: P2P files stream inline — no server-side file list"
        client  = getattr(view, "_lan_client",       None)
        room_id = getattr(view, "_lan_current_room", None)
        if not client or not client.is_connected():
            return "[File]: not connected to a LAN server"
        if not room_id:
            return "[File]: not in a room — join one first"
        client.list_files(room_id)
        return "[File]: file list requested — results will appear in the sidebar"

    def _cmd_file_download(self, arg: str) -> str:
        c    = self._ctrl
        view = getattr(c, "view", None)
        if not view:
            return "[File]: view not available"
        if getattr(view, "_p2p_mode", False):
            return "[File]: P2P files stream automatically — no download command needed"
        parts = arg.split(None, 1)
        if not parts:
            return "[File]: Usage: file.download <file_id> [dest_path]"
        file_id   = parts[0].strip()
        dest_path = parts[1].strip().strip("\"'") if len(parts) > 1 else None
        client  = getattr(view, "_lan_client",       None)
        room_id = getattr(view, "_lan_current_room", None)
        if not client or not client.is_connected():
            return "[File]: not connected to a LAN server"
        if not room_id:
            return "[File]: not in a room"
        client.download_file(room_id, file_id, dest_path=dest_path)
        dest_str = dest_path or "(default downloads folder)"
        return f"[File]: download requested — {file_id[:16]} → {dest_str}"

    # ─────────────────────────────────────────────────────────────────────────
    # Utility implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_update(self, _: str) -> str:
        try:
            from spoaken_update import SpoakenUpdater
            c = self._ctrl
            c.view.after(0, lambda: SpoakenUpdater(c.view))
            return "[Update]: opening update window …"
        except Exception as exc:
            return f"[Update Error]: could not open updater — {exc}"

    # ─────────────────────────────────────────────────────────────────────────
    # Shared helper
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_call(fn):
        try:
            fn()
        except Exception as exc:
            print(f"[Command Error]: {exc}", file=sys.stderr)
        return None
