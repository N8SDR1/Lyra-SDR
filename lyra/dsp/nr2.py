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

from typing import Callable, Optional

import numpy as np

try:
    from scipy.special import exp1 as _scipy_exp1
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


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

        # Pre-compute the MMSE-LSA gain lookup table.
        self._gain_table = self._build_gain_table()
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

    # ── Public API ───────────────────────────────────────────────

    def reset(self) -> None:
        """Drop streaming state.  Called on freq/mode/rate changes
        (audio discontinuities).  Keeps profile + enabled flag and
        operator preferences (aggression, smoothing, VAD)."""
        n_bins = self._fft // 2 + 1
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_carry = np.zeros(self._hop, dtype=np.float32)
        self._lambda_d = np.full(n_bins, 1e-8, dtype=np.float64)
        self._prev_gain = np.ones(n_bins, dtype=np.float64)
        self._prev_clean_pow = np.zeros(n_bins, dtype=np.float64)
        self._vad_hangover_frames = 0

    def set_aggression(self, value: float) -> None:
        """Operator-tunable suppression strength.  0.0 = unity
        (effectively NR off), 1.0 = full MMSE-LSA, up to 1.5 for
        more aggressive cleanup at the cost of some artifacts."""
        self.aggression = float(max(0.0, min(1.5, value)))

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

            # Noise-tracker update (per-bin asymmetric smoothing).
            below = mag_sq < self._lambda_d
            self._lambda_d = np.where(
                below,
                (self.NOISE_TRACK_DOWN * self._lambda_d
                 + (1.0 - self.NOISE_TRACK_DOWN) * mag_sq),
                (self.NOISE_TRACK_UP * self._lambda_d
                 + (1.0 - self.NOISE_TRACK_UP) * mag_sq),
            )
            # Floor — prevent the estimate from going to literal
            # zero, which would explode γ.
            np.maximum(self._lambda_d, 1e-12, out=self._lambda_d)

            # γ — a-posteriori SNR (current frame).
            gamma = mag_sq / self._lambda_d

            # ξ — a-priori SNR via decision-directed smoothing.
            alpha = (self.XI_SMOOTHING_ON
                     if self.musical_noise_smoothing
                     else self.XI_SMOOTHING_OFF)
            # |G[n-1] · Y[n-1]|² / λ_d[n] — using stored prev clean
            # estimate squared (which IS |G[n-1]·Y[n-1]|² already).
            ml_estimate = self._prev_clean_pow / self._lambda_d
            # Maximum-likelihood term: max(γ - 1, 0).
            ml_term = np.maximum(gamma - 1.0, 0.0)
            xi = alpha * ml_estimate + (1.0 - alpha) * ml_term
            # Floor on ξ to prevent gain collapse at very-low-SNR
            # bins.
            np.maximum(xi, self.XI_FLOOR, out=xi)

            # MMSE-LSA gain via 2-D LUT lookup.
            gain = self._lookup_gain(gamma, xi)

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

    def _build_gain_table(self) -> np.ndarray:
        """Pre-compute the MMSE-LSA gain G(γ, ξ) as a 2-D LUT.

        Called once at init.  Runtime cost is then a vectorized
        bilinear interpolation per FFT block — orders of magnitude
        cheaper than evaluating exp1 per-bin per-frame.
        """
        n = self.GAIN_TABLE_SIZE
        gamma_db = np.linspace(self.GAMMA_DB_MIN,
                                self.GAMMA_DB_MAX, n)
        xi_db = np.linspace(self.XI_DB_MIN, self.XI_DB_MAX, n)
        gamma_lin = 10.0 ** (gamma_db / 10.0)
        xi_lin = 10.0 ** (xi_db / 10.0)
        # Mesh — γ varies along axis 0, ξ along axis 1.
        G_mesh, X_mesh = np.meshgrid(gamma_lin, xi_lin, indexing="ij")
        v = X_mesh * G_mesh / (1.0 + X_mesh)
        # Clip v to avoid blow-up in the exponential integral at
        # very small values (E1 → ∞ as v → 0).
        v_clipped = np.maximum(v, 1e-10)
        e1 = _exponential_integral_e1(v_clipped)
        gain = (X_mesh / (1.0 + X_mesh)) * np.exp(0.5 * e1)
        # Final clamp — gain should never exceed 1 (no signal
        # amplification in noise reduction); occasional numerical
        # overshoots from the exp1 approximation get caught here.
        return np.clip(gain, 0.0, 1.0).astype(np.float64)

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
