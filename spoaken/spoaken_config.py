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

    # ── T5 text-to-text transformer model ────────────────────────────────────
    "t5_model":               "vennify/t5-base-grammar-correction",
}

config_data = dict(_DEFAULTS)

for config_path in _CONFIG_CANDIDATES:
    if Path(config_path).exists():
        try:
            with open(config_path, encoding="utf-8") as _f:
                config_data.update(json.load(_f))
            break
        except Exception as parse_error:
            print(f"[Config Warning]: could not parse {_cp}: {_e}", file=sys.stderr)

# ── Public API ────────────────────────────────────────────────────────────────

# Vosk
VOSK_ENABLED          = bool(config_data["vosk_enabled"])
QUICK_VOSK_MODEL      = str(config_data["vosk_model"])
ENABLE_GIGA_MODEL     = bool(config_data["enable_giga_model"])
ACCURATE_VOSK_MODEL   = str(config_data["vosk_model_accurate"])

# Whisper
WHISPER_ENABLED       = bool(config_data["whisper_enabled"])
WHISPER_MODEL         = str(config_data["whisper_model"])
WHISPER_COMPUTE       = str(config_data.get("whisper_compute", "auto"))

# Grammar
GRAMMAR_ENABLED       = bool(config_data["grammar"])

# Hardware
GPU_ENABLED           = bool(config_data["gpu"])
MIC_DEVICE            = config_data["mic_device"]           
NOISE_SUPPRESSION     = bool(config_data["noise_suppression"])

# Networking
CHAT_SERVER_ENABLED   = bool(config_data["chat_server_enabled"])
CHAT_SERVER_PORT      = int(config_data["chat_server_port"])
CHAT_SERVER_TOKEN     = str(config_data["chat_server_token"])

# Warn if the token is still the factory default — it should be changed before
# exposing the chat server to any network you don't fully trust.
if CHAT_SERVER_TOKEN in ("spoaken", ""):
    print(
        "[Config Warning]: chat_server_token is set to the default value.\n"
        "  Anyone who knows the default can connect to your LAN chat server.\n"
        "  Edit spoaken_config.json and set a unique token before enabling chat.\n"
        "  Please keep port off unless you know what you are doing,",
        file=sys.stderr,
    )
ANDROID_STREAM_ENABLED = bool(config_data["android_stream_enabled"])
ANDROID_STREAM_PORT   = int(config_data["android_stream_port"])

# Memory
MEMORY_CAP_WORDS      = int(config_data["memory_cap_words"])
MEMORY_CAP_MINUTES    = int(config_data["memory_cap_minutes"])

# Text quality
DUPLICATE_FILTER      = bool(config_data["duplicate_filter"])

# T5 text-to-text model
T5_MODEL              = str(config_data.get("t5_model", "vennify/t5-base-grammar-correction"))




