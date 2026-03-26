"""
ui - User Interface Components
===============================

Graphical user interface for Spoaken transcription application.
Built with customtkinter for modern, themed UI.

Imports are intentionally lazy (not at module level) to avoid importing
customtkinter / tkinter — and transitively triggering X11 / pyautogui —
before the display is ready.  Import directly from the submodules when
you need these classes:

    from spoaken.ui.gui    import TranscriptionView
    from spoaken.ui.splash import SpoakenSplash
"""

__all__ = [
    "TranscriptionView",
    "SpoakenSplash",
]


def __getattr__(name):
    if name == "TranscriptionView":
        from spoaken.ui.gui import TranscriptionView
        return TranscriptionView
    if name == "SpoakenSplash":
        from spoaken.ui.splash import SpoakenSplash
        return SpoakenSplash
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
