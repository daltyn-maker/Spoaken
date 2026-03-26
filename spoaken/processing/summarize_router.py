"""
processing/summarize_router.py
───────────────────────────────
Single entry-point for all summarization in Spoaken.

Previously the controller had to import both spoaken.processing.llm and
spoaken.processing.summarize and manage the Ollama→extractive fallback
chain itself in two different places (_llm_chunk_worker and run_summarize).
This module owns that fallback chain so callers only ever call summarize().

Fallback order
──────────────
  1. Ollama LLM          (if running and llm module available)
  2. Extractive (TF-IDF) (always available — no external dependencies)
  3. Hard truncation     (last resort — never fails)

Public API
──────────
  summarize(text, model=None, ratio=0.33)  → str
  is_llm_available()                        → bool
"""

from __future__ import annotations
import sys
from typing import Optional

# ── Availability detection ─────────────────────────────────────────────────
_LLM_AVAILABLE = False
try:
    from spoaken.processing.llm import summarize_llm as _llm_summarize, is_ollama_running
    _LLM_AVAILABLE = True
except ImportError:
    _llm_summarize    = None
    is_ollama_running = lambda: False  # noqa: E731


def is_llm_available() -> bool:
    """Return True if Ollama is installed and the daemon is reachable."""
    return _LLM_AVAILABLE and is_ollama_running()


def summarize(
    text:   str,
    model:  Optional[str] = None,
    ratio:  float         = 0.33,
) -> str:
    """
    Summarize *text* using the best available backend.

    Parameters
    ----------
    text  : Source text (transcript segment or full session).
    model : Ollama model name override — None = auto-pick from preferred list.
    ratio : Target summary ratio for both LLM prompt and extractive fallback.

    Returns
    -------
    Summary string.  Never raises — falls back gracefully at every stage.
    """
    if not text or not text.strip():
        return "Nothing to summarize."

    # ── Stage 1: Ollama LLM ───────────────────────────────────────────────────
    if _LLM_AVAILABLE:
        try:
            if is_ollama_running():
                result = _llm_summarize(text, model=model, ratio=ratio)
                if result and result.strip():
                    return result
        except Exception as exc:
            print(f"[SummarizeRouter]: LLM failed — {exc}", file=sys.stderr)

    # ── Stage 2: Extractive TF-IDF ────────────────────────────────────────────
    try:
        from spoaken.processing.summarize import summarize as _extractive
        result = _extractive(text, ratio=ratio)
        if result and result.strip():
            return result
    except Exception as exc:
        print(f"[SummarizeRouter]: extractive failed — {exc}", file=sys.stderr)

    # ── Stage 3: Hard truncation (never fails) ────────────────────────────────
    words = text.split()
    keep  = max(20, int(len(words) * ratio))
    return " ".join(words[:keep]) + " …"
