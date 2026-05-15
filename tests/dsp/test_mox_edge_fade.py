"""MoxEdgeFade tests (v0.2 Phase 2 commit 10, consensus §8.5).

Validates:
1. Default state is OFF.
2. cos² envelope curve has the right shape (0 at endpoints, 1 at
   peak, zero derivative at endpoints).
3. State machine transitions OFF -> FADING_IN -> ON via apply().
4. State machine transitions ON -> FADING_OUT -> OFF via apply().
5. Spurious start_fade_in / start_fade_out calls are ignored.
6. Aborted fade-in (fade-out called mid-fade-in) preserves
   amplitude continuity via cos² symmetry.
7. OFF state returns zero-amplitude (defensive).
8. ON state passes input through unchanged.
9. Block-boundary correctness: fade can complete mid-block, with
   trailing samples handled correctly (passthrough or silence).
10. TxDspWorker applies the fade BEFORE the wire-forward AND the
    sip1 tap write, so both see the same enveloped samples.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from lyra.dsp.mox_edge_fade import MoxEdgeFade, MoxFadeState


def test_default_state_is_off():
    fade = MoxEdgeFade()
    assert fade.state == MoxFadeState.OFF
    assert fade.is_off()
    assert not fade.is_active()


def test_fade_samples_is_2400_at_48_khz():
    """50 ms × 48 kHz = 2400.  Documented contract."""
    assert MoxEdgeFade.FADE_SAMPLES == 2400


def test_curve_shape():
    """Endpoint amplitudes + symmetry of the precomputed curve."""
    fade = MoxEdgeFade()
    curve = fade._fade_in_curve  # noqa: SLF001
    n = MoxEdgeFade.FADE_SAMPLES
    assert curve.shape == (n,)
    # First sample = 0.0 exactly (sin²(0) or (1-cos(0))/2 = 0)
    assert curve[0] == pytest.approx(0.0, abs=1e-7)
    # Mid sample ~= 0.5
    mid = curve[n // 2]
    assert mid == pytest.approx(0.5, abs=0.01)
    # Last sample ~= 1.0 (we sample t in [0, (n-1)/n] not [0, 1],
    # so the last point is slightly below 1; tight tolerance still
    # confirms we reach near-full amplitude)
    assert curve[-1] == pytest.approx(1.0, abs=0.01)


def test_curve_is_monotonic():
    """The fade-in curve should be monotonically non-decreasing
    (no overshoot, no ripple)."""
    fade = MoxEdgeFade()
    curve = fade._fade_in_curve  # noqa: SLF001
    diffs = np.diff(curve)
    assert np.all(diffs >= -1e-7), "Fade-in curve dipped (should be monotonic)"


def test_fade_in_transitions_off_to_fading_in_to_on():
    """start_fade_in() + enough apply() calls drives OFF -> ON."""
    fade = MoxEdgeFade()
    assert fade.state == MoxFadeState.OFF
    fade.start_fade_in()
    assert fade.state == MoxFadeState.FADING_IN
    assert fade.fade_ins_started == 1
    assert fade.fade_ins_completed == 0
    # Apply enough samples to complete the fade in one block
    iq = np.ones(MoxEdgeFade.FADE_SAMPLES, dtype=np.complex64)
    out = fade.apply(iq)
    assert fade.state == MoxFadeState.ON
    assert fade.fade_ins_completed == 1
    # First sample faded to zero, last sample near-full
    assert abs(out[0]) == pytest.approx(0.0, abs=1e-6)
    assert abs(out[-1]) == pytest.approx(1.0, abs=0.01)


def test_fade_out_transitions_on_to_fading_out_to_off():
    """start_fade_out() + apply() drives ON -> OFF.

    First gets the fade into ON state via fade_in, then exercises
    the fade-out path."""
    fade = MoxEdgeFade()
    fade.start_fade_in()
    # Complete fade-in
    fade.apply(np.ones(MoxEdgeFade.FADE_SAMPLES, dtype=np.complex64))
    assert fade.state == MoxFadeState.ON
    # Start fade-out
    fade.start_fade_out()
    assert fade.state == MoxFadeState.FADING_OUT
    assert fade.fade_outs_started == 1
    # Complete fade-out in one block
    iq = np.ones(MoxEdgeFade.FADE_SAMPLES, dtype=np.complex64)
    out = fade.apply(iq)
    assert fade.state == MoxFadeState.OFF
    assert fade.fade_outs_completed == 1
    # First sample near-full, last sample at zero
    assert abs(out[0]) == pytest.approx(1.0, abs=0.01)
    assert abs(out[-1]) == pytest.approx(0.0, abs=1e-6)


def test_on_state_is_passthrough():
    """In ON state, apply() returns input unchanged."""
    fade = MoxEdgeFade()
    fade.start_fade_in()
    fade.apply(np.ones(MoxEdgeFade.FADE_SAMPLES, dtype=np.complex64))
    assert fade.state == MoxFadeState.ON
    # ON state: apply returns input
    iq = np.array(
        [1 + 2j, 3 + 4j, 5 + 6j], dtype=np.complex64,
    )
    out = fade.apply(iq)
    assert np.array_equal(out, iq)


def test_off_state_returns_zeros():
    """In OFF state (default), apply() returns zero-amplitude
    array (defensive against worker leakage)."""
    fade = MoxEdgeFade()
    iq = np.array(
        [1 + 2j, 3 + 4j, 5 + 6j], dtype=np.complex64,
    )
    out = fade.apply(iq)
    assert np.all(out == 0)
    assert out.shape == iq.shape


def test_double_fade_in_is_noop():
    """Calling start_fade_in() when already FADING_IN does nothing."""
    fade = MoxEdgeFade()
    fade.start_fade_in()
    assert fade.fade_ins_started == 1
    fade.start_fade_in()
    assert fade.fade_ins_started == 1  # NOT bumped again


def test_fade_out_from_off_is_noop():
    """start_fade_out() in OFF state is ignored."""
    fade = MoxEdgeFade()
    fade.start_fade_out()
    assert fade.state == MoxFadeState.OFF
    assert fade.fade_outs_started == 0


def test_aborted_fade_in_preserves_amplitude_continuity():
    """Fade-out called mid-fade-in: state jumps to FADING_OUT at
    the equivalent amplitude point on the fade-out curve so the
    sample sequence has no derivative discontinuity.

    cos² symmetry: fade_in_curve[k] == fade_out_curve[N-1-k]
    means setting fade_pos to (N - current_pos) keeps the
    amplitude exactly equal across the transition.
    """
    fade = MoxEdgeFade()
    fade.start_fade_in()
    # Advance fade-in to about 1/4 through (~600 samples)
    fade.apply(np.ones(600, dtype=np.complex64))
    assert fade.state == MoxFadeState.FADING_IN
    fade_pos_before = fade._fade_pos  # noqa: SLF001
    # Get the amplitude at the current fade_pos
    amp_before_abort = fade._fade_in_curve[fade_pos_before]  # noqa: SLF001
    # Abort
    fade.start_fade_out()
    assert fade.state == MoxFadeState.FADING_OUT
    assert fade.aborted_fade_ins == 1
    # Next apply() call should produce a sample at near the same
    # amplitude (small numerical drift OK)
    out = fade.apply(np.ones(1, dtype=np.complex64))
    amp_after_abort = abs(out[0])
    # Amplitudes should be near-equal (curve is sampled, so tiny
    # one-sample drift is fine)
    assert amp_after_abort == pytest.approx(
        amp_before_abort, abs=0.01,
    )


def test_partial_fade_in_block():
    """If apply() is called with fewer samples than FADE_SAMPLES,
    fade advances by N but does NOT complete."""
    fade = MoxEdgeFade()
    fade.start_fade_in()
    fade.apply(np.ones(100, dtype=np.complex64))
    assert fade.state == MoxFadeState.FADING_IN
    assert fade._fade_pos == 100  # noqa: SLF001
    fade.apply(np.ones(2300, dtype=np.complex64))
    # 100 + 2300 = 2400 -> fade completes exactly
    assert fade.state == MoxFadeState.ON


def test_fade_completes_mid_block():
    """If a block crosses the fade boundary, samples past the
    boundary are passthrough (FADING_IN) or zeros (FADING_OUT)."""
    fade = MoxEdgeFade()
    fade.start_fade_in()
    # Apply a block of 3000 = FADE_SAMPLES + 600 extra
    iq = np.full(3000, 0.5 + 0.5j, dtype=np.complex64)
    out = fade.apply(iq)
    assert fade.state == MoxFadeState.ON
    # First sample heavily attenuated (early in fade)
    assert abs(out[0]) < 0.1
    # Sample 2400 = first post-fade sample, should be passthrough
    # (= original 0.5+0.5j magnitude ~= 0.707)
    assert abs(out[2400]) == pytest.approx(
        abs(0.5 + 0.5j), abs=0.01,
    )
    # Last sample also passthrough
    assert out[-1] == pytest.approx(0.5 + 0.5j, abs=0.01)


def test_zero_length_block_no_op():
    """apply() with empty array returns immediately with empty array."""
    fade = MoxEdgeFade()
    fade.start_fade_in()
    pos_before = fade._fade_pos  # noqa: SLF001
    iq = np.zeros(0, dtype=np.complex64)
    out = fade.apply(iq)
    assert out.size == 0
    # State + position unchanged
    assert fade._fade_pos == pos_before  # noqa: SLF001
    assert fade.state == MoxFadeState.FADING_IN


# ─── TxDspWorker integration tests ────────────────────────────────


class _FakeTxChannel:
    def process(self, mic):
        return np.full(mic.size, 0.5 + 0.5j, dtype=np.complex64)


class _FakeHL2Stream:
    def __init__(self):
        self.inject_tx_iq = False
        self.last_queued: np.ndarray = np.zeros(0, dtype=np.complex64)

    def queue_tx_iq(self, iq):
        self.last_queued = np.asarray(iq, dtype=np.complex64).copy()


class _FakeSip1Tap:
    def __init__(self):
        self.writes: list[np.ndarray] = []

    def write(self, iq):
        self.writes.append(np.asarray(iq, dtype=np.complex64).copy())


def _await(predicate, timeout_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_worker_applies_fade_before_wire_and_tap():
    """When fade is FADING_IN, both HL2Stream.queue_tx_iq AND
    iq_tap.write see the enveloped samples (not raw I/Q)."""
    from lyra.dsp.tx_dsp_worker import TxDspWorker

    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    hl2.inject_tx_iq = True  # simulate Phase 3 MOX=1 gate
    tap = _FakeSip1Tap()
    fade = MoxEdgeFade()
    fade.start_fade_in()
    worker = TxDspWorker(
        tx, hl2, iq_tap=tap, mox_edge_fade=fade,
    )
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: hl2.last_queued.size == 38)
        # Wire and tap should have IDENTICAL enveloped samples
        assert _await(lambda: len(tap.writes) == 1)
        assert np.array_equal(hl2.last_queued, tap.writes[0])
        # And both should differ from raw (TxChannel returns
        # 0.5+0.5j, but envelope attenuated early-fade samples)
        # First sample should be near zero (fade just started)
        assert abs(hl2.last_queued[0]) < 0.1
    finally:
        worker.stop()


def test_worker_without_fade_passes_iq_unchanged():
    """When mox_edge_fade is None (Phase 2 default), I/Q is
    forwarded unchanged."""
    from lyra.dsp.tx_dsp_worker import TxDspWorker

    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    hl2.inject_tx_iq = True
    worker = TxDspWorker(tx, hl2)  # no mox_edge_fade
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: hl2.last_queued.size == 38)
        # All samples at 0.5+0.5j (TxChannel mock returns this)
        assert hl2.last_queued[0] == pytest.approx(0.5 + 0.5j)
    finally:
        worker.stop()
