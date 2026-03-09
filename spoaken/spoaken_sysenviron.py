"""
spoaken_sysenviron.py
─────────────────────
System environment watchdog for Spoaken.

Startup benchmark
─────────────────
  On start(), a quick benchmark runs in the background (~0.5–1 s total):
    1. Single-core CPU throughput test (integer ops in 0.25 s)
    2. Available RAM headroom (psutil virtual_memory)
    3. Idle CPU headroom (0.5 s psutil measurement)

  Result: a machine_tier ("fast" | "medium" | "slow" | "very_slow") and a
  calibrated llm_chunk_words budget — how many transcript words the LLM
  background worker may consume per pass without starving Vosk/Whisper.

  Budget tiers:
    fast       → 150 words / pass
    medium     →  80 words / pass
    slow       →  40 words / pass
    very_slow  →  20 words / pass  (LLM background strongly discouraged)

  The budget dynamically shrinks if live CPU rises above WARN_CPU_PCT.

Runtime watchdog
────────────────
  Polls CPU + RAM every 5 s (3-sample rolling average).
  Cascade fallback on overload:
    ≥ 80 % CPU / 85 % RAM → console warning
    ≥ 92 % CPU / 93 % RAM → disable LLM background, show plane.png alert
    ≥ 96 % CPU            → disable Whisper
    ≥ 99 % CPU            → disable Vosk (last resort)
  Each level re-enables automatically when load drops 8 points.

Dependencies
────────────
  psutil  (pip install psutil) — soft optional; gracefully disables if absent.

Public API
──────────
  SysEnviron(controller)
  .start()                   — benchmark then begin monitoring
  .stop()                    — stop background thread
  .can_run_llm() → bool      — safe to fire an LLM job right now?
  .get_llm_chunk_budget() → int  — calibrated words-per-pass limit
  .get_stats() → dict        — {cpu_pct, ram_pct, status, machine_tier,
                                 chunk_budget, benchmark_done}
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque

# ── Runtime thresholds ────────────────────────────────────────────────────────
WARN_CPU_PCT     = 80
CRITICAL_CPU_PCT = 92
DISABLE_WHISPER  = 96
DISABLE_VOSK     = 99

WARN_RAM_PCT     = 85
CRITICAL_RAM_PCT = 93

SAMPLE_INTERVAL  = 5      # seconds between watchdog polls
SMOOTHING        = 3      # rolling-average window

# ── LLM chunk budgets per tier (words per background pass) ────────────────────
_TIER_BUDGET = {
    "fast"      : 150,
    "medium"    :  80,
    "slow"      :  40,
    "very_slow" :  20,
}

# ── Benchmark tuning knobs ────────────────────────────────────────────────────
_BENCH_SECS       = 0.25    # how long to spin the CPU loop
_OPS_FAST         = 6_000_000
_OPS_MEDIUM       = 2_500_000
_OPS_SLOW         =   800_000
_MIN_FREE_RAM_MB  = 800      # below this → very_slow regardless of CPU


class SysEnviron:
    """Background CPU/RAM watchdog with startup calibration."""

    def __init__(self, controller=None):
        self._ctrl   = controller
        self._running = False
        self._thread : threading.Thread | None = None

        self._cpu_samples : deque[float] = deque(maxlen=SMOOTHING)
        self._ram_samples : deque[float] = deque(maxlen=SMOOTHING)

        self._last_cpu = 0.0
        self._last_ram = 0.0
        self._status   = "starting"

        # Benchmark results
        self._machine_tier   : str  = "medium"
        self._chunk_budget   : int  = 80
        self._benchmark_done : bool = False

        # Auto-disable tracking
        self._llm_auto_disabled     = False
        self._whisper_auto_disabled = False
        self._vosk_auto_disabled    = False

        # Alert throttle
        self._last_alert_time = 0.0
        self._alert_interval  = 60.0   # seconds between GUI popups

        self._psutil_ok = self._init_psutil()

    # ─────────────────────────────────────────────────────────────────────────

    def _init_psutil(self) -> bool:
        try:
            import psutil  # noqa: F401
            return True
        except ImportError:
            print(
                "[SysEnviron]: psutil not installed — load monitoring disabled.\n"
                "  Fix:  pip install psutil",
                file=sys.stderr,
            )
            return False

    def start(self):
        if not self._psutil_ok:
            self._status = "no_psutil"
            return
        self._running = True
        self._thread  = threading.Thread(target=self._main_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def can_run_llm(self) -> bool:
        """True if it's safe to start an LLM background job right now."""
        if not self._psutil_ok:
            return True
        return (
            self._last_cpu < CRITICAL_CPU_PCT
            and self._last_ram < CRITICAL_RAM_PCT
            and not self._llm_auto_disabled
        )

    def get_llm_chunk_budget(self) -> int:
        """
        Calibrated words-per-pass for the LLM background worker.
        Shrinks linearly between WARN and CRITICAL CPU levels.
        """
        base = self._chunk_budget
        if not self._psutil_ok:
            return base
        cpu = self._last_cpu
        if cpu >= WARN_CPU_PCT:
            # Scale from 100 % at WARN down to 50 % at CRITICAL
            span  = CRITICAL_CPU_PCT - WARN_CPU_PCT
            ratio = max(0.5, 1.0 - (cpu - WARN_CPU_PCT) / span * 0.5)
            return max(10, int(base * ratio))
        return base

    def get_stats(self) -> dict:
        return {
            "cpu_pct"        : round(self._last_cpu, 1),
            "ram_pct"        : round(self._last_ram, 1),
            "status"         : self._status,
            "machine_tier"   : self._machine_tier,
            "chunk_budget"   : self.get_llm_chunk_budget(),
            "benchmark_done" : self._benchmark_done,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Startup benchmark
    # ─────────────────────────────────────────────────────────────────────────

    def _run_benchmark(self):
        """
        Measures single-core CPU throughput, free RAM, and idle headroom.
        Runs once on the daemon thread before the watchdog loop starts.
        Takes ~0.75–1.25 s total.
        """
        import psutil

        self._status = "benchmarking"
        self._console("[SysEnviron]: running startup benchmark …")

        # 1. CPU throughput — tight integer loop for _BENCH_SECS
        t0, ops, x = time.perf_counter(), 0, 0
        while time.perf_counter() - t0 < _BENCH_SECS:
            x = (x + 1) * 3 % 997
            ops += 1
        ops_per_sec = int(ops / _BENCH_SECS)

        # 2. Free RAM
        vm      = psutil.virtual_memory()
        free_mb = vm.available / (1024 * 1024)

        # 3. Idle CPU headroom  (0.5 s blocking measurement)
        psutil.cpu_percent(interval=None)      # prime the counter
        time.sleep(0.5)
        idle_cpu = psutil.cpu_percent(interval=0.5)
        headroom = max(0.0, 100.0 - idle_cpu)

        self._console(
            f"[SysEnviron]: bench — ops/s={ops_per_sec:,}  "
            f"free_RAM={free_mb:.0f} MB  CPU_headroom={headroom:.0f}%"
        )

        # 4. Classify tier
        if free_mb < _MIN_FREE_RAM_MB:
            tier = "very_slow"
        elif ops_per_sec >= _OPS_FAST and headroom >= 40:
            tier = "fast"
        elif ops_per_sec >= _OPS_MEDIUM and headroom >= 25:
            tier = "medium"
        elif ops_per_sec >= _OPS_SLOW and headroom >= 15:
            tier = "slow"
        else:
            tier = "very_slow"

        # Headroom override — fast CPU but already busy
        if headroom < 15 and tier not in ("slow", "very_slow"):
            tier = "slow"
            self._console("[SysEnviron]: low CPU headroom → downgraded tier to 'slow'")

        self._machine_tier   = tier
        self._chunk_budget   = _TIER_BUDGET[tier]
        self._benchmark_done = True
        self._status         = "ok"

        self._console(
            f"[SysEnviron]: tier={tier}  "
            f"LLM chunk budget={self._chunk_budget} words/pass"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Main thread: benchmark → watchdog loop
    # ─────────────────────────────────────────────────────────────────────────

    def _main_loop(self):
        try:
            self._run_benchmark()
        except Exception as exc:
            print(f"[SysEnviron]: benchmark error — {exc}", file=sys.stderr)
            self._status = "ok"

        import psutil
        psutil.cpu_percent(interval=None)   # warm up watchdog counter
        time.sleep(1)

        while self._running:
            try:
                cpu = psutil.cpu_percent(interval=None)
                ram = psutil.virtual_memory().percent

                self._cpu_samples.append(cpu)
                self._ram_samples.append(ram)

                avg_cpu = sum(self._cpu_samples) / len(self._cpu_samples)
                avg_ram = sum(self._ram_samples) / len(self._ram_samples)

                self._last_cpu = avg_cpu
                self._last_ram = avg_ram

                self._evaluate(avg_cpu, avg_ram)

            except Exception as exc:
                print(f"[SysEnviron]: poll error — {exc}", file=sys.stderr)

            time.sleep(SAMPLE_INTERVAL)

    # ─────────────────────────────────────────────────────────────────────────
    # Watchdog evaluation — cascade fallback
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate(self, cpu: float, ram: float):
        # ── Warning level ──────────────────────────────────────────────────────
        if cpu >= WARN_CPU_PCT or ram >= WARN_RAM_PCT:
            self._console(
                f"[SysEnviron]: high load — CPU {cpu:.0f}%  RAM {ram:.0f}%"
            )

        # ── Critical: disable LLM ─────────────────────────────────────────────
        if (cpu >= CRITICAL_CPU_PCT or ram >= CRITICAL_RAM_PCT) and not self._llm_auto_disabled:
            self._llm_auto_disabled = True
            self._status = "overloaded_llm_off"
            msg = (
                f"[SysEnviron ⚠]: CPU {cpu:.0f}% / RAM {ram:.0f}% — "
                "LLM background disabled."
            )
            self._console(msg)
            self._set_llm(False)
            self._show_alert(cpu, ram, "LLM background disabled")

        elif (cpu < CRITICAL_CPU_PCT - 8 and ram < CRITICAL_RAM_PCT - 8
              and self._llm_auto_disabled):
            self._llm_auto_disabled = False
            self._status = "ok"
            self._console("[SysEnviron]: load normalised — LLM re-enabled.")
            self._set_llm(True)

        # ── Severe: disable Whisper ───────────────────────────────────────────
        if cpu >= DISABLE_WHISPER and not self._whisper_auto_disabled:
            self._whisper_auto_disabled = True
            self._status = "overloaded_whisper_off"
            self._console(
                f"[SysEnviron ⚠⚠]: CPU {cpu:.0f}% — Whisper disabled."
            )
            self._set_engine("whisper", False)
            self._show_alert(cpu, ram, "Whisper disabled")

        elif cpu < DISABLE_WHISPER - 8 and self._whisper_auto_disabled:
            self._whisper_auto_disabled = False
            self._console("[SysEnviron]: Whisper re-enabled.")
            self._set_engine("whisper", True)

        # ── Critical: disable Vosk ────────────────────────────────────────────
        if cpu >= DISABLE_VOSK and not self._vosk_auto_disabled:
            self._vosk_auto_disabled = True
            self._status = "overloaded_vosk_off"
            self._console(
                f"[SysEnviron 🛑]: CPU {cpu:.0f}% — Vosk disabled (critical)."
            )
            self._set_engine("vosk", False)
            self._show_alert(cpu, ram, "Vosk disabled — system critical")

        elif cpu < DISABLE_VOSK - 8 and self._vosk_auto_disabled:
            self._vosk_auto_disabled = False
            self._console("[SysEnviron]: Vosk re-enabled.")
            self._set_engine("vosk", True)

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _console(self, msg: str):
        print(msg, file=sys.stderr)
        ctrl = self._ctrl
        if ctrl and hasattr(ctrl, "view") and ctrl.view:
            try:
                ctrl.view.thread_safety_console(msg)
            except Exception:
                pass

    def _set_llm(self, enabled: bool):
        ctrl = self._ctrl
        if ctrl:
            try:
                ctrl.set_llm_enabled(enabled)
            except Exception:
                pass

    def _set_engine(self, engine: str, enabled: bool):
        ctrl = self._ctrl
        if ctrl:
            try:
                ctrl.set_engine_enabled(engine, enabled)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # GUI alert (throttled, plane.png)
    # ─────────────────────────────────────────────────────────────────────────

    def _show_alert(self, cpu: float, ram: float, action: str):
        now = time.time()
        if now - self._last_alert_time < self._alert_interval:
            return
        self._last_alert_time = now

        ctrl = self._ctrl
        if not ctrl or not hasattr(ctrl, "view") or not ctrl.view:
            return

        # Capture snapshot of stats for the popup
        tier   = self._machine_tier
        budget = self.get_llm_chunk_budget()

        def _popup():
            try:
                import customtkinter as ctk
                from PIL import Image
                from paths import ART_DIR

                dlg = ctk.CTkToplevel(ctrl.view)
                dlg.title("Spoaken — System Load Warning")
                dlg.resizable(False, False)
                dlg.configure(fg_color="#060c1a")
                dlg.geometry("370x280")

                plane_img  = None
                plane_path = ART_DIR / "plane.gif"
                if plane_path.exists():
                    try:
                        _img = Image.open(plane_path).resize((80, 80), Image.LANCZOS)
                        plane_img = ctk.CTkImage(
                            light_image=_img, dark_image=_img, size=(80, 80)
                        )
                    except Exception:
                        pass

                frm = ctk.CTkFrame(
                    dlg, fg_color="#0a1128",
                    border_color="#1a2d60", border_width=1, corner_radius=10,
                )
                frm.pack(fill="both", expand=True, padx=8, pady=8)

                if plane_img:
                    ctk.CTkLabel(frm, image=plane_img, text="").pack(pady=(14, 4))
                else:
                    ctk.CTkLabel(
                        frm, text="✈",
                        font=("Segoe UI", 36), text_color="#d4aa00",
                    ).pack(pady=(14, 4))

                ctk.CTkLabel(
                    frm, text="System Under Heavy Load",
                    font=("Segoe UI Semibold", 13), text_color="#d4aa00",
                ).pack(pady=(0, 4))

                ctk.CTkLabel(
                    frm,
                    text=(
                        f"CPU {cpu:.0f}%  ·  RAM {ram:.0f}%\n"
                        f"Action: {action}\n\n"
                        f"Machine tier: {tier}  ·  "
                        f"LLM budget: {budget} words/pass"
                    ),
                    font=("Segoe UI", 10), text_color="#007bff",
                    justify="center",
                ).pack(pady=(0, 10))

                ctk.CTkButton(
                    frm, text="OK", width=100,
                    fg_color="#1a2d60", hover_color="#2545a8",
                    text_color="#00bdff",
                    command=dlg.destroy,
                ).pack(pady=(0, 14))

                dlg.after(15_000, lambda: dlg.destroy() if dlg.winfo_exists() else None)

            except Exception as exc:
                print(f"[SysEnviron]: alert popup failed — {exc}", file=sys.stderr)

        try:
            ctrl.view.after(0, _popup)
        except Exception:
            pass
