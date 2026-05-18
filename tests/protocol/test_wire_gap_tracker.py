"""Wire-cadence gap tracker tests (the NON-blind instrument).

`un`/`ov` are structurally blind to a coherent whole-chain freeze:
a system GPU/compositor event blocks the Qt main thread, the GIL
freezes producer AND the EP2 writer together, so the deque never
im-balances (un/ov stay 0/green) yet the HL2 sees a wire-cadence
gap = the audible click + the AGC "volume slam" on resume.

`HL2Stream._note_wire_send(now)` + `read_max_wire_gap_ms()`
(reset-on-read, mirrors read_tx_audio_high_water) are the readout
that DOES move on that trigger.  These pin the tracker math with
synthetic monotonic timestamps (no sockets/threads).
"""
from __future__ import annotations

from lyra.protocol.stream import HL2Stream


def _make_stream() -> HL2Stream:
    # __init__ touches no sockets/threads (start() does).
    return HL2Stream("10.10.10.1", sample_rate=96000)


def test_first_send_records_no_interval() -> None:
    s = _make_stream()
    s._note_wire_send(1000.0)            # first send: no prior
    assert s.read_max_wire_gap_ms() == 0.0


def test_steady_cadence_tracks_largest_small_gap() -> None:
    s = _make_stream()
    t = 1000.0
    s._note_wire_send(t)                 # seed
    for dt in (0.0026, 0.0026, 0.011, 0.0026):   # ms: 2.6/2.6/11/2.6
        t += dt
        s._note_wire_send(t)
    # Max gap this window == the 11 ms inter-block gap.
    assert abs(s.read_max_wire_gap_ms() - 11.0) < 1e-6


def test_stall_gap_is_captured_in_ms() -> None:
    s = _make_stream()
    s._note_wire_send(5.0)
    s._note_wire_send(5.0 + 0.250)       # 250 ms freeze
    assert abs(s.read_max_wire_gap_ms() - 250.0) < 1e-6


def test_read_resets_then_tracks_fresh() -> None:
    s = _make_stream()
    s._note_wire_send(0.0)
    s._note_wire_send(0.180)             # 180 ms stall
    assert abs(s.read_max_wire_gap_ms() - 180.0) < 1e-6
    # After read, max is reset; a subsequent small gap is the new max.
    assert s.read_max_wire_gap_ms() == 0.0
    s._note_wire_send(0.181)             # +1 ms (prev was 0.180)
    assert abs(s.read_max_wire_gap_ms() - 1.0) < 1e-6


def test_max_is_peak_not_last() -> None:
    s = _make_stream()
    s._note_wire_send(0.0)
    s._note_wire_send(0.300)             # 300 ms (the peak)
    s._note_wire_send(0.305)             # 5 ms (smaller, after)
    # The reading must be the PEAK gap in the window, not the last.
    assert abs(s.read_max_wire_gap_ms() - 300.0) < 1e-6


def test_default_state_clean() -> None:
    s = _make_stream()
    assert s._last_wire_send_t is None
    assert s._max_wire_gap_ms == 0.0
    assert s.read_max_wire_gap_ms() == 0.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
