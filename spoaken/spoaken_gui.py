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

# ── Optional-module availability flags ──────────────────────────────────────
# These are checked at build time so every widget that depends on an optional
# file is disabled (not hidden) immediately if that file has been deleted.
# We use find_spec() — zero imports, zero side-effects.
import importlib.util as _iutil

_LLM_AVAILABLE    = _iutil.find_spec("spoaken_llm")         is not None
_P2P_AVAILABLE    = _iutil.find_spec("spoaken_chat_online")  is not None
_UPDATE_AVAILABLE = _iutil.find_spec("spoaken_update")       is not None
_WRITER_AVAILABLE = _iutil.find_spec("spoaken_writer")       is not None

# Human-readable reason strings shown in tooltips / console when unavailable
_LLM_REASON    = "spoaken_llm.py not found — delete removed Ollama support"
_P2P_REASON    = "spoaken_chat_online.py not found — P2P/Tor chat unavailable"
_UPDATE_REASON = "spoaken_update.py not found — updater unavailable"
_WRITER_REASON = "spoaken_writer.py not found — window writing unavailable"

del _iutil   # keep namespace clean

# ── LLM module is optional — imported lazily to avoid blocking startup ───────
def _scan_llm_models() -> list[str]:
    """Return list of Ollama models (empty list if Ollama not running)."""
    try:
        from spoaken_llm import list_ollama_models
        models = list_ollama_models()
        return models if models else ["(Ollama offline)"]
    except Exception:
        return ["(Ollama not installed)"]


def _scan_t5_models_default() -> list[str]:
    """Return T5 model options — active first, then curated list."""
    try:
        from spoaken_config import T5_MODEL
        active = T5_MODEL
    except Exception:
        active = "vennify/t5-base-grammar-correction"

    # Curated T5 models (mirrors _T5_MODELS in spoaken_update.py)
    _KNOWN = [
        "vennify/t5-base-grammar-correction",
        "prithivida/grammar_error_correcter_v1",
        "Unbabel/gec-t5_small",
        "deep-learning-analytics/GrammarCorrector",
        "pszemraj/grammar-synthesis-small",
        "pszemraj/grammar-synthesis-base",
        "ramsrigouthamg/t5_paraphraser",
        "Vamsi/T5_Paraphrase_Paws",
        "sshleifer/distilbart-cnn-12-6",
        "facebook/bart-large-cnn",
    ]
    ordered = [active] + [m for m in _KNOWN if m != active]
    return ordered

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
BTN_LOG_H  = "#125660"

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

        # ── 2-column layout: [PanedWindow] | [chat sidebar] ─────────────────────
        # The PanedWindow holds transcript (left pane) + controls (right pane),
        # with a draggable sash between them.
        # col 0 = PanedWindow – stretchy
        # col 1 = chat sidebar – hidden until toggled
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0, minsize=0)
        self.grid_rowconfigure(0, weight=1)

        # Draggable horizontal split — drag the sash to resize transcript vs controls
        self._h_pane = tk.PanedWindow(
            self, orient=tk.HORIZONTAL,
            bg=BORDER_SUB,       # sash colour matches the UI border palette
            sashwidth=7,
            sashrelief="flat",
            sashpad=1,
            bd=0,
            opaqueresize=True,
        )
        self._h_pane.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="nsew")

        self._build_transcript_panel()   # adds left pane to _h_pane
        self._build_centre_panel()       # adds right pane to _h_pane
        self._build_sidebar()            # col 1 (hidden until toggled)
        self._configure_log_tags()

        # Set initial sash position after window maps (defer to allow geometry to settle)
        self.after(150, self._restore_sash)

        self.after(_WF_FPS, self._wf_loop)
        self.protocol("WM_DELETE_WINDOW", self.controller.on_close_request)

    def _restore_sash(self, attempt: int = 0):
        """
        Place the sash so the controls pane is ~400 px wide and the transcript
        fills the left ~2/3 of the window.  Retries if not yet mapped.
        """
        try:
            self.update_idletasks()
            total = self._h_pane.winfo_width()
            if total > 600:
                self._h_pane.sash_place(0, total - 400, 0)
                return
        except Exception:
            pass
        if attempt < 5:
            self.after(120 * (attempt + 1), lambda: self._restore_sash(attempt + 1))

    # ─────────────────────────────────────────────────────────────────────────
    # Left panel — Transcript
    # ─────────────────────────────────────────────────────────────────────────

    def _build_transcript_panel(self):
        """Left pane of the PanedWindow: label header + scrollable transcript."""
        lp = ctk.CTkFrame(
            self._h_pane, fg_color=BG_CARD,
            border_color=BORDER_SUB, border_width=1, corner_radius=8,
        )
        self._h_pane.add(lp, minsize=220, stretch="always")
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
        """Right pane of the PanedWindow: header/waveform, console, controls."""
        mp = ctk.CTkFrame(self._h_pane, fg_color="transparent")
        self._h_pane.add(mp, minsize=360, width=400, stretch="never")
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
        _upd_fg    = "#00e5cc" if _UPDATE_AVAILABLE else "#1a2a3a"
        _upd_hover = "#00c8b0" if _UPDATE_AVAILABLE else "#1a2a3a"
        _upd_txt   = "#000000" if _UPDATE_AVAILABLE else "#2a4060"
        _upd_label = "⟳  Update" if _UPDATE_AVAILABLE else "⟳  Update (n/a)"
        _upd_state = "normal"  if _UPDATE_AVAILABLE else "disabled"
        ctk.CTkButton(
            tr,
            text=_upd_label,
            font=FONT_SMALL, height=24, width=100, corner_radius=5,
            fg_color   = _upd_fg,
            hover_color= _upd_hover,
            text_color = _upd_txt,
            state      = _upd_state,
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

        # Header row: label + clear button
        hdr = ctk.CTkFrame(cf, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr, text="Console",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            hdr, text="Clear",
            font=FONT_SMALL, height=18, width=44, corner_radius=4,
            fg_color="transparent", hover_color=BORDER_SUB,
            text_color=TXT_DIM, border_width=0,
            command=self._clear_console,
        ).grid(row=0, column=1, sticky="e")

        self.console = ctk.CTkTextbox(
            cf,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            font=("Courier New", 10), text_color=TXT_CONSOLE,
            scrollbar_button_color=BORDER_ACT, corner_radius=6,
        )
        self.console.grid(row=1, column=0, padx=8, pady=(0, 10), sticky="nsew")
        self.console.configure(state="disabled")

    def _clear_console(self):
        tb = self.console._textbox
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.configure(state="disabled")

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

        # ── Voice-to-Text section label ───────────────────────────────────────
        ctk.CTkLabel(
            cf, text="Voice-to-Text Models",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=2, column=0, padx=16, pady=(6, 0), sticky="w")

        # ── Row 3: Whisper + Vosk on one row ──────────────────────────────────
        wv_row = ctk.CTkFrame(cf, fg_color="transparent")
        wv_row.grid(row=3, column=0, padx=14, pady=(4, 8), sticky="ew")
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
                     ).grid(row=4, column=0, sticky="ew")

        # ── Text-to-Text Models section label ─────────────────────────────────
        ctk.CTkLabel(
            cf, text="Text-to-Text Models",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=5, column=0, padx=16, pady=(6, 0), sticky="w")

        # ── Row 6: LLM (Ollama) + T5 transformer on one row ──────────────────
        t2t_row = ctk.CTkFrame(cf, fg_color="transparent")
        t2t_row.grid(row=6, column=0, padx=14, pady=(4, 8), sticky="ew")
        t2t_row.grid_columnconfigure(1, weight=1)
        t2t_row.grid_columnconfigure(3, weight=1)

        self._llm_enabled = True
        self._llm_models  = _scan_llm_models() if _LLM_AVAILABLE else ["(unavailable)"]
        _llm_col   = "#c084fc" if _LLM_AVAILABLE else TXT_DIM
        _llm_state = "normal"  if _LLM_AVAILABLE else "disabled"
        _llm_tip   = "" if _LLM_AVAILABLE else f"  ({_LLM_REASON})"
        self.lbl_llm = ctk.CTkLabel(
            t2t_row, text="LLM",
            font=FONT_SMALL, text_color=_llm_col, anchor="w", width=36,
            cursor="hand2" if _LLM_AVAILABLE else "arrow",
        )
        self.lbl_llm.grid(row=0, column=0, sticky="w", padx=(0, 4))
        if _LLM_AVAILABLE:
            self.lbl_llm.bind("<Button-1>", lambda e: self._toggle_llm())

        _llm_display_models = self._llm_models if _LLM_AVAILABLE else ["(unavailable)"]
        self.cmb_llm = ctk.CTkComboBox(
            t2t_row,
            values=_llm_display_models,
            font=("Courier New", 9), text_color=_llm_col,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color="#3a1a60" if _LLM_AVAILABLE else BORDER_SUB,
            button_hover_color="#5a2a90" if _LLM_AVAILABLE else BORDER_SUB,
            dropdown_fg_color=BG_CARD, dropdown_text_color=_llm_col,
            height=28, corner_radius=5,
            state=_llm_state,
            command=self._on_llm_model_change,
        )
        self.cmb_llm.set(_llm_display_models[0])
        self.cmb_llm.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        # T5 model selector (grammar / paraphrase / summarise)
        self._t5_enabled = True
        self.lbl_t5 = ctk.CTkLabel(
            t2t_row, text="T5",
            font=FONT_SMALL, text_color="#fbbf24", anchor="w", width=28,
            cursor="hand2",
        )
        self.lbl_t5.grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.lbl_t5.bind("<Button-1>", lambda e: self._toggle_t5())

        self._t5_models = self._scan_t5_models()
        self.cmb_t5 = ctk.CTkComboBox(
            t2t_row,
            values=self._t5_models,
            font=("Courier New", 9), text_color="#fbbf24",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color="#60480a", button_hover_color="#8a6810",
            dropdown_fg_color=BG_CARD, dropdown_text_color="#fbbf24",
            height=28, corner_radius=5,
            command=self._on_t5_model_change,
        )
        self.cmb_t5.set(self._t5_models[0])
        self.cmb_t5.grid(row=0, column=3, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            t2t_row, text="↺",
            font=("Segoe UI", 12), height=28, width=28, corner_radius=5,
            fg_color="#0d2040", hover_color="#1a3a60",
            command=self._refresh_t2t_models,
        ).grid(row=0, column=4)

        # LLM action buttons row
        llm_btn_row = ctk.CTkFrame(cf, fg_color="transparent")
        llm_btn_row.grid(row=7, column=0, padx=14, pady=(0, 6), sticky="ew")
        llm_btn_row.grid_columnconfigure(0, weight=1)
        llm_btn_row.grid_columnconfigure(1, weight=1)

        self.btn_llm_translate = ctk.CTkButton(
            llm_btn_row, text="Translate",
            font=FONT_SMALL, height=28, corner_radius=5,
            fg_color="#2a1a40" if _LLM_AVAILABLE else "#181820",
            hover_color="#3d2660" if _LLM_AVAILABLE else "#181820",
            text_color="#c084fc" if _LLM_AVAILABLE else TXT_DIM,
            state="normal" if _LLM_AVAILABLE else "disabled",
            command=lambda: self._llm_set_mode("translate"),
        )
        self.btn_llm_translate.grid(row=0, column=0, padx=(0, 2), sticky="ew")

        self.btn_llm_summarize = ctk.CTkButton(
            llm_btn_row, text="Summarize",
            font=FONT_SMALL, height=28, corner_radius=5,
            fg_color="#1a1a40" if _LLM_AVAILABLE else "#181820",
            hover_color="#282870" if _LLM_AVAILABLE else "#181820",
            text_color="#9090d0" if _LLM_AVAILABLE else TXT_DIM,
            state="normal" if _LLM_AVAILABLE else "disabled",
            command=lambda: self._llm_set_mode("summarize"),
        )
        self.btn_llm_summarize.grid(row=0, column=1, padx=(0, 2), sticky="ew")

        self.btn_t5_correct = ctk.CTkButton(
            llm_btn_row, text="T5 Correct",
            font=FONT_SMALL, height=28, corner_radius=5,
            fg_color="#3d2e08", hover_color="#5a4410", text_color="#fbbf24",
            command=lambda: self._t5_set_mode("correct"),
        )
        self.btn_t5_correct.grid(row=0, column=2, padx=(0, 2), sticky="ew")

        self._llm_mode = None
        self._t5_mode = None

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=8, column=0, sticky="ew", pady=(4, 0))

        # ── Row 9-10: Target window + Lock ────────────────────────────────────
        _wrt_lbl = (
            "Write to application  (e.g. Notepad, Chrome, LibreOffice)"
            if _WRITER_AVAILABLE else
            "Write to application  [unavailable — spoaken_writer.py deleted]"
        )
        ctk.CTkLabel(
            cf,
            text=_wrt_lbl,
            font=FONT_SMALL,
            text_color=TXT_DIM if _WRITER_AVAILABLE else "#2a3a50",
            anchor="w",
        ).grid(row=9, column=0, padx=14, pady=(8, 2), sticky="w")

        target_row = ctk.CTkFrame(cf, fg_color="transparent")
        target_row.grid(row=10, column=0, padx=14, pady=(0, 8), sticky="ew")
        target_row.grid_columnconfigure(0, weight=1)

        self.ent_target = ctk.CTkEntry(
            target_row,
            placeholder_text="Enter window title …" if _WRITER_AVAILABLE
                             else "unavailable",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN if _WRITER_AVAILABLE else TXT_DIM,
            placeholder_text_color=TXT_DIM,
            height=34, corner_radius=6, font=FONT_UI,
            state="normal" if _WRITER_AVAILABLE else "disabled",
        )
        self.ent_target.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        if _WRITER_AVAILABLE:
            self.ent_target.bind("<Return>",
                                 lambda e: self.controller.lock_writer_target())

        self.btn_lock = ctk.CTkButton(
            target_row, text="Lock In",
            font=FONT_SMALL, height=34, corner_radius=6,
            fg_color="#1a3a5e" if _WRITER_AVAILABLE else "#0d1f30",
            hover_color="#2450a0" if _WRITER_AVAILABLE else "#0d1f30",
            text_color=TXT_MAIN if _WRITER_AVAILABLE else TXT_DIM,
            width=90,
            state="normal" if _WRITER_AVAILABLE else "disabled",
            command=self.controller.lock_writer_target,
        )
        self.btn_lock.grid(row=0, column=1)

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=11, column=0, sticky="ew")

        # ── Row 12: Start Recording ────────────────────────────────────────────
        self.btn_start = ctk.CTkButton(
            cf, text="Start Recording",
            font=FONT_UI, height=42, corner_radius=6,
            fg_color=BTN_REC, hover_color=BTN_REC_H,
            command=self.controller.toggle_recording,
        )
        self.btn_start.grid(row=12, column=0, padx=14, pady=(8, 6), sticky="ew")

        # ── Row 13: Write | Logs  (wide — top of inverted pyramid) ───────────
        top_btn_row = ctk.CTkFrame(cf, fg_color="transparent")
        top_btn_row.grid(row=13, column=0, padx=14, pady=(0, 4), sticky="ew")
        top_btn_row.grid_columnconfigure(0, weight=1)
        top_btn_row.grid_columnconfigure(1, weight=1)

        self.btn_writing = ctk.CTkButton(
            top_btn_row, text="Write: OFF",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_WOFF if _WRITER_AVAILABLE else "#0d1520",
            hover_color=BTN_WOFF_H if _WRITER_AVAILABLE else "#0d1520",
            text_color=TXT_MAIN if _WRITER_AVAILABLE else TXT_DIM,
            state="normal" if _WRITER_AVAILABLE else "disabled",
            command=self.controller.toggle_page_writing,
        )
        self.btn_writing.grid(row=0, column=0, padx=(0, 3), sticky="ew")

        ctk.CTkButton(
            top_btn_row, text="Logs",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_LOG, hover_color="#146060",
            command=self.controller.open_logs,
        ).grid(row=0, column=1, padx=(3, 0), sticky="ew")

        # ── Row 14: Clear | Polish | Chat  (3 narrower — base of pyramid) ────
        bot_btn_row = ctk.CTkFrame(cf, fg_color="transparent")
        bot_btn_row.grid(row=14, column=0, padx=14, pady=(0, 10), sticky="ew")
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
        # ── Transcript tags ───────────────────────────────────────────────────
        t = self.log._textbox
        t.tag_configure("vosk",      foreground=TXT_VOSK)
        t.tag_configure("whisper",   foreground=TXT_WHISPER)
        t.tag_configure("partial",   foreground=TXT_PARTIAL)
        t.tag_configure("pending",   foreground=TXT_PARTIAL)   # legacy
        t.tag_configure("confirmed", foreground=TXT_VOSK)      # legacy

        # ── Console tags — severity-coded ─────────────────────────────────────
        c = self.console._textbox
        c.tag_configure("con_ts",      foreground="#1a3460", font=("Courier New", 9))
        c.tag_configure("con_info",    foreground=TXT_CONSOLE)
        c.tag_configure("con_success", foreground="#00e5a0",  font=("Courier New", 11, "bold"))
        c.tag_configure("con_warning", foreground="#f0c040")
        c.tag_configure("con_error",   foreground="#ff5555",  font=("Courier New", 11, "bold"))
        c.tag_configure("con_dim",     foreground="#1a3060")
        c.tag_configure("con_sep",     foreground="#0d2040")

        # ── Chat log tags ─────────────────────────────────────────────────────
        cl = self._chat_log._textbox
        cl.tag_configure("chat_peer",   foreground="#e8f4ff",
                         font=("Segoe UI", 10, "bold"))
        cl.tag_configure("chat_me",     foreground=TXT_TEAL,
                         font=("Segoe UI", 10))
        cl.tag_configure("chat_system", foreground=TXT_DIM,
                         font=("Segoe UI", 8))
        cl.tag_configure("chat_header", foreground=TXT_VOSK,
                         font=("Segoe UI", 9, "bold"))
        cl.tag_configure("chat_error",  foreground="#ff6060",
                         font=("Segoe UI", 9))

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

        self._p2p_mode          = False    # False = Local, True = P2P/Online
        self._p2p_node          = None     # SpoakenP2PNode instance
        self._p2p_current_room  = None     # room_id string
        self._p2p_rooms_cache   = {}       # room_id → display_name

        self._sidebar_frame.grid_rowconfigure(4, weight=1)
        self._sidebar_frame.grid_columnconfigure(0, weight=1)

        # ══ Row 0: Header — title + status badge ════════════════════════════════
        sb_hdr = ctk.CTkFrame(self._sidebar_frame, fg_color=BG_PANEL, corner_radius=0)
        sb_hdr.grid(row=0, column=0, sticky="ew")
        sb_hdr.grid_columnconfigure(1, weight=1)

        # Title + live connection status badge on the same row
        title_cell = ctk.CTkFrame(sb_hdr, fg_color="transparent")
        title_cell.grid(row=0, column=0, padx=(10, 4), pady=(8, 2), sticky="w")

        ctk.CTkLabel(
            title_cell, text="💬  Chat",
            font=FONT_TITLE, text_color=TXT_VOSK, anchor="w",
        ).pack(side="left")

        self._conn_status_lbl = ctk.CTkLabel(
            title_cell, text="  ● offline",
            font=("Segoe UI", 8), text_color=STA_IDLE,
        )
        self._conn_status_lbl.pack(side="left", padx=(6, 0))

        # ── Local ⟷ Online segmented toggle ──────────────────────────────────
        self._mode_var = tk.StringVar(value="Local")
        toggle_frame = ctk.CTkFrame(sb_hdr, fg_color="transparent")
        toggle_frame.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        toggle_frame.grid_columnconfigure(0, weight=1)
        toggle_frame.grid_columnconfigure(1, weight=1)

        self._btn_local = ctk.CTkButton(
            toggle_frame, text="🖧  LAN",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color=BORDER_TEA, hover_color="#0d6080",
            text_color=TXT_VOSK,
            command=self._switch_to_local,
        )
        self._btn_local.grid(row=0, column=0, padx=(0, 1), sticky="ew")

        self._btn_online = ctk.CTkButton(
            toggle_frame, text="🌐  P2P" if _P2P_AVAILABLE else "🌐  P2P (n/a)",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#12182e", hover_color="#1a2a50" if _P2P_AVAILABLE else "#12182e",
            text_color=TXT_DIM,
            state="normal" if _P2P_AVAILABLE else "disabled",
            command=self._switch_to_online,
        )
        self._btn_online.grid(row=0, column=1, padx=(1, 0), sticky="ew")

        # LAN Access toggle — amber when OFF, red when ON
        # OFF = amber/yellow  →  "LAN Access: Off"   (inviting the user to enable)
        # ON  = red           →  "LAN Access: On"    (active / prominent warning)
        self._port_on = False
        self.btn_port = ctk.CTkButton(
            sb_hdr, text="LAN Access: Off",
            font=FONT_SMALL, height=26, width=110, corner_radius=5,
            fg_color="#3d2e00", hover_color="#5a4400",
            text_color="#f0c040",
            border_color="#f0c040", border_width=1,
            command=self._on_toggle_port,
        )
        self.btn_port.grid(row=0, column=2, padx=(0, 6), pady=6)

        # Mode hint — updates when toggling LAN vs P2P
        self._mode_hint_lbl = ctk.CTkLabel(
            sb_hdr,
            text="  Connect to a Spoaken server on your local network",
            font=("Segoe UI", 7), text_color=TXT_DIM, anchor="w",
        )
        self._mode_hint_lbl.grid(row=1, column=0, columnspan=3, padx=10,
                                 pady=(0, 6), sticky="w")

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

        # ── Active room button (opens popup picker) ───────────────────────────
        self._room_var    = tk.StringVar(value="(not connected)")
        self._rooms_cache = {}   # display_name → room_id

        self.btn_active_room = ctk.CTkButton(
            room_row,
            textvariable=self._room_var,
            font=("Courier New", 9),
            text_color=TXT_TEAL,
            fg_color=BG_INPUT, hover_color="#0d2840",
            border_color=BORDER_TEA, border_width=1,
            height=26, corner_radius=4, anchor="w",
            command=self._open_room_picker,
        )
        self.btn_active_room.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        # Keep a .configure-compatible shim so old code doing
        # self.cmb_room.configure(values=...) / .set(...) still works
        class _RoomBtnShim:
            """Proxy that makes btn_active_room behave like a CTkComboBox for legacy callers."""
            def __init__(self_, btn, var, cache_ref, picker_ref):
                self_._btn      = btn
                self_._var      = var
                self_._cache    = cache_ref
                self_._picker   = picker_ref
                self_._values   = []

            def configure(self_, **kw):
                if "values" in kw:
                    self_._values = list(kw["values"])
                    # Rebuild cache: display name → index (room_id looked up later)
                if "text_color" in kw:
                    try: self_._btn.configure(text_color=kw["text_color"])
                    except Exception: pass

            def set(self_, val: str):
                self_._var.set(val)

            def get(self_) -> str:
                return self_._var.get()

            def cget(self_, key: str):
                if key == "values":
                    return tuple(self_._values)
                return self_._btn.cget(key)

        self.cmb_room = _RoomBtnShim(
            self.btn_active_room, self._room_var, self._rooms_cache, self._open_room_picker
        )

        self.btn_create_room = ctk.CTkButton(
            room_row, text="+ Create",
            font=FONT_SMALL, height=26, width=66, corner_radius=4,
            fg_color="#0d2a3a", hover_color="#0d4050",
            text_color=TXT_TEAL,
            command=self._on_create_room,
        )
        self.btn_create_room.grid(row=0, column=1, padx=(0, 2))

        # "→ Join" button — visible in online mode as a quick-join shortcut
        # (opens the join-address entry in the online panel if needed)
        self.btn_join_room_bar = ctk.CTkButton(
            room_row, text="→ Join",
            font=FONT_SMALL, height=26, width=56, corner_radius=4,
            fg_color="#1a2d60", hover_color="#2545a8",
            text_color=TXT_MAIN,
            command=self._on_room_bar_join,
        )
        self.btn_join_room_bar.grid(row=0, column=2, padx=(0, 0))
        self.btn_join_room_bar.grid_remove()   # shown only in online mode

        # Browse rooms button kept as a narrow icon for the LAN room list
        self.btn_browse_rooms = ctk.CTkButton(
            room_row, text="⊞",
            font=("Segoe UI", 13), height=26, width=30, corner_radius=4,
            fg_color="#0d2040", hover_color="#1a3a60",
            text_color=TXT_TEAL,
            command=self._open_room_browser,
        )
        self.btn_browse_rooms.grid(row=0, column=3, padx=(2, 0))
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

        ctk.CTkButton(
            msg_row, text="📎",
            font=("Segoe UI", 13), height=30, width=34, corner_radius=4,
            fg_color="#0d2a3a", hover_color="#0d4050",
            text_color=TXT_TEAL,
            command=self._open_file_transfer_dialog,
        ).grid(row=0, column=2, padx=(4, 0))

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
    # Online panel  (P2P Tor — no Matrix, no relay, no external servers)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_online_panel(self):
        """P2P Tor identity + room panel.  Replaces the old Matrix login card."""
        self._online_panel = ctk.CTkFrame(
            self._conn_container,
            fg_color=BG_CARD,
            border_color="#1a2d60", border_width=1, corner_radius=6,
        )
        self._online_panel.grid_columnconfigure(1, weight=1)

        # When spoaken_chat_online.py has been deleted, show a single banner
        # instead of wiring up controls that would immediately crash.
        if not _P2P_AVAILABLE:
            ctk.CTkLabel(
                self._online_panel,
                text=(
                    "⚠  P2P / Tor chat is unavailable\n\n"
                    "spoaken_chat_online.py has been deleted.\n"
                    "Restore the file to re-enable this feature."
                ),
                font=FONT_SMALL, text_color="#2a4a60",
                justify="left", anchor="w", wraplength=240,
            ).grid(row=0, column=0, columnspan=3,
                   padx=16, pady=20, sticky="w")

            # Create stubs so any code that references these attributes
            # (e.g. _on_room_bar_join) doesn't raise AttributeError.
            class _Stub:
                def get(self):    return ""
                def delete(self, *a): pass
                def insert(self, *a): pass
                def focus(self):  pass
                def configure(self, **kw): pass

            self._p2p_user_entry  = _Stub()
            self._p2p_join_entry  = _Stub()
            self._p2p_pw_entry    = _Stub()
            self._p2p_did_lbl     = _Stub()
            self._p2p_status_lbl  = _Stub()
            self._btn_copy_onion  = _Stub()

            # Stub buttons that other methods may call .configure() on
            class _BtnStub:
                def configure(self, **kw): pass
                def grid(self, **kw): pass
                def grid_remove(self): pass

            self.btn_p2p_claim = _BtnStub()
            self.btn_p2p_start = _BtnStub()
            self.btn_p2p_stop  = _BtnStub()
            self.btn_p2p_join  = _BtnStub()
            return

        # ── Identity row ──────────────────────────────────────────────────────
        ctk.CTkLabel(
            self._online_panel, text="Name",
            font=FONT_SMALL, text_color=TXT_DIM, width=42, anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=(8, 2), sticky="w")

        name_row = ctk.CTkFrame(self._online_panel, fg_color="transparent")
        name_row.grid(row=0, column=1, columnspan=2, padx=(0, 8), pady=(8, 2), sticky="ew")
        name_row.grid_columnconfigure(0, weight=1)

        self._p2p_user_entry = ctk.CTkEntry(
            name_row,
            placeholder_text="choose a username …",
            fg_color=BG_INPUT, border_color="#1a2d60", border_width=1,
            text_color=TXT_VOSK, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9),
        )
        self._p2p_user_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._p2p_user_entry.bind("<Return>", lambda e: self._on_p2p_claim_identity())

        self.btn_p2p_claim = ctk.CTkButton(
            name_row, text="Claim",
            font=FONT_SMALL, height=26, width=52, corner_radius=4,
            fg_color=BORDER_TEA, hover_color="#145060",
            text_color="#000000",
            command=self._on_p2p_claim_identity,
        )
        self.btn_p2p_claim.grid(row=0, column=1)

        # DID display (read-only, shows after identity is created)
        self._p2p_did_lbl = ctk.CTkLabel(
            self._online_panel,
            text="  DID: (not set)",
            font=("Courier New", 7), text_color=TXT_DIM, anchor="w",
            wraplength=220,
        )
        self._p2p_did_lbl.grid(row=1, column=0, columnspan=3, padx=8,
                                pady=(0, 4), sticky="w")

        # ── Join an existing room ─────────────────────────────────────────────
        ctk.CTkLabel(
            self._online_panel, text="Join",
            font=FONT_SMALL, text_color=TXT_DIM, width=42, anchor="w",
        ).grid(row=2, column=0, padx=(8, 4), pady=(4, 0), sticky="w")

        join_row = ctk.CTkFrame(self._online_panel, fg_color="transparent")
        join_row.grid(row=2, column=1, columnspan=2, padx=(0, 8), pady=(4, 0), sticky="ew")
        join_row.grid_columnconfigure(0, weight=1)

        self._p2p_join_entry = ctk.CTkEntry(
            join_row,
            placeholder_text="abc…xyz.onion/!roomid  (or paste)",
            fg_color=BG_INPUT, border_color="#1a2d60", border_width=1,
            text_color=TXT_TEAL, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9),
        )
        self._p2p_join_entry.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self._p2p_join_entry.bind("<Return>", lambda e: self._on_p2p_join_room())

        # 📋 Paste button
        ctk.CTkButton(
            join_row, text="📋", width=28, height=26, corner_radius=4,
            fg_color="#0d2040", hover_color="#1a3060", text_color=TXT_DIM,
            font=("Segoe UI", 11),
            command=self._p2p_paste_join_address,
        ).grid(row=0, column=1)

        # Hint label: expected address format
        ctk.CTkLabel(
            self._online_panel,
            text="  format:  <host>.onion/<room-id>",
            font=("Segoe UI", 7), text_color=TXT_DIM, anchor="w",
        ).grid(row=3, column=0, columnspan=3, padx=8, pady=(0, 2), sticky="w")

        self._p2p_pw_entry = ctk.CTkEntry(
            self._online_panel,
            placeholder_text="room password (optional)",
            fg_color=BG_INPUT, border_color="#1a2d60", border_width=1,
            text_color=TXT_DIM, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=4, font=("Courier New", 9), show="*",
        )
        self._p2p_pw_entry.grid(row=4, column=0, columnspan=3, padx=8,
                                pady=(0, 4), sticky="ew")
        self._p2p_pw_entry.bind("<Return>", lambda e: self._on_p2p_join_room())

        # ── Buttons ───────────────────────────────────────────────────────────
        p2p_btn_row = ctk.CTkFrame(self._online_panel, fg_color="transparent")
        p2p_btn_row.grid(row=5, column=0, columnspan=3, padx=8, pady=(2, 8), sticky="ew")
        for c in range(3):
            p2p_btn_row.grid_columnconfigure(c, weight=1)

        self.btn_p2p_start = ctk.CTkButton(
            p2p_btn_row, text="⬡  Start",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#0d3a20", hover_color="#145030",
            text_color=TXT_VOSK,
            command=self._on_p2p_start,
        )
        self.btn_p2p_start.grid(row=0, column=0, padx=(0, 2), sticky="ew")

        self.btn_p2p_join = ctk.CTkButton(
            p2p_btn_row, text="→  Join",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#1a2d60", hover_color="#2545a8",
            text_color=TXT_MAIN,
            command=self._on_p2p_join_room,
        )
        self.btn_p2p_join.grid(row=0, column=1, padx=2, sticky="ew")

        self.btn_p2p_stop = ctk.CTkButton(
            p2p_btn_row, text="■  Stop",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#3a1010", hover_color="#5a2020",
            text_color="#e07070",
            command=self._on_p2p_stop, state="disabled",
        )
        self.btn_p2p_stop.grid(row=0, column=2, padx=(2, 0), sticky="ew")

        # Status / onion address (with copy button)
        status_row = ctk.CTkFrame(self._online_panel, fg_color="transparent")
        status_row.grid(row=6, column=0, columnspan=3, padx=8, pady=(0, 6), sticky="ew")
        status_row.grid_columnconfigure(0, weight=1)

        self._p2p_status_lbl = ctk.CTkLabel(
            status_row,
            text="  Stopped — click Start to launch Tor node",
            font=("Segoe UI", 7), text_color=TXT_DIM, anchor="w",
            wraplength=200,
        )
        self._p2p_status_lbl.grid(row=0, column=0, sticky="w")

        self._btn_copy_onion = ctk.CTkButton(
            status_row, text="⧉", width=24, height=20, corner_radius=3,
            fg_color="transparent", hover_color="#1a2d60", text_color=TXT_DIM,
            font=("Segoe UI", 10),
            command=self._p2p_copy_onion,
        )
        self._btn_copy_onion.grid(row=0, column=1, padx=(2, 0))
        self._btn_copy_onion.grid_remove()   # shown once node is running

        # Load any saved identity
        self._p2p_load_saved_identity()

    # ─────────────────────────────────────────────────────────────────────────
    # Mode toggle — Local ⟷ P2P Online
    # ─────────────────────────────────────────────────────────────────────────

    def _switch_to_local(self):
        if not self._p2p_mode:
            return
        self._p2p_mode = False
        self._btn_local.configure(fg_color=BORDER_TEA, text_color=TXT_VOSK)
        self._btn_online.configure(fg_color="#12182e", text_color=TXT_DIM)
        self._online_panel.grid_remove()
        self._local_panel.grid(row=0, column=0, sticky="ew")
        self.btn_port.grid()
        self.btn_browse_rooms.grid_remove()
        self.btn_join_room_bar.grid_remove()
        self.btn_create_room.grid()
        try:
            self._mode_hint_lbl.configure(
                text="  Connect to a Spoaken server on your local network"
            )
        except Exception:
            pass
        self.chat_receive("[ 🖧  Switched to LAN mode ]")

    def _switch_to_online(self):
        if not _P2P_AVAILABLE:
            self.chat_receive(
                "[ ✗  P2P unavailable — spoaken_chat_online.py has been deleted ]"
            )
            self.update_console(
                f"[Console]: {_P2P_REASON}"
            )
            return
        if self._p2p_mode:
            return
        self._p2p_mode = True
        self._btn_online.configure(fg_color=BORDER_TEA, text_color=TXT_VOSK)
        self._btn_local.configure(fg_color="#12182e", text_color=TXT_DIM)
        self._local_panel.grid_remove()
        self._online_panel.grid(row=0, column=0, sticky="ew")
        self.btn_port.grid_remove()
        self.btn_browse_rooms.grid()
        self.btn_join_room_bar.grid()
        try:
            self._mode_hint_lbl.configure(
                text="  Encrypted P2P chat via Tor — no central server needed"
            )
        except Exception:
            pass
        self.chat_receive("[ 🌐  Switched to P2P Online mode ]")
        self.chat_receive("")
        self.chat_receive("  How to get started:")
        self.chat_receive("  1.  Enter a username and click Claim")
        self.chat_receive("  2.  Click ⬡ Start to launch your Tor node")
        self.chat_receive("  3.  Click + Create to make a room, or paste a")
        self.chat_receive("       .onion/room-id address and click → Join")
        self.chat_receive("  4.  Share your .onion address with peers")
        self.chat_receive("")

    # ─────────────────────────────────────────────────────────────────────────
    # P2P Identity helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _p2p_load_saved_identity(self):
        """Populate identity fields from stored config on startup."""
        try:
            from spoaken_chat_online import load_identity
            from paths import ROOT_DIR
            cfg_path = str(ROOT_DIR / "spoaken_config.json")
            ident    = load_identity(cfg_path)
            if ident.get("username"):
                self._p2p_user_entry.delete(0, "end")
                self._p2p_user_entry.insert(0, ident["username"])
            if ident.get("did"):
                self._p2p_did_lbl.configure(
                    text=f"  DID: {ident['did']}", text_color=TXT_DIM)
                # Username is already claimed — grey out the Claim button
                self.btn_p2p_claim.configure(
                    text="✔ Claimed", state="normal",
                    fg_color="#0d3a20", text_color=TXT_VOSK)
        except Exception:
            pass

    def _on_p2p_claim_identity(self):
        """
        Save / create the local identity with the chosen username.

        This does NOT start the Tor node — it simply persists the username
        and generates a DID keypair if one doesn't already exist.
        Feedback is shown inline in the DID label and in the chat log.
        """
        uname = self._p2p_user_entry.get().strip()
        if not uname:
            self.chat_receive("[P2P]: Enter a username to claim.")
            return
        try:
            from spoaken_chat_online import create_identity
            from paths import ROOT_DIR
            cfg_path = str(ROOT_DIR / "spoaken_config.json")
            ident = create_identity(cfg_path, uname)
            did   = ident.get("did", "")
            self._p2p_did_lbl.configure(
                text=f"  DID: {did}", text_color=TXT_DIM)
            self.btn_p2p_claim.configure(
                text="✔ Claim", fg_color="#0d3a20", text_color=TXT_VOSK)
            self.chat_receive(f"[P2P]: Identity claimed as '{uname}'")
            self.chat_receive(f"[P2P]: DID: {did}")
            self.chat_receive("[P2P]: Click ⬡ Start to launch your Tor node.")
        except Exception as exc:
            self.chat_receive(f"[P2P Claim Error]: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # P2P node controls
    # ─────────────────────────────────────────────────────────────────────────

    def _on_p2p_start(self):
        """Create or re-use the local P2P node and start the Tor hidden service."""
        uname = self._p2p_user_entry.get().strip()
        if not uname:
            self.chat_receive("[P2P]: Enter a username before starting.")
            return

        # Auto-claim identity silently if not already done (avoids re-printing
        # "Identity claimed" every time Start is pressed after the first claim).
        try:
            from spoaken_chat_online import create_identity, load_identity
            from paths import ROOT_DIR
            cfg_path_early = str(ROOT_DIR / "spoaken_config.json")
            existing = load_identity(cfg_path_early)
            if not existing.get("has_key"):
                self._on_p2p_claim_identity()
        except Exception:
            pass

        self.btn_p2p_start.configure(state="disabled")
        self._p2p_status_lbl.configure(text="  Starting Tor node …", text_color=TXT_DIM)

        def _do():
            try:
                from spoaken_chat_online import SpoakenOnlineClient, create_identity
                from paths import ROOT_DIR
                cfg_path = str(ROOT_DIR / "spoaken_config.json")

                # Ensure identity exists / update username
                ident = create_identity(cfg_path, uname)

                if self._p2p_node is None:
                    node = SpoakenOnlineClient(
                        on_event=self._on_p2p_event,
                        log_cb=lambda m: self.after(0, lambda msg=m: self.chat_receive(msg)),
                        cfg_path=cfg_path,
                    )
                    node.username = uname
                else:
                    node = self._p2p_node
                    node.username = uname

                ok = node.start()
                self._p2p_node = node

                def _update():
                    if ok:
                        self.btn_p2p_start.configure(state="disabled")
                        self.btn_p2p_stop.configure(state="normal")
                        self._p2p_status_lbl.configure(
                            text=f"  ✔ {node.onion_address}",
                            text_color=TXT_VOSK,
                        )
                        self._p2p_did_lbl.configure(
                            text=f"  DID: {node.did}", text_color=TXT_DIM)
                        try:
                            self._btn_copy_onion.grid()
                        except Exception:
                            pass
                        try:
                            self._conn_status_lbl.configure(
                                text="  ● node running", text_color=TXT_VOSK)
                        except Exception:
                            pass
                        self.chat_receive(f"✔  Tor node started — {node.onion_address}")
                        self.chat_receive(f"    Your DID: {node.did}")
                        self.chat_receive(f"    Share your .onion address so peers can join.")
                    else:
                        self.btn_p2p_start.configure(state="normal")
                        self._p2p_status_lbl.configure(
                            text="  ✗ Failed — is Tor running? (or: pip install torpy)",
                            text_color="#e03535")
                        self.chat_receive("✗  Failed to start Tor node.")
                        self.chat_receive("   Option A:  sudo apt install tor && systemctl start tor")
                        self.chat_receive("   Option B:  pip install torpy  (no daemon needed)")
                self.after(0, _update)
            except Exception as exc:
                self.after(0, lambda e=exc: (
                    self.chat_receive(f"[P2P Error]: {e}"),
                    self.btn_p2p_start.configure(state="normal"),
                ))

        threading.Thread(target=_do, daemon=True).start()

    def _on_p2p_stop(self):
        if self._p2p_node:
            self._p2p_node.stop()
            self._p2p_node = None
        self.btn_p2p_start.configure(state="normal")
        self.btn_p2p_stop.configure(state="disabled")
        self._p2p_status_lbl.configure(
            text="  Stopped", text_color=TXT_DIM)
        try:
            self._btn_copy_onion.grid_remove()
        except Exception:
            pass
        self.chat_receive("[P2P]: Node stopped.")

    def _on_room_bar_join(self):
        """
        Quick-join shortcut from the room bar → Join button (online mode only).
        If the join entry in the online panel already has an address, joins it
        directly.  Otherwise, opens the sidebar/online panel and focuses the join entry.
        """
        if not self._p2p_mode:
            return
        addr = self._p2p_join_entry.get().strip()
        if addr:
            self._on_p2p_join_room()
        else:
            # Make the sidebar visible, ensure online panel is shown, focus the entry
            if not self._sidebar_open:
                self._toggle_sidebar()
            self._switch_to_online()
            try:
                self._p2p_join_entry.focus()
            except Exception:
                pass
            self.chat_receive("[P2P]: Paste or type the .onion/room-id address, then press Enter.")

    def _p2p_paste_join_address(self):
        """Paste clipboard content into the join address field."""
        try:
            text = self.clipboard_get().strip()
            if text:
                self._p2p_join_entry.delete(0, "end")
                self._p2p_join_entry.insert(0, text)
        except Exception:
            pass

    def _p2p_copy_onion(self):
        """Copy the node's .onion address to clipboard."""
        if self._p2p_node:
            addr = self._p2p_node.onion_address
            if addr and addr != "(not started)":
                self.clipboard_clear()
                self.clipboard_append(addr)
                self.chat_receive(f"[P2P]: Copied to clipboard: {addr}")

    def _on_p2p_join_room(self):
        """
        Join a P2P room.

        Accepts these address formats:
          • <host>.onion/<room_id>          (canonical)
          • <host>.onion <room_id>          (space-separated)
          • <host>.onion  (host only — use room_id = host for single-room nodes)
        """
        if not self._p2p_node or not self._p2p_node.is_started():
            self.chat_receive("[P2P]: Start the node first.")
            return

        raw = self._p2p_join_entry.get().strip()
        pw  = self._p2p_pw_entry.get().strip()

        if not raw:
            self.chat_receive("[P2P]: Enter a room address to join.")
            return

        # Normalise — accept slash or space as separator
        raw_norm = raw.replace(" ", "/", 1)

        if "/" in raw_norm and ".onion" in raw_norm:
            onion, _, room_id = raw_norm.partition("/")
        elif ".onion" in raw_norm:
            # Host-only — derive a room_id from the onion address
            onion   = raw_norm
            room_id = raw_norm
            self.chat_receive(f"[P2P]: No room ID found — attempting to connect to {onion} …")
        else:
            self.chat_receive("[P2P]: Invalid address — expected: <host>.onion/<room-id>")
            return

        self.chat_receive(f"[P2P]: Joining {onion}/{room_id} …")
        self._p2p_join_entry.delete(0, "end")

        def _do():
            try:
                ok = self._p2p_node.join_room(onion, room_id, password=pw)
                self.after(0, lambda: self.chat_receive(
                    f"[P2P]: ✔ Joined {onion}/{room_id}" if ok
                    else f"[P2P]: ✗ Could not join — check address and password"
                ))
            except Exception as exc:
                self.after(0, lambda e=exc: self.chat_receive(f"[P2P Join Error]: {e}"))

        threading.Thread(target=_do, daemon=True).start()

    def _on_p2p_event(self, ev: dict):
        """Handle events fired by the P2P node."""
        t = ev.get("type", "")
        c = ev.get("content", {})

        if t == "m.room.message":
            sender   = c.get("sender", "?")
            body     = c.get("body", "")
            room_id  = ev.get("room_id", "")
            room_name = self._p2p_rooms_cache.get(room_id, room_id[:16])
            if self._p2p_current_room is None or room_id == self._p2p_current_room:
                prefix = f"[{room_name}] " if self._p2p_current_room is None else ""
                self.after(0, lambda s=sender, b=body, p=prefix:
                           self.chat_receive(f"{p}[{s}]: {b}"))

        elif t == "m.member.join":
            uname = c.get("username", "?")
            did   = c.get("did", "")
            self.after(0, lambda u=uname, d=did:
                       self.chat_receive(f"  ── {u} joined ({d[:24]}…) ──"))

        elif t == "m.member.leave":
            uname = c.get("username", "?")
            self.after(0, lambda u=uname:
                       self.chat_receive(f"  ── {u} left ──"))

        elif t == "m.room.created":
            room_id   = ev.get("room_id", "")
            room_name = c.get("name", room_id)
            onion     = c.get("host_onion", "")
            self._p2p_rooms_cache[room_id] = room_name
            self._p2p_current_room         = room_id
            self.after(0, lambda rid=room_id, rn=room_name, o=onion: (
                self.chat_receive(f"[P2P]: ✔ Created room '{rn}'"),
                self.chat_receive(f"[P2P]: Share address:  {o}/{rid}"),
                self.cmb_room.configure(values=list(self._p2p_rooms_cache.values()) or ["(no rooms)"]),
                self.cmb_room.set(rn),
            ))

        elif t == "m.auth.ok":
            room_id   = ev.get("room_id", "")
            room_name = self._p2p_rooms_cache.get(room_id, room_id[:16])
            self._p2p_current_room = room_id
            rooms_display = list(self._p2p_rooms_cache.values()) or ["(no rooms)"]
            self.after(0, lambda rn=room_name, d=rooms_display: (
                self.cmb_room.configure(values=d),
                self.cmb_room.set(rn),
            ))

        elif t == "m.room.list":
            rooms = c.get("rooms", [])
            self._p2p_rooms_cache = {r["room_id"]: r["name"] for r in rooms}
            display = list(self._p2p_rooms_cache.values()) or ["(no rooms)"]
            self.after(0, lambda d=display: self.cmb_room.configure(values=d))

        elif t == "m.file.received":
            fname  = c.get("filename", "file")
            size   = c.get("size", 0)
            path   = c.get("_saved_path", "")
            kb     = size // 1024 if size else 0
            msg    = f"  📥  Received '{fname}'  ({kb} KB)" + (f"\n       → {path}" if path else "")
            self.after(0, lambda m=msg: self.chat_receive(m))

    # ─────────────────────────────────────────────────────────────────────────
    # File Transfer — send / browse files in the current room
    # ─────────────────────────────────────────────────────────────────────────

    def _open_file_transfer_dialog(self):
        """
        File Transfer panel — opened by the 📎 button in the message row.

        LAN mode
        ────────
          • Send File  — file-picker → SpoakenLANClient.send_file(room_id, path)
          • Room Files — list_files(room_id) → m.file.list → _on_file_list_received()

        P2P / Tor mode
        ──────────────
          • Send File  — file-picker → SpoakenP2PNode.send_file(room_id, path)
          • Room Files — not server-stored in P2P; dialog explains this clearly.

        Non-modal (no grab_set) so the chat window remains usable.
        """
        import pathlib as _pl
        from tkinter import filedialog as _fd

        dlg = ctk.CTkToplevel(self)
        dlg.title("File Transfer")
        dlg.geometry("430x400")
        dlg.configure(fg_color="#060c1a")
        dlg.resizable(True, True)
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(2, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(dlg, fg_color="#0a1128", corner_radius=0)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr, text="📎  File Transfer",
            font=("Segoe UI Semibold", 13), text_color=TXT_TEAL, anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(10, 2), sticky="w")

        room_name = self._room_var.get() if hasattr(self, "_room_var") else "(no room)"
        ctk.CTkLabel(
            hdr, text=f"Room: {room_name}",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=1, column=0, padx=14, pady=(0, 8), sticky="w")
        ctk.CTkFrame(hdr, height=1, fg_color=BORDER_TEA, corner_radius=0,
                     ).grid(row=2, column=0, sticky="ew")

        # ── Send section ──────────────────────────────────────────────────────
        send_frm = ctk.CTkFrame(dlg, fg_color="#0d1735",
                                border_color=BORDER_TEA, border_width=1, corner_radius=8)
        send_frm.grid(row=1, column=0, padx=10, pady=(10, 4), sticky="ew")
        send_frm.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            send_frm, text="Send a file to this room",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(8, 4), sticky="w")

        path_var  = tk.StringVar()
        path_row  = ctk.CTkFrame(send_frm, fg_color="transparent")
        path_row.grid(row=1, column=0, padx=10, pady=(0, 4), sticky="ew")
        path_row.grid_columnconfigure(0, weight=1)

        path_entry = ctk.CTkEntry(
            path_row, textvariable=path_var,
            placeholder_text="Select or paste a file path …",
            fg_color="#0c1636", border_color=BORDER_TEA, border_width=1,
            text_color=TXT_TEAL, placeholder_text_color=TXT_DIM,
            height=30, corner_radius=4, font=("Courier New", 9),
        )
        path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        def _pick():
            p = _fd.askopenfilename(
                parent=dlg, title="Choose file to send",
                filetypes=[("All files", "*.*"),
                           ("Text / Transcript", "*.txt *.md *.log"),
                           ("Audio", "*.wav"), ("JSON", "*.json")],
            )
            if p:
                path_var.set(p)

        ctk.CTkButton(
            path_row, text="Browse …",
            font=FONT_SMALL, height=30, width=76, corner_radius=4,
            fg_color="#0d2a3a", hover_color="#0d4050", text_color=TXT_TEAL,
            command=_pick,
        ).grid(row=0, column=1)

        send_status = ctk.CTkLabel(
            send_frm, text="",
            font=("Courier New", 9), text_color=TXT_DIM, anchor="w",
        )
        send_status.grid(row=2, column=0, padx=12, pady=(0, 2), sticky="w")

        def _do_send():
            fp = path_var.get().strip()
            if not fp:
                send_status.configure(text="⚠  No file selected.", text_color="#f0c040")
                return
            p = _pl.Path(fp)
            if not p.exists():
                send_status.configure(text=f"⚠  Not found: {p.name}", text_color="#ff6060")
                return
            if p.stat().st_size > 50 * 1024 * 1024:
                send_status.configure(text="⚠  Too large (max 50 MB).", text_color="#ff6060")
                return
            kb = p.stat().st_size // 1024
            send_status.configure(text=f"  Sending '{p.name}' ({kb} KB) …", text_color=TXT_DIM)
            btn_send.configure(state="disabled")

            def _bg():
                try:
                    if self._p2p_mode:
                        if self._p2p_node and self._p2p_current_room:
                            self._p2p_node.send_file(self._p2p_current_room, fp)
                        else:
                            self.after(0, lambda: send_status.configure(
                                text="⚠  Start node and join a room first.", text_color="#ff6060"))
                            self.after(0, lambda: btn_send.configure(state="normal"))
                            return
                    else:
                        if self._lan_client and self._lan_client.is_connected() \
                                and self._lan_current_room:
                            self._lan_client.send_file(self._lan_current_room, fp)
                        else:
                            self.after(0, lambda: send_status.configure(
                                text="⚠  Connect to a room first.", text_color="#ff6060"))
                            self.after(0, lambda: btn_send.configure(state="normal"))
                            return
                    self.after(0, lambda n=p.name, k=kb: (
                        send_status.configure(
                            text=f"  ✔  '{n}' queued.", text_color="#00e5a0"),
                        btn_send.configure(state="normal"),
                        self.chat_receive(f"  📤  Sending '{n}'  ({k} KB) …"),
                    ))
                except Exception as exc:
                    self.after(0, lambda e=exc: (
                        send_status.configure(text=f"✗  {e}", text_color="#ff6060"),
                        btn_send.configure(state="normal"),
                    ))

            threading.Thread(target=_bg, daemon=True).start()

        btn_send = ctk.CTkButton(
            send_frm, text="📤  Send File",
            height=30, corner_radius=5,
            fg_color=BORDER_TEA, hover_color="#145060", text_color="#000000",
            font=("Segoe UI Semibold", 10),
            command=_do_send,
        )
        btn_send.grid(row=3, column=0, padx=10, pady=(0, 10), sticky="ew")

        # ── Room files section ─────────────────────────────────────────────────
        files_hdr = ctk.CTkFrame(dlg, fg_color="transparent")
        files_hdr.grid(row=2, column=0, padx=10, pady=(0, 0), sticky="nsew")
        files_hdr.grid_columnconfigure(0, weight=1)
        files_hdr.grid_rowconfigure(1, weight=1)

        lbl_row = ctk.CTkFrame(files_hdr, fg_color="transparent")
        lbl_row.grid(row=0, column=0, sticky="ew")
        lbl_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            lbl_row, text="Files in this room",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        def _refresh_files():
            if self._p2p_mode:
                self.chat_receive(
                    "  [Files]: P2P files stream inline — no server-side file list.")
                return
            if not self._lan_client or not self._lan_client.is_connected():
                self.chat_receive("  [Files]: Connect to a server first.")
                return
            if not self._lan_current_room:
                self.chat_receive("  [Files]: Join a room first.")
                return
            self._lan_client.list_files(self._lan_current_room)
            self.chat_receive("  [Files]: Fetching room file list …")

        ctk.CTkButton(
            lbl_row, text="↺  Refresh",
            font=FONT_SMALL, height=24, width=76, corner_radius=4,
            fg_color="#0d2040", hover_color="#1a3a60", text_color=TXT_DIM,
            command=_refresh_files,
        ).grid(row=0, column=1)

        self._file_list_frame = ctk.CTkScrollableFrame(
            files_hdr, fg_color="#0d1735",
            border_color=BORDER_TEA, border_width=1, corner_radius=6,
            scrollbar_button_color=BORDER_ACT,
        )
        self._file_list_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self._file_list_frame.grid_columnconfigure(0, weight=1)
        self._file_list_dlg = dlg

        ctk.CTkLabel(
            self._file_list_frame,
            text="  Click ↺ Refresh to load room files",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=0, column=0, padx=8, pady=12, sticky="w")

        ctk.CTkButton(
            dlg, text="✕  Close",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#2a1010", hover_color="#4a2020", text_color="#e07070",
            command=dlg.destroy,
        ).grid(row=3, column=0, padx=10, pady=(4, 8), sticky="e")

        # Auto-refresh if already in a LAN room
        if not self._p2p_mode and self._lan_client and \
                self._lan_client.is_connected() and self._lan_current_room:
            dlg.after(200, _refresh_files)

    def _on_file_list_received(self, files: list, mode: str = "lan"):
        """
        Populate the file-list frame inside the open File Transfer dialog.
        Called on the main thread via after() from the m.file.list event handler.
        """
        if not (hasattr(self, "_file_list_dlg") and
                self._file_list_dlg is not None and
                self._file_list_dlg.winfo_exists()):
            return
        if not hasattr(self, "_file_list_frame"):
            return

        sf = self._file_list_frame
        for w in sf.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass

        if not files:
            ctk.CTkLabel(
                sf, text="  No files in this room yet.",
                font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
            ).grid(row=0, column=0, padx=8, pady=12, sticky="w")
            return

        from tkinter import filedialog as _fd

        for i, f in enumerate(files):
            fname   = f.get("filename", "file")
            size    = f.get("size", 0)
            sender  = f.get("sender", "?")
            file_id = f.get("file_id", "")
            kb      = size // 1024 if size else 0
            alt_bg  = "#080f20" if i % 2 else "#0d1735"

            row_f = ctk.CTkFrame(sf, fg_color=alt_bg, corner_radius=4)
            row_f.grid(row=i, column=0, padx=4, pady=2, sticky="ew")
            row_f.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                row_f,
                text=f"  📄  {fname}",
                font=("Segoe UI Semibold", 10),
                text_color=TXT_TEAL, anchor="w",
            ).grid(row=0, column=0, padx=4, pady=(5, 0), sticky="w")

            ctk.CTkLabel(
                row_f,
                text=f"   {kb} KB  ·  from {sender}  ·  id:{file_id[:12]}",
                font=("Courier New", 7), text_color=TXT_DIM, anchor="w",
            ).grid(row=1, column=0, padx=4, pady=(0, 4), sticky="w")

            def _dl(fid=file_id, fn=fname):
                parent = (self._file_list_dlg
                          if hasattr(self, "_file_list_dlg") and
                          self._file_list_dlg.winfo_exists() else self)
                dest = _fd.asksaveasfilename(parent=parent, initialfile=fn,
                                             title="Save file as …")
                if dest and self._lan_client and self._lan_current_room:
                    self._lan_client.download_file(
                        self._lan_current_room, fid, dest_path=dest)
                    self.chat_receive(f"  📥  Downloading '{fn}' → {dest} …")

            ctk.CTkButton(
                row_f, text="⬇ Save",
                font=FONT_SMALL, height=26, width=64, corner_radius=4,
                fg_color="#0d3a40", hover_color="#145060",
                state="normal" if mode == "lan" else "disabled",
                command=_dl,
            ).grid(row=0, column=1, rowspan=2, padx=6, pady=4)

    # ─────────────────────────────────────────────────────────────────────────
    # P2P room browser  (shows locally created + joined rooms)
    # ─────────────────────────────────────────────────────────────────────────

    def _open_room_browser(self):
        if not self._p2p_node or not self._p2p_node.is_started():
            self.chat_receive("[P2P]: Start the node first.")
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title("P2P Rooms")
        dlg.geometry("420x340")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(True, True)

        top = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0)
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="Your P2P rooms (hosted + joined)",
                     font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
                     ).grid(row=0, column=0, padx=10, pady=8, sticky="w")
        ctk.CTkButton(
            top, text="↺ Refresh", height=28, width=70, corner_radius=5,
            fg_color=BG_CARD, hover_color=BORDER_ACT, text_color=TXT_DIM,
            font=FONT_SMALL, command=lambda: _refresh(),
        ).grid(row=0, column=1, padx=8, pady=8)

        sf = ctk.CTkScrollableFrame(dlg, fg_color=BG_CARD, corner_radius=0)
        sf.pack(fill="both", expand=True)
        sf.grid_columnconfigure(0, weight=1)

        _widgets: list = []

        def _refresh():
            for w in _widgets:
                try:
                    w.destroy()
                except Exception:
                    pass
            _widgets.clear()
            rooms = self._p2p_node.list_rooms(notify=False) if self._p2p_node else []
            if not rooms:
                lbl = ctk.CTkLabel(sf, text="  No rooms yet. Create one with + Room.",
                                   font=FONT_SMALL, text_color=TXT_DIM)
                lbl.grid(row=0, column=0, padx=10, pady=20)
                _widgets.append(lbl)
                return
            for i, r in enumerate(rooms):
                rf = ctk.CTkFrame(sf, fg_color=BG_SIDEBAR if i % 2 else BG_CARD,
                                  corner_radius=4)
                rf.grid(row=i, column=0, padx=4, pady=2, sticky="ew")
                rf.grid_columnconfigure(0, weight=1)
                _widgets.append(rf)
                role_col = TXT_VOSK if r.get("role") == "host" else TXT_MAIN
                ctk.CTkLabel(rf, text=f"  {r.get('name', r['room_id'])}",
                             font=("Segoe UI Semibold", 10),
                             text_color=role_col, anchor="w",
                             ).grid(row=0, column=0, padx=4, pady=(4, 0), sticky="w")
                ctk.CTkLabel(rf, text=f"  {r['room_id']}   [{r.get('role','?')}]",
                             font=("Courier New", 7), text_color=TXT_DIM, anchor="w",
                             ).grid(row=1, column=0, padx=4, pady=(0, 4), sticky="w")
                ctk.CTkButton(
                    rf, text="Select", font=FONT_SMALL, height=24, width=54,
                    fg_color="#0d3a40", hover_color="#145060",
                    command=lambda rid=r["room_id"], rn=r.get("name", ""):
                        self._on_room_select_by_id(rid, rn) or dlg.destroy(),
                ).grid(row=0, column=1, rowspan=2, padx=6, pady=4)

        _refresh()

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
        self._cmd_hist_idx : int = 0   # points past-the-end so first Up shows last entry
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
        """Show help text in the chat log (sidebar context)."""
        try:
            if self.controller._cmd_parser is not None:
                help_text = self.controller._cmd_parser.help_text()
            else:
                help_text = "Commands not available — controller not ready."
            for line in help_text.splitlines():
                self.chat_receive(line)
        except Exception as exc:
            self.chat_receive(f"[Help Error]: {exc}")

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
        _chat_width = 290
        if self._sidebar_open:
            self.grid_columnconfigure(1, weight=0, minsize=_chat_width)
            self._sidebar_frame.grid(
                row=0, column=1, padx=(0, 8), pady=8, sticky="nsew"
            )
            self.btn_chat.configure(text="Chat ◀")
            new_w = self._base_width + _chat_width + 14
            self.geometry(f"{new_w}x{self.winfo_height()}")
            # Show welcome hint the very first time the panel opens
            if not getattr(self, "_sidebar_welcomed", False):
                self._sidebar_welcomed = True
                self.chat_receive("Welcome to Spoaken Chat!")
                self.chat_receive("")
                self.chat_receive("  🖧  LAN  — talk to people on your Wi-Fi / network.")
                self.chat_receive("       Click 'Host Server' to share your transcript,")
                self.chat_receive("       or connect to a friend's IP address.")
                self.chat_receive("")
                self.chat_receive("  🌐  P2P  — encrypted Tor chat, works over the internet.")
                self.chat_receive("       No account needed. Choose a username, start,")
                self.chat_receive("       create a room, and share your .onion address.")
                self.chat_receive("")
        else:
            self._sidebar_frame.grid_remove()
            self.grid_columnconfigure(1, weight=0, minsize=0)
            self.btn_chat.configure(text="Chat ▶")
            self.geometry(f"{self._base_width}x{self.winfo_height()}")

    def chat_receive(self, message: str):
        """Insert a colour-coded line into the chat log."""
        cl = self._chat_log._textbox
        cl.configure(state="normal")

        m = message.strip()

        if m == "":
            cl.insert("end", "\n", "chat_system")

        # My own outgoing messages
        elif m.startswith("[Me]:") or m.startswith("⌘"):
            cl.insert("end", f"{message}\n", "chat_me")

        # Error / failure lines
        elif any(x in m for x in ("✗", "error", "Error", "failed", "Failed")):
            cl.insert("end", f"{message}\n", "chat_error")

        # Status / system lines (indented, icons, separators)
        elif (
            m.startswith("  ") or m.startswith("✔") or m.startswith("⬡")
            or m.startswith("─") or m.startswith("[") or m.startswith("  ──")
            or m.startswith("Option") or m.startswith("  How")
        ):
            cl.insert("end", f"{message}\n", "chat_system")

        # Section headers / welcome text
        elif "Welcome" in m or m.startswith("🌐") or m.startswith("🖧"):
            cl.insert("end", f"{message}\n", "chat_header")

        # Peer message — "Username: some text" pattern (most important, stands out)
        elif ":" in m and not m.startswith(" "):
            # Bold white for peer messages so they jump out
            cl.insert("end", f"{message}\n", "chat_peer")

        else:
            cl.insert("end", f"{message}\n", "chat_system")

        cl.see("end")
        cl.configure(state="disabled")

    def _on_chat_send(self, event=None):
        msg = self._chat_entry.get().strip()
        if not msg:
            return
        self._chat_entry.delete(0, "end")
        self.chat_receive(f"[Me]: {msg}")
        if self._p2p_mode:
            if self._p2p_node and self._p2p_current_room:
                self._p2p_node.send_message(self._p2p_current_room, msg)
            else:
                self.chat_receive("[P2P]: Start the node and join a room first.")
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

        threading.Thread(target=_connect_bg, daemon=True).start()

    def _on_lan_connected(self):
        self.btn_lan_connect.configure(state="disabled")
        self.btn_lan_disconnect.configure(state="normal")
        try:
            self._conn_status_lbl.configure(text="  ● connected", text_color=TXT_VOSK)
        except Exception:
            pass
        self.chat_receive("✔  Connected to LAN server")

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
        try:
            self._conn_status_lbl.configure(text="  ● offline", text_color=STA_IDLE)
        except Exception:
            pass
        self.chat_receive("⬡  Disconnected from LAN server")

    def _on_lan_scan(self):
        self.chat_receive("🔍  Scanning your network for Spoaken servers …")
        def _scan():
            try:
                from spoaken_chat import discover_servers
                servers = discover_servers(wait=2.0)
                if servers:
                    self.after(0, lambda: self.chat_receive(
                        f"  Found {len(servers)} server(s):"
                    ))
                    for s in servers:
                        self.after(0, lambda s=s: self.chat_receive(
                            f"  ✔  {s.name}  ·  {s.ip}:{s.port}"
                            f"  ({s.room_count} room{'s' if s.room_count != 1 else ''})"
                        ))
                        # Auto-fill host/port with the first result
                        self.after(0, lambda s=s: (
                            self._lan_host_entry.delete(0, "end"),
                            self._lan_host_entry.insert(0, s.ip),
                            self._lan_port_entry.delete(0, "end"),
                            self._lan_port_entry.insert(0, str(s.port)),
                        ))
                    self.after(0, lambda: self.chat_receive(
                        "  Fields filled in — click Connect to join."
                    ))
                else:
                    self.after(0, lambda: self.chat_receive(
                        "  No servers found.  Ask a friend to click 'Host Server',"
                        " then scan again."
                    ))
            except Exception as e:
                self.after(0, lambda: self.chat_receive(f"  Scan error: {e}"))
        threading.Thread(target=_scan, daemon=True).start()

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
            count = len(rooms)
            self.after(0, lambda: self.chat_receive(
                f"  {count} room{'s' if count != 1 else ''} available"
                + ("  — click the room picker to join one." if count else
                   "  — create one with + Create.")
            ))

        elif t == "m.room.joined":
            room_name = c.get("name", "?")
            self.after(0, lambda: self.chat_receive(
                f"  ✔  Joined room '{room_name}'"
            ))
            history = c.get("history", [])
            if history:
                self.after(0, lambda: self.chat_receive(
                    f"  ── last {min(len(history), 20)} message(s) ──"
                ))
            for ev in history[-20:]:
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
                self.chat_receive(f"{s}: {b}"))

        elif t == "m.room.created":
            room_id = c.get("room_id", "")
            name    = c.get("name", "room")
            self.after(0, lambda: self.chat_receive(
                f"  ✔  Room '{name}' created — joining …"
            ))
            if self._lan_client and room_id:
                self._lan_current_room = room_id
                self._lan_client.list_rooms()

        elif t == "m.room.member":
            membership = c.get("membership", "")
            uname      = c.get("username", "?")
            symbol     = "→" if membership == "join" else "←"
            self.after(0, lambda: self.chat_receive(
                f"  {symbol}  {uname} {membership}ed"
            ))

        elif t == "m.client.disconnected":
            self.after(0, self._on_lan_disconnect)

        elif t == "m.error":
            err = c.get("error", "unknown error")
            self.after(0, lambda: self.chat_receive(f"  ✗  {err}"))

        elif t in ("m.auth.ok",):
            pass

        elif t == "m.server.shutdown":
            self.after(0, lambda: self.chat_receive("  Server is shutting down …"))
            self.after(0, self._on_lan_disconnect)

        elif t == "m.file.list":
            files = c.get("files", [])
            self.after(0, lambda fl=files: self._on_file_list_received(fl, mode="lan"))

        elif t in ("m.file.received", "m.file.sent"):
            fname  = c.get("filename", "file")
            size   = c.get("size", 0)
            path   = c.get("_saved_path", "")
            kb     = size // 1024 if size else 0
            if t == "m.file.received":
                msg = f"  📥  Received '{fname}'  ({kb} KB)" + (f"\n       → {path}" if path else "")
                self.after(0, lambda m=msg: self.chat_receive(m))
            else:
                self.after(0, lambda f=fname, k=kb: self.chat_receive(
                    f"  📤  Sent '{f}'  ({k} KB)"))

    # ─────────────────────────────────────────────────────────────────────────
    # Room picker popup
    # ─────────────────────────────────────────────────────────────────────────

    def _open_room_picker(self):
        """
        Popup dialog that shows available rooms with search/filter.
        Works for both LAN and P2P Online modes.
        Single-click selects and closes the dialog.
        """
        # Gather room list
        rooms: list[dict] = []   # each: {id, name, topic, members, source}

        if self._p2p_mode:
            if self._p2p_node:
                for r in self._p2p_node.list_rooms(notify=False):
                    rooms.append({
                        "id"     : r.get("room_id", ""),
                        "name"   : r.get("name", r.get("room_id", "?")),
                        "topic"  : f"[{r.get('role','?')}]  host: {r.get('onion','') or r.get('host','')}",
                        "members": len(r.get("members", [])),
                        "source" : "p2p",
                    })
        else:
            for rid, rd in self._lan_rooms_cache.items():
                rooms.append({
                    "id"     : rid,
                    "name"   : rd.get("name", rid),
                    "topic"  : rd.get("topic", ""),
                    "members": rd.get("member_count", 0),
                    "source" : "lan",
                })

        dlg = ctk.CTkToplevel(self)
        dlg.title("Select Room")
        dlg.geometry("460x380")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(True, True)
        dlg.grid_columnconfigure(0, weight=1)
        dlg.grid_rowconfigure(1, weight=1)
        # Defer grab_set until the window is actually mapped — avoids
        # "grab failed: window not viewable" on some Linux WMs / Python 3.14
        dlg.after(50, lambda: dlg.grab_set() if dlg.winfo_exists() else None)

        # ── Search bar ────────────────────────────────────────────────────────
        top = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)

        search_var = tk.StringVar()
        search_entry = ctk.CTkEntry(
            top,
            textvariable=search_var,
            placeholder_text="🔍  Filter rooms …",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=32, corner_radius=6, font=FONT_UI,
        )
        search_entry.grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        search_entry.focus()

        ctk.CTkFrame(top, height=1, fg_color=BORDER_TEA, corner_radius=0,
                     ).grid(row=1, column=0, sticky="ew")

        # ── Room list ─────────────────────────────────────────────────────────
        sf = ctk.CTkScrollableFrame(
            dlg, fg_color=BG_CARD, corner_radius=0,
            scrollbar_button_color=BORDER_ACT,
        )
        sf.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        sf.grid_columnconfigure(0, weight=1)

        # Status bar at the bottom
        status_bar = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0)
        status_bar.grid(row=2, column=0, sticky="ew")
        status_bar.grid_columnconfigure(0, weight=1)
        # Local variable — this dialog owns this widget and it must not outlive it
        room_picker_status = ctk.CTkLabel(
            status_bar,
            text=f"  {len(rooms)} room(s) available",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        )
        room_picker_status.grid(row=0, column=0, padx=8, pady=4, sticky="w")
        ctk.CTkButton(
            status_bar, text="✕ Close",
            font=FONT_SMALL, height=26, width=70, corner_radius=4,
            fg_color="#3a1010", hover_color="#661a1a", text_color="#e07070",
            command=dlg.destroy,
        ).grid(row=0, column=1, padx=8, pady=4)

        # ── Build / rebuild rows ──────────────────────────────────────────────
        _row_widgets: list = []

        def _clear():
            for w in _row_widgets:
                try: w.destroy()
                except Exception: pass
            _row_widgets.clear()

        def _select(room: dict):
            dlg.destroy()
            self._on_room_select_by_id(room["id"], room["name"])

        def _build(filter_text: str = ""):
            _clear()
            filt    = filter_text.lower().strip()
            visible = [r for r in rooms
                       if not filt or filt in r["name"].lower()
                       or filt in r["topic"].lower()
                       or filt in r["id"].lower()]

            room_picker_status.configure(
                text=f"  {len(visible)} / {len(rooms)} room(s)")

            if not visible:
                lbl = ctk.CTkLabel(sf,
                    text="  No rooms match your filter." if filt
                         else "  No rooms yet. Create one with + Room.",
                    font=FONT_SMALL, text_color=TXT_DIM, anchor="w")
                lbl.grid(row=0, column=0, padx=12, pady=20, sticky="w")
                _row_widgets.append(lbl)
                return

            for i, room in enumerate(visible):
                alt_bg = BG_DEEP if i % 2 else BG_CARD
                rf = ctk.CTkFrame(sf, fg_color=alt_bg, corner_radius=4,
                                  border_color=BORDER_TEA, border_width=0)
                rf.grid(row=i, column=0, padx=4, pady=2, sticky="ew")
                rf.grid_columnconfigure(0, weight=1)
                _row_widgets.append(rf)

                # Highlight matched text colour
                is_active = (room["name"] == self._room_var.get()
                             or room["id"] == self._p2p_current_room
                             or room["id"] == self._lan_current_room)
                name_col = TXT_VOSK if is_active else TXT_TEAL

                ctk.CTkLabel(
                    rf,
                    text=f"  {'●  ' if is_active else '○  '}{room['name']}",
                    font=("Segoe UI Semibold", 10),
                    text_color=name_col, anchor="w",
                ).grid(row=0, column=0, padx=4, pady=(5, 0), sticky="w")

                detail = []
                if room.get("topic"):
                    detail.append(room["topic"])
                if room.get("members"):
                    detail.append(f"👥 {room['members']}")
                detail_text = "   " + "  ·  ".join(detail) if detail else ""

                if detail_text:
                    ctk.CTkLabel(
                        rf, text=detail_text,
                        font=("Courier New", 7), text_color=TXT_DIM, anchor="w",
                    ).grid(row=1, column=0, padx=4, pady=(0, 4), sticky="w")

                ctk.CTkButton(
                    rf, text="Select →",
                    font=FONT_SMALL, height=28, width=72, corner_radius=4,
                    fg_color="#0d3a40" if not is_active else "#1a5a40",
                    hover_color="#145060",
                    command=lambda r=room: _select(r),
                ).grid(row=0, column=1, rowspan=2, padx=6, pady=4)

        _build()

        # Live filter on keystrokes
        def _on_filter(*_):
            _build(search_var.get())

        search_var.trace_add("write", _on_filter)
        search_entry.bind("<Return>", lambda e: (
            _build(search_var.get()),
            _row_widgets[0].winfo_children()[-1].invoke()
            if _row_widgets else None
        ))

    def _on_room_select_by_id(self, room_id: str, display_name: str):
        """Switch to a room given its ID.  Called from the room picker."""
        if self._p2p_mode:
            self._p2p_current_room = room_id
            self._room_var.set(display_name)
            self.chat_receive(f"[P2P]: Active room → {display_name}")
            return

        # LAN mode
        if not self._lan_client or not self._lan_client.is_connected():
            self.chat_receive("[LAN]: Not connected.")
            return
        self._join_room_with_password(room_id)

    def _on_room_select(self, choice: str):
        """Legacy callback — kept for any code that still calls it directly."""
        if self._p2p_mode:
            # Reverse-lookup room_id from display name in p2p_rooms_cache
            for rid, name in self._p2p_rooms_cache.items():
                if name == choice:
                    self._on_room_select_by_id(rid, name)
                    return
            return

        # LAN: reverse-lookup
        for rid, rd in self._lan_rooms_cache.items():
            display = f"{rd['name']}  ({rid[:12]})"
            if display == choice:
                self._join_room_with_password(rid)
                return

    def _join_room_with_password(self, room_id: str):
        """Pop a minimal password dialog then join the room."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Join Room")
        dlg.geometry("280x130")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, False)
        dlg.after(50, lambda: dlg.grab_set() if dlg.winfo_exists() else None)

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
        """Dialog to create a new room — P2P Online or LAN depending on mode."""
        if self._p2p_mode:
            self._p2p_create_room_dialog()
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
        dlg.after(50, lambda: dlg.grab_set() if dlg.winfo_exists() else None)

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

    def _p2p_create_room_dialog(self):
        """
        Create a P2P Tor room.

        Presents a clean dialog with:
          • Room name (required)
          • Password  (optional — leave blank for no password)
          • Topic     (optional description shown in room list)
          • Public / Private toggle
        After creation the .onion/room-id address is shown and can be copied.
        """
        if not self._p2p_node or not self._p2p_node.is_started():
            self.chat_receive("[P2P]: Start the node first.")
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title("Create P2P Room")
        dlg.geometry("360x280")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, False)
        dlg.after(50, lambda: dlg.grab_set() if dlg.winfo_exists() else None)

        # Header
        hdr = ctk.CTkFrame(dlg, fg_color=BG_PANEL, corner_radius=0)
        hdr.pack(fill="x")
        ctk.CTkLabel(hdr, text="⬡  New Tor Room",
                     font=("Segoe UI Semibold", 12), text_color=TXT_VOSK,
                     ).pack(padx=12, pady=8, anchor="w")
        ctk.CTkFrame(hdr, height=1, fg_color=BORDER_TEA, corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(padx=14, pady=10, fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)

        fields = {}
        field_defs = [
            ("Room name *", "e.g.  Physics Lab",    "",  False),
            ("Password",    "leave blank = no lock", "", True ),
            ("Topic",       "short description …",  "",  False),
        ]
        for row_i, (label, placeholder, default, is_pw) in enumerate(field_defs):
            ctk.CTkLabel(body, text=label, font=FONT_SMALL,
                         text_color=TXT_DIM, width=88, anchor="w",
                         ).grid(row=row_i, column=0, padx=(0, 6), pady=3, sticky="w")
            e = ctk.CTkEntry(body, height=28, fg_color=BG_INPUT,
                             border_color=BORDER_TEA, text_color=TXT_VOSK,
                             corner_radius=4, font=("Courier New", 9),
                             placeholder_text=placeholder,
                             show="*" if is_pw else "")
            e.grid(row=row_i, column=1, pady=3, sticky="ew")
            if default:
                e.insert(0, default)
            fields[label] = e

        # Public / Private toggle
        _public = tk.BooleanVar(value=True)
        tog_row = ctk.CTkFrame(body, fg_color="transparent")
        tog_row.grid(row=3, column=0, columnspan=2, pady=(6, 0), sticky="w")
        ctk.CTkLabel(tog_row, text="Visibility", font=FONT_SMALL,
                     text_color=TXT_DIM, width=88, anchor="w").pack(side="left")
        ctk.CTkSwitch(
            tog_row, text="Public  (visible to peers)",
            font=FONT_SMALL, text_color=TXT_CHAT,
            progress_color=BORDER_TEA, variable=_public,
        ).pack(side="left", padx=4)

        # Hint label
        ctk.CTkLabel(
            body,
            text="After creation, your .onion/room-id address is shown in chat.\nShare it with peers so they can join.",
            font=("Segoe UI", 7), text_color=TXT_DIM, wraplength=320, justify="left",
        ).grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="w")

        def _create():
            name  = fields["Room name *"].get().strip()
            pw    = fields["Password"].get()
            topic = fields["Topic"].get().strip()
            pub   = _public.get()
            if not name:
                fields["Room name *"].configure(border_color="#e03535")
                return
            dlg.destroy()
            def _do():
                room_id = self._p2p_node.create_room(
                    name, password=pw, public=pub, topic=topic)
                if room_id:
                    self._p2p_rooms_cache[room_id] = name
                    self._p2p_current_room = room_id
                    self.after(0, lambda: self._room_var.set(name))
            threading.Thread(target=_do, daemon=True).start()

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=8)
        ctk.CTkButton(btn_row, text="✕  Cancel", height=30, width=90, corner_radius=4,
                      fg_color="#2a1010", hover_color="#4a2020", text_color="#e07070",
                      command=dlg.destroy).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="✔  Create Room", height=30, width=120, corner_radius=4,
                      fg_color=BORDER_TEA, hover_color="#145060", text_color="#000000",
                      font=("Segoe UI Semibold", 10),
                      command=_create).pack(side="left", padx=4)

        fields["Room name *"].focus()

    # ─────────────────────────────────────────────────────────────────────────
    # LLM toggle / mode methods
    # ─────────────────────────────────────────────────────────────────────────

    def _scan_t5_models(self) -> list[str]:
        """Return the T5 model list (active model first)."""
        return _scan_t5_models_default()

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

    def _refresh_t2t_models(self):
        """Re-scan both LLM (Ollama) and T5 model lists."""
        self._refresh_llm_models()
        t5_models = self._scan_t5_models()
        self.cmb_t5.configure(values=t5_models)
        if self.cmb_t5.get() not in t5_models:
            self.cmb_t5.set(t5_models[0])
        self.update_console("[Console]: Text-to-Text model lists refreshed")

    def _toggle_t5(self):
        """Enable/disable the T5 text-correction engine (click the T5 label)."""
        self._t5_enabled = not self._t5_enabled
        if self._t5_enabled:
            self.lbl_t5.configure(text="T5", text_color="#fbbf24")
            self.cmb_t5.configure(
                state="normal", fg_color=BG_INPUT,
                text_color="#fbbf24", button_color="#60480a",
            )
        else:
            self.lbl_t5.configure(text="T5 ✕", text_color=TXT_DIM)
            self.cmb_t5.configure(
                state="disabled", fg_color="#0a0f20",
                text_color=TXT_DIM, button_color="#111a30",
            )
            self._t5_mode = None
            self._update_t5_mode_buttons()
        self.controller.set_t5_enabled(self._t5_enabled)
        self.update_console(
            f"[Console]: T5 engine {'enabled' if self._t5_enabled else 'disabled'}"
        )

    def _t5_set_mode(self, mode: str):
        """Toggle T5 correct mode on/off."""
        if not self._t5_enabled:
            self.update_console("[Console]: Enable T5 first (click the T5 label)")
            return
        self._t5_mode = None if self._t5_mode == mode else mode
        self._update_t5_mode_buttons()
        self.controller.set_t5_mode(self._t5_mode, self.cmb_t5.get())
        self.update_console(f"[Console]: T5 mode → {self._t5_mode or 'off'}")

    def _update_t5_mode_buttons(self):
        if self._t5_mode == "correct":
            self.btn_t5_correct.configure(fg_color="#5a4410", text_color="#ffe580")
        else:
            self.btn_t5_correct.configure(fg_color="#3d2e08", text_color="#fbbf24")

    def _on_t5_model_change(self, choice: str):
        self.controller.set_t5_model(choice)
        self.update_console(f"[Console]: T5 model → {choice}")

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

        self.update_console(
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

    def _on_toggle_port(self):
        """
        LAN Access toggle.

        OFF → amber  "LAN Access: Off"   (port closed, clicking will open)
        ON  → red    "LAN Access: On"    (port open,  clicking will close)

        Button is disabled for the duration of the toggle to block double-clicks;
        update_chat_port_btn() re-enables it once the server confirms its new state.
        """
        self.btn_port.configure(state="disabled")

        if self._port_on:
            # ── TURNING OFF — close port + full teardown ───────────────────
            self.controller.toggle_chat_port()

            if self._lan_client and self._lan_client.is_connected():
                try:
                    self._lan_client.disconnect()
                except Exception:
                    pass
                self._lan_client       = None
                self._lan_current_room = None
                self._lan_rooms_cache  = {}
                try:
                    self.btn_lan_connect.configure(state="normal")
                    self.btn_lan_disconnect.configure(state="disabled")
                    self.cmb_room.configure(values=["(not connected)"])
                    self.cmb_room.set("(not connected)")
                except Exception:
                    pass

            if self._p2p_node:
                try:
                    self._p2p_node.stop()
                except Exception:
                    pass
                self._p2p_node         = None
                self._p2p_current_room = None
                self._p2p_rooms_cache  = {}
                try:
                    self.btn_p2p_start.configure(state="normal")
                    self.btn_p2p_stop.configure(state="disabled")
                    self._p2p_status_lbl.configure(
                        text="  Stopped", text_color=TXT_DIM)
                    self._btn_copy_onion.grid_remove()
                except Exception:
                    pass

            self.chat_receive("")
            self.chat_receive("  ✗  LAN access disabled — port is now closed.")
            self.chat_receive("     All rooms and connections have been cleared.")
            self.chat_receive("")

        else:
            # ── TURNING ON — open port ─────────────────────────────────────
            self.controller.toggle_chat_port()

    def update_chat_port_btn(self, is_open: bool):
        """
        Reflect LAN server state on the toggle button and always re-enable it.

        OFF  →  amber / yellow   "LAN Access: Off"   (port closed)
        ON   →  red              "LAN Access: On"    (port open, prominent)

        The button was disabled in _on_toggle_port to block double-clicks while
        the server was starting or stopping; this method re-enables it.
        """
        self._port_on = is_open
        if is_open:
            # ON = red — active, prominent, easy to see it needs turning off
            self.btn_port.configure(
                text="LAN Access: On",
                fg_color="#5a0a0a",
                hover_color="#7a1010",
                text_color="#ff8080",
                border_color="#cc3333",
                border_width=1,
                state="normal",
            )
            try:
                self._conn_status_lbl.configure(
                    text="  ● hosting", text_color="#ff6060")
            except Exception:
                pass
            self.chat_receive("")
            self.chat_receive("  ✔  LAN access enabled — others on your network can connect.")
            self.chat_receive("")
        else:
            # OFF = amber — inviting, shows it can be enabled
            self.btn_port.configure(
                text="LAN Access: Off",
                fg_color="#3d2e00",
                hover_color="#5a4400",
                text_color="#f0c040",
                border_color="#f0c040",
                border_width=1,
                state="normal",
            )
            try:
                self._conn_status_lbl.configure(
                    text="  ● offline", text_color=STA_IDLE)
            except Exception:
                pass

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
        """Insert a timestamped, colour-coded line into the console."""
        tb = self.console._textbox
        tb.configure(state="normal")

        m = message.strip()
        m_lower = m.lower()

        # Pick severity tag
        if any(x in m_lower for x in (
            "error", "exception", "traceback", "fatal", "✘", " ✗ ",
            "failed to", "not found", "not installed",
        )):
            tag = "con_error"
        elif any(x in m_lower for x in (
            "warning", "warn", "[!]", "missing", "could not", "disabled",
        )):
            tag = "con_warning"
        elif any(x in m_lower for x in (
            "✔", "✓", "ready", "loaded", "complete", "installed",
            "enabled", "started", "success",
        )):
            tag = "con_success"
        elif m == "" or m.startswith("─") or m.startswith("══"):
            tb.insert("end", "\n", "con_sep")
            tb.configure(state="disabled")
            return
        else:
            tag = "con_info"

        ts = time.strftime("%H:%M")
        tb.insert("end", f" {ts} ", "con_ts")
        tb.insert("end", f"{message}\n", tag)
        tb.see("end")
        tb.configure(state="disabled")

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
        
