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

from paths import VOSK_DIR, WHISPER_DIR, ROOT_DIR
HAPPY_DIR = ROOT_DIR / "models" / "happy"
from spoaken_config import (
    VOSK_ENABLED, VOSK_MODEL,
    WHISPER_ENABLED, WHISPER_MODEL,
    GRAMMAR_ENABLED, GPU_ENABLED, NOISE_SUPPRESSION,
    WHISPER_COMPUTE, HAPPY_ONLINE_ONLY,
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

# ── Mic config dict — written by MicConfigPanel.apply_settings() ─────────────
# All audio processing reads from here so settings take effect immediately
# without restarting. spoaken_mic_config.MicConfigPanel writes to this dict.
_mic_config: dict = {
    "vad_enabled"  : True,
    "vad_agg"      : 2,
    "min_speech"   : 200,
    "silence_gap"  : 500,
    "eq_profile"   : "speech",   # flat | speech | aggressive | custom
    "hp_cutoff"    : 80,
    "nr_enabled"   : False,
    "nr_strength"  : 0.75,
    "noise_profile": None,        # np.ndarray captured by MicConfigPanel
}

# ── Global VAD singleton — lazily created, reconfigured by MicConfigPanel ─────
_global_vad = None

def _get_vad():
    global _global_vad
    if _global_vad is not None:
        return _global_vad
    try:
        from spoaken_vad import VAD
        _global_vad = VAD(
            aggressiveness = _mic_config.get("vad_agg", 2),
            min_speech_ms  = _mic_config.get("min_speech", 200),
            silence_gap_ms = _mic_config.get("silence_gap", 500),
        )
    except Exception as exc:
        print(f"[Connect]: VAD init failed — {exc}", file=sys.stderr)
        _global_vad = None
    return _global_vad

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

# ── HappyTransformer (T5 grammar) — LOCAL CACHE ONLY ─────────────────────────
# After install, grammar correction runs entirely offline from the cached model.
# HappyTransformer will NEVER download weights at runtime.  To cache a model,
# use the Update & Repair window (⬇ Download & Cache) while online.
#
# _happy_ok   = package is importable
# _happy_cached = a pre-downloaded model exists in HAPPY_DIR
_happy_ok     = False
_happy_cached = False
HappyTextToText = TTSettings = None

_T5_HF_NAME   = "prithivida/grammar_error_correcter_v1"
_T5_CACHE_DIR = HAPPY_DIR
_T5_HUB_DIR   = HAPPY_DIR / "hub" / "models--prithivida--grammar_error_correcter_v1"

# Grammar is only usable when the package is present AND a local cache exists.
# When HAPPY_ONLINE_ONLY is True (default) we never fall back to a live HF download.
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

# Detect if a locally cached model exists (any T5 hub folder counts).
if _happy_ok:
    _hub_root = HAPPY_DIR / "hub"
    _happy_cached = _T5_HUB_DIR.is_dir() or (
        _hub_root.is_dir()
        and any(p.is_dir() for p in _hub_root.iterdir() if p.name.startswith("models--"))
    )
    if not _happy_cached:
        print(
            "[Connect]: No local T5 model cache found — grammar correction disabled.\n"
            "  To enable: open Update & Repair → T5 Models → Download & Cache (requires internet).",
            file=sys.stderr,
        )

# _T5_SOURCE: always point to the local hub directory; never use the bare HF name.
# This prevents any implicit HuggingFace download at load time.
_T5_SOURCE = str(_T5_HUB_DIR) if _T5_HUB_DIR.is_dir() else None

# ── Deep-translator (optional translate command) — ONLINE ONLY ───────────────
# deep_translator sends text to Google's cloud API — it requires internet.
# Import is still attempted so the module is usable when online, but translate_text()
# will return None immediately if the device is offline.
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
    """
    Full audio processing pipeline (runs in the capture callback):
      1. EQ / high-pass filter   — cuts fan/HVAC low-frequency rumble
      2. Noise reduction         — spectral noisereduce (optional)

    Does NOT apply the VAD gate — use audio_gate() for that so the
    controller decides whether to enqueue the chunk.
    """
    cfg = _mic_config
    arr = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)

    # ── 1. EQ filtering ───────────────────────────────────────────────────────
    profile = cfg.get("eq_profile", "speech")
    if profile != "flat":
        try:
            from scipy.signal import butter, sosfilt, iirnotch, tf2sos
            x = arr / 32768.0
            if profile in ("speech", "custom"):
                cutoff = float(cfg.get("hp_cutoff", 80)) if profile == "custom" else 80.0
                sos = butter(4, cutoff / (sr / 2), btype="high", output="sos")
                x   = sosfilt(sos, x)
            elif profile == "aggressive":
                sos = butter(5, 100.0 / (sr / 2), btype="high", output="sos")
                x   = sosfilt(sos, x)
                for freq in (60.0, 120.0):
                    b, a = iirnotch(freq, 30.0, sr)
                    x    = sosfilt(tf2sos(b, a), x)
            arr = np.clip(x * 32768.0, -32768, 32767).astype(np.float32)
        except ImportError:
            pass   # scipy not installed — EQ silently skipped
        except Exception:
            pass

    # ── 2. Noise reduction ────────────────────────────────────────────────────
    nr_on = cfg.get("nr_enabled", False) or NOISE_SUPPRESSION
    if nr_on and _NR_AVAILABLE:
        try:
            strength    = cfg.get("nr_strength", 0.75)
            noise_prof  = cfg.get("noise_profile", None)
            if noise_prof is not None:
                y_noise = (noise_prof * 32768.0).astype(np.float32)
                arr = nr.reduce_noise(y=arr, y_noise=y_noise, sr=sr,
                                      prop_decrease=strength, stationary=True)
            else:
                arr = nr.reduce_noise(y=arr, sr=sr,
                                      prop_decrease=strength, stationary=True)
        except Exception:
            pass

    return np.clip(arr, -32768, 32767).astype(np.int16).tobytes()


def audio_gate(audio_bytes: bytes) -> bytes | None:
    """
    VAD gate — returns audio_bytes if speech is detected, None if silence.
    Reads _mic_config["vad_enabled"]; falls back to passing everything if
    VAD is disabled or unavailable.
    """
    if not _mic_config.get("vad_enabled", True):
        return audio_bytes
    vad = _get_vad()
    if vad is None:
        return audio_bytes
    return vad.process(audio_bytes)


def reset_vad():
    """Reset VAD state — call at the start of each recording session."""
    vad = _get_vad()
    if vad:
        vad.reset()


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
    Translate text to target_lang via Google Translate.

    Requires internet access — returns None immediately when offline.
    target_lang may be a language name (fuzzy-matched) or an ISO code.
    """
    if not _translate_ok:
        return None
    # Online check — avoid sending data to Google when offline
    try:
        from spoaken_config import is_online
        if not is_online():
            return None
    except Exception:
        pass
    try:
        from rapidfuzz import process
        lang_lower = target_lang.lower().strip()
        code = _LANG_MAP.get(lang_lower)
        if code is None:
            m = process.extractOne(lang_lower, list(_LANG_MAP.keys()), score_cutoff=60)
            if m:
                code = _LANG_MAP[m[0]]
        if code is None:
            code = lang_lower[:5]
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

    def __init__(self, vosk_model: str | None = None, status_callback=None):
        """
        vosk_model      : Vosk model folder name (or None if Vosk disabled).
        status_callback : Optional callable(progress: float, text: str) —
                          used to update the splash screen during loading.
        """

        # ── Vosk ──────────────────────────────────────────────────────────────
        self.small_model = None

        if _vosk_ok and vosk_model:
            try:
                self.small_model = VoskModel(_resolve_vosk(vosk_model))
            except FileNotFoundError as exc:
                print(exc, file=sys.stderr)

        # ── Whisper ───────────────────────────────────────────────────────────
        self.whisper_model = None
        if _whisper_ok:
            try:
                device       = "cuda" if GPU_ENABLED else "cpu"
                compute_type = _resolve_compute_type(WHISPER_COMPUTE, GPU_ENABLED)

                # Tell the splash whether we are downloading or loading from cache
                if status_callback:
                    if WHISPER_MODEL in scan_installed_whisper_models():
                        status_callback(0.90, f"Loading Whisper ({WHISPER_MODEL}) …")
                    else:
                        status_callback(0.90, f"Downloading Whisper ({WHISPER_MODEL}) — please wait …")

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
        self.whisper_queue = Queue()   # → whisper_loop

        # Legacy alias kept so controller code that references model.audio_queue
        # still compiles; route it to vosk_queue.
        self.audio_queue   = self.vosk_queue

        self.is_running  = False
        self.data_store  = []          # Vosk confirmed sentences
        self.whisper_store = []        # Whisper final sentences

    # ── Background loader (T5 grammar model) ──────────────────────────────────
    # Only loads from the local cache written by Update & Repair.
    # Will never trigger a HuggingFace download at runtime.

    def _background_load(self):
        if not (_happy_ok and _happy_cached and _T5_SOURCE):
            return   # no local cache — grammar correction unavailable offline
        try:
            os.environ["TRANSFORMERS_CACHE"] = str(_T5_CACHE_DIR)
            # TRANSFORMERS_OFFLINE=1 prevents any implicit network access by transformers
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
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
