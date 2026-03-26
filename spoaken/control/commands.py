"""
spoaken_commands.py (PRODUCTION)
Fixes: Command random triggers, model controls, cache clearing.

Fixes applied:
  • _cmd_status: reads runtime _sc.VOSK_ACTIVE / _sc.WHISPER_ACTIVE and the
    live ENGINE_MODE from spoaken_connect instead of the startup config value,
    so status correctly reflects changes made via 'engine', 'vosk', 'whisper'
    commands during a session.
"""

import re
import sys
import gc
from typing import Callable


class _Cmd:
    __slots__ = ("name", "handler", "description", "usage", "aliases", "section")

    def __init__(self, name, handler, description, usage, aliases=(), section=""):
        self.name        = name
        self.handler     = handler
        self.description = description
        self.usage       = usage
        self.aliases     = aliases
        self.section     = section


class CommandParser:
    def __init__(self, controller):
        self._ctrl = controller
        self._cmds: dict  = {}
        self._aliases: dict = {}
        self._register_all()

    # ── Parsing ───────────────────────────────────────────────────────────────

    def parse(self, text: str) -> bool:
        """Parse command. Confidence threshold prevents random triggers."""
        if not text or not isinstance(text, str):
            return False

        text = text.strip()

        if len(text) < 3 or not self._looks_like_command(text):
            return False

        for pattern in [
            r"^spoaken\.(\w+(?:\.\w+)?)\s*\((.*?)\)$",  # spoaken.cmd(args)
            r"^[/!:](\w+(?:\.\w+)?)\s*(.*)$",            # /cmd args
            r"^(\w+(?:\.\w+)?)\s+(.+)$",                 # cmd args (requires arg)
            r"^(\w+(?:\.\w+)?)$",                         # bare cmd
        ]:
            m = re.match(pattern, text, re.IGNORECASE)
            if m:
                return self._execute_command(
                    m.group(1),
                    m.group(2).strip() if m.lastindex > 1 else "",
                )

        return False

    def _looks_like_command(self, text: str) -> bool:
        """Return True only if text is plausibly a command."""
        if text.startswith(("spoaken.", "/", "!", ":")):
            return True

        first_word = text.lower().split()[0] if text.split() else ""
        if first_word in self._cmds or first_word in self._aliases:
            return True

        # Reject sentences starting with common speech words
        common_words = {
            "the", "a", "an", "is", "are", "was", "were",
            "this", "that", "i", "you", "we", "they", "it",
            "and", "or", "but", "so", "if", "in", "on", "at",
        }
        if first_word in common_words:
            return False

        return False

    def _execute_command(self, cmd_name: str, args: str) -> bool:
        cmd_name  = cmd_name.lower()
        real_name = self._aliases.get(cmd_name, cmd_name)
        cmd       = self._cmds.get(real_name)

        if not cmd:
            return False

        try:
            result = cmd.handler(args)
            if result and hasattr(self._ctrl, "view") and self._ctrl.view:
                self._ctrl.view.thread_safety_console(result)
            return True
        except Exception as e:
            print(f"[Command Error]: {e}", file=sys.stderr)
            return False

    def _safe_call(self, fn):
        try:
            return fn()
        except Exception as e:
            return f"[Error]: {e}"

    # ── Help ──────────────────────────────────────────────────────────────────

    def help_text(self) -> str:
        sections = [
            "Help", "Recording", "Transcript", "Engines",
            "Model Control", "LAN Access", "Chat / Rooms",
            "File Transfer", "Utilities",
        ]
        lines = [
            "",
            "╔═══════════════════════════════════════════════╗",
            "║  SPOAKEN COMMAND REFERENCE                    ║",
            "╚═══════════════════════════════════════════════╝",
        ]

        buckets: dict = {}
        for cmd in self._cmds.values():
            buckets.setdefault(cmd.section, []).append(cmd)

        for sec in sections:
            cmds = buckets.get(sec, [])
            if cmds:
                lines.append(f"  ── {sec} {'─' * (46 - len(sec))}")
                for cmd in sorted(cmds, key=lambda c: c.name):
                    lines.append(f"  {cmd.usage:<30}  {cmd.description}")

        return "\n".join(lines)

    # ── Registration ──────────────────────────────────────────────────────────

    def _register(self, name, handler, description, usage, aliases=(), section=""):
        cmd = _Cmd(name, handler, description, usage, aliases, section)
        self._cmds[name] = cmd
        for alias in aliases:
            self._aliases[alias] = name

    def _register_all(self):
        c = self._ctrl

        self._register("help", lambda _: self.help_text(),
                       "Show command reference", "help",
                       aliases=("?", "commands"), section="Help")

        self._register(
            "record",
            lambda _: self._safe_call(c.toggle_recording) if not c.model.is_running else None,
            "Start recording", "record", aliases=("start",), section="Recording",
        )
        self._register(
            "stop",
            lambda _: self._safe_call(c.toggle_recording) if c.model.is_running else None,
            "Stop recording", "stop", section="Recording",
        )

        self._register("copy",
                       lambda _: self._safe_call(getattr(c, "copy_transcript", lambda: None)),
                       "Copy transcript to clipboard", "copy", section="Transcript")
        self._register("clear",
                       lambda _: self._safe_call(getattr(c, "clear_all_logs", lambda: None)),
                       "Clear transcript and logs", "clear",
                       aliases=("wipe",), section="Transcript")
        self._register("polish",
                       lambda _: self._safe_call(c.polish_and_display),
                       "Run grammar correction", "polish",
                       aliases=("fix",), section="Transcript")
        self._register("translate", self._cmd_translate,
                       "Enable/disable translation", "translate <lang|off>",
                       section="Transcript")
        self._register("summarize", self._cmd_summarize,
                       "Summarize transcript", "summarize",
                       aliases=("summary",), section="Transcript")

        # Model Control
        self._register("vosk", self._cmd_vosk_control,
                       "Control Vosk engine", "vosk <on|off|model <name>>",
                       section="Model Control")
        self._register("whisper", self._cmd_whisper_control,
                       "Control Whisper engine", "whisper <on|off|model <name>>",
                       section="Model Control")
        self._register("cache.clear", self._cmd_clear_cache,
                       "Clear model cache and run GC", "cache.clear",
                       aliases=("clearcache",), section="Model Control")
        self._register("engine", self._cmd_engine_mode,
                       "Switch engine mode", "engine <vosk|whisper|both>",
                       section="Model Control")

        # Engines
        self._register("noise", self._cmd_noise,
                       "Toggle noise suppression", "noise <on|off>", section="Engines")
        self._register("preset", self._cmd_preset,
                       "Apply hardware preset", "preset <clean|budget_usb|headset|laptop>",
                       section="Engines")
        self._register("llm", self._cmd_llm,
                       "LLM control", "llm <on|off|status>", section="Engines")
        self._register("status", self._cmd_status,
                       "Show status", "status", section="Engines")

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _cmd_translate(self, args: str) -> str:
        if not args:
            return "Usage: translate <language|off>"
        if args.lower() == "off":
            self._ctrl._translate_lang = None
            return "[Translation]: disabled"
        self._ctrl._translate_lang = args.lower()
        return f"[Translation]: enabled ({args})"

    def _cmd_summarize(self, args: str) -> str:
        return self._ctrl.run_summarize()

    def _cmd_noise(self, args: str) -> str:
        a = args.lower()
        if a == "on":
            self._ctrl.toggle_noise_suppression(True)
            return "[Noise]: ON"
        if a == "off":
            self._ctrl.toggle_noise_suppression(False)
            return "[Noise]: OFF"
        return "Usage: noise <on|off>"

    def _cmd_llm(self, args: str) -> str:
        a = args.lower()
        if a == "on":
            self._ctrl.set_llm_enabled(True)
            return "[LLM]: enabled"
        if a == "off":
            self._ctrl.set_llm_enabled(False)
            return "[LLM]: disabled"
        if a == "status":
            state = "enabled" if self._ctrl._llm_enabled else "disabled"
            return f"[LLM]: {state}"
        return "Usage: llm <on|off|status>"

    def _cmd_status(self, args: str) -> str:
        """
        FIX: reads live runtime state from spoaken_connect module globals,
        not the frozen startup value from spoaken_config.ENGINE_MODE.
        """
        import spoaken.core.engine as _sc
        lines = [
            # Runtime engine mode derived from current active flags
            f"[Engine Mode]: vosk={'on' if _sc.VOSK_ACTIVE else 'off'} "
            f"whisper={'on' if _sc.WHISPER_ACTIVE else 'off'}",
            f"[Recording]: {'yes' if self._ctrl.model.is_running else 'no'}",
            f"[LLM]: {'enabled' if self._ctrl._llm_enabled else 'disabled'}",
            f"[Translate]: {self._ctrl._translate_lang or 'off'}",
        ]
        return "\n".join(lines)

    def _cmd_vosk_control(self, args: str) -> str:
        import spoaken.core.engine as _sc
        a = args.lower()
        if a == "on":
            _sc.VOSK_ACTIVE = True
            return "[Vosk]: enabled"
        if a == "off":
            _sc.VOSK_ACTIVE = False
            return "[Vosk]: disabled"
        if a.startswith("model "):
            model_name = args[6:].strip()
            if self._ctrl.model.reload_vosk(model_name):
                return f"[Vosk]: switched to {model_name}"
            return "[Vosk]: model switch failed"
        return "Usage: vosk <on|off|model <name>>"

    def _cmd_whisper_control(self, args: str) -> str:
        import spoaken.core.engine as _sc
        a = args.lower()
        if a == "on":
            _sc.WHISPER_ACTIVE = True
            return "[Whisper]: enabled"
        if a == "off":
            _sc.WHISPER_ACTIVE = False
            return "[Whisper]: disabled"
        if a.startswith("model "):
            model_name = args[6:].strip()
            if self._ctrl.model.reload_whisper(model_name):
                return f"[Whisper]: switched to {model_name}"
            return "[Whisper]: model switch failed"
        return "Usage: whisper <on|off|model <name>>"

    def _cmd_clear_cache(self, args: str) -> str:
        try:
            self._ctrl.model.data_store.clear()
            self._ctrl.model.whisper_store.clear()
            self._ctrl._pending_segments.clear()
            gc.collect()
            return "[Cache]: cleared, GC executed"
        except Exception as e:
            return f"[Cache]: clear failed — {e}"

    def _cmd_engine_mode(self, args: str) -> str:
        import spoaken.core.engine as _sc
        mode = args.lower()
        if mode == "vosk":
            _sc.VOSK_ACTIVE    = True
            _sc.WHISPER_ACTIVE = False
            return "[Engine]: Vosk-only mode"
        if mode == "whisper":
            _sc.VOSK_ACTIVE    = False
            _sc.WHISPER_ACTIVE = True
            return "[Engine]: Whisper-only mode"
        if mode == "both":
            _sc.VOSK_ACTIVE    = True
            _sc.WHISPER_ACTIVE = True
            return "[Engine]: Both engines active"
        return "Usage: engine <vosk|whisper|both>"
    
    def _cmd_preset(self, args: str) -> str:
        """Apply hardware preset for microphone type."""
        import spoaken.core.engine as _sc
        
        preset = args.lower().strip()
        
        if not preset:
            # Show available presets
            presets = list(_sc._HARDWARE_PRESETS.keys())
            return f"[Preset]: Available presets: {', '.join(presets)}\nUsage: preset <name>"
        
        if preset not in _sc._HARDWARE_PRESETS:
            available = ', '.join(_sc._HARDWARE_PRESETS.keys())
            return f"[Preset]: Unknown preset '{preset}'. Available: {available}"
        
        try:
            if _sc.apply_hardware_preset(preset):
                preset_info = _sc._HARDWARE_PRESETS[preset]
                return f"[Preset]: Applied '{preset_info['name']}' — {preset_info.get('description', '')}"
            else:
                return f"[Preset]: Failed to apply '{preset}'"
        except Exception as e:
            return f"[Preset Error]: {e}"
