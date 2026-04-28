"""Radio — central state + I/O controller for Lyra.

The single source of truth for radio state and the orchestrator of the
HL2 stream, DSP pipeline, demods, notches, and audio sink. UI panels
(and the TCI server, later) subscribe to this object's Qt signals and
call its setter methods — they never share state with each other.

This is the architectural seam: panels and controllers read FROM Radio
and push changes TO Radio. Swap the UI layout without touching any DSP
logic; add a TCI bridge by wiring another subscriber to the same signals.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from lyra.protocol.stream import HL2Stream, SAMPLE_RATES
from lyra.dsp.demod import (
    SSBDemod, CWDemod, AMDemod, DSBDemod, FMDemod, NotchFilter,
)
from lyra.dsp.audio_sink import AK4951Sink, SoundDeviceSink, NullSink
from lyra.hardware.oc import (
    N2ADR_PRESET, n2adr_pattern_for_band, format_bits,
)
from lyra.hardware.usb_bcd import (
    UsbBcdCable, bcd_for_band, Ftd2xxNotInstalled,
)
from lyra.bands import band_for_freq


class _SampleBridge(QObject):
    """Tiny helper to cross threads: RX thread -> Qt main thread."""
    samples_ready = Signal(object)


@dataclass
class Notch:
    """One manual notch in the user's notch bank.

    Width-based model (the SDR-client convention operators expect):
    operators think in absolute "kill this 100 Hz wide chunk", not
    in dimensionless Q values. Internal filter design converts
    width_hz to whatever the underlying scipy call needs.

    Flags:
    - `active`: bypass the notch in DSP without removing the
      placement. Lets you A/B whether the notch is helping. Inactive
      notches render in grey on the spectrum.
    - `deep`: cascade the IIR filter (apply twice in series), roughly
      doubling the dB attenuation at every offset. For stubborn
      carriers where a single notch leaks audibly at the edges of
      the kill region. Costs 2× CPU and 2× settling time.
    """
    abs_freq_hz: float          # absolute sky frequency of notch center
    width_hz: float             # -3 dB bandwidth in Hz
    active: bool                # individually enableable; False = bypass
    deep: bool                  # cascade for ~2× dB attenuation
    filter: NotchFilter         # the actual DSP object


class Radio(QObject):
    # ── State change signals (UI subscribes) ───────────────────────────
    stream_state_changed = Signal(bool)
    freq_changed         = Signal(int)
    rate_changed         = Signal(int)
    mode_changed         = Signal(str)
    gain_changed         = Signal(int)
    volume_changed       = Signal(float)
    af_gain_db_changed   = Signal(int)   # AF makeup gain, 0..+50 dB
    balance_changed      = Signal(float) # stereo pan, -1..0..+1
    rx_bw_changed        = Signal(str, int)       # mode, Hz
    tx_bw_changed        = Signal(str, int)
    bw_lock_changed      = Signal(bool)
    notches_changed      = Signal(list)           # list[Notch] (see dataclass above)
    notch_enabled_changed = Signal(bool)
    notch_default_width_changed = Signal(float)   # default width for new notches, in Hz
    audio_output_changed = Signal(str)
    pc_audio_device_changed = Signal(object)   # int index, or None for auto
    ip_changed           = Signal(str)

    # HL2 hardware telemetry (temperature, supply voltage, fwd/rev power).
    # Emitted at ~2 Hz from a QTimer that polls FrameStats so the UI
    # never has to touch the protocol layer directly. Values are in
    # engineering units (°C, V, W) — conversion from raw 12-bit ADC
    # counts lives in _emit_hl2_telemetry below. When the stream is
    # stopped or no telemetry has been seen yet, fields are NaN so the
    # UI can show "--" instead of a garbage zero reading.
    hl2_telemetry_changed = Signal(dict)  # {temp_c, supply_v, fwd_w, rev_w}

    # ── Streaming data signals ─────────────────────────────────────────
    spectrum_ready       = Signal(object, float, int)   # db, center_hz, rate
    smeter_level         = Signal(float)
    smeter_mode_changed  = Signal(str)                  # "peak" | "avg"
    status_message       = Signal(str, int)             # text, timeout_ms

    # ── TCI spots (DX cluster markers on the panadapter) ───────────────
    spots_changed        = Signal(list)  # list of dict(call, mode, freq_hz, color)
    spot_activated       = Signal(str, str, int)  # call, mode, freq_hz
    spot_lifetime_changed = Signal(int)   # seconds; drives age-fade on widget
    spot_mode_filter_changed = Signal(str)  # raw CSV (e.g. "FT8,CW,USB,LSB")

    # ── Visuals (spectrum / waterfall display preferences) ─────────────
    # UI-state signals broadcast from the Visuals settings tab. Radio
    # is just the central bus so any painted widget can subscribe and
    # apply the change live without the settings dialog knowing which
    # widget instances exist.
    waterfall_palette_changed  = Signal(str)           # palette name
    # Lyra constellation watermark visibility behind the panadapter
    # trace. Wired to both spectrum widget backends.
    lyra_constellation_changed = Signal(bool)
    # Occasional meteor streaks across the panadapter — separate
    # toggle from the constellation watermark so operators can run
    # one, the other, both, or neither.
    lyra_meteors_changed       = Signal(bool)
    # Panadapter grid lines (the 9×9 horiz/vert divisions). Some
    # operators rely on them for visual reference; some find them
    # noisy. Toggleable.
    spectrum_grid_changed      = Signal(bool)
    spectrum_db_range_changed  = Signal(float, float)  # (min_db, max_db)
    spectrum_cal_db_changed    = Signal(float)         # operator cal trim, dB
    smeter_cal_db_changed      = Signal(float)         # S-meter cal trim, dB
    spectrum_auto_scale_changed = Signal(bool)          # auto-fit on/off
    waterfall_auto_scale_changed = Signal(bool)         # waterfall auto-fit on/off
    waterfall_db_range_changed = Signal(float, float)  # (min_db, max_db)
    # RX filter passband (for panadapter overlay) — (low_offset_hz, high_offset_hz)
    # relative to the tuned center frequency. Recomputed whenever mode or
    # RX BW changes so the widget can draw the translucent passband rect.
    passband_changed = Signal(int, int)    # (low_offset_hz, high_offset_hz)
    cw_pitch_changed = Signal(int)         # Hz, operator-set CW tone
    # CW Zero (white) line offset from the VFO marker, in Hz.
    # Vertical reference line drawn at the filter center — i.e., where
    # a clicked CW signal lands and where the audio is generated.
    # CWU: +pitch (right of marker). CWL: -pitch (left). 0 outside CW
    # (line hidden). Emitted on mode change and pitch change.
    cw_zero_offset_changed = Signal(int)

    # Panadapter zoom + update rates
    zoom_changed                  = Signal(float)      # 1.0 = full span
    spectrum_fps_changed          = Signal(int)        # frames/sec
    waterfall_divider_changed     = Signal(int)        # push 1 row per N FFT ticks
    waterfall_multiplier_changed  = Signal(int)        # push M rows per tick (visual speedup)
    # Separate signal for waterfall so it can fire at a different rate
    # than spectrum. Shape matches spectrum_ready: (spec_db, center_hz,
    # effective_rate).
    waterfall_ready               = Signal(object, float, int)

    # Mute + Auto-LNA (levels-side automation)
    muted_changed      = Signal(bool)        # True = muted
    lna_auto_changed   = Signal(bool)        # True = auto-adjusting
    lna_auto_pullup_changed = Signal(bool)   # True = bidirectional auto
    # Emitted whenever Auto-LNA actually changes the gain (not on
    # every tick — only on real adjustments). Payload dict:
    #   delta_db    : signed dB step applied (negative for back-off)
    #   peak_dbfs   : ADC peak that triggered the adjustment
    #   new_gain_db : the LNA value AFTER the adjustment
    #   when_local  : "HH:MM:SS" string for the UI badge
    # The UI uses this to flash the slider + show a "last event"
    # badge so operators can SEE Auto working in real time.
    lna_auto_event     = Signal(dict)
    lna_peak_dbfs      = Signal(float)       # live ADC peak, for UI readout
    lna_rms_dbfs       = Signal(float)       # live ADC RMS, companion to peak

    # Noise Reduction (NR) — classical spectral subtraction backend.
    # Profile name ∈ {"light","medium","aggressive","neural"}; "neural"
    # is a placeholder reserved for future RNNoise / DeepFilterNet
    # integration, greyed out in the UI until a suitable package is
    # importable.
    nr_enabled_changed = Signal(bool)
    nr_profile_changed = Signal(str)

    # Panadapter noise-floor estimate — 20th percentile of the current
    # spectrum, rolling-averaged. Emitted at ~6 Hz (not every FFT tick)
    # so the widget's horizontal reference line doesn't twitch.
    noise_floor_changed = Signal(float)   # dBFS

    # Band plan / region — drives the panadapter sub-band strip +
    # landmark markers + out-of-band warnings. "NONE" disables the
    # whole feature (HL2 hardware remains unlocked either way).
    band_plan_region_changed = Signal(str)
    band_plan_show_segments_changed = Signal(bool)
    band_plan_show_landmarks_changed = Signal(bool)
    band_plan_edge_warn_changed      = Signal(bool)

    # Peak-markers — a persistent "peak hold" overlay drawn only within
    # the RX passband. Bounded display + user-toggleable so it stays
    # diagnostic rather than visual clutter.
    peak_markers_enabled_changed = Signal(bool)
    peak_markers_decay_changed   = Signal(float)   # dB / second
    peak_markers_style_changed   = Signal(str)     # "line"/"dots"/"triangles"
    peak_markers_show_db_changed = Signal(bool)    # show numeric dB at peaks

    # User-picked colors — spectrum trace + per-segment band-plan
    # fills. Stored as #RRGGBB hex strings for simple QSettings
    # round-trip. Empty string = use the built-in default color.
    spectrum_trace_color_changed = Signal(str)
    segment_colors_changed       = Signal(dict)    # {kind: hex, ...}
    noise_floor_color_changed    = Signal(str)    # NF line color hex
    peak_markers_color_changed   = Signal(str)    # peak marker color hex

    # ── DSP profile signals ────────────────────────────────────────────
    agc_profile_changed  = Signal(str)    # off / fast / med / slow / auto / custom
    agc_action_db        = Signal(float)  # live gain reduction, dB
    agc_threshold_changed = Signal(float) # current threshold (target), dBFS-ish

    # AGC presets (industry-standard). Attack is always instant.
    # "auto" uses a medium release/hang and additionally tracks the noise
    # floor continuously (auto_set_agc_threshold every AGC_AUTO_INTERVAL_MS)
    # so the threshold follows band conditions without user intervention.
    # AGC release coefficient is applied per audio block (~43 ms at
    # 48 kHz / 2048 samples). Time constant τ for peak decay is
    # τ ≈ -43 ms / ln(1 - release). Values calibrated against
    # Thetis 2.10.3.13 (WDSP wcpAGC) reference profiles:
    #   Thetis FAST:  hang  0 ms, decay  50 ms
    #   Thetis MED :  hang  0 ms, decay 250 ms
    #   Thetis SLOW:  hang 1000 ms, decay 500 ms
    # The original Lyra values had release coefficients ~20-30×
    # too slow (Fast τ was 2.1 s, Slow τ was 43 s), which made
    # audio stay clamped for many seconds after a peak — exact
    # symptom: "audio doesn't come back up to audible after a
    # strong signal." Hang on Fast/Med is now ZERO (matches
    # Thetis); recovery starts on the very first block after
    # the peak passes. Slow keeps a 1 s hang for steady-carrier
    # listening (AM broadcast, DX nets).
    AGC_PRESETS: dict[str, dict] = {
        "off":    {"release": 0.0,   "hang_blocks":  0},   # disabled
        # Fast was originally tuned to Thetis values (0 hang, 50 ms
        # decay), but Lyra's AGC is a simpler peak-tracker than
        # Thetis/WDSP (no attack-envelope buffer, no hang-index
        # threshold curve). Without those secondary smoothing
        # stages, 0/50ms produced audible pumping on AM voice
        # envelopes — "wavey in/out rapid audio." Adding a small
        # hang (~130 ms) lets brief envelope dips ride through
        # while still recovering quickly between CW dits or
        # discrete signal events.
        "fast":   {"release": 0.30,  "hang_blocks":  3},   # τ≈120 ms, hang 130 ms
        "med":    {"release": 0.158, "hang_blocks":  0},   # τ≈250 ms
        "slow":   {"release": 0.083, "hang_blocks": 23},   # τ≈500 ms, hang 1 s
        "auto":   {"release": 0.158, "hang_blocks":  0},   # med + track
    }
    AGC_AUTO_INTERVAL_MS = 3000   # re-track threshold every 3 s in auto mode

    # ── External filter board (N2ADR etc.) ─────────────────────────────
    oc_bits_changed      = Signal(int, str)     # raw_bits, human-readable
    filter_board_changed = Signal(bool)         # enabled/disabled

    # ── USB-BCD cable for external linear amplifier band switching ────
    bcd_value_changed    = Signal(int, str)     # bcd_byte, band_name
    usb_bcd_changed      = Signal(bool)         # enabled/disabled

    # Modes match HPSDR standard DSPMode set (practical subset — SAM/DRM/AM_LSB/
    # AM_USB are in backlog). Each mode has its own bandwidth preset list.
    ALL_MODES = ["LSB", "USB", "CWL", "CWU", "DSB", "FM", "AM",
                 "DIGU", "DIGL", "Tone", "Off"]

    SSB_BW = [1500, 1800, 2100, 2400, 2700, 3000, 3600, 4000, 6000, 8000]
    CW_BW  = [50, 100, 150, 250, 400, 500, 750, 1000]
    AM_BW  = [3000, 4000, 6000, 8000, 10000, 12000]
    DSB_BW = [3000, 4000, 5000, 6000, 8000, 10000]
    FM_BW  = [6000, 8000, 10000, 12000, 15000]
    DIG_BW = [1500, 2400, 3000, 3600, 4000, 6000]

    BW_PRESETS = {
        "LSB":  SSB_BW,  "USB":  SSB_BW,
        "CWL":  CW_BW,   "CWU":  CW_BW,
        "DSB":  DSB_BW,
        "AM":   AM_BW,
        "FM":   FM_BW,
        "DIGL": DIG_BW,  "DIGU": DIG_BW,
    }
    BW_DEFAULTS = {
        "LSB": 2400,  "USB": 2400,
        "CWL": 250,   "CWU": 250,
        "DSB": 5000,
        "AM":  6000,
        "FM":  10000,
        "DIGL": 3000, "DIGU": 3000,
    }

    def __init__(self):
        super().__init__()

        # ── Persistent-ish state ──────────────────────────────────────
        self._ip = "10.10.30.100"
        self._freq_hz = 7074000
        self._rate = 48000
        self._mode = "USB"
        self._gain_db = 19
        # CW pitch (Hz) — operator-adjustable via Settings → DSP.
        # Drives both the CWDemod tone position AND the panadapter
        # passband overlay AND the click-to-tune CW correction so
        # all three stay in sync. Persisted to QSettings; defaults
        # to 650 Hz (matches the legacy hardcoded value most ham
        # SDR clients use). Typical operator range 400-800 Hz;
        # individual preference often driven by hearing comfort.
        from PySide6.QtCore import QSettings as _QS
        try:
            saved_pitch = int(_QS("N8SDR", "Lyra").value(
                "dsp/cw_pitch_hz", 650))
            self._cw_pitch_hz = max(200, min(1500, saved_pitch))
        except (TypeError, ValueError):
            self._cw_pitch_hz = 650
        # Volume chain — TWO stages since 2026-04-24:
        #   AF Gain (af_gain_db): makeup gain in dB, for cases where
        #     AGC is off (digital modes like FT8 run AGC off to avoid
        #     pumping) or AGC target is low relative to the weak-
        #     signal demod output. Set once per station/band, forget.
        #     Range 0..+50 dB.
        #   Volume: final output trim, 0..1.0 multiplier driven by a
        #     perceptual-curve slider 0..100%. Ride this for moment-
        #     to-moment loudness comfort.
        # Chain: demod → AGC (if on) → AF Gain → Volume → tanh → sink
        # Default +25 dB: with AGC OFF (digital-mode operating, or
        # operator preference), AF Gain is the ONLY source of
        # makeup gain — AGC's 30-60 dB of automatic amplification
        # is not contributing. Default 0 left fresh installs
        # silent on AGC-off until the operator discovered the AF
        # Gain slider, which is operator-hostile. +25 dB lands
        # most antennas in the audible zone with the Volume slider
        # at 80% and AGC off; AGC-on path is unaffected (AGC
        # normalizes to target regardless of pre-AGC gain). Saved
        # QSettings values still override on subsequent launches.
        self._af_gain_db = 25                   # integer dB, 0..+50
        # Stereo balance / pan for RX1.
        # Range: -1.0 (full left) .. 0.0 (center) .. +1.0 (full right)
        #
        # Equal-power pan law (cos/sin) applied in the sink-write
        # path so total energy stays constant as the operator pans
        # across center. Useful for:
        #   - DX-split listening: pan RX1 left, route DX-spot RX2
        #     hard right (when RX2 ships) — DX in one ear, pile-up
        #     in the other.
        #   - A/B-ing against a noise source localized to one
        #     channel.
        #
        # FUTURE — when RX2 + Split arrive:
        #   * Add _balance_rx2 (independent pan for second receiver)
        #   * Add _stereo_routing_mode enum: Mono / SplitLR / SplitRL
        #   * Audio mix becomes:
        #       L_out = RX1_audio * RX1_L_gain + RX2_audio * RX2_L_gain
        #       R_out = RX1_audio * RX1_R_gain + RX2_audio * RX2_R_gain
        #     done either in Radio (preferred — sink stays dumb) or
        #     in a future stereo-aware sink layer.
        # Today the sink does the pan since there's only one source
        # (RX1). The set_lr_gains hook on each sink already exists
        # so we can drop in the multi-source mixer without changing
        # sink internals.
        self._balance = 0.0
        self._volume = 0.5                      # 50% = ~-12 dB trim
        self._muted = False
        # Auto-LNA loop: periodically adjust _gain_db to keep the ADC
        # peak near a target headroom. Engaged only when the operator
        # enables it; manual LNA is the default.
        self._lna_auto = False
        self._lna_auto_target_dbfs = -15.0  # headroom target
        self._lna_auto_max_step_db = 3       # clamp per-step change
        self._lna_auto_hysteresis_db = 3.0   # deadband around target
        # Rolling peak history, updated from the sample stream. 90th
        # percentile over this window drives the control loop (ignores
        # brief transient spikes).
        self._lna_peaks: list[float] = []
        self._lna_rms: list[float] = []      # parallel to _lna_peaks
        self._lna_peaks_max = 120
        self._lna_current_peak_dbfs = -120.0
        # Auto-LNA pull-up — opt-in bidirectional Auto-LNA. When True,
        # the auto loop ALSO raises gain on sustained quiet bands, in
        # addition to the always-on overload-protection back-off.
        # Default OFF; v1 of upward-chasing Auto-LNA caused IMD on
        # 40 m at +44 dB so this stays opt-in until field-tested. See
        # _adjust_lna_auto for the conservative climb logic.
        self._lna_auto_pullup = False
        # Monotonic timestamp of the last MANUAL gain change. Auto
        # pull-up defers to the user — no auto-raise within
        # LNA_AUTO_PULLUP_DEFER_S seconds of a slider/scroll change.
        self._lna_last_user_change_ts = 0.0
        # Re-entrancy flag — auto-driven set_gain_db calls don't bump
        # _lna_last_user_change_ts (otherwise the auto loop would
        # forever defer to itself).
        self._lna_in_auto_adjust = False
        # Sustained-quiet streak counter for pull-up hysteresis.
        # Pull-up only fires after LNA_AUTO_PULLUP_QUIET_TICKS
        # consecutive ticks of "quiet" — keeps the loop from chasing
        # gaps between band activity.
        self._lna_pullup_quiet_streak = 0
        # Passband peak in dBFS — captured by the FFT loop so the
        # pull-up gate can reject "strong narrowband signal in your
        # filter" cases that full-IQ peak/RMS doesn't see (e.g. WWV
        # at 10 MHz in a 192 kHz IQ stream — 1.6% of the band).
        # None means "FFT hasn't run yet"; treat as no information.
        self._lna_passband_peak_dbfs: float | None = None
        self._rx_bw_by_mode = dict(self.BW_DEFAULTS)
        self._tx_bw_by_mode = dict(self.BW_DEFAULTS)
        self._bw_locked = False
        self._audio_output = "AK4951"
        # Optional explicit PortAudio device index for the PC Soundcard
        # sink. None means "auto-pick" (prefers WASAPI default — see
        # SoundDeviceSink). Operators can override via Settings →
        # Audio → Output device. Persisted by app.py QSettings.
        self._pc_audio_device_index: Optional[int] = None

        # ── Config register (C0=0x00) — composed full ──────────────────
        # C1: sample rate bits[1:0]
        # C2: OC-output pattern bits[7:1] + CW-eer bit[0]
        # C3: preamp / ADC config (unused for now)
        # C4: duplex bit[2] + NDDC bits[5:3] + antenna selection
        # Keep composed so any single-bit change can recompose + resend.
        self._config_c1 = SAMPLE_RATES[self._rate]
        self._config_c2 = 0x00
        self._config_c3 = 0x00
        self._config_c4 = 0x04   # duplex=1, NDDC=1 (required for RX)
        self._keepalive_cc: tuple[int, int, int, int, int] = (
            0x00, self._config_c1, self._config_c2,
            self._config_c3, self._config_c4,
        )

        # Per-band memory — last freq/mode/gain when each band was active.
        # Keyed by Band.name (e.g., "40m"). Populated as the operator
        # tunes; recall_band(name) restores the saved state. Persists
        # across launches via QSettings.
        self._band_memory: dict[str, dict] = {}
        self._suppress_band_save = False  # set during recall to avoid loop

        # External filter board (N2ADR or compatible)
        self._filter_board_enabled = False
        self._oc_preset: dict[str, tuple[int, int]] = dict(N2ADR_PRESET)
        self._oc_bits_current = 0

        # USB-BCD cable for external linear amplifier band-switching
        self._usb_bcd_enabled = False
        self._usb_bcd_serial: str = ""
        self._usb_bcd_cable: Optional[UsbBcdCable] = None
        self._usb_bcd_value = 0
        self._bcd_60m_as_40m = True   # most amps share 40m filter for 60m

        # ── Runtime ───────────────────────────────────────────────────
        self._stream: Optional[HL2Stream] = None
        self._audio_sink = NullSink()
        self._audio_block = 2048
        self._tone_phase = 0.0

        # RX DSP channel — the WDSP integration seam. Owns its own
        # decimator, audio buffer, demod instances, NR, and notch
        # chain; Radio configures via setters and feeds IQ into
        # process(). See lyra/dsp/channel.py for the full contract.
        from lyra.dsp.channel import PythonRxChannel
        self._rx_channel: PythonRxChannel = PythonRxChannel(
            in_rate=self._rate, block_size=self._audio_block,
        )

        # AGC: peak-track with hang time. Profile presets select
        # (release rate, hang blocks); Custom exposes the parameters
        # directly. "off" disables AGC entirely — volume scales the
        # raw demod output.
        self._agc_peak = 0.01
        # AGC target 0.0316 linear = -30 dBFS peak. Progression:
        #   0.3  (-10 dBFS)  pre-AF-Gain-split — too hot, AGC had to
        #                    do all the work, stacked with AF caused
        #                    clipping/tanh saturation
        #   0.1  (-20 dBFS)  AF-split era — still too hot, on/off
        #                    delta was ~17 dB (noticeable)
        #   0.0316(-30 dBFS) current — matches the typical reference-
        #                    client target; AGC does less aggressive
        #                    work, preserves dynamic range better,
        #                    on/off delta drops to ~8-10 dB (the
        #                    "slight feel" operators expect)
        # Trade-off: requires slightly higher Vol slider for same
        # loudness, but the user gains more expressive dynamic range
        # on signals and much less AGC pumping on digital modes.
        self._agc_target = 0.0316
        self._agc_profile = "med"        # off / fast / med / slow / custom
        self._agc_release = 0.003
        self._agc_hang_blocks = 23
        self._agc_hang_counter = 0
        # Rolling noise-floor estimate — lowest block peak over the
        # recent window. Used by "Auto Threshold" to calibrate the
        # AGC target above ambient noise (like the right-click →
        # "automatic AGC threshold" option).
        self._noise_baseline = 0.01
        self._noise_history: list[float] = []
        self._noise_history_max = 70     # ~3 seconds at 43 ms/block
        self._apply_agc_preset(self._agc_profile)

        # Auto-tracking timer: only runs while profile == "auto". Owned by
        # Radio (not UI) so tracking continues even if the panel is hidden.
        from PySide6.QtCore import QTimer as _QTimer
        self._agc_auto_timer = _QTimer(self)
        self._agc_auto_timer.setInterval(self.AGC_AUTO_INTERVAL_MS)
        self._agc_auto_timer.timeout.connect(self.auto_set_agc_threshold)

        # Auto-LNA control loop — 500 ms cadence. Originally 1.5 s,
        # bumped down to make pull-up feel responsive when tuning to
        # a weak signal at low LNA. Streak gate (multiple consecutive
        # ticks of "quiet") still filters transients so back-off
        # doesn't get jumpy. With 500 ms / FAR-tier 1-tick / +2 dB
        # step, a -6 → +15 climb is ~12 s instead of the original
        # ~40 s.
        self._lna_auto_timer = _QTimer(self)
        self._lna_auto_timer.setInterval(500)
        self._lna_auto_timer.timeout.connect(self._adjust_lna_auto)

        # ADC peak reporter — emits lna_peak_dbfs at ~4 Hz so the UI
        # can show a live dBFS indicator regardless of whether Auto-
        # LNA is engaged. Operator uses this to diagnose RF-chain
        # health: clipping, too hot, sweet spot, or too cold.
        self._peak_report_timer = _QTimer(self)
        self._peak_report_timer.setInterval(250)
        self._peak_report_timer.timeout.connect(self._emit_peak_reading)
        # Started when stream starts, stopped when stream stops.

        # HL2 telemetry poll — reads the most recent raw ADC counts off
        # the stream's FrameStats and emits engineering-unit values
        # (°C, V, W) at 2 Hz. Slow on purpose: temp + supply don't
        # change fast, and a faster cadence would just flicker labels.
        self._hl2_telem_timer = _QTimer(self)
        self._hl2_telem_timer.setInterval(500)
        self._hl2_telem_timer.timeout.connect(self._emit_hl2_telemetry)
        # Started/stopped alongside the stream so we don't churn signals
        # with stale ADC counts when nothing is connected.

        # Notch bank — list of Notch dataclasses (see top of file).
        # Operators add/remove via right-click on spectrum/waterfall;
        # each notch carries its own width and active flag. Default
        # width 80 Hz comfortably covers FT8 (47 Hz spread) on first
        # placement; operator can adjust per-notch via wheel/drag.
        self._notches: list[Notch] = []
        self._notch_enabled = False
        # Notch defaults — tuned for a "kills the carrier" feel rather
        # than the previous 80 Hz / single-biquad which left audible
        # leak at the edges of the kill region.
        # 40 Hz width: at typical heterodyne center frequencies (1-3 kHz)
        # this gives Q ~ 25-75 — narrow enough to surgically remove a
        # whistle without taking out adjacent voice content.
        # Deep cascade: doubles dB attenuation at every offset (~30-40 dB
        # → 60-80 dB at center). Operator can untoggle deep per-notch
        # via right-click → "Deep" if they want a softer kill.
        self._notch_default_width_hz = 40.0
        self._notch_default_deep = True

        # TCI spots — keyed by callsign, capped size, oldest-first eviction.
        self._spots: dict[str, dict] = {}   # call -> {call, mode, freq_hz, color, ts}
        # Kept small on purpose — FT8/FT4 pile up dense spot clusters.
        # Settings → Network/TCI lets the user override (cap 100).
        self._max_spots = 30
        self._spot_lifetime_s = 600  # 10 min; 0 = never expire
        # Mode-filter for spot rendering — same idiom as SDRLogger+:
        # comma-separated list of modes to show (case-insensitive).
        # Empty string = no filter, show every spot. "SSB" auto-expands
        # to SSB/USB/LSB since cluster spots are almost always tagged
        # as USB or LSB rather than the generic "SSB".
        self._spot_mode_filter_csv = ""

        # Visuals — dB-range defaults are set for the post-cal-fix
        # spectrum (true dBFS, where a unit-amplitude tone reads
        # 0 dBFS and the noise floor on a quiet band lands around
        # -130 dBFS). Old-scale saved settings (min > -90) get
        # auto-shifted by the SPECTRUM_OLD_SCALE_DB_SHIFT migration
        # in app.py:_load_settings so existing users see continuity.
        self._waterfall_palette = "Classic"
        # Panadapter Lyra watermark — stylized lyre/constellation
        # image rendered with additive blending behind the spectrum
        # trace. Operator-toggleable in Settings → Visuals; persisted
        # to QSettings. Default ON since it's part of the brand
        # identity. Loaded value (if any) is restored in app.py.
        self._show_lyra_constellation = True
        # Occasional meteors — opt-in flair, off by default. Spawn
        # gap 15..50 s, max 1 visible at a time. Independent of the
        # constellation watermark.
        self._show_lyra_meteors = False
        # Panadapter grid lines (9×9 dotted divisions). Default ON
        # since most operators expect a reference grid; turn off for
        # a cleaner trace-only view. Persisted via QSettings.
        self._show_spectrum_grid = True
        self._spectrum_min_db   = -140.0
        self._spectrum_max_db   = -50.0
        # Operator-set BOUNDS for the spectrum range. Auto-scale is
        # allowed to move the live display range (`_spectrum_min/max_db`
        # above) within these bounds, but never outside. Set by any
        # `set_spectrum_db_range(from_user=True)` call (Y-axis drag,
        # Settings sliders, etc.). Defaults match the live range so
        # the bounds are inert until the operator intentionally
        # narrows them.
        self._user_range_min_db = self._spectrum_min_db
        self._user_range_max_db = self._spectrum_max_db
        # Auto-fit the dB scale to current band conditions when on.
        # Engineering: every AUTO_SCALE_INTERVAL_TICKS, recompute
        # (noise_floor - 15) .. (peak + 15), CLAMP to user range,
        # and call set_spectrum_db_range. Auto-scale is ONLY toggled
        # by the explicit checkbox — manual range changes update the
        # bounds but no longer flip the auto flag (operator request).
        self._spectrum_auto_scale = False
        self._auto_scale_tick_counter = 0
        # Rolling-max history of FFT-frame peaks. Filled per-tick in
        # _tick_fft when auto-scale is enabled; used to set the high
        # end of the dB range so transient spikes don't overshoot the
        # display the way a single-frame max does.
        self._auto_scale_peak_history: list[float] = []
        self._waterfall_min_db  = -140.0
        self._waterfall_max_db  = -60.0
        # Operator preference — when True (default) the waterfall's
        # dB range tracks the spectrum auto-scale on each tick. When
        # False the waterfall stays at whatever min/max the operator
        # set in Settings → Visuals, regardless of band activity. Some
        # operators prefer a fixed darker waterfall so weaker signals
        # 'pop' against a near-black background; this gives them that.
        self._waterfall_auto_scale = True
        # Zoom (panadapter scaling). 1.0 = full sample-rate span;
        # higher values crop to centered bins and report a reduced
        # rate so SpectrumWidget + WaterfallWidget auto-scale their
        # frequency axis.
        self._zoom = 1.0
        # FFT tick interval + waterfall push divider. The waterfall
        # divider lets the operator slow the scrolling heatmap without
        # affecting spectrum refresh rate (e.g. 3x divider = waterfall
        # scrolls at 10 rows/sec while spectrum stays at 30 fps).
        self._fft_interval_ms = 33   # ~30 Hz
        self._waterfall_divider = 1
        self._waterfall_tick_counter = 0
        # Multiplier lets the waterfall scroll FASTER than the FFT tick
        # rate by emitting the same spectrum row multiple times per
        # tick. With M=3 + divider=1 + 30 fps, the waterfall scrolls
        # at 90 rows/sec (3x). Rows are duplicates of the latest FFT —
        # no extra signal information, just faster visual scroll, which
        # is exactly what the operator wants when a slow-moving mode
        # like JS8 or WSPR would otherwise take forever to fill the
        # pane.
        self._waterfall_multiplier = 1
        # Panadapter noise-floor marker (toggleable, default on).
        # Rolling 30-frame window of 20th-percentile dB values; a simple
        # EMA on top of that yields a steady reference line. Emission is
        # throttled via _nf_emit_counter below.
        self._noise_floor_enabled = True
        self._noise_floor_history: list[float] = []
        self._noise_floor_history_max = 30
        self._noise_floor_db: float | None = None
        self._nf_emit_counter = 0

        # Band plan — per-region allocations drive the panadapter strip
        # at the top (colored sub-bands) and the landmark ticks (FT8,
        # FT4, WSPR watering holes). HL2 hardware stays unlocked; this
        # is purely an advisory / navigational overlay.
        from lyra.band_plan import DEFAULT_REGION
        self._band_plan_region = DEFAULT_REGION
        self._band_plan_show_segments = True
        self._band_plan_show_landmarks = True
        self._band_plan_edge_warn = True
        # Remember the last in-band state so we only toast on edge
        # transitions, not every frequency-change tick.
        self._last_in_band: bool = True

        # Peak-markers: in-passband peak-hold trace with linear decay.
        # The decay rate is in dB/sec — at 10 dB/s a peak 30 dB above
        # the noise floor fades away in 3 seconds.
        self._peak_markers_enabled = False
        self._peak_markers_decay_dbps = 10.0
        self._peak_markers_style = "dots"        # "line" / "dots" / "triangles"
        self._peak_markers_show_db = False       # show numeric dB at top peaks

        # User-picked colors. Empty string means "use the hardcoded
        # default" so the UI can reset by clearing. Segment overrides
        # apply on top of band_plan.SEGMENT_COLORS.
        self._spectrum_trace_color: str = ""    # e.g. "#5ec8ff"
        self._segment_colors: dict[str, str] = {}  # kind → hex override
        self._noise_floor_color: str = ""       # NF line color override
        self._peak_markers_color: str = ""      # peak marker color override

        # ── Noise Reduction ───────────────────────────────────────────
        # NR processor is owned by self._rx_channel (see lyra/dsp/channel.py).
        # Radio just exposes the operator-facing on/off + profile via
        # the channel's setters. Neural NR (RNNoise / DeepFilterNet)
        # will hook in via the channel's NR pipeline when added.
        # Keep `_nr_profile` separate from the processor's internal
        # value so the UI can expose a "neural" placeholder even when
        # the processor itself only supports the classical profiles.
        from lyra.dsp.nr import SpectralSubtractionNR as _SSNR
        self._nr_profile = _SSNR.DEFAULT_PROFILE

        # ── FFT ring buffer ───────────────────────────────────────────
        self._fft_size = 4096
        self._window = np.hanning(self._fft_size).astype(np.float32)
        # True-dBFS normalization for a windowed FFT. For a windowed
        # complex sinusoid of unit amplitude the FFT bin magnitude is
        # `N * mean(window)` (the window's coherent gain). Squaring
        # that gives the power-spectrum normalization that makes a
        # full-scale tone read exactly 0 dBFS:
        #
        #   spec_dBFS = 10 · log10( |X[k]|² / (N · mean(w))² )
        #
        # Old normalization (sum of squared window samples) gave a
        # PSD-style scale that ran ~34 dB hot relative to dBFS — the
        # noise floor sat at -100ish when it should have been at
        # -134ish for true dBFS. This is the "cal offset" cleanup.
        self._win_coherent_gain = float(np.mean(self._window))   # ≈ 0.5 for Hanning
        self._win_norm = (self._fft_size * self._win_coherent_gain) ** 2
        # Operator-adjustable cal trim, in dB. Added to every
        # spec_db sample so the operator can compensate for per-rig
        # losses (preselector loss, antenna efficiency, internal
        # cable loss, cal against a known signal generator, etc.).
        # Default 0 = pure theoretical dBFS based on the math above.
        # Settings → Visuals exposes a slider; persisted to QSettings.
        self._spectrum_cal_db = 0.0
        # Independent S-meter cal trim. Applied ONLY to the
        # smeter_level signal (so the meter dBm reading shifts), NOT
        # to the spectrum display itself. This lets the operator
        # calibrate the S-meter against a known reference (e.g. a
        # signal generator at -73 dBm = S9) without re-shifting the
        # whole panadapter scale. Settable via Settings → Visuals →
        # "S-meter cal" or by right-click on the meter →
        # "Calibrate to current = …".
        #
        # Default +28.0 dB: empirically derived on N8SDR's HL2+
        # against Thetis 2.10.3.13 with WWV @ 10 MHz AM 8K and 40 m
        # noise floor as references. Math chain:
        #   - HL2 IQ stream is dBFS relative to ADC full-scale
        #   - Lyra integrates passband power (np.sum of linear bins)
        #   - LNA-invariant: meter formula subtracts current
        #     self._gain_db so reading reflects antenna dBm, not
        #     ADC-port dBm. Calibrate once, holds across LNA moves.
        #   - +28 dB shifts the result onto a typical dBm scale.
        #     Came from +21 (pre-LNA-invariant cal at LNA=+7) plus
        #     7 (the LNA value at cal time) — the +7 used to be
        #     baked into the reading by the LNA-dependent old
        #     formula and now has to live in the cal constant.
        # Operators on different rigs/antennas/RF environments can
        # nudge this via the right-click "Calibrate to specific dBm"
        # option; their value is saved in QSettings and overrides
        # this default on subsequent launches.
        self._smeter_cal_db = 28.0

        # S-meter response mode — "peak" (default, instant max bin in
        # the passband) or "avg" (time-smoothed mean of passband bins,
        # in linear-power, then back to dB).
        # Peak is responsive but jumpy on transients (CW dits, FT8
        # tones, lightning crashes). Average is steadier and more
        # representative of the actual signal level the AGC sees —
        # useful for setting AF gain or comparing band noise levels.
        # Operator switches via right-click on the meter face.
        self._smeter_mode = "peak"
        # Time-smoothing for average mode — exponential moving average
        # of recent linear-power readings. Tau ~0.5 s feels natural
        # (long enough to smooth out jitter, short enough to track
        # band changes within a fade).
        self._smeter_avg_lin = 0.0    # linear power running average
        self._sample_ring: deque = deque(maxlen=self._fft_size * 4)
        self._ring_lock = threading.Lock()

        # ── Demods ─────────────────────────────────────────────────────
        # Demod instances live inside self._rx_channel (one per mode,
        # built from the channel's _rx_bw_by_mode + _cw_pitch_hz).
        # Sync the channel's per-mode BW with Radio's current values
        # so the UI's saved BW state takes effect immediately.
        for _m, _bw in self._rx_bw_by_mode.items():
            self._rx_channel.set_rx_bw(_m, int(_bw))
        self._rx_channel.set_cw_pitch_hz(float(self._cw_pitch_hz))
        self._rx_channel.set_mode(self._mode)

        # ── Thread bridge ─────────────────────────────────────────────
        # Batch samples in the RX thread before bridging to reduce Qt
        # event-loop pressure (was emitting at ~381 Hz; now ~23 Hz at 48k).
        # Reduces audio pops caused by main-thread paint blocking.
        self._rx_batch: list = []
        self._rx_batch_size = 2048
        self._rx_batch_lock = threading.Lock()
        self._bridge = _SampleBridge()
        self._bridge.samples_ready.connect(self._on_samples_main_thread)

        # ── Periodic FFT tick ─────────────────────────────────────────
        self._fft_timer = QTimer(self)
        self._fft_timer.timeout.connect(self._tick_fft)
        self._fft_timer.start(33)

    # ── Read-only properties ──────────────────────────────────────────
    @property
    def ip(self): return self._ip
    @property
    def freq_hz(self): return self._freq_hz
    @property
    def rate(self): return self._rate
    @property
    def mode(self): return self._mode
    @property
    def gain_db(self): return self._gain_db
    @property
    def volume(self): return self._volume
    @property
    def rx_bw(self): return self._rx_bw_by_mode.get(self._mode, 2400)
    @property
    def tx_bw(self): return self._tx_bw_by_mode.get(self._mode, 2400)
    def rx_bw_for(self, mode): return self._rx_bw_by_mode.get(mode, 2400)
    def tx_bw_for(self, mode): return self._tx_bw_by_mode.get(mode, 2400)
    @property
    def bw_locked(self): return self._bw_locked
    @property
    def notches(self) -> list[Notch]:
        """Live list of notch objects. Read-only — use add_notch /
        remove_nearest_notch / set_notch_width_at / etc. to mutate."""
        return list(self._notches)
    @property
    def notch_freqs(self) -> list[float]:
        """Just the absolute centre frequencies, for legacy callers."""
        return [n.abs_freq_hz for n in self._notches]
    @property
    def notch_details(self) -> list[tuple[float, float, bool, bool]]:
        """(freq_hz, width_hz, active, deep) tuples — emitted on
        notches_changed. Stable shape so UI/TCI subscribers don't
        depend on the Notch dataclass internals. The `deep` flag
        lets the visualization show cascaded notches with a thicker
        outline."""
        return [
            (n.abs_freq_hz, n.width_hz, n.active, n.deep)
            for n in self._notches
        ]
    @property
    def notch_enabled(self): return self._notch_enabled
    @property
    def notch_default_width_hz(self) -> float:
        """Width used for newly-placed notches. Operator changes via
        the right-click 'Default width for new notches' submenu."""
        return self._notch_default_width_hz
    @property
    def audio_output(self): return self._audio_output
    @property
    def is_streaming(self): return self._stream is not None
    @property
    def filter_board_enabled(self): return self._filter_board_enabled
    @property
    def oc_bits(self): return self._oc_bits_current
    @property
    def usb_bcd_enabled(self): return self._usb_bcd_enabled
    @property
    def usb_bcd_serial(self): return self._usb_bcd_serial
    @property
    def usb_bcd_value(self): return self._usb_bcd_value
    @property
    def bcd_60m_as_40m(self): return self._bcd_60m_as_40m

    def set_bcd_60m_as_40m(self, on: bool):
        """Toggle whether 60 m uses the 40 m BCD code (3) or the
        unassigned code 0 (amp bypasses). Most amps share the 40 m
        filter for 60 m; the default is True."""
        self._bcd_60m_as_40m = bool(on)
        if self._usb_bcd_enabled:
            self._apply_bcd_for_current_freq()

    # ── Setters (mutate + emit) ───────────────────────────────────────
    def set_ip(self, ip: str):
        if ip and ip != self._ip:
            self._ip = ip
            self.ip_changed.emit(ip)

    def set_freq_hz(self, hz: int):
        hz = int(hz)
        if hz == self._freq_hz:
            return
        self._freq_hz = hz
        if self._stream:
            try:
                self._stream._set_rx1_freq(hz)  # noqa: SLF001
            except Exception as e:
                self.status_message.emit(f"Freq set failed: {e}", 3000)
        with self._ring_lock:
            self._sample_ring.clear()
        # Reset waterfall tick counter on freq change too, so the
        # next waterfall row arrives promptly instead of inheriting
        # whatever counter state existed at the previous frequency.
        self._waterfall_tick_counter = 0
        # Flush the audio chain. Field test on AM 10 MHz WWV → DIGU
        # 7.074 MHz FT8: audio could get stuck silent across big
        # freq jumps until the operator cycled the sample rate.
        # channel.reset() drops in-flight audio buffer + forces
        # decimator rebuild (matches what set_in_rate did, which
        # was the only previous escape hatch). Also reset AGC peak
        # so a stale loud-signal peak from the prior band doesn't
        # clamp gain to silence on the new band while it slowly
        # decays.
        self._rx_channel.reset()
        self._agc_peak = 1e-4
        self._agc_hang_counter = 0
        self._smeter_avg_lin = 0.0
        # Re-assert the sample-rate keepalive C&C. _set_rx1_freq
        # above replaced _keepalive_cc with the freq command, so
        # the periodic keepalive stopped re-asserting C&C 0x00
        # (sample-rate config). HL2 gateware seems to need that
        # re-assertion across LO retunes; without it the EP6
        # stream can stall in a way only a manual rate cycle
        # unsticks. This single C&C resend is the cheap fix.
        if self._stream:
            try:
                self._stream.reassert_rate_keepalive()
            except Exception:
                pass
        self._rebuild_notches()
        # If the band just changed and filter board is active, push the
        # new OC pattern so the N2ADR relays follow.
        if self._filter_board_enabled:
            self._apply_oc_for_current_freq()
        if self._usb_bcd_enabled:
            self._apply_bcd_for_current_freq()
        # Auto-save freq into the current band's memory slot
        if not self._suppress_band_save:
            self._save_current_band_memory()
        # Advisory: fire a toast on band-plan edge transitions.
        self._check_in_band()
        self.freq_changed.emit(hz)

    def set_rate(self, rate: int):
        if rate not in SAMPLE_RATES or rate == self._rate:
            return
        prev_rate = self._rate
        self._rate = rate
        with self._ring_lock:
            self._sample_ring.clear()
        # Reset the waterfall tick counter so the divider check
        # starts cleanly with the new rate. Without this, a counter
        # mid-cycle could leave the next waterfall row up to N FFT
        # ticks late (looked like a brief hang on rate change).
        self._waterfall_tick_counter = 0
        if self._stream:
            try:
                self._stream.set_sample_rate(rate)
            except Exception as e:
                self.status_message.emit(f"Rate change failed: {e}", 3000)
        # Channel rebuilds its decimator on the next IQ block at the
        # new rate; notches use rate in coefficient calc so rebuild here.
        self._rx_channel.set_in_rate(rate)
        self._rebuild_notches()
        self.rate_changed.emit(rate)

        # NOTE: previous versions auto-switched audio output from
        # AK4951 → PC Soundcard whenever IQ rate > 48 k, on the
        # premise that "AK4951 requires 48 k IQ rate." That premise
        # was wrong. The AK4951 codec runs at 48 kHz audio rate
        # always — that's the chip spec AND it's what every
        # downstream consumer (speakers, WSJT-X, fldigi, audio
        # routing software) wants. The HPSDR EP2 audio protocol slot
        # is also 48 kHz regardless of IQ rate. So the audio path is
        # totally independent of the IQ spectrum rate; "demod stays
        # at 48 k while spectrum runs at 192/384 k" is the design,
        # not a bug. Confirmed empirically by the operator running
        # AK4951 cleanly at 192 k IQ for an extended session.
        # Therefore: no auto-switch. Operator's audio output choice
        # is sticky across rate (and band, mode, etc.) changes.

    def _rebuild_notches(self):
        """Re-design every notch's underlying filter — needed when
        sample rate or VFO frequency changes (since both affect the
        baseband offset that the filter is centered on). Preserves
        each notch's width, active flag, and deep flag."""
        rebuilt = []
        for n in self._notches:
            nf = self._make_notch_filter(
                n.abs_freq_hz, n.width_hz, deep=n.deep)
            if nf:
                rebuilt.append(Notch(
                    abs_freq_hz=n.abs_freq_hz, width_hz=n.width_hz,
                    active=n.active, deep=n.deep, filter=nf,
                ))
        self._notches = rebuilt
        if self._notch_enabled:
            self.notches_changed.emit(self.notch_details)

    def set_mode(self, mode: str):
        # Accept legacy aliases from old saved settings so a loaded value
        # like "CW" (before we split into CWL/CWU) doesn't leave the radio
        # in a state with no matching demod (→ silent audio).
        alias = {"CW": "CWU", "NFM": "FM", "WFM": "FM"}.get(mode, mode)
        if alias not in self.ALL_MODES:
            alias = "USB"
        if alias == self._mode:
            return
        self._mode = alias
        # Channel handles its own audio-buffer flush + NR reset on
        # mode switch so the previous mode's noise-floor estimate
        # doesn't leak in as an audible transient.
        self._rx_channel.set_mode(alias)
        # Also reset AGC + S-meter state. Different demods produce
        # very different peak levels (AM envelope vs SSB sideband
        # vs CW pitched tone), so a peak captured under the old
        # mode would mis-clamp gain under the new one until it
        # decayed. Same field-test motivation as set_freq_hz.
        self._agc_peak = 1e-4
        self._agc_hang_counter = 0
        self._smeter_avg_lin = 0.0
        # Re-assert the sample-rate keepalive C&C — same reason as
        # set_freq_hz. Mode change typically pairs with bandwidth /
        # filter changes that may have queued LNA-gain or other
        # C&Cs that displaced the sample-rate keepalive. Cheap
        # belt-and-suspenders against the stuck-stream symptom.
        if self._stream:
            try:
                self._stream.reassert_rate_keepalive()
            except Exception:
                pass
        if not self._suppress_band_save:
            self._save_current_band_memory()
        self.mode_changed.emit(alias)
        self._emit_passband()
        # CW Zero line lives at +/-pitch in CWU/CWL, hidden elsewhere —
        # re-emit so the panadapter draws or removes the white line.
        self._emit_cw_zero()

    def _compute_passband(self) -> tuple[int, int]:
        """Return (low_hz, high_hz) offsets from the tuned center for
        the current mode + RX BW. Used by the panadapter to draw a
        translucent passband rectangle.

        Conventions:
          USB / DIGU         : center .. center + BW
          LSB / DIGL         : center - BW .. center
          CWU                : center + pitch - BW/2 .. center + pitch + BW/2
          CWL                : center - pitch - BW/2 .. center - pitch + BW/2
                                (CW filter is centered on the pitch.
                                The visible gap between the marker and
                                the passband rectangle IS the zero-beat
                                indicator — tune until the CW signal
                                sits inside the offset rectangle.
                                Click-to-tune handles the offset for
                                you. Decoupled from BW so narrow
                                contest filters stay usable.)
          AM / DSB / FM      : center - BW/2 .. center + BW/2
        """
        mode = self._mode
        bw = int(self._rx_bw_by_mode.get(mode, 2400))
        if mode in ("USB", "DIGU"):
            return (0, bw)
        if mode in ("LSB", "DIGL"):
            return (-bw, 0)
        # CW: filter sits offset from the carrier by ±pitch. The
        # panadapter is in sky-freq convention (display-side mirror
        # applied), so CWU draws RIGHT of marker and CWL draws LEFT —
        # matching SSB/USB sky-freq convention. The HL2 baseband mirror
        # is handled inside CWDemod and is invisible at this layer.
        if mode == "CWU":
            half = bw // 2
            p = int(self._cw_pitch_hz)
            return (p - half, p + half)
        if mode == "CWL":
            half = bw // 2
            p = int(self._cw_pitch_hz)
            return (-p - half, -p + half)
        if mode in ("AM", "DSB", "FM"):
            half = bw // 2
            return (-half, half)
        # Tone / Off — no meaningful passband, return nothing
        return (0, 0)

    def _emit_passband(self):
        lo, hi = self._compute_passband()
        self.passband_changed.emit(int(lo), int(hi))

    # HL2 LNA range matches reference HL2 client convention: -12..+31 dB.
    # (the reference HL2 client uses -28..+31 full-span; Lyra currently encodes via
    # `+12 bias` against the HPSDR P1 C0=0x14 register, which clips
    # the lower end at -12. Upper end is the HL2 hardware cap at +31 —
    # values 32..48 produce no further gain and can push the AD9866
    # PGA into IMD territory.)
    LNA_MIN_DB = -12
    LNA_MAX_DB = 31

    # ── Auto-LNA pull-up tunables ──
    # All values empirically conservative. Pull-up is opt-in via
    # set_lna_auto_pullup(); back-off branch (always on when
    # lna_auto=True) uses its own thresholds in _adjust_lna_auto.
    #
    # Quiet detection — both must be true for the band to count as
    # quiet (RMS catches sustained noise floor; peak guards against
    # a strong signal that just brushed the passband). RMS-based
    # detection deliberately avoids the v1 trap of chasing peaks.
    LNA_AUTO_QUIET_RMS_DBFS = -50.0
    LNA_AUTO_QUIET_PEAK_DBFS = -25.0
    # Passband-aware gate: pull-up bails if the demod passband peak
    # sticks out above the noise floor by more than this margin —
    # i.e. there's a real signal in your filter that you're listening
    # to, even if the rest of the 192 kHz band is quiet (RMS-wise).
    # Without this gate, a strong narrowband signal like WWV at 10 MHz
    # (a few kHz wide in a 192 kHz IQ stream) doesn't budge the
    # full-band RMS / peak metrics, so pull-up climbs and pushes the
    # AD9866 PGA toward its compression knee — producing AGC pumping
    # / pulsing-spectrum / chopped audio.
    LNA_AUTO_PULLUP_PASSBAND_MARGIN_DB = 10.0
    # Sustained-quiet streak in ticks (500 ms each). Tiered by
    # distance from the active ceiling so the climb feels responsive
    # when starting from low LNA but still careful in the last
    # few dB before the ceiling:
    #   FAR    — more than NEAR_BAND_DB below ceiling → 1 tick
    #            (500 ms), +2 dB step (rapid climb to bring weak
    #            signals up)
    #   NEAR   — within NEAR_BAND_DB of ceiling → 2 ticks (1 s),
    #            +1 dB step (gentle approach to avoid overshoot)
    # Total -6 → +15 climb under signal-in-passband conditions:
    # ~7 FAR jumps × 0.5 s + ~8 NEAR jumps × 1 s = ~12 s.
    LNA_AUTO_PULLUP_FAR_TICKS  = 1
    LNA_AUTO_PULLUP_FAR_STEP   = 2
    LNA_AUTO_PULLUP_NEAR_TICKS = 2
    LNA_AUTO_PULLUP_NEAR_STEP  = 1
    # Distance from the active ceiling that switches FAR → NEAR.
    LNA_AUTO_PULLUP_NEAR_BAND_DB = 8
    # Ceiling for AUTO climb. User can still manually go higher.
    # Set well below LNA_MAX_DB and below the +44 dB IMD zone the
    # v1 auto-chase reached. Loop is also self-limiting: as gain
    # rises, RMS rises with it and eventually crosses the quiet
    # threshold, halting climb naturally.
    LNA_AUTO_PULLUP_CEILING_DB = 24
    # Two-tier ceiling: when there's a real signal in the demod
    # passband (passband peak > NF + LNA_AUTO_PULLUP_PASSBAND_MARGIN_DB),
    # climb stops at this lower value to keep the AD9866 PGA out of
    # the +18..+28 dB compression zone where strong passband signals
    # produce IMD/AGC pumping. Below this gain, signal-present is
    # IRRELEVANT to pull-up — that's exactly the case where pull-up
    # is most useful (bringing weak signals up from inaudible). The
    # fix for the WWV-at-LNA-negative case where pull-up wouldn't
    # climb because WWV's carrier was correctly visible.
    LNA_AUTO_PULLUP_SIGNAL_CEILING_DB = 15
    # Defer pull-up after a manual gain change so the operator's
    # intent isn't immediately overridden.
    LNA_AUTO_PULLUP_DEFER_S = 5.0

    def set_gain_db(self, db: int):
        db = max(self.LNA_MIN_DB, min(self.LNA_MAX_DB, int(db)))
        if db == self._gain_db:
            return
        self._gain_db = db
        # Stamp manual changes for pull-up's defer-to-user logic.
        # Auto-driven calls are wrapped in _lna_in_auto_adjust so
        # the auto loop doesn't keep deferring to itself.
        if not self._lna_in_auto_adjust:
            import time as _time
            self._lna_last_user_change_ts = _time.monotonic()
            # Manual change resets the quiet-streak counter — start
            # fresh from whatever band conditions look like now.
            self._lna_pullup_quiet_streak = 0
        if self._stream:
            try:
                self._stream.set_lna_gain_db(db)
            except Exception:
                pass
        if not self._suppress_band_save:
            self._save_current_band_memory()
        self.gain_changed.emit(db)

    def set_volume(self, v: float):
        # Volume is now purely a final trim stage (post AF Gain), so
        # its effective range is 0..1.0 (0 = silent, 1 = unity pass of
        # AF-gained signal). Old QSettings values in the 0..3.0 range
        # from pre-split code get clamped to 1.0 at load time; the
        # operator can re-dial to taste from there.
        v = max(0.0, min(1.0, float(v)))
        self._volume = v
        self.volume_changed.emit(v)

    # ── AF Gain (post-AGC, pre-Volume makeup gain) ────────────────────
    @property
    def af_gain_db(self) -> int:
        return self._af_gain_db

    def set_af_gain_db(self, db: int):
        """Integer dB, clamped 0..+80. Applied in _apply_agc_and_volume
        as a linear multiplier between AGC and Volume. Dedicated stage
        so operators running AGC off on digital modes have a natural
        "station loudness" knob independent of moment-to-moment
        Volume trim.

        Range goes to +80 dB (was +50) because AGC OFF has no other
        source of makeup gain — and AGC ON internally provides up to
        +60 dB of automatic amplification, so a +50 dB AF cap left
        AGC OFF roughly 30 dB quieter on weak signals than AGC ON.
        +80 dB closes that gap; operators who don't need the upper
        range simply never visit it."""
        db = max(0, min(80, int(db)))
        if db == self._af_gain_db:
            return
        self._af_gain_db = db
        self.af_gain_db_changed.emit(db)

    @property
    def af_gain_linear(self) -> float:
        # Cached linear multiplier — used by the audio loop to avoid
        # doing 10^(db/20) per block. Trivial to compute on-demand
        # since it's just integer dB, but kept as a property for
        # clarity at call sites.
        return 10.0 ** (self._af_gain_db / 20.0)

    # ── Stereo balance (pan) ──────────────────────────────────────────
    @property
    def balance(self) -> float:
        """Current stereo balance, -1 (full left) .. 0 (center) ..
        +1 (full right)."""
        return self._balance

    def set_balance(self, value: float):
        """Set stereo balance. Clamped to [-1, 1]. Pushes the
        equal-power L/R gains into the active sink immediately so
        the change is audible without waiting for the next audio
        block."""
        v = max(-1.0, min(1.0, float(value)))
        if v == self._balance:
            return
        self._balance = v
        self._push_balance_to_sink()
        self.balance_changed.emit(v)

    def _push_balance_to_sink(self):
        """Translate the current balance value to L/R gains and tell
        the active sink. Sinks that can't pan (AK4951) silently
        ignore. Called by set_balance and any time the sink is
        rebuilt (set_audio_output, set_pc_audio_device_index)."""
        l, r = self.balance_lr_gains
        try:
            self._audio_sink.set_lr_gains(l, r)
        except (AttributeError, Exception):
            pass

    @property
    def balance_lr_gains(self) -> tuple[float, float]:
        """Return (left_gain, right_gain) for the current balance
        using an EQUAL-POWER pan law:
            L = cos((b + 1) * π/4)
            R = sin((b + 1) * π/4)
        At center (b=0): L = R = √2/2 ≈ 0.707 (each channel -3 dB,
        sum-power constant). Full left (b=-1): L=1, R=0. Full
        right (b=+1): L=0, R=1.

        Equal-power matters because a constant-amplitude pan would
        make a center-panned signal sound 3 dB louder than a hard-
        panned one. Equal-power keeps perceived loudness stable as
        the operator sweeps the pan."""
        import math
        angle = (self._balance + 1.0) * math.pi / 4.0   # 0 .. π/2
        return (math.cos(angle), math.sin(angle))

    # ── Mute ────────────────────────────────────────────────────────
    @property
    def muted(self) -> bool:
        return self._muted

    # ── Noise-floor marker API ───────────────────────────────────────
    @property
    def noise_floor_enabled(self) -> bool:
        return self._noise_floor_enabled

    def set_noise_floor_enabled(self, on: bool):
        """Toggle the panadapter's horizontal noise-floor reference
        line. State is emitted immediately so the widget can hide the
        line without waiting for the next emission tick."""
        on = bool(on)
        if on == self._noise_floor_enabled:
            return
        self._noise_floor_enabled = on
        # When disabled, push a NaN sentinel so the widget hides the
        # line. Python floats don't round-trip cleanly through Qt's
        # Signal(float) on all platforms with NaN, so we use a huge
        # negative magic value the widget treats as "off".
        payload = self._noise_floor_db if on else -999.0
        self.noise_floor_changed.emit(float(payload) if payload is not None else -999.0)

    # ── Band plan API ────────────────────────────────────────────────
    @property
    def band_plan_region(self) -> str:
        return self._band_plan_region

    def set_band_plan_region(self, region_id: str):
        """Switch the active region. Triggers a panadapter repaint via
        the emitted signal, and a fresh in-band check (so if the new
        region has a stricter allocation and the current freq is
        outside, the toast fires right away)."""
        from lyra.band_plan import REGIONS
        region_id = str(region_id).strip() or "NONE"
        if region_id not in REGIONS:
            region_id = "NONE"
        if region_id == self._band_plan_region:
            return
        self._band_plan_region = region_id
        self.band_plan_region_changed.emit(region_id)
        # Recompute in-band state so a toast can fire if the region
        # switch has put us on the wrong side of the allocation.
        self._last_in_band = True   # force re-emit path
        self._check_in_band()

    @property
    def band_plan_show_segments(self) -> bool:
        return self._band_plan_show_segments

    def set_band_plan_show_segments(self, on: bool):
        on = bool(on)
        if on == self._band_plan_show_segments:
            return
        self._band_plan_show_segments = on
        self.band_plan_show_segments_changed.emit(on)

    @property
    def band_plan_show_landmarks(self) -> bool:
        return self._band_plan_show_landmarks

    def set_band_plan_show_landmarks(self, on: bool):
        on = bool(on)
        if on == self._band_plan_show_landmarks:
            return
        self._band_plan_show_landmarks = on
        self.band_plan_show_landmarks_changed.emit(on)

    @property
    def band_plan_edge_warn(self) -> bool:
        return self._band_plan_edge_warn

    def set_band_plan_edge_warn(self, on: bool):
        on = bool(on)
        if on == self._band_plan_edge_warn:
            return
        self._band_plan_edge_warn = on
        self.band_plan_edge_warn_changed.emit(on)

    # ── Peak-markers API ─────────────────────────────────────────────
    @property
    def peak_markers_enabled(self) -> bool:
        return self._peak_markers_enabled

    def set_peak_markers_enabled(self, on: bool):
        on = bool(on)
        if on == self._peak_markers_enabled:
            return
        self._peak_markers_enabled = on
        self.peak_markers_enabled_changed.emit(on)

    @property
    def peak_markers_decay_dbps(self) -> float:
        return self._peak_markers_decay_dbps

    # ── User color pickers API ───────────────────────────────────────
    @property
    def spectrum_trace_color(self) -> str:
        return self._spectrum_trace_color

    def set_spectrum_trace_color(self, hex_str: str):
        """Hex like '#5ec8ff', or '' to revert to default."""
        v = str(hex_str or "").strip()
        if v == self._spectrum_trace_color:
            return
        self._spectrum_trace_color = v
        self.spectrum_trace_color_changed.emit(v)

    @property
    def segment_colors(self) -> dict:
        return dict(self._segment_colors)

    def set_segment_color(self, kind: str, hex_str: str):
        """Override the color for one segment kind (CW / DIG / SSB /
        FM / MIX / BC). Empty hex reverts to the built-in default."""
        kind = str(kind).upper()
        if not kind:
            return
        v = str(hex_str or "").strip()
        cur = self._segment_colors.get(kind, "")
        if v == cur:
            return
        if v:
            self._segment_colors[kind] = v
        else:
            self._segment_colors.pop(kind, None)
        self.segment_colors_changed.emit(dict(self._segment_colors))

    def reset_segment_colors(self):
        """Clear every per-segment override in one shot."""
        if not self._segment_colors:
            return
        self._segment_colors.clear()
        self.segment_colors_changed.emit({})

    @property
    def noise_floor_color(self) -> str:
        return self._noise_floor_color

    def set_noise_floor_color(self, hex_str: str):
        """Noise-floor reference line color. '' reverts to default
        sage green. User-visible color separate from the spectrum
        trace so the NF line doesn't vanish when they paint the
        trace in a similar tone."""
        v = str(hex_str or "").strip()
        if v == self._noise_floor_color:
            return
        self._noise_floor_color = v
        self.noise_floor_color_changed.emit(v)

    @property
    def peak_markers_color(self) -> str:
        return self._peak_markers_color

    def set_peak_markers_color(self, hex_str: str):
        """Peak-markers color override. '' reverts to the default
        amber (255,190,90). Separate picker so users can match peak
        color to their spectrum-trace choice or pick a high-contrast
        accent."""
        v = str(hex_str or "").strip()
        if v == self._peak_markers_color:
            return
        self._peak_markers_color = v
        self.peak_markers_color_changed.emit(v)

    def set_peak_markers_decay_dbps(self, dbps: float):
        """Set peak decay rate in dB/second. 0.1 = very slow (peaks
        linger ~5 minutes), 60 = very fast (peaks gone in half a
        second). Clamp 0.5..120."""
        v = max(0.5, min(120.0, float(dbps)))
        if abs(v - self._peak_markers_decay_dbps) < 1e-3:
            return
        self._peak_markers_decay_dbps = v
        self.peak_markers_decay_changed.emit(v)

    PEAK_MARKER_STYLES = ("line", "dots", "triangles")

    @property
    def peak_markers_style(self) -> str:
        return self._peak_markers_style

    def set_peak_markers_style(self, name: str):
        name = (name or "").strip().lower()
        if name not in self.PEAK_MARKER_STYLES:
            name = "dots"
        if name == self._peak_markers_style:
            return
        self._peak_markers_style = name
        self.peak_markers_style_changed.emit(name)

    @property
    def peak_markers_show_db(self) -> bool:
        return self._peak_markers_show_db

    def set_peak_markers_show_db(self, on: bool):
        on = bool(on)
        if on == self._peak_markers_show_db:
            return
        self._peak_markers_show_db = on
        self.peak_markers_show_db_changed.emit(on)

    def _check_in_band(self):
        """Emit a status toast when the freq crosses into / out of an
        allocated band for the current region. Called after any tune
        change; only emits on state *transitions* so we don't spam
        the status bar while tuning around outside the plan."""
        if self._band_plan_region == "NONE":
            return
        from lyra.band_plan import find_band
        band = find_band(self._band_plan_region, int(self._freq_hz))
        in_band = band is not None
        if in_band == self._last_in_band:
            return  # no transition, nothing to announce
        self._last_in_band = in_band
        if not self._band_plan_edge_warn:
            return
        if in_band:
            self.status_message.emit(
                f"In band: {band['name']}  ({self._band_plan_region})", 2500)
        else:
            self.status_message.emit(
                f"⚠ Out of band — {self._freq_hz/1e6:.3f} MHz is outside "
                f"the {self._band_plan_region} amateur allocations",
                5000)

    # ── Noise Reduction API ──────────────────────────────────────────
    NR_PROFILES = ("light", "medium", "aggressive", "neural")

    @staticmethod
    def neural_nr_available() -> bool:
        """Probe whether a neural-NR backend (RNNoise or DeepFilterNet)
        is importable. Used to enable/disable the 'Neural' profile in
        the front-panel right-click menu. Safe to call anywhere — if
        probing fails we return False rather than raising."""
        for name in ("rnnoise_wrapper", "deepfilternet"):
            try:
                __import__(name)
                return True
            except ImportError:
                continue
        return False

    @property
    def nr_enabled(self) -> bool:
        return self._rx_channel.nr_enabled

    def set_nr_enabled(self, on: bool):
        on = bool(on)
        if on == self._rx_channel.nr_enabled:
            return
        # Channel handles its own NR state (including the fresh-reset
        # on enable so a stale overlap tail doesn't leak in).
        self._rx_channel.set_nr_enabled(on)
        self.nr_enabled_changed.emit(on)

    @property
    def nr_profile(self) -> str:
        return self._nr_profile

    def set_nr_profile(self, name: str):
        name = (name or "").strip().lower()
        if name not in self.NR_PROFILES:
            name = "medium"
        self._nr_profile = name
        if name == "neural":
            # Reserved UI slot — no classical backend change. When a
            # neural package gets wired in (probably via a different
            # DspChannel subclass), this branch will swap the channel.
            # For now fall back to medium so audio still flows.
            self._rx_channel.set_nr_profile("medium")
        else:
            self._rx_channel.set_nr_profile(name)
        self.nr_profile_changed.emit(name)

    def set_muted(self, on: bool):
        on = bool(on)
        if on == self._muted:
            return
        self._muted = on
        self.muted_changed.emit(on)

    def toggle_muted(self):
        self.set_muted(not self._muted)

    # ── Auto-LNA ────────────────────────────────────────────────────
    # Periodically nudges LNA gain up/down to keep the ADC peak inside
    # a comfortable band (target ± hysteresis). Does NOT fight with the
    # user — each adjustment is clamped to ±3 dB per step so the user
    # can always override by dragging the slider; Auto will walk back
    # toward the target next tick.
    @property
    def lna_auto(self) -> bool:
        return self._lna_auto

    def set_lna_auto(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self._lna_auto:
            return
        self._lna_auto = enabled
        if enabled:
            # Reset history so we evaluate from current conditions
            self._lna_peaks = []
            self._lna_rms = []
            self._lna_pullup_quiet_streak = 0
            self._lna_auto_timer.start()
        else:
            self._lna_auto_timer.stop()
        self.lna_auto_changed.emit(enabled)

    # ── Auto-LNA pull-up (opt-in bidirectional mode) ──
    @property
    def lna_auto_pullup(self) -> bool:
        return self._lna_auto_pullup

    def set_lna_auto_pullup(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self._lna_auto_pullup:
            return
        self._lna_auto_pullup = enabled
        # Reset streak whenever the toggle changes — start fresh.
        self._lna_pullup_quiet_streak = 0
        self.lna_auto_pullup_changed.emit(enabled)

    def _emit_peak_reading(self):
        """Periodic (4 Hz) ADC peak broadcast — drives the toolbar
        indicator. Independent of Auto-LNA state.

        Uses a SHORT window (last ~20 block peaks ≈ 200 ms) instead
        of the full rolling history, so LNA changes are reflected in
        the reading within a fraction of a second rather than taking
        1+ seconds for the stale max to decay out. This matches how
        the RFP / ADC meters in other SDR clients behave — responsive
        to the current signal environment, not a rolling worst-case.
        """
        if not self._lna_peaks:
            return
        # Use last ~200ms for responsiveness, not the full 1.28 s
        # history window that _lna_peaks_max holds. The longer window
        # is still tracked for Auto-LNA's overload-protection logic,
        # which legitimately wants the worst-case peak.
        recent_peaks = (self._lna_peaks[-20:]
                        if len(self._lna_peaks) >= 20 else self._lna_peaks)
        recent_rms = (self._lna_rms[-20:]
                      if len(self._lna_rms) >= 20 else self._lna_rms)
        p = max(recent_peaks) if recent_peaks else 0.0
        r = (sum(x * x for x in recent_rms) / len(recent_rms)) ** 0.5 if recent_rms else 0.0
        # Convert to dBFS; floor at something sensible to avoid -inf
        # when the stream is still starting up.
        peak_db = 20.0 * float(np.log10(max(p, 1e-6)))
        rms_db = 10.0 * float(np.log10(max(r * r, 1e-12)))
        self._lna_current_peak_dbfs = peak_db
        self.lna_peak_dbfs.emit(peak_db)
        self.lna_rms_dbfs.emit(rms_db)

    def _emit_hl2_telemetry(self):
        """Periodic (2 Hz) HL2 hardware telemetry broadcast → toolbar.

        Reads the latest raw ADC counts the protocol layer has folded
        into FrameStats and converts them to engineering units.

        Conversion formulas — physics / hardware constants only, no
        external code reuse:

        TEMPERATURE — AD9866 on-die temperature diode. Per the chip
            datasheet, the diode output crosses 0.5 V at 0 °C with a
            10 mV/°C slope into a 3.26 V ADC reference:
                temp_C = (3.26 * (adc / 4096) - 0.5) / 0.01
                       = ((adc / 4096) * 3.26 - 0.5) * 100

        SUPPLY VOLTAGE — 12 V rail via the on-board AIN6 sense
            divider. The supply path uses an external scaling stage
            with a 5.0 V reference and a 22 + 1 ohm / 1.1 ohm
            resistor network (ratio 23/1.1):
                v_supply = (adc / 4095) * 5.0 * (23.0 / 1.1)

            These constants are properties of the HL2 PCB, not of any
            particular host program — any client reading AIN6 must
            apply this scaling to recover the rail voltage.

        FWD / REV POWER — raw ADC counts only. Real-power conversion
            depends on the SWR-bridge calibration which varies per HL2
            unit; the UI doesn't display these yet (future TX feature)
            but they're in the payload so future widgets can read them.
        """
        s = self._stream.stats if self._stream is not None else None
        if s is None:
            payload = {"temp_c":   float("nan"),
                       "supply_v": float("nan"),
                       "fwd_w":    float("nan"),
                       "rev_w":    float("nan")}
        else:
            # ADC == 0 means we've not yet seen a telemetry frame for
            # that field — emit NaN so the UI shows "--" rather than
            # claiming the rig is at 0 °C / 0 V.
            temp_c = (((s.temp_adc / 4096.0) * 3.26 - 0.5) * 100.0
                      if s.temp_adc else float("nan"))
            # Supply voltage — try the standard slot first (addr 3),
            # fall back to the firmware-variant slot (addr 0 C1:C2 >> 4)
            # when the standard slot is empty. Any HL2 firmware that
            # works with other clients populates ONE of these.
            adc = s.supply_adc if s.supply_adc else s.supply_adc_alt
            supply_v = ((adc / 4095.0) * 5.0 * (23.0 / 1.1)
                        if adc else float("nan"))
            payload = {
                "temp_c":   temp_c,
                "supply_v": supply_v,
                "fwd_w":    float(s.fwd_pwr_adc),   # raw ADC for now
                "rev_w":    float(s.rev_pwr_adc),
            }
        self.hl2_telemetry_changed.emit(payload)

    def _adjust_lna_auto(self):
        """Overload-protection LNA loop — only REDUCE gain on impending
        overload, never chase a target upward.

        First-pass Lyra Auto-LNA was a target-chasing loop aiming at
        -15 dBFS peak. That target is HOTTER than the HL2 front-end
        likes; in real-world antenna environments on 40 m the loop
        drove LNA to +44 dB where IMD became audible ("odd mixed
        audio") and weak signals drowned in garbage. The community
        consensus for HL2 auto-attenuation is the back-off-only
        approach implemented below.

        Logic:
            peak > -3 dBFS  → drop 3 dB (urgent, close to clipping)
            peak > -10 dBFS → drop 2 dB (hot, leave margin)
            otherwise       → do not touch gain

        The operator sets their preferred gain manually (e.g. +5 dB
        on 40 m); Auto only engages when band conditions demand it.
        Recovery happens manually — when conditions calm down the
        user drags the slider back up (or clicks a band button,
        restoring band memory)."""
        if not self._lna_auto or not self._lna_peaks:
            return
        # Use MAX of recent window — we want the worst case for
        # overload protection, not a percentile (percentiles hide
        # exactly the spikes we care about).
        p_max = max(self._lna_peaks)
        if p_max <= 1e-6:
            return
        peak_dbfs = 20.0 * float(np.log10(p_max))
        self._lna_current_peak_dbfs = peak_dbfs
        self.lna_peak_dbfs.emit(peak_dbfs)

        # Overload-protection (back-off) branch — always active when
        # lna_auto is True. Two thresholds so we react aggressively
        # to near-clipping but gently to "just hot."
        step = 0
        reason = ""
        if peak_dbfs > -3.0:
            step = -3
            reason = "back-off (urgent)"
        elif peak_dbfs > -10.0:
            step = -2
            reason = "back-off"
        else:
            # Passband-signal back-off — if pull-up has driven LNA
            # high AND there's now a strong passband signal driving
            # the AD9866 PGA toward its compression knee, drop gain
            # even though the full-IQ peak is still cool. Catches
            # the WWV-arrives-at-+24-LNA case automatically when
            # pull-up is on; harmless in static manual-LNA setups
            # because gain is already where the user put it.
            pb_peak = self._lna_passband_peak_dbfs
            nf_db = self._noise_floor_db
            if (self._lna_auto_pullup
                    and self._gain_db > 12
                    and pb_peak is not None and nf_db is not None
                    and (pb_peak - nf_db) > 25.0):
                step = -2
                reason = "back-off (strong passband)"
            else:
                # ── Pull-up branch (opt-in, bidirectional Auto-LNA) ──
                # Reaches here only when the band is healthy from the
                # back-off perspective. If pull-up is enabled AND the
                # band has been sustained-quiet, climb 1 dB.
                if self._lna_auto_pullup:
                    step, reason = self._evaluate_pullup(peak_dbfs)
                if step == 0:
                    return   # nothing to do — healthy, no climb warranted

        new_db = max(self.LNA_MIN_DB,
                     min(self.LNA_MAX_DB, self._gain_db + step))
        # Apply the auto-specific ceiling only on the way UP. Going
        # down past the ceiling is fine (back-off must always be
        # allowed to lower gain however far it needs to).
        if step > 0:
            new_db = min(new_db, self.LNA_AUTO_PULLUP_CEILING_DB)
        if new_db == self._gain_db:
            return
        old_db = self._gain_db
        # Mark this gain change as auto-driven so set_gain_db doesn't
        # update _lna_last_user_change_ts (which would make the loop
        # forever defer to itself).
        self._lna_in_auto_adjust = True
        try:
            self.set_gain_db(new_db)
        finally:
            self._lna_in_auto_adjust = False
        self.status_message.emit(
            f"Auto-LNA: {reason} peak {peak_dbfs:+.1f} dBFS → "
            f"LNA {new_db:+d} dB",
            2000)
        # Structured event for the UI so it can flash the slider +
        # show a "last event" badge (signal-driven, not status-bar
        # polling — status messages disappear after 2 s).
        from datetime import datetime as _dt
        self.lna_auto_event.emit({
            "delta_db":    int(new_db - old_db),
            "peak_dbfs":   float(peak_dbfs),
            "new_gain_db": int(new_db),
            "when_local":  _dt.now().strftime("%H:%M:%S"),
        })
        self._lna_peaks = []
        self._lna_rms = []
        # Reset quiet streak after any auto adjustment — let conditions
        # re-prove themselves before we climb again.
        self._lna_pullup_quiet_streak = 0

    def _evaluate_pullup(self, peak_dbfs: float) -> tuple[int, str]:
        """Decide whether the pull-up branch should raise gain by 1 dB.

        Returns (step_db, reason). step_db == 0 means "do nothing."

        Rules (ALL must hold for a +1 dB step):
        - Pull-up is enabled (caller already checked this).
        - Current gain is below LNA_AUTO_PULLUP_CEILING_DB.
        - Last manual gain change was > LNA_AUTO_PULLUP_DEFER_S
          ago (don't immediately override the operator).
        - Peak dBFS over the recent window is below
          LNA_AUTO_QUIET_PEAK_DBFS.
        - Worst-case RMS over the recent window is below
          LNA_AUTO_QUIET_RMS_DBFS (i.e. true band quiet, not just
          gaps between transients).
        - The above conditions have held for
          LNA_AUTO_PULLUP_QUIET_TICKS consecutive ticks.

        Hits a self-limit naturally: each +1 dB of LNA raises the
        observed noise floor by ~1 dB, so RMS eventually crosses
        the quiet threshold and the streak stops accumulating —
        even before the hard ceiling is reached on a typical
        station."""
        # Two-tier ceiling: lower ceiling when a real signal is in
        # the demod passband (PGA-compression risk). Higher ceiling
        # otherwise (truly quiet band, only noise). Below either
        # ceiling, signal-present does NOT block climb — that's the
        # whole point of pull-up: bringing weak signals up.
        pb_peak = self._lna_passband_peak_dbfs
        nf_db = self._noise_floor_db
        signal_in_passband = (
            pb_peak is not None and nf_db is not None
            and (pb_peak - nf_db)
                > self.LNA_AUTO_PULLUP_PASSBAND_MARGIN_DB)
        if signal_in_passband:
            ceiling = self.LNA_AUTO_PULLUP_SIGNAL_CEILING_DB
        else:
            ceiling = self.LNA_AUTO_PULLUP_CEILING_DB
        if self._gain_db >= ceiling:
            self._lna_pullup_quiet_streak = 0
            return 0, ""
        # Defer to recent manual changes
        import time as _time
        since_user = _time.monotonic() - self._lna_last_user_change_ts
        if since_user < self.LNA_AUTO_PULLUP_DEFER_S:
            return 0, ""
        # Peak gate (already in dBFS from caller). Full-IQ peak —
        # protects against ADC-overload-imminent regardless of
        # whether the loud signal is in passband or out of it.
        if peak_dbfs >= self.LNA_AUTO_QUIET_PEAK_DBFS:
            self._lna_pullup_quiet_streak = 0
            return 0, ""
        # RMS gate — worst case (max) over the window for the same
        # reason the back-off uses peak max: we want to NOT pull up
        # if any recent tick saw real signal across the IQ band.
        if not self._lna_rms:
            return 0, ""
        rms_max_lin = max(self._lna_rms)
        if rms_max_lin <= 1e-6:
            # No data yet — don't act
            return 0, ""
        rms_max_dbfs = 20.0 * float(np.log10(rms_max_lin))
        if rms_max_dbfs >= self.LNA_AUTO_QUIET_RMS_DBFS:
            self._lna_pullup_quiet_streak = 0
            return 0, ""
        # Tiered cadence: FAR from ceiling → fewer ticks, bigger step
        # so weak-signal climbs feel responsive. NEAR ceiling → more
        # ticks, smaller step to avoid overshoot. The active ceiling
        # is 'ceiling' (set above based on signal-in-passband).
        gap_to_ceiling = ceiling - self._gain_db
        if gap_to_ceiling > self.LNA_AUTO_PULLUP_NEAR_BAND_DB:
            need_ticks = self.LNA_AUTO_PULLUP_FAR_TICKS
            step_db   = self.LNA_AUTO_PULLUP_FAR_STEP
        else:
            need_ticks = self.LNA_AUTO_PULLUP_NEAR_TICKS
            step_db   = self.LNA_AUTO_PULLUP_NEAR_STEP
        # All gates passed for this tick — accumulate streak
        self._lna_pullup_quiet_streak += 1
        if self._lna_pullup_quiet_streak < need_ticks:
            return 0, ""
        # Streak satisfied — clamp step so we don't overshoot the
        # ceiling on a FAR-tier +2 dB jump.
        step_db = min(step_db, max(0, gap_to_ceiling))
        if step_db <= 0:
            return 0, ""
        # Streak reset happens after the gain change in the caller.
        return step_db, "pull-up (band quiet)"

    def set_rx_bw(self, mode: str, bw: int):
        self._rx_bw_by_mode[mode] = int(bw)
        # Always push to channel so the per-mode BW state stays in sync
        # — the demod for `mode` rebuilds inside the channel.
        self._rx_channel.set_rx_bw(mode, int(bw))
        if mode == self._mode:
            self._emit_passband()
        self.rx_bw_changed.emit(mode, int(bw))
        if self._bw_locked and self._tx_bw_by_mode.get(mode) != int(bw):
            self._tx_bw_by_mode[mode] = int(bw)
            self.tx_bw_changed.emit(mode, int(bw))

    def set_tx_bw(self, mode: str, bw: int):
        self._tx_bw_by_mode[mode] = int(bw)
        self.tx_bw_changed.emit(mode, int(bw))
        if self._bw_locked and self._rx_bw_by_mode.get(mode) != int(bw):
            self._rx_bw_by_mode[mode] = int(bw)
            self._rx_channel.set_rx_bw(mode, int(bw))
            self.rx_bw_changed.emit(mode, int(bw))

    def set_bw_lock(self, locked: bool):
        self._bw_locked = bool(locked)
        if locked:
            rx = self._rx_bw_by_mode.get(self._mode)
            if rx is not None:
                self.set_tx_bw(self._mode, rx)
        self.bw_lock_changed.emit(self._bw_locked)

    def set_notch_enabled(self, enabled: bool):
        self._notch_enabled = bool(enabled)
        self.notch_enabled_changed.emit(self._notch_enabled)

    # ── Per-band memory ───────────────────────────────────────────────
    # Factory default auto-scale BOUNDS per band group. Different
    # bands have very different noise floor + dynamic range, so a
    # one-size-fits-all set of bounds either runs too tight on quiet
    # bands (10m / 6m, missing weak DX) or too wide on noisy ones
    # (160m, leaving the floor pegged to the bottom).
    # Operators can override these per-band — these are just the
    # starting point for any band the operator hasn't tweaked yet.
    _DEFAULT_BAND_RANGE_DB = {
        # Noisy LF/MF/lower-HF: noise often -100 to -110 dBFS
        "160m": (-130.0, -30.0),
        "80m":  (-130.0, -30.0),
        "60m":  (-130.0, -35.0),
        "40m":  (-130.0, -30.0),
        # Mid-HF: typical conditions
        "30m":  (-135.0, -40.0),
        "20m":  (-135.0, -40.0),
        "17m":  (-135.0, -40.0),
        # Quiet upper HF + 6m: weak signals, low noise floor
        "15m":  (-140.0, -50.0),
        "12m":  (-140.0, -50.0),
        "10m":  (-140.0, -50.0),
        "6m":   (-145.0, -55.0),
    }

    def _save_current_band_memory(self):
        band = band_for_freq(self._freq_hz)
        if band is None:
            return
        # Preserve any existing band-specific range bounds; we only
        # update the freq/mode/gain on every save (those change with
        # ordinary tuning). Range bounds change only when the
        # operator explicitly sets them, so we read-modify-write to
        # avoid clobbering on every freq tweak.
        existing = self._band_memory.get(band.name, {})
        existing.update({
            "freq_hz": self._freq_hz,
            "mode":    self._mode,
            "gain_db": self._gain_db,
        })
        self._band_memory[band.name] = existing

    def _save_current_band_range(self):
        """Save the operator's current spectrum range as the bounds
        for whichever band we're currently tuned to. Called whenever
        set_spectrum_db_range fires with from_user=True."""
        band = band_for_freq(self._freq_hz)
        if band is None:
            return
        existing = self._band_memory.get(band.name, {})
        existing["range_min_db"] = float(self._user_range_min_db)
        existing["range_max_db"] = float(self._user_range_max_db)
        self._band_memory[band.name] = existing

    def _apply_band_range(self, band_name: str):
        """Pull the saved range bounds for `band_name` (or the factory
        default for that band group) and apply them as the auto-scale
        bounds. Called from recall_band on band change so auto-scale
        re-fits within the new band's appropriate window."""
        memory = self._band_memory.get(band_name, {})
        if "range_min_db" in memory and "range_max_db" in memory:
            lo, hi = memory["range_min_db"], memory["range_max_db"]
        elif band_name in self._DEFAULT_BAND_RANGE_DB:
            lo, hi = self._DEFAULT_BAND_RANGE_DB[band_name]
        else:
            # Unknown band (broadcast-only / GEN sub-segment) — leave
            # bounds at whatever they currently are, no change.
            return
        # Update bounds + display range. from_user=True so the user
        # bounds are stored; the band's saved range becomes the new
        # baseline for auto.
        self.set_spectrum_db_range(lo, hi, from_user=True)

    def recall_band(self, band_name: str, defaults_freq: int,
                    defaults_mode: str):
        """Restore freq/mode/gain saved for `band_name` if present, else
        tune to the band's defaults. Also applies the band's saved
        spectrum range bounds (or factory defaults for that band group)
        so auto-scale re-fits within an appropriate window for the
        band's typical noise floor + signal levels.

        Suppresses the auto-save during the apply so we don't
        immediately overwrite the memory we just loaded with
        intermediate tuning steps."""
        memory = self._band_memory.get(band_name)
        self._suppress_band_save = True
        try:
            if memory:
                self.set_freq_hz(memory["freq_hz"])
                self.set_mode(memory["mode"])
                self.set_gain_db(memory["gain_db"])
            else:
                self.set_freq_hz(defaults_freq)
                self.set_mode(defaults_mode)
            # Apply per-band range bounds AFTER freq/mode are set so
            # band_for_freq() returns the right band for the
            # downstream save.
            self._apply_band_range(band_name)
        finally:
            self._suppress_band_save = False
        # Save (now that the dust has settled) so the next reactivation
        # of this band brings back exactly this state.
        self._save_current_band_memory()

    @property
    def band_memory_snapshot(self) -> dict:
        """Snapshot for QSettings persistence."""
        return dict(self._band_memory)

    def restore_band_memory(self, snapshot: dict):
        if isinstance(snapshot, dict):
            self._band_memory = {
                k: dict(v) for k, v in snapshot.items()
                if isinstance(v, dict) and "freq_hz" in v
            }

    # ── External filter board (N2ADR) ─────────────────────────────────
    def set_filter_board_enabled(self, enabled: bool):
        """Enable/disable automatic OC-pattern output for the N2ADR (or
        compatible) external filter board. When enabled, the board's
        relays track the current band automatically on every tune."""
        self._filter_board_enabled = bool(enabled)
        if self._filter_board_enabled:
            self._apply_oc_for_current_freq()
        else:
            self._set_oc_bits(0)
        self.filter_board_changed.emit(self._filter_board_enabled)

    def _apply_oc_for_current_freq(self):
        band = band_for_freq(self._freq_hz)
        pattern = n2adr_pattern_for_band(band.name if band else "", False)
        self._set_oc_bits(pattern)

    def _set_oc_bits(self, pattern: int):
        """Store new OC pattern and push to the radio via the config
        register. HL2's gateware forwards the bits to the N2ADR board
        via I²C."""
        pattern &= 0x7F
        if pattern == self._oc_bits_current:
            return
        self._oc_bits_current = pattern
        # Pack into C2[7:1]. C2[0] remains the CW-eer bit (0 for now).
        self._config_c2 = (pattern << 1) & 0xFE
        self._send_full_config()
        self.oc_bits_changed.emit(pattern, format_bits(pattern))

    def _send_full_config(self):
        """Send the current composed C0=0x00 config register to the radio.

        HL2 registers are sticky — one write persists until explicitly
        changed. No need to add this to the stream keepalive rotation;
        a single fire-and-forget send is enough."""
        if self._stream is None:
            return
        try:
            self._stream._send_cc(0x00, self._config_c1, self._config_c2,  # noqa: SLF001
                                  self._config_c3, self._config_c4)
        except Exception as e:
            self.status_message.emit(f"OC write failed: {e}", 3000)

    # ── USB-BCD cable (linear-amp band switching) ─────────────────────
    def set_usb_bcd_serial(self, serial: str):
        """Pick which FTDI device to use. If a cable is already open,
        close it and re-open on the new serial when re-enabled."""
        self._usb_bcd_serial = (serial or "").strip()
        if self._usb_bcd_cable is not None:
            try:
                self._usb_bcd_cable.close()
            except Exception:
                pass
            self._usb_bcd_cable = None
        if self._usb_bcd_enabled:
            self._open_usb_bcd()

    def set_usb_bcd_enabled(self, on: bool):
        """Open/close the FTDI cable. When on, immediately push the
        current band's BCD code so the amp tracks the radio."""
        on = bool(on)
        self._usb_bcd_enabled = on
        if on:
            self._open_usb_bcd()
            if self._usb_bcd_cable is not None:
                self._apply_bcd_for_current_freq()
        else:
            if self._usb_bcd_cable is not None:
                try:
                    self._usb_bcd_cable.close()
                except Exception:
                    pass
                self._usb_bcd_cable = None
            self._usb_bcd_value = 0
            self.bcd_value_changed.emit(0, "(disabled)")
        self.usb_bcd_changed.emit(on)

    def _open_usb_bcd(self):
        if not self._usb_bcd_serial:
            self.status_message.emit(
                "USB-BCD: no FTDI device selected", 4000)
            self._usb_bcd_enabled = False
            self.usb_bcd_changed.emit(False)
            return
        try:
            self._usb_bcd_cable = UsbBcdCable(self._usb_bcd_serial)
        except Ftd2xxNotInstalled as e:
            self.status_message.emit(str(e), 6000)
            self._usb_bcd_enabled = False
            self.usb_bcd_changed.emit(False)
        except Exception as e:
            self.status_message.emit(
                f"USB-BCD open failed: {e}", 5000)
            self._usb_bcd_enabled = False
            self.usb_bcd_changed.emit(False)

    def _apply_bcd_for_current_freq(self):
        if not self._usb_bcd_enabled or self._usb_bcd_cable is None:
            return
        band = band_for_freq(self._freq_hz)
        bcd = bcd_for_band(band.name if band else "",
                           sixty_as_forty=self._bcd_60m_as_40m)
        self._usb_bcd_value = bcd
        try:
            self._usb_bcd_cable.write_byte(bcd)
            self.bcd_value_changed.emit(
                bcd, band.name if band else "(no amp band)")
        except Exception as e:
            self.status_message.emit(f"USB-BCD write failed: {e}", 4000)

    # ── Notch bank API ────────────────────────────────────────────────
    # All operator-facing notch operations live here. Width is the
    # primary parameter (Hz, not Q). The IIR filter design is in
    # _make_notch_filter; this layer just manages the bank.

    NOTCH_WIDTH_MIN_HZ = 5.0       # narrowest practical width
    NOTCH_WIDTH_MAX_HZ = 2000.0    # widest practical width
    NOTCH_NEAREST_TOLERANCE_HZ = 2000.0   # for "find notch near click"

    def _find_nearest_notch_idx(self, abs_freq_hz: float,
                                tolerance_hz: float | None = None
                                ) -> int | None:
        if not self._notches:
            return None
        idx = min(range(len(self._notches)),
                  key=lambda i: abs(self._notches[i].abs_freq_hz - abs_freq_hz))
        tol = (tolerance_hz if tolerance_hz is not None
               else self.NOTCH_NEAREST_TOLERANCE_HZ)
        if abs(self._notches[idx].abs_freq_hz - abs_freq_hz) > tol:
            return None
        return idx

    def set_notch_default_width_hz(self, width_hz: float):
        """Change the width used for newly placed notches. Existing
        notches keep their individual widths unless explicitly
        adjusted via wheel/drag/menu."""
        w = max(self.NOTCH_WIDTH_MIN_HZ,
                min(self.NOTCH_WIDTH_MAX_HZ, float(width_hz)))
        self._notch_default_width_hz = w
        self.notch_default_width_changed.emit(w)

    def add_notch(self, abs_freq_hz: float,
                  width_hz: float | None = None,
                  active: bool = True,
                  deep: bool | None = None):
        """Place a new notch. Width defaults to the current
        notch_default_width_hz; deep defaults to the current
        notch_default_deep (True out of the box, for genuine
        "kill the carrier" attenuation). Auto-enables the notch
        bank if it's currently off, on the assumption that an
        operator placing a notch wants to hear the result.

        Pass `deep=True` or `deep=False` explicitly to override
        the default — useful for TCI restore + tests that need
        deterministic state."""
        w = width_hz if width_hz is not None else self._notch_default_width_hz
        w = max(self.NOTCH_WIDTH_MIN_HZ, min(self.NOTCH_WIDTH_MAX_HZ, float(w)))
        d = self._notch_default_deep if deep is None else bool(deep)
        nf = self._make_notch_filter(abs_freq_hz, w, deep=d)
        if nf is None:
            return
        self._notches.append(Notch(
            abs_freq_hz=float(abs_freq_hz), width_hz=w,
            active=bool(active), deep=d, filter=nf,
        ))
        if not self._notch_enabled:
            self.set_notch_enabled(True)
        self.notches_changed.emit(self.notch_details)

    def remove_nearest_notch(self, abs_freq_hz: float):
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz=1e9)
        if idx is None:
            return
        del self._notches[idx]
        self.notches_changed.emit(self.notch_details)

    def set_notch_width_at(self, abs_freq_hz: float, new_width_hz: float,
                           tolerance_hz: float | None = None) -> bool:
        """Find the notch nearest abs_freq_hz and rebuild it with a
        new width. Used by mouse-wheel and drag gestures over an
        existing notch. Returns True if a notch was matched + updated.

        Rebuild-throttle: drag gestures fire many events per second.
        Each filter rebuild zeroes the IIR state — repeated rebuilds
        during a fast drag would prevent the filter from settling
        and audibly leak the notched signal. Skip rebuilds where the
        width changes by less than 4%."""
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        w = max(self.NOTCH_WIDTH_MIN_HZ,
                min(self.NOTCH_WIDTH_MAX_HZ, float(new_width_hz)))
        if n.width_hz > 0 and abs(w - n.width_hz) / n.width_hz < 0.04:
            return False
        nf = self._make_notch_filter(n.abs_freq_hz, w, deep=n.deep)
        if nf is None:
            return False
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=w,
            active=n.active, deep=n.deep, filter=nf,
        )
        self.notches_changed.emit(self.notch_details)
        return True

    def set_notch_active_at(self, abs_freq_hz: float, active: bool,
                            tolerance_hz: float | None = None) -> bool:
        """Toggle one notch active/inactive without removing it. The
        DSP loop bypasses inactive notches; the spectrum overlay shows
        them in a grey/desaturated color so the operator can A/B
        whether the notch is helping."""
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        if n.active == bool(active):
            return True
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=n.width_hz,
            active=bool(active), deep=n.deep, filter=n.filter,
        )
        self.notches_changed.emit(self.notch_details)
        return True

    def toggle_notch_active_at(self, abs_freq_hz: float,
                               tolerance_hz: float | None = None) -> bool:
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        return self.set_notch_active_at(
            n.abs_freq_hz, not n.active, tolerance_hz)

    def set_notch_deep_at(self, abs_freq_hz: float, deep: bool,
                          tolerance_hz: float | None = None) -> bool:
        """Toggle one notch's cascade-depth mode. Rebuilds the
        underlying filter (since the cascade allocates a second
        biquad-state pair internally). Spectrum overlay renders deep
        notches with a thicker outline so the operator can see at a
        glance which notches are running cascaded."""
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        if n.deep == bool(deep):
            return True
        nf = self._make_notch_filter(
            n.abs_freq_hz, n.width_hz, deep=bool(deep))
        if nf is None:
            return False
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=n.width_hz,
            active=n.active, deep=bool(deep), filter=nf,
        )
        self.notches_changed.emit(self.notch_details)
        return True

    def toggle_notch_deep_at(self, abs_freq_hz: float,
                             tolerance_hz: float | None = None) -> bool:
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        return self.set_notch_deep_at(
            n.abs_freq_hz, not n.deep, tolerance_hz)

    def clear_notches(self):
        self._notches.clear()
        self.notches_changed.emit([])

    # ── TCI spots API ─────────────────────────────────────────────────
    @property
    def spots(self) -> list[dict]:
        return list(self._spots.values())

    def add_spot(self, callsign: str, mode: str, freq_hz: int,
                 color_argb: int = 0xFFFFD700, display: str | None = None):
        """Add or update a spot.

        `callsign` is the raw ham callsign (used as the key and sent back
        in TCI events). `display` is an optional label rendered in the
        panadapter (e.g., with a flag prefix). Defaults to `callsign`."""
        import time
        callsign = (callsign or "").strip()
        if not callsign:
            return
        self._spots[callsign] = {
            "call": callsign,
            "display": display if display else callsign,
            "mode": (mode or "").strip() or "USB",
            "freq_hz": int(freq_hz),
            "color": int(color_argb),
            "ts": time.monotonic(),
        }
        # LRU cap
        if len(self._spots) > self._max_spots:
            oldest = min(self._spots.items(), key=lambda kv: kv[1]["ts"])[0]
            del self._spots[oldest]
        self.spots_changed.emit(self.spots)

    def delete_spot(self, callsign: str):
        callsign = (callsign or "").strip()
        if callsign in self._spots:
            del self._spots[callsign]
            self.spots_changed.emit(self.spots)

    def clear_spots(self):
        if self._spots:
            self._spots.clear()
            self.spots_changed.emit([])

    # ── Spot list sizing (wired to Settings → Network/TCI → Spots) ──
    @property
    def max_spots(self) -> int:
        return self._max_spots

    def set_max_spots(self, n: int):
        # Hard cap at 100 — panadapter can't usefully display more without
        # becoming unreadable, especially on dense digital-mode bands.
        n = max(0, min(100, int(n)))
        self._max_spots = n
        # Trim existing spot dict if it's now over cap
        while len(self._spots) > self._max_spots:
            oldest = min(self._spots.items(), key=lambda kv: kv[1]["ts"])[0]
            del self._spots[oldest]
        self.spots_changed.emit(self.spots)

    @property
    def spot_lifetime_s(self) -> int:
        return self._spot_lifetime_s

    def set_spot_lifetime_s(self, seconds: int):
        """0 = never expire."""
        self._spot_lifetime_s = max(0, int(seconds))
        self.spot_lifetime_changed.emit(self._spot_lifetime_s)

    # ── Spot mode filter ─────────────────────────────────────────────
    # Renders only spots whose mode is in the CSV list (case-insensitive).
    # Empty = show all. "SSB" expands to match USB/LSB/SSB automatically.
    @property
    def spot_mode_filter_csv(self) -> str:
        return self._spot_mode_filter_csv

    def set_spot_mode_filter_csv(self, csv: str):
        self._spot_mode_filter_csv = (csv or "").strip()
        self.spot_mode_filter_changed.emit(self._spot_mode_filter_csv)

    # ── Visuals (palette + dB ranges) ────────────────────────────────
    @property
    def waterfall_palette(self) -> str:
        return self._waterfall_palette

    def set_waterfall_palette(self, name: str):
        # Canonicalize via the palettes module's alias table so older
        # palette-name strings (lowercase, "default", etc.) migrate to
        # the canonical names on load without the user having to
        # re-pick anything.
        from lyra.ui import palettes
        name = palettes.canonical_name(name)
        if name == self._waterfall_palette:
            return
        self._waterfall_palette = name
        self.waterfall_palette_changed.emit(name)

    @property
    def show_lyra_meteors(self) -> bool:
        return self._show_lyra_meteors

    def set_show_lyra_meteors(self, visible: bool) -> None:
        """Toggle occasional meteor streaks across the panadapter.
        Independent of the constellation watermark; persisted via
        QSettings."""
        v = bool(visible)
        if v == self._show_lyra_meteors:
            return
        self._show_lyra_meteors = v
        from PySide6.QtCore import QSettings as _QS
        _QS("N8SDR", "Lyra").setValue("visuals/show_lyra_meteors", v)
        self.lyra_meteors_changed.emit(v)

    @property
    def show_lyra_constellation(self) -> bool:
        return self._show_lyra_constellation

    def set_show_lyra_constellation(self, visible: bool) -> None:
        """Toggle the Lyra constellation watermark behind the panadapter
        trace. Persisted via QSettings; both spectrum widget backends
        listen for the change and repaint."""
        v = bool(visible)
        if v == self._show_lyra_constellation:
            return
        self._show_lyra_constellation = v
        from PySide6.QtCore import QSettings as _QS
        _QS("N8SDR", "Lyra").setValue("visuals/show_lyra_constellation", v)
        self.lyra_constellation_changed.emit(v)

    @property
    def show_spectrum_grid(self) -> bool:
        return self._show_spectrum_grid

    def set_show_spectrum_grid(self, visible: bool) -> None:
        """Toggle the panadapter grid (the 9×9 horiz/vert divisions).
        Persisted via QSettings; both spectrum widget backends listen
        for the change and repaint."""
        v = bool(visible)
        if v == self._show_spectrum_grid:
            return
        self._show_spectrum_grid = v
        from PySide6.QtCore import QSettings as _QS
        _QS("N8SDR", "Lyra").setValue("visuals/show_spectrum_grid", v)
        self.spectrum_grid_changed.emit(v)

    # ── Spectrum cal trim ──────────────────────────────────────────
    # Operator-adjustable per-rig calibration offset (dB) added to
    # every FFT bin before display. Use to compensate for known
    # pre-LNA losses (preselector, cable, antenna efficiency) or to
    # match the panadapter readings to a known reference signal.
    SPECTRUM_CAL_MIN_DB = -40.0
    SPECTRUM_CAL_MAX_DB = +40.0

    @property
    def spectrum_cal_db(self) -> float:
        return float(self._spectrum_cal_db)

    def set_spectrum_cal_db(self, db: float):
        v = max(self.SPECTRUM_CAL_MIN_DB,
                min(self.SPECTRUM_CAL_MAX_DB, float(db)))
        if abs(v - self._spectrum_cal_db) < 0.01:
            return
        self._spectrum_cal_db = v
        self.spectrum_cal_db_changed.emit(v)

    # ── S-meter cal trim ───────────────────────────────────────────
    SMETER_CAL_MIN_DB = -40.0
    SMETER_CAL_MAX_DB = +40.0

    @property
    def smeter_cal_db(self) -> float:
        return float(self._smeter_cal_db)

    def set_smeter_cal_db(self, db: float):
        v = max(self.SMETER_CAL_MIN_DB,
                min(self.SMETER_CAL_MAX_DB, float(db)))
        if abs(v - self._smeter_cal_db) < 0.01:
            return
        self._smeter_cal_db = v
        self.smeter_cal_db_changed.emit(v)

    # ── S-meter response mode (peak vs average) ─────────────────────
    SMETER_MODES = ("peak", "avg")

    @property
    def smeter_mode(self) -> str:
        return self._smeter_mode

    def set_smeter_mode(self, mode: str):
        """Switch the S-meter between 'peak' (instant max bin in
        passband — jumpy, responsive) and 'avg' (time-smoothed mean
        of passband bins in linear power — steady, AGC-friendly)."""
        m = mode if mode in self.SMETER_MODES else "peak"
        if m == self._smeter_mode:
            return
        # Reset the linear-power average when switching INTO avg mode
        # so the meter doesn't briefly show a stale value from last
        # time avg mode was active.
        if m == "avg":
            self._smeter_avg_lin = 0.0
        self._smeter_mode = m
        self.smeter_mode_changed.emit(m)

    def calibrate_smeter_to_dbm(self, target_dbm: float,
                                 current_meter_dbm: float):
        """One-click S-meter calibration: 'set the meter to read
        target_dbm given that it's currently reading current_meter_dbm
        for the same input signal.' Computes the offset adjustment and
        applies it on top of the existing cal.

        Example: operator injects a signal generator at -73 dBm but
        the meter shows -65 dBm → call calibrate_smeter_to_dbm(-73, -65)
        and the cal trim shifts by -8 dB so the next reading is -73."""
        delta = float(target_dbm) - float(current_meter_dbm)
        self.set_smeter_cal_db(self._smeter_cal_db + delta)

    @property
    def spectrum_db_range(self) -> tuple[float, float]:
        return (self._spectrum_min_db, self._spectrum_max_db)

    def set_spectrum_db_range(self, min_db: float, max_db: float,
                              from_user: bool = True):
        """Apply a new spectrum dB range.

        `from_user=True` (default) means a manual / interactive change
        (slider drag, reset button, Y-axis right-edge drag). The
        operator-supplied range becomes a CLAMP for the auto-scale
        loop — auto-fit is allowed to move the displayed range INSIDE
        these bounds but never outside. Auto-scale stays ON until the
        operator explicitly unchecks the auto-scale checkbox.

        This replaces an earlier "manual change → auto OFF" rule that
        caused operator pain: the right-edge Y-axis drag fires this
        on EVERY pixel of mouse motion, so even a 1-pixel jitter
        during a click flipped auto off. Now auto-scale is ONLY
        disabled by the explicit checkbox toggle.

        Internal calls from the auto-scale tick pass `from_user=False`
        — those update only the live display range, not the user
        bounds.
        """
        lo, hi = float(min_db), float(max_db)
        if hi - lo < 3.0:
            hi = lo + 3.0
        self._spectrum_min_db, self._spectrum_max_db = lo, hi
        if from_user:
            # Operator just set their preferred range — store as the
            # bounds within which auto-scale is allowed to move, AND
            # save them as this band's preferred bounds so switching
            # to another band and back restores them.
            self._user_range_min_db = lo
            self._user_range_max_db = hi
            self._save_current_band_range()
        self.spectrum_db_range_changed.emit(lo, hi)

    # ── Spectrum auto-scale ──────────────────────────────────────────
    AUTO_SCALE_INTERVAL_TICKS = 60   # ~2 sec at 30 fps; ~1 sec at 60 fps
    AUTO_SCALE_NOISE_HEADROOM_DB = 15.0   # margin BELOW noise floor
    AUTO_SCALE_PEAK_HEADROOM_DB  = 15.0   # margin ABOVE strongest signal
    # Rolling-max window so a momentary peak from the last few
    # seconds keeps the ceiling raised even after the transient
    # fades. Without this, a strong intermittent signal would have
    # peaks at the very top edge (or off-scale entirely) every time
    # the scale was recomputed between transients.
    AUTO_SCALE_PEAK_WINDOW_TICKS = 300    # ~10 sec at 30 fps
    AUTO_SCALE_MIN_SPAN_DB = 50.0         # never collapse below this

    @property
    def spectrum_auto_scale(self) -> bool:
        return self._spectrum_auto_scale

    def set_spectrum_auto_scale(self, on: bool):
        on = bool(on)
        if on == self._spectrum_auto_scale:
            return
        self._spectrum_auto_scale = on
        self._auto_scale_tick_counter = 0   # fire on next FFT tick
        self.spectrum_auto_scale_changed.emit(on)

    @property
    def waterfall_auto_scale(self) -> bool:
        """If True, the waterfall dB range mirrors the spectrum auto-
        scale on each tick. If False, the waterfall keeps the
        operator's manually set min/max regardless of band activity."""
        return self._waterfall_auto_scale

    def set_waterfall_auto_scale(self, on: bool):
        on = bool(on)
        if on == self._waterfall_auto_scale:
            return
        self._waterfall_auto_scale = on
        self.waterfall_auto_scale_changed.emit(on)

    @property
    def waterfall_db_range(self) -> tuple[float, float]:
        return (self._waterfall_min_db, self._waterfall_max_db)

    def set_waterfall_db_range(self, min_db: float, max_db: float):
        lo, hi = float(min_db), float(max_db)
        if hi - lo < 3.0:
            hi = lo + 3.0
        self._waterfall_min_db, self._waterfall_max_db = lo, hi
        self.waterfall_db_range_changed.emit(lo, hi)

    # ── Panadapter zoom ──────────────────────────────────────────────
    # Picks a centered subset of FFT bins before emitting spectrum_ready
    # so SpectrumWidget / WaterfallWidget magnify the middle of the
    # current RX span. No impact on the demod path — purely display.
    ZOOM_LEVELS = (1.0, 2.0, 4.0, 8.0, 16.0)

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float):
        z = max(1.0, min(32.0, float(zoom)))
        if abs(z - self._zoom) < 1e-6:
            return
        self._zoom = z
        self.zoom_changed.emit(z)

    def zoom_step(self, direction: int):
        """Step to the next / previous preset zoom level. `direction`
        is +1 (zoom in) or -1 (zoom out). Called by the spectrum
        wheel handler."""
        levels = list(self.ZOOM_LEVELS)
        # Find current position (snap to nearest preset)
        cur = min(range(len(levels)),
                  key=lambda i: abs(levels[i] - self._zoom))
        cur = max(0, min(len(levels) - 1, cur + direction))
        self.set_zoom(levels[cur])

    # ── Spectrum FPS ─────────────────────────────────────────────────
    @property
    def spectrum_fps(self) -> int:
        return int(round(1000.0 / max(1, self._fft_interval_ms)))

    def set_spectrum_fps(self, fps: int):
        fps = max(5, min(120, int(fps)))
        interval = int(round(1000.0 / fps))
        self._fft_interval_ms = interval
        # Live update the running timer (if it exists yet — __init__
        # order means set_spectrum_fps can be called from QSettings
        # load before _fft_timer is created).
        timer = getattr(self, "_fft_timer", None)
        if timer is not None:
            timer.setInterval(interval)
        self.spectrum_fps_changed.emit(fps)

    # ── Waterfall rate (divider) ─────────────────────────────────────
    @property
    def waterfall_divider(self) -> int:
        return self._waterfall_divider

    def set_waterfall_divider(self, n: int):
        n = max(1, min(20, int(n)))
        self._waterfall_divider = n
        self.waterfall_divider_changed.emit(n)

    @property
    def waterfall_multiplier(self) -> int:
        return self._waterfall_multiplier

    def set_waterfall_multiplier(self, m: int):
        """Push the same spectrum row multiple times per FFT tick for
        a fast-scroll effect. Range 1..10 (1=normal, 10=10× visual
        speed). Bumped from 8 to 10 after follow-up feedback that
        max was still not fast enough."""
        m = max(1, min(10, int(m)))
        self._waterfall_multiplier = m
        self.waterfall_multiplier_changed.emit(m)

    @staticmethod
    def parse_mode_filter_csv(csv: str) -> set[str]:
        """Convert user CSV (e.g. 'FT8, CW, SSB') → expanded uppercase
        set of allowed mode strings. 'SSB' → {'SSB','USB','LSB'}.
        Empty / whitespace-only input returns the empty set (= no filter)."""
        if not csv:
            return set()
        raw = [m.strip().upper() for m in csv.split(",") if m.strip()]
        expanded: set[str] = set()
        for m in raw:
            if m == "SSB":
                expanded.update(("SSB", "USB", "LSB"))
            else:
                expanded.add(m)
        return expanded

    def activate_spot_near(self, freq_hz: float, tolerance_hz: float = 500.0) -> bool:
        """Click-to-activate: find the nearest spot to `freq_hz` and
        fire spot_activated. Tune the radio there. Returns True on hit."""
        if not self._spots:
            return False
        best = min(self._spots.values(), key=lambda s: abs(s["freq_hz"] - freq_hz))
        if abs(best["freq_hz"] - freq_hz) > tolerance_hz:
            return False
        self.set_freq_hz(best["freq_hz"])
        self.spot_activated.emit(best["call"], best["mode"], best["freq_hz"])
        return True

    # Removed duplicate set_notch_q_at — superseded by
    # set_notch_width_at (Hz-based parameter, dataclass model).

    def set_audio_output(self, output: str):
        if output == self._audio_output:
            return
        # NOTE: an earlier version of this method force-dropped the
        # IQ rate to 48 kHz when switching INTO AK4951, on the
        # (mistaken) premise that AK4951 audio required 48 k IQ.
        # That was never true — AK4951 audio runs at 48 kHz
        # internally regardless of the IQ stream rate (HPSDR EP2
        # is its own 48 k audio slot, independent of EP6 IQ). The
        # corresponding logic in set_rate that auto-switched
        # AK4951 → PC at >48 k was already removed for the same
        # reason after the operator field-tested AK4951 at 192 k
        # IQ for an extended session. Removing it here too closes
        # the symmetric bug: flipping AK→PC→AK at 192 k IQ used
        # to drop the rate to 48 k on the way back into AK4951.
        # Now both sinks accept any IQ rate and the operator's
        # rate setting is sticky across output swaps.
        self._audio_output = output
        # Remember this choice as the user's preferred output for the
        # automatic fallback logic in set_rate (so if they later bump
        # rate above 48k we know to auto-restore AK4951 afterward).
        self._preferred_audio_output = output
        # Sink-swap cleanup. THREE things have to happen, in order,
        # to prevent the "digitized robotic" sound right after a
        # swap (caused by stale samples from the OLD sink leaking
        # into the NEW one):
        #   1. Close old sink — drains internal buffers (AK4951 also
        #      clears the HL2 stream's TX queue per its close()).
        #   2. Drop in-flight demod chunks (_audio_buf) that were
        #      queued for the old sink at potentially the wrong
        #      sample rate / format expectations.
        #   3. Build new sink. PortAudio close → reopen on the same
        #      physical device sometimes races; a tiny sleep gives
        #      Windows the moment it needs to release exclusive-use
        #      handles before we ask for them again.
        try:
            self._audio_sink.close()
        except Exception:
            pass
        self._rx_channel.reset()
        # 30 ms — long enough for PortAudio/WASAPI to fully release
        # the device handle, short enough to be imperceptible to the
        # operator. Tested across AK4951↔PC swaps with no recurrence
        # of the robotic-sound symptom.
        import time as _time
        _time.sleep(0.030)
        self._audio_sink = self._make_sink() if self._stream else NullSink()
        # New sink starts at default L/R (equal-power center) — push
        # the operator's current balance so the new sink picks up the
        # pan immediately, not on the next set_balance.
        self._push_balance_to_sink()
        self.audio_output_changed.emit(output)

    # ── Stream lifecycle ──────────────────────────────────────────────
    def start(self):
        if self._stream:
            return
        try:
            self._stream = HL2Stream(self._ip, sample_rate=self._rate)
            self._stream.start(
                on_samples=self._stream_cb,
                rx_freq_hz=self._freq_hz,
                lna_gain_db=self._gain_db,
            )
        except Exception as e:
            self.status_message.emit(f"Start failed: {e}", 5000)
            self._stream = None
            return
        self._audio_sink = self._make_sink()
        self._push_balance_to_sink()
        # Push the filter-board OC pattern now that the stream is live
        if self._filter_board_enabled:
            self._apply_oc_for_current_freq()
        # Start the ADC-peak broadcaster so the toolbar indicator lights up
        self._peak_report_timer.start()
        # Start polling HL2 hardware telemetry (temp/voltage) so the
        # banner readouts begin updating once the first EP6 frame
        # carrying the right C0 address arrives.
        self._hl2_telem_timer.start()
        self.stream_state_changed.emit(True)

    def stop(self):
        # Stop the peak broadcaster first so no more readings emit
        self._peak_report_timer.stop()
        # Stop the HL2 telemetry poll so the banner shows stale-then-NaN
        # rather than continuing to emit the last-seen reading forever.
        self._hl2_telem_timer.stop()
        try:
            self._audio_sink.close()
        except Exception:
            pass
        self._audio_sink = NullSink()
        # Drop the USB-BCD cable to a safe (zero) state when stopping
        if self._usb_bcd_cable is not None:
            try:
                self._usb_bcd_cable.write_byte(0)
            except Exception:
                pass
        if self._stream:
            self._stream.stop()
            self._stream = None
        with self._ring_lock:
            self._sample_ring.clear()
        self._rx_channel.reset()
        self._lna_peaks = []
        self._lna_rms = []
        self.stream_state_changed.emit(False)

    def discover(self):
        """Auto-discover an HL2 on any local network interface.
        On failure, suggest the diagnostic probe so the operator can
        see EXACTLY which interfaces were tried + what came back."""
        from lyra.protocol.discovery import discover
        log: list[str] = []
        radios = discover(timeout_s=1.0, attempts=2, debug_log=log)
        # Always print the discovery log to console so tester reports
        # can include the lines without needing to re-run via the
        # probe dialog.
        for line in log:
            print(f"[discover] {line}")
        if not radios:
            self.status_message.emit(
                "No radios found. Try Help → Network Discovery Probe "
                "for details, or enter the IP manually in Settings → Radio.",
                8000)
            return
        r = radios[0]
        self.set_ip(r.ip)
        self.status_message.emit(
            f"Found {r.board_name} at {r.ip}  "
            f"gateware v{r.code_version}.{r.beta_version}",
            5000,
        )

    # ── Internal: sample flow ─────────────────────────────────────────
    def _stream_cb(self, samples, _stats):
        """RX-thread callback. Accumulate into a batch; bridge when full."""
        with self._rx_batch_lock:
            self._rx_batch.extend(samples.tolist())
            if len(self._rx_batch) >= self._rx_batch_size:
                batch = np.asarray(self._rx_batch, dtype=np.complex64)
                self._rx_batch = []
            else:
                return
        self._bridge.samples_ready.emit(batch)

    def _on_samples_main_thread(self, samples):
        with self._ring_lock:
            self._sample_ring.extend(samples)
        # Track IQ peak AND RMS magnitude for Auto-LNA + toolbar readout.
        # Peak captures transients (good for clipping detection), RMS
        # tracks steady-state signal energy (good for level linearity
        # diagnostics — responds predictably to LNA gain changes).
        # Cheap to compute per block; history size clamped.
        if len(samples) > 0:
            mag_sq = (samples.real * samples.real
                      + samples.imag * samples.imag)
            peak = float(np.sqrt(np.max(mag_sq)))
            rms = float(np.sqrt(np.mean(mag_sq)))
            self._lna_peaks.append(peak)
            self._lna_rms.append(rms)
            if len(self._lna_peaks) > self._lna_peaks_max:
                self._lna_peaks.pop(0)
            if len(self._lna_rms) > self._lna_peaks_max:
                self._lna_rms.pop(0)
        self._do_demod(samples)

    def _do_demod(self, iq):
        """Route IQ through the RX channel, then apply AGC + volume,
        then send to the audio sink.

        Phase 2 refactor: the channel (lyra/dsp/channel.py) owns the
        decimation, notch chain, demods, and NR. AGC + final volume
        staging stay here since they're routing-side concerns. The
        channel returns 48 kHz audio for any complete demod blocks
        ready in its buffer; empty array if no full block yet.

        WDSP integration path: when WdspChannel(DspChannel) lands,
        this function doesn't change at all. Channel returns audio,
        AGC + sink runs the same way."""
        mode = self._mode
        if mode == "Off":
            return
        if mode == "Tone":
            self._emit_tone(len(iq))
            return

        # Push current notch state to channel each call (cheap; the
        # channel just stores the references). We do this here rather
        # than in every notch-mutation site so the channel always sees
        # fresh state without us tracking 8+ call sites.
        self._rx_channel.set_notches(self._notches, self._notch_enabled)

        try:
            audio = self._rx_channel.process(iq)
        except Exception as e:
            print(f"channel error: {e}")
            return
        if audio.size == 0:
            return
        try:
            audio = self._apply_agc_and_volume(audio)
            self._audio_sink.write(audio)
        except Exception as e:
            print(f"audio sink error: {e}")

    def _emit_tone(self, n: int):
        # n is the size of the incoming IQ block at self._rate. The
        # audio sink runs at AUDIO_RATE (48 kHz) regardless of IQ
        # rate — _do_demod's normal path goes through the channel
        # which decimates IQ→48k. Tone needs to match: generate
        # samples at 48 kHz and at the audio block size, NOT the
        # IQ block size.
        #
        # The original code used self._rate as both the sample rate
        # AND the block size, which produced 4× too many samples at
        # 192 kHz IQ and 8× too many at 384 kHz. The over-sized
        # write would queue up in the audio sink faster than it
        # could drain, eventually backpressuring the GUI thread →
        # hard "Not Responding" hang. Several operators hit this.
        AUDIO_RATE = 48000
        iq_rate = max(1, self._rate)
        audio_n = max(1, int(n) * AUDIO_RATE // iq_rate)
        t = (np.arange(audio_n) + self._tone_phase) / float(AUDIO_RATE)
        audio = (0.3 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        self._tone_phase = (self._tone_phase + audio_n) % AUDIO_RATE
        # Tone uses AF Gain + Volume so operator's listening level
        # stays consistent when switching to Mode → Tone for rig
        # testing. Run through tanh as a safety limiter — without
        # it, a high AF Gain (e.g. +35 dB = 56× linear) on the
        # 0.3-amplitude sine would write peak ≈ 17 to the sink,
        # which clips hard and on some sinks throws / blocks.
        af = self.af_gain_linear
        vol = 0.0 if self._muted else self._volume
        audio = np.tanh(audio * af * vol).astype(np.float32)
        try:
            self._audio_sink.write(audio)
        except Exception:
            pass

    def _apply_agc_and_volume(self, audio):
        # Chain: audio → AF Gain (pre-AGC makeup) → AGC → Volume → tanh
        #
        # AF Gain sits BEFORE AGC for two critical reasons:
        #   1. When AGC is ON, it normalizes to target regardless of
        #      AF Gain — so AF just feeds more signal into AGC, which
        #      needs to do less work. Output level stays at target.
        #      This prevents the "AF + AGC stack and clip" bug.
        #   2. When AGC is OFF (FT8/FT4/digital modes where pumping
        #      is unwanted), AF Gain is the manual makeup gain — the
        #      only way to bring weak signals up to audible.
        #
        # Net effect: switching AGC on ↔ off produces only a slight
        # loudness delta (the expected SDR-client behaviour). Vol slider has a
        # useful full range in both AGC-on and AGC-off modes.
        #
        # Mute multiplies final gain by 0 — keeps everything else
        # (AGC state, noise-floor tracking, meter feeds) running so
        # unmuting doesn't cause a glitch.
        vol = 0.0 if self._muted else self._volume
        af = self.af_gain_linear
        # Apply AF Gain first — same for both AGC paths.
        audio = audio * af
        if self._agc_profile == "off":
            # AGC disabled — AF Gain + Volume scale the raw demod
            # output. Critical for digital modes (FT8/FT4/RTTY)
            # where operators intentionally run AGC off.
            #
            # Apply a fixed +14 dB makeup gain to compensate for
            # the absence of AGC's automatic amplification stage.
            # Field test on AM/SSB with weak signals showed demod
            # outputs around -94 dBFS, where AGC ON's typical gain
            # (~5x = +14 dB) made signals audible while AGC OFF at
            # the same AF+Vol was 14 dB quieter. Matching the
            # "only a slight loudness delta" promise from the chain
            # design comment requires this constant compensation —
            # without it, operators have to re-dial AF and Vol every
            # time they toggle AGC. tanh limiter still prevents
            # over-driving the sink on strong signals; digital-mode
            # operators feeding a decoder can simply lower AF Gain
            # by ~14 dB to get the previous (uncompensated) level.
            AGC_OFF_MAKEUP = 5.0119   # 10 ** (14/20) — +14 dB linear
            out = audio * AGC_OFF_MAKEUP * vol
            return np.tanh(out).astype(np.float32)

        block_peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        # Track a rolling noise-floor baseline: the minimum block peak
        # we've seen in the last ~3 seconds. Used by auto-threshold.
        self._noise_history.append(block_peak)
        if len(self._noise_history) > self._noise_history_max:
            self._noise_history.pop(0)
        if self._noise_history:
            self._noise_baseline = min(self._noise_history)

        if block_peak > self._agc_peak:
            self._agc_peak = block_peak   # instant attack
            self._agc_hang_counter = self._agc_hang_blocks
        elif self._agc_hang_counter > 0:
            self._agc_hang_counter -= 1
        else:
            self._agc_peak *= (1.0 - self._agc_release)
        self._agc_peak = max(self._agc_peak, 1e-4)

        # AGC max gain cap. Was previously 10× (20 dB), which was far
        # too conservative — any signal below ~-30 dBFS couldn't be
        # boosted to audible levels. Professional SDR clients give
        # 80-120 dB of AGC range (typical commercial SDR / rig). We
        # use 1000× (60 dB) as a safe middle ground: lets weak
        # signals down to ~-70 dBFS reach audible levels, but the
        # final tanh limiter still prevents any amplitude damage at
        # the speaker. Strong signals aren't affected — they hit
        # target well before the cap matters.
        AGC_MAX_GAIN = 1000.0   # 60 dB maximum AGC gain
        agc_gain = min(self._agc_target / self._agc_peak, AGC_MAX_GAIN)
        # Report the current AGC action to meters / diagnostics
        try:
            action_db = 20.0 * np.log10(max(agc_gain, 1e-6))
            self.agc_action_db.emit(float(action_db))
        except Exception:
            pass
        # Final: audio-already-AF-scaled × AGC × Volume → tanh
        # (AF Gain was applied BEFORE the AGC tracker above.)
        audio = audio * agc_gain * vol
        return np.tanh(audio).astype(np.float32)

    # ── AGC profile API ───────────────────────────────────────────────
    @property
    def agc_profile(self) -> str:
        return self._agc_profile

    @property
    def agc_release(self) -> float:
        return self._agc_release

    @property
    def agc_hang_blocks(self) -> int:
        return self._agc_hang_blocks

    def set_agc_profile(self, name: str):
        name = name.lower().strip()
        if name not in (*self.AGC_PRESETS, "custom"):
            name = "med"
        self._agc_profile = name
        if name != "custom":
            self._apply_agc_preset(name)
        # Auto-track the threshold only in "auto" profile; everything else
        # leaves the threshold where the user put it.
        if name == "auto":
            # Kick an immediate calibration so the threshold snaps to the
            # current noise floor rather than waiting a tick.
            self.auto_set_agc_threshold()
            if not self._agc_auto_timer.isActive():
                self._agc_auto_timer.start()
        else:
            if self._agc_auto_timer.isActive():
                self._agc_auto_timer.stop()
        self.agc_profile_changed.emit(name)

    def set_agc_custom(self, release: float, hang_blocks: int):
        """Set AGC params and switch profile to 'custom'."""
        self._agc_release = max(0.0, min(0.1, float(release)))
        self._agc_hang_blocks = max(0, min(200, int(hang_blocks)))
        self._agc_profile = "custom"
        self.agc_profile_changed.emit("custom")

    def _apply_agc_preset(self, name: str):
        params = self.AGC_PRESETS.get(name)
        if params is None:
            return
        self._agc_release = params["release"]
        self._agc_hang_blocks = params["hang_blocks"]

    # ── CW pitch ─────────────────────────────────────────────────────
    @property
    def cw_pitch_hz(self) -> int:
        return int(self._cw_pitch_hz)

    @property
    def cw_zero_offset_hz(self) -> int:
        """Where to draw the CW Zero (white) reference line, as a Hz
        offset from the VFO marker. This is the filter center — i.e.,
        where a clicked CW signal lands in the spectrum and where the
        audio is generated from.

          CWU: +pitch  (filter / signal sit RIGHT of the marker)
          CWL: -pitch  (filter / signal sit LEFT of the marker)
          else: 0      (line is hidden in non-CW modes)

        The panadapter is in sky-freq convention (display-side mirror
        flip applied in _tick_fft), so CWU appears RIGHT of marker
        like USB. The HL2 baseband mirror is handled inside CWDemod.
        """
        if self._mode == "CWU":
            return +int(self._cw_pitch_hz)
        if self._mode == "CWL":
            return -int(self._cw_pitch_hz)
        return 0

    def _emit_cw_zero(self) -> None:
        self.cw_zero_offset_changed.emit(int(self.cw_zero_offset_hz))

    def set_cw_pitch_hz(self, pitch: int) -> None:
        """Set the CW pitch tone in Hz (clamped to 200..1500). Updates:
          - The stored value (persisted to QSettings)
          - The CWDemod instances (rebuilt at the new pitch)
          - The passband overlay (re-emit with new offset)
          - The CW Zero line position (white reference line)
          - The cw_pitch_changed signal for any listeners
        Operator-driven; typical preference range 400-800 Hz."""
        new_pitch = int(max(200, min(1500, int(pitch))))
        if new_pitch == self._cw_pitch_hz:
            return
        self._cw_pitch_hz = new_pitch
        from PySide6.QtCore import QSettings as _QS
        _QS("N8SDR", "Lyra").setValue("dsp/cw_pitch_hz", new_pitch)
        # Channel rebuilds CWU/CWL demods at the new pitch internally.
        self._rx_channel.set_cw_pitch_hz(float(new_pitch))
        # Recompute + re-emit passband so the panadapter overlay
        # shifts to the new CW position immediately.
        self._emit_passband()
        self.cw_pitch_changed.emit(new_pitch)
        self._emit_cw_zero()

    # ── AGC threshold (target audio level) ───────────────────────────
    @property
    def agc_threshold(self) -> float:
        return self._agc_target

    def set_agc_threshold(self, threshold: float):
        """Set AGC target audio level. Range 0.05..0.9 — the peak level
        AGC tries to hold the audio at. Higher = AGC kicks in at louder
        signals (less responsive); lower = AGC reacts to weaker signals
        (more sensitive)."""
        self._agc_target = max(0.05, min(0.9, float(threshold)))
        self.agc_threshold_changed.emit(self._agc_target)

    def auto_set_agc_threshold(self, margin_db: float = 18.0) -> float:
        """Calibrate AGC threshold to sit `margin_db` above the current
        rolling noise floor. Bound to the AGC right-click "Auto" action.
        Returns the new threshold value."""
        baseline = max(self._noise_baseline, 1e-4)
        factor = 10 ** (margin_db / 20.0)
        target = max(0.05, min(0.9, baseline * factor))
        self.set_agc_threshold(target)
        self.status_message.emit(
            f"AGC auto-threshold: {20*np.log10(target):+.0f} dBFS "
            f"(noise floor {20*np.log10(baseline):+.0f} + {margin_db:.0f} dB)",
            3000)
        return target

    # Note: _decimate_to_48k and _rebuild_demods used to live here.
    # Both are now owned by self._rx_channel (lyra/dsp/channel.py),
    # which is the WDSP integration seam. Radio configures the channel
    # via setters and feeds IQ into self._rx_channel.process(iq).

    def _make_notch_filter(self, abs_freq_hz: float,
                           width_hz: float,
                           deep: bool = False) -> NotchFilter | None:
        """Design one notch filter at the given sky frequency with the
        given -3 dB bandwidth. The DSP pipeline runs at a fixed
        48 kHz (decimation happens before notching) — coefficients
        always designed for 48 kHz regardless of the RX sample rate.

        Two regimes:
        - **Near-DC** (offset < width/2 + 10 Hz): use the high-pass
          DC-blocker mode of NotchFilter. iirnotch can't catch DC
          because its center frequency must be > 0 — its bandwidth
          collapses to zero as freq → 0. The high-pass kills the
          carrier and matches the visible "kill region" of width Hz.
        - **Off-DC**: standard iirnotch centered at the offset, with
          bandwidth = width Hz. Right tool for FT8 tones, RTTY pairs,
          heterodynes, etc.

        `deep=True` cascades the resulting filter twice for ~2× dB
        attenuation on stubborn carriers.
        """
        NOTCH_RATE = 48000
        offset = abs_freq_hz - self._freq_hz
        max_off = NOTCH_RATE / 2 - 100
        offset = max(-max_off, min(max_off, offset))
        eff_freq = abs(offset)
        # If the visible notch extent (freq ± width/2) crosses DC,
        # iirnotch can't model it accurately. Switch to the
        # DC-blocker path so the actual filter shape matches the
        # rectangle the operator sees on the spectrum.
        try:
            if eff_freq < (width_hz * 0.5 + 10.0):
                return NotchFilter(NOTCH_RATE, eff_freq, width_hz,
                                   dc_blocker=True, deep=deep)
            return NotchFilter(NOTCH_RATE, eff_freq, width_hz,
                               deep=deep)
        except Exception as e:
            self.status_message.emit(f"Notch error: {e}", 3000)
            return None

    @property
    def pc_audio_device_index(self):
        return self._pc_audio_device_index

    def set_pc_audio_device_index(self, device):
        """Set the PortAudio device index for the PC Soundcard sink.
        None = auto (WASAPI default). Triggers a sink rebuild if PC
        Soundcard is currently active so the change takes effect
        immediately."""
        new_dev = None if device is None else int(device)
        if new_dev == self._pc_audio_device_index:
            return
        self._pc_audio_device_index = new_dev
        self.pc_audio_device_changed.emit(new_dev)
        # If PC Soundcard is the active sink, rebuild it so the new
        # device choice takes effect right away. Same swap-cleanup
        # treatment as set_audio_output.
        if self._audio_output != "AK4951" and self._stream:
            try:
                self._audio_sink.close()
            except Exception:
                pass
            self._rx_channel.reset()
            import time as _time
            _time.sleep(0.030)
            self._audio_sink = self._make_sink()
            self._push_balance_to_sink()

    def _make_sink(self):
        if self._audio_output == "AK4951":
            return AK4951Sink(self._stream)
        try:
            return SoundDeviceSink(
                rate=48000, device=self._pc_audio_device_index)
        except Exception as e:
            self.status_message.emit(f"Audio output error: {e}", 6000)
            return NullSink()

    # ── FFT tick → spectrum + S-meter signals ─────────────────────────
    def _tick_fft(self):
        with self._ring_lock:
            if len(self._sample_ring) < self._fft_size:
                return
            arr = np.fromiter(self._sample_ring, dtype=np.complex64,
                              count=len(self._sample_ring))
        seg = arr[-self._fft_size:] * self._window
        f = np.fft.fftshift(np.fft.fft(seg))
        # HL2 baseband is spectrum-mirrored relative to sky frequency:
        # signals above the LO show up at NEGATIVE baseband bins, not
        # positive. The SSBDemod path handles this with its own sign
        # flip for audio. For DISPLAY we un-mirror here so the
        # panadapter shows USB signals to the RIGHT of the carrier
        # (above LO) and LSB signals to the LEFT (below LO), matching
        # the sky-frequency convention every other SDR UI uses. This
        # also makes click-to-tune, notch placement, spot markers,
        # and the RX filter passband overlay all agree visually.
        f = f[::-1]
        # 10·log10(|X|²/N²·CG²)  —  windowed-FFT dBFS, plus the
        # operator's per-rig cal trim. Float32 throughout to keep
        # the ~6 Hz FFT loop cheap.
        spec_db = (10.0 * np.log10((np.abs(f) ** 2) / self._win_norm + 1e-20)
                   + self._spectrum_cal_db)

        # S-meter uses the full (un-zoomed) spectrum — it must measure
        # the tuned signal regardless of display zoom. Bins are now in
        # sky-frequency order after the un-mirror flip above, but the
        # center bin position is unchanged so the ±3 kHz window still
        # captures the tuned signal correctly.
        center_bin = self._fft_size // 2
        half_bw_bins = int(3000 / (self._rate / self._fft_size))
        lo = max(0, center_bin - half_bw_bins)
        hi = min(self._fft_size, center_bin + half_bw_bins)
        if hi > lo:
            # Compute both metrics; emit the one matching current mode.
            #
            # Spectrum cal is already baked into spec_db at FFT time;
            # smeter cal is added below so the operator can shift the
            # meter without touching the spectrum scale.
            band = spec_db[lo:hi]
            # Both modes report INTEGRATED power across the passband
            # (sum of linear power per bin, not per-bin mean and not
            # max single bin). That's what an S-meter conventionally
            # measures — total signal energy in the RX BW.
            #
            # Difference between the modes is the temporal response:
            #   "peak"  — instantaneous integrated power, no smoothing
            #             (jumpier, follows transient signal energy)
            #   "avg"   — EWMA-smoothed integrated power (~1 s at 5 fps)
            #             so the meter feels stable rather than twitchy
            #
            # Earlier the modes diverged in shape (peak = single bin
            # max, avg = per-bin mean). Both produced readings ~18-30
            # dB below Thetis on the same antenna/signal. Now they're
            # both in the right ballpark; cal trim covers the residual
            # absolute offset.
            lin = 10.0 ** (band / 10.0)            # dB → linear power
            total_lin = float(np.sum(lin))
            # Cache passband peak (max bin in dBFS) for Auto-LNA
            # pull-up's passband-signal gate. Use the unsmoothed
            # spectrum so the gate reacts to real signal arrivals
            # within one FFT (~150 ms) rather than waiting for
            # smeter EWMA to catch up.
            self._lna_passband_peak_dbfs = float(np.max(band))
            # LNA-invariant S-meter: subtract the current LNA gain
            # from the dBFS reading so the displayed dBm reflects the
            # signal level AT THE ANTENNA, not at the ADC. Without
            # this, moving the LNA slider changes the meter reading
            # one-for-one even though no signal at the antenna has
            # changed. Matches the standard SDR-client convention
            # (Thetis, SDR# etc.) — calibrate once, the meter holds
            # across LNA changes. The +21 dB default cal in
            # _smeter_cal_db was originally tuned at LNA=+7, so
            # post-this-change the equivalent is +28 dB; new default
            # bumped accordingly.
            if self._smeter_mode == "avg":
                if self._smeter_avg_lin <= 0.0:
                    self._smeter_avg_lin = total_lin
                else:
                    self._smeter_avg_lin = (0.80 * self._smeter_avg_lin
                                            + 0.20 * total_lin)
                level_db = (10.0 * float(np.log10(max(self._smeter_avg_lin, 1e-20)))
                            + self._smeter_cal_db
                            - float(self._gain_db))
            else:  # "peak" — instantaneous integrated power
                level_db = (10.0 * float(np.log10(max(total_lin, 1e-20)))
                            + self._smeter_cal_db
                            - float(self._gain_db))
            self.smeter_level.emit(level_db)

        # Noise-floor estimate — 20th percentile rejects the upper 80%
        # of bins (which likely contain signals), leaving the ambient
        # noise. Rolling-averaged over ~1 s to damp out FFT-to-FFT
        # jitter. Emitted at ~6 Hz rather than every tick.
        if self._noise_floor_enabled:
            pct20 = float(np.percentile(spec_db, 20))
            self._noise_floor_history.append(pct20)
            if len(self._noise_floor_history) > self._noise_floor_history_max:
                self._noise_floor_history.pop(0)
            avg = float(np.mean(self._noise_floor_history))
            # Exponential smoothing on top of the rolling average for
            # extra stability — reference-line should feel rock-steady.
            if self._noise_floor_db is None:
                self._noise_floor_db = avg
            else:
                self._noise_floor_db = 0.85 * self._noise_floor_db + 0.15 * avg
            self._nf_emit_counter += 1
            if self._nf_emit_counter >= 5:
                self._nf_emit_counter = 0
                self.noise_floor_changed.emit(float(self._noise_floor_db))

        # Spectrum auto range scaling. Every AUTO_SCALE_INTERVAL_TICKS,
        # rebuild the dB range to:
        #   low edge  = noise_floor − 15 dB
        #   high edge = (rolling max of peaks over ~10 sec) + 15 dB
        #   guarantee at least AUTO_SCALE_MIN_SPAN_DB total span
        # Operator's manual drag turns the auto flag off (handled
        # in set_spectrum_db_range, from_user=True path).
        #
        # Rolling-max design rationale: a single-frame max kept the
        # scale chasing transients — strong intermittent signals
        # would briefly spike above the recently-fitted top, then
        # the next auto-fit would catch up. With a 10-sec rolling
        # window, recent spikes "stick" to the ceiling until they
        # age out, eliminating the off-scale-then-catch-up cycle.
        if self._spectrum_auto_scale:
            # Track per-tick peak so we have a rolling history.
            self._auto_scale_peak_history.append(float(np.max(spec_db)))
            if len(self._auto_scale_peak_history) > self.AUTO_SCALE_PEAK_WINDOW_TICKS:
                self._auto_scale_peak_history.pop(0)
            self._auto_scale_tick_counter += 1
            if self._auto_scale_tick_counter >= self.AUTO_SCALE_INTERVAL_TICKS:
                self._auto_scale_tick_counter = 0
                # Use noise_floor_db if we've been computing it; else
                # fall back to the 20th percentile of the current FFT.
                if self._noise_floor_db is not None:
                    nf = float(self._noise_floor_db)
                else:
                    nf = float(np.percentile(spec_db, 20))
                # Rolling max — the strongest peak in the last
                # ~10 seconds, NOT just the current frame.
                pk_max = max(self._auto_scale_peak_history)
                target_lo = nf - self.AUTO_SCALE_NOISE_HEADROOM_DB
                target_hi = pk_max + self.AUTO_SCALE_PEAK_HEADROOM_DB
                # Guarantee a comfortably wide scale even on bands
                # with vanishingly small dynamic range (very weak
                # signals on a quiet noise floor) — without this,
                # the auto-fit could produce a 10-15 dB display
                # span that left no room for stronger signals to
                # appear above the current peaks.
                if target_hi - target_lo < self.AUTO_SCALE_MIN_SPAN_DB:
                    target_hi = target_lo + self.AUTO_SCALE_MIN_SPAN_DB
                # No user-range clamp on auto-scale.
                #
                # An earlier design used self._user_range_min/max as a
                # CLAMP that auto-scale couldn't escape — meant to honor
                # an operator who'd narrowed the display to a specific
                # window. In practice, an accidental Y-axis drag would
                # pinch the bounds (e.g. -121..-109, a 12 dB window)
                # and auto-scale could never expand again, so on a band
                # with strong signals the trace clipped against the top
                # and looked broken ("upside down" was the user's
                # description). Now auto-scale always finds a range
                # that fits the actual signals; an operator who really
                # wants a narrow window just turns auto-scale off via
                # the Settings checkbox to lock it. Final safety clamp
                # below still guards against pathological values.
                target_lo = max(-150.0, min(-3.0, target_lo))
                target_hi = max(target_lo + 3.0, min(0.0, target_hi))
                # Internal call — `from_user=False` updates only the
                # live display range, NOT the user bounds.
                self.set_spectrum_db_range(
                    target_lo, target_hi, from_user=False)
                # Mirror the same range to the waterfall so its
                # heatmap fits the band's actual dynamic range too —
                # but ONLY if the operator has waterfall auto-scale
                # enabled (default). Some operators prefer a fixed
                # darker waterfall so weaker signals 'pop' against a
                # near-black background; turning waterfall auto-scale
                # off in Settings lets them keep that look while the
                # spectrum still auto-fits.
                if self._waterfall_auto_scale:
                    self.set_waterfall_db_range(target_lo, target_hi)
        elif self._auto_scale_peak_history:
            # Auto turned off — drop the history so it doesn't grow
            # unbounded if the operator never re-enables.
            self._auto_scale_peak_history = []

        # Zoom = crop to centered subset of bins. Widgets infer span
        # from the `effective_rate` we report here, so their freq axis
        # scales automatically.
        if self._zoom > 1.0:
            total = spec_db.shape[0]
            keep = max(64, int(total / self._zoom))
            lo_b = (total - keep) // 2
            spec_out = spec_db[lo_b:lo_b + keep]
            eff_rate = int(self._rate / self._zoom)
        else:
            spec_out = spec_db
            eff_rate = int(self._rate)

        self.spectrum_ready.emit(spec_out, float(self._freq_hz), eff_rate)

        # Waterfall fires on its own cadence (1 row per N FFT ticks)
        # and can burst M rows per push for fast-scroll mode.
        #
        # Multi-row push: if waterfall_multiplier > 1 we used to emit
        # the SAME spec_out M times, which produced visible vertical
        # blockiness — each group of M identical rows rendered as a
        # solid stripe. Now we LINEARLY INTERPOLATE between the
        # previous emitted spectrum and the current one, so the M
        # rows form a smooth gradient. Looks like real fast-scroll
        # at the same CPU cost.
        self._waterfall_tick_counter += 1
        if self._waterfall_tick_counter >= self._waterfall_divider:
            self._waterfall_tick_counter = 0
            mult = self._waterfall_multiplier
            prev = getattr(self, "_wf_prev_spec", None)
            if mult <= 1 or prev is None or prev.shape != spec_out.shape:
                # Cold path / first frame after rate change: just emit
                # the current spectrum once (mult==1) or M times (no
                # previous frame to interp from yet).
                for _ in range(mult):
                    self.waterfall_ready.emit(
                        spec_out, float(self._freq_hz), eff_rate)
            else:
                # Hot path: emit M interpolated frames spanning
                # prev → spec_out. The kth (1-based) frame is
                # prev*(1 - k/M) + spec_out*(k/M), so the LAST
                # frame is the actual current spectrum and the
                # earlier frames bridge the gap from prev.
                for k in range(1, mult + 1):
                    t = k / mult
                    frame = (prev * (1.0 - t) + spec_out * t).astype(
                        spec_out.dtype, copy=False)
                    self.waterfall_ready.emit(
                        frame, float(self._freq_hz), eff_rate)
            # Snapshot for next tick's interpolation. Use a copy so
            # the consumer side doesn't see future mutations.
            self._wf_prev_spec = np.array(spec_out, copy=True)
