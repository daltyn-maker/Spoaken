"""
spoaken_vad.py
──────────────
Voice Activity Detection for Spoaken.

Backends (preference order)
────────────────────────────
  1. webrtcvad  — Google WebRTC VAD. Fast, frame-level (30 ms).
                  pip install webrtcvad
  2. Energy gate — pure numpy fallback. No extra dependencies.

Stateful gate bridges short silence gaps so natural pauses don't
fragment sentences, and debounces single noise pops so a fan spike
doesn't trigger a transcription run.

Public API
──────────
  VAD(aggressiveness=2, sample_rate=16000,
      min_speech_ms=200, silence_gap_ms=500)
  .process(pcm_bytes) → bytes | None   — stateful gate
  .is_speech(pcm_bytes) → bool         — quick non-stateful check
  .set_aggressiveness(0–3)
  .set_energy_threshold(rms)
  .set_min_speech(ms)
  .set_silence_gap(ms)
  .reset()
"""

from __future__ import annotations
import sys
import numpy as np

try:
    import webrtcvad as _wrtcvad
    _WEBRTC_OK = True
except ImportError:
    _wrtcvad   = None
    _WEBRTC_OK = False

_FRAME_MS      = 30
_SAMPLE_RATE   = 16000
_FRAME_SAMPLES = _SAMPLE_RATE * _FRAME_MS // 1000   # 480
_FRAME_BYTES   = _FRAME_SAMPLES * 2                  # 960 bytes (int16)


class VAD:
    """Stateful VAD with hysteresis — bridges pauses, debounces noise bursts."""

    def __init__(
        self,
        aggressiveness  : int   = 2,
        sample_rate     : int   = 16000,
        min_speech_ms   : int   = 200,
        silence_gap_ms  : int   = 500,
        energy_threshold: float = 0.015,
    ):
        self._sr           = sample_rate
        self._min_speech   = min_speech_ms
        self._silence_gap  = silence_gap_ms
        self._energy_thr   = energy_threshold
        self._aggressiveness = aggressiveness

        self._vad = None
        if _WEBRTC_OK:
            try:
                self._vad = _wrtcvad.Vad(aggressiveness)
            except Exception as exc:
                print(f"[VAD]: webrtcvad init failed — {exc}", file=sys.stderr)

        self._speech_accum  = 0
        self._silence_accum = 0
        self._gate_open     = False
        self._leftover      = b""

        if not _WEBRTC_OK:
            print("[VAD]: webrtcvad not installed — energy gate active.\n"
                  "  Better VAD:  pip install webrtcvad", file=sys.stderr)

    # ── Config ────────────────────────────────────────────────────────────────

    def set_aggressiveness(self, level: int):
        level = max(0, min(3, int(level)))
        self._aggressiveness = level
        if _WEBRTC_OK:
            try: self._vad = _wrtcvad.Vad(level)
            except Exception: pass

    def set_energy_threshold(self, rms: float):
        self._energy_thr = max(0.001, rms)

    def set_min_speech(self, ms: int):
        self._min_speech = max(0, ms)

    def set_silence_gap(self, ms: int):
        self._silence_gap = max(50, ms)

    @property
    def backend(self) -> str:
        return "webrtcvad" if self._vad else "energy"

    @property
    def gate_open(self) -> bool:
        return self._gate_open

    # ── Frame-level detection ─────────────────────────────────────────────────

    def _frame_is_speech(self, frame: bytes) -> bool:
        if self._vad:
            try: return self._vad.is_speech(frame, self._sr)
            except Exception: pass
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(arr ** 2))) / 32768.0 > self._energy_thr

    # ── Public ────────────────────────────────────────────────────────────────

    def is_speech(self, pcm_bytes: bytes) -> bool:
        """Quick non-stateful check — for UI meters."""
        if len(pcm_bytes) < _FRAME_BYTES:
            arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
            return float(np.sqrt(np.mean(arr ** 2))) / 32768.0 > self._energy_thr
        return self._frame_is_speech(pcm_bytes[:_FRAME_BYTES])

    def process(self, pcm_bytes: bytes) -> bytes | None:
        """
        Stateful gate.
        Gate opens after min_speech_ms of continuous speech.
        Gate closes after silence_gap_ms of continuous silence.
        Returns pcm_bytes when open, None when closed.
        """
        buf = self._leftover + pcm_bytes
        self._leftover = b""
        pos = 0

        while pos + _FRAME_BYTES <= len(buf):
            frame    = buf[pos : pos + _FRAME_BYTES]
            speaking = self._frame_is_speech(frame)
            pos     += _FRAME_BYTES

            if speaking:
                self._speech_accum  += _FRAME_MS
                self._silence_accum  = 0
                if not self._gate_open and self._speech_accum >= self._min_speech:
                    self._gate_open = True
            else:
                self._silence_accum += _FRAME_MS
                self._speech_accum   = 0
                if self._gate_open and self._silence_accum >= self._silence_gap:
                    self._gate_open = False

        self._leftover = buf[pos:]
        return pcm_bytes if self._gate_open else None

    def reset(self):
        self._speech_accum  = 0
        self._silence_accum = 0
        self._gate_open     = False
        self._leftover      = b""
