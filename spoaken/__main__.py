#!/usr/bin/env python3
"""
__main__.py
───────────
Entry point for Spoaken.

Run with:
    python -m spoaken          (from inside the activated venv)

Features
--------
  • Venv guard — detects if running outside the venv and re-execs into it
    automatically, so the app always uses the correct interpreter + packages.
  • Global exception handler catches ALL crashes
  • Detailed crash logs with system info
  • User-friendly error dialogs
  • Lazy model loading based on engine mode
"""

import sys
import os

# ── Venv guard — MUST be first, before any third-party imports ────────────────
# If we are not already running inside the project venv, find it and re-exec.
# This means `python3 -m spoaken` always works regardless of which Python
# the user typed, as long as the venv exists.

def _find_venv_python():
    """
    Look for the venv Python relative to this file's location.
    This file is at:  <install_dir>/spoaken/__main__.py
    The venv is at:   <install_dir>/venv/
    """
    from pathlib import Path as _P
    install_dir = _P(__file__).resolve().parent.parent
    candidates = [
        install_dir / "venv" / "bin"     / "python",    # Unix
        install_dir / "venv" / "Scripts" / "python.exe",# Windows
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None

def _in_venv():
    """Return True if the current interpreter is inside a virtual environment."""
    return (
        sys.prefix != sys.base_prefix
        or os.environ.get("VIRTUAL_ENV") is not None
    )

if not _in_venv():
    venv_python = _find_venv_python()
    if venv_python and venv_python != sys.executable:
        # Re-exec into the venv Python, passing through all arguments.
        # os.execv replaces the current process — no subprocess overhead.
        os.execv(venv_python, [venv_python, "-m", "spoaken"] + sys.argv[1:])
    elif venv_python is None:
        print(
            "[Spoaken]: venv not found.\n"
            "  Run the installer first:\n"
            "    ./install.sh\n"
            "  Then launch with:\n"
            "    ./run.sh",
            file=sys.stderr,
        )
        sys.exit(1)
    # If venv_python == sys.executable we're somehow already on the right one


# ── Version check ──────────────────────────────────────────────────────────────
if sys.version_info < (3, 9):
    print(
        f"[Spoaken]: Python 3.9+ required "
        f"(current: {sys.version_info.major}.{sys.version_info.minor})",
        file=sys.stderr,
    )
    sys.exit(1)

import threading

# ── Set up crash logging FIRST (before any other imports) ─────────────────────
try:
    from spoaken.system.crashlog import setup_global_exception_handler, log_crashes
    crash_logger = setup_global_exception_handler("Spoaken")
    print("[Spoaken]: Crash logging enabled")
except ImportError:
    print("[Spoaken Warning]: Crash logging not available", file=sys.stderr)
    crash_logger = None

    def log_crashes(context=""):
        def decorator(func):
            return func
        return decorator


# ── Pre-startup install-directory notice ──────────────────────────────────────
# Print the install location before the splash screen or any model loading,
# so the user always knows where to find run.sh even from an IDE or raw python3.
def _print_launch_hint():
    """
    Read install_dir from spoaken_config.json and print a one-line console
    notice showing where Spoaken is installed and the command to relaunch.

    Runs unconditionally at import time — before the Tk mainloop — so it
    appears in the terminal that invoked the app even when the GUI takes over.
    """
    try:
        from pathlib import Path as _P
        import json as _json
        # Config sits one level above spoaken/__main__.py
        _cfg = _P(__file__).resolve().parent.parent / "spoaken_config.json"
        if _cfg.exists():
            _data = _json.loads(_cfg.read_text(encoding="utf-8"))
            _idir = _data.get("install_dir", "")
            if _idir:
                _run  = str(_P(_idir) / "run.sh")
                print("")
                print("[Spoaken]: ─────────────────────────────────────────────")
                print(f"[Spoaken]: Installed at:  {_idir}")
                print(f"[Spoaken]: Run anytime :  {_run}")
                print("[Spoaken]: ─────────────────────────────────────────────")
                print("")
    except Exception:
        pass   # never block startup over a hint message

_print_launch_hint()


# ── Main application ───────────────────────────────────────────────────────────

@log_crashes("Application Initialization")
def main():
    """Main entry point with crash logging."""
    try:
        from spoaken.ui.splash import SpoakenSplash

        splash      = SpoakenSplash()
        result:      dict = {}
        init_errors: list = []

        @log_crashes("Background Initialization")
        def init_background():
            """Background initialization — only loads enabled components."""
            try:
                from spoaken.core.config import (
                    VOSK_ENABLED,
                    QUICK_VOSK_MODEL,
                    WHISPER_ENABLED,
                    WHISPER_MODEL,
                    ENGINE_MODE,
                )
                VOSK_MODEL = QUICK_VOSK_MODEL

                splash.after(
                    0, splash.set_progress, 0.10,
                    f"Loading config ({ENGINE_MODE} mode) …",
                )

                from spoaken.core.engine import TranscriptionModel
                splash.after(0, splash.set_progress, 0.30, "Initialising model layer …")

                from spoaken.control.controller import TranscriptionController
                splash.after(0, splash.set_progress, 0.50, "Building controller …")

                from spoaken.ui.gui import TranscriptionView
                splash.after(0, splash.set_progress, 0.70, "Building interface …")

                controller = TranscriptionController()
                splash.after(0, splash.set_progress, 0.85, "Loading models …")

                # status_callback is invoked from this background thread.
                # Routing it through splash.after(0, …) ensures every Tk
                # widget update (progress bar, label) happens on the main
                # thread — calling Tk methods from any other thread is
                # undefined behaviour and causes intermittent crashes on
                # macOS and some Linux Tk builds.
                def _safe_progress(value: float, text: str):
                    try:
                        splash.after(0, splash.set_progress, value, text)
                    except Exception:
                        pass   # splash may have been dismissed already

                vosk_model = VOSK_MODEL if VOSK_ENABLED else None
                model = TranscriptionModel(
                    vosk_model=vosk_model,
                    status_callback=_safe_progress,
                )
                splash.after(0, splash.set_progress, 1.00, "Ready!")

                result["controller"]        = controller
                result["model"]             = model
                result["TranscriptionView"] = TranscriptionView

            except Exception as exc:
                init_errors.append(exc)
                raise

            finally:
                splash.after(600, splash._finish)

        init_thread = threading.Thread(target=init_background, daemon=True)
        init_thread.start()
        splash.mainloop()

        if init_errors:
            raise init_errors[0]

        controller        = result["controller"]
        model             = result["model"]
        TranscriptionView = result["TranscriptionView"]

        view = TranscriptionView(controller)
        controller.set_objects(model, view)

        print("[Spoaken]: Application started successfully")
        view.mainloop()

    except FileNotFoundError as exc:
        print(f"[Spoaken Error]: Missing file — {exc}", file=sys.stderr)
        print("Run the installer to download required models.", file=sys.stderr)
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n[Spoaken]: Interrupted by user")
        sys.exit(0)

    except Exception as exc:
        print(f"[Spoaken Fatal]: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
