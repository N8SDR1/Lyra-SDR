"""All-mode squelch — RMS level-based with auto-tracked noise floor.

Lyra's squelch tracks the audio RMS over a short window and the
band's noise floor over a long window, then gates audio output
when RMS is at or near the noise floor.  The operator threshold
controls how far above the noise floor a signal must rise before
the gate opens.

This is fundamentally simpler than WDSP's SSQL frequency-to-voltage
voice detector.  The first iteration of Lyra's squelch ported the
SSQL algorithm directly, but on-air testing across N8SDR's RF
environment (which has stable AM broadcaster harmonics within the
ham bands) showed the FTOV detector mis-classified those harmonics
as "voice" or alternately got confused by them, making the gate
flicker.  RMS-based detection sidesteps the issue: voice has
distinctly higher RMS than noise floor regardless of zero-crossing
behavior, and operator threshold maps to a clear "X dB above
floor" semantic.

Algorithm
=========

1. **RMS tracking** — maintain RMS over a sliding window of
   ~20 ms.  Cheap and effectively tracks vocal envelope.

2. **Noise-floor tracking** — asymmetric exponential:
   - track-down fast (when RMS drops, noise floor follows quickly)
   - track-up slow (loud signals don't pollute the floor estimate)
   Result: the floor settles to the band's quiet level within
   ~1-2 seconds of empty band, then stays put through voice.

3. **Hysteresis gate** — open when RMS > floor·k_open, close when
   RMS < floor·k_close (k_close < k_open).  Prevents chatter on
   signals near the threshold.

4. **Smooth attack/release** — when the gate state changes, the
   audio gain ramps with a cosine envelope (~70 ms attack /
   release) to avoid clicks.

Lyra GPL v3+ (since v0.0.6).  This module is original to Lyra —
no algorithmic content is taken from WDSP/Thetis.  Architectural
inspiration only (chain placement, attack/release timing, the
existence of an "all-mode" squelch concept).
"""
# Lyra-SDR — RMS-based all-mode squelch
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License v3
# or later.  See LICENSE in the project root for the full terms.
from __future__ import annotations

import numpy as np


class AllModeSquelch:
    """RMS-level squelch with auto-tracked noise floor.

    Streaming, sample-accurate, length-preserving.  Bypass-fast
    when ``enabled`` is False (single attribute check, early
    return).
    """

    # ── Defaults ─────────────────────────────────────────────────
    DEFAULT_THRESHOLD: float = 0.20      # 0.0..1.0 operator scale
    DEFAULT_TUP: float = 0.070           # attack ramp seconds
    DEFAULT_TDOWN: float = 0.070         # release ramp seconds
    DEFAULT_MUTED_GAIN: float = 0.0      # output gain while closed

    # RMS sliding-window length, in seconds.  150 ms bridges
    # natural speech pauses (50-200 ms gaps between syllables) so
    # the gate doesn't chatter mid-word.  Shorter windows produce
    # sample-accurate envelope tracking but cause audible clipping
    # on natural speech; longer windows lag the actual signal
    # envelope and miss fast transients.
    RMS_WINDOW_SEC: float = 0.150

    # Noise-floor tracker time constants.  Track-down (signal
    # quieting) is fast so the floor settles after voice ends.
    # Track-up tau is mostly irrelevant now because we only
    # update the floor when the gate is CLOSED (see _process_block);
    # this keeps speech from dragging the floor up over long
    # transmissions.
    FLOOR_TRACK_DOWN_TAU: float = 0.5    # seconds
    FLOOR_TRACK_UP_TAU: float = 8.0      # seconds (rarely active)

    # Hang time — once the gate opens, keep it open for at least
    # this many seconds even if ratio dips below k_close.
    # Prevents the gate from closing on brief mid-syllable
    # silences during continuous transmission.  300 ms covers
    # typical inter-word pauses; longer pauses (operator letting
    # go of the mic) still close it via the normal timeout.
    HANG_TIME_SEC: float = 0.300

    # Hysteresis ratios — RMS / floor must exceed K_OPEN to open
    # the gate, and must drop below K_CLOSE to close it.  The gap
    # between them prevents chatter on signals right at threshold.
    # Both scale with the operator threshold.
    K_OPEN_BASE: float = 1.5             # at threshold=0
    K_OPEN_RANGE: float = 6.0            # threshold=1 adds this
    K_CLOSE_FRACTION: float = 0.5        # close-thresh = open · this

    # Floor for the noise-floor estimate so we don't divide by
    # zero in the ratio comparisons.
    FLOOR_MIN: float = 1.0e-6

    # State-machine constants (kept as class attrs so external
    # code / tests can reference them by name).
    MUTED: int = 0
    INCREASE: int = 1
    UNMUTED: int = 2
    DECREASE: int = 3

    # Backwards-compat shim — old FTOV-based squelch had this
    # attribute.  External callers that still reference it get
    # the unmute-side state value (no functional difference).
    TR_SS_UNMUTE: float = 0.0
    tr_thresh: float = 0.0

    def __init__(self, rate: int = 48000) -> None:
        self.rate = int(rate)
        self.enabled: bool = False

        # Operator-tunable parameters.
        self.threshold: float = self.DEFAULT_THRESHOLD
        self.tup: float = self.DEFAULT_TUP
        self.tdown: float = self.DEFAULT_TDOWN
        self.muted_gain: float = self.DEFAULT_MUTED_GAIN

        self._recompute_derived()
        self._build_ramps()
        self._init_state()

    # ── Public API ────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear streaming state.  Call on band/mode/freq changes."""
        self._init_state()

    def set_threshold(self, value: float) -> None:
        """Operator threshold, 0.0..1.0.

        Maps to "how far above the noise floor must the signal
        rise" before the gate opens:

          0.00  - 1.5× floor (effectively always open)
          0.20  - ~2.7× floor — voice-friendly default
          0.40  - ~3.9× floor — clean signals only
          0.60  - ~5.1× floor — strong signals
          0.80  - ~6.3× floor — very tight
          1.00  - 7.5× floor — only the loudest stations open

        The gate has hysteresis: it closes at 70% of the open
        threshold to prevent chatter on signals right at the edge.
        """
        self.threshold = max(0.0, min(1.0, float(value)))
        self._recompute_derived()

    def set_muted_gain(self, value: float) -> None:
        """Gain while gate is closed.  0.0 = full mute (default)."""
        self.muted_gain = max(0.0, min(1.0, float(value)))
        self._build_ramps()

    def is_passing(self) -> bool:
        """True when the gate is currently passing audio."""
        return self._gate_open

    def get_floor_db(self) -> float:
        """Current noise-floor estimate in dB FS (debug aid)."""
        return float(20.0 * np.log10(max(self._floor, 1e-12)))

    def get_rms_db(self) -> float:
        """Current short-window RMS in dB FS (debug aid)."""
        rms = float(np.sqrt(max(self._sumsq / self._rms_n, 1e-24)))
        return float(20.0 * np.log10(max(rms, 1e-12)))

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one block.  Length-preserving; returns float32."""
        if not self.enabled or audio.size == 0:
            return audio
        x = audio.astype(np.float64, copy=False)
        return self._process_block(x).astype(np.float32, copy=False)

    # ── Internals ─────────────────────────────────────────────────

    def _recompute_derived(self) -> None:
        # Sliding RMS window length in samples.
        self._rms_n = max(8, int(self.RMS_WINDOW_SEC * self.rate))

        # Floor-tracking exponential coefficients.  alpha = exp(-1/τ)
        # per sample so the τ values mean "samples to decay 1/e".
        self._floor_alpha_down = float(np.exp(
            -1.0 / max(self.FLOOR_TRACK_DOWN_TAU * self.rate, 1.0)))
        self._floor_alpha_up = float(np.exp(
            -1.0 / max(self.FLOOR_TRACK_UP_TAU * self.rate, 1.0)))

        # Hysteresis thresholds.
        self._k_open = (self.K_OPEN_BASE
                        + self.K_OPEN_RANGE * self.threshold)
        self._k_close = self._k_open * self.K_CLOSE_FRACTION

    def _build_ramps(self) -> None:
        """Cosine attack/release ramp tables."""
        ntup = max(1, int(self.tup * self.rate))
        ntdown = max(1, int(self.tdown * self.rate))
        self._ntup = ntup
        self._ntdown = ntdown
        theta_up = np.linspace(0.0, np.pi, ntup + 1)
        self._cup = (self.muted_gain
                     + (1.0 - self.muted_gain)
                     * 0.5 * (1.0 - np.cos(theta_up)))
        theta_down = np.linspace(0.0, np.pi, ntdown + 1)
        self._cdown = (self.muted_gain
                       + (1.0 - self.muted_gain)
                       * 0.5 * (1.0 + np.cos(theta_down)))

    def _init_state(self) -> None:
        """Reset all per-stream state."""
        # RMS sliding window — store squared samples in a ring
        # buffer; sumsq is the running sum so we can extract
        # mean-square in O(1) per sample.
        self._rms_buf = np.zeros(self._rms_n, dtype=np.float64)
        self._rms_idx: int = 0
        self._sumsq: float = 0.0

        # Noise-floor estimate.  Seeded to a "no info yet" marker
        # so the first audio block can calibrate from actual
        # incoming RMS.  See _process_block — once we've seen a
        # full RMS-window of audio after reset(), floor is set to
        # rms / 2 (one octave below current level), which gives
        # the gate a sensible starting state regardless of band
        # noise level.
        self._floor: float = -1.0
        # Counter for first-block seeding — decrements each sample
        # until 0, then we seed the floor from current RMS.
        self._floor_seed_remaining: int = self._rms_n

        # Gate state — start OPEN so audio passes immediately on
        # enable.  The detector will close it later if appropriate.
        self._gate_open: bool = True

        # Hang-time counter — when > 0, the gate stays open even if
        # the ratio drops below k_close.  Reset to HANG_TIME_SEC ×
        # rate every time ratio is firmly above k_open; counts down
        # otherwise.  Bridges natural speech pauses.
        self._hang_remaining: int = 0

        # Smooth-ramp state — UNMUTED (gain=1) initially.
        self._state: int = self.UNMUTED
        self._count: int = 0

    def _process_block(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        out = np.empty(n, dtype=np.float64)

        # Hoist locals for speed.
        rms_buf = self._rms_buf
        rms_idx = self._rms_idx
        rms_n = self._rms_n
        rms_n_inv = 1.0 / rms_n
        sumsq = self._sumsq

        floor = self._floor
        floor_seed_remaining = self._floor_seed_remaining
        floor_min = self.FLOOR_MIN
        alpha_down = self._floor_alpha_down
        alpha_up = self._floor_alpha_up
        k_open = self._k_open
        k_close = self._k_close

        gate_open = self._gate_open
        hang_remaining = self._hang_remaining
        hang_reload = int(self.HANG_TIME_SEC * self.rate)
        state = self._state
        count = self._count
        cup = self._cup
        cdown = self._cdown
        ntup = self._ntup
        ntdown = self._ntdown
        muted_gain = self.muted_gain

        # Threshold ≈ 0 → always open (true bypass).  Compute once
        # rather than checking inside the per-sample loop.
        always_open = (self.threshold < 0.005)

        for i in range(n):
            xs = float(x[i])
            xs2 = xs * xs

            # ── Update sliding-window RMS ────────────────────────
            sumsq += xs2 - rms_buf[rms_idx]
            rms_buf[rms_idx] = xs2
            rms_idx += 1
            if rms_idx == rms_n:
                rms_idx = 0
            mean_sq = sumsq * rms_n_inv
            if mean_sq < 0.0:
                mean_sq = 0.0  # numerical safety
            rms = mean_sq ** 0.5

            # ── First-block floor seeding ────────────────────────
            # On the first RMS_WINDOW worth of samples after
            # reset(), wait for the RMS estimate to fully reflect
            # the input, then seed the floor at rms/2.  This puts
            # the gate in a defensible state from sample 1 of the
            # second block onward.
            if floor_seed_remaining > 0:
                floor_seed_remaining -= 1
                if floor_seed_remaining == 0:
                    # Seed at half the current RMS — gives ratio
                    # of 2.0 on band noise, comfortably between
                    # K_CLOSE (0.5×k_open) and K_OPEN (1.5+).
                    # If RMS is somehow zero, fall back to a
                    # default that keeps the gate behaving.
                    seed = rms * 0.5 if rms > floor_min else 0.01
                    floor = max(seed, floor_min)
            else:
                # ── Normal floor tracking (asymmetric) ───────────
                # Track DOWN always — band can get quieter at any
                # time and we want the floor to follow.
                # Track UP only when gate is CLOSED — prevents
                # speech / signal from polluting the floor.
                if rms < floor:
                    floor = (alpha_down * floor
                             + (1.0 - alpha_down) * rms)
                elif not gate_open:
                    floor = (alpha_up * floor
                             + (1.0 - alpha_up) * rms)
                if floor < floor_min:
                    floor = floor_min

            # ── Hysteresis gate ─────────────────────────────────
            # Threshold=0 is a true bypass — gate always open
            # regardless of RMS/floor ratio.
            if always_open:
                gate_open = True
                hang_remaining = 0
            else:
                ratio = rms / floor
                if not gate_open:
                    if ratio > k_open:
                        gate_open = True
                        hang_remaining = hang_reload
                else:
                    # Gate currently open.  Refresh the hang timer
                    # whenever the ratio is firmly above k_open
                    # (so brief mid-syllable dips don't trigger a
                    # closure).
                    if ratio > k_open:
                        hang_remaining = hang_reload
                    elif hang_remaining > 0:
                        hang_remaining -= 1
                    elif ratio < k_close:
                        gate_open = False

            # ── State machine for smooth gain ramp ──────────────
            if state == AllModeSquelch.UNMUTED:
                if not gate_open:
                    state = AllModeSquelch.DECREASE
                    count = ntdown
                gain = 1.0
            elif state == AllModeSquelch.MUTED:
                if gate_open:
                    state = AllModeSquelch.INCREASE
                    count = ntup
                gain = muted_gain
            elif state == AllModeSquelch.INCREASE:
                gain = cup[ntup - count]
                if count == 0:
                    state = AllModeSquelch.UNMUTED
                else:
                    count -= 1
            else:  # DECREASE
                gain = cdown[ntdown - count]
                if count == 0:
                    state = AllModeSquelch.MUTED
                else:
                    count -= 1

            out[i] = gain * xs

        # Persist state.
        self._rms_buf = rms_buf
        self._rms_idx = rms_idx
        self._sumsq = sumsq
        self._floor = floor
        self._floor_seed_remaining = floor_seed_remaining
        self._gate_open = gate_open
        self._hang_remaining = hang_remaining
        self._state = state
        self._count = count

        return out
