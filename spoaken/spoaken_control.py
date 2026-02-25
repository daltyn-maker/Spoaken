"""
spoaken_control.py
──────────────────
Controller layer — glues the model and view together.

What's new vs v1
────────────────
  • pyaudio → sounddevice (cross-platform, no .whl gymnastics)
  • Dual transcription: Vosk real-time + Whisper final (separate logs)
  • Duplicate-text filter (configurable similarity threshold)
  • Memory cap: auto-polish at N words OR T minutes (both configurable)
  • Microphone device selection forwarded from GUI
  • Noise suppression toggle forwarded from GUI
  • spoaken.translate(lang) / spoaken.translate(off) command
  • Lock button ↔ Unlock button synchronised with writer state
  • Chat server: optional TCP broadcast server for LAN peers
  • Android stream: optional HTTP SSE server for browser clients
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
    ENABLE_GIGA_MODEL,
    MIC_DEVICE, NOISE_SUPPRESSION,
    MEMORY_CAP_WORDS, MEMORY_CAP_MINUTES,
    DUPLICATE_FILTER,
    VOSK_ENABLED, WHISPER_ENABLED,
    CHAT_SERVER_ENABLED, CHAT_SERVER_PORT, CHAT_SERVER_TOKEN,
    ANDROID_STREAM_ENABLED, ANDROID_STREAM_PORT,
)
from spoaken_connect import maybe_suppress_noise, translate_text
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
_SAMPLE_RATE   = 16000
_BLOCK_SIZE    = 800     # ~50 ms frames
_WHISPER_SECS  = 4       # accumulate this many seconds before sending to Whisper


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
        self._mic_device = MIC_DEVICE   # None = system default

        # Memory cap
        self._session_start = None

        # Duplicate filter (ring buffer of recent normalised phrases)
        self._recent_texts: deque = deque(maxlen=30)

        # Translation
        self._translate_lang: str | None = None

        # LLM (Ollama) integration
        self._llm_enabled : bool       = False
        self._llm_mode    : str | None = None   # None | "translate" | "summarize"
        self._llm_model   : str | None = None   # None = auto-pick

        # Chat server and SSE server objects (created in set_objects)
        self._chat_server : ChatServer | None = None
        self._sse_server  : SSEServer  | None = None

        # Command parser — wired up in set_objects after view is available
        self._cmd_parser  : CommandParser | None = None

    # ─────────────────────────────────────────────────────────────────────────
    # Initialisation
    # ─────────────────────────────────────────────────────────────────────────

    def set_objects(self, model, view):
        self.model = model
        self.view  = view
        self._ensure_logs()
        self._check_display_server()

        # Build the chat server (port is closed until user toggles it on)
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

        # Sync the Port button with the initial server state
        if CHAT_SERVER_ENABLED:
            self.view.after(100, self.view.update_chat_port_btn, True)

        # Build the command parser now that both model and view are live
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
    # Microphone
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
        """
        Enable or disable a transcription engine at runtime.
        engine : "vosk" | "whisper"
        """
        import spoaken_connect as _sc
        if engine == "vosk":
            _sc.VOSK_ACTIVE = enabled
        elif engine == "whisper":
            _sc.WHISPER_ACTIVE = enabled

    def set_llm_enabled(self, enabled: bool):
        """Enable / disable the LLM translation/summarization pipeline."""
        self._llm_enabled = enabled

    def set_llm_mode(self, mode, model: str = None):
        """
        Set LLM post-processing mode.
        mode  : None | "translate" | "summarize"
        model : Ollama model name override (None = auto)
        """
        self._llm_mode  = mode
        if model:
            self._llm_model = model

    def set_llm_model(self, model: str):
        """Switch the active Ollama model."""
        self._llm_model = model

    def run_summarize(self, text: str = None) -> str:
        """
        Summarize the current transcript (or provided text).
        Uses LLM if enabled and Ollama is running, else extractive fallback.
        """
        src = text or " ".join(self.model.data_store)
        if not src.strip():
            return "Nothing to summarise yet."
        try:
            from spoaken_llm import summarize_llm, ensure_ollama_pkg
            ensure_ollama_pkg(log_fn=self.view.thread_safety_console)
            result = summarize_llm(src, model=getattr(self, "_llm_model", None))
        except Exception:
            from spoaken_summarize import summarize as _ext
            result = _ext(src)
        self.view.thread_safety_console(f"[Summary]:\n{result}")
        return result


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
    # Command parser  — delegates to spoaken_commands.CommandParser
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_command(self, text: str) -> bool:
        """
        Route text through the CommandParser.
        Returns True if text was a recognised command (suppress from transcript).
        Output messages are forwarded to the console automatically.
        """
        if self._cmd_parser is None:
            # Fallback: handle translate inline before parser is ready
            return self._parse_command_fallback(text)

        handled, output = self._cmd_parser.parse(text)
        if handled and output:
            self.view.thread_safety_console(output)
        return handled

    def _parse_command_fallback(self, text: str) -> bool:
        """Minimal fallback used only before CommandParser is initialised."""
        import re as _re
        t = text.strip().lower()
        m = _re.match(r"spoaken\.translate\(\s*(.+?)\s*\)", t)
        if m:
            lang = m.group(1).strip()
            if lang in ("off", "stop", "none"):
                self._translate_lang = None
            else:
                self._translate_lang = lang
            return True
        return False

    def _maybe_translate(self, text: str) -> str:
        if not self._translate_lang:
            return text

        lang    = self._translate_lang
        use_llm = getattr(self, "_llm_enabled", False)
        model   = getattr(self, "_llm_model", None)

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

        # Legacy / fallback path
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
            elapsed = (time.time() - self._session_start) / 60
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

    # ─────────────────────────────────────────────────────────────────────────
    # Recording toggle
    # ─────────────────────────────────────────────────────────────────────────

    def toggle_recording(self):
        if not self.model.is_running:
            if not messagebox.askokcancel("Spoaken", "Allow microphone access?"):
                return

            self._session_start   = time.time()
            self.model.is_running = True

            # Drain stale queue data
            for q in (self.model.vosk_queue, self.model.whisper_queue,
                      self.model.giga_queue):
                while not q.empty():
                    try: q.get_nowait()
                    except Exception: pass

            self.view.update_console("[Console]: recording started")
            self.view.update_status("RECORDING", STA_REC)
            self.view.set_waveform_state("recording")
            self.view.btn_start.configure(
                text="Stop Recording",
                fg_color="#c42828",
                hover_color="#e03535",
            )

            # Audio capture — single stream, callback fans out to all queues
            threading.Thread(
                target=self._audio_capture_loop, daemon=True
            ).start()

            if VOSK_ENABLED and self.model.small_model is not None:
                threading.Thread(
                    target=self.audio_stream_loop, daemon=True
                ).start()

            if WHISPER_ENABLED and self.model.whisper_model is not None:
                threading.Thread(
                    target=self.whisper_loop, daemon=True
                ).start()

            if ENABLE_GIGA_MODEL:
                threading.Thread(
                    target=self.accuracy_process_loop, daemon=True
                ).start()

        else:
            self.model.is_running = False
            self.view.update_console("[Console]: recording stopped")
            self.view.update_status("IDLE", STA_IDLE)
            self.view.set_waveform_state("idle")
            self.view.btn_start.configure(
                text="Start Recording",
                fg_color="#1a5e2a",
                hover_color="#24883c",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Audio capture  (sounddevice — feeds all consumer queues)
    # ─────────────────────────────────────────────────────────────────────────

    def _audio_capture_loop(self):
        """
        Opens a single sounddevice RawInputStream.
        The callback pushes denoised int16 PCM bytes into every consumer queue
        so each transcription thread gets its own independent copy.
        """
        def _cb(indata, frames, time_info, status):
            raw = maybe_suppress_noise(bytes(indata))
            self.model.vosk_queue.put(raw)
            if WHISPER_ENABLED:
                self.model.whisper_queue.put(raw)
            if ENABLE_GIGA_MODEL:
                self.model.giga_queue.put(raw)
            # Broadcast raw level to waveform (use RMS)
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(arr ** 2))) / 32768.0
            self.view.after(0, self.view.push_audio_level, rms)

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
        except Exception as exc:
            self.view.thread_safety_console(f"[Audio Error]: {exc}")
        finally:
            # Poison pills to unblock consumer threads
            for q in (self.model.vosk_queue, self.model.whisper_queue,
                      self.model.giga_queue):
                q.put(None)
            self.view.thread_safety_console("[Console]: audio stream closed")

    # ─────────────────────────────────────────────────────────────────────────
    # Vosk real-time loop
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
                    text = json.loads(rec.Result()).get("text", "").strip()
                    last_partial   = ""
                    partial_seg_id = None

                    import spoaken_connect as _sc_mod
                    if not _sc_mod.VOSK_ACTIVE:
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
                    self.model.data_store.append(text)
                    self._broadcast(f"[Vosk] {text}")
                    self._check_memory_cap()
                    # Full sentences are stored in data_store/transcript only;
                    # partials are what we write to the log file (see below).

                else:
                    partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                    if partial and partial != last_partial:
                        # Real-time partial update — show as live log line
                        if partial_seg_id is None:
                            partial_seg_id = self._register_pending(partial)
                            self.view.thread_safety_insert_pending(
                                f"[…] {partial}", partial_seg_id, tag="partial"
                            )
                        else:
                            self.view.thread_safety_replace_segments(
                                [partial_seg_id], f"[…] {partial}", tag="partial"
                            )

                        # Log partials to file (not full sentences)
                        with open(VOSK_LOG, "a", encoding="utf-8") as f:
                            f.write(f"[Partial]: {partial}\n")

                        if self.writing_status and self.writer:
                            if partial.startswith(last_partial):
                                self.writer.write(partial[len(last_partial):])
                            else:
                                self.writer.backspace(len(last_partial))
                                self.writer.write(partial)

                        last_partial = partial

        except Exception as exc:
            self.view.thread_safety_console(f"[Vosk Error]: {exc}")
        finally:
            self.view.thread_safety_console("[Console]: Vosk stream closed")

    # ─────────────────────────────────────────────────────────────────────────
    # Whisper final transcription loop
    # ─────────────────────────────────────────────────────────────────────────

    def whisper_loop(self):
        self.view.thread_safety_console("[Console]: Whisper engine active")
        buf         = b""
        chunk_bytes = _SAMPLE_RATE * _WHISPER_SECS * 2   # int16 = 2 bytes/sample
        last_whisper = ""

        def _flush(buffer: bytes):
            nonlocal last_whisper
            if not buffer:
                return
            import spoaken_connect as _sc_mod
            if not _sc_mod.WHISPER_ACTIVE:
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
    # Giga Vosk + T5 accuracy loop
    # ─────────────────────────────────────────────────────────────────────────

    def accuracy_process_loop(self):
        if not ENABLE_GIGA_MODEL:
            return

        if not self.model.giga_model_status:
            self.view.thread_safety_console("[Console]: loading giga Vosk + T5 …")
            self.view.thread_safety_waveform("correcting")
            self.model._background_load()
            self.model.giga_model_status = True
            self.view.thread_safety_console("[Console]: giga + T5 ready — corrections active")

        if self.model.is_running:
            self.view.thread_safety_status("RECORDING", STA_REC)
            self.view.thread_safety_waveform("recording")

        buf            = []
        rec_giga       = self.model.get_accurate_recognizer()
        giga_seg_id    = None
        last_giga_text = ""

        while self.model.is_running:
            try:
                data = self.model.giga_queue.get(timeout=1.0)
            except Exception:
                continue
            if data is None:
                break

            buf.append(data)

            if len(buf) % 5 == 0:
                rec_giga.AcceptWaveform(b"".join(buf[-5:]))
                partial = json.loads(rec_giga.PartialResult()).get("partial", "")
                if partial and partial != last_giga_text:
                    if giga_seg_id is None:
                        giga_seg_id = self._register_pending(partial)
                        self.view.thread_safety_insert_pending(
                            f"[Giga]: {partial}", giga_seg_id, tag="vosk"
                        )
                    else:
                        self.view.thread_safety_replace_segments(
                            [giga_seg_id], f"[Giga]: {partial}", tag="vosk"
                        )
                    last_giga_text = partial

            if len(buf) >= 20:
                rec_giga.AcceptWaveform(b"".join(buf))
                text   = json.loads(rec_giga.Result()).get("text", "")
                buf    = []
                giga_seg_id    = None
                last_giga_text = ""

                segs, total_chars = self._pop_all_pending()
                if not segs:
                    if text:
                        self.model.data_store.append(text)
                    continue

                seg_ids = [s["seg_id"] for s in segs]
                if text:
                    self.view.thread_safety_replace_segments(
                        seg_ids, f"[Corrected]: {text}\n", tag="whisper"
                    )
                    if self.writing_status and self.writer and total_chars > 0:
                        threading.Thread(
                            target=self._window_correct,
                            args=(total_chars, text), daemon=True,
                        ).start()
                    self.model.data_store.append(text)
                    with open(VOSK_LOG, "a", encoding="utf-8") as f:
                        f.write(f"[Corrected]: {text}\n")
                else:
                    raw = " ".join(s["raw"] for s in segs)
                    self.view.thread_safety_replace_segments(
                        seg_ids, f"[Fast]: {raw}\n", tag="vosk"
                    )
                    self.model.data_store.append(raw)

    def _window_correct(self, backspaces: int, corrected_text: str):
        time.sleep(0.12)
        if self.writer:
            self.writer.backspace(backspaces)
            time.sleep(0.05)
            self.writer.write(corrected_text)

    # ─────────────────────────────────────────────────────────────────────────
    # Segment helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _register_pending(self, text: str) -> int:
        with self.pending_lock:
            seg_id = self.seg_counter
            self.seg_counter += 1
            chars = len(text) + 1
            self.pending_segments.append({
                "seg_id": seg_id, "raw": text, "window_chars": chars
            })
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
                threading.Thread(
                    target=self._init_writer, args=(target,), daemon=True
                ).start()
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
            self.writer         = None
            self._writer_locked = False
            self.view.update_lock_btn(False)
            self.view.update_console("[Console]: writer target cleared")
            return
        self.view.update_console(f"[Console]: locking to '{target}'")
        threading.Thread(
            target=self._init_writer, args=(target,), daemon=True
        ).start()

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
                self.view.thread_safety_console("[Console]: polish saved to final_session_log.txt")
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
        """Stop recording if active. Returns True if we had to stop it."""
        if self.model.is_running:
            self.model.is_running = False
            self.view.update_status("IDLE", STA_IDLE)
            self.view.set_waveform_state("idle")
            self.view.btn_start.configure(
                text="Start Recording",
                fg_color="#1a5e2a",
                hover_color="#24883c",
            )
            time.sleep(0.35)   # let audio threads drain
            return True
        return False

    def swap_vosk_model(self, model_name: str):
        """
        Hot-swap the Vosk model. Stops recording first if needed.
        Called from the GUI dropdown callback.
        """
        if model_name.startswith("("):
            return   # placeholder entry — no-op

        def _do():
            was_running = self._stop_if_running()
            self.view.thread_safety_console(f"[Console]: loading Vosk → {model_name} …")
            ok = self.model.reload_vosk(model_name)
            if ok:
                self.view.thread_safety_console(f"[Console]: Vosk model active: {model_name}")
            else:
                self.view.thread_safety_console(
                    f"[Console]: Vosk swap failed — '{model_name}' not found.\n"
                    f"  Expected in: {__import__('paths').VOSK_DIR}"
                )
            if was_running:
                self.view.thread_safety_console("[Console]: recording was stopped to swap model")

        threading.Thread(target=_do, daemon=True).start()

    def swap_whisper_model(self, model_name: str):
        """
        Hot-swap the Whisper model. Stops recording first if needed.
        Called from the GUI dropdown callback.
        """
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
                    f"[Console]: Whisper swap failed — check model is downloaded.\n"
                    f"  Re-run installer with model '{model_name}' selected."
                )
            if was_running:
                self.view.thread_safety_console("[Console]: recording was stopped to swap model")

        threading.Thread(target=_do, daemon=True).start()



    def clear_all_logs(self):
        self.view.update_console("[Console]: clearing logs …")
        for path in (VOSK_LOG, WHISPER_LOG, FINAL_LOG):
            try: open(path, "w", encoding="utf-8").close()
            except Exception: pass
        try:
            t = self.view.log._textbox
            t.configure(state="normal")
            t.delete("1.0", "end")
            t.configure(state="disabled")
        except Exception:
            pass
        self.model.data_store    = []
        self.model.whisper_store = []
        self._recent_texts.clear()
        with self.pending_lock:
            self.pending_segments.clear()
            self.total_window_chars = 0
        self.view.update_console("[Console]: logs cleared")

    def copy_transcript(self):
        """Copy full transcript text to the system clipboard."""
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
    # Chat server  (TCP broadcast — LAN peers / other Spoaken instances)
    # ─────────────────────────────────────────────────────────────────────────

    def chat_send(self, message: str):
        """Broadcast a message to all connected chat peers."""
        if self._chat_server:
            self._chat_server.send(message)

    def toggle_chat_port(self):
        """
        Toggle the TCP chat server port open or closed.
        Called by the 'Port: ON/OFF' button in the sidebar.
        """
        if self._chat_server is None:
            return

        if self._chat_server.is_open():
            self._chat_server.stop()
            self.view.after(0, self.view.update_chat_port_btn, False)
        else:
            ok = self._chat_server.start()
            self.view.after(0, self.view.update_chat_port_btn, ok)

    def _broadcast(self, text: str):
        """Broadcast a transcription line to chat peers and SSE clients."""
        if self._chat_server:
            self._chat_server.send(text)
        if self._sse_server:
            self._sse_server.push(text)

    def _on_chat_message(self, sender_ip: str, message: str):
        """Called by ChatServer when a peer sends a message."""
        self.view.after(0, self.view.chat_receive, f"[{sender_ip}]: {message}")

    # ─────────────────────────────────────────────────────────────────────────
    # Android / browser SSE server
    # ─────────────────────────────────────────────────────────────────────────

    def toggle_sse_server(self):
        """Toggle the SSE broadcast server on/off."""
        if self._sse_server is None:
            return
        if self._sse_server.is_open():
            self._sse_server.stop()
        else:
            self._sse_server.start()

    # ─────────────────────────────────────────────────────────────────────────
    # Close
    # ─────────────────────────────────────────────────────────────────────────

    def on_close_request(self):
        self.view.update_console("[Console]: close requested")
        self._show_close_dialog()

    def _show_close_dialog(self):
        """
        Three-option close dialog: Summarise / Polish / Nothing.
        """
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
        ctk.CTkLabel(frm,
                     text="What would you like to do with the transcript?",
                     font=("Segoe UI", 10), text_color=TD,
                     ).pack(pady=(0, 12))

        choice = tk.StringVar(value="")

        btn_cfg = dict(width=300, height=36, corner_radius=7,
                       font=("Segoe UI", 11))

        def _pick(val):
            choice.set(val)
            dlg.destroy()

        ctk.CTkButton(frm, text="📝  Summarise transcript",
                      fg_color="#0d3a40", hover_color="#145060",
                      text_color=TT, **btn_cfg,
                      command=lambda: _pick("summarize"),
                      ).pack(pady=3)
        ctk.CTkButton(frm, text="✏  Polish / grammar correct",
                      fg_color="#1a3a5e", hover_color="#2450a0",
                      text_color=TC, **btn_cfg,
                      command=lambda: _pick("polish"),
                      ).pack(pady=3)
        ctk.CTkButton(frm, text="✕  Close without altering",
                      fg_color="#3a1010", hover_color="#661a1a",
                      text_color="#e07070", **btn_cfg,
                      command=lambda: _pick("nothing"),
                      ).pack(pady=3)

        self.view.wait_window(dlg)
        self._handle_close_choice(choice.get())

    def _handle_close_choice(self, choice: str):
        """Execute the chosen close action then destroy the window."""
        if choice in ("nothing", ""):
            self.model.is_running = False
            self.view.destroy()
            return

        self.model.is_running = False

        if choice == "summarize":
            def _run():
                try:
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
            
            
            
            
            
            
            
            
            
            
