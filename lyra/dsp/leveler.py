"""Audio leveler — feed-forward soft-knee compressor.

The "TV late-night mode" / "loudness leveler" effect: tames sudden
loud bursts (audio pops, transient amplitude spikes, single-syllable
shouts in speech) while keeping quieter content audible.  Cuts
through ambient room noise without waking the family.

Different from Lyra's existing AGC because:
- AGC operates on the RF/IF envelope at second-scale time constants
  (band-noise vs strong-signal compensation)
- Audio leveler operates on the demodulated audio at hundred-ms
  time constants (within-signal dynamics: a guy talking quietly
  then yelling, a sudden audio pop, etc.)

They run in series, each handling its own time-scale.

Algorithm
---------
Standard feed-forward soft-knee compressor with smoothed envelope
detection and makeup gain.  Implemented clean-room from public
DSP literature on dynamic-range processors:

  - Reiss & McPherson, "Audio Effects: Theory, Implementation
    and Application" (2014), Chapter 6 (compressors / limiters /
    expanders)
  - Zölzer (ed.), "DAFX: Digital Audio Effects" (2nd ed., 2011),
    Chapter 4 (nonlinear processing — dynamic range)

Both are graduate-level audio-DSP textbooks; the soft-knee
compressor algorithm is standard material across the field.  No
code from any other SDR or audio-software source has been read or
adapted.

Per-sample flow:

  1.  level_db[n] = 20·log10(|audio[n]| + ε)        (instantaneous)
  2.  env_db[n]  = attack/release smooth(level_db)  (envelope)
  3.  gain_red_db = soft_knee_curve(env_db, threshold,
                                    ratio, knee_width)
  4.  out[n] = audio[n] · 10^((gain_red_db + makeup_db) / 20)

The envelope detector uses asymmetric attack/release (fast attack
for catching peaks, slower release for natural decay) with the
standard analog-emulation IIR form:

  env = (alpha · env_prev) + ((1 - alpha) · level)

  alpha_attack  = exp(-1 / (rate · attack_sec))
  alpha_release = exp(-1 / (rate · release_sec))

Pick alpha_attack when level > env (attack phase), alpha_release
when level <= env (release phase).

The soft-knee gain-reduction curve is the standard cubic-blend:

           threshold     gain_red = 0
            -knee/2      gain_red = -((env - thr + knee/2)^2 / (2·knee)) · (1 - 1/ratio)
              ...           (smooth quadratic transition)
           threshold +
            knee/2       gain_red = -(env - thr) · (1 - 1/ratio)
              ...           (linear above the knee)

This produces a C¹-continuous gain curve — no audible kink at
threshold like a hard-knee compressor would have.

Operator-facing knobs
---------------------
- Profile: Off / Light / Medium / Late Night / Custom
- (Custom exposes threshold + ratio + makeup via Settings sliders;
   attack/release/knee stay at sensible defaults — these are
   mechanical parameters operators rarely tune.)

Internal constants for v1:
- Attack:        5 ms  (catches transients fast)
- Release:       150 ms  (natural-sounding decay)
- Knee width:    6 dB  (smooth transition, no audible threshold kink)
"""
from __future__ import annotations

from typing import Optional

import numpy as np


class AudioLeveler:
    """Feed-forward soft-knee audio compressor / leveler.

    Operates on float32 mono OR stereo (N,2) audio at 48 kHz.
    Length-preserving: process() returns the same shape as input.

    State (envelope follower) persists across blocks so attack
    and release remain continuous.
    """

    # ── Profile presets ──────────────────────────────────────────
    # threshold = level (dBFS) above which compression engages.
    # ratio     = N:1 (so ratio=4 means above threshold, every
    #             4 dB of input increase produces 1 dB of output
    #             increase).
    # makeup    = post-compression gain (dB) to bring overall
    #             loudness back up after the peaks were tamed.
    PROFILES: dict[str, dict[str, float]] = {
        "off":    {"threshold": 0.0, "ratio": 1.0, "makeup": 0.0,
                   "enabled": False},
        # Light: gentle; rounds off obvious peaks but preserves
        # most dynamics.  Speech sounds natural, transient pops
        # get caught.
        "light":  {"threshold": -18.0, "ratio": 2.5, "makeup": 3.0,
                   "enabled": True},
        # Medium: standard speech compression.  Most operators'
        # everyday setting.  Pops definitely caught, sudden bursts
        # tamed, quieter content lifted.
        "medium": {"threshold": -22.0, "ratio": 4.0, "makeup": 6.0,
                   "enabled": True},
        # Late Night: aggressive leveling.  Big dynamics get
        # squashed; ambient room noise is no longer drowned out
        # by sudden loud passages.  TV-style loudness leveling.
        "latenight": {"threshold": -28.0, "ratio": 8.0,
                      "makeup": 10.0, "enabled": True},
    }
    DEFAULT_PROFILE: str = "off"

    # ── Internal constants (advanced; fixed for v1) ──────────────

    # Attack time — how fast the envelope follower reacts to a
    # rising input.  5 ms is fast enough to catch transient pops
    # and the leading edge of yelled syllables; slow enough to
    # avoid audibly distorting the natural envelope of speech.
    ATTACK_SEC: float = 0.005

    # Release time — how fast the follower decays after the input
    # drops back below threshold.  150 ms is the conventional
    # "natural-sounding" value: long enough to avoid pumping
    # artifacts, short enough to recover before the next peak.
    RELEASE_SEC: float = 0.150

    # Knee width (dB) — soft transition zone around the threshold.
    # 6 dB is the standard textbook value; produces a perceptually-
    # imperceptible threshold transition.  0 = hard knee (audible
    # kink at threshold).  Larger = gentler but more "sponginess".
    KNEE_DB: float = 6.0

    # Floor for log-magnitude computation (prevents log(0)).
    MAG_FLOOR: float = 1e-10

    # Operator-tunable parameter ranges for Custom profile.
    THRESHOLD_MIN_DB: float = -50.0
    THRESHOLD_MAX_DB: float = -3.0
    RATIO_MIN: float = 1.0     # 1:1 = no compression
    RATIO_MAX: float = 20.0    # 20:1 ≈ limiter
    MAKEUP_MIN_DB: float = 0.0
    MAKEUP_MAX_DB: float = 24.0

    def __init__(self, rate: int = 48000) -> None:
        self._rate: int = int(rate)
        self.enabled: bool = False
        self.profile: str = self.DEFAULT_PROFILE
        self._threshold_db: float = 0.0
        self._ratio: float = 1.0
        self._makeup_db: float = 0.0
        # Envelope-follower coefficients — recomputed when rate
        # changes.
        self._alpha_attack: float = 0.0
        self._alpha_release: float = 0.0
        # Envelope state — single float in dB, per-channel
        # (mono = 1 element, stereo = 2 elements).  Stays at the
        # quiet floor so the first samples don't trigger spurious
        # gain reduction.
        self._env_db: np.ndarray = np.full(
            1, -120.0, dtype=np.float64)
        self._n_channels: int = 1
        self._apply_profile()
        self._recompute_alphas()

    # ── Public API ───────────────────────────────────────────────

    def set_rate(self, rate: int) -> None:
        """Update the audio sample rate.  Recomputes envelope-
        follower α coefficients to keep attack/release in seconds
        consistent."""
        new_rate = int(rate)
        if new_rate == self._rate:
            return
        self._rate = new_rate
        self._recompute_alphas()
        self.reset()

    def set_profile(self, name: str) -> None:
        """Apply a named preset.  Names: off / light / medium /
        latenight / custom."""
        name = (name or "").strip().lower()
        if name not in self.PROFILES and name != "custom":
            name = self.DEFAULT_PROFILE
        self.profile = name
        if name == "custom":
            self.enabled = self._ratio > 1.0
        else:
            self._apply_profile()

    def set_threshold_db(self, db: float) -> None:
        """Operator-set threshold (Custom profile)."""
        self._threshold_db = float(max(
            self.THRESHOLD_MIN_DB,
            min(self.THRESHOLD_MAX_DB, db)))
        self.profile = "custom"
        self.enabled = self._ratio > 1.0

    def set_ratio(self, ratio: float) -> None:
        """Operator-set compression ratio (Custom profile)."""
        self._ratio = float(max(
            self.RATIO_MIN,
            min(self.RATIO_MAX, ratio)))
        self.profile = "custom"
        self.enabled = self._ratio > 1.0

    def set_makeup_db(self, db: float) -> None:
        """Operator-set makeup gain (Custom profile)."""
        self._makeup_db = float(max(
            self.MAKEUP_MIN_DB,
            min(self.MAKEUP_MAX_DB, db)))
        # Makeup-only adjustments don't disable; ratio drives that.

    def reset(self) -> None:
        """Reset envelope follower to the quiet floor.  Call on
        stream restart, freq/mode changes — anywhere there's a
        fundamental discontinuity in audio input."""
        self._env_db.fill(-120.0)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one audio block.  Returns same length, float32.

        Mono input → mono output.  Stereo (N, 2) input → stereo
        output, with SHARED gain reduction across channels (linked
        compression) so center-panned content stays centered when
        one channel triggers compression.
        """
        if not self.enabled or audio.size == 0:
            return audio
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)

        # Detect channel count and accommodate envelope-state size.
        if audio.ndim == 1:
            n_ch = 1
        elif audio.ndim == 2 and audio.shape[1] in (1, 2):
            n_ch = audio.shape[1]
        else:
            # Unknown shape — pass through.  Don't crash.
            return audio
        if self._n_channels != n_ch:
            # Channel count changed — reset env to match.
            self._env_db = np.full(n_ch, -120.0, dtype=np.float64)
            self._n_channels = n_ch

        # Compute the per-sample magnitude.  For stereo we use the
        # MAX of the two channels (linked compression — both
        # channels see the same gain reduction so panned content
        # stays balanced).
        if n_ch == 1:
            mag = np.abs(audio.astype(np.float64))
        else:
            mag = np.max(np.abs(audio.astype(np.float64)), axis=1)
        # Convert to dBFS.  Floor prevents log(0).
        level_db = 20.0 * np.log10(mag + self.MAG_FLOOR)

        # Envelope follower — asymmetric attack/release.
        # The sequential dependence (env[n] depends on env[n-1])
        # forces a Python loop; vectorization isn't possible
        # without dropping the asymmetry.
        env_db = np.empty_like(level_db)
        prev = float(self._env_db[0])
        a_atk = self._alpha_attack
        a_rel = self._alpha_release
        for n in range(level_db.size):
            x = level_db[n]
            if x > prev:
                # Attack — fast smooth toward the new peak.
                prev = a_atk * prev + (1.0 - a_atk) * x
            else:
                # Release — slow smooth back toward quieter level.
                prev = a_rel * prev + (1.0 - a_rel) * x
            env_db[n] = prev
        self._env_db[0] = prev   # carry across blocks

        # Soft-knee gain-reduction curve.  Vectorized — env_db is
        # the input, gain_red_db is the output (negative or zero).
        gain_red_db = self._compute_gain_reduction(env_db)

        # Total per-sample gain in dB = gain reduction + makeup.
        total_gain_db = gain_red_db + self._makeup_db
        # Convert back to linear and apply.
        gain_lin = (10.0 ** (total_gain_db / 20.0)
                    ).astype(np.float32)

        if n_ch == 1:
            out = audio * gain_lin
        else:
            # Broadcast the per-sample gain to both channels —
            # linked compression keeps stereo image intact.
            out = audio * gain_lin[:, np.newaxis]

        return out.astype(np.float32, copy=False)

    # ── Internals ────────────────────────────────────────────────

    def _apply_profile(self) -> None:
        p = self.PROFILES.get(self.profile, self.PROFILES["off"])
        self._threshold_db = float(p["threshold"])
        self._ratio = float(p["ratio"])
        self._makeup_db = float(p["makeup"])
        self.enabled = bool(p["enabled"])

    def _recompute_alphas(self) -> None:
        """Recompute attack/release α coefficients from the
        configured time constants and current sample rate."""
        self._alpha_attack = float(np.exp(
            -1.0 / max(1e-9, self.ATTACK_SEC * self._rate)))
        self._alpha_release = float(np.exp(
            -1.0 / max(1e-9, self.RELEASE_SEC * self._rate)))

    def _compute_gain_reduction(
            self, env_db: np.ndarray) -> np.ndarray:
        """Soft-knee gain-reduction curve.

        Three regions:
        - env_db < threshold − knee/2:    no reduction (gain = 0)
        - within ± knee/2 of threshold:   smooth quadratic blend
        - env_db > threshold + knee/2:    linear above-threshold

        Vectorized; output shape matches input.
        """
        thr = self._threshold_db
        ratio = self._ratio
        knee = self.KNEE_DB
        if ratio <= 1.0 or knee <= 0:
            # No compression configured — fast path.
            return np.zeros_like(env_db)
        # Slope above threshold — every ratio dB of input gets 1
        # dB out, so gain reduction = -(env-thr) · (1 - 1/ratio).
        slope = 1.0 - (1.0 / ratio)
        gr = np.zeros_like(env_db)
        # Below the knee — no reduction (already zero).
        # Inside the knee — smooth quadratic (Reiss & McPherson eq).
        knee_lo = thr - knee / 2.0
        knee_hi = thr + knee / 2.0
        in_knee = (env_db > knee_lo) & (env_db <= knee_hi)
        if in_knee.any():
            # gr_in_knee = -(env - knee_lo)^2 / (2·knee) · slope
            x = env_db[in_knee] - knee_lo
            gr[in_knee] = -(x * x) / (2.0 * knee) * slope
        # Above the knee — linear (full slope).
        above = env_db > knee_hi
        if above.any():
            gr[above] = -(env_db[above] - thr) * slope
        return gr
