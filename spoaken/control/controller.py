"""
control/controller.py
─────────────────────
Main application controller for Spoaken — v2.2

Changes from v2.1
─────────────────
  • Single log file ("log.txt") — vosk_log and whisper_log removed.
    Both engines write to the same unlimited rotating FileHandler.
    Log entries are prefixed [vosk] / [whisper] for filtering.
    Sessions are separated by a timestamp header line.

  • Model unloading — when the engine is switched via set_engine() the
    inactive model is actually unloaded from memory and gc.collect() is
    called.  Reload happens on-demand in toggle_recording() if needed.

  • GC throughout — gc.collect() called after every major deallocation:
    model unload, polish, clear, session stop, grammar worker drain,
    LLM worker completion, close, recovery restore.

  • First-run banner — one-time startup message explaining the auto-
    selected engine based on available RAM.

  • Session auto-save / crash recovery — SessionRecovery saves data_store
    every 60 s.  On next launch a Restore button is shown in the GUI if
    a recent recovery file is found.  Controller exposes restore_session()
    and discard_recovery() for the button callbacks.

  • Noise profile auto-capture — 1 s of ambient audio is captured just
    before recording starts and fed to AudioPipeline.capture_noise_profile().

  • Audio level pre-check — after the first 5 s of a session, warns in the
    console if average RMS is too low (mic not working) or too high (clipping).

  • Live word count — _register_text() calls view.thread_safety_word_count()
    so the transcript header stays current.

  • Degraded pipeline status indicator — _check_system_pressure() calls
    view.thread_safety_status() with degraded=True/False so the status label
    shows ⚡ when the pipeline is running in minimal mode.

  • VAD / mic config now read from main config.json — no separate file.
"""

import os
import gc
import uuid
import platform
import threading
import time
import json
import subprocess
import sys
from collections import deque
from pathlib import Path
from queue import Empty as _QueueEmpty

import sounddevice as sd
import numpy as np
# tkinter.messagebox imported lazily inside toggle_recording so controller.py
# loads cleanly on headless systems (Wayland with no DISPLAY, CI, etc.).

import spoaken.core.engine as _sc

from spoaken.system.paths import LOG_DIR
from spoaken.core.config import (
    MIC_DEVICE, MEMORY_CAP_WORDS, MEMORY_CAP_MINUTES, DUPLICATE_FILTER,
    VOSK_ENABLED,
    CHAT_SERVER_ENABLED, CHAT_SERVER_PORT, CHAT_SERVER_TOKEN,
    ANDROID_STREAM_ENABLED, ANDROID_STREAM_PORT,
    ENABLE_PARTIALS,
    BACKGROUND_MODE, AUDIO_LOOKAHEAD_BUFFER,
    FIRST_RUN_SHOWN, QUICK_VOSK_MODEL, WHISPER_MODEL,
)
from spoaken.core.engine import (
    process_audio, audio_gate, reset_vad, translate_text,
    configure_pipeline, apply_hardware_preset,
)
from spoaken.processing.writer import DirectWindowWriter
from spoaken.processing.summarize_router import summarize as _route_summarize
from spoaken.system.session_recovery import SessionRecovery

import logging

# ── Lazy imports ───────────────────────────────────────────────────────────────
# Three-state: False = not tried | True = succeeded | None = last attempt failed
# None state only retries when importlib confirms the module is now available
# (supports hot-fixing from the Update & Repair window mid-session).
_imports_done: dict = {"chat": False, "cmd": False}
ChatServer = SSEServer = CommandParser = None


def _ensure_chat() -> bool:
    global ChatServer, SSEServer
    state = _imports_done["chat"]
    if state is True:
        return True
    if state is None:
        import importlib.util
        if importlib.util.find_spec("spoaken.network.chat") is None:
            return False
    try:
        from spoaken.network.chat import ChatServer as _CS, SSEServer as _SS
        ChatServer            = _CS
        SSEServer             = _SS
        _imports_done["chat"] = True
        return True
    except ImportError as exc:
        print(f"[Controller]: chat module unavailable — {exc}", file=sys.stderr)
        _imports_done["chat"] = None
        return False


def _ensure_commands() -> bool:
    global CommandParser
    state = _imports_done["cmd"]
    if state is True:
        return True
    if state is None:
        import importlib.util
        if importlib.util.find_spec("spoaken.control.commands") is None:
            return False
    try:
        from spoaken.control.commands import CommandParser as _CP
        CommandParser         = _CP
        _imports_done["cmd"]  = True
        return True
    except ImportError as exc:
        print(f"[Controller]: commands module unavailable — {exc}", file=sys.stderr)
        _imports_done["cmd"]  = None
        return False


# ── Crashlog ───────────────────────────────────────────────────────────────────
def _crashlog(context: str, exc: Exception):
    """
    Write a crash report via CrashLogger.write_crash_log().
    crashlog.py exports CrashLogger (class) and log_crashes (decorator) but
    NOT a top-level log_crash() function — use the class method directly.
    """
    try:
        from spoaken.system.crashlog import CrashLogger
        CrashLogger().write_crash_log(
            exc, type(exc), exc.__traceback__, context=context
        )
    except Exception:
        import traceback
        print(f"[Crash][{context}]: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


# ── Config persistence ─────────────────────────────────────────────────────────
# spoaken/control/controller.py
#   _HERE        → spoaken/control/
#   _SPOAKEN_DIR → spoaken/
#   _ROOT        → <install_dir>/
_HERE        = Path(__file__).resolve().parent   # spoaken/control/
_SPOAKEN_DIR = _HERE.parent                      # spoaken/
_ROOT        = _SPOAKEN_DIR.parent               # <install_dir>/

_CONFIG_CANDIDATES = [
    _ROOT        / "spoaken_config.json",
    _SPOAKEN_DIR / "spoaken_config.json",
    Path.home()  / ".spoaken" / "config.json",
]


# Shared write lock — prevents concurrent writes from controller, vad, and
# any other module that saves spoaken_config.json within the same process.
try:
    from spoaken.core.engine import _config_write_lock
except ImportError:
    import threading as _threading
    _config_write_lock = _threading.Lock()


def _save_config(overrides: dict):
    target = next((p for p in _CONFIG_CANDIDATES if p.exists()), _CONFIG_CANDIDATES[-1])
    target.parent.mkdir(parents=True, exist_ok=True)
    with _config_write_lock:
        try:
            existing: dict = {}
            if target.exists():
                with open(target, encoding="utf-8") as f:
                    existing = json.load(f)
            existing.update(overrides)
            # Write via temp→rename for atomicity
            tmp = target.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
            tmp.replace(target)
        except Exception as exc:
            print(f"[Config]: save failed — {exc}", file=sys.stderr)


# ── Status colours ─────────────────────────────────────────────────────────────
STA_IDLE, STA_REC, STA_CORR = "#44537a", "#d42b2b", "#2c5fe6"

# ── Single log file ─────────────────────────────────────────────────────────────
LOG_FILE = str(LOG_DIR / "log.txt")

# Commands only parsed for short segments
_CMD_MAX_WORDS = 15

# System-pressure check interval
_PRESSURE_CHECK_INTERVAL = 30


def _make_session_logger() -> logging.Logger:
    """
    Single unlimited log file — all ASR output goes here.
    FileHandler with no rotation and no size cap.
    Previous vosk_log.txt and whisper_log.txt are no longer written.
    """
    logger = logging.getLogger("spoaken.session")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


_SAMPLE_RATE  = 16_000
_BLOCK_SIZE   = 800
_WHISPER_SECS = 8


# ═════════════════════════════════════════════════════════════════════════════
# TranscriptionController
# ═════════════════════════════════════════════════════════════════════════════

class TranscriptionController:

    def __init__(self):
        self.model   = None
        self.view    = None
        self.writer  = None
        self.writing_status = False
        self._mic_device    = MIC_DEVICE

        self._active_engine = "vosk" if VOSK_ENABLED else "whisper"

        self._pending_segments: dict = {}
        self._last_texts: deque      = deque(maxlen=20)

        self._session_start  = None
        self._polishing      = False

        # LLM
        self._llm_enabled     = False
        self._llm_mode        = None
        self._llm_model       = "llama3.2"
        self._llm_word_cursor = 0
        self._llm_bg_lock     = threading.Lock()
        self._llm_bg_running  = False
        self._translate_lang  = None

        # Live grammar
        self._live_grammar_enabled    = False
        self._live_grammar_replace_ui = False
        self._grammar_bg_queue: deque = deque()
        self._grammar_bg_lock         = threading.Lock()
        self._grammar_bg_running      = False

        # T5
        self._t5_enabled = False
        self._t5_mode    = None
        self._t5_model   = "vennify/t5-base-grammar-correction"

        # Servers
        self._chat_server = None
        self._sse_server  = None
        self._cmd_parser  = None
        self._sysenviron  = None

        # System pressure
        self._pipeline_degraded   = False
        self._last_pressure_check = 0.0

        # Audio level pre-check
        self._precheck_rms_sum   = 0.0
        self._precheck_rms_count = 0
        self._precheck_done      = False

        # Audio pause state — paused drops incoming frames without ending the session.
        # Recovery auto-save continues while paused so no transcript data is lost.
        self._paused            = False

        # Session recovery
        self._recovery          = SessionRecovery(self)
        self._recovery_pending: list | None = None

        # Performance
        self._background_mode    = BACKGROUND_MODE
        self._session_word_count = 0
        self._last_gc            = time.time()
        self._gc_interval        = 60

    # ─────────────────────────────────────────────────────────────────────────
    # Wiring
    # ─────────────────────────────────────────────────────────────────────────

    def set_objects(self, model, view):
        self.model = model
        self.view  = view

        self._ensure_logs()
        self._check_display_server()

        if not getattr(model, "_grammar_loaded", False):
            threading.Thread(target=model._background_load, daemon=True).start()

        try:
            from spoaken.system.environ import SysEnviron
            self._sysenviron = SysEnviron(log_fn=self._log)
            threading.Thread(
                target=self._sysenviron.benchmark,
                args=(self._log,),
                daemon=True,
            ).start()
        except ImportError:
            pass

        if CHAT_SERVER_ENABLED:
            _ensure_chat()
            self._chat_server = ChatServer(
                port=CHAT_SERVER_PORT,
                token=CHAT_SERVER_TOKEN,
                broadcast_cb=lambda m: None,
            )
            self._chat_server.start()

        if ANDROID_STREAM_ENABLED:
            _ensure_chat()
            self._sse_server = SSEServer(port=ANDROID_STREAM_PORT)
            self._sse_server.start()

        _ensure_commands()
        self._cmd_parser = CommandParser(self)

        # First-run banner (background thread — no startup delay)
        threading.Thread(target=self._maybe_show_first_run_banner, daemon=True).start()

        # Crash recovery check (background thread)
        threading.Thread(target=self._check_crash_recovery, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Logging
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        if self._background_mode or not self.view:
            print(f"[Console]: {msg}")
        else:
            self.view.thread_safety_console(msg)

    def _ensure_logs(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._session_logger = _make_session_logger()
        self._log_session_separator()

    def _log_session_separator(self):
        """Write a timestamp header line to the log at session init."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._session_logger.info(f"\n{'─' * 60}\n  Session started  {stamp}\n{'─' * 60}")

    def _check_display_server(self):
        if platform.system() != "Linux":
            return
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            self._log("[Warning]: Wayland detected — xdotool unavailable")

    # ─────────────────────────────────────────────────────────────────────────
    # First-run banner
    # ─────────────────────────────────────────────────────────────────────────

    def _maybe_show_first_run_banner(self):
        """
        Show the one-time welcome message in the GUI console.

        Runs in a background thread (started from set_objects) so it never
        delays startup.  Waits 0.8 s before posting so the GUI is fully
        painted and the Tk event loop is accepting after() callbacks.

        Uses view.after(0, ...) rather than calling self._log() directly —
        self._log() from a non-main thread calls view.thread_safety_console()
        which itself schedules via after(0, ...), so both paths are safe, but
        being explicit here makes the intent clear and avoids the extra hop.
        """
        try:
            if FIRST_RUN_SHOWN:
                return

            # Give the GUI time to fully paint before we post console messages.
            time.sleep(0.8)

            try:
                import psutil
                ram_gb  = psutil.virtual_memory().total / (1024 ** 3)
                ram_str = f"{ram_gb:.1f} GB RAM detected"
            except ImportError:
                ram_str = "RAM unknown"

            if VOSK_ENABLED:
                engine_msg = (
                    f"[Spoaken]: Welcome!  {ram_str} — Vosk selected "
                    "(faster, lower RAM). Switch to Whisper in the engine "
                    "toggle if you have > 6 GB free."
                )
            else:
                engine_msg = (
                    f"[Spoaken]: Welcome!  {ram_str} — Whisper selected "
                    "(higher accuracy). Switch to Vosk if you see high CPU use."
                )

            tip_msg = (
                "[Spoaken]: Tip — ⚙ Mic Setup tunes audio for your hardware.  "
                "Ctrl+R starts/stops recording."
            )

            # Schedule both messages onto the Tk main thread via after()
            if not self._background_mode and self.view:
                self.view.after(0, self.view.thread_safety_console, engine_msg)
                self.view.after(50, self.view.thread_safety_console, tip_msg)
            else:
                # Headless / background mode — print is fine
                print(engine_msg)
                print(tip_msg)

            _save_config({"first_run_shown": True})

        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Crash recovery
    # ─────────────────────────────────────────────────────────────────────────

    def _check_crash_recovery(self):
        try:
            segments = self._recovery.check_restore()
            if not segments:
                return

            word_count = sum(len(s.split()) for s in segments)
            age_mins   = self._recovery.recovery_file_age_minutes() or 0
            self._recovery_pending = segments

            self._log(
                f"[Recovery]: ⚠  Unsaved session found — "
                f"{len(segments)} segments, ~{word_count} words, "
                f"{age_mins:.0f} min ago."
            )

            # Show restore button in GUI
            if not self._background_mode and self.view:
                self.view.after(
                    0, self.view.show_restore_prompt,
                    len(segments), word_count,
                )
        except Exception:
            pass

    def restore_session(self):
        """Restore crash-recovered segments into data_store and transcript."""
        if not self._recovery_pending:
            self._log("[Recovery]: nothing to restore")
            return
        try:
            segments = self._recovery_pending
            self.model.data_store.extend(segments)
            self._session_word_count += sum(len(s.split()) for s in segments)

            for seg in segments:
                seg_id = self._register_pending(seg)
                if not self._background_mode and self.view:
                    self.view.thread_safety_insert_pending(
                        f"\n[Restored]: {seg}\n", seg_id, tag="vosk"
                    )

            self._recovery.discard()
            self._recovery_pending = None
            gc.collect()

            self._log(f"[Recovery]: ✔  Restored {len(segments)} segments")
            if not self._background_mode and self.view:
                self.view.after(0, self.view.hide_restore_prompt)
                self.view.thread_safety_word_count(self._session_word_count)

        except Exception as exc:
            self._log(f"[Recovery Error]: {exc}")

    def discard_recovery(self):
        self._recovery_pending = None
        self._recovery.discard()
        self._log("[Recovery]: session discarded")
        if not self._background_mode and self.view:
            self.view.after(0, self.view.hide_restore_prompt)

    # ─────────────────────────────────────────────────────────────────────────
    # Engine / pipeline controls
    # ─────────────────────────────────────────────────────────────────────────

    def set_engine(self, engine: str):
        """
        Switch the active ASR engine exclusively.

        Immediately unloads the inactive model from memory and calls
        gc.collect() to free RAM.  The new model is (re)loaded on demand
        in toggle_recording() if it was previously unloaded.
        """
        if engine not in ("vosk", "whisper"):
            return

        self._active_engine = engine
        _sc.VOSK_ACTIVE     = (engine == "vosk")
        _sc.WHISPER_ACTIVE  = (engine == "whisper")

        # ── Unload the inactive model ──────────────────────────────────────────
        if self.model:
            if engine == "vosk" and self.model.whisper_model is not None:
                self.model.whisper_model = None
                gc.collect()
                self._log("[Engine]: Whisper unloaded from memory")

            elif engine == "whisper" and self.model.small_model is not None:
                self.model.small_model = None
                gc.collect()
                self._log("[Engine]: Vosk unloaded from memory")

        import spoaken.core.config as _cfg
        if engine == "vosk":
            _cfg.VOSK_ENABLED    = True
            _cfg.WHISPER_ENABLED = False
        else:
            _cfg.VOSK_ENABLED    = False
            _cfg.WHISPER_ENABLED = True

        _save_config({
            "engine_mode":     engine + "_only",
            "vosk_enabled":    (engine == "vosk"),
            "whisper_enabled": (engine == "whisper"),
        })

        label = "Vosk" if engine == "vosk" else "Whisper"
        if self.model and self.model.is_running:
            self._log(f"[Engine]: will use {label} on next recording start")
        else:
            self._log(f"[Engine]: {label} selected ✔")

    def set_engine_enabled(self, engine: str, enabled: bool):
        """Legacy shim — routes to set_engine when enabling."""
        if enabled:
            self.set_engine(engine)

    def set_mic_device(self, device_index):
        self._mic_device = device_index
        _save_config({"mic_device": device_index})
        self._log(f"microphone → {device_index or 'system default'}")

    def toggle_noise_suppression(self, state: bool):
        configure_pipeline(nr_enabled=state)
        _save_config({"noise_suppression": state})
        self._log(f"noise suppression {'ON' if state else 'OFF'}")

    def set_audio_preset(self, preset_name: str):
        if apply_hardware_preset(preset_name):
            _save_config({"audio_preset": preset_name})
            self._log(f"audio preset → '{preset_name}'")
        else:
            self._log(f"[Audio]: unknown preset '{preset_name}'")

    # ─────────────────────────────────────────────────────────────────────────
    # System-pressure pipeline gating
    # ─────────────────────────────────────────────────────────────────────────

    def _check_system_pressure(self):
        if not self._sysenviron:
            return
        now = time.time()
        if now - self._last_pressure_check < _PRESSURE_CHECK_INTERVAL:
            return
        self._last_pressure_check = now

        try:
            import psutil
            cpu_pct    = psutil.cpu_percent(interval=None)
            free_mb    = psutil.virtual_memory().available / (1024 ** 2)
            under_load = cpu_pct > 85 or free_mb < 400

            if under_load and not self._pipeline_degraded:
                configure_pipeline(nr_enabled=False, board_preset="clean")
                self._pipeline_degraded = True
                self._log(
                    f"[Pipeline]: high load ({cpu_pct:.0f}% CPU, "
                    f"{free_mb:.0f} MB free) — stripped to minimal chain ⚡"
                )
                if not self._background_mode and self.view:
                    self.view.thread_safety_status("RECORDING", STA_REC, degraded=True)

            elif not under_load and self._pipeline_degraded:
                from spoaken.core.config import NOISE_SUPPRESSION
                configure_pipeline(nr_enabled=NOISE_SUPPRESSION, board_preset="budget")
                self._pipeline_degraded = False
                self._log("[Pipeline]: load normalised — full chain restored ✔")
                if not self._background_mode and self.view:
                    self.view.thread_safety_status("RECORDING", STA_REC, degraded=False)

        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # LLM controls
    # ─────────────────────────────────────────────────────────────────────────

    def set_llm_enabled(self, enabled: bool):
        self._llm_enabled = enabled

    def set_llm_mode(self, mode, model=None):
        self._llm_mode = mode
        if model:
            self._llm_model = model

    def set_llm_model(self, model: str):
        self._llm_model = model

    # ─────────────────────────────────────────────────────────────────────────
    # Live grammar
    # ─────────────────────────────────────────────────────────────────────────

    def set_live_grammar(self, enabled: bool, replace_ui: bool = False):
        self._live_grammar_enabled    = enabled
        self._live_grammar_replace_ui = replace_ui
        self._log(f"live grammar {'ON' if enabled else 'OFF'}")

    # ─────────────────────────────────────────────────────────────────────────
    # T5
    # ─────────────────────────────────────────────────────────────────────────

    def set_t5_enabled(self, enabled: bool):
        self._t5_enabled = enabled

    def set_t5_mode(self, mode, model=None):
        self._t5_mode = mode
        if model:
            self._t5_model = model

    def set_t5_model(self, model: str):
        self._t5_model = model

    def run_t5_correction(self, text=None) -> str:
        """
        Run grammar/T5 correction on text or the full data_store.

        Routes through self.model.run_polish() which uses the already-cached
        HappyTextToText model rather than loading AutoModelForSeq2SeqLM fresh
        on every call (which was a 3–10 s stall per invocation on a 480 MB model).
        Falls back to returning the source text unchanged on any error.
        """
        src = text or " ".join(self.model.data_store)
        if not src.strip():
            return "Nothing to process."
        try:
            store = src.split(". ")   # split into sentence-like chunks for run_polish
            _orig, result = self.model.run_polish(store if store else [src])
            gc.collect()
        except Exception:
            result = src
        self._log(f"[T5]: {result}")
        return result

    def run_summarize(self, text=None) -> str:
        src = text or " ".join(self.model.data_store)
        if not src.strip():
            return "Nothing to summarize."
        result = _route_summarize(src, model=self._llm_model)
        self._log(f"[Summary]: {result}")
        return result

    # ═════════════════════════════════════════════════════════════════════════
    # Text post-processing pipeline
    # ═════════════════════════════════════════════════════════════════════════

    def _finalize_segment(
        self,
        raw_text: str,
        source:   str        = "asr",
        seg_id:   str | None = None,
        is_vosk:  bool       = False,
    ) -> str | None:
        if not raw_text or not raw_text.strip():
            return None

        if self._is_duplicate(raw_text):
            return None
        self._register_text(raw_text)

        word_count = len(raw_text.split())
        if is_vosk or word_count <= _CMD_MAX_WORDS:
            if self._parse_command(raw_text):
                return None

        # NOTE: _maybe_trigger_llm_chunk is NOT called here directly.
        # _check_memory_cap() (called below) always calls it — a second call
        # here would double the LLM background thread churn on every segment.

        if self._live_grammar_enabled and self.model:
            self._schedule_live_grammar(raw_text, seg_id=seg_id, source=source)

        display_text = self._maybe_translate(raw_text)

        self.model.data_store.append(display_text)
        self._broadcast(f"[{source.upper()}] {display_text}")

        # Single log file — prefixed by source
        stamp = time.strftime("%H:%M:%S")
        self._session_logger.info(f"[{stamp}][{source}] {display_text}")

        self._check_memory_cap()
        return display_text

    # ── Live grammar ───────────────────────────────────────────────────────────

    def _schedule_live_grammar(self, text: str, seg_id: str | None, source: str):
        with self._grammar_bg_lock:
            self._grammar_bg_queue.append((text, seg_id, source))
            if self._grammar_bg_running:
                return
            self._grammar_bg_running = True
        threading.Thread(target=self._grammar_worker, daemon=True).start()

    def _grammar_worker(self):
        processed = 0
        while True:
            with self._grammar_bg_lock:
                if not self._grammar_bg_queue:
                    self._grammar_bg_running = False
                    break
                text, seg_id, source = self._grammar_bg_queue.popleft()
            try:
                corrected = self.model.correct_grammar(text)
                stamp = time.strftime("%H:%M:%S")
                self._session_logger.info(f"[{stamp}][{source}✓] {corrected}")
                processed += 1

                if (
                    self._live_grammar_replace_ui
                    and seg_id
                    and not self._background_mode
                    and self.view
                ):
                    self.view.after(
                        0,
                        self.view.thread_safety_replace_segments,
                        [seg_id],
                        f"\n[{source.capitalize()} ✓]: {corrected}\n",
                        "corrected",
                    )
            except Exception as exc:
                self._log(f"[Grammar Worker]: {exc}")

        # GC after draining the queue
        if processed > 0:
            gc.collect()

    # ── LLM background ─────────────────────────────────────────────────────────

    def _llm_chunk_budget(self) -> int:
        if self._sysenviron and getattr(self._sysenviron, "_benchmark_done", False):
            return self._sysenviron.get_llm_chunk_budget()
        return 80

    def _maybe_trigger_llm_chunk(self):
        if not self._llm_enabled or not self._llm_mode:
            return
        if self._sysenviron and not self._sysenviron.can_run_llm():
            return

        with self._llm_bg_lock:
            if self._llm_bg_running:
                return
            budget    = self._llm_chunk_budget()
            all_words = " ".join(self.model.data_store).split()
            new_count = len(all_words) - self._llm_word_cursor
            if new_count < budget:
                return
            chunk_words = all_words[self._llm_word_cursor: self._llm_word_cursor + budget]
            chunk_text  = " ".join(chunk_words)
            cursor_end  = self._llm_word_cursor + len(chunk_words)
            self._llm_bg_running = True

        threading.Thread(
            target=self._llm_chunk_worker,
            args=(chunk_text, cursor_end, self._llm_mode, self._llm_model, self._translate_lang),
            daemon=True,
        ).start()

    def _llm_chunk_worker(self, text, cursor_end, mode, model_name, lang):
        try:
            if mode == "summarize":
                result   = _route_summarize(text, model=model_name)
                log_path = LOG_DIR / "llm_summary.txt"
            elif mode == "translate" and lang:
                result   = translate_text(text, target_lang=lang) or text
                log_path = LOG_DIR / "llm_translation.txt"
            else:
                return

            if result:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(result + "\n")

            with self._llm_bg_lock:
                self._llm_word_cursor = cursor_end

        except Exception as exc:
            self._log(f"[LLM Error]: {exc}")
        finally:
            with self._llm_bg_lock:
                self._llm_bg_running = False
            gc.collect()

    def flush_llm_full(self):
        all_text = " ".join(self.model.data_store)
        if not all_text.strip():
            return

        def _worker():
            try:
                if self._llm_mode == "summarize":
                    result   = _route_summarize(all_text, model=self._llm_model)
                    log_path = LOG_DIR / "llm_summary.txt"
                elif self._llm_mode == "translate" and self._translate_lang:
                    result   = translate_text(all_text, self._translate_lang) or all_text
                    log_path = LOG_DIR / "llm_translation.txt"
                else:
                    return
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[Full Flush]\n{result}\n")
            except Exception:
                pass
            finally:
                gc.collect()

        threading.Thread(target=_worker, daemon=True).start()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _is_duplicate(self, text: str) -> bool:
        if not DUPLICATE_FILTER:
            return False
        return text.lower().strip() in self._last_texts

    def _register_text(self, text: str):
        if DUPLICATE_FILTER:
            self._last_texts.append(text.lower().strip())
        self._session_word_count += len(text.split())

        # Update live word count in transcript header
        if not self._background_mode and self.view and hasattr(self.view, "thread_safety_word_count"):
            self.view.thread_safety_word_count(self._session_word_count)

    def _maybe_translate(self, text: str) -> str:
        if self._translate_lang and self._translate_lang != "en":
            return translate_text(text, target_lang=self._translate_lang) or text
        return text

    def _parse_command(self, text: str) -> bool:
        return self._cmd_parser.parse(text) if self._cmd_parser else False

    def _register_pending(self, text: str) -> str:
        seg_id = str(uuid.uuid4())[:8]
        self._pending_segments[seg_id] = text
        return seg_id

    def _broadcast(self, message: str):
        if self._chat_server:
            self._chat_server.broadcast(message)
        if self._sse_server:
            self._sse_server.broadcast(message)

    def _maybe_run_gc(self):
        now = time.time()
        if now - self._last_gc > self._gc_interval:
            gc.collect()
            self._last_gc = now

    def _check_memory_cap(self):
        time_cap = False
        if self._session_start:
            elapsed  = (time.time() - self._session_start) / 60
            time_cap = elapsed >= MEMORY_CAP_MINUTES

        if (self._session_word_count >= MEMORY_CAP_WORDS or time_cap) and not self._polishing:
            self._log("memory cap — auto-polish")
            self._session_start      = time.time()
            self._session_word_count = 0
            threading.Thread(target=self.polish_and_display, daemon=True).start()

        self._maybe_trigger_llm_chunk()
        self._maybe_run_gc()

    # ═════════════════════════════════════════════════════════════════════════
    # Noise profile auto-capture
    # ═════════════════════════════════════════════════════════════════════════

    def _capture_noise_profile_async(self):
        """
        Record ~1 s of ambient audio and feed it to AudioPipeline as a
        stationary noise reference.  Non-critical — silently continues on
        any failure.

        Note: opens a second sounddevice stream briefly (before the main
        capture loop starts) which can fail on exclusive-mode devices
        (e.g. some Windows WASAPI configs).  If this happens the error is
        logged and recording continues without a noise profile.
        """
        try:
            frames = int(1.1 * _SAMPLE_RATE)
            silent = sd.rec(
                frames,
                samplerate=_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                device=self._mic_device,
            )
            sd.wait()
            _sc._pipeline.capture_noise_profile(silent.tobytes())
        except Exception as exc:
            self._log(f"[Pipeline]: noise profile skipped — {exc}")

    # ═════════════════════════════════════════════════════════════════════════
    # Recording
    # ═════════════════════════════════════════════════════════════════════════

    def toggle_recording(self):
        if not self.model.is_running:
            if not self._background_mode:
                # Import messagebox lazily — avoids crashing on headless systems
                # (Wayland with DISPLAY unset, CI runners, etc.) where importing
                # tkinter.messagebox at module level raises TclError.
                try:
                    from tkinter import messagebox as _mb
                    if not _mb.askokcancel("Spoaken", "Allow microphone?"):
                        return
                except Exception:
                    pass  # headless / no display — skip the dialog, proceed

            self._session_start      = time.time()
            self._session_word_count = 0
            self._precheck_rms_sum   = 0.0
            self._precheck_rms_count = 0
            self._precheck_done      = False
            self.model.is_running    = True

            # Ensure the active model is loaded (may have been unloaded on engine switch)
            if _sc.VOSK_ACTIVE and self.model.small_model is None:
                self._log("[Engine]: reloading Vosk model …")
                if not self.model.reload_vosk(QUICK_VOSK_MODEL):
                    self._log("[Warning]: Vosk model failed to reload — check Update panel")

            elif _sc.WHISPER_ACTIVE and self.model.whisper_model is None:
                self._log("[Engine]: reloading Whisper model …")
                if not self.model.reload_whisper(WHISPER_MODEL):
                    self._log("[Warning]: Whisper model failed to reload — check Update panel")

            # Drain active queue — get_nowait loop is thread-safe; .empty() is not
            active_q = (
                self.model.vosk_queue if _sc.VOSK_ACTIVE else self.model.whisper_queue
            )
            while True:
                try:
                    active_q.get_nowait()
                except Exception:
                    break
            reset_vad()

            self._pipeline_degraded   = False
            self._last_pressure_check = 0.0

            # Write session header to log
            self._log_session_separator()

            engine_label    = "Vosk" if _sc.VOSK_ACTIVE else "Whisper"
            pipeline_stages = getattr(
                getattr(_sc, "_pipeline", None), "stages_active",
                ["agc", "compress", "eq"],
            )
            self._log(
                f"recording started  |  engine: {engine_label}  |  "
                f"pipeline: {pipeline_stages}"
            )

            if not self._background_mode and self.view:
                self.view.update_status("RECORDING", STA_REC)
                self.view.set_waveform_state("recording")
                self.view.btn_start.configure(
                    text="Stop Recording", fg_color="#c42828", hover_color="#e03535",
                )

            # Start recovery auto-save
            self._recovery.start()

            # Capture ambient noise profile — runs concurrently with stream open.
            # We do NOT join() so startup is not blocked by the 1 s recording.
            # The noise profile will be ready within ~1.1 s; any audio processed
            # before that simply runs without NR, which is acceptable.
            threading.Thread(
                target=self._capture_noise_profile_async, daemon=True
            ).start()

            # Start capture
            threading.Thread(target=self._audio_capture_loop, daemon=True).start()

            if _sc.VOSK_ACTIVE and self.model.small_model:
                threading.Thread(target=self.audio_stream_loop, daemon=True).start()
            elif _sc.WHISPER_ACTIVE and self.model.whisper_model:
                threading.Thread(target=self.whisper_loop, daemon=True).start()
            else:
                self._log(
                    f"[Warning]: {engine_label} model not loaded — "
                    "install via the Update panel then restart"
                )

        else:
            self.model.is_running = False
            self._paused          = False   # always reset on stop
            self._log("recording stopped")
            self._recovery.stop()   # clean stop — deletes recovery file

            if not self._background_mode and self.view:
                self.view.update_status("IDLE", STA_IDLE)
                self.view.set_waveform_state("idle")
                self.view.btn_start.configure(
                    text="Start Recording", fg_color="#1a5e2a", hover_color="#24883c",
                )

            if self._llm_enabled and self._llm_mode:
                self.flush_llm_full()

            gc.collect()

    # ═════════════════════════════════════════════════════════════════════════
    # Pause / Resume
    # ═════════════════════════════════════════════════════════════════════════

    def toggle_pause(self):
        """
        Pause or resume audio capture without ending the session.

        Pausing drops incoming audio frames at the callback level — the
        sounddevice stream stays open so resume is instant (no stream
        re-open latency).  Session recovery auto-save continues during a
        pause so no in-progress transcript data is lost if the process dies.
        """
        if not self.model.is_running:
            return

        self._paused = not self._paused
        label = "PAUSED" if self._paused else "RECORDING"
        color = STA_IDLE  if self._paused else STA_REC
        wf    = "idle"    if self._paused else "recording"

        self._log(f"[Recording]: {'paused — mic muted' if self._paused else 'resumed'}")

        if not self._background_mode and self.view:
            self.view.update_status(label, color)
            self.view.set_waveform_state(wf)
            self.view.btn_start.configure(
                text="Resume Recording" if self._paused else "Stop Recording",
                fg_color="#5a5a00"      if self._paused else "#c42828",
                hover_color="#7a7a00"  if self._paused else "#e03535",
            )

    # ═════════════════════════════════════════════════════════════════════════
    # Audio capture loop
    # ═════════════════════════════════════════════════════════════════════════

    def _audio_capture_loop(self):
        lookahead_buffer: deque = deque(maxlen=AUDIO_LOOKAHEAD_BUFFER)
        half_lookahead = max(1, AUDIO_LOOKAHEAD_BUFFER // 2)

        # target_queue is intentionally re-evaluated on every callback invocation
        # so that a mid-session engine switch (set_engine()) immediately routes
        # new audio to the correct queue without restarting the stream.
        def _cb(indata, frames, time_info, status):
            # Drop frames while paused — keeps the stream open so resume is instant.
            if self._paused:
                return

            clean = process_audio(bytes(indata))
            gated = audio_gate(clean)

            if gated is not None:
                lookahead_buffer.append(gated)
                if len(lookahead_buffer) >= half_lookahead:
                    q = (
                        self.model.vosk_queue
                        if _sc.VOSK_ACTIVE
                        else self.model.whisper_queue
                    )
                    q.put(lookahead_buffer.popleft())

            if not self._background_mode and self.view:
                arr = np.frombuffer(clean, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(arr ** 2))) / 32768.0
                self.view.after(0, self.view.push_audio_level, rms)

                # ── Audio level pre-check (first 5 s) ─────────────────────────
                if not self._precheck_done:
                    self._precheck_rms_sum   += rms
                    self._precheck_rms_count += 1
                    elapsed = time.time() - (self._session_start or time.time())
                    if elapsed >= 5.0 and self._precheck_rms_count > 0:
                        avg = self._precheck_rms_sum / self._precheck_rms_count
                        self._precheck_done = True
                        if avg < 0.008:
                            self._log(
                                f"[Mic Warning]: ⚠  Very low signal (avg RMS {avg:.4f}). "
                                "Try: increase OS mic gain, enable Noise ON, "
                                "or switch to the Laptop audio preset."
                            )
                        elif avg > 0.85:
                            self._log(
                                f"[Mic Warning]: ⚠  Signal very high (avg RMS {avg:.3f}). "
                                "Reduce OS mic gain to prevent clipping."
                            )

        try:
            with sd.RawInputStream(
                samplerate=_SAMPLE_RATE,
                blocksize=_BLOCK_SIZE,
                device=self._mic_device,
                dtype="int16",
                channels=1,
                callback=_cb,
            ):
                while self.model.is_running:
                    time.sleep(0.05)
                    self._check_system_pressure()

        except Exception as exc:
            _crashlog("_audio_capture_loop", exc)
            self._log(f"[Audio Error]: {exc}")
        finally:
            # Flush remaining lookahead into whichever queue is currently active,
            # then send the None sentinel to signal the consumer loop to stop.
            final_q = (
                self.model.vosk_queue
                if _sc.VOSK_ACTIVE
                else self.model.whisper_queue
            )
            while lookahead_buffer:
                final_q.put(lookahead_buffer.popleft())
            final_q.put(None)

    # ═════════════════════════════════════════════════════════════════════════
    # Vosk loop
    # ═════════════════════════════════════════════════════════════════════════

    def audio_stream_loop(self):
        self._log("Vosk stream open")
        rec            = self.model.get_fast_recognizer()
        last_partial   = ""
        partial_seg_id = None

        try:
            while True:
                try:
                    data = self.model.vosk_queue.get(timeout=1.0)
                except _QueueEmpty:
                    if not self.model.is_running:
                        break
                    continue

                if data is None:
                    break

                if rec.AcceptWaveform(data):
                    raw_text = json.loads(rec.Result()).get("text", "").strip()

                    if ENABLE_PARTIALS and partial_seg_id and not self._background_mode:
                        self.view.thread_safety_replace_segments(
                            [partial_seg_id], "", tag="partial"
                        )
                    last_partial   = ""
                    partial_seg_id = None

                    if not _sc.VOSK_ACTIVE or not raw_text:
                        continue

                    seg_id = self._register_pending(raw_text)
                    if not self._background_mode and self.view:
                        self.view.thread_safety_insert_pending(
                            f"\n[Vosk]: {raw_text}\n", seg_id, tag="vosk"
                        )

                    final = self._finalize_segment(
                        raw_text, source="vosk", seg_id=seg_id, is_vosk=True
                    )
                    if final and self.writing_status and self.writer:
                        self.writer.write(final + " ")

                elif ENABLE_PARTIALS:
                    partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                    if partial and partial != last_partial and not self._background_mode and self.view:
                        if partial_seg_id is None:
                            partial_seg_id = self._register_pending(partial)
                            self.view.thread_safety_insert_pending(
                                f"[…] {partial}", partial_seg_id, tag="partial"
                            )
                        else:
                            self.view.thread_safety_replace_segments(
                                [partial_seg_id], f"[…] {partial}", tag="partial"
                            )
                        last_partial = partial

        except Exception as exc:
            _crashlog("audio_stream_loop", exc)
            self._log(f"[Vosk Error]: {exc}")

    # ═════════════════════════════════════════════════════════════════════════
    # Whisper loop
    # ═════════════════════════════════════════════════════════════════════════

    def whisper_loop(self):
        self._log("Whisper engine active")
        buf          = b""
        chunk_bytes  = _SAMPLE_RATE * _WHISPER_SECS * 2
        last_whisper = ""

        def _flush(buffer: bytes):
            nonlocal last_whisper
            if not buffer or not _sc.WHISPER_ACTIVE:
                return

            raw_text = self.model.transcribe_whisper(buffer)
            if not raw_text or raw_text == last_whisper:
                return

            last_whisper = raw_text
            seg_id = self._register_pending(raw_text)
            if not self._background_mode and self.view:
                self.view.thread_safety_insert_pending(
                    f"\n[Whisper]: {raw_text}\n", seg_id, tag="whisper"
                )

            final = self._finalize_segment(
                raw_text, source="whisper", seg_id=seg_id, is_vosk=False
            )
            if final:
                self.model.whisper_store.append(final)
                if self.writing_status and self.writer:
                    self.writer.write(final + " ")

        try:
            while True:
                try:
                    data = self.model.whisper_queue.get(timeout=1.0)
                except _QueueEmpty:
                    if not self.model.is_running:
                        break
                    continue

                if data is None:
                    break

                buf += data
                if len(buf) >= chunk_bytes:
                    _flush(buf[:chunk_bytes])
                    buf = buf[chunk_bytes:]

        except Exception as exc:
            _crashlog("whisper_loop", exc)
            self._log(f"[Whisper Error]: {exc}")
        finally:
            if buf:
                _flush(buf)

    # ═════════════════════════════════════════════════════════════════════════
    # Polish
    # ═════════════════════════════════════════════════════════════════════════

    def polish_and_display(self):
        self._polishing = True
        if not self._background_mode and self.view:
            self.view.after(0, self.view.update_status, "CORRECTING", STA_CORR)

        try:
            raw, corrected = self.model.run_polish()
            stamp = time.strftime("%Y-%m-%d %H:%M")
            self._log(f"\n[Polished]\n{corrected}\n")
            self._session_logger.info(f"\n[Polish {stamp}]\n{corrected}\n")
            self.model.data_store.clear()
            gc.collect()
        except Exception as exc:
            self._log(f"[Polish Error]: {exc}")
        finally:
            self._polishing = False
            if not self._background_mode and self.view:
                self.view.after(0, self.view.update_status, "IDLE", STA_IDLE)

    # ═════════════════════════════════════════════════════════════════════════
    # Window writer
    # ═════════════════════════════════════════════════════════════════════════

    def toggle_page_writing(self):
        if not self.writing_status:
            try:
                title = ""
                if self.view and hasattr(self.view, "ent_target"):
                    title = self.view.ent_target.get().strip()
                self.writer         = DirectWindowWriter(title, log_cb=self._log)
                self.writing_status = True
                self._log("window writer enabled")
                if self.view and hasattr(self.view, "set_writing_btn"):
                    self.view.after(0, self.view.set_writing_btn, True)
            except Exception as exc:
                self._log(f"[Writer Error]: {exc}")
                self.writing_status = False
        else:
            self.writing_status = False
            self.writer         = None
            gc.collect()
            self._log("window writer disabled")
            if self.view and hasattr(self.view, "set_writing_btn"):
                self.view.after(0, self.view.set_writing_btn, False)

    def toggle_writing(self):
        self.toggle_page_writing()

    def lock_writer_target(self):
        title = ""
        if self.view and hasattr(self.view, "ent_target"):
            title = self.view.ent_target.get().strip()
        if not title:
            self._log("[Writer]: enter a window title first")
            return
        try:
            if self.writer:
                self.writer.refresh(title)
                self._log(f"[Writer]: retargeted → '{title}'")
            else:
                self.writer         = DirectWindowWriter(title, log_cb=self._log)
                self.writing_status = True
            if self.view and hasattr(self.view, "update_lock_btn"):
                self.view.after(0, self.view.update_lock_btn, True)
        except Exception as exc:
            self._log(f"[Writer Error]: {exc}")

    def set_window_writer_target(self):
        self.lock_writer_target()

    # ═════════════════════════════════════════════════════════════════════════
    # GUI utility methods
    # ═════════════════════════════════════════════════════════════════════════

    def on_close_request(self):
        self.model.is_running = False
        self._recovery.stop()
        if self._llm_enabled and self._llm_mode:
            self.flush_llm_full()
        # Unload models before exit
        if self.model:
            self.model.small_model    = None
            self.model.whisper_model  = None
            self.model.tool           = None
        gc.collect()
        if self.view:
            try:
                self.view.destroy()
            except Exception:
                pass

    def copy_transcript(self):
        if not self.view:
            return
        try:
            text = " ".join(self.model.data_store)
            self.view.clipboard_clear()
            self.view.clipboard_append(text)
            self._log("[Copy]: transcript copied to clipboard")
        except Exception as exc:
            self._log(f"[Copy Error]: {exc}")

    def clear_all_logs(self):
        self.model.data_store.clear()
        self.model.whisper_store.clear()
        self._pending_segments.clear()
        self._session_word_count = 0
        self._last_texts.clear()
        gc.collect()

        if self.view and hasattr(self.view, "log"):
            try:
                self.view.log.configure(state="normal")
                self.view.log.delete("1.0", "end")
                self.view.log.configure(state="disabled")
            except Exception:
                pass

        # Update word count display
        if not self._background_mode and self.view and hasattr(self.view, "thread_safety_word_count"):
            self.view.thread_safety_word_count(0)

        # Truncate the single log file cleanly
        try:
            with open(LOG_FILE, "w", encoding="utf-8"):
                pass
        except Exception:
            pass

        self._log("[Clear]: transcript and logs cleared")

    def open_logs(self):
        try:
            log_path = str(LOG_DIR)
            if sys.platform == "win32":
                os.startfile(log_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", log_path])
            else:
                subprocess.Popen(["xdg-open", log_path])
        except Exception as exc:
            self._log(f"[Logs]: could not open folder — {exc}")

    def swap_polishing(self):
        if not self._polishing:
            threading.Thread(target=self.polish_and_display, daemon=True).start()
        else:
            self._log("[Polish]: already running")

    def swap_vosk_model(self, model_name: str):
        if not model_name or not model_name.strip():
            return
        self._log(f"[Vosk]: switching to {model_name} …")
        if self.model.reload_vosk(model_name):
            self._log(f"[Vosk]: loaded {model_name} ✔")
        else:
            self._log(f"[Vosk]: failed to load {model_name}")

    def swap_whisper_model(self, model_name: str):
        if not model_name or not model_name.strip():
            return
        self._log(f"[Whisper]: switching to {model_name} …")
        if self.model.reload_whisper(model_name):
            self._log(f"[Whisper]: loaded {model_name} ✔")
        else:
            self._log(f"[Whisper]: failed to load {model_name}")

    def chat_send(self, message: str):
        if not message.strip():
            return
        self._broadcast(message)
        self._log(f"[Chat]: {message}")

    def toggle_chat_port(self):
        if self._chat_server is None:
            _ensure_chat()
            self._chat_server = ChatServer(
                port=CHAT_SERVER_PORT,
                token=CHAT_SERVER_TOKEN,
                broadcast_cb=lambda m: None,
            )
            self._chat_server.start()
            self._log(f"[LAN]: server started on port {self._chat_server.port}")
            if self.view and hasattr(self.view, "update_chat_port_btn"):
                self.view.after(0, self.view.update_chat_port_btn, True)
        else:
            self._chat_server = None
            self._log("[LAN]: server stopped")
            if self.view and hasattr(self.view, "update_chat_port_btn"):
                self.view.after(0, self.view.update_chat_port_btn, False)
