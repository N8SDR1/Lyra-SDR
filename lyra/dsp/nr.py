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

from typing import Callable, Optional

import numpy as np


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
    # Profile naming canonical across the noise-rejection modules
    # (NB / ANF / NR1) is "light" / "medium" / "heavy" — operator
    # mental model: "how hard does this thing work."  Leveler is
    # different: it uses "latenight" for its strongest tier because
    # that name describes the actual late-night-listening use case
    # the profile was tuned for, not just an intensity level.
    # Legacy QSettings values ("aggressive") are accepted via
    # _CANONICAL_ALIASES so old saves still load.
    # Profile parameter tuning (re-tuned 2026-05-01 after operator
    # listening test):
    #
    # alpha — over-subtraction factor.  Higher = more noise removed
    #          per bin.  Larger alpha means bins near the noise
    #          floor get pushed harder toward zero.  Range used
    #          here (1.5..3.0) is the standard Berouti-Schwartz
    #          range for speech NR.
    # beta  — spectral floor.  Caps how low gain can drop.  IMPORTANT
    #          UX nuance: a HIGH beta keeps "comfort noise" but also
    #          lets per-bin gain modulate randomly between beta and
    #          1.0, producing audible "watery / underwater" musical-
    #          noise artifacts.  A LOW beta pushes quiet bins to
    #          near-silence — bins stay low instead of fluctuating,
    #          so musical noise is much less audible even though
    #          subtraction is more aggressive overall.  We keep beta
    #          low across all three tiers; the tiers vary alpha to
    #          control HOW MUCH gets subtracted, not how loud the
    #          residue is.
    # gain_smooth — temporal smoothing factor for the per-bin gain.
    #          Each frame: g[n] = gain_smooth * g[n-1] + (1-gain_smooth) * g_raw
    #          0.0 = no smoothing (raw gain — strong musical noise)
    #          0.7 = strong smoothing (clean but slower response)
    #          Light gets the heaviest smoothing because the user
    #          expects "Light = barely processed = clean."  Heavy
    #          gets lighter smoothing — operators picking Heavy are
    #          accepting some processing character in exchange for
    #          deeper hiss reduction.
    PROFILES: dict[str, dict[str, float]] = {
        # Light: subtle subtraction; only the loudest noise bins get
        # cut.  Heavy temporal smoothing (gain_smooth=0.7) kills the
        # "watery / underwater" musical-noise artifact that bothered
        # operators in the previous tuning.  Should sound "barely
        # processed" — minor hiss reduction without artifact.
        "light":      {"alpha": 1.5, "beta": 0.05, "noise_track": 0.020,
                       "vad_gate": 3.0, "gain_smooth": 0.70},
        # Medium: standard speech-NR setting.  Moderate smoothing
        # keeps the artifact down while letting the gain track real
        # noise-floor changes (someone switching from a quiet band
        # to a noisy one shouldn't take 5 seconds to converge).
        "medium":     {"alpha": 2.2, "beta": 0.04, "noise_track": 0.012,
                       "vad_gate": 3.0, "gain_smooth": 0.55},
        # Heavy: aggressive subtraction for noisy bands and
        # weak-signal DX.  Lighter smoothing (0.35) — at this
        # subtraction depth, most quiet bins are clamped at the
        # beta floor anyway, so the dominant audible quality is
        # the depth not the modulation.  Faster response is more
        # valuable here than artifact suppression.
        "heavy":      {"alpha": 3.0, "beta": 0.03, "noise_track": 0.008,
                       "vad_gate": 4.0, "gain_smooth": 0.35},
    }
    _CANONICAL_ALIASES: dict[str, str] = {
        "aggressive": "heavy",
    }
    DEFAULT_PROFILE = "medium"

    # Smart-guard threshold — coefficient of variation (std/mean) of
    # per-frame power during capture above which we flag "suspect".
    # Quiet band noise has CV well under 0.5 (frame-to-frame power
    # stable); CW keying or SSB syllables push CV above 0.5 quickly.
    GUARD_VARIANCE_THRESHOLD: float = 0.5

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
        # NOTE: this gets OVERWRITTEN on the first real frame via
        # the seed-from-first-frame logic in process().  Without
        # seeding, the VAD gate (which compares frame_pow against
        # noise_pow*vad_gate) never triggers because real noise sits
        # in the 0.05-0.5 magnitude range and the threshold here
        # (1e-3)^2 * 3 = 3e-6 is too small to ever be crossed.
        self._noise_mag = np.full(n_bins, 1e-3, dtype=np.float32)
        # Tracks whether the first frame has seeded _noise_mag yet.
        # Reset() clears this so the seed re-runs after audio
        # discontinuities (mode switch, freq jump, etc.).
        self._noise_mag_seeded: bool = False
        # Previous frame's gain — used for temporal gain smoothing
        # to suppress the "musical noise" artifact (random per-bin
        # gain modulation between frames creates audible "watery /
        # underwater" tones).  Smoothing rate is per-profile so
        # Light gets stronger smoothing (cleaner sound, less
        # processing depth) and Heavy gets lighter smoothing (faster
        # response to changing noise).
        self._prev_gain = np.ones(n_bins, dtype=np.float32)

        # Streaming state
        self._in_buf = np.zeros(0, dtype=np.float32)
        # overlap-add carry for the tail of the last frame
        self._out_carry = np.zeros(self._hop, dtype=np.float32)

        self.enabled = False
        self.profile = self.DEFAULT_PROFILE
        self._apply_profile()

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
        # Per-frame total-power list for the smart-guard variance
        # check.  Cleared at begin_noise_capture; populated during
        # capture; inspected at finalize.  Kept around afterwards so
        # the UI can re-query smart_guard_verdict() if it wants.
        self._capture_per_frame_powers: list[float] = []
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
    def set_profile(self, name: str):
        # Canonicalize legacy names ("aggressive") so saved QSettings
        # from prior Lyra versions still resolve.  Unknown names are
        # ignored (silent no-op preserves existing behavior).
        canonical = self._CANONICAL_ALIASES.get(name, name)
        if canonical in self.PROFILES:
            self.profile = canonical
            self._apply_profile()

    def reset(self):
        """Drop all streaming state — call on mode / rate / stream
        transitions so a stale overlap tail doesn't leak into new audio.

        Cancels any in-progress capture (the audio discontinuity
        that triggered reset is exactly the kind of thing that
        would corrupt a noise profile).  The captured profile
        itself — operator-locked — is preserved across reset.
        """
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_carry = np.zeros(self._hop, dtype=np.float32)
        n_bins = self._fft // 2 + 1
        self._noise_mag = np.full(n_bins, 1e-3, dtype=np.float32)
        # Re-seed _noise_mag from the first frame after reset so the
        # VAD gate has a sensible threshold relative to the new
        # mode/band's noise floor.  Without this re-seed, post-reset
        # NR would inherit the chicken-and-egg dead-tracker problem
        # the seeding was added to fix.
        self._noise_mag_seeded = False
        # Temporal gain smoother — reset to unity so the first frame
        # after a reset doesn't blend with stale state from the
        # previous mode / freq.
        self._prev_gain = np.ones(n_bins, dtype=np.float32)
        if self._capture_state == "capturing":
            self.cancel_noise_capture()

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
        # Use float64 for the accumulator — capture can run for
        # thousands of frames and float32 sums lose precision.
        # Cast back to float32 at finalize.
        self._capture_accum = np.zeros(n_bins, dtype=np.float64)
        self._capture_per_frame_powers = []
        self._capture_frames_target = frames
        self._capture_frames_done = 0
        self._capture_verdict = "n/a"
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

        Validates the array size against the current FFT bin count
        and raises ValueError on mismatch — used to flag profiles
        from a different FFT_SIZE as incompatible at load time
        rather than silently producing wrong-sized output.
        """
        n_bins = self._fft // 2 + 1
        arr = np.asarray(mag, dtype=np.float32)
        if arr.shape != (n_bins,):
            raise ValueError(
                f"captured profile size {arr.shape} does not match "
                f"current FFT bin count ({n_bins},) — "
                f"profile was likely saved with a different FFT_SIZE")
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
        """Smart-guard heuristic: inspect the per-frame total-power
        list collected during capture.  Quiet noise has stable
        frame-to-frame power; signals (CW keying, SSB syllables,
        AM modulation) drive frame power up and down sharply.

        Coefficient of variation (CV = stdev / mean) is the metric:
        - CV ≲ 0.3  → clean noise
        - CV ≳ 0.5  → suspect (signal likely present in capture)
        - in between is ambiguous; we conservatively flag as suspect
          above the threshold so the UI can warn the operator.

        Returns "n/a" if the smart-guard is disabled or no per-frame
        data exists.
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
        mean = float(np.mean(powers))
        if mean <= 0.0:
            return "n/a"
        cv = float(np.std(powers)) / mean
        return "suspect" if cv > self.GUARD_VARIANCE_THRESHOLD else "clean"

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

            # First-frame seed: real noise floors live in the
            # 0.05..0.5 magnitude range, but _noise_mag starts at
            # 1e-3.  Without seeding, the VAD-gate threshold
            # (noise_pow * vad_gate ~= 3e-6) is so far below real
            # frame power (~0.01..1.0) that the gate condition
            # NEVER fires — the tracker stays frozen at 1e-3 and
            # all profiles produce essentially identical output
            # because alpha * 1e-3 / |X| is microscopic.  Seeding
            # from the first real frame gives the tracker a
            # sensible starting point so the VAD gate works as
            # designed thereafter.  If the first frame happens to
            # contain signal (not noise-only), the seed is too
            # high but subsequent quieter frames will pass the
            # now-correctly-sized gate and pull the estimate down.
            if not self._noise_mag_seeded:
                self._noise_mag = mag.copy().astype(np.float32)
                self._noise_mag_seeded = True

            # Noise-floor tracking — VAD-gated update.  Now that the
            # tracker is properly seeded above, the gate condition
            # (frame_pow <= noise_pow * vad_gate) actually fires on
            # quiet frames and pulls _noise_mag toward real noise
            # levels.  Always runs even when the captured source is
            # active so the live estimate stays warm as a fallback
            # if the operator clears or re-toggles the captured
            # profile.
            #
            # Earlier draft of this fix added a per-bin minimum-pull
            # for "spectral valley tracking" but it caused tone
            # sidelobes (FFT leakage) to get tracked AS noise,
            # producing audible musical-noise artifacts on Light/
            # Medium profiles.  Removed.  The VAD gate alone is
            # sufficient now that seeding is in place.
            frame_pow = float(np.mean(mag * mag))
            noise_pow = float(np.mean(self._noise_mag * self._noise_mag))
            if frame_pow <= noise_pow * self._vad_gate:
                a = self._noise_track
                self._noise_mag = (1.0 - a) * self._noise_mag + a * mag

            # Choose the noise reference based on the source toggle.
            # Captured source wins when the toggle is on AND a
            # profile is loaded; otherwise fall back to the live
            # VAD-tracked estimate (both for "source = Live" and
            # for "source = Captured but no profile loaded yet").
            if (self._use_captured_profile
                    and self._captured_noise_mag is not None):
                noise_ref = self._captured_noise_mag
            else:
                noise_ref = self._noise_mag

            # Magnitude-domain subtraction with spectral floor.
            denom = np.maximum(mag, 1e-10)
            gain = np.maximum(1.0 - self._alpha * noise_ref / denom,
                              self._beta).astype(np.float32)

            # Temporal gain smoothing — eliminates the per-bin random
            # gain modulation between frames that produces audible
            # "watery / underwater" musical-noise artifacts.  Each
            # bin's gain is a weighted average with the previous
            # frame's gain for that bin.  Per-profile smoothing rate
            # (gain_smooth) sets how aggressive the smoothing is:
            # Light gets the heaviest smoothing because operators
            # picking Light expect "barely processed = clean."
            if self._gain_smooth > 0.0:
                s = self._gain_smooth
                gain = (s * self._prev_gain + (1.0 - s) * gain).astype(
                    np.float32)
            self._prev_gain = gain

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
    def _apply_profile(self):  # noqa: pre-existing name
        p = self.PROFILES[self.profile]
        self._alpha = p["alpha"]
        self._beta = p["beta"]
        self._noise_track = p["noise_track"]
        self._vad_gate = p["vad_gate"]
        # Temporal gain smoothing factor.  Defaults to 0.0 (no
        # smoothing) so any future profile variant that omits the
        # key gets backwards-compatible behavior.
        self._gain_smooth = float(p.get("gain_smooth", 0.0))

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
        self._capture_accum += mag.astype(np.float64)
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
        self._capture_verdict = self._evaluate_capture_quality()
        # Drop the accumulator; per-frame-power list is kept around
        # in case the UI re-queries the verdict, and gets reset on
        # the next begin_noise_capture() call.
        self._capture_accum = None
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
