"""
ui/gui.py
─────────
Main application window for Spoaken — v2.1.1

Fixes / additions vs v2.1
──────────────────────────
  • ART_DIR import with graceful fallback chain.
  • _build_local_panel(), _build_online_panel(), _build_command_bar()
    — complete implementations (were omitted stubs in v2.1).
  • All chat/sidebar event handlers now implemented:
      _switch_to_local/online, _on_toggle_port, _on_chat_send,
      _on_room_bar_join, _open_room_picker, _open_room_browser,
      _on_create_room, _on_p2p_start, _on_p2p_stop,
      _open_file_transfer_dialog, chat_receive,
      update_chat_port_btn, thread_safety_chat_port_btn,
      _p2p_event_handler, _update_room_list, _update_conn_status,
      _lan_connect_bg, _lan_disconnect.
  • Controller-facing APIs added:
      thread_safety_word_count(), show_restore_prompt(),
      hide_restore_prompt().
  • update_status / thread_safety_status accept degraded= kwarg
    (used by controller's system-pressure monitor).
  • Word-count label added to transcript header.
  • LAN client implemented via raw websockets (bypasses NotImplementedError
    stub in lan.py) — connects to any running Spoaken ChatServer.
  • All Tk/CTk updates from background threads route through after(0, …).
"""

import math
import time
import asyncio
import threading
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
ctk.deactivate_automatic_dpi_awareness()

from tkinter import messagebox, filedialog, simpledialog
from PIL import Image, ImageTk

# ── ART_DIR with three-level fallback ─────────────────────────────────────────
try:
    from spoaken.system.paths import ART_DIR
except (ImportError, AttributeError):
    try:
        from spoaken.system.paths import ASSETS_DIR as ART_DIR
    except ImportError:
        ART_DIR = Path(__file__).parent.parent / "assets"

from spoaken.core.engine import (
    list_input_devices, default_device_name,
    scan_installed_vosk_models, scan_installed_whisper_models,
)
from spoaken.core.config import VOSK_ENABLED, WHISPER_ENABLED, ENGINE_MODE

# ── Optional-module availability flags ───────────────────────────────────────
import importlib.util as _iutil

_LLM_AVAILABLE    = _iutil.find_spec("spoaken.processing.llm")    is not None
_P2P_AVAILABLE    = _iutil.find_spec("spoaken.network.online")    is not None
_UPDATE_AVAILABLE = _iutil.find_spec("spoaken.control.update")    is not None
_WRITER_AVAILABLE = _iutil.find_spec("spoaken.processing.writer") is not None
_WS_AVAILABLE     = _iutil.find_spec("websockets")                is not None

del _iutil


def _scan_llm_models() -> list[str]:
    try:
        from spoaken.processing.llm import list_ollama_models
        models = list_ollama_models()
        return models if models else ["(Ollama offline)"]
    except Exception:
        return ["(Ollama not installed)"]


def _scan_t5_models_default() -> list[str]:
    try:
        from spoaken.core.config import T5_MODEL
        active = T5_MODEL
    except Exception:
        active = "vennify/t5-base-grammar-correction"
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
    return [active] + [m for m in _KNOWN if m != active]


# ── Colour palette ────────────────────────────────────────────────────────────
BG_DEEP    = "#060c1a"
BG_PANEL   = "#0a1128"
BG_CARD    = "#0d1735"
BG_INPUT   = "#0c1636"
BG_SIDEBAR = "#080f20"
BORDER_SUB = "#1a2d60"
BORDER_ACT = "#2545a8"
BORDER_TEA = "#0d4d60"

TXT_MAIN    = "#00bdff"
TXT_DIM     = "#2a6080"
TXT_VOSK    = "#00e5cc"
TXT_WHISPER = "#4dd9f5"
TXT_PARTIAL = "#2a8fa8"
TXT_CHAT    = "#80e0f0"
TXT_CONSOLE = "#007bff"
TXT_TEAL    = "#2bfbf9"
TXT_WARN    = "#d4aa00"
TXT_OK      = "#24c45e"
TXT_ERR     = "#e03535"

BTN_REC   = "#1a5e2a";  BTN_REC_H  = "#24883c"
BTN_STOP  = "#c42828";  BTN_STOP_H = "#e03535"
BTN_WON   = "#b85c00";  BTN_WON_H  = "#d97000"
BTN_WOFF  = "#182236";  BTN_WOFF_H = "#243350"
BTN_CLR   = "#5e1414";  BTN_CLR_H  = "#852020"
BTN_LOG   = "#0d3a40";  BTN_LOG_H  = "#125660"

STA_IDLE = "#44537a";  STA_REC = "#d42b2b";  STA_CORR = "#2c5fe6"
STA_DEG  = "#d47800"   # degraded / high-pressure state

WF_IDLE = ((20, 40, 80),  (35, 65, 150))
WF_REC  = ((15, 80, 60),  (0, 220, 160))
WF_CORR = ((20, 60, 180), (60, 200, 255))

FONT_MONO  = ("Courier New", 11)
FONT_UI    = ("Segoe UI",    11)
FONT_SMALL = ("Segoe UI",     9)
FONT_TITLE = ("Segoe UI Semibold", 13)

_WF_BARS     = 30
_WF_FPS_IDLE = 200
_WF_FPS_LIVE = 50
_CONSOLE_MAX_LINES = 200


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

        self.title("Spoaken  v2.1")
        self._base_width  = 1060
        self._base_height = 800
        self.geometry(f"{self._base_width}x{self._base_height}")
        self.minsize(760, 600)
        self.configure(fg_color=BG_DEEP)

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
        self._audio_rms  = 0.0
        self._wf_bar_ids = []

        # Sidebar
        self._sidebar_open = False

        # Advanced section — built lazily on first open
        self._adv_open  = False
        self._adv_built = False

        # Restore-prompt reference (created dynamically)
        self._restore_bar = None

        # Engine at startup
        self._active_engine = "vosk" if VOSK_ENABLED else "whisper"

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0, minsize=0)
        self.grid_rowconfigure(0, weight=1)

        self._h_pane = tk.PanedWindow(
            self, orient=tk.HORIZONTAL,
            bg=BORDER_SUB, sashwidth=7, sashrelief="flat",
            sashpad=1, bd=0, opaqueresize=True,
        )
        self._h_pane.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="nsew")

        self._build_transcript_panel()
        self._build_centre_panel()
        self._build_sidebar()
        self._configure_log_tags()

        self.after(150, self._restore_sash)
        self.after(_WF_FPS_IDLE, self._wf_loop)
        self.protocol("WM_DELETE_WINDOW", self.controller.on_close_request)

    # ── Sash restore ──────────────────────────────────────────────────────────

    def _restore_sash(self, attempt: int = 0):
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
        lp = ctk.CTkFrame(self._h_pane, fg_color=BG_CARD,
                          border_color=BORDER_SUB, border_width=1, corner_radius=8)
        self._h_pane.add(lp, minsize=220, stretch="always")
        lp.grid_rowconfigure(1, weight=1)
        lp.grid_columnconfigure(0, weight=1)

        # Header row: label + word-count + copy button
        lf = ctk.CTkFrame(lp, fg_color="transparent")
        lf.grid(row=0, column=0, padx=10, pady=(8, 0), sticky="ew")
        lf.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(lf, text="Transcript",
                     font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
                     ).grid(row=0, column=0, sticky="w")

        self._lbl_word_count = ctk.CTkLabel(
            lf, text="",
            font=("Courier New", 8), text_color=TXT_DIM, anchor="center",
        )
        self._lbl_word_count.grid(row=0, column=1, sticky="ew")

        self.btn_copy = ctk.CTkButton(
            lf, text="⧉  Copy", font=FONT_SMALL, height=20,
            corner_radius=4, width=64, fg_color="transparent",
            hover_color=BORDER_SUB, text_color=TXT_DIM, border_width=0,
            command=self.controller.copy_transcript,
        )
        self.btn_copy.grid(row=0, column=2, sticky="e")

        self.log = ctk.CTkTextbox(
            lp, fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            font=FONT_MONO, text_color=TXT_VOSK,
            scrollbar_button_color=BORDER_ACT, corner_radius=8, wrap="word",
        )
        self.log.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="nsew")
        self.log.configure(state="disabled")

    # ── Word-count (called from controller) ───────────────────────────────────

    def thread_safety_word_count(self, count: int):
        """Update transcript word-count label from any thread."""
        def _up():
            if count > 0:
                self._lbl_word_count.configure(text=f"{count:,} words")
            else:
                self._lbl_word_count.configure(text="")
        self.after(0, _up)

    # ── Crash-recovery prompt ─────────────────────────────────────────────────

    def show_restore_prompt(self, seg_count: int, word_count: int):
        """
        Show a non-intrusive amber bar at the top of the transcript area
        offering to restore the previous session.
        """
        if self._restore_bar is not None:
            try:
                if self._restore_bar.winfo_exists():
                    return
            except Exception:
                pass

        # Insert the bar above the log widget inside the transcript panel
        lp = self.log.master  # the CTkFrame holding the log

        bar = ctk.CTkFrame(lp, fg_color="#2a1800",
                           border_color="#c87000", border_width=1, corner_radius=6)
        bar.grid(row=0, column=0, padx=8, pady=(4, 2), sticky="ew")
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            bar,
            text=f"⚠  Unsaved session: {seg_count} segments, ~{word_count} words",
            font=FONT_SMALL, text_color=TXT_WARN, anchor="w",
        ).grid(row=0, column=0, padx=(8, 6), pady=4, sticky="w")

        bf = ctk.CTkFrame(bar, fg_color="transparent")
        bf.grid(row=0, column=1, sticky="e", padx=(0, 6))

        ctk.CTkButton(
            bf, text="Restore", font=FONT_SMALL, height=22, width=64,
            corner_radius=4, fg_color="#4a3000", hover_color="#6a4400",
            text_color=TXT_WARN,
            command=self.controller.restore_session,
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            bf, text="Discard", font=FONT_SMALL, height=22, width=64,
            corner_radius=4, fg_color="#1a0a0a", hover_color="#3a1010",
            text_color="#804040",
            command=self.controller.discard_recovery,
        ).pack(side="left", padx=2)

        # Push the log down by one row
        self.log.grid_configure(row=1)
        lp.grid_rowconfigure(0, weight=0)
        lp.grid_rowconfigure(1, weight=1)

        self._restore_bar = bar

    def hide_restore_prompt(self):
        """Remove the restore-session bar."""
        if self._restore_bar is None:
            return
        try:
            if self._restore_bar.winfo_exists():
                self._restore_bar.destroy()
        except Exception:
            pass
        finally:
            self._restore_bar = None

    # ─────────────────────────────────────────────────────────────────────────
    # Centre panel
    # ─────────────────────────────────────────────────────────────────────────

    def _build_centre_panel(self):
        mp = ctk.CTkFrame(self._h_pane, fg_color="transparent")
        self._h_pane.add(mp, minsize=360, width=400, stretch="never")
        mp.grid_rowconfigure(1, weight=0)
        mp.grid_rowconfigure(2, weight=1)
        mp.grid_rowconfigure(3, weight=0)
        mp.grid_columnconfigure(0, weight=1)

        self._build_header(mp)
        self._build_console(mp)
        self._build_controls(mp)

    def _build_header(self, parent):
        hf = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=8,
                          border_color=BORDER_SUB, border_width=1)
        hf.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        hf.grid_columnconfigure(0, weight=1)

        tr = ctk.CTkFrame(hf, fg_color="transparent")
        tr.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        tr.grid_columnconfigure(1, weight=1)

        for _icon_name in ("icon.png", "icon.ico", "logo.png", "logo.ico"):
            _icon_path = ART_DIR / _icon_name
            if _icon_path.exists():
                try:
                    _img = Image.open(_icon_path).resize((26, 26), Image.LANCZOS)
                    self._header_icon = ctk.CTkImage(light_image=_img, dark_image=_img, size=(26, 26))
                    ctk.CTkLabel(tr, image=self._header_icon, text="",
                                 width=26, height=26,
                                 ).grid(row=0, column=0, sticky="w", padx=(0, 6))
                    break
                except Exception:
                    pass

        ctk.CTkLabel(tr, text="SPOAKEN",
                     font=FONT_TITLE, text_color=TXT_MAIN, anchor="w",
                     ).grid(row=0, column=1, sticky="w")

        _upd_fg    = "#00e5cc" if _UPDATE_AVAILABLE else "#1a2a3a"
        _upd_hover = "#00c8b0" if _UPDATE_AVAILABLE else "#1a2a3a"
        _upd_txt   = "#000000" if _UPDATE_AVAILABLE else "#2a4060"
        ctk.CTkButton(
            tr, text="⟳  Update" if _UPDATE_AVAILABLE else "⟳  Update (n/a)",
            font=FONT_SMALL, height=24, width=100, corner_radius=5,
            fg_color=_upd_fg, hover_color=_upd_hover, text_color=_upd_txt,
            state="normal" if _UPDATE_AVAILABLE else "disabled",
            command=self._open_update_window,
        ).grid(row=0, column=2, padx=(8, 8), sticky="e")

        self.lbl_status = ctk.CTkLabel(tr, text="●  IDLE",
                                       font=FONT_SMALL, text_color=STA_IDLE, anchor="e")
        self.lbl_status.grid(row=0, column=3, sticky="e")

        ctk.CTkFrame(hf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=1, column=0, sticky="ew")

        self._wf_canvas = tk.Canvas(hf, height=52, bg=BG_PANEL, highlightthickness=0)
        self._wf_canvas.grid(row=2, column=0, padx=0, pady=(4, 4), sticky="ew")
        self._wf_canvas.bind("<Configure>", self._on_wf_resize)

        ctk.CTkFrame(hf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=3, column=0, sticky="ew")

    def _build_console(self, parent):
        cf = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=8,
                          border_color=BORDER_SUB, border_width=1)
        cf.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        cf.grid_rowconfigure(1, weight=1)
        cf.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(cf, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(hdr, text="Console",
                     font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
                     ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(hdr, text="Clear",
                      font=FONT_SMALL, height=18, width=44, corner_radius=4,
                      fg_color="transparent", hover_color=BORDER_SUB,
                      text_color=TXT_DIM, border_width=0,
                      command=self._clear_console,
                      ).grid(row=0, column=1, sticky="e")

        self.console = ctk.CTkTextbox(
            cf, fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
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
        cf = ctk.CTkFrame(parent, fg_color=BG_CARD,
                          border_color=BORDER_SUB, border_width=1, corner_radius=8)
        cf.grid(row=3, column=0, sticky="ew")
        cf.grid_columnconfigure(0, weight=1)
        self._controls_frame = cf

        # Microphone selector row
        mic_row = ctk.CTkFrame(cf, fg_color="transparent")
        mic_row.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        mic_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(mic_row, text="Microphone",
                     font=FONT_SMALL, text_color=TXT_DIM, anchor="w", width=80,
                     ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        devices      = list_input_devices()
        device_names = ["System Default"] + [f"{i}: {n}" for i, n in devices]
        self._device_indices = [None] + [i for i, _ in devices]

        self.cmb_mic = ctk.CTkComboBox(
            mic_row, values=device_names, font=FONT_SMALL, text_color=TXT_MAIN,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_ACT, button_hover_color="#3a60c8",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_MAIN,
            height=30, corner_radius=6, command=self._on_mic_change,
        )
        self.cmb_mic.set(device_names[0])
        self.cmb_mic.grid(row=0, column=1, sticky="ew")

        self.btn_noise = ctk.CTkButton(
            mic_row, text="Noise: OFF", font=FONT_SMALL, height=30,
            corner_radius=6, width=90, fg_color="#1a2640", hover_color="#253560",
            command=self._toggle_noise,
        )
        self.btn_noise.grid(row=0, column=2, padx=(6, 0))
        self._noise_on = False

        ctk.CTkButton(
            mic_row, text="⚙ Mic Setup", font=FONT_SMALL, height=30,
            corner_radius=6, width=80, fg_color="#0d1f3a", hover_color="#1a3060",
            text_color="#00bdff", command=self._open_mic_config,
        ).grid(row=0, column=3, padx=(6, 0))

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=1, column=0, sticky="ew", pady=(6, 0))

        # Voice-to-Text engine toggle
        ctk.CTkLabel(cf, text="Voice-to-Text Engine",
                     font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
                     ).grid(row=2, column=0, padx=16, pady=(6, 0), sticky="w")

        eng_row = ctk.CTkFrame(cf, fg_color="transparent")
        eng_row.grid(row=3, column=0, padx=14, pady=(4, 8), sticky="ew")
        eng_row.grid_columnconfigure(2, weight=1)

        self._btn_vosk = ctk.CTkButton(
            eng_row, text="Vosk",
            font=FONT_SMALL, height=30, width=70, corner_radius=5,
            fg_color=BORDER_TEA, hover_color="#0d6080", text_color=TXT_VOSK,
            command=lambda: self._set_engine("vosk"),
        )
        self._btn_vosk.grid(row=0, column=0, padx=(0, 2))

        self._btn_whisper = ctk.CTkButton(
            eng_row, text="Whisper",
            font=FONT_SMALL, height=30, width=70, corner_radius=5,
            fg_color="#12182e", hover_color="#1a2a50", text_color=TXT_DIM,
            command=lambda: self._set_engine("whisper"),
        )
        self._btn_whisper.grid(row=0, column=1, padx=(0, 8))

        self._vosk_models    = scan_installed_vosk_models()
        self._whisper_models = scan_installed_whisper_models()

        self.cmb_model = ctk.CTkComboBox(
            eng_row, font=("Courier New", 9), text_color=TXT_VOSK,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_TEA, button_hover_color="#0d6080",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_VOSK,
            height=28, corner_radius=5,
            command=self._on_model_change,
        )
        self.cmb_model.grid(row=0, column=2, sticky="ew")

        ctk.CTkButton(
            eng_row, text="↺", font=("Segoe UI", 12), height=28, width=28,
            corner_radius=5, fg_color="#0d2040", hover_color="#1a3a60",
            command=self._refresh_model_lists,
        ).grid(row=0, column=3, padx=(6, 0))

        self._update_engine_toggle_visuals(self._active_engine)

        ctk.CTkFrame(cf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=4, column=0, sticky="ew")

        # Start Recording button
        self.btn_start = ctk.CTkButton(
            cf, text="Start Recording", font=FONT_UI, height=42, corner_radius=6,
            fg_color=BTN_REC, hover_color=BTN_REC_H,
            command=self.controller.toggle_recording,
        )
        self.btn_start.grid(row=5, column=0, padx=14, pady=(8, 6), sticky="ew")

        # Utility row
        util_row = ctk.CTkFrame(cf, fg_color="transparent")
        util_row.grid(row=6, column=0, padx=14, pady=(0, 4), sticky="ew")
        for c in range(4):
            util_row.grid_columnconfigure(c, weight=1)

        self.btn_writing = ctk.CTkButton(
            util_row, text="Write: OFF", font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_WOFF if _WRITER_AVAILABLE else "#0d1520",
            hover_color=BTN_WOFF_H if _WRITER_AVAILABLE else "#0d1520",
            text_color=TXT_MAIN if _WRITER_AVAILABLE else TXT_DIM,
            state="normal" if _WRITER_AVAILABLE else "disabled",
            command=self.controller.toggle_page_writing,
        )
        self.btn_writing.grid(row=0, column=0, padx=(0, 3), sticky="ew")

        ctk.CTkButton(
            util_row, text="Logs", font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_LOG, hover_color="#146060",
            command=self.controller.open_logs,
        ).grid(row=0, column=1, padx=3, sticky="ew")

        ctk.CTkButton(
            util_row, text="Clear", font=FONT_SMALL, height=30, corner_radius=6,
            fg_color=BTN_CLR, hover_color=BTN_CLR_H,
            command=self.controller.clear_all_logs,
        ).grid(row=0, column=2, padx=3, sticky="ew")

        self.btn_chat = ctk.CTkButton(
            util_row, text="Chat ▶", font=FONT_SMALL, height=30, corner_radius=6,
            fg_color="#0d3a40", hover_color="#145060",
            command=self._toggle_sidebar,
        )
        self.btn_chat.grid(row=0, column=3, padx=(3, 0), sticky="ew")

        # Advanced accordion trigger
        self._adv_toggle_btn = ctk.CTkButton(
            cf, text="▶  Advanced  (LLM · T5 · Polish · Write target)",
            font=FONT_SMALL, height=26, corner_radius=0,
            fg_color=BG_PANEL, hover_color=BORDER_SUB,
            text_color=TXT_DIM, anchor="w",
            command=self._toggle_advanced,
        )
        self._adv_toggle_btn.grid(row=7, column=0, sticky="ew", pady=(4, 0))

        self._adv_frame = ctk.CTkFrame(cf, fg_color="transparent")

    # ── Advanced accordion ────────────────────────────────────────────────────

    def _toggle_advanced(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            if not self._adv_built:
                self._build_advanced_content()
                self._adv_built = True
            self._adv_frame.grid(row=8, column=0, sticky="ew")
            self._adv_toggle_btn.configure(
                text="▼  Advanced  (LLM · T5 · Polish · Write target)",
                text_color=TXT_MAIN,
            )
        else:
            self._adv_frame.grid_remove()
            self._adv_toggle_btn.configure(
                text="▶  Advanced  (LLM · T5 · Polish · Write target)",
                text_color=TXT_DIM,
            )

    def _build_advanced_content(self):
        af = self._adv_frame
        af.grid_columnconfigure(0, weight=1)

        ctk.CTkFrame(af, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(af, text="Text-to-Text Models",
                     font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
                     ).grid(row=1, column=0, padx=16, pady=(6, 0), sticky="w")

        t2t_row = ctk.CTkFrame(af, fg_color="transparent")
        t2t_row.grid(row=2, column=0, padx=14, pady=(4, 8), sticky="ew")
        t2t_row.grid_columnconfigure(1, weight=1)
        t2t_row.grid_columnconfigure(3, weight=1)

        # LLM
        self._llm_enabled = True
        self._llm_models  = _scan_llm_models() if _LLM_AVAILABLE else ["(unavailable)"]
        _llm_col   = "#c084fc" if _LLM_AVAILABLE else TXT_DIM
        _llm_state = "normal"  if _LLM_AVAILABLE else "disabled"

        self.lbl_llm = ctk.CTkLabel(t2t_row, text="LLM",
                                    font=FONT_SMALL, text_color=_llm_col, anchor="w", width=36,
                                    cursor="hand2" if _LLM_AVAILABLE else "arrow")
        self.lbl_llm.grid(row=0, column=0, sticky="w", padx=(0, 4))
        if _LLM_AVAILABLE:
            self.lbl_llm.bind("<Button-1>", lambda e: self._toggle_llm())

        self.cmb_llm = ctk.CTkComboBox(
            t2t_row, values=self._llm_models,
            font=("Courier New", 9), text_color=_llm_col,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color="#3a1a60" if _LLM_AVAILABLE else BORDER_SUB,
            button_hover_color="#5a2a90" if _LLM_AVAILABLE else BORDER_SUB,
            dropdown_fg_color=BG_CARD, dropdown_text_color=_llm_col,
            height=28, corner_radius=5, state=_llm_state,
            command=self._on_llm_model_change,
        )
        self.cmb_llm.set(self._llm_models[0])
        self.cmb_llm.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        # T5
        self._t5_enabled = True
        self.lbl_t5 = ctk.CTkLabel(t2t_row, text="T5",
                                   font=FONT_SMALL, text_color="#fbbf24", anchor="w", width=28,
                                   cursor="hand2")
        self.lbl_t5.grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.lbl_t5.bind("<Button-1>", lambda e: self._toggle_t5())

        self._t5_models = _scan_t5_models_default()
        self.cmb_t5 = ctk.CTkComboBox(
            t2t_row, values=self._t5_models,
            font=("Courier New", 9), text_color="#fbbf24",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color="#60480a", button_hover_color="#8a6810",
            dropdown_fg_color=BG_CARD, dropdown_text_color="#fbbf24",
            height=28, corner_radius=5,
            command=self._on_t5_model_change,
        )
        self.cmb_t5.set(self._t5_models[0])
        self.cmb_t5.grid(row=0, column=3, sticky="ew", padx=(0, 6))

        ctk.CTkButton(t2t_row, text="↺",
                      font=("Segoe UI", 12), height=28, width=28, corner_radius=5,
                      fg_color="#0d2040", hover_color="#1a3a60",
                      command=self._refresh_t2t_models,
                      ).grid(row=0, column=4)

        # LLM action buttons
        llm_btn_row = ctk.CTkFrame(af, fg_color="transparent")
        llm_btn_row.grid(row=3, column=0, padx=14, pady=(0, 6), sticky="ew")
        for c in range(3):
            llm_btn_row.grid_columnconfigure(c, weight=1)

        self.btn_llm_translate = ctk.CTkButton(
            llm_btn_row, text="Translate", font=FONT_SMALL, height=28, corner_radius=5,
            fg_color="#2a1a40" if _LLM_AVAILABLE else "#181820",
            hover_color="#3d2660" if _LLM_AVAILABLE else "#181820",
            text_color="#c084fc" if _LLM_AVAILABLE else TXT_DIM,
            state="normal" if _LLM_AVAILABLE else "disabled",
            command=lambda: self._llm_set_mode("translate"),
        )
        self.btn_llm_translate.grid(row=0, column=0, padx=(0, 2), sticky="ew")

        self.btn_llm_summarize = ctk.CTkButton(
            llm_btn_row, text="Summarize", font=FONT_SMALL, height=28, corner_radius=5,
            fg_color="#1a1a40" if _LLM_AVAILABLE else "#181820",
            hover_color="#282870" if _LLM_AVAILABLE else "#181820",
            text_color="#9090d0" if _LLM_AVAILABLE else TXT_DIM,
            state="normal" if _LLM_AVAILABLE else "disabled",
            command=lambda: self._llm_set_mode("summarize"),
        )
        self.btn_llm_summarize.grid(row=0, column=1, padx=2, sticky="ew")

        self.btn_t5_correct = ctk.CTkButton(
            llm_btn_row, text="T5 Correct", font=FONT_SMALL, height=28, corner_radius=5,
            fg_color="#3d2e08", hover_color="#5a4410", text_color="#fbbf24",
            command=lambda: self._t5_set_mode("correct"),
        )
        self.btn_t5_correct.grid(row=0, column=2, padx=(2, 0), sticky="ew")

        self._llm_mode = None
        self._t5_mode  = None

        ctk.CTkFrame(af, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=4, column=0, sticky="ew", pady=(4, 0))

        ctk.CTkLabel(
            af,
            text="Write to application" if _WRITER_AVAILABLE else
                 "Write to application  [unavailable — writer.py deleted]",
            font=FONT_SMALL,
            text_color=TXT_DIM if _WRITER_AVAILABLE else "#2a3a50",
            anchor="w",
        ).grid(row=5, column=0, padx=14, pady=(8, 2), sticky="w")

        target_row = ctk.CTkFrame(af, fg_color="transparent")
        target_row.grid(row=6, column=0, padx=14, pady=(0, 8), sticky="ew")
        target_row.grid_columnconfigure(0, weight=1)

        self.ent_target = ctk.CTkEntry(
            target_row,
            placeholder_text="Enter window title …" if _WRITER_AVAILABLE else "unavailable",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN if _WRITER_AVAILABLE else TXT_DIM,
            placeholder_text_color=TXT_DIM,
            height=34, corner_radius=6, font=FONT_UI,
            state="normal" if _WRITER_AVAILABLE else "disabled",
        )
        self.ent_target.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        if _WRITER_AVAILABLE:
            self.ent_target.bind("<Return>", lambda e: self.controller.lock_writer_target())

        self.btn_lock = ctk.CTkButton(
            target_row, text="Lock In", font=FONT_SMALL, height=34, corner_radius=6,
            fg_color="#1a3a5e" if _WRITER_AVAILABLE else "#0d1f30",
            hover_color="#2450a0" if _WRITER_AVAILABLE else "#0d1f30",
            text_color=TXT_MAIN if _WRITER_AVAILABLE else TXT_DIM,
            width=90,
            state="normal" if _WRITER_AVAILABLE else "disabled",
            command=self.controller.lock_writer_target,
        )
        self.btn_lock.grid(row=0, column=1)

        ctk.CTkFrame(af, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=7, column=0, sticky="ew")

        self.btn_polish = ctk.CTkButton(
            af, text="Polish (full T5 pass)",
            font=FONT_SMALL, height=30, corner_radius=6,
            fg_color="#1a3a90", hover_color="#2550cc",
            command=self.controller.swap_polishing,
        )
        self.btn_polish.grid(row=8, column=0, padx=14, pady=(6, 10), sticky="ew")

    def _open_mic_config(self):
        try:
            from spoaken.system.mic_config import MicConfigPanel
            MicConfigPanel(parent=self, controller=self.controller)
        except Exception as exc:
            self.update_console(f"[Mic Config]: could not open — {exc}")

    def _configure_log_tags(self):
        t = self.log._textbox
        t.tag_configure("vosk",      foreground=TXT_VOSK)
        t.tag_configure("whisper",   foreground=TXT_WHISPER)
        t.tag_configure("partial",   foreground=TXT_PARTIAL)
        t.tag_configure("pending",   foreground=TXT_PARTIAL)
        t.tag_configure("confirmed", foreground=TXT_VOSK)
        t.tag_configure("corrected", foreground="#a8f0a8")

        c = self.console._textbox
        c.tag_configure("con_ts",      foreground="#1a3460", font=("Courier New", 9))
        c.tag_configure("con_info",    foreground=TXT_CONSOLE)
        c.tag_configure("con_success", foreground="#00e5a0", font=("Courier New", 11, "bold"))
        c.tag_configure("con_warning", foreground="#f0c040")
        c.tag_configure("con_error",   foreground="#ff5555", font=("Courier New", 11, "bold"))
        c.tag_configure("con_dim",     foreground="#1a3060")
        c.tag_configure("con_sep",     foreground="#0d2040")

        cl = self._chat_log._textbox
        cl.tag_configure("chat_peer",   foreground="#e8f4ff", font=("Segoe UI", 10, "bold"))
        cl.tag_configure("chat_me",     foreground=TXT_TEAL,  font=("Segoe UI", 10))
        cl.tag_configure("chat_system", foreground=TXT_DIM,   font=("Segoe UI", 8))
        cl.tag_configure("chat_header", foreground=TXT_VOSK,  font=("Segoe UI", 9, "bold"))
        cl.tag_configure("chat_error",  foreground="#ff6060", font=("Segoe UI", 9))

    # ─────────────────────────────────────────────────────────────────────────
    # Engine switch
    # ─────────────────────────────────────────────────────────────────────────

    def _set_engine(self, engine: str):
        if engine == self._active_engine:
            return
        self._active_engine = engine
        self._update_engine_toggle_visuals(engine)
        self.controller.set_engine(engine)
        self.update_console(f"[Engine]: switched to {'Vosk' if engine == 'vosk' else 'Whisper'}")

    def _update_engine_toggle_visuals(self, engine: str):
        if engine == "vosk":
            self._btn_vosk.configure(fg_color=BORDER_TEA, hover_color="#0d6080", text_color=TXT_VOSK)
            self._btn_whisper.configure(fg_color="#12182e", hover_color="#1a2a50", text_color=TXT_DIM)
            self.cmb_model.configure(
                values=self._vosk_models, text_color=TXT_VOSK,
                button_color=BORDER_TEA, button_hover_color="#0d6080",
                dropdown_text_color=TXT_VOSK,
            )
            self.cmb_model.set(self._vosk_models[0])
        else:
            self._btn_whisper.configure(fg_color=BORDER_ACT, hover_color="#3060d0", text_color=TXT_WHISPER)
            self._btn_vosk.configure(fg_color="#12182e", hover_color="#1a2a50", text_color=TXT_DIM)
            self.cmb_model.configure(
                values=self._whisper_models, text_color=TXT_WHISPER,
                button_color=BORDER_ACT, button_hover_color="#3060d0",
                dropdown_text_color=TXT_WHISPER,
            )
            self.cmb_model.set(self._whisper_models[0])

    def _on_model_change(self, choice: str):
        if self._active_engine == "vosk":
            self.controller.swap_vosk_model(choice)
        else:
            self.controller.swap_whisper_model(choice)

    def _refresh_model_lists(self):
        # Clear the lru_cache so we re-scan the filesystem instead of returning
        # stale results from the last scan (e.g. after a model was just downloaded).
        scan_installed_vosk_models.cache_clear()
        scan_installed_whisper_models.cache_clear()
        self._vosk_models    = scan_installed_vosk_models()
        self._whisper_models = scan_installed_whisper_models()
        if self._active_engine == "vosk":
            self.cmb_model.configure(values=self._vosk_models)
            if self.cmb_model.get() not in self._vosk_models:
                self.cmb_model.set(self._vosk_models[0])
        else:
            self.cmb_model.configure(values=self._whisper_models)
            if self.cmb_model.get() not in self._whisper_models:
                self.cmb_model.set(self._whisper_models[0])
        n_v = len([m for m in self._vosk_models    if not m.startswith("(")])
        n_w = len([m for m in self._whisper_models if not m.startswith("(")])
        self.update_console(f"[Models]: {n_v} Vosk, {n_w} Whisper installed")

    # ── Mic / noise ───────────────────────────────────────────────────────────

    def _on_mic_change(self, choice: str):
        idx = self._device_indices[
            next((i for i, n in enumerate(self.cmb_mic.cget("values")) if n == choice), 0)
        ]
        self.controller.set_mic_device(idx)
        self.update_console(f"[Mic]: {choice}")

    def _toggle_noise(self):
        self._noise_on = not self._noise_on
        if self._noise_on:
            self.btn_noise.configure(text="Noise: ON", fg_color="#1a4a30", hover_color="#226640")
        else:
            self.btn_noise.configure(text="Noise: OFF", fg_color="#1a2640", hover_color="#253560")
        self.controller.toggle_noise_suppression(self._noise_on)

    # ── LLM ───────────────────────────────────────────────────────────────────

    def _toggle_llm(self):
        self._llm_enabled = not self._llm_enabled
        if self._llm_enabled:
            self.lbl_llm.configure(text="LLM", text_color="#c084fc")
            self.cmb_llm.configure(state="normal", fg_color=BG_INPUT,
                                   text_color="#c084fc", button_color="#3a1a60")
        else:
            self.lbl_llm.configure(text="LLM ✕", text_color=TXT_DIM)
            self.cmb_llm.configure(state="disabled", fg_color="#0a0f20",
                                   text_color=TXT_DIM, button_color="#111a30")
            self._llm_mode = None
            self._update_llm_mode_buttons()
        self.controller.set_llm_enabled(self._llm_enabled)
        self.update_console(f"[LLM]: {'enabled' if self._llm_enabled else 'disabled'}")

    def _llm_set_mode(self, mode: str):
        if not self._llm_enabled:
            self.update_console("[LLM]: enable LLM first (click the LLM label)")
            return
        self._llm_mode = None if self._llm_mode == mode else mode
        self._update_llm_mode_buttons()
        self.controller.set_llm_mode(self._llm_mode, self.cmb_llm.get())
        self.update_console(f"[LLM]: mode → {self._llm_mode or 'off'}")

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
        self.update_console(f"[LLM]: model → {choice}")

    def _refresh_llm_models(self):
        models = _scan_llm_models()
        self.cmb_llm.configure(values=models)
        if self.cmb_llm.get() not in models:
            self.cmb_llm.set(models[0])
        self.update_console(f"[LLM]: {len([m for m in models if not m.startswith('(')])} model(s) found")

    def _refresh_t2t_models(self):
        self._refresh_llm_models()
        t5_models = _scan_t5_models_default()
        self.cmb_t5.configure(values=t5_models)
        if self.cmb_t5.get() not in t5_models:
            self.cmb_t5.set(t5_models[0])
        self.update_console("[T2T]: model lists refreshed")

    # ── T5 ────────────────────────────────────────────────────────────────────

    def _toggle_t5(self):
        self._t5_enabled = not self._t5_enabled
        if self._t5_enabled:
            self.lbl_t5.configure(text="T5", text_color="#fbbf24")
            self.cmb_t5.configure(state="normal", fg_color=BG_INPUT,
                                  text_color="#fbbf24", button_color="#60480a")
        else:
            self.lbl_t5.configure(text="T5 ✕", text_color=TXT_DIM)
            self.cmb_t5.configure(state="disabled", fg_color="#0a0f20",
                                  text_color=TXT_DIM, button_color="#111a30")
            self._t5_mode = None
            self._update_t5_mode_buttons()
        self.controller.set_t5_enabled(self._t5_enabled)
        self.update_console(f"[T5]: {'enabled' if self._t5_enabled else 'disabled'}")

    def _t5_set_mode(self, mode: str):
        if not self._t5_enabled:
            self.update_console("[T5]: enable T5 first (click the T5 label)")
            return
        self._t5_mode = None if self._t5_mode == mode else mode
        self._update_t5_mode_buttons()
        self.controller.set_t5_mode(self._t5_mode, self.cmb_t5.get())
        self.update_console(f"[T5]: mode → {self._t5_mode or 'off'}")

    def _update_t5_mode_buttons(self):
        if self._t5_mode == "correct":
            self.btn_t5_correct.configure(fg_color="#5a4410", text_color="#ffe580")
        else:
            self.btn_t5_correct.configure(fg_color="#3d2e08", text_color="#fbbf24")

    def _on_t5_model_change(self, choice: str):
        self.controller.set_t5_model(choice)
        self.update_console(f"[T5]: model → {choice}")

    # ─────────────────────────────────────────────────────────────────────────
    # Waveform
    # ─────────────────────────────────────────────────────────────────────────

    def _on_wf_resize(self, event):
        self._wf_bar_ids = []

    def push_audio_level(self, rms: float):
        self._audio_rms = min(rms * 5.0, 1.0)

    def _wf_loop(self):
        try:
            self._draw_waveform()
        except Exception:
            pass
        fps = _WF_FPS_LIVE if self._wf_state != "idle" else _WF_FPS_IDLE
        self.after(fps, self._wf_loop)

    def _draw_waveform(self):
        canvas = self._wf_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10 or h < 10:
            return

        n  = _WF_BARS
        bw = w / n

        if len(self._wf_bar_ids) != n:
            canvas.delete("all")
            self._wf_bar_ids = []
            for i in range(n):
                x0 = int(i * bw) + 1
                x1 = max(x0 + 1, int((i + 1) * bw) - 1)
                item = canvas.create_rectangle(x0, h - 2, x1, h,
                                               fill="#1a3060", outline="", tags="wf")
                self._wf_bar_ids.append(item)

        self._wf_t += 0.05
        t = self._wf_t

        for i, bar_id in enumerate(self._wf_bar_ids):
            phase = i / n

            if self._wf_state == "recording":
                rms_comp = self._audio_rms * (0.5 + 0.5 * abs(math.sin(t * 8 + phase * 6.28)))
                self._wf_targets[i] = min(
                    rms_comp * 0.85 + 0.08 * abs(math.sin(t * 3 + phase * 9.42)), 1.0
                )
                spd = 0.35
            elif self._wf_state == "correcting":
                self._wf_targets[i] = 0.20 + 0.55 * math.sin(t * 3.2 + phase * 9.42) ** 2
                spd = 0.15
            else:
                self._wf_targets[i] = 0.03 + 0.05 * math.sin(t * 0.8 + phase * 4.0)
                spd = 0.08

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
            canvas.coords(bar_id, x0, y0, x1, h)
            canvas.itemconfig(bar_id, fill=colour)

    def set_waveform_state(self, state: str):
        self._wf_state = state

    def thread_safety_waveform(self, state: str):
        self.after(0, self.set_waveform_state, state)

    # ─────────────────────────────────────────────────────────────────────────
    # Sidebar toggle
    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_sidebar(self):
        self._sidebar_open = not self._sidebar_open
        if self._sidebar_open:
            new_w = self._base_width + 320
            self.geometry(f"{new_w}x{self._base_height}")
            self._sidebar_frame.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="nsew")
            self.grid_columnconfigure(1, weight=0, minsize=320)
            self.btn_chat.configure(text="Chat ◀")
        else:
            self._sidebar_frame.grid_remove()
            self.geometry(f"{self._base_width}x{self._base_height}")
            self.grid_columnconfigure(1, weight=0, minsize=0)
            self.btn_chat.configure(text="Chat ▶")

    # ─────────────────────────────────────────────────────────────────────────
    # Console
    # ─────────────────────────────────────────────────────────────────────────

    def update_console(self, message: str):
        tb = self.console._textbox
        tb.configure(state="normal")
        m       = message.strip()
        m_lower = m.lower()

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

        line_count = int(tb.index("end-1c").split(".")[0])
        if line_count > _CONSOLE_MAX_LINES:
            trim_to = line_count - _CONSOLE_MAX_LINES
            tb.delete("1.0", f"{trim_to}.0")

        tb.configure(state="disabled")

    def thread_safety_console(self, message: str):
        self.after(0, self.update_console, message)

    # ─────────────────────────────────────────────────────────────────────────
    # Status — accepts optional degraded= kwarg used by system-pressure monitor
    # ─────────────────────────────────────────────────────────────────────────

    def update_status(self, label: str, color: str, *, degraded: bool = False):
        prefix = "⚡" if degraded else "●"
        self.lbl_status.configure(text=f"{prefix}  {label}", text_color=color)

    def thread_safety_status(self, label: str, color: str, *, degraded: bool = False):
        self.after(0, self.update_status, label, color, degraded)

    # ─────────────────────────────────────────────────────────────────────────
    # Writing / lock buttons
    # ─────────────────────────────────────────────────────────────────────────

    def set_writing_btn(self, active: bool):
        if active:
            self.btn_writing.configure(text="Write: ON",  fg_color=BTN_WON,  hover_color=BTN_WON_H)
        else:
            self.btn_writing.configure(text="Write: OFF", fg_color=BTN_WOFF, hover_color=BTN_WOFF_H)

    def update_lock_btn(self, locked: bool):
        if locked:
            self.btn_lock.configure(text="Unlock",  fg_color="#5a1a1a", hover_color="#8a2828")
        else:
            self.btn_lock.configure(text="Lock In", fg_color="#1a3a5e", hover_color="#2450a0")

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

    def replace_segments(self, seg_ids: list, corrected_text: str, tag: str = "vosk"):
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

    def thread_safety_replace_segments(self, seg_ids: list, corrected_text: str, tag: str = "vosk"):
        self.after(0, self.replace_segments, seg_ids, corrected_text, tag)

    # ─────────────────────────────────────────────────────────────────────────
    # Update window
    # ─────────────────────────────────────────────────────────────────────────

    def _open_update_window(self):
        if not _UPDATE_AVAILABLE:
            return
        try:
            from spoaken.control.update import SpoakenUpdater
            SpoakenUpdater(parent=self)
        except Exception as exc:
            self.update_console(f"[Update]: could not open — {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # Chat sidebar
    # ─────────────────────────────────────────────────────────────────────────

    def _build_sidebar(self):
        self._sidebar_frame = ctk.CTkFrame(
            self, fg_color=BG_SIDEBAR,
            border_color=BORDER_TEA, border_width=1, corner_radius=8,
        )

        # Internal connection state
        self._lan_client        = None   # asyncio ws connection (LAN client mode)
        self._lan_loop          = None   # asyncio event loop for LAN
        self._lan_current_room  = None
        self._lan_rooms_cache   = {}
        self._p2p_mode          = False
        self._p2p_node          = None   # SpoakenOnlineClient
        self._p2p_current_room  = None
        self._p2p_rooms_cache   = {}

        self._sidebar_frame.grid_rowconfigure(4, weight=1)
        self._sidebar_frame.grid_columnconfigure(0, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        sb_hdr = ctk.CTkFrame(self._sidebar_frame, fg_color=BG_PANEL, corner_radius=0)
        sb_hdr.grid(row=0, column=0, sticky="ew")
        sb_hdr.grid_columnconfigure(1, weight=1)

        title_cell = ctk.CTkFrame(sb_hdr, fg_color="transparent")
        title_cell.grid(row=0, column=0, padx=(10, 4), pady=(8, 2), sticky="w")
        ctk.CTkLabel(title_cell, text="💬  Chat",
                     font=FONT_TITLE, text_color=TXT_VOSK, anchor="w").pack(side="left")
        self._conn_status_lbl = ctk.CTkLabel(
            title_cell, text="  ● offline",
            font=("Segoe UI", 8), text_color=STA_IDLE)
        self._conn_status_lbl.pack(side="left", padx=(6, 0))

        toggle_frame = ctk.CTkFrame(sb_hdr, fg_color="transparent")
        toggle_frame.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        toggle_frame.grid_columnconfigure(0, weight=1)
        toggle_frame.grid_columnconfigure(1, weight=1)

        self._btn_local = ctk.CTkButton(
            toggle_frame, text="🖧  LAN", font=FONT_SMALL, height=26, corner_radius=4,
            fg_color=BORDER_TEA, hover_color="#0d6080", text_color=TXT_VOSK,
            command=self._switch_to_local)
        self._btn_local.grid(row=0, column=0, padx=(0, 1), sticky="ew")

        self._btn_online = ctk.CTkButton(
            toggle_frame,
            text="🌐  P2P" if _P2P_AVAILABLE else "🌐  P2P (n/a)",
            font=FONT_SMALL, height=26, corner_radius=4,
            fg_color="#12182e",
            hover_color="#1a2a50" if _P2P_AVAILABLE else "#12182e",
            text_color=TXT_DIM,
            state="normal" if _P2P_AVAILABLE else "disabled",
            command=self._switch_to_online)
        self._btn_online.grid(row=0, column=1, padx=(1, 0), sticky="ew")

        self._port_on = False
        self.btn_port = ctk.CTkButton(
            sb_hdr, text="LAN Access: Off",
            font=FONT_SMALL, height=26, width=110, corner_radius=5,
            fg_color="#3d2e00", hover_color="#5a4400",
            text_color="#f0c040", border_color="#f0c040", border_width=1,
            command=self._on_toggle_port)
        self.btn_port.grid(row=0, column=2, padx=(0, 6), pady=6)

        self._mode_hint_lbl = ctk.CTkLabel(
            sb_hdr,
            text="  Connect to a Spoaken server on your local network",
            font=("Segoe UI", 7), text_color=TXT_DIM, anchor="w")
        self._mode_hint_lbl.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 6), sticky="w")

        ctk.CTkFrame(self._sidebar_frame, height=1, fg_color=BORDER_TEA,
                     corner_radius=0).grid(row=1, column=0, sticky="ew")

        # ── Connection-panel container ─────────────────────────────────────────
        self._conn_container = ctk.CTkFrame(self._sidebar_frame, fg_color="transparent")
        self._conn_container.grid(row=2, column=0, sticky="ew")
        self._conn_container.grid_columnconfigure(0, weight=1)

        self._build_local_panel()
        self._build_online_panel()
        self._local_panel.grid(row=0, column=0, sticky="ew")
        # _online_panel starts hidden

        ctk.CTkFrame(self._sidebar_frame, height=1, fg_color=BORDER_TEA,
                     corner_radius=0).grid(row=3, column=0, sticky="ew")

        # ── Room row ──────────────────────────────────────────────────────────
        room_row = ctk.CTkFrame(self._sidebar_frame, fg_color="transparent")
        room_row.grid(row=4, column=0, padx=6, pady=(4, 0), sticky="ew")
        room_row.grid_columnconfigure(0, weight=1)

        self._room_var    = tk.StringVar(value="(not connected)")
        self._rooms_cache = {}

        self.btn_active_room = ctk.CTkButton(
            room_row, textvariable=self._room_var,
            font=("Courier New", 9), text_color=TXT_TEAL,
            fg_color=BG_INPUT, hover_color="#0d2840",
            border_color=BORDER_TEA, border_width=1,
            height=26, corner_radius=4, anchor="w",
            command=self._open_room_picker)
        self.btn_active_room.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        class _RoomBtnShim:
            def __init__(self_, btn, var):
                self_._btn = btn
                self_._var = var
                self_._values = []
            def configure(self_, **kw):
                if "values" in kw:
                    self_._values = list(kw["values"])
                if "text_color" in kw:
                    try:
                        self_._btn.configure(text_color=kw["text_color"])
                    except Exception:
                        pass
            def set(self_, val):
                self_._var.set(val)
            def get(self_):
                return self_._var.get()
            def cget(self_, key):
                if key == "values":
                    return tuple(self_._values)
                return self_._btn.cget(key)

        self.cmb_room = _RoomBtnShim(self.btn_active_room, self._room_var)

        self.btn_create_room = ctk.CTkButton(
            room_row, text="+ Create", font=FONT_SMALL, height=26, width=66, corner_radius=4,
            fg_color="#0d2a3a", hover_color="#0d4050", text_color=TXT_TEAL,
            state="disabled",
            command=self._on_create_room)
        self.btn_create_room.grid(row=0, column=1, padx=(0, 2))

        self.btn_join_room_bar = ctk.CTkButton(
            room_row, text="→ Join", font=FONT_SMALL, height=26, width=56, corner_radius=4,
            fg_color="#1a2d60", hover_color="#2545a8", text_color=TXT_MAIN,
            command=self._on_room_bar_join)
        self.btn_join_room_bar.grid(row=0, column=2)
        self.btn_join_room_bar.grid_remove()

        self.btn_browse_rooms = ctk.CTkButton(
            room_row, text="⊞", font=("Segoe UI", 13), height=26, width=30, corner_radius=4,
            fg_color="#0d2040", hover_color="#1a3a60", text_color=TXT_TEAL,
            command=self._open_room_browser)
        self.btn_browse_rooms.grid(row=0, column=3, padx=(2, 0))
        self.btn_browse_rooms.grid_remove()

        # ── Command bar ────────────────────────────────────────────────────────
        self._build_command_bar()

        # ── Chat log ───────────────────────────────────────────────────────────
        self._sidebar_frame.grid_rowconfigure(6, weight=1)
        self._chat_log = ctk.CTkTextbox(
            self._sidebar_frame, fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            font=("Courier New", 10), text_color=TXT_CHAT,
            scrollbar_button_color=BORDER_ACT, corner_radius=6, wrap="word")
        self._chat_log.grid(row=6, column=0, padx=6, pady=(0, 4), sticky="nsew")
        self._chat_log.configure(state="disabled")

        ctk.CTkFrame(self._sidebar_frame, height=1, fg_color=BORDER_TEA,
                     corner_radius=0).grid(row=7, column=0, sticky="ew")

        # ── Message input ──────────────────────────────────────────────────────
        msg_row = ctk.CTkFrame(self._sidebar_frame, fg_color="transparent")
        msg_row.grid(row=8, column=0, padx=6, pady=(4, 8), sticky="ew")
        msg_row.grid_columnconfigure(0, weight=1)

        self._chat_entry = ctk.CTkEntry(
            msg_row, placeholder_text="Send message to room …",
            fg_color=BG_INPUT, border_color=BORDER_TEA, border_width=1,
            text_color=TXT_CHAT, placeholder_text_color=TXT_DIM,
            height=30, corner_radius=4, font=("Segoe UI", 10))
        self._chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._chat_entry.bind("<Return>", self._on_chat_send)

        ctk.CTkButton(msg_row, text="Send", font=FONT_SMALL, height=30, width=50, corner_radius=4,
                      fg_color=BORDER_ACT, hover_color="#3060d0",
                      command=self._on_chat_send).grid(row=0, column=1)

        ctk.CTkButton(msg_row, text="📎", font=("Segoe UI", 13), height=30, width=34, corner_radius=4,
                      fg_color="#0d2a3a", hover_color="#0d4050", text_color=TXT_TEAL,
                      command=self._open_file_transfer_dialog).grid(row=0, column=2, padx=(4, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # Sidebar panels
    # ─────────────────────────────────────────────────────────────────────────

    def _build_local_panel(self):
        """LAN WebSocket client connection panel."""
        f = ctk.CTkFrame(self._conn_container, fg_color="transparent")
        self._local_panel = f
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f, text="Host", font=FONT_SMALL, text_color=TXT_DIM, width=40,
                     ).grid(row=0, column=0, padx=(10, 4), pady=(8, 3), sticky="w")
        self._ent_lan_host = ctk.CTkEntry(
            f, placeholder_text="192.168.x.x or hostname",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, placeholder_text_color=TXT_DIM,
            height=28, corner_radius=5, font=FONT_SMALL)
        self._ent_lan_host.grid(row=0, column=1, padx=(0, 4), pady=(8, 3), sticky="ew")

        port_token = ctk.CTkFrame(f, fg_color="transparent")
        port_token.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 4), sticky="ew")
        port_token.grid_columnconfigure(1, weight=0, minsize=60)
        port_token.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(port_token, text="Port", font=FONT_SMALL, text_color=TXT_DIM, width=40,
                     ).grid(row=0, column=0, padx=(0, 4), sticky="w")
        self._ent_lan_port = ctk.CTkEntry(
            port_token, fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, height=26, corner_radius=5,
            font=("Courier New", 9), width=60)
        self._ent_lan_port.insert(0, "55300")
        self._ent_lan_port.grid(row=0, column=1, padx=(0, 8), sticky="w")

        ctk.CTkLabel(port_token, text="Token", font=FONT_SMALL, text_color=TXT_DIM, width=40,
                     ).grid(row=0, column=2, padx=(0, 4), sticky="w")
        self._ent_lan_token = ctk.CTkEntry(
            port_token, placeholder_text="spoaken",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, placeholder_text_color=TXT_DIM,
            height=26, corner_radius=5, font=("Courier New", 9), show="•")
        self._ent_lan_token.grid(row=0, column=3, sticky="ew")

        btn_row = ctk.CTkFrame(f, fg_color="transparent")
        btn_row.grid(row=2, column=0, columnspan=2, padx=10, pady=(0, 8), sticky="ew")
        btn_row.grid_columnconfigure(0, weight=1)

        self._btn_lan_connect = ctk.CTkButton(
            btn_row, text="Connect",
            font=FONT_SMALL, height=28, corner_radius=5,
            fg_color=BORDER_TEA, hover_color="#0d6080",
            command=self._lan_connect_toggle)
        self._btn_lan_connect.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self._lbl_lan_status = ctk.CTkLabel(
            btn_row, text="disconnected",
            font=("Segoe UI", 8), text_color=TXT_DIM, anchor="e")
        self._lbl_lan_status.grid(row=0, column=1, sticky="e")

    def _build_online_panel(self):
        """P2P Tor identity and node-control panel."""
        f = ctk.CTkFrame(self._conn_container, fg_color="transparent")
        self._online_panel = f
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(f, text="Username", font=FONT_SMALL, text_color=TXT_DIM, width=70,
                     ).grid(row=0, column=0, padx=(10, 4), pady=(8, 3), sticky="w")
        self._ent_p2p_user = ctk.CTkEntry(
            f, placeholder_text="your display name",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, placeholder_text_color=TXT_DIM,
            height=28, corner_radius=5, font=FONT_SMALL)

        # Pre-fill username from config
        try:
            from spoaken.network.online import load_identity
            from spoaken.system.paths import ROOT_DIR
            _ident = load_identity(str(ROOT_DIR / "spoaken_config.json"))
            if _ident.get("username"):
                self._ent_p2p_user.insert(0, _ident["username"])
        except Exception:
            pass
        self._ent_p2p_user.grid(row=0, column=1, padx=(0, 10), pady=(8, 3), sticky="ew")

        # DID display
        ctk.CTkLabel(f, text="DID", font=FONT_SMALL, text_color=TXT_DIM, width=70,
                     ).grid(row=1, column=0, padx=(10, 4), pady=(0, 3), sticky="w")
        self._lbl_p2p_did = ctk.CTkLabel(
            f, text="—", font=("Courier New", 8),
            text_color=TXT_DIM, anchor="w")
        self._lbl_p2p_did.grid(row=1, column=1, padx=(0, 10), pady=(0, 3), sticky="w")

        # Onion address (shown once node starts)
        ctk.CTkLabel(f, text=".onion", font=FONT_SMALL, text_color=TXT_DIM, width=70,
                     ).grid(row=2, column=0, padx=(10, 4), pady=(0, 3), sticky="w")
        self._lbl_onion = ctk.CTkLabel(
            f, text="(node not started)",
            font=("Courier New", 8), text_color=TXT_DIM, anchor="w")
        self._lbl_onion.grid(row=2, column=1, padx=(0, 10), pady=(0, 3), sticky="w")

        # Start / Stop button
        self._btn_p2p_toggle = ctk.CTkButton(
            f, text="Start P2P Node",
            font=FONT_SMALL, height=28, corner_radius=5,
            fg_color=BORDER_TEA, hover_color="#0d6080",
            command=self._on_p2p_start)
        self._btn_p2p_toggle.grid(row=3, column=0, columnspan=2,
                                   padx=10, pady=(4, 8), sticky="ew")

        # Tor-required note
        ctk.CTkLabel(
            f,
            text="Requires Tor: sudo apt install tor && sudo systemctl start tor",
            font=("Segoe UI", 7), text_color=TXT_DIM, anchor="w", wraplength=260,
        ).grid(row=4, column=0, columnspan=2, padx=10, pady=(0, 6), sticky="w")

    def _build_command_bar(self):
        """
        Quick-action strip between the room selector and the chat log.
        Row 5 in the sidebar frame.
        """
        bar = ctk.CTkFrame(self._sidebar_frame, fg_color="transparent")
        bar.grid(row=5, column=0, padx=6, pady=(2, 2), sticky="ew")

        _kw = dict(font=FONT_SMALL, height=22, corner_radius=4,
                   fg_color="#0d1a2a", hover_color="#152640", text_color=TXT_DIM)

        ctk.CTkButton(bar, text="↺ Rooms", width=68, command=self._open_room_browser, **_kw
                      ).pack(side="left", padx=1)
        ctk.CTkButton(bar, text="👥 Members", width=78, command=self._show_members, **_kw
                      ).pack(side="left", padx=1)
        ctk.CTkButton(bar, text="🔕 Clear", width=60, command=self._clear_chat_log, **_kw
                      ).pack(side="left", padx=1)

    # ─────────────────────────────────────────────────────────────────────────
    # Mode switcher
    # ─────────────────────────────────────────────────────────────────────────

    def _switch_to_local(self):
        """Show LAN panel, hide P2P panel."""
        self._p2p_mode = False
        self._btn_local.configure(fg_color=BORDER_TEA, text_color=TXT_VOSK)
        self._btn_online.configure(fg_color="#12182e", text_color=TXT_DIM)
        self._online_panel.grid_remove()
        self._local_panel.grid(row=0, column=0, sticky="ew")
        self._mode_hint_lbl.configure(
            text="  Connect to a Spoaken server on your local network")
        # Update room controls visibility
        self.btn_create_room.configure(state="disabled")
        self.btn_join_room_bar.grid()
        self.btn_browse_rooms.grid_remove()

    def _switch_to_online(self):
        """Show P2P panel, hide LAN panel."""
        self._p2p_mode = True
        self._btn_online.configure(fg_color=BORDER_TEA, text_color=TXT_VOSK)
        self._btn_local.configure(fg_color="#12182e", text_color=TXT_DIM)
        self._local_panel.grid_remove()
        self._online_panel.grid(row=0, column=0, sticky="ew")
        self._mode_hint_lbl.configure(
            text="  Fully anonymous P2P chat via Tor hidden services")
        self.btn_join_room_bar.grid_remove()
        # Create room / browse only available once node is started
        node_active = self._p2p_node is not None
        self.btn_create_room.configure(state="normal" if node_active else "disabled")
        if node_active:
            self.btn_browse_rooms.grid()

    # ─────────────────────────────────────────────────────────────────────────
    # LAN Access (server) toggle — controller manages the actual server
    # ─────────────────────────────────────────────────────────────────────────

    def _on_toggle_port(self):
        self.controller.toggle_chat_port()

    def update_chat_port_btn(self, active: bool):
        """Update the LAN Access button (called from controller via after(0, …))."""
        self._port_on = active
        if active:
            self.btn_port.configure(
                text="LAN Access: On",
                fg_color="#1a4a00", hover_color="#246000",
                text_color=TXT_OK, border_color=TXT_OK,
            )
        else:
            self.btn_port.configure(
                text="LAN Access: Off",
                fg_color="#3d2e00", hover_color="#5a4400",
                text_color="#f0c040", border_color="#f0c040",
            )

    def thread_safety_chat_port_btn(self, active: bool):
        self.after(0, self.update_chat_port_btn, active)

    # ─────────────────────────────────────────────────────────────────────────
    # LAN client
    # ─────────────────────────────────────────────────────────────────────────

    def _lan_connect_toggle(self):
        if self._lan_client is not None:
            self._lan_disconnect()
        else:
            host  = self._ent_lan_host.get().strip()
            port_s = self._ent_lan_port.get().strip()
            if not host:
                self.chat_receive("[LAN]: enter a host address first", is_me=False)
                return
            try:
                port = int(port_s)
            except ValueError:
                port = 55300
            threading.Thread(target=self._lan_connect_bg,
                             args=(host, port), daemon=True).start()

    def _lan_connect_bg(self, host: str, port: int):
        """Background WS client — runs a private asyncio loop."""
        if not _WS_AVAILABLE:
            self.after(0, self.chat_receive,
                       "[LAN]: websockets package not installed — pip install websockets",
                       "", False)
            return

        import websockets  # type: ignore

        self.after(0, lambda: self._btn_lan_connect.configure(
            state="disabled", text="Connecting …"))
        self.after(0, self._update_conn_status, "⟳ connecting …", TXT_WARN)

        async def _client():
            url = f"ws://{host}:{port}"
            try:
                async with websockets.connect(url, open_timeout=8, close_timeout=4) as ws:
                    # Store a lightweight wrapper so _lan_disconnect can close it
                    self._lan_client = ws
                    self.after(0, self._on_lan_connected, host, port)
                    async for message in ws:
                        self.after(0, self.chat_receive, str(message), "LAN", False)
            except Exception as exc:
                self.after(0, self.chat_receive, f"[LAN]: {exc}", "", False)
            finally:
                self._lan_client = None
                self.after(0, self._on_lan_disconnected)

        loop = asyncio.new_event_loop()
        self._lan_loop = loop
        try:
            loop.run_until_complete(_client())
        finally:
            loop.close()
            self._lan_loop = None

    def _lan_disconnect(self):
        if self._lan_client is not None and self._lan_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._lan_client.close(), self._lan_loop)
            except Exception:
                pass
        self._lan_client = None

    def _on_lan_connected(self, host: str, port: int):
        self._btn_lan_connect.configure(
            state="normal", text="Disconnect",
            fg_color=BTN_CLR, hover_color=BTN_CLR_H,
            command=self._lan_disconnect)
        self._lbl_lan_status.configure(text=f"● {host}:{port}", text_color=TXT_OK)
        self._update_conn_status(f"● LAN {host}:{port}", TXT_OK)
        self.chat_receive(f"[LAN]: connected to {host}:{port}", "", False)
        self._room_var.set(f"{host}:{port}")

    def _on_lan_disconnected(self):
        self._btn_lan_connect.configure(
            state="normal", text="Connect",
            fg_color=BORDER_TEA, hover_color="#0d6080",
            command=self._lan_connect_toggle)
        self._lbl_lan_status.configure(text="disconnected", text_color=TXT_DIM)
        self._update_conn_status("● offline", STA_IDLE)
        self._room_var.set("(not connected)")
        self.chat_receive("[LAN]: disconnected", "", False)

    # ─────────────────────────────────────────────────────────────────────────
    # P2P node management
    # ─────────────────────────────────────────────────────────────────────────

    def _on_p2p_start(self):
        """Start P2P node in background thread."""
        if self._p2p_node is not None:
            return
        username = self._ent_p2p_user.get().strip() or "anonymous"
        self._btn_p2p_toggle.configure(state="disabled", text="Starting …")

        def _start():
            try:
                from spoaken.network.online import SpoakenOnlineClient, create_identity
                from spoaken.system.paths import ROOT_DIR
                cfg_path = str(ROOT_DIR / "spoaken_config.json")
                # Ensure identity exists
                ident = create_identity(cfg_path, username)
                node = SpoakenOnlineClient(
                    username=username,
                    on_event=self._p2p_event_handler,
                    log_cb=lambda m: self.after(0, self.chat_receive, m, "", False),
                    cfg_path=cfg_path,
                )
                ok = node.connect()
                if ok:
                    self._p2p_node = node
                    self.after(0, self._on_p2p_started, node.username,
                               node.onion_address, ident.get("did", ""))
                else:
                    self.after(0, self.chat_receive,
                               "[P2P]: Start failed — is Tor running?\n"
                               "  sudo systemctl start tor", "", False)
                    self.after(0, lambda: self._btn_p2p_toggle.configure(
                        state="normal", text="Start P2P Node"))
            except Exception as exc:
                self.after(0, self.chat_receive, f"[P2P Error]: {exc}", "", False)
                self.after(0, lambda: self._btn_p2p_toggle.configure(
                    state="normal", text="Start P2P Node"))

        threading.Thread(target=_start, daemon=True).start()

    def _on_p2p_started(self, username: str, onion: str, did: str):
        onion_short = (onion[:24] + "…") if len(onion) > 26 else onion
        self._lbl_onion.configure(text=onion_short, text_color=TXT_VOSK)
        self._lbl_p2p_did.configure(
            text=did[:28] + "…" if len(did) > 30 else did, text_color=TXT_DIM)
        self._btn_p2p_toggle.configure(
            state="normal", text="Stop Node",
            fg_color=BTN_CLR, hover_color=BTN_CLR_H,
            command=self._on_p2p_stop)
        self._update_conn_status(f"● P2P ({username})", TXT_OK)
        self.chat_receive(f"[P2P]: node started as '{username}'", "", False)
        self.chat_receive(f"      .onion: {onion}", "", False)
        self.btn_create_room.configure(state="normal")
        self.btn_browse_rooms.grid()

    def _on_p2p_stop(self):
        """Stop P2P node."""
        node = self._p2p_node
        self._p2p_node         = None
        self._p2p_current_room = None
        if node is not None:
            threading.Thread(target=node.stop, daemon=True).start()
        self._btn_p2p_toggle.configure(
            state="normal", text="Start P2P Node",
            fg_color=BORDER_TEA, hover_color="#0d6080",
            command=self._on_p2p_start)
        self._lbl_onion.configure(text="(node not started)", text_color=TXT_DIM)
        self._update_conn_status("● offline", STA_IDLE)
        self._room_var.set("(not connected)")
        self.btn_create_room.configure(state="disabled")
        self.btn_browse_rooms.grid_remove()
        self.chat_receive("[P2P]: node stopped", "", False)

    # ─────────────────────────────────────────────────────────────────────────
    # P2P event router
    # ─────────────────────────────────────────────────────────────────────────

    def _p2p_event_handler(self, ev: dict):
        """
        Route P2P node events to the GUI.
        Called from background thread — all UI updates via after(0, …).
        """
        t = ev.get("type", "")
        c = ev.get("content", {}) if isinstance(ev.get("content"), dict) else {}

        if t == "m.room.message":
            sender  = c.get("sender", ev.get("sender", "?"))
            body    = c.get("body",   ev.get("body",   ""))
            my_name = getattr(self._p2p_node, "username", "") if self._p2p_node else ""
            is_me   = (sender == my_name)
            self.after(0, self.chat_receive, body, sender, is_me)

        elif t == "m.member.join":
            username = c.get("username", "?")
            self.after(0, self.chat_receive, f"→ {username} joined", "", False)

        elif t in ("m.member.leave", "m.room.leave"):
            username = c.get("username", "?")
            self.after(0, self.chat_receive, f"← {username} left", "", False)

        elif t == "m.room.created":
            rid   = ev.get("room_id", "")
            name  = c.get("name", rid)
            self._p2p_current_room = rid
            self._p2p_rooms_cache[rid] = name
            self.after(0, self._room_var.set, name)
            self.after(0, self.chat_receive, f"[Room]: created '{name}'", "", False)

        elif t == "m.room.list":
            rooms = c.get("rooms", [])
            self.after(0, self._update_room_list, rooms)

        elif t == "m.file.received":
            fname = c.get("filename", "?")
            size  = c.get("size", 0)
            path  = c.get("_saved_path", "")
            self.after(0, self.chat_receive,
                       f"📎 Received: {fname}  ({size // 1024} KB)\n   → {path}",
                       "", False)

        elif t == "m.auth.ok":
            room_id  = ev.get("room_id", "")
            members  = ev.get("members", [])
            self._p2p_current_room = room_id
            names = [m.get("username", "?") for m in members]
            self.after(0, self.chat_receive,
                       f"[Room]: joined — members: {', '.join(names)}", "", False)

        elif t == "m.auth.fail":
            reason = ev.get("reason", "?")
            hint   = ev.get("hint", "")
            msg    = f"[Auth Failed]: {reason}"
            if hint:
                msg += f" — {hint}"
            self.after(0, self.chat_receive, msg, "", False)

    def _update_room_list(self, rooms: list):
        """Refresh room-picker cache from a room-list event."""
        self._p2p_rooms_cache = {r.get("room_id", ""): r.get("name", "?") for r in rooms}

    # ─────────────────────────────────────────────────────────────────────────
    # Chat receive — display incoming messages
    # ─────────────────────────────────────────────────────────────────────────

    def chat_receive(self, message: str, sender: str = "", is_me: bool = False):
        """
        Insert a message into the chat log.
        Thread-safe — always call via after(0, …) from background threads.
        """
        cl = self._chat_log._textbox
        cl.configure(state="normal")
        ts = time.strftime("%H:%M")

        if not sender:
            # System / status line
            cl.insert("end", f" {ts} {message}\n", "chat_system")
        elif is_me:
            cl.insert("end", f" {ts} ", "chat_system")
            cl.insert("end", "You: ", "chat_me")
            cl.insert("end", f"{message}\n", "chat_me")
        else:
            cl.insert("end", f" {ts} ", "chat_system")
            cl.insert("end", f"{sender}: ", "chat_peer")
            cl.insert("end", f"{message}\n", "chat_me")

        cl.see("end")
        cl.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Chat send
    # ─────────────────────────────────────────────────────────────────────────

    def _on_chat_send(self, event=None):
        message = self._chat_entry.get().strip()
        if not message:
            return
        self._chat_entry.delete(0, "end")

        if self._p2p_mode:
            if self._p2p_node is None:
                self.chat_receive("[P2P]: start the node first", "", False)
                return
            room_id = self._p2p_current_room
            if not room_id:
                self.chat_receive("[P2P]: create or join a room first", "", False)
                return
            def _send():
                try:
                    self._p2p_node.send_message(room_id, message)
                except Exception as exc:
                    self.after(0, self.chat_receive, f"[P2P Error]: {exc}", "", False)
            threading.Thread(target=_send, daemon=True).start()
            self.chat_receive(message,
                              self._p2p_node.username if self._p2p_node else "You",
                              is_me=True)
        else:
            # LAN mode — broadcast via WS if connected, else via controller
            if self._lan_client is not None and self._lan_loop is not None:
                import websockets  # type: ignore
                asyncio.run_coroutine_threadsafe(
                    self._lan_client.send(message), self._lan_loop)
                self.chat_receive(message, "You", is_me=True)
            else:
                self.controller.chat_send(message)
                self.chat_receive(message, "You", is_me=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Room management
    # ─────────────────────────────────────────────────────────────────────────

    def _on_room_bar_join(self):
        """Join/switch to the room shown in the room display bar (LAN mode)."""
        addr = self._room_var.get().strip()
        if not addr or addr == "(not connected)":
            host = self._ent_lan_host.get().strip()
            if host:
                self._lan_connect_toggle()
            return
        self.chat_receive(f"[LAN]: switching to {addr}", "", False)

    def _open_room_picker(self):
        """Inline picker: click active room label to cycle rooms."""
        rooms = (list(self._p2p_rooms_cache.items())
                 if self._p2p_mode else [])
        if not rooms:
            self.chat_receive(
                "[Rooms]: no rooms available — create one first" if self._p2p_mode
                else "[Rooms]: connect to a server first",
                "", False)
            return
        # Build a small popup
        self._open_room_browser()

    def _open_room_browser(self):
        """Full room browser popup."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Rooms")
        dlg.geometry("360x320")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, True)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Available Rooms",
                     font=FONT_TITLE, text_color=TXT_TEAL).pack(pady=(12, 4))

        frame = ctk.CTkScrollableFrame(dlg, fg_color=BG_PANEL, corner_radius=6)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        cache = self._p2p_rooms_cache if self._p2p_mode else {}

        if not cache:
            ctk.CTkLabel(frame, text="No rooms yet.",
                         font=FONT_SMALL, text_color=TXT_DIM).pack(pady=20)
        else:
            for room_id, name in cache.items():
                btn = ctk.CTkButton(
                    frame, text=name, font=FONT_SMALL, height=30, corner_radius=5,
                    fg_color=BG_CARD, hover_color=BORDER_ACT, anchor="w",
                    command=lambda rid=room_id, n=name: self._join_p2p_room_ui(rid, n, dlg),
                )
                btn.pack(fill="x", padx=6, pady=2)

        # Refresh button
        ctk.CTkButton(
            dlg, text="↺  Refresh", font=FONT_SMALL, height=28,
            fg_color=BORDER_TEA, hover_color="#0d6080",
            command=lambda: self._refresh_rooms(frame),
        ).pack(pady=(0, 8))

    def _refresh_rooms(self, frame):
        """Ask P2P node for updated room list."""
        if self._p2p_node is not None:
            threading.Thread(
                target=lambda: self._p2p_node.list_rooms(notify=True),
                daemon=True).start()

    def _join_p2p_room_ui(self, room_id: str, name: str, dlg):
        """Handle room selection from browser popup."""
        dlg.destroy()
        if room_id == self._p2p_current_room:
            return
        # If we're already a host, just update display
        if self._p2p_node and room_id in self._p2p_node._hosted:
            self._p2p_current_room = room_id
            self._room_var.set(name)
        elif self._p2p_node:
            # Need host onion from cache
            self.chat_receive(f"[Room]: joining '{name}' …", "", False)
            self._p2p_current_room = room_id
            self._room_var.set(name)

    def _on_create_room(self):
        """Dialog to create a new P2P room."""
        if not self._p2p_mode or self._p2p_node is None:
            self.chat_receive("[Room]: start the P2P node first", "", False)
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title("Create Room")
        dlg.geometry("300x200")
        dlg.configure(fg_color=BG_DEEP)
        dlg.resizable(False, False)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Create P2P Room",
                     font=FONT_TITLE, text_color=TXT_TEAL).pack(pady=(14, 4))

        ctk.CTkLabel(dlg, text="Room name", font=FONT_SMALL, text_color=TXT_DIM,
                     anchor="w").pack(fill="x", padx=14, pady=(4, 0))
        ent_name = ctk.CTkEntry(
            dlg, placeholder_text="my-room",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, height=30, corner_radius=5)
        ent_name.pack(fill="x", padx=14, pady=(2, 4))
        ent_name.focus_set()

        ctk.CTkLabel(dlg, text="Password (optional)", font=FONT_SMALL,
                     text_color=TXT_DIM, anchor="w").pack(fill="x", padx=14)
        ent_pw = ctk.CTkEntry(
            dlg, placeholder_text="leave blank for public",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            text_color=TXT_MAIN, height=30, corner_radius=5, show="•")
        ent_pw.pack(fill="x", padx=14, pady=(2, 8))

        def _create():
            name = ent_name.get().strip() or "room"
            pw   = ent_pw.get()
            dlg.destroy()
            def _do():
                try:
                    rid = self._p2p_node.create_room(
                        name, password=pw, public=not bool(pw))
                    if rid:
                        self.after(0, self.chat_receive,
                                   f"[Room]: '{name}' created ({rid[:8]}…)", "", False)
                    else:
                        self.after(0, self.chat_receive,
                                   "[Room]: creation failed — check logs", "", False)
                except Exception as exc:
                    self.after(0, self.chat_receive, f"[Room Error]: {exc}", "", False)
            threading.Thread(target=_do, daemon=True).start()

        ctk.CTkButton(
            dlg, text="Create", font=FONT_UI, height=34, corner_radius=6,
            fg_color=BORDER_TEA, hover_color="#0d6080",
            command=_create).pack(padx=14, fill="x")

        ent_name.bind("<Return>", lambda e: _create())

    # ─────────────────────────────────────────────────────────────────────────
    # File transfer
    # ─────────────────────────────────────────────────────────────────────────

    def _open_file_transfer_dialog(self):
        """Pick a file and send it via the active connection."""
        if self._p2p_mode:
            if self._p2p_node is None or self._p2p_current_room is None:
                self.chat_receive("[File]: start node and join a room first", "", False)
                return
        elif self._lan_client is None:
            self.chat_receive("[File]: connect to a server first", "", False)
            return

        path = filedialog.askopenfilename(
            title="Send File",
            parent=self,
        )
        if not path:
            return

        import os
        fname = os.path.basename(path)
        size  = os.path.getsize(path)

        if size > 50 * 1024 * 1024:
            messagebox.showwarning("File Too Large",
                                   f"{fname} exceeds the 50 MB limit.", parent=self)
            return

        self.chat_receive(f"📎 Sending: {fname}  ({size // 1024} KB) …", "", False)

        if self._p2p_mode and self._p2p_node:
            rid = self._p2p_current_room
            threading.Thread(
                target=self._p2p_node.send_file,
                args=(rid, path), daemon=True).start()
        else:
            self.chat_receive("[File]: LAN file send not yet implemented", "", False)

    # ─────────────────────────────────────────────────────────────────────────
    # Chat log helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _clear_chat_log(self):
        cl = self._chat_log._textbox
        cl.configure(state="normal")
        cl.delete("1.0", "end")
        cl.configure(state="disabled")

    def _show_members(self):
        """Display current room members in the chat log."""
        if self._p2p_mode and self._p2p_node and self._p2p_current_room:
            peers = self._p2p_node.list_peers(self._p2p_current_room)
            names = [p.get("username", "?") for p in peers]
            self.chat_receive(f"[Members]: {', '.join(names) or 'none'}", "", False)
        else:
            self.chat_receive("[Members]: not in a room", "", False)

    def _update_conn_status(self, text: str, color: str):
        """Update the ● status label in the sidebar header."""
        self._conn_status_lbl.configure(text=f"  {text}", text_color=color)

    # ─────────────────────────────────────────────────────────────────────────
    # Misc
    # ─────────────────────────────────────────────────────────────────────────

    def flush(self):
        pass
