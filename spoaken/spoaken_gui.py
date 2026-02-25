"""
spoaken_gui.py
──────────────
Main application window for Spoaken — v2.

New in this version
───────────────────
  • Microphone selector dropdown (top of controls panel)
  • Noise suppression toggle
  • Lock button ↔ Unlock button (reflects actual writer lock state)
  • "Transcript" label has a hidden copy button (⧉) on the right
  • High-contrast transcript text: teal for Vosk, cyan for Whisper, dim for partials
  • Waveform driven by real audio RMS levels instead of random jitter
  • Collapsible Chat sidebar panel (toggleable)
  • Command entry in sidebar for local commands
  • All UI strictly thread-safe via self.after()
"""

import math
import time
import tkinter as tk
import threading
from pathlib import Path

import customtkinter as ctk
from tkinter import messagebox
from PIL import Image, ImageTk

from paths import ART_DIR
from spoaken_connect import list_input_devices, default_device_name, \
    scan_installed_vosk_models, scan_installed_whisper_models

# LLM module is optional — imported lazily to avoid blocking startup
def _scan_llm_models() -> list[str]:
    """Return list of Ollama models (empty list if Ollama not running)."""
    try:
        from spoaken_llm import list_ollama_models
        models = list_ollama_models()
        return models if models else ["(Ollama offline)"]
    except Exception:
        return ["(Ollama not installed)"]

# ── Colour palette ──────────────────────────────────────────────────────────
BG_DEEP    = "#060c1a"   # window background
BG_PANEL   = "#0a1128"   # header
BG_CARD    = "#0d1735"   # controls card
BG_INPUT   = "#0c1636"   # text fields / textboxes
BG_SIDEBAR = "#080f20"   # chat sidebar
BORDER_SUB = "#1a2d60"
BORDER_ACT = "#2545a8"
BORDER_TEA = "#0d4d60"   # teal border accent

TXT_MAIN   = "#00bdff"   # title / primary blue
TXT_DIM    = "#2a6080"   # muted blue
TXT_VOSK   = "#00e5cc"   # high-contrast teal  → vosk confirmed lines
TXT_WHISPER= "#4dd9f5"   # bright cyan         → whisper final lines
TXT_PARTIAL= "#2a8fa8"   # dim teal            → live partials
TXT_CHAT   = "#80e0f0"   # chat sidebar text
TXT_CONSOLE= "#007bff"   # console output
TXT_TEAL=    "#2bfbf9"

BTN_REC    = "#1a5e2a"
BTN_REC_H  = "#24883c"
BTN_STOP   = "#c42828"
BTN_STOP_H = "#e03535"
BTN_WON    = "#b85c00"
BTN_WON_H  = "#d97000"
BTN_WOFF   = "#182236"
BTN_WOFF_H = "#243350"
BTN_CLR    = "#5e1414"
BTN_CLR_H  = "#852020"
BTN_LOG    = "#0d3a40"
BTN_LOG_H  = "#12566080"

STA_IDLE   = "#44537a"
STA_REC    = "#d42b2b"
STA_CORR   = "#2c5fe6"

# Waveform colour stops  (lo, hi)  as RGB tuples
WF_IDLE  = ((20, 40, 80),  (35, 65, 150))
WF_REC   = ((15, 80, 60),  (0,  220, 160))
WF_CORR  = ((20, 60, 180), (60, 200, 255))

FONT_MONO  = ("Courier New", 11)
FONT_UI    = ("Segoe UI",    11)
FONT_SMALL = ("Segoe UI",     9)
FONT_TITLE = ("Segoe UI Semibold", 13)

_WF_BARS = 60
_WF_FPS  = 40   # ms per frame


def _lerp_colour(lo: tuple, hi: tuple, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r = int(lo[0] + (hi[0] - lo[0]) * t)
    g = int(lo[1] + (hi[1] - lo[1]) * t)
    b = int(lo[2] + (hi[2] - lo[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


# ═════════════════════════════════════════════════════════════════════════════
class TranscriptionView(ctk.CTk):
# ═════════════════════════════════════════════════════════════════════════════

    def __init__(self, controller):
        super().__init__(className="Spoaken")
        self.controller = controller

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("Spoaken  v2.0")
        # Base window size (3-panel: transcript | controls | [chat hidden])
        self._base_width  = 1060
        self._base_height = 800
        self.geometry(f"{self._base_width}x{self._base_height}")
        self.minsize(760, 600)
        self.configure(fg_color=BG_DEEP)

        # Taskbar / window icon — checks parent_dir/spoaken/Art/
        _ico = ART_DIR / "logo.ico"
        _png = ART_DIR / "logo.png"
        if _ico.exists():
            try:
                self.iconbitmap(str(_ico))
            except Exception:
                pass
        if _png.exists():
            try:
                img = Image.open(_png).resize((64, 64))
                self._icon = ImageTk.PhotoImage(img)
                self.after(200, lambda: self.wm_iconphoto(True, self._icon))
            except Exception:
                pass

        # Waveform state
        self._wf_state   = "idle"
        self._wf_heights = [0.04] * _WF_BARS
        self._wf_targets = [0.04] * _WF_BARS
        self._wf_t       = 0.0
        self._audio_rms  = 0.0   # fed by controller callback

        # Sidebar visibility
        self._sidebar_open = False

        # ── 3-column horizontal layout ────────────────────────────────────────
        # col 0 = transcript panel  (weight=1, stretches)
        # col 1 = controls/centre panel (fixed ~380 px)
        # col 2 = chat sidebar (hidden by default, weight=0)
        self.grid_columnconfigure(0, weight=1)           # transcript – stretchy
        self.grid_columnconfigure(1, weight=0, minsize=380)  # controls – fixed
        self.grid_columnconfigure(2, weight=0, minsize=0)    # chat – hidden
        self.grid_rowconfigure(0, weight=1)              # single content row

        self._build_transcript_panel()   # left col
        self._build_centre_panel()       # middle col
        self._build_sidebar()            # right col (hidden until toggled)
        self._configure_log_tags()

        self.after(_WF_FPS, self._wf_loop)
        self.protocol("WM_DELETE_WINDOW", self.controller.on_close_request)

    # ─────────────────────────────────────────────────────────────────────────
    # Left panel — Transcript
    # ─────────────────────────────────────────────────────────────────────────

    def _build_transcript_panel(self):
        """Left column: transcript label + scrollable transcript text box."""
        lp = ctk.CTkFrame(
            self, fg_color=BG_CARD,
            border_color=BORDER_SUB, border_width=1, corner_radius=8,
        )
        lp.grid(row=0, column=0, padx=(10, 4), pady=10, sticky="nsew")
        lp.grid_rowconfigure(1, weight=1)
        lp.grid_columnconfigure(0, weight=1)

        # Label row with copy button
        lf = ctk.CTkFrame(lp, fg_color="transparent")
        lf.grid(row=0, column=0, padx=10, pady=(8, 0), sticky="ew")
        lf.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            lf, text="Transcript",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self.btn_copy = ctk.CTkButton(
            lf, text="⧉  Copy",
            font=FONT_SMALL, height=20, corner_radius=4, width=64,
            fg_color="transparent", hover_color=BORDER_SUB,
            text_color=TXT_DIM, border_width=0,
            command=self.controller.copy_transcript,
        )
        self.btn_copy.grid(row=0, column=1, sticky="e")

        # Transcript textbox — fills remaining height
        self.log = ctk.CTkTextbox(
            lp,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            font=FONT_MONO, text_color=TXT_VOSK,
            scrollbar_button_color=BORDER_ACT, corner_radius=8, wrap="word",
        )
        self.log.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="nsew")
        self.log.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Middle panel — Header info + Controls
    # ─────────────────────────────────────────────────────────────────────────

    def _build_centre_panel(self):
        """Middle column: title/waveform at top, console (expands), controls at bottom."""
        mp = ctk.CTkFrame(self, fg_color="transparent")
        mp.grid(row=0, column=1, padx=(0, 4), pady=10, sticky="nsew")
        mp.grid_rowconfigure(1, weight=0)   # header card – fixed
        mp.grid_rowconfigure(2, weight=1)   # console – stretches
        mp.grid_rowconfigure(3, weight=0)   # controls card – fixed
        mp.grid_columnconfigure(0, weight=1)

        self._build_header(mp)
        self._build_console(mp)
        self._build_controls(mp)

    def _build_header(self, parent):
        hf = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=8,
                          border_color=BORDER_SUB, border_width=1)
        hf.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        hf.grid_columnconfigure(0, weight=1)

        # Title row
        tr = ctk.CTkFrame(hf, fg_color="transparent")
        tr.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        tr.grid_columnconfigure(1, weight=1)   # col 0 = icon, col 1 = label, 2 = update, 3 = status
        tr.grid_columnconfigure(0, weight=0)
        tr.grid_columnconfigure(2, weight=0)
        tr.grid_columnconfigure(3, weight=0)

        # ── App icon (Art/icon.png or Art/icon.ico) ───────────────────────────
        _header_icon_label = None
        for _icon_name in ("icon.png", "icon.ico", "logo.png", "logo.ico"):
            _icon_path = ART_DIR / _icon_name
            if _icon_path.exists():
                try:
                    _img = Image.open(_icon_path).resize((26, 26), Image.LANCZOS)
                    self._header_icon = ctk.CTkImage(light_image=_img, dark_image=_img, size=(26, 26))
                    _header_icon_label = ctk.CTkLabel(
                        tr, image=self._header_icon, text="",
                        width=26, height=26,
                    )
                    _header_icon_label.grid(row=0, column=0, sticky="w", padx=(0, 6))
                    break
                except Exception:
                    pass

        ctk.CTkLabel(
            tr, text="SPOAKEN",
            font=FONT_TITLE, text_color=TXT_MAIN, anchor="w",
        ).grid(row=0, column=1, sticky="w")

        # ── Update launcher button (top-right in header title row) ─────────────
        ctk.CTkButton(
            tr,
            text="⟳  Update",
            font=FONT_SMALL, height=24, width=78, corner_radius=5,
            fg_color   = "#00e5cc",
            hover_color= "#00c8b0",
            text_color = "#000000",
            command    = self._open_update_window,
        ).grid(row=0, column=2, padx=(8, 8), sticky="e")

        self.lbl_status = ctk.CTkLabel(
            tr, text="●  IDLE",
            font=FONT_SMALL, text_color=STA_IDLE, anchor="e",
        )
        self.lbl_status.grid(row=0, column=3, sticky="e")

        ctk.CTkFrame(hf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=1, column=0, sticky="ew")

        # Waveform canvas
        self._wf_canvas = tk.Canvas(
            hf, height=52, bg=BG_PANEL, highlightthickness=0,
        )
        self._wf_canvas.grid(row=2, column=0, padx=0, pady=(4, 4), sticky="ew")

        ctk.CTkFrame(hf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=3, column=0, sticky="ew")

    def _build_console(self, parent):
        """Console textbox — row=2 of centre panel, expands vertically."""
        cf = ctk.CTkFrame(
            parent, fg_color=BG_PANEL, corner_radius=8,
            border_color=BORDER_SUB, border_width=1,
        )
        cf.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        cf.grid_rowconfigure(1, weight=1)
        cf.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            cf, text="Console",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(6, 2), sticky="w")

        self.console = ctk.CTkTextbox(
            cf,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            font=FONT_MONO, text_color=TXT_CONSOLE,
            scrollbar_button_color=BORDER_ACT, corner_radius=6,
        )
        self.console.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.console.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Controls
    # ─────────────────────────────────────────────────────────────────────────

    def _build_controls(self, parent):
        cf = ctk.CTkFrame(
            parent, fg_color=BG_CARD,
            border_color=BORDER_SUB, border_width=1, corner_radius=8,
        )
        cf.grid(row=3, column=0, sticky="ew")
        cf.grid_columnconfigure(0, weight=1)

        # ── Row 0: Microphone selector ────────────────────────────────────────
        mic_row = ctk.CTkFrame(cf, fg_color="transparent")
        mic_row.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        mic_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            mic_row, text="Microphone",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w", width=80,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        devices = list_input_devices()
        device_names  = ["System Default"] + [f"{i}: {n}" for i, n in devices]
        self._device_indices = [None] + [i for i, _ in devices]

        self.cmb_mic = ctk.CTkComboBox(
            mic_row,
            values=device_names,
            font=FONT_SMALL, text_color=TXT_MAIN,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_ACT, button_hover_color="#3a60c8",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_MAIN,
            height=30, corner_radius=6,
            command=self._on_mic_change,
        )
        self.cmb_mic.set(device_names[0])
        self.cmb_mic.grid(row=0, column=1, sticky="ew")

        self.btn_noise = ctk.CTkButton(
            mic_row, text="Noise: OFF",
            font=FONT_SMALL, height=30, corner_radius=6, width=90,
            fg_color="#1a2640", hover_color="#253560",
            command=self._toggle_noise,
        )
        self.btn_noise.grid(row=0, column=2, padx=(6, 0))
        self._noise_on = False

        ctk.CTkButton(
            mic_row, text="⚙ Mic Setup",
            font=FONT_SMALL, height=30, corner_radius=6, width=80,
            fg_color="#0d1f3a", hover_color="#1a3060", text_color="#00bdff",
            command=self._open_mic_config,
        ).grid(row=0, column=3, padx=(6, 0))

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=1, column=0, sticky="ew", pady=(6, 0))

        # ── Row 2: Whisper + Vosk on one row ──────────────────────────────────
        wv_row = ctk.CTkFrame(cf, fg_color="transparent")
        wv_row.grid(row=2, column=0, padx=14, pady=(8, 8), sticky="ew")
        wv_row.grid_columnconfigure(1, weight=1)
        wv_row.grid_columnconfigure(3, weight=1)

        self._whisper_enabled = True
        self.lbl_whisper = ctk.CTkLabel(
            wv_row, text="Whisper",
            font=FONT_SMALL, text_color=TXT_WHISPER, anchor="w", width=52,
            cursor="hand2",
        )
        self.lbl_whisper.grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.lbl_whisper.bind("<Button-1>", lambda e: self._toggle_whisper())

        whisper_models = scan_installed_whisper_models()
        self.cmb_whisper = ctk.CTkComboBox(
            wv_row,
            values=whisper_models,
            font=("Courier New", 9), text_color=TXT_WHISPER,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_ACT, button_hover_color="#3a60c8",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_WHISPER,
            height=28, corner_radius=5,
            command=self.controller.swap_whisper_model,
        )
        self.cmb_whisper.set(whisper_models[0])
        self.cmb_whisper.grid(row=0, column=1, sticky="ew", padx=(0, 10))

        self._vosk_enabled = True
        self.lbl_vosk = ctk.CTkLabel(
            wv_row, text="Vosk",
            font=FONT_SMALL, text_color=TXT_VOSK, anchor="w", width=36,
            cursor="hand2",
        )
        self.lbl_vosk.grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.lbl_vosk.bind("<Button-1>", lambda e: self._toggle_vosk())

        vosk_models = scan_installed_vosk_models()
        self.cmb_vosk = ctk.CTkComboBox(
            wv_row,
            values=vosk_models,
            font=("Courier New", 9), text_color=TXT_VOSK,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_TEA, button_hover_color="#0d6080",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_VOSK,
            height=28, corner_radius=5,
            command=self.controller.swap_vosk_model,
        )
        self.cmb_vosk.set(vosk_models[0])
        self.cmb_vosk.grid(row=0, column=3, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            wv_row, text="↺",
            font=("Segoe UI", 12), height=28, width=28, corner_radius=5,
            fg_color="#0d2040", hover_color="#1a3a60",
            command=self._refresh_model_lists,
        ).grid(row=0, column=4)

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=3, column=0, sticky="ew")

        # ── Row 4: LLM model selector ─────────────────────────────────────────
        llm_row = ctk.CTkFrame(cf, fg_color="transparent")
        llm_row.grid(row=4, column=0, padx=14, pady=(8, 8), sticky="ew")
        llm_row.grid_columnconfigure(1, weight=1)

        self._llm_enabled = True
        self.lbl_llm = ctk.CTkLabel(
            llm_row, text="LLM",
            font=FONT_SMALL, text_color="#c084fc", anchor="w", width=52,
            cursor="hand2",
        )
        self.lbl_llm.grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.lbl_llm.bind("<Button-1>", lambda e: self._toggle_llm())

        self._llm_models = _scan_llm_models()
        self.cmb_llm = ctk.CTkComboBox(
            llm_row,
            values=self._llm_models,
            font=("Courier New", 9), text_color="#c084fc",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color="#3a1a60", button_hover_color="#5a2a90",
            dropdown_fg_color=BG_CARD, dropdown_text_color="#c084fc",
            height=28, corner_radius=5,
            command=self._on_llm_model_change,
        )
        self.cmb_llm.set(self._llm_models[0])
        self.cmb_llm.grid(row=0, column=1, sticky="ew", padx=(0, 6))

        llm_btn_frame = ctk.CTkFrame(llm_row, fg_color="transparent")
        llm_btn_frame.grid(row=0, column=2)

        self.btn_llm_translate = ctk.CTkButton(
            llm_btn_frame, text="Translate",
            font=FONT_SMALL, height=28, corner_radius=5, width=70,
            fg_color="#2a1a40", hover_color="#3d2660", text_color="#c084fc",
            command=lambda: self._llm_set_mode("translate"),
        )
        self.btn_llm_translate.grid(row=0, column=0, padx=(0, 2))

        self.btn_llm_summarize = ctk.CTkButton(
            llm_btn_frame, text="Summarize",
            font=FONT_SMALL, height=28, corner_radius=5, width=75,
            fg_color="#1a1a40", hover_color="#282870", text_color="#9090d0",
            command=lambda: self._llm_set_mode("summarize"),
        )
        self.btn_llm_summarize.grid(row=0, column=1, padx=(0, 2))

        ctk.CTkButton(
            llm_btn_frame, text="↺",
            font=("Segoe UI", 12), height=28, width=28, corner_radius=5,
            fg_color="#0d2040", hover_color="#1a3a60",
            command=self._refresh_llm_models,
        ).grid(row=0, column=2)

        self._llm_mode = None

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=5, column=0, sticky="ew", pady=(4, 0))

        # ── Row 6-7: Target window + Lock ────────────────────────────────────
        ctk.CTkLabel(
            cf,
            text="Write to application  (e.g. Notepad, Chrome, LibreOffice)",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=6, column=0, padx=14, pady=(8, 2), sticky="w")

        target_row = ctk.CTkFrame(cf, fg_color="transparent")
        target_row.grid(row=7, column=0, padx=14, pady=(0, 8), sticky="ew")
        target_row.grid_columnconfigure(0, weight=1)

        self.ent_target = ctk.CTkEntry(
            target_row,
            placeholder_text="Enter window title …",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, placeholder_text_color=TXT_DIM,
            height=34, corner_radius=6, font=FONT_UI,
        )
        self.ent_target.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.ent_target.bind("<Return>", lambda e: self.controller.lock_writer_target())

        self.btn_lock = ctk.CTkButton(
            target_row, text="Lock In",
            font=FONT_SMALL, height=34, corner_radius=6,
            fg_color="#1a3a5e", hover_color="#2450a0", width=90,
            command=self.controller.lock_writer_target,
        )
        self.btn_lock.grid(row=0, column=1)

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=8, column=0, sticky="ew")

        # ── Row 9: Start Recording ────────────────────────────────────────────
        self.btn_start = ctk.CTkButton(
            cf, text="Start Recording",
            font=FONT_UI, height=42, corner_radius=6,
            fg_color=BTN_REC, hover_color=BTN_REC_H,
            command=self.controller.toggle_recording,
        )
        self.btn_start.grid(row=9, column=0, padx=14, pady=(8, 6), sticky="ew")

        # ── Row 10: Write | Logs  (wide — top of inverted pyramid) ───────────
        top_btn_row = ctk.CTkFrame(cf, fg_color="transparent")
        top_btn_row.grid(row=10, column=0, padx=14, pady=(0, 4), sticky="ew")
        top_btn_row.grid_columnconfigure(0, weight=1)
        top_btn_row.grid_columnconfigure(1, weight=1)

        self.btn_writing = ctk.CTkButton(
            top_btn_row, text="Write: OFF",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_WOFF, hover_color=BTN_WOFF_H,
            command=self.controller.toggle_page_writing,
        )
        self.btn_writing.grid(row=0, column=0, padx=(0, 3), sticky="ew")

        ctk.CTkButton(
            top_btn_row, text="Logs",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_LOG, hover_color="#146060",
            command=self.controller.open_logs,
        ).grid(row=0, column=1, padx=(3, 0), sticky="ew")

        # ── Row 11: Clear | Polish | Chat  (3 narrower — base of pyramid) ────
        bot_btn_row = ctk.CTkFrame(cf, fg_color="transparent")
        bot_btn_row.grid(row=11, column=0, padx=14, pady=(0, 10), sticky="ew")
        for col in range(3):
            bot_btn_row.grid_columnconfigure(col, weight=1)

        ctk.CTkButton(
            bot_btn_row, text="Clear",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_CLR, hover_color=BTN_CLR_H,
            command=self.controller.clear_all_logs,
        ).grid(row=0, column=0, padx=(0, 3), sticky="ew")

        self.btn_polish = ctk.CTkButton(
            bot_btn_row, text="Polish",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color="#1a3a90", hover_color="#2550cc",
            command=self.controller.swap_polishing,
        )
        self.btn_polish.grid(row=0, column=1, padx=3, sticky="ew")

        self.btn_chat = ctk.CTkButton(
            bot_btn_row, text="Chat ▶",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color="#0d3a40", hover_color="#145060",
            command=self._toggle_sidebar,
        )
        self.btn_chat.grid(row=0, column=2, padx=(3, 0), sticky="ew")

    def _open_mic_config(self):
        """Open the Microphone Setup & Audio Tuning panel."""
        try:
            from spoaken_mic_config import MicConfigPanel
            MicConfigPanel(parent=self, controller=self.controller)
        except Exception as exc:
            self.update_console(f"[Mic Config]: could not open — {exc}")

    def _configure_log_tags(self):
        t = self.log._textbox
        t.tag_configure("vosk",    foreground=TXT_VOSK)
        t.tag_configure("whisper", foreground=TXT_WHISPER)
        t.tag_configure("partial", foreground=TXT_PARTIAL)
        # Legacy tags kept for compat
        t.tag_configure("pending",   foreground=TXT_PARTIAL)
        t.tag_configure("confirmed", foreground=TXT_VOSK)

    # ─────────────────────────────────────────────────────────────────────────
    # Chat sidebar  (full SpoakenLANClient integration)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_sidebar(self):
        self._sidebar_frame = ctk.CTkFrame(
            self, fg_color=BG_SIDEBAR,
            border_color=BORDER_TEA, border_width=1, corner_radius=8,
        )
        # ── State ─────────────────────────────────────────────────────────────
        # Local LAN state
        self._lan_client        = None
        self._lan_current_room  = None
        self._lan_rooms_cache   = {}

        # Online / Matrix state
        self._mx_mode           = False    # False = Local, True = Online
        self._mx_access_token   = None
        self._mx_user_id        = None
        self._mx_homeserver     = "https://matrix.org"
        self._mx_current_room   = None    # room_id
        self._mx_rooms_cache    = {}      # room_id → display_name
        self._mx_since          = None    # sync `since` token
        self._mx_sync_running   = False

        self._sidebar_frame.grid_rowconfigure(4, weight=1)
        self._sidebar_frame.grid_columnconfigure(0, weight=1)

        # ══ Row 0: Header — title  |  Local ⟷ Online toggle  |  Port/Status ═══
        sb_hdr = ctk.CTkFrame(self._sidebar_frame, fg_color=BG_PANEL, corner_radius=0)
        sb_hdr.grid(row=0, column=0, sticky="ew")
        sb_hdr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            sb_hdr, text="◈  Chat",
            font=FONT_TITLE, text_color=TXT_VOSK, anchor="w",
        ).grid(row=0, column=0, padx=(12, 6), pady=6, sticky="w")

        # ── Local ⟷ Online segmented toggle ──────────────────────────────────
        self._mode_var = tk.StringVar(value="Local")
        toggle_frame = ctk.CTkFrame(sb_hdr, fg_color="transparent")
        toggle_frame.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        toggle_frame.grid_columnconfigure(0, weight=1)
        toggle_frame.grid_columnconfigure(1, weight=1)

        self._btn_local = ctk.CTkButton(
            toggle_frame, text="⬡  Local",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color=BORDER_TEA, hover_color="#0d6080",
            text_color=TXT_VOSK,
            command=self._switch_to_local,
        )
        self._btn_local.grid(row=0, column=0, padx=(0, 1), sticky="ew")

        self._btn_online = ctk.CTkButton(
            toggle_frame, text="⬡  Online",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#12182e", hover_color="#1a2a50",
            text_color=TXT_DIM,
            command=self._switch_to_online,
        )
        self._btn_online.grid(row=0, column=1, padx=(1, 0), sticky="ew")

        # Port button — only meaningful in Local mode
        self.btn_port = ctk.CTkButton(
            sb_hdr, text="Port: OFF",
            font=FONT_SMALL, height=26, width=82, corner_radius=5,
            fg_color="#1a2640", hover_color="#253560",
            command=self.controller.toggle_chat_port,
        )
        self.btn_port.grid(row=0, column=2, padx=(0, 4), pady=6)

        ctk.CTkFrame(self._sidebar_frame, height=1, fg_color=BORDER_TEA,
                     corner_radius=0).grid(row=1, column=0, sticky="ew")

        # ══ Row 2: Mode-specific connection panel (swapped by toggle) ══════════
        # This frame is a container that holds either _local_panel or _online_panel
        self._conn_container = ctk.CTkFrame(
            self._sidebar_frame, fg_color="transparent",
        )
        self._conn_container.grid(row=2, column=0, sticky="ew")
        self._conn_container.grid_columnconfigure(0, weight=1)

        self._build_local_panel()
        self._build_online_panel()

        # Show local panel by default
        self._local_panel.grid(row=0, column=0, sticky="ew")

        ctk.CTkFrame(self._sidebar_frame, height=1, fg_color=BORDER_TEA,
                     corner_radius=0).grid(row=3, column=0, sticky="ew")

        # ══ Row 4: Room bar + chat log ══════════════════════════════════════════
        room_row = ctk.CTkFrame(self._sidebar_frame, fg_color="transparent")
        room_row.grid(row=4, column=0, padx=6, pady=(4, 0), sticky="ew")
        room_row.grid_columnconfigure(0, weight=1)

        self._room_var = tk.StringVar(value="(not connected)")
        self.cmb_room = ctk.CTkComboBox(
            room_row,
            variable=self._room_var,
            values=["(not connected)"],
            font=("Courier New", 9),
            text_color=TXT_TEAL,
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            button_color="#0d4d60", button_hover_color="#0d6080",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_TEAL,
            height=26, corner_radius=4,
            command=self._on_room_select,
        )
        self.cmb_room.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self.btn_create_room = ctk.CTkButton(
            room_row, text="+ Room",
            font=FONT_SMALL, height=26, width=58, corner_radius=4,
            fg_color="#0d2a3a", hover_color="#0d4050",
            command=self._on_create_room,
        )
        self.btn_create_room.grid(row=0, column=1)

        # Browse rooms button (for online mode — opens room directory)
        self.btn_browse_rooms = ctk.CTkButton(
            room_row, text="⊞",
            font=("Segoe UI", 13), height=26, width=30, corner_radius=4,
            fg_color="#0d2040", hover_color="#1a3a60",
            text_color=TXT_TEAL,
            command=self._open_room_browser,
        )
        self.btn_browse_rooms.grid(row=0, column=2, padx=(4, 0))
        self.btn_browse_rooms.grid_remove()   # hidden until online mode

        # ── Command bar ───────────────────────────────────────────────────────
        self._build_command_bar()

        # ── Chat messages area ────────────────────────────────────────────────
        self._sidebar_frame.grid_rowconfigure(6, weight=1)
        self._chat_log = ctk.CTkTextbox(
            self._sidebar_frame,
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            font=("Courier New", 10), text_color=TXT_CHAT,
            scrollbar_button_color=BORDER_ACT, corner_radius=6, wrap="word",
        )
        self._chat_log.grid(row=6, column=0, padx=6, pady=(0, 4), sticky="nsew")
        self._chat_log.configure(state="disabled")

        ctk.CTkFrame(self._sidebar_frame, height=1, fg_color=BORDER_TEA,
                     corner_radius=0).grid(row=7, column=0, sticky="ew")

        # ── Message send row ──────────────────────────────────────────────────
        msg_row = ctk.CTkFrame(self._sidebar_frame, fg_color="transparent")
        msg_row.grid(row=8, column=0, padx=6, pady=(4, 8), sticky="ew")
        msg_row.grid_columnconfigure(0, weight=1)

        self._chat_entry = ctk.CTkEntry(
            msg_row,
            placeholder_text="Send message to room …",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=30, corner_radius=4, font=("Segoe UI", 10),
        )
        self._chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._chat_entry.bind("<Return>", self._on_chat_send)

        ctk.CTkButton(
            msg_row, text="Send",
            font=FONT_SMALL, height=30, width=50, corner_radius=4,
            fg_color=BORDER_ACT, hover_color="#3060d0",
            command=self._on_chat_send,
        ).grid(row=0, column=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Local panel  (LAN / Spoaken server)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_local_panel(self):
        """LAN connection card — host / port / username / token + buttons."""
        self._local_panel = ctk.CTkFrame(
            self._conn_container,
            fg_color=BG_CARD,
            border_color=BORDER_TEA, border_width=1, corner_radius=6,
        )
        self._local_panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self._local_panel, text="Host",
            font=FONT_SMALL, text_color=TXT_DIM, width=32, anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=(6, 2), sticky="w")

        self._lan_host_entry = ctk.CTkEntry(
            self._local_panel,
            placeholder_text="192.168.x.x",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9),
        )
        self._lan_host_entry.grid(row=0, column=1, padx=(0, 4), pady=(6, 2), sticky="ew")
        self._lan_host_entry.insert(0, "localhost")

        self._lan_port_entry = ctk.CTkEntry(
            self._local_panel,
            placeholder_text="55300",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9), width=52,
        )
        self._lan_port_entry.grid(row=0, column=2, padx=(0, 8), pady=(6, 2))
        self._lan_port_entry.insert(0, "55300")

        ctk.CTkLabel(
            self._local_panel, text="Name",
            font=FONT_SMALL, text_color=TXT_DIM, width=32, anchor="w",
        ).grid(row=1, column=0, padx=(8, 4), pady=(2, 2), sticky="w")

        self._lan_user_entry = ctk.CTkEntry(
            self._local_panel,
            placeholder_text="username",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9),
        )
        self._lan_user_entry.grid(row=1, column=1, padx=(0, 4), pady=(2, 2), sticky="ew")
        self._lan_user_entry.insert(0, "spoaken")

        self._lan_token_entry = ctk.CTkEntry(
            self._local_panel,
            placeholder_text="token",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9), width=52, show="*",
        )
        self._lan_token_entry.grid(row=1, column=2, padx=(0, 8), pady=(2, 2))
        self._lan_token_entry.insert(0, "spoaken")

        conn_btn_row = ctk.CTkFrame(self._local_panel, fg_color="transparent")
        conn_btn_row.grid(row=2, column=0, columnspan=3, padx=8, pady=(2, 8), sticky="ew")
        for c in range(3):
            conn_btn_row.grid_columnconfigure(c, weight=1)

        self.btn_lan_connect = ctk.CTkButton(
            conn_btn_row, text="Connect",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#0d3a40", hover_color="#145060",
            command=self._on_lan_connect,
        )
        self.btn_lan_connect.grid(row=0, column=0, padx=(0, 2), sticky="ew")

        self.btn_lan_scan = ctk.CTkButton(
            conn_btn_row, text="Scan LAN",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#1a2030", hover_color="#253050",
            command=self._on_lan_scan,
        )
        self.btn_lan_scan.grid(row=0, column=1, padx=2, sticky="ew")

        self.btn_lan_disconnect = ctk.CTkButton(
            conn_btn_row, text="Disconnect",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#3a1a1a", hover_color="#5a2020",
            command=self._on_lan_disconnect, state="disabled",
        )
        self.btn_lan_disconnect.grid(row=0, column=2, padx=(2, 0), sticky="ew")

    # ─────────────────────────────────────────────────────────────────────────
    # Online panel  (Matrix.org protocol)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_online_panel(self):
        """Matrix homeserver login card."""
        self._online_panel = ctk.CTkFrame(
            self._conn_container,
            fg_color=BG_CARD,
            border_color="#2a1a60", border_width=1, corner_radius=6,
        )
        self._online_panel.grid_columnconfigure(1, weight=1)

        # Homeserver
        ctk.CTkLabel(
            self._online_panel, text="Server",
            font=FONT_SMALL, text_color=TXT_DIM, width=42, anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=(6, 2), sticky="w")

        self._mx_server_entry = ctk.CTkEntry(
            self._online_panel,
            placeholder_text="matrix.org",
            fg_color=BG_INPUT, border_color="#2a1a60", border_width=1,
            text_color="#c084fc", placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9),
        )
        self._mx_server_entry.grid(row=0, column=1, columnspan=2, padx=(0, 8),
                                   pady=(6, 2), sticky="ew")
        self._mx_server_entry.insert(0, "matrix.org")

        # Username
        ctk.CTkLabel(
            self._online_panel, text="User",
            font=FONT_SMALL, text_color=TXT_DIM, width=42, anchor="w",
        ).grid(row=1, column=0, padx=(8, 4), pady=(2, 2), sticky="w")

        self._mx_user_entry = ctk.CTkEntry(
            self._online_panel,
            placeholder_text="@you:matrix.org",
            fg_color=BG_INPUT, border_color="#2a1a60", border_width=1,
            text_color="#c084fc", placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9),
        )
        self._mx_user_entry.grid(row=1, column=1, columnspan=2, padx=(0, 8),
                                 pady=(2, 2), sticky="ew")

        # Password
        ctk.CTkLabel(
            self._online_panel, text="Pass",
            font=FONT_SMALL, text_color=TXT_DIM, width=42, anchor="w",
        ).grid(row=2, column=0, padx=(8, 4), pady=(2, 2), sticky="w")

        self._mx_pass_entry = ctk.CTkEntry(
            self._online_panel,
            placeholder_text="password",
            fg_color=BG_INPUT, border_color="#2a1a60", border_width=1,
            text_color="#c084fc", placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9), show="*",
        )
        self._mx_pass_entry.grid(row=2, column=1, padx=(0, 4), pady=(2, 2), sticky="ew")

        # Access token (paste-in alternative to password)
        self._mx_token_entry = ctk.CTkEntry(
            self._online_panel,
            placeholder_text="token",
            fg_color=BG_INPUT, border_color="#2a1a60", border_width=1,
            text_color=TXT_DIM, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9), width=52, show="*",
        )
        self._mx_token_entry.grid(row=2, column=2, padx=(0, 8), pady=(2, 2))

        # Small hint label
        ctk.CTkLabel(
            self._online_panel,
            text="  ← or paste access token →",
            font=("Segoe UI", 7), text_color=TXT_DIM, anchor="w",
        ).grid(row=3, column=0, columnspan=3, padx=8, pady=(0, 2), sticky="w")

        # Action buttons
        mx_btn_row = ctk.CTkFrame(self._online_panel, fg_color="transparent")
        mx_btn_row.grid(row=4, column=0, columnspan=3, padx=8, pady=(2, 8), sticky="ew")
        for c in range(3):
            mx_btn_row.grid_columnconfigure(c, weight=1)

        self.btn_mx_login = ctk.CTkButton(
            mx_btn_row, text="Login",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#2a1a50", hover_color="#3d2680",
            text_color="#c084fc",
            command=self._on_mx_login,
        )
        self.btn_mx_login.grid(row=0, column=0, padx=(0, 2), sticky="ew")

        self.btn_mx_register = ctk.CTkButton(
            mx_btn_row, text="Register",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#1a1a40", hover_color="#282870",
            text_color="#9090d0",
            command=self._on_mx_register,
        )
        self.btn_mx_register.grid(row=0, column=1, padx=2, sticky="ew")

        self.btn_mx_logout = ctk.CTkButton(
            mx_btn_row, text="Logout",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#3a1a1a", hover_color="#5a2020",
            command=self._on_mx_logout, state="disabled",
        )
        self.btn_mx_logout.grid(row=0, column=2, padx=(2, 0), sticky="ew")

        # Status label
        self._mx_status_lbl = ctk.CTkLabel(
            self._online_panel, text="  Not logged in",
            font=("Segoe UI", 8), text_color=TXT_DIM, anchor="w",
        )
        self._mx_status_lbl.grid(row=5, column=0, columnspan=3,
                                 padx=8, pady=(0, 6), sticky="w")

    # ─────────────────────────────────────────────────────────────────────────
    # Mode toggle — Local ⟷ Online
    # ─────────────────────────────────────────────────────────────────────────

    def _switch_to_local(self):
        if not self._mx_mode:
            return
        self._mx_mode = False
        # Update toggle button appearance
        self._btn_local.configure(fg_color=BORDER_TEA, text_color=TXT_VOSK)
        self._btn_online.configure(fg_color="#12182e", text_color=TXT_DIM)
        # Swap panels
        self._online_panel.grid_remove()
        self._local_panel.grid(row=0, column=0, sticky="ew")
        # Show Port button, hide Browse rooms
        self.btn_port.grid()
        self.btn_browse_rooms.grid_remove()
        self.btn_create_room.grid()
        self.chat_receive("[ ⬡ Switched to Local mode ]")
        # Stop any matrix sync
        self._mx_sync_running = False

    def _switch_to_online(self):
        if self._mx_mode:
            return
        self._mx_mode = True
        self._btn_online.configure(fg_color="#3a1a70", text_color="#c084fc")
        self._btn_local.configure(fg_color="#12182e", text_color=TXT_DIM)
        # Swap panels
        self._local_panel.grid_remove()
        self._online_panel.grid(row=0, column=0, sticky="ew")
        # Hide Port button (meaningless in online mode), show Browse
        self.btn_port.grid_remove()
        self.btn_browse_rooms.grid()
        self.chat_receive("[ ⬡ Switched to Online mode ]")
        self.chat_receive("[Online]: Enter homeserver, username, password then Login.")

    # ─────────────────────────────────────────────────────────────────────────
    # Matrix helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _mx_homeserver_url(self) -> str:
        raw = self._mx_server_entry.get().strip() or "matrix.org"
        if not raw.startswith("http"):
            raw = "https://" + raw
        return raw.rstrip("/")

    def _mx_api(self, method: str, path: str, body: dict = None,
                token: str = None, timeout: int = 15) -> dict:
        """
        Synchronous Matrix Client-Server API call.
        Returns parsed JSON dict or raises on error.
        Uses only stdlib urllib — no extra dependencies.
        """
        import urllib.request, urllib.error, json as _json
        url  = self._mx_homeserver_url() + "/_matrix/client/v3" + path
        data = _json.dumps(body).encode() if body else None
        req  = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            err_body = {}
            try:
                err_body = _json.loads(exc.read().decode())
            except Exception:
                pass
            raise RuntimeError(err_body.get("error", str(exc))) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Matrix login / register / logout
    # ─────────────────────────────────────────────────────────────────────────

    def _on_mx_login(self):
        # Check if a raw access token was pasted instead
        raw_token = self._mx_token_entry.get().strip()
        if raw_token and raw_token not in ("token", ""):
            # Direct token login
            self._mx_access_token = raw_token
            uid = self._mx_user_entry.get().strip() or "unknown"
            self._mx_user_id = uid
            self._on_mx_logged_in(uid)
            return

        server = self._mx_homeserver_url()
        user   = self._mx_user_entry.get().strip()
        pw     = self._mx_pass_entry.get()

        if not user or not pw:
            self.chat_receive("[Online]: Enter username and password (or paste token).")
            return

        # Strip @user:server → just the localpart for the API
        localpart = user.lstrip("@").split(":")[0] if ":" in user else user.lstrip("@")

        self.chat_receive(f"[Online]: Logging in as {user} on {server} …")
        self.btn_mx_login.configure(state="disabled")

        def _do():
            try:
                resp = self._mx_api("POST", "/login", {
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": localpart},
                    "password": pw,
                    "initial_device_display_name": "Spoaken",
                })
                token = resp["access_token"]
                uid   = resp["user_id"]
                self._mx_access_token = token
                self._mx_user_id      = uid
                self.after(0, lambda: self._on_mx_logged_in(uid))
            except Exception as exc:
                self.after(0, lambda e=exc: (
                    self.chat_receive(f"[Online Error]: {e}"),
                    self.btn_mx_login.configure(state="normal"),
                ))
        threading.Thread(target=_do, daemon=True).start()

    def _on_mx_logged_in(self, uid: str):
        self.btn_mx_login.configure(state="disabled")
        self.btn_mx_register.configure(state="disabled")
        self.btn_mx_logout.configure(state="normal")
        self._mx_status_lbl.configure(
            text=f"  ✔ {uid}", text_color=TXT_VOSK,
        )
        self.chat_receive(f"[Online]: ✔ Logged in as {uid}")
        # Fetch joined rooms immediately
        self._mx_refresh_rooms()
        # Start long-poll sync
        self._mx_sync_running = True
        threading.Thread(target=self._mx_sync_loop, daemon=True).start()

    def _on_mx_register(self):
        """Open the homeserver's registration URL in the system browser."""
        import webbrowser
        server = self._mx_homeserver_url()
        url    = f"{server}/_matrix/static/"
        try:
            webbrowser.open(url)
            self.chat_receive(f"[Online]: Opened registration page: {url}")
        except Exception as exc:
            self.chat_receive(f"[Online]: Could not open browser: {exc}")

    def _on_mx_logout(self):
        if not self._mx_access_token:
            return
        self._mx_sync_running = False
        def _do():
            try:
                self._mx_api("POST", "/logout", {}, token=self._mx_access_token)
            except Exception:
                pass
            self._mx_access_token = None
            self._mx_user_id      = None
            self._mx_current_room = None
            self._mx_rooms_cache  = {}
            self.after(0, self._on_mx_logged_out)
        threading.Thread(target=_do, daemon=True).start()

    def _on_mx_logged_out(self):
        self.btn_mx_login.configure(state="normal")
        self.btn_mx_register.configure(state="normal")
        self.btn_mx_logout.configure(state="disabled")
        self._mx_status_lbl.configure(text="  Not logged in", text_color=TXT_DIM)
        self.cmb_room.configure(values=["(not connected)"])
        self.cmb_room.set("(not connected)")
        self.chat_receive("[Online]: Logged out.")

    # ─────────────────────────────────────────────────────────────────────────
    # Matrix rooms
    # ─────────────────────────────────────────────────────────────────────────

    def _mx_refresh_rooms(self):
        """Fetch joined rooms and populate the room dropdown."""
        if not self._mx_access_token:
            return
        def _do():
            try:
                resp = self._mx_api("GET", "/joined_rooms",
                                    token=self._mx_access_token)
                room_ids = resp.get("joined_rooms", [])
                cache = {}
                display = []
                for rid in room_ids:
                    # Try to get a friendly name
                    try:
                        nr = self._mx_api(
                            "GET", f"/rooms/{rid}/state/m.room.name",
                            token=self._mx_access_token,
                        )
                        name = nr.get("name", rid)
                    except Exception:
                        name = rid
                    cache[rid] = name
                    display.append(f"{name}")
                self._mx_rooms_cache = cache
                if not display:
                    display = ["(no joined rooms)"]
                self.after(0, lambda d=display: (
                    self.cmb_room.configure(values=d),
                    self.cmb_room.set(d[0]),
                    self.chat_receive(
                        f"[Online]: {len(room_ids)} joined room(s) — "
                        "use ⊞ to browse public rooms"
                    ),
                ))
            except Exception as exc:
                self.after(0, lambda e=exc:
                    self.chat_receive(f"[Online Room Error]: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    def _open_room_browser(self):
        """Popup window: public room directory with search."""
        if not self._mx_access_token:
            self.chat_receive("[Online]: Login first.")
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title("Browse Public Rooms")
        dlg.geometry("520x460")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(True, True)

        # ── Search bar ─────────────────────────────────────────────────────
        top = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0)
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)

        srch = ctk.CTkEntry(
            top, placeholder_text="Search rooms (e.g. spoaken, general, python) …",
            fg_color=BG_INPUT, border_color="#2a1a60", border_width=1,
            text_color="#c084fc", placeholder_text_color=TXT_DIM,
            height=32, corner_radius=6, font=FONT_UI,
        )
        srch.grid(row=0, column=0, padx=8, pady=8, sticky="ew")

        ctk.CTkButton(
            top, text="Search", height=32, width=70, corner_radius=6,
            fg_color="#2a1a50", hover_color="#3d2680", text_color="#c084fc",
            font=FONT_SMALL,
            command=lambda: _run_search(srch.get().strip()),
        ).grid(row=0, column=1, padx=(0, 8), pady=8)

        # Also search on Enter
        srch.bind("<Return>", lambda e: _run_search(srch.get().strip()))

        # ── Results list ───────────────────────────────────────────────────
        results_frame = ctk.CTkScrollableFrame(
            dlg, fg_color=BG_CARD, corner_radius=0,
        )
        results_frame.pack(fill="both", expand=True, padx=0, pady=0)
        results_frame.grid_columnconfigure(0, weight=1)

        status_lbl = ctk.CTkLabel(
            dlg, text="Type a search term and press Search",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        )
        status_lbl.pack(fill="x", padx=10, pady=(0, 6))

        _room_widgets = []

        def _clear_results():
            for w in _room_widgets:
                w.destroy()
            _room_widgets.clear()

        def _run_search(term: str):
            status_lbl.configure(text="  Searching …", text_color=TXT_DIM)
            _clear_results()

            def _do():
                try:
                    params = {"limit": 40}
                    if term:
                        params["filter"] = term
                    # Build query string
                    import urllib.parse
                    qs = urllib.parse.urlencode(params)
                    resp = self._mx_api(
                        "GET",
                        f"/publicRooms?{qs}",
                        token=self._mx_access_token,
                        timeout=20,
                    )
                    rooms = resp.get("chunk", [])
                    self.after(0, lambda r=rooms: _populate(r))
                except Exception as exc:
                    self.after(0, lambda e=exc:
                        status_lbl.configure(
                            text=f"  Error: {e}", text_color="#ff6060",
                        ))

            threading.Thread(target=_do, daemon=True).start()

        def _populate(rooms: list):
            _clear_results()
            status_lbl.configure(
                text=f"  {len(rooms)} room(s) found",
                text_color=TXT_DIM,
            )
            for i, r in enumerate(rooms):
                room_id    = r.get("room_id", "")
                alias      = r.get("canonical_alias", room_id)
                name       = r.get("name", alias or room_id)
                topic      = r.get("topic", "")
                members    = r.get("num_joined_members", 0)

                row_f = ctk.CTkFrame(
                    results_frame, fg_color=BG_SIDEBAR if i % 2 else BG_CARD,
                    corner_radius=4,
                )
                row_f.grid(row=i, column=0, padx=4, pady=2, sticky="ew")
                row_f.grid_columnconfigure(0, weight=1)
                _room_widgets.append(row_f)

                ctk.CTkLabel(
                    row_f, text=f"  {name}",
                    font=("Segoe UI Semibold", 10), text_color=TXT_TEAL,
                    anchor="w",
                ).grid(row=0, column=0, padx=4, pady=(4, 0), sticky="w")

                ctk.CTkLabel(
                    row_f, text=f"  {alias}   👥 {members}",
                    font=("Courier New", 8), text_color=TXT_DIM, anchor="w",
                ).grid(row=1, column=0, padx=4, pady=0, sticky="w")

                if topic:
                    short_topic = (topic[:80] + "…") if len(topic) > 80 else topic
                    ctk.CTkLabel(
                        row_f, text=f"  {short_topic}",
                        font=("Segoe UI", 8), text_color="#4a6080", anchor="w",
                        wraplength=340,
                    ).grid(row=2, column=0, padx=4, pady=(0, 2), sticky="w")

                ctk.CTkButton(
                    row_f, text="Join",
                    font=FONT_SMALL, height=24, width=46, corner_radius=4,
                    fg_color="#0d3a40", hover_color="#145060",
                    command=lambda rid=room_id, n=name, d=dlg: _join(rid, n, d),
                ).grid(row=0, column=1, rowspan=3, padx=6, pady=4)

        def _join(room_id: str, name: str, parent_dlg):
            parent_dlg.destroy()
            self.chat_receive(f"[Online]: Joining {name} …")
            def _do():
                try:
                    resp = self._mx_api(
                        "POST", f"/join/{urllib.parse.quote(room_id)}",
                        {}, token=self._mx_access_token,
                    )
                    joined_id = resp.get("room_id", room_id)
                    self._mx_current_room = joined_id
                    self._mx_rooms_cache[joined_id] = name
                    self.after(0, lambda: (
                        self.chat_receive(f"[Online]: ✔ Joined {name}"),
                        self._mx_refresh_rooms(),
                    ))
                except Exception as exc:
                    self.after(0, lambda e=exc:
                        self.chat_receive(f"[Online Join Error]: {e}"))
            threading.Thread(target=_do, daemon=True).start()

        # Load top rooms immediately
        _run_search("")

    def _mx_join_by_alias(self, alias: str):
        """Join a room by alias (e.g. #general:matrix.org)."""
        if not self._mx_access_token:
            self.chat_receive("[Online]: Login first.")
            return
        import urllib.parse
        self.chat_receive(f"[Online]: Joining {alias} …")
        def _do():
            try:
                resp = self._mx_api(
                    "POST", f"/join/{urllib.parse.quote(alias)}",
                    {}, token=self._mx_access_token,
                )
                room_id = resp.get("room_id", alias)
                self._mx_current_room = room_id
                self._mx_rooms_cache[room_id] = alias
                self.after(0, lambda: (
                    self.chat_receive(f"[Online]: ✔ Joined {alias}"),
                    self._mx_refresh_rooms(),
                ))
            except Exception as exc:
                self.after(0, lambda e=exc:
                    self.chat_receive(f"[Online Join Error]: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Matrix sync loop  (long-poll for new events)
    # ─────────────────────────────────────────────────────────────────────────

    def _mx_sync_loop(self):
        """Runs in background thread; calls /sync with 30 s timeout."""
        import urllib.parse, json as _json, urllib.request, urllib.error
        since = None
        while self._mx_sync_running and self._mx_access_token:
            try:
                params = {"timeout": "30000", "full_state": "false"}
                if since:
                    params["since"] = since
                qs   = urllib.parse.urlencode(params)
                resp = self._mx_api(
                    "GET", f"/sync?{qs}",
                    token=self._mx_access_token, timeout=40,
                )
                since = resp.get("next_batch", since)
                self._mx_since = since
                # Process room events
                rooms_join = resp.get("rooms", {}).get("join", {})
                for room_id, room_data in rooms_join.items():
                    timeline = room_data.get("timeline", {}).get("events", [])
                    for ev in timeline:
                        self._mx_handle_event(room_id, ev)
                # Process invites
                invites = resp.get("rooms", {}).get("invite", {})
                for room_id in invites:
                    self.after(0, lambda rid=room_id:
                        self.chat_receive(
                            f"[Online]: Invited to {rid} — use ⊞ browser to join"
                        ))
            except Exception as exc:
                if self._mx_sync_running:
                    self.after(0, lambda e=exc:
                        self.chat_receive(f"[Online Sync]: {e} (retrying …)"))
                    import time as _time
                    _time.sleep(5)

    def _mx_handle_event(self, room_id: str, event: dict):
        """Process a Matrix event from the sync loop."""
        ev_type = event.get("type", "")
        content = event.get("content", {})
        sender  = event.get("sender", "?")
        # Show sender as just the localpart
        short   = sender.split(":")[0].lstrip("@") if ":" in sender else sender

        if ev_type == "m.room.message":
            body  = content.get("body", "")
            mtype = content.get("msgtype", "m.text")
            # Only show messages for the room we're currently in, or all if none selected
            if self._mx_current_room is None or room_id == self._mx_current_room:
                room_name = self._mx_rooms_cache.get(room_id, room_id[:16])
                prefix = f"[{room_name}]" if self._mx_current_room is None else ""
                if mtype == "m.text":
                    self.after(0, lambda s=short, b=body, p=prefix:
                        self.chat_receive(f"{p}[{s}]: {b}"))
                elif mtype == "m.image":
                    url = content.get("url", "")
                    self.after(0, lambda s=short, u=url, p=prefix:
                        self.chat_receive(f"{p}[{s}]: 🖼  {u}"))
                elif mtype == "m.file":
                    fn = content.get("filename", "file")
                    self.after(0, lambda s=short, f=fn, p=prefix:
                        self.chat_receive(f"{p}[{s}]: 📎  {f}"))

        elif ev_type == "m.room.member":
            membership = content.get("membership", "")
            if membership in ("join", "leave"):
                if self._mx_current_room is None or room_id == self._mx_current_room:
                    verb = "joined" if membership == "join" else "left"
                    self.after(0, lambda s=short, v=verb:
                        self.chat_receive(f"  ── {s} {v} ──"))

    # ─────────────────────────────────────────────────────────────────────────
    # Matrix send message
    # ─────────────────────────────────────────────────────────────────────────

    def _mx_send(self, msg: str):
        """Send a message to the currently active Matrix room."""
        if not self._mx_access_token or not self._mx_current_room:
            self.chat_receive("[Online]: Join a room first.")
            return
        import time as _time
        txn_id = str(int(_time.time() * 1000))
        room   = self._mx_current_room
        def _do():
            try:
                self._mx_api(
                    "PUT",
                    f"/rooms/{room}/send/m.room.message/{txn_id}",
                    {"msgtype": "m.text", "body": msg},
                    token=self._mx_access_token,
                )
            except Exception as exc:
                self.after(0, lambda e=exc:
                    self.chat_receive(f"[Online Send Error]: {e}"))
        threading.Thread(target=_do, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Spoaken Commands bar  (sidebar section, powered by spoaken_commands.py)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_command_bar(self):
        """
        Build the command-entry panel inside the sidebar.
        All command routing goes through controller._parse_command() which
        delegates to spoaken_commands.CommandParser.
        """
        outer = ctk.CTkFrame(
            self._sidebar_frame,
            fg_color=BG_CARD,
            border_color=BORDER_TEA, border_width=1, corner_radius=6,
        )
        outer.grid(row=5, column=0, padx=6, pady=(4, 4), sticky="ew")
        outer.grid_columnconfigure(0, weight=1)

        # Label row
        lbl_row = ctk.CTkFrame(outer, fg_color="transparent")
        lbl_row.grid(row=0, column=0, padx=8, pady=(6, 2), sticky="ew")
        lbl_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            lbl_row,
            text="⌘  Commands",
            font=("Segoe UI Semibold", 10),
            text_color=TXT_TEAL, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        # Inline help hint
        ctk.CTkLabel(
            lbl_row,
            text="type  help  for full list",
            font=("Segoe UI", 8),
            text_color=TXT_DIM, anchor="e",
        ).grid(row=0, column=1, sticky="e")

        # Entry + Run row
        entry_row = ctk.CTkFrame(outer, fg_color="transparent")
        entry_row.grid(row=1, column=0, padx=6, pady=(0, 6), sticky="ew")
        entry_row.grid_columnconfigure(0, weight=1)

        self._cmd_entry = ctk.CTkEntry(
            entry_row,
            placeholder_text="spoaken.translate(french)  ·  clear  ·  help …",
            fg_color=BG_INPUT,
            border_color=BORDER_TEA, border_width=1,
            text_color=TXT_WHISPER,
            placeholder_text_color=TXT_DIM,
            height=30, corner_radius=5,
            font=("Courier New", 10),
        )
        self._cmd_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._cmd_entry.bind("<Return>", self._on_cmd_submit)

        # History navigation (↑ / ↓)
        self._cmd_history : list[str] = []
        self._cmd_hist_idx : int = -1
        self._cmd_entry.bind("<Up>",   self._cmd_hist_up)
        self._cmd_entry.bind("<Down>", self._cmd_hist_down)

        btn_col = ctk.CTkFrame(entry_row, fg_color="transparent")
        btn_col.grid(row=0, column=1)

        ctk.CTkButton(
            btn_col, text="Run",
            font=FONT_SMALL, height=30, width=40, corner_radius=5,
            fg_color="#0d3a40", hover_color="#145060",
            command=self._on_cmd_submit,
        ).grid(row=0, column=0, padx=(0, 2))

        ctk.CTkButton(
            btn_col, text="?",
            font=FONT_SMALL, height=30, width=28, corner_radius=5,
            fg_color="#0d2a3a", hover_color="#0d4050",
            text_color=TXT_TEAL,
            command=self._show_command_help,
        ).grid(row=0, column=1)

    def _show_command_help(self):
        """Trigger the help command through the controller pipeline."""
        self._run_command("help")

    def _cmd_hist_up(self, event=None):
        if not self._cmd_history:
            return
        self._cmd_hist_idx = max(0, self._cmd_hist_idx - 1)
        self._cmd_entry.delete(0, "end")
        self._cmd_entry.insert(0, self._cmd_history[self._cmd_hist_idx])

    def _cmd_hist_down(self, event=None):
        if not self._cmd_history:
            return
        if self._cmd_hist_idx < len(self._cmd_history) - 1:
            self._cmd_hist_idx += 1
            self._cmd_entry.delete(0, "end")
            self._cmd_entry.insert(0, self._cmd_history[self._cmd_hist_idx])
        else:
            self._cmd_hist_idx = len(self._cmd_history)
            self._cmd_entry.delete(0, "end")

    def _run_command(self, cmd_text: str):
        """Execute a command string and echo it to the chat log."""
        if not cmd_text.strip():
            return
        # Add to history (avoid consecutive duplicates)
        if not self._cmd_history or self._cmd_history[-1] != cmd_text:
            self._cmd_history.append(cmd_text)
        self._cmd_hist_idx = len(self._cmd_history)

        self.chat_receive(f"⌘ {cmd_text}")
        self.controller._parse_command(cmd_text)

    # ─────────────────────────────────────────────────────────────────────────
    # Update window launcher
    # ─────────────────────────────────────────────────────────────────────────

    def _open_update_window(self):
        """Open SpoakenUpdater as a toplevel from the header button."""
        try:
            from spoaken_update import SpoakenUpdater
            SpoakenUpdater(self)
        except Exception as exc:
            self.update_console(f"[Update Error]: {exc}")

    def _toggle_sidebar(self):
        self._sidebar_open = not self._sidebar_open
        _chat_width = 280
        if self._sidebar_open:
            self.grid_columnconfigure(2, weight=0, minsize=_chat_width)
            self._sidebar_frame.grid(
                row=0, column=2, padx=(0, 10), pady=10, sticky="nsew"
            )
            self.btn_chat.configure(text="Chat ◀")
            new_w = self._base_width + _chat_width + 14  # 14 = padding
            self.geometry(f"{new_w}x{self.winfo_height()}")
        else:
            self._sidebar_frame.grid_remove()
            self.grid_columnconfigure(2, weight=0, minsize=0)
            self.btn_chat.configure(text="Chat ▶")
            # Restore original width
            self.geometry(f"{self._base_width}x{self.winfo_height()}")

    def chat_receive(self, message: str):
        """Called from controller when a chat peer sends a message."""
        self._chat_log.configure(state="normal")
        self._chat_log.insert("end", f"{message}\n")
        self._chat_log.see("end")
        self._chat_log.configure(state="disabled")

    def _on_chat_send(self, event=None):
        msg = self._chat_entry.get().strip()
        if not msg:
            return
        self._chat_entry.delete(0, "end")
        self.chat_receive(f"[Me]: {msg}")
        if self._mx_mode:
            self._mx_send(msg)
        elif self._lan_client and self._lan_client.is_connected() and self._lan_current_room:
            self._lan_client.send_message(self._lan_current_room, msg)
        else:
            self.controller.chat_send(msg)

    def _on_cmd_submit(self, event=None):
        cmd = self._cmd_entry.get().strip()
        if not cmd:
            return
        self._cmd_entry.delete(0, "end")
        self._run_command(cmd)

    # ─────────────────────────────────────────────────────────────────────────
    # Mic and noise callbacks
    # ─────────────────────────────────────────────────────────────────────────

    def _on_mic_change(self, choice: str):
        idx = self._device_indices[
            self.cmb_mic._values.index(choice) if hasattr(self.cmb_mic, "_values")
            else list(self.cmb_mic.cget("values")).index(choice)
        ]
        self.controller.set_mic_device(idx)

    def _toggle_noise(self):
        self._noise_on = not self._noise_on
        self.btn_noise.configure(
            text=f"Noise: {'ON' if self._noise_on else 'OFF'}",
            fg_color="#0d4040" if self._noise_on else "#1a2640",
            hover_color="#156060" if self._noise_on else "#253560",
        )
        self.controller.toggle_noise_suppression(self._noise_on)

    # ─────────────────────────────────────────────────────────────────────────
    # LAN Chat handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _on_lan_connect(self):
        host  = self._lan_host_entry.get().strip() or "localhost"
        port  = int(self._lan_port_entry.get().strip() or "55300")
        uname = self._lan_user_entry.get().strip() or "spoaken"
        token = self._lan_token_entry.get().strip() or "spoaken"

        self.chat_receive(f"[LAN]: Connecting to {host}:{port} as {uname} …")

        def _connect_bg():
            try:
                from spoaken_chat import SpoakenLANClient
                client = SpoakenLANClient(
                    username     = uname,
                    server_token = token,
                    on_event     = self._on_lan_event,
                    log_cb       = self.thread_safety_console,
                )
                ok = client.connect(host, port)
                if ok:
                    self._lan_client = client
                    self.after(0, self._on_lan_connected)
                    client.list_rooms()
                else:
                    self.after(0, lambda: self.chat_receive("[LAN]: Connection failed."))
            except Exception as exc:
                self.after(0, lambda: self.chat_receive(f"[LAN Error]: {exc}"))

        import threading as _t
        _t.Thread(target=_connect_bg, daemon=True).start()

    def _on_lan_connected(self):
        self.btn_lan_connect.configure(state="disabled")
        self.btn_lan_disconnect.configure(state="normal")
        self.chat_receive("[LAN]: ✔ Connected")

    def _on_lan_disconnect(self):
        if self._lan_client:
            self._lan_client.disconnect()
            self._lan_client = None
        self._lan_current_room = None
        self._lan_rooms_cache  = {}
        self.btn_lan_connect.configure(state="normal")
        self.btn_lan_disconnect.configure(state="disabled")
        self.cmb_room.configure(values=["(not connected)"])
        self.cmb_room.set("(not connected)")
        self.chat_receive("[LAN]: Disconnected.")

    def _on_lan_scan(self):
        self.chat_receive("[LAN]: Scanning for servers …")
        def _scan():
            try:
                from spoaken_chat import discover_servers
                servers = discover_servers(wait=2.0)
                if servers:
                    for s in servers:
                        self.after(0, lambda s=s: self.chat_receive(
                            f"[LAN]: Found  {s.name}  @ {s.ip}:{s.port}"
                            f"  ({s.room_count} rooms)"
                        ))
                        # Auto-fill the host/port fields with first result
                        self.after(0, lambda s=s: (
                            self._lan_host_entry.delete(0, "end"),
                            self._lan_host_entry.insert(0, s.ip),
                            self._lan_port_entry.delete(0, "end"),
                            self._lan_port_entry.insert(0, str(s.port)),
                        ))
                else:
                    self.after(0, lambda: self.chat_receive(
                        "[LAN]: No servers found on this network."
                    ))
            except Exception as exc:
                self.after(0, lambda: self.chat_receive(f"[LAN Scan Error]: {exc}"))
        import threading as _t
        _t.Thread(target=_scan, daemon=True).start()

    def _on_lan_event(self, event: dict):
        """Called from SpoakenLANClient receive loop (background thread)."""
        t = event.get("type", "")
        c = event.get("content", {})

        if t == "m.room.list":
            rooms = c.get("rooms", [])
            self._lan_rooms_cache = {r["room_id"]: r for r in rooms}
            names = [f"{r['name']}  ({r['room_id'][:12]})" for r in rooms]
            if not names:
                names = ["(no public rooms)"]
            self.after(0, lambda n=names: (
                self.cmb_room.configure(values=n),
                self.cmb_room.set(n[0]),
            ))
            self.after(0, lambda: self.chat_receive(
                f"[LAN]: {len(rooms)} room(s) available"
            ))

        elif t == "m.room.joined":
            room_name = c.get("name", "?")
            self.after(0, lambda: self.chat_receive(
                f"[LAN]: ✔ Joined room '{room_name}'"
            ))
            history = c.get("history", [])
            for ev in history[-20:]:   # show last 20 events
                if ev.get("type") == "m.room.message":
                    body   = ev.get("content", {}).get("body", "")
                    sender = ev.get("sender", "?").split("@")[1].split(":")[0] \
                             if "@" in ev.get("sender", "") else ev.get("sender", "?")
                    self.after(0, lambda s=sender, b=body:
                        self.chat_receive(f"  {s}: {b}"))

        elif t == "m.room.message":
            body   = c.get("body", "")
            sender = event.get("sender", "?")
            if "@" in sender:
                sender = sender.split("@")[1].split(":")[0]
            self.after(0, lambda s=sender, b=body:
                self.chat_receive(f"[{s}]: {b}"))

        elif t == "m.room.created":
            room_id = c.get("room_id", "")
            name    = c.get("name", "room")
            self.after(0, lambda: self.chat_receive(
                f"[LAN]: Room '{name}' created — joining …"
            ))
            if self._lan_client and room_id:
                self._lan_current_room = room_id
                self._lan_client.list_rooms()

        elif t == "m.room.member":
            membership = c.get("membership", "")
            uname      = c.get("username", "?")
            self.after(0, lambda: self.chat_receive(
                f"[LAN]: {uname} {membership}"
            ))

        elif t == "m.client.disconnected":
            self.after(0, self._on_lan_disconnect)

        elif t == "m.error":
            err = c.get("error", "unknown error")
            self.after(0, lambda: self.chat_receive(f"[LAN Error]: {err}"))

        elif t in ("m.auth.ok",):
            pass   # handled elsewhere

        elif t == "m.server.shutdown":
            self.after(0, lambda: self.chat_receive("[LAN]: Server shutting down."))
            self.after(0, self._on_lan_disconnect)

    def _on_room_select(self, choice: str):
        """User picked a room from the dropdown — join it."""
        if self._mx_mode:
            # Online: find room_id from cache by display name
            for rid, name in self._mx_rooms_cache.items():
                if name == choice:
                    self._mx_current_room = rid
                    self.chat_receive(f"[Online]: Active room → {choice}")
                    return
            # Might be a raw alias or id typed in
            if choice.startswith("#") or choice.startswith("!"):
                self._mx_join_by_alias(choice)
            return

        if not self._lan_client or not self._lan_client.is_connected():
            self.chat_receive("[LAN]: Not connected.")
            return
        room_id = None
        for rid, rd in self._lan_rooms_cache.items():
            display = f"{rd['name']}  ({rid[:12]})"
            if display == choice:
                room_id = rid
                break
        if not room_id:
            return
        self._join_room_with_password(room_id)

    def _join_room_with_password(self, room_id: str):
        """Pop a minimal password dialog then join the room."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Join Room")
        dlg.geometry("280x130")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, False)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Room password:", font=FONT_SMALL,
                     text_color=TXT_DIM).pack(pady=(18, 4))
        pw_entry = ctk.CTkEntry(dlg, show="*", height=30,
                                fg_color=BG_INPUT, border_color=BORDER_TEA,
                                text_color=TXT_CHAT, corner_radius=4)
        pw_entry.pack(padx=20, fill="x")
        pw_entry.focus()

        def _do_join():
            pw = pw_entry.get()
            dlg.destroy()
            if self._lan_client:
                self._lan_current_room = room_id
                self._lan_client.join_room(room_id, pw)

        pw_entry.bind("<Return>", lambda e: _do_join())
        ctk.CTkButton(dlg, text="Join", height=28, corner_radius=4,
                      fg_color="#0d3a40", hover_color="#145060",
                      command=_do_join).pack(pady=10)

    def _on_create_room(self):
        """Dialog to create a new room — LAN or Matrix depending on mode."""
        if self._mx_mode:
            self._mx_create_room_dialog()
            return
        if not self._lan_client or not self._lan_client.is_connected():
            self.chat_receive("[LAN]: Not connected — cannot create room.")
            return
        self._lan_create_room_dialog()

    def _lan_create_room_dialog(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Create Room")
        dlg.geometry("300x200")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, False)
        dlg.grab_set()

        for label, placeholder, show in [
            ("Room name", "Physics Lab", ""),
            ("Password",  "secret",      "*"),
            ("Topic",     "optional …",  ""),
        ]:
            row = ctk.CTkFrame(dlg, fg_color="transparent")
            row.pack(padx=14, pady=3, fill="x")
            ctk.CTkLabel(row, text=label, font=FONT_SMALL,
                         text_color=TXT_DIM, width=72, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, height=26, fg_color=BG_INPUT,
                             border_color=BORDER_TEA, text_color=TXT_CHAT,
                             corner_radius=4, font=("Courier New", 9),
                             placeholder_text=placeholder, show=show)
            e.pack(side="left", expand=True, fill="x")
            if label == "Room name":
                name_entry = e
            elif label == "Password":
                pw_entry = e
            else:
                topic_entry = e

        def _create():
            n = name_entry.get().strip()
            p = pw_entry.get()
            t = topic_entry.get().strip()
            dlg.destroy()
            if n and p and self._lan_client:
                self._lan_client.create_room(n, p, public=True, topic=t)

        ctk.CTkButton(dlg, text="Create", height=30, corner_radius=4,
                      fg_color=BORDER_ACT, hover_color="#3060d0",
                      command=_create).pack(pady=10)

    def _mx_create_room_dialog(self):
        """Create a Matrix room on the homeserver."""
        if not self._mx_access_token:
            self.chat_receive("[Online]: Login first.")
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title("Create Online Room")
        dlg.geometry("320x240")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, False)
        dlg.grab_set()

        fields = {}
        for label, placeholder, show in [
            ("Room name",  "My Spoaken Room", ""),
            ("Alias",      "my-room  (optional)", ""),
            ("Topic",      "optional description …", ""),
        ]:
            row = ctk.CTkFrame(dlg, fg_color="transparent")
            row.pack(padx=14, pady=4, fill="x")
            ctk.CTkLabel(row, text=label, font=FONT_SMALL,
                         text_color=TXT_DIM, width=80, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, height=26, fg_color=BG_INPUT,
                             border_color="#2a1a60", text_color="#c084fc",
                             corner_radius=4, font=("Courier New", 9),
                             placeholder_text=placeholder, show=show)
            e.pack(side="left", expand=True, fill="x")
            fields[label] = e

        # Public/Private toggle
        visibility_var = tk.StringVar(value="public")
        vis_row = ctk.CTkFrame(dlg, fg_color="transparent")
        vis_row.pack(padx=14, pady=4, fill="x")
        ctk.CTkLabel(vis_row, text="Visibility", font=FONT_SMALL,
                     text_color=TXT_DIM, width=80, anchor="w").pack(side="left")
        ctk.CTkRadioButton(vis_row, text="Public", variable=visibility_var,
                           value="public", font=FONT_SMALL,
                           text_color="#c084fc").pack(side="left", padx=(0, 12))
        ctk.CTkRadioButton(vis_row, text="Private", variable=visibility_var,
                           value="private", font=FONT_SMALL,
                           text_color=TXT_DIM).pack(side="left")

        def _create():
            name    = fields["Room name"].get().strip()
            alias   = fields["Alias"].get().strip().replace(" ", "-").lower() or None
            topic   = fields["Topic"].get().strip() or None
            is_pub  = visibility_var.get() == "public"
            dlg.destroy()
            if not name:
                return
            body = {
                "name": name,
                "visibility": "public" if is_pub else "private",
                "preset": "public_chat" if is_pub else "private_chat",
            }
            if alias:
                body["room_alias_name"] = alias
            if topic:
                body["topic"] = topic
            self.chat_receive(f"[Online]: Creating room '{name}' …")
            def _do():
                try:
                    resp = self._mx_api("POST", "/createRoom", body,
                                        token=self._mx_access_token)
                    room_id = resp.get("room_id", "")
                    self._mx_current_room = room_id
                    self._mx_rooms_cache[room_id] = name
                    self.after(0, lambda: (
                        self.chat_receive(f"[Online]: ✔ Room '{name}' created"),
                        self._mx_refresh_rooms(),
                    ))
                except Exception as exc:
                    self.after(0, lambda e=exc:
                        self.chat_receive(f"[Online Create Error]: {e}"))
            threading.Thread(target=_do, daemon=True).start()

        ctk.CTkButton(dlg, text="Create Room", height=30, corner_radius=4,
                      fg_color="#2a1a50", hover_color="#3d2680",
                      text_color="#c084fc",
                      command=_create).pack(pady=10)

    # ─────────────────────────────────────────────────────────────────────────
    # LLM toggle / mode methods
    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_llm(self):
        self._llm_enabled = not self._llm_enabled
        if self._llm_enabled:
            self.lbl_llm.configure(text="LLM", text_color="#c084fc")
            self.cmb_llm.configure(
                state="normal", fg_color=BG_INPUT,
                text_color="#c084fc", button_color="#3a1a60",
            )
        else:
            self.lbl_llm.configure(text="LLM ✕", text_color=TXT_DIM)
            self.cmb_llm.configure(
                state="disabled", fg_color="#0a0f20",
                text_color=TXT_DIM, button_color="#111a30",
            )
            self._llm_mode = None
            self._update_llm_mode_buttons()
        self.controller.set_llm_enabled(self._llm_enabled)
        self.update_console(
            f"[Console]: LLM engine {'enabled' if self._llm_enabled else 'disabled'}"
        )

    def _llm_set_mode(self, mode: str):
        """Toggle translate / summarize mode for LLM output."""
        if not self._llm_enabled:
            self.update_console("[Console]: Enable LLM first (click the LLM label)")
            return
        if self._llm_mode == mode:
            self._llm_mode = None   # second click = toggle off
        else:
            self._llm_mode = mode
        self._update_llm_mode_buttons()
        self.controller.set_llm_mode(self._llm_mode, self.cmb_llm.get())
        self.update_console(
            f"[Console]: LLM mode → {self._llm_mode or 'off'}"
        )

    def _update_llm_mode_buttons(self):
        if self._llm_mode == "translate":
            self.btn_llm_translate.configure(fg_color="#5a2a90", text_color="#e0b0ff")
            self.btn_llm_summarize.configure(fg_color="#1a1a40", text_color="#9090d0")
        elif self._llm_mode == "summarize":
            self.btn_llm_translate.configure(fg_color="#2a1a40", text_color="#c084fc")
            self.btn_llm_summarize.configure(fg_color="#282870", text_color="#b0b0f0")
        else:
            self.btn_llm_translate.configure(fg_color="#2a1a40", text_color="#c084fc")
            self.btn_llm_summarize.configure(fg_color="#1a1a40", text_color="#9090d0")

    def _on_llm_model_change(self, choice: str):
        self.controller.set_llm_model(choice)
        self.update_console(f"[Console]: LLM model → {choice}")

    def _refresh_llm_models(self):
        """Re-scan Ollama for available models."""
        models = _scan_llm_models()
        self.cmb_llm.configure(values=models)
        if self.cmb_llm.get() not in models:
            self.cmb_llm.set(models[0])
        self.update_console(
            f"[Console]: {len([m for m in models if not m.startswith('(')])} LLM model(s) found"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Engine enable / disable toggles  (click the label to swap state)
    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_vosk(self):
        """
        Toggle Vosk on/off.
        • Enabled  → label is teal, combo is interactive.
        • Disabled → label is dimmed with strikethrough-style prefix,
                     combo is faded and non-interactive.
        """
        self._vosk_enabled = not self._vosk_enabled
        if self._vosk_enabled:
            self.lbl_vosk.configure(text="Vosk", text_color=TXT_VOSK)
            self.cmb_vosk.configure(
                state="normal",
                fg_color=BG_INPUT,
                text_color=TXT_VOSK,
                button_color=BORDER_TEA,
            )
        else:
            self.lbl_vosk.configure(text="Vosk ✕", text_color=TXT_DIM)
            self.cmb_vosk.configure(
                state="disabled",
                fg_color="#0a0f20",
                text_color=TXT_DIM,
                button_color="#111a30",
            )
        self.controller.set_engine_enabled("vosk", self._vosk_enabled)
        self.update_console(
            f"[Console]: Vosk engine {'enabled' if self._vosk_enabled else 'disabled'}"
        )

    def _toggle_whisper(self):
        """
        Toggle Whisper on/off.
        • Enabled  → label is cyan, combo is interactive.
        • Disabled → label is dimmed with strikethrough-style prefix,
                     combo is faded and non-interactive.
        """
        self._whisper_enabled = not self._whisper_enabled
        if self._whisper_enabled:
            self.lbl_whisper.configure(text="Whisper", text_color=TXT_WHISPER)
            self.cmb_whisper.configure(
                state="normal",
                fg_color=BG_INPUT,
                text_color=TXT_WHISPER,
                button_color=BORDER_ACT,
            )
        else:
            self.lbl_whisper.configure(text="Whisper ✕", text_color=TXT_DIM)
            self.cmb_whisper.configure(
                state="disabled",
                fg_color="#0a0f20",
                text_color=TXT_DIM,
                button_color="#111a30",
            )
        self.controller.set_engine_enabled("whisper", self._whisper_enabled)
        self.update_console(
            f"[Console]: Whisper engine {'enabled' if self._whisper_enabled else 'disabled'}"
        )

    def _refresh_model_lists(self):
        """Rescan the model directories and update both dropdowns."""
        vosk_models    = scan_installed_vosk_models()
        whisper_models = scan_installed_whisper_models()

        # CTkComboBox.configure(values=...) updates the dropdown list
        self.cmb_vosk.configure(values=vosk_models)
        self.cmb_whisper.configure(values=whisper_models)

        # Keep displayed value valid
        if self.cmb_vosk.get() not in vosk_models:
            self.cmb_vosk.set(vosk_models[0])
        if self.cmb_whisper.get() not in whisper_models:
            self.cmb_whisper.set(whisper_models[0])

        self.controller.view.update_console(
            f"[Console]: model list refreshed — "
            f"{len([m for m in vosk_models if not m.startswith('(')])} Vosk, "
            f"{len([m for m in whisper_models if not m.startswith('(')])} Whisper"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Waveform animation
    # ─────────────────────────────────────────────────────────────────────────

    def push_audio_level(self, rms: float):
        """Called from controller callback with real-time RMS level (0–1)."""
        self._audio_rms = min(rms * 5.0, 1.0)   # scale up for visibility

    def _wf_loop(self):
        try:
            self._draw_waveform()
        except Exception:
            pass
        self.after(_WF_FPS, self._wf_loop)

    def _draw_waveform(self):
        canvas = self._wf_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10 or h < 10:
            return

        self._wf_t += 0.05
        t  = self._wf_t
        n  = _WF_BARS
        bw = w / n

        canvas.delete("wf")

        for i in range(n):
            phase = i / n

            if self._wf_state == "recording":
                # Drive bar heights from real RMS + smooth oscillation
                rms_component = self._audio_rms * (0.5 + 0.5 * abs(math.sin(t * 8 + phase * 6.28)))
                self._wf_targets[i] = min(
                    rms_component * 0.85 + 0.08 * abs(math.sin(t * 3 + phase * 9.42)),
                    1.0,
                )
            elif self._wf_state == "correcting":
                self._wf_targets[i] = (
                    0.20 + 0.55 * math.sin(t * 3.2 + phase * 9.42) ** 2
                )
            else:
                self._wf_targets[i] = (
                    0.03 + 0.06 * math.sin(t * 1.1 + phase * 4.71)
                    + 0.03 * math.sin(t * 0.6 + phase * 9.42)
                )

            spd = 0.35 if self._wf_state == "recording" else 0.15
            self._wf_heights[i] += (self._wf_targets[i] - self._wf_heights[i]) * spd

            bar_h = max(2, int(self._wf_heights[i] * h * 0.90))
            x0    = int(i * bw) + 1
            x1    = max(x0 + 1, int((i + 1) * bw) - 1)
            y0    = h - bar_h

            bright = self._wf_heights[i]
            if self._wf_state == "recording":
                colour = _lerp_colour(*WF_REC, bright)
            elif self._wf_state == "correcting":
                colour = _lerp_colour(*WF_CORR, bright)
            else:
                colour = _lerp_colour(*WF_IDLE, bright)

            canvas.create_rectangle(x0, y0, x1, h, fill=colour, outline="", tags="wf")

    def set_waveform_state(self, state: str):
        self._wf_state = state

    def thread_safety_waveform(self, state: str):
        self.after(0, self.set_waveform_state, state)

    def update_chat_port_btn(self, is_open: bool):
        """
        Update the Port toggle button to reflect the current server state.
        is_open=True  → green  'Port: ON'
        is_open=False → dim    'Port: OFF'
        """
        if is_open:
            self.btn_port.configure(
                text="Port: ON",
                fg_color="#0d4040",
                hover_color="#156060",
            )
        else:
            self.btn_port.configure(
                text="Port: OFF",
                fg_color="#1a2640",
                hover_color="#253560",
            )

    def thread_safety_chat_port_btn(self, is_open: bool):
        self.after(0, self.update_chat_port_btn, is_open)

    # ─────────────────────────────────────────────────────────────────────────
    # Lock button state
    # ─────────────────────────────────────────────────────────────────────────

    def update_lock_btn(self, locked: bool):
        """
        locked=True  → button becomes red 'Unlock'
        locked=False → button becomes blue 'Lock In'
        """
        if locked:
            self.btn_lock.configure(
                text="Unlock",
                fg_color="#5a1a1a",
                hover_color="#8a2828",
            )
        else:
            self.btn_lock.configure(
                text="Lock In",
                fg_color="#1a3a5e",
                hover_color="#2450a0",
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Console
    # ─────────────────────────────────────────────────────────────────────────

    def update_console(self, message: str):
        self.console.configure(state="normal")
        self.console.insert("end", f"{message}\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def thread_safety_console(self, message: str):
        self.after(0, self.update_console, message)

    # ─────────────────────────────────────────────────────────────────────────
    # Status label
    # ─────────────────────────────────────────────────────────────────────────

    def update_status(self, label: str, color: str):
        self.lbl_status.configure(text=f"●  {label}", text_color=color)

    def thread_safety_status(self, label: str, color: str):
        self.after(0, self.update_status, label, color)

    # ─────────────────────────────────────────────────────────────────────────
    # Writing button
    # ─────────────────────────────────────────────────────────────────────────

    def set_writing_btn(self, active: bool):
        if active:
            self.btn_writing.configure(
                text="Write: ON", fg_color=BTN_WON, hover_color=BTN_WON_H,
            )
        else:
            self.btn_writing.configure(
                text="Write: OFF", fg_color=BTN_WOFF, hover_color=BTN_WOFF_H,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Transcript segment insertion / replacement
    # ─────────────────────────────────────────────────────────────────────────

    def insert_pending_segment(self, text: str, seg_id: int, tag: str = "vosk"):
        t = self.log._textbox
        t.configure(state="normal")
        t.insert("end", text, (tag, f"seg_{seg_id}"))
        t.see("end")
        t.configure(state="disabled")

    def thread_safety_insert_pending(self, text: str, seg_id: int, tag: str = "vosk"):
        self.after(0, self.insert_pending_segment, text, seg_id, tag)

    def replace_segments(
        self, seg_ids: list, corrected_text: str, tag: str = "vosk"
    ):
        if not seg_ids:
            return
        t = self.log._textbox
        first_tag = f"seg_{seg_ids[0]}"
        last_tag  = f"seg_{seg_ids[-1]}"
        t.configure(state="normal")
        try:
            first_ranges = t.tag_ranges(first_tag)
            last_ranges  = t.tag_ranges(last_tag)
            if first_ranges and last_ranges:
                t.delete(first_ranges[0], last_ranges[1])
                t.insert(first_ranges[0], corrected_text, (tag,))
                for sid in seg_ids:
                    t.tag_delete(f"seg_{sid}")
            else:
                t.insert("end", corrected_text, (tag,))
        except Exception:
            t.insert("end", corrected_text, (tag,))
        finally:
            t.see("end")
            t.configure(state="disabled")

    def thread_safety_replace_segments(
        self, seg_ids: list, corrected_text: str, tag: str = "vosk"
    ):
        self.after(0, self.replace_segments, seg_ids, corrected_text, tag)

    # ─────────────────────────────────────────────────────────────────────────
    # Misc
    # ─────────────────────────────────────────────────────────────────────────

    def flush(self):
        pass
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
