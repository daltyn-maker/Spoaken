"""
paths.py
────────
Central directory resolver for Spoaken.
Reads the installer-generated spoaken_config.json for model cache paths
and falls back to sensible defaults when the file is absent.

Installer layout written by install.py:
    <install_dir>/
        spoaken_config.json       ← installer writes this
        models/
            whisper/              ← WHISPER_DIR  (faster-whisper cache)
            vosk/                 ← VOSK_DIR     (vosk model folders)
        happy/                    ← HAPPY_DIR    (T5 grammar model cache)
        Logs/                     ← LOG_DIR
        spoaken/                  ← SPOAKEN_DIR  (this file lives here)
            Art/                  ← ART_DIR
"""

import json
import sys
from pathlib import Path

SPOAKEN_DIR = Path(__file__).parent   # …/spoaken/
ROOT_DIR    = SPOAKEN_DIR.parent      # project root

# ── Try to load the installer-generated config for model paths ────────────────
_CONFIG_CANDIDATES = [
    ROOT_DIR    / "spoaken_config.json",     # beside the spoaken/ package
    SPOAKEN_DIR / "spoaken_config.json",     # inside the package (legacy)
    Path.home() / ".spoaken" / "config.json",# user-home fallback
]

_cfg: dict = {}
for _cp in _CONFIG_CANDIDATES:
    if _cp.exists():
        try:
            with open(_cp, encoding="utf-8") as _f:
                _cfg = json.load(_f)
            break
        except Exception as _e:
            print(f"[Paths]: could not parse {_cp}: {_e}", file=sys.stderr)

# ── Resolve directories (installer config overrides defaults) ─────────────────
WHISPER_DIR = Path(_cfg.get("whisper_dir", ROOT_DIR / "models" / "whisper"))
VOSK_DIR    = Path(_cfg.get("vosk_dir",    ROOT_DIR / "models" / "vosk"))
HAPPY_DIR   = ROOT_DIR / "happy"
ART_DIR     = SPOAKEN_DIR / "Art"
LOG_DIR     = ROOT_DIR / "Logs"

# ── Auto-create all required folders ─────────────────────────────────────────
for _d in (ART_DIR, WHISPER_DIR, VOSK_DIR, HAPPY_DIR, LOG_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(
            f"[Paths Warning]: cannot create {_d} — "
            "check permissions or run installer as admin/sudo",
            file=sys.stderr,
        )
        
        
        
        
        
        
