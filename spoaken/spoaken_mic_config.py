"""
spoaken_mic_config.py
─────────────────────
Microphone configuration and audio tuning panel for Spoaken.

Opens as a CTkToplevel. Lets the user tune settings against their actual
environment (fan noise, classroom acoustics) before recording.

Features
────────
  • Live RMS level meter  — see what the mic is picking up in real time
  • VAD gate indicator    — SPEECH / SILENCE badge updates live
  • VAD aggressiveness / min-speech / silence-gap sliders
  • EQ / frequency profile presets:
      Flat        no filtering
      Speech      80 Hz high-pass  (removes fan/HVAC rumble)   ← default
      Aggressive  100 Hz HP + 60/120 Hz notch  (fan harmonics)
      Custom      manual high-pass cutoff
  • Noise profile capture — 2 s ambient sample for stationary NR
  • noisereduce strength slider
  • "Record 5 s test" — runs current settings through Vosk AND Whisper
    and shows word counts + transcription so you can compare before/after
  • Apply saves to spoaken_connect._mic_config immediately

Usage
─────
  from spoaken_mic_config import MicConfigPanel
  MicConfigPanel(parent_window, controller)
"""

from __future__ import annotations

import sys
import time
import threading
import numpy as np

import customtkinter as ctk
import sounddevice as sd

# ── Theme (matches Spoaken palette) ──────────────────────────────────────────
BG_DEEP   = "#060c1a"
BG_PANEL  = "#0a1128"
BG_CARD   = "#0d1735"
BG_INPUT  = "#0c1636"
BORDER    = "#1a2d60"
BORDER_ACTIVE  = "#2545a8"

COLOR_MAIN    = "#00bdff"
COLOR_TEAL    = "#00e5cc"
COLOR_DIM     = "#2a6080"
COLOR_WARN    = "#d4aa00"
COLOR_OK      = "#24c45e"
COLOR_ERR     = "#e03535"
COLOR_CONSOLE    = "#007bff"

FONT_TITLE   = ("Segoe UI Semibold", 13)
FONT_UI      = ("Segoe UI", 11)
FONT_SMALL      = ("Segoe UI", 9)
FONT_MONO    = ("Courier New", 10)

_SR = 16000


# ─────────────────────────────────────────────────────────────────────────────
class MicConfigPanel(ctk.CTkToplevel):

    def __init__(self, parent=None, controller=None):
        super().__init__(parent)
        self._ctrl = controller

        self.title("Spoaken — Microphone Setup")
        self.geometry("660x700")          # smaller initial height — scroll for the rest
        self.minsize(560, 480)
        self.configure(fg_color=BG_DEEP)
        self.resizable(True, True)

        # Runtime state
        self._stream        = None
        self._stream_lock   = threading.Lock()
        self._meter_rms     = 0.0
        self._gate_open     = False
        self._noise_profile = None
        self._capturing     = False
        self._testing       = False

        # Own VAD for the live meter (separate from the global one in connect)
        try:
            from spoaken_vad import VAD
            self._vad = VAD()
        except Exception:
            self._vad = None

        self._build_ui()
        self.after(50,  self._centre)
        self.after(300, self._start_meter)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Window layout:
        #   row 0 — fixed header banner
        #   row 1 — scrollable frame (cards + log)
        #   row 2 — fixed bottom action bar (Apply / Reset / Close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)   # only scroll area stretches
        self.grid_rowconfigure(2, weight=0)   # action bar stays at bottom

        self._build_header()   # row 0 — fixed

        # ── Scrollable content area ────────────────────────────────────────────
        self._scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color="#1a2d60",
            scrollbar_button_hover_color="#2545a8",
            corner_radius=0,
        )
        self._scroll_frame.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._scroll_frame.grid_columnconfigure(0, weight=1)

        # All cards are placed inside _scroll_frame
        self._build_device_row()     # card 0
        self._build_meter_section()  # card 1
        self._build_vad_section()    # card 2
        self._build_eq_section()     # card 3
        self._build_nr_section()     # card 4
        self._build_test_section()   # card 5
        self._build_log()            # card 6

        # ── Fixed bottom action bar (always visible, never scrolled) ──────────
        self._build_actions()        # row 2

    def _build_header(self):
        header_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        header_frame.grid(row=0, column=0, sticky="ew")
        header_frame.grid_columnconfigure(1, weight=1)

        # ── Logo icon ────────────────────────────────────────────────────────
        _logo_img = None
        try:
            from paths import ART_DIR
            from PIL import Image
            for _name in ("logo.png", "logo.ico", "icon.png", "icon.ico"):
                _p = ART_DIR / _name
                if _p.exists():
                    _img = Image.open(_p).resize((32, 32), Image.LANCZOS)
                    _logo_img = ctk.CTkImage(light_image=_img, dark_image=_img, size=(32, 32))
                    break
        except Exception:
            pass

        if _logo_img:
            ctk.CTkLabel(
                header_frame, image=_logo_img, text="",
            ).grid(row=0, column=0, rowspan=2, padx=(14, 6), pady=(8, 8), sticky="w")
            col_start = 1
        else:
            col_start = 0

        ctk.CTkLabel(header_frame, text="Microphone Setup & Audio Tuning",
                     font=FONT_TITLE, text_color=COLOR_TEAL, anchor="w",
                     ).grid(row=0, column=col_start, padx=(0 if _logo_img else 16, 16),
                            pady=(12, 2), sticky="w")
        ctk.CTkLabel(header_frame,
                     text="Tune VAD, EQ, and noise filtering against your actual environment",
                     font=FONT_SMALL, text_color=COLOR_DIM, anchor="w",
                     ).grid(row=1, column=col_start, padx=(0 if _logo_img else 16, 16),
                            pady=(0, 10), sticky="w")
        ctk.CTkFrame(header_frame, height=1, fg_color=BORDER).grid(
            row=2, column=0, columnspan=2, sticky="ew")

    # ── Device row ────────────────────────────────────────────────────────────

    def _build_device_row(self):
        f = self._card(1, "Microphone Device")
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f, text="Device", font=FONT_SMALL, text_color=COLOR_DIM,
                     ).grid(row=0, column=0, padx=(12, 6), pady=10, sticky="w")

        from spoaken_connect import list_input_devices, default_device_name
        devices   = list_input_devices()
        names     = ["[sys] System Default"] + [f"[{i}] {n}" for i, n in devices]

        self._cmb_device = ctk.CTkComboBox(
            f, values=names, font=FONT_SMALL, text_color=COLOR_MAIN,
            fg_color=BG_INPUT, border_color=BORDER, border_width=1,
            button_color=BORDER_ACTIVE, button_hover_color="#3a60c8",
            dropdown_fg_color=BG_CARD, dropdown_text_color=COLOR_MAIN,
            height=30, corner_radius=5,
            command=self._on_device_change,
        )
        self._cmb_device.set(names[0])
        self._cmb_device.grid(row=0, column=1, padx=(0, 12), pady=10, sticky="ew")

        self._lbl_dev_info = ctk.CTkLabel(f, text="", font=FONT_SMALL,
                                           text_color=COLOR_DIM, anchor="w")
        self._lbl_dev_info.grid(row=1, column=0, columnspan=2,
                                 padx=12, pady=(0, 8), sticky="w")
        self._refresh_device_info()

    # ── Level meter ────────────────────────────────────────────────────────────

    def _build_meter_section(self):
        f = self._card(2, "Live Input Level")
        f.grid_columnconfigure(0, weight=1)

        self._meter_bar = ctk.CTkProgressBar(
            f, height=20, corner_radius=5,
            fg_color=BG_INPUT, progress_color=COLOR_TEAL,
        )
        self._meter_bar.set(0)
        self._meter_bar.grid(row=0, column=0, columnspan=3,
                              padx=12, pady=(10, 4), sticky="ew")

        info = ctk.CTkFrame(f, fg_color="transparent")
        info.grid(row=1, column=0, columnspan=3,
                  padx=12, pady=(0, 10), sticky="ew")
        info.grid_columnconfigure(1, weight=1)

        self._lbl_rms = ctk.CTkLabel(info, text="RMS: 0.000",
                                      font=FONT_MONO, text_color=COLOR_DIM, anchor="w")
        self._lbl_rms.grid(row=0, column=0, sticky="w")

        self._lbl_vad_badge = ctk.CTkLabel(
            info, text="  ● SILENCE  ",
            font=("Segoe UI Semibold", 10), text_color="#44537a",
            fg_color=BG_DEEP, corner_radius=4,
        )
        self._lbl_vad_badge.grid(row=0, column=2, sticky="e")

    # ── VAD ────────────────────────────────────────────────────────────────────

    def _build_vad_section(self):
        f = self._card(3, "Voice Activity Detection (VAD)")
        f.grid_columnconfigure(1, weight=1)

        self._vad_on = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(f, text="Enable VAD gate  (block audio during silence)",
                         variable=self._vad_on, font=FONT_SMALL, text_color=COLOR_MAIN,
                         fg_color=BORDER_ACTIVE, hover_color="#3a60c8",
                         command=self._on_vad_toggle,
                         ).grid(row=0, column=0, columnspan=3,
                                padx=12, pady=(10, 4), sticky="w")

        rows = [
            ("Aggressiveness", 0, 3, 3, 2, self._on_vad_agg_change, "_lbl_vad_agg", "2  (office/classroom)"),
            ("Min speech (ms)", 50, 600, 11, 200, self._on_min_speech, "_lbl_min_speech", "200 ms"),
            ("Silence gap (ms)", 100, 1200, 11, 500, self._on_silence_gap, "_lbl_silence_gap", "500 ms"),
        ]
        sliders = []
        for r, (label, lo, hi, steps, default, cb, attr, lbl_text) in enumerate(rows, start=1):
            last = (r == len(rows))
            ctk.CTkLabel(f, text=label, font=FONT_SMALL, text_color=COLOR_DIM,
                         ).grid(row=r, column=0, padx=(12, 6),
                                pady=(4, 10 if last else 4), sticky="w")
            sld = ctk.CTkSlider(f, from_=lo, to=hi, number_of_steps=steps,
                                 fg_color=BG_INPUT, progress_color=BORDER_ACTIVE,
                                 button_color=COLOR_MAIN, button_hover_color=COLOR_TEAL,
                                 command=cb)
            sld.set(default)
            sld.grid(row=r, column=1, padx=6, pady=(4, 10 if last else 4), sticky="ew")
            lbl = ctk.CTkLabel(f, text=lbl_text, font=FONT_SMALL, text_color=COLOR_MAIN,
                                width=170, anchor="w")
            lbl.grid(row=r, column=2, padx=(0, 12), sticky="w")
            setattr(self, attr, lbl)
            sliders.append(sld)
        self._vad_sliders = sliders

    # ── EQ ─────────────────────────────────────────────────────────────────────

    def _build_eq_section(self):
        f = self._card(4, "Frequency Profile  (attenuates fan / HVAC rumble)")
        f.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.grid(row=0, column=0, padx=12, pady=(10, 4), sticky="ew")
        for i in range(4):
            btn_row.grid_columnconfigure(i, weight=1)

        self._eq_var = ctk.StringVar(value="speech")
        presets = [
            ("flat",       "Flat",        "No filtering"),
            ("speech",     "Speech ★",    "80 Hz high-pass  (recommended)"),
            ("aggressive", "Aggressive",  "100 Hz HP + 60/120 Hz notch"),
            ("custom",     "Custom HP",   "Manual cutoff below"),
        ]
        for col, (val, lbl, _) in enumerate(presets):
            ctk.CTkRadioButton(
                btn_row, text=lbl, variable=self._eq_var, value=val,
                font=FONT_SMALL, text_color=COLOR_MAIN,
                fg_color=BORDER_ACTIVE, hover_color="#3a60c8",
                command=self._on_eq_change,
            ).grid(row=0, column=col, padx=6, sticky="w")

        self._lbl_eq_tip = ctk.CTkLabel(
            f, text="→ 80 Hz high-pass removes low-frequency fan/HVAC noise",
            font=FONT_SMALL, text_color=COLOR_DIM, anchor="w",
        )
        self._lbl_eq_tip.grid(row=1, column=0, padx=12, pady=(0, 4), sticky="w")

        custom_row = ctk.CTkFrame(f, fg_color="transparent")
        custom_row.grid(row=2, column=0, padx=12, pady=(0, 10), sticky="ew")
        custom_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(custom_row, text="HP cutoff (Hz)", font=FONT_SMALL, text_color=COLOR_DIM,
                     ).grid(row=0, column=0, padx=(0, 6), sticky="w")
        self._sld_hp = ctk.CTkSlider(
            custom_row, from_=40, to=300, number_of_steps=26,
            fg_color=BG_INPUT, progress_color=BORDER_ACTIVE,
            button_color=COLOR_MAIN, button_hover_color=COLOR_TEAL,
            command=self._on_hp_change, state="disabled",
        )
        self._sld_hp.set(80)
        self._sld_hp.grid(row=0, column=1, padx=6, sticky="ew")
        self._lbl_hp = ctk.CTkLabel(custom_row, text="80 Hz", font=FONT_SMALL,
                                     text_color=COLOR_DIM, width=60, anchor="w")
        self._lbl_hp.grid(row=0, column=2, sticky="w")

    # ── Noise reduction ────────────────────────────────────────────────────────

    def _build_nr_section(self):
        f = self._card(5, "Noise Suppression  (noisereduce)")
        f.grid_columnconfigure(1, weight=1)

        self._nr_on = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(f, text="Enable spectral noise suppression",
                         variable=self._nr_on, font=FONT_SMALL, text_color=COLOR_MAIN,
                         fg_color=BORDER_ACTIVE, hover_color="#3a60c8",
                         ).grid(row=0, column=0, columnspan=3,
                                padx=12, pady=(10, 4), sticky="w")

        ctk.CTkLabel(f, text="Strength", font=FONT_SMALL, text_color=COLOR_DIM,
                     ).grid(row=1, column=0, padx=(12, 6), pady=4, sticky="w")
        self._sld_nr = ctk.CTkSlider(
            f, from_=0.1, to=1.0, number_of_steps=9,
            fg_color=BG_INPUT, progress_color=BORDER_ACTIVE,
            button_color=COLOR_MAIN, button_hover_color=COLOR_TEAL,
            command=lambda v: self._lbl_nr_val.configure(text=f"{float(v):.2f}"),
        )
        self._sld_nr.set(0.75)
        self._sld_nr.grid(row=1, column=1, padx=6, pady=4, sticky="ew")
        self._lbl_nr_val = ctk.CTkLabel(f, text="0.75", font=FONT_SMALL,
                                         text_color=COLOR_MAIN, width=50, anchor="w")
        self._lbl_nr_val.grid(row=1, column=2, padx=(0, 12), pady=4, sticky="w")

        cap_row = ctk.CTkFrame(f, fg_color="transparent")
        cap_row.grid(row=2, column=0, columnspan=3,
                     padx=12, pady=(4, 10), sticky="ew")
        cap_row.grid_columnconfigure(1, weight=1)

        self._btn_cap = ctk.CTkButton(
            cap_row, text="🎙  Sample ambient noise (2 s)",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color="#0d3a40", hover_color="#145060", text_color=COLOR_TEAL,
            command=self._capture_noise,
        )
        self._btn_cap.grid(row=0, column=0, sticky="w")

        self._lbl_cap_status = ctk.CTkLabel(
            cap_row, text="No profile — using stationary estimation",
            font=FONT_SMALL, text_color=COLOR_DIM, anchor="w",
        )
        self._lbl_cap_status.grid(row=0, column=1, padx=(10, 0), sticky="w")

    # ── Test recorder ──────────────────────────────────────────────────────────

    def _build_test_section(self):
        f = self._card(6, "Test  (record 5 s → compare Vosk + Whisper output)")
        f.grid_columnconfigure(0, weight=1)

        top_row = ctk.CTkFrame(f, fg_color="transparent")
        top_row.grid(row=0, column=0, padx=12, pady=(10, 4), sticky="ew")
        top_row.grid_columnconfigure(1, weight=1)

        self._btn_test = ctk.CTkButton(
            top_row, text="▶  Record 5 s test",
            font=FONT_UI, height=36, corner_radius=7,
            fg_color="#1a3a5e", hover_color="#2450a0", text_color=COLOR_TEAL,
            command=self._start_test,
        )
        self._btn_test.grid(row=0, column=0, sticky="w")

        self._lbl_test_status = ctk.CTkLabel(
            top_row, text="Speak after pressing — results show below",
            font=FONT_SMALL, text_color=COLOR_DIM, anchor="w",
        )
        self._lbl_test_status.grid(row=0, column=1, padx=(12, 0), sticky="w")

        results = ctk.CTkFrame(f, fg_color=BG_INPUT, corner_radius=6)
        results.grid(row=1, column=0, padx=12, pady=(4, 10), sticky="ew")
        results.grid_columnconfigure(0, weight=1)
        results.grid_columnconfigure(1, weight=1)

        for col, (name, colour, attr) in enumerate([
            ("Vosk",    COLOR_TEAL, "_lbl_vosk_res"),
            ("Whisper", "#4dd9f5", "_lbl_whisper_res"),
        ]):
            pane = ctk.CTkFrame(results, fg_color="transparent")
            pane.grid(row=0, column=col, padx=10, pady=8, sticky="nsew")
            ctk.CTkLabel(pane, text=name, font=("Segoe UI Semibold", 10),
                         text_color=colour, anchor="w").pack(anchor="w")
            lbl = ctk.CTkLabel(pane, text="—", font=FONT_SMALL, text_color=COLOR_DIM,
                               anchor="w", justify="left", wraplength=230)
            lbl.pack(anchor="w", pady=(2, 0))
            setattr(self, attr, lbl)

    # ── Fixed bottom action bar (row 2 of the window, never scrolled) ─────────

    def _build_actions(self):
        # Top separator line
        ctk.CTkFrame(self, height=1, fg_color=BORDER).grid(
            row=2, column=0, sticky="ew"
        )

        # Outer container sits at row 3, always visible
        action_row = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        action_row.grid(row=3, column=0, sticky="ew", padx=0, pady=0)
        action_row.grid_columnconfigure(0, weight=1)   # Apply stretches
        action_row.grid_columnconfigure(1, weight=0)   # Reset fixed
        action_row.grid_columnconfigure(2, weight=0)   # Close fixed

        ctk.CTkButton(
            action_row, text="✔  Apply Settings",
            font=("Segoe UI Semibold", 12), height=42, corner_radius=0,
            fg_color="#00e5cc", hover_color="#00c8b0", text_color="#000000",
            command=self._apply,
        ).grid(row=0, column=0, sticky="ew", padx=0, pady=0)

        ctk.CTkButton(
            action_row, text="↺  Reset",
            font=FONT_UI, height=42, corner_radius=0, width=100,
            fg_color=BG_CARD, hover_color=BORDER_ACTIVE, text_color=COLOR_DIM,
            border_width=0,
            command=self._reset,
        ).grid(row=0, column=1, padx=0, pady=0)

        ctk.CTkButton(
            action_row, text="✕  Close",
            font=FONT_UI, height=42, corner_radius=0, width=90,
            fg_color="#3a1010", hover_color="#661a1a", text_color="#e07070",
            border_width=0,
            command=self._on_close,
        ).grid(row=0, column=2, padx=0, pady=0)

    # ── Log ────────────────────────────────────────────────────────────────────

    def _build_log(self):
        ctk.CTkLabel(self._scroll_frame, text="Diagnostics", font=FONT_SMALL, text_color=COLOR_DIM,
                     anchor="w").grid(row=7, column=0, padx=16, pady=(4, 0), sticky="w")
        self._log_box = ctk.CTkTextbox(
            self._scroll_frame, fg_color=BG_INPUT, border_color=BORDER, border_width=1,
            font=FONT_MONO, text_color=COLOR_CONSOLE, corner_radius=8, wrap="word", height=110,
        )
        self._log_box.grid(row=8, column=0, padx=14, pady=(2, 14), sticky="ew")
        self._log_box.configure(state="disabled")

    # ── Card helper ────────────────────────────────────────────────────────────

    def _card(self, row: int, label: str) -> ctk.CTkFrame:
        """Create a labelled card inside the scrollable frame."""
        outer = ctk.CTkFrame(self._scroll_frame, fg_color=BG_CARD,
                              border_color=BORDER, border_width=1, corner_radius=8)
        outer.grid(row=row, column=0, padx=14, pady=(4, 2), sticky="ew")
        outer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(outer, text=label, font=("Segoe UI Semibold", 10),
                     text_color=COLOR_TEAL, anchor="w",
                     ).grid(row=0, column=0, padx=12, pady=(8, 0), sticky="w")
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.grid(row=1, column=0, sticky="ew")
        inner.grid_columnconfigure(0, weight=1)
        return inner

    # ─────────────────────────────────────────────────────────────────────────
    # Live meter
    # ─────────────────────────────────────────────────────────────────────────

    def _dev_index(self) -> int | None:
        sel = self._cmb_device.get()
        if sel.startswith("[sys]"):
            return None
        try:
            return int(sel.split("]")[0].lstrip("["))
        except Exception:
            return None

    def _start_meter(self):
        self._stop_meter()
        try:
            dev = self._dev_index()
            def _cb(indata, frames, ti, status):
                raw = bytes(indata)
                arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                self._meter_rms = float(np.sqrt(np.mean(arr ** 2))) / 32768.0
                if self._vad:
                    self._gate_open = self._vad.is_speech(raw)
            with self._stream_lock:
                self._stream = sd.RawInputStream(
                    samplerate=_SR, blocksize=800, device=dev,
                    dtype="int16", channels=1, callback=_cb,
                )
                self._stream.start()
        except Exception as exc:
            self._log(f"[Meter]: {exc}")
        self._tick_meter()

    def _stop_meter(self):
        with self._stream_lock:
            if self._stream:
                try:
                    self._stream.stop(); self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def _tick_meter(self):
        if not self.winfo_exists():
            return
        # Pause meter updates while the window is hidden / minimized to avoid
        # burning CPU and keeping the audio stream alive unnecessarily.
        if not self.winfo_viewable():
            self.after(250, self._tick_meter)
            return
        rms = self._meter_rms
        colour = COLOR_ERR if rms > 0.85 else COLOR_WARN if rms > 0.55 else COLOR_OK if rms > 0.04 else COLOR_DIM
        self._meter_bar.configure(progress_color=colour)
        self._meter_bar.set(min(1.0, rms * 2.8))
        self._lbl_rms.configure(text=f"RMS: {rms:.3f}")
        gate = self._gate_open
        self._lbl_vad_badge.configure(
            text="  ● SPEECH   " if gate else "  ● SILENCE  ",
            text_color=COLOR_OK if gate else "#44537a",
        )
        self.after(40, self._tick_meter)

    # ─────────────────────────────────────────────────────────────────────────
    # Callbacks
    # ─────────────────────────────────────────────────────────────────────────

    def _on_device_change(self, _=None):
        self._refresh_device_info()
        self._start_meter()

    def _refresh_device_info(self):
        dev = self._dev_index()
        try:
            info = sd.query_devices(dev if dev is not None else sd.default.device[0])
            ch  = info.get("max_input_channels", "?")
            sr  = int(info.get("default_samplerate", 0))
            lat = info.get("default_low_input_latency", 0) * 1000
            self._lbl_dev_info.configure(
                text=f"Channels: {ch}   Default SR: {sr} Hz   Latency: {lat:.1f} ms",
                text_color=COLOR_DIM,
            )
        except Exception as exc:
            self._lbl_dev_info.configure(text=f"Cannot query device: {exc}",
                                          text_color=COLOR_WARN)

    def _on_vad_toggle(self):
        state = "normal" if self._vad_on.get() else "disabled"
        for sld in self._vad_sliders:
            sld.configure(state=state)

    _VAD_DESCS = [
        "0  (permissive — passes most audio)",
        "1  (balanced)",
        "2  (office / classroom)",
        "3  (aggressive — noisy room)",
    ]

    def _on_vad_agg_change(self, val):
        v = int(round(float(val)))
        self._lbl_vad_agg.configure(text=self._VAD_DESCS[v])
        if self._vad:
            self._vad.set_aggressiveness(v)

    def _on_min_speech(self, val):
        v = int(round(float(val) / 50) * 50)
        self._lbl_min_speech.configure(text=f"{v} ms")
        if self._vad:
            self._vad.set_min_speech(v)

    def _on_silence_gap(self, val):
        v = int(round(float(val) / 100) * 100)
        self._lbl_silence_gap.configure(text=f"{v} ms")
        if self._vad:
            self._vad.set_silence_gap(v)

    _EQ_TIPS = {
        "flat"      : "No filtering — raw mic input sent to engines",
        "speech"    : "→ 80 Hz high-pass removes low-frequency background noise (fans, HVAC)",
        "aggressive": "→ 100 Hz HP + 60/120 Hz notch filters fan motor harmonics",
        "custom"    : "→ Set your own high-pass cutoff with the slider",
    }

    def _on_eq_change(self):
        p = self._eq_var.get()
        self._lbl_eq_tip.configure(text=self._EQ_TIPS.get(p, ""))
        state = "normal" if p == "custom" else "disabled"
        self._sld_hp.configure(state=state)
        self._lbl_hp.configure(text_color=COLOR_MAIN if p == "custom" else COLOR_DIM)

    def _on_hp_change(self, val):
        self._lbl_hp.configure(text=f"{int(val)} Hz")

    # ─────────────────────────────────────────────────────────────────────────
    # Noise profile capture
    # ─────────────────────────────────────────────────────────────────────────

    def _capture_noise(self):
        if self._capturing:
            return
        self._capturing = True
        self._btn_cap.configure(state="disabled", text="Recording ambient noise …")
        threading.Thread(target=self._capture_worker, daemon=True).start()

    def _capture_worker(self):
        try:
            dev = self._dev_index()
            self._log("[Noise profile]: recording 2 s of ambient noise …")
            audio = sd.rec(int(_SR * 2), samplerate=_SR, channels=1,
                           dtype="float32", device=dev)
            sd.wait()
            self._noise_profile = audio.flatten()
            self._log("[Noise profile]: captured ✔")
            self.after(0, lambda: self._lbl_cap_status.configure(
                text="✔  Profile captured — applied during recording",
                text_color=COLOR_OK,
            ))
        except Exception as exc:
            self._log(f"[Noise profile]: {exc}")
            self.after(0, lambda: self._lbl_cap_status.configure(
                text=f"✗  {exc}", text_color=COLOR_ERR,
            ))
        finally:
            self._capturing = False
            self.after(0, lambda: self._btn_cap.configure(
                state="normal", text="🎙  Sample ambient noise (2 s)",
            ))

    # ─────────────────────────────────────────────────────────────────────────
    # Test recording
    # ─────────────────────────────────────────────────────────────────────────

    def _start_test(self):
        if self._testing:
            return
        self._testing = True
        self._btn_test.configure(state="disabled", text="Recording …")
        self._lbl_test_status.configure(text="Speak now — 5 s …", text_color=COLOR_WARN)
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        try:
            dev = self._dev_index()
            self._log("[Test]: recording 5 s …")
            raw = sd.rec(int(_SR * 5), samplerate=_SR, channels=1,
                         dtype="int16", device=dev)
            sd.wait()
            pcm = raw.flatten().astype(np.int16)

            # Apply current pipeline settings
            pcm_proc = self._apply_pipeline(pcm)
            data     = pcm_proc.tobytes()

            self._log("[Test]: running Vosk …")
            v_text = self._run_vosk(data)
            self._log("[Test]: running Whisper …")
            w_text = self._run_whisper(data)

            vw = len(v_text.split()) if v_text.strip() else 0
            ww = len(w_text.split()) if w_text.strip() else 0

            self._log(
                f"[Test]: Vosk={vw} words  Whisper={ww} words\n"
                f"  Vosk:    {v_text or '(nothing)'}\n"
                f"  Whisper: {w_text or '(nothing)'}"
            )

            def _upd():
                vc = COLOR_OK if vw > 0 else COLOR_WARN
                wc = COLOR_OK if ww > 0 else COLOR_WARN
                self._lbl_vosk_res.configure(
                    text=f"{vw} words\n\"{v_text[:120] or 'nothing detected'}\"",
                    text_color=vc,
                )
                self._lbl_whisper_res.configure(
                    text=f"{ww} words\n\"{w_text[:120] or 'nothing detected'}\"",
                    text_color=wc,
                )
                self._lbl_test_status.configure(
                    text=f"Done — Vosk: {vw} words  ·  Whisper: {ww} words",
                    text_color=COLOR_OK if (vw + ww) > 0 else COLOR_WARN,
                )
            self.after(0, _upd)

        except Exception as exc:
            self._log(f"[Test error]: {exc}")
            self.after(0, lambda: self._lbl_test_status.configure(
                text=f"Error: {exc}", text_color=COLOR_ERR,
            ))
        finally:
            self._testing = False
            self.after(0, lambda: self._btn_test.configure(
                state="normal", text="▶  Record 5 s test",
            ))

    def _apply_pipeline(self, pcm: np.ndarray) -> np.ndarray:
        """Apply current EQ + NR settings to a test buffer."""
        arr = pcm.astype(np.float32)
        profile = self._eq_var.get()

        if profile != "flat":
            try:
                from scipy.signal import butter, sosfilt, iirnotch, tf2sos
                x = arr / 32768.0
                if profile in ("speech", "custom"):
                    cutoff = float(self._sld_hp.get()) if profile == "custom" else 80.0
                    sos = butter(4, cutoff / (_SR / 2), btype="high", output="sos")
                    x   = sosfilt(sos, x)
                elif profile == "aggressive":
                    sos = butter(5, 100.0 / (_SR / 2), btype="high", output="sos")
                    x   = sosfilt(sos, x)
                    for freq in (60.0, 120.0):
                        b, a = iirnotch(freq, 30.0, _SR)
                        x    = sosfilt(tf2sos(b, a), x)
                arr = np.clip(x * 32768.0, -32768, 32767).astype(np.float32)
            except ImportError:
                self._log("[EQ]: scipy not installed — pip install scipy")
            except Exception as exc:
                self._log(f"[EQ]: {exc}")

        if self._nr_on.get():
            try:
                import noisereduce as nr_
                strength = float(self._sld_nr.get())
                if self._noise_profile is not None:
                    yn = (self._noise_profile * 32768.0).astype(np.float32)
                    arr = nr_.reduce_noise(y=arr, y_noise=yn, sr=_SR,
                                           prop_decrease=strength, stationary=True)
                else:
                    arr = nr_.reduce_noise(y=arr, sr=_SR,
                                           prop_decrease=strength, stationary=True)
            except ImportError:
                self._log("[NR]: noisereduce not installed — pip install noisereduce")
            except Exception as exc:
                self._log(f"[NR]: {exc}")

        return np.clip(arr, -32768, 32767).astype(np.int16)

    def _run_vosk(self, pcm_bytes: bytes) -> str:
        try:
            from spoaken_connect import VoskModel, KaldiRecognizer, _vosk_ok, _resolve_vosk
            from spoaken_config  import QUICK_VOSK_MODEL
            import json as _json
            if not _vosk_ok:
                return "(vosk not installed)"
            model = VoskModel(_resolve_vosk(QUICK_VOSK_MODEL))
            rec   = KaldiRecognizer(model, _SR)
            rec.SetWords(True)
            for i in range(0, len(pcm_bytes), 3200):
                rec.AcceptWaveform(pcm_bytes[i:i+3200])
            return _json.loads(rec.FinalResult()).get("text", "").strip()
        except Exception as exc:
            return f"(error: {exc})"

    def _run_whisper(self, pcm_bytes: bytes) -> str:
        try:
            from spoaken_connect import WhisperModel, _whisper_ok, _resolve_compute_type
            from spoaken_config  import WHISPER_MODEL, GPU_ENABLED, WHISPER_COMPUTE
            from paths import WHISPER_DIR
            if not _whisper_ok:
                return "(faster-whisper not installed)"
            device       = "cuda" if GPU_ENABLED else "cpu"
            compute_type = _resolve_compute_type(WHISPER_COMPUTE, GPU_ENABLED)
            model        = WhisperModel(WHISPER_MODEL, device=device,
                                        compute_type=compute_type,
                                        download_root=str(WHISPER_DIR))
            arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            segs, _ = model.transcribe(arr, beam_size=3, vad_filter=True)
            return " ".join(s.text.strip() for s in segs).strip()
        except Exception as exc:
            return f"(error: {exc})"

    # ─────────────────────────────────────────────────────────────────────────
    # Apply / Reset
    # ─────────────────────────────────────────────────────────────────────────

    def _apply(self):
        """Write all settings to spoaken_connect._mic_config immediately."""
        dev = self._dev_index()
        if self._ctrl:
            try:
                self._ctrl.set_mic_device(dev)
            except Exception:
                pass

        # Push to connect module
        try:
            import spoaken_connect as _sc
            _sc._mic_config.update({
                "vad_enabled"  : self._vad_on.get(),
                "vad_agg"      : int(round(self._vad_sliders[0].get())),
                "min_speech"   : int(round(self._vad_sliders[1].get() / 50) * 50),
                "silence_gap"  : int(round(self._vad_sliders[2].get() / 100) * 100),
                "eq_profile"   : self._eq_var.get(),
                "hp_cutoff"    : int(self._sld_hp.get()),
                "nr_enabled"   : self._nr_on.get(),
                "nr_strength"  : round(float(self._sld_nr.get()), 2),
                "noise_profile": self._noise_profile,
            })

            # Re-configure the global VAD singleton
            if _sc._global_vad is not None:
                _sc._global_vad.set_aggressiveness(_sc._mic_config["vad_agg"])
                _sc._global_vad.set_min_speech(_sc._mic_config["min_speech"])
                _sc._global_vad.set_silence_gap(_sc._mic_config["silence_gap"])

            self._log(
                f"[Apply]: device={dev}  VAD={'on' if _sc._mic_config['vad_enabled'] else 'off'}"
                f"/agg={_sc._mic_config['vad_agg']}  "
                f"EQ={_sc._mic_config['eq_profile']}  "
                f"NR={'on' if _sc._mic_config['nr_enabled'] else 'off'}"
            )
        except Exception as exc:
            self._log(f"[Apply error]: {exc}")

        # Toggle noise suppression flag in controller
        if self._ctrl:
            try:
                self._ctrl.toggle_noise_suppression(self._nr_on.get())
            except Exception:
                pass

    def _reset(self):
        self._vad_on.set(True)
        self._vad_sliders[0].set(2);  self._on_vad_agg_change(2)
        self._vad_sliders[1].set(200); self._lbl_min_speech.configure(text="200 ms")
        self._vad_sliders[2].set(500); self._lbl_silence_gap.configure(text="500 ms")
        self._eq_var.set("speech");    self._on_eq_change()
        self._nr_on.set(False)
        self._sld_nr.set(0.75);        self._lbl_nr_val.configure(text="0.75")
        self._noise_profile = None
        self._lbl_cap_status.configure(
            text="No profile — using stationary estimation", text_color=COLOR_DIM,
        )
        self._log("[Reset]: defaults restored")

    # ─────────────────────────────────────────────────────────────────────────
    # Logging
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        def _ins():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        if threading.current_thread() is threading.main_thread():
            _ins()
        else:
            try:
                self.after(0, _ins)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _on_close(self):
        self._stop_meter()
        if self.winfo_exists():
            self.destroy()

    def _centre(self):
        try:
            sw = self.winfo_screenwidth(); sh = self.winfo_screenheight()
            w  = self.winfo_width();       h  = self.winfo_height()
            self.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")
        except Exception:
            pass
