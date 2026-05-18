"""S0 — wire-cadence analysis harness (pure; off the wire path).

Turns a captured LYRA_WIRE_DEBUG log (or a synthetic gap series)
into an OBJECTIVE before/after yardstick for the S0..S7 wire-egress
rebuild.  Codifies the proven-broken 2026-05-18 baseline so the
post-S2 operator capture can be asserted improved by computation,
not eyeball.  No production behavior is touched by S0.
"""
from __future__ import annotations

from lyra._wirediag import (
    bucket_gaps, parse_wire_log, summarize_capture)

# Verbatim excerpt of the operator's 2026-05-18 lyra_wire.log
# (the proven-broken BEFORE state: chronic 25-56 ms + a big
# startup MAINSTALL), used as the regression fixture.
_BEFORE_LOG = """\
[Radio] DSP worker thread started (queue depth 10)
[WIRE] MAINSTALL Qt main thread blocked ~2642ms (20ms probe fired that late) -- expect a matching EP2 gap with un/ov flat
[WIRE] EP2 gap=26ms  un=0 ov=0 deque=500 inject=True  <- counters flat across this gap = the blind-spot
[WIRE] EP2 gap=37ms  un=0 ov=0 deque=448 inject=True  <- counters flat across this gap = the blind-spot
[WIRE] EP2 gap=53ms  un=0 ov=0 deque=420 inject=True  <- counters flat across this gap = the blind-spot
[WIRE] MAINSTALL Qt main thread blocked ~268ms (20ms probe fired that late) -- expect a matching EP2 gap with un/ov flat
[WIRE] EP2 gap=51ms  un=0 ov=0 deque=468 inject=True  <- counters flat across this gap = the blind-spot
[WIRE] EP2 gap=29ms  un=0 ov=0 deque=446 inject=True  <- counters flat across this gap = the blind-spot
"""


def test_parse_extracts_gap_and_mainstall_series() -> None:
    p = parse_wire_log(_BEFORE_LOG)
    assert p["ep2_gaps_ms"] == [26.0, 37.0, 53.0, 51.0, 29.0]
    assert p["mainstalls_ms"] == [2642.0, 268.0]


def test_before_baseline_is_objectively_broken() -> None:
    s = summarize_capture(_BEFORE_LOG)
    assert s["healthy"] is False
    # Every gap is a STALL or FENCE sample (none < 25 ms).
    assert s["stall_or_fence"] == s["n"] == 5
    assert s["buckets"]["<12"] == 0
    assert s["max_ms"] == 53.0
    assert s["mainstall_max_ms"] == 2642.0
    assert s["mainstall_n"] == 2


def test_healthy_cadence_passes_the_gate() -> None:
    # Post-S2 expectation: steady ~2.6 ms lockstep cadence, with
    # an occasional benign ~10 ms DSP-block gap.
    healthy = [2.6, 2.7, 2.6, 10.0, 2.6, 2.6, 11.0, 2.6] * 20
    b = bucket_gaps(healthy)
    assert b["healthy"] is True
    assert b["stall_or_fence"] == 0
    assert b["buckets"]["25-50"] == 0 and b["buckets"][">=50"] == 0
    assert b["p95_ms"] < 12.0


def test_one_stall_fails_the_gate() -> None:
    # A single 25+ ms excursion is enough to fail (the gate is
    # strict: zero STALL/FENCE samples allowed).
    series = [2.6] * 200 + [30.0]
    b = bucket_gaps(series)
    assert b["healthy"] is False
    assert b["stall_or_fence"] == 1


def test_buckets_partition_correctly() -> None:
    b = bucket_gaps([5.0, 11.9, 12.0, 24.9, 25.0, 49.9, 50.0, 999.0])
    assert b["buckets"] == {"<12": 2, "12-25": 2, "25-50": 2, ">=50": 2}
    assert b["n"] == 8
    assert b["max_ms"] == 999.0


def test_empty_capture_is_not_healthy_and_safe() -> None:
    s = summarize_capture("no wire lines here\n")
    assert s["n"] == 0
    assert s["healthy"] is False          # nothing proven good
    assert s["max_ms"] == 0.0
    assert s["mainstall_max_ms"] == 0.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
