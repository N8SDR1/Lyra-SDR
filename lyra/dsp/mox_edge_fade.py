"""MOX-edge envelope for TX I/Q (v0.2 Phase 2 commit 10, consensus §8.5).

Applies a 50 ms cos² raised-cosine envelope to TX I/Q samples at
MOX state transitions.  Prevents the audible click + broadband
spectral splatter that a hard 0→full amplitude step at PTT
keydown would produce (and the mirror at keyup).

Envelope rationale:

The cos² (raised-cosine, sometimes called Hann window) shape has
zero amplitude at both endpoints AND zero derivative at both
endpoints.  A simple linear ramp has zero amplitude at endpoints
but a discontinuous derivative (kink at start/end) which produces
broadband artifacts above the keyup frequency, just less severe
than a hard step.  cos² is smooth in both amplitude and slope ->
no broadband content from the envelope transition itself.

State machine:

    OFF  ──[start_fade_in]──> FADING_IN  ──[2400 samples]──> ON
    ON   ──[start_fade_out]─> FADING_OUT ──[2400 samples]──> OFF

Phase 3 PTT state machine integration (NOT in v0.2 commit 10):

* MOX=1 edge (PTT keydown):
    1. Phase 3: ``fade.start_fade_in()``
    2. Phase 3: ``stream.inject_tx_iq = True``
    3. Worker forwards faded I/Q.  Envelope ramps 0->1 over 2400
       samples (~50 ms at 48 kHz), then settles in ON state.
* MOX=0 edge (PTT keyup):
    1. Phase 3: ``fade.start_fade_out()``
    2. Phase 3 holds ``inject_tx_iq=True`` until ``fade.is_off()``
       so the ramp-out samples actually reach the wire.
    3. After ~50 ms of fade-out samples flow, fade hits OFF state.
    4. Phase 3: ``stream.inject_tx_iq = False``, stop wire forward.

For v0.2 commit 10, the fade is wired but stays in OFF state for
its entire lifetime (no caller flips it -- Phase 3 PTT state
machine does that).  Worker never reaches the apply() call
because ``inject_tx_iq=False`` gates the entire forward branch.
Wire behavior is byte-identical to commit 9.

Why 50 ms / 2400 samples / 48 kHz:

* 50 ms is the established convention in OpenHPSDR / Thetis /
  WDSP for SSB MOX edges (operator-imperceptible delay, fully
  suppresses click).
* 2400 = 50 ms × 48 kHz.  The HL2 TX I/Q wire rate is 48 kHz
  (gateware-fixed via AK4951 codec; CLAUDE.md §3.5), so 2400
  samples = exactly 50 ms of wire time.
* Spans ~19 EP2 frames (~381 Hz cadence) -- envelope is
  per-sample inside complex64, not per-EP2-frame, so the curve
  is smooth across frame boundaries.
"""
from __future__ import annotations

import threading
from enum import IntEnum

import numpy as np


class MoxFadeState(IntEnum):
    OFF = 0
    FADING_IN = 1
    ON = 2
    FADING_OUT = 3


class MoxEdgeFade:
    """50 ms cos² envelope applied to TX I/Q at MOX state transitions.

    Single-instance per Radio.  Construction is cheap (~10 us for
    the curve precompute); typical usage is one instance for the
    lifetime of a TxDspWorker.

    Threading: ``start_fade_in`` / ``start_fade_out`` are callable
    from any thread (typically Qt main when PTT state changes).
    ``apply()`` is called from the TxDspWorker thread.  Both paths
    are lock-guarded for state consistency.
    """

    FADE_MS = 50.0
    RATE_HZ = 48000.0
    # 2400 samples at the HL2 TX I/Q wire rate.
    FADE_SAMPLES = int(FADE_MS * RATE_HZ / 1000.0)

    def __init__(self) -> None:
        self._state: MoxFadeState = MoxFadeState.OFF
        self._fade_pos: int = 0  # 0..FADE_SAMPLES during fade
        self._lock = threading.Lock()
        # Pre-compute the cos² ramp lookup table (saves ~2400
        # ``cos()`` calls per fade transition).  Math:
        #   gain(t) = 0.5 * (1 - cos(π * t / T))
        # At t=0: gain=0, derivative=0 (smooth start).
        # At t=T: gain=1, derivative=0 (smooth end).
        # This is equivalent to ``sin²(π * t / (2T))`` and to
        # ``cos²`` of a phase-shifted argument; "cos²" is the
        # name in HPSDR / Thetis tradition.
        n = self.FADE_SAMPLES
        t = np.arange(n, dtype=np.float32) / float(n)
        self._fade_in_curve = (
            0.5 * (1.0 - np.cos(np.pi * t))
        ).astype(np.float32)
        # Fade-out curve is the reverse (1->0 with mirrored shape).
        self._fade_out_curve = self._fade_in_curve[::-1].copy()
        # Diagnostic counters
        self.fade_ins_started: int = 0
        self.fade_outs_started: int = 0
        self.fade_ins_completed: int = 0
        self.fade_outs_completed: int = 0
        self.aborted_fade_ins: int = 0  # fade-out called during fade-in

    @property
    def state(self) -> MoxFadeState:
        return self._state

    def is_off(self) -> bool:
        """True if the envelope is in the OFF state (no TX should
        be reaching the wire).  Phase 3 PTT uses this to decide
        when to flip ``inject_tx_iq`` False after a fade-out
        completes."""
        return self._state == MoxFadeState.OFF

    def is_active(self) -> bool:
        """True if the envelope is in any state OTHER than OFF
        (i.e., TX samples should still flow to the wire -- either
        steady-state TX or mid-fade)."""
        return self._state != MoxFadeState.OFF

    def start_fade_in(self) -> None:
        """Begin a fade-in transition (MOX=0 → MOX=1).

        From OFF: state -> FADING_IN, position resets to 0.
        From any other state: no-op (operator double-keyed or
        Phase 3 race -- ignore the spurious call).
        """
        with self._lock:
            if self._state == MoxFadeState.OFF:
                self._state = MoxFadeState.FADING_IN
                self._fade_pos = 0
                self.fade_ins_started += 1

    def start_fade_out(self) -> None:
        """Begin a fade-out transition (MOX=1 → MOX=0).

        From ON: state -> FADING_OUT, position resets to 0.

        From FADING_IN (operator aborts a fresh PTT before it
        completes): jump to FADING_OUT from the equivalent
        amplitude point on the fade-out curve, preserving
        amplitude continuity.  Avoids a click from suddenly
        reversing direction at a non-curve-symmetric point.
        Bumps ``aborted_fade_ins``.

        From OFF or FADING_OUT: no-op.
        """
        with self._lock:
            if self._state == MoxFadeState.ON:
                self._state = MoxFadeState.FADING_OUT
                self._fade_pos = 0
                self.fade_outs_started += 1
            elif self._state == MoxFadeState.FADING_IN:
                # cos² is symmetric: fade_in_curve[k] ==
                # fade_out_curve[FADE_SAMPLES - 1 - k] for all k
                # in [0, FADE_SAMPLES).  So to continue from the
                # current amplitude on the fade-out path:
                self._state = MoxFadeState.FADING_OUT
                self._fade_pos = self.FADE_SAMPLES - self._fade_pos
                self.aborted_fade_ins += 1
                self.fade_outs_started += 1

    def apply(self, iq: np.ndarray) -> np.ndarray:
        """Apply the current envelope to a block of complex64 I/Q.

        Returns a complex64 ndarray of the same shape as ``iq``.
        May be the input array unchanged (ON state) or a fresh
        array (any fade state).  Advances the fade position and
        transitions state when a fade completes.

        Caller (TxDspWorker) feeds the returned array to both
        ``HL2Stream.queue_tx_iq`` (for the wire) and the sip1
        tap (for PureSignal history) -- both should see the
        SAME enveloped samples so PS calibration aligns
        correctly against the feedback path.
        """
        n = iq.size
        if n == 0:
            return iq
        with self._lock:
            state = self._state
            pos = self._fade_pos
            if state == MoxFadeState.OFF:
                # Suppress any leakage; should not normally be
                # called in OFF state since the worker gates on
                # inject_tx_iq, but be defensive.
                return np.zeros_like(iq)
            if state == MoxFadeState.ON:
                # Passthrough.  Hot path during steady-state TX.
                return iq

            # FADING_IN or FADING_OUT: apply per-sample envelope
            # over the in-curve segment of the block; the
            # remainder (if the block extends past the fade end)
            # is passthrough (FADING_IN -> ON) or silence
            # (FADING_OUT -> OFF).
            if state == MoxFadeState.FADING_IN:
                curve = self._fade_in_curve
            else:
                curve = self._fade_out_curve

            remaining = self.FADE_SAMPLES - pos
            fade_n = min(n, remaining)

            out = np.empty_like(iq)
            if fade_n > 0:
                # complex64 * float32 -> complex64 (vectorized;
                # ~2 us for a 38-sample block, well under EP2 cadence).
                envelope = curve[pos:pos + fade_n].astype(np.float32)
                out[:fade_n] = iq[:fade_n] * envelope
            if fade_n < n:
                # Block extends past fade boundary.
                if state == MoxFadeState.FADING_IN:
                    out[fade_n:] = iq[fade_n:]
                else:
                    out[fade_n:] = 0.0

            # Advance state machine.
            new_pos = pos + n
            if new_pos >= self.FADE_SAMPLES:
                if state == MoxFadeState.FADING_IN:
                    self._state = MoxFadeState.ON
                    self.fade_ins_completed += 1
                else:
                    self._state = MoxFadeState.OFF
                    self.fade_outs_completed += 1
                self._fade_pos = 0
            else:
                self._fade_pos = new_pos

            return out
