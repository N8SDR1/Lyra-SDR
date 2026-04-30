"""IQ-domain impulse blanker (Phase 3.D #2).

Detects narrow impulse noise in the complex-baseband IQ stream and
replaces those samples with predicted values before the bandpass
filter spreads the impulse across the audio passband.

The algorithm is the classical IQ-domain detect-then-replace
impulse blanker described in HF receiver design literature.  It is
implemented clean-room here from the public DSP literature; no
code from any other SDR client has been ported, adapted, or
translated.

Algorithm
---------
For each IQ sample x[n] at the input sample rate (pre-decimation):

  p[n]   = |x[n]|^2                        (instantaneous power)
  bg[n]  = α·bg[n-1] + (1-α)·p[n]          (1-pole exponential
                                             background reference)
  hit[n] = (p[n] > threshold · bg[n])       (impulse detection)

When ``hit[n]`` is True, sample ``n`` is replaced by the most
recent clean (non-hit) sample — "hold-last-clean" replacement.
This produces near-inaudible artifacts for typical HF impulses
(ignition noise, lightning crashes, switching-supply spikes —
all microseconds to a few hundred microseconds long) while costing
no extra latency and very little CPU.

Two refinements:

1.  **Cosine slew at edges.**  Transitioning abruptly from clean
    to held-clean (or back) introduces a small step that the
    bandpass filter rings on.  We taper the transition over a few
    samples on each side of the blanked region using a cosine
    half-window, so the boundary is C¹-smooth.

2.  **Consecutive-blank cap.**  A continuously-strong signal whose
    instantaneous power runs above ``threshold·bg`` would otherwise
    be blanked indefinitely — wiping out the very signal the
    operator is listening to.  We cap how many samples in a row
    can be blanked at ~25 ms (configurable).  Once the cap is hit,
    the run is forced back to clean and the bg tracker absorbs the
    "signal" so subsequent samples no longer trigger.

Operator-facing knobs (Phase 3.D #2 first cut):

  - **Profile**: Off / Light / Medium / Aggressive / Custom
  - **Threshold**: numerical multiplier of the background reference
    (operator-tunable in Custom; presets pick reasonable values)

Internal constants (advanced, fixed for v1; expose later if
operator field tests show the need):

  - Background time constant: 20 ms
  - Slew length: 4 samples
  - Max consecutive blank: 25 ms

Why pre-decimation
------------------
Lyra's IQ chain decimates the HL2's 96k/192k/384k input down to
48k internally before demod.  An impulse spike at the input rate is
typically only a few samples wide.  After decimation, the same
energy spreads across many more output samples — by the time the
demod sees it, you can't tell which audio samples are impulse.

Running NB at the input rate, before decimation, keeps the
impulses narrow and easy to detect.  This module is therefore
slotted into Radio's IQ pipeline ahead of ``rx_channel.process``.

Performance
-----------
The hot path uses scipy's ``lfilter`` (vectorized 1-pole IIR for
the background tracker) plus NumPy ``where``/``maximum.accumulate``
for the forward-fill replacement.  The consecutive-blank cap
requires a small Python loop over impulse-run boundaries — only
runs when impulses are actually detected, so cost is proportional
to noise activity, not block size.

Streaming state preserved across ``process()`` calls:
- Background-filter state (zi for lfilter)
- Last clean sample (for cross-block hold-replacement)
- Consecutive-blank counter (for cap continuity across blocks)
"""
from __future__ import annotations

from typing import Optional

import numpy as np

try:
    from scipy.signal import lfilter
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


class ImpulseBlanker:
    """IQ-domain impulse blanker — Phase 3.D #2.

    Operates on complex64 IQ blocks at the input sample rate.
    Length-preserving: process() returns an array of the same shape
    as its input.  Designed to live ahead of channel.process() in
    Lyra's audio chain.
    """

    # ── Profile presets ──────────────────────────────────────────
    # threshold = multiplier on background-power reference; samples
    # whose instantaneous power exceeds threshold × bg are flagged
    # as impulses.  Higher threshold → fewer false positives, fewer
    # flagged samples; lower threshold → more aggressive blanking.
    PROFILES: dict[str, dict[str, float]] = {
        "off":        {"threshold": 0.0,  "enabled": False},
        # Catches obvious lightning crashes and high-energy spikes
        # only.  Gentlest setting; very low risk of clipping
        # legitimate signal transients.
        "light":      {"threshold": 12.0, "enabled": True},
        # Default — typical ignition noise + power-line spikes get
        # blanked, most signal transients survive.
        "medium":     {"threshold": 6.0,  "enabled": True},
        # Catches subtle impulse noise too, but more likely to
        # clip the leading edge of fast CW dits or sharp keying.
        "aggressive": {"threshold": 3.0,  "enabled": True},
    }
    DEFAULT_PROFILE: str = "off"

    # ── Internal constants ───────────────────────────────────────
    # Time constant for the exponential-smoothing background tracker.
    # 20 ms is the standard HF-receiver-design value: long enough that
    # a few-sample-wide impulse doesn't pull the bg up significantly,
    # short enough that the tracker follows operator-driven gain
    # changes (S-meter swings, AGC pumping) without lagging.
    BACKGROUND_TC_SECONDS: float = 0.020

    # Cosine half-window slew at impulse-region edges.  Wider slews
    # smooth the transition more but also blanket more legitimate
    # signal.  4 samples is the conventional sweet spot.
    SLEW_SAMPLES: int = 4

    # Maximum consecutive samples that can be blanked.  At 192 kHz,
    # 25 ms = 4800 samples — comfortably longer than any real
    # impulse but far shorter than any real signal we care about
    # not to suppress.  Cap prevents the blanker from "locking on"
    # to a continuous strong carrier.
    MAX_BLANK_DURATION_SEC: float = 0.025

    # Threshold range exposed to operators (Custom profile).  Below
    # 1.5× the bg tracker can't separate signal from noise; above
    # 50× practically nothing gets blanked.
    THRESHOLD_MIN: float = 1.5
    THRESHOLD_MAX: float = 50.0

    def __init__(self, rate: int = 96000) -> None:
        self._rate: int = int(rate)
        self.enabled: bool = False
        self.profile: str = self.DEFAULT_PROFILE
        self._threshold: float = 0.0
        # Filter coefficients for the 1-pole bg tracker.
        # bg[n] = α·bg[n-1] + (1-α)·p[n]
        #   →  b = [1-α]
        #      a = [1, -α]
        self._bg_alpha: float = 0.0
        self._bg_b: np.ndarray = np.zeros(1, dtype=np.float64)
        self._bg_a: np.ndarray = np.zeros(2, dtype=np.float64)
        # lfilter state (zi) carried across process() calls so the
        # bg tracker is continuous block-to-block.
        self._bg_zi: np.ndarray = np.zeros(1, dtype=np.float64)
        # Last clean (non-impulse) IQ sample — used for hold-last
        # replacement when a block starts with an impulse.
        self._last_clean: complex = complex(0.0, 0.0)
        # Consecutive-blank counter, carried across blocks.
        self._blank_run: int = 0
        # Cosine slew lookup (precomputed for SLEW_SAMPLES).
        self._slew_in: np.ndarray = self._cosine_slew_window(
            self.SLEW_SAMPLES, ramp_up=True)
        self._slew_out: np.ndarray = self._cosine_slew_window(
            self.SLEW_SAMPLES, ramp_up=False)
        # Cap on consecutive blanks (samples).
        self._max_blank: int = max(
            1, int(self.MAX_BLANK_DURATION_SEC * self._rate))
        self._apply_profile()
        self._recompute_filter()

    # ── Public API ───────────────────────────────────────────────

    def set_rate(self, rate: int) -> None:
        """Update the input sample rate.  Recomputes bg tracker
        coefficients and the consecutive-blank cap.  Call when
        Radio's IQ rate changes (operator picks 96k → 192k etc.)."""
        new_rate = int(rate)
        if new_rate == self._rate:
            return
        self._rate = new_rate
        self._max_blank = max(
            1, int(self.MAX_BLANK_DURATION_SEC * self._rate))
        self._recompute_filter()
        self.reset()

    def set_profile(self, name: str) -> None:
        """Apply a named preset.  Unknown name falls back to 'off'."""
        name = (name or "").strip().lower()
        if name not in self.PROFILES and name != "custom":
            name = self.DEFAULT_PROFILE
        self.profile = name
        if name == "custom":
            # Custom retains the current threshold; only enable flag
            # follows from the threshold being meaningful.
            self.enabled = self._threshold >= self.THRESHOLD_MIN
        else:
            self._apply_profile()

    def set_threshold(self, threshold: float) -> None:
        """Operator-set threshold; switches profile to 'custom'.

        Clamped to [THRESHOLD_MIN, THRESHOLD_MAX].  When called from
        the UI threshold slider, the profile combo should switch
        to Custom to reflect that the operator is hand-tuning."""
        self._threshold = float(max(
            self.THRESHOLD_MIN,
            min(self.THRESHOLD_MAX, threshold)))
        self.profile = "custom"
        self.enabled = True

    def reset(self) -> None:
        """Drop streaming state — call on stream restart, big freq
        jumps, sink swap.  Background tracker re-initializes from
        the next block; last-clean memory zeroes; consecutive-blank
        counter resets."""
        self._bg_zi = np.zeros_like(self._bg_zi)
        self._last_clean = complex(0.0, 0.0)
        self._blank_run = 0

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Process one IQ block.  Returns same length, complex64.

        When NB is disabled (profile 'off' or threshold below
        THRESHOLD_MIN) returns the input unchanged — cheapest
        possible bypass.

        When NB is enabled, runs the detect-then-replace pipeline:
        - 1-pole bg tracker via scipy.lfilter (vectorized)
        - Threshold compare → impulse mask
        - Forward-fill last-clean over impulse-marked indices
        - Cosine slew at run boundaries
        - Cap consecutive-blank runs
        """
        if not self.enabled or iq.size == 0:
            return iq
        if iq.dtype != np.complex64:
            iq = iq.astype(np.complex64, copy=False)

        # Per-sample power.  Real-valued; same length as iq.
        p = (iq.real.astype(np.float64) ** 2
             + iq.imag.astype(np.float64) ** 2)

        # Background reference — exponential 1-pole IIR, state
        # carried across blocks via _bg_zi.
        if _HAS_SCIPY:
            bg, self._bg_zi = lfilter(
                self._bg_b, self._bg_a, p, zi=self._bg_zi)
        else:
            # Fallback if scipy somehow isn't importable.  Pure-
            # Python recurrence — slower but still correct.
            bg = np.empty_like(p)
            alpha = self._bg_alpha
            prev = float(self._bg_zi[0])
            for i, pi in enumerate(p):
                prev = alpha * prev + (1.0 - alpha) * pi
                bg[i] = prev
            self._bg_zi = np.asarray([prev], dtype=np.float64)

        # Detect impulses.  Add a tiny floor to bg to avoid divide-
        # like behavior right at startup when bg is still ~0.
        bg_floor = np.maximum(bg, 1e-20)
        mask = p > (self._threshold * bg_floor)
        if not mask.any():
            # No impulses in this block — fast path.  Update last-
            # clean to the final sample and we're done.
            self._last_clean = complex(iq[-1])
            self._blank_run = 0
            return iq

        # Apply the consecutive-blank cap.  Walk the mask, force
        # any run longer than _max_blank back to clean (False).
        # _blank_run carries leftover from the previous block so a
        # run that straddles a boundary still gets capped correctly.
        self._enforce_blank_cap(mask)

        # If after the cap there are no impulses left, bail out.
        if not mask.any():
            self._last_clean = complex(iq[-1])
            return iq

        # Forward-fill: for each impulse index, find the most recent
        # non-impulse index.  np.maximum.accumulate over a "clean
        # index" array gives us this in O(N).
        n = iq.size
        clean_idx = np.where(mask, -1, np.arange(n, dtype=np.int64))
        filled_idx = np.maximum.accumulate(clean_idx)
        # filled_idx[i] is the index of the most recent clean sample
        # at or before i.  For impulses at the very start of the
        # block (before any clean sample appears), filled_idx is -1
        # — those get the cross-block last_clean memory instead.
        leading = filled_idx < 0
        replacement = np.empty_like(iq)
        # Indices where we have a clean sample to copy from:
        not_leading = ~leading
        # Use np.maximum to clip -1 to 0 — only valid where
        # not_leading is True; the leading samples get last_clean.
        safe_idx = np.maximum(filled_idx, 0)
        replacement[not_leading] = iq[safe_idx[not_leading]]
        replacement[leading] = self._last_clean

        # Build the output — clean samples pass through, impulse
        # samples take the replacement value.
        out = np.where(mask, replacement, iq).astype(
            np.complex64, copy=False)

        # Cosine slew at impulse-region boundaries — smooth the
        # transition between original and replaced samples so the
        # downstream bandpass filter doesn't ring.  Cheap: only
        # touches the few samples on either side of each run.
        self._apply_slew(out, iq, replacement, mask)

        # Update streaming state for the next block.
        # Last clean sample = the final non-impulse sample in this
        # block (or carry forward if the block ended on an impulse).
        if not mask[-1]:
            self._last_clean = complex(iq[-1])
        # else: keep the existing last_clean (block ended mid-run)

        return out

    # ── Internals ────────────────────────────────────────────────

    def _apply_profile(self) -> None:
        """Pull the named profile's params into instance state.
        Custom is handled by set_threshold directly."""
        p = self.PROFILES.get(self.profile, self.PROFILES["off"])
        self._threshold = float(p["threshold"])
        self.enabled = bool(p["enabled"])

    def _recompute_filter(self) -> None:
        """Rebuild the 1-pole bg-tracker filter coefficients from
        the configured time constant and current sample rate."""
        # α = exp(-1 / (TC × rate)) — standard 1-pole exp-smoothing.
        self._bg_alpha = float(np.exp(
            -1.0 / max(1e-9, self.BACKGROUND_TC_SECONDS * self._rate)))
        self._bg_b = np.asarray([1.0 - self._bg_alpha],
                                 dtype=np.float64)
        self._bg_a = np.asarray([1.0, -self._bg_alpha],
                                 dtype=np.float64)
        # Reset state — filter coefficients changed.
        self._bg_zi = np.zeros(1, dtype=np.float64)

    def _enforce_blank_cap(self, mask: np.ndarray) -> None:
        """Walk the mask and force any consecutive-impulse run
        longer than ``_max_blank`` samples back to clean (False).

        Carries the run-length counter across block boundaries via
        ``self._blank_run`` — a run that started in the previous
        block and would have already exceeded the cap is capped
        starting from sample 0 of this block.

        Mutates ``mask`` in place.
        """
        cap = self._max_blank
        run = self._blank_run
        # Bool view for fast item access in the Python loop.
        m = mask  # alias
        for i in range(m.size):
            if m[i]:
                run += 1
                if run > cap:
                    m[i] = False
                    # Don't reset run — the operator's continuous
                    # signal will keep running over the threshold;
                    # we want to keep forcing it clean until the
                    # bg tracker catches up.  But we also don't
                    # want run to grow unboundedly, so cap it at
                    # cap+1 (which is just above the threshold).
                    run = cap + 1
            else:
                run = 0
        self._blank_run = run

    def _apply_slew(self, out: np.ndarray, iq: np.ndarray,
                    replacement: np.ndarray,
                    mask: np.ndarray) -> None:
        """For each impulse run boundary, blend ``iq`` ↔ ``replacement``
        across the slew window so the transition is C¹-smooth.

        The slew is applied IN PLACE on ``out`` — the caller's
        ``np.where`` sets the inside of each run to replacement
        and the outside to iq; this method overwrites the few
        samples right at each boundary with a cosine-blended mix.

        For a run starting at index ``s`` and ending at ``e`` (so
        mask is True for s..e-1), the slew touches:
        - s-SLEW_SAMPLES .. s-1   (ramping iq → replacement)
        - e .. e+SLEW_SAMPLES-1   (ramping replacement → iq)

        Boundaries near block edges are clipped so we don't write
        outside the array.
        """
        n = out.size
        if n == 0:
            return
        slew = self.SLEW_SAMPLES
        if slew <= 0:
            return
        # Find run starts and ends via mask diff.
        # Pad mask with False on both ends so np.diff catches any
        # run that touches the block edges.
        padded = np.concatenate(([False], mask, [False]))
        d = np.diff(padded.astype(np.int8))
        run_starts = np.where(d == 1)[0]   # indices into mask
        run_ends = np.where(d == -1)[0]    # exclusive end into mask
        win_in = self._slew_in
        win_out = self._slew_out
        for s, e in zip(run_starts, run_ends):
            # In-slew (ramp iq → replacement) just before run start.
            i0 = max(0, s - slew)
            n_in = s - i0
            if n_in > 0:
                idx = np.arange(i0, s)
                w = win_in[slew - n_in:]
                out[idx] = (
                    (1.0 - w) * iq[idx] + w * replacement[idx])
            # Out-slew (ramp replacement → iq) just after run end.
            i1 = min(n, e + slew)
            n_out = i1 - e
            if n_out > 0:
                idx = np.arange(e, i1)
                w = win_out[:n_out]
                out[idx] = (
                    w * replacement[idx] + (1.0 - w) * iq[idx])

    @staticmethod
    def _cosine_slew_window(length: int, ramp_up: bool
                            ) -> np.ndarray:
        """Cosine half-window for edge-slew blending.

        ramp_up=True   → 0 → 1 (used on the leading edge; weight
                        for the replacement/inside-run sample)
        ramp_up=False  → 1 → 0 (used on the trailing edge; weight
                        for the replacement/inside-run sample)

        Length 0 returns an empty array — caller should guard.
        """
        if length <= 0:
            return np.zeros(0, dtype=np.float64)
        # 0.5·(1 - cos(π·k/N)) for k = 1..N gives 0 → 1 over the
        # interval, with smooth derivative at both ends.
        ks = np.arange(1, length + 1, dtype=np.float64)
        w = 0.5 * (1.0 - np.cos(np.pi * ks / (length + 1)))
        return w if ramp_up else w[::-1]
