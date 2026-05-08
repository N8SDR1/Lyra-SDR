"""DSP channel — state container for Radio's DSP module instances.

Phase 5 (v0.0.9.6) shrunk this module dramatically.  Originally it
implemented the full RX chain in pure Python (decimator + custom
demods + NR + notches + APF + binaural + sink), wrapped by a
``DspChannel`` ABC so a future ``WdspChannel`` could swap in.

The "future WDSP path" landed on the WDSP cffi engine living
directly in ``Radio._do_demod_wdsp`` — not as a DspChannel subclass
— so the abstract façade no longer pulls its weight.  What remains
here is a thin state container that:

  * Owns the operator-state for each DSP module (NR1, NR2, NB,
    ANF, LMS, SSQL, APF).  WDSP performs the actual signal
    processing in its C-side engine; the channel mirrors operator
    knobs so saved settings persist across mode / freq changes.
    Phase 6 (v0.0.9.6) replaced the live AudioPeakFilter class
    with a dataclass state container — APF runs through WDSP's
    SPEAK biquad now.  Phases 6.B / 6.C continue the same swap
    for the remaining modules.
  * Exposes setters / getters Radio uses to mirror operator state
    (mode, rx-bw, cw-pitch, notches) onto whichever module needs
    it (e.g. APF center follows the CW pitch).
  * Holds the captured-noise capture machinery on its embedded NR1
    instance — which is the only nr.py interface still alive even
    when NR2 is the active processor (NR1 owns the FFT-magnitude
    accumulator + smart-guard logic).

The ``DspChannel`` ABC is kept for documentation / future
swap-in possibilities, but the legacy ``process()`` abstractmethod
is gone — channels no longer process IQ in this layer.

Phases 6-8 will continue trimming this file (replacing live DSP
instances with dataclasses, deleting now-orphan setters).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


# ── State containers (Phase 6) ─────────────────────────────────────
#
# These dataclass-style holders replace the legacy DSP-module
# instances (AudioPeakFilter, ImpulseBlanker, AutoNotchFilter,
# LineEnhancerLMS, AllModeSquelch, EphraimMalahNR) on the channel.
# WDSP performs the actual signal processing; what the channel needs
# is a place to mirror operator-tunable state so its setters /
# getters keep working unchanged for radio.py and the UI.
#
# Each class deliberately exposes the same surface the original DSP
# module did (same setter names, same property names, same default
# values), so callers don't change.  Phase 6 lifts the heavy DSP
# bodies out from under that surface and leaves only the state.


@dataclass
class _APFState:
    """State container for the Audio Peaking Filter — what the
    channel needs after Phase 6 deleted the live AudioPeakFilter
    class.  Defaults match the historical AudioPeakFilter defaults
    so the operator's saved settings still apply.

    Lyra's APF is now driven by WDSP's internal SPEAK biquad (see
    Radio._push_wdsp_apf_state); the dataclass below is just the
    operator-facing knob mirror.
    """
    sample_rate: int = 48000
    enabled: bool = False
    center_hz: float = 650.0
    bw_hz: int = 100
    gain_db: float = 12.0

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def set_center_hz(self, hz: float) -> None:
        self.center_hz = float(hz)

    def set_bw_hz(self, hz: int) -> None:
        self.bw_hz = int(hz)

    def set_gain_db(self, db: float) -> None:
        self.gain_db = float(db)

    def reset(self) -> None:
        # Live audio path lives in WDSP — nothing biquad-state-wise
        # to clear here.  Method exists so channel.reset() doesn't
        # need a special-case branch.
        return


# ── Abstract channel ───────────────────────────────────────────────
class DspChannel(ABC):
    """Abstract RX DSP state container.

    Phase 5 (v0.0.9.6) reduced this from a "drives the audio
    pipeline" contract to a "owns operator-mirrored DSP state"
    contract.  The actual demod path lives in
    ``Radio._do_demod_wdsp`` (the WDSP cffi engine); the channel
    holds whichever DSP modules Radio still calls into directly
    (post-AGC APF, captured-noise capture on NR1, NR profile
    state) plus operator-mirrored fields (mode, rx-bw, cw-pitch,
    notches).

    The ABC is kept for forward-compatibility — if a future DSP
    backend wants to ship its own state container shape, it can
    subclass here and Radio's setter calls Just Work.
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


# ── Concrete: Lyra's native Python channel ─────────────────────────
class PythonRxChannel(DspChannel):
    """RX state container Radio uses to mirror operator state +
    house DSP module instances.

    Live callers of the embedded DSP instances (as of v0.0.9.6
    Phase 6.A):

      * ``_apf``  → Pure state container (``_APFState`` dataclass,
                    defined above).  Operator knobs (enabled / BW /
                    gain / center) propagate to WDSP's SPEAK biquad
                    via ``Radio._push_wdsp_apf_state``.  Center
                    tracks CW pitch through ``set_cw_pitch_hz``.
      * ``_nr``   → Radio drives the captured-noise capture
                    machinery through it (see ``begin_noise_capture``
                    and friends below).  The ``feed_capture()`` API
                    on this instance is the only nr.py interface
                    still alive when NR2 is the active processor.
      * ``_nr2`` → Radio reads/writes ``gain_method`` for the NR2
                    method picker (see set_nr2_*); also receives
                    set_use_captured_profile / load_captured_profile
                    so switching NR1↔NR2 mid-session keeps the
                    same profile loaded.

    The other instances (``_nb``, ``_lms``, ``_anf``, ``_squelch``)
    are operator-state containers — Radio sets profile / strength /
    threshold / enabled on them, but their ``process()`` methods are
    no longer called.  Phase 6 replaces them with dataclasses.

    The ``block_size`` constructor parameter is accepted but ignored
    — it used to size the legacy ``_audio_buf`` drain unit.  Kept on
    the signature for one cleanup cycle so radio.py doesn't need a
    same-commit signature change; will be dropped in Phase 8.
    """

    def __init__(self, in_rate: int, block_size: int = 1024):
        super().__init__(in_rate)
        # block_size accepted but unused — see class docstring.
        del block_size
        self._mode: str = "USB"
        self._cw_pitch_hz: float = 650.0

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
        #   "nr1" → use _nr (spectral subtraction)
        #   "nr2" → use _nr2 (Ephraim-Malah MMSE-LSA / Wiener)
        # ("neural" was explored in v0.0.6 development as both
        # PyTorch/DeepFilterNet and onnxruntime/NSNet2 backends but
        # ultimately deferred until after RX2 + TX work — the menu
        # entry stays as a "planned" marker.  set_nr_profile()
        # silently routes neural-requests to nr1 in the meantime.)
        # The "off" / NR-disabled state is independent of this flag
        # — it's controlled by the active NR's .enabled attribute.
        self._active_nr: str = "nr1"

        # APF (Audio Peaking Filter) state mirror — Phase 6 swapped
        # the live AudioPeakFilter class for a state container.  The
        # operator-tunable knobs (enabled / BW / gain / center) still
        # propagate to WDSP via Radio._push_wdsp_apf_state.  Center
        # tracks the CW pitch automatically (set_cw_pitch_hz).
        self._apf = _APFState(
            sample_rate=self.audio_rate,
            center_hz=self._cw_pitch_hz,
        )

    # ── Setters ────────────────────────────────────────────────────

    def set_in_rate(self, rate: int) -> None:
        rate = int(rate)
        if rate == self.in_rate:
            return
        self.in_rate = rate
        # NB tracks the input rate via its ``set_rate`` method.
        # Even though Radio no longer feeds IQ through ``_nb.process``
        # (Phase 5 deleted that legacy path), the rate field is part
        # of the operator-mirrored state and may be reused when
        # Phase 6 swaps NB for a dataclass equivalent — keeping the
        # call here means that swap is a one-liner.
        self._nb.set_rate(rate)

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        # NR state is mode-dependent in character (a CW noise floor
        # is different from AM), so flush both NR processors —
        # operator may toggle between them at any time and we don't
        # want NR2's decision-directed smoothing to blend in stale
        # spectral state from the previous mode.  Even with NR1/NR2
        # no longer in the live audio path, the captured-profile
        # capture accumulator (which IS still live, on _nr) wants a
        # fresh noise reference per mode.
        self._nr.reset()
        self._nr2.reset()
        # LMS / Squelch state mirrors — kept fresh per mode for the
        # same reason set_in_rate keeps NB's rate field current:
        # Phase 6 dataclass swap is a one-line change if the call
        # site already exists.
        self._lms.reset()
        self._squelch.reset()

    def set_rx_bw(self, mode: str, bw_hz: int) -> None:  # noqa: ARG002
        # Phase 5: Radio is the authoritative store for per-mode
        # bandwidth (``Radio._rx_bw_by_mode``); the channel no longer
        # needs its own copy.  Setter kept on the API so radio.py's
        # call site doesn't need a same-commit signature change.
        # Phase 6 deletes this method entirely along with the orphan
        # setter sweep.
        return

    def set_cw_pitch_hz(self, pitch_hz: float) -> None:
        new_pitch = float(pitch_hz)
        if new_pitch == self._cw_pitch_hz:
            return
        self._cw_pitch_hz = new_pitch
        # APF center follows pitch automatically — that's the natural
        # operator mental model ("I tuned to the pitch, now boost
        # what I tuned to").  Phase 6.A: ``_apf`` is now an
        # ``_APFState`` dataclass; the value mirrors here and Radio's
        # ``_push_wdsp_apf_state`` propagates it to WDSP's SPEAK
        # biquad on the next push tick.
        self._apf.set_center_hz(new_pitch)

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
        - ``"nr2"``     → NR2 (Ephraim-Malah MMSE-LSA / Wiener).
                          Strength is via set_nr2_aggression.
        - ``"neural"``  → reserved.  Silently routes to NR1 until
                          we revisit AI noise filtering after RX2
                          + TX work lands.
        - Legacy names (light/medium/heavy/aggressive/captured) are
          accepted for QSettings backwards compat and routed to NR1
          with the appropriate strength via NR1's legacy alias map.

        Both NR1 and NR2 processors stay alive; the active one is
        selected by ``_active_nr`` and consumed in ``process()``.
        """
        if profile == "nr2":
            self._active_nr = "nr2"
        else:
            # nr1, neural, or legacy → all route to nr1.
            self._active_nr = "nr1"
            # Legacy profile names still set NR1 strength via the
            # alias map.
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

    def set_nr_capture_done_callback(self, fn) -> None:
        """Register the function NR fires when a capture finalizes.
        Radio uses this to emit a Qt signal so the UI can react."""
        self._nr.set_capture_done_callback(fn)

    def set_nr_staleness_callback(self, fn) -> None:
        """Register the function NR fires when the loaded captured
        profile drifts beyond the staleness threshold.  Argument is
        the smoothed drift in dB.  Radio uses this to emit a Qt
        signal so the UI can show a "recapture recommended" toast.

        See SpectralSubtractionNR.set_staleness_callback() for the
        full state-machine semantics — at most one fire per stale
        event with hysteresis-based rearm."""
        self._nr.set_staleness_callback(fn)

    def set_nr_staleness_check_enabled(self, on: bool) -> None:
        """Master toggle for the staleness check.  Default ON.
        Operator can disable via Settings -> Noise."""
        self._nr.set_staleness_check_enabled(bool(on))

    def set_nr_staleness_threshold_db(self, threshold_db: float) -> None:
        """Operator-tunable staleness fire threshold (dB).  Default
        10 dB.  Range [3.0, 25.0]; rearm held at 70% of fire.  See
        ``SpectralSubtractionNR.set_staleness_threshold_db()``.
        Added v0.0.9.5."""
        self._nr.set_staleness_threshold_db(float(threshold_db))

    def nr_staleness_drift_db(self) -> float:
        """Most recent smoothed drift between live noise and the
        loaded captured profile, in dB.  0.0 if no profile is loaded
        or no checks have run yet."""
        return self._nr.staleness_drift_db()

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
        """Reset band-specific transient state on operator-driven
        discontinuities (freq change, mode change, stream restart).

        Phase 5 (v0.0.9.6): the legacy ``_audio_buf`` and
        ``_decimator`` are gone, so this method only forwards the
        reset to the embedded DSP modules.  Of those, only ``_nr``
        and ``_apf`` see live calls in the current build:

          * ``_nr.reset()`` clears the noise estimate so a fresh
            ``feed_capture()`` round starts clean.
          * ``_apf.reset()`` clears the post-AGC peaking biquad's
            zi state so the CW tone-track has no carry-over from
            the previous band.

        The other resets (``_nb``, ``_anf``, ``_nr2``, ``_lms``,
        ``_squelch``) are state-mirror plumbing — kept here so
        Phase 6's dataclass swap is a one-line change at each call
        site, with no need to also re-thread ``reset()``.
        """
        self._nr.reset()
        # State-mirror resets (kept for Phase 6 forward-compat;
        # see method docstring).  Phase 6.A swapped ``_apf`` for a
        # dataclass; its ``reset()`` is a no-op since live filtering
        # happens inside WDSP's SPEAK biquad.
        self._nb.reset()
        self._anf.reset()
        self._nr2.reset()
        self._lms.reset()
        self._squelch.reset()
        self._apf.reset()

    # ── Misc accessors for Radio (read-only views into channel state) ─

    @property
    def nr_enabled(self) -> bool:
        return bool(self._nr.enabled)

    @property
    def cw_pitch_hz(self) -> float:
        return self._cw_pitch_hz
