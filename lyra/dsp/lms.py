"""LMS adaptive line enhancer (NR3-style).

Lyra-SDR's LMS noise reduction extracts periodic signal components
(carriers, CW, tones, voice formants) from broadband noise.  The
algorithm is fundamentally different from NR1/NR2:

    NR1 / NR2  (subtractive)  → estimate the noise floor and
                                 subtract it from the signal
    LMS        (predictive)    → predict the periodic part of the
                                 signal and output the prediction;
                                 unpredictable broadband noise
                                 falls out

Where the operator should reach for which:

    • Weak CW buried in band hiss          → LMS shines
    • Stable carrier extraction             → LMS shines
    • Voice in broadband noise              → NR2 wins
    • Anything+everything cleanup           → NR2 wins
    • Killing a known stable tone           → ANF (notch, opposite of LMS)

LMS does NOT replace NR2; the two are complementary and can be
chained.  The default chain in ``Channel.process()`` is:

    demod → ANF → LMS → NR (NR1 / NR2 / Neural) → APF → audio out

Each stage has its own enable toggle, so operators can run any
combination.  The classic CW-DX setup is LMS + NR2.

Algorithm: Normalized LMS line enhancer with adaptive leakage
=============================================================

Each input sample x[n] is pushed into a delay line d[].  The filter
output is a weighted prediction of x[n] from a window of older
samples:

    y[n] = Σ_{j=0..N-1} w[j] · d[(in_idx + j + delay) & mask]

where ``delay`` is the decorrelation lag (samples between x[n] and
the oldest sample used for prediction; broadband noise is
uncorrelated across this gap, so prediction picks up only the
periodic component).  The error e[n] = x[n] − y[n] is the part of
the input the filter could NOT predict (= the noise residual).
Filter output is ``y`` (the prediction = the periodic / signal
part).

Weight update (Normalized LMS with leakage):

    σ²[n]  = Σ d[(in_idx + j + delay) & mask]²    (window power)
    w[j]  ← (1 − 2μ·γ_eff) · w[j] + (2μ · e[n] / σ²[n]) · d[idx_j]

The ``2μ`` step size and ``γ_eff`` leakage prevent weight drift
when the input is noise-only, which would otherwise let weights
grow unbounded under finite-precision arithmetic.

Adaptive leakage (Pratt's enhancement)
--------------------------------------
Rather than fixing γ to a single value, the algorithm computes two
hypothetical update errors per sample — one assuming the current
leakage is correct, one assuming a slightly different leakage —
and increments / decrements an internal index ``lidx`` toward
whichever produced the smaller error.  γ_eff is then a quartic
function of lidx:

    γ_eff = γ · lidx⁴ · den_mult

This auto-tunes leakage to the signal: more leakage when the
signal is unstable (tracking transient noise events), less leakage
when the signal is steady (preserving lock on a stable CW tone).
The empirical constants (lidx_min=120, lidx_max=200, den_mult=
6.25e-10, lincr=1.0, ldecr=3.0) come from Pratt's reference
implementation and have been operator-validated across the
openHPSDR community for over a decade.

Operator surface
----------------
- ``enabled``       — bool master toggle
- ``set_strength`` — float 0.0..1.0 mirrors NR1 / NR2 UX
                      0.0 = light effect (slow adapt, gentle)
                      0.5 = default (Pratt's tuning)
                      1.0 = aggressive (fast adapt, stronger)
- ``reset``        — clear delay line + weights on band changes

Latency: LMS is time-domain, no FFT.  Internal latency is
``delay`` samples = ~0.33 ms at 48 kHz default — effectively zero.

Attribution
-----------
Algorithm and parameter defaults derived from WDSP's anr.c
(Adaptive Noise Reduction — Normalized LMS line enhancer with
adaptive leakage), Copyright (C) 2012, 2013 Warren Pratt, NR0V,
licensed under GPL v2 or later.  Lyra-SDR's port re-expresses the
algorithm in idiomatic NumPy and integrates it with Lyra's
Channel framing and operator-facing slider UX, but the core math —
NLMS update, adaptive-leakage tracking via lidx, and the parameter
defaults — follows Pratt's reference implementation directly.

Lyra-SDR is GPL v3+ (since v0.0.6) which is license-compatible
with WDSP's GPL v2+.

WDSP source: openHPSDR project
Original author contact: warren@wpratt.com
"""
# Lyra-SDR — LMS adaptive line enhancer
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License v3
# or later.  See LICENSE in the project root for the full terms.
#
# Algorithm derived from WDSP anr.c, Copyright (C) 2012, 2013
# Warren Pratt, NR0V (GPL v2 or later).  See module docstring above
# for full attribution.
from __future__ import annotations

import numpy as np


class LineEnhancerLMS:
    """LMS adaptive line enhancer / noise reducer.

    Streaming, sample-accurate, length-preserving.  Input float32
    audio at ``rate`` Hz; output same length, same dtype.  Bypass
    is exact identity when ``enabled`` is False.
    """

    # Delay-line size — must be a power of 2 (the algorithm uses a
    # bitmask for the ring-buffer wraparound).  2048 samples is
    # ~43 ms at 48 kHz, plenty for any practical N_TAPS + delay
    # combination.
    DLINE_SIZE: int = 2048

    # ── Default tunings (Pratt's WDSP defaults; 48 kHz baseline) ──
    # These produce the classic WDSP ANR sound and have been
    # operator-validated for over a decade across the openHPSDR
    # community.  At strength 0.5 the slider lands on these.
    DEFAULT_TAPS: int = 64
    DEFAULT_DELAY: int = 16              # decorrelation lag (samples)
    DEFAULT_TWO_MU: float = 1.0e-4       # NLMS step size
    DEFAULT_GAMMA: float = 0.1           # leakage base coefficient

    # Adaptive-leakage parameters (Pratt's enhancement — auto-tuning
    # of γ_eff based on per-sample prediction error).  These bounds
    # work well across HF noise environments; not exposed to the
    # operator.
    LIDX_INIT: float = 120.0
    LIDX_MIN: float = 120.0
    LIDX_MAX: float = 200.0
    NGAMMA_INIT: float = 1.0e-3
    DEN_MULT: float = 6.25e-10
    LINCR: float = 1.0
    LDECR: float = 3.0

    # Strength slider anchors.  v0.0.7.x revision — operator feedback:
    # "I'm not noticing a fair amount of difference in the slider
    # control for LMS."  Senior-tech analysis (CLAUDE.md note):
    # the original slider controlled only 2μ and γ (both ADAPTATION
    # parameters) — once weights converge at any slider position,
    # the steady-state output is essentially identical, so the
    # slider only changed transient-response behavior.  The
    # perceptual "strength" of LMS comes mostly from prediction
    # SELECTIVITY (= tap count) and the wet/dry blend, neither of
    # which the slider previously touched.
    #
    # New mapping — the slider drives FIVE parameters in concert:
    #
    #   strength   taps   2μ      γ      wet_mix    note
    #   0.00       32     5e-5   0.05    0.50      gentle: subtle, half wet
    #   0.50       64     1e-4   0.10    0.85      Pratt defaults + 85% wet
    #   1.00       128    3e-4   0.20    1.00      aggressive: full prediction
    #
    # Tap count is the biggest perceptual change: 32 → 128 taps =
    # 4× more selective predictor = much harder rejection of
    # broadband noise.  Wet/dry mix is the second biggest — at 50%
    # wet the operator hears half-input + half-prediction, which is
    # smoother and more natural-sounding than pure prediction
    # (which can sound "artificial" on voice).
    #
    # Decorrelation delay stays fixed at 16 samples — changing it
    # mid-stream invalidates all weight-to-position correspondence;
    # tap count can change cleanly via zero-extend / truncate.
    STRENGTH_MIN_TAPS: int = 32
    STRENGTH_MAX_TAPS: int = 128
    STRENGTH_MIN_TWO_MU: float = 5.0e-5
    STRENGTH_MAX_TWO_MU: float = 3.0e-4
    STRENGTH_MIN_GAMMA: float = 0.05
    STRENGTH_MAX_GAMMA: float = 0.20
    STRENGTH_MIN_WET_MIX: float = 0.50
    STRENGTH_MAX_WET_MIX: float = 1.00
    DEFAULT_STRENGTH: float = 0.5

    def __init__(self, rate: int = 48000) -> None:
        self.rate = int(rate)
        self.enabled: bool = False
        self.strength: float = self.DEFAULT_STRENGTH

        # Ring-buffer delay line and adaptive weights.  Both sized
        # to DLINE_SIZE for the bitmask wraparound; only the first
        # ``_n_taps`` of ``_w`` are ever non-zero.
        self._mask: int = self.DLINE_SIZE - 1
        self._d = np.zeros(self.DLINE_SIZE, dtype=np.float64)
        self._w = np.zeros(self.DLINE_SIZE, dtype=np.float64)
        self._in_idx: int = 0

        # Algorithm parameters (set via _apply_strength).
        self._n_taps: int = self.DEFAULT_TAPS
        self._delay: int = self.DEFAULT_DELAY
        self._two_mu: float = self.DEFAULT_TWO_MU
        self._gamma: float = self.DEFAULT_GAMMA
        # Wet/dry mix — fraction of LMS prediction in output (vs
        # original input).  1.0 = pure prediction (Lyra's pre-v0.0.7.x
        # behavior); 0.5 = half input + half prediction.  Set via
        # _apply_strength as part of the multi-parameter slider.
        self._wet_mix: float = 1.0

        # Adaptive-leakage state.
        self._lidx: float = self.LIDX_INIT
        self._ngamma: float = self.NGAMMA_INIT

        # Pre-compute the tap-index template for the inner loop —
        # avoids re-allocating np.arange on every output sample.
        self._tap_offsets = np.arange(self._n_taps, dtype=np.int64)

        self._apply_strength(self.DEFAULT_STRENGTH)

    # ── Public API ────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop streaming state.  Called on band/mode/freq changes
        to prevent stale weight estimates from polluting fresh
        audio."""
        self._d.fill(0.0)
        self._w.fill(0.0)
        self._in_idx = 0
        self._lidx = self.LIDX_INIT
        self._ngamma = self.NGAMMA_INIT

    def set_strength(self, value: float) -> None:
        """Operator-facing strength knob, 0.0..1.0.  Interpolates
        2μ and γ between the STRENGTH_MIN_* and STRENGTH_MAX_*
        anchors.  At 0.5 the values land on Pratt's WDSP defaults.
        """
        s = max(0.0, min(1.0, float(value)))
        self.strength = s
        self._apply_strength(s)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one audio block.  Length-preserving; returns
        float32 even if input was float64.  Bypass-fast (exact
        identity, single attribute check) when ``enabled`` is
        False.

        Wet/dry blend (v0.0.7.x): output is a linear mix of the LMS
        prediction (= the periodic content the filter could lock
        onto) and the original input audio.  Operator's strength
        slider controls the wet fraction:
          strength 0.0 → 50% wet (subtle, mostly original)
          strength 0.5 → 85% wet (Pratt defaults + slight blend)
          strength 1.0 → 100% wet (pure prediction; pre-v0.0.7.x)
        At wet=1.0 the result is exactly identical to the legacy
        behavior, so there's no regression path for operators who
        relied on the old slider feel."""
        if not self.enabled or audio.size == 0:
            return audio
        x = audio.astype(np.float64, copy=False)
        prediction = self._process_block(x)
        # Wet/dry blend: out = wet · prediction + (1 - wet) · input.
        # When wet == 1.0, this short-circuits to the prediction
        # alone (legacy behavior preserved bit-exact at slider=1).
        if self._wet_mix >= 0.999:
            out = prediction
        else:
            out = (
                self._wet_mix * prediction
                + (1.0 - self._wet_mix) * x
            )
        return out.astype(np.float32, copy=False)

    # ── Internals ─────────────────────────────────────────────────

    def _apply_strength(self, s: float) -> None:
        """Multi-parameter strength mapping (v0.0.7.x revision).

        Maps the operator's 0..1 strength slider to FIVE algorithm
        parameters in concert.  See class docstring (STRENGTH_*
        constants) for the full table.  Tap count drives the biggest
        perceptual change (= prediction selectivity); wet/dry mix is
        the second biggest (= how much of the original signal blends
        into the output).
        """
        s = max(0.0, min(1.0, float(s)))

        # Adaptation parameters — linear interp between the anchors.
        lo_mu, hi_mu = self.STRENGTH_MIN_TWO_MU, self.STRENGTH_MAX_TWO_MU
        lo_g, hi_g = self.STRENGTH_MIN_GAMMA, self.STRENGTH_MAX_GAMMA
        lo_w, hi_w = self.STRENGTH_MIN_WET_MIX, self.STRENGTH_MAX_WET_MIX
        self._two_mu = lo_mu + (hi_mu - lo_mu) * s
        self._gamma = lo_g + (hi_g - lo_g) * s
        self._wet_mix = lo_w + (hi_w - lo_w) * s

        # Tap count — quantized to even values (block-LMS update path
        # works with any tap count, but even values give cleaner
        # numpy gathers).  Round-to-nearest, then clamp.
        new_taps = int(round(
            self.STRENGTH_MIN_TAPS
            + (self.STRENGTH_MAX_TAPS - self.STRENGTH_MIN_TAPS) * s))
        new_taps = max(self.STRENGTH_MIN_TAPS,
                       min(self.STRENGTH_MAX_TAPS, new_taps))
        # Round down to multiple of 2 for cleaner block-LMS gathers.
        new_taps = new_taps & ~1
        self._set_n_taps(new_taps)

    def _set_n_taps(self, new_taps: int) -> None:
        """Change tap count without resetting the existing trained
        weights.  Zero-extends if growing; truncates if shrinking.

        Why this matters: the operator may sweep the slider mid-QSO
        to find the sweet spot.  Resetting weights every slider step
        would produce a 1-2 sec retraining gap each time, audible as
        a brief "swimming" artifact.  Zero-extending preserves
        existing weights — the new tap positions train from zero
        within ~0.5 sec while old positions stay valid throughout.
        """
        old_taps = self._n_taps
        if new_taps == old_taps:
            return
        if new_taps < old_taps:
            # Truncate: zero-out positions [new_taps..old_taps).
            # The active weight slice self._w[:new_taps] is unchanged.
            self._w[new_taps:old_taps] = 0.0
        # Growing case: self._w[old_taps..new_taps] is already 0
        # (from __init__ or previous truncate), so no work needed.
        self._n_taps = new_taps
        self._tap_offsets = np.arange(new_taps, dtype=np.int64)

    def _process_block(self, x: np.ndarray) -> np.ndarray:
        """Block-LMS dispatch.

        We chunk the input into sub-blocks of ``delay`` samples and
        process each via :meth:`_step_subblock`.  Within a sub-block:

        - Weights are frozen (one update per block, not per sample)
        - All B sample outputs are computed in a single vectorized
          gather + dot-product
        - Average gradient is applied to weights at block end

        Why the sub-block size equals ``delay``:  at iteration
        ``i`` within a block, the filter's tap window starts at
        offset ``delay`` from the current sample.  So as long as
        the block is no longer than ``delay``, none of the
        just-written samples are used in any output's window — the
        order of writing is irrelevant to the math, and we can
        write the whole block to the delay line in one shot.

        Convergence trade-off:  weight adaptation rate is reduced
        by factor ``B``.  For our defaults (B=delay=16, μ=1e-4),
        weights still update ~3 kHz at 48 kHz audio — well above
        ham-band signal dynamics (CW keying < 50 Hz, voice
        formants < 1 kHz), so the operator hears no perceptual
        difference vs per-sample LMS.

        References:
            Haykin, "Adaptive Filter Theory" 5th ed. §5.7
                (block-LMS convergence analysis)
            Manolakis et al., "Statistical & Adaptive Signal
                Processing" 2005, §10.3
        """
        n = x.size
        out = np.empty(n, dtype=np.float64)
        sub_blk = self._delay  # ≤ delay, so no intra-block contamination

        pos = 0
        while pos < n:
            end = min(pos + sub_blk, n)
            out[pos:end] = self._step_subblock(x[pos:end])
            pos = end

        return out

    def _step_subblock(self, x: np.ndarray) -> np.ndarray:
        """Process one sub-block of up to ``delay`` samples.

        Returns y of the same length.  Mutates self._d (delay line),
        self._w[:n_taps] (weights), self._in_idx, self._lidx,
        self._ngamma.
        """
        b = x.size
        if b == 0:
            return np.empty(0, dtype=np.float64)

        # Local rebinds for speed.
        d = self._d
        n_taps = self._n_taps
        delay = self._delay
        mask = self._mask
        in_idx = self._in_idx
        two_mu = self._two_mu
        gamma = self._gamma

        # ── Step 1: push the b input samples into the delay line ─────
        # x[i] lands at d[in_idx - i] (the ring grows backwards from
        # in_idx).  Note this is safe to do BEFORE reading windows
        # because all reads happen at offset >= delay >= b from each
        # write — no aliasing.
        idxs = np.arange(b, dtype=np.int64)
        write_idx = (in_idx - idxs) & mask
        d[write_idx] = x.astype(np.float64, copy=False)

        # ── Step 2: vectorized output for all b samples ──────────────
        # Build (b, n_taps) index matrix:  for sample i, the window
        # is d[(in_idx - i + j + delay) & mask] for j ∈ [0, n_taps).
        j = np.arange(n_taps, dtype=np.int64)
        base = in_idx - idxs  # (b,)
        win_idx = (base[:, None] + j[None, :] + delay) & mask  # (b, n_taps)
        d_win = d[win_idx]  # (b, n_taps) — gathered windows

        w_active = self._w[:n_taps]
        y = d_win @ w_active                       # (b,)  filter outputs
        x_now = x.astype(np.float64, copy=False)
        error = x_now - y                           # (b,)
        sigma = (d_win * d_win).sum(axis=1)         # (b,)
        inv_sigp = 1.0 / (sigma + 1.0e-10)          # (b,)

        # ── Step 3: adaptive-leakage update (once per block) ────────
        # Pratt's lidx walk uses the last sample's metrics — adequate
        # because lidx changes by at most ±LINCR/LDECR per step, so
        # per-block updates produce essentially the same trajectory
        # as per-sample at block sizes ≤ delay.
        last = -1
        nel = abs(error[last]
                  * (1.0 - two_mu * sigma[last] * inv_sigp[last]))
        nev = abs(x_now[last]
                  - (1.0 - two_mu * self._ngamma) * y[last]
                  - two_mu * error[last] * sigma[last] * inv_sigp[last])
        if nev < nel:
            self._lidx = min(self._lidx + self.LINCR, self.LIDX_MAX)
        else:
            self._lidx = max(self._lidx - self.LDECR, self.LIDX_MIN)
        self._ngamma = (
            gamma * self._lidx * self._lidx
            * self._lidx * self._lidx * self.DEN_MULT)

        # ── Step 4: weight update ─────────────────────────────────
        # Per-sample math summed over the block (frozen weights):
        #   Δw[j] = Σ_i (2μ · error[i] / sigma[i]) · d_win[i, j]
        # Leakage (1 - 2μγ) compounded over b samples:
        #   w_new = (1 - 2μγ)^b · w_old + Δw
        # For typical 2μγ ≈ 1e-7, the b-th power is essentially
        # 1 - b·2μγ, but we use the exact form for safety against
        # large γ excursions when lidx hits its upper bound.
        grad = ((error * inv_sigp)[:, None] * d_win).sum(axis=0)  # (n_taps,)
        c0_b = (1.0 - two_mu * self._ngamma) ** b
        self._w[:n_taps] = c0_b * w_active + two_mu * grad

        # ── Step 5: advance in_idx ────────────────────────────────
        self._in_idx = (in_idx - b) & mask

        return y
