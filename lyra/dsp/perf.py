"""DSP performance instrumentation — Phase 1a of the GPU-FFT plan.

Why this exists
---------------
Before we can credibly say "GPU FFT is faster than CPU FFT", we need
real measurements from real machines. This module gives us a tiny,
always-on-but-cheap timing primitive that the FFT path (and later, NR
/ demod / etc.) can sprinkle into hot loops to capture:

- per-operation duration (ms)
- EWMA-smoothed average (so the displayed numbers don't jitter)
- min/max over a rolling window (catches transient spikes)
- frame rate (operations per second) the loop is actually hitting

The data feeds a small status-bar overlay (toggled via View menu)
so the operator can SEE what their machine is doing without any
profiler / log-trawling. Testers can screenshot the readout and
attach it to a bug report — that's the baseline data set we'll
compare GPU runs against in Phase 1d.

Design notes
------------
- **Disabled-state cost is one bool check.** The intent is for perf
  instrumentation to ship enabled in dev builds and OFF by default in
  the released installer; operators turn it on via View menu when
  they want to see it. The hot path's overhead when off must be
  effectively zero — a single attribute read.
- **No external deps.** stdlib `time.perf_counter` only. We do NOT
  depend on numpy or psutil here so this module loads cleanly even
  in the most-stripped runtime configurations.
- **Per-name registry.** Multiple call sites (FFT proper, full
  tick, demod, NR, …) each get their own named PerfTimer. The
  registry lets the UI emit a single snapshot dict containing all
  active timers without each site having to wire its own signal.

Future expansions (after Phase 1):
- Per-backend tagging once VulkanFFTBackend lands (so the UI can
  show "FFT (vulkan): 0.3 ms" vs "FFT (numpy): 2.3 ms" side-by-side
  during the A/B comparison phase).
- Histogram export for offline analysis.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class PerfSnapshot:
    """Read-only point-in-time view of one PerfTimer's stats.

    All durations in milliseconds; rate in operations per second.
    Used for emission across Qt signals — Qt marshals dataclasses
    cleanly when wrapped in a dict.
    """
    name: str
    last_ms: float        # most recent single-call duration
    avg_ms: float         # EWMA-smoothed average (alpha=0.1)
    min_ms: float         # min over rolling window
    max_ms: float         # max over rolling window
    rate_hz: float        # operations / second (last 1 second)
    count: int            # total ops since timer created

    def to_dict(self) -> dict:
        return {
            "name":    self.name,
            "last_ms": self.last_ms,
            "avg_ms":  self.avg_ms,
            "min_ms":  self.min_ms,
            "max_ms":  self.max_ms,
            "rate_hz": self.rate_hz,
            "count":   self.count,
        }


class PerfTimer:
    """Lightweight rolling timer for one named operation.

    Usage:
        timer = PerfTimer("fft")
        with timer:                    # context-manager form
            np.fft.fft(buf)
        # or:
        t0 = time.perf_counter()
        np.fft.fft(buf)
        timer.observe_ms((time.perf_counter() - t0) * 1000.0)

    Both forms are cheap. Context-manager form has the trivial
    overhead of __enter__/__exit__; the observe_ms form is one
    function call. Either is fine in a 6-30 Hz loop.
    """

    # Keep the last N raw samples for min/max. 60 samples ≈ 2 sec
    # at 30 Hz, ≈ 10 sec at 6 Hz — both comfortable for spotting
    # transient spikes without noise from ancient data.
    _WINDOW = 60

    # EWMA coefficient. 0.1 gives a ~10-sample time constant —
    # smooth enough to be readable, fast enough to react to a real
    # change in load.
    _EWMA_ALPHA = 0.1

    def __init__(self, name: str):
        self.name = name
        self.count = 0
        self._last_ms = 0.0
        self._avg_ms = 0.0
        self._samples: List[float] = []        # rolling window
        self._rate_window: List[float] = []    # timestamps for FPS calc
        self._enter_t: float = 0.0

    # ── Context manager form ───────────────────────────────────────
    def __enter__(self):
        self._enter_t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.observe_ms((time.perf_counter() - self._enter_t) * 1000.0)
        return False

    # ── Direct observation ─────────────────────────────────────────
    def observe_ms(self, dt_ms: float) -> None:
        """Record a single operation's duration (already in ms)."""
        self.count += 1
        self._last_ms = dt_ms
        # EWMA — first sample seeds the average so we don't bias from 0.
        if self.count == 1:
            self._avg_ms = dt_ms
        else:
            self._avg_ms = ((1.0 - self._EWMA_ALPHA) * self._avg_ms
                            + self._EWMA_ALPHA * dt_ms)
        # Rolling window for min/max.
        self._samples.append(dt_ms)
        if len(self._samples) > self._WINDOW:
            self._samples.pop(0)
        # Rate calc — keep timestamps from the last 1 second.
        now = time.perf_counter()
        self._rate_window.append(now)
        cutoff = now - 1.0
        # Drop everything older than 1 second. Walk from front; rare
        # to have many entries (limited by FFT loop rate ≤ 60 Hz).
        while self._rate_window and self._rate_window[0] < cutoff:
            self._rate_window.pop(0)

    # ── Snapshot for UI / signal emission ──────────────────────────
    def snapshot(self) -> PerfSnapshot:
        if self._samples:
            mn = min(self._samples)
            mx = max(self._samples)
        else:
            mn = mx = 0.0
        return PerfSnapshot(
            name=self.name,
            last_ms=self._last_ms,
            avg_ms=self._avg_ms,
            min_ms=mn,
            max_ms=mx,
            rate_hz=float(len(self._rate_window)),
            count=self.count,
        )

    def reset(self) -> None:
        """Wipe rolling state — used when the operator changes FFT
        size, or switches backends, so the new measurements aren't
        contaminated by averages from the previous configuration."""
        self.count = 0
        self._last_ms = 0.0
        self._avg_ms = 0.0
        self._samples.clear()
        self._rate_window.clear()


# ── Process-wide registry ──────────────────────────────────────────
# Multiple call sites register their PerfTimers here so the UI can
# emit a single snapshot dict containing everything active. Keeping
# it simple: a flat dict keyed by timer name. Names should be stable
# (e.g. "fft", "tick", "demod") so the UI can show consistent labels.

_REGISTRY: Dict[str, PerfTimer] = {}


def get_or_create(name: str) -> PerfTimer:
    """Return the named PerfTimer, creating it if missing.

    Idempotent — safe to call repeatedly from a hot path. Most
    callers should grab the reference once at __init__ and store
    it on the instance to skip the dict lookup per call.
    """
    t = _REGISTRY.get(name)
    if t is None:
        t = PerfTimer(name)
        _REGISTRY[name] = t
    return t


def snapshot_all() -> Dict[str, dict]:
    """Snapshot every registered timer as a dict-of-dicts. Used by
    the UI to push a single signal payload to the status-bar
    overlay, not one signal per timer."""
    return {name: t.snapshot().to_dict() for name, t in _REGISTRY.items()}


def reset_all() -> None:
    """Clear all rolling state — used when FFT size or backend
    changes, so the displayed averages aren't a confusing mix of
    before/after measurements."""
    for t in _REGISTRY.values():
        t.reset()


# ── Process-wide enable flag ───────────────────────────────────────
# Used by call sites in radio.py AND in ui/spectrum.py (paint timers)
# so a single toggle in the View menu instruments BOTH the FFT loop
# and the panadapter paint chain. A direct module attribute is cheaper
# to read than a function call — important for hot paths like
# paintEvent which can fire 30+ times per second per widget.

enabled: bool = False


def set_enabled(on: bool) -> None:
    """Flip the process-wide instrumentation flag. Resets all rolling
    timers on the off→on transition so newly-displayed averages aren't
    contaminated by stale data from a previous enabled period."""
    global enabled
    if bool(on) == enabled:
        return
    if on:
        reset_all()
    enabled = bool(on)


def is_enabled() -> bool:
    """Function-call form of `enabled` for callers who don't want to
    import the module attribute directly."""
    return enabled
