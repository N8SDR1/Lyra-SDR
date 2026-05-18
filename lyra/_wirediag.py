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
import re
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


# ---------------------------------------------------------------------------
# S0 — wire-cadence analysis helpers (PURE; off the wire path).
#
# These turn a captured `LYRA_WIRE_DEBUG` log (or a synthetic gap
# series) into an OBJECTIVE before/after yardstick for the S0..S7
# wire-egress rebuild.  Nominal lockstep cadence is ~2.6 ms; the
# proven pathology is a sustained 25-56 ms baseline.  Buckets are
# chosen around that: HEALTHY (<12 ms = within a DSP-block gap),
# MARGINAL (12-25), STALL (25-50), FENCE (>=50 = crosses the HL2
# keepalive fence).  S2 succeeds when the captured mass moves out
# of STALL/FENCE into HEALTHY.  No production behavior here.
# ---------------------------------------------------------------------------

_EP2_GAP_RE = re.compile(r"\[WIRE\]\s+EP2\s+gap=(\d+(?:\.\d+)?)ms")
_MAINSTALL_RE = re.compile(
    r"\[WIRE\]\s+MAINSTALL[^~]*~\s*(\d+(?:\.\d+)?)\s*ms")


def _pct(sorted_vals: list, q: float) -> float:
    """Nearest-rank percentile (q in 0..1); 0.0 for empty."""
    if not sorted_vals:
        return 0.0
    idx = int(round(q * (len(sorted_vals) - 1)))
    return float(sorted_vals[idx])


def bucket_gaps(samples_ms) -> dict:
    """Classify EP2 inter-send gaps into cadence-health buckets.

    Returns counts + max/p50/p95 + a ``healthy`` bool (the
    objective S2 gate: no STALL/FENCE samples and p95 < 12 ms).
    """
    vals = sorted(float(x) for x in samples_ms)
    b = {"<12": 0, "12-25": 0, "25-50": 0, ">=50": 0}
    for v in vals:
        if v < 12.0:
            b["<12"] += 1
        elif v < 25.0:
            b["12-25"] += 1
        elif v < 50.0:
            b["25-50"] += 1
        else:
            b[">=50"] += 1
    n = len(vals)
    p95 = _pct(vals, 0.95)
    return {
        "n": n,
        "buckets": b,
        "max_ms": (vals[-1] if vals else 0.0),
        "p50_ms": _pct(vals, 0.50),
        "p95_ms": p95,
        "stall_or_fence": b["25-50"] + b[">=50"],
        # The S2 success criterion, computed not eyeballed.
        "healthy": (n > 0 and b["25-50"] == 0 and b[">=50"] == 0
                    and p95 < 12.0),
    }


def parse_wire_log(text: str) -> dict:
    """Extract EP2 gap (ms) and MAINSTALL (ms) series from a
    captured ``[WIRE]`` log.  Pure text in -> numbers out, so an
    operator's before/after captures diff objectively.
    """
    gaps = [float(m) for m in _EP2_GAP_RE.findall(text)]
    stalls = [float(m) for m in _MAINSTALL_RE.findall(text)]
    return {"ep2_gaps_ms": gaps, "mainstalls_ms": stalls}


def summarize_capture(text: str) -> dict:
    """One-call before/after yardstick for an operator capture:
    parsed series + the gap health verdict + worst MAINSTALL.
    """
    parsed = parse_wire_log(text)
    summary = bucket_gaps(parsed["ep2_gaps_ms"])
    stalls = parsed["mainstalls_ms"]
    summary["mainstall_n"] = len(stalls)
    summary["mainstall_max_ms"] = max(stalls) if stalls else 0.0
    return summary
