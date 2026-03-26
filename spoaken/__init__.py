"""
Spoaken - Real-time Speech Transcription
========================================

A high-performance speech-to-text application with support for multiple
transcription engines (Vosk, Whisper), LLM integration, and network features.

Modules
-------
- core: Transcription engines and configuration
- ui: User interface components
- network: Chat and networking features
- processing: Text processing (LLM, summarization, formatting)
- system: System utilities and monitoring
- control: Application control and commands
"""

__version__ = "3.0.0"
__author__ = "Spoaken Team"

# Core exports for convenience
from spoaken.core.config import (
    ENGINE_MODE,
    VOSK_ENABLED,
    WHISPER_ENABLED,
    GRAMMAR_ENABLED,
)

__all__ = [
    "__version__",
    "ENGINE_MODE",
    "VOSK_ENABLED",
    "WHISPER_ENABLED",
    "GRAMMAR_ENABLED",
]
