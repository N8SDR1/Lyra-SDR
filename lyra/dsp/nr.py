"""Classical noise reduction — streaming spectral subtraction.

Single-channel mono float32 audio at 48 kHz. Uses an STFT with 50 %
Hanning-window overlap (COLA-exact reconstruction) and magnitude-
domain subtraction. A VAD-like rule updates the noise floor estimate
only on frames quieter than the current estimate, so speech doesn't
pollute the noise model.

The NR processor exposes TWO INDEPENDENT operator controls:

    profile       — subtraction aggression (alpha / beta tuning)
    noise source  — where the noise estimate comes from

Profiles (alpha / beta tuning):

    Light       — SSB ragchew, subtle hiss reduction, minimal artifacts
    Medium      — standard speech NR (default)
    Heavy       — weak-signal DX, noisy bands; accept more "musical
                  noise" artifacts for deeper noise suppression

Noise source (selectable independently of profile):

    Live (VAD)  — Lyra's adaptive estimator updates the noise model
                  on quiet frames during normal listening
    Captured    — Audacity-style: operator records a noise-only sample
                  via begin_noise_capture(); the captured per-bin
                  magnitude profile becomes the locked noise model.
                  Much more accurate than the live VAD estimate
                  because it's measured on actual noise without any
                  signal contamination.

Operator picks both:

    Light + Captured       → gentle subtraction, locked profile
    Medium + Captured      → standard subtraction, locked profile
    Heavy + Live           → harder subtraction with live tracking
    ... etc.

Profile and source were tangled in an earlier draft (a 4th "captured"
profile entry that bundled both choices); separating them gives the
operator the full 3×2 = 6 combinations of "how aggressive" × "what
noise model".

The "musical noise" artifact of classical subtraction is a known
limitation; the aggressive profile spreads this by using a higher
spectral floor. Captured-source mode generally has cleaner output
than live-source because the noise model is more accurate.  Neural
NR (RNNoise / DeepFilterNet — planned) eliminates the musical-noise
artifact almost entirely.  See docs/backlog.md.

Integration: Radio calls `.process(audio_block)` once per demod
block. The module is length-preserving: input N samples → output N
samples (with ~2.7 ms internal latency, one FFT frame of 256 at 48k).

Captured-profile workflow (Phase 3.D #1):
    1. Operator tunes to noise-only frequency / transmission gap
    2. UI calls nr.begin_noise_capture(seconds=2.0)
    3. Lyra accumulates per-bin magnitudes for `seconds` of audio
       inside process()
    4. When done, the average becomes _captured_noise_mag, state
       flips to "ready", and the registered done-callback fires
       (Radio's signal-emit shim — see set_capture_done_callback)
    5. Operator (or UI auto-) selects profile = "captured"; from
       then on, subtraction uses the locked profile instead of the
       live VAD-tracked estimate.
    6. Operator can re-capture at will, save/load via the JSON
       persistence layer (lyra/dsp/noise_profile_store), or clear
       via clear_captured_profile().

Smart-guard (always on by default): after capture finishes,
inspect frame-to-frame power variance. High variance suggests a
signal was riding through the capture window; smart_guard_verdict()
returns "suspect" so the UI can warn the operator before they save.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

import numpy as np


class _MinStatsTracker:
    """Minimum-statistics noise tracker (Martin 2001).

    Maintains a sliding ring buffer of the most recent ``n_frames``
    magnitude spectra and reports the per-bin minimum across the
    window as the noise-floor estimate.  Rationale: even active
    speech / music has gaps on a per-bin basis — over a 1-2 second
    window, every bin spends some time near the actual noise floor.
    The minimum naturally locks onto that floor while ignoring the
    transient peaks that signal energy adds.

    Two characteristics matter for our use case:

    1.  Self-bootstrapping.  No VAD gate, no chicken-and-egg
        initialization problem.  Even a tracker that's never seen a
        truly quiet moment will return SOME estimate (the lowest mag
        observed so far), which converges toward truth as more frames
        arrive.  This is the fix for the dead-on-arrival live tracker
        bug documented in ``SpectralSubtractionNR.__init__``.

    2.  Bias correction.  The minimum of ``N`` samples drawn from a
        noise distribution is biased low (lower than the mean noise
        level by a factor that depends on ``N`` and the underlying
        statistics).  Multiplying by ``BIAS_CORRECTION`` (≈1.5x for
        our window size) approximates the mean noise level.  The
        exact value Martin derived depends on overlap and window
        details; 1.5 is the empirical rule-of-thumb for ~1.5 sec
        windows with 50% overlap that we use here.

    Memory: ``n_frames × n_bins`` float32.  Default sizing at 48 kHz
    / 128-hop / 256-FFT is ~562 × 129 ≈ 290 KB; at 12 kHz it's ~140 ×
    129 ≈ 72 KB.  Update cost: one ``np.min(buf, axis=0)`` per frame,
    which is ~72k float comparisons at 12 kHz → microseconds.

    Trade-off vs the legacy VAD-gated tracker: a slight added latency
    in adapting to changing noise (the ring takes ``n_frames`` worth
    of audio to fully refresh), but no dead-zone bug and far better
    behavior under continuous signal (where the legacy gate never
    fires at all).  See ``docs/AUDIT-2026-05-01.md`` for the full
    discussion.
    """

    # Bias correction factor (multiplies per-bin minimum to approximate
    # the true noise floor).  Martin's paper derives this as a
    # function of window size and overlap; literature values for
    # ~1-2 sec windows with 50% overlap range from 1.5 to 2.5.  The
    # subtle-vs-aggressive trade-off:
    #   - Lower bias  → noise estimate is too low → undersubtraction
    #     → noise leaks through, mild perceived NR
    #   - Higher bias → noise estimate exceeds the actual floor →
    #     over-subtraction → speech distortion / "watery" artifacts
    # Set as a class attribute so it can be tweaked per-deployment
    # without touching other code.
    #
    # CURRENT TUNING: 2.5 — operator-validated 2026-05-01 against
    # min-stats NR1 strength sweep.  Light/Medium/Heavy now feel
    # like a meaningful Light → Heavy progression.  TUNING MAY NEED
    # FURTHER ADJUSTMENT based on field-test feedback across a
    # variety of bands / noise environments.  Drop toward 1.5 if
    # over-subtraction surfaces; raise toward 3.0 if undersubtraction.
    BIAS_CORRECTION: float = 2.5

    # Floor for the initial buffer fill — small enough that real
    # noise quickly dominates as the buffer warms up, but non-zero
    # so we never produce an exact-zero noise estimate.
    INIT_FLOOR: float = 1e-3

    def __init__(self, n_bins: int, n_frames: int,
                 bias: Optional[float] = None):
        """``bias`` overrides BIAS_CORRECTION for this instance.  Per
        Martin (2001) the appropriate bias depends on whether the
        input is raw periodogram bins or pre-smoothed magnitudes —
        consumers that pre-smooth (e.g. NR2) should pass a smaller
        bias so the resulting minimum doesn't over-shoot.  None
        defaults to the class attribute (raw-input calibration)."""
        self._n_bins = int(n_bins)
        self._n_frames = max(8, int(n_frames))
        self._bias = float(bias) if bias is not None else self.BIAS_CORRECTION
        # Pre-fill with INIT_FLOOR so the per-bin minimum during the
        # first ``n_frames`` of audio (the warmup window) is bounded
        # at INIT_FLOOR.  This gives near-bypass behavior during
        # warmup (small noise estimate → tiny subtraction) instead of
        # whatever artifact a zero-init would produce.  Real audio
        # frames overwrite the buffer one slot at a time as they
        # arrive, so by ``n_frames`` worth of input the tracker has
        # entirely measured-data to work with.
        self._buf = np.full(
            (self._n_frames, self._n_bins),
            self.INIT_FLOOR,
            dtype=np.float32,
        )
        self._write_idx = 0

    def update(self, mag: np.ndarray) -> np.ndarray:
        """Push one frame's magnitude spectrum, return the bias-
        corrected per-bin minimum across the current window."""
        self._buf[self._write_idx] = mag
        self._write_idx = (self._write_idx + 1) % self._n_frames
        return np.min(self._buf, axis=0) * self._bias

    def reset(self) -> None:
        """Reset the ring buffer to the initial floor.  Called from
        :meth:`SpectralSubtractionNR.reset` so that band/mode/stream
        transitions don't leak stale noise estimates into a fresh
        audio context (different band == different noise floor).
        """
        self._buf.fill(self.INIT_FLOOR)
        self._write_idx = 0


class SpectralSubtractionNR:
    FFT_SIZE: int = 256
    HOP: int = 128       # 50% overlap → COLA-exact with Hanning

    # Per-profile DSP parameters:
    #   alpha        — over-subtraction factor (higher = more noise removed)
    #   beta         — spectral floor (higher = less musical-noise artifact)
    #   noise_track  — exp-smoothing rate for live noise estimator
    #   vad_gate     — frame is "noise" if power < noise_est × this factor
    #
    # Profiles control AGGRESSION ONLY.  Whether the noise reference
    # used in the gain math is the live VAD-tracked estimate or the
    # operator's captured profile is independent — see
    # ``set_use_captured_profile`` below.
    # NR1's only operator-facing knob is now a continuous strength
    # value in [0.0, 1.0].  0.0 = barely-on (subtle subtraction with
    # generous spectral floor), 1.0 = aggressive (deep subtraction
    # with tight floor).  All four DSP parameters are linearly
    # interpolated between the anchor values below — these are the
    # operator-validated tunings from the previous discrete-profile
    # design (Light / Medium / Heavy).  Anchor points map to:
    #
    #   strength=0.0 → old "Light"  (alpha=1.0, beta=0.20)
    #   strength=0.5 → old "Medium" (alpha=1.8, beta=0.12)
    #   strength=1.0 → old "Heavy"  (alpha=2.8, beta=0.06)
    #
    # Operators who liked "Heavy" can drag to 1.0; "Medium" sits at
    # 0.5; "Light" at 0.0; anything in between is now reachable
    # without picking from a fixed list.  Mirrors NR2's continuous
    # aggression slider for UX consistency.
    #
    # Legacy ``set_profile()`` API + DEFAULT_PROFILE are kept as a
    # backwards-compat shim so saved QSettings with values like
    # "light" / "medium" / "heavy" / "aggressive" still load.
    # LEGACY VAD-gated-tracker anchors.  Unchanged from v1.  These are
    # what the slider uses when the min-stats tracker is OFF — i.e.,
    # when LYRA_NR1_TRACKER is not "minstats" and set_minstats_tracker()
    # has not been called.  Because the legacy tracker is dead-on-arrival
    # (see KNOWN LIMITATION below), the audible difference between
    # Light/Medium/Heavy with these anchors is small — most of the
    # subtraction math is multiplied by a 1e-3 noise estimate that
    # never grows.  Kept for backwards compat / A-B testing.
    STRENGTH_MIN_PARAMS = {
        "alpha":       1.0,
        "beta":        0.20,
        "noise_track": 0.03,
        "vad_gate":    3.0,
    }
    STRENGTH_MAX_PARAMS = {
        "alpha":       2.8,
        "beta":        0.06,
        "noise_track": 0.008,
        "vad_gate":    4.0,
    }
    # MIN-STATS tracker anchors (Option B, actively working).
    #
    # ``beta`` dominates the perceived noise-floor residue: with a real
    # noise estimate, gain on noise-only bins clips to beta, so the
    # operator hears noise attenuated by ``-20*log10(beta)``.  The
    # legacy anchors were calibrated against a dead noise estimate
    # where subtraction barely fired, so slider movement was almost
    # cosmetic — the operator wouldn't hear a step change between
    # Light and Heavy.  The min-stats anchors stretch beta from 0.40
    # (-8 dB residue, audibly subtle) to 0.05 (-26 dB, deep silence)
    # so that strength 0.0 / 0.5 / 1.0 each have a distinct sound:
    #
    #   strength=0.0 → beta=0.40 → -8 dB  residue (subtle / Light)
    #   strength=0.5 → beta=0.22 → -13 dB residue (default / Medium)
    #   strength=1.0 → beta=0.05 → -26 dB residue (aggressive / Heavy)
    #
    # ``alpha`` (over-subtraction factor) is unchanged at the high end
    # because once it exceeds 1.0 the gain on pure-noise bins clips to
    # beta anyway — alpha mainly affects how aggressively we attack
    # marginal-SNR bins (signals close to the noise floor).
    STRENGTH_MIN_PARAMS_MINSTATS = {
        "alpha":       1.0,
        "beta":        0.40,
        "noise_track": 0.03,
        "vad_gate":    3.0,
    }
    STRENGTH_MAX_PARAMS_MINSTATS = {
        "alpha":       3.0,
        "beta":        0.03,
        "noise_track": 0.008,
        "vad_gate":    4.0,
    }
    DEFAULT_STRENGTH: float = 0.5

    # Legacy profile-name → strength map.  Used by set_profile() and
    # by Radio's QSettings migration path so saved values with the
    # old discrete names map cleanly to the new slider.
    _LEGACY_PROFILE_TO_STRENGTH: dict[str, float] = {
        "light":      0.0,
        "medium":     0.5,
        "heavy":      1.0,
        "aggressive": 1.0,   # legacy synonym for "heavy"
    }
    DEFAULT_PROFILE = "medium"   # kept for any callers that still
                                  # ask for it; maps to strength=0.5

    # ── Smart-guard thresholds ───────────────────────────────────
    # Two-layer detection (v0.0.7.x revision per nr_audit §4.2(d)):
    #
    # 1. Total-power CV check (LEGACY): coefficient of variation of
    #    per-frame total power across the capture window.  Catches
    #    captures with broadly unstable power (heavy QSB, intermittent
    #    sources).  Threshold 0.5 — stable band noise is well under,
    #    SSB syllables / CW keying push it well over.
    #
    # 2. Per-bin variance anomaly check (NEW): for each bin compute
    #    its CV (std/mean) over the capture frames.  Stationary tonal
    #    noise (powerline 60/120 Hz comb) has low CV per bin —
    #    correctly passes.  Signal contamination concentrates in a
    #    small set of bins which then have much higher CV than the
    #    rest of the spectrum — flagged as anomalous.  This catches
    #    the corner case the legacy CV-only check missed: tonal
    #    interference that's stable (low total-power CV) but is
    #    actually a contaminating signal.
    #
    # The fraction-anomalous threshold is operator-untuned; 5% of
    # bins flagging anomalous means 6+ bins out of 129 (FFT=256).
    # CW = 1-3 bins, SSB voice = 5-15 bins, AM carrier-with-modulation
    # = 1 carrier bin + a few sideband bins — all comfortably above
    # the threshold.  Stable powerline comb has only the carrier
    # bins themselves with low per-bin CV — comfortably below.
    GUARD_VARIANCE_THRESHOLD: float = 0.5
    # Layer-2 anomaly detection thresholds:
    #   GUARD_ANOMALY_BIN_FRAC: triggers when a fraction of bins
    #     (relative to total) exceeds the MAD-based outlier threshold.
    #     Catches SSB voice (5-15 bins anomalous) and AM
    #     modulation (carrier + sideband bins).
    #   GUARD_ANOMALY_MAX_CV: backstop that triggers if ANY single
    #     bin's CV exceeds this absolute threshold.  Catches CW
    #     keying which contaminates only 1-2 bins (below the
    #     fraction threshold) but pushes those bins to per-bin CV
    #     well above 1.0 (stdev > mean — a strong sign of an
    #     intermittent source).
    # The two are OR'd — flag if either triggers.
    GUARD_ANOMALY_BIN_FRAC: float = 0.03
    GUARD_ANOMALY_MAX_CV: float = 1.5
    # MAD multiplier for "anomalous bin" detection — robust analog
    # to ~5 sigma on Gaussian-distributed CV values.  Higher = more
    # permissive (only flag VERY anomalous bins); lower = more
    # strict.  5.0 is the textbook sigma-equivalent for MAD-based
    # outlier detection on Gaussian-ish data.
    GUARD_ANOMALY_MAD_MULT: float = 5.0

    # Capture duration sanity bounds (seconds).  Operator UI exposes
    # 1.0 - 5.0 sec range per locked operator decision; these are the
    # absolute bounds applied at the DSP layer in case a programmatic
    # caller passes something silly.
    CAPTURE_MIN_SEC: float = 0.5
    CAPTURE_MAX_SEC: float = 30.0

    def __init__(self, rate: int = 48000):
        self.rate = rate
        self._fft = self.FFT_SIZE
        self._hop = self.HOP
        self._window = np.hanning(self._fft).astype(np.float32)

        n_bins = self._fft // 2 + 1
        # Initial noise-floor guess — very small so the first speech
        # frame won't be obliterated while the estimator catches up.
        #
        # KNOWN LIMITATION: the live VAD-gated tracker has a
        # chicken-and-egg problem — the gate condition
        # (frame_pow <= noise_pow * vad_gate) never fires when
        # noise_pow starts at 1e-6 because real audio frames are
        # always much louder.  Result: the live noise estimate stays
        # frozen at 1e-3, which means LIVE-source NR1 produces
        # essentially zero subtraction regardless of profile choice
        # (alpha * 1e-3 / |X| is microscopic).
        #
        # An attempt to fix this (2026-05-01) added first-frame
        # seeding + temporal gain smoothing, but field-test feedback
        # was that the resulting subtraction had audible "watery /
        # underwater" artifacts at all three tiers.  Reverted.
        # Operators get effective NR via:
        #   - Captured noise profiles (bypass the live tracker)
        #   - NR2 (Ephraim-Malah MMSE-LSA, no musical noise)
        # A proper redesign of the live tracker (likely
        # minimum-statistics + spectral smoothing) is queued as
        # backlog work.
        self._noise_mag = np.full(n_bins, 1e-3, dtype=np.float32)

        # Minimum-statistics noise tracker (Martin 2001).  Replaces
        # the dead VAD-gated estimator above as the source of the
        # live noise reference.  ENABLED BY DEFAULT — operators get
        # working live-source NR1 out of the box without setting
        # any env vars.  Override behavior:
        #
        #     LYRA_NR_TRACKER=legacy     → opt OUT (both NR1 + NR2)
        #     LYRA_NR1_TRACKER=legacy    → opt OUT for NR1 only
        #     LYRA_NR_TRACKER=minstats   → explicit opt-in (no-op now)
        #     unset                      → DEFAULT (min-stats enabled)
        #
        # The env var only controls the *startup* default —
        # set_minstats_tracker() can flip it at runtime for A/B
        # testing without restarting.  See _MinStatsTracker docstring
        # for the algorithm details.
        self._minstats: Optional[_MinStatsTracker] = None
        env = (os.environ.get("LYRA_NR_TRACKER", "")
               or os.environ.get("LYRA_NR1_TRACKER", "")
               ).strip().lower()
        # Default ON; env var opts out.
        if env != "legacy":
            self._enable_minstats()

        # Streaming state
        self._in_buf = np.zeros(0, dtype=np.float32)
        # overlap-add carry for the tail of the last frame
        self._out_carry = np.zeros(self._hop, dtype=np.float32)

        self.enabled = False
        # Strength is the new operator-facing knob.  profile remains
        # tracked for the legacy set_profile() API; it always reflects
        # whichever name's strength is currently active (or "custom"
        # if the strength was set directly to a non-anchor value).
        self.strength: float = self.DEFAULT_STRENGTH
        self.profile: str = self.DEFAULT_PROFILE
        self._apply_strength(self.DEFAULT_STRENGTH)

        # ── Phase 3.D #1: captured-noise-profile state ────────────
        # ``_captured_noise_mag`` holds the locked per-bin magnitude
        # array when a profile is loaded (n_bins float32).  None
        # means "no profile loaded" — the source toggle below has no
        # effect when the array is None (graceful fallback to live
        # tracking).
        self._captured_noise_mag: Optional[np.ndarray] = None
        # Source toggle: when True AND _captured_noise_mag is loaded,
        # the gain math substitutes the captured magnitudes for the
        # live VAD-tracked estimate.  Independent of self.profile
        # (operator picks aggression and source separately).
        self._use_captured_profile: bool = False
        # Capture lifecycle:
        #   "idle"       — no capture in progress, no recent result
        #   "capturing"  — accumulating frames; process() is feeding
        #                  the accumulator
        #   "ready"      — last capture finished; results in
        #                  _captured_noise_mag and _capture_verdict
        # State transitions are single-attribute writes (GIL-safe);
        # the worker thread reads "capturing"/"idle" inside process()
        # and the main thread reads "ready" after the done-callback
        # fires.
        self._capture_state: str = "idle"
        self._capture_frames_target: int = 0
        self._capture_frames_done: int = 0
        self._capture_accum: Optional[np.ndarray] = None
        # Per-frame total-power list for the legacy smart-guard
        # variance check.  Cleared at begin_noise_capture; populated
        # during capture; inspected at finalize.  Kept around
        # afterwards so the UI can re-query smart_guard_verdict().
        self._capture_per_frame_powers: list[float] = []
        # Sum-of-squares-per-bin accumulator (v0.0.7.x — for the
        # per-bin variance anomaly check).  Same lifecycle as
        # _capture_accum; lets us compute per-bin std at finalize
        # without storing every frame's spectrum (memory-bounded).
        # Math:
        #   E[X²]   = sum_sq / N
        #   E[X]²   = (sum / N)²
        #   Var[X]  = E[X²] - E[X]²
        self._capture_accum_sq: Optional[np.ndarray] = None
        self._capture_verdict: str = "n/a"  # n/a | clean | suspect
        # Operator-tunable in v2 (Settings → Noise tab).  For day 1
        # the smart guard is always on; UI exposes a toggle later.
        self._smart_guard_enabled: bool = True
        # Done-callback: Radio registers a function here that emits
        # a Qt signal so the UI can react.  Fires from inside
        # process() on whatever thread is running it (worker thread
        # in worker mode, Qt main in single-thread).  The callback
        # is responsible for any cross-thread dispatch (Qt signals
        # with QueuedConnection handle this for free).
        self._capture_done_callback: Optional[Callable[[], None]] = None

    # ── public API ────────────────────────────────────────────────
    def set_strength(self, value: float) -> None:
        """Set the NR1 strength (new continuous-knob API).

        ``value`` is clamped to [0.0, 1.0].  0.0 = barely-on subtle
        subtraction; 1.0 = aggressive deep subtraction.  Each of
        alpha / beta / noise_track / vad_gate is linearly
        interpolated between the STRENGTH_MIN_PARAMS and
        STRENGTH_MAX_PARAMS anchor dicts.

        Side-effect: updates ``self.profile`` to the legacy name
        whose strength the new value matches (light/medium/heavy)
        if the value lands on an anchor, else "custom".  The legacy
        ``profile`` field is kept around so external code that
        still inspects it (e.g. the existing nr_profile_changed
        signal piggy-backing) keeps working — but the new code
        path should treat ``self.strength`` as the source of truth.
        """
        s = max(0.0, min(1.0, float(value)))
        self.strength = s
        self._apply_strength(s)
        # Update the legacy profile-name field so old read sites
        # still report something meaningful.  Anchor matches are
        # exact (operator clicked a preset shortcut); anything else
        # is reported as "custom".
        if s == 0.0:
            self.profile = "light"
        elif abs(s - 0.5) < 1e-6:
            self.profile = "medium"
        elif s == 1.0:
            self.profile = "heavy"
        else:
            self.profile = "custom"

    def set_profile(self, name: str) -> None:
        """Legacy API — accepts old discrete profile names and maps
        them to the appropriate strength via _LEGACY_PROFILE_TO_STRENGTH.

        Kept so that QSettings from older Lyra versions still load
        cleanly.  New code should call set_strength() directly.
        """
        if name in self._LEGACY_PROFILE_TO_STRENGTH:
            self.set_strength(self._LEGACY_PROFILE_TO_STRENGTH[name])

    def reset(self):
        """Drop all streaming state — call on mode / rate / stream
        transitions so a stale overlap tail doesn't leak into new audio.

        Cancels any in-progress capture (the audio discontinuity
        that triggered reset is exactly the kind of thing that
        would corrupt a noise profile).  The captured profile
        itself — operator-locked — is preserved across reset.

        The min-stats tracker (when enabled) also gets reset so a
        new band with a different noise floor doesn't inherit the
        previous band's per-bin minima.
        """
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_carry = np.zeros(self._hop, dtype=np.float32)
        n_bins = self._fft // 2 + 1
        self._noise_mag = np.full(n_bins, 1e-3, dtype=np.float32)
        if self._minstats is not None:
            self._minstats.reset()
        if self._capture_state == "capturing":
            self.cancel_noise_capture()

    # ── Live noise tracker selection (legacy VAD vs min-stats) ─────

    def _enable_minstats(self) -> None:
        """Construct the min-stats tracker sized for the current
        rate / FFT / hop.  ~1.5 sec sliding window."""
        n_bins = self._fft // 2 + 1
        win_sec = 1.5
        n_frames = max(8, int(round(win_sec * self.rate / self._hop)))
        self._minstats = _MinStatsTracker(n_bins, n_frames)

    def set_minstats_tracker(self, on: bool) -> None:
        """Enable / disable the minimum-statistics noise tracker at
        runtime.  Default state at construction is governed by the
        ``LYRA_NR1_TRACKER`` env var.

        Captured-profile mode is unaffected — when a captured profile
        is loaded and selected as source, it always wins regardless
        of which live tracker is active in the background.

        Re-applies the current strength after the toggle so the
        tracker-specific anchor set (legacy vs min-stats) takes
        effect immediately.  Without this, toggling at runtime would
        swap trackers but leave the gain coefficients calibrated for
        the previous tracker.
        """
        if bool(on):
            if self._minstats is None:
                self._enable_minstats()
        else:
            self._minstats = None
        self._apply_strength(self.strength)

    def is_minstats_enabled(self) -> bool:
        """True if the min-stats tracker is the active live noise
        estimator.  Used by the audit driver and by any future UI
        diagnostics that want to surface the choice."""
        return self._minstats is not None

    # ── Captured noise profile API (Phase 3.D #1) ─────────────────

    def begin_noise_capture(self, seconds: float = 2.0) -> None:
        """Start capturing the noise profile from the next ``seconds``
        of incoming audio.

        Returns immediately; the actual capture happens inside the
        ``process()`` loop over the next ~``seconds * rate / hop``
        frames.  Caller can poll ``capture_progress()`` to drive a
        progress bar, or rely on the registered done-callback (see
        ``set_capture_done_callback``) to fire when capture finishes.

        If a capture is already in progress, the call is a no-op
        (UI should disable the button while ``_capture_state ==
        "capturing"`` to prevent this case in the first place, but
        the silent no-op is the safest fallback).

        ``seconds`` is clamped to ``[CAPTURE_MIN_SEC, CAPTURE_MAX_SEC]``;
        the operator UI normally constrains it to 1.0 - 5.0 per the
        locked operator decision.
        """
        if self._capture_state == "capturing":
            return
        seconds = float(max(self.CAPTURE_MIN_SEC,
                            min(seconds, self.CAPTURE_MAX_SEC)))
        # Frames per second = rate / hop (~375 fps at 48k / 128 hop).
        frames = max(1, int(round(seconds * self.rate / self._hop)))
        n_bins = self._fft // 2 + 1
        # Use float64 for the accumulators — capture can run for
        # thousands of frames and float32 sums lose precision.
        # Cast back to float32 at finalize.
        self._capture_accum = np.zeros(n_bins, dtype=np.float64)
        self._capture_accum_sq = np.zeros(n_bins, dtype=np.float64)
        self._capture_per_frame_powers = []
        self._capture_frames_target = frames
        self._capture_frames_done = 0
        self._capture_verdict = "n/a"
        # Flush leftover STFT samples from previous processing so
        # the first capture frame is built from purely-new audio.
        # Without this flush, leftover samples from a previous
        # different signal (e.g., a CW capture before a noise
        # capture) contaminate the first 1-2 frames of the new
        # capture and can push the smart-guard's per-bin variance
        # check just over the anomaly threshold.  Operator-visible
        # symptom: stable noise after a noisy capture sometimes
        # gets flagged "suspect" even though the new audio is clean.
        self._in_buf = np.zeros(0, dtype=np.float32)
        # Last write — flips state for process() to start
        # accumulating on its next frame.  Single-attribute write
        # is atomic under GIL; safe across thread boundaries.
        self._capture_state = "capturing"

    def cancel_noise_capture(self) -> None:
        """Abort an in-progress capture.

        State returns to "idle"; partial accumulator is discarded;
        the active captured profile (if any) is preserved.  No-op
        if no capture is in progress.
        """
        if self._capture_state != "capturing":
            return
        self._capture_state = "idle"
        self._capture_accum = None
        self._capture_accum_sq = None
        self._capture_per_frame_powers = []
        self._capture_frames_target = 0
        self._capture_frames_done = 0
        self._capture_verdict = "n/a"

    def has_captured_profile(self) -> bool:
        """True if a captured profile is currently loaded.

        Independent of the source toggle: a profile may be loaded
        but the source toggle could be Live (operator listening
        with live tracking).  Use ``is_using_captured_source()``
        to check whether the captured profile is actively driving
        the gain math."""
        return self._captured_noise_mag is not None

    def set_use_captured_profile(self, on: bool) -> None:
        """Operator toggles the noise SOURCE.

        When True, ``process()`` uses ``_captured_noise_mag`` as the
        noise reference (assuming a profile is loaded; falls back to
        live tracking if not).  When False, always uses the live
        VAD-tracked estimate.

        Independent of which profile (Light/Medium/Heavy) is
        active — those control subtraction aggression, this controls
        which noise model the subtraction works against.
        """
        self._use_captured_profile = bool(on)

    def is_using_captured_source(self) -> bool:
        """True if the source toggle is set to "Captured" AND a
        profile is loaded — i.e., process() will actually use the
        captured magnitudes for this block.  False otherwise (either
        the toggle is off, OR there's no profile loaded so we're
        falling back to live anyway)."""
        return (self._use_captured_profile
                and self._captured_noise_mag is not None)

    def captured_profile_array(self) -> Optional[np.ndarray]:
        """Return a copy of the active captured noise magnitudes,
        or None if no profile is loaded.

        Used by the JSON persistence layer (Day 2) to serialize the
        profile to disk.  Always returns a copy so callers can't
        mutate the live NR state."""
        if self._captured_noise_mag is None:
            return None
        return self._captured_noise_mag.copy()

    def load_captured_profile(self, mag: np.ndarray) -> None:
        """Install a previously-saved captured profile (loaded from
        the JSON persistence layer).

        Auto-resamples the bin axis to match the current FFT_SIZE.
        Captured profiles cover the same frequency range (DC → Nyquist)
        regardless of FFT_SIZE — they just sample it at different
        resolutions — so linear interpolation across normalized bin
        index produces a valid profile at any target FFT size.  This
        unblocks FFT_SIZE upgrades without invalidating saved
        profiles.

        Raises ValueError only on degenerate inputs (1-D shape,
        non-empty).
        """
        n_bins = self._fft // 2 + 1
        arr = np.asarray(mag, dtype=np.float32).ravel()
        if arr.size < 2:
            raise ValueError(
                f"captured profile must have >=2 bins; got {arr.size}")
        if arr.size != n_bins:
            # Linear interp across normalized bin axis.  Both old and
            # new arrays represent the same frequency range, so
            # x_old = linspace(0, 1, n_old), x_new = linspace(0, 1, n_new),
            # and np.interp gives the right answer.  Endpoints are
            # preserved exactly.
            x_old = np.linspace(0.0, 1.0, arr.size, dtype=np.float64)
            x_new = np.linspace(0.0, 1.0, n_bins, dtype=np.float64)
            arr = np.interp(x_new, x_old, arr).astype(np.float32)
        # Defensive: ensure no zero / negative bins (would divide-
        # by-zero in the gain calc).  Floor at 1e-6 (well below any
        # real noise, well above zero).
        arr = np.maximum(arr, 1e-6).astype(np.float32, copy=False)
        self._captured_noise_mag = arr.copy()

    def clear_captured_profile(self) -> None:
        """Drop the active captured profile.

        After this call, ``has_captured_profile()`` returns False.
        If ``profile == "captured"`` was active, ``process()`` will
        fall back to live noise tracking with the captured-profile
        tuning values (UI normally switches profile back to a live
        mode in this case)."""
        self.cancel_noise_capture()
        self._captured_noise_mag = None

    def capture_progress(self) -> tuple[str, float]:
        """Return ``(state, fraction_complete)`` for UI progress
        reporting.

        - state ∈ {"idle", "capturing", "ready"}
        - fraction is 0.0 - 1.0 while capturing, 0.0 in other states
        """
        if self._capture_state != "capturing":
            return (self._capture_state, 0.0)
        if self._capture_frames_target <= 0:
            return ("capturing", 0.0)
        frac = self._capture_frames_done / self._capture_frames_target
        return ("capturing", min(1.0, max(0.0, frac)))

    def feed_capture(self, audio: np.ndarray) -> None:
        """FFT-only path that accumulates magnitudes into the capture
        buffer WITHOUT touching the output audio.

        Why this exists: when NR2 is the active processor, Channel
        routes audio through ``_nr2.process()`` and never calls
        ``_nr.process()`` — so this accumulator never advances and
        the Cap button appears dead.  ``feed_capture()`` lets
        Channel run the lightweight FFT-and-accumulate loop on the
        side regardless of which NR is active, so Cap works
        identically in NR1 and NR2 modes.

        Behavior:
        - If no capture is in progress: returns immediately, no work
        - Otherwise: walks ``audio`` through the same STFT framing
          ``process()`` uses, accumulates frame magnitudes into
          ``_capture_accum``, and increments ``_capture_frames_done``
        - Audio is NOT modified and NOT returned (caller's audio
          pipeline already has its samples)

        Implementation note: this reuses ``_in_buf`` (NR1's STFT
        ring) the same way the capture-without-NR branch in
        ``process()`` does.  If the operator switches back to NR1
        mid-capture, the buffer state is exactly what it would be
        had NR1 been running with ``enabled=False`` the whole time
        — so the switch is sample-clean.
        """
        if self._capture_state != "capturing" or audio.size == 0:
            return
        x = audio.astype(np.float32, copy=False)
        self._in_buf = np.concatenate([self._in_buf, x])
        while self._in_buf.size >= self._fft:
            frame = self._in_buf[:self._fft] * self._window
            mag = np.abs(np.fft.rfft(frame))
            self._accumulate_capture_frame(mag)
            self._in_buf = self._in_buf[self._hop:]

    def smart_guard_verdict(self) -> str:
        """Verdict from the most recent capture's smart-guard check.

        - "clean"   — low frame-to-frame power variance (= band noise)
        - "suspect" — high variance suggests a signal was present
        - "n/a"     — guard disabled, or no recent capture
        """
        return self._capture_verdict

    def set_capture_done_callback(
            self, fn: Optional[Callable[[], None]]) -> None:
        """Register (or clear) a function to be called when a
        capture completes — i.e., the state transitions from
        "capturing" → "ready".

        The callback is invoked from inside ``process()`` on
        whatever thread is currently running the audio chain.  In
        worker mode this is the DSP worker thread; in single-thread
        mode it's the Qt main thread.  The callback is responsible
        for any cross-thread dispatch — typical pattern is to emit
        a Qt signal with QueuedConnection so the slot runs on main
        regardless of where it was emitted.

        Pass None to clear an existing callback.  Multiple calls
        replace; we don't support multiple subscribers (Radio is
        the only intended caller).
        """
        self._capture_done_callback = fn

    def _evaluate_capture_quality(self) -> str:
        """Smart-guard heuristic — two-layer detection.

        Layer 1 (legacy total-power CV):
            Inspect the per-frame total-power list.  Stable band
            noise has CV well under 0.5; SSB syllables / CW keying
            push it well over.  Catches captures with broadly
            unstable power.

        Layer 2 (per-bin variance anomaly, NEW v0.0.7.x):
            For each FFT bin, compute its CV (std/mean) over the
            capture frames.  Stationary tonal noise (60/120 Hz
            powerline harmonic comb) has low per-bin CV — passes
            cleanly.  Signal contamination concentrates in a small
            set of bins (CW = 1-3 bins, SSB voice = 5-15 bins) that
            then have CV much higher than the rest of the spectrum
            — flagged as anomalous via robust MAD-based outlier
            detection.  Catches the corner case Layer 1 misses:
            stable tonal interference that's actually a signal.

        Verdict logic:
            Both pass               → "clean"
            Layer 1 fails           → "suspect" (broadly noisy)
            Layer 2 fails           → "suspect" (signal contamination)
            Either fails            → "suspect"
            Insufficient data       → "n/a"

        For UI compatibility the return value stays in the historical
        {"n/a", "clean", "suspect"} set.  Layer-2 detection is now
        the dominant failure mode for tonal contamination — the most
        common real-world false-pass case before this change.
        """
        if not self._smart_guard_enabled:
            return "n/a"
        if not self._capture_per_frame_powers:
            return "n/a"
        powers = np.asarray(self._capture_per_frame_powers,
                            dtype=np.float64)
        if powers.size < 4:
            # Too few frames for a meaningful variance estimate.
            return "n/a"

        # ── Layer 1: total-power CV ─────────────────────────────
        mean = float(np.mean(powers))
        if mean <= 0.0:
            return "n/a"
        total_cv = float(np.std(powers)) / mean
        layer1_suspect = total_cv > self.GUARD_VARIANCE_THRESHOLD

        # ── Layer 2: per-bin variance anomaly ───────────────────
        # Requires both the sum and sum-of-squares accumulators.
        # If either is missing (capture aborted, accumulator never
        # created), fall back to layer-1 result only.
        #
        # Algorithm:
        #   1. Compute per-bin CV.  But — and this is the key fix —
        #      only analyze bins whose mean magnitude is meaningfully
        #      above the overall median.  Low-magnitude bins (DC,
        #      near-Nyquist) have numerically noisy CV due to
        #      small-denominator effects and would dominate any
        #      max-CV check with garbage.
        #   2. For "active" bins (magnitude > threshold), look for
        #      ones whose CV is anomalously high vs the rest of the
        #      active bins.
        #
        # Three signature patterns:
        #   * Pure broadband noise: most bins similar mean (Rayleigh),
        #     all with CV ~0.52 (Rayleigh constant).  No active-bin
        #     anomalies.  → clean
        #   * Stable carrier / harmonics: 1-3 bins with mean MUCH
        #     higher than median, those bins with LOW CV (signal
        #     dominates noise).  Active-bin CV is below the noise-
        #     bin CV → not flagged.  → clean
        #   * Intermittent signal (CW, SSB syllables): 1-15 bins with
        #     elevated mean AND high CV (signal swings between
        #     present/absent).  → suspect
        layer2_suspect = False
        if (self._capture_accum is not None
                and self._capture_accum_sq is not None
                and self._capture_frames_done >= 4):
            n = float(self._capture_frames_done)
            mean_per_bin = self._capture_accum / n               # E[X]
            mean_sq_per_bin = self._capture_accum_sq / n         # E[X²]
            # Var = E[X²] - E[X]² ; clip to 0 for numerical safety.
            var_per_bin = np.maximum(
                mean_sq_per_bin - mean_per_bin * mean_per_bin,
                0.0)
            std_per_bin = np.sqrt(var_per_bin)

            # Filter to "active" bins — mean magnitude above
            # half the median across all bins.  This drops near-zero
            # numerical-noise bins (DC, top of audio band) that
            # would otherwise pollute the CV analysis.
            median_mag = float(np.median(mean_per_bin))
            active_mask = mean_per_bin > 0.5 * median_mag
            n_active = int(np.sum(active_mask))
            if n_active >= 8:
                cv_active = (std_per_bin[active_mask]
                             / np.maximum(mean_per_bin[active_mask],
                                          1e-10))
                # Robust outlier detection within active bins.
                median_cv = float(np.median(cv_active))
                mad = float(np.median(np.abs(cv_active - median_cv)))
                outlier_thresh = (median_cv
                                  + self.GUARD_ANOMALY_MAD_MULT
                                  * 1.4826 * mad)
                n_anomalous = int(np.sum(cv_active > outlier_thresh))
                frac_anomalous = n_anomalous / float(n_active)
                max_cv = float(np.max(cv_active))
                layer2_suspect = (
                    frac_anomalous > self.GUARD_ANOMALY_BIN_FRAC
                    or max_cv > self.GUARD_ANOMALY_MAX_CV)

        return "suspect" if (layer1_suspect or layer2_suspect) else "clean"

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one demod block. Returns the same length of audio
        (possibly delayed by one hop on the very first call) or the
        input unchanged when both NR is disabled and no capture is
        running.

        Behavior gates:
        - ``enabled == False`` and not capturing → input returned
          unchanged (full NR bypass; cheapest path)
        - ``enabled == False`` and capturing → input returned
          unchanged BUT a parallel FFT loop accumulates noise
          magnitudes for the capture profile; lets the operator
          capture without first turning NR on
        - ``enabled == True`` → full NR processing; if capturing
          also runs, the same FFT serves both purposes
        - ``profile == "captured"`` and a captured profile is loaded
          → uses the captured magnitudes as the noise reference;
          live VAD tracker still runs in the background as a
          fallback in case the captured profile is cleared
        """
        if audio.size == 0:
            return audio
        capturing = (self._capture_state == "capturing")

        # Fast path: NR disabled, not capturing — nothing to do.
        if not self.enabled and not capturing:
            return audio

        # Capture-without-NR path: do a lightweight FFT loop that
        # accumulates magnitudes but does NOT modify the audio.
        # Operator hears their unchanged audio while the profile
        # builds up in the background.
        if not self.enabled and capturing:
            x = audio.astype(np.float32, copy=False)
            self._in_buf = np.concatenate([self._in_buf, x])
            while self._in_buf.size >= self._fft:
                frame = self._in_buf[:self._fft] * self._window
                spec = np.fft.rfft(frame)
                mag = np.abs(spec)
                self._accumulate_capture_frame(mag)
                # Advance — overlap by hop, NO out_chunks emitted,
                # NO _out_carry update.  Audio is untouched.
                self._in_buf = self._in_buf[self._hop:]
            return audio

        # NR enabled — full processing path (with optional capture
        # piggybacking on the same FFT).
        x = audio.astype(np.float32, copy=False)
        self._in_buf = np.concatenate([self._in_buf, x])

        out_chunks: list[np.ndarray] = []
        while self._in_buf.size >= self._fft:
            frame = self._in_buf[:self._fft] * self._window
            spec = np.fft.rfft(frame)
            mag = np.abs(spec)

            # Phase 3.D #1 — capture accumulation, when active.
            # Runs in the same FFT loop as NR for efficiency — one
            # FFT serves both purposes.
            if capturing:
                self._accumulate_capture_frame(mag)
                # Re-read state — _accumulate may have flipped it
                # to "ready" via _finalize_capture().
                capturing = (self._capture_state == "capturing")

            # Live noise-floor tracking.  Two implementations:
            #
            #   1. Legacy VAD-gated (default): update _noise_mag only
            #      when the frame is quieter than vad_gate × current
            #      estimate.  Has the dead-on-arrival bug noted in
            #      __init__ — gate rarely fires on real audio so the
            #      estimate stays frozen at 1e-3.
            #
            #   2. Minimum-statistics (LYRA_NR1_TRACKER=minstats):
            #      sliding-window per-bin minimum.  Self-bootstraps,
            #      no gate, no dead zone.
            #
            # Whichever live tracker is active runs in the background
            # even when the captured source is selected, so the live
            # estimate stays warm as a fallback if the operator
            # clears or re-toggles the captured profile.
            if self._minstats is not None:
                live_noise_ref = self._minstats.update(mag)
            else:
                frame_pow = float(np.mean(mag * mag))
                noise_pow = float(
                    np.mean(self._noise_mag * self._noise_mag))
                if frame_pow <= noise_pow * self._vad_gate:
                    a = self._noise_track
                    self._noise_mag = (
                        (1.0 - a) * self._noise_mag + a * mag)
                live_noise_ref = self._noise_mag

            # Choose the noise reference based on the source toggle.
            # Captured source wins when the toggle is on AND a
            # profile is loaded; otherwise fall back to the active
            # live estimator (legacy or min-stats).
            if (self._use_captured_profile
                    and self._captured_noise_mag is not None):
                noise_ref = self._captured_noise_mag
            else:
                noise_ref = live_noise_ref

            # Magnitude-domain subtraction with spectral floor.
            denom = np.maximum(mag, 1e-10)
            gain = np.maximum(1.0 - self._alpha * noise_ref / denom,
                              self._beta).astype(np.float32)
            time_frame = np.fft.irfft(spec * gain, self._fft).astype(np.float32)

            # Overlap-add: first hop samples get combined with the
            # carried tail from the previous frame; back half becomes
            # new carry. Hanning @ 50% overlap = COLA-exact, so the
            # sum cleanly reconstructs unity-gain signal regions.
            head = self._out_carry + time_frame[:self._hop]
            out_chunks.append(head)
            self._out_carry = time_frame[self._hop:].copy()

            # Advance input by one hop
            self._in_buf = self._in_buf[self._hop:]

        if not out_chunks:
            # First call didn't produce a full hop yet — return silence
            # of matching length so downstream doesn't see a length change.
            # This happens only when the very first block is smaller than
            # FFT_SIZE, which doesn't occur with Radio's 2048-sample blocks.
            return np.zeros_like(audio)

        output = np.concatenate(out_chunks)
        # Match length to the input block by padding or trimming. This
        # preserves per-block timing even with the ½-frame internal lag.
        if output.size < audio.size:
            output = np.concatenate(
                [output, np.zeros(audio.size - output.size, dtype=np.float32)])
        elif output.size > audio.size:
            output = output[:audio.size]
        return output

    # ── internals ─────────────────────────────────────────────────
    def _apply_strength(self, s: float) -> None:
        """Recompute alpha / beta / noise_track / vad_gate from the
        slider value via linear interpolation between two anchor dicts.

        Anchor selection depends on which live tracker is active:
        - min-stats tracker enabled → STRENGTH_*_PARAMS_MINSTATS
          (wider beta range so the slider produces audibly distinct
          Light / Medium / Heavy)
        - legacy VAD tracker        → STRENGTH_*_PARAMS
          (v1 anchors; preserved for backwards compat)

        Called from set_strength(), from __init__, and from
        set_minstats_tracker() so the curve switches in real time
        when the operator toggles the tracker.
        """
        s = max(0.0, min(1.0, float(s)))
        if self._minstats is not None:
            lo = self.STRENGTH_MIN_PARAMS_MINSTATS
            hi = self.STRENGTH_MAX_PARAMS_MINSTATS
        else:
            lo = self.STRENGTH_MIN_PARAMS
            hi = self.STRENGTH_MAX_PARAMS
        def lerp(key: str) -> float:
            return float(lo[key] + (hi[key] - lo[key]) * s)
        self._alpha = lerp("alpha")
        self._beta = lerp("beta")
        self._noise_track = lerp("noise_track")
        self._vad_gate = lerp("vad_gate")

    def _accumulate_capture_frame(self, mag: np.ndarray) -> None:
        """Add one frame's magnitude spectrum + total power into the
        capture accumulator.  When the target frame count is hit,
        finalize via :meth:`_finalize_capture`.

        Called from inside ``process()``'s per-frame loop on
        whatever thread is running NR (worker thread in worker
        mode, Qt main in single-thread mode).
        """
        if self._capture_accum is None:
            return
        mag64 = mag.astype(np.float64)
        self._capture_accum += mag64
        # Per-bin sum-of-squares for the per-bin variance check at
        # finalize time.  Cost: one ndarray multiply + one add per
        # frame — microseconds at FFT=256.
        if self._capture_accum_sq is not None:
            self._capture_accum_sq += mag64 * mag64
        self._capture_per_frame_powers.append(float(np.sum(mag * mag)))
        self._capture_frames_done += 1
        if self._capture_frames_done >= self._capture_frames_target:
            self._finalize_capture()

    def _finalize_capture(self) -> None:
        """Convert the accumulated frame sum into a captured profile,
        run the smart-guard quality check, transition state to
        "ready", and fire the registered done-callback.

        Always called from inside ``process()`` (i.e. on the audio
        thread).  The done-callback is responsible for any cross-
        thread dispatch (Radio's wrapper emits a Qt signal with
        QueuedConnection).
        """
        if self._capture_accum is None or self._capture_frames_target <= 0:
            # Defensive — _accumulate_capture_frame guards against
            # this but belt-and-suspenders if state is somehow
            # inconsistent.
            self._capture_state = "idle"
            return
        # Average over the actual frame count we collected (matches
        # _capture_frames_done — should equal _capture_frames_target
        # at this point but use the actual count for safety).
        n = max(1, self._capture_frames_done)
        avg_mag = (self._capture_accum / n).astype(np.float32)
        # Floor at 1e-6 — same protection load_captured_profile()
        # applies for externally-loaded profiles.
        avg_mag = np.maximum(avg_mag, 1e-6).astype(np.float32, copy=False)
        self._captured_noise_mag = avg_mag
        # Smart-guard verdict — uses the per-bin sum-of-squares too,
        # so this MUST run before we null out _capture_accum_sq.
        self._capture_verdict = self._evaluate_capture_quality()
        # Drop the accumulators; per-frame-power list is kept around
        # in case the UI re-queries the verdict, and gets reset on
        # the next begin_noise_capture() call.
        self._capture_accum = None
        self._capture_accum_sq = None
        # Last write — flips state.  Done before the callback so
        # the callback can rely on state == "ready".
        self._capture_state = "ready"
        cb = self._capture_done_callback
        if cb is not None:
            try:
                cb()
            except Exception as exc:
                # Never let a bad callback crash the audio thread.
                # Print for diagnostics; capture is still complete
                # and the profile is available via the public API
                # if the UI polls instead of relying on the callback.
                print(f"[SpectralSubtractionNR] capture-done "
                      f"callback raised: {exc}")
