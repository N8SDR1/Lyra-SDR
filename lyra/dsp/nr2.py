"""NR2 — MMSE-LSA noise reduction (Phase 3.D #4).

Ephraim-Malah Minimum Mean-Squared Error Log-Spectral Amplitude
estimator: the state-of-the-art for non-neural speech noise
reduction.  Eliminates the "musical noise" / "underwater"
artifact that plagues classical spectral subtraction (NR1).

Algorithm
---------
For each STFT frame at 48 kHz audio rate:

  Y(k)    = FFT of windowed input frame
  γ(k)    = a-posteriori SNR  = |Y(k)|² / λ_d(k)
  ξ(k)    = a-priori SNR       (smoothed across frames — the key)
  v(k)    = ξ(k) · γ(k) / (1 + ξ(k))
  G(k)    = (ξ/(1+ξ)) · exp(½ · ∫_v^∞ (e^-t / t) dt)
  Ŝ(k)    = G(k) · Y(k)        (estimated clean spectrum)

The a-priori SNR ξ is updated using Ephraim-Malah's
"decision-directed" approach:

  ξ(k)[n] = α · |G[n-1] · Y[n-1]|² / λ_d[n]
              + (1-α) · max(γ[n] - 1, 0)

α ≈ 0.98.  This smoothing is what kills the musical-noise
artifact: the per-bin gain is now mostly a function of last
frame's gain rather than this frame's instantaneous SNR, so
random bin flicker is gone.

The MMSE-LSA gain function involves the exponential integral
E1(v) = ∫_v^∞ (e^-t / t) dt, which is expensive to evaluate
per-bin per-frame.  We pre-compute G(γ, ξ) as a 2-D lookup
table at init time and use vectorized bilinear interpolation
at runtime.

Noise tracker (v1)
------------------
Continuous-spectral-minimum tracker — per-bin asymmetric
exponential smoothing:

  if |Y(k)|² < λ_d(k):
      λ_d(k) ← α_track · λ_d(k) + (1 - α_track) · |Y(k)|²
  else:
      λ_d(k) ← β_release · λ_d(k) + (1 - β_release) · |Y(k)|²

α_track ≈ 0.95 (fast track-down on quiet bins → finds the
noise floor quickly), β_release ≈ 0.9995 (very slow track-up
on loud bins → speech doesn't pollute the noise estimate).

This is simpler than full Martin (2001) minimum-statistics
(~200 lines) and competent for stationary HF band noise.
Upgradeable to full minimum-statistics in a future commit if
field tests show non-stationary noise causes issues.

References
----------
- Ephraim, Malah (1985):  "Speech Enhancement Using a Minimum
  Mean-Square Error Log-Spectral Amplitude Estimator", IEEE
  Trans. ASSP
- Martin (2001):  "Noise Power Spectral Density Estimation Based
  on Optimal Smoothing and Minimum Statistics", IEEE Trans. SAP
- Standard treatment in adaptive-filter / speech-enhancement
  textbooks (Haykin, Hänsler, etc.)

Implemented clean-room from these public DSP literature
references.  No code from any other SDR or speech-enhancement
software has been read or adapted.

Operator-facing knobs
---------------------
- enabled (bool)
- aggression (0.0 .. 1.5, default 1.0):
    Scales the gain reduction.
    0.0 = unity gain (no NR)
    1.0 = full MMSE-LSA
    >1.0 = "more aggressive than vanilla" (gain^aggression)
- musical_noise_smoothing (bool, default True):
    True  → decision-directed α = 0.98 (full anti-musical-noise)
    False → α = 0.5  (closer to NR1 behavior, useful for A/B)
- speech_aware (bool, default False):
    True  → simple VAD reduces suppression during detected
            voice (preserves consonants better)
    False → uniform processing
"""
from __future__ import annotations

import os
from typing import Callable, Optional

import numpy as np

# Min-stats tracker shared with NR1 — same algorithm, same window
# sizing, same bias correction.  Importing keeps the implementation
# in one place; if BIAS_CORRECTION needs tuning later the change
# applies to both processors automatically.
from lyra.dsp.nr import _MinStatsTracker

try:
    from scipy.special import exp1 as _scipy_exp1
    from scipy.special import i0 as _scipy_i0
    from scipy.special import i1 as _scipy_i1
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _bessel_i0(x: np.ndarray) -> np.ndarray:
    """Modified Bessel function of the first kind, order 0.

    Polynomial approximation when scipy isn't available — matches
    WDSP's bessI0() (Pratt; from Abramowitz & Stegun "Handbook of
    Mathematical Functions" §9.8 and Zhang & Jin "Computation of
    Special Functions").  Used at init only (LUT pre-compute), so
    fallback accuracy is fine.
    """
    if _HAS_SCIPY:
        return _scipy_i0(x)
    # Polynomial fallback.
    x = np.abs(x.astype(np.float64))
    out = np.empty_like(x)
    small = x <= 3.75
    if small.any():
        xs = x[small]
        p = (xs / 3.75) ** 2
        out[small] = (((((((0.0045813 * p
                          + 0.0360768) * p
                          + 0.2659732) * p
                          + 1.2067492) * p
                          + 3.0899424) * p
                          + 3.5156229) * p
                          + 1.0))
    big = ~small
    if big.any():
        xb = x[big]
        p = 3.75 / xb
        out[big] = (np.exp(xb) / np.sqrt(xb)
                    * ((((((((0.00392377 * p
                             - 0.01647633) * p
                             + 0.02635537) * p
                             - 0.02057706) * p
                             + 0.00916281) * p
                             - 0.00157565) * p
                             + 0.00225319) * p
                             + 0.01328592) * p
                             + 0.39894228))
    return out


def _bessel_i1(x: np.ndarray) -> np.ndarray:
    """Modified Bessel function of the first kind, order 1.
    Companion to ``_bessel_i0``; same source/attribution.
    """
    if _HAS_SCIPY:
        return _scipy_i1(x)
    x = np.abs(x.astype(np.float64))
    out = np.empty_like(x)
    small = x <= 3.75
    if small.any():
        xs = x[small]
        p = (xs / 3.75) ** 2
        out[small] = xs * ((((((0.00032411 * p
                              + 0.00301532) * p
                              + 0.02658733) * p
                              + 0.15084934) * p
                              + 0.51498869) * p
                              + 0.87890594) * p
                              + 0.5)
    big = ~small
    if big.any():
        xb = x[big]
        p = 3.75 / xb
        out[big] = (np.exp(xb) / np.sqrt(xb)
                    * ((((((((-0.00420059 * p
                             + 0.01787654) * p
                             - 0.02895312) * p
                             + 0.02282967) * p
                             - 0.01031555) * p
                             + 0.00163801) * p
                             - 0.00362018) * p
                             - 0.03988024) * p
                             + 0.39894228))
    return out


class _MartinMinStatsTracker:
    """Full Martin (2001) minimum-statistics noise-PSD tracker.

    Direct port from WDSP emnr.c ``LambdaD`` function — Copyright
    Warren Pratt NR0V, GPL v2 or later.  Lyra-SDR is GPL v3+ since
    v0.0.6, license-compatible.

    Replaces the simplified ring-buffer per-bin minimum we use in
    NR1 (and used to use in NR2).  The full Martin algorithm:

    1. Smooth the periodogram |Y|² with a per-bin adaptive
       coefficient α̂[k] that responds to local SNR.
    2. Estimate the variance of the smoothed periodogram via
       p̄ / p²̄ moments → equivalent degrees of freedom Q_eq[k].
    3. Compute bias correction b_min[k] from Q_eq[k] using
       Pratt's Mvals/Hvals interpolation tables.
    4. Find current sub-window minimum (D = U·V frames).
    5. Maintain a ring of U sub-window minima; the running
       per-bin minimum across this ring is pmin_u[k].
    6. Apply noise-slope-max safety: prevents the minimum
       estimate from being trapped at unrealistically low
       levels during quiet stretches.
    7. Emit lambda_d[k] = noise PSD estimate.

    State per instance:
      ~13 per-bin float64 arrays plus a U×msize ring buffer.
      For msize=129 (256-pt FFT) and U=8: ~13 KB total.

    Per-frame cost: ~10 vectorized ops over msize bins, plus one
    sub-window settlement once every V frames (~5 ms at 48 kHz/
    128 hop).  Roughly 4× the cost of our simplified tracker but
    still microseconds per block — negligible.
    """

    # Martin's bias-correction interpolation tables (lines 303-308
    # of emnr.c).  D values along x-axis, M (compensation factor)
    # and H (auxiliary value, unused in our LambdaD path) along y.
    _DVALS = np.array([
        1.0, 2.0, 5.0, 8.0, 10.0, 15.0, 20.0, 30.0, 40.0,
        60.0, 80.0, 120.0, 140.0, 160.0, 180.0, 220.0, 260.0, 300.0
    ], dtype=np.float64)
    _MVALS = np.array([
        0.000, 0.260, 0.480, 0.580, 0.610, 0.668, 0.705, 0.762, 0.800,
        0.841, 0.865, 0.890, 0.900, 0.910, 0.920, 0.930, 0.935, 0.940
    ], dtype=np.float64)

    @classmethod
    def _interp_M(cls, x: float) -> float:
        """log-x linear-y interpolation of M(D) (matches WDSP
        ``interpM``).  Used to look up Martin bias correction at
        the framework's actual D and V values."""
        xv = cls._DVALS
        yv = cls._MVALS
        if x <= xv[0]:
            return float(yv[0])
        if x >= xv[-1]:
            return float(yv[-1])
        # Find bracketing index.
        idx = 1
        while x > xv[idx]:
            idx += 1
        xllow = float(np.log10(xv[idx - 1]))
        xlhigh = float(np.log10(xv[idx]))
        frac = (float(np.log10(x)) - xllow) / (xlhigh - xllow)
        return float(yv[idx - 1] + frac * (yv[idx] - yv[idx - 1]))

    def __init__(self, msize: int, hop: int, rate: int):
        self.msize = int(msize)
        self.hop = int(hop)
        self.rate = int(rate)

        # Smoothing-factor time constants — match WDSP.  The 8000 Hz
        # reference comes from Martin's original 8 kHz speech work;
        # the exp() converts to per-frame factor at our actual rate.
        ref_rate = 8000.0
        ref_hop = 128.0

        def _alpha_for(ref_factor: float) -> float:
            tau = -ref_hop / ref_rate / np.log(ref_factor)
            return float(np.exp(-self.hop / self.rate / tau))

        self.alphaCsmooth = _alpha_for(0.7)
        self.alphaMax = _alpha_for(0.96)
        self.alphaCmin = _alpha_for(0.7)
        self.alphaMin_max_value = _alpha_for(0.3)
        self.snrq = -self.hop / (0.064 * self.rate)
        self.betamax = _alpha_for(0.8)
        self.invQeqMax = 0.5
        self.av = 2.12

        # Sub-window framework: D = U × V frames over Dtime seconds.
        # Default Dtime = 8 · 12 · 128 / 8000 = 1.536 sec.  Same as
        # WDSP — the U×V framework is what makes Martin's bias
        # correction tractable.
        self.Dtime = 8.0 * 12.0 * 128.0 / 8000.0
        self.U = 8
        self.V = max(4, int(0.5 + self.Dtime * self.rate
                            / (self.U * self.hop)))
        new_U = int(0.5 + self.Dtime * self.rate / (self.V * self.hop))
        self.U = max(1, new_U)
        self.D = self.U * self.V

        self.MofD = self._interp_M(self.D)
        self.MofV = self._interp_M(self.V)

        self.invQbar_points = np.array([0.03, 0.05, 0.06, 1.0e300],
                                       dtype=np.float64)
        # noise_slope_max thresholds — converted to per-frame
        # factors at the active rate using V·hop/rate as the
        # sub-window duration.
        sub_dur = self.V * self.hop / self.rate
        ref_dur = 12.0 * 128.0 / 8000.0
        self.nsmax = np.array([
            10.0 ** (np.log10(8.0) / ref_dur * sub_dur),
            10.0 ** (np.log10(4.0) / ref_dur * sub_dur),
            10.0 ** (np.log10(2.0) / ref_dur * sub_dur),
            10.0 ** (np.log10(1.2) / ref_dur * sub_dur),
        ], dtype=np.float64)

        # Per-bin state.  Initialized to lambda_y = 0.5 per WDSP.
        self.alphaC = 1.0
        self.subwc = self.V
        self.amb_idx = 0
        self._init_state()

    def _init_state(self) -> None:
        m = self.msize
        seed = 0.5
        self.p = np.full(m, seed, dtype=np.float64)
        self.sigma2N = np.full(m, seed, dtype=np.float64)
        self.pbar = np.full(m, seed, dtype=np.float64)
        self.pmin_u = np.full(m, seed, dtype=np.float64)
        self.p2bar = np.full(m, seed * seed, dtype=np.float64)
        self.actmin = np.full(m, 1.0e300, dtype=np.float64)
        self.actmin_sub = np.full(m, 1.0e300, dtype=np.float64)
        self.actminbuff = np.full((self.U, m), 1.0e300,
                                   dtype=np.float64)
        self.lmin_flag = np.zeros(m, dtype=np.int32)
        self.alphaOptHat = np.ones(m, dtype=np.float64)
        self.alphaHat = np.ones(m, dtype=np.float64)
        self.Qeq = np.ones(m, dtype=np.float64)
        self.bmin = np.ones(m, dtype=np.float64)
        self.bmin_sub = np.ones(m, dtype=np.float64)
        self.k_mod = np.zeros(m, dtype=np.bool_)

    def reset(self) -> None:
        """Reset all state — call on band/mode/freq change."""
        self.alphaC = 1.0
        self.subwc = self.V
        self.amb_idx = 0
        self._init_state()

    def update(self, lambda_y: np.ndarray) -> np.ndarray:
        """Push one frame's |Y|² spectrum, return the noise-PSD
        estimate (lambda_d) for this frame.

        ``lambda_y`` must be a 1-D float64 array of size ``msize``.
        Returns a copy of the current ``sigma2N`` so callers can
        safely overwrite it.
        """
        m = self.msize

        # Step 1: aggregates over bins.
        sum_prev_p = float(np.sum(self.p))
        sum_lambda_y = float(np.sum(lambda_y))
        sum_prev_sigma2N = float(np.sum(self.sigma2N))

        # Step 2: alphaOptHat per bin, floored by alphaMin.
        f0 = self.p / self.sigma2N - 1.0
        self.alphaOptHat = 1.0 / (1.0 + f0 * f0)
        snr = sum_prev_p / max(sum_prev_sigma2N, 1.0e-300)
        alphaMin = min(self.alphaMin_max_value,
                       snr ** self.snrq if snr > 0 else self.alphaMin_max_value)
        np.maximum(self.alphaOptHat, alphaMin, out=self.alphaOptHat)

        # Step 3: alphaC (scalar) and alphaHat (per-bin).
        f1 = sum_prev_p / max(sum_lambda_y, 1.0e-300) - 1.0
        alphaCtilda = 1.0 / (1.0 + f1 * f1)
        self.alphaC = (
            self.alphaCsmooth * self.alphaC
            + (1.0 - self.alphaCsmooth) * max(alphaCtilda, self.alphaCmin))
        f2 = self.alphaMax * self.alphaC
        self.alphaHat = f2 * self.alphaOptHat

        # Step 4: smoothed periodogram p[k].
        self.p = self.alphaHat * self.p + (1.0 - self.alphaHat) * lambda_y

        # Step 5: variance estimation → Qeq[k].
        beta = np.minimum(self.betamax, self.alphaHat * self.alphaHat)
        self.pbar = beta * self.pbar + (1.0 - beta) * self.p
        self.p2bar = beta * self.p2bar + (1.0 - beta) * self.p * self.p
        varHat = self.p2bar - self.pbar * self.pbar
        # Floor variance at 0 (sometimes goes slightly negative
        # from numerical roundoff at very-low-noise bins).
        np.maximum(varHat, 0.0, out=varHat)
        invQeq = varHat / np.maximum(2.0 * self.sigma2N * self.sigma2N,
                                      1.0e-300)
        np.minimum(invQeq, self.invQeqMax, out=invQeq)
        self.Qeq = 1.0 / np.maximum(invQeq, 1.0e-300)
        invQbar = float(np.mean(invQeq))

        # Step 6: bias correction bmin[k].
        bc = 1.0 + self.av * np.sqrt(invQbar)
        QeqTilda = (self.Qeq - 2.0 * self.MofD) / max(1.0 - self.MofD, 1.0e-300)
        QeqTildaSub = (self.Qeq - 2.0 * self.MofV) / max(
            1.0 - self.MofV, 1.0e-300)
        # Guard against zero/negative QeqTilda (happens at SNR
        # extremes where the variance estimator breaks down).
        QeqTilda = np.maximum(QeqTilda, 1.0e-3)
        QeqTildaSub = np.maximum(QeqTildaSub, 1.0e-3)
        self.bmin = 1.0 + 2.0 * (self.D - 1.0) / QeqTilda
        self.bmin_sub = 1.0 + 2.0 * (self.V - 1.0) / QeqTildaSub

        # Step 7: track per-frame sub-window minimum.
        f3 = self.p * self.bmin * bc
        below = f3 < self.actmin
        self.k_mod = below
        self.actmin = np.where(below, f3, self.actmin)
        sub_candidate = self.p * self.bmin_sub * bc
        self.actmin_sub = np.where(below, sub_candidate, self.actmin_sub)

        # Step 8: sub-window roll-over.
        if self.subwc == self.V:
            # Pick noise_slope_max bracket from invQbar.
            if invQbar < self.invQbar_points[0]:
                noise_slope_max = float(self.nsmax[0])
            elif invQbar < self.invQbar_points[1]:
                noise_slope_max = float(self.nsmax[1])
            elif invQbar < self.invQbar_points[2]:
                noise_slope_max = float(self.nsmax[2])
            else:
                noise_slope_max = float(self.nsmax[3])

            # Bins that were updated this sub-window: clear
            # lmin_flag (they got a new minimum, no longer eligible
            # for slope-max promotion).
            self.lmin_flag = np.where(self.k_mod, 0, self.lmin_flag)

            # Push current actmin into the ring buffer.
            self.actminbuff[self.amb_idx] = self.actmin
            # pmin_u[k] = min over U of actminbuff[u][k]
            self.pmin_u = np.min(self.actminbuff, axis=0)

            # Slope-max promotion: bins flagged from a previous
            # sub-window that have a "between" actmin_sub get
            # promoted into pmin_u and propagate to all U buffers.
            promote = ((self.lmin_flag == 1)
                       & (self.actmin_sub < noise_slope_max * self.pmin_u)
                       & (self.actmin_sub > self.pmin_u))
            if promote.any():
                # Update pmin_u where promoted.
                self.pmin_u = np.where(promote, self.actmin_sub,
                                        self.pmin_u)
                # Propagate to all U buffers for promoted bins.
                for ku in range(self.U):
                    self.actminbuff[ku] = np.where(
                        promote, self.actmin_sub, self.actminbuff[ku])

            # Reset for next sub-window.
            self.lmin_flag.fill(0)
            self.actmin.fill(1.0e300)
            self.actmin_sub.fill(1.0e300)
            self.amb_idx = (self.amb_idx + 1) % self.U
            self.subwc = 1
        else:
            if self.subwc > 1:
                # Mid-sub-window update of sigma2N for bins that
                # got a new minimum this frame.  This is what makes
                # the algorithm responsive to noise-floor changes
                # WITHIN a sub-window — without it, sigma2N would
                # only update at sub-window boundaries (every V
                # frames ≈ 192 ms).
                update_mask = self.k_mod
                if update_mask.any():
                    self.lmin_flag = np.where(update_mask, 1,
                                               self.lmin_flag)
                    new_sigma = np.minimum(self.actmin_sub,
                                            self.pmin_u)
                    self.sigma2N = np.where(update_mask, new_sigma,
                                             self.sigma2N)
                    self.pmin_u = np.where(update_mask,
                                            self.sigma2N, self.pmin_u)
            self.subwc += 1

        return self.sigma2N.copy()


def _exponential_integral_e1(x: np.ndarray) -> np.ndarray:
    """Compute E1(x) = ∫_x^∞ (e^-t / t) dt, vectorized.

    Uses scipy.special.exp1 when available; falls back to a
    series + asymptotic approximation for environments without
    scipy.  Only called at init (gain-table pre-compute), so
    the fallback's slightly-lower accuracy is acceptable.
    """
    if _HAS_SCIPY:
        return _scipy_exp1(x)
    # Fallback: piecewise series + asymptotic.
    # For x small (< 1), use the series:
    #   E1(x) = -γ - ln(x) - Σ_{n=1}^∞ (-1)^n · x^n / (n · n!)
    # For x large (≥ 1), use the asymptotic:
    #   E1(x) ≈ (e^-x / x) · (1 - 1/x + 2/x² - 6/x³ + ...)
    EULER = 0.5772156649015329
    result = np.empty_like(x, dtype=np.float64)
    small = x < 1.0
    if small.any():
        xs = x[small]
        s = np.zeros_like(xs)
        # Sum 30 terms of the series — plenty for x < 1.
        sign = -1.0
        xn = xs.copy()
        nfact = 1.0
        for n in range(1, 30):
            sign = -sign
            nfact *= n
            s = s + sign * xn / (n * nfact)
            xn = xn * xs
        result[small] = -EULER - np.log(xs) - s
    big = ~small
    if big.any():
        xb = x[big]
        # Asymptotic expansion, 8 terms.
        s = np.ones_like(xb)
        term = np.ones_like(xb)
        for k in range(1, 9):
            term = term * (-k) / xb
            s = s + term
        result[big] = np.exp(-xb) / xb * s
    return result


class EphraimMalahNR:
    """NR2 — MMSE-LSA noise reducer (Phase 3.D #4).

    Operates on float32 mono audio at 48 kHz.  Length-preserving:
    process() returns the same shape as input (with ~2.7 ms
    internal latency from the 50% overlap-add framing — same as
    NR1).

    Designed as a drop-in alternative to SpectralSubtractionNR
    (NR1).  Channel can route audio through either based on which
    NR profile the operator picked.
    """

    # ── STFT / framing constants ─────────────────────────────────
    # Match NR1 exactly so they share the same timing characteristics
    # and switching between them is sample-accurate.
    FFT_SIZE: int = 256
    HOP: int = 128

    # ── Noise-tracker constants ──────────────────────────────────
    # α_track: smoothing factor when current bin power is BELOW
    # the running estimate.  Closer to 1 = slower (smoother) track-
    # down; closer to 0 = faster.  0.95 finds the noise floor in
    # ~6 frames (~32 ms) — fast enough to follow band-noise shifts,
    # slow enough to ignore brief silent moments inside speech.
    NOISE_TRACK_DOWN: float = 0.95
    # β_release: smoothing factor when current bin power is ABOVE
    # the running estimate.  Very close to 1 — speech / signal
    # energy should NOT pollute the noise estimate.  0.9995 means
    # the estimate barely budges on a loud bin (~1300 frames =
    # 7 seconds for a 50% rise).  Standard value across MMSE-LSA
    # implementations.
    NOISE_TRACK_UP: float = 0.9995

    # ── A-priori SNR smoothing ───────────────────────────────────
    # Decision-directed update: ξ[n] = α · prior_estimate +
    # (1-α) · current_instantaneous_estimate.  α = 0.98 is the
    # canonical Ephraim-Malah value and is what kills the
    # musical-noise artifact.  Operator can disable
    # (musical_noise_smoothing=False) to A/B against NR1-like
    # behavior — drops α to 0.5 for visible diagnostic difference.
    XI_SMOOTHING_ON: float = 0.98
    XI_SMOOTHING_OFF: float = 0.50
    # Floor on a-priori SNR — Ephraim-Malah recommends -25 dB
    # equivalent (~0.003) to prevent gain from collapsing entirely
    # at very-low-SNR bins (which would sound like clipping).
    XI_FLOOR: float = 10.0 ** (-25.0 / 10.0)

    # ── Gain-table (pre-computed) constants ──────────────────────
    # Resolution of the 2-D LUT for G(γ, ξ).  200×200 points over
    # the dB ranges below gives us 0.2 dB precision in γ and 0.25
    # dB in ξ — more than enough; bilinear interp covers the rest.
    GAMMA_DB_MIN: float = -10.0
    GAMMA_DB_MAX: float = +30.0
    XI_DB_MIN: float = -25.0
    XI_DB_MAX: float = +30.0
    GAIN_TABLE_SIZE: int = 200

    # ── Init / state ─────────────────────────────────────────────

    def __init__(self, rate: int = 48000) -> None:
        self.rate = int(rate)
        self._fft = self.FFT_SIZE
        self._hop = self.HOP
        self._window = np.hanning(self._fft).astype(np.float32)
        n_bins = self._fft // 2 + 1

        # Operator-facing parameters.
        self.enabled: bool = False
        # aggression: 0.0..1.5; 1.0 = full MMSE-LSA, scales the
        # gain reduction so operators can dial how strongly NR2
        # acts.  See _apply_aggression() for the math.
        self.aggression: float = 1.0
        # Musical-noise smoothing toggle (decision-directed α).
        self.musical_noise_smoothing: bool = True
        # Speech-aware mode (simple VAD that backs off suppression
        # during detected voice).  Off by default; operator opts in.
        self.speech_aware: bool = False

        # AEPF (adaptive equalization post-filter) — direct port
        # from WDSP emnr.c.  Median-smooths the gain mask across
        # frequency with an adaptive kernel whose width tracks how
        # much suppression is happening.  Default ON because the
        # cost is one numpy convolution per FFT block (microseconds)
        # and the artifact reduction is meaningful even at gentle
        # NR settings.  Operator can disable via set_aepf_enabled()
        # to A/B; off = WDSP "ae_run = 0" equivalent.
        self.aepf_enabled: bool = True

        # Speech-Presence-Probability ("witchHat") soft mask —
        # direct port from WDSP emnr.c case-0 gain.  Multiplies the
        # MMSE-LSA gain by a soft mask that's high where speech is
        # likely and low where it's not, sharpening the
        # speech-vs-silence distinction.  Default ON because the
        # cost is trivial (one np.exp per block) and the
        # consonant-preservation benefit is the single biggest
        # perceptual upgrade in WDSP's stack.  Operator can disable
        # via set_spp_enabled() for diagnostic A/B.
        self.spp_enabled: bool = True

        # Streaming framing state.
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_carry = np.zeros(self._hop, dtype=np.float32)
        # Per-bin state for the algorithm.
        # λ_d — noise power estimate.  Seeded to a small value so
        # the first frame doesn't divide by zero.
        self._lambda_d = np.full(n_bins, 1e-8, dtype=np.float64)
        # G[n-1] — last frame's gain, used in the decision-directed
        # ξ update.  Initialized to unity so the first frame
        # behaves like pure spectral subtraction.
        self._prev_gain = np.ones(n_bins, dtype=np.float64)
        # |S[n-1]|² — last frame's clean estimate squared, used in
        # the decision-directed ξ update.
        self._prev_clean_pow = np.zeros(n_bins, dtype=np.float64)

        # Pre-compute BOTH gain lookup tables — MMSE-LSA (E1 / expint
        # based) and Wiener (Bessel I0/I1 based).  Both are
        # ~200×200×8 bytes = 320 KB; trivial cost to keep both in
        # memory so we can switch gain methods at runtime without
        # re-building.
        self._mmse_lsa_table = self._build_mmse_lsa_table()
        self._wiener_table = self._build_wiener_table()
        # Active table — picked by gain_method.
        self.gain_method: str = self.DEFAULT_GAIN_METHOD
        self._gain_table = self._mmse_lsa_table
        # Linear-axis edges for the table (dB → linear).
        self._gamma_min_lin = 10.0 ** (self.GAMMA_DB_MIN / 10.0)
        self._gamma_max_lin = 10.0 ** (self.GAMMA_DB_MAX / 10.0)
        self._xi_min_lin = 10.0 ** (self.XI_DB_MIN / 10.0)
        self._xi_max_lin = 10.0 ** (self.XI_DB_MAX / 10.0)

        # Speech-aware VAD — short-term energy compared to the
        # noise estimate.  Persistent state for hangover.
        self._vad_hangover_frames: int = 0
        # When VAD is active, suppression is reduced by this factor
        # (0..1; 0 = full reduction during speech, 1 = no reduction
        # during speech).
        self._vad_suppression_relax: float = 0.4

        # ── Captured-profile noise reference ────────────────────────
        # When the operator captures a noise profile (📷 Cap), NR1
        # owns the FFT-magnitude collector and stores the result as
        # per-bin |D[k]| magnitudes.  NR2's algorithm uses POWER
        # (λ_d, the noise PSD), so we store the captured magnitudes
        # squared up front, in the same n_bins resolution NR2's STFT
        # uses (FFT_SIZE = 256, hop = 128 → 129 bins, matching NR1's
        # default).  When _use_captured_profile is True AND a
        # profile is loaded, process() uses _captured_lambda_d in
        # place of the live tracker; otherwise falls back to the
        # asymmetric exponential _lambda_d.  The live tracker keeps
        # running in the background regardless, so toggling source
        # off mid-stream gives a warm noise estimate with no glitch.
        self._captured_lambda_d: Optional[np.ndarray] = None
        self._use_captured_profile: bool = False

        # ── Min-statistics noise tracker (Martin 2001) ───────────────
        # ENABLED BY DEFAULT — replaces the asymmetric exponential
        # tracker (NOISE_TRACK_DOWN / NOISE_TRACK_UP) as the source
        # of λ_d.  The legacy asymmetric exponential converges slowly
        # and undertracks in steady-state radio noise, which made
        # NR2 feel "subtle" compared to what MMSE-LSA can actually
        # deliver.  Min-stats with the MINSTATS_PRE_SMOOTH_ALPHA
        # pre-smoother and MINSTATS_BIAS below produces a stable
        # per-bin noise PSD estimate that lets MMSE-LSA reach -5 dB
        # at default aggression.
        #
        # Override behavior:
        #
        #   LYRA_NR_TRACKER=legacy     → opt OUT (both NR1 + NR2)
        #   LYRA_NR2_TRACKER=legacy    → opt OUT for NR2 only
        #   LYRA_NR_TRACKER=minstats   → explicit opt-in (no-op now)
        #   unset                      → DEFAULT (min-stats enabled)
        #
        # When active, ``process()`` uses the bias-corrected per-bin
        # minimum across a ~1.5 sec window.  Tracker operates in
        # MAGNITUDE domain (where the bias correction is calibrated);
        # we square the result to get the power-domain ``λ_d``.
        # Two min-stats trackers, mutually exclusive:
        #
        # 1. _martin (default, full Martin 2001) — direct port of
        #    WDSP's LambdaD.  The good one.  Uses the U×V sub-
        #    window framework with proper bias correction tables.
        #    Doesn't need pre-smoothing.
        #
        # 2. _minstats (legacy, our simplified ring-buffer min) —
        #    kept for diagnostic A/B and as fallback.  Uses
        #    pre-smoothing to compensate for missing variance
        #    correction.
        #
        # Selection via env var:
        #     LYRA_NR_TRACKER=legacy     → asymmetric exponential (oldest)
        #     LYRA_NR2_TRACKER=simple    → simplified min-stats
        #     LYRA_NR2_TRACKER=martin    → full Martin (default)
        #     LYRA_NR_TRACKER=martin     → same, also affects NR1
        #     unset                      → DEFAULT (martin)
        self._minstats: Optional[_MinStatsTracker] = None
        self._martin: Optional[_MartinMinStatsTracker] = None
        self._smoothed_mag: Optional[np.ndarray] = None
        env_global = os.environ.get("LYRA_NR_TRACKER", "").strip().lower()
        env_nr2 = os.environ.get("LYRA_NR2_TRACKER", "").strip().lower()
        env = env_nr2 or env_global
        if env == "legacy":
            pass  # asymmetric exponential, no min-stats
        elif env == "simple":
            self._enable_minstats()
        else:
            # Default: Martin
            self._enable_martin()

    # ── Public API ───────────────────────────────────────────────

    def reset(self) -> None:
        """Drop streaming state.  Called on freq/mode/rate changes
        (audio discontinuities).  Keeps profile + enabled flag and
        operator preferences (aggression, smoothing, VAD).

        The min-stats tracker (when enabled) gets reset too so a new
        band's noise floor isn't biased by the previous band's
        per-bin minima.
        """
        n_bins = self._fft // 2 + 1
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_carry = np.zeros(self._hop, dtype=np.float32)
        self._lambda_d = np.full(n_bins, 1e-8, dtype=np.float64)
        self._prev_gain = np.ones(n_bins, dtype=np.float64)
        self._prev_clean_pow = np.zeros(n_bins, dtype=np.float64)
        self._vad_hangover_frames = 0
        if self._minstats is not None:
            self._minstats.reset()
        if self._martin is not None:
            self._martin.reset()
        # Drop the pre-smoother so it re-seeds from the next frame.
        self._smoothed_mag = None

    # ── Min-stats noise tracker selection ─────────────────────────

    # Pre-smoothing IIR coefficient for the min-stats path.  Per-frame
    # update: smoothed_mag[k] = α·smoothed_mag[k] + (1-α)·|Y[k]|.
    # α=0.7 → ~3-frame effective window — enough smoothing to
    # collapse per-bin variance from 45,000x (raw periodogram) to
    # ~4x (stable for MMSE-LSA's gain math) without dragging the
    # minimum so far toward the mean that the bias correction
    # over-shoots.  Tuned empirically against MINSTATS_BIAS below
    # via the audit sweep; the two parameters interact and should
    # be re-balanced together if either is changed.
    MINSTATS_PRE_SMOOTH_ALPHA: float = 0.7

    # Bias correction passed to _MinStatsTracker for the magnitude-
    # domain min after pre-smoothing.  Calibrated so that NR2 at
    # default aggression (1.0) delivers ~-5 dB attenuation on
    # noise-only audio — comparable to NR1's strength=1.0 (Heavy).
    # Lower than NR1's 2.5 because:
    #   (a) pre-smoothing already raises the per-bin minimum,
    #       reducing how much extra correction is needed
    #   (b) MMSE-LSA's gain math is sensitive — a higher bias
    #       (= higher noise estimate) drives the gain WAY down,
    #       causing audible over-suppression
    # Squaring: power-domain bias = 0.81, close to Martin's
    # tabulated bias for α=0.7 smoothing.
    #
    # CURRENT TUNING: 0.9 — operator-validated 2026-05-01.  TUNING
    # MAY NEED FURTHER ADJUSTMENT based on field-test feedback.
    # If speech sounds robotic / chopped consonants, drop toward
    # 0.7; if NR2 still feels too subtle, raise toward 1.1.
    MINSTATS_BIAS: float = 0.9

    # ── Adaptive Equalization Post-Filter (AEPF) ──────────────────
    # Direct port from WDSP emnr.c (Pratt 2015, 2025; GPL v2+).
    # Smooths the gain mask across frequency with an adaptive kernel
    # whose width grows when the mask is more aggressively
    # suppressing — kills the per-bin "musical noise / underwater"
    # sparkle artifact that classical MMSE-LSA can produce at low
    # SNR.  Light smoothing when noise reduction is mild (high
    # zetaT, narrow kernel); heavy smoothing when reduction is
    # deep (low zetaT, wide kernel).
    AEPF_PSI: float = 20.0           # max kernel half-width
    AEPF_ZETA_THRESH: float = 0.75   # threshold above which N=1

    # ── Speech-Presence-Probability soft mask ("witchHat") ────────
    # Direct port from WDSP emnr.c case-0 gain (Pratt; GPL v2+).
    # Multiplies the MMSE-LSA gain by a soft mask that's high in
    # bins where speech is likely present and low where it's
    # likely absent.  The result is sharper noise suppression in
    # silence and better preservation of speech consonants.  q is
    # the a-priori probability of speech absence (Cohen 2002
    # convention: q ≈ 0.2 means we expect 80% of bins-frames to
    # contain some speech).
    SPP_Q: float = 0.2

    # ── Gain-method options ────────────────────────────────────────
    # Lyra exposes two operator-selectable gain functions, both
    # ported from WDSP emnr.c with attribution.  The MMSE-LSA gain
    # is our pre-existing default; Wiener is the WDSP case-0 base.
    # Both produce ≤ 1.0 gains; both work with SPP and AEPF.
    #
    # Character difference (operator-perceived):
    #   - MMSE-LSA  : sharper attack on noise; classical sound
    #   - Wiener    : smoother per-bin transitions; "fuller" residue
    #
    # WDSP's case-2 (dual-LUT MMSE/MMSE-SPP) and case-3 (Wiener +
    # zetaHat hard-mask post-correction) require shipping
    # pre-computed binary tables (calculus, zetaHat) and are
    # deferred to a future port.
    GAIN_METHOD_MMSE_LSA = "mmse_lsa"
    GAIN_METHOD_WIENER = "wiener"
    GAIN_METHODS = (GAIN_METHOD_MMSE_LSA, GAIN_METHOD_WIENER)
    DEFAULT_GAIN_METHOD = GAIN_METHOD_MMSE_LSA

    def _enable_minstats(self) -> None:
        """Construct the simplified min-stats tracker.  Mutually
        exclusive with the full-Martin tracker."""
        n_bins = self._fft // 2 + 1
        win_sec = 1.5
        n_frames = max(8, int(round(win_sec * self.rate / self._hop)))
        self._minstats = _MinStatsTracker(n_bins, n_frames,
                                           bias=self.MINSTATS_BIAS)
        self._martin = None
        self._smoothed_mag = None

    def _enable_martin(self) -> None:
        """Construct the full Martin (2001) tracker.  Mutually
        exclusive with the simplified ring-buffer min-stats."""
        n_bins = self._fft // 2 + 1
        self._martin = _MartinMinStatsTracker(
            msize=n_bins, hop=self._hop, rate=self.rate)
        self._minstats = None
        self._smoothed_mag = None

    def set_minstats_tracker(self, on: bool) -> None:
        """Enable / disable the simplified min-stats tracker.  Sets
        the simplified tracker active when ``on=True`` (replacing
        Martin if currently active), else falls back to the
        asymmetric-exponential legacy tracker."""
        if bool(on):
            if self._minstats is None:
                self._enable_minstats()
        else:
            self._minstats = None

    def set_martin_tracker(self, on: bool) -> None:
        """Enable / disable the full Martin (2001) min-stats
        tracker.  Sets Martin active when ``on=True`` (replacing
        the simplified tracker if currently active), else falls
        back to the asymmetric-exponential legacy tracker."""
        if bool(on):
            if self._martin is None:
                self._enable_martin()
        else:
            self._martin = None

    def is_minstats_enabled(self) -> bool:
        """True if either min-stats tracker (simple or Martin) is
        the active live noise estimator."""
        return self._minstats is not None or self._martin is not None

    def is_martin_enabled(self) -> bool:
        """True if the full Martin (2001) tracker is active."""
        return self._martin is not None

    def set_aggression(self, value: float) -> None:
        """Operator-tunable suppression strength.  Range 0.0..2.0:
        - 0.0 = unity gain (effectively NR off)
        - 1.0 = full MMSE-LSA / Wiener gain (default-aggression)
        - >1.0 = power-law cleanup (gain^aggression) — pushes
          residual noise floor lower at cost of some thinning
        - 2.0 = ceiling — operator-validated upper bound; above
          this, even WDSP-port machinery (SPP + AEPF) can't
          rescue speech intelligibility

        Operators with the WDSP-port stack (Martin + SPP + AEPF +
        Wiener gain) can usefully push past 1.5 because SPP guards
        speech bins from over-attenuation while AEPF smooths the
        resulting deep-suppression mask.  Pre-WDSP-port codebases
        would distort speech aggressively above 1.5; the new
        machinery makes the higher range listenable.
        """
        self.aggression = float(max(0.0, min(2.0, value)))

    def set_musical_noise_smoothing(self, on: bool) -> None:
        """Toggle the decision-directed ξ smoothing that kills
        the musical-noise artifact.  True = full MMSE-LSA;
        False = closer to NR1 behavior (for A/B diagnostic)."""
        self.musical_noise_smoothing = bool(on)

    def set_speech_aware(self, on: bool) -> None:
        """Toggle the simple-VAD mode that reduces suppression
        during detected voice (preserves consonants).  Off by
        default — operator opts in if they want it."""
        self.speech_aware = bool(on)

    def set_aepf_enabled(self, on: bool) -> None:
        """Toggle the adaptive-equalization post-filter (AEPF).

        AEPF smooths the per-bin gain mask across frequency with
        a kernel that widens as the mask becomes more aggressively
        suppressing.  Direct port from WDSP emnr.c; the single
        biggest perceptual win against the "musical noise"
        artifact that classical MMSE-LSA produces at low SNR.

        Default ON; disable for diagnostic A/B against the bare
        gain calculation."""
        self.aepf_enabled = bool(on)

    def set_gain_method(self, method: str) -> None:
        """Switch the per-bin gain function.

        Valid values:
          - ``"mmse_lsa"`` — Ephraim-Malah Minimum Mean-Squared Error
            Log-Spectral Amplitude estimator (default).  Sharper
            attack on noise; classical sound.
          - ``"wiener"``   — Ephraim-Malah Wiener-MMSE magnitude
            estimator.  Smoother per-bin transitions; "fuller"
            residue.  Direct port from WDSP emnr.c case-0.

        Both gain functions work identically with SPP and AEPF —
        the method choice only affects the pre-SPP gain mask.
        Switching is sample-instant (we keep both LUTs in memory).
        Invalid method names default to MMSE-LSA.
        """
        m = (method or "").strip().lower()
        if m not in self.GAIN_METHODS:
            m = self.DEFAULT_GAIN_METHOD
        self.gain_method = m
        if m == self.GAIN_METHOD_WIENER:
            self._gain_table = self._wiener_table
        else:
            self._gain_table = self._mmse_lsa_table

    def set_spp_enabled(self, on: bool) -> None:
        """Toggle the Speech-Presence-Probability soft mask.

        SPP multiplies the MMSE-LSA gain by a sigmoid that's near
        1 in bins where the a-posteriori SNR suggests speech and
        near 0 where it suggests pure noise.  Direct port from
        WDSP emnr.c case-0 gain.  Sharpens speech-vs-silence
        distinction and preserves consonants better at deep NR
        settings.

        Default ON; disable for diagnostic A/B."""
        self.spp_enabled = bool(on)

    # ── Captured-profile API (mirror of NR1's surface) ──────────────
    # Channel calls these in lockstep with NR1 so the captured-source
    # toggle works identically regardless of which NR is the active
    # processor.

    def load_captured_profile(self, mag: np.ndarray) -> None:
        """Install a captured-noise magnitudes array as NR2's noise
        reference.  Stored as POWER (mag²) since MMSE-LSA operates
        on PSD, not magnitude.

        Raises ValueError on size mismatch — the captured profile
        must match NR2's bin count (FFT_SIZE//2 + 1).
        """
        n_bins = self._fft // 2 + 1
        arr = np.asarray(mag, dtype=np.float64).ravel()
        if arr.size != n_bins:
            raise ValueError(
                f"NR2 captured-profile size {arr.size} doesn't match "
                f"NR2 FFT bin count {n_bins}; resample or recapture")
        # Floor at 1e-12 (same protection NR1 uses) and square to
        # convert magnitude → power.
        self._captured_lambda_d = np.maximum(arr, 1e-6) ** 2

    def clear_captured_profile(self) -> None:
        """Drop the loaded captured-profile noise reference.  After
        this call, process() falls back to the live noise tracker
        even if _use_captured_profile is still True."""
        self._captured_lambda_d = None

    def set_use_captured_profile(self, on: bool) -> None:
        """Toggle whether process() uses the captured-profile noise
        PSD as its λ_d reference.  When False (or no profile loaded),
        the live asymmetric tracker drives the gain math — same as
        NR2 has always done."""
        self._use_captured_profile = bool(on)

    def has_captured_profile(self) -> bool:
        """True iff a captured-profile reference is loaded."""
        return self._captured_lambda_d is not None

    def is_using_captured_source(self) -> bool:
        """True iff the source toggle is on AND a profile is loaded
        (i.e. captured magnitudes are actively driving the gain
        math)."""
        return (self._use_captured_profile
                and self._captured_lambda_d is not None)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one audio block.  Returns same length, float32.

        Bypass-fast when disabled.  Same STFT framing as NR1 so
        the operator can switch between them mid-stream without
        hearing a boundary.
        """
        if not self.enabled or audio.size == 0:
            return audio
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)

        x = audio.astype(np.float32, copy=False)
        self._in_buf = np.concatenate([self._in_buf, x])

        out_chunks: list[np.ndarray] = []
        while self._in_buf.size >= self._fft:
            frame = self._in_buf[:self._fft] * self._window
            spec = np.fft.rfft(frame)
            mag_sq = (spec.real * spec.real
                      + spec.imag * spec.imag).astype(np.float64)

            # Noise-PSD update — two implementations selectable at
            # runtime.  Always runs even when captured-source is
            # active, so the live estimate stays warm as a fallback
            # if the operator clears or toggles off the captured
            # profile.  Same pattern NR1 uses.
            #
            # 1. Min-stats (Martin 2001): sliding-window per-bin
            #    minimum of |Y|, then squared to get noise power.
            #    The bias correction in _MinStatsTracker is
            #    calibrated for the magnitude domain — squaring the
            #    bias-corrected minimum gives the correct power
            #    estimate (since (k·m_min)² = k²·m_min² and the
            #    cross-domain bias factor follows naturally).
            # 2. Asymmetric exponential (default): legacy NR2 tracker.
            #    Fast track-down, slow track-up.
            if self._martin is not None:
                # Full Martin (2001) — feeds power directly, no
                # pre-smoothing needed (the algorithm has its own
                # smoothing built into the alphaHat update).
                self._lambda_d = self._martin.update(mag_sq)
            elif self._minstats is not None:
                mag = np.sqrt(mag_sq).astype(np.float32)
                # Pre-smooth across frames before taking minimum.
                # See MINSTATS_PRE_SMOOTH_ALPHA docstring; without
                # this, per-bin minimum has huge variance and
                # destabilizes MMSE-LSA gain math.
                if self._smoothed_mag is None:
                    self._smoothed_mag = mag.copy()
                else:
                    a = self.MINSTATS_PRE_SMOOTH_ALPHA
                    self._smoothed_mag = (
                        a * self._smoothed_mag + (1.0 - a) * mag)
                noise_mag = self._minstats.update(self._smoothed_mag)
                self._lambda_d = (noise_mag.astype(np.float64)) ** 2
            else:
                below = mag_sq < self._lambda_d
                self._lambda_d = np.where(
                    below,
                    (self.NOISE_TRACK_DOWN * self._lambda_d
                     + (1.0 - self.NOISE_TRACK_DOWN) * mag_sq),
                    (self.NOISE_TRACK_UP * self._lambda_d
                     + (1.0 - self.NOISE_TRACK_UP) * mag_sq),
                )
            # Floor — prevent the estimate from going to literal
            # zero, which would explode γ.  Belt-and-suspenders for
            # both trackers; min-stats's INIT_FLOOR already keeps it
            # well above this in practice.
            np.maximum(self._lambda_d, 1e-12, out=self._lambda_d)

            # Pick the noise-PSD reference the gain math will use.
            # Captured wins when the toggle is on AND a profile is
            # loaded; otherwise the live tracker drives the math.
            if (self._use_captured_profile
                    and self._captured_lambda_d is not None):
                lambda_ref = self._captured_lambda_d
            else:
                lambda_ref = self._lambda_d

            # γ — a-posteriori SNR (current frame).
            gamma = mag_sq / lambda_ref

            # ξ — a-priori SNR via decision-directed smoothing.
            alpha = (self.XI_SMOOTHING_ON
                     if self.musical_noise_smoothing
                     else self.XI_SMOOTHING_OFF)
            # |G[n-1] · Y[n-1]|² / λ_d[n] — using stored prev clean
            # estimate squared (which IS |G[n-1]·Y[n-1]|² already).
            ml_estimate = self._prev_clean_pow / lambda_ref
            # Maximum-likelihood term: max(γ - 1, 0).
            ml_term = np.maximum(gamma - 1.0, 0.0)
            xi = alpha * ml_estimate + (1.0 - alpha) * ml_term
            # Floor on ξ to prevent gain collapse at very-low-SNR
            # bins.
            np.maximum(xi, self.XI_FLOOR, out=xi)

            # MMSE-LSA gain via 2-D LUT lookup.
            gain = self._lookup_gain(gamma, xi)

            # Speech-Presence-Probability soft mask ("witchHat").
            # Multiplies gain by a sigmoid that's high in bins
            # where speech is likely present, low otherwise.
            # Direct port of WDSP emnr.c case-0 gain.  Runs BEFORE
            # AEPF so the smoother sees a sharper input mask.
            if self.spp_enabled:
                # v = (ξ_hat / (1 + ξ_hat)) · γ  — same v WDSP uses.
                v = (xi / (1.0 + xi)) * gamma
                gain = self._apply_spp(gain, v, mag_sq, lambda_ref)

            # AEPF — adaptive frequency-domain smoothing of the
            # gain mask.  Runs BEFORE aggression scaling so the
            # operator's slider position controls how much of the
            # smoothed mask is applied (smoothing then blend toward
            # unity).  Direct port of WDSP's ``aepf`` post-filter;
            # most effective at deep attenuation where the gain
            # mask becomes spiky and produces musical noise.
            if self.aepf_enabled:
                gain = self._apply_aepf(gain, mag_sq)

            # Speech-aware VAD adjustment — reduce suppression
            # during detected voice.
            if self.speech_aware:
                gain = self._apply_vad_relax(gain, mag_sq)

            # Aggression scaling — blend unity gain ↔ full MMSE-LSA.
            gain = self._apply_aggression(gain)

            # Update prev_clean_pow for the next frame's
            # decision-directed update.  This is |G·Y|² evaluated
            # NOW so it'll be ready for n+1.
            self._prev_clean_pow = (gain * gain) * mag_sq
            self._prev_gain = gain

            # Apply gain in the spectral domain.
            cleaned_spec = spec * gain.astype(np.float32)
            time_frame = np.fft.irfft(
                cleaned_spec, self._fft).astype(np.float32)

            # Overlap-add (same COLA reconstruction as NR1).
            head = self._out_carry + time_frame[:self._hop]
            out_chunks.append(head)
            self._out_carry = time_frame[self._hop:].copy()

            self._in_buf = self._in_buf[self._hop:]

        if not out_chunks:
            return np.zeros_like(audio)
        output = np.concatenate(out_chunks)
        if output.size < audio.size:
            output = np.concatenate(
                [output, np.zeros(audio.size - output.size,
                                   dtype=np.float32)])
        elif output.size > audio.size:
            output = output[:audio.size]
        return output

    # ── Internals ────────────────────────────────────────────────

    def _build_mmse_lsa_table(self) -> np.ndarray:
        """Pre-compute the MMSE-LSA gain G(γ, ξ) as a 2-D LUT.

        Called once at init.  Runtime cost is then a vectorized
        bilinear interpolation per FFT block — orders of magnitude
        cheaper than evaluating exp1 per-bin per-frame.

        The Ephraim-Malah MMSE-LSA estimator (1985):
            G(γ,ξ) = (ξ/(1+ξ)) · exp(½·E₁(v))
            where v = (ξ/(1+ξ))·γ
            and E₁(x) = ∫_x^∞ (e⁻ᵗ/t) dt
        """
        n = self.GAIN_TABLE_SIZE
        gamma_db = np.linspace(self.GAMMA_DB_MIN,
                                self.GAMMA_DB_MAX, n)
        xi_db = np.linspace(self.XI_DB_MIN, self.XI_DB_MAX, n)
        gamma_lin = 10.0 ** (gamma_db / 10.0)
        xi_lin = 10.0 ** (xi_db / 10.0)
        G_mesh, X_mesh = np.meshgrid(gamma_lin, xi_lin, indexing="ij")
        v = X_mesh * G_mesh / (1.0 + X_mesh)
        v_clipped = np.maximum(v, 1e-10)
        e1 = _exponential_integral_e1(v_clipped)
        gain = (X_mesh / (1.0 + X_mesh)) * np.exp(0.5 * e1)
        return np.clip(gain, 0.0, 1.0).astype(np.float64)

    def _build_wiener_table(self) -> np.ndarray:
        """Pre-compute the Wiener-MMSE gain G(γ, ξ) as a 2-D LUT.

        Direct port from WDSP emnr.c case-0 gain (Pratt; GPL v2+).
        The Ephraim-Malah Wiener-magnitude estimator (1984):

            G(γ,ξ) = (√π/2)·(√v/γ)·exp(-v/2)
                     · [(1+v)·I₀(v/2) + v·I₁(v/2)]

            where v = (ξ/(1+ξ))·γ
            and I₀, I₁ are modified Bessel functions of the first
            kind, orders 0 and 1.

        Compared to MMSE-LSA: produces slightly smoother per-bin
        transitions; "fuller" residual character; more aggressive
        attack on isolated outlier bins.  Operator-selectable via
        ``set_gain_method``.
        """
        n = self.GAIN_TABLE_SIZE
        gamma_db = np.linspace(self.GAMMA_DB_MIN,
                                self.GAMMA_DB_MAX, n)
        xi_db = np.linspace(self.XI_DB_MIN, self.XI_DB_MAX, n)
        gamma_lin = 10.0 ** (gamma_db / 10.0)
        xi_lin = 10.0 ** (xi_db / 10.0)
        G_mesh, X_mesh = np.meshgrid(gamma_lin, xi_lin, indexing="ij")
        v = X_mesh * G_mesh / (1.0 + X_mesh)
        v_safe = np.maximum(v, 1e-10)
        # Bessel argument is v/2 — clip to prevent overflow in the
        # I₀/I₁ approximations at very high SNR (these pre-multiply
        # by exp(-v/2) so the product stays finite even past v=700).
        v_half = np.minimum(v_safe / 2.0, 350.0)
        gf1p5 = np.sqrt(np.pi) / 2.0
        i0 = _bessel_i0(v_half)
        i1 = _bessel_i1(v_half)
        # WDSP form: gain = gf1p5 · sqrt(v)/γ · exp(-v/2)
        #                    · [(1+v)·I₀(v/2) + v·I₁(v/2)]
        # The sqrt(v)/γ term fails when γ → 0; clip to prevent NaN.
        gamma_safe = np.maximum(G_mesh, 1e-10)
        # exp(-v/2) can underflow for very large v — that's fine,
        # gain → 0 in that regime which gets clipped to [0, 1] anyway.
        gain = (gf1p5
                * np.sqrt(v_safe) / gamma_safe
                * np.exp(-0.5 * np.minimum(v_safe, 700.0))
                * ((1.0 + v_safe) * i0 + v_safe * i1))
        return np.clip(gain, 0.0, 1.0).astype(np.float64)

    # Backwards-compat alias for any external callers.
    _build_gain_table = _build_mmse_lsa_table

    def _lookup_gain(self, gamma: np.ndarray,
                     xi: np.ndarray) -> np.ndarray:
        """Vectorized bilinear-interp lookup of G(γ, ξ) from the
        pre-built table.  Both inputs are linear (not dB) and
        same shape; output matches.
        """
        # Convert to dB-axis float indices.
        gamma_db = 10.0 * np.log10(np.maximum(gamma, 1e-12))
        xi_db = 10.0 * np.log10(np.maximum(xi, 1e-12))
        n = self.GAIN_TABLE_SIZE
        # Map dB to fractional table index.
        gi_f = ((gamma_db - self.GAMMA_DB_MIN)
                / (self.GAMMA_DB_MAX - self.GAMMA_DB_MIN)
                * (n - 1))
        xi_f = ((xi_db - self.XI_DB_MIN)
                / (self.XI_DB_MAX - self.XI_DB_MIN)
                * (n - 1))
        # Clamp to table bounds.
        gi_f = np.clip(gi_f, 0.0, n - 1.001)
        xi_f = np.clip(xi_f, 0.0, n - 1.001)
        # Integer floor + fractional remainder for bilinear weights.
        gi0 = gi_f.astype(np.int32)
        xi0 = xi_f.astype(np.int32)
        gf = gi_f - gi0
        xf = xi_f - xi0
        # Bilinear interpolation across the 4 nearest table cells.
        T = self._gain_table
        g00 = T[gi0, xi0]
        g10 = T[gi0 + 1, xi0]
        g01 = T[gi0, xi0 + 1]
        g11 = T[gi0 + 1, xi0 + 1]
        # Weighted blend (NumPy broadcasts over the bin axis).
        return ((1.0 - gf) * (1.0 - xf) * g00
                + gf * (1.0 - xf) * g10
                + (1.0 - gf) * xf * g01
                + gf * xf * g11)

    def _apply_spp(self, gain: np.ndarray, v: np.ndarray,
                   lambda_y: np.ndarray,
                   lambda_d: np.ndarray) -> np.ndarray:
        """Speech-Presence-Probability soft mask.

        Direct port from WDSP emnr.c case-0 gain — Copyright Warren
        Pratt NR0V, GPL v2+.  Lyra-SDR is GPL v3+ since v0.0.6,
        license-compatible.

        Math:
            v       = (ξ_hat / (1 + ξ_hat)) · γ                   (input)
            v2      = min(v, 700)            (clip for exp safety)
            η       = G² · |Y|² / λ_d         (a-post SNR scaled by G²)
            ε       = η / (1 - q)
            wH      = ((1-q)/q) · exp(v2) / (1 + ε)
            G_new   = G · wH / (1 + wH)

        wH/(1+wH) is a soft mask in [0, 1]: high in bins where the
        a-posteriori SNR is high (speech likely present), low in
        bins where it's low (pure noise likely).  q ≈ 0.2 is the
        standard a-priori probability of speech absence per Cohen
        2002.

        Vectorized over all bins.  Input ``gain`` not mutated;
        returns the SPP-corrected mask.
        """
        q = self.SPP_Q
        # Clip v to prevent exp() overflow.  WDSP uses 700; matches
        # numpy's float64 exp() roof of ~exp(709).
        v_safe = np.minimum(v, 700.0)
        eta = gain * gain * lambda_y / lambda_d
        eps = eta / (1.0 - q)
        # (1-q)/q is constant — precompute outside loop normally,
        # but per-frame it's just one division.
        wh = ((1.0 - q) / q) * np.exp(v_safe) / (1.0 + eps)
        # Soft mask wh/(1+wh) ∈ [0, 1].
        return gain * (wh / (1.0 + wh))

    def _apply_aepf(self, gain: np.ndarray,
                    lambda_y: np.ndarray) -> np.ndarray:
        """Adaptive-Equalization Post-Filter.

        Direct port from WDSP emnr.c (function ``aepf``) — Copyright
        Warren Pratt NR0V, GPL v2+.  Lyra-SDR is GPL v3+ since
        v0.0.6, license-compatible.

        Algorithm:
          zeta  = Σ(gain² · |Y|²) / Σ|Y|²
          zetaT = min(zeta, ZETA_THRESH)
          N     = 1 if zetaT==1 else 1 + 2·round(PSI·(1 − zetaT/ZETA_THRESH))
          gain' = moving-average smooth of gain with kernel half-width N//2

        Edge handling matches Pratt: partial windows at the band
        edges (kernel grows from k=0 outward and shrinks symmetrically
        toward k=msize-1) so we don't bias the smoothing toward DC
        or Nyquist.

        Returns the smoothed gain mask; input ``gain`` is not
        mutated.  Vectorized via NumPy convolution where possible
        with explicit edge handling for the half-windows.
        """
        msize = gain.size
        sum_pre = float(np.sum(lambda_y))
        if sum_pre <= 0.0:
            return gain
        sum_post = float(np.sum(gain * gain * lambda_y))
        zeta = sum_post / sum_pre
        zeta_t = min(zeta, self.AEPF_ZETA_THRESH)

        if zeta_t >= self.AEPF_ZETA_THRESH:
            return gain  # N = 1, no smoothing
        # N is odd; widens toward 2*PSI+1 as zeta_t -> 0.
        N = 1 + 2 * int(round(
            self.AEPF_PSI * (1.0 - zeta_t / self.AEPF_ZETA_THRESH)))
        n = N // 2
        if n == 0:
            return gain
        if n >= msize:
            # Pathological case — kernel wider than spectrum.
            return np.full_like(gain, float(np.mean(gain)))

        # Symmetric moving-average with shrinking edge windows.
        # Pratt's interior loop (k ∈ [n, msize-n)) is a true
        # box-window of 2n+1 samples — we use cumulative-sum trick
        # for O(msize) cost instead of O(msize·N).
        out = np.empty_like(gain)
        # Cumulative sum with leading zero so cs[i+1] - cs[j] is
        # the sum of gain[j..i] (inclusive).
        cs = np.concatenate(([0.0], np.cumsum(gain)))

        # Interior region: full 2n+1 window
        # Indices k ∈ [n, msize-n) inclusive on left, exclusive on right
        if msize > 2 * n:
            ks = np.arange(n, msize - n)
            out[n:msize - n] = (cs[ks + n + 1] - cs[ks - n]) / (2 * n + 1)

        # Left edge: k ∈ [0, n) — partial windows of width 2k+1
        # mean(gain[0..2k]) = (cs[2k+1] - cs[0]) / (2k+1)
        for k in range(min(n, msize)):
            denom = 2 * k + 1
            if 2 * k + 1 > msize:
                # Spectrum smaller than window; clamp.
                out[k] = float(np.mean(gain))
            else:
                out[k] = (cs[2 * k + 1] - cs[0]) / denom

        # Right edge: k ∈ [msize-n, msize) — symmetric to left
        # mean(gain[2k-msize+1 .. msize-1]) of length 2(msize-k)-1
        for k in range(max(msize - n, 0), msize):
            lo = 2 * k - msize + 1
            if lo < 0:
                lo = 0
            denom = msize - lo
            out[k] = (cs[msize] - cs[lo]) / denom

        return out

    def _apply_aggression(self, gain: np.ndarray) -> np.ndarray:
        """Scale gain reduction by the operator's aggression knob.

        agg = 0.0  → output unity gain (no NR)
        agg = 1.0  → output full MMSE-LSA gain (default)
        agg > 1.0  → harder cleanup (gain^agg pushes residual
                     noise lower at the cost of some thinning)

        Implementation:  unity_blend = 1.0 + agg·(gain − 1.0) for
        agg ≤ 1.0;  for agg > 1.0 we use gain^agg to push further.
        """
        agg = self.aggression
        if agg == 1.0:
            return gain
        if agg <= 1.0:
            return 1.0 + agg * (gain - 1.0)
        # agg > 1.0 — power-law for harder cleanup.  Floor at the
        # XI_FLOOR-equivalent gain so we never collapse entirely.
        return np.power(np.maximum(gain, 1e-3), agg)

    def _apply_vad_relax(self, gain: np.ndarray,
                         mag_sq: np.ndarray) -> np.ndarray:
        """Speech-aware VAD: when current frame's average power
        is several dB above the noise estimate, assume voice and
        relax suppression to preserve consonants.

        Hangover (~250 ms) prevents flutter at end of utterances.
        """
        # Frame-average SNR in dB.
        frame_pow = float(np.mean(mag_sq))
        noise_pow = float(np.mean(self._lambda_d))
        snr_db = 10.0 * np.log10(
            max(frame_pow / max(noise_pow, 1e-20), 1e-12))
        # Threshold for voice detection — 6 dB above noise floor.
        if snr_db > 6.0:
            self._vad_hangover_frames = 30   # ~250 ms at 48k/128hop
        active = self._vad_hangover_frames > 0
        if active:
            self._vad_hangover_frames -= 1
            # Relax suppression: blend gain ↔ unity by
            # _vad_suppression_relax.
            relax = self._vad_suppression_relax
            return (1.0 - relax) * gain + relax * 1.0
        return gain
