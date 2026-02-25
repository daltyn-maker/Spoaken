"""
spoaken_splash.py
─────────────────
Themed splash screen shown while models load.

Changes in this revision
────────────────────────
  • Minimize button (–) — hides the splash without killing anything.
  • Close/dismiss button (✕) — hides the splash window; the init thread
    continues running.  The main window will still appear when ready.
  • Force-quit button (⏻) — hard-exits the entire process immediately if
    the user genuinely needs out (e.g. hung import).  Confirmation required.
  • Python version gate  (3.9+ required).
  • Critical import check (warns if packages are missing).
"""

import sys
import platform
import importlib
import customtkinter as ctk
from PIL import Image

# ── Hard Python version gate ──────────────────────────────────────────────────
_MAJOR, _MINOR = sys.version_info[:2]
if (_MAJOR, _MINOR) < (3, 9):
    print(
        f"[Spoaken Fatal]: Python 3.9+ is required "
        f"(you have {_MAJOR}.{_MINOR}).\n"
        "  Download: https://www.python.org/downloads/",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Soft package checks (warn, don't abort) ───────────────────────────────────
_OPTIONAL_PKGS = {
    "vosk":             "pip install vosk",
    "faster_whisper":   "pip install faster-whisper",
    "happytransformer": 'pip install "happytransformer<4.0.0"',
    "sounddevice":      "pip install sounddevice",
    "numpy":            "pip install numpy",
    "pyautogui":        "pip install pyautogui",
    "rapidfuzz":        "pip install rapidfuzz",
    "noisereduce":      "pip install noisereduce    (optional — noise suppression)",
    "deep_translator":  "pip install deep-translator  (optional — translation)",
}

def _check_missing_packages() -> list[str]:
    missing = []
    for pkg, fix in _OPTIONAL_PKGS.items():
        if importlib.util.find_spec(pkg) is None:
            missing.append(f"  ✗  {pkg:<20}  →  {fix}")
    return missing

import threading as _threading
_missing_result: list[str] = []
_missing_done   = _threading.Event()

def _bg_check():
    global _missing_result
    _missing_result = _check_missing_packages()
    _missing_done.set()

_threading.Thread(target=_bg_check, daemon=True).start()

BG_DEEP    = "#060c1a"
BG_PANEL   = "#0a1128"
BORDER_SUB = "#1a2d60"
TXT_MAIN   = "#00bdff"
TXT_DIM    = "#007bff"
TXT_TEAL   = "#00e5cc"
TXT_WARN   = "#d4aa00"
ACCENT     = "#2c5fe6"
BTN_DIM    = "#0d1f45"
BTN_QUIT   = "#3a0a0a"
BTN_QUIT_H = "#661010"


class SpoakenSplash(ctk.CTk):

    def __init__(self):
        super().__init__(className="Spoaken")
        self.title("Spoaken")
        self.overrideredirect(True)

        _missing_done.wait(timeout=2.0)
        missing = _missing_result

        w, h = 480, 300 + (len(missing) * 14 if missing else 0)
        x = (self.winfo_screenwidth()  // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.configure(fg_color=BG_DEEP)

        # ── Outer frame ───────────────────────────────────────────────────────
        self.main_frame = ctk.CTkFrame(
            self, fg_color=BG_PANEL,
            border_color=BORDER_SUB, border_width=2, corner_radius=12,
        )
        self.main_frame.pack(fill="both", expand=True, padx=4, pady=4)

        # ── Title bar row (drag area + window controls) ───────────────────────
        title_bar = ctk.CTkFrame(self.main_frame, fg_color="transparent", height=28)
        title_bar.pack(fill="x", padx=8, pady=(6, 0))
        title_bar.pack_propagate(False)

        # Drag support — click-drag on the title bar moves the window
        title_bar.bind("<ButtonPress-1>",   self._drag_start)
        title_bar.bind("<B1-Motion>",       self._drag_motion)

        ctk.CTkLabel(
            title_bar, text="SPOAKEN  ·  loading …",
            font=("Segoe UI", 9), text_color=TXT_DIM, anchor="w",
        ).pack(side="left", padx=4)

        # Window control buttons (right side)
        _btn_kw = dict(width=22, height=22, corner_radius=4, font=("Segoe UI", 10))

        # Force-quit (red ⏻) — exits entire process after confirmation
        ctk.CTkButton(
            title_bar, text="⏻",
            fg_color=BTN_QUIT, hover_color=BTN_QUIT_H, text_color="#e07070",
            command=self._force_quit, **_btn_kw,
        ).pack(side="right", padx=(2, 0))

        # Dismiss / close splash only (does NOT stop the program)
        ctk.CTkButton(
            title_bar, text="✕",
            fg_color=BTN_DIM, hover_color="#1a3060", text_color=TXT_DIM,
            command=self._dismiss, **_btn_kw,
        ).pack(side="right", padx=2)

        # Minimize
        ctk.CTkButton(
            title_bar, text="–",
            fg_color=BTN_DIM, hover_color="#1a3060", text_color=TXT_DIM,
            command=self._minimize, **_btn_kw,
        ).pack(side="right", padx=2)

        # ── App icon + title ──────────────────────────────────────────────────
        from pathlib import Path
        _ART_DIR = Path(__file__).parent / "Art"
        _splash_icon = None
        for _name in ("icon.png", "icon.ico", "logo.png", "logo.ico"):
            _p = _ART_DIR / _name
            if _p.exists():
                try:
                    _img = Image.open(_p).resize((52, 52), Image.LANCZOS)
                    _splash_icon = ctk.CTkImage(light_image=_img, dark_image=_img, size=(52, 52))
                except Exception:
                    pass
                break

        if _splash_icon:
            ctk.CTkLabel(
                self.main_frame,
                image=_splash_icon, text="",
            ).pack(pady=(20, 4))
            ctk.CTkLabel(
                self.main_frame,
                text="SPOAKEN",
                font=("Segoe UI Semibold", 28),
                text_color=TXT_TEAL,
            ).pack(pady=(0, 2))
        else:
            ctk.CTkLabel(
                self.main_frame,
                text="◈  SPOAKEN",
                font=("Segoe UI Semibold", 30),
                text_color=TXT_TEAL,
            ).pack(pady=(28, 2))

        ctk.CTkLabel(
            self.main_frame,
            text=f"v2.0  ·  Python {_MAJOR}.{_MINOR}  ·  {platform.system()}",
            font=("Segoe UI", 10),
            text_color=TXT_DIM,
        ).pack(pady=(0, 10))

        # Package warnings
        if missing:
            warn_text = "Some packages are missing:\n" + "\n".join(missing)
            ctk.CTkLabel(
                self.main_frame,
                text=warn_text,
                font=("Courier New", 9),
                text_color=TXT_WARN,
                justify="left",
            ).pack(padx=20, pady=(0, 8))

        self.lbl_status = ctk.CTkLabel(
            self.main_frame,
            text="Warming up …",
            font=("Segoe UI", 11),
            text_color=TXT_DIM,
        )
        self.lbl_status.pack(pady=(0, 10))

        self.progress = ctk.CTkProgressBar(
            self.main_frame,
            width=320, height=5,
            fg_color=BG_DEEP, progress_color=ACCENT, corner_radius=3,
        )
        self.progress.pack()
        self.progress.set(0)

        ctk.CTkLabel(
            self.main_frame,
            text="✕ closes this window  ·  ⏻ force-quits the app",
            font=("Segoe UI", 8),
            text_color="#1a3060",
        ).pack(pady=(8, 6))

        # Drag state
        self._drag_x = 0
        self._drag_y = 0

        # Safety timeout
        self.after(30_000, self._finish)

    # ─────────────────────────────────────────────────────────────────────────
    # Drag support
    # ─────────────────────────────────────────────────────────────────────────

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.geometry(f"+{x}+{y}")

    # ─────────────────────────────────────────────────────────────────────────
    # Window controls
    # ─────────────────────────────────────────────────────────────────────────

    def _minimize(self):
        """Hide the splash to the taskbar. Init thread keeps running."""
        self.overrideredirect(False)   # needed for iconify to work on some WMs
        self.iconify()

    def _dismiss(self):
        """
        Close/hide the splash window.
        The init thread is NOT stopped — the main window will still appear.
        """
        if self.winfo_exists():
            self.withdraw()

    def _force_quit(self):
        """
        Emergency exit — kills the entire Python process.
        Requires the user to click a confirmation button.
        """
        import tkinter as tk
        confirm = ctk.CTkToplevel(self)
        confirm.title("Force Quit?")
        confirm.configure(fg_color=BTN_QUIT)
        confirm.resizable(False, False)
        confirm.geometry("260x110")
        confirm.grab_set()

        ctk.CTkLabel(
            confirm,
            text="⚠  Force-quit Spoaken?",
            font=("Segoe UI Semibold", 12), text_color="#e07070",
        ).pack(pady=(18, 4))
        ctk.CTkLabel(
            confirm,
            text="All loading will stop immediately.",
            font=("Segoe UI", 9), text_color="#a04040",
        ).pack(pady=(0, 10))

        btn_row = ctk.CTkFrame(confirm, fg_color="transparent")
        btn_row.pack()

        def _do_quit():
            try:
                confirm.destroy()
                self.destroy()
            except Exception:
                pass
            sys.exit(0)

        ctk.CTkButton(
            btn_row, text="Yes, quit", width=100,
            fg_color="#661010", hover_color="#991a1a", text_color="#ffaaaa",
            command=_do_quit,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_row, text="Cancel", width=80,
            fg_color=BTN_DIM, hover_color="#1a3060", text_color=TXT_DIM,
            command=confirm.destroy,
        ).pack(side="left", padx=6)

    # ─────────────────────────────────────────────────────────────────────────
    # Progress API
    # ─────────────────────────────────────────────────────────────────────────

    def set_progress(self, value: float, text: str):
        if self.winfo_exists():
            self.progress.set(max(0.0, min(1.0, value)))
            self.lbl_status.configure(text=text)

    def _finish(self):
        """Cancel pending after() callbacks and close the splash."""
        try:
            for after_id in self.tk.call("after", "info"):
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        except Exception:
            pass
        if self.winfo_exists():
            self.destroy()
