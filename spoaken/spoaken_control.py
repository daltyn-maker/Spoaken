"""
spoaken_control.py
──────────────────
Controller layer — glues the model and view together.

LLM background processing
─────────────────────────
  During a session the LLM processes the transcript in chunks.
  Chunk size is calibrated at startup by SysEnviron.benchmark():
    fast machine   → 150 words / pass
    medium         →  80 words / pass
    slow           →  40 words / pass
    very_slow      →  20 words / pass

  The worker keeps an internal write-cursor (_llm_word_cursor) so each
  pass only feeds new words since the last run — no re-processing.
  A pass is triggered whenever enough new words have accumulated (equal
  to the current chunk budget) and the system load gate allows it.

  At session end (recording stopped) a full-transcript flush runs on a
  separate thread with no chunk limit, processing everything that was
  not yet handled.  Output goes to Logs/llm_summary.txt or
  Logs/llm_translation.txt.

Engine changes vs prior version
────────────────────────────────
  • Vosk: partials show in GUI log only — never written to target window.
    Only final accepted sentences are committed to the target window.
  • Whisper: 8-second audio chunks for full-sentence output.
  • ENABLE_GIGA_MODEL / accuracy_process_loop removed entirely.
  • SysEnviron wired at set_objects() time — benchmark runs in background.
"""

import os
import platform
import re
import socket
import subprocess
import threading
import time
import json
from collections import deque

import sounddevice as sd
import numpy as np
from tkinter import messagebox

from paths import LOG_DIR
from spoaken_config import (
    MIC_DEVICE, NOISE_SUPPRESSION,
    MEMORY_CAP_WORDS, MEMORY_CAP_MINUTES,
    DUPLICATE_FILTER,
    VOSK_ENABLED, WHISPER_ENABLED,
    CHAT_SERVER_ENABLED, CHAT_SERVER_PORT, CHAT_SERVER_TOKEN,
    ANDROID_STREAM_ENABLED, ANDROID_STREAM_PORT,
)
from spoaken_connect import maybe_suppress_noise, audio_gate, reset_vad, translate_text
from spoaken_writer  import DirectWindowWriter
from spoaken_chat    import ChatServer, SSEServer
from spoaken_commands import CommandParser

# ── Status colours ─────────────────────────────────────────────────────────────
STA_IDLE = "#44537a"
STA_REC  = "#d42b2b"
STA_CORR = "#2c5fe6"

# ── Log file paths ─────────────────────────────────────────────────────────────
VOSK_LOG    = str(LOG_DIR / "vosk_log.txt")
WHISPER_LOG = str(LOG_DIR / "whisper_log.txt")
FINAL_LOG   = str(LOG_DIR / "final_session_log.txt")

# ── Audio constants ─────────────────────────────────────────────────────────────
_SAMPLE_RATE  = 16000
_BLOCK_SIZE   = 800       # ~50 ms frames
_WHISPER_SECS = 8         # 8 s chunks → full-sentence Whisper output

# ── Minimum new words before triggering a chunk LLM pass ─────────────────────
# This multiplier is applied to chunk_budget so a pass fires once a full
# chunk's worth of new content is available.
_LLM_TRIGGER_RATIO = 1.0   # trigger at exactly 1× the chunk budget


# ═════════════════════════════════════════════════════════════════════════════
class TranscriptionController:
# ═════════════════════════════════════════════════════════════════════════════

    def __init__(self):
        self.model          = None
        self.view           = None
        self.writing_status = False
        self.writer         = None
        self._writer_locked = False

        # Segment tracking
        self.pending_segments   = []
        self.pending_lock       = threading.Lock()
        self.seg_counter        = 0
        self.total_window_chars = 0
        self._polishing         = False

        # Microphone
        self._mic_device = MIC_DEVICE

        # Memory cap
        self._session_start = None

        # Duplicate filter
        self._recent_texts: deque = deque(maxlen=30)

        # Translation
        self._translate_lang: str | None = None

        # LLM state
        self._llm_enabled : bool       = False
        self._llm_mode    : str | None = None   # "summarize" | "translate"
        self._llm_model   : str | None = None

        # LLM background worker state
        # _llm_word_cursor tracks the last word index processed so each pass
        # only feeds new content — never re-reads old text.
        self._llm_word_cursor : int                    = 0
        self._llm_bg_lock     : threading.Lock         = threading.Lock()
        self._llm_bg_running  : bool                   = False

        # Chat / SSE servers
        self._chat_server : ChatServer | None = None
        self._sse_server  : SSEServer  | None = None

        # Command parser
        self._cmd_parser  : CommandParser | None = None

        # SysEnviron (set in set_objects)
        self._sysenviron = None

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def set_objects(self, model, view):
        self.model = model
        self.view  = view
        self._ensure_logs()
        self._check_display_server()

        # Start SysEnviron — runs benchmark in background, then begins polling
        try:
            from spoaken_sysenviron import SysEnviron
            self._sysenviron = SysEnviron(controller=self)
            self._sysenviron.start()
        except Exception as exc:
            print(f"[SysEnviron]: not loaded — {exc}")

        self._chat_server = ChatServer(
            port       = CHAT_SERVER_PORT,
            token      = CHAT_SERVER_TOKEN,
            on_message = self._on_chat_message,
            log_cb     = self.view.thread_safety_console,
        )
        if CHAT_SERVER_ENABLED:
            self._chat_server.start()

        self._sse_server = SSEServer(
            port   = ANDROID_STREAM_PORT,
            log_cb = self.view.thread_safety_console,
        )
        if ANDROID_STREAM_ENABLED:
            self._sse_server.start()

        if CHAT_SERVER_ENABLED:
            self.view.after(100, self.view.update_chat_port_btn, True)

        self._cmd_parser = CommandParser(self)

    def _ensure_logs(self):
        for p in (VOSK_LOG, WHISPER_LOG, FINAL_LOG):
            if not os.path.exists(p):
                open(p, "w", encoding="utf-8").close()

    def _check_display_server(self):
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        wayland = os.environ.get("WAYLAND_DISPLAY", "")
        if session == "wayland" or wayland:
            self.view.update_console(
                "[Warning]: Wayland detected — xdotool/wmctrl unavailable.\n"
                "  Log out and choose 'on Xorg' at the login screen for native\n"
                "  window targeting.  pyautogui fallback is active."
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Microphone / engine controls
    # ─────────────────────────────────────────────────────────────────────────

    def set_mic_device(self, device_index: int | None):
        self._mic_device = device_index
        label = f"device {device_index}" if device_index is not None else "system default"
        self.view.update_console(f"[Console]: microphone → {label}")

    def toggle_noise_suppression(self, state: bool):
        import spoaken_connect as _sc
        import spoaken_config  as _cfg
        _cfg.NOISE_SUPPRESSION = state
        _sc.NOISE_SUPPRESSION  = state
        self.view.update_console(
            f"[Console]: noise suppression {'ON' if state else 'OFF'}"
        )

    def set_engine_enabled(self, engine: str, enabled: bool):
        import spoaken_connect as _sc
        if engine == "vosk":
            _sc.VOSK_ACTIVE = enabled
        elif engine == "whisper":
            _sc.WHISPER_ACTIVE = enabled

    def set_llm_enabled(self, enabled: bool):
        self._llm_enabled = enabled

    def set_llm_mode(self, mode, model: str = None):
        self._llm_mode = mode
        if model:
            self._llm_model = model

    def set_llm_model(self, model: str):
        self._llm_model = model

    def run_summarize(self, text: str = None) -> str:
        src = text or " ".join(self.model.data_store)
        if not src.strip():
            return "Nothing to summarise yet."
        try:
            from spoaken_llm import summarize_llm, ensure_ollama_pkg
            ensure_ollama_pkg(log_fn=self.view.thread_safety_console)
            result = summarize_llm(src, model=self._llm_model)
        except Exception:
            from spoaken_summarize import summarize as _ext
            result = _ext(src)
        self.view.thread_safety_console(f"[Summary]:\n{result}")
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Background LLM — incremental chunk processing
    # ─────────────────────────────────────────────────────────────────────────

    def _llm_chunk_budget(self) -> int:
        """
        Words per LLM pass — comes from SysEnviron's calibrated budget.
        Falls back to 80 if SysEnviron isn't available yet.
        """
        if self._sysenviron and self._sysenviron._benchmark_done:
            return self._sysenviron.get_llm_chunk_budget()
        return 80   # safe default before benchmark completes

    def _maybe_trigger_llm_chunk(self):
        """
        Called after every new sentence is added to data_store.
        Fires a background chunk pass if:
          • LLM is enabled and a mode is set
          • system load gate passes (SysEnviron.can_run_llm)
          • no other chunk pass is running
          • enough new words have accumulated (≥ chunk budget)
        """
        if not self._llm_enabled or not self._llm_mode:
            return
        if self._sysenviron and not self._sysenviron.can_run_llm():
            return
        if self._llm_bg_running:
            return

        budget     = self._llm_chunk_budget()
        all_words  = " ".join(self.model.data_store).split()
        new_count  = len(all_words) - self._llm_word_cursor

        # Only proceed when we have at least a full chunk's worth of new text
        if new_count < budget:
            return

        # Grab exactly budget-many new words
        chunk_words = all_words[self._llm_word_cursor : self._llm_word_cursor + budget]
        chunk_text  = " ".join(chunk_words)
        cursor_end  = self._llm_word_cursor + len(chunk_words)

        self._llm_bg_running = True
        threading.Thread(
            target=self._llm_chunk_worker,
            args=(chunk_text, cursor_end, self._llm_mode, self._llm_model,
                  self._translate_lang),
            daemon=True,
        ).start()

    def _llm_chunk_worker(
        self,
        text       : str,
        cursor_end : int,
        mode       : str,
        model_name : str | None,
        lang       : str | None,
    ):
        """
        Runs on a daemon thread.  Processes one chunk and appends to the log.
        Updates _llm_word_cursor on success so the next pass continues from here.
        """
        try:
            from spoaken_llm import translate, summarize_llm, is_ollama_running
            if not is_ollama_running():
                return

            if mode == "summarize":
                result   = summarize_llm(text, model=model_name)
                log_path = str(LOG_DIR / "llm_summary.txt")
                label    = "[LLM Summary chunk]"
            elif mode == "translate":
                target = lang or "english"
                result  = translate(text, target_lang=target, model=model_name)
                log_path = str(LOG_DIR / "llm_translation.txt")
                label    = f"[LLM Translation chunk → {target}]"
            else:
                return

            if result and result.strip():
                ts    = time.strftime("%Y-%m-%d %H:%M:%S")
                entry = (
                    f"\n── {label}  {ts} ─────────────────\n"
                    f"{result.strip()}\n"
                )
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(entry)
                # Advance cursor only on success — failed chunks are retried next pass
                self._llm_word_cursor = cursor_end
                self.view.thread_safety_console(
                    f"{label}: +{len(text.split())} words → "
                    f"{os.path.basename(log_path)}"
                )

        except Exception as exc:
            self.view.thread_safety_console(f"[LLM Chunk]: {exc}")
        finally:
            self._llm_bg_running = False

    # ─────────────────────────────────────────────────────────────────────────
    # Full-transcript LLM flush (session end)
    # ─────────────────────────────────────────────────────────────────────────

    def flush_llm_full(self, mode: str | None = None, lang: str | None = None):
        """
        Process the ENTIRE remaining transcript (from _llm_word_cursor to end)
        in one Ollama call.  Called when recording stops so all un-chunked text
        is handled while CPU is no longer shared with Vosk/Whisper.

        If the transcript is very long and the system is weak, falls back to
        the extractive summariser for safety.

        mode: override self._llm_mode (useful for on-close dialog).
        lang: override self._translate_lang.
        """
        mode = mode or self._llm_mode
        lang = lang or self._translate_lang

        if not mode:
            return

        all_words    = " ".join(self.model.data_store).split()
        unseen_words = all_words[self._llm_word_cursor:]

        if len(unseen_words) < 10:
            return   # nothing meaningful left

        full_text = " ".join(unseen_words)
        cursor_end = len(all_words)

        def _run():
            try:
                from spoaken_llm import translate, summarize_llm, is_ollama_running
                from spoaken_summarize import summarize as _ext

                if mode == "summarize":
                    log_path = str(LOG_DIR / "llm_summary.txt")
                    label    = "[LLM Summary — session end]"
                    if is_ollama_running():
                        result = summarize_llm(full_text, model=self._llm_model)
                    else:
                        result = _ext(full_text)
                        label  = "[Extractive Summary — session end]"

                elif mode == "translate":
                    target   = lang or "english"
                    log_path = str(LOG_DIR / "llm_translation.txt")
                    label    = f"[LLM Translation — session end → {target}]"
                    if is_ollama_running():
                        result = translate(full_text, target_lang=target,
                                           model=self._llm_model)
                    else:
                        result = full_text  # can't translate without Ollama
                        label  = "[Translation skipped — Ollama offline]"
                else:
                    return

                if result and result.strip():
                    ts    = time.strftime("%Y-%m-%d %H:%M:%S")
                    entry = (
                        f"\n══ {label}  {ts} ══════════════════\n"
                        f"{result.strip()}\n"
                    )
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(entry)
                    self._llm_word_cursor = cursor_end
                    self.view.thread_safety_console(
                        f"{label}: saved → {os.path.basename(log_path)}"
                    )

            except Exception as exc:
                self.view.thread_safety_console(f"[LLM Flush]: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Duplicate filter
    # ─────────────────────────────────────────────────────────────────────────

    def _is_duplicate(self, text: str) -> bool:
        if not DUPLICATE_FILTER:
            return False
        norm = text.strip().lower()
        if not norm:
            return True
        for prev in self._recent_texts:
            if norm == prev:
                return True
            words_new  = set(norm.split())
            words_prev = set(prev.split())
            if not words_new or not words_prev:
                continue
            overlap = len(words_new & words_prev) / max(len(words_new), len(words_prev))
            if overlap > 0.82:
                return True
        return False

    def _register_text(self, text: str):
        self._recent_texts.append(text.strip().lower())

    # ─────────────────────────────────────────────────────────────────────────
    # Command parser
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_command(self, text: str) -> bool:
        if self._cmd_parser is None:
            return self._parse_command_fallback(text)
        handled, output = self._cmd_parser.parse(text)
        if handled and output:
            self.view.thread_safety_console(output)
        return handled

    def _parse_command_fallback(self, text: str) -> bool:
        t = text.strip().lower()
        m = re.match(r"spoaken\.translate\(\s*(.+?)\s*\)", t)
        if m:
            lang = m.group(1).strip()
            self._translate_lang = None if lang in ("off", "stop", "none") else lang
            return True
        return False

    def _maybe_translate(self, text: str) -> str:
        if not self._translate_lang:
            return text
        lang    = self._translate_lang
        use_llm = self._llm_enabled
        model   = self._llm_model

        if use_llm:
            try:
                from spoaken_llm import translate, is_ollama_running
                if is_ollama_running():
                    result = translate(text, lang, model=model)
                    if result and result != text:
                        return f"[{lang}] {result}"
            except Exception as exc:
                self.view.thread_safety_console(
                    f"[LLM Translate]: failed — {exc}  (falling back to deep-translator)"
                )

        result = translate_text(text, lang)
        if result:
            return f"[{lang}] {result}"
        self.view.thread_safety_console(
            "[Translate Warning]: deep-translator not installed or network error.\n"
            "  Fix:  pip install deep-translator"
        )
        return text

    # ─────────────────────────────────────────────────────────────────────────
    # Memory cap
    # ─────────────────────────────────────────────────────────────────────────

    def _check_memory_cap(self):
        word_count = sum(len(s.split()) for s in self.model.data_store)
        time_cap   = False
        if self._session_start:
            elapsed  = (time.time() - self._session_start) / 60
            time_cap = elapsed >= MEMORY_CAP_MINUTES

        if (word_count >= MEMORY_CAP_WORDS or time_cap) and not self._polishing:
            reason = (
                f"{word_count} words" if word_count >= MEMORY_CAP_WORDS
                else f"{int((time.time()-self._session_start)/60)} min"
            )
            self.view.thread_safety_console(
                f"[Console]: memory cap hit ({reason}) — auto-polish running"
            )
            self._session_start = time.time()
            threading.Thread(target=self.polish_and_display, daemon=True).start()

        # Trigger incremental LLM chunk if enough new content has accumulated
        self._maybe_trigger_llm_chunk()

    # ─────────────────────────────────────────────────────────────────────────
    # Recording toggle
    # ─────────────────────────────────────────────────────────────────────────

    def toggle_recording(self):
        if not self.model.is_running:
            if not messagebox.askokcancel("Spoaken", "Allow microphone access?"):
                return

            self._session_start   = time.time()
            self.model.is_running = True

            for q in (self.model.vosk_queue, self.model.whisper_queue):
                while not q.empty():
                    try: q.get_nowait()
                    except Exception: pass
            reset_vad()   # clear VAD state from any previous session

            self.view.update_console("[Console]: recording started")
            self.view.update_status("RECORDING", STA_REC)
            self.view.set_waveform_state("recording")
            self.view.btn_start.configure(
                text="Stop Recording", fg_color="#c42828", hover_color="#e03535",
            )

            threading.Thread(target=self._audio_capture_loop, daemon=True).start()

            if VOSK_ENABLED and self.model.small_model is not None:
                threading.Thread(target=self.audio_stream_loop, daemon=True).start()

            if WHISPER_ENABLED and self.model.whisper_model is not None:
                threading.Thread(target=self.whisper_loop, daemon=True).start()

        else:
            self.model.is_running = False
            self.view.update_console("[Console]: recording stopped")
            self.view.update_status("IDLE", STA_IDLE)
            self.view.set_waveform_state("idle")
            self.view.btn_start.configure(
                text="Start Recording", fg_color="#1a5e2a", hover_color="#24883c",
            )
            # Recording stopped — flush any remaining unseen text to LLM
            if self._llm_enabled and self._llm_mode:
                self.view.thread_safety_console(
                    "[LLM]: flushing remaining transcript …"
                )
                self.flush_llm_full()

    # ─────────────────────────────────────────────────────────────────────────
    # Audio capture
    # ─────────────────────────────────────────────────────────────────────────

    def _audio_capture_loop(self):
        def _cb(indata, frames, time_info, status):
            raw   = maybe_suppress_noise(bytes(indata))
            gated = audio_gate(raw)   # None = silence, drop it
            if gated is not None:
                self.model.vosk_queue.put(gated)
                if WHISPER_ENABLED:
                    self.model.whisper_queue.put(gated)
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr ** 2))) / 32768.0
            self.view.after(0, self.view.push_audio_level, rms)

        try:
            with sd.RawInputStream(
                samplerate=_SAMPLE_RATE, blocksize=_BLOCK_SIZE,
                device=self._mic_device, dtype="int16", channels=1,
                callback=_cb,
            ):
                while self.model.is_running:
                    time.sleep(0.05)
        except Exception as exc:
            self.view.thread_safety_console(f"[Audio Error]: {exc}")
        finally:
            for q in (self.model.vosk_queue, self.model.whisper_queue):
                q.put(None)
            self.view.thread_safety_console("[Console]: audio stream closed")

    # ─────────────────────────────────────────────────────────────────────────
    # Vosk real-time loop
    #
    # Partials → GUI log only, never written to the target window.
    # Final sentences → GUI log + target window write (no backtracking).
    # ─────────────────────────────────────────────────────────────────────────

    def audio_stream_loop(self):
        self.view.thread_safety_console("[Console]: Vosk stream open")
        rec = self.model.get_fast_recognizer()
        last_partial   = ""
        partial_seg_id = None

        try:
            while self.model.is_running:
                data = self.model.vosk_queue.get(timeout=1.0)
                if data is None:
                    break

                if rec.AcceptWaveform(data):
                    # ── Final sentence ────────────────────────────────────────
                    text = json.loads(rec.Result()).get("text", "").strip()

                    if partial_seg_id is not None:
                        self.view.thread_safety_replace_segments(
                            [partial_seg_id], "", tag="partial"
                        )
                    last_partial   = ""
                    partial_seg_id = None

                    import spoaken_connect as _sc
                    if not _sc.VOSK_ACTIVE:
                        continue
                    if not text or self._is_duplicate(text):
                        continue
                    self._register_text(text)
                    if self._parse_command(text):
                        continue
                    text = self._maybe_translate(text)

                    seg_id = self._register_pending(text)
                    self.view.thread_safety_insert_pending(
                        f"\n[Vosk]: {text}\n", seg_id, tag="vosk"
                    )
                    if self.writing_status and self.writer:
                        self.writer.write(text + " ")

                    self.model.data_store.append(text)
                    self._broadcast(f"[Vosk] {text}")
                    with open(VOSK_LOG, "a", encoding="utf-8") as f:
                        f.write(f"[Final]: {text}\n")
                    self._check_memory_cap()

                else:
                    # ── Partial — GUI only ─────────────────────────────────────
                    partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                    if partial and partial != last_partial:
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
                        with open(VOSK_LOG, "a", encoding="utf-8") as f:
                            f.write(f"[Partial]: {partial}\n")

        except Exception as exc:
            self.view.thread_safety_console(f"[Vosk Error]: {exc}")
        finally:
            self.view.thread_safety_console("[Console]: Vosk stream closed")

    # ─────────────────────────────────────────────────────────────────────────
    # Whisper final transcription loop  (8-second chunks)
    # ─────────────────────────────────────────────────────────────────────────

    def whisper_loop(self):
        self.view.thread_safety_console("[Console]: Whisper engine active (8 s window)")
        buf          = b""
        chunk_bytes  = _SAMPLE_RATE * _WHISPER_SECS * 2
        last_whisper = ""

        def _flush(buffer: bytes):
            nonlocal last_whisper
            if not buffer:
                return
            import spoaken_connect as _sc
            if not _sc.WHISPER_ACTIVE:
                return
            text = self.model.transcribe_whisper(buffer)
            if not text or text == last_whisper or self._is_duplicate(text):
                return
            self._register_text(text)
            last_whisper = text
            if self._parse_command(text):
                return
            text = self._maybe_translate(text)

            seg_id = self._register_pending(text)
            self.view.thread_safety_insert_pending(
                f"\n[Whisper]: {text}\n", seg_id, tag="whisper"
            )
            self.model.whisper_store.append(text)
            self._broadcast(f"[Whisper] {text}")
            with open(WHISPER_LOG, "a", encoding="utf-8") as f:
                f.write(f"[Whisper]: {text}\n")
            self._check_memory_cap()

        try:
            while self.model.is_running:
                try:
                    data = self.model.whisper_queue.get(timeout=0.4)
                except Exception:
                    if len(buf) >= chunk_bytes:
                        _flush(buf)
                        buf = b""
                    continue
                if data is None:
                    break
                buf += data
                if len(buf) >= chunk_bytes:
                    _flush(buf)
                    buf = b""
            _flush(buf)
        except Exception as exc:
            self.view.thread_safety_console(f"[Whisper Error]: {exc}")
        finally:
            self.view.thread_safety_console("[Console]: Whisper engine stopped")

    # ─────────────────────────────────────────────────────────────────────────
    # Segment helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _register_pending(self, text: str) -> int:
        with self.pending_lock:
            seg_id = self.seg_counter
            self.seg_counter += 1
            chars = len(text) + 1
            self.pending_segments.append({"seg_id": seg_id, "raw": text, "window_chars": chars})
            self.total_window_chars += chars
        return seg_id

    def _pop_all_pending(self):
        with self.pending_lock:
            segs  = list(self.pending_segments)
            self.pending_segments.clear()
            total = sum(s["window_chars"] for s in segs)
            self.total_window_chars = max(0, self.total_window_chars - total)
        return segs, total

    # ─────────────────────────────────────────────────────────────────────────
    # Page writing
    # ─────────────────────────────────────────────────────────────────────────

    def toggle_page_writing(self):
        self.writing_status = not self.writing_status
        self.view.set_writing_btn(self.writing_status)

        if self.writing_status:
            target = self.view.ent_target.get().strip()
            if target:
                self.view.update_console(f"[Console]: locking writer to '{target}'")
                threading.Thread(target=self._init_writer, args=(target,), daemon=True).start()
            else:
                self.view.update_console("[Console]: writing to active window")
                self.writer = DirectWindowWriter("", self.view.thread_safety_console)
        else:
            self.view.update_console("[Console]: page writing OFF")
            self.writer         = None
            self._writer_locked = False
            self.view.after(0, self.view.update_lock_btn, False)

    def _init_writer(self, target: str):
        self.writer = DirectWindowWriter(target, self.view.thread_safety_console)
        locked = self.writer._backend is not None
        self._writer_locked = locked
        self.view.after(0, self.view.update_lock_btn, locked)

    def lock_writer_target(self):
        target = self.view.ent_target.get().strip()
        if not target:
            self.writer = None; self._writer_locked = False
            self.view.update_lock_btn(False)
            self.view.update_console("[Console]: writer target cleared")
            return
        self.view.update_console(f"[Console]: locking to '{target}'")
        threading.Thread(target=self._init_writer, args=(target,), daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Polish
    # ─────────────────────────────────────────────────────────────────────────

    def polish_and_display(self):
        if not self.model.data_store:
            self.view.update_console("[Console]: nothing to polish yet")
            return
        if self._polishing:
            self.view.update_console("[Console]: polish already running …")
            return
        self._polishing = True
        self.view.update_console("[Console]: polishing transcript …")

        def _run():
            try:
                raw_text, final_text = self.model.run_polish()
                self.model.data_store = []
                with open(FINAL_LOG, "a", encoding="utf-8") as f:
                    f.write("── POLISHED ──────────────────────────────\n")
                    f.write(f"{final_text}\n\n")
                self.view.thread_safety_console(
                    "[Console]: polish saved to final_session_log.txt"
                )
            except Exception as exc:
                self.view.thread_safety_console(f"[Polish Error]: {exc}")
            finally:
                self._polishing = False

        threading.Thread(target=_run, daemon=True).start()

    def swap_polishing(self):
        if not self._polishing:
            self.polish_and_display()
            self.view.btn_polish.configure(
                text="Polishing…", fg_color="#090b66", hover_color="#0c0f92"
            )
        else:
            self._polishing = False
            self.view.btn_polish.configure(
                text="Paused…", fg_color="#1041a5", hover_color="#0143cb"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Model hot-swap
    # ─────────────────────────────────────────────────────────────────────────

    def _stop_if_running(self) -> bool:
        if self.model.is_running:
            self.model.is_running = False
            self.view.update_status("IDLE", STA_IDLE)
            self.view.set_waveform_state("idle")
            self.view.btn_start.configure(
                text="Start Recording", fg_color="#1a5e2a", hover_color="#24883c",
            )
            time.sleep(0.35)
            return True
        return False

    def swap_vosk_model(self, model_name: str):
        if model_name.startswith("("):
            return
        def _do():
            was_running = self._stop_if_running()
            self.view.thread_safety_console(f"[Console]: loading Vosk → {model_name} …")
            ok = self.model.reload_vosk(model_name)
            if ok:
                self.view.thread_safety_console(f"[Console]: Vosk model active: {model_name}")
            else:
                self.view.thread_safety_console(
                    f"[Console]: Vosk swap failed — '{model_name}' not found."
                )
            if was_running:
                self.view.thread_safety_console("[Console]: recording was stopped to swap model")
        threading.Thread(target=_do, daemon=True).start()

    def swap_whisper_model(self, model_name: str):
        if model_name.startswith("("):
            return
        def _do():
            was_running = self._stop_if_running()
            self.view.thread_safety_console(f"[Console]: loading Whisper → {model_name} …")
            self.view.thread_safety_waveform("correcting")
            ok = self.model.reload_whisper(model_name)
            self.view.thread_safety_waveform("idle")
            if ok:
                self.view.thread_safety_console(f"[Console]: Whisper model active: {model_name}")
            else:
                self.view.thread_safety_console(
                    f"[Console]: Whisper swap failed — check model is downloaded."
                )
            if was_running:
                self.view.thread_safety_console("[Console]: recording was stopped to swap model")
        threading.Thread(target=_do, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Log management
    # ─────────────────────────────────────────────────────────────────────────

    def clear_all_logs(self):
        self.view.update_console("[Console]: clearing logs …")
        for path in (VOSK_LOG, WHISPER_LOG, FINAL_LOG):
            try: open(path, "w", encoding="utf-8").close()
            except Exception: pass
        try:
            t = self.view.log._textbox
            t.configure(state="normal"); t.delete("1.0", "end")
            t.configure(state="disabled")
        except Exception:
            pass
        self.model.data_store    = []
        self.model.whisper_store = []
        self._recent_texts.clear()
        self._llm_word_cursor = 0
        with self.pending_lock:
            self.pending_segments.clear()
            self.total_window_chars = 0
        self.view.update_console("[Console]: logs cleared")

    def copy_transcript(self):
        try:
            t    = self.view.log._textbox
            text = t.get("1.0", "end").strip()
            self.view.clipboard_clear()
            self.view.clipboard_append(text)
            self.view.update_console("[Console]: transcript copied to clipboard")
        except Exception as exc:
            self.view.update_console(f"[Copy Error]: {exc}")

    def open_logs(self):
        self.view.update_console("[Console]: opening log files …")
        sys_ = platform.system()
        for path in (VOSK_LOG, WHISPER_LOG, FINAL_LOG):
            if not os.path.exists(path):
                open(path, "w", encoding="utf-8").close()
            try:
                if sys_ == "Windows":
                    os.startfile(path)
                elif sys_ == "Darwin":
                    subprocess.call(["open", path])
                else:
                    subprocess.call(["xdg-open", path])
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Chat / SSE
    # ─────────────────────────────────────────────────────────────────────────

    def chat_send(self, message: str):
        if self._chat_server:
            self._chat_server.send(message)

    def toggle_chat_port(self):
        if self._chat_server is None:
            return
        if self._chat_server.is_open():
            self._chat_server.stop()
            self.view.after(0, self.view.update_chat_port_btn, False)
        else:
            ok = self._chat_server.start()
            self.view.after(0, self.view.update_chat_port_btn, ok)

    def _broadcast(self, text: str):
        if self._chat_server: self._chat_server.send(text)
        if self._sse_server:  self._sse_server.push(text)

    def _on_chat_message(self, sender_ip: str, message: str):
        self.view.after(0, self.view.chat_receive, f"[{sender_ip}]: {message}")

    def toggle_sse_server(self):
        if self._sse_server is None:
            return
        if self._sse_server.is_open():
            self._sse_server.stop()
        else:
            self._sse_server.start()

    # ─────────────────────────────────────────────────────────────────────────
    # Close dialog
    # ─────────────────────────────────────────────────────────────────────────

    def on_close_request(self):
        self.view.update_console("[Console]: close requested")
        self._show_close_dialog()

    def _show_close_dialog(self):
        import tkinter as tk
        import customtkinter as ctk

        BG_D = "#060c1a"; BG_P = "#0a1128"; BOR = "#1a2d60"
        TC   = "#00bdff"; TT   = "#00e5cc"; TD  = "#2a6080"

        dlg = ctk.CTkToplevel(self.view)
        dlg.title("Close Spoaken")
        dlg.resizable(False, False)
        dlg.configure(fg_color=BG_D)
        dlg.grab_set()
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

        dlg.update_idletasks()
        x = self.view.winfo_x() + (self.view.winfo_width()  - 360) // 2
        y = self.view.winfo_y() + (self.view.winfo_height() - 220) // 2
        dlg.geometry(f"360x220+{x}+{y}")

        frm = ctk.CTkFrame(dlg, fg_color=BG_P,
                           border_color=BOR, border_width=1, corner_radius=10)
        frm.pack(fill="both", expand=True, padx=6, pady=6)

        ctk.CTkLabel(frm, text="◈  Close Spoaken",
                     font=("Segoe UI Semibold", 13), text_color=TT,
                     ).pack(pady=(16, 4))
        ctk.CTkLabel(frm, text="What would you like to do with the transcript?",
                     font=("Segoe UI", 10), text_color=TD,
                     ).pack(pady=(0, 12))

        choice = tk.StringVar(value="")
        btn_cfg = dict(width=300, height=36, corner_radius=7, font=("Segoe UI", 11))

        def _pick(val):
            choice.set(val); dlg.destroy()

        ctk.CTkButton(frm, text="📝  Summarise transcript",
                      fg_color="#0d3a40", hover_color="#145060",
                      text_color=TT, **btn_cfg,
                      command=lambda: _pick("summarize")).pack(pady=3)
        ctk.CTkButton(frm, text="✏  Polish / grammar correct",
                      fg_color="#1a3a5e", hover_color="#2450a0",
                      text_color=TC, **btn_cfg,
                      command=lambda: _pick("polish")).pack(pady=3)
        ctk.CTkButton(frm, text="✕  Close without altering",
                      fg_color="#3a1010", hover_color="#661a1a",
                      text_color="#e07070", **btn_cfg,
                      command=lambda: _pick("nothing")).pack(pady=3)

        self.view.wait_window(dlg)
        self._handle_close_choice(choice.get())

    def _handle_close_choice(self, choice: str):
        if choice in ("nothing", ""):
            self.model.is_running = False
            self.view.destroy()
            return

        self.model.is_running = False

        if choice == "summarize":
            def _run():
                try:
                    # Full LLM flush first (session just ended — CPU is free)
                    self.flush_llm_full(mode="summarize")
                    # Also run extractive as fallback / parallel log entry
                    from spoaken_summarize import summarize
                    raw_text = " ".join(self.model.data_store).strip()
                    if raw_text:
                        summary = summarize(raw_text)
                        with open(FINAL_LOG, "a", encoding="utf-8") as f:
                            f.write("── SUMMARY ───────────────────────────────\n")
                            f.write(f"{summary}\n\n")
                        self.view.thread_safety_console(
                            "[Console]: summary saved → final_session_log.txt"
                        )
                    else:
                        self.view.thread_safety_console("[Console]: nothing to summarise")
                except Exception as exc:
                    self.view.thread_safety_console(f"[Summarise Error]: {exc}")
                finally:
                    self.view.after(0, self.view.destroy)
            threading.Thread(target=_run, daemon=True).start()

        elif choice == "polish":
            def _run():
                try:
                    raw_text, final_text = self.model.run_polish()
                    with open(FINAL_LOG, "a", encoding="utf-8") as f:
                        f.write("── FINAL SESSION ──────────────────────\n")
                        f.write(f"RAW:\n{raw_text}\n\nPOLISHED:\n{final_text}\n\n")
                    self.model.data_store = []
                    self.view.thread_safety_console("[Console]: final log written")
                except Exception as exc:
                    self.view.thread_safety_console(f"[Close Error]: {exc}")
                finally:
                    self.view.after(0, self.view.destroy)
            threading.Thread(target=_run, daemon=True).start()
