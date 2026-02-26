#!/usr/bin/env python3
"""
spoaken_main.py
───────────────
Entry point for Spoaken v2.0.

Threading architecture
──────────────────────
  Main thread : Splash screen (CTk mainloop) → then main window (CTk mainloop).
  Init thread : Loads Vosk/Whisper models in the background while splash is
                visible.  Signals splash to close when done.

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
"""

import sys
import threading

# ── Python version gate (redundant safety check, splash also checks) ──────────
if sys.version_info < (3, 9):
    print(
        f"[Spoaken]: Python 3.9+ required "
        f"(current: {sys.version_info.major}.{sys.version_info.minor})",
        file=sys.stderr,
    )
    sys.exit(1)

from spoaken_splash import SpoakenSplash


def main():
    try:
        # ── Splash appears immediately ─────────────────────────────────────────
        splash     = SpoakenSplash()
        result: dict = {}
        init_errors: list = []

        def init_background():
            """
            All heavy imports and model loading happen here while the splash
            is visible in the main thread.
            """
            try:
                # Config
                from spoaken_config import (
                    VOSK_ENABLED, QUICK_VOSK_MODEL,
                    ENABLE_GIGA_MODEL, ACCURATE_VOSK_MODEL,
                    WHISPER_ENABLED, WHISPER_MODEL,
                )
                splash.after(0, splash.set_progress, 0.10, "Loading config …")

                # Model layer
                from spoaken_connect import TranscriptionModel
                splash.after(0, splash.set_progress, 0.30, "Loading Vosk model …")

                # Controller
                from spoaken_control import TranscriptionController
                splash.after(0, splash.set_progress, 0.50, "Initialising controller …")

                # View
                from spoaken_gui import TranscriptionView
                splash.after(0, splash.set_progress, 0.70, "Building interface …")

                # Instantiate models
                quick_vosk    = QUICK_VOSK_MODEL if VOSK_ENABLED else None
                accurate_vosk = ACCURATE_VOSK_MODEL if ENABLE_GIGA_MODEL else None

                controller = TranscriptionController()
                splash.after(0, splash.set_progress, 0.85, "Loading models …")

                model = TranscriptionModel(
                    quick_vosk=quick_vosk,
                    accurate_vosk=accurate_vosk,
                )
                splash.after(0, splash.set_progress, 1.00, "Ready!")

                result["controller"]        = controller
                result["model"]             = model
                result["TranscriptionView"] = TranscriptionView

            except Exception as exc:
                init_errors.append(exc)

            finally:
                splash.after(600, splash._finish)

        init_thread = threading.Thread(target=init_background, daemon=True)
        init_thread.start()
        splash.mainloop()

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
    
    
    
    
    
    
    
    
    
    
