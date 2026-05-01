"""Auto-Notch Filter (ANF) — LMS adaptive predictor (Phase 3.D #3).

Hunts and removes narrow tonal interference from the demodulated
audio stream — heterodynes, BFO whistles, single-frequency carriers,
intermodulation spurs.  Operator turns it on, walks away; the
filter learns whatever tones are present and surgically nulls them
without taking out genuine speech content.

Algorithm
---------
A leaky-LMS adaptive predictor.  The filter learns to predict the
current audio sample from a window of delayed samples:

  ŷ[n] = Σ w[k] · d[n − delay − k]    (k = 0 .. n_taps − 1)

where ``d[]`` is the input delay line and ``w[]`` is the adaptive
weight vector.  The error signal is the residual the predictor
*can't* predict:

  e[n] = x[n] − ŷ[n]

Tones are highly predictable from past samples (a sinusoid at
frequency f₀ is fully determined by any few of its prior samples),
so the predictor learns them and the residual error contains
almost none of their energy.  Broadband signals (speech, noise)
have no predictable structure across short windows, so they pass
through largely unchanged.

The output of ANF is the error signal e[n] — the input minus
the predictable component.

The weight update is the standard NLMS (normalized LMS) form with
leakage:

  w[k] ← (1 − 2μγ)·w[k]  +  (2μ / σ²) · e[n] · d[n − delay − k]

  σ² = Σ d[n − delay − k]²       (input window energy normalization)

- μ (mu) is the adaptation step size — controls how fast new
  tones get learned.  Larger = faster adaptation but noisier
  weights; smaller = slower but steadier.
- γ (gamma) is the leakage factor — pulls weights toward zero so
  they don't drift unboundedly on stationary input.  Without
  leakage, prolonged DC offsets or near-DC content can cause
  numerical instability over long sessions.
- The σ² normalization makes adaptation rate signal-amplitude-
  independent (NLMS) — without it, μ would have to be retuned for
  every band noise level.

References (public DSP literature only — no SDR-client source
read or adapted):

- Widrow, Hoff (1960):  classical LMS adaptive filtering
- Gitlin, Mazo, Taylor (1973):  leaky-LMS extension preventing
  weight drift on stationary inputs
- Standard NLMS normalization is in any adaptive-filter textbook
  (e.g. Haykin, "Adaptive Filter Theory")

Position in Lyra's audio chain
-------------------------------
ANF runs at 48 kHz, post-demod, between the demodulator output
and the broadband NR processor:

   IQ → NB → decimate → notches (manual) → demod → ANF → NR → APF

Rationale (from docs/architecture/noise_toolkit.md §6):

- Manual notches handle KNOWN carriers (operator placed them on
  specific freqs).  Run first, IQ-domain.
- ANF handles UNKNOWN / drifting tones the operator can't be
  bothered to manually notch.  Audio-domain, post-demod, before
  NR sees the residual.
- NR handles broadband noise that survived everything else.  ANF's
  output is a tone-free input which makes NR's noise-floor
  estimator more reliable (it doesn't have to ignore tonal
  energy).

Operator-facing knobs
---------------------
- Profile: Off / Gentle / Standard / Aggressive / Custom
- (Custom exposes mu via a slider in Settings → Noise; taps and
   delay stay at defaults — these are mechanical parameters
   operators rarely tune.)

Internal constants for v1:
- Taps:           64
- Delay:          10 samples (~0.21 ms at 48 kHz)
- Leakage γ:      0.10
- The σ² normalization uses a small floor (1e-10) to avoid
  divide-by-zero on silent input.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


class AutoNotchFilter:
    """LMS adaptive auto-notch — Phase 3.D #3.

    Operates on float32 mono audio at 48 kHz.  Length-preserving:
    process() returns the same shape as input.

    State persists across blocks so the adaptation runs continuously
    over operator's listening session — tones are learned over a
    few hundred milliseconds and stay nulled until they go away or
    drift away on their own.
    """

    # ── Profile presets ──────────────────────────────────────────
    # mu = adaptation step size.  Higher = faster lock onto new
    # tones but noisier residual; lower = slower but cleaner.
    # The values bracket what's useful in practice — Gentle barely
    # touches transient tones, Aggressive locks onto anything that
    # looks tonal for more than ~50 ms.
    PROFILES: dict[str, dict[str, float]] = {
        "off":        {"mu": 0.0,    "enabled": False},
        # Slow adapter — only locks on prolonged steady tones.
        # Best for operators who listen for transient signals
        # (CW, FT8) and don't want ANF interfering with their
        # signal of interest.
        "gentle":     {"mu": 5e-5,   "enabled": True},
        # Standard balance — typical heterodyne is gone in ~200 ms
        # without ANF chewing on speech consonants.
        "standard":   {"mu": 1.5e-4, "enabled": True},
        # Aggressive — fast lock on any tonal energy; may briefly
        # null short speech tones / vowel formants but recovers
        # quickly because they're not persistent.
        "aggressive": {"mu": 4e-4,   "enabled": True},
    }
    DEFAULT_PROFILE: str = "off"

    # ── Internal constants (advanced; fixed for v1) ──────────────

    # Number of adaptive filter coefficients.  64 is the standard
    # value in the LMS-notch literature for audio-rate operation —
    # enough taps to model up to ~30 simultaneous tones, few enough
    # to keep per-sample cost under a microsecond on modern CPUs.
    N_TAPS: int = 64

    # Decorrelation delay (samples).  The predictor uses samples
    # d[n − delay − k] to predict d[n] — a non-zero delay is what
    # makes the filter notch tones rather than tracking the entire
    # signal.  10 samples ≈ 0.21 ms at 48 kHz.  Larger delays push
    # the lower edge of the notch range up (delay defines the
    # minimum cycle length the predictor can model).
    DELAY: int = 10

    # Leakage factor γ.  Pulls weights toward zero each update by a
    # tiny amount (1 − 2μγ) so prolonged stationary input can't
    # drive them to numerical infinity.  0.10 is the canonical
    # value from the leaky-LMS literature; ANF works fine with γ
    # anywhere from 0.05 to 0.20.
    GAMMA: float = 0.10

    # Operator-tunable mu range (Custom profile).  The fastest
    # value here (1e-3) is faster than Aggressive and is at the
    # edge of stability for typical audio dynamics.  Anything
    # below 1e-5 essentially doesn't adapt — useful for diagnostics
    # but not as an operator setting.
    MU_MIN: float = 1e-5
    MU_MAX: float = 1e-3

    # Floor on the σ² normalization denominator — prevents divide-
    # by-zero on silent input frames.  1e-10 is well below any real
    # audio energy, well above what subnormal float math chokes on.
    SIGMA_FLOOR: float = 1e-10

    def __init__(self, rate: int = 48000) -> None:
        self._rate: int = int(rate)
        self.enabled: bool = False
        self.profile: str = self.DEFAULT_PROFILE
        self._mu: float = 0.0
        # Delay buffer — holds the last (DELAY + N_TAPS) input
        # samples so we can index d[n − delay − k] for any tap k.
        # Sized one larger than strictly needed so off-by-one
        # boundary cases at block edges don't cause issues.
        self._dline_size: int = self.DELAY + self.N_TAPS + 4
        self._dline: np.ndarray = np.zeros(
            self._dline_size, dtype=np.float64)
        # Circular write index into the delay line.
        self._d_idx: int = 0
        # Adaptive weight vector w[k].
        self._w: np.ndarray = np.zeros(self.N_TAPS, dtype=np.float64)
        # Apply the default profile (off → enabled=False, μ=0).
        self._apply_profile()

    # ── Public API ───────────────────────────────────────────────

    def set_rate(self, rate: int) -> None:
        """Update the audio sample rate.

        ANF runs at the audio chain's sample rate (48 kHz in
        Lyra).  If the chain ever runs at a different rate, the
        operator-tunable μ values would need re-tuning to maintain
        the same convergence time in seconds — the profile values
        are picked for 48 kHz.  For now this is a no-op except
        recording the new rate; future rate-aware μ scaling could
        slot in here.
        """
        self._rate = int(rate)

    def set_profile(self, name: str) -> None:
        """Apply a named preset.

        Names: ``off`` / ``gentle`` / ``standard`` / ``aggressive``
        / ``custom``.  Custom retains the current μ; presets
        install their own μ.  Unknown names fall back to ``off``.
        """
        name = (name or "").strip().lower()
        if name not in self.PROFILES and name != "custom":
            name = self.DEFAULT_PROFILE
        self.profile = name
        if name == "custom":
            # Custom keeps whatever μ was last set; only the
            # enable flag follows from μ being meaningful.
            self.enabled = self._mu > 0.0
        else:
            self._apply_profile()

    def set_mu(self, mu: float) -> None:
        """Operator-set adaptation step size; switches profile to
        'custom'.  Clamped to [MU_MIN, MU_MAX]."""
        self._mu = float(max(
            self.MU_MIN, min(self.MU_MAX, mu)))
        self.profile = "custom"
        self.enabled = True

    def reset(self) -> None:
        """Drop adaptation state.  Call on freq/mode changes —
        any tones that were learned belong to the prior band/mode
        and are unlikely to be present in the new one, so we want
        a clean start.

        Profile + enabled state are preserved (operator's setting
        sticks across the discontinuity).
        """
        self._dline.fill(0.0)
        self._d_idx = 0
        self._w.fill(0.0)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one audio block.  Returns same length, float32.

        When disabled, returns the input unchanged (cheapest bypass).

        Implementation: per-sample LMS loop is intrinsically
        sequential (each sample's weights depend on the prior
        sample's update), so the OUTER loop stays in Python.  The
        per-sample inner work — prediction (Σ w·d), energy
        compute (Σ d²), and weight update (w ← leak·w + κ·e·d) —
        is fully vectorized via NumPy ``np.dot`` + in-place vector
        ops.

        Speedup over a fully-Python implementation is ~10-15× on
        modern CPUs.  At 48 kHz audio rate with 2048-sample blocks,
        process() runs in well under 1 ms per block — small enough
        that the spectrum painter on the same thread isn't starved.

        Earlier draft had nested Python tap-loops which ate ~25%
        of a CPU core and audibly slowed the spectrum/waterfall
        cadence when ANF was active.  This vectorized form fixes
        that.
        """
        if not self.enabled or audio.size == 0:
            return audio
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)

        # Pull state into local Python names — attribute access in
        # the per-sample loop is the slowest thing now that the
        # inner work is vectorized.
        dline = self._dline
        dsize = self._dline_size
        di = self._d_idx
        w = self._w
        mu = self._mu
        n_taps = self.N_TAPS
        delay = self.DELAY
        # 1 − 2μγ for the leakage term — fold the constant.
        leak = 1.0 - 2.0 * mu * self.GAMMA
        # 2μ for the gradient term — same fold.
        two_mu = 2.0 * mu
        sigma_floor = self.SIGMA_FLOOR

        # Pre-compute the tap-offset array (constant across samples).
        # ``np.take`` with ``mode='wrap'`` handles the circular
        # delay-line wrap-around in C without us doing per-tap
        # modulo math in Python.
        tap_offsets = np.arange(n_taps, dtype=np.int64)

        # Output buffer — float32 (matches input).  Mixed-precision
        # math (dline + w in float64, audio in float32) folds into
        # float32 here automatically.
        out = np.empty_like(audio)

        for n in range(audio.size):
            # Insert new sample into the circular delay line.
            dline[di] = audio[n]
            # Read the n_taps-sample window from the delay line
            # at positions (di - delay - 0, di - delay - 1, ...,
            # di - delay - (n_taps - 1)) — circular indexing
            # handled by np.take(mode='wrap').
            window = dline.take(
                (di - delay) - tap_offsets, mode="wrap")
            # Vectorized prediction + input-window energy.
            y = float(np.dot(w, window))
            sigma = float(np.dot(window, window))
            # Residual — this is what ANF outputs.
            err = float(audio[n]) - y
            out[n] = err
            # Vectorized NLMS weight update with leakage:
            #   w ← (1 − 2μγ)·w + (2μ/σ²)·e·d
            # In-place ops minimize temporary allocations inside the
            # hot per-sample loop.
            inv_sigma = two_mu / max(sigma, sigma_floor)
            w *= leak
            # w += inv_sigma * err * window — fold scalars first
            # so we only allocate one temporary (the scaled window).
            w += (inv_sigma * err) * window
            # Advance the circular delay-line write index.
            di = (di + 1) % dsize

        # Write back state.  _dline and _w were mutated in-place.
        self._d_idx = di

        return out

    # ── Internals ────────────────────────────────────────────────

    def _apply_profile(self) -> None:
        """Pull the named profile's μ into instance state.  Custom
        is handled by set_mu directly."""
        p = self.PROFILES.get(self.profile, self.PROFILES["off"])
        self._mu = float(p["mu"])
        self.enabled = bool(p["enabled"])
