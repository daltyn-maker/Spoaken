#!/usr/bin/env python3
"""
spoaken_main.py
───────────────
Entry point for Spoaken v2.0.

Threading architecture
──────────────────────
  Main thread : Splash screen (CTk mainloop) → then main window (CTk mainloop).
                Polls a queue.Queue every 50 ms to update the progress bar.
                This is the ONLY thread that ever touches tkinter.
  Init thread : All heavy imports, package installs, and model downloads.
                Communicates with the splash exclusively via progress_queue —
                never calls splash.after() or any tkinter method directly.

Folder layout (installer creates this)
───────────────────────────────────────
    <install_dir>/
      spoaken_config.json
      models/
        whisper/        faster-whisper model cache
        vosk/           vosk model folders
      happy/            T5 grammar model cache
      Logs/
      spoaken/          ← all .py files live here
        Art/            icons / images

Auto-repair features (v2.1)
────────────────────────────
  • Missing Python packages are pip-installed automatically before the splash
    even opens, so the user never sees a raw ImportError.
  • Missing / empty Vosk model folders are downloaded and extracted in the
    background while the splash is visible (progress is reflected on the bar).
  • urllib3 / chardet version-mismatch warnings from requests are silenced.
"""

import os
import sys
import queue
import subprocess
import threading
import warnings

# ── Silence noisy requests/urllib3 compatibility warning ─────────────────────
warnings.filterwarnings("ignore", category=Warning, module="requests")
os.environ.setdefault("PYTHONWARNINGS", "ignore::Warning:requests")

# ── Python version gate ───────────────────────────────────────────────────────
if sys.version_info < (3, 9):
    print(
        f"[Spoaken]: Python 3.9+ required "
        f"(current: {sys.version_info.major}.{sys.version_info.minor})",
        file=sys.stderr,
    )
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Install-dir resolution
# ─────────────────────────────────────────────────────────────────────────────
# Layout:  <INSTALL_DIR>/spoaken/spoaken_main.py   ← this file
#          <INSTALL_DIR>/models/vosk/<model_name>
#          <INSTALL_DIR>/models/whisper/
#          <INSTALL_DIR>/happy/
#          <INSTALL_DIR>/spoaken_config.json

_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_INSTALL_DIR = os.path.dirname(_SCRIPT_DIR)   # one level up from spoaken/
_VOSK_DIR    = os.path.join(_INSTALL_DIR, "models", "vosk")


def _resolve_vosk_path(model_value: str) -> str:
    """
    Turn whatever VOSK_MODEL contains into a usable absolute path.

    Handles three cases:
      1. Already an absolute path  → use as-is.
      2. A bare model name like 'vosk-model-small-en-us-0.15'
         → <INSTALL_DIR>/models/vosk/<name>
      3. A relative path → joined onto <INSTALL_DIR>/models/vosk/
    """
    if not model_value:
        return ""
    if os.path.isabs(model_value):
        return model_value
    return os.path.join(_VOSK_DIR, os.path.basename(model_value))


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: ensure required packages are installed (runs before splash)
# ─────────────────────────────────────────────────────────────────────────────

_REQUIRED_PACKAGES: dict[str, str] = {
    "happytransformer": "happytransformer<4.0.0",
    "vosk":             "vosk",
    "faster_whisper":   "faster-whisper",
    "customtkinter":    "customtkinter",
    "requests":         "requests",
}


def _pip_install(pkg_spec: str) -> bool:
    """Install *pkg_spec* via pip. Returns True on success."""
    print(f"[Spoaken Setup]: installing '{pkg_spec}' …")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "--quiet", "--break-system-packages", pkg_spec],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(
            f"[Spoaken Setup]: WARNING – could not install '{pkg_spec}':\n"
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    print(f"[Spoaken Setup]: '{pkg_spec}' installed OK.")
    return True


def _ensure_packages() -> list[str]:
    still_missing: list[str] = []
    for import_name, pip_spec in _REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError:
            if not _pip_install(pip_spec):
                still_missing.append(pip_spec)
    return still_missing


_missing = _ensure_packages()
if _missing:
    print(
        "[Spoaken Setup]: The following packages could not be installed "
        "automatically:\n  " + "\n  ".join(_missing),
        file=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Vosk model auto-download helpers
# ─────────────────────────────────────────────────────────────────────────────

_VOSK_MODEL_URLS: dict[str, str] = {
    "vosk-model-small-en-us-0.15":
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    "vosk-model-en-us-0.22":
        "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip",
    "vosk-model-en-us-0.22-lgraph":
        "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip",
    "vosk-model-small-en-us-0.22":
        "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.22.zip",
}

_VOSK_SENTINEL = "am/final.mdl"


def _vosk_model_ok(model_path: str) -> bool:
    """Return True if *model_path* contains a valid Vosk model."""
    return os.path.isfile(os.path.join(model_path, _VOSK_SENTINEL))


def _download_vosk_model(
    model_path: str,
    progress_cb=None,   # callable(fraction: float, label: str)  — thread-safe
) -> bool:
    """
    Download and extract the Vosk model into *model_path*.
    *progress_cb* is called from the background thread; it must be thread-safe
    (i.e. must NOT call any tkinter method directly — use a queue instead).
    Returns True on success, False on failure.
    """
    import zipfile
    import urllib.request

    model_name = os.path.basename(model_path.rstrip("/\\"))
    url = _VOSK_MODEL_URLS.get(model_name)

    if url is None:
        print(
            f"[Spoaken Setup]: No download URL known for '{model_name}'.\n"
            f"  Download manually from https://alphacephei.com/vosk/models\n"
            f"  and place the extracted folder at: {model_path}",
            file=sys.stderr,
        )
        return False

    parent_dir = os.path.dirname(model_path)
    os.makedirs(parent_dir, exist_ok=True)
    zip_path = model_path + ".zip"
    print(f"[Spoaken Setup]: Downloading Vosk model from:\n  {url}")

    try:
        def _reporthook(block_num, block_size, total_size):
            if total_size > 0 and progress_cb:
                frac = min(block_num * block_size / total_size, 1.0)
                # Map download into the 0.20–0.58 window of the splash bar
                progress_cb(
                    0.20 + frac * 0.38,
                    f"Downloading Vosk model … {int(frac * 100)}%",
                )

        urllib.request.urlretrieve(url, zip_path, reporthook=_reporthook)

        # Verify zip integrity before extracting
        if not zipfile.is_zipfile(zip_path):
            raise ValueError(f"Downloaded file is not a valid zip: {zip_path}")

        if progress_cb:
            progress_cb(0.60, "Extracting Vosk model …")
        print(f"[Spoaken Setup]: Extracting {zip_path} …")

        with zipfile.ZipFile(zip_path, "r") as zf:
            top_dirs = {n.split("/")[0] for n in zf.namelist() if n.strip()}
            print(f"[Spoaken Setup]: Zip top-level entries: {top_dirs}")
            zf.extractall(parent_dir)

        os.remove(zip_path)
        print("[Spoaken Setup]: Extraction complete.")

        if _vosk_model_ok(model_path):
            print(f"[Spoaken Setup]: Vosk model ready at {model_path}")
            return True

        extracted = [
            d for d in os.listdir(parent_dir)
            if os.path.isdir(os.path.join(parent_dir, d))
        ]
        print(
            f"[Spoaken Setup]: Sentinel not found at {model_path}\n"
            f"  Folders present in {parent_dir}: {extracted}",
            file=sys.stderr,
        )
        return False

    except Exception as exc:
        print(f"[Spoaken Setup]: Failed to download Vosk model: {exc}",
              file=sys.stderr)
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

from spoaken_splash import SpoakenSplash   # noqa: E402


def main():
    try:
        splash       = SpoakenSplash()
        result: dict = {}
        init_errors: list = []

        # ── Thread-safe progress queue ─────────────────────────────────────────
        # Background thread puts (frac, label) tuples.
        # Sentinel value None signals that the thread is done.
        # Main thread drains this queue via a repeating splash.after() poll —
        # the ONLY thread that ever calls tkinter methods is the main thread.
        progress_queue: queue.Queue = queue.Queue()

        def _progress(frac: float, label: str) -> None:
            """Called from the background thread. Never touches tkinter."""
            progress_queue.put((frac, label))

        def _poll_progress() -> None:
            """
            Called from the main thread via splash.after().
            Drains the queue and updates the splash bar, then reschedules
            itself until the sentinel (None) arrives.
            """
            done = False
            while True:
                try:
                    item = progress_queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    done = True
                    break
                frac, label = item
                splash.set_progress(frac, label)

            if done:
                # Give the user a moment to see "Ready!" before closing
                splash.after(600, splash._finish)
            else:
                splash.after(50, _poll_progress)

        # Kick off the poll loop from the main thread
        splash.after(50, _poll_progress)

        # ── Background init thread ─────────────────────────────────────────────
        def init_background():
            try:
                # Config
                from spoaken_config import (
                    VOSK_ENABLED, VOSK_MODEL,
                    WHISPER_ENABLED, WHISPER_MODEL,  # noqa: F401
                )
                _progress(0.10, "Loading config …")

                # Vosk model auto-repair
                vosk_model = (
                    _resolve_vosk_path(VOSK_MODEL) if VOSK_ENABLED else None
                )

                if VOSK_ENABLED and vosk_model:
                    if not _vosk_model_ok(vosk_model):
                        model_label = os.path.basename(vosk_model)
                        _progress(0.15,
                                  f"Vosk model '{model_label}' missing — downloading …")
                        print(
                            f"[Spoaken Setup]: Vosk model not found at '{vosk_model}'.\n"
                            f"  Attempting auto-download …"
                        )
                        ok = _download_vosk_model(vosk_model, progress_cb=_progress)
                        if not ok:
                            print(
                                "[Spoaken Setup]: Vosk disabled for this session "
                                "(model unavailable). Whisper will be used if enabled.",
                                file=sys.stderr,
                            )
                            vosk_model = None
                    else:
                        print(f"[Spoaken Setup]: Vosk model OK at '{vosk_model}'")

                # Model layer
                from spoaken_connect import TranscriptionModel
                _progress(0.65, "Loading models …")

                # Controller
                from spoaken_control import TranscriptionController
                _progress(0.75, "Initialising controller …")

                # View
                from spoaken_gui import TranscriptionView
                _progress(0.82, "Building interface …")

                controller = TranscriptionController()
                _progress(0.88, "Starting up …")

                model = TranscriptionModel(vosk_model=vosk_model)
                _progress(1.00, "Ready!")

                result["controller"]        = controller
                result["model"]             = model
                result["TranscriptionView"] = TranscriptionView

            except Exception as exc:
                init_errors.append(exc)

            finally:
                # Signal the poll loop that we are done (success or failure)
                progress_queue.put(None)

        init_thread = threading.Thread(target=init_background, daemon=True)
        init_thread.start()
        splash.mainloop()

        # Ensure the background thread has fully finished before we read results
        init_thread.join()

        if init_errors:
            raise init_errors[0]

        # ── Build main view in main thread (tkinter requirement) ───────────────
        controller        = result["controller"]
        model             = result["model"]
        TranscriptionView = result["TranscriptionView"]

        view = TranscriptionView(controller)
        controller.set_objects(model, view)
        view.mainloop()

    except FileNotFoundError as exc:
        print(f"[Spoaken Error]: {exc}", file=sys.stderr)
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n[Spoaken]: interrupted by user")
        sys.exit(0)

    except Exception as exc:
        print(f"[Spoaken Fatal]: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
