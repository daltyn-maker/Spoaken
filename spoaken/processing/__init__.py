"""
processing - Text Processing and AI
====================================

Text processing, LLM integration, summarization, and window writing.

Imports are intentionally lazy to avoid importing pyautogui / ollama /
torch at package-init time — those trigger X11 / heavy model loads before
the display and runtime are ready.  Import directly from submodules:

    from spoaken.processing.llm    import LLMProcessor
    from spoaken.processing.summarize import summarize
    from spoaken.processing.writer import DirectWindowWriter

Note: format_transcript was removed — it was never defined in writer.py.
"""

__all__ = [
    "LLMProcessor",
    "summarize",
    "DirectWindowWriter",
]


def __getattr__(name):
    if name == "LLMProcessor":
        from spoaken.processing.llm import LLMProcessor
        return LLMProcessor
    if name == "summarize":
        from spoaken.processing.summarize import summarize
        return summarize
    if name == "DirectWindowWriter":
        from spoaken.processing.writer import DirectWindowWriter
        return DirectWindowWriter
    # Legacy alias that was erroneously exported — guide callers clearly
    if name == "format_transcript":
        raise ImportError(
            "format_transcript was removed from processing/__init__.py — "
            "it was never defined in writer.py.  "
            "Use DirectWindowWriter.write() instead."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
