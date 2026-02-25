"""
spoaken_update.py
─────────────────
Spoaken Update & Repair window.

Can run in two modes:
  1. Embedded  — opened as a CTkToplevel from the running application.
               SpoakenUpdater(parent_window)

  2. Standalone — run from the command line:
               python spoaken_update.py

Features
────────
  • Full dependency manifest with current-version / latest-version columns
  • Per-package status icons  ✔ up-to-date  ↑ upgrade available  ✗ missing
  • Big neon-teal "Update" button (black text) — upgrades all out-of-date
    or missing packages in a background thread with live log output
  • "Repair" quick action — reinstalls every package regardless of version
  • "Check" — refreshes the version table without installing anything
  • Optional Vosk & Whisper model re-download (reads spoaken_config.json)
  • Platform-aware pip invocation (mirrors install.py's pip_run logic)
  • System-package reminder for Linux / macOS prerequisites
"""

import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import customtkinter as ctk

# ── Colour palette (matches Spoaken theme) ────────────────────────────────────
BG_DEEP    = "#060c1a"
BG_PANEL   = "#0a1128"
BG_CARD    = "#0d1735"
BG_INPUT   = "#0c1636"
BORDER_SUB = "#1a2d60"
BORDER_ACT = "#2545a8"

TXT_MAIN   = "#00bdff"
TXT_DIM    = "#2a6080"
TXT_TEAL   = "#00e5cc"
TXT_WARN   = "#d4aa00"
TXT_OK     = "#24c45e"
TXT_ERR    = "#e03535"
TXT_CONS   = "#007bff"

# Neon-teal update button
BTN_UPDATE      = "#00e5cc"
BTN_UPDATE_H    = "#00c8b0"
BTN_UPDATE_TXT  = "#000000"

FONT_MONO  = ("Courier New", 10)
FONT_UI    = ("Segoe UI",    11)
FONT_SMALL = ("Segoe UI",     9)
FONT_TITLE = ("Segoe UI Semibold", 13)

_OS = platform.system()   # Windows | Darwin | Linux


# ── Package manifest ──────────────────────────────────────────────────────────
# Each entry: (pip_install_name, importlib_name, required, constraint)
#   pip_install_name   — name passed to pip install
#   importlib_name     — name used in importlib.metadata / import
#   required           — True = mandatory for Spoaken to run
#   constraint         — version constraint string or "" for none

_PACKAGES = [
    # ── Core UI ───────────────────────────────────────────────────────────────
    ("customtkinter",          "customtkinter",     True,  ""),
    ("Pillow",                 "Pillow",             True,  ""),

    # ── Audio ─────────────────────────────────────────────────────────────────
    ("sounddevice",            "sounddevice",        True,  ""),
    ("numpy",                  "numpy",              True,  ""),

    # ── Transcription ─────────────────────────────────────────────────────────
    ("faster-whisper",         "faster_whisper",     True,  ""),
    ("vosk",                   "vosk",               False, ""),

    # ── Grammar ───────────────────────────────────────────────────────────────
    ("happytransformer<4.0.0", "happytransformer",   False, "<4.0.0"),
    ("transformers",           "transformers",       False, ""),
    ("torch",                  "torch",              False, ""),

    # ── Text automation ───────────────────────────────────────────────────────
    ("pyautogui",              "pyautogui",          True,  ""),
    ("rapidfuzz",              "rapidfuzz",          True,  ""),

    # ── Optional quality ──────────────────────────────────────────────────────
    ("noisereduce",            "noisereduce",        False, ""),
    ("deep-translator",        "deep_translator",    False, ""),

    # ── LLM / Ollama ──────────────────────────────────────────────────────────
    ("ollama",                 "ollama",             False, ""),

    # ── Summarization ─────────────────────────────────────────────────────────
    ("sumy",                   "sumy",               False, ""),
    ("nltk",                   "nltk",               False, ""),
    ("scikit-learn",           "sklearn",            False, ""),
]

# ── Model catalogues ──────────────────────────────────────────────────────────

_VOSK_MODELS = [
    # (display_name, download_url, approx_size_mb, description)
    ("vosk-model-small-en-us-0.15",      "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",      40,   "English — small (fast, low RAM)"),
    ("vosk-model-en-us-0.22",            "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip",            1800, "English — medium (balanced)"),
    ("vosk-model-en-us-0.42-gigaspeech", "https://alphacephei.com/vosk/models/vosk-model-en-us-0.42-gigaspeech.zip",2300, "English — GigaSpeech (high accuracy)"),
    ("vosk-model-small-fr-0.22",         "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip",         41,   "French — small"),
    ("vosk-model-small-de-0.15",         "https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip",         45,   "German — small"),
    ("vosk-model-small-es-0.42",         "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip",         39,   "Spanish — small"),
    ("vosk-model-small-pt-0.3",          "https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip",          31,   "Portuguese — small"),
    ("vosk-model-small-it-0.22",         "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip",         48,   "Italian — small"),
    ("vosk-model-small-ru-0.22",         "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",         45,   "Russian — small"),
    ("vosk-model-small-zh-0.22",         "https://alphacephei.com/vosk/models/vosk-model-small-zh-0.22.zip",         42,   "Chinese — small"),
    ("vosk-model-small-ja-0.22",         "https://alphacephei.com/vosk/models/vosk-model-small-ja-0.22.zip",         48,   "Japanese — small"),
    ("vosk-model-small-ko-0.22",         "https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip",         49,   "Korean — small"),
]

_WHISPER_MODELS = [
    # (model_name, approx_size_mb, description)
    ("tiny.en",    75,    "English — tiny (fastest, ~39M params)"),
    ("tiny",       75,    "Multilingual — tiny"),
    ("base.en",    145,   "English — base (recommended for most users)"),
    ("base",       145,   "Multilingual — base"),
    ("small.en",   488,   "English — small (better accuracy)"),
    ("small",      488,   "Multilingual — small"),
    ("medium.en",  1500,  "English — medium (high accuracy)"),
    ("medium",     1500,  "Multilingual — medium"),
    ("large-v2",   2900,  "Multilingual — large v2 (best accuracy)"),
    ("large-v3",   2900,  "Multilingual — large v3 (latest)"),
    ("turbo",      800,   "Multilingual — turbo (fast + accurate)"),
]

# Platform-specific extras
_PLATFORM_EXTRAS = {
    "Windows": [
        ("pywin32",    "win32api",   False, ""),
        ("pywinauto",  "pywinauto",  False, ""),
    ],
    "Darwin":  [],
    "Linux":   [],
}


# ── pip helpers ───────────────────────────────────────────────────────────────

def _pip_exe() -> list[str]:
    """Return [python, -m, pip] using the current interpreter."""
    cmd = [sys.executable, "-m", "pip"]
    if _OS == "Linux" and shutil.which("apt"):
        # Debian/Ubuntu: newer pip requires --break-system-packages
        cmd += []   # appended per-call below
    return cmd


def _pip_install(pkg: str, log_fn=print, upgrade: bool = True) -> bool:
    """
    Install or upgrade a single pip package.
    Returns True on success.
    """
    cmd = _pip_exe() + ["install", "--quiet"]
    if upgrade:
        cmd.append("--upgrade")
    if _OS == "Linux" and shutil.which("apt"):
        cmd.append("--break-system-packages")
    cmd.append(pkg)

    log_fn(f"  $ {' '.join(cmd[-3:])}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            log_fn(f"  ✔  {pkg}")
            return True
        else:
            err = (proc.stderr or proc.stdout).strip().splitlines()[-1]
            log_fn(f"  ✗  {pkg}  →  {err}")
            return False
    except Exception as exc:
        log_fn(f"  ✗  {pkg}  →  {exc}")
        return False


def _get_installed_version(importlib_name: str) -> str | None:
    """Return installed package version string, or None if not found."""
    # Try importlib.metadata first (most reliable)
    try:
        return importlib.metadata.version(importlib_name)
    except importlib.metadata.PackageNotFoundError:
        pass
    # Some packages use different dist names — fall back to importlib.util
    if importlib.util.find_spec(importlib_name.split(".")[0]) is not None:
        return "installed (version unknown)"
    return None


def _get_latest_version(pip_name: str) -> str | None:
    """
    Query PyPI for the latest version of pip_name.
    Returns the version string, or None if the query fails.
    Strips any version constraint (e.g. 'happytransformer<4.0.0' → 'happytransformer').
    """
    bare = re.split(r"[<>=!]", pip_name)[0].strip()
    try:
        import urllib.request, json as _json
        url = f"https://pypi.org/pypi/{bare}/json"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = _json.loads(r.read())
        return data["info"]["version"]
    except Exception:
        return None


def _version_lt(installed: str, latest: str) -> bool:
    """Return True if installed < latest (semver best-effort)."""
    def _parts(v: str) -> tuple:
        try:
            return tuple(int(x) for x in re.split(r"[.\-]", v)[:3])
        except ValueError:
            return (0, 0, 0)
    return _parts(installed) < _parts(latest)


# ═════════════════════════════════════════════════════════════════════════════
# SpoakenUpdater — the main update window
# ═════════════════════════════════════════════════════════════════════════════

class SpoakenUpdater(ctk.CTkToplevel):
    """
    Toplevel window for updating and repairing Spoaken dependencies.

    Parameters
    ----------
    parent : ctk.CTk | ctk.CTkToplevel  — parent window (may be None for
             standalone mode).
    """

    def __init__(self, parent=None):
        if parent is not None:
            super().__init__(parent)
        else:
            # Standalone: create a root window so CTkToplevel works
            self._standalone_root = ctk.CTk()
            self._standalone_root.withdraw()
            super().__init__(self._standalone_root)

        self.title("Spoaken — Update & Repair")
        self.geometry("720x680")
        self.minsize(600, 520)
        self.configure(fg_color=BG_DEEP)
        self.resizable(True, True)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        # Centre on parent or screen
        self.after(50, self._centre)

        # State
        self._pkg_rows   : list[dict] = []   # {pip, import, required, constraint,
                                              #  lbl_status, lbl_installed, lbl_latest}
        self._busy       = False

        self._build_ui()

        # Kick off a background version check immediately
        self.after(300, self._check_versions_bg)

    # ─────────────────────────────────────────────────────────────────────────
    # Layout
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # ── Header ─────────────────────────────────────────────────────────────
        hf = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        hf.grid(row=0, column=0, sticky="ew")
        hf.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hf, text="◈  SPOAKEN  —  Update & Repair",
            font=FONT_TITLE, text_color=TXT_TEAL, anchor="w",
        ).grid(row=0, column=0, padx=16, pady=(12, 4), sticky="w")

        ctk.CTkLabel(
            hf,
            text=(
                f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                f"  ·  {_OS}  ·  {sys.executable}"
            ),
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=1, column=0, padx=16, pady=(0, 10), sticky="w")

        ctk.CTkFrame(hf, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=2, column=0, sticky="ew")

        # ── Action buttons row ─────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=1, column=0, padx=14, pady=10, sticky="ew")
        for col in (0, 1, 2, 3):
            btn_row.grid_columnconfigure(col, weight=1)

        # ★ THE NEON-TEAL UPDATE BUTTON ★
        self.btn_update = ctk.CTkButton(
            btn_row,
            text="⟳  UPDATE",
            font=("Segoe UI Semibold", 14),
            height=46,
            corner_radius=8,
            fg_color    = BTN_UPDATE,
            hover_color = BTN_UPDATE_H,
            text_color  = BTN_UPDATE_TXT,
            command     = self._on_update,
        )
        self.btn_update.grid(row=0, column=0, columnspan=2, padx=(0, 4), sticky="ew")

        ctk.CTkButton(
            btn_row,
            text="⚕  Repair All",
            font=FONT_UI, height=46, corner_radius=8,
            fg_color="#1a3a5e", hover_color="#2450a0",
            command=self._on_repair,
        ).grid(row=0, column=2, padx=4, sticky="ew")

        ctk.CTkButton(
            btn_row,
            text="↺  Check",
            font=FONT_UI, height=46, corner_radius=8,
            fg_color="#0d3a40", hover_color="#145060",
            command=self._on_check,
        ).grid(row=0, column=3, padx=(4, 0), sticky="ew")

        # ── Package table ──────────────────────────────────────────────────────
        table_card = ctk.CTkFrame(
            self, fg_color=BG_CARD,
            border_color=BORDER_SUB, border_width=1, corner_radius=8,
        )
        table_card.grid(row=2, column=0, padx=14, pady=(0, 6), sticky="ew")
        table_card.grid_columnconfigure(0, weight=1)

        # Column headers
        hdr = ctk.CTkFrame(table_card, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=10, pady=(6, 2), sticky="ew")
        hdr.grid_columnconfigure(0, weight=0, minsize=20)
        hdr.grid_columnconfigure(1, weight=1)
        hdr.grid_columnconfigure(2, weight=0, minsize=120)
        hdr.grid_columnconfigure(3, weight=0, minsize=120)
        hdr.grid_columnconfigure(4, weight=0, minsize=50)

        for col, txt in enumerate(("", "Package", "Installed", "Latest", "Req")):
            ctk.CTkLabel(
                hdr, text=txt,
                font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
            ).grid(row=0, column=col, padx=4, sticky="w")

        ctk.CTkFrame(table_card, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=1, column=0, sticky="ew", padx=6)

        # Scrollable body for package rows
        self._table_body = ctk.CTkScrollableFrame(
            table_card,
            fg_color="transparent",
            height=210,
            corner_radius=0,
        )
        self._table_body.grid(row=2, column=0, padx=4, pady=4, sticky="ew")
        self._table_body.grid_columnconfigure(0, weight=0, minsize=20)
        self._table_body.grid_columnconfigure(1, weight=1)
        self._table_body.grid_columnconfigure(2, weight=0, minsize=120)
        self._table_body.grid_columnconfigure(3, weight=0, minsize=120)
        self._table_body.grid_columnconfigure(4, weight=0, minsize=50)

        self._build_package_rows()

        # ── Progress log ───────────────────────────────────────────────────────
        ctk.CTkLabel(
            self, text="Output log",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        ).grid(row=3, column=0, padx=16, pady=(4, 0), sticky="w")

        self._log_box = ctk.CTkTextbox(
            self,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            font=FONT_MONO, text_color=TXT_CONS,
            scrollbar_button_color=BORDER_ACT, corner_radius=8, wrap="word",
        )
        self._log_box.grid(row=4, column=0, padx=14, pady=(2, 14), sticky="nsew")
        self._log_box.configure(state="disabled")
        self.grid_rowconfigure(4, weight=1)

        self._log("Spoaken Update & Repair ready.")
        self._log(f"  Interpreter : {sys.executable}")
        self._log(f"  Platform    : {_OS} / {platform.version()}")

        if _OS == "Linux" and shutil.which("apt"):
            self._log(
                "  Note: pip install will use --break-system-packages (Debian/Ubuntu)"
            )
        if _OS == "Darwin":
            self._log("  Note: Homebrew system packages (portaudio, ffmpeg) not managed here.")
        self._log("")

        # ── Model installer section ────────────────────────────────────────────
        self._build_model_section()

    def _build_model_section(self):
        """Model downloader — Vosk, Whisper, and Ollama LLM pickers."""
        self.grid_rowconfigure(5, weight=0)

        sep = ctk.CTkFrame(self, height=1, fg_color=BORDER_SUB, corner_radius=0)
        sep.grid(row=5, column=0, padx=14, pady=(0, 4), sticky="ew")

        ctk.CTkLabel(
            self, text="Model Installer",
            font=FONT_TITLE, text_color=TXT_TEAL, anchor="w",
        ).grid(row=6, column=0, padx=16, pady=(4, 0), sticky="w")

        model_card = ctk.CTkFrame(
            self, fg_color=BG_CARD,
            border_color=BORDER_SUB, border_width=1, corner_radius=8,
        )
        model_card.grid(row=7, column=0, padx=14, pady=(4, 14), sticky="ew")
        model_card.grid_columnconfigure(1, weight=1)
        model_card.grid_columnconfigure(3, weight=1)

        # ── Vosk picker ────────────────────────────────────────────────────────
        ctk.CTkLabel(model_card, text="Vosk", font=FONT_SMALL,
                     text_color="#00e5cc", anchor="w",
                     ).grid(row=0, column=0, padx=(12, 4), pady=(10, 4), sticky="w")

        vosk_names = [f"{m[0]}  ({m[2]} MB)  — {m[3]}" for m in _VOSK_MODELS]
        self._cmb_vosk = ctk.CTkComboBox(
            model_card, values=vosk_names,
            font=("Courier New", 9), text_color=TXT_MAIN,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_ACT, button_hover_color="#3a60c8",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_MAIN,
            height=28, corner_radius=5,
        )
        self._cmb_vosk.set(vosk_names[0])
        self._cmb_vosk.grid(row=0, column=1, padx=(0, 8), pady=(10, 4), sticky="ew")

        ctk.CTkButton(
            model_card, text="⬇  Install Vosk",
            font=FONT_SMALL, height=28, corner_radius=6,
            fg_color="#0d3a40", hover_color="#145060",
            command=self._on_install_vosk,
        ).grid(row=0, column=2, columnspan=2, padx=(0, 12), pady=(10, 4))

        # ── Whisper picker ─────────────────────────────────────────────────────
        ctk.CTkLabel(model_card, text="Whisper", font=FONT_SMALL,
                     text_color="#4dd9f5", anchor="w",
                     ).grid(row=1, column=0, padx=(12, 4), pady=(4, 4), sticky="w")

        whisper_names = [f"{m[0]}  ({m[1]} MB)  — {m[2]}" for m in _WHISPER_MODELS]
        self._cmb_whisper = ctk.CTkComboBox(
            model_card, values=whisper_names,
            font=("Courier New", 9), text_color=TXT_MAIN,
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color=BORDER_ACT, button_hover_color="#3a60c8",
            dropdown_fg_color=BG_CARD, dropdown_text_color=TXT_MAIN,
            height=28, corner_radius=5,
        )
        self._cmb_whisper.set(whisper_names[0])
        self._cmb_whisper.grid(row=1, column=1, padx=(0, 8), pady=(4, 4), sticky="ew")

        ctk.CTkButton(
            model_card, text="⬇  Install Whisper",
            font=FONT_SMALL, height=28, corner_radius=6,
            fg_color="#1a3a5e", hover_color="#2450a0",
            command=self._on_install_whisper,
        ).grid(row=1, column=2, columnspan=2, padx=(0, 12), pady=(4, 4))

        # ── LLM separator ──────────────────────────────────────────────────────
        ctk.CTkFrame(model_card, height=1, fg_color=BORDER_SUB, corner_radius=0,
                     ).grid(row=2, column=0, columnspan=4, sticky="ew", padx=8, pady=2)

        ctk.CTkLabel(
            model_card, text="LLM  (via Ollama — https://ollama.com)",
            font=("Segoe UI Semibold", 9), text_color="#c084fc", anchor="w",
        ).grid(row=3, column=0, columnspan=4, padx=12, pady=(4, 2), sticky="w")

        # Ollama daemon status indicator
        self._lbl_ollama_status = ctk.CTkLabel(
            model_card, text="Ollama: checking …",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        )
        self._lbl_ollama_status.grid(row=4, column=0, columnspan=2,
                                      padx=(12, 4), pady=(0, 4), sticky="w")

        ctk.CTkButton(
            model_card, text="↺ Check Ollama",
            font=FONT_SMALL, height=26, corner_radius=5,
            fg_color="#1a1040", hover_color="#2a1860",
            text_color="#c084fc",
            command=self._check_ollama_status,
        ).grid(row=4, column=2, columnspan=2, padx=(0, 12), pady=(0, 4))

        # Preferred model list
        _LLM_MODELS = [
            ("mistral-small:24b",
             "Mistral-Small-24B-Instruct-2501  (Q8_0 recommended)",
             "ollama pull mistral-small:24b"),
            ("deepseek-r1:14b",
             "DeepSeek-R1 14B  (strong reasoning)",
             "ollama pull deepseek-r1:14b"),
            ("huihui_ai/qwen2.5-1m-abliterated:14b",
             "Qwen2.5 1M abliterated 14B  (long context)",
             "ollama pull huihui_ai/qwen2.5-1m-abliterated:14b"),
        ]
        llm_names = [f"{m[0]}  — {m[1]}" for m in _LLM_MODELS]
        self._llm_model_data = _LLM_MODELS

        ctk.CTkLabel(model_card, text="Model", font=FONT_SMALL,
                     text_color="#c084fc", anchor="w",
                     ).grid(row=5, column=0, padx=(12, 4), pady=(0, 4), sticky="w")

        self._cmb_llm = ctk.CTkComboBox(
            model_card, values=llm_names,
            font=("Courier New", 8), text_color="#c084fc",
            fg_color=BG_INPUT, border_color=BORDER_SUB, border_width=1,
            button_color="#3a1a60", button_hover_color="#5a2a90",
            dropdown_fg_color=BG_CARD, dropdown_text_color="#c084fc",
            height=28, corner_radius=5,
        )
        self._cmb_llm.set(llm_names[0])
        self._cmb_llm.grid(row=5, column=1, padx=(0, 8), pady=(0, 4), sticky="ew")

        ctk.CTkButton(
            model_card, text="⬇  Pull via Ollama",
            font=FONT_SMALL, height=28, corner_radius=6,
            fg_color="#2a1a40", hover_color="#3d2660",
            text_color="#c084fc",
            command=self._on_pull_llm,
        ).grid(row=5, column=2, columnspan=2, padx=(0, 12), pady=(0, 4))

        # Install ollama Python package button
        ollama_pkg_row = ctk.CTkFrame(model_card, fg_color="transparent")
        ollama_pkg_row.grid(row=6, column=0, columnspan=4,
                             padx=12, pady=(0, 10), sticky="ew")
        ollama_pkg_row.grid_columnconfigure(0, weight=1)

        self._lbl_ollama_pkg = ctk.CTkLabel(
            ollama_pkg_row,
            text="",
            font=FONT_SMALL, text_color=TXT_DIM, anchor="w",
        )
        self._lbl_ollama_pkg.grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            ollama_pkg_row, text="pip install ollama",
            font=FONT_SMALL, height=26, corner_radius=5,
            fg_color="#1a1040", hover_color="#2a1860",
            text_color="#c084fc",
            command=self._on_install_ollama_pkg,
        ).grid(row=0, column=1)

        ctk.CTkButton(
            ollama_pkg_row, text="pip install summarize-pkgs",
            font=FONT_SMALL, height=26, corner_radius=5,
            fg_color="#101830", hover_color="#1a2850",
            text_color=TXT_MAIN,
            command=self._on_install_summarize_pkgs,
        ).grid(row=0, column=2, padx=(6, 0))

        # Kick off Ollama status check
        self.after(500, self._check_ollama_status)

    # ─────────────────────────────────────────────────────────────────────────
    # Ollama / LLM methods
    # ─────────────────────────────────────────────────────────────────────────

    def _check_ollama_status(self):
        """Check if Ollama daemon is running and update the status label."""
        def _check():
            try:
                import urllib.request
                with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
                    import json
                    data   = json.loads(r.read())
                    models = data.get("models", [])
                    names  = ", ".join(m["name"] for m in models[:4])
                    extra  = f" +{len(models)-4}" if len(models) > 4 else ""
                    msg    = f"Ollama: ● running  |  {len(models)} model(s): {names}{extra}"
                    colour = TXT_OK
            except Exception:
                msg    = "Ollama: ○ offline  —  download from https://ollama.com"
                colour = TXT_WARN
            self.after(0, lambda: self._lbl_ollama_status.configure(
                text=msg, text_color=colour))
            # Check ollama Python pkg
            import importlib.util
            has_pkg = importlib.util.find_spec("ollama") is not None
            pkg_msg = "ollama pip pkg: ✔ installed" if has_pkg else "ollama pip pkg: ✗ missing"
            self.after(0, lambda: self._lbl_ollama_pkg.configure(
                text=pkg_msg,
                text_color=TXT_OK if has_pkg else TXT_WARN,
            ))
        import threading as _t
        _t.Thread(target=_check, daemon=True).start()

    def _on_pull_llm(self):
        """Run 'ollama pull <model>' for the selected LLM."""
        if self._busy:
            return
        sel   = self._cmb_llm.get()
        idx   = 0
        names = [f"{m[0]}  — {m[1]}" for m in self._llm_model_data]
        if sel in names:
            idx = names.index(sel)
        model_tag, _, pull_cmd = self._llm_model_data[idx]
        self._set_busy(True, f"Pulling {model_tag} …")
        threading.Thread(target=self._pull_llm_worker,
                         args=(model_tag,), daemon=True).start()

    def _pull_llm_worker(self, model_tag: str):
        import subprocess, shutil
        self._log(f"\nPulling LLM model: {model_tag}")
        self._log("  (This may take several minutes — model files are large)\n")
        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            self._log(
                "  ✗  Ollama CLI not found.\n"
                "  Download and install Ollama from https://ollama.com,\n"
                "  then click 'Pull via Ollama' again.\n"
            )
            self._set_busy(False)
            return
        try:
            proc = subprocess.Popen(
                [ollama_exe, "pull", model_tag],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                self._log(f"  {line.rstrip()}")
            proc.wait()
            if proc.returncode == 0:
                self._log(f"  ✔  {model_tag} ready\n")
            else:
                self._log(f"  ✗  pull exited with code {proc.returncode}\n")
        except Exception as exc:
            self._log(f"  ✗  {exc}\n")
        finally:
            self._set_busy(False)
            self.after(500, self._check_ollama_status)

    def _on_install_ollama_pkg(self):
        """pip install ollama (Python client)."""
        if self._busy:
            return
        self._set_busy(True, "Installing ollama package …")
        threading.Thread(target=self._install_ollama_pkg_worker, daemon=True).start()

    def _install_ollama_pkg_worker(self):
        self._log("\nInstalling ollama Python package …")
        ok = _pip_install("ollama", log_fn=self._log)
        if ok:
            self._log("  ✔  ollama package ready\n")
        else:
            self._log("  ✗  install failed — check output above\n")
        self._set_busy(False)
        self.after(500, self._check_ollama_status)

    def _on_install_summarize_pkgs(self):
        """Install sumy, nltk, scikit-learn for advanced summarization."""
        if self._busy:
            return
        pkgs = ["sumy", "nltk", "scikit-learn"]
        self._set_busy(True, f"Installing {len(pkgs)} summarization packages …")
        threading.Thread(target=self._install_summarize_worker,
                         args=(pkgs,), daemon=True).start()

    def _install_summarize_worker(self, pkgs: list):
        self._log("\nInstalling summarization packages …")
        for pkg in pkgs:
            _pip_install(pkg, log_fn=self._log)
        # Download NLTK data
        try:
            import nltk
            self._log("  Downloading NLTK punkt tokenizer …")
            nltk.download("punkt", quiet=True)
            nltk.download("punkt_tab", quiet=True)
            nltk.download("stopwords", quiet=True)
            self._log("  ✔  NLTK data downloaded")
        except Exception as exc:
            self._log(f"  NLTK data: {exc}")
        self._log("\n  ✔  Summarization packages ready\n")
        self._set_busy(False)

    def _build_package_rows(self):
        """Populate the package table rows."""
        all_pkgs = list(_PACKAGES) + _PLATFORM_EXTRAS.get(_OS, [])
        body     = self._table_body

        for row_idx, (pip_name, import_name, required, constraint) in enumerate(all_pkgs):
            bg = BG_CARD if row_idx % 2 == 0 else BG_DEEP

            lbl_status = ctk.CTkLabel(
                body, text="…",
                font=("Segoe UI", 11), text_color=TXT_DIM, width=20, anchor="center",
            )
            lbl_status.grid(row=row_idx, column=0, padx=(4, 2), pady=1, sticky="w")

            display_name = pip_name.split("<")[0].split(">")[0].split("=")[0]
            ctk.CTkLabel(
                body, text=display_name,
                font=FONT_SMALL, text_color=TXT_MAIN, anchor="w",
            ).grid(row=row_idx, column=1, padx=4, pady=1, sticky="ew")

            lbl_installed = ctk.CTkLabel(
                body, text="—",
                font=("Courier New", 9), text_color=TXT_DIM, anchor="w", width=120,
            )
            lbl_installed.grid(row=row_idx, column=2, padx=4, pady=1, sticky="w")

            lbl_latest = ctk.CTkLabel(
                body, text="—",
                font=("Courier New", 9), text_color=TXT_DIM, anchor="w", width=120,
            )
            lbl_latest.grid(row=row_idx, column=3, padx=4, pady=1, sticky="w")

            req_txt = "✔" if required else "opt"
            ctk.CTkLabel(
                body, text=req_txt,
                font=FONT_SMALL,
                text_color=TXT_OK if required else TXT_DIM,
                anchor="center", width=50,
            ).grid(row=row_idx, column=4, padx=4, pady=1, sticky="w")

            self._pkg_rows.append({
                "pip"       : pip_name,
                "import"    : import_name,
                "required"  : required,
                "constraint": constraint,
                "lbl_status"   : lbl_status,
                "lbl_installed": lbl_installed,
                "lbl_latest"   : lbl_latest,
                "installed_ver": None,
                "latest_ver"   : None,
            })

    # ─────────────────────────────────────────────────────────────────────────
    # Logging
    # ─────────────────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        """Append a line to the output log (thread-safe via after())."""
        def _insert():
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        if threading.current_thread() is threading.main_thread():
            _insert()
        else:
            try:
                self.after(0, _insert)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Version checking
    # ─────────────────────────────────────────────────────────────────────────

    def _check_versions_bg(self):
        """Spawn a background thread to check all versions without blocking UI."""
        if self._busy:
            return
        self._set_busy(True, "Checking versions …")
        threading.Thread(target=self._check_versions_worker, daemon=True).start()

    def _check_versions_worker(self):
        self._log("Checking installed versions …")
        for row in self._pkg_rows:
            iv = _get_installed_version(row["import"])
            row["installed_ver"] = iv
            self.after(0, row["lbl_installed"].configure,
                       {"text": iv or "not installed",
                        "text_color": TXT_OK if iv else TXT_ERR})

        self._log("Fetching latest versions from PyPI …")
        for row in self._pkg_rows:
            lv = _get_latest_version(row["pip"])
            row["latest_ver"] = lv
            self.after(0, row["lbl_latest"].configure,
                       {"text": lv or "?",
                        "text_color": TXT_MAIN if lv else TXT_DIM})

        # Compute status icons
        for row in self._pkg_rows:
            iv = row["installed_ver"]
            lv = row["latest_ver"]
            con = row["constraint"]

            if iv is None:
                icon, colour = "✗", TXT_ERR
            elif lv and "unknown" not in iv and _version_lt(iv, lv):
                icon, colour = "↑", TXT_WARN
            else:
                icon, colour = "✔", TXT_OK

            # Respect version constraint: if constraint says <4.0.0 and
            # latest is ≥4.0.0, the installed version might already be fine
            if con and lv and iv:
                icon, colour = "✔", TXT_OK   # pinned — leave it

            self.after(0, row["lbl_status"].configure,
                       {"text": icon, "text_color": colour})

        upgradeable = sum(
            1 for r in self._pkg_rows
            if r["installed_ver"] is None
            or (r["latest_ver"] and "unknown" not in (r["installed_ver"] or "")
                and not r["constraint"]
                and _version_lt(r["installed_ver"] or "0", r["latest_ver"]))
        )

        self._log(
            f"\nCheck complete — {upgradeable} package{'s' if upgradeable != 1 else ''} "
            f"can be updated.\n"
        )
        self._set_busy(False)

    # ─────────────────────────────────────────────────────────────────────────
    # Buttons
    # ─────────────────────────────────────────────────────────────────────────

    def _on_update(self):
        """Install/upgrade only the packages that are missing or out-of-date."""
        if self._busy:
            return
        to_update = [
            r for r in self._pkg_rows
            if r["installed_ver"] is None or (
                r["latest_ver"]
                and "unknown" not in (r["installed_ver"] or "")
                and not r["constraint"]
                and _version_lt(r["installed_ver"] or "0", r["latest_ver"])
            )
        ]
        if not to_update:
            self._log("All packages are already up to date. ✔")
            return
        self._set_busy(True, f"Updating {len(to_update)} package(s) …")
        threading.Thread(
            target=self._install_worker,
            args=(to_update, False),
            daemon=True,
        ).start()

    def _on_repair(self):
        """Force-reinstall every package in the manifest."""
        if self._busy:
            return
        self._set_busy(True, f"Repairing {len(self._pkg_rows)} package(s) …")
        threading.Thread(
            target=self._install_worker,
            args=(self._pkg_rows, True),
            daemon=True,
        ).start()

    def _on_check(self):
        """Re-run the version check."""
        if self._busy:
            return
        # Reset icons
        for row in self._pkg_rows:
            self.after(0, row["lbl_status"].configure,
                       {"text": "…", "text_color": TXT_DIM})
            self.after(0, row["lbl_installed"].configure,
                       {"text": "—", "text_color": TXT_DIM})
            self.after(0, row["lbl_latest"].configure,
                       {"text": "—", "text_color": TXT_DIM})
        self._check_versions_bg()

    # ─────────────────────────────────────────────────────────────────────────
    # Install worker
    # ─────────────────────────────────────────────────────────────────────────

    def _install_worker(self, rows: list, force: bool):
        total   = len(rows)
        success = 0
        failed  = []

        self._log(
            f"\n{'Repairing' if force else 'Updating'} {total} package(s) …\n"
            + "─" * 50
        )

        for i, row in enumerate(rows, 1):
            pkg = row["pip"]
            self._log(f"[{i}/{total}]  {pkg}")
            self.after(0, row["lbl_status"].configure,
                       {"text": "⟳", "text_color": TXT_TEAL})

            ok = _pip_install(pkg, log_fn=self._log, upgrade=not force)
            if not ok:
                ok = _pip_install(pkg, log_fn=self._log, upgrade=True)

            if ok:
                success += 1
                # Refresh installed version
                new_ver = _get_installed_version(row["import"]) or "installed"
                row["installed_ver"] = new_ver
                self.after(0, row["lbl_installed"].configure,
                           {"text": new_ver, "text_color": TXT_OK})
                self.after(0, row["lbl_status"].configure,
                           {"text": "✔", "text_color": TXT_OK})
            else:
                failed.append(pkg)
                self.after(0, row["lbl_status"].configure,
                           {"text": "✗", "text_color": TXT_ERR})

        self._log("\n" + "─" * 50)
        self._log(
            f"Done — {success}/{total} succeeded"
            + (f"  |  {len(failed)} failed: {', '.join(failed)}" if failed else "")
        )

        if failed:
            self._log(
                "\n  Possible fixes for failed packages:\n"
                "  • An internet connection is required to update\n"
                "  • Windows: ensure Visual C++ Build Tools are installed\n"
                "  • Linux  : sudo apt install python3-dev build-essential\n"
                "  • macOS  : xcode-select --install\n"
                "  Then click ⚕ Repair All to retry.\n"
            )

        self._log_system_dep_hints()
        self._set_busy(False)

    # ─────────────────────────────────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool, label: str = ""):
        self._busy = busy
        def _apply():
            if busy:
                self.btn_update.configure(
                    text=f"⟳  {label}" if label else "⟳  Working …",
                    state="disabled",
                    fg_color="#008a7a",
                )
            else:
                self.btn_update.configure(
                    text="⟳  UPDATE",
                    state="normal",
                    fg_color=BTN_UPDATE,
                )
        self.after(0, _apply)


    # ─────────────────────────────────────────────────────────────────────────
    # Model installer
    # ─────────────────────────────────────────────────────────────────────────

    def _on_install_vosk(self):
        if self._busy:
            return
        sel = self._cmb_vosk.get()
        vals = list(self._cmb_vosk.cget("values"))
        idx = vals.index(sel) if sel in vals else 0
        name, url, size_mb, desc = _VOSK_MODELS[idx]
        self._set_busy(True, f"Downloading {name} …")
        threading.Thread(target=self._download_vosk_worker, args=(name, url), daemon=True).start()

    def _on_install_whisper(self):
        if self._busy:
            return
        sel = self._cmb_whisper.get()
        vals = list(self._cmb_whisper.cget("values"))
        idx = vals.index(sel) if sel in vals else 0
        model_name = _WHISPER_MODELS[idx][0]
        self._set_busy(True, f"Downloading Whisper {model_name} …")
        threading.Thread(target=self._download_whisper_worker, args=(model_name,), daemon=True).start()

    def _download_vosk_worker(self, model_name: str, url: str):
        import urllib.request
        import zipfile
        import tempfile
        tmp_path = ""
        try:
            try:
                from paths import VOSK_DIR
                dest_dir = Path(VOSK_DIR)
            except Exception:
                dest_dir = Path(sys.executable).parent.parent / "models" / "vosk"
            dest_dir.mkdir(parents=True, exist_ok=True)
            model_path = dest_dir / model_name
            if model_path.exists():
                self._log(f"  ✔  {model_name} already installed at {model_path}")
                self._set_busy(False)
                return
            self._log(f"\nDownloading Vosk model: {model_name}")
            self._log(f"  URL    : {url}")
            self._log(f"  Target : {dest_dir}")
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            def _hook(count, block, total):
                if total > 0 and count % 500 == 0:
                    self._log(f"  … {min(100, count*block*100//total)}%")
            urllib.request.urlretrieve(url, tmp_path, _hook)
            self._log("  Download complete — extracting …")
            with zipfile.ZipFile(tmp_path, "r") as zf:
                zf.extractall(dest_dir)
            Path(tmp_path).unlink(missing_ok=True)
            self._log(f"  ✔  {model_name} installed to {dest_dir}\n")
        except Exception as exc:
            self._log(f"  ✗  Vosk download failed: {exc}\n")
            if tmp_path:
                try: Path(tmp_path).unlink(missing_ok=True)
                except Exception: pass
        finally:
            self._set_busy(False)

    def _download_whisper_worker(self, model_name: str):
        try:
            try:
                from paths import WHISPER_DIR
                download_root = str(WHISPER_DIR)
            except Exception:
                download_root = str(Path(sys.executable).parent.parent / "models" / "whisper")
            self._log(f"\nDownloading Whisper model: {model_name}")
            self._log(f"  Download root: {download_root}")
            from faster_whisper import WhisperModel
            _m = WhisperModel(model_name, device="cpu", compute_type="int8",
                              download_root=download_root)
            del _m
            self._log(f"  ✔  Whisper '{model_name}' ready in {download_root}\n")
        except ImportError:
            self._log("  ✗  faster-whisper not installed — run UPDATE first.\n")
        except Exception as exc:
            self._log(f"  ✗  Whisper download failed: {exc}\n")
        finally:
            self._set_busy(False)


    def _log_system_dep_hints(self):
        """Print platform-specific reminders for C-level dependencies."""
        if _OS == "Linux":
            pm = "apt" if shutil.which("apt") else "dnf" if shutil.which("dnf") else None
            if pm:
                self._log(
                    f"\n  System package reminder ({pm}):\n"
                    f"  sudo {pm} install ffmpeg portaudio19-dev python3-dev "
                    f"wmctrl xdotool\n"
                )
        elif _OS == "Darwin":
            self._log(
                "\n  System package reminder (Homebrew):\n"
                "  brew install ffmpeg portaudio\n"
            )
        elif _OS == "Windows":
            self._log(
                "\n  System package reminder:\n"
                "  FFmpeg: https://www.gyan.dev/ffmpeg/builds/\n"
                "  Visual C++ Build Tools: "
                "https://visualstudio.microsoft.com/visual-cpp-build-tools/\n"
            )

    def _centre(self):
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            w  = self.winfo_width()
            h  = self.winfo_height()
            x  = (sw - w) // 2
            y  = (sh - h) // 2
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# Standalone entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    root = ctk.CTk()
    root.withdraw()   # hide the invisible root window

    win = SpoakenUpdater(parent=None)
    win._standalone_root = root

    def _on_close():
        try:
            root.destroy()
        except Exception:
            pass

    win.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
    
    
    
    
    
    
    
    
    
    
