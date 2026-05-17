"""EP2 underrun slew-fill tests (audio-pop structural fix).

The legacy EP2 underrun path spliced literal ``(0.0, 0.0)`` pairs
onto the tail of a short frame -- a hard full-scale step into the
on-board AK4951 codec = the long-standing residual pop that is
reproducible into a 50 ohm dummy load with all DSP off.  The
host-PC-soundcard egress path already fades gracefully on
underflow; the EP2/codec path did not.

``HL2Stream._slew_fill_pairs`` gives the EP2 path the same
treatment Lyra-native: a raised-cosine fade-out of the last real
sample on underrun, and a raised-cosine fade-in on the first
healthy frame after recovery, so neither the drop nor the resume
is a step.  Steady state is a no-op (one branch + one read).

These pin: exact frame size, steady-state byte-equivalence,
monotonic click-free fade-out, recovery fade-in, the
last-sample fade-continuity path (avail == 0), the underrun
counter, and the short-need (need < slew_n) edge.
"""
from __future__ import annotations

from lyra.protocol.stream import HL2Stream


def _make_stream() -> HL2Stream:
    # __init__ touches no sockets/threads (start() does).
    return HL2Stream("10.10.10.1", sample_rate=96000)


def test_steady_state_is_noop_and_records_last() -> None:
    s = _make_stream()
    frame = [(0.5, -0.5)] * 126
    before = s.tx_audio_underruns
    out = s._slew_fill_pairs(list(frame), 126)
    assert out == frame                       # byte-identical
    assert s.tx_audio_underruns == before     # not an underrun
    assert s._ep2_underrun_active is False
    assert s._ep2_last_lr == (0.5, -0.5)      # last sample recorded


def test_underrun_fades_out_then_zeros() -> None:
    s = _make_stream()
    avail = 60                                 # need == 66 > slew_n
    pulled = [(0.8, 0.4)] * avail
    before = s.tx_audio_underruns
    out = s._slew_fill_pairs(pulled, 126)

    assert len(out) == 126
    assert s.tx_audio_underruns == before + 1
    assert s._ep2_underrun_active is True
    # The real samples are untouched.
    assert out[:avail] == [(0.8, 0.4)] * avail
    # The fill region fades the last real value (0.8, 0.4) DOWN to
    # silence monotonically -- never a step up, never above source.
    fill = out[avail:]
    assert len(fill) == 66
    prev_mag = 0.8
    for lv, rv in fill:
        assert 0.0 <= lv <= 0.8 + 1e-9
        assert lv <= prev_mag + 1e-9          # non-increasing
        # L/R keep the source ratio (0.4 / 0.8 == 0.5).
        if lv > 0.0:
            assert abs(rv - lv * 0.5) < 1e-6
        prev_mag = lv
    # First fill sample is already below full scale (a fade, not a
    # one-sample cliff); need > slew_n so the tail reaches exact
    # silence and stays there.
    assert fill[0][0] < 0.8
    assert fill[-1] == (0.0, 0.0)
    assert out[-1] == (0.0, 0.0)


def test_recovery_frame_fades_back_in() -> None:
    s = _make_stream()
    # Force an underrun so the recovery flag is armed.
    s._slew_fill_pairs([(0.7, 0.7)] * 10, 126)
    assert s._ep2_underrun_active is True

    healthy = [(0.9, 0.9)] * 126
    out = s._slew_fill_pairs(list(healthy), 126)

    assert len(out) == 126
    assert s._ep2_underrun_active is False     # cleared
    n = s._ep2_slew_n
    # First sample ramps up from near-zero (faded, not a step);
    # non-decreasing across the slew region; reaches full by the
    # last slew index; everything past the ramp is full level.
    assert 0.0 <= out[0][0] < 0.9
    prev = -1.0
    for k in range(n):
        lv = out[k][0]
        assert lv >= prev - 1e-9               # non-decreasing
        assert lv <= 0.9 + 1e-9
        prev = lv
    assert abs(out[n - 1][0] - 0.9) < 1e-9      # ramp complete
    assert out[n] == (0.9, 0.9)                 # past the ramp = full


def test_sustained_underrun_is_silent_not_sawtooth() -> None:
    # Regression: a sustained underrun must fade out ONCE then stay
    # silent.  Re-fading from the last real value every frame would
    # be a frame-rate sawtooth (an audible buzz), which is worse
    # than the original hard-zero behaviour it replaced.
    s = _make_stream()
    s._slew_fill_pairs([(0.9, 0.9)] * 126, 126)      # healthy: rec last
    first = s._slew_fill_pairs([], 126)              # entry: fade out
    assert first[0][0] > 0.0                          # faded from 0.9
    assert s._ep2_underrun_active is True
    # Every subsequent total-underrun frame is PURE silence -- no
    # re-introduced energy, no fade restart.
    for _ in range(5):
        cont = s._slew_fill_pairs([], 126)
        assert cont == [(0.0, 0.0)] * 126
    # A short (partial) sustained-underrun frame keeps its real
    # prefix then silence -- still no re-fade.
    part = s._slew_fill_pairs([(0.5, 0.5)] * 20, 126)
    assert part[:20] == [(0.5, 0.5)] * 20
    assert part[20:] == [(0.0, 0.0)] * 106


def test_full_underrun_uses_last_recorded_sample() -> None:
    s = _make_stream()
    # A healthy frame records the last sample...
    s._slew_fill_pairs([(0.6, -0.3)] * 126, 126)
    assert s._ep2_last_lr == (0.6, -0.3)
    # ...then a TOTAL underrun (avail == 0) must fade from THAT,
    # not from silence (no audible step at the boundary).
    out = s._slew_fill_pairs([], 126)
    assert len(out) == 126
    assert out[0][0] < 0.6 and out[0][0] > 0.0  # faded from 0.6
    assert out[-1] == (0.0, 0.0)
    assert s._ep2_underrun_active is True


def test_short_need_smaller_than_slew_table() -> None:
    s = _make_stream()
    # need == 4 (< slew_n == 32): fade table is used truncated and
    # the frame is still exactly `target` long, no IndexError.
    out = s._slew_fill_pairs([(1.0, 1.0)] * 122, 126)
    assert len(out) == 126
    assert out[:122] == [(1.0, 1.0)] * 122
    tail = out[122:]
    assert len(tail) == 4
    # Monotonic fade, strictly below source, no zeros needed here
    # (need < slew_n so the whole tail is fade, not pad).
    prev = 1.0
    for lv, _rv in tail:
        assert lv < 1.0 and lv <= prev + 1e-9
        prev = lv


def test_slew_tables_are_well_formed() -> None:
    s = _make_stream()
    n = s._ep2_slew_n
    assert len(s._ep2_fade_out) == n
    assert len(s._ep2_fade_in) == n
    # fade-out starts near unity, ends at silence; fade-in inverse.
    assert s._ep2_fade_out[0] > 0.9
    assert abs(s._ep2_fade_out[-1]) < 1e-9
    assert s._ep2_fade_in[0] < 0.1
    assert abs(s._ep2_fade_in[-1] - 1.0) < 1e-9
    # Complementary: out[k] + in[k] == 1 (raised-cosine pair).
    for a, b in zip(s._ep2_fade_out, s._ep2_fade_in):
        assert abs(a + b - 1.0) < 1e-9


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
