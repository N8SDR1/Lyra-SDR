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

    Width-based model (matches Thetis / ExpertSDR3 mental model):
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


class _Decimator:
    """Stateful complex-signal decimator. Low-pass FIR + downsample with
    persistent filter state so blocks joined back-to-back have no FIR
    startup transient at the block boundary.
    """

    def __init__(self, rate_in: int, rate_out: int, taps: int = 257):
        from scipy.signal import firwin
        self.decim = rate_in // rate_out
        # Anti-alias cutoff at 90% of output Nyquist
        cutoff = (rate_out / 2.0) * 0.90
        self.taps = firwin(taps, cutoff, fs=rate_in, window="hann").astype(np.float64)
        self.state_i = np.zeros(taps - 1, dtype=np.float64)
        self.state_q = np.zeros(taps - 1, dtype=np.float64)
        self._phase = 0   # offset for decimation stride across block boundaries

    def process(self, iq: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter
        i_out, self.state_i = lfilter(self.taps, 1.0, iq.real, zi=self.state_i)
        q_out, self.state_q = lfilter(self.taps, 1.0, iq.imag, zi=self.state_q)
        # Keep every `decim`-th sample, starting at the carried-over phase.
        start = (-self._phase) % self.decim
        i_dec = i_out[start::self.decim]
        q_dec = q_out[start::self.decim]
        consumed_to_end = len(i_out) - start
        self._phase = (self._phase + consumed_to_end) % self.decim
        return (i_dec + 1j * q_dec).astype(np.complex64)


class Radio(QObject):
    # ── State change signals (UI subscribes) ───────────────────────────
    stream_state_changed = Signal(bool)
    freq_changed         = Signal(int)
    rate_changed         = Signal(int)
    mode_changed         = Signal(str)
    gain_changed         = Signal(int)
    volume_changed       = Signal(float)
    af_gain_db_changed   = Signal(int)   # AF makeup gain, 0..+50 dB
    rx_bw_changed        = Signal(str, int)       # mode, Hz
    tx_bw_changed        = Signal(str, int)
    bw_lock_changed      = Signal(bool)
    notches_changed      = Signal(list)           # list[Notch] (see dataclass above)
    notch_enabled_changed = Signal(bool)
    notch_default_width_changed = Signal(float)   # default width for new notches, in Hz
    audio_output_changed = Signal(str)
    ip_changed           = Signal(str)

    # ── Streaming data signals ─────────────────────────────────────────
    spectrum_ready       = Signal(object, float, int)   # db, center_hz, rate
    smeter_level         = Signal(float)
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
    spectrum_db_range_changed  = Signal(float, float)  # (min_db, max_db)
    waterfall_db_range_changed = Signal(float, float)  # (min_db, max_db)
    # RX filter passband (for panadapter overlay) — (low_offset_hz, high_offset_hz)
    # relative to the tuned center frequency. Recomputed whenever mode or
    # RX BW changes so the widget can draw the translucent passband rect.
    passband_changed = Signal(int, int)    # (low_offset_hz, high_offset_hz)

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
    # the RX passband. Not the reference client's "blobs" (different name because
    # ours only shows inside the filter window + is user-toggleable).
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
    AGC_PRESETS: dict[str, dict] = {
        "off":    {"release": 0.0,   "hang_blocks": 0},   # disabled
        "fast":   {"release": 0.020, "hang_blocks": 3},
        "med":    {"release": 0.005, "hang_blocks": 12},
        "slow":   {"release": 0.001, "hang_blocks": 46},
        "auto":   {"release": 0.005, "hang_blocks": 12},  # med + track
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
        self._af_gain_db = 0                    # integer dB, 0..+50
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
        self._rx_bw_by_mode = dict(self.BW_DEFAULTS)
        self._tx_bw_by_mode = dict(self.BW_DEFAULTS)
        self._bw_locked = False
        self._audio_output = "AK4951"

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
        self._audio_buf: list = []
        self._audio_block = 2048
        self._tone_phase = 0.0
        # Stateful decimator for RX rates > 48 k. Built lazily on first use.
        self._decimator = None

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
        #   0.0316(-30 dBFS) current — matches Thetis's typical target;
        #                    AGC does less aggressive work, preserves
        #                    dynamic range better, on/off delta drops
        #                    to ~8-10 dB (the "Thetis slight feel"
        #                    operators expect)
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

        # Auto-LNA control loop — slow cadence (1.5 s) so we don't
        # chase transient peaks. Only ticks when lna_auto is True.
        self._lna_auto_timer = _QTimer(self)
        self._lna_auto_timer.setInterval(1500)
        self._lna_auto_timer.timeout.connect(self._adjust_lna_auto)

        # ADC peak reporter — emits lna_peak_dbfs at ~4 Hz so the UI
        # can show a live dBFS indicator regardless of whether Auto-
        # LNA is engaged. Operator uses this to diagnose RF-chain
        # health: clipping, too hot, sweet spot, or too cold.
        self._peak_report_timer = _QTimer(self)
        self._peak_report_timer.setInterval(250)
        self._peak_report_timer.timeout.connect(self._emit_peak_reading)
        # Started when stream starts, stopped when stream stops.

        # Notch bank — list of Notch dataclasses (see top of file).
        # Operators add/remove via right-click on spectrum/waterfall;
        # each notch carries its own width and active flag. Default
        # width 80 Hz comfortably covers FT8 (47 Hz spread) on first
        # placement; operator can adjust per-notch via wheel/drag.
        self._notches: list[Notch] = []
        self._notch_enabled = False
        self._notch_default_width_hz = 80.0

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

        # Visuals — defaults match the pre-settings hardcoded values so
        # upgraders see no visual change until they open Visuals tab.
        self._waterfall_palette = "Classic"
        self._spectrum_min_db   = -110.0
        self._spectrum_max_db   = -20.0
        self._waterfall_min_db  = -110.0
        self._waterfall_max_db  = -30.0
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
        # Classical spectral-subtraction NR; neural NR (RNNoise /
        # DeepFilterNet) is on the backlog. Processor is always alive;
        # its .enabled flag gates the audio path.
        from lyra.dsp.nr import SpectralSubtractionNR
        self._nr = SpectralSubtractionNR(rate=48000)
        # Keep `_nr_profile` separate from the processor's internal
        # value so the UI can expose a "neural" placeholder even when
        # the processor itself only supports the classical profiles.
        self._nr_profile = SpectralSubtractionNR.DEFAULT_PROFILE

        # ── FFT ring buffer ───────────────────────────────────────────
        self._fft_size = 4096
        self._window = np.hanning(self._fft_size).astype(np.float32)
        self._win_norm = float(np.sum(self._window ** 2))
        self._sample_ring: deque = deque(maxlen=self._fft_size * 4)
        self._ring_lock = threading.Lock()

        # ── Demods ─────────────────────────────────────────────────────
        self._demods: dict = {}
        self._rebuild_demods()

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
        self._audio_buf.clear()
        if self._stream:
            try:
                self._stream.set_sample_rate(rate)
            except Exception as e:
                self.status_message.emit(f"Rate change failed: {e}", 3000)
        # Decimator is rate-dependent; notches use rate in coefficient calc.
        self._decimator = None
        self._rebuild_notches()
        self.rate_changed.emit(rate)

        # AK4951 path works reliably only at 48 kHz — at higher IQ
        # rates the EP2 audio-slot drain outpaces the 48 kHz demod
        # feed, producing zero-interleaved silence ("chopped audio").
        # Auto-fallback to the PC sound-device sink at higher rates,
        # restore AK4951 when rate returns to 48 k. The operator's
        # chosen preference is remembered in _preferred_audio_output.
        if not hasattr(self, "_preferred_audio_output"):
            self._preferred_audio_output = self._audio_output
        if rate > 48000 and self._audio_output == "AK4951":
            self._preferred_audio_output = "AK4951"
            self.set_audio_output("PC Soundcard")
            self.status_message.emit(
                f"Audio routed to PC soundcard at {rate//1000} k "
                "(AK4951 requires 48 k)", 4000)
        elif rate == 48000 and self._preferred_audio_output == "AK4951" \
                and self._audio_output != "AK4951":
            self.set_audio_output("AK4951")
            self.status_message.emit(
                "Audio restored to AK4951 at 48 k", 2500)

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
        self._audio_buf.clear()
        self._rebuild_demods()
        # Flush NR state on mode change — otherwise the noise-floor
        # estimate from the previous mode (often with very different
        # bandwidth characteristics) leaks in as an audible transient.
        self._nr.reset()
        if not self._suppress_band_save:
            self._save_current_band_memory()
        self.mode_changed.emit(alias)
        self._emit_passband()

    def _compute_passband(self) -> tuple[int, int]:
        """Return (low_hz, high_hz) offsets from the tuned center for
        the current mode + RX BW. Used by the panadapter to draw a
        translucent passband rectangle.

        Conventions:
          USB / DIGU         : center .. center + BW
          LSB / DIGL         : center - BW .. center
          CWU                : narrow window around center + CW pitch
          CWL                : narrow window around center - CW pitch
          AM / DSB / FM      : center - BW/2 .. center + BW/2
        """
        mode = self._mode
        bw = int(self._rx_bw_by_mode.get(mode, 2400))
        cw_pitch = 650  # matches CWDemod default
        if mode in ("USB", "DIGU"):
            return (0, bw)
        if mode in ("LSB", "DIGL"):
            return (-bw, 0)
        if mode == "CWU":
            half = bw // 2
            return (cw_pitch - half, cw_pitch + half)
        if mode == "CWL":
            half = bw // 2
            return (-cw_pitch - half, -cw_pitch + half)
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

    def set_gain_db(self, db: int):
        db = max(self.LNA_MIN_DB, min(self.LNA_MAX_DB, int(db)))
        if db == self._gain_db:
            return
        self._gain_db = db
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
        """Integer dB, clamped 0..+50. Applied in _apply_agc_and_volume
        as a linear multiplier between AGC and Volume. Dedicated stage
        so operators running AGC off on digital modes have a natural
        "station loudness" knob independent of moment-to-moment
        Volume trim."""
        db = max(0, min(50, int(db)))
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
        return self._nr.enabled

    def set_nr_enabled(self, on: bool):
        on = bool(on)
        if on == self._nr.enabled:
            return
        self._nr.enabled = on
        if on:
            # Fresh state each time NR is turned back on so a stale
            # overlap tail from a previous mode doesn't leak in.
            self._nr.reset()
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
            # neural package gets wired in, this branch will swap the
            # processor instance. For now fall back to medium so audio
            # still flows rather than going silent.
            self._nr.set_profile("medium")
        else:
            self._nr.set_profile(name)
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
            self._lna_auto_timer.start()
        else:
            self._lna_auto_timer.stop()
        self.lna_auto_changed.emit(enabled)

    def _emit_peak_reading(self):
        """Periodic (4 Hz) ADC peak broadcast — drives the toolbar
        indicator. Independent of Auto-LNA state.

        Uses a SHORT window (last ~20 block peaks ≈ 200 ms) instead
        of the full rolling history, so LNA changes are reflected in
        the reading within a fraction of a second rather than taking
        1+ seconds for the stale max to decay out. This matches how
        Thetis's RFP and ExpertSDR3's ADC meter behave — responsive
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

    def _adjust_lna_auto(self):
        """Overload-protection LNA loop — modeled on the reference HPSDR client's Auto-ATT
        (the reference HPSDR client itself has no closed-loop auto-LNA-gain; its Auto-ATT
        only backs gain OFF when the ADC is about to clip, and never
        chases a target upward).

        First-pass Lyra Auto-LNA was a target-chasing loop aiming at
        -15 dBFS peak. That target is HOTTER than the HL2 front-end
        likes; in real-world antenna environments on 40 m the loop
        drove LNA to +44 dB where IMD became audible ("odd mixed
        audio") and weak signals drowned in garbage. the reference HPSDR client's
        approach is correct: only REDUCE gain on impending overload.

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

        # Overload-protection only. Two thresholds so we react
        # aggressively to near-clipping but gently to "just hot."
        if peak_dbfs > -3.0:
            step = -3
        elif peak_dbfs > -10.0:
            step = -2
        else:
            return   # healthy — don't touch the user's gain setting

        new_db = max(self.LNA_MIN_DB,
                     min(self.LNA_MAX_DB, self._gain_db + step))
        if new_db == self._gain_db:
            return
        self.set_gain_db(new_db)
        self.status_message.emit(
            f"Auto-LNA: peak {peak_dbfs:+.1f} dBFS → LNA {new_db:+d} dB",
            2000)
        self._lna_peaks = []
        self._lna_rms = []

    def set_rx_bw(self, mode: str, bw: int):
        self._rx_bw_by_mode[mode] = int(bw)
        if mode == self._mode:
            self._rebuild_demods()
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
            if mode == self._mode:
                self._rebuild_demods()
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
    def _save_current_band_memory(self):
        band = band_for_freq(self._freq_hz)
        if band is None:
            return
        self._band_memory[band.name] = {
            "freq_hz": self._freq_hz,
            "mode":    self._mode,
            "gain_db": self._gain_db,
        }

    def recall_band(self, band_name: str, defaults_freq: int,
                    defaults_mode: str):
        """Restore freq/mode/gain saved for `band_name` if present, else
        tune to the band's defaults. Suppresses the auto-save during
        the apply so we don't immediately overwrite the memory we just
        loaded with intermediate tuning steps."""
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
                  deep: bool = False):
        """Place a new notch. Width defaults to the current
        notch_default_width_hz. Auto-enables the notch bank if it's
        currently off, on the assumption that an operator placing a
        notch wants to hear the result.

        `deep=True` cascades the IIR filter for ~2× attenuation —
        normally toggled per-notch via the right-click menu after
        placement, but accepted here for programmatic use (TCI,
        QSettings restore, tests)."""
        w = width_hz if width_hz is not None else self._notch_default_width_hz
        w = max(self.NOTCH_WIDTH_MIN_HZ, min(self.NOTCH_WIDTH_MAX_HZ, float(w)))
        nf = self._make_notch_filter(abs_freq_hz, w, deep=bool(deep))
        if nf is None:
            return
        self._notches.append(Notch(
            abs_freq_hz=float(abs_freq_hz), width_hz=w,
            active=bool(active), deep=bool(deep), filter=nf,
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
        # Canonicalize via the palettes module's alias table so legacy
        # "the reference HPSDR client" saves migrate to "Default" on load without the user
        # having to re-pick anything.
        from lyra.ui import palettes
        name = palettes.canonical_name(name)
        if name == self._waterfall_palette:
            return
        self._waterfall_palette = name
        self.waterfall_palette_changed.emit(name)

    @property
    def spectrum_db_range(self) -> tuple[float, float]:
        return (self._spectrum_min_db, self._spectrum_max_db)

    def set_spectrum_db_range(self, min_db: float, max_db: float):
        # Guarantee a sane >=3 dB span so the trace doesn't collapse
        # to a flat line if the user drags sliders past each other.
        lo, hi = float(min_db), float(max_db)
        if hi - lo < 3.0:
            hi = lo + 3.0
        self._spectrum_min_db, self._spectrum_max_db = lo, hi
        self.spectrum_db_range_changed.emit(lo, hi)

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
        # AK4951 audio only works cleanly at 48 kHz (EP2 frames fire
        # 1:1 with EP6 RX frames, so at higher IQ rates the audio
        # queue under-drains and silence gets zero-padded — chopped
        # distortion). Rather than veto the user's pick (confusing
        # "why can't I select AK4951?" UX), drop the rate to 48 kHz
        # ourselves and announce what we did. One click, works.
        if output == "AK4951" and self._rate > 48000:
            prev_rate = self._rate
            self.status_message.emit(
                f"AK4951 requires 48 k — dropping rate from "
                f"{prev_rate//1000} k to 48 k and switching.", 4500)
            self.set_rate(48000)
            # Fall through to apply AK4951 now that rate is safe.
        self._audio_output = output
        # Remember this choice as the user's preferred output for the
        # automatic fallback logic in set_rate (so if they later bump
        # rate above 48k we know to auto-restore AK4951 afterward).
        self._preferred_audio_output = output
        try:
            self._audio_sink.close()
        except Exception:
            pass
        self._audio_sink = self._make_sink() if self._stream else NullSink()
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
        # Push the filter-board OC pattern now that the stream is live
        if self._filter_board_enabled:
            self._apply_oc_for_current_freq()
        # Start the ADC-peak broadcaster so the toolbar indicator lights up
        self._peak_report_timer.start()
        self.stream_state_changed.emit(True)

    def stop(self):
        # Stop the peak broadcaster first so no more readings emit
        self._peak_report_timer.stop()
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
        self._audio_buf.clear()
        self._lna_peaks = []
        self._lna_rms = []
        self.stream_state_changed.emit(False)

    def discover(self):
        from lyra.protocol.discovery import discover
        radios = discover(timeout_s=1.0, attempts=2)
        if not radios:
            self.status_message.emit("No radios found.", 3000)
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
        mode = self._mode
        if mode == "Off":
            return
        if mode == "Tone":
            self._emit_tone(len(iq))
            return

        iq_48k = self._decimate_to_48k(iq)
        if iq_48k.size == 0:
            return
        self._audio_buf.extend(iq_48k.tolist())

        block = self._audio_block
        demod = self._demods.get(mode)
        if demod is None:
            return

        while len(self._audio_buf) >= block:
            chunk = np.asarray(self._audio_buf[:block], dtype=np.complex64)
            del self._audio_buf[:block]
            try:
                if self._notch_enabled:
                    for n in self._notches:
                        if n.active:
                            chunk = n.filter.process(chunk)
                audio = demod.process(chunk)
                # NR sits between demod and AGC — it cleans up the
                # recovered audio before AGC evaluates gain, so hiss
                # doesn't dominate AGC's peak-tracker during quiet
                # moments. No-op when nr.enabled is False.
                audio = self._nr.process(audio)
                audio = self._apply_agc_and_volume(audio)
                self._audio_sink.write(audio)
            except Exception as e:
                print(f"demod error: {e}")

    def _emit_tone(self, n: int):
        rate = self._rate
        t = (np.arange(n) + self._tone_phase) / rate
        audio = (0.3 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        self._tone_phase = (self._tone_phase + n) % rate
        # Tone uses the same AF Gain + Volume chain as demod output
        # so the operator's listening level stays consistent when
        # switching to Mode → Tone for rig testing.
        af = self.af_gain_linear
        vol = 0.0 if self._muted else self._volume
        audio = audio * af * vol
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
        # loudness delta (matches Thetis behaviour). Vol slider has a
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
            out = audio * vol
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
        # 80-120 dB of AGC range (Thetis, ExpertSDR3, FT-DX10). We
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
        rolling noise floor. Matches the reference client's "automatic AGC threshold"
        right-click action. Returns the new threshold value."""
        baseline = max(self._noise_baseline, 1e-4)
        factor = 10 ** (margin_db / 20.0)
        target = max(0.05, min(0.9, baseline * factor))
        self.set_agc_threshold(target)
        self.status_message.emit(
            f"AGC auto-threshold: {20*np.log10(target):+.0f} dBFS "
            f"(noise floor {20*np.log10(baseline):+.0f} + {margin_db:.0f} dB)",
            3000)
        return target

    def _decimate_to_48k(self, iq):
        if self._rate == 48000:
            return iq
        decim = self._rate // 48000
        if decim <= 1:
            return iq
        if self._decimator is None:
            self._decimator = _Decimator(self._rate, 48000)
        return self._decimator.process(iq)

    def _rebuild_demods(self):
        try:
            bw = self._rx_bw_by_mode
            self._demods = {
                "LSB":  SSBDemod(48000, "LSB", low_hz=300,
                                 high_hz=300 + bw.get("LSB", 2400)),
                "USB":  SSBDemod(48000, "USB", low_hz=300,
                                 high_hz=300 + bw.get("USB", 2400)),
                "CWL":  CWDemod(48000, pitch_hz=650,
                                bw_hz=bw.get("CWL", 250), sideband="L"),
                "CWU":  CWDemod(48000, pitch_hz=650,
                                bw_hz=bw.get("CWU", 250), sideband="U"),
                "DSB":  DSBDemod(48000, bw_hz=bw.get("DSB", 5000)),
                "AM":   AMDemod(48000, bw_hz=bw.get("AM", 6000) / 2),
                "FM":   FMDemod(48000, deviation_hz=5000,
                                audio_bw_hz=bw.get("FM", 10000) / 2),
                "DIGL": SSBDemod(48000, "LSB", low_hz=200,
                                 high_hz=200 + bw.get("DIGL", 3000)),
                "DIGU": SSBDemod(48000, "USB", low_hz=200,
                                 high_hz=200 + bw.get("DIGU", 3000)),
            }
        except RuntimeError as e:
            print(f"demod init failed: {e}")
            self._demods = {}

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

    def _make_sink(self):
        if self._audio_output == "AK4951":
            return AK4951Sink(self._stream)
        try:
            return SoundDeviceSink(rate=48000)
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
        spec_db = 10.0 * np.log10((np.abs(f) ** 2) / self._win_norm + 1e-20)

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
            self.smeter_level.emit(float(np.max(spec_db[lo:hi])))

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
        self._waterfall_tick_counter += 1
        if self._waterfall_tick_counter >= self._waterfall_divider:
            self._waterfall_tick_counter = 0
            for _ in range(self._waterfall_multiplier):
                self.waterfall_ready.emit(
                    spec_out, float(self._freq_hz), eff_rate)
