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
  help                          — list every available command
  translate  <lang | off>       — enable / disable live translation
  clear                         — wipe transcript, logs, and memory
  polish                        — run grammar correction pass now
  noise  <on | off>             — toggle noise suppression
  port   <on | off>             — toggle chat server port
  record / start                — start recording
  stop                          — stop recording
  copy                          — copy transcript to clipboard
  status                        — print current engine status
  logs                          — open log folder
  graph                         — (feature preview) waveform graph
"""

import re
import sys
from typing import Callable

# ─────────────────────────────────────────────────────────────────────────────
# Internal record type
# ─────────────────────────────────────────────────────────────────────────────

class _Cmd:
    __slots__ = ("name", "handler", "description", "usage", "aliases")

    def __init__(
        self,
        name       : str,
        handler    : Callable,
        description: str,
        usage      : str,
        aliases    : tuple = (),
    ):
        self.name        = name
        self.handler     = handler
        self.description = description
        self.usage       = usage
        self.aliases     = aliases


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
        r"^(?:spoaken\.)?"          # optional  "spoaken."
        r"([a-z_]+)"                # command name
        r"(?:[(\s]\s*([^)]*?)\s*\)?"   # optional (arg) or  arg
        r")?$",
        re.IGNORECASE,
    )

    # Strip leading/trailing punctuation shortcuts   /help  :help  !help
    _PREFIX_RE = re.compile(r"^[/!:]+")

    def __init__(self, controller):
        self._ctrl    = controller
        self._cmds    : dict[str, _Cmd] = {}   # name → _Cmd
        self._aliases : dict[str, str]  = {}   # alias → canonical name
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

        # Resolve alias
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
        """Return a formatted help string listing every registered command."""
        lines = [
            "─────────────────────────────────────────────────",
            "  SPOAKEN COMMANDS",
            "─────────────────────────────────────────────────",
        ]
        for name, cmd in sorted(self._cmds.items()):
            alias_str = (
                f"  (alias: {', '.join(cmd.aliases)})" if cmd.aliases else ""
            )
            lines.append(f"  {cmd.usage:<36}  {cmd.description}{alias_str}")
        lines += [
            "─────────────────────────────────────────────────",
            "  Tip: prefix with 'spoaken.'  e.g. spoaken.clear()",
            "       or just type the bare command: clear",
            "─────────────────────────────────────────────────",
        ]
        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────────────
    # Registration helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _register(
        self,
        name       : str,
        handler    : Callable,
        description: str,
        usage      : str,
        aliases    : tuple = (),
    ):
        cmd = _Cmd(name, handler, description, usage, aliases)
        self._cmds[name] = cmd
        for alias in aliases:
            self._aliases[alias] = name

    def _register_all(self):
        c = self._ctrl   # shorthand

        self._register(
            "help",
            lambda _: self.help_text(),
            "List all available commands",
            "help",
            aliases=("?", "commands", "cmds"),
        )

        self._register(
            "translate",
            self._cmd_translate,
            "Enable / disable live translation  (e.g. french / off)",
            "translate <lang | off>",
        )

        self._register(
            "clear",
            lambda _: self._safe_call(c.clear_all_logs),
            "Wipe transcript display, data stores, and log files",
            "clear",
            aliases=("wipe", "reset"),
        )

        self._register(
            "polish",
            lambda _: self._safe_call(c.swap_polishing),
            "Run grammar-correction pass on current transcript",
            "polish",
            aliases=("fix", "correct", "grammar"),
        )

        self._register(
            "noise",
            self._cmd_noise,
            "Toggle noise suppression  (on / off)",
            "noise <on | off>",
            aliases=("denoise",),
        )

        self._register(
            "port",
            self._cmd_port,
            "Toggle chat server TCP port  (on / off)",
            "port <on | off>",
            aliases=("chat",),
        )

        self._register(
            "record",
            lambda _: self._safe_call(c.toggle_recording)
                      if not c.model.is_running else None,
            "Start recording",
            "record",
            aliases=("start", "rec", "listen"),
        )

        self._register(
            "stop",
            lambda _: self._safe_call(c.toggle_recording)
                      if c.model.is_running else None,
            "Stop recording",
            "stop",
            aliases=("pause", "end"),
        )

        self._register(
            "copy",
            lambda _: self._safe_call(c.copy_transcript),
            "Copy full transcript to clipboard",
            "copy",
            aliases=("clipboard",),
        )

        self._register(
            "logs",
            lambda _: self._safe_call(c.open_logs),
            "Open the Logs folder",
            "logs",
            aliases=("log", "files"),
        )

        self._register(
            "status",
            self._cmd_status,
            "Show current engine / model status",
            "status",
            aliases=("info", "state"),
        )

        self._register(
            "update",
            self._cmd_update,
            "Open the Spoaken Update & Repair window",
            "update",
            aliases=("upgrade", "repair"),
        )

        self._register(
            "graph",
            lambda _: "[Graph]: waveform graph feature is in development — coming soon",
            "Show waveform analysis graph  (preview — not yet available)",
            "graph",
        )

        self._register(
            "summarize",
            self._cmd_summarize,
            "Summarize the current transcript (LLM if available, else extractive)",
            "summarize",
            aliases=("summary", "tldr", "sum"),
        )

        self._register(
            "llm",
            self._cmd_llm,
            "LLM control: llm on|off|status|model <name>|translate|summarize",
            "llm <on|off|status|model <name>|translate|summarize>",
            aliases=("ollama",),
        )

        self._register(
            "chat.list",
            self._cmd_chat_list,
            "List rooms on the connected LAN server",
            "chat.list",
            aliases=("rooms",),
        )

        self._register(
            "chat.send",
            self._cmd_chat_send,
            "Send a message to the current LAN room",
            "chat.send <message>",
        )

        self._register(
            "chat.connect",
            self._cmd_chat_connect,
            "Connect to a LAN server  (host:port  user  token)",
            "chat.connect <host[:port]> [user] [token]",
        )

        self._register(
            "chat.disconnect",
            self._cmd_chat_disconnect,
            "Disconnect from the current LAN server",
            "chat.disconnect",
            aliases=("disconnect",),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Individual command implementations
    # ─────────────────────────────────────────────────────────────────────────

    def _cmd_translate(self, arg: str) -> str:
        c = self._ctrl
        if not arg:
            current = c._translate_lang or "off"
            return f"[Translate]: currently → {current}  |  usage: translate <lang | off>"

        if arg.lower() in ("off", "stop", "none", "disable"):
            c._translate_lang = None
            return "[Translate]: translation disabled"
        else:
            c._translate_lang = arg
            return f"[Translate]: live translation → {arg}"

    def _cmd_noise(self, arg: str) -> str:
        c   = self._ctrl
        on  = arg.lower() in ("on", "1", "yes", "true", "enable")
        off = arg.lower() in ("off", "0", "no", "false", "disable")
        if not on and not off:
            return "[Noise]: usage: noise on  |  noise off"
        c.toggle_noise_suppression(on)
        # Sync GUI checkbox / button if it exists
        try:
            c.view.after(0, lambda: c.view.btn_noise.configure(
                text=f"Noise: {'ON' if on else 'OFF'}",
                fg_color="#0d4040" if on else "#1a2640",
                hover_color="#156060" if on else "#253560",
            ))
        except Exception:
            pass
        return f"[Noise]: suppression {'ON' if on else 'OFF'}"

    def _cmd_port(self, arg: str) -> str:
        c   = self._ctrl
        on  = arg.lower() in ("on", "1", "yes", "true", "open", "enable")
        off = arg.lower() in ("off", "0", "no", "false", "close", "disable")

        if not on and not off:
            state = "open" if (c._chat_server and c._chat_server.is_open()) else "closed"
            return f"[Port]: chat server port is currently {state}  |  usage: port on  |  port off"

        if on and c._chat_server and not c._chat_server.is_open():
            c.toggle_chat_port()
            return "[Port]: chat server opened"
        elif off and c._chat_server and c._chat_server.is_open():
            c.toggle_chat_port()
            return "[Port]: chat server closed"
        else:
            state = "open" if (c._chat_server and c._chat_server.is_open()) else "closed"
            return f"[Port]: already {state}"

    def _cmd_status(self, _: str) -> str:
        c = self._ctrl
        try:
            vosk_ok    = c.model.small_model is not None
            whisper_ok = c.model.whisper_model is not None
            giga_ok    = c.model.giga_model is not None
            grammar_ok = c.model.tool is not None
            recording  = c.model.is_running
            writing    = c.writing_status
            port_open  = bool(c._chat_server and c._chat_server.is_open())
            lang       = c._translate_lang or "off"
            peers      = c._chat_server.peer_count() if c._chat_server else 0

            lines = [
                "─────────────────────────── STATUS ─",
                f"  Recording   : {'● ON' if recording  else '○ OFF'}",
                f"  Writer      : {'● ON' if writing    else '○ OFF'}",
                f"  Vosk model  : {'✔' if vosk_ok    else '✗  not loaded'}",
                f"  Whisper     : {'✔' if whisper_ok else '✗  not loaded'}",
                f"  Giga Vosk   : {'✔' if giga_ok    else '✗  not loaded'}",
                f"  Grammar T5  : {'✔' if grammar_ok else '✗  not loaded'}",
                f"  Chat port   : {'● open' if port_open else '○ closed'}  ({peers} peer{'s' if peers != 1 else ''})",
                f"  Translation : {lang}",
                "────────────────────────────────────",
            ]
            return "\n".join(lines)
        except Exception as exc:
            return f"[Status Error]: {exc}"

    def _cmd_update(self, _: str) -> str:
        """Open the Spoaken Update window."""
        try:
            from spoaken_update import SpoakenUpdater
            c = self._ctrl
            c.view.after(0, lambda: SpoakenUpdater(c.view))
            return "[Update]: opening update window …"
        except Exception as exc:
            return f"[Update Error]: could not open updater — {exc}"

    def _cmd_summarize(self, arg: str) -> str:
        """Summarize the current transcript."""
        c = self._ctrl
        try:
            c.view.thread_safety_console("[Summarize]: running …")
            import threading as _t
            _t.Thread(target=c.run_summarize, daemon=True).start()
            return "[Summarize]: summary requested — see console"
        except Exception as exc:
            return f"[Summarize Error]: {exc}"

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
            lines = [
                "──────────────── LLM STATUS ─────",
                f"  Enabled  : {'✔' if enabled else '✗'}",
                f"  Mode     : {mode}",
                f"  Model    : {model}",
                f"  Ollama   : {'● running' if online else '○ offline'}",
                f"  Models   : {model_s}",
                "─────────────────────────────────",
                "  Commands: llm on | llm off | llm translate | llm summarize",
                "            llm model <name>",
            ]
            return "\n".join(lines)

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

        if arg in ("translate",):
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

        return f"[LLM]: unknown arg '{arg}'  |  usage: llm on|off|status|translate|summarize|model <name>|install|pull"

    def _cmd_chat_list(self, _: str) -> str:
        c = self._ctrl
        try:
            view = c.view
            client = getattr(view, "_lan_client", None)
            if not client or not client.is_connected():
                return "[Chat]: Not connected to a LAN server. Use chat.connect first."
            client.list_rooms()
            return "[Chat]: room list requested — see sidebar"
        except Exception as exc:
            return f"[Chat Error]: {exc}"

    def _cmd_chat_send(self, arg: str) -> str:
        c = self._ctrl
        try:
            view    = c.view
            client  = getattr(view, "_lan_client", None)
            room_id = getattr(view, "_lan_current_room", None)
            if not client or not client.is_connected():
                return "[Chat]: Not connected."
            if not room_id:
                return "[Chat]: Not in a room. Join a room first via the sidebar."
            if not arg:
                return "[Chat]: Usage: chat.send <message>"
            client.send_message(room_id, arg)
            view.after(0, lambda: view.chat_receive(f"[Me]: {arg}"))
            return f"[Chat]: sent → {arg[:40]}"
        except Exception as exc:
            return f"[Chat Error]: {exc}"

    def _cmd_chat_connect(self, arg: str) -> str:
        """Programmatically connect: chat.connect host[:port] [user] [token]"""
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

            view = c.view
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

    # ─────────────────────────────────────────────────────────────────────────
    # Utility
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_call(fn):
        """Call fn() in the controller's thread context, swallow exceptions."""
        try:
            fn()
        except Exception as exc:
            print(f"[Command Error]: {exc}", file=sys.stderr)
        return None
        
        
        
        
        
