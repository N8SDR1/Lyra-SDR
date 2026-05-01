"""DSP channel abstraction — the WDSP integration seam.

This module defines the contract between Lyra's network/protocol
layer and its DSP layer:

  ┌─────────────┐   IQ at any rate   ┌──────────────────┐  Audio @ 48k
  │ HL2Stream   │ ─────────────────▶ │   DspChannel     │ ─────────────▶
  │ (network)   │                    │  (decim+demod)   │
  └─────────────┘                    └──────────────────┘

Today the only concrete implementation is `PythonRxChannel`, which
wraps Lyra's existing scipy-based custom demods (SSB / CW / AM / DSB
/ FM). When WDSP integration lands, a future `WdspChannel(DspChannel)`
will call `wdsp.dll`'s `fexchange0()` instead — and no other code
outside this module will need to change.

Architectural intent (mirrors Thetis / WDSP RXA, 100% clean-room):
  - Channel owns ALL its DSP state internally: decimator, demods,
    audio buffer, NR, notch chain.
  - Radio configures the channel via setters; the channel never
    looks back at Radio's attributes.
  - All sample-rate matching happens INSIDE the channel — outputs
    are always 48 kHz audio regardless of input IQ rate, so the
    EP2 frame builder always sees a clean 48 kHz audio stream.
  - AGC and final volume staging live OUTSIDE the channel (in
    Radio) — those are routing-side concerns. WDSP later puts
    them in too; we'll move them when we wire WDSP.
  - Notches are inside the channel (they operate on baseband IQ
    before demod, so they have to be in this scope).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

import numpy as np

from lyra.dsp.demod import (
    SSBDemod, CWDemod, AMDemod, DSBDemod, FMDemod,
)


# ── Stateful complex-signal decimator ──────────────────────────────
#
# Used by PythonRxChannel when in_rate > audio_rate. Persistent
# filter state across blocks so back-to-back chunks don't introduce
# FIR startup transients at block boundaries.
class _Decimator:
    """Anti-aliased integer-rate complex decimator."""

    def __init__(self, rate_in: int, rate_out: int, taps: int = 257):
        from scipy.signal import firwin
        self.decim = rate_in // rate_out
        # Anti-alias cutoff at 90% of output Nyquist
        cutoff = (rate_out / 2.0) * 0.90
        self.taps = firwin(taps, cutoff, fs=rate_in, window="hann").astype(np.float64)
        self.state_i = np.zeros(taps - 1, dtype=np.float64)
        self.state_q = np.zeros(taps - 1, dtype=np.float64)
        self._phase = 0   # decimation stride offset across block boundaries

    def process(self, iq: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter
        i_out, self.state_i = lfilter(self.taps, 1.0, iq.real, zi=self.state_i)
        q_out, self.state_q = lfilter(self.taps, 1.0, iq.imag, zi=self.state_q)
        start = (-self._phase) % self.decim
        i_dec = i_out[start::self.decim]
        q_dec = q_out[start::self.decim]
        consumed_to_end = len(i_out) - start
        self._phase = (self._phase + consumed_to_end) % self.decim
        return (i_dec + 1j * q_dec).astype(np.complex64)


# ── Abstract channel ───────────────────────────────────────────────
class DspChannel(ABC):
    """Abstract RX DSP channel.

    Contract:
      - Inputs: complex64 IQ at `in_rate` Hz, arbitrary block size
      - Outputs: float32 audio at `audio_rate` Hz (default 48 kHz)
      - Stateful: maintains demod / filter / buffer state across
        process() calls. Caller must call reset() on freq change
        to avoid stale-buffer artifacts.

    All concrete implementations must satisfy the same interface so
    swapping (e.g. PythonRxChannel → WdspChannel) is transparent to
    callers.
    """

    AUDIO_RATE = 48000

    def __init__(self, in_rate: int):
        self.in_rate: int = int(in_rate)
        self.audio_rate: int = self.AUDIO_RATE

    # ── Configuration setters (called by Radio when state changes) ─

    @abstractmethod
    def set_in_rate(self, rate: int) -> None:
        """Switch the input IQ sample rate. Decimator rebuilds; audio
        buffer flushes to avoid mixed-rate samples."""

    @abstractmethod
    def set_mode(self, mode: str) -> None:
        """Select active demod (LSB/USB/CWL/CWU/AM/DSB/FM/DIGL/DIGU).
        Modes 'Off' and 'Tone' are passed through; the channel
        produces no audio for those (Radio handles tone generation)."""

    @abstractmethod
    def set_rx_bw(self, mode: str, bw_hz: int) -> None:
        """Update the filter bandwidth for `mode`. Rebuilds that
        mode's demod with the new bandwidth."""

    @abstractmethod
    def set_cw_pitch_hz(self, pitch_hz: float) -> None:
        """Update CW pitch — affects CWL/CWU demods' tone."""

    @abstractmethod
    def set_notches(self, notches: Sequence, enabled: bool) -> None:
        """Update the notch filter chain. `notches` is a sequence of
        objects with a `.filter` attribute (a NotchFilter or None) and
        a `.active` bool, matching Radio's Notch dataclass shape.
        `enabled` is the master notch-engine on/off."""

    @abstractmethod
    def set_nr_enabled(self, enabled: bool) -> None:
        """Master noise-reduction on/off."""

    @abstractmethod
    def set_nr_profile(self, profile: str) -> None:
        """Switch NR profile (light / medium / aggressive)."""

    @abstractmethod
    def set_apf_enabled(self, enabled: bool) -> None:
        """Master APF (Audio Peaking Filter) on/off. Only audible
        in CW modes — channel mode-gates internally."""

    @abstractmethod
    def set_apf_bw_hz(self, bw_hz: int) -> None:
        """APF -3 dB bandwidth in Hz. Lower = sharper peak."""

    @abstractmethod
    def set_apf_gain_db(self, gain_db: float) -> None:
        """APF peak gain in dB. Boost amount at the CW pitch."""

    @abstractmethod
    def reset(self) -> None:
        """Drop in-flight buffers + transient state. Called on
        frequency change, mode change, stream restart."""

    # ── DSP entry point ────────────────────────────────────────────

    @abstractmethod
    def process(self, iq: np.ndarray) -> np.ndarray:
        """Run the full channel: IQ in (any rate) → audio out (48 kHz).

        Returns an empty float32 array if the channel is in a no-audio
        state (mode == 'Off' / 'Tone', or insufficient samples to
        produce a complete demod block yet)."""


# ── Concrete: Lyra's native Python channel ─────────────────────────
class PythonRxChannel(DspChannel):
    """Lyra's stock RX channel built on its scipy-based custom demods.

    Owns the decimator, audio buffer, demod instances (one per mode),
    NR processor, and notch chain. Radio configures via setters and
    feeds IQ into process().

    WDSP integration path: a future WdspChannel will call into the
    DLL's fexchange0() and ignore most of this state. Both classes
    satisfy the same DspChannel ABC, so Radio doesn't care which
    one it has.
    """

    def __init__(self, in_rate: int, block_size: int = 1024):
        super().__init__(in_rate)
        self._block_size: int = int(block_size)
        self._mode: str = "USB"

        # Per-mode RX bandwidth — operator-set, persists across mode
        # switches. Matches Radio.BW_DEFAULTS so the channel produces
        # the same audio characteristics as the pre-refactor pipeline.
        self._rx_bw_by_mode: dict[str, int] = {
            "LSB":  2400, "USB":  2400,
            "CWL":  250,  "CWU":  250,
            "DSB":  5000,
            "AM":   6000,
            "FM":   10000,
            "DIGL": 3000, "DIGU": 3000,
        }
        self._cw_pitch_hz: float = 650.0

        # State that gets (re)built lazily.
        self._decimator: Optional[_Decimator] = None
        self._audio_buf: list = []
        self._demods: dict = {}
        self._rebuild_demods()

        # NR processor — owned by the channel.
        from lyra.dsp.nr import SpectralSubtractionNR
        self._nr = SpectralSubtractionNR(rate=self.audio_rate)

        # NB (Impulse Blanker) — owned by the channel.  Operates on
        # the IQ input rate (PRE-decimation) so impulses stay narrow
        # and easy to detect; bandpass filtering inside the
        # decimator would otherwise spread each impulse across many
        # output samples and make it hard to surgically blank.
        # Default profile = off; operator opts in via the DSP-row
        # NB button or Settings → Noise tab.
        from lyra.dsp.nb import ImpulseBlanker
        self._nb = ImpulseBlanker(rate=self.in_rate)

        # ANF (Auto Notch Filter, LMS adaptive) — owned by the
        # channel.  Operates on the AUDIO rate (post-demod, 48 kHz),
        # between the demodulator output and the NR processor.
        # Default profile = off; operator opts in via DSP-row ANF
        # button or Settings → Noise tab.
        from lyra.dsp.anf import AutoNotchFilter
        self._anf = AutoNotchFilter(rate=self.audio_rate)

        # LMS adaptive line enhancer (NR3-style) — predictive NR
        # complementary to NR1/NR2's subtractive approach.  Slots
        # AFTER ANF and BEFORE NR in the chain so that:
        #   - ANF kills any known stable carriers/whistles first
        #   - LMS lifts the periodic signal (CW tones, voice
        #     formants) above the broadband residual
        #   - NR cleans up whatever broadband hiss remains
        # Disabled by default; operator opts in via DSP-row LMS
        # button.  Most useful in CW mode for weak-signal work, but
        # also helps SSB clarity on noisy bands.
        from lyra.dsp.lms import LineEnhancerLMS
        self._lms = LineEnhancerLMS(rate=self.audio_rate)

        # All-mode voice-presence squelch — slots LAST in the chain
        # (after APF) so the detector sees the cleanest possible
        # audio.  Direct port from WDSP ssql.c.  Mutes the output
        # entirely when no voice is detected; opens with a smooth
        # cosine ramp when voice arrives.  Works on every mode —
        # SSB, AM, FM, CW.  Disabled by default.
        from lyra.dsp.squelch import AllModeSquelch
        self._squelch = AllModeSquelch(rate=self.audio_rate)

        # NR2 (Phase 3.D #4) — Ephraim-Malah MMSE-LSA noise reducer.
        # Lives alongside NR1 (self._nr).  Channel routes audio
        # through whichever is active based on the operator's NR
        # profile selection — see set_nr_profile() and process().
        # Both stay in memory; switching is sample-accurate (same
        # STFT framing).  Default disabled — operator opts in via
        # the "High Quality (NR2)" entry in the DSP-row right-click
        # menu.
        from lyra.dsp.nr2 import EphraimMalahNR
        self._nr2 = EphraimMalahNR(rate=self.audio_rate)
        # Tracks which NR processor process() should route through.
        # Mirror of operator's active NR profile string:
        #   "light"|"medium"|"heavy"|"neural" → use _nr (NR1)
        #   "nr2"                             → use _nr2
        # The "off" / NR-disabled state is independent of this flag
        # — it's controlled by the active NR's .enabled attribute.
        self._active_nr: str = "nr1"

        # APF (Audio Peaking Filter) — owned by the channel. Mode-
        # gated to CWU/CWL inside process(). Center freq tracks the
        # CW pitch automatically, so the operator only needs to
        # toggle it on/off and (optionally) tune BW/gain.
        from lyra.dsp.apf import AudioPeakFilter
        self._apf = AudioPeakFilter(
            sample_rate=self.audio_rate,
            center_hz=self._cw_pitch_hz,
        )

        # Notch chain — list of objects with .filter and .active
        # attrs. Channel doesn't own these (Radio's notch-management
        # state machine does); it just applies them inside process().
        self._notches: Sequence = ()
        self._notch_enabled: bool = False

    # ── Setters ────────────────────────────────────────────────────

    def set_in_rate(self, rate: int) -> None:
        rate = int(rate)
        if rate == self.in_rate:
            return
        self.in_rate = rate
        # Force decimator rebuild on next IQ block. We don't build
        # eagerly because rate may be set before the first sample
        # arrives, and we want the first build to use the rate that's
        # actually in effect when audio starts.
        self._decimator = None
        self._audio_buf.clear()
        # NB tracks the input rate (it operates pre-decimation).
        # ImpulseBlanker.set_rate is a no-op when rate is unchanged
        # and recomputes coefficients + resets state otherwise.
        self._nb.set_rate(rate)

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._audio_buf.clear()
        # Demods themselves don't change on mode switch (they're all
        # built up-front in _rebuild_demods); we just route to a
        # different one. NR state is mode-dependent in character (a
        # CW noise floor is different from AM), so flush both NR
        # processors — operator may toggle between them at any time
        # and we don't want NR2's decision-directed smoothing to
        # blend in stale spectral state from the previous mode.
        self._nr.reset()
        self._nr2.reset()
        # LMS weights/delay line are similarly mode-dependent — a
        # converged CW lock would mispredict on switching to SSB.
        self._lms.reset()
        # Squelch detector retrains for the new mode's noise floor.
        self._squelch.reset()

    def set_rx_bw(self, mode: str, bw_hz: int) -> None:
        self._rx_bw_by_mode[mode] = int(bw_hz)
        # Rebuild only the affected demod — cheaper than rebuilding all.
        self._rebuild_demods()

    def set_cw_pitch_hz(self, pitch_hz: float) -> None:
        new_pitch = float(pitch_hz)
        if new_pitch == self._cw_pitch_hz:
            return
        self._cw_pitch_hz = new_pitch
        # CW demods reference pitch; rebuild so they pick up the change.
        self._rebuild_demods()
        # APF center follows pitch automatically — that's the natural
        # operator mental model ("I tuned to the pitch, now boost
        # what I tuned to"). Coefficient swap is smooth on a
        # populated zi (low-Q peaking filter), so no click.
        self._apf.set_center_hz(new_pitch)

    def set_notches(self, notches: Sequence, enabled: bool) -> None:
        self._notches = notches
        self._notch_enabled = bool(enabled)

    def set_nr_enabled(self, enabled: bool) -> None:
        """Master enable for whichever NR is currently active.
        Both processors track the same enabled flag so switching
        the active processor preserves operator's on/off intent."""
        on = bool(enabled)
        self._nr.enabled = on
        self._nr2.enabled = on
        if not on:
            # Reset whichever was active so resuming starts clean.
            self._nr.reset()
            self._nr2.reset()

    def set_nr_profile(self, profile: str) -> None:
        """Apply an NR backend selection.

        - ``"nr1"``     → NR1 (classical spectral subtraction).
                          Strength is controlled via set_nr1_strength.
        - ``"nr2"``     → NR2 (Ephraim-Malah MMSE-LSA).
                          Strength is via set_nr2_aggression.
        - ``"neural"``  → reserved slot; falls through to NR1 for now.
        - Legacy names (light/medium/heavy/aggressive/captured) are
          accepted for QSettings backwards compat and routed to NR1
          with the appropriate strength via NR1's legacy alias map.

        Both NR1 and NR2 stay alive; the active one is selected
        by ``_active_nr`` and consumed in ``process()``.
        """
        if profile == "nr2":
            self._active_nr = "nr2"
            # Don't change NR1's strength — leave operator's last
            # NR1 setting intact so flipping back to NR1 picks up
            # right where they left off.
        else:
            self._active_nr = "nr1"
            # Legacy profile names (light/medium/heavy/aggressive)
            # still set NR1 strength via the alias map; "nr1" /
            # "neural" leave the strength alone.
            if profile in ("light", "medium", "heavy", "aggressive"):
                self._nr.set_profile(profile)

    def set_nr1_strength(self, value: float) -> None:
        """Set NR1's continuous strength (0.0..1.0).  Mirrors the
        NR2 aggression slider's API for UX consistency."""
        self._nr.set_strength(float(value))

    @property
    def nr1_strength(self) -> float:
        """Current NR1 strength (0.0..1.0)."""
        return float(self._nr.strength)

    # ── Captured noise profile API (Phase 3.D #1) ─────────────────────
    # Thin proxies onto the embedded SpectralSubtractionNR.  Channel is
    # the operator-facing layer Radio talks to; we don't want Radio
    # reaching into _nr directly.

    def begin_noise_capture(self, seconds: float = 2.0) -> None:
        """Start an N-second noise-profile capture.  See
        :meth:`SpectralSubtractionNR.begin_noise_capture` for details."""
        self._nr.begin_noise_capture(float(seconds))

    def cancel_noise_capture(self) -> None:
        self._nr.cancel_noise_capture()

    def has_captured_profile(self) -> bool:
        return self._nr.has_captured_profile()

    def captured_profile_array(self):
        """Return a copy of the active captured-noise magnitudes,
        or None if no profile is loaded.  Used by Radio's
        save_current_capture_as() to persist the latest capture."""
        return self._nr.captured_profile_array()

    def load_captured_profile(self, mag) -> None:
        """Install a captured-profile magnitudes array (loaded from
        the JSON store) into BOTH NR1 and NR2 — they share the same
        operator-facing source-toggle, so both must be primed with
        the profile or switching processors mid-session would lose
        the noise reference.  Raises ValueError on size mismatch.

        Atomic across NR1+NR2: if either load fails (e.g., bin-count
        mismatch), the other is rolled back so the operator never
        ends up in a half-loaded state where one processor has the
        new profile and the other has the old one (or none).  The
        ValueError is re-raised so the UI can surface a Save-failed
        dialog as before.
        """
        # Snapshot prior state for rollback.
        nr1_prev = self._nr.captured_profile_array()
        nr2_was_loaded = self._nr2.has_captured_profile()
        try:
            self._nr.load_captured_profile(mag)
        except Exception:
            # NR1 raised — neither processor was modified.  Re-raise.
            raise
        try:
            self._nr2.load_captured_profile(mag)
        except Exception:
            # NR2 raised after NR1 succeeded — roll NR1 back so we
            # don't leave the operator with a desynced state.
            try:
                if nr1_prev is not None:
                    self._nr.load_captured_profile(nr1_prev)
                else:
                    self._nr.clear_captured_profile()
                if not nr2_was_loaded:
                    self._nr2.clear_captured_profile()
            except Exception:
                # Rollback failed — that's worse than the original
                # error, but we've already lost; surface the original.
                pass
            raise

    def clear_captured_profile(self) -> None:
        self._nr.clear_captured_profile()
        self._nr2.clear_captured_profile()

    def set_use_captured_profile(self, on: bool) -> None:
        """Toggle the NR noise SOURCE on whichever processor is active.

        Both NR1 and NR2 honor the captured-source toggle: when True
        and a profile is loaded, the captured magnitudes (NR1) /
        magnitudes-squared (NR2 = noise PSD) drive the gain math.
        Operator-facing API doesn't care which is active — same
        toggle, same outcome.  Live tracker keeps warming up in the
        background so flipping off the toggle is glitch-free.
        """
        self._nr.set_use_captured_profile(bool(on))
        self._nr2.set_use_captured_profile(bool(on))

    def is_using_captured_source(self) -> bool:
        """True if the source toggle is on AND a profile is loaded
        (i.e. captured magnitudes are actively driving the gain
        math).  Reports for whichever processor is the active one."""
        if self._active_nr == "nr2":
            return self._nr2.is_using_captured_source()
        return self._nr.is_using_captured_source()

    # ── Noise blanker proxies (Phase 3.D #2) ──────────────────────────

    def set_nb_profile(self, profile: str) -> None:
        """Apply an NB preset: off / light / medium / aggressive /
        custom.  See ImpulseBlanker.PROFILES."""
        self._nb.set_profile(profile)

    def set_nb_threshold(self, threshold: float) -> None:
        """Operator-tunable NB threshold (Custom profile).  Multiplier
        on the background-power reference.  Clamped to NB's
        [THRESHOLD_MIN, THRESHOLD_MAX]."""
        self._nb.set_threshold(threshold)

    @property
    def nb_enabled(self) -> bool:
        return bool(self._nb.enabled)

    @property
    def nb_profile(self) -> str:
        return self._nb.profile

    @property
    def nb_threshold(self) -> float:
        return float(self._nb._threshold)

    # ── Auto Notch Filter proxies (Phase 3.D #3) ──────────────────────

    def set_anf_profile(self, profile: str) -> None:
        """Apply an ANF preset: off / gentle / standard / aggressive
        / custom.  See AutoNotchFilter.PROFILES."""
        self._anf.set_profile(profile)

    def set_anf_mu(self, mu: float) -> None:
        """Operator-tunable ANF adaptation step size (Custom profile).
        Clamped to AutoNotchFilter's [MU_MIN, MU_MAX]."""
        self._anf.set_mu(mu)

    @property
    def anf_enabled(self) -> bool:
        return bool(self._anf.enabled)

    @property
    def anf_profile(self) -> str:
        return self._anf.profile

    @property
    def anf_mu(self) -> float:
        return float(self._anf._mu)

    # ── NR2 proxies (Phase 3.D #4) ────────────────────────────────────

    def set_nr2_aggression(self, value: float) -> None:
        """Operator-tunable NR2 suppression strength (0.0..1.5).

        0.0 ≈ NR off (unity gain); 1.0 = full MMSE-LSA;
        >1.0 = power-law for harder cleanup at the cost of some
        thinning.  See AutoNotchFilter docstring for details."""
        self._nr2.set_aggression(value)

    def set_nr2_musical_noise_smoothing(self, on: bool) -> None:
        """Toggle the decision-directed ξ smoothing (α=0.98) that
        kills the musical-noise artifact.  False switches to α=0.5
        for diagnostic A/B comparison against NR1-like behavior."""
        self._nr2.set_musical_noise_smoothing(on)

    def set_nr2_speech_aware(self, on: bool) -> None:
        """Toggle the simple-VAD mode that reduces NR2 suppression
        during detected voice.  Off by default."""
        self._nr2.set_speech_aware(on)

    @property
    def nr2_aggression(self) -> float:
        return float(self._nr2.aggression)

    @property
    def nr2_musical_noise_smoothing(self) -> bool:
        return bool(self._nr2.musical_noise_smoothing)

    @property
    def nr2_speech_aware(self) -> bool:
        return bool(self._nr2.speech_aware)

    # ── LMS proxies (NR3-style line enhancer) ─────────────────────────

    def set_lms_enabled(self, on: bool) -> None:
        """Master toggle for the LMS adaptive line enhancer.  When
        on, the LMS predictor lifts periodic signal components above
        broadband noise — most effective on weak CW, also useful for
        SSB on noisy bands.  Disabled state is a single-attribute-
        check bypass; no CPU cost when off."""
        prev = self._lms.enabled
        self._lms.enabled = bool(on)
        # Fresh weights / delay line on every enable transition so a
        # stale converged state from a different signal doesn't bleed
        # in for the first ~half-second of audio.
        if not prev and self._lms.enabled:
            self._lms.reset()

    def set_lms_strength(self, value: float) -> None:
        """Strength slider, 0.0..1.0.  Mirrors NR1 / NR2 UX.  At 0.5
        the algorithm parameters land on Pratt's WDSP defaults (the
        operator-validated 'classic ANR' tuning)."""
        self._lms.set_strength(float(value))

    @property
    def lms_enabled(self) -> bool:
        return bool(self._lms.enabled)

    @property
    def lms_strength(self) -> float:
        return float(self._lms.strength)

    # ── All-mode squelch proxies ──────────────────────────────────────

    def set_squelch_enabled(self, on: bool) -> None:
        """Master toggle for the all-mode voice-presence squelch.
        When enabled, audio output is muted whenever the SSQL
        detector reports no voice / signal present.  Disabled is
        a single-attribute-check bypass."""
        prev = self._squelch.enabled
        self._squelch.enabled = bool(on)
        # Fresh detector state on every enable transition — old
        # window-detector averages from the previous band would
        # bias the threshold for a long time otherwise.
        if not prev and self._squelch.enabled:
            self._squelch.reset()

    def set_squelch_threshold(self, value: float) -> None:
        """Squelch threshold, 0.0..1.0.  See AllModeSquelch
        docstring for the operator-meaningful zones (default 0.16,
        loose ~0.10, tight ~0.30)."""
        self._squelch.set_threshold(float(value))

    @property
    def squelch_enabled(self) -> bool:
        return bool(self._squelch.enabled)

    @property
    def squelch_threshold(self) -> float:
        return float(self._squelch.threshold)

    @property
    def squelch_passing(self) -> bool:
        """True when the squelch is currently passing audio (UI
        binds this for the green/grey activity indicator)."""
        return self._squelch.is_passing()

    @property
    def active_nr(self) -> str:
        """Which NR processor is currently active.  'nr1' for the
        spectral-subtraction variants (light/medium/aggressive),
        'nr2' for the Ephraim-Malah MMSE-LSA processor.  Used by
        Radio + UI to know whether the captured-source toggle and
        NR2 knobs apply."""
        return self._active_nr

    def nr_capture_progress(self) -> tuple[str, float]:
        return self._nr.capture_progress()

    def nr_smart_guard_verdict(self) -> str:
        return self._nr.smart_guard_verdict()

    def set_nr_capture_done_callback(self, fn) -> None:
        """Register the function NR fires when a capture finalizes.
        Radio uses this to emit a Qt signal so the UI can react."""
        self._nr.set_capture_done_callback(fn)

    @property
    def nr_fft_size(self) -> int:
        """FFT size used by the embedded NR processor.  Profiles
        saved on disk store this so the manager UI can grey out
        incompatible files at load time."""
        return int(self._nr.FFT_SIZE)

    def set_apf_enabled(self, enabled: bool) -> None:
        self._apf.set_enabled(bool(enabled))

    def set_apf_bw_hz(self, bw_hz: int) -> None:
        self._apf.set_bw_hz(int(bw_hz))

    def set_apf_gain_db(self, gain_db: float) -> None:
        self._apf.set_gain_db(float(gain_db))

    def reset(self) -> None:
        """Drop in-flight buffers + transient state.

        Also force a decimator rebuild — observed in field test that
        big freq/mode jumps (e.g. AM 10 MHz WWV → DIGU 7.074 MHz FT8)
        could leave audio stuck silent until the operator cycled the
        sample rate, which is the only path that previously rebuilt
        the decimator. Rebuilding here too closes that gap; the
        per-call cost is negligible (one np.zeros(taps - 1)) and the
        first IQ block after reset transparently rebuilds the
        _Decimator instance via _decimate_to_48k."""
        self._audio_buf.clear()
        self._nr.reset()
        # NB state — bg tracker, last-clean memory, blank-run counter.
        # Same justification as the others: reset() runs on operator-
        # driven discontinuities (freq/mode change) where a fresh
        # bg tracker is appropriate.
        self._nb.reset()
        # ANF state — adaptive weights + delay line.  Tones learned
        # on the prior band are unlikely to be present on the new
        # one, so a fresh start is right.  Profile + enabled flag
        # are preserved (operator's setting sticks).
        self._anf.reset()
        # NR2 state — noise estimate per bin, prev-frame gain,
        # decision-directed memory.  Discontinuity = clean start.
        self._nr2.reset()
        # LMS state — adaptive weights + delay line ring.  Same
        # rationale as ANF: a stale converged state for a different
        # signal would mispredict on the new band/mode.
        self._lms.reset()
        # Squelch state — window-detector average and trigger
        # voltage.  A new band has a different noise floor, so the
        # SSQL detector needs to re-track.
        self._squelch.reset()
        # APF state — safe to clear here because reset() is only
        # called on freq/mode changes, where an audio discontinuity
        # is already expected.
        self._apf.reset()
        # Force decimator rebuild on next block. Cheap (one filter-
        # state allocation) and matches what set_in_rate does, which
        # is the only path the operator had previously to recover
        # from the stuck state.
        self._decimator = None

    # ── Misc accessors for Radio (read-only views into channel state) ─

    @property
    def nr_enabled(self) -> bool:
        return bool(self._nr.enabled)

    @property
    def cw_pitch_hz(self) -> float:
        return self._cw_pitch_hz

    @property
    def block_size(self) -> int:
        return self._block_size

    # ── Internals ──────────────────────────────────────────────────

    def _rebuild_demods(self) -> None:
        """Construct one demod instance per supported mode at the
        channel's audio rate. Called on init, on rx_bw change, and
        on cw_pitch change."""
        try:
            bw = self._rx_bw_by_mode
            ar = self.audio_rate
            self._demods = {
                "LSB":  SSBDemod(ar, "LSB", low_hz=300,
                                 high_hz=300 + bw.get("LSB", 2400)),
                "USB":  SSBDemod(ar, "USB", low_hz=300,
                                 high_hz=300 + bw.get("USB", 2400)),
                "CWL":  CWDemod(ar, pitch_hz=self._cw_pitch_hz,
                                bw_hz=bw.get("CWL", 250), sideband="L"),
                "CWU":  CWDemod(ar, pitch_hz=self._cw_pitch_hz,
                                bw_hz=bw.get("CWU", 250), sideband="U"),
                "DSB":  DSBDemod(ar, bw_hz=bw.get("DSB", 5000)),
                "AM":   AMDemod(ar, bw_hz=bw.get("AM", 6000) / 2),
                "FM":   FMDemod(ar, deviation_hz=5000,
                                audio_bw_hz=bw.get("FM", 10000) / 2),
                "DIGL": SSBDemod(ar, "LSB", low_hz=200,
                                 high_hz=200 + bw.get("DIGL", 3000)),
                "DIGU": SSBDemod(ar, "USB", low_hz=200,
                                 high_hz=200 + bw.get("DIGU", 3000)),
            }
        except RuntimeError as e:
            print(f"[channel] demod init failed: {e}")
            self._demods = {}

    def _decimate_to_48k(self, iq: np.ndarray) -> np.ndarray:
        if self.in_rate == self.audio_rate:
            return iq
        if self._decimator is None:
            self._decimator = _Decimator(self.in_rate, self.audio_rate)
        return self._decimator.process(iq)

    # ── Main DSP entry point ───────────────────────────────────────

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Run the full channel. Returns concatenated 48 kHz audio
        for any complete demod blocks ready in the buffer; empty
        array otherwise."""
        mode = self._mode
        if mode in ("Off", "Tone"):
            # Channel produces no audio for these — Radio handles them.
            return np.zeros(0, dtype=np.float32)

        # ── Impulse blanker (NB, Phase 3.D #2) ────────────────────
        # Runs PRE-decimation so impulses are still narrow time-
        # domain spikes that the detect-then-replace algorithm can
        # surgically blank.  Bypass-fast when NB is disabled (the
        # default).
        iq = self._nb.process(iq)

        iq_48k = self._decimate_to_48k(iq)
        if iq_48k.size == 0:
            return np.zeros(0, dtype=np.float32)

        self._audio_buf.extend(iq_48k.tolist())

        block = self._block_size
        demod = self._demods.get(mode)
        if demod is None:
            return np.zeros(0, dtype=np.float32)

        # Drain complete blocks. Each block runs through
        #   notches (baseband IQ) → demod (audio) → NR (audio)
        #                        → APF (audio, CW-only).
        # AGC + volume happen OUTSIDE the channel. APF is the last
        # in-channel audio stage — it sits before AGC so AGC chases
        # the boosted tone (which is the whole point: operator hears
        # the CW signal at AGC target, not target-minus-boost).
        is_cw = mode in ("CWU", "CWL")
        out_chunks: list[np.ndarray] = []
        while len(self._audio_buf) >= block:
            chunk = np.asarray(
                self._audio_buf[:block], dtype=np.complex64,
            )
            del self._audio_buf[:block]
            try:
                if self._notch_enabled:
                    for n in self._notches:
                        if getattr(n, "active", False) and \
                                getattr(n, "filter", None) is not None:
                            chunk = n.filter.process(chunk)
                audio = demod.process(chunk)
                # ANF (Phase 3.D #3) — LMS adaptive notch.  Slots
                # between demod and NR per the canonical ham-SDR
                # noise-toolkit chain (see docs/architecture/
                # noise_toolkit.md §6): manual notches handle KNOWN
                # carriers (already done up above when notch_enabled);
                # ANF handles UNKNOWN / drifting tones; NR handles
                # whatever broadband residual is left.
                # Bypass-fast when ANF is disabled.
                audio = self._anf.process(audio)
                # All-mode squelch — slotted BEFORE the NR stages
                # so the voice-presence detector sees audio with
                # its full noise variance.  Putting it AFTER NR
                # (where Pratt's WDSP chain has it) leaves the
                # detector looking at heavily-smoothed audio where
                # voice and noise are indistinguishable; the
                # detector then mis-classifies real signals as
                # noise and over-mutes.  When the squelch is
                # closed, downstream LMS/NR see silence and
                # consume essentially zero CPU.
                audio = self._squelch.process(audio)
                # LMS adaptive line enhancer — sits between ANF and
                # NR.  Predictive: lifts periodic content (CW,
                # voice formants) above broadband noise.  Bypass-
                # fast when disabled (single attribute check).
                audio = self._lms.process(audio)
                # Route NR through whichever processor the operator
                # selected.  Both have identical STFT framing and
                # length-preserving contracts, so switching is
                # sample-accurate.  Inactive processor's process()
                # returns input unchanged (when its .enabled is
                # False) — bypass cost is one attribute-check per
                # block.
                if self._active_nr == "nr2":
                    # Keep the Cap button working in NR2 mode: NR1
                    # owns the capture accumulator (it's the only
                    # processor with the FFT-magnitude collector +
                    # smart-guard logic), so feed it on the side
                    # whenever a capture is in progress.  Cheap when
                    # idle (single state-check + early return).
                    self._nr.feed_capture(audio)
                    audio = self._nr2.process(audio)
                else:
                    audio = self._nr.process(audio)
                # APF — only useful in CW. The operator's enable
                # state is preserved across mode switches (button
                # stays "on"), but the filter only runs when there's
                # actually CW content to boost.
                if is_cw and self._apf.enabled:
                    audio = self._apf.process(audio)
                out_chunks.append(audio)
            except Exception as e:
                print(f"[channel] demod error: {e}")
                # Don't propagate — keep the audio thread alive.
                break

        if not out_chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out_chunks)
