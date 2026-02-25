"""
spoaken_splash.py
─────────────────
Themed splash screen shown while models load.
Also performs:
  • Python version gate  (3.9+ required)
  • Critical import check (warns if packages are missing)
  • Installer config discovery notice
"""

import sys
import platform
import importlib
import customtkinter as ctk
from PIL import Image

# ── Hard Python version gate ──────────────────────────────────────────────────
_MAJOR, _MINOR = sys.version_info[:2]
if (_MAJOR, _MINOR) < (3, 9):
    # Can't use ctk yet — fall back to bare print
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
    """Scan for missing packages (runs in background thread for speed)."""
    missing = []
    for pkg, fix in _OPTIONAL_PKGS.items():
        if importlib.util.find_spec(pkg) is None:
            missing.append(f"  ✗  {pkg:<20}  →  {fix}")
    return missing

# Start package check in a thread immediately so it overlaps with CTk init
import threading as _threading
_missing_result: list[str] = []
_missing_done   = _threading.Event()

def _bg_check():
    global _missing_result
    _missing_result = _check_missing_packages()
    _missing_done.set()

_threading.Thread(target=_bg_check, daemon=True).start()
_MISSING: list[str] = []   # populated lazily in SpoakenSplash.__init__

BG_DEEP    = "#060c1a"
BG_PANEL   = "#0a1128"
BORDER_SUB = "#1a2d60"
TXT_MAIN   = "#00bdff"
TXT_DIM    = "#007bff"
TXT_TEAL   = "#00e5cc"
TXT_WARN   = "#d4aa00"
ACCENT     = "#2c5fe6"


class SpoakenSplash(ctk.CTk):

    def __init__(self):
        super().__init__(className="Spoaken")
        self.title("Spoaken")
        self.overrideredirect(True)

        # Wait for background package check (with 2 s cap so splash is never blocked)
        _missing_done.wait(timeout=2.0)
        missing = _missing_result

        w, h = 480, 280 + (len(missing) * 14 if missing else 0)
        x = (self.winfo_screenwidth()  // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.configure(fg_color=BG_DEEP)

        self.main_frame = ctk.CTkFrame(
            self, fg_color=BG_PANEL,
            border_color=BORDER_SUB, border_width=2, corner_radius=12,
        )
        self.main_frame.pack(fill="both", expand=True, padx=4, pady=4)

        # ── App icon + title ──────────────────────────────────────────────────
        # Try to load Art/icon.png or Art/icon.ico relative to this file
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
            ).pack(pady=(34, 4))
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
            ).pack(pady=(40, 2))

        ctk.CTkLabel(
            self.main_frame,
            text=f"v2.0  ·  Python {_MAJOR}.{_MINOR}  ·  {platform.system()}",
            font=("Segoe UI", 10),
            text_color=TXT_DIM,
        ).pack(pady=(0, 14))

        # Package warnings
        if missing:
            warn_text = "Some packages are missing:\n" + "\n".join(missing)
            ctk.CTkLabel(
                self.main_frame,
                text=warn_text,
                font=("Courier New", 9),
                text_color=TXT_WARN,
                justify="left",
            ).pack(padx=20, pady=(0, 10))

        self.lbl_status = ctk.CTkLabel(
            self.main_frame,
            text="Warming up …",
            font=("Segoe UI", 11),
            text_color=TXT_DIM,
        )
        self.lbl_status.pack(pady=(0, 14))

        self.progress = ctk.CTkProgressBar(
            self.main_frame,
            width=320, height=5,
            fg_color=BG_DEEP, progress_color=ACCENT, corner_radius=3,
        )
        self.progress.pack()
        self.progress.set(0)

        # Safety timeout: force-close after 30 s if init hangs
        self.after(30_000, self._finish)

    def set_progress(self, value: float, text: str):
        if self.winfo_exists():
            self.progress.set(max(0.0, min(1.0, value)))
            self.lbl_status.configure(text=text)

    def _finish(self):
        """Cancel all pending after() callbacks, then destroy."""
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
            
            
            
            
            
            
            
            
            
