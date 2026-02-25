"""
spoaken_connect.py
──────────────────
Data / model layer for Spoaken.

Backends
────────
  Vosk          — low-latency real-time partials (always-on during recording)
  faster-whisper — high-accuracy final transcriptions (4-second chunks)

Each backend writes to its own log:
    Logs/vosk_log.txt
    Logs/whisper_log.txt

Grammar correction (T5) is loaded lazily in the background and applied
when the user triggers "Polish" or the memory-cap fires.

Noise suppression
─────────────────
Optional.  Requires:  pip install noisereduce
Enabled via spoaken_config.json → "noise_suppression": true
"""

import os
import sys
import numpy as np
from queue   import Queue
from pathlib import Path

import sounddevice as sd

from paths import VOSK_DIR, HAPPY_DIR, WHISPER_DIR
from spoaken_config import (
    VOSK_ENABLED, QUICK_VOSK_MODEL, ENABLE_GIGA_MODEL, ACCURATE_VOSK_MODEL,
    WHISPER_ENABLED, WHISPER_MODEL,
    GRAMMAR_ENABLED, GPU_ENABLED, NOISE_SUPPRESSION,
    WHISPER_COMPUTE,
)

# ── Optional: noisereduce ─────────────────────────────────────────────────────
try:
    import noisereduce as nr
    _NR_AVAILABLE = True
except ImportError:
    _NR_AVAILABLE = False

# ── Runtime engine-enable flags (toggled via GUI label click) ─────────────────
# These mirror the config defaults but can be flipped at any time without
# restarting.  spoaken_control.set_engine_enabled() writes to these.
VOSK_ACTIVE    = VOSK_ENABLED     # True = process Vosk results
WHISPER_ACTIVE = WHISPER_ENABLED  # True = process Whisper results

# ── Vosk ──────────────────────────────────────────────────────────────────────
_vosk_ok = False
VoskModel = KaldiRecognizer = None
if VOSK_ENABLED:
    try:
        from vosk import Model as VoskModel, KaldiRecognizer
        _vosk_ok = True
    except ImportError:
        print(
            "[Connect Warning]: vosk not installed — Vosk backend disabled.\n"
            "  Fix:  pip install vosk",
            file=sys.stderr,
        )

# ── faster-whisper ────────────────────────────────────────────────────────────
_whisper_ok = False
WhisperModel = None
if WHISPER_ENABLED:
    try:
        from faster_whisper import WhisperModel
        _whisper_ok = True
    except ImportError:
        print(
            "[Connect Warning]: faster-whisper not installed — Whisper disabled.\n"
            "  Fix:  pip install faster-whisper",
            file=sys.stderr,
        )

# ── HappyTransformer (T5 grammar) ─────────────────────────────────────────────
_happy_ok = False
HappyTextToText = TTSettings = None
if GRAMMAR_ENABLED:
    try:
        from happytransformer import HappyTextToText, TTSettings
        _happy_ok = True
    except ImportError:
        print(
            "[Connect Warning]: happytransformer not installed — grammar disabled.\n"
            '  Fix:  pip install "happytransformer<4.0.0"',
            file=sys.stderr,
        )

_T5_HF_NAME   = "prithivida/grammar_error_correcter_v1"
_T5_CACHE_DIR = HAPPY_DIR
_T5_HUB_DIR   = HAPPY_DIR / "hub" / "models--prithivida--grammar_error_correcter_v1"
_T5_SOURCE    = str(_T5_HUB_DIR) if _T5_HUB_DIR.is_dir() else _T5_HF_NAME

# ── Deep-translator (optional translate command) ──────────────────────────────
try:
    from deep_translator import GoogleTranslator as _GoogleTranslator
    _translate_ok = True
except ImportError:
    _GoogleTranslator = None
    _translate_ok = False

# ─────────────────────────────────────────────────────────────────────────────
# Audio device utilities
# ─────────────────────────────────────────────────────────────────────────────

_DEVICE_CACHE: list | None = None

def list_input_devices() -> list:
    """Return [(index, name), …] for all available input devices (cached)."""
    global _DEVICE_CACHE
    if _DEVICE_CACHE is not None:
        return _DEVICE_CACHE
    try:
        _DEVICE_CACHE = [
            (i, d["name"])
            for i, d in enumerate(sd.query_devices())
            if d.get("max_input_channels", 0) > 0
        ]
        return _DEVICE_CACHE
    except Exception as e:
        print(f"[Connect Warning]: cannot enumerate audio devices — {e}", file=sys.stderr)
        _DEVICE_CACHE = []
        return []


def default_device_name() -> str:
    try:
        idx  = sd.default.device[0]
        info = sd.query_devices(idx)
        return info.get("name", "System Default")
    except Exception:
        return "System Default"


# ─────────────────────────────────────────────────────────────────────────────
# Noise suppression
# ─────────────────────────────────────────────────────────────────────────────

def maybe_suppress_noise(audio_bytes: bytes, sr: int = 16000) -> bytes:
    """Spectral noise gate — no-op if noisereduce is unavailable or disabled."""
    if not (_NR_AVAILABLE and NOISE_SUPPRESSION):
        return audio_bytes
    try:
        arr     = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        reduced = nr.reduce_noise(y=arr, sr=sr, stationary=True, prop_decrease=0.75)
        return reduced.astype(np.int16).tobytes()
    except Exception:
        return audio_bytes


# ─────────────────────────────────────────────────────────────────────────────
# ── Whisper compute-type resolver ─────────────────────────────────────────────

def _resolve_compute_type(requested: str, use_gpu: bool) -> str:
    """
    Resolve the faster-whisper compute_type from user config.

    Accepted values (case-insensitive)
    ───────────────────────────────────
      auto    → float16 on GPU, int8 on CPU  (default)
      float16 / fp16 / 16   → 16-bit floats  (GPU recommended)
      float32 / fp32 / 32   → 32-bit floats  (highest accuracy, slower)
      int8    / iso8 / 8    → 8-bit integers (fastest CPU inference)
    """
    r = requested.strip().lower()
    if r in ("float16", "fp16", "16"):
        return "float16"
    if r in ("float32", "fp32", "32"):
        return "float32"
    if r in ("int8", "iso8", "8"):
        return "int8"
    # "auto" or anything unrecognised → safe defaults
    return "float16" if use_gpu else "int8"


# Vosk model resolver
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_vosk(name: str) -> str:
    p = Path(name)
    if p.is_absolute() and p.is_dir():
        return str(p)
    in_vosk = VOSK_DIR / name
    if in_vosk.is_dir():
        return str(in_vosk)
    legacy = Path(__file__).parent / name
    if legacy.is_dir():
        return str(legacy)
    raise FileNotFoundError(
        f"\n[Spoaken Error]: Vosk model '{name}' not found.\n"
        f"  Expected:  {in_vosk}\n"
        f"  Download models: https://alphacephei.com/vosk/models\n"
        f"  Or re-run the installer:  python install.py --vosk-only\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Translation helper
# ─────────────────────────────────────────────────────────────────────────────

_LANG_MAP = {
    "english": "en", "french": "fr", "spanish": "es", "german": "de",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
    "chinese": "zh-CN", "japanese": "ja", "korean": "ko", "arabic": "ar",
    "hindi": "hi", "turkish": "tr", "polish": "pl", "swedish": "sv",
    "norwegian": "no", "danish": "da", "finnish": "fi", "greek": "el",
    "czech": "cs", "romanian": "ro", "hungarian": "hu", "ukrainian": "uk",
    "hebrew": "iw", "thai": "th", "vietnamese": "vi", "indonesian": "id",
}

def translate_text(text: str, target_lang: str) -> str | None:
    """
    Translate text to target_lang.
    target_lang may be a language name (fuzzy-matched) or an ISO code.
    Returns None if translation is unavailable.
    """
    if not _translate_ok:
        return None
    try:
        from rapidfuzz import process
        lang_lower = target_lang.lower().strip()
        # Try exact map first
        code = _LANG_MAP.get(lang_lower)
        if code is None:
            # Fuzzy match language names
            m = process.extractOne(lang_lower, list(_LANG_MAP.keys()), score_cutoff=60)
            if m:
                code = _LANG_MAP[m[0]]
        if code is None:
            code = lang_lower[:5]  # assume it's already a code
        return _GoogleTranslator(source="auto", target=code).translate(text)
    except Exception as e:
        print(f"[Translate Warning]: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Installed model scanners  (used by GUI dropdowns)
# ─────────────────────────────────────────────────────────────────────────────

def scan_installed_vosk_models() -> list:
    """
    Return a sorted list of Vosk model folder names found inside VOSK_DIR.
    Each entry is just the folder name (e.g. 'vosk-model-small-en-us-0.15').
    Returns ['(none installed)'] if the directory is empty or absent.
    """
    try:
        found = sorted(
            d.name for d in VOSK_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        return found if found else ["(none installed)"]
    except Exception:
        return ["(none installed)"]


def scan_installed_whisper_models() -> list:
    """
    Return a sorted list of faster-whisper model names found inside WHISPER_DIR.

    faster-whisper stores models as:
        <WHISPER_DIR>/models--Systran--faster-whisper-<name>/snapshots/<hash>/

    We strip the 'models--Systran--faster-whisper-' prefix to get the friendly
    model name (e.g. 'base.en', 'turbo', 'large-v3').

    Returns ['(none installed)'] if nothing is found.
    """
    _PREFIX = "models--Systran--faster-whisper-"
    try:
        found = sorted(
            d.name[len(_PREFIX):]
            for d in WHISPER_DIR.iterdir()
            if d.is_dir() and d.name.startswith(_PREFIX)
        )
        return found if found else ["(none installed)"]
    except Exception:
        return ["(none installed)"]


# ═════════════════════════════════════════════════════════════════════════════
# TranscriptionModel
# ═════════════════════════════════════════════════════════════════════════════

class TranscriptionModel:

    def __init__(self, quick_vosk: str | None = None, accurate_vosk: str | None = None):
        """
        quick_vosk    : Vosk small model folder name (or None if Vosk disabled).
        accurate_vosk : Vosk giga model folder name (or None).
        """

        # ── Vosk ──────────────────────────────────────────────────────────────
        self.small_model     = None
        self.giga_model      = None
        self.giga_model_path = None
        self.giga_model_status = False

        if _vosk_ok and quick_vosk:
            try:
                self.small_model = VoskModel(_resolve_vosk(quick_vosk))
            except FileNotFoundError as exc:
                print(exc, file=sys.stderr)

        if _vosk_ok and accurate_vosk and ENABLE_GIGA_MODEL:
            try:
                self.giga_model_path = _resolve_vosk(accurate_vosk)
            except FileNotFoundError as exc:
                print(exc, file=sys.stderr)

        # ── Whisper ───────────────────────────────────────────────────────────
        self.whisper_model = None
        if _whisper_ok:
            try:
                device       = "cuda" if GPU_ENABLED else "cpu"
                compute_type = _resolve_compute_type(WHISPER_COMPUTE, GPU_ENABLED)
                self.whisper_model = WhisperModel(
                    WHISPER_MODEL,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(WHISPER_DIR),
                )
                print(
                    f"[Connect]: Whisper loaded — model={WHISPER_MODEL}, "
                    f"device={device}, compute={compute_type}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"[Connect Warning]: Whisper model load failed — {exc}", file=sys.stderr)

        # ── Grammar (T5) — loaded lazily via _background_load() ───────────────
        self.tool = None

        # ── Audio queues — one per consumer so they never race ────────────────
        self.vosk_queue    = Queue()   # → audio_stream_loop (Vosk fast)
        self.giga_queue    = Queue()   # → accuracy_process_loop
        self.whisper_queue = Queue()   # → whisper_loop

        # Legacy alias kept so controller code that references model.audio_queue
        # still compiles; route it to vosk_queue.
        self.audio_queue   = self.vosk_queue

        self.is_running  = False
        self.data_store  = []          # Vosk confirmed sentences
        self.whisper_store = []        # Whisper final sentences

    # ── Background loader (giga Vosk + T5) ────────────────────────────────────

    def _background_load(self):
        if self.giga_model_path:
            self.giga_model = VoskModel(self.giga_model_path)

        if _happy_ok:
            try:
                os.environ["TRANSFORMERS_CACHE"] = str(_T5_CACHE_DIR)
                self.tool = HappyTextToText("T5", _T5_SOURCE)
            except Exception as exc:
                print(f"[Connect Warning]: T5 load failed — {exc}", file=sys.stderr)
                self.tool = None

    # ── Recognizer factories ───────────────────────────────────────────────────

    def get_fast_recognizer(self) -> "KaldiRecognizer":
        if self.small_model is None:
            raise RuntimeError("[Connect]: Vosk small model not loaded.")
        rec = KaldiRecognizer(self.small_model, 16000)
        rec.SetWords(True)
        rec.SetPartialWords(True)
        return rec

    def get_accurate_recognizer(self) -> "KaldiRecognizer":
        if self.giga_model is None:
            raise RuntimeError("[Connect]: Giga model not loaded yet.")
        return KaldiRecognizer(self.giga_model, 16000)

    # ── Hot-swap model loaders ─────────────────────────────────────────────────
    # IMPORTANT: is_running must be False before calling these.

    def reload_vosk(self, model_name: str) -> bool:
        """
        Replace the active Vosk small model with a different installed model.
        Returns True on success, False on failure.
        Call only while recording is stopped.
        """
        if not _vosk_ok:
            return False
        try:
            new_path = _resolve_vosk(model_name)
            self.small_model = VoskModel(new_path)
            return True
        except Exception as exc:
            print(f"[Connect]: reload_vosk failed — {exc}", file=sys.stderr)
            return False

    def reload_whisper(self, model_name: str) -> bool:
        if not _whisper_ok:
            return False
        try:
            device       = "cuda" if GPU_ENABLED else "cpu"
            compute_type = _resolve_compute_type(WHISPER_COMPUTE, GPU_ENABLED)
            self.whisper_model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=str(WHISPER_DIR),
            )
            return True
        except Exception as exc:
            print(f"[Connect]: reload_whisper failed — {exc}", file=sys.stderr)
            return False



    def transcribe_whisper(self, audio_bytes: bytes) -> str:
        """Convert a raw int16 PCM buffer into text via faster-whisper."""
        if self.whisper_model is None:
            return ""
        try:
            arr = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self.whisper_model.transcribe(
                arr,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as exc:
            print(f"[Whisper Warning]: transcription failed — {exc}", file=sys.stderr)
            return ""

    # ── Grammar polish ─────────────────────────────────────────────────────────

    def run_polish(self, store: list | None = None) -> tuple:
        src = store if store is not None else self.data_store
        if not src:
            return "Empty", "No text to polish."

        full = " ".join(src)

        if self.tool:
            try:
                args   = TTSettings(num_beams=2, min_length=1, max_length=512)
                words  = full.split()
                chunks = [" ".join(words[i : i + 100]) for i in range(0, len(words), 100)]
                corrected = " ".join(
                    self.tool.generate_text(f"grammar: {c}", args=args).text
                    for c in chunks
                )
            except Exception as exc:
                corrected = f"[Polish failed — {exc}]"
        else:
            corrected = full   # no T5 — return raw unchanged

        return full, corrected
        
        
        
        
        
        
