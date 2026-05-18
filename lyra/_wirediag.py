"""Wire-cadence / main-thread-stall diagnostics
(env-gated: ``LYRA_WIRE_DEBUG=1``).

Zero cost when the env var is unset (one bool check).  Exists
because the ``un``/``ov`` TX-audio counters are STRUCTURALLY BLIND
to the dominant glitch mode: a system GPU/compositor event (open a
browser, launch a GPU app, drag a window between monitors) blocks
the Qt main thread, holds the GIL, and freezes the whole Python
chain -- producer AND the EP2 writer -- *coherently*.  The deque
never under/overflows (nothing drains or fills it), so un/ov stay
0/green while the HL2 still sees a wire-cadence gap (click on the
freeze, AGC-overshoot "volume slam" on the unfreeze).

This instrument measures the thing the counter cannot see:
  * the max EP2 inter-send gap (always-on, surfaced live in the
    status-bar telemetry as ``gap=NNms`` -- the non-blind readout);
  * detailed per-event console logging + a Qt main-thread stall
    detector (env-gated, off by default).

Run with::

    set LYRA_WIRE_DEBUG=1
    python -u -m lyra.ui.app > "%USERPROFILE%\\lyra_wire.log" 2>&1

then reproduce the trigger (open Chrome / drag the window across
monitors) and read the [WIRE]/[MAINSTALL] lines.
"""
from __future__ import annotations

import os
import sys
import time

_ON = bool(os.environ.get("LYRA_WIRE_DEBUG"))

# A wire inter-send gap above this is a stall, not normal cadence
# (nominal lockstep ~2.6 ms; worst normal DSP-block gap ~11 ms).
WIRE_GAP_LOG_MS = 25.0
# Qt main-thread timer lateness above this = a real main-thread
# freeze (the 20 ms probe firing >50 ms late).
MAIN_STALL_LOG_MS = 50.0

_last: dict[str, float] = {}


def wire_debug_on() -> bool:
    """True when LYRA_WIRE_DEBUG is set (cheap; for guard sites)."""
    return _ON


def wiredbg(msg: str, *, every_s: float | None = None,
            key: str = "") -> None:
    """Print one ``[WIRE]`` line to stdout (flushed) when
    ``LYRA_WIRE_DEBUG`` is set.  ``every_s`` rate-limits a hot
    call site (keyed by ``key``) so it can't flood.
    """
    if not _ON:
        return
    if every_s is not None:
        now = time.monotonic()
        if now - _last.get(key, 0.0) < every_s:
            return
        _last[key] = now
    print(f"[WIRE] {msg}", file=sys.stdout, flush=True)
