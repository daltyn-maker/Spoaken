# ══════════════════════════════════════════════════════════════════════════════
#  spoaken_config.py
#  Auto-loaded from spoaken_config.json (written by the installer wizard).
#  You can edit spoaken_config.json directly or re-run the installer.
# ══════════════════════════════════════════════════════════════════════════════

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent

_CONFIG_CANDIDATES = [
    _ROOT    / "spoaken_config.json",
    _HERE    / "spoaken_config.json",
    Path.home() / ".spoaken" / "config.json",
]

# ── Built-in defaults (used when no config file is found) ─────────────────────
_DEFAULTS: dict = {
    # ── Vosk ──────────────────────────────────────────────────────────────────
    "vosk_enabled":           True,
    "vosk_model":             "vosk-model-small-en-us-0.15",
    "enable_giga_model":      False,
    "vosk_model_accurate":    "vosk-model-en-us-0.42-gigaspeech",

    # ── Whisper (faster-whisper) ───────────────────────────────────────────────
    "whisper_enabled":        True,
    "whisper_model":          "base.en",
    "whisper_compute":        "auto",   # auto | int8 | float16 | float32

    # ── Grammar / T5 ─────────────────────────────────────────────────────────
    "grammar":                True,

    # ── Hardware ──────────────────────────────────────────────────────────────
    "gpu":                    False,
    "mic_device":             None,      # None = system default; int = device index
    "noise_suppression":      False,

    # ── Networking / optional services ───────────────────────────────────────
    "chat_server_enabled":    False,
    "chat_server_port":       55300,
    "chat_server_token":      "spoaken",  # simple shared-token auth
    "android_stream_enabled": False,
    "android_stream_port":    55301,

    # ── Memory management ────────────────────────────────────────────────────
    "memory_cap_words":       300,        # auto-polish at this many words
    "memory_cap_minutes":     10,         # …or after this many minutes

    # ── Text quality ─────────────────────────────────────────────────────────
    "duplicate_filter":       True,       # suppress repeated phrases
}

_cfg = dict(_DEFAULTS)

for _cp in _CONFIG_CANDIDATES:
    if Path(_cp).exists():
        try:
            with open(_cp, encoding="utf-8") as _f:
                _cfg.update(json.load(_f))
            break
        except Exception as _e:
            print(f"[Config Warning]: could not parse {_cp}: {_e}", file=sys.stderr)

# ── Public API ────────────────────────────────────────────────────────────────

# Vosk
VOSK_ENABLED          = bool(_cfg["vosk_enabled"])
QUICK_VOSK_MODEL      = str(_cfg["vosk_model"])
ENABLE_GIGA_MODEL     = bool(_cfg["enable_giga_model"])
ACCURATE_VOSK_MODEL   = str(_cfg["vosk_model_accurate"])

# Whisper
WHISPER_ENABLED       = bool(_cfg["whisper_enabled"])
WHISPER_MODEL         = str(_cfg["whisper_model"])
WHISPER_COMPUTE       = str(_cfg.get("whisper_compute", "auto"))

# Grammar
GRAMMAR_ENABLED       = bool(_cfg["grammar"])

# Hardware
GPU_ENABLED           = bool(_cfg["gpu"])
MIC_DEVICE            = _cfg["mic_device"]           
NOISE_SUPPRESSION     = bool(_cfg["noise_suppression"])

# Networking
CHAT_SERVER_ENABLED   = bool(_cfg["chat_server_enabled"])
CHAT_SERVER_PORT      = int(_cfg["chat_server_port"])
CHAT_SERVER_TOKEN     = str(_cfg["chat_server_token"])
ANDROID_STREAM_ENABLED = bool(_cfg["android_stream_enabled"])
ANDROID_STREAM_PORT   = int(_cfg["android_stream_port"])

# Memory
MEMORY_CAP_WORDS      = int(_cfg["memory_cap_words"])
MEMORY_CAP_MINUTES    = int(_cfg["memory_cap_minutes"])

# Text quality
DUPLICATE_FILTER      = bool(_cfg["duplicate_filter"])




