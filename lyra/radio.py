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

import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from lyra.protocol.stream import HL2Stream, SAMPLE_RATES
# Phase 6.B (v0.0.9.6): lyra.dsp.demod was deleted — its 5 demod
# classes were dead code post-WDSP and its NotchFilter object was
# orphan (constructed + mutated but never .process()'d, since
# WDSP receives notch parameters as plain (freq, width, active)
# tuples via _push_wdsp_notches).  See git history for the prior
# Python implementations.
from lyra.dsp.audio_sink import AK4951Sink, SoundDeviceSink, NullSink
from lyra.dsp.captured_profile_iq import CapturedProfileIQ
from lyra.radio_state import DispatchState, RadioFamily
from lyra.protocol.capabilities import (
    HL2_CAPABILITIES, RadioCapabilities,
)
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
    in dimensionless Q values.  Internal filter design uses
    Q = freq / width.

    v0.0.7.1 notch v2: replaced ``deep: bool`` with explicit
    ``depth_db: float`` and ``cascade: int`` parameters.  Operator
    can now specify the exact attenuation depth they want
    (independent of width) and the cascade depth (1-4 stages,
    sharper transition shoulders for higher values).  See
    ``docs/architecture/notch_v2_design.md`` for the full design.

    Backward compat: ``n.deep`` is still readable as a derived
    attribute (== ``cascade > 1``) so legacy callers keep working.

    Phase 6.B (v0.0.9.6) dropped the ``filter`` field — WDSP
    receives notch parameters directly as (freq, width, active)
    tuples via ``_push_wdsp_notches``, so the per-notch Python
    biquad object that used to live here was orphan state.

    Fields:
    - ``abs_freq_hz``: absolute sky frequency of notch center.
    - ``width_hz``: operator-set -3-dB-from-peak bandwidth in Hz.
    - ``active``: individually enableable; False = DSP bypass but
      placement is preserved.  Inactive notches render in grey.
    - ``depth_db``: notch attenuation at center, in dB (negative).
      Default -50 (Normal preset).  Slider range -20 to -80.
      Currently advisory in WDSP mode (WDSP collapses depth into
      width approximation — see ``_push_wdsp_notches``).
    - ``cascade``: number of biquad stages (1-4).  Each stage gets
      ``depth_db / cascade``.  Default 2.  Currently advisory in
      WDSP mode for the same reason.
    """
    abs_freq_hz: float          # absolute sky frequency of notch center
    width_hz: float             # -3 dB-from-peak bandwidth in Hz
    active: bool                # individually enableable; False = bypass
    depth_db: float             # notch attenuation at center (dB, negative)
    cascade: int                # number of biquad stages (1-4)

    @property
    def deep(self) -> bool:
        """Legacy compat: True if cascade > 1.  Existing code that
        reads ``n.deep`` continues working.  Writes go through the
        new setters (``set_notch_depth_db_at`` /
        ``set_notch_cascade_at``)."""
        return self.cascade > 1


# ──────────────────────────────────────────────────────────────────────
# §15.7 sync investigation — env-var-gated timing instrumentation.
# Operator UX (Rick 2026-05-13): "enable this on my local copy, I will
# get you the information on both an HL2 audio and PC Soundcard runs."
# Set ``LYRA_TIMING_DEBUG=1`` before launching Lyra to get rate-limited
# (one line per ~1 sec) summary output showing min / avg / max latency
# at four key measurement points in the RX signal flow.
#
# All four metrics are tracked in ``_TimingStats`` instances on the
# ``Radio`` object.  Worker thread and main thread both contribute to
# the same instance via thread-safe ``record()`` calls.  Once per
# second, the next call to ``record()`` triggers a flush() that prints
# all buckets and clears them.
# ──────────────────────────────────────────────────────────────────────
class _TimingStats:
    """Rate-limited timing collector for §15.7 sync investigation.

    Append measurements via ``record(name, dt_ns)``.  Every ~1 second
    the next call prints a one-line summary covering all buckets:

        [TIMING] audio_worker n=187 min=4.8 avg=5.6 max=11.2 ms
                 fft_worker  n=46  min=8.1 avg=9.4 max=14.7 ms
                 spec_main   n=46  min=2.0 avg=3.2 max=6.5 ms
                 wf_offset   n=46  min=0.02 avg=0.04 max=0.18 ms
                 q_rx1=0 q_rx2=0  sink=HL2

    Thread-safe via a small lock.  Cheap when not flushing (just
    append).  The flush itself is O(N) over the window's samples
    (computes min/avg/max).  Window samples are dropped after print.
    """
    import threading as _threading

    def __init__(self):
        from collections import defaultdict
        self._buckets = defaultdict(list)
        self._lock = self._threading.Lock()
        self._last_flush_ns: int = 0
        self._period_ns: int = 1_000_000_000   # 1 sec
        self._ctx: dict = {}   # static keys updated per-record (sink, etc.)

    def set_context(self, key: str, value) -> None:
        """Set a static context field shown on each flush (e.g. sink
        name).  Thread-safe."""
        with self._lock:
            self._ctx[key] = value

    def record(self, name: str, dt_ns: int) -> None:
        """Append a per-event latency in nanoseconds.  Flushes when
        the 1-sec window expires.  Safe from any thread."""
        import time
        now = time.monotonic_ns()
        with self._lock:
            self._buckets[name].append(int(dt_ns))
            if self._last_flush_ns == 0:
                self._last_flush_ns = now
                return
            if (now - self._last_flush_ns) < self._period_ns:
                return
            # 1 sec elapsed -- flush all buckets and reset.
            snapshot = self._buckets
            self._buckets = type(snapshot)(list)
            ctx_snap = dict(self._ctx)
            self._last_flush_ns = now
        # Print OUTSIDE the lock so signal slots / file flushes
        # don't serialize on the lock held by the worker thread.
        try:
            parts: list[str] = []
            for name in sorted(snapshot.keys()):
                vals = snapshot[name]
                if not vals:
                    continue
                lo = min(vals) / 1_000_000.0
                hi = max(vals) / 1_000_000.0
                avg = (sum(vals) / len(vals)) / 1_000_000.0
                parts.append(
                    f"{name} n={len(vals)} "
                    f"min={lo:.1f} avg={avg:.1f} max={hi:.1f} ms")
            ctx_parts = [f"{k}={v}" for k, v in sorted(ctx_snap.items())]
            ctx_str = "  " + " ".join(ctx_parts) if ctx_parts else ""
            print(f"[TIMING] " + "  ".join(parts) + ctx_str, flush=True)
        except Exception:
            pass


class Radio(QObject):
    # ── State change signals (UI subscribes) ───────────────────────────
    stream_state_changed = Signal(bool)
    # ── Dispatch state product (v0.1 Phase 0, per consensus-plan §4.2.x) ──
    # Fires whenever any axis of the DispatchState changes (mox / ps_armed
    # / rx2_enabled / family).  Payload is the new DispatchState snapshot.
    # Phase 0 has no live consumers wired -- Phase 1+ subscribes:
    # protocol layer for per-DDC routing, UI panadapter for source switch,
    # captured-profile pre-pass for MOX+PS bypass.  See
    # ``lyra/radio_state.py`` for the dataclass + ConsumerID enum.
    dispatch_state_changed = Signal(object)        # DispatchState
    # Phase 1 v0.1 RX2 freq change (bench-test surface; Phase 3 wires
    # the full focus-model UI).  Payload is the new VFO B frequency
    # in Hz.  ``Radio.set_rx2_freq_hz(hz)`` writes the C&C register
    # (via HL2Stream._set_rx2_freq) AND emits this signal so any
    # bench-test dialog can mirror its readout.
    rx2_freq_changed = Signal(int)
    # Phase 3.A v0.1 (2026-05-12) -- focused-RX state for the hybrid
    # focus model per consensus plan §6.1 + §6.7.  Payload is the
    # canonical host-channel ID (0 = RX1, 2 = RX2 -- matches the
    # ConsumerID enum + wdsp_engine.RxChannel.channel convention).
    # UI binds to this signal to move the orange focus border and
    # re-source the MODE+FILTER / DSP+AUDIO panels to the new
    # focused RX's state.  Phase 3.A introduces the field and the
    # signal; Phase 3.B+ wires actual UI consumers.
    focused_rx_changed = Signal(int)
    # Phase 3.E.1 v0.1 (2026-05-12): panadapter source RX changed.
    # Payload is the new source RX id (0 = RX1, 2 = RX2).  Worker
    # listens to this so it can flush its FFT sample ring on source
    # change (otherwise the next FFT frame would be a mix of the
    # old and new RX's IQ).  UI panels (notch overlay, EiBi labels,
    # click-to-tune target) listen so they bind to the right RX.
    panadapter_source_changed = Signal(int)
    # ── TCI streaming taps (v0.0.9.1+) ─────────────────────────────────
    # These signals fire whenever the audio / IQ data is finalised so
    # the TciServer can broadcast binary frames to subscribed clients.
    # Both are cross-thread-safe -- when emitted from the worker thread
    # in DSP_THREADING_WORKER mode, Qt automatically uses
    # QueuedConnection to deliver to the main thread (where TciServer
    # lives and where QWebSocket.sendBinaryMessage must run).
    #
    # audio_for_tci_emit:
    #   payload = mono float32 audio block at 48 kHz, post-AGC,
    #   post-leveler, post-tanh -- the same audio the operator hears.
    #   Fires once per audio block written to the sink (~93 emits/sec
    #   at 512-sample block_size).
    #
    # iq_for_tci_emit:
    #   payload = (complex64 IQ array, sample_rate_hz)
    #   Fires per IQ batch from the HL2 stream (~1500 emits/sec at
    #   192 kHz IQ rate -- see Radio._stream_cb tap point for batching).
    audio_for_tci_emit   = Signal(object)        # np.ndarray (float32 mono 48 kHz)
    iq_for_tci_emit      = Signal(object, int)   # np.ndarray complex64, sample_rate
    freq_changed         = Signal(int)
    rate_changed         = Signal(int)
    mode_changed         = Signal(str)
    gain_changed         = Signal(int)
    volume_changed       = Signal(float)
    af_gain_db_changed   = Signal(int)   # AF makeup gain, 0..+50 dB
    balance_changed      = Signal(float) # stereo pan, -1..0..+1
    rx_bw_changed        = Signal(str, int)       # mode, Hz
    # Phase 3.C v0.1 (2026-05-12): per-RX2 sibling signals.  RX1's
    # original signals stay -- panels listening for RX1-specific
    # state (S-meter, band stack, mode-aware menu items) keep
    # working unchanged.  Panels that want to follow the focused
    # RX (MODE+FILTER, DSP+AUDIO, GainPanel) connect to BOTH the
    # RX1 and RX2 signal pairs + ``focused_rx_changed``, and
    # refresh their UI on whichever is currently focused.
    mode_changed_rx2     = Signal(str)
    af_gain_db_changed_rx2 = Signal(int)
    rx_bw_changed_rx2    = Signal(str, int)
    # Phase 3.D v0.1: per-RX volume + mute signals (§6.8 consensus
    # plan -- Vol-A / Vol-B and Mute-A / Mute-B surface when SUB is
    # enabled).
    volume_changed_rx2   = Signal(float)
    muted_changed_rx2    = Signal(bool)
    tx_bw_changed        = Signal(str, int)
    bw_lock_changed      = Signal(bool)
    notches_changed      = Signal(list)           # list[Notch] (see dataclass above)
    notch_enabled_changed = Signal(bool)
    notch_default_width_changed = Signal(float)   # default width for new notches, in Hz
    # v0.0.7.1 notch v2: fires whenever the saved-bank list changes
    # (save / delete).  Right-click menu rebuilds its 'Load preset'
    # submenu when this fires.  See save_notch_bank / delete_notch_bank.
    notch_banks_changed  = Signal()
    # v0.0.9 Step 4: fires after the EiBi CSV is (re)loaded so
    # the panadapter can refresh its label cache and the Settings
    # tab can update its "loaded / X days old" status line.
    eibi_store_changed   = Signal()
    audio_output_changed = Signal(str)
    pc_audio_device_changed = Signal(object)   # int index, or None for auto
    # v0.0.9.6: operator-selected PortAudio host API label (see
    # lyra/dsp/audio_sink.py::HOST_API_LABEL_*).
    pc_audio_host_api_changed = Signal(str)
    ip_changed           = Signal(str)

    # Phase 3.B B.5 — sink-swap channel for worker mode.
    # When DSP runs on the worker thread, the worker keeps its OWN
    # reference to the audio sink (so it never sees the sink getting
    # closed mid-write under its feet).  On every sink swap (start,
    # stop, set_audio_output, PC device change) Radio emits this
    # signal carrying the NEW sink object; the worker's slot updates
    # its local reference and closes the old sink between blocks.
    # Single-thread mode never connects this signal — Radio mutates
    # _audio_sink directly as it always has.
    worker_audio_sink_changed = Signal(object)  # AudioSink-like or NullSink

    # HL2 hardware telemetry (temperature, supply voltage, fwd/rev power).
    # Emitted at ~2 Hz from a QTimer that polls FrameStats so the UI
    # never has to touch the protocol layer directly. Values are in
    # engineering units (°C, V, W) — conversion from raw 12-bit ADC
    # counts lives in _emit_hl2_telemetry below. When the stream is
    # stopped or no telemetry has been seen yet, fields are NaN so the
    # UI can show "--" instead of a garbage zero reading.
    hl2_telemetry_changed = Signal(dict)  # {temp_c, supply_v, fwd_w, rev_w}

    # ── Streaming data signals ─────────────────────────────────────────
    spectrum_ready       = Signal(object, float, int)   # db, center_hz (= DDS in v0.0.9.8+), rate
    # VFO marker offset from spectrum visual center, in Hz.  Under
    # v0.0.9.8's carrier-freq VFO convention the spectrum widget
    # centers FFT data on DDS (= VFO ± cw_pitch in CW modes) — the
    # operator's VFO marker therefore needs a horizontal offset to
    # land where the actual carrier sits.  0 in non-CW; +pitch in
    # CWU; -pitch in CWL.  Re-emitted on freq, mode, or pitch
    # changes (anything that shifts the DDS-vs-VFO relationship).
    marker_offset_changed = Signal(int)
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
    # RIT (Receiver Incremental Tuning) — v0.1.1 feature.  RX-only
    # frequency offset (-9999..+9999 Hz, signed) applied to the
    # RX1 DDS while leaving the operator-displayed VFO unchanged.
    # Classic ham-rig idiom for chasing a slightly off-frequency DX
    # station without retuning the main VFO.  XIT (the TX mirror)
    # lands with v0.2 TX bring-up; v0.1.1 ships RIT only.
    # Scope: RX1 only -- per-RX RIT deferred per §15.16 v0.1.1 scope
    # lock (RX1-only matches "no RIT in Lyra today" baseline; v0.1.2
    # may add per-RX after stepper redesign settles).  Applied
    # centrally inside ``_compute_dds_freq_hz`` so every freq write
    # to the protocol layer carries the offset.
    rit_enabled_changed = Signal(bool)
    rit_offset_changed  = Signal(int)      # Hz, signed (-9999..+9999)
    # v0.2 Phase 2 (5/N) -- mic-input source selection.  Payload is
    # the new source string ("hl2_jack" or "pc_soundcard").  Settings
    # UI subscribes to mirror the radio-button state without firing
    # a feedback loop.  Future status-bar badge could also subscribe
    # to display the active path.
    mic_source_changed       = Signal(str)
    pc_mic_device_changed    = Signal(object)   # int index or None
    pc_mic_channel_changed   = Signal(str)      # "L" / "R" / "BOTH"
    # v0.2.0 Phase 0: TX-active edge signal.  Emitted True when the
    # PTT state machine (``lyra.ptt.PttStateMachine``) transitions
    # out of RX into ANY TX state (MOX_TX / TUN_TX / CW_TX / VOX_TX),
    # False when it returns to RX.  Phase 0 has no emitter (the
    # state machine + MOX button + hardware PTT input land in
    # Phase 3); UI consumers can connect now and stay inert until
    # Phase 3.  Consumers: ``FrequencyDisplay.set_tx_active`` (red
    # VFO LED per §15.9), ``SMeter.set_tx_active`` (TX-style
    # meter layout), spectrum widget (red TX passband rectangle
    # per §15.9), AAmixer auto-mute-on-TX rules per §15.14, status
    # bar AAmixer state badge per §15.15.
    tx_active_changed = Signal(bool)
    # CW Zero (white) line offset from the VFO marker, in Hz.
    # Vertical reference line drawn at the filter center — i.e., where
    # a clicked CW signal lands and where the audio is generated.
    # CWU: +pitch (right of marker). CWL: -pitch (left). 0 outside CW
    # (line hidden). Emitted on mode change and pitch change.
    cw_zero_offset_changed = Signal(int)

    # Panadapter zoom + update rates
    zoom_changed                  = Signal(float)      # 1.0 = full span
    panadapter_scroll_step_changed = Signal(int)       # mouse-wheel-tune step, Hz
    panadapter_round_to_100hz_changed = Signal(bool)   # Exact / Round 100 Hz toggle
    _ncdxf_follow_changed         = Signal(str)        # NCDXF follow station callsign, "" = off
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
    # Profile name ∈ {"light","medium","heavy","nr2","neural"};
    # "neural" is a placeholder reserved for future RNNoise /
    # DeepFilterNet integration, greyed out in the UI until a
    # suitable package is importable.  Legacy "aggressive" maps to
    # "heavy" via _NR_PROFILE_ALIASES for QSettings backwards compat.
    nr_enabled_changed = Signal(bool)
    nr_profile_changed = Signal(str)
    nr_mode_changed = Signal(int)        # NR mode (1..4) — WDSP gain_method picker
    aepf_enabled_changed = Signal(bool)  # AEPF (anti-musical-noise) on/off
    npe_method_changed = Signal(int)     # NPE method — 0=OSMS, 1=MCRA

    # Phase 3.D #2 — Noise blanker (impulse suppression, IQ-domain).
    # Profile = preset name (off / light / medium / aggressive /
    # custom).  Threshold = numerical multiplier on background-
    # power reference (operator-tunable in Custom; presets pick
    # reasonable values).  See lyra/dsp/nb.py and
    # docs/architecture/noise_toolkit.md §3.4.
    nb_profile_changed = Signal(str)
    nb_threshold_changed = Signal(float)

    # Phase 3.D #3 — Auto Notch Filter (LMS adaptive).  Profile =
    # preset name (off / gentle / standard / aggressive / custom).
    # Mu = adaptation step size (operator-tunable in Custom).
    # See lyra/dsp/anf.py and docs/architecture/noise_toolkit.md §3.3.
    anf_profile_changed = Signal(str)
    anf_mu_changed = Signal(float)

    # NOTE: Audio Leveler signals (leveler_profile_changed,
    # leveler_threshold_changed, leveler_ratio_changed,
    # leveler_makeup_changed) removed in Phase 4 of legacy-DSP
    # cleanup (CLAUDE.md §14.9).  WDSP's AGC subsumes the leveler's
    # dynamic-range work — the leveler was inert in WDSP mode after
    # Phase 3, so the operator-facing UI controls were misleading.
    # Removed entirely rather than left as dead state container.

    # NR2 (Ephraim-Malah MMSE-LSA).  Active when nr_profile == "nr2"
    # (orthogonal to the live/captured source toggle).  Three
    # operator-facing knobs each get a dedicated change signal so
    # both the panel slider and the Settings tab checkboxes can
    # bind without spurious cross-firing.
    nr2_aggression_changed = Signal(float)
    nr2_musical_noise_smoothing_changed = Signal(bool)
    nr2_speech_aware_changed = Signal(bool)
    # Gain-method selector (MMSE-LSA vs Wiener) — added with the
    # WDSP-port stack.  Persists to QSettings under noise/nr2_gain_method.
    nr2_gain_method_changed = Signal(str)

    # NR1 — continuous strength slider (0.0..1.0).  Replaces the
    # discrete light/medium/heavy profile picker as of 2026-05-01;
    # parallel UX to NR2's aggression slider.
    nr1_strength_changed = Signal(float)

    # LMS (NR3-style line enhancer) — independent stage in the
    # audio chain (slots between ANF and NR).  Has its own enable
    # toggle and strength slider, both with dedicated change signals
    # so the DSP panel button + Settings tab can bind cleanly.
    lms_enabled_changed = Signal(bool)
    lms_strength_changed = Signal(float)

    # All-mode voice-presence squelch (SSQL — direct port from WDSP).
    # Final stage in the audio chain; works on every modulation type.
    squelch_enabled_changed = Signal(bool)
    squelch_threshold_changed = Signal(float)

    # Phase 3.D #1 — Captured-noise-profile signals.
    # noise_capture_done fires when a capture finalizes inside the
    # NR processor.  Payload was the smart-guard verdict in v0.0.7.x
    # through v0.0.9.4; smart-guard removed in v0.0.9.5 so the
    # payload is now always empty string (kept for slot-signature
    # compatibility — slots ignore the arg).
    # noise_active_profile_changed fires whenever the loaded
    # captured profile is replaced or cleared; payload is the
    # display name (or "" when cleared) so the panel badge updates.
    # noise_profiles_changed fires after save / delete / rename
    # so the manager dialog refreshes its list view.
    noise_capture_done = Signal(str)            # always "" post-v0.0.9.5
    noise_active_profile_changed = Signal(str)  # name or ""
    noise_profiles_changed = Signal()
    # NR source toggle — fires when set_nr_use_captured_profile flips.
    # UI binds to this to update menu check-states + status badges.
    nr_use_captured_profile_changed = Signal(bool)
    # P1.2 — fires once when the loaded captured profile drifts
    # beyond the staleness threshold (default 10 dB).  Payload is
    # the smoothed drift in dB.  UI shows a toast suggesting
    # recapture.  At most one fire per "stale event"; rearms after
    # ~15 sec of stable conditions.  See SpectralSubtractionNR for
    # the full state-machine semantics.
    noise_profile_stale = Signal(float)

    # APF (Audio Peaking Filter) — CW-only narrow peaking biquad
    # centered on cw_pitch_hz. Boosts the CW tone without the ringing
    # tail of a brick-wall narrow filter. Channel mode-gates inside
    # process() so the operator's setting is preserved across mode
    # switches but only runs when there's CW content to boost.
    apf_enabled_changed = Signal(bool)
    apf_bw_changed = Signal(int)         # -3 dB bandwidth in Hz
    apf_gain_changed = Signal(float)     # peak gain in dB

    # BIN — Binaural pseudo-stereo. Hilbert phase-split puts the audio
    # "in the middle of the head" with adjustable depth. Useful for CW
    # spatial perception and for SSB voice widening on headphones.
    # Operator-toggled, runs on all modes (no mode gate).
    bin_enabled_changed = Signal(bool)
    bin_depth_changed = Signal(float)    # 0.0..1.0

    # Phase 3.B+: DSP threading mode — operator-selectable BETA toggle.
    # "single" runs DSP on the Qt main thread (v0.0.5 behavior).
    # "worker" runs DSP on a dedicated lyra.dsp.worker.DspWorker thread
    # (BETA, opt-in via Settings → DSP → Threading). Switching modes
    # requires a Lyra restart — the worker thread is set up once at
    # Radio construction time. Until Phase 3.B is fully wired
    # (B.3+), selecting "worker" is currently a no-op preference.
    dsp_threading_mode_changed = Signal(str)
    DSP_THREADING_SINGLE = "single"
    DSP_THREADING_WORKER = "worker"
    DSP_THREADING_MODES = (DSP_THREADING_SINGLE, DSP_THREADING_WORKER)

    # Panadapter noise-floor estimate — 20th percentile of the current
    # spectrum, rolling-averaged. Emitted at ~6 Hz (not every FFT tick)
    # so the widget's horizontal reference line doesn't twitch.
    noise_floor_changed = Signal(float)   # dBFS

    # Operator / Station identification — global settings consumed
    # by multiple features (TCI spots, WX-Alerts, future logging
    # integration).  Persisted under operator/* in QSettings.
    callsign_changed = Signal(str)
    grid_square_changed = Signal(str)
    # Emitted when the effective operator location changes — either
    # from a new grid square or from manual lat/lon override.  Args:
    # (lat, lon).  WX-Alerts subscribes to this to re-query sources
    # when the operator moves their station.
    operator_location_changed = Signal(float, float)

    # Weather Alerts — emitted by the WxWorker after each poll cycle.
    # The header indicator + any future map view subscribe.
    wx_snapshot_changed = Signal(object)        # WxSnapshot
    wx_enabled_changed = Signal(bool)
    wx_error = Signal(str)                       # non-fatal source errors

    # Band plan / region — drives the panadapter sub-band strip +
    # landmark markers + out-of-band warnings. "NONE" disables the
    # whole feature (HL2 hardware remains unlocked either way).
    band_plan_region_changed = Signal(str)
    band_plan_show_segments_changed = Signal(bool)
    band_plan_show_landmarks_changed = Signal(bool)
    band_plan_show_ncdxf_changed    = Signal(bool)
    band_plan_edge_warn_changed      = Signal(bool)

    # Peak-markers — a persistent "peak hold" overlay drawn only within
    # the RX passband. Bounded display + user-toggleable so it stays
    # diagnostic rather than visual clutter.
    peak_markers_enabled_changed = Signal(bool)
    peak_markers_decay_changed   = Signal(float)   # dB / second
    peak_markers_style_changed   = Signal(str)     # "line"/"dots"/"triangles"
    # Peak-hold timer + decay preset (Display-panel combos).
    # peak_hold_secs payload is float seconds; -1.0 means Infinite.
    # peak_hold_decay_preset payload is "fast"/"med"/"slow".
    peak_hold_secs_changed         = Signal(float)
    peak_hold_decay_preset_changed = Signal(str)
    # Fired by clear_peak_holds(); spectrum widgets reset their
    # per-bin peak buffers + last-updated arrays in response.
    peak_holds_cleared             = Signal()
    peak_markers_show_db_changed = Signal(bool)    # show numeric dB at peaks

    # Spectrum trace smoothing — display-only EWMA filter applied before
    # the trace is drawn. Off by default (true raw FFT). Strength 1..10
    # maps to an alpha in roughly [0.91 .. 0.09]; lower alpha = more
    # smoothing / slower response. Pure visual feature; no DSP impact.
    spectrum_smoothing_enabled_changed  = Signal(bool)
    spectrum_smoothing_strength_changed = Signal(int)   # 1..10

    # User-picked colors — spectrum trace + per-segment band-plan
    # fills. Stored as #RRGGBB hex strings for simple QSettings
    # round-trip. Empty string = use the built-in default color.
    spectrum_trace_color_changed = Signal(str)
    # Fill-area-under-trace controls (operator request 2026-05-09).
    # Toggle enables/disables the gradient fill below the spectrum
    # trace; color picker overrides the default (which is the trace
    # color itself).  Empty color string = "use trace color".
    spectrum_fill_enabled_changed = Signal(bool)
    spectrum_fill_color_changed   = Signal(str)
    segment_colors_changed       = Signal(dict)    # {kind: hex, ...}
    noise_floor_color_changed    = Signal(str)    # NF line color hex
    peak_markers_color_changed   = Signal(str)    # peak marker color hex

    # ── DSP profile signals ────────────────────────────────────────────
    agc_profile_changed  = Signal(str)    # off / fast / med / slow / long / auto / custom
    agc_action_db        = Signal(float)  # live gain reduction, dB
    agc_threshold_changed = Signal(float) # current threshold (target), dBFS-ish
    # Phase 3.C per-RX2 siblings (see comment above on Phase 3.C
    # per-RX2 signal pattern).
    agc_profile_changed_rx2  = Signal(str)
    agc_threshold_changed_rx2 = Signal(float)

    # AGC presets (industry-standard). Attack is always instant.
    # "auto" uses a medium release/hang and additionally tracks the noise
    # floor continuously (auto_set_agc_threshold every AGC_AUTO_INTERVAL_MS)
    # so the threshold follows band conditions without user intervention.
    # AGC release coefficient is applied per audio block (~43 ms at
    # 48 kHz / 2048 samples). Time constant τ for peak decay is
    # τ ≈ -43 ms / ln(1 - release). Profile target characteristics:
    #   FAST:  hang ~130 ms, decay ~120 ms
    #   MED :  hang    0 ms, decay ~250 ms
    #   SLOW:  hang ~1000 ms, decay ~500 ms
    # The original Lyra values had release coefficients ~20-30×
    # too slow (Fast τ was 2.1 s, Slow τ was 43 s), which made
    # audio stay clamped for many seconds after a peak — exact
    # symptom: "audio doesn't come back up to audible after a
    # strong signal." Hang on Med is now ZERO; recovery starts on
    # the very first block after the peak passes. Slow keeps a 1 s
    # hang for steady-carrier listening (AM broadcast, DX nets).
    AGC_PRESETS: dict[str, dict] = {
        "off":    {"release": 0.0,   "hang_blocks":  0},   # disabled
        # The Release/Hang values in this table were operator-facing
        # in the legacy single-state Python AGC engine.  As of v0.0.9.6
        # AGC runs entirely inside WDSP and these values are advisory
        # state only — Phase 6.A1 deleted the Python wcpAGC wrapper
        # they used to drive.  Kept as a UI default table so the
        # Settings sliders position to a sensible value per profile.
        "fast":   {"release": 0.30,  "hang_blocks":  3},   # WDSP FAST
        "med":    {"release": 0.158, "hang_blocks":  0},   # WDSP MED
        "slow":   {"release": 0.083, "hang_blocks": 23},   # WDSP SLOW
        "long":   {"release": 0.040, "hang_blocks": 46},   # WDSP LONG
        "auto":   {"release": 0.158, "hang_blocks":  0},   # rides MED today
    }
    AGC_AUTO_INTERVAL_MS = 3000   # re-track threshold every 3 s in auto mode

    # WDSP AGC threshold operator-tunable range, in dBFS.  Drives
    # the Settings → DSP → AGC Threshold slider clamp + auto-
    # threshold output clamp.  Range -150..-40 covers everything
    # from "very quiet receiver / weak DX hunting" to "broadcast
    # / strong-signal listening" with the Thetis-typical -100
    # comfortable in the middle.
    _AGC_THRESH_MIN_DBFS: float = -150.0
    _AGC_THRESH_MAX_DBFS: float = -40.0
    _AGC_THRESH_DEFAULT_DBFS: float = -100.0

    # ── Notch v2 presets (operator-facing right-click choices) ──────
    # Each preset maps to (depth_db, cascade).  See notch_v2_design.md
    # §4.2 for the bench-validated behaviour of each profile.
    NOTCH_PRESETS: dict[str, dict] = {
        "normal":   {"depth_db": -50.0, "cascade": 2},
        "deep":     {"depth_db": -70.0, "cascade": 2},
        "surgical": {"depth_db": -50.0, "cascade": 4},
    }
    NOTCH_DEFAULT_PRESET: str = "normal"
    # Operator-tunable depth slider range.
    NOTCH_DEPTH_MIN_DB: float = -80.0
    NOTCH_DEPTH_MAX_DB: float = -20.0
    NOTCH_CASCADE_MIN: int = 1
    NOTCH_CASCADE_MAX: int = 4

    # ── APF (CW Audio Peaking Filter) constants ───────────────────────
    # Inlined from lyra/dsp/apf.py in Phase 4 of legacy-DSP cleanup
    # so radio.py no longer needs to import that module just to read
    # operator-facing bounds.  Values are 1:1 with the legacy
    # AudioPeakFilter class constants.  In WDSP mode these gate the
    # Radio.set_apf_* clamps; the actual peaking happens via WDSP's
    # SPEAK biquad (see _push_wdsp_apf_state).
    APF_BW_MIN_HZ: int = 30
    APF_BW_MAX_HZ: int = 200
    APF_BW_DEFAULT_HZ: int = 100
    APF_GAIN_MIN_DB: float = 0.0
    APF_GAIN_MAX_DB: float = 18.0
    APF_GAIN_DEFAULT_DB: float = 12.0

    # (v0.0.9.3) Removed _AGC_LEGACY_BLOCK_N -- it was the per-block
    # sample count the legacy AGC's per-sample-constant translator
    # consumed.  WDSP works in canonical seconds-form parameters
    # internally and doesn't need this.

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
        # 96 k is the default IQ rate (was 48 k).  48 k was dropped
        # from operator-selectable rates because at 48 k the DSP
        # block produces 16 EP2 frames' worth of audio per producer
        # call (1:1 IQ-to-audio mapping, no decimation), which the
        # HL2 gateware FIFO can't absorb cleanly under Path C's
        # producer-paced semaphore.  See SAMPLE_RATES comment in
        # stream.py for the full rationale.
        self._rate = 96000
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
        # RIT (Receiver Incremental Tuning) -- v0.1.1.  Offset applied
        # to RX1 DDS only when ``_rit_enabled``.  Defaults loaded from
        # QSettings by ``autoload_rit_settings`` after the toolbar is
        # built (so the panel sees the correct initial button state).
        # Constructor seeds safe defaults so the centrally-applied
        # offset in ``_compute_dds_freq_hz`` is a no-op until the
        # operator opts in.
        self._rit_enabled: bool = False
        self._rit_offset_hz: int = 0
        # v0.2 Phase 2 (5/N) -- mic-input source selection.
        # Lyra supports two mic paths because the HL2 hardware family
        # has two variants:
        #   * HL2+ (AK4951 codec) -- mic input on the radio, samples
        #     arrive via EP6 byte slot 24-25 per CLAUDE.md §3.3.
        #     Default in the field for "+" operators including N8SDR.
        #   * Standard HL2 (no codec) -- no mic on the radio; operator
        #     plugs a mic into the PC sound card.
        # Operator picks via Settings -> TX -> Mic input.  Default
        # "hl2_jack" matches the canonical-dev-target (N8SDR's setup);
        # standard-HL2 operators switch to "pc_soundcard" on first
        # setup.  Persisted to QSettings under radio/mic_source.
        # Future v0.4 ANAN-class hardware lacks any radio-side mic so
        # the default will need a per-family override via the
        # capabilities struct (see CLAUDE.md §6.7 discipline #4).
        self._mic_source: str = "hl2_jack"
        # PC mic device + channel-select stored separately so they
        # survive the operator toggling between mic sources -- they're
        # operator-tunable independently of which source is currently
        # active.
        self._pc_mic_device: Optional[int] = None    # None = host-API default
        self._pc_mic_channel: str = "L"              # "L" / "R" / "BOTH"
        # SoundDeviceMicSource instance -- created lazily on first
        # set_mic_source("pc_soundcard"), torn down when switching
        # back to hl2_jack.  None when not active.
        self._pc_mic_source = None
        # v0.2 Phase 2 commit 7.2 (polish): log-once latch for PC mic
        # start failures.  Without this, every Radio.start() with a
        # stale/invalid PC mic device floods the console with the
        # same "PC mic start failed" message.  With it, operator sees
        # ONE toast + ONE console line per session per device config;
        # subsequent stream restarts silently retry so hot-plug
        # recovery (operator plugs headset back in) works without
        # status-bar churn.  Reset to False on successful start, on
        # set_pc_mic_device, and on set_pc_mic_channel -- any config
        # change re-arms the log so a new failure is visible.
        #
        # Note: we never silently fall back to hl2_jack on PC mic
        # failure.  Standard HL2 (no AK4951 codec) operators have NO
        # mic on the radio at all; falling back would route mic to
        # a path that physically can't carry voice.  The operator's
        # hardware-source choice always wins; we just surface the
        # error gracefully.
        self._pc_mic_failure_logged: bool = False
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
        # v0.0.9.6: operator-pickable PortAudio host API for the
        # PC Soundcard sink ("Auto" / "MME" / "WASAPI shared" /
        # "WASAPI exclusive" / "WDM-KS" / "DirectSound" / "ASIO").
        # Defaults to "Auto" = current behavior (sounddevice picks
        # WASAPI shared by default on Windows).  Operators on
        # machines where WASAPI shared has glitches (Windows audio
        # engine pauses, etc.) can switch to a host API that
        # bypasses the engine.  Persisted by app.py QSettings.
        # See lyra/dsp/audio_sink.py::HOST_API_LABEL_* constants.
        self._pc_audio_host_api: str = "Auto"

        # ── Config register (C0=0x00) — composed full ──────────────────
        # C1: sample rate bits[1:0]
        # C2: OC-output pattern bits[7:1] + CW-eer bit[0]
        # C3: preamp / ADC config (unused for now)
        # C4: duplex bit[2] + NDDC bits[6:3] (4-bit field per IM-1) +
        #     antenna selection
        # Keep composed so any single-bit change can recompose + resend.
        #
        # Phase 1 v0.1 (2026-05-11) bug-fix: ``_config_c4`` is the
        # MAIN-LOOP C4 byte = ``(nddc-1)<<3 | duplex`` = 0x18 | 0x04
        # = 0x1C for HL2 nddc=4 + duplex.  Earlier value 0x04 here
        # was a v0.0.9.x leftover (nddc=1).  When Phase 1 flipped
        # ``HL2Stream._config_c4`` to 0x1C, this duplicate at the
        # Radio layer was missed -- so any band change that fired
        # ``_send_full_config`` (filter board enabled → OC bit
        # change → recompose register 0x00) wrote 0x04 to the
        # wire, telling the gateware to drop back to nddc=1.  The
        # parser still expected 26-byte slots → garbage IQ →
        # blood-red waterfall + S9+72 meter + pulsing audio.
        # Single-source-of-truth fix: read C4 from the stream's
        # ``_config_c4`` at send time (``_send_full_config``),
        # leaving this field only as the discovery-time fallback
        # before the stream is constructed.
        self._config_c1 = SAMPLE_RATES[self._rate]
        self._config_c2 = 0x00
        self._config_c3 = 0x00
        self._config_c4 = 0x1C   # nddc=4 + duplex (Phase 1 v0.1)

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
        # ── v0.0.9.6 round 16: AudioMixer DISABLED diagnostically ──────
        # The audio mixer thread (lyra/dsp/audio_mixer.py), even when
        # idle on its Ready semaphore, appears to add ~5-7% throughput
        # loss to the DSP worker via Python GIL pressure -- field-
        # measured at v0.0.9.6 round 15: PC Sound feed rate 93-95% of
        # nominal with periodic stumbling underrun spikes.  Hypothesis:
        # carrying a 4th Python thread (DSP / EP2 writer / mixer / Qt
        # main) increases per-cycle GIL switching cost beyond what the
        # DSP worker can absorb without falling behind real-time.
        #
        # For v0.0.9.6 we set ``_audio_mixer = None`` -- both sinks
        # accept ``mixer=None`` and fall back to the round-9 legacy
        # direct path: AK4951Sink writes straight to HL2 deque,
        # SoundDeviceSink writes straight to RMatch, no separate
        # audio mixer thread.
        #
        # Wire-cadence pacing for HL2 audio (the architectural reason
        # the mixer + lockstep was added in the first place) comes
        # back via WaitableTimerEx HIGH_RESOLUTION inside the EP2
        # writer thread directly -- no extra Python thread needed,
        # so no GIL pressure.  Lands as the next v0.0.9.6 task.
        self._audio_mixer = None
        self._audio_sink = NullSink()
        # Audio block size — historical inner-loop drain unit for
        # the legacy channel.process() path (deleted in Phase 5,
        # v0.0.9.6).  WDSP picks its own internal frame size, so
        # this constant no longer drives anything in the live audio
        # chain.  Kept on the Radio object because:
        #   1. ``_emit_tone(len(samples))`` reads ``samples`` length
        #      directly, but a few legacy callers still pass
        #      ``self._audio_block`` to size test buffers; rather
        #      than chase those down with Phase 5's surgery, we
        #      retire the field in the Phase 8 orphan setter sweep.
        #   2. ``PythonRxChannel.__init__`` still accepts a
        #      ``block_size`` kwarg (no-op, will be dropped in
        #      Phase 8); passing the constant keeps the call site
        #      diff-free this commit.
        # Original tuning rationale: 512 was chosen v0.0.9.1 to bring
        # worker burst length down from 43 ms (block_size=2048) to
        # 10.7 ms so the AK4951 EP2 builder's 2.6 ms polling cadence
        # could keep its 4800-sample pre-fill from draining empty
        # under producer jitter.
        self._audio_block = 512
        self._tone_phase = 0.0

        # Stream-gap tracking attributes (``_last_seen_seq_errors``,
        # ``_gap_fade_samples``) used to drive a 10 ms post-AGC
        # linear fade-in inside ``_apply_agc_and_volume`` when the
        # UDP RX socket dropped a frame.  Phase 6.A deleted that
        # method as orphan; WDSP doesn't know about UDP-frame-level
        # discontinuity so the fade is no longer applied.  Logged
        # for restoration in Phase 9.5 if operators report audible
        # clicks on lossy networks.

        # RX DSP channel — the WDSP integration seam. Owns its own
        # decimator, audio buffer, demod instances, NR, and notch
        # chain; Radio configures via setters and feeds IQ into
        # process(). See lyra/dsp/channel.py for the full contract.
        from lyra.dsp.channel import PythonRxChannel
        self._rx_channel: PythonRxChannel = PythonRxChannel(
            in_rate=self._rate,
        )
        # Phase 3.D #1 — register the NR capture-done callback so
        # captures complete with a Qt signal emission.  The callback
        # runs on whatever thread NR.process() lives on (worker
        # thread in worker mode, Qt main otherwise); Qt signal emit
        # is thread-safe and the slot connection lands on the main
        # thread via auto/queued connection.
        self._rx_channel.set_nr_capture_done_callback(
            self._on_nr_capture_done)
        # P1.2 — staleness callback.  NR fires this on the audio
        # thread when the loaded captured profile drifts; we emit a
        # Qt signal here, which Qt routes to the UI thread via auto/
        # queued connection for the toast notification.
        self._rx_channel.set_nr_staleness_callback(
            self._on_nr_profile_stale)
        # Track the active captured profile name so the UI can show
        # which profile is currently loaded (and so we can persist
        # the selection across Lyra restarts).  "" = no profile.
        self._active_captured_profile_name: str = ""
        # And the small metadata bundle the inline status badge needs
        # for display: when the profile was captured, on what freq
        # and in what mode.  Populated by load_saved_noise_profile
        # and save_current_capture_as.  None = no profile loaded
        # (or one captured before this metadata was tracked).
        self._active_captured_profile_meta: Optional[dict] = None

        # AGC operator-facing state.  The actual envelope tracking +
        # gain calculation is done by the WDSP engine (constructed
        # below).  These fields are kept for UI bindings (custom-
        # AGC sliders, threshold display, profile persistence) and
        # for the "auto threshold" feature.  The legacy per-sample
        # peak tracker that consumed _agc_release / _agc_hang_blocks
        # / _agc_target directly was retired in v0.0.9.3 -- those
        # fields are now informational only on the audio path; the
        # WDSP engine derives its time constants from operator-
        # facing presets via ``_wdsp_rx.set_agc(mode)``.  The Python
        # WdspAgc wrapper that used to forward these was deleted in
        # Phase 6.A.
        # AGC threshold, in dBFS.  This is WDSP's
        # SetRXAAGCThresh parameter — the noise-floor reference
        # used to compute ``max_gain`` (= the AGC's gain ceiling).
        # Lower threshold → larger max_gain → AGC boosts weak
        # signals more.  Higher threshold → smaller max_gain →
        # AGC compresses earlier.  Operator-facing range
        # ``_AGC_THRESH_MIN_DBFS .. _AGC_THRESH_MAX_DBFS``;
        # default -100 dBFS (~70 dB AGC headroom; Thetis-default
        # territory).  v0.0.9.8 fix: was previously a 0..1 linear
        # field with the wrong semantic (output target rather
        # than input threshold) and never reached WDSP — see
        # ``set_agc_threshold`` for the fix history.
        self._agc_target = -100.0        # dBFS, UI-displayed and pushed to WDSP
        self._agc_profile = "med"        # off / fast / med / slow / long / auto / custom
        self._agc_release = 0.003        # custom-slider value, UI only
        self._agc_hang_blocks = 23       # custom-slider value, UI only
        # Rolling noise-floor estimate -- legacy field, kept for
        # auto_set_agc_threshold's "calibrate above ambient noise"
        # behavior.  No longer updated automatically (the legacy
        # tracker that wrote to _noise_history is gone); auto-AGC
        # uses whatever value is here, defaulting to 1e-4 (-80
        # dBFS) on cold start.  Returns as a Settings-controlled
        # WDSP hang_thresh slider in a future release.
        self._noise_baseline = 1e-4

        # ── AGC ─────────────────────────────────────────────────────
        # AGC runs entirely inside the WDSP engine (wcpAGC, look-
        # ahead 5-state soft-knee).  The Python-side WdspAgc wrapper
        # that originally drove `_apply_agc_and_volume` was deleted
        # in Phase 6.A (v0.0.9.6) — see the deletion note where
        # `_apply_agc_and_volume` used to live.  AGC mode/profile
        # synchronization with WDSP happens through
        # `self._wdsp_rx.set_agc(self._wdsp_agc_for(name))` in
        # set_agc_profile.

        # ── WDSP RX engine (v0.0.9.6 audio rebuild) ──────────────────
        # Native WDSP RX channel via cffi.  WDSP is the only DSP path
        # as of Phase 3 of the legacy-cleanup arc (CLAUDE.md §14.9).
        # The engine takes IQ in at the current ``_rate`` and produces
        # 48 kHz stereo audio out.  Decim, notches, demod, AGC, NR,
        # ANF, filters, mode dispatch all happen inside the engine in
        # its own GIL-free C thread; Lyra applies output-stage volume
        # / mute / capture-feed / BIN post-processor / TCI tap on top.
        self._wdsp_rx = None
        self._wdsp_rx_in_rate: int = 0
        # v0.2 Phase 2 (7/N): TX WDSP channel for the SSB modulator
        # chain.  Mirrors the _wdsp_rx pattern -- lazy-opened when
        # the stream starts, closed when the stream stops.  Channel
        # index = 4 per consensus §2.2 locked host-channel-ID
        # convention (Round 2 verified Thetis source: TX is 2*2=4
        # for CMrcvr=2, CMsubrcvr=2 on HL2).
        self._tx_channel = None
        # v0.2 Phase 2 commit 7-redo (2026-05-15): dedicated TX DSP
        # worker thread.  Replaces the broken inline-dispatch path
        # from commit 7 + the env-var gate from commit 7.1.
        #
        # Mic samples arrive in 38-sample int16 blocks per EP6
        # datagram (HL2+ AK4951 path) or operator-blocksize float32
        # blocks per PortAudio callback (PC sound card path).  Both
        # producer paths call ``TxDspWorker.submit`` (non-blocking
        # queue put) and the worker thread drains, runs
        # ``TxChannel.process`` (which blocks ~10 ms per in_size=512
        # block on WDSP's ``fexchange0``), then pushes the resulting
        # complex64 I/Q to ``HL2Stream.queue_tx_iq`` -- gated by
        # ``HL2Stream.inject_tx_iq`` which Phase 3 PTT state machine
        # flips on MOX=1 edge.
        #
        # Lifecycle tied to stream start/stop: worker is constructed
        # + started in ``start()`` (after ``_open_tx_channel``),
        # stopped + dropped in ``stop()`` (before
        # ``_close_tx_channel``).
        self._tx_dsp_worker = None
        # v0.2 Phase 2 commit 9 (consensus §8.2): sip1 TX I/Q tap.
        # Bounded ring buffer of recent outgoing TX I/Q samples for
        # v0.3 PureSignal calibration to align against the DDC0+DDC1
        # feedback path.  Producer side wired in v0.2 (TxDspWorker
        # writes each on-air I/Q block); consumer added in v0.3.
        # Constructed alongside TxDspWorker on stream start; cleared
        # + dropped on stream stop.
        self._tx_iq_tap = None
        # v0.2 Phase 2 commit 10 (consensus §8.5): 50 ms cos² MOX-edge
        # fade.  Anti-click envelope applied to TX I/Q at PTT
        # keydown/keyup transitions so the gateware doesn't produce
        # spectral splatter from hard amplitude steps.  Constructed
        # alongside TxDspWorker on stream start; dropped on stop().
        # Phase 3 PTT state machine calls start_fade_in() /
        # start_fade_out() at MOX edges.  v0.2 keeps the fade in OFF
        # state permanently since no caller flips it.
        self._mox_edge_fade = None
        # Phase 2 v0.1 (2026-05-11): second WDSP RX channel for RX2.
        # Mirrors ``_wdsp_rx`` lifecycle (created/recreated together
        # in ``_open_wdsp_rx``).  RX2's mode/filter/AGC follow RX1
        # automatically until Phase 3 wires per-RX UI; only the NCO
        # frequency (via ``_set_rx2_freq``) and the L/R pan position
        # are RX2-specific in Phase 2.
        #
        # HL2/HL2+ hardware nuance (operator-verified 2026-05-11):
        # the front-end BPF behavior is more permissive than a
        # strict "RX2 must match RX1's band" rule.  In-ham-band
        # tuning engages the band's BPF; out-of-ham-band tuning
        # (BCB / SWL between amateur allocations) often bypasses
        # to broader RX.  Operator confirmed cross-band dual-RX
        # works in practice (RX1=40m FT8 + RX2=WWV 15 MHz both
        # audible simultaneously) -- the DDCs share one wideband
        # ADC sample stream and tune independently within whatever
        # the front-end is passing at the time.
        #
        # ANAN/Brick rigs with wider ADCs + per-DDC filter switching
        # have even more cross-band flexibility; their capability
        # will be flagged on RadioCapabilities (cross_band_rx2 or
        # similar) in v0.4 multi-radio work.  Phase 3 UI should
        # NOT hard-block VFO B to RX1's band -- it should warn
        # only when the operator's setup would clearly attenuate
        # the RX2 freq (operator-config-dependent).
        # See CLAUDE.md §6.7 disciplines + Phase 3 UI note.
        self._wdsp_rx2 = None

        # ── Phase 3.A v0.1 (2026-05-12) per-RX state scaffolding ─────
        #
        # Consensus plan §6.1 (hybrid focus model) + §6.7 (active-VFO
        # indicators + swap controls) + §6.8 (per-RX volume + mute).
        # Phase 3.A introduces the per-RX state fields + focus state
        # field; Phase 3.B+ introduces target_rx semantics on setters
        # + UI consumers that let RX2 state diverge from RX1.
        #
        # Initialization invariant for Phase 3.A: per-RX state mirrors
        # RX1 state at construction time AND stays in lock-step with
        # RX1 state through the Phase 2 fan-out path in the existing
        # setters.  That keeps Phase 3.A operator-visible behavior
        # identical to Phase 2 (RX2 follows RX1).  Phase 3.B switches
        # the setters to ``target_rx`` semantics that respect the
        # focused RX, at which point the per-RX state starts
        # genuinely diverging.
        #
        # Field naming convention: ``_<base>`` is RX1's state; the
        # corresponding RX2 mirror is ``_<base>_rx2``.  The
        # ``_resolve_rx_target`` helper (added below) maps a
        # ``target_rx`` parameter (0 / 2 / None) to the right state
        # access pattern + WDSP channel.

        # Focused RX -- canonical host channel ID per §1.1.  0 = RX1,
        # 2 = RX2.  Default 0 preserves single-RX UI semantics
        # before Phase 3.B's UI changes land.  Persisted to QSettings
        # so the operator's last-focused RX is restored on launch.
        self._focused_rx: int = 0

        # Per-RX operator state mirrors.  At init these are exact
        # copies of RX1's state -- Phase 2 fan-out in set_mode /
        # set_agc_profile / set_af_gain_db / set_agc_threshold keeps
        # them in sync.  Phase 3.B replaces fan-out with target_rx
        # dispatch and these start tracking RX2-specific operator
        # changes independently.
        self._mode_rx2: str = self._mode
        self._rx_bw_by_mode_rx2: dict = dict(self._rx_bw_by_mode)
        self._agc_profile_rx2: str = self._agc_profile
        self._agc_target_rx2: float = self._agc_target
        self._af_gain_db_rx2: int = self._af_gain_db
        # Phase 3.E.1 v0.1 (2026-05-12): panadapter source RX.
        # Controls which RX's IQ stream feeds the FFT + waterfall.
        # Default = 0 (RX1).  Auto-tracks ``_focused_rx`` so when
        # the operator clicks VFO B's LED the panadapter retunes
        # to RX2's band; Phase 3.E.2 TX integration decouples this
        # to allow a "TX override" path (panadapter shows the TX
        # VFO regardless of focus, then returns on PTT release).
        self._panadapter_source_rx: int = 0
        # Phase 3.D v0.1 (2026-05-12): per-RX volume + mute state.
        # The UI only surfaces Vol-A / Vol-B / Mute-A / Mute-B when
        # ``dispatch_state.rx2_enabled`` is True (per consensus plan
        # §6.8); when RX2 is off, the single Vol / Mute control
        # writes to RX1's per-RX state and the mixer's RX2 path is
        # zeroed anyway.  RX1 fields keep their existing names (
        # ``_volume`` / ``_muted``) so Phase 0 callers aren't
        # disturbed.
        self._volume_rx2: float = self._volume
        self._muted_rx2: bool = self._muted

        # Phase 4 (v0.1.0) RX2 persistence: when restoring from
        # QSettings on startup, the SUB-rising-edge mirror in
        # ``set_rx2_enabled`` would clobber the persisted RX2
        # vol/mute/AF gain with RX1's values.  ``autoload_rx2_state``
        # flips this flag to True around the dispatch-state restore
        # so persisted RX2 state survives.  Always False in normal
        # operator-driven SUB toggles so the bench-test speaker-blast
        # safety net stays armed.
        self._suppress_sub_mirror: bool = False

        # Captured-profile IQ-domain engine (§14.6, v0.0.9.9).
        # Created/recreated alongside the WDSP channel so the
        # engine's IQ rate always matches the radio's.  None until
        # _open_wdsp_rx successfully completes.  Lifecycle:
        #   * Created in _open_wdsp_rx
        #   * Recreated on rate change (_open_wdsp_rx tears down +
        #     rebuilds at the new rate; profiles captured at the
        #     old rate remain on disk but won't be applicable until
        #     the operator switches back)
        #   * Profile loaded via Radio.load_saved_noise_profile
        #     once the engine exists
        #
        # Thread safety: the worker thread reads/writes engine
        # state (~188 calls/sec at 192 kHz IQ) via _do_demod_wdsp's
        # IQ tap, while UI calls (begin_capture / cancel_capture /
        # load_profile / clear_profile / save / has_profile /
        # progress) come from the Qt main thread.  CapturedProfileIQ's
        # docstring is explicit that it's NOT thread-safe across
        # multiple owners, so Radio mediates with this RLock.
        # Contention is essentially zero (worker holds for tens of
        # microseconds per accumulate; UI calls are operator-rate).
        # RLock so a method that takes the lock can call into
        # another locked method without deadlock.
        self._iq_capture: CapturedProfileIQ | None = None
        self._iq_capture_lock = threading.RLock()

        # ── Dispatch state product (v0.1 Phase 0, consensus-plan §4.2.x) ──
        # Single frozen snapshot owned by the Qt main thread.  Mutation
        # is exclusively via ``set_mox`` / ``set_ps_armed`` /
        # ``set_rx2_enabled`` / ``set_radio_family``, each of which
        # produces a new instance via ``dataclasses.replace`` and emits
        # ``dispatch_state_changed``.  Reader threads (RX loop, DSP
        # worker) call ``snapshot_dispatch_state()`` which is a
        # GIL-atomic reference read; no lock required.  See module
        # docstring at ``lyra/radio_state.py``.
        self._dispatch_state: DispatchState = DispatchState()

        # ── Captured-profile bypass edge-detector flag (R5-2 / §4.2.x) ──
        # Tracks "was the captured-profile pre-pass active on the
        # PREVIOUS WDSP block?" so the bypass-detector in
        # ``_do_demod_wdsp`` can flush the STFT overlap buffer on the
        # rising edge of MOX+PS (entering bypass).  Without this flag
        # initialized here, the first ``_do_demod_wdsp`` call hits
        # ``AttributeError`` -- the edge detector reads this attribute
        # unconditionally on every block.  Phase 1 wires the actual
        # bypass logic; Phase 0 just ships the attribute.
        self._captured_profile_was_active: bool = False
        # FFT size used for new captures and engine init.
        # Operator-configurable via Settings → DSP → Captured
        # Profile → FFT size dropdown (Phase 5c).  Persisted via
        # QSettings ``noise/iq_capture_fft_size``.  Existing
        # profiles stamp their own fft_size into the JSON so
        # loaded profiles are self-describing regardless of what
        # the runtime default is at apply time.
        try:
            from PySide6.QtCore import QSettings as _QS
            _s = _QS("N8SDR", "Lyra")
            _fft = int(_s.value(
                "noise/iq_capture_fft_size",
                CapturedProfileIQ.DEFAULT_FFT_SIZE,
                type=int))
            if _fft not in (1024, 2048, 4096):
                _fft = CapturedProfileIQ.DEFAULT_FFT_SIZE
            self._iq_capture_fft_size: int = _fft
        except Exception:
            self._iq_capture_fft_size = (
                CapturedProfileIQ.DEFAULT_FFT_SIZE)
        # Gain-smoothing coefficient for the temporal-smoothing
        # IIR on the Wiener gain mask.  Operator-tunable via
        # Settings → DSP → Captured Profile → Gain smoothing
        # slider (Phase 5b).  Persisted via QSettings
        # ``noise/gain_smoothing``.  Live-pushed to the engine
        # on slider change AND seeded into the engine at each
        # _open_wdsp_rx so a fresh engine starts at the
        # operator's last-set value.
        try:
            from PySide6.QtCore import QSettings as _QS
            _s = _QS("N8SDR", "Lyra")
            _g = float(_s.value(
                "noise/gain_smoothing",
                CapturedProfileIQ.DEFAULT_GAIN_SMOOTHING,
                type=float))
            self._iq_capture_gain_smoothing: float = max(
                0.0, min(0.95, _g))
        except Exception:
            self._iq_capture_gain_smoothing = (
                CapturedProfileIQ.DEFAULT_GAIN_SMOOTHING)
        try:
            self._open_wdsp_rx(self._rate)
        except Exception as exc:
            # WDSP DLL set is bundled at lyra/dsp/_native/ — should
            # never fail in production.  If it does (corrupt install,
            # missing DLL, ABI mismatch), Lyra can't function: there
            # is no fallback DSP path, and TX/PS work also depends on
            # WDSP.  Raise a clear, actionable error rather than
            # crashing later in some confusing way.
            raise RuntimeError(
                "Lyra requires the bundled WDSP DLL set at "
                "lyra/dsp/_native/ (wdsp.dll, libfftw3-3.dll, "
                "libfftw3f-3.dll, rnnoise.dll, specbleach.dll).  "
                f"Engine initialization failed: {exc}.  "
                "Reinstall Lyra or check that the _native/ directory "
                "is present and readable."
            ) from exc
        # Mirror Lyra's notch-mutation signal into WDSP's notch DB
        # without scattering the push call across every mutator
        # method.  notches_changed fires whenever any of add_notch /
        # remove_nearest_notch / set_notch_*_at / clear_notches
        # touches state — _push_wdsp_notches re-derives the WDSP
        # database from the current Notch list on every emission.
        # Only matters in WDSP mode; the helper no-ops otherwise.
        try:
            self.notches_changed.connect(
                lambda _details: self._push_wdsp_notches()
            )
        except Exception as exc:
            print(f"[Radio] notches→WDSP connect failed: {exc}")

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

        # NCDXF beacon auto-follow — fires every second so the pump
        # catches each 10-second slot transition reliably regardless
        # of when follow was activated.  The pump itself is cheap (4
        # arithmetic ops) and a no-op when follow is off, so the
        # 1 Hz cadence is fine.  Start whenever the operator picks a
        # follow station; stop when they pick "Off" (handled inside
        # set_ncdxf_follow_station via _ncdxf_follow_pump).
        self._ncdxf_follow_station: Optional[str] = None
        self._ncdxf_follow_timer = _QTimer(self)
        self._ncdxf_follow_timer.setInterval(1000)
        self._ncdxf_follow_timer.timeout.connect(self._ncdxf_follow_pump)

        # Notch bank — list of Notch dataclasses (see top of file).
        # Operators add/remove via right-click on spectrum/waterfall;
        # each notch carries its own width, depth, cascade, and active
        # flag.  v0.0.7.1 notch v2: replaced the old iirnotch +
        # deep=bool design with a parametric peaking-EQ biquad +
        # cascade integer.  See ``docs/architecture/notch_v2_design.md``.
        self._notches: list[Notch] = []
        self._notch_enabled = False
        # Per-notch defaults applied to newly-placed notches.  Operator
        # can change via Settings → Notches → "Default for new notches"
        # or by right-clicking and picking a preset before placing.
        # 40 Hz width: at typical heterodyne center frequencies
        # (1-3 kHz) this gives Q ~ 25-75 — narrow enough to surgically
        # remove a whistle without taking out adjacent voice content.
        # -50 dB depth + 2-stage cascade matches the "Normal" preset:
        # delivers operator-promised attenuation across the kill
        # region, sharp shoulders, predictable transition.
        self._notch_default_width_hz = 40.0
        self._notch_default_depth_db = -50.0
        self._notch_default_cascade = 2
        # Legacy compat — still read by the right-click default-deep
        # toggle on older code paths.  Tracks (cascade > 1).  Setter
        # below keeps the integer cascade in sync.
        self._notch_default_deep = self._notch_default_cascade > 1

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
        # Edge-lock flags (2026-05-08).  Tracks whether the operator
        # has explicitly set the floor or ceiling via drag / slider
        # / settings.  When True, auto-scale honors that side:
        #   * Floor lock — auto never moves the floor; operator owns it.
        #   * Ceiling lock — auto can RAISE above the operator's value
        #     if signals exceed it, but never lowers below it.
        # The asymmetry is deliberate: if a strong signal arrives we
        # WANT the display to show it (don't squeeze it off-screen),
        # but the floor is purely about how much noise space the
        # operator wants to see — auto has no business overriding
        # that.  Set per-edge by `set_spectrum_db_range(from_user=True)`
        # based on which edge changed.  Persisted per-band via
        # _save_current_band_range / _apply_band_range.  Cleared by
        # reset_spectrum_db_locks() (right-click on dB zone, or the
        # "Reset display range" Settings affordance).
        self._user_floor_locked   = False
        self._user_ceiling_locked = False
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
        self._fft_interval_ms = 25   # 40 Hz default — common SDR convention
        self._waterfall_divider = 1
        self._waterfall_tick_counter = 0

        # ── Radio-side instrumentation (LYRA_PAINT_DEBUG=1) ──────────
        # When enabled, counts how often _tick_fft fires vs how often
        # spectrum_ready is actually emitted, and how much main-thread
        # time _on_samples_main_thread + _do_demod consume per 5 sec
        # window. The point is to disambiguate three scenarios when
        # the panadapter feels slow:
        #   (a) FFT timer is firing on schedule but ring is drained
        #       between ticks  → emit count low, tick count = expected
        #   (b) FFT timer is starved (main thread busy with DSP)
        #       → tick count low (e.g. 5/s instead of 30/s)
        #   (c) DSP is fine but something else is on the main thread
        #       → tick + emit both fine, lag must be elsewhere
        import os as _os
        self._radio_debug = (_os.environ.get("LYRA_PAINT_DEBUG", "")
                             .strip() in ("1", "true", "True"))
        # ── §15.7 audio/spectrum/waterfall sync instrumentation ──────
        # LYRA_TIMING_DEBUG=1 enables rate-limited (1 sec) timing
        # measurements at four points in the signal flow.  Output goes
        # to stdout in dev-tree runs and to ``%APPDATA%\Lyra\crash.log``
        # in the PyInstaller windowed build (per v0.20 stdio redirect).
        # Used to confirm or refute operator-perceived skew between
        # the three live RX surfaces.  See CLAUDE.md §15.7 for the
        # investigation context.
        self._timing_debug = (_os.environ.get("LYRA_TIMING_DEBUG", "")
                              .strip() in ("1", "true", "True"))
        self._timing_stats = _TimingStats() if self._timing_debug else None
        self._dbg_t0_window: float = 0.0
        self._dbg_fft_ticks: int = 0
        self._dbg_fft_emits: int = 0
        self._dbg_samples_calls: int = 0
        self._dbg_samples_total_ms: float = 0.0
        self._dbg_samples_max_ms: float = 0.0
        # Per-stage timing inside _do_demod so we can pinpoint WHICH
        # DSP stage is causing the 65ms spike. Track max + total per
        # 5-second window.
        self._dbg_stage_max = {
            "channel": 0.0, "agc": 0.0, "bin": 0.0, "sink": 0.0}
        self._dbg_stage_total = {
            "channel": 0.0, "agc": 0.0, "bin": 0.0, "sink": 0.0}
        self._dbg_largest_iq_n: int = 0   # biggest single batch this window
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
        self._band_plan_show_ncdxf = True   # NCDXF beacon markers — independent toggle
        self._band_plan_edge_warn = True

        # Operator / Station identification.  Global settings used
        # by multiple features (TCI spots, WX-Alerts, future
        # logging integration).  Loaded from QSettings on first
        # access of autoload_operator_settings(); empty until then.
        # Lat/lon are computed from the grid square when valid; the
        # manual-override fields are stored separately so they
        # survive a grid edit.
        self._callsign: str = ""
        self._grid_square: str = ""
        self._operator_lat_manual: Optional[float] = None
        self._operator_lon_manual: Optional[float] = None

        # Weather Alerts — disabled by default, behind the operator's
        # disclaimer acceptance.  WxWorker is constructed lazily on
        # first call to set_wx_enabled(True) so we don't import any
        # network code unless the operator actually opted in.
        self._wx_worker = None
        self._wx_enabled: bool = False
        self._wx_disclaimer_accepted: bool = False
        self._wx_last_snapshot = None
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

        # Spectrum smoothing (display-only EWMA). Default OFF so the
        # raw FFT is still the baseline operator view; opt-in via
        # Settings → Display.
        self._spectrum_smoothing_enabled = False
        self._spectrum_smoothing_strength = 4    # 1..10 (higher = smoother)

        # User-picked colors. Empty string means "use the hardcoded
        # default" so the UI can reset by clearing. Segment overrides
        # apply on top of band_plan.SEGMENT_COLORS.
        self._spectrum_trace_color: str = ""    # e.g. "#5ec8ff"
        # Spectrum trace fill — gradient-filled area below the trace
        # line.  Default ON (matches pre-patch behavior — was always
        # on before).  Operator can disable for a "line-only" look.
        # Color "" means "derive from trace color"; explicit hex
        # uses that hex for the fill regardless of trace color.
        self._spectrum_fill_enabled: bool = True
        self._spectrum_fill_color:   str  = ""
        self._segment_colors: dict[str, str] = {}  # kind → hex override
        self._noise_floor_color: str = ""       # NF line color override
        self._peak_markers_color: str = ""      # peak marker color override

        # NOTE: Audio Leveler removed in Phase 4 of legacy-DSP cleanup
        # (CLAUDE.md §14.9).  WDSP's AGC FAST/MED/SLOW/LONG modes
        # subsume the dynamic-range work the soft-knee compressor
        # used to do.  See commit history for the deleted state +
        # API surface if anyone needs to recover it.

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
        # Phase 3.D #1 — NR noise SOURCE toggle.  Independent of
        # _nr_profile (which only controls subtraction aggression).
        # Default off — fresh install gets the live VAD-tracked
        # estimate, same as v0.0.5 NR1 behavior.  Operator flips on
        # when they have a captured profile they want to use, OR
        # save_current_capture_as auto-flips it on after a successful
        # save.  Persisted via QSettings noise/use_captured_profile.
        self._nr_use_captured_profile: bool = False

        # NR mode (1..4) — operator-facing selector that maps to
        # WDSP's EMNR gain_method (0..3 internally).  Replaces the
        # legacy NR1/NR2/Neural backend dropdown + dual strength
        # sliders with a single mode selector that matches Thetis's
        # NR2 mode UX.  Default mode 3 = MMSE-LSA (= old "NR1"
        # default behavior — preserves the audio character of fresh
        # installs).  See _push_wdsp_nr_state for the mode→gain_method
        # mapping.
        self._nr_mode: int = 3
        # AEPF — Adaptive Equalization Post-Filter — anti-musical-
        # noise smoother in WDSP's EMNR output.  Default ON because
        # off is noticeably more "watery" / pronounced spectral-
        # subtraction residue.  Operator can disable to A/B and on
        # really clean bands where the smoothing isn't needed.
        # Persisted via QSettings noise/aepf_enabled.
        self._aepf_enabled: bool = True
        # NPE method — Noise Power Estimator selection.
        # 0 = OSMS (recursive averaging, smoother tracking — WDSP default)
        # 1 = MCRA (Minimum-Controlled Recursive Averaging — newer,
        #     faster-tracking, better for non-stationary band noise)
        # Operator-tunable on the DSP+Audio panel.  Persisted via
        # QSettings noise/npe_method.  Surfacing this knob is one of
        # Lyra's WDSP-UX differentiators (Thetis hides it).
        self._npe_method: int = 0

        # APF — Audio Peaking Filter (CW only). Operator can tune
        # bandwidth + gain via Settings → DSP → CW.  Default OFF —
        # opt-in feature.  Constants below are the Radio-level source
        # of truth (Phase 4 inlined them from `lyra/dsp/apf.py` to
        # decouple radio.py from the legacy DSP module).
        self._apf_enabled: bool = False
        self._apf_bw_hz: int = self.APF_BW_DEFAULT_HZ
        self._apf_gain_db: float = self.APF_GAIN_DEFAULT_DB
        # Push initial values into the channel so its APF is in sync
        # with Radio state from frame 0 (channel was just constructed
        # earlier in __init__ with bare defaults).
        self._rx_channel.set_apf_bw_hz(self._apf_bw_hz)
        self._rx_channel.set_apf_gain_db(self._apf_gain_db)
        self._rx_channel.set_apf_enabled(self._apf_enabled)

        # BIN — Binaural pseudo-stereo. Lives in Radio (not the
        # channel) because it produces stereo output and the audio
        # sinks own the stereo plumbing. Default OFF; default depth
        # is BinauralFilter.DEPTH_DEFAULT (~0.7, strong but not
        # extreme). Runs LAST in the audio chain — after AGC, AF,
        # Volume, and the tanh limiter — so the spatial split is
        # the final transform before the sink applies L/R balance.
        from lyra.dsp.binaural import BinauralFilter as _BIN
        self._bin_enabled: bool = False
        self._bin_depth: float = _BIN.DEPTH_DEFAULT
        self._binaural = _BIN(
            sample_rate=PythonRxChannel.AUDIO_RATE,
            depth=self._bin_depth,
        )

        # ── DSP threading mode (v0.0.9.2 audio rebuild Commit 1) ─────
        # Operator preference for whether DSP runs on the Qt main
        # thread ("single", legacy fallback) or a dedicated worker
        # thread ("worker", default as of v0.0.9.2).  Changes are
        # persisted via QSettings under ``dsp/threading_mode`` and
        # only take effect on Lyra restart — the worker thread is
        # set up once at Radio construction.
        #
        # **Why the default flipped (audio_rebuild_v0.1.md sec 3.1):**
        # the Qt main thread runs paint events, mouse handling, and
        # signal dispatch.  Co-locating the audio DSP chain on it
        # caused producer-side jitter that drained the EP2 / sound-
        # device ring buffers and produced clicks.  Worker mode
        # isolates DSP from UI activity.
        #
        # **Operator escape hatch:**
        # Settings → DSP → Threading combo offers "Single-thread
        # (legacy)" + "Worker (default)".  If a regression appears
        # on any rig the operator can flip back without a rebuild.
        # See audio_rebuild_v0.1.md sec 9.3.
        #
        # Two values tracked:
        #   _dsp_threading_mode_at_startup  — what was loaded when
        #     Radio was constructed; the mode currently RUNNING
        #   _dsp_threading_mode             — operator's currently
        #     selected preference; what gets persisted; what will
        #     be RUNNING after the next restart
        # When the two differ, the Settings dialog displays a
        # "restart required" hint to the operator.
        #
        # **QSettings ordering note:** prior to Commit 1 the load
        # happened in ``app.py::_load_settings()`` AFTER Radio init,
        # which meant operator opt-in to worker mode set the flag
        # but never started the worker thread (Radio init had
        # already taken the SINGLE branch by the time the override
        # ran).  Read it here, BEFORE the worker-construction
        # decision below, so the persisted preference actually
        # takes effect.
        from PySide6.QtCore import QSettings as _QS
        _persisted = _QS("N8SDR", "Lyra").value("dsp/threading_mode", None)
        if _persisted is not None:
            _mode = str(_persisted).strip().lower()
            if _mode not in self.DSP_THREADING_MODES:
                _mode = self.DSP_THREADING_WORKER
        else:
            _mode = self.DSP_THREADING_WORKER  # v0.0.9.2 default
        self._dsp_threading_mode_at_startup: str = _mode
        self._dsp_threading_mode: str = _mode
        print(f"[Radio] DSP threading mode: "
              f"{self._dsp_threading_mode_at_startup}")
        # Worker thread + DspWorker — constructed only when worker
        # mode is the active mode at startup. Stored on Radio so
        # they survive for the radio's lifetime; shut down via
        # shutdown_dsp_worker() at app close.
        self._dsp_worker = None      # type: Optional["DspWorker"]
        self._dsp_worker_thread = None
        if self._dsp_threading_mode_at_startup == self.DSP_THREADING_WORKER:
            self._build_and_start_dsp_worker()

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
        # against an external reference receiver with WWV @ 10 MHz
        # AM 8K and 40 m noise floor as known-strength signals.
        # Math chain:
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
        # Peak-hold-with-decay state for "peak" mode.  Without this,
        # the meter snaps to whatever single bin was loudest in each
        # ~5 Hz FFT tick, which jumps ±6 dB block-to-block on voice
        # content (operator perceives it as jittery vs Thetis-style
        # smoothness).  With peak-hold:
        #
        #   - on each tick, if the new peak exceeds the held value,
        #     the displayed value snaps to it (fast attack)
        #   - otherwise, the held value decays toward the new peak
        #     by a fixed factor per tick (slow release)
        #
        # 0.85 per 200 ms tick → ~500 ms decay time constant, the
        # same feel as analog mechanical S-meters and Thetis's
        # default meter response.  Operator can tune later.
        self._smeter_peak_hold_lin = 0.0
        self._SMETER_PEAK_DECAY = 0.85
        # ── Squelch (WDSP mode) ──────────────────────────────────
        # All-mode squelch is handled natively inside WDSP (FM SQ /
        # AM SQ / SSQL via `_push_wdsp_squelch_state`).  No Python-
        # side state needed here — earlier hand-rolled gate designs
        # (audio-RMS, AllModeSquelch delegate, spectrum-SNR) all
        # fought WDSP's AGC compression and lost.  WDSP SSQL
        # operates pre-AGC on the IQ-domain F-to-V detector, exactly
        # where Pratt designed it to live.  See CLAUDE.md §14.8 for
        # the full architecture history.
        self._sample_ring: deque = deque(maxlen=self._fft_size * 4)
        self._ring_lock = threading.Lock()

        # ── Channel state-mirror sync ──────────────────────────────────
        # Push Radio's authoritative per-mode bandwidth + CW pitch +
        # current mode onto the channel.  Phase 5 (v0.0.9.6) reduced
        # the channel to a state container — set_rx_bw is now a
        # no-op (Radio's _rx_bw_by_mode is the only authoritative
        # store), but set_cw_pitch_hz still drives the post-AGC
        # APF's center frequency on _rx_channel._apf, and set_mode
        # flushes the noise-floor estimators on the captured-profile
        # NR1 instance so a fresh capture starts clean.
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
        # Phase 2 v0.1 (2026-05-11): sibling batch accumulator for
        # RX2 (DDC1).  Lock-step with ``_rx_batch`` -- same threshold
        # since per-DDC sample counts are equal at nddc=4 (CLAUDE.md
        # §3.6 HL2 P1 caveat).  Filled by ``_stream_cb_rx2`` on the
        # RX-loop thread; drained when full into the worker's RX2
        # queue (or, in single-thread mode, dropped since the
        # main-thread bridge doesn't carry stereo combine yet --
        # worker mode is the canonical Phase 2 path).
        self._rx2_batch: list = []
        self._rx2_batch_lock = threading.Lock()

        # ── Phase 1 v0.1: RX2 stub-consumer diagnostic state ────────
        #
        # Phase 1 wires the protocol dispatch of DDC1 (RX2) samples
        # but does NOT yet build a full RX2 DSP / audio chain.  Per
        # consensus plan §4.2: "channel 2 has a stub DSP chain that
        # produces zero audio (audio routing comes in phase 2)."
        # ``_stream_cb_rx2`` is the registered consumer for the
        # ``RX_AUDIO_CH2`` ConsumerID slot; in Phase 1 it just
        # increments the diagnostic counters below so we can verify
        # via the verification protocol §4.4 steps 1-5 (bench tests)
        # that DDC1 samples are actually flowing to the right slot.
        #
        # When RX2 is disabled (default for v0.1 first launch) the
        # bytes still flow over the wire and through the parser --
        # they just arrive at this stub which discards them.  Phase
        # 2's RX2 audio chain replaces this stub.
        self._rx2_samples_received: int = 0
        self._rx2_datagrams_received: int = 0

        # Phase 1 RX2 frequency + IQ ring buffer for bench-test
        # verification (consensus plan §4.4 step 2: tune VFO B to a
        # known carrier and verify channel 2's input stream shows
        # that carrier).
        #
        # The ring buffer is sized for ~85 ms at 192 kHz IQ rate
        # (16384 complex samples) -- enough for an 8192-bin FFT with
        # 50% overlap, which gives ~23 Hz bin resolution at 192k.
        # That's plenty to read off WWV's 5 / 10 / 15 / 20 / 25 MHz
        # carriers offset from the DDC1 NCO frequency.  Memory cost:
        # 16384 * 8 bytes = 128 KB.
        #
        # Writers: ``_stream_cb_rx2`` on the RX-loop thread (one
        # writer).  Readers: the Phase 1 bench-test dialog
        # (Help -> RX2 Bench Test...) on the Qt main thread.  Lock
        # is held only for the copy-out window, so the RX-loop
        # thread waits at most a microsecond per fill.
        self._rx2_freq_hz: int = 7250000  # operator-tunable default
        self._rx2_iq_ring: np.ndarray = np.zeros(
            16384, dtype=np.complex64
        )
        self._rx2_iq_ring_pos: int = 0
        self._rx2_iq_ring_lock = threading.Lock()
        # Bench-dialog activity gate -- when False the ring buffer
        # fill in ``_stream_cb_rx2`` skips, saving ~10 ms/sec of
        # CPU on the RX-loop thread at 192 kHz nddc=4 cadence.
        # Phase 1 dialog (rx2_bench_dialog.py) flips this on open
        # and back to False on close.  Phase 2 wiring of real RX2
        # DSP will replace this with a "RX2 enabled" gate driven
        # by the focus-model UI.
        self._rx2_bench_active: bool = False

        # ── Periodic FFT tick ─────────────────────────────────────────
        self._fft_timer = QTimer(self)
        self._fft_timer.timeout.connect(self._tick_fft)
        # Match the default _fft_interval_ms above. Operator can change
        # via the Spec rate slider; that calls set_spectrum_fps which
        # updates this timer's interval live.
        self._fft_timer.start(self._fft_interval_ms)

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
    def notch_details(self) -> list[tuple]:
        """``(freq_hz, width_hz, active, deep, depth_db, cascade)``
        tuples — emitted on ``notches_changed``.  Stable shape so
        UI / TCI subscribers don't depend on the Notch dataclass
        internals.

        v0.0.7.1 notch v2: extended the tuple from 4 to 6 fields
        adding ``depth_db`` and ``cascade``.  Old subscribers that
        unpack 4-tuples are tolerated everywhere we control (the
        spectrum widget falls back to the legacy shape via per-
        item adaptive unpacking).  ``deep`` is kept in position 3
        for backward compat (== ``cascade > 1``)."""
        return [
            (n.abs_freq_hz, n.width_hz, n.active, n.deep,
             n.depth_db, n.cascade)
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

    # ── Dispatch state contract (v0.1 Phase 0, consensus-plan §4.2.x) ───
    #
    # Reader (snapshot) + four setters + a Qt signal.  This is the full
    # read/write surface; Phase 0 has NO live consumers wired (Phase 1
    # adds protocol-layer dispatch + UI subscriptions).  The point of
    # landing the whole surface in Phase 0 is so the Phase 0 → Phase 1
    # hand-off has a testable contract (§4.4 verification step 7
    # programmatically toggles ``set_mox`` and verifies ``ddc_map(state)``
    # returns the expected per-DDC routing).
    #
    # Threading: only the Qt main thread may call the setters.  Reader
    # threads (RX loop, DSP worker) call ``snapshot_dispatch_state()``
    # which is GIL-atomic.  See ``lyra/radio_state.py`` for the rationale.

    def snapshot_dispatch_state(self) -> DispatchState:
        """Return the current DispatchState as a frozen snapshot.

        Safe to call from any thread (RX loop, DSP worker, UI).  The
        return value is a reference to the same frozen dataclass
        instance shared with other readers; ``frozen=True`` plus the
        single-writer-on-main-thread discipline means concurrent
        readers cannot observe a torn state.

        Callers should snapshot ONCE per work-unit (one UDP datagram
        for ``HL2Stream._rx_loop``, one WDSP block for
        ``_do_demod_wdsp``) and use that single reference for all
        decisions within that work-unit.  This avoids "mox flipped
        mid-datagram and now half the DDCs route to TX and half to
        RX" pathologies.
        """
        return self._dispatch_state

    @property
    def dispatch_state(self) -> DispatchState:
        """Convenience property mirror of ``snapshot_dispatch_state()``
        for Qt main-thread readers (panels, dialogs) that want
        property-style access.  Identical semantics: returns the
        current frozen ``DispatchState``.  Phase 3.D v0.1."""
        return self._dispatch_state

    # ── Phase 3.A v0.1: focused-RX surface ──────────────────────────
    @property
    def focused_rx(self) -> int:
        """Current focused RX (host channel ID per §1.1).

        Returns 0 for RX1 or 2 for RX2.  Phase 3.B+ UI components
        (MODE+FILTER panel, DSP+AUDIO panel) read this to bind
        their displays + setters to the focused receiver's state.
        """
        return int(self._focused_rx)

    def set_focused_rx(self, rx_id: int) -> None:
        """Switch focus to ``rx_id`` (0 = RX1, 2 = RX2).

        Called by Phase 3.B+ UI on Ctrl+1 / Ctrl+2 hotkey, click on
        a VFO LED, or middle-click on the panadapter.  Idempotent
        (no-op when rx_id already equals current focus).  Emits
        ``focused_rx_changed(int)`` only on actual transitions.

        Raises ``ValueError`` for unknown ``rx_id`` to catch
        propagated bugs in UI dispatch -- callers should only
        pass 0 or 2.
        """
        new_focus = int(rx_id)
        if new_focus not in (0, 2):
            raise ValueError(
                f"focused_rx must be 0 (RX1) or 2 (RX2); got {new_focus}"
            )
        if new_focus == self._focused_rx:
            return
        prev_focus = int(self._focused_rx)
        self._focused_rx = new_focus
        # ── Phase 3.E.1 hotfix v0.3 (2026-05-12): SUB-off vol mirror ──
        # Operator UX (Rick 2026-05-12): "if you had it turned up to
        # hear something and switch over or restart and flip back,
        # could be nasty -- yes that might be a good safety net."
        #
        # When SUB is off, only the focused RX is audible.  Carrying
        # the previously-active volume/mute forward to the newly-
        # focused RX ensures the operator's working level (whatever
        # they last set) stays in force across the focus flip --
        # there is no sudden level jump from a stale ``_volume_rx2``
        # default (0.5) when they click VFO B with Vol-A trimmed
        # down to 0.2, and no surprise blast when they click VFO A
        # after running RX2 hot.
        #
        # When SUB is on, both RXes are audible simultaneously and
        # the per-RX Vol-A / Vol-B sliders are operator-independent
        # by design (consensus plan §6.8) -- skip the mirror.
        # Scope: volume + mute ONLY.  AF gain is the pre-AGC makeup
        # reference level (not an output stage gain), so a stale
        # ``_af_gain_db_rx2`` does NOT produce the "surprise blast"
        # the safety net targets -- the AGC compresses the result
        # back to its target level regardless of AF Gain setting.
        # Per-RX AF gain independence is the existing Phase 3.C
        # contract; the mirror leaves it alone.
        try:
            if not self._dispatch_state.rx2_enabled:
                if prev_focus == 0 and new_focus == 2:
                    self._volume_rx2 = float(self._volume)
                    self._muted_rx2 = bool(self._muted)
                    try:
                        self.volume_changed_rx2.emit(self._volume_rx2)
                    except Exception:
                        pass
                    try:
                        self.muted_changed_rx2.emit(self._muted_rx2)
                    except Exception:
                        pass
                elif prev_focus == 2 and new_focus == 0:
                    self._volume = float(self._volume_rx2)
                    self._muted = bool(self._muted_rx2)
                    try:
                        self.volume_changed.emit(self._volume)
                    except Exception:
                        pass
                    try:
                        self.muted_changed.emit(self._muted)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[Radio] focus-flip vol mirror error: {exc}")
        try:
            self.focused_rx_changed.emit(new_focus)
        except Exception:
            pass
        # Phase 3.E.1 v0.1 (2026-05-12): focus change auto-updates
        # the panadapter source RX so the panadapter retunes to
        # whatever VFO the operator just focused.  Phase 3.E.2 TX
        # work may decouple this when MOX is active (panadapter
        # stays on TX VFO regardless of focus until PTT release).
        try:
            self.set_panadapter_source_rx(new_focus)
        except Exception:
            pass

    # ── Phase 3.E.1 v0.1: panadapter source RX ──────────────────────
    @property
    def panadapter_source_rx(self) -> int:
        """Which RX's IQ stream currently feeds the FFT + waterfall.
        0 = RX1 (DDC0), 2 = RX2 (DDC1).  Default auto-tracks
        ``focused_rx``; Phase 3.E.2 TX work will add an override
        path for the MOX-active state product."""
        return self._panadapter_source_rx

    def set_panadapter_source_rx(self, rx_id: int) -> None:
        """Set the panadapter source RX.  Idempotent; emits
        ``panadapter_source_changed(int)`` only on transitions.
        Worker flushes its FFT sample ring on the signal so the
        next emitted frame is clean (no mixed-source bins)."""
        new_src = int(rx_id)
        if new_src not in (0, 2):
            raise ValueError(
                f"panadapter_source_rx must be 0 (RX1) or 2 (RX2); "
                f"got {new_src}"
            )
        if new_src == self._panadapter_source_rx:
            return
        self._panadapter_source_rx = new_src
        try:
            self.panadapter_source_changed.emit(new_src)
        except Exception:
            pass
        # Phase 3.E.1 hotfix v0.12 / v0.14 (2026-05-12): both
        # ``marker_offset_hz`` and ``_compute_passband`` are now
        # source-RX-aware (they read RX2's state when source=RX2).
        # Re-emit both on source switch so the spectrum widget
        # repositions marker AND passband rectangle to match the
        # new pane.
        try:
            self._emit_marker_offset()
        except Exception:
            pass
        try:
            self._emit_passband()
        except Exception:
            pass

    def _resolve_rx_target(
        self, target_rx: Optional[int],
    ) -> tuple[int, str]:
        """Map a ``target_rx`` parameter to its canonical RX id +
        state-field suffix.

        Phase 3.B+ setters use this to route writes to the right
        per-RX state fields.  Phase 3.A's existing setters still
        fan out to both channels for back-compat, but the helper
        is here so Phase 3.B can drop in without further plumbing.

        Args:
            target_rx: 0 = RX1, 2 = RX2, None = focused RX.

        Returns:
            ``(rx_id, suffix)`` where ``rx_id`` is the canonical
            channel ID (0 or 2) and ``suffix`` is the per-RX
            state-field suffix: ``""`` for RX1 (e.g., ``_mode``)
            or ``"_rx2"`` for RX2 (e.g., ``_mode_rx2``).  Callers
            use ``getattr(self, f"_mode{suffix}")`` patterns.

        Raises:
            ValueError: For target_rx values other than 0 / 2 /
                None.
        """
        if target_rx is None:
            target_rx = self._focused_rx
        target_rx = int(target_rx)
        if target_rx == 0:
            return (0, "")
        if target_rx == 2:
            return (2, "_rx2")
        raise ValueError(
            f"target_rx must be 0 (RX1), 2 (RX2), or None "
            f"(focused); got {target_rx}"
        )

    # ── Phase 3.C per-RX query accessors ───────────────────────────
    # Panels read these to populate themselves from the focused RX's
    # state.  ``target_rx`` follows the same convention as the
    # setters: 0 = RX1, 2 = RX2, None = focused.
    def mode_for_rx(self, target_rx: Optional[int] = None) -> str:
        rx_id, _ = self._resolve_rx_target(target_rx)
        return self._mode if rx_id == 0 else self._mode_rx2

    def rx_bw_for_rx(
        self, mode: str, target_rx: Optional[int] = None,
    ) -> int:
        rx_id, _ = self._resolve_rx_target(target_rx)
        bw_dict = (self._rx_bw_by_mode if rx_id == 0
                   else self._rx_bw_by_mode_rx2)
        return bw_dict.get(mode, 2400)

    def af_gain_db_for_rx(self, target_rx: Optional[int] = None) -> int:
        rx_id, _ = self._resolve_rx_target(target_rx)
        return self._af_gain_db if rx_id == 0 else self._af_gain_db_rx2

    def agc_profile_for_rx(self, target_rx: Optional[int] = None) -> str:
        rx_id, _ = self._resolve_rx_target(target_rx)
        return (self._agc_profile if rx_id == 0
                else self._agc_profile_rx2)

    def agc_threshold_for_rx(
        self, target_rx: Optional[int] = None,
    ) -> float:
        rx_id, _ = self._resolve_rx_target(target_rx)
        return (self._agc_target if rx_id == 0
                else self._agc_target_rx2)

    def volume_for_rx(self, target_rx: Optional[int] = None) -> float:
        rx_id, _ = self._resolve_rx_target(target_rx)
        return self._volume if rx_id == 0 else self._volume_rx2

    def muted_for_rx(self, target_rx: Optional[int] = None) -> bool:
        rx_id, _ = self._resolve_rx_target(target_rx)
        return self._muted if rx_id == 0 else self._muted_rx2

    def set_mox(self, mox: bool) -> None:
        """Set the MOX axis of DispatchState.  Call from Qt main thread.

        Wired by PTT state machine (v0.1.x onward) on every PTT /
        MOX-button / CW-key / TUN-button edge.  Routes the HPSDR P1
        MOX bit emission in HL2Stream's EP2 writer (via the
        dispatch-state-provider snapshot helper), the DDC enable
        mask, and the captured-profile pre-pass bypass off
        ``state.mox AND state.ps_armed``.

        Idempotent: setting MOX to its current value is a no-op (no
        signal emission, no replace).  This matters because some PTT
        callers fire on every poll cycle, not just edges.

        v0.2 Phase 1 (10/10): also emits ``tx_active_changed(bool)``
        on every MOX edge so UI consumers (FrequencyDisplay red
        treatment, SMeter style flip, spectrum widget passband
        rectangle color per §15.9) get a clean signal without
        needing to subscribe to the full ``dispatch_state_changed``
        and filter for MOX edges themselves.
        """
        new = bool(mox)
        if self._dispatch_state.mox == new:
            return
        from dataclasses import replace
        self._dispatch_state = replace(self._dispatch_state, mox=new)
        self.dispatch_state_changed.emit(self._dispatch_state)
        # Phase 1 (10/10): convenience edge signal for TX-active UI
        # consumers.  Phase 3 wires set_tx_active slots on
        # FrequencyDisplay / SMeter / spectrum widget here.
        self.tx_active_changed.emit(new)

    def set_ps_armed(self, ps_armed: bool) -> None:
        """Set the PS-armed axis of DispatchState.  Call from Qt main thread.

        Wired by PSDialog's FSM (v0.3) when entering/leaving the
        ARMED state.  When ``mox AND ps_armed`` both hold, HL2
        gateware re-routes DDC0/DDC1 to the PA-coupler via
        ``cntrl1=4`` (see ``CLAUDE.md`` §3.8 "PS feedback DDC
        routing" corrected entry); the dispatch table reroutes those
        DDC slots to ``PS_FEEDBACK_I`` / ``PS_FEEDBACK_Q`` consumers
        and the captured-profile pre-pass bypasses (per §4.2.x
        captured-profile bypass call site).
        """
        new = bool(ps_armed)
        if self._dispatch_state.ps_armed == new:
            return
        from dataclasses import replace
        self._dispatch_state = replace(self._dispatch_state, ps_armed=new)
        self.dispatch_state_changed.emit(self._dispatch_state)

    def set_rx2_enabled(self, rx2_enabled: bool) -> None:
        """Set the RX2-enabled axis of DispatchState.  Call from Qt main thread.

        Wired by the RX2 toggle (v0.1 Phase 3 UI) when the operator
        turns the second receiver on/off.  Reader consumers:
        ``AudioMixer.set_state(...)`` (stereo split routing),
        protocol layer (DDC1 host-channel-2 dispatch on RX-only state).

        Independent of MOX -- valid to have rx2_enabled=True during
        MOX (single-receiver TX with RX2 muted at the AAmixer per
        §8.1 MuteRX*OnVFOBTX rule) AND during MOX+PS (RX2 is
        gateware-disabled during HL2 PS+TX since DDC1 is sync-paired
        to DDC0 for the PA coupler feedback).  The UI may show a
        "PS-paused" badge in the latter case but the operator's
        rx2_enabled INTENT persists across the transition.
        """
        new = bool(rx2_enabled)
        if self._dispatch_state.rx2_enabled == new:
            return
        from dataclasses import replace
        self._dispatch_state = replace(self._dispatch_state, rx2_enabled=new)
        # Phase 3.D hotfix v0.1 (2026-05-12): re-pan both WDSP
        # channels on SUB-edge so the audio routing flips between
        # "center mono on RX1" (SUB off) and "RX1-left, RX2-right
        # stereo split" (SUB on).  Worker also gates the dual demod
        # path on this same flag.
        try:
            self._apply_rx2_routing()
        except Exception as exc:
            print(f"[Radio] _apply_rx2_routing error: {exc}")
        # Phase 3.D hotfix v0.1 (2026-05-12): on SUB rising edge,
        # MIRROR RX1's current volume + AF gain + mute onto RX2 so
        # the operator's existing level calibration carries over to
        # the right channel.  Without this, RX2 starts at the
        # construction-time defaults regardless of how the operator
        # has dialed RX1 -- producing a startling level mismatch on
        # SUB click (and was a contributing factor to the bench-test
        # speaker damage on 2026-05-12).  Operator can independently
        # adjust Vol-B / AF Gain RX2 / Mute-B afterwards.
        #
        # Phase 4 v0.1 (2026-05-12): suppressed during autoload from
        # QSettings so persisted RX2 vol/mute/AF gain survive across
        # restarts.  See ``autoload_rx2_state``.
        if new and not self._suppress_sub_mirror:
            self._volume_rx2 = self._volume
            self._muted_rx2 = self._muted
            # AF Gain RX2 mirror -- push through the setter so
            # WDSP's RX2 PanelGain1 actually gets updated, not just
            # the Python-side state field.
            try:
                self.set_af_gain_db(self._af_gain_db, target_rx=2)
            except Exception as exc:
                print(f"[Radio] AF Gain RX2 mirror on SUB: {exc}")
            # Volume + mute don't have a WDSP push -- they're
            # applied in ``_do_demod_wdsp_dual`` pre-sum.  Emit the
            # sibling signals so any UI binding (Vol-B slider,
            # Mute-B button) refreshes to the mirrored value.
            try:
                self.volume_changed_rx2.emit(self._volume_rx2)
                self.muted_changed_rx2.emit(self._muted_rx2)
            except Exception as exc:
                print(f"[Radio] Vol/Mute RX2 mirror signal: {exc}")
        self.dispatch_state_changed.emit(self._dispatch_state)

    def _apply_rx2_routing(self) -> None:
        """Re-apply WDSP RX1+RX2 pan based on the current
        ``rx2_enabled`` dispatch axis.  Phase 3.D hotfix v0.1.

        When ``rx2_enabled`` is False:
          * RX1 pan = 0.5 (center) -- audio routes to BOTH L and R
            at unity per WDSP's sin-π curve, so the operator hears
            mono in both ears as expected for single-RX listening.
          * RX2 pan = 0.5 (center) -- harmless because the worker
            also gates the dual demod path off and RX2 audio is
            never summed into the sink.

        When ``rx2_enabled`` is True:
          * RX1 pan = 0.0 (hard-left)
          * RX2 pan = 1.0 (hard-right)
        Combined sum in ``_do_demod_wdsp_dual`` produces RX1-left
        + RX2-right stereo split as planned in §6.1.
        """
        enabled = bool(self._dispatch_state.rx2_enabled)
        rx1_pan = 0.0 if enabled else 0.5
        rx2_pan = 1.0 if enabled else 0.5
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_panel_pan(rx1_pan)
            except Exception as exc:
                print(f"[Radio] WDSP RX1 pan apply: {exc}")
        if self._wdsp_rx2 is not None:
            try:
                self._wdsp_rx2.set_panel_pan(rx2_pan)
            except Exception as exc:
                print(f"[Radio] WDSP RX2 pan apply: {exc}")

    @property
    def capabilities(self) -> RadioCapabilities:
        """Per-radio-family hardware capability struct.

        v0.1 Phase 0 returns the HL2 capability instance
        unconditionally -- v0.1 / v0.2 / v0.3 are HL2-only by
        scope and HL2+ shares all HL2 capabilities (same protocol,
        same audio path, same PS posture, different TCXO + PA).

        v0.4 multi-radio work wires discovery-driven selection
        here.  When that lands, this property reads from
        ``self._dispatch_state.family`` and returns the matching
        family's capability instance from the sibling modules
        under ``lyra/protocol/`` (one per family).

        Phase 0 consumers SHOULD read ONLY:
          * ``nddc`` -- DDC count for EP6 parser stride.
          * ``has_onboard_codec`` -- offer HL2-jack audio option?
          * ``default_audio_path`` -- fresh-install default.

        Reading other fields in Phase 0 code is harmless but the
        plan reserves them for v0.2 / v0.3 / v0.4 consumers.
        Smell test: any ``isinstance(radio.protocol, HL2)`` in
        UI code is wrong (the audit gate in Phase 0 item 9
        enforces this); use this property instead.
        """
        return HL2_CAPABILITIES

    def set_radio_family(self, family: RadioFamily) -> None:
        """Set the radio-family axis of DispatchState.  Call from Qt main thread.

        Wired by stream-discovery / connection logic (one-shot at
        connection time per §4.2.x lifecycle).  v0.1 hardcodes
        ``RadioFamily.HL2`` at connection; v0.4 multi-radio work
        populates this from the discovery response.

        Family-specific behavior delta lives in
        ``radio.protocol.ddc_map(state)`` (Phase 1 deliverable) and
        ``RadioCapabilities`` (Phase 0 item 8) -- this enum is just
        the routing-table selector key.  See CLAUDE.md §6.7
        discipline #6 for the full per-family DDC mapping contract.
        """
        if not isinstance(family, RadioFamily):
            raise TypeError(
                f"family must be RadioFamily enum, got {type(family).__name__}")
        if self._dispatch_state.family == family:
            return
        from dataclasses import replace
        self._dispatch_state = replace(self._dispatch_state, family=family)
        self.dispatch_state_changed.emit(self._dispatch_state)

    def set_ip(self, ip: str):
        if ip and ip != self._ip:
            self._ip = ip
            self.ip_changed.emit(ip)

    # ── DDS / VFO frequency separation (v0.0.9.8 convention) ─────────
    #
    # ``_freq_hz`` is the operator-displayed VFO frequency — i.e., the
    # carrier frequency of the signal the operator wants to hear.  This
    # matches the convention used across the major HF SDR applications,
    # where the displayed freq is the on-air carrier and the radio
    # internally offsets the actual hardware tuning (DDC) by the CW
    # pitch in CW modes so the carrier lands inside the receive
    # bandpass and the operator hears it as a tone at the configured
    # pitch.
    #
    # Until v0.0.9.7.x Lyra used the inverse convention: ``_freq_hz``
    # was the filter-zero (= where the bandpass sat in the IQ
    # baseband), and operator-side tuning surfaces (click-to-tune,
    # NCDXF marker click, NCDXF auto-follow, TCI spot click) each
    # applied the CW pitch offset themselves before writing
    # ``_freq_hz``.  v0.0.9.8 moves the offset CENTRALLY into this
    # method (and into ``set_mode`` / ``set_cw_pitch_hz`` so a mode
    # or pitch change re-pushes the corrected DDS freq).  All
    # per-call-site offsets in the tuning surfaces are reverted so we
    # don't double-offset.
    def _compute_dds_freq_hz(
        self, vfo_hz: Optional[int] = None,
        target_rx: Optional[int] = None,
    ) -> int:
        """Compute the actual HL2 DDC freq for the given (or current)
        operator-displayed VFO freq.  Mode-aware: subtracts the CW
        pitch in CWU, adds it in CWL, identity for every other mode.

        ``target_rx`` (Phase 3.E.1 hotfix v0.8 2026-05-12): which
        RX's mode + default freq to read.  ``0`` (or ``None`` =
        legacy default) -> RX1's ``_mode`` + ``_freq_hz``.  ``2``
        -> RX2's ``_mode_rx2`` + ``_rx2_freq_hz``.  CW pitch is
        shared (single operator-ear preference, not per-RX).
        Caller can override ``vfo_hz`` regardless of target.

        Fixes operator-reported bug (Rick 2026-05-12): "CW no
        pitch on RX2 -- if I was on RX1 say DIGU and click RX2
        and go to CWL or CWU".  Pre-fix, ``set_rx2_freq_hz``
        wrote raw VFO to the wire without applying the pitch
        offset, so WDSP's CW filter (correctly centered on
        ±pitch in baseband) had no signal in its passband.
        """
        rx_id = 2 if target_rx == 2 else 0
        if vfo_hz is None:
            vfo_hz = self._rx2_freq_hz if rx_id == 2 else self._freq_hz
        m = self._mode_rx2 if rx_id == 2 else self._mode
        if m == "CWU":
            result = int(vfo_hz) - int(self._cw_pitch_hz)
        elif m == "CWL":
            result = int(vfo_hz) + int(self._cw_pitch_hz)
        else:
            result = int(vfo_hz)
        # RIT (v0.1.1): RX1-only frequency offset.  Operator's
        # displayed VFO stays put; the DDC re-tunes by RIT Hz so the
        # operator hears a slightly different frequency without
        # disturbing the band-stack memory / dial position.  Applied
        # AFTER the CW-pitch offset so the two compose cleanly
        # (RIT shifts a CW carrier the same Hz amount it shifts an
        # SSB voice).  RX2 ignores RIT in v0.1.1 -- per-RX RIT
        # deferred per §15.16 scope lock.
        if rx_id == 0 and self._rit_enabled and self._rit_offset_hz:
            result += int(self._rit_offset_hz)
        return result

    @property
    def dds_freq_hz(self) -> int:
        """Read-only convenience: the actual hardware DDC0 freq Lyra
        is using right now, accounting for CW pitch offset.  Useful
        for the spectrum widget (which centers FFT bins on the DDS
        freq) and any layer that needs to know "where the radio is
        actually tuned" vs "what the operator sees on the LED"."""
        return self._compute_dds_freq_hz()

    @property
    def marker_offset_hz(self) -> int:
        """VFO marker offset from the spectrum's visual center, in
        Hz.  = (VFO − DDS).  0 in non-CW modes; +cw_pitch in CWU
        (marker right of center); -cw_pitch in CWL.  Drives the
        spectrum widget's marker positioning under v0.0.9.8's
        carrier-freq VFO convention.

        Phase 3.E.1 hotfix v0.12 (2026-05-12): tracks the
        panadapter-source RX so the marker reflects whichever
        VFO the operator is currently looking at on the spectrum.
        When ``panadapter_source_rx == 2``, returns
        (VFO_RX2 − DDS_RX2); otherwise (VFO_RX1 − DDS_RX1).
        """
        if self._panadapter_source_rx == 2:
            return (int(self._rx2_freq_hz)
                    - self._compute_dds_freq_hz(target_rx=2))
        return int(self._freq_hz) - self._compute_dds_freq_hz()

    # ── TX frequency (v0.2.0 Phase 3 commit 1, §15.25) ──────────────
    # The TX-NCO frequency Lyra writes to HL2 C&C 0x02/0x08/0x0a via
    # HL2Stream._set_tx_freq on the MOX=1 edge (commit 2 wires the
    # call site).  This is DELIBERATELY a separate computation from
    # _compute_dds_freq_hz -- it must NOT inherit RIT.
    #
    # §15.25 NEW FINDING #1 (Thetis 2-agent verify 2026-05-16,
    # HIGH trap §15.24 missed): Thetis applies RIT to rx_freq ONLY
    # (console.cs:32502-32503; :22310 "RIT is rx1-only when not
    # txing") and XIT to tx_freq ONLY (:32508-32509).  Lyra's
    # _compute_dds_freq_hz adds _rit_offset_hz (rx_id==0 path) --
    # reusing it for TX would put every RIT-engaged transmission
    # off-frequency.  So tx_freq_hz is computed from the raw VFO,
    # NOT routed through _compute_dds_freq_hz.
    #
    # Phase-3 scope = SSB-only, non-SPLIT, no XIT:
    #   * value = focused/active VFO carrier.  Non-SPLIT (SPLIT is
    #     §15.6 deferred) the TX VFO is always VFOA == self._freq_hz
    #     (Thetis console.cs:11402-11417: non-RX2 non-SPLIT ->
    #     VFOAFreq).
    #   * NO RIT (the trap above).
    #   * NO XIT yet -- v0.2.3 (§15.10) adds `+ xit_offset_hz` here.
    #   * NO CW-pitch offset -- Phase 3 is USB/LSB; CW TX (v0.2.2)
    #     needs Thetis's `cw_fw_keyer`-GATED pitch handling
    #     (console.cs:32553-32588), NOT the RX unconditional path,
    #     so it gets added here as an explicit gated branch then,
    #     deliberately not by reusing _compute_dds_freq_hz.
    #   * SPLIT (VFO-B as TX) extends this when §15.6 lands.
    @property
    def tx_freq_hz(self) -> int:
        """Operator TX carrier frequency in Hz (the value
        ``HL2Stream._set_tx_freq`` loads into the HL2 TX NCO).

        RIT-free by design (see the block comment above / §15.25):
        RIT is an RX-only offset and must never shift TX.  Phase-3
        SSB-only non-SPLIT value = the main VFO carrier
        (``self._freq_hz``).  XIT (v0.2.3), CW-pitch-gated TX
        (v0.2.2), and SPLIT VFO-B (§15.6) extend this later --
        each as an explicit branch here, never via
        ``_compute_dds_freq_hz`` (which carries RIT).
        """
        return int(self._freq_hz)

    def _emit_marker_offset(self) -> None:
        """Re-emit the marker offset.  Call from any state change
        that shifts the DDS-vs-VFO relationship — freq, mode,
        CW pitch, or panadapter source switch."""
        self.marker_offset_changed.emit(int(self.marker_offset_hz))

    def set_freq_hz(self, hz: int):
        hz = int(hz)
        if hz == self._freq_hz:
            return
        self._freq_hz = hz
        if self._stream:
            try:
                # Send the offset DDS freq to the protocol layer, NOT
                # the operator-displayed value.  See the convention
                # note above _compute_dds_freq_hz for the why.
                self._stream._set_rx1_freq(self._compute_dds_freq_hz(hz))  # noqa: SLF001
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
        # The reset drops in-flight audio buffer + forces decimator
        # rebuild + zeroes AGC peak (so a stale loud-signal peak
        # from the prior band doesn't clamp gain to silence on the
        # new band while it slowly decays) + zeroes binaural state
        # (Hilbert + delay line — prevents prior band's audio
        # bleeding across the discontinuity).
        #
        # B.9: in worker mode this routes through DspWorker so the
        # reset runs between blocks (no race with worker's
        # process_block).  Single-thread mode: synchronous on main.
        self._request_dsp_reset_full()
        # NOTE: previous versions called an explicit rate-keepalive
        # reassert here as a band-aid for stuck-audio after big freq
        # jumps.  No longer needed because the stream uses round-robin
        # C&C cycling (every register stays fresh automatically) --
        # see HL2Stream._cc_registers / _cc_cycle / _ep2_writer_loop.
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
        # Marker offset only changes if mode or pitch shift, not on
        # freq alone — but emit it here too so the widget stays in
        # sync if it was constructed mid-session and missed earlier
        # mode/pitch events (defensive; cost is negligible).
        self._emit_marker_offset()

    def set_rate(self, rate: int):
        if rate not in SAMPLE_RATES or rate == self._rate:
            return
        prev_rate = self._rate
        self._rate = rate
        # Keep the cached config-register c1 byte in sync with the
        # current rate.  ``_send_full_config`` (band change with
        # filter board enabled, OC bit changes) recomposes register
        # 0x00 from this field; if it's stale, the band change writes
        # the OLD rate code into _cc_registers and the round-robin
        # propagates it to the HL2, dropping the IQ rate back to
        # whatever ``_config_c1`` was last set to (= 48 k from
        # __init__'s default).  Operator-visible as DSP throttling
        # to ~23 Hz + audio dragging after band change.  See
        # ``_send_full_config`` for the matching defensive fix.
        self._config_c1 = SAMPLE_RATES[rate]
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
        # WDSP RX engine: the channel's input rate is fixed at
        # OpenChannel time, so we close + reopen on rate change. WDSP
        # finishes any partially-processed audio inside _open_wdsp_rx's
        # close, so this is graceful (no crackle).
        if rate != self._wdsp_rx_in_rate:
            try:
                self._open_wdsp_rx(rate)
            except Exception as exc:
                print(f"[Radio] WDSP rx rate-change reopen error: {exc}")
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
        baseband offset that the filter is centered on).  Preserves
        each notch's width, depth, cascade, and active flag.

        Phase 6.B (v0.0.9.6) simplified this method: WDSP's notch
        DB takes ABSOLUTE frequencies and does its own VFO-relative
        mapping internally via ``set_notch_tune_frequency``, so
        the per-notch coefficient rebuild this loop used to do (on
        the deleted Lyra-side NotchFilter) is no longer needed.
        Push WDSP the new tunefreq + the notch list and we're done.
        """
        # WDSP path: tell the notch engine the current VFO so it can
        # do its own absolute→baseband mapping, then push the notch
        # list (in case widths / active flags changed).  Both calls
        # are cheap when the notch list hasn't changed (DLL filter
        # rebuild only fires on actual difference).
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_notch_tune_frequency(float(self._freq_hz))
                self._push_wdsp_notches()
            except Exception as exc:
                print(f"[Radio] WDSP notch refresh: {exc}")
        if self._notch_enabled:
            self.notches_changed.emit(self.notch_details)

    def set_mode(self, mode: str, target_rx: Optional[int] = None):
        """Set mode for ``target_rx`` (default = focused RX).

        Phase 3.C v0.1 (2026-05-12) introduced ``target_rx``
        semantics per consensus plan §6.1 + §6.7.  Pre-Phase-3.C
        behavior was to fan the mode change out to both channels.
        Now the setter routes:

        * ``target_rx == 0`` -> RX1 mode change with full
          RX1-context side effects (band memory, S-meter reset,
          panadapter passband + marker, CW Zero line, DDS re-push
          for CW pitch).  Emits ``mode_changed(str)``.
        * ``target_rx == 2`` -> RX2-only mode change.  No
          RX1-context side effects (Phase 4 split panadapter may
          add per-RX equivalents).  Emits ``mode_changed_rx2(str)``.
        * ``target_rx is None`` -> use focused RX
          (``self._focused_rx``).

        Existing call sites pass ``target_rx=0`` when they're
        explicitly RX1-context: band buttons, TIME button, band
        memory recall, spot click-tune, panadapter click-tune.
        The MODE+FILTER panel's mode picker passes
        ``target_rx=self.radio.focused_rx`` so operators can
        change RX2's mode when focused there.
        """
        # Accept legacy aliases from old saved settings so a loaded value
        # like "CW" (before we split into CWL/CWU) doesn't leave the radio
        # in a state with no matching demod (→ silent audio).
        alias = {"CW": "CWU", "NFM": "FM", "WFM": "FM"}.get(mode, mode)
        if alias not in self.ALL_MODES:
            alias = "USB"

        rx_id, _suffix = self._resolve_rx_target(target_rx)

        if rx_id == 2:
            # RX2-only mode change.  Skip the RX1-context side
            # effects that don't apply (band memory, S-meter,
            # panadapter passband for the focused-RX1 case), but
            # DO re-push the DDS with the CW pitch offset so the
            # carrier lands in WDSP's CW passband when the new
            # mode is CWU/CWL.  Pre-fix omission caused operator-
            # reported "CW no pitch on RX2" (Rick 2026-05-12) --
            # switching RX2 from DIGU to CWL/CWU left the DDS at
            # raw VFO instead of VFO ± pitch.
            if alias == self._mode_rx2:
                return
            self._mode_rx2 = alias
            if self._wdsp_rx2 is not None:
                try:
                    self._wdsp_rx2.reset()
                    self._wdsp_rx2.set_mode(self._wdsp_mode_for(alias))
                    low, high = self._wdsp_filter_for(alias, target_rx=2)
                    self._wdsp_rx2.set_filter(low, high)
                except Exception as exc:
                    print(f"[Radio] WDSP rx2 mode-change error: {exc}")
            # Phase 3.E.1 hotfix v0.8 (2026-05-12): DDS re-push
            # for RX2 mirrors the RX1 path at line ~2583 that
            # re-pushes ``_compute_dds_freq_hz()`` after every
            # mode change.  Without this, the gateware DDC1 stays
            # at the old offset and the new mode's WDSP filter
            # produces silence.
            if self._stream is not None:
                try:
                    self._stream._set_rx2_freq(  # noqa: SLF001
                        self._compute_dds_freq_hz(target_rx=2))
                except Exception as exc:
                    print(f"[Radio] WDSP rx2 mode-change DDS re-push: "
                          f"{exc}")
            self.mode_changed_rx2.emit(alias)
            # Phase 3.E.1 hotfix v0.12 / v0.14 (2026-05-12): when
            # the panadapter is sourced from RX2, both the marker
            # offset AND the passband overlay depend on RX2's
            # mode.  Re-emit so the spectrum widget repositions
            # the marker AND redraws the passband rectangle.
            if self._panadapter_source_rx == 2:
                self._emit_marker_offset()
                self._emit_passband()
            return

        # RX1 mode change -- full original behavior.
        if alias == self._mode:
            return
        self._mode = alias
        # Channel handles its own audio-buffer flush + NR reset on
        # mode switch so the previous mode's noise-floor estimate
        # doesn't leak in as an audible transient.
        self._rx_channel.set_mode(alias)
        # WDSP RX engine — push mode + recompute filter (CW pitch
        # changes the centre frequency, so filter follows mode).
        # Reset clears any in-flight half-block IQ + WDSP iobuffs so
        # the previous mode's audio doesn't leak in.
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.reset()
                self._wdsp_rx.set_mode(self._wdsp_mode_for(alias))
                low, high = self._wdsp_filter_for(alias)
                self._wdsp_rx.set_filter(low, high)
                # APF is mode-gated to CW only — push state so the
                # peaking filter activates / deactivates at the mode
                # switch, not on the operator's next slider tweak.
                self._push_wdsp_apf_state()
                # Re-route the squelch module to match the new mode
                # (FM ↔ AM ↔ SSQL).  Without this, operator's SQ
                # toggle would stay attached to the old mode's
                # module and silently no-op on the new one.
                self._push_wdsp_squelch_state()
            except Exception as exc:
                print(f"[Radio] WDSP rx mode-change error: {exc}")
        # Also reset S-meter state. Different demods produce very
        # different peak levels (AM envelope vs SSB sideband vs CW
        # pitched tone), so a peak captured under the old mode
        # would mis-clamp gain under the new one until it decayed.
        # WDSP's AGC runs inside the engine and resets internally
        # through the mode-change wiring above.
        self._smeter_avg_lin = 0.0
        self._smeter_peak_hold_lin = 0.0
        # WDSP SSQL handles all-mode squelch natively; mode-specific
        # re-routing (FM ↔ AM ↔ SSQL) happens in
        # `_push_wdsp_squelch_state` via the WDSP-rx mode-change
        # block above.  Phase 6.C swapped the channel's _squelch
        # for an `_SquelchState` dataclass; its set_mode reset
        # call is a no-op (state-mirror only).
        # NOTE: legacy `self._leveler.reset()` removed in Phase 4
        # along with the rest of the leveler API.  WDSP AGC handles
        # the equivalent envelope-state cleanup internally on mode
        # change via `_wdsp_rx.reset()` (called above).
        # No reassert needed — round-robin C&C keepalive in
        # HL2Stream._rx_loop keeps every register fresh.
        if not self._suppress_band_save:
            self._save_current_band_memory()
        # Re-push the freq to the protocol layer.  Switching between a
        # CW mode and a non-CW mode (or between CWU and CWL) changes
        # the DDS-vs-VFO offset, so the actual hardware tuning needs
        # to follow even though the operator-displayed VFO didn't
        # change.  Without this, switching SSB→CWU would leave the
        # DDS at carrier (= VFO) instead of carrier - pitch and the
        # operator would hear zero-beat silence at their pitch tone.
        if self._stream:
            try:
                self._stream._set_rx1_freq(self._compute_dds_freq_hz())  # noqa: SLF001
            except Exception as exc:
                print(f"[Radio] mode-change DDS re-push error: {exc}")
        self.mode_changed.emit(alias)
        self._emit_passband()
        # CW Zero line lives at +/-pitch in CWU/CWL, hidden elsewhere —
        # re-emit so the panadapter draws or removes the white line.
        self._emit_cw_zero()
        # Marker offset shifts when entering / leaving CW modes.
        self._emit_marker_offset()

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

        Phase 3.E.1 hotfix v0.14 (2026-05-12): when the panadapter
        is sourced from RX2, read RX2's mode + per-mode BW instead
        of RX1's.  Operator-reported "extra pitch line on RX2 CWL"
        (Rick 2026-05-12) was the passband overlay drawing for
        RX1's mode (e.g. CWU at +pitch) on the RX2 panadapter,
        appearing as an extra cyan rectangle on the wrong side of
        the marker.
        """
        if self._panadapter_source_rx == 2:
            mode = self._mode_rx2
            bw = int(self._rx_bw_by_mode_rx2.get(mode, 2400))
        else:
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

    def set_volume(self, v: float, target_rx: Optional[int] = None):
        """Final-trim volume for ``target_rx`` (default = focused RX).

        Volume is post AF Gain, range 0..1.0 (0 = silent, 1 = unity
        pass of AF-gained signal). Old QSettings values in the
        0..3.0 range from pre-split code get clamped to 1.0 at load
        time; the operator can re-dial to taste from there.

        Phase 3.D v0.1: per-RX semantics surface as Vol-A / Vol-B
        in the DSP+Audio panel when ``dispatch_state.rx2_enabled``
        is True (consensus plan §6.8).  RX1's combined volume
        applies to the summed dual-RX audio path when RX2 is OFF.
        """
        v = max(0.0, min(1.0, float(v)))
        rx_id, _ = self._resolve_rx_target(target_rx)
        if rx_id == 2:
            self._volume_rx2 = v
            self.volume_changed_rx2.emit(v)
            return
        self._volume = v
        self.volume_changed.emit(v)

    # ── AF Gain (post-AGC, pre-Volume makeup gain) ────────────────────
    @property
    def af_gain_db(self) -> int:
        return self._af_gain_db

    def set_af_gain_db(self, db: int, target_rx: Optional[int] = None):
        """Integer dB, clamped 0..+80.  Pre-AGC makeup gain pushed
        into WDSP's PanelGain1 stage (matches Thetis's AF Gain
        semantics).

        Routing: ``set_af_gain_db`` → ``self._af_gain_db`` →
        ``self.af_gain_linear`` → ``_wdsp_rx.set_panel_gain``.
        WDSP's SetRXAPanelGain1 takes a LINEAR multiplier; we
        convert from operator-facing integer dB at push time.

        Phase 6.A1 (v0.0.9.6) wired this up after Phase 6.A
        surfaced that the legacy ``_apply_agc_and_volume`` had been
        the only consumer of ``af_gain_linear`` for live audio and
        had been orphan since Phase 4 — meaning AF Gain had been
        silently inert for actual demodulated signal.  The
        ``_emit_tone`` test-tone path also reads ``af_gain_linear``
        directly; that wasn't affected by the orphan and continues
        unchanged.

        Range goes to +80 dB because AGC OFF has no other source
        of makeup gain — and AGC ON internally provides up to +60
        dB of automatic amplification, so a +50 dB AF cap left
        AGC OFF roughly 30 dB quieter on weak signals than AGC ON.
        +80 dB closes that gap; operators who don't need the upper
        range simply never visit it."""
        db = max(0, min(80, int(db)))
        rx_id, _suffix = self._resolve_rx_target(target_rx)

        if rx_id == 2:
            # RX2 path: independent per-RX state, no fan-out.
            if db == self._af_gain_db_rx2:
                return
            self._af_gain_db_rx2 = db
            linear = 10.0 ** (db / 20.0)
            wdsp2 = getattr(self, "_wdsp_rx2", None)
            if wdsp2 is not None:
                try:
                    wdsp2.set_panel_gain(linear)
                except Exception as exc:
                    print(f"[Radio] AF Gain → WDSP rx2 push error: {exc}")
            self.af_gain_db_changed_rx2.emit(db)
            return

        # RX1 path.
        if db == self._af_gain_db:
            return
        self._af_gain_db = db
        # Push to WDSP if the engine is up.  Init order: __init__
        # creates WDSP before persisting AGC defaults call this,
        # so the engine is normally ready by the time the slider
        # signal connects.  getattr() guards a hot-reload edge.
        wdsp = getattr(self, "_wdsp_rx", None)
        if wdsp is not None:
            try:
                wdsp.set_panel_gain(self.af_gain_linear)
            except Exception as exc:
                print(f"[Radio] AF Gain → WDSP push error: {exc}")
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

    # ── Operator / Station API ───────────────────────────────────────

    @property
    def callsign(self) -> str:
        """Operator's callsign — uppercase, no whitespace.  Empty
        string when not yet configured."""
        return self._callsign

    @property
    def grid_square(self) -> str:
        """Operator's Maidenhead grid square (4, 6, or 8 chars).
        Empty string when not yet configured.  Always uppercase."""
        return self._grid_square

    @property
    def operator_lat(self) -> Optional[float]:
        """Effective operator latitude.  Returns the grid-derived
        value if a valid grid is set, else the manual override, else
        None.  Consumers should treat None as 'no location'."""
        from lyra.ham.grid import grid_to_latlon
        if self._grid_square:
            ll = grid_to_latlon(self._grid_square)
            if ll is not None:
                return ll[0]
        return self._operator_lat_manual

    # ── EiBi SW broadcaster overlay (v0.0.9 Step 4) ────────────────

    @property
    def eibi_store(self):
        """Lazy-initialized singleton ``EibiStore`` shared across
        the Settings tab and the panadapter overlay.

        On first access, attempts to load any previously-downloaded
        EiBi CSV from the Lyra app-data directory.  Returns the
        store regardless -- ``store.loaded`` is False when no file
        is present yet, so callers don't have to second-guess.

        See ``docs/architecture/v0.0.9_memory_stations_design.md``
        section 5.
        """
        store = getattr(self, "_eibi_store", None)
        if store is None:
            from lyra.swdb.store import EibiStore
            store = EibiStore()
            try:
                path = self._eibi_default_path()
                if path is not None and path.exists():
                    store.load(path)
            except Exception as e:
                # Logged but non-fatal -- store stays "not loaded"
                # and the Settings tab surfaces this state.
                print(f"[Radio.eibi_store] initial load failed: {e}")
            self._eibi_store = store
        return store

    def _eibi_default_path(self):
        """Compute the operator-side default path for the EiBi
        CSV.  Looks at QSettings first (so an operator-overridden
        custom path wins), else falls back to
        ``%APPDATA%/Lyra-SDR/swdb/sked-{season}.csv``.

        Returns a ``Path`` or None when no candidate can be
        resolved (e.g. no QStandardPaths writable location)."""
        from pathlib import Path
        from PySide6.QtCore import (
            QSettings as _QS, QStandardPaths,
        )
        qs = _QS("N8SDR", "Lyra")
        custom = str(qs.value("swdb/file_path", "") or "")
        if custom:
            return Path(custom)
        appdata = QStandardPaths.writableLocation(
            QStandardPaths.AppLocalDataLocation)
        if not appdata:
            return None
        from lyra.swdb.downloader import season_filename
        try:
            fname = season_filename(season="auto")
        except Exception:
            fname = "sked-A26.csv"   # arbitrary fallback
        return Path(appdata) / "swdb" / fname

    def reload_eibi_store(self, path=None):
        """Reload the EiBi store from disk.  Called by the
        Settings tab after a successful download (or after the
        operator manually drops a file in the swdb folder).

        ``path`` overrides the default path.  When None, uses
        the QSettings-configured custom path or the auto-named
        seasonal file under AppLocalDataLocation.
        """
        from pathlib import Path
        from lyra.swdb.store import EibiStore
        store = getattr(self, "_eibi_store", None)
        if store is None:
            store = EibiStore()
            self._eibi_store = store
        if path is None:
            path = self._eibi_default_path()
        if path is not None and Path(path).exists():
            store.load(Path(path))
        # Notify any subscribers (panadapter, settings refresh).
        self.eibi_store_changed.emit()

    @property
    def operator_country_iso(self) -> str:
        """Operator's country as ISO-3166-1 alpha-2 ('US', 'CA',
        'DE', etc.).  Derived from the configured callsign via the
        DXCC prefix table (lyra/ham/dxcc.py + cty.dat).  Returns
        empty string when no callsign is set or the prefix isn't
        in the DXCC table.

        Used for features that benefit from operator-location
        priority (time-station cycle ordering, EiBi-overlay
        regional defaults, etc.) without requiring a separate
        Settings field.

        DxccLookup is cached on Radio so repeated property reads
        don't re-parse cty.dat each time.
        """
        if not self._callsign:
            return ""
        try:
            from pathlib import Path
            from lyra.ham.dxcc import DxccLookup
            from lyra.ham.country_iso import COUNTRY_TO_ISO
            import lyra
            dxcc = getattr(self, "_dxcc_lookup", None)
            if dxcc is None:
                # cty.dat ships alongside the package under <root>/data/.
                # Use Lyra.resource_root() so the path is correct in
                # both the dev tree and the PyInstaller-frozen bundle.
                cty_path = Path(lyra.resource_root()) / "data" / "cty.dat"
                dxcc = DxccLookup(cty_path)
                self._dxcc_lookup = dxcc
            country = dxcc.country_of(self._callsign)
            return COUNTRY_TO_ISO.get(country, "") if country else ""
        except Exception:
            return ""

    @property
    def operator_lon(self) -> Optional[float]:
        """Effective operator longitude — see operator_lat for the
        grid-vs-override resolution."""
        from lyra.ham.grid import grid_to_latlon
        if self._grid_square:
            ll = grid_to_latlon(self._grid_square)
            if ll is not None:
                return ll[1]
        return self._operator_lon_manual

    @property
    def operator_lat_manual(self) -> Optional[float]:
        """Raw manual-override latitude (None if unset).  Only used
        when grid_square is blank or invalid.  UI exposes this as a
        backup field."""
        return self._operator_lat_manual

    @property
    def operator_lon_manual(self) -> Optional[float]:
        """Raw manual-override longitude (None if unset)."""
        return self._operator_lon_manual

    def set_callsign(self, value: str) -> None:
        """Update the operator callsign.  Auto-strips whitespace and
        upper-cases.  Persists to QSettings and emits change signal.
        """
        cs = (value or "").strip().upper()
        if cs == self._callsign:
            return
        self._callsign = cs
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("operator/callsign", cs)
        except Exception as exc:
            print(f"[Radio] could not persist callsign: {exc}")
        self.callsign_changed.emit(cs)

    def set_grid_square(self, value: str) -> None:
        """Update the operator's Maidenhead grid.  Validates format;
        empty string clears.  Emits change signal AND
        operator_location_changed if the grid produces a valid
        lat/lon."""
        from lyra.ham.grid import is_valid_grid, normalize_grid
        old_lat = self.operator_lat
        old_lon = self.operator_lon
        gs = normalize_grid(value or "")
        if not gs and value and value.strip():
            # Caller passed a non-empty string that isn't valid —
            # clear the field so we don't store garbage.
            gs = ""
        if gs == self._grid_square:
            return
        self._grid_square = gs
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("operator/grid", gs)
        except Exception as exc:
            print(f"[Radio] could not persist grid_square: {exc}")
        self.grid_square_changed.emit(gs)
        # Re-emit location signal if the effective lat/lon changed.
        new_lat = self.operator_lat
        new_lon = self.operator_lon
        if (new_lat != old_lat or new_lon != old_lon) and (
                new_lat is not None and new_lon is not None):
            self.operator_location_changed.emit(
                float(new_lat), float(new_lon))

    def set_operator_lat_lon(self, lat: Optional[float],
                              lon: Optional[float]) -> None:
        """Update the manual-override lat/lon (used as backup when
        no valid grid is set).  Pass (None, None) to clear.
        Persists and emits operator_location_changed when the
        effective location changes."""
        old_lat = self.operator_lat
        old_lon = self.operator_lon
        self._operator_lat_manual = (
            float(lat) if lat is not None else None)
        self._operator_lon_manual = (
            float(lon) if lon is not None else None)
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            if self._operator_lat_manual is None:
                s.remove("operator/lat_manual")
            else:
                s.setValue("operator/lat_manual",
                           self._operator_lat_manual)
            if self._operator_lon_manual is None:
                s.remove("operator/lon_manual")
            else:
                s.setValue("operator/lon_manual",
                           self._operator_lon_manual)
        except Exception as exc:
            print(f"[Radio] could not persist operator lat/lon: {exc}")
        new_lat = self.operator_lat
        new_lon = self.operator_lon
        if (new_lat != old_lat or new_lon != old_lon) and (
                new_lat is not None and new_lon is not None):
            self.operator_location_changed.emit(
                float(new_lat), float(new_lon))

    # ── Weather Alerts API ───────────────────────────────────────

    @property
    def wx_enabled(self) -> bool:
        return self._wx_enabled

    @property
    def wx_disclaimer_accepted(self) -> bool:
        return self._wx_disclaimer_accepted

    @property
    def wx_last_snapshot(self):
        """Most-recent WxSnapshot, or None if the worker hasn't
        completed a poll cycle yet."""
        return self._wx_last_snapshot

    def set_wx_disclaimer_accepted(self, accepted: bool) -> None:
        """Operator acknowledges the safety disclaimer.  Required
        before set_wx_enabled(True) will succeed.  Persists.
        """
        self._wx_disclaimer_accepted = bool(accepted)
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("wx/disclaimer_accepted",
                       self._wx_disclaimer_accepted)
        except Exception as exc:
            print(f"[Radio] could not persist wx_disclaimer: {exc}")
        # If the operator just rejected the disclaimer while alerts
        # were active, force-disable.
        if not self._wx_disclaimer_accepted and self._wx_enabled:
            self.set_wx_enabled(False)

    def set_wx_enabled(self, on: bool) -> None:
        """Master toggle for the weather-alerts feature.  Refuses
        to enable when the disclaimer hasn't been accepted (silent
        no-op so the UI can fail closed cleanly).  Spawns the
        WxWorker thread on first enable."""
        on = bool(on)
        if on and not self._wx_disclaimer_accepted:
            return
        if on == self._wx_enabled:
            return
        if on and self._wx_worker is None:
            from lyra.wx.worker import WxWorker
            self._wx_worker = WxWorker(self)
            self._wx_worker.snapshot_ready.connect(
                self._on_wx_snapshot_ready)
            self._wx_worker.error_occurred.connect(self.wx_error.emit)
            # Apply current config before starting.
            self._wx_worker.set_config(self._build_wx_config())
            self._wx_worker.start()
        self._wx_enabled = on
        if self._wx_worker is not None:
            self._wx_worker.set_enabled(on)
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("wx/enabled", on)
        except Exception as exc:
            print(f"[Radio] could not persist wx_enabled: {exc}")
        self.wx_enabled_changed.emit(on)

    def set_wx_config(self, **fields) -> None:
        """Update one or more WxConfig fields and push to the worker.
        Accepts any subset of WxConfig field names; unknown fields
        are silently ignored.  Persists each field under wx/* in
        QSettings.
        """
        from PySide6.QtCore import QSettings
        from lyra.wx.aggregator import WxConfig
        valid_fields = {f.name for f in WxConfig.__dataclass_fields__.values()}
        s = QSettings("N8SDR", "Lyra")
        for k, v in fields.items():
            if k not in valid_fields:
                continue
            try:
                s.setValue(f"wx/{k}", v)
            except Exception as exc:
                print(f"[Radio] could not persist wx/{k}: {exc}")
        if self._wx_worker is not None:
            self._wx_worker.set_config(self._build_wx_config())

    def fire_wx_test_toast(self) -> None:
        """Operator clicked 'Send test toast' — fires a toast that
        bypasses hysteresis AND blinks all three header indicators
        for ~6 seconds so the operator can preview both the audio /
        desktop notification AND the visual cue.  After the preview
        window expires, the previous snapshot is restored (or a
        clean 'none' state if there was no prior snapshot).
        """
        # Need an active worker to fire; if one doesn't exist yet,
        # construct it without enabling the poll loop.
        if self._wx_worker is None:
            from lyra.wx.worker import WxWorker
            self._wx_worker = WxWorker(self)
            self._wx_worker.snapshot_ready.connect(
                self._on_wx_snapshot_ready)
            self._wx_worker.error_occurred.connect(self.wx_error.emit)
        self._wx_worker.fire_test_toast()

        # Build a synthetic "all alerts firing" snapshot so the
        # operator can see what the header indicators look like in
        # the wild.  Mid-tier values picked so all three icons are
        # visibly distinct and the tooltips have realistic content.
        from lyra.wx.aggregator import (
            WxSnapshot, LightningState, WindState, SevereState,
            LIGHTNING_CLOSE, WIND_EXTREME, SEVERE_ACTIVE)
        test_snap = WxSnapshot(
            lightning=LightningState(
                tier=LIGHTNING_CLOSE,
                closest_km=12.0,         # ~7.5 mi — red tier
                closest_bearing_deg=225.0,  # SW
                strikes_recent=14,
                sources_with_data=["test"]),
            wind=WindState(
                tier=WIND_EXTREME,
                sustained_mph=48.0,
                gust_mph=63.0,
                direction_deg=270.0,
                nws_alert_headline="Test — High Wind Warning",
                sources_with_data=["test"]),
            severe=SevereState(
                tier=SEVERE_ACTIVE,
                headline="Test — Severe Thunderstorm Warning"))

        # Stash the prior snapshot so we can restore it when the
        # preview window expires.  Operators with live alerts active
        # don't lose their real readings.
        self._wx_test_prior_snapshot = self._wx_last_snapshot

        # Emit the test snapshot — header indicator subscribes to
        # this signal and lights up.
        self._wx_last_snapshot = test_snap
        self.wx_snapshot_changed.emit(test_snap)

        # Schedule restoration after 6 seconds — long enough for the
        # operator to look at the toolbar, short enough not to hide
        # real conditions if any are active.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(6000, self._restore_after_wx_test)

    def _restore_after_wx_test(self) -> None:
        """One-shot callback for fire_wx_test_toast — restore the
        prior snapshot (or emit a clean 'none' snapshot if none
        was active before the test)."""
        prior = getattr(self, "_wx_test_prior_snapshot", None)
        if prior is not None:
            self._wx_last_snapshot = prior
            self.wx_snapshot_changed.emit(prior)
        else:
            # No prior snapshot — emit a fresh "none" snapshot so
            # all indicators clear back to hidden.
            from lyra.wx.aggregator import WxSnapshot
            blank = WxSnapshot()
            self._wx_last_snapshot = blank
            self.wx_snapshot_changed.emit(blank)
        self._wx_test_prior_snapshot = None

    def _on_wx_snapshot_ready(self, snap) -> None:
        """Slot for WxWorker.snapshot_ready — store + re-emit."""
        self._wx_last_snapshot = snap
        self.wx_snapshot_changed.emit(snap)

    def _build_wx_config(self):
        """Build a fresh WxConfig from the persisted operator
        settings + wx/* QSettings.  Called whenever any wx setting
        changes."""
        from PySide6.QtCore import QSettings
        from lyra.wx.aggregator import WxConfig
        s = QSettings("N8SDR", "Lyra")
        cfg = WxConfig()
        cfg.lightning_range_km = float(
            s.value("wx/lightning_range_km", cfg.lightning_range_km,
                    type=float))
        cfg.lightning_mid_km = float(
            s.value("wx/lightning_mid_km", cfg.lightning_mid_km,
                    type=float))
        cfg.lightning_close_km = float(
            s.value("wx/lightning_close_km", cfg.lightning_close_km,
                    type=float))
        cfg.wind_sustained_mph = float(
            s.value("wx/wind_sustained_mph", cfg.wind_sustained_mph,
                    type=float))
        cfg.wind_gust_mph = float(
            s.value("wx/wind_gust_mph", cfg.wind_gust_mph,
                    type=float))
        cfg.src_blitzortung = bool(
            s.value("wx/src_blitzortung", False, type=bool))
        cfg.src_nws = bool(s.value("wx/src_nws", False, type=bool))
        cfg.src_nws_metar = bool(
            s.value("wx/src_nws_metar", False, type=bool))
        cfg.src_ambient = bool(
            s.value("wx/src_ambient", False, type=bool))
        cfg.src_ecowitt = bool(
            s.value("wx/src_ecowitt", False, type=bool))
        cfg.ambient_api_key = str(
            s.value("wx/ambient_api_key", "", type=str))
        cfg.ambient_app_key = str(
            s.value("wx/ambient_app_key", "", type=str))
        cfg.ecowitt_app_key = str(
            s.value("wx/ecowitt_app_key", "", type=str))
        cfg.ecowitt_api_key = str(
            s.value("wx/ecowitt_api_key", "", type=str))
        cfg.ecowitt_mac = str(
            s.value("wx/ecowitt_mac", "", type=str))
        cfg.nws_metar_station = str(
            s.value("wx/nws_metar_station", "", type=str))
        return cfg

    def autoload_wx_settings(self) -> None:
        """Restore disclaimer + master enable + audio toggle from
        QSettings.  Source enables, thresholds, and credentials are
        loaded lazily by _build_wx_config() when the worker starts."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            disc = bool(s.value(
                "wx/disclaimer_accepted", False, type=bool))
            enabled = bool(s.value("wx/enabled", False, type=bool))
            audio = bool(s.value("wx/audio_enabled", True, type=bool))
            desktop = bool(s.value("wx/desktop_enabled", True, type=bool))
        except Exception:
            return
        self._wx_disclaimer_accepted = disc
        # Even if the operator had wx_enabled=true persisted, only
        # re-enable if the disclaimer is still accepted (safety
        # belt-and-suspenders — disclaimer is the master gate).
        if disc and enabled:
            self.set_wx_enabled(True)
        # Audio + desktop toggles apply to the worker's toast
        # dispatcher; create a worker if needed (without enabling
        # poll) so the toggles persist.
        if self._wx_worker is None:
            from lyra.wx.worker import WxWorker
            self._wx_worker = WxWorker(self)
            self._wx_worker.snapshot_ready.connect(
                self._on_wx_snapshot_ready)
            self._wx_worker.error_occurred.connect(self.wx_error.emit)
        self._wx_worker.set_audio_enabled(audio)
        self._wx_worker.set_desktop_enabled(desktop)

    def autoload_operator_settings(self) -> None:
        """Restore callsign + grid + manual lat/lon from QSettings.
        Called once at app startup.  Pre-populates callsign from the
        TCI server's saved own_callsign on first run if our key is
        empty (graceful migration for users upgrading from a Lyra
        version that only had the TCI callsign field)."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            cs = str(s.value("operator/callsign", "", type=str))
            if not cs:
                # Migrate from TCI-only callsign field if present.
                cs = str(s.value("tci/own_callsign", "", type=str))
            grid = str(s.value("operator/grid", "", type=str))
            lat_m = s.value("operator/lat_manual", None)
            lon_m = s.value("operator/lon_manual", None)
            try:
                lat_m = float(lat_m) if lat_m is not None else None
            except (TypeError, ValueError):
                lat_m = None
            try:
                lon_m = float(lon_m) if lon_m is not None else None
            except (TypeError, ValueError):
                lon_m = None
        except Exception:
            return
        try:
            self.set_callsign(cs)
            self.set_grid_square(grid)
            self.set_operator_lat_lon(lat_m, lon_m)
        except Exception as exc:
            print(f"[Radio] could not autoload operator settings: {exc}")

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
    def band_plan_show_ncdxf(self) -> bool:
        """NCDXF beacon markers — independent of the digital
        watering-hole landmarks (FT8 / FT4 / WSPR / PSK).  Operators
        who don't care about beacons can hide just those triangles
        without losing the digital ones, and vice versa."""
        return self._band_plan_show_ncdxf

    def set_band_plan_show_ncdxf(self, on: bool):
        on = bool(on)
        if on == self._band_plan_show_ncdxf:
            return
        self._band_plan_show_ncdxf = on
        self.band_plan_show_ncdxf_changed.emit(on)

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

    # ── Spectrum smoothing API (display-only EWMA) ───────────────────
    @property
    def spectrum_smoothing_enabled(self) -> bool:
        return self._spectrum_smoothing_enabled

    def set_spectrum_smoothing_enabled(self, on: bool):
        on = bool(on)
        if on == self._spectrum_smoothing_enabled:
            return
        self._spectrum_smoothing_enabled = on
        self.spectrum_smoothing_enabled_changed.emit(on)

    @property
    def spectrum_smoothing_strength(self) -> int:
        return self._spectrum_smoothing_strength

    def set_spectrum_smoothing_strength(self, strength: int):
        s = max(1, min(10, int(strength)))
        if s == self._spectrum_smoothing_strength:
            return
        self._spectrum_smoothing_strength = s
        self.spectrum_smoothing_strength_changed.emit(s)

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
    def spectrum_fill_enabled(self) -> bool:
        """When True, the spectrum trace gets a gradient fill below
        the curve (alpha 100 → 10, top-to-bottom).  Default True —
        matches pre-patch behavior where the fill was always drawn."""
        return bool(getattr(self, "_spectrum_fill_enabled", True))

    def set_spectrum_fill_enabled(self, on: bool):
        on = bool(on)
        if on == self.spectrum_fill_enabled:
            return
        self._spectrum_fill_enabled = on
        self.spectrum_fill_enabled_changed.emit(on)

    @property
    def spectrum_fill_color(self) -> str:
        """Hex string for the spectrum trace fill color, or empty
        to derive from the trace color (default)."""
        return str(getattr(self, "_spectrum_fill_color", ""))

    def set_spectrum_fill_color(self, hex_str: str):
        """Hex like '#5ec8ff', or '' to derive from trace color."""
        v = str(hex_str or "").strip()
        if v == self.spectrum_fill_color:
            return
        self._spectrum_fill_color = v
        self.spectrum_fill_color_changed.emit(v)

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
        """Set peak decay rate in dB/second. 0.5 = very slow (peaks
        linger ~5 minutes), 120 = very fast (peaks gone in half a
        second). Clamp 0.5..120."""
        v = max(0.5, min(120.0, float(dbps)))
        if abs(v - self._peak_markers_decay_dbps) < 1e-3:
            return
        self._peak_markers_decay_dbps = v
        self.peak_markers_decay_changed.emit(v)

    # ── Peak-hold timer + decay preset (operator request 2026-05-09)
    # Tester Brent asked for a configurable hold timer on the panadapter
    # peak markers — freeze the peaks for N seconds before letting the
    # existing decay slope take over.  Common spectrum-analyzer feature
    # ("MAX HOLD" with timed release).  Plus a 4-preset decay combo on
    # the Display panel (None / Fast / Med / Slow) so the operator can
    # swap decay speed without diving into Settings.
    #
    # Sentinel values for peak_hold_secs:
    #   0.0  — Off:   peak markers hidden entirely
    #   -1.0 — Hold:  freeze forever, never decay (operator clears
    #                 manually via the Display-panel Clear button or
    #                 radio.clear_peak_holds())
    #   -2.0 — Live:  no max accumulation; markers track the live
    #                 spectrum bin-for-bin, rendering in whatever
    #                 style is set in Settings → Visuals (line /
    #                 dots / triangles).  Decay is irrelevant in
    #                 Live mode — there's nothing to fade.  Tester
    #                 request 2026-05-09 (Brent) for a "ride-along"
    #                 visual that highlights the current spectrum
    #                 in the chosen style without freezing or fading.
    #   >0.0 — Timed: freeze for that many seconds per bin, then
    #                 decay at the operator's selected rate.
    PEAK_HOLD_INFINITE = -1.0
    PEAK_HOLD_LIVE     = -2.0
    # Combo presets (seconds), in display order.  Single source of
    # truth used by both the Display-panel combo and any future
    # Settings dialog readout.
    PEAK_HOLD_PRESETS_SECS = (
        0.0,    # Off
        -2.0,   # Live (NEW 2026-05-09)
        1.0,
        2.0,
        5.0,
        10.0,
        30.0,
        -1.0,   # Hold / Infinite
    )
    # Decay preset map — combo on Display panel picks one of three.
    # Operator-tuned 2026-05-09 (Brent) for "fade in N seconds for a
    # typical 60 dB peak" intuition:
    #   "fast" — 30 dB/sec (~2 sec to fade 60 dB)
    #   "med"  — 12 dB/sec (~5 sec to fade 60 dB)  default
    #   "slow" — 6 dB/sec  (~10 sec to fade 60 dB)
    # ("None" preset removed — operator-tested 2026-05-09: with the
    # 30 dB/s Fast preset, instant-snap behavior is visually
    # indistinguishable from Fast at any reasonable viewing
    # distance.  Use Hold + Clear if you really need a strobe.)
    PEAK_HOLD_DECAY_PRESETS = {
        "fast": 30.0,
        "med":  12.0,
        "slow":  6.0,
    }

    @property
    def peak_hold_secs(self) -> float:
        """How long (seconds) to freeze the peak buffer at its current
        max before letting the decay slope take over.

        Special values:
            0.0  — Off  (peak markers hidden entirely)
            -1.0 — Hold (Infinite; never decay; manual clear required)
            -2.0 — Live (no max accumulation; track current spectrum
                         in the operator's chosen style)
            >0.0 — Timed: freeze N seconds, then decay

        Implementation lives in the spectrum widgets — they consume
        this value via their per-tick set_spectrum() call."""
        # Default Live (-2.0) — matches the pre-patch behavior where
        # peak markers were always-visible-and-tracking (operator
        # request 2026-05-09: Option B for the upgrade UX so legacy
        # operators don't lose their peak markers on first launch).
        return float(getattr(self, "_peak_hold_secs", -2.0))

    def set_peak_hold_secs(self, secs: float) -> None:
        """Set the peak-hold mode / freeze duration.  See
        ``peak_hold_secs`` for special values.  Persists via
        QSettings.  Snaps negative values to the closest sentinel
        (-1 Hold or -2 Live) to avoid float-rounding drift."""
        v = float(secs)
        # Snap negatives to the closest sentinel — operator-passed
        # values come from the combo via item-data so they're
        # already exact, but be defensive against float drift.
        if v < -1.5:
            v = self.PEAK_HOLD_LIVE
        elif v < 0:
            v = self.PEAK_HOLD_INFINITE
        old = self.peak_hold_secs
        if abs(v - old) < 1e-3:
            return
        self._peak_hold_secs = v
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("display/peak_hold_secs", v)
        except Exception as exc:
            print(f"[Radio] persist peak_hold_secs: {exc}")
        self.peak_hold_secs_changed.emit(v)

    def autoload_peak_hold_secs(self) -> None:
        """Restore the persisted peak-hold seconds on startup.
        Default 0.0 (Off) — preserves pre-toggle behavior."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            # Default Live (-2.0) on missing key — see property comment
            # for rationale (legacy-operator upgrade UX).
            v = float(s.value("display/peak_hold_secs", -2.0, type=float))
        except Exception:
            return
        # Same sentinel-snap as set_peak_hold_secs.
        if v < -1.5:
            v = self.PEAK_HOLD_LIVE
        elif v < 0:
            v = self.PEAK_HOLD_INFINITE
        self._peak_hold_secs = v

    @property
    def peak_hold_decay_preset(self) -> str:
        """Currently-active decay preset key ('fast' / 'med' / 'slow').

        Picking a preset on the Display-panel combo snaps
        peak_markers_decay_dbps to the preset's value via
        set_peak_hold_decay_preset().  When the operator drags the
        Settings → Visuals decay slider directly, this property
        returns whichever preset is closest (or '' if no preset is
        within tolerance) — useful for the combo to reflect external
        slider drags."""
        return str(getattr(self, "_peak_hold_decay_preset", "med"))

    def set_peak_hold_decay_preset(self, preset: str) -> None:
        """Apply a decay preset.  Snaps the existing
        peak_markers_decay_dbps slider to the preset's dB/sec value
        AND records the preset name for the Display-panel combo to
        display.  Two-way-syncs with the slider through this call.
        """
        key = (preset or "").strip().lower()
        if key not in self.PEAK_HOLD_DECAY_PRESETS:
            key = "med"
        old = self.peak_hold_decay_preset
        # Push the slider value first so the existing decay-changed
        # signal cascade fires once with the right number.
        self.set_peak_markers_decay_dbps(self.PEAK_HOLD_DECAY_PRESETS[key])
        if key == old:
            return
        self._peak_hold_decay_preset = key
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("display/peak_hold_decay_preset", key)
        except Exception as exc:
            print(f"[Radio] persist peak_hold_decay_preset: {exc}")
        self.peak_hold_decay_preset_changed.emit(key)

    def autoload_peak_hold_decay_preset(self) -> None:
        """Restore the persisted decay preset on startup.  Default
        'med'.  Also pushes the slider value so Settings + Display
        panel start in sync."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            key = str(s.value(
                "display/peak_hold_decay_preset", "med", type=str))
        except Exception:
            return
        if key not in self.PEAK_HOLD_DECAY_PRESETS:
            key = "med"
        self._peak_hold_decay_preset = key
        # Also snap the underlying slider so the two stay coherent
        # at startup (without firing through set_peak_hold_decay_preset
        # which would re-persist).
        self._peak_markers_decay_dbps = self.PEAK_HOLD_DECAY_PRESETS[key]

    def clear_peak_holds(self) -> None:
        """Clear the panadapter peak-hold buffer.  Emits a signal that
        the spectrum widgets listen to so they reset their per-bin
        peak_hold_db arrays.  Operator path: Display-panel Clear
        button.  Useful primarily when peak_hold_secs is Infinite —
        otherwise the decay handles it eventually."""
        self.peak_holds_cleared.emit()

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
    # NR profile = subtraction AGGRESSION for the NR1 path (Light /
    # Medium / Heavy), OR a selector for an alternative NR
    # algorithm ("nr2" → Ephraim-Malah MMSE-LSA; "neural" → reserved
    # for RNNoise / DeepFilterNet when those are wired in).
    #
    # Whether the noise reference is the live VAD-tracked estimate or
    # the operator's captured profile is independent of profile — see
    # the source-toggle API below (set_nr_use_captured_profile,
    # nr_use_captured_profile property).  Earlier draft tangled the
    # two as a 4th "captured" profile entry; separating them gives
    # the operator the full 3 × 2 combinations for NR1.  For NR2 the
    # source toggle still applies (NR2 + Captured = best classical
    # NR).
    # NR backend selector: nr1 / nr2 / neural.  Legacy strength-tier
    # names (light/medium/heavy/aggressive) are canonicalized to "nr1"
    # via _NR_PROFILE_ALIASES + their strength is applied through the
    # nr1_strength path so saved QSettings still load.  "captured"
    # was a legacy bundled-state name (medium + source-toggle on);
    # set_nr_profile handles it explicitly.
    NR_PROFILES = ("nr1", "nr2", "neural")
    _NR_PROFILE_ALIASES = {
        "light":      "nr1",
        "medium":     "nr1",
        "heavy":      "nr1",
        "aggressive": "nr1",
    }

    @staticmethod
    def neural_nr_available() -> bool:
        """Permanently False — Neural NR was explored in v0.0.6
        development (PyTorch / DeepFilterNet, then onnxruntime /
        NSNet2) but deferred until after RX2 + TX work lands.
        The right-click NR menu shows 'Neural (deferred — pending
        RX2 + TX)' as a planned-feature marker.
        """
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
        # WDSP RX engine — flip the matching WDSP NR module.  Lyra's
        # NR1 (spectral subtraction) maps to WDSP's EMNR (Ephraim-Malah
        # MMSE-LSA), and Lyra's NR2 / LMS map to WDSP's ANR (LMS line
        # enhancer).  We pick the right WDSP module based on the
        # operator's current backend selection.
        if self._wdsp_rx is not None:
            self._push_wdsp_nr_state()
        self.nr_enabled_changed.emit(on)

    # NR mode (1..4) → WDSP gain_method (0..3) mapping.
    # Operator-facing modes are 1-indexed because Thetis is 1-indexed
    # and that's what HPSDR-class operators expect.  Per emnr.c:
    #   gain_method 0 = Wiener with witchHat (SPP soft mask)
    #   gain_method 1 = Wiener simple (no SPP)
    #   gain_method 2 = MMSE-LSA via 2-D LUT (WDSP default)
    #   gain_method 3 = Trained Wiener with adaptive thresholds
    _NR_MODE_TO_GAIN_METHOD = {
        1: 0,  # Wiener + SPP — smooth, mid-aggressive
        2: 1,  # Wiener simple — edgier, more raw subtraction
        3: 2,  # MMSE-LSA — WDSP default, smoothest
        4: 3,  # Trained adaptive — newest, most aggressive
    }

    def _push_wdsp_nr_state(self) -> None:
        """Sync WDSP's NR-module run flags + EMNR character to operator state.

        Operator-facing controls in WDSP mode (post-2026-05-07 NR-UX
        overhaul):
          * NR enable button → EMNR run flag
          * NR mode 1..4     → EMNR gain_method
          * AEPF toggle      → EMNR ae_run
          * LMS enable       → ANR run flag (separate, not part of NR)

        Legacy `_nr_profile` ("nr1"/"nr2"/"neural") is preserved for
        QSettings backwards compat + external CAT/TCI but is now a
        derived value: it tracks `_nr_mode` for "did NR change?"
        signaling.  When operators select Mode 1-4 directly via the
        new UI, this method drives WDSP's EMNR knobs without
        consulting the legacy backend string.
        """
        if self._wdsp_rx is None:
            return
        on = bool(self._rx_channel.nr_enabled) and bool(
            getattr(self, "_lyra_nr_master_on", True)
        )
        # Legacy "lms" backend still routes to ANR via this method
        # so external CAT calling set_nr_profile("lms") still works.
        # New code paths use set_lms_enabled directly.
        # Init-order guard: _open_wdsp_rx runs in __init__ before
        # self._nr_profile is set.  getattr defaults the legacy
        # backend to "" so the initial push runs cleanly without
        # an AttributeError (caught + printed by _open_wdsp_rx).
        legacy_backend = (getattr(self, "_nr_profile", "") or "").lower()
        anr_on = on and legacy_backend == "lms"
        # EMNR runs whenever NR is on and the operator hasn't
        # specifically picked LMS.
        emnr_on = on and legacy_backend != "lms"
        # Map current mode to WDSP gain_method (default 3=MMSE-LSA
        # if mode is out of range somehow).
        mode = int(getattr(self, "_nr_mode", 3))
        gain_method = self._NR_MODE_TO_GAIN_METHOD.get(mode, 2)
        aepf_on = bool(getattr(self, "_aepf_enabled", True))
        npe_method = int(getattr(self, "_npe_method", self.NPE_OSMS))
        try:
            if emnr_on:
                self._wdsp_rx.set_emnr_gain_method(gain_method)
                self._wdsp_rx.set_emnr_aepf(aepf_on)
                self._wdsp_rx.set_emnr_npe_method(npe_method)
            self._wdsp_rx.set_emnr(emnr_on)
            self._wdsp_rx.set_anr(anr_on)
        except Exception as exc:
            print(f"[Radio] WDSP rx NR state error: {exc}")

    @property
    def nr_profile(self) -> str:
        return self._nr_profile

    def set_nr_profile(self, name: str):
        """Pick the NR backend: ``nr1`` / ``nr2`` / ``neural``.

        Legacy migration paths handled here:
          - ``captured`` (old bundled name = Medium + source-on)
            → set_nr_use_captured_profile(True), backend = nr1,
            strength left wherever the operator had it (or migrated
            via autoload_nr1_settings if it was saved as a legacy
            tier name).
          - ``light`` / ``medium`` / ``heavy`` / ``aggressive``
            (old discrete strength tiers) → backend = nr1, strength
            updated to the equivalent slider value via the legacy
            alias map in SpectralSubtractionNR.

        Strength is no longer set here — operators use set_nr1_strength
        (continuous slider) for NR1 and set_nr2_aggression for NR2.
        """
        from lyra.dsp.nr import SpectralSubtractionNR
        raw = (name or "").strip().lower()
        # Legacy "captured" — strength stays where it is, just flip
        # the source toggle.
        if raw == "captured":
            self.set_nr_use_captured_profile(True)
            raw = "nr1"
        # Legacy strength-tier names: route to NR1 backend AND
        # apply the equivalent strength so the operator's previous
        # tier preference carries over.
        if raw in SpectralSubtractionNR._LEGACY_PROFILE_TO_STRENGTH:
            self.set_nr1_strength(
                SpectralSubtractionNR._LEGACY_PROFILE_TO_STRENGTH[raw])
            raw = "nr1"
        # Final canonicalization (also handles None/empty → default).
        backend = self._NR_PROFILE_ALIASES.get(raw, raw)
        if backend not in self.NR_PROFILES:
            backend = "nr1"
        # Neural backend is currently a deferred-feature placeholder
        # — silently fall through to NR1 so any saved-state pointing
        # at "neural" still produces functional audio.  See
        # neural_nr_available() docstring for the deferment context.
        if backend == "neural":
            backend = "nr1"
        self._nr_profile = backend
        self._rx_channel.set_nr_profile(backend)
        # Legacy "nr1"/"nr2" backends migrate to NR mode for the new
        # UI: nr1 → mode 3 (MMSE-LSA, current default behavior),
        # nr2 → mode 1 (Wiener+SPP — old NR2 character).  This keeps
        # the operator's saved-state experience consistent across
        # the UX overhaul.  External CAT/TCI calling set_nr_profile
        # see the equivalent NR mode in WDSP.
        if backend == "nr2":
            self._nr_mode = 1
        elif backend == "nr1":
            self._nr_mode = 3
        # WDSP NR module pick follows the operator's backend selection.
        if self._wdsp_rx is not None:
            self._push_wdsp_nr_state()
        self.nr_profile_changed.emit(backend)
        self.nr_mode_changed.emit(self._nr_mode)

    # ── NR mode (Thetis-style 1..4 selector) ─────────────────────────
    #
    # Replaces the legacy NR1/NR2 backend dropdown + dual strength
    # sliders with a single integer-valued mode selector that drives
    # WDSP's EMNR gain_method directly.  See _NR_MODE_TO_GAIN_METHOD
    # above for the mapping; see _push_wdsp_nr_state for the apply
    # path.

    NR_MODE_MIN = 1
    NR_MODE_MAX = 4
    NR_MODE_DEFAULT = 3   # MMSE-LSA — WDSP default, matches old NR1

    @property
    def nr_mode(self) -> int:
        return int(getattr(self, "_nr_mode", self.NR_MODE_DEFAULT))

    def set_nr_mode(self, mode: int) -> None:
        """Set the NR mode (1..4).  Clamps out-of-range values."""
        m = max(self.NR_MODE_MIN, min(self.NR_MODE_MAX, int(mode)))
        if m == getattr(self, "_nr_mode", None):
            return
        self._nr_mode = m
        # Persist for next session.
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").setValue("noise/nr_mode", m)
        except Exception as exc:
            print(f"[Radio] could not persist nr_mode: {exc}")
        if self._wdsp_rx is not None:
            self._push_wdsp_nr_state()
        self.nr_mode_changed.emit(m)

    # ── NPE method (noise power estimator selection) ───────────────
    #
    # Operator-tunable noise-tracker choice — one of WDSP EMNR's
    # internal knobs that's normally hidden in other clients.
    # Surfaced on the DSP+Audio panel for quick on-air A/B between
    # OSMS (smooth, stationary noise) and MCRA (fast-tracking,
    # non-stationary band conditions).
    NPE_OSMS = 0  # WDSP default — recursive averaging
    NPE_MCRA = 1  # Newer — Minimum-Controlled Recursive Averaging

    @property
    def npe_method(self) -> int:
        return int(getattr(self, "_npe_method", self.NPE_OSMS))

    def set_npe_method(self, method: int) -> None:
        """Pick the EMNR noise-power estimator (0 = OSMS, 1 = MCRA)."""
        m = int(method)
        if m not in (self.NPE_OSMS, self.NPE_MCRA):
            m = self.NPE_OSMS
        if m == getattr(self, "_npe_method", None):
            return
        self._npe_method = m
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").setValue("noise/npe_method", m)
        except Exception as exc:
            print(f"[Radio] could not persist npe_method: {exc}")
        if self._wdsp_rx is not None:
            self._push_wdsp_nr_state()
        self.npe_method_changed.emit(m)

    # ── AEPF (anti-musical-noise post-filter) ──────────────────────
    @property
    def aepf_enabled(self) -> bool:
        return bool(getattr(self, "_aepf_enabled", True))

    def set_aepf_enabled(self, on: bool) -> None:
        """Toggle WDSP's Adaptive Equalization Post-Filter.

        AEPF smooths the EMNR gain mask across frequency bins to
        reduce musical-noise artifacts.  Default ON because the
        un-AEPF residual character is noticeably more "watery" /
        pronounced.  Operator can disable for raw EMNR character on
        clean bands where AEPF's smoothing isn't needed.
        """
        on = bool(on)
        if on == bool(getattr(self, "_aepf_enabled", True)):
            return
        self._aepf_enabled = on
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").setValue("noise/aepf_enabled", on)
        except Exception as exc:
            print(f"[Radio] could not persist aepf_enabled: {exc}")
        if self._wdsp_rx is not None:
            self._push_wdsp_nr_state()
        self.aepf_enabled_changed.emit(on)

    # ── Noise SOURCE toggle (Phase 3.D #1, orthogonal to profile) ────

    @property
    def nr_use_captured_profile(self) -> bool:
        """Operator's preference for the NR noise SOURCE.

        True  → use the loaded captured profile as the noise reference
                (falls back to live tracking if no profile is loaded)
        False → always use the live VAD-tracked estimate

        Independent of ``nr_profile`` — operator picks aggression
        (Light/Medium/Heavy) and source separately."""
        return self._nr_use_captured_profile

    def set_nr_use_captured_profile(self, on: bool) -> None:
        on = bool(on)
        if on == self._nr_use_captured_profile:
            return
        self._nr_use_captured_profile = on
        # Legacy nr.py state-mirror call.  Harmless flag-set on the
        # orphan ``_rx_channel._nr`` instance (no consumer in WDSP
        # mode); deferred for the cleanup pass.
        self._rx_channel.set_use_captured_profile(on)
        # Reset apply streaming state on every transition so the
        # ring buffers don't carry stale IQ across an OFF→ON
        # cycle.  Without this the first frame after re-engage
        # would be a mix of leftover samples from the last ON
        # period with new IQ — audible as a click (joint-audit
        # finding, Phase 4 review).  Cheap (~µs) and on-thread.
        with self._iq_capture_lock:
            if self._iq_capture is not None:
                self._iq_capture.reset_apply_streaming_state()
        self.nr_use_captured_profile_changed.emit(on)
        # §14.6 Phase 4: the IQ-domain apply pass is now wired
        # (pre-WDSP, in _do_demod_wdsp).  Toggling this flag
        # actually enables/disables spectral subtraction at the
        # IQ layer, so the previous "INERT in WDSP mode" status
        # warning is no longer applicable and has been removed.
        # Operators can hear the effect directly; the DSP+Audio
        # panel badge shows the loaded profile name.
        # Persist the toggle state so the next Lyra start matches
        # what the operator left running.
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/use_captured_profile", bool(on))
        except Exception as exc:
            print(f"[Radio] could not persist nr source toggle: {exc}")

    # ── Captured noise profile API (Phase 3.D #1) ────────────────────

    def begin_noise_capture(self, seconds: float = 2.0) -> None:
        """Start an N-second IQ-domain capture of the current
        band noise (§14.6, v0.0.9.9 IQ-domain engine).

        Operator-driven entry point.  UI button hooks into this.
        Caller is responsible for tuning to a noise-only patch of
        band (or being inside a transmission gap) before invoking.
        Capture progresses inside the IQ chain on subsequent
        blocks (tapped pre-WDSP in ``_do_demod_wdsp``); when it
        completes, ``noise_capture_done`` fires so UI can prompt
        for a save name.

        ``seconds`` is clamped at the engine to a minimum of one
        frame (very short captures produce overfit profiles); UI
        typically clamps to 1.0..5.0 s for operator sanity.

        No-op if the IQ capture engine isn't available (failed to
        initialize at WDSP channel open time).
        """
        with self._iq_capture_lock:
            if self._iq_capture is None:
                print("[Radio] capture skipped — iq_capture not "
                      "initialized")
                return
            self._iq_capture.begin_capture(float(seconds))

    def cancel_noise_capture(self) -> None:
        """Abort an in-progress IQ capture.  No-op if none
        running or engine not initialized."""
        with self._iq_capture_lock:
            if self._iq_capture is None:
                return
            self._iq_capture.cancel_capture()

    def has_captured_profile(self) -> bool:
        """True if a v2 IQ-domain profile is currently loaded
        into the apply engine."""
        with self._iq_capture_lock:
            if self._iq_capture is None:
                return False
            return self._iq_capture.has_profile

    def nr_capture_progress(self) -> tuple[str, float]:
        """Return ``(state, fraction_complete)`` for UI progress
        bar.  ``state ∈ {"idle", "capturing", "ready"}``.
        Returns ``("idle", 0.0)`` if engine not initialized."""
        with self._iq_capture_lock:
            if self._iq_capture is None:
                return ("idle", 0.0)
            return self._iq_capture.progress()

    @property
    def active_captured_profile_name(self) -> str:
        """Display name of the currently-loaded captured profile,
        or "" if none is loaded.  Lyra-restart-persistent via
        QSettings."""
        return self._active_captured_profile_name

    @property
    def active_captured_profile_meta(self) -> Optional[dict]:
        """Metadata bundle for the active captured profile, or
        None if no profile is loaded.  Keys: name, captured_at_iso,
        freq_hz, mode, duration_sec, fft_size.  Used by the inline
        status badge on the DSP+Audio panel for age coloring +
        mode/band mismatch warnings."""
        return self._active_captured_profile_meta

    def clear_captured_profile(self) -> None:
        """Drop the loaded captured profile from the IQ apply
        engine.  Apply path becomes a passthrough.  If the NR
        source toggle was on, callers typically also flip it back
        to "stock" (UI handles that)."""
        had = self.has_captured_profile()
        with self._iq_capture_lock:
            if self._iq_capture is not None:
                self._iq_capture.clear_profile()
        if self._active_captured_profile_name:
            self._active_captured_profile_name = ""
            self._active_captured_profile_meta = None
            self.noise_active_profile_changed.emit("")
        # If the source toggle was on, flip it back to Live — there's
        # no captured profile to use anymore, so the source flag
        # would just be a misleading UI state.  NR aggression
        # profile (Light/Medium/Heavy) is left alone.
        if self._nr_use_captured_profile:
            self.set_nr_use_captured_profile(False)
        # Persist the cleared state so the next Lyra start doesn't
        # try to auto-restore a no-longer-active profile.
        self._save_active_profile_name_setting("")
        if had:
            # Fire profiles_changed too — manager UI may want to
            # update the "currently loaded" indicator dot.
            self.noise_profiles_changed.emit()

    # ── Captured-profile JSON persistence wrappers ───────────────────

    @property
    def noise_profile_folder(self):
        """Pathlib.Path to the active noise-profile storage folder.

        Resolved lazily from QSettings ``noise/profile_folder``;
        falls back to the OS-default user-data folder
        (%APPDATA%/Lyra/noise_profiles on Windows etc.).  See
        :func:`lyra.dsp.noise_profile_store.resolve_profile_folder`."""
        from lyra.dsp import noise_profile_store as nps
        from PySide6.QtCore import QSettings
        s = QSettings("N8SDR", "Lyra")
        custom = str(s.value("noise/profile_folder", "", type=str) or "")
        return nps.resolve_profile_folder(custom)

    def set_noise_profile_folder(self, path: str) -> None:
        """Set a custom storage folder.  Empty string restores the
        default.  Persisted via QSettings; takes effect immediately
        for subsequent save/load operations.
        """
        from PySide6.QtCore import QSettings
        s = QSettings("N8SDR", "Lyra")
        s.setValue("noise/profile_folder", str(path or ""))
        # The manager-dialog list view will re-scan the new folder;
        # signal lets it know to refresh.
        self.noise_profiles_changed.emit()

    def list_saved_noise_profiles(self):
        """Scan the active profile folder and return a list of
        :class:`ProfileMeta` records (newest first)."""
        from lyra.dsp import noise_profile_store as nps
        return nps.list_profiles(self.noise_profile_folder)

    def save_current_capture_as(self, name: str,
                                overwrite: bool = False):
        """Persist the currently-loaded v2 IQ-domain captured
        profile to disk under ``name``.

        Pulls the live magnitudes array from the IQ capture
        engine plus current operator metadata (freq, mode, IQ
        rate, FFT size, capture duration as recorded) and packages
        it via :func:`noise_profile_store.make_profile_from_capture`.

        Returns the Path the profile was saved to.  Raises
        ``FileExistsError`` if a profile with the same name
        already exists and ``overwrite`` is False; ``ValueError``
        if there's no captured profile to save.
        """
        from lyra.dsp import noise_profile_store as nps
        from lyra import __version__ as lyra_version

        # Snapshot engine state under the lock so worker-thread
        # mutations during a re-capture can't tear our values
        # apart.  Do file I/O (save_profile) outside the lock —
        # save can take milliseconds on a slow disk and we don't
        # want to block the worker's _do_demod_wdsp tap.
        with self._iq_capture_lock:
            if self._iq_capture is None:
                raise ValueError(
                    "no IQ capture engine — open a WDSP channel "
                    "first")
            mag = self._iq_capture.captured_profile_array()
            if mag is None:
                raise ValueError(
                    "no captured profile loaded — capture one "
                    "first")
            # Capture duration: derived from the engine's
            # last-armed frame target.  See
            # ``CapturedProfileIQ.last_capture_duration_sec`` for
            # the math + edge-case behavior (returns 0.0 if no
            # local capture has been armed in this session, e.g.
            # operator is saving a profile they just loaded from
            # disk — acceptable since "save loaded profile under
            # new name" is a fringe flow with Export as a cleaner
            # path).
            duration = self._iq_capture.last_capture_duration_sec
            engine_fft_size = int(self._iq_capture.fft_size)
            engine_rate_hz = int(self._iq_capture.rate_hz)

        profile = nps.make_profile_from_capture(
            name=name,
            magnitudes=mag,
            freq_hz=int(self._freq_hz),
            mode=str(self._mode),
            duration_sec=duration,
            fft_size=engine_fft_size,
            rate_hz=engine_rate_hz,
            lyra_version=str(lyra_version),
        )
        path = nps.save_profile(self.noise_profile_folder,
                                profile, overwrite=overwrite)

        # Mark this profile as the active one (it IS what's loaded
        # in the IQ engine right now) so a subsequent Lyra restart
        # auto-restores it via autoload_active_noise_profile.
        self._active_captured_profile_name = name
        # Cache the metadata bundle the inline DSP-panel badge
        # needs.  Mirror of the JSON fields so the badge can
        # display matching info regardless of whether the profile
        # was just captured or loaded from disk.
        self._active_captured_profile_meta = {
            "name": name,
            "captured_at_iso": profile.captured_at_iso,
            "freq_hz": profile.freq_hz,
            "mode": profile.mode,
            "duration_sec": profile.duration_sec,
            "fft_size": profile.fft_size,
            "rate_hz": profile.rate_hz,
        }
        self._save_active_profile_name_setting(name)
        self.noise_active_profile_changed.emit(name)
        self.noise_profiles_changed.emit()
        return path

    def load_saved_noise_profile(self, name: str) -> None:
        """Load a v2 IQ-domain profile from disk into the apply
        engine.

        The on-disk profile must be schema v2 IQ-domain (v1
        audio-domain profiles are refused upstream by
        :func:`noise_profile_store.load_profile` with a clear
        recapture hint).  Profile FFT size and IQ rate must match
        the current engine; mismatches raise ``ValueError`` so
        the operator gets a clean error rather than silently
        plausible-but-wrong subtraction.

        Apply-path activation is operator-controlled separately
        via ``set_nr_use_captured_profile`` — loading stages the
        profile in the engine, but the spectral subtraction only
        runs when the operator turns the source toggle ON.
        Loading also resets the engine's apply streaming state so
        the first frame after a load doesn't carry stale samples
        from a previous profile (joint-audit P1, Phase 4 review).

        Raises:
            FileNotFoundError: profile doesn't exist on disk.
            ValueError: schema mismatch, FFT size mismatch, or
                IQ rate mismatch.
            RuntimeError: IQ engine not initialized (no WDSP
                channel open).
        """
        from lyra.dsp import noise_profile_store as nps
        # File I/O (load_profile reads the JSON) happens outside
        # the engine lock — disk reads can be milliseconds and we
        # don't want to block the worker thread.  The engine
        # check + rate/FFT match + actual load go under the lock.
        prof = nps.load_profile(self.noise_profile_folder, name)
        with self._iq_capture_lock:
            if self._iq_capture is None:
                raise RuntimeError(
                    "no IQ capture engine — open a WDSP channel "
                    "first")
            # Refuse cross-rate or cross-FFT-size profiles up
            # front.  Phase 4 apply assumes the loaded profile
            # matches engine config bin-for-bin; refusing here
            # is the explicit "interpolation across rates not
            # supported" decision from §14.6.
            if prof.rate_hz != self._iq_capture.rate_hz:
                raise ValueError(
                    f"profile {name!r} was captured at "
                    f"{prof.rate_hz} Hz IQ rate; current radio "
                    f"rate is {self._iq_capture.rate_hz} Hz.  "
                    f"Switch the radio to {prof.rate_hz} Hz or "
                    f"recapture at the current rate.")
            if prof.fft_size != self._iq_capture.fft_size:
                raise ValueError(
                    f"profile {name!r} was captured at FFT size "
                    f"{prof.fft_size}; current engine uses "
                    f"{self._iq_capture.fft_size}.  Recapture, "
                    f"or change the FFT-size setting before "
                    f"loading.")
            self._iq_capture.load_profile(prof.magnitudes)
        self._active_captured_profile_name = prof.name
        # Cache metadata for the DSP+Audio panel inline badge
        # (freq / mode / age / rate display).
        self._active_captured_profile_meta = {
            "name": prof.name,
            "captured_at_iso": prof.captured_at_iso,
            "freq_hz": prof.freq_hz,
            "mode": prof.mode,
            "duration_sec": prof.duration_sec,
            "fft_size": prof.fft_size,
            "rate_hz": prof.rate_hz,
        }
        self._save_active_profile_name_setting(prof.name)
        self.noise_active_profile_changed.emit(prof.name)

    def delete_saved_noise_profile(self, name: str) -> bool:
        from lyra.dsp import noise_profile_store as nps
        deleted = nps.delete_profile(self.noise_profile_folder, name)
        if deleted:
            # If the deleted profile was the active one, clear the
            # active marker (the in-NR magnitudes stay until the
            # operator clears them or loads another — deletion of
            # the disk file shouldn't disrupt audio).
            if name == self._active_captured_profile_name:
                self._active_captured_profile_name = ""
                self._save_active_profile_name_setting("")
                self.noise_active_profile_changed.emit("")
            self.noise_profiles_changed.emit()
        return deleted

    def rename_saved_noise_profile(self, old_name: str,
                                   new_name: str,
                                   overwrite: bool = False):
        from lyra.dsp import noise_profile_store as nps
        path = nps.rename_profile(
            self.noise_profile_folder, old_name, new_name,
            overwrite=overwrite)
        if old_name == self._active_captured_profile_name:
            self._active_captured_profile_name = new_name
            self._save_active_profile_name_setting(new_name)
            self.noise_active_profile_changed.emit(new_name)
        self.noise_profiles_changed.emit()
        return path

    def export_saved_noise_profile(self, name: str, dst_path):
        from lyra.dsp import noise_profile_store as nps
        return nps.export_profile(
            self.noise_profile_folder, name, dst_path)

    def import_saved_noise_profile(self, src_path,
                                   rename_to: str | None = None,
                                   overwrite: bool = False) -> str:
        from lyra.dsp import noise_profile_store as nps
        name = nps.import_profile(
            src_path, self.noise_profile_folder,
            rename_to=rename_to, overwrite=overwrite)
        self.noise_profiles_changed.emit()
        return name

    # ── Internal: capture-done callback + settings persistence ───────

    # ── Noise Blanker (NB) API — Phase 3.D #2 ─────────────────────

    # Strength tiers + custom slot.  Old name "aggressive" canonicalizes
    # to "heavy" via _NB_PROFILE_ALIASES so saved QSettings still load.
    NB_PROFILES = ("off", "light", "medium", "heavy", "custom")
    _NB_PROFILE_ALIASES = {"aggressive": "heavy"}

    @property
    def nb_enabled(self) -> bool:
        """True if NB is currently doing work (profile != 'off' AND
        threshold is at/above the minimum useful value)."""
        return self._rx_channel.nb_enabled

    @property
    def nb_profile(self) -> str:
        return self._rx_channel.nb_profile

    @property
    def nb_threshold(self) -> float:
        return self._rx_channel.nb_threshold

    def set_nb_profile(self, name: str) -> None:
        """Apply an NB profile preset.

        Names: ``off`` / ``light`` / ``medium`` / ``heavy`` /
        ``custom``.  Custom retains the current threshold; other
        names install the preset's threshold.  Persists via
        QSettings.  Legacy ``aggressive`` is canonicalized to
        ``heavy``.
        """
        name = (name or "").strip().lower()
        name = self._NB_PROFILE_ALIASES.get(name, name)
        if name not in self.NB_PROFILES:
            name = "off"
        self._rx_channel.set_nb_profile(name)
        # WDSP RX engine — noise blanker (NOB/ANB) lives at the EXT
        # layer, before the RXA channel.  RxChannel.__init__ calls
        # init_blankers automatically so SetEXTNOBRun is safe; we
        # mirror Lyra's profile -> WDSP NOB threshold via the
        # helper (off / light / medium / heavy / custom).
        self._push_wdsp_nb_state()
        # Persist for next Lyra start.
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nb_profile", name)
            # Save the threshold too — the channel may have changed
            # it (presets bake their own values).
            s.setValue("noise/nb_threshold",
                       float(self._rx_channel.nb_threshold))
        except Exception as exc:
            print(f"[Radio] could not persist NB profile: {exc}")
        self.nb_profile_changed.emit(name)
        self.nb_threshold_changed.emit(self._rx_channel.nb_threshold)

    def set_nb_threshold(self, threshold: float) -> None:
        """Operator-tunable NB threshold (Custom profile).

        Switches profile to ``custom`` because the operator is
        hand-tuning.  Clamped to ``_NBState.[THRESHOLD_MIN,
        THRESHOLD_MAX]``.  Persists via QSettings.
        """
        self._rx_channel.set_nb_threshold(float(threshold))
        # Mirror operator-set custom threshold into WDSP NOB.
        self._push_wdsp_nb_state()
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nb_profile", "custom")
            s.setValue("noise/nb_threshold",
                       float(self._rx_channel.nb_threshold))
        except Exception as exc:
            print(f"[Radio] could not persist NB threshold: {exc}")
        self.nb_profile_changed.emit("custom")
        self.nb_threshold_changed.emit(self._rx_channel.nb_threshold)

    def autoload_nb_settings(self) -> None:
        """Restore NB profile + threshold from QSettings on Lyra
        startup.  Called from app.py alongside the captured-profile
        autoload.  Silently no-ops if the saved values are missing
        or out of range — operator can reconfigure from the Settings
        → Noise tab."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            profile = str(s.value("noise/nb_profile", "off",
                                  type=str) or "off")
            threshold = float(s.value("noise/nb_threshold", 6.0,
                                      type=float))
        except Exception:
            return
        # Apply threshold first (in case profile is "custom"), then
        # the profile.  Profile presets overwrite threshold; "custom"
        # preserves it.
        try:
            if profile == "custom":
                self.set_nb_threshold(threshold)
            else:
                # Pre-seed the threshold so a later switch to
                # custom recalls the operator's last hand-tuned
                # value.
                self._rx_channel.set_nb_threshold(threshold)
                self.set_nb_profile(profile)
        except Exception as exc:
            print(f"[Radio] could not autoload NB settings: {exc}")

    # ── Auto Notch Filter (ANF) API — Phase 3.D #3 ────────────────

    # Strength tiers + custom slot.  Old names (gentle/standard/
    # aggressive) canonicalize to (light/medium/heavy) via
    # _ANF_PROFILE_ALIASES so saved QSettings still load.
    ANF_PROFILES = ("off", "light", "medium", "heavy", "custom")
    _ANF_PROFILE_ALIASES = {
        "gentle":     "light",
        "standard":   "medium",
        "aggressive": "heavy",
    }

    @property
    def anf_enabled(self) -> bool:
        """True if ANF is currently doing work (profile != 'off')."""
        return self._rx_channel.anf_enabled

    @property
    def anf_profile(self) -> str:
        return self._rx_channel.anf_profile

    @property
    def anf_mu(self) -> float:
        return self._rx_channel.anf_mu

    def set_anf_profile(self, name: str) -> None:
        """Apply an ANF profile preset.

        Names: off / light / medium / heavy / custom.  Custom
        retains the current μ; presets install the preset's
        value.  Persists via QSettings.  Legacy names
        (gentle/standard/aggressive) are canonicalized.
        """
        name = (name or "").strip().lower()
        name = self._ANF_PROFILE_ALIASES.get(name, name)
        if name not in self.ANF_PROFILES:
            name = "off"
        self._rx_channel.set_anf_profile(name)
        # WDSP RX engine — auto-notch is one binary run flag; profile
        # name controls strength in Lyra's port but WDSP's ANF has
        # fixed defaults that work well for most signals. Anything
        # other than "off" enables the notch.
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_anf(name != "off")
            except Exception as exc:
                print(f"[Radio] WDSP rx ANF state error: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/anf_profile", name)
            s.setValue("noise/anf_mu",
                       float(self._rx_channel.anf_mu))
        except Exception as exc:
            print(f"[Radio] could not persist ANF profile: {exc}")
        self.anf_profile_changed.emit(name)
        self.anf_mu_changed.emit(self._rx_channel.anf_mu)

    def set_anf_mu(self, mu: float) -> None:
        """Operator-tunable ANF adaptation step size.

        Switches profile to 'custom'.  Clamped to ANF's
        [MU_MIN, MU_MAX].  Persists via QSettings.

        Phase 6.A4: also pushes the value to WDSP via
        SetRXAANFVals (anf.c) so the slider actually drives ANF
        behavior — previously the value was persisted on the
        channel but never reached the engine.
        """
        self._rx_channel.set_anf_mu(float(mu))
        # Push the new μ to WDSP.  Keep taps=64 / delay=16 /
        # gamma=0.10 (anf.c-recommended) and just update two_mu.
        if self._wdsp_rx is not None:
            try:
                two_mu = float(self._rx_channel.anf_mu)
                self._wdsp_rx.set_anf_vals(
                    taps=64, delay=16,
                    gain=two_mu, leakage=0.10,
                )
            except Exception as exc:
                print(f"[Radio] WDSP ANF μ push: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/anf_profile", "custom")
            s.setValue("noise/anf_mu",
                       float(self._rx_channel.anf_mu))
        except Exception as exc:
            print(f"[Radio] could not persist ANF mu: {exc}")
        self.anf_profile_changed.emit("custom")
        self.anf_mu_changed.emit(self._rx_channel.anf_mu)

    def autoload_anf_settings(self) -> None:
        """Restore ANF profile + μ from QSettings on Lyra startup."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            profile = str(s.value("noise/anf_profile", "off",
                                  type=str) or "off")
            mu = float(s.value("noise/anf_mu", 1.5e-4, type=float))
        except Exception:
            return
        try:
            if profile == "custom":
                self.set_anf_mu(mu)
            else:
                self._rx_channel.set_anf_mu(mu)
                self.set_anf_profile(profile)
        except Exception as exc:
            print(f"[Radio] could not autoload ANF settings: {exc}")

    # NOTE: Audio Leveler API (LEVELER_PROFILES, leveler_enabled,
    # leveler_profile, leveler_threshold_db, leveler_ratio,
    # leveler_makeup_db, set_leveler_profile, set_leveler_threshold_db,
    # set_leveler_ratio, set_leveler_makeup_db, _persist_leveler_custom,
    # autoload_leveler_settings) removed in Phase 4 of legacy-DSP
    # cleanup.  The leveler became inert in WDSP mode after Phase 3
    # (its only callers were inside the orphan _apply_agc_and_volume
    # method) and WDSP's AGC modes already provide better dynamic-
    # range handling.  See git history for the deleted code if
    # anyone needs to recover the API surface.
    #
    # Persisted QSettings keys (audio/leveler_profile,
    # audio/leveler_threshold_db, audio/leveler_ratio,
    # audio/leveler_makeup_db) are intentionally NOT cleared from
    # the operator's QSettings store — silently orphaning them
    # avoids a destructive read-then-delete cycle if the operator
    # ever rolls back to a pre-Phase-4 build.

    # ── NR2 (Ephraim-Malah MMSE-LSA) API — Phase 3.D #4 ─────────────

    @property
    def nr2_aggression(self) -> float:
        return self._rx_channel.nr2_aggression

    @property
    def nr2_musical_noise_smoothing(self) -> bool:
        return self._rx_channel.nr2_musical_noise_smoothing

    @property
    def nr2_speech_aware(self) -> bool:
        return self._rx_channel.nr2_speech_aware

    def set_nr2_aggression(self, value: float) -> None:
        """Operator-tunable NR2 suppression strength.

        0.0 = unity gain (effectively NR off)
        1.0 = full MMSE-LSA (default)
        2.0 = harder cleanup with mild thinning
        Clamped to [0.0, 2.0] inside ``_NR2State``.  Persists to
        QSettings; pushed to WDSP's EMNR via _push_wdsp_nr_state.
        """
        self._rx_channel.set_nr2_aggression(float(value))
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nr2_aggression",
                       float(self._rx_channel.nr2_aggression))
        except Exception as exc:
            print(f"[Radio] could not persist nr2 aggression: {exc}")
        self.nr2_aggression_changed.emit(self._rx_channel.nr2_aggression)

    def set_nr2_musical_noise_smoothing(self, on: bool) -> None:
        """Toggle the decision-directed ξ smoothing that eliminates
        the musical-noise artifact.  On (default) = full MMSE-LSA;
        Off = closer to NR1 behavior (diagnostic A/B).  Persists."""
        self._rx_channel.set_nr2_musical_noise_smoothing(bool(on))
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nr2_musical_noise_smoothing", bool(on))
        except Exception as exc:
            print(f"[Radio] could not persist nr2 smoothing: {exc}")
        self.nr2_musical_noise_smoothing_changed.emit(bool(on))

    def set_nr2_speech_aware(self, on: bool) -> None:
        """Toggle simple-VAD speech-aware mode.  Reduces NR2
        suppression during detected voice (preserves consonants).
        Off by default.  Persists."""
        self._rx_channel.set_nr2_speech_aware(bool(on))
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nr2_speech_aware", bool(on))
        except Exception as exc:
            print(f"[Radio] could not persist nr2 speech_aware: {exc}")
        self.nr2_speech_aware_changed.emit(bool(on))

    def autoload_nr2_settings(self) -> None:
        """Restore NR2's operator knobs from QSettings."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            agg = float(s.value("noise/nr2_aggression", 1.0,
                                type=float))
            smooth = bool(s.value("noise/nr2_musical_noise_smoothing",
                                  True, type=bool))
            speech = bool(s.value("noise/nr2_speech_aware", False,
                                  type=bool))
            method = str(s.value("noise/nr2_gain_method", "mmse_lsa",
                                  type=str))
        except Exception:
            return
        try:
            self.set_nr2_aggression(agg)
            self.set_nr2_musical_noise_smoothing(smooth)
            self.set_nr2_speech_aware(speech)
            self.set_nr2_gain_method(method)
        except Exception as exc:
            print(f"[Radio] could not autoload NR2 settings: {exc}")

    @property
    def nr2_gain_method(self) -> str:
        """Current NR2 gain function name: 'mmse_lsa' or 'wiener'."""
        try:
            return str(self._rx_channel._nr2.gain_method)
        except Exception:
            return "mmse_lsa"

    def set_nr2_gain_method(self, method: str) -> None:
        """Pick the NR2 gain function — 'mmse_lsa' (default) or
        'wiener'.  Persists to QSettings and emits
        ``nr2_gain_method_changed``."""
        m = (method or "").strip().lower()
        if m not in ("mmse_lsa", "wiener"):
            m = "mmse_lsa"
        try:
            self._rx_channel._nr2.set_gain_method(m)
        except Exception as exc:
            print(f"[Radio] could not set NR2 gain method: {exc}")
            return
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nr2_gain_method", m)
        except Exception as exc:
            print(f"[Radio] could not persist nr2 gain method: {exc}")
        self.nr2_gain_method_changed.emit(m)

    # ── LMS (NR3 line enhancer) API ───────────────────────────────────

    @property
    def lms_enabled(self) -> bool:
        return self._rx_channel.lms_enabled

    @property
    def lms_strength(self) -> float:
        return self._rx_channel.lms_strength

    def set_lms_enabled(self, on: bool) -> None:
        """Master toggle for the LMS adaptive line enhancer.  Persists
        to QSettings and emits ``lms_enabled_changed``."""
        on = bool(on)
        self._rx_channel.set_lms_enabled(on)
        # WDSP RX engine — LMS adaptive line enhancer is the same
        # algorithm WDSP exposes as ANR.  When the operator wants LMS
        # on, ANR runs.  Mutual-exclusion with EMNR (NR backend) is
        # handled in _push_wdsp_nr_state when set_nr_profile flips to
        # "lms"; this setter just toggles the run flag directly so
        # the operator's LMS button works whether or not NR is on.
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_anr(on)
            except Exception as exc:
                print(f"[Radio] WDSP rx LMS state error: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/lms_enabled", on)
        except Exception as exc:
            print(f"[Radio] could not persist lms_enabled: {exc}")
        self.lms_enabled_changed.emit(on)

    def set_lms_strength(self, value: float) -> None:
        """LMS strength slider (0.0..1.0).  Persists to QSettings
        and emits ``lms_strength_changed``.

        In WDSP mode the slider drives ``SetRXAANRGain`` — the LMS
        step size (a.k.a. ``two_mu`` / mu).  WDSP's default is
        0.0001; stable upper bound roughly 0.001.  We map slider
        0..1 onto a logarithmic 0.00005..0.001 sweep so:
          * slider 0%  → mu = 5e-5    (very gentle, barely adapts)
          * slider 50% → mu = ~2e-4   (close to WDSP default)
          * slider 100% → mu = 1e-3   (aggressive, just below
                                       instability threshold)
        Logarithmic mapping makes the slider behavior feel
        progressive across the full range — linear would put
        most of the audible change in the top 10% of travel.
        """
        v = max(0.0, min(1.0, float(value)))
        self._rx_channel.set_lms_strength(v)
        # WDSP path: map 0..1 slider → 5e-5..1e-3 ANR gain
        # (logarithmic).  Operator's slider position now actually
        # changes the LMS step size, addressing the "sliders don't
        # do anything in WDSP mode" complaint for LMS.
        if self._wdsp_rx is not None:
            try:
                import math
                # Log-interp between [5e-5, 1e-3].
                lo_log = math.log(5e-5)
                hi_log = math.log(1e-3)
                mu = math.exp(lo_log + v * (hi_log - lo_log))
                self._wdsp_rx.set_anr_gain(mu)
            except Exception as exc:
                print(f"[Radio] WDSP rx LMS strength error: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/lms_strength", v)
        except Exception as exc:
            print(f"[Radio] could not persist lms_strength: {exc}")
        self.lms_strength_changed.emit(v)

    def autoload_lms_settings(self) -> None:
        """Restore LMS toggle + strength from QSettings."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            on = bool(s.value("noise/lms_enabled", False, type=bool))
            strength = float(s.value("noise/lms_strength", 0.5,
                                      type=float))
        except Exception:
            return
        try:
            self.set_lms_strength(strength)
            self.set_lms_enabled(on)
        except Exception as exc:
            print(f"[Radio] could not autoload LMS settings: {exc}")

    def autoload_staleness_settings(self) -> None:
        """Restore captured-profile staleness settings from QSettings.

        Two operator preferences:
          * ``noise/staleness_check_enabled`` — default ON
          * ``noise/staleness_threshold_db`` — default 10 dB
            (added v0.0.9.5)

        Called once at startup.  Both fall back to defaults silently
        if QSettings can't be reached or the values are corrupt.
        """
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            on = bool(s.value("noise/staleness_check_enabled",
                              True, type=bool))
            threshold_db = float(s.value(
                "noise/staleness_threshold_db", 10.0, type=float))
        except Exception:
            on = True
            threshold_db = 10.0
        try:
            # Channel-level direct call so we don't redundantly
            # re-persist the value on autoload.
            self._rx_channel.set_nr_staleness_check_enabled(on)
            self._rx_channel.set_nr_staleness_threshold_db(threshold_db)
        except Exception as exc:
            print(f"[Radio] could not autoload staleness setting: {exc}")

    # ── All-mode squelch API ───────────────────────────────────────────

    @property
    def squelch_enabled(self) -> bool:
        return self._rx_channel.squelch_enabled

    @property
    def squelch_threshold(self) -> float:
        return self._rx_channel.squelch_threshold

    @property
    def squelch_passing(self) -> bool:
        """True when audio is currently passing the squelch — UI
        binds this for the activity-indicator dot."""
        return self._rx_channel.squelch_passing

    def set_squelch_enabled(self, on: bool) -> None:
        """Master toggle for the all-mode voice-presence squelch.

        Routes to the right WDSP squelch module based on current mode:
          * FM        → SetRXAFMSQRun
          * AM / SAM / DSB → SetRXAAMSQRun
          * SSB / CW / DIG / SPEC → SetRXASSQLRun (the all-mode
            voice-activity detector Thetis uses for SSB SQ)
        Only one is active at a time.  Mode changes re-route through
        ``_push_wdsp_squelch_state`` from set_mode.
        """
        on = bool(on)
        self._rx_channel.set_squelch_enabled(on)
        self._push_wdsp_squelch_state()
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("audio/squelch_enabled", on)
        except Exception as exc:
            print(f"[Radio] could not persist squelch_enabled: {exc}")
        self.squelch_enabled_changed.emit(on)

    # ── SSQL slider → threshold mapping ──
    # WDSP's WU2O-tested-good default for SSQL threshold is 0.16.
    # Direct 1:1 mapping (slider value passed straight to SSQL) put
    # the operator's typical slider position (~0.20-0.30) above
    # that default, producing "slightly tight" behavior in field
    # test 2026-05-07.  Scale factor 0.65 lands the operator's
    # typical zone in WU2O-friendly territory:
    #   slider 0.16 → SSQL 0.104  (just below WU2O default — loose)
    #   slider 0.20 → SSQL 0.130  (≈ WU2O default — comfortable)
    #   slider 0.30 → SSQL 0.195  (slightly above WU2O default)
    #   slider 0.50 → SSQL 0.325  (firm)
    #   slider 1.00 → SSQL 0.650  (very tight, not pathological)
    _SSQL_SCALE: float = 0.65

    # ── SSQL trigger-voltage time constants ──
    # SSQL's window detector (`wdaverage`) tracks an EWMA of the
    # F-to-V signal with a fixed wdtau=0.5s.  On a quasi-stationary
    # signal (continuous SSB conversation, digital modes) the
    # average converges to the signal level within 1-2 sec → gate
    # marks "no signal" → trigger voltage rises toward mute.
    # That convergence is hardcoded in the DLL; we can't change it
    # without rebuilding WDSP, but we CAN slow the trigger-voltage
    # rise via tau_mute, which is the actual time constant that
    # turns the false "no signal" reading into a closed gate.
    #
    # WDSP create_ssql default: tau_mute=0.1 s, tau_unmute=0.1 s.
    # Operator field test 2026-05-07: with these defaults, the gate
    # "starts okay then pulls back and clamps after a bit" — the
    # ~134 ms trigger rise time means a transient window-detector
    # convergence translates almost-instantly into a clamp.
    #
    # Lyra's SSQL_TAU_MUTE = 0.7 s lets the trigger voltage take
    # ~0.94 s to reach mute threshold — long enough that brief
    # window-detector convergences don't clamp the gate, while
    # genuine end-of-transmission still mutes within ~1 s of
    # speech ending (snappier than 1.0 s while still bridging
    # convergence transients).  Operator-tuned 2026-05-07 from
    # initial 1.0 s default.
    # tau_unmute stays at 0.1 s for snappy speech-onset response.
    _SSQL_TAU_MUTE: float = 0.7     # seconds (vs WDSP default 0.1)
    _SSQL_TAU_UNMUTE: float = 0.1   # seconds (matches WDSP default)

    def set_squelch_threshold(self, value: float) -> None:
        """Squelch threshold, 0.0..1.0.  Higher = more aggressive
        muting.  Persists to QSettings and emits change signal."""
        v = max(0.0, min(1.0, float(value)))
        self._rx_channel.set_squelch_threshold(v)
        # Push to whichever WDSP squelch module is active for the
        # current mode.  AM threshold is dB-scaled; SSQL is scaled
        # via _SSQL_SCALE for operator-friendly slider feel.
        if self._wdsp_rx is not None:
            try:
                if self._mode in ("AM", "SAM", "DSB"):
                    # AM threshold range is roughly -160..0 dB.
                    # Map slider 0..1 → -160..-50 dB.  Higher slider
                    # = less negative threshold = harder to open.
                    am_db = -160.0 + 110.0 * v
                    self._wdsp_rx.set_am_squelch(
                        bool(self._rx_channel.squelch_enabled), am_db)
                elif self._mode == "FM":
                    # FM SQ has no per-threshold setter we've wired;
                    # WDSP's FM squelch decides on RF SNR with
                    # internal thresholds.  Slider unused for FM.
                    pass
                else:
                    # SSB / CW / DIG / SPEC → SSQL via scaling.
                    self._wdsp_rx.set_ssql_threshold(v * self._SSQL_SCALE)
            except Exception as exc:
                print(f"[Radio] WDSP rx squelch threshold error: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("audio/squelch_threshold", v)
        except Exception as exc:
            print(f"[Radio] could not persist squelch_threshold: {exc}")
        self.squelch_threshold_changed.emit(v)

    def _push_wdsp_squelch_state(self) -> None:
        """Re-route WDSP squelch modules to match the active mode.

        Called from set_squelch_enabled and from set_mode (so that
        switching modes hands off cleanly between FM ↔ AM ↔ SSQL).
        Disables the inactive modules; enables the right one for the
        current mode if the operator's master toggle is on.
        """
        if self._wdsp_rx is None:
            return
        on = bool(self._rx_channel.squelch_enabled)
        try:
            mode = self._mode
            # Master-off path FIRST — disable ALL three squelch
            # modules unconditionally so toggling SQ off cleanly
            # restores audio regardless of which module was active.
            #
            # Bug (v0.0.9.6 pre-Phase-6.A4 fix-up): the original
            # implementation interleaved mode-targeted disables
            # with the master-off check, which left the active
            # mode's module running when SQ went off — operator
            # in AM mode toggling SQ off would still hear audio
            # gated because set_am_squelch(False) was skipped by
            # the mode-mismatch guard.  Same bug for FM mode.
            if not on:
                self._wdsp_rx.set_ssql_run(False)
                self._wdsp_rx.set_fm_squelch(False)
                self._wdsp_rx.set_am_squelch(False)
                return
            # Master is ON — disable any module that doesn't apply
            # to this mode (cleans up after a mode change), then
            # enable + threshold the right one.
            if mode != "FM":
                self._wdsp_rx.set_fm_squelch(False)
            if mode not in ("AM", "SAM", "DSB"):
                self._wdsp_rx.set_am_squelch(False)
            if mode in ("FM", "AM", "SAM", "DSB"):
                self._wdsp_rx.set_ssql_run(False)
            if mode == "FM":
                # FM SQ has its own threshold field independent of
                # AM SQ / SSQL.  Wired in Phase 6.A4 — previously
                # FM mode just got the master run flag and sat at
                # the WDSP create-time default (tail_thresh=0.750).
                # Mapping mirrors the reference 10^(-2*v) curve so
                # operators get fine control on the tight end:
                #   v=0   → 1.000 (loosest, squelch effectively off)
                #   v=0.5 → 0.100
                #   v=1   → 0.010 (tightest)
                v = float(self._rx_channel.squelch_threshold)
                fm_threshold = 10.0 ** (-2.0 * v)
                self._wdsp_rx.set_fm_squelch_threshold(fm_threshold)
                self._wdsp_rx.set_fm_squelch(True)
            elif mode in ("AM", "SAM", "DSB"):
                v = float(self._rx_channel.squelch_threshold)
                am_db = -160.0 + 110.0 * v
                self._wdsp_rx.set_am_squelch(True, am_db)
            else:
                # SSB / CW / DIG / SPEC — SSQL handles all-mode squelch.
                # Scale matches set_squelch_threshold (see _SSQL_SCALE
                # docstring for the operator-friendly mapping rationale).
                v = float(self._rx_channel.squelch_threshold)
                self._wdsp_rx.set_ssql_threshold(v * self._SSQL_SCALE)
                # Override WDSP's create_ssql tau defaults — see
                # _SSQL_TAU_MUTE / _SSQL_TAU_UNMUTE docstrings for the
                # "starts okay then clamps" field-test rationale.
                # Push these BEFORE enabling so the new run takes
                # effect with the right time constants from the start.
                self._wdsp_rx.set_ssql_tau_mute(self._SSQL_TAU_MUTE)
                self._wdsp_rx.set_ssql_tau_unmute(self._SSQL_TAU_UNMUTE)
                self._wdsp_rx.set_ssql_run(True)
        except Exception as exc:
            print(f"[Radio] WDSP squelch re-route error: {exc}")

    def autoload_squelch_settings(self) -> None:
        """Restore squelch toggle + threshold from QSettings."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            on = bool(s.value("audio/squelch_enabled", False,
                              type=bool))
            threshold = float(s.value("audio/squelch_threshold",
                                       0.16, type=float))
        except Exception:
            return
        try:
            self.set_squelch_threshold(threshold)
            self.set_squelch_enabled(on)
        except Exception as exc:
            print(f"[Radio] could not autoload squelch settings: {exc}")

    # ── NR1 strength API (continuous slider) ───────────────────────

    @property
    def nr1_strength(self) -> float:
        """Current NR1 strength (0.0..1.0).  Replaced the discrete
        light/medium/heavy profile picker as of 2026-05-01."""
        return self._rx_channel.nr1_strength

    def set_nr1_strength(self, value: float) -> None:
        """Set NR1's continuous suppression strength.

        0.0 = barely-on (subtle, generous spectral floor)
        1.0 = aggressive deep subtraction
        0.5 = balanced (≈ the old "Medium" preset)

        Clamped to [0.0, 1.0] inside SpectralSubtractionNR.
        Persists to QSettings.  Mirrors set_nr2_aggression's
        contract for UX consistency.
        """
        v = max(0.0, min(1.0, float(value)))
        self._rx_channel.set_nr1_strength(v)
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/nr1_strength",
                       float(self._rx_channel.nr1_strength))
        except Exception as exc:
            print(f"[Radio] could not persist nr1 strength: {exc}")
        self.nr1_strength_changed.emit(self._rx_channel.nr1_strength)

    def autoload_nr_mode_settings(self) -> None:
        """Restore NR mode (1..4) and AEPF toggle from QSettings.

        Operators upgrading from the legacy NR1/NR2 backend dropdown
        get migrated automatically:
          * saved ``nr/profile = nr2`` → mode 1 (Wiener+SPP)
          * saved ``nr/profile = nr1`` (or anything else) → mode 3
            (MMSE-LSA — current default behavior)
          * AEPF defaults to ON for all migrations
        """
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            # Load mode — direct or via legacy migration.
            if s.contains("noise/nr_mode"):
                mode = int(s.value("noise/nr_mode", 3, type=int))
            else:
                legacy = ""
                for legacy_key in ("nr/profile", "noise/nr_profile"):
                    val = str(s.value(legacy_key, "") or "").strip().lower()
                    if val:
                        legacy = val
                        break
                if legacy == "nr2":
                    mode = 1
                else:
                    mode = self.NR_MODE_DEFAULT
            mode = max(self.NR_MODE_MIN,
                       min(self.NR_MODE_MAX, int(mode)))
            self._nr_mode = mode
            # Load AEPF toggle — default True if no saved value.
            aepf = bool(s.value("noise/aepf_enabled", True, type=bool))
            self._aepf_enabled = aepf
            # Load NPE method — default OSMS (0) if no saved value.
            npe = int(s.value("noise/npe_method", self.NPE_OSMS, type=int))
            if npe not in (self.NPE_OSMS, self.NPE_MCRA):
                npe = self.NPE_OSMS
            self._npe_method = npe
            # Push to WDSP if engine is up.
            if self._wdsp_rx is not None:
                self._push_wdsp_nr_state()
            # Inform the UI of the current values so it paints right.
            self.nr_mode_changed.emit(self._nr_mode)
            self.aepf_enabled_changed.emit(self._aepf_enabled)
            self.npe_method_changed.emit(self._npe_method)
        except Exception as exc:
            print(f"[Radio] could not autoload NR mode/AEPF: {exc}")

    def autoload_nr1_settings(self) -> None:
        """Restore NR1's strength from QSettings.

        Migration path: if no ``noise/nr1_strength`` is saved but
        the historical ``nr/profile`` key holds a legacy discrete
        name (light/medium/heavy/aggressive), we map the name to
        the equivalent strength so operators upgrading from the
        old UI don't lose their preference.  The legacy
        nr/profile entry is left untouched — set_nr_profile()
        handles its own canonicalization on next operator action.

        Note on key prefixes: ``nr/profile`` is the historical key
        used by app.py's _load_settings() since v0.0.x.  My recent
        audit cleanup work uses ``noise/`` for new keys; we check
        both for forward-compat with anyone who shipped a build
        where the prefix differed.
        """
        try:
            from PySide6.QtCore import QSettings
            from lyra.dsp.nr import SpectralSubtractionNR
            s = QSettings("N8SDR", "Lyra")
            if s.contains("noise/nr1_strength"):
                strength = float(s.value("noise/nr1_strength",
                                         0.5, type=float))
            else:
                # Legacy migration — check both possible legacy keys.
                legacy = ""
                for legacy_key in ("nr/profile", "noise/nr_profile"):
                    val = str(s.value(legacy_key, "") or "").strip().lower()
                    if val:
                        legacy = val
                        break
                strength = SpectralSubtractionNR.\
                    _LEGACY_PROFILE_TO_STRENGTH.get(legacy, 0.5)
        except Exception:
            return
        try:
            self.set_nr1_strength(strength)
        except Exception as exc:
            print(f"[Radio] could not autoload NR1 strength: {exc}")

    def _on_nr_capture_done(self) -> None:
        """Called from inside NR.process() when a capture finalizes.

        Runs on whatever thread the audio chain is on (worker thread
        in worker mode, Qt main otherwise).  We just emit the Qt
        signal — Qt's queued connection delivers the slot on the
        main thread regardless of where we emit from.  Slots
        (typically the UI's "save profile" prompt) handle the rest.
        """
        # v0.0.9.5: smart-guard removed; emit empty string for
        # slot-signature compatibility with code that still expects
        # the (str) shape.
        self.noise_capture_done.emit("")

    def _on_nr_profile_stale(self, drift_db: float) -> None:
        """Called from inside NR.process() when staleness threshold
        is crossed.  Same threading discipline as _on_nr_capture_done
        — runs on the audio thread, just emits a Qt signal that lands
        on the UI thread via queued connection."""
        self.noise_profile_stale.emit(float(drift_db))

    def set_nr_staleness_check_enabled(self, on: bool) -> None:
        """Master toggle for the captured-profile staleness check
        (Settings -> Noise -> "Profile staleness notifications").
        Default ON.  Persists to QSettings so the choice carries
        across launches."""
        try:
            self._rx_channel.set_nr_staleness_check_enabled(bool(on))
        except Exception as exc:
            print(f"[Radio] could not set staleness check: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/staleness_check_enabled", bool(on))
        except Exception:
            pass

    def nr_staleness_drift_db(self) -> float:
        """Live-read the smoothed drift (dB) between the loaded
        captured profile and current band noise.  0.0 if no profile
        is loaded.  Useful for diagnostic readouts in the profile
        manager dialog."""
        try:
            return float(self._rx_channel.nr_staleness_drift_db())
        except Exception:
            return 0.0

    def set_nr_staleness_threshold_db(self, threshold_db: float) -> None:
        """Operator-tunable staleness fire threshold (dB).

        Default 10 dB; range [3.0, 25.0].  Rearm threshold tracks at
        70% of fire (historical 7-of-10 ratio) so operators only
        think about one number.  Persists to QSettings; autoloaded
        on startup via ``autoload_staleness_settings``.

        Added v0.0.9.5 to expose what was previously a hard-coded
        constant.  Operators with very stable noise floors can
        tighten to 5-7 dB; operators with band conditions that drift
        a lot can loosen to 15-20 dB to suppress spurious toasts.
        """
        try:
            self._rx_channel.set_nr_staleness_threshold_db(
                float(threshold_db))
        except Exception as exc:
            print(f"[Radio] could not set staleness threshold: {exc}")
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/staleness_threshold_db",
                       float(threshold_db))
        except Exception:
            pass

    def _save_active_profile_name_setting(self, name: str) -> None:
        """Persist the active-profile name to QSettings so the next
        Lyra start can auto-restore it.  Centralised here so all
        write sites (load, save, delete, clear, rename) hit the
        same key."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("noise/active_profile_name", str(name or ""))
        except Exception as exc:
            # Non-fatal — operator just won't see auto-restore.
            print(f"[Radio] could not persist active-profile name: {exc}")

    def autoload_active_noise_profile(self) -> None:
        """Try to load the profile recorded in QSettings as the
        last-active one + restore the source toggle the operator
        had set when Lyra last closed.  Called by the UI after
        Radio is fully constructed (see lyra/ui/app.py startup
        path).  Silently no-ops if there's no recorded name, the
        file is gone, or the profile is incompatible — operator
        can re-load manually from the manager dialog."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            name = str(s.value("noise/active_profile_name", "", type=str)
                       or "")
            use_captured = bool(s.value(
                "noise/use_captured_profile", False, type=bool))
        except Exception:
            return
        if not name:
            # No saved profile name — nothing to load.  Still honor
            # the source toggle preference in case the operator had
            # it on with a profile that's since been deleted: if
            # that's the case, the toggle is harmless (NR's process()
            # falls back to live tracking when no profile is loaded).
            if use_captured:
                self.set_nr_use_captured_profile(True)
            return
        try:
            self.load_saved_noise_profile(name)
            print(f"[Radio] auto-loaded captured profile: {name!r}")
            # Restore the source toggle — operator may have left
            # the profile loaded but switched source to Live before
            # closing.
            self.set_nr_use_captured_profile(use_captured)
        except (OSError, ValueError, RuntimeError,
                NotImplementedError) as exc:
            # OSError covers FileNotFoundError + PermissionError +
            # other Windows ACL / antivirus / network-share read
            # failures that can hit on startup with a stale
            # QSettings active-profile pointer.  Without this,
            # an unreadable JSON would crash __init__ and the
            # operator couldn't reach the manager dialog to
            # clear it.
            #
            # ValueError covers schema-version refusal (v1 hint),
            # rate / FFT-size mismatches from
            # load_saved_noise_profile's strict checks, and
            # json.JSONDecodeError (which is itself a ValueError
            # subclass).
            #
            # RuntimeError covers the §14.6 Phase 3 guard on
            # load_saved_noise_profile when the IQ engine failed
            # init (e.g., DLL set missing, exception caught at
            # _open_wdsp_rx and _iq_capture left as None).
            # Without this catch, autoload would propagate the
            # RuntimeError up out of __init__ and crash startup.
            #
            # NotImplementedError is the legacy §14.6 Phase 1
            # guard, now dead code (Phase 3 replaced the stub
            # bodies with real implementations).  Kept as
            # belt-and-suspenders since adding to the catch
            # tuple is free.
            print(f"[Radio] could not auto-load captured profile "
                  f"{name!r}: {exc}")
            # Don't clear the persisted name — once Phases 3-4 land
            # the operator's last-used profile should auto-restore.
            # If the file is genuinely missing, FileNotFoundError
            # will fire and we leave the stale name on disk; the
            # manager dialog surfaces "missing profile" cleanly
            # enough that an auto-clear here would be premature.
            # (When the captured-profile feature ships fully in
            # v0.0.9.9, revisit whether to restore the old auto-
            # clear behavior — for the rebuild window we err on
            # the side of preserving operator intent.)

    # ── APF (Audio Peaking Filter) ─────────────────────────────────
    @property
    def apf_enabled(self) -> bool:
        return self._apf_enabled

    @property
    def apf_bw_hz(self) -> int:
        return self._apf_bw_hz

    @property
    def apf_gain_db(self) -> float:
        return self._apf_gain_db

    def set_apf_enabled(self, on: bool) -> None:
        on = bool(on)
        if on == self._apf_enabled:
            return
        self._apf_enabled = on
        self._rx_channel.set_apf_enabled(on)
        # WDSP path: APF lives on the RXA chain as the SPEAK biquad
        # stage.  Mode-gate to CW only — outside CWU/CWL the legacy
        # AudioPeakFilter passes audio through; we mirror that by
        # only running WDSP's biquad when in CW mode.  Operator's
        # toggle state is preserved across mode switches via
        # _apf_enabled.
        if self._wdsp_rx is not None:
            try:
                self._push_wdsp_apf_state()
            except Exception as exc:
                print(f"[Radio] WDSP APF toggle: {exc}")
        self.apf_enabled_changed.emit(on)

    def set_apf_bw_hz(self, bw_hz: int) -> None:
        # Clamp here too so external callers (TCI, CAT) can't push
        # a degenerate value past the channel's biquad / WDSP's
        # SPEAK biquad.  Defence-in-depth.
        bw = max(self.APF_BW_MIN_HZ,
                 min(self.APF_BW_MAX_HZ, int(bw_hz)))
        if bw == self._apf_bw_hz:
            return
        self._apf_bw_hz = bw
        self._rx_channel.set_apf_bw_hz(bw)
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_apf_bw(float(bw))
            except Exception as exc:
                print(f"[Radio] WDSP APF bw: {exc}")
        self.apf_bw_changed.emit(bw)

    def set_apf_gain_db(self, gain_db: float) -> None:
        g = max(self.APF_GAIN_MIN_DB,
                min(self.APF_GAIN_MAX_DB, float(gain_db)))
        if g == self._apf_gain_db:
            return
        self._apf_gain_db = g
        self._rx_channel.set_apf_gain_db(g)
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_apf_gain_db(float(g))
            except Exception as exc:
                print(f"[Radio] WDSP APF gain: {exc}")
        self.apf_gain_changed.emit(g)

    def _push_wdsp_apf_state(self) -> None:
        """Push APF run flag + center freq to WDSP, mode-gated to CW.

        Outside CWU/CWL the APF is forced off regardless of operator
        toggle state — same behavior as legacy AudioPeakFilter, which
        passes audio through in non-CW modes.  Re-entering CW restores
        the operator's prior on/off state automatically because
        _apf_enabled is the source of truth.
        """
        if self._wdsp_rx is None:
            return
        # Init-order guard: _open_wdsp_rx runs early in __init__,
        # before _apf_enabled / _apf_bw_hz / _apf_gain_db are set.
        # Use getattr defaults so the initial push is a no-op rather
        # than an AttributeError (which gets caught by the try/except
        # in _open_wdsp_rx but emits a noisy warning).
        apf_enabled = bool(getattr(self, "_apf_enabled", False))
        apf_bw_hz = float(getattr(self, "_apf_bw_hz", 100.0))
        apf_gain_db = float(getattr(self, "_apf_gain_db", 12.0))
        # Only run in CW.
        active = apf_enabled and self._mode in ("CWU", "CWL")
        # Center frequency = current CW pitch (same as legacy
        # AudioPeakFilter).  WDSP's BiQuad takes the freq in absolute
        # audio-domain Hz — at 48 kHz audio rate, the operator's CW
        # pitch directly drives the peak.
        if active:
            try:
                self._wdsp_rx.set_apf_freq(float(self._cw_pitch_hz))
                self._wdsp_rx.set_apf_bw(apf_bw_hz)
                self._wdsp_rx.set_apf_gain_db(apf_gain_db)
            except Exception as exc:
                print(f"[Radio] WDSP APF param push: {exc}")
        self._wdsp_rx.set_apf(active)

    # ── BIN (Binaural pseudo-stereo) ───────────────────────────────
    @property
    def bin_enabled(self) -> bool:
        return self._bin_enabled

    @property
    def bin_depth(self) -> float:
        return self._bin_depth

    def set_bin_enabled(self, on: bool) -> None:
        on = bool(on)
        if on == self._bin_enabled:
            return
        self._bin_enabled = on
        self._binaural.set_enabled(on)
        self.bin_enabled_changed.emit(on)

    def set_bin_depth(self, depth: float) -> None:
        from lyra.dsp.binaural import BinauralFilter as _BIN
        d = max(_BIN.DEPTH_MIN, min(_BIN.DEPTH_MAX, float(depth)))
        if d == self._bin_depth:
            return
        self._bin_depth = d
        self._binaural.set_depth(d)
        self.bin_depth_changed.emit(d)

    # ── DSP threading mode (Phase 3.B+, restart-required) ──────────
    @property
    def dsp_threading_mode(self) -> str:
        """Operator's currently selected DSP threading mode.

        This is the persisted preference — what will be RUNNING
        after the next Lyra restart. To check what's currently
        running this session, use ``dsp_threading_mode_at_startup``."""
        return self._dsp_threading_mode

    @property
    def dsp_threading_mode_at_startup(self) -> str:
        """The DSP threading mode that was active when this Radio
        instance was constructed — i.e., what's RUNNING right now.
        Compare to ``dsp_threading_mode`` to detect when the operator
        has changed the preference but not yet restarted."""
        return self._dsp_threading_mode_at_startup

    def set_dsp_threading_mode(self, mode: str) -> None:
        """Set the DSP threading mode preference. Persisted via
        QSettings; takes effect on next Lyra restart.

        Accepts 'single' or 'worker'. Unknown values are clamped
        to 'single' (safe default)."""
        m = str(mode or "").strip().lower()
        if m not in self.DSP_THREADING_MODES:
            m = self.DSP_THREADING_SINGLE
        if m == self._dsp_threading_mode:
            return
        self._dsp_threading_mode = m
        self.dsp_threading_mode_changed.emit(m)

    def set_muted(self, on: bool, target_rx: Optional[int] = None):
        """Set mute state for ``target_rx`` (default = focused RX).

        Phase 3.D v0.1: per-RX semantics surface as Mute-A / Mute-B
        in the DSP+Audio panel when ``dispatch_state.rx2_enabled``
        is True (consensus plan §6.8).  When RX2 is OFF, RX1's mute
        applies to the summed audio path; RX2's mute is a no-op
        because the RX2 demod loop isn't producing audio anyway.
        """
        on = bool(on)
        rx_id, _ = self._resolve_rx_target(target_rx)
        if rx_id == 2:
            if on == self._muted_rx2:
                return
            self._muted_rx2 = on
            self.muted_changed_rx2.emit(on)
            return
        if on == self._muted:
            return
        self._muted = on
        self.muted_changed.emit(on)

    def toggle_muted(self, target_rx: Optional[int] = None):
        rx_id, _ = self._resolve_rx_target(target_rx)
        current = self._muted if rx_id == 0 else self._muted_rx2
        self.set_muted(not current, target_rx=rx_id)

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

    def set_rx_bw(self, mode: str, bw: int, target_rx: Optional[int] = None):
        """Set the per-mode RX bandwidth for ``target_rx`` (default
        = focused RX).

        Phase 3.C v0.1: ``target_rx`` semantics replace Phase 2's
        fan-out.  RX1 gets the BW saved to ``_rx_bw_by_mode`` and
        the existing TX-BW-lock side effect.  RX2 gets BW saved to
        ``_rx_bw_by_mode_rx2``; no TX BW interaction since TX is
        v0.2 work and currently only mirrors RX1's BW.
        """
        rx_id, _suffix = self._resolve_rx_target(target_rx)
        bw_int = int(bw)

        if rx_id == 2:
            self._rx_bw_by_mode_rx2[mode] = bw_int
            # Push filter to RX2's WDSP channel only when this is
            # RX2's active mode.
            if self._wdsp_rx2 is not None and mode == self._mode_rx2:
                try:
                    low, high = self._wdsp_filter_for(mode, target_rx=2)
                    self._wdsp_rx2.set_filter(low, high)
                except Exception as exc:
                    print(f"[Radio] WDSP rx2 bw-change error: {exc}")
            # Phase 3.E.1 hotfix v0.14 (2026-05-12): when the
            # panadapter is sourced from RX2 and the BW just
            # changed for RX2's active mode, re-emit the passband
            # so the cyan rectangle resizes/repositions on screen.
            if (mode == self._mode_rx2
                    and self._panadapter_source_rx == 2):
                self._emit_passband()
            self.rx_bw_changed_rx2.emit(mode, bw_int)
            return

        # RX1 path.
        self._rx_bw_by_mode[mode] = bw_int
        # Always push to channel so the per-mode BW state stays in sync
        # — the demod for `mode` rebuilds inside the channel.
        self._rx_channel.set_rx_bw(mode, bw_int)
        # WDSP RX engine — only push filter when this matches the
        # currently active mode; per-mode BW is stored above and applied
        # on the next ``set_mode`` if the operator changes mode.
        if self._wdsp_rx is not None and mode == self._mode:
            try:
                low, high = self._wdsp_filter_for(mode)
                self._wdsp_rx.set_filter(low, high)
            except Exception as exc:
                print(f"[Radio] WDSP rx bw-change error: {exc}")
        if mode == self._mode:
            self._emit_passband()
        self.rx_bw_changed.emit(mode, bw_int)
        if self._bw_locked and self._tx_bw_by_mode.get(mode) != bw_int:
            self._tx_bw_by_mode[mode] = bw_int
            self.tx_bw_changed.emit(mode, bw_int)

    def set_tx_bw(self, mode: str, bw: int):
        """Set the TX BW for a mode.  When BW lock is on, also
        updates RX1's BW for the same mode (back-compat with the
        v0.0.9.x BW-lock UX).  RX2's BW is NOT pulled along by
        TX BW lock per Phase 3.C -- it's a per-RX independent
        setting.  Operator wanting RX2 BW = TX BW should set it
        explicitly via the focused-RX path.
        """
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
        # WDSP path: master notch run flag.  When disabled, the WDSP
        # NotchDB is bypassed even if individual notches have
        # active=1 — saves CPU and lets the operator A/B the notch
        # bank in one click.
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_notches_master_run(self._notch_enabled)
            except Exception as exc:
                print(f"[Radio] WDSP notch master toggle: {exc}")
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

    def _save_current_band_memory(self, target_rx: int | None = None):
        """Save current freq+mode+gain into the band memory keyed on
        whichever band the resolved RX is tuned to.

        ``target_rx`` (Phase 3.E.1 hotfix v0.4 2026-05-12): which RX
        the band-band memory write follows.  ``None`` defaults to the
        focused RX, so a band-button click while VFO B is highlighted
        saves RX2's freq+mode into the shared band memory (instead
        of clobbering it with RX1's unrelated freq).  LNA gain stays
        shared regardless of target RX -- the HL2 has one ADC.
        """
        rx, _ = self._resolve_rx_target(target_rx)
        if rx == 2:
            freq_hz = int(self._rx2_freq_hz)
            mode = self._mode_rx2
        else:
            freq_hz = int(self._freq_hz)
            mode = self._mode
        band = band_for_freq(freq_hz)
        if band is None:
            return
        # Preserve any existing band-specific range bounds; we only
        # update the freq/mode/gain on every save (those change with
        # ordinary tuning). Range bounds change only when the
        # operator explicitly sets them, so we read-modify-write to
        # avoid clobbering on every freq tweak.
        existing = self._band_memory.get(band.name, {})
        existing.update({
            "freq_hz": freq_hz,
            "mode":    mode,
            "gain_db": self._gain_db,
        })
        self._band_memory[band.name] = existing

    def _save_current_band_range(self):
        """Save the operator's current spectrum + waterfall ranges as
        the bounds for whichever band we're currently tuned to.
        Called whenever set_spectrum_db_range or set_waterfall_db_range
        fires with from_user=True.

        Persists:
            range_min_db / range_max_db       — spectrum bounds
            floor_locked / ceiling_locked     — spectrum per-edge locks
            waterfall_min_db / max_db         — waterfall manual range
                (per-band 2026-05-09 — was global before the fix)

        v0.1.0-pre3 guard (2026-05-13, operator report):  when the
        panadapter is showing RX2 (``_panadapter_source_rx == 2``),
        ``_freq_hz`` still refers to RX1's frequency, so an
        unguarded save would write the RX2 drag values under RX1's
        band-memory entry — corrupting whatever the operator had
        set up for RX1 on that band.  Since Lyra always restarts
        in RX1-focused mode (intentional UX choice), saving only
        when source == RX1 (0) keeps the persisted values correct
        for the next startup.  In-session, RX2 drags still update
        the live display (set_*_db_range applies the visual change
        BEFORE calling here); they're just session-scoped.
        """
        if self._panadapter_source_rx != 0:
            return
        band = band_for_freq(self._freq_hz)
        if band is None:
            return
        existing = self._band_memory.get(band.name, {})
        existing["range_min_db"] = float(self._user_range_min_db)
        existing["range_max_db"] = float(self._user_range_max_db)
        existing["floor_locked"]   = bool(self._user_floor_locked)
        existing["ceiling_locked"] = bool(self._user_ceiling_locked)
        existing["waterfall_min_db"] = float(self._waterfall_min_db)
        existing["waterfall_max_db"] = float(self._waterfall_max_db)
        self._band_memory[band.name] = existing

    def _apply_band_range(self, band_name: str):
        """Pull the saved range bounds for `band_name` (or the factory
        default for that band group) and apply them as the auto-scale
        bounds. Called from recall_band on band change so auto-scale
        re-fits within the new band's appropriate window.

        Restoration goes through from_user=False so the per-edge
        edge-lock auto-detection in set_spectrum_db_range doesn't
        re-trigger on the band-switch itself (that would falsely
        relock edges the operator had never touched).  We set the
        user-range fields + lock flags manually here.
        """
        memory = self._band_memory.get(band_name, {})
        from_memory = (
            "range_min_db" in memory and "range_max_db" in memory)
        if from_memory:
            lo, hi = memory["range_min_db"], memory["range_max_db"]
            floor_lock = bool(memory.get("floor_locked", False))
            ceil_lock  = bool(memory.get("ceiling_locked", False))
        elif band_name in self._DEFAULT_BAND_RANGE_DB:
            lo, hi = self._DEFAULT_BAND_RANGE_DB[band_name]
            # Factory-default branch — first visit to this band.
            # Locks default to False so auto fully governs.
            floor_lock = False
            ceil_lock  = False
        else:
            # Unknown band (broadcast-only / GEN sub-segment) — leave
            # bounds at whatever they currently are, no change.
            return
        self._user_range_min_db = float(lo)
        self._user_range_max_db = float(hi)
        self._user_floor_locked   = floor_lock
        self._user_ceiling_locked = ceil_lock
        # Push display range through from_user=False so we DON'T
        # re-trigger edge-detection / band-memory save (we're already
        # restoring from memory; saving again would be a no-op at
        # best, lock-misdetection at worst).
        self.set_spectrum_db_range(lo, hi, from_user=False)
        # Restore waterfall manual range too if this band has one
        # saved (per-band waterfall persistence added 2026-05-09).
        # When the band has never been visited or no waterfall range
        # is in memory, the operator's last global waterfall manual
        # values are kept — same effect as pre-patch behavior for
        # un-customized bands.
        if (memory.get("waterfall_min_db") is not None
                and memory.get("waterfall_max_db") is not None):
            wf_lo = float(memory["waterfall_min_db"])
            wf_hi = float(memory["waterfall_max_db"])
            # Tiny-span guard — same logic that protects the
            # spectrum range autoload (visuals/waterfall_db_range
            # fall-back in app.py).  A pinched < 30 dB span would
            # produce a near-monochrome waterfall.
            if wf_hi - wf_lo >= 30.0:
                self.set_waterfall_db_range(
                    wf_lo, wf_hi, from_user=False)

    def tune_preset(self, freq_hz: int, mode: str,
                    target_rx: int | None = None,
                    rx_bw_hz: int | None = None) -> None:
        """Atomic preset tune for band-panel buttons (GEN, TIME, Mem).

        Phase 3.E.1 hotfix v0.5 (2026-05-12): centralizes the
        ``set_mode + set_freq_hz`` pattern used across
        BandSelectorPanel handlers so they all follow the focused
        VFO without each caller having to know about the per-RX
        setter split (``set_freq_hz`` vs ``set_rx2_freq_hz``).

        Order: mode FIRST so the demod is right when the freq
        lands -- mirrors the existing TIME-button + memory-recall
        ordering.  Then freq.  Then optional per-mode RX BW pin
        (used by Mem entries that lock a custom passband width).

        ``target_rx``: 0 (RX1), 2 (RX2), or None (focused).
        """
        rx, _ = self._resolve_rx_target(target_rx)
        self.set_mode(mode, target_rx=rx)
        if rx == 2:
            self.set_rx2_freq_hz(int(freq_hz))
        else:
            self.set_freq_hz(int(freq_hz))
        if rx_bw_hz is not None:
            try:
                self.set_rx_bw(mode, int(rx_bw_hz), target_rx=rx)
            except Exception:
                # Rare: mode might not have a settable BW path.
                # Best-effort -- the freq + mode tune is the
                # important part.
                pass

    def recall_band(self, band_name: str, defaults_freq: int,
                    defaults_mode: str, target_rx: int | None = None):
        """Restore freq/mode/gain saved for `band_name` if present, else
        tune to the band's defaults. Also applies the band's saved
        spectrum range bounds (or factory defaults for that band group)
        so auto-scale re-fits within an appropriate window for the
        band's typical noise floor + signal levels.

        Suppresses the auto-save during the apply so we don't
        immediately overwrite the memory we just loaded with
        intermediate tuning steps.

        ``target_rx`` (Phase 3.E.1 hotfix v0.4 2026-05-12): which RX
        receives the freq + mode write.  ``None`` defaults to the
        currently-focused RX so the operator's mental model holds:
        "the highlighted VFO is the one my controls drive".  Pass
        ``0`` / ``2`` explicitly to override (e.g. CAT command
        forcing a particular RX).

        LNA gain (``set_gain_db``) and per-band spectrum/waterfall
        range stay shared across both RXes -- the HL2 has a single
        ADC and front-end filter bank, and the spectrum widget is
        single-pane (Phase 4 split-panadapter work may revisit the
        range axis).  Band memory itself is keyed on band_name
        only, so both RXes share the operator's saved per-band
        defaults; per-RX band memory is a Phase 4+ concern.
        """
        rx, _ = self._resolve_rx_target(target_rx)
        memory = self._band_memory.get(band_name)
        self._suppress_band_save = True
        try:
            if memory:
                if rx == 2:
                    self.set_rx2_freq_hz(memory["freq_hz"])
                else:
                    self.set_freq_hz(memory["freq_hz"])
                self.set_mode(memory["mode"], target_rx=rx)
                # LNA gain stays shared (single HL2 ADC front-end).
                self.set_gain_db(memory["gain_db"])
            else:
                if rx == 2:
                    self.set_rx2_freq_hz(defaults_freq)
                else:
                    self.set_freq_hz(defaults_freq)
                self.set_mode(defaults_mode, target_rx=rx)
            # Apply per-band range bounds AFTER freq/mode are set so
            # band_for_freq() returns the right band for the
            # downstream save.
            self._apply_band_range(band_name)
        finally:
            self._suppress_band_save = False
        # Save (now that the dust has settled) so the next reactivation
        # of this band brings back exactly this state.  Band memory is
        # shared across RXes (see docstring); pass ``rx`` through so
        # the save reads RX2's freq+mode when RX2 was the recall
        # target, instead of clobbering with RX1's unrelated state.
        self._save_current_band_memory(target_rx=rx)

    @property
    def band_memory_snapshot(self) -> dict:
        """Snapshot for QSettings persistence."""
        return dict(self._band_memory)

    def apply_current_band_range(self) -> None:
        """Apply the per-band saved spectrum + waterfall ranges for
        whichever band the radio is currently tuned to.

        Public wrapper around ``_apply_band_range`` for use during
        startup autoload — ``recall_band`` (the only other caller of
        ``_apply_band_range``) only runs when the operator clicks a
        band button, NOT at startup, so without this call the
        operator's per-band saved waterfall min/max + spectrum
        bounds would only take effect after a manual band-button
        click.  v0.0.9.8.1 fix: bug surfaced when operator reported
        "Waterfall Min-Max isn't staying where set with manual when
        you restart Lyra" — actual cause was per-band values WERE
        saved and restored to the in-memory ``_band_memory`` dict,
        but never APPLIED to the live radio state on startup.

        ``band_for_freq`` is the same helper ``_save_current_band_range``
        uses (imported at module top from ``lyra.bands``)."""
        band = band_for_freq(int(self._freq_hz))
        if band is None:
            return
        self._apply_band_range(band.name)

    def restore_band_memory(self, snapshot: dict):
        """Restore the per-band memory dict from QSettings.

        Each band entry can carry a mix of fields:
          - freq_hz / mode / gain_db (saved by _save_current_band_memory
            on tuning changes)
          - range_min_db / range_max_db / floor_locked / ceiling_locked
            (saved by _save_current_band_range on dB-scale drag)
          - waterfall_min_db / waterfall_max_db (per-band waterfall
            persistence added 2026-05-09)

        Bug fix 2026-05-09: the previous filter `"freq_hz" in v`
        was too aggressive — it dropped any band entry that had
        ONLY dB-scale-drag data without a freq write.  Operator-
        reported: dB-lock recall didn't work on restart for bands
        the operator had drag-customized but not tuned to during
        the session.  New filter accepts any dict-shaped entry
        with at least one known field, so partial entries
        survive.
        """
        if not isinstance(snapshot, dict):
            return
        valid_keys = {
            "freq_hz", "mode", "gain_db",
            "range_min_db", "range_max_db",
            "floor_locked", "ceiling_locked",
            "waterfall_min_db", "waterfall_max_db",
        }
        self._band_memory = {
            k: dict(v) for k, v in snapshot.items()
            if isinstance(v, dict)
               and any(field in v for field in valid_keys)
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
        changed. With the stream's round-robin C&C cycling, this
        single write becomes part of the rotation (the stream's
        ``_send_cc`` updates ``_cc_registers[0x00]``) and gets
        re-emitted automatically.

        Path C.2 followup (band-change rate-flip fix): c1 is composed
        FRESH from ``self._rate`` here, not read from the cached
        ``self._config_c1``.  Reason: ``_config_c1`` is initialized
        in ``__init__`` from the constructor default rate (48 k) and
        ``set_rate`` did not update it -- so any later band change
        would write the stale 48 k rate code into register 0x00,
        which (under round-robin) drops the radio's IQ rate from
        whatever the operator actually selected back down to 48 k.
        Operator-visible: display throttles to ~23 Hz and audio
        drags after every band change with the filter board
        enabled.  Reading from ``self._rate`` here is defensive --
        we also sync ``_config_c1`` in ``set_rate`` -- but reading
        fresh ensures the bug can't recur if any future code path
        writes a stale ``_config_c1``.
        """
        if self._stream is None:
            return
        try:
            c1 = SAMPLE_RATES[self._rate]
            # Phase 1 v0.1 bug-fix: read C4 from HL2Stream so the
            # nddc / duplex bit field stays in lockstep with the
            # protocol layer's main-loop value.  If the Radio-side
            # ``self._config_c4`` ever falls out of sync with the
            # stream's value (as happened in the initial Phase 1
            # patch), this defer-to-stream pattern catches the
            # drift here instead of writing a stale byte to the
            # wire and degrading the radio's DDC count mid-session.
            c4 = getattr(self._stream, "_config_c4", self._config_c4)
            self._stream._send_cc(0x00, c1, self._config_c2,  # noqa: SLF001
                                  self._config_c3, c4)
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
    # All operator-facing notch operations live here.  Width is the
    # primary parameter (Hz, not Q).  Phase 6.B (v0.0.9.6): per-notch
    # IIR filter coefficient design is gone — WDSP runs the live
    # notch filtering and reads the operator's notch list as
    # (abs_freq_hz, width_hz, active) tuples via
    # ``_push_wdsp_notches``.  This layer just manages the bank.

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
                  deep: bool | None = None,
                  depth_db: float | None = None,
                  cascade: int | None = None):
        """Place a new notch.

        Defaults:
          - ``width_hz``  → ``_notch_default_width_hz`` (40 Hz).
          - ``depth_db``  → ``_notch_default_depth_db`` (-50 dB).
          - ``cascade``   → ``_notch_default_cascade`` (2).

        Backward-compat ``deep`` kwarg: when passed explicitly,
        translates to (cascade=2 if True else 1) and depth_db keeps
        the operator default.  New callers should use
        ``depth_db`` / ``cascade`` directly.

        Auto-enables the notch bank if it's currently off — operator
        placing a notch wants to hear the result.
        """
        w = width_hz if width_hz is not None else self._notch_default_width_hz
        w = max(self.NOTCH_WIDTH_MIN_HZ,
                min(self.NOTCH_WIDTH_MAX_HZ, float(w)))
        # Resolve depth + cascade.  Explicit `deep` arg overrides the
        # cascade default for legacy callers.
        if depth_db is None:
            depth_db = self._notch_default_depth_db
        if cascade is None:
            if deep is not None:
                cascade = 2 if deep else 1
            else:
                cascade = self._notch_default_cascade
        depth_db = max(self.NOTCH_DEPTH_MIN_DB,
                       min(self.NOTCH_DEPTH_MAX_DB, float(depth_db)))
        cascade = max(self.NOTCH_CASCADE_MIN,
                      min(self.NOTCH_CASCADE_MAX, int(cascade)))
        self._notches.append(Notch(
            abs_freq_hz=float(abs_freq_hz), width_hz=w,
            active=bool(active),
            depth_db=depth_db, cascade=cascade,
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
        """Find the notch nearest abs_freq_hz and update it with a
        new width.  Used by mouse-wheel and drag gestures over an
        existing notch.  Returns True if a notch was matched + updated.

        Phase 6.B (v0.0.9.6): the per-notch ``filter.update_coeffs``
        coefficient swap is no longer needed — WDSP picks up the
        new width through ``_push_wdsp_notches`` (called by the
        ``notches_changed`` signal handler).

        Width-change throttle: drag gestures fire many events per
        second.  We skip updates where the width changed by less than
        4% — at the operator's drag cadence, sub-4% changes would
        compound into a continuous coefficient-storm with no
        perceptible benefit."""
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        w = max(self.NOTCH_WIDTH_MIN_HZ,
                min(self.NOTCH_WIDTH_MAX_HZ, float(new_width_hz)))
        if n.width_hz > 0 and abs(w - n.width_hz) / n.width_hz < 0.04:
            return False
        # Replace the dataclass entry with updated width (immutable
        # for clean signal emission).
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=w,
            active=n.active, depth_db=n.depth_db, cascade=n.cascade,
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
            active=bool(active),
            depth_db=n.depth_db, cascade=n.cascade,
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
        """Legacy compat wrapper.  In v0.0.7.1 notch v2, "deep" is a
        derived attribute (cascade > 1).  This setter translates a
        bool toggle to ``cascade=2`` (deep) or ``cascade=1`` (shallow)
        without touching depth_db.  New code should call
        ``set_notch_cascade_at`` directly."""
        return self.set_notch_cascade_at(
            abs_freq_hz, 2 if bool(deep) else 1, tolerance_hz)

    def toggle_notch_deep_at(self, abs_freq_hz: float,
                             tolerance_hz: float | None = None) -> bool:
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        return self.set_notch_deep_at(
            n.abs_freq_hz, not n.deep, tolerance_hz)

    # ── New v0.0.7.1 notch v2 setters (depth_db, cascade, presets) ──

    def set_notch_depth_db_at(self, abs_freq_hz: float, depth_db: float,
                              tolerance_hz: float | None = None) -> bool:
        """Set the notch attenuation depth (dB, negative) on the
        notch nearest ``abs_freq_hz``.  Phase 6.B (v0.0.9.6):
        depth_db is currently advisory only — WDSP collapses
        depth into width approximation in ``_push_wdsp_notches``.
        We persist the operator's value so the UI keeps showing
        their setting and so a future cascade-of-WDSP-notches
        upgrade can read it.

        ``depth_db`` is clamped to [NOTCH_DEPTH_MIN_DB,
        NOTCH_DEPTH_MAX_DB]."""
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        d = max(self.NOTCH_DEPTH_MIN_DB,
                min(self.NOTCH_DEPTH_MAX_DB, float(depth_db)))
        if abs(d - n.depth_db) < 0.5:
            # Sub-half-dB change is sub-perceptible; skip.
            return True
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=n.width_hz,
            active=n.active, depth_db=d, cascade=n.cascade,
        )
        self.notches_changed.emit(self.notch_details)
        return True

    def set_notch_cascade_at(self, abs_freq_hz: float, cascade: int,
                             tolerance_hz: float | None = None) -> bool:
        """Set the notch cascade depth (1-4 stages) on the notch
        nearest ``abs_freq_hz``.  Phase 6.B (v0.0.9.6): currently
        advisory only — WDSP collapses cascade onto a single
        notch.  Persisted so a future cascade-of-WDSP-notches
        upgrade can read the operator's intent."""
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        c = max(self.NOTCH_CASCADE_MIN,
                min(self.NOTCH_CASCADE_MAX, int(cascade)))
        if c == n.cascade:
            return True
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=n.width_hz,
            active=n.active, depth_db=n.depth_db, cascade=c,
        )
        self.notches_changed.emit(self.notch_details)
        return True

    def set_notch_preset_at(self, abs_freq_hz: float, preset: str,
                            tolerance_hz: float | None = None) -> bool:
        """Apply a Normal / Deep / Surgical preset to the notch
        nearest ``abs_freq_hz``.  Updates both ``depth_db`` and
        ``cascade`` in one click-free swap.

        Unknown preset names are silently ignored (return False).
        Right-click menu wires "Normal" / "Deep" / "Surgical"
        entries to this setter."""
        params = self.NOTCH_PRESETS.get(preset)
        if params is None:
            return False
        idx = self._find_nearest_notch_idx(abs_freq_hz, tolerance_hz)
        if idx is None:
            return False
        n = self._notches[idx]
        self._notches[idx] = Notch(
            abs_freq_hz=n.abs_freq_hz, width_hz=n.width_hz,
            active=n.active,
            depth_db=params["depth_db"],
            cascade=params["cascade"],
        )
        self.notches_changed.emit(self.notch_details)
        return True

    def set_notch_default_depth_db(self, depth_db: float) -> None:
        """Default depth applied to newly placed notches."""
        self._notch_default_depth_db = max(
            self.NOTCH_DEPTH_MIN_DB,
            min(self.NOTCH_DEPTH_MAX_DB, float(depth_db)),
        )

    def set_notch_default_cascade(self, cascade: int) -> None:
        """Default cascade applied to newly placed notches."""
        self._notch_default_cascade = max(
            self.NOTCH_CASCADE_MIN,
            min(self.NOTCH_CASCADE_MAX, int(cascade)),
        )
        # Keep the legacy bool default in sync.
        self._notch_default_deep = self._notch_default_cascade > 1

    def set_notch_default_preset(self, preset: str) -> None:
        """Apply Normal / Deep / Surgical to BOTH default depth AND
        default cascade — convenience for "operator wants every new
        notch to be Surgical-style"."""
        params = self.NOTCH_PRESETS.get(preset)
        if params is None:
            return
        self._notch_default_depth_db = float(params["depth_db"])
        self._notch_default_cascade = int(params["cascade"])
        self._notch_default_deep = self._notch_default_cascade > 1

    # ── Notch bank save/load (operator-named presets) ───────────

    def save_notch_bank(self, name: str) -> bool:
        """Persist the current notch bank to QSettings under
        ``notches/banks/<name>``.  Operator-facing feature — they
        save current setups as 'My 40m setup', 'Contest weekend',
        etc., and load them later via ``load_notch_bank``.

        Each notch is serialized as a dict with the fields needed
        to reconstruct it: abs_freq_hz, width_hz, active, depth_db,
        cascade.  The filter object itself is rebuilt on load (its
        coefficients depend on the current VFO freq anyway).

        Empty name strings are rejected (return False).  Existing
        names are silently overwritten -- caller (right-click menu
        UX) confirms with the operator before invoking this.

        Fires ``notch_banks_changed`` so the UI can refresh the
        right-click submenu listing.
        """
        import json
        from PySide6.QtCore import QSettings as _QS
        nm = (name or "").strip()
        if not nm:
            return False
        items = [
            {
                "abs_freq_hz": float(n.abs_freq_hz),
                "width_hz": float(n.width_hz),
                "active": bool(n.active),
                "depth_db": float(n.depth_db),
                "cascade": int(n.cascade),
            }
            for n in self._notches
        ]
        try:
            qs = _QS("N8SDR", "Lyra")
            qs.setValue(f"notches/banks/{nm}", json.dumps(items))
        except Exception as e:
            self.status_message.emit(
                f"Save notch bank failed: {e}", 3000)
            return False
        self.notch_banks_changed.emit()
        self.status_message.emit(
            f"Notch bank '{nm}' saved ({len(items)} notches)", 2000)
        return True

    def load_notch_bank(self, name: str,
                        replace: bool = True) -> bool:
        """Restore a previously-saved notch bank.

        ``replace=True`` (default) clears the current notch bank
        before loading -- operator gets exactly what was saved.
        ``replace=False`` appends, useful for combining banks
        (uncommon).

        Returns True on success, False if the bank doesn't exist or
        the JSON payload is malformed.
        """
        import json
        from PySide6.QtCore import QSettings as _QS
        nm = (name or "").strip()
        if not nm:
            return False
        try:
            qs = _QS("N8SDR", "Lyra")
            raw = qs.value(f"notches/banks/{nm}", None)
        except Exception:
            return False
        if raw is None:
            self.status_message.emit(
                f"Notch bank '{nm}' not found", 3000)
            return False
        try:
            items = json.loads(str(raw))
            if not isinstance(items, list):
                raise ValueError("not a list")
        except Exception as e:
            self.status_message.emit(
                f"Notch bank '{nm}' corrupt: {e}", 3000)
            return False
        if replace:
            self._notches.clear()
        loaded = 0
        for it in items:
            try:
                self.add_notch(
                    abs_freq_hz=float(it["abs_freq_hz"]),
                    width_hz=float(it.get("width_hz",
                                          self._notch_default_width_hz)),
                    active=bool(it.get("active", True)),
                    depth_db=float(it.get("depth_db",
                                          self._notch_default_depth_db)),
                    cascade=int(it.get("cascade",
                                       self._notch_default_cascade)),
                )
                loaded += 1
            except Exception:
                # Skip malformed entries; keep loading the rest.
                continue
        self.notches_changed.emit(self.notch_details)
        self.status_message.emit(
            f"Loaded '{nm}' ({loaded} notches)", 2000)
        return True

    def delete_notch_bank(self, name: str) -> bool:
        """Remove a saved notch bank from QSettings.  Returns True
        if removed, False if it didn't exist."""
        from PySide6.QtCore import QSettings as _QS
        nm = (name or "").strip()
        if not nm:
            return False
        try:
            qs = _QS("N8SDR", "Lyra")
            key = f"notches/banks/{nm}"
            if qs.value(key, None) is None:
                return False
            qs.remove(key)
        except Exception as e:
            self.status_message.emit(
                f"Delete failed: {e}", 3000)
            return False
        self.notch_banks_changed.emit()
        self.status_message.emit(
            f"Notch bank '{nm}' deleted", 2000)
        return True

    def list_notch_banks(self) -> list[str]:
        """Return saved notch-bank names, sorted alphabetically.
        Right-click menu builds its 'Load preset...' submenu from
        this list."""
        from PySide6.QtCore import QSettings as _QS
        try:
            qs = _QS("N8SDR", "Lyra")
            qs.beginGroup("notches/banks")
            keys = qs.childKeys()
            qs.endGroup()
        except Exception:
            return []
        return sorted(str(k) for k in keys)

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
        # Reset whichever filter is about to become active so the
        # meter doesn't briefly show a stale value carried over from
        # the last time that mode was used.
        if m == "avg":
            self._smeter_avg_lin = 0.0
        else:
            self._smeter_peak_hold_lin = 0.0
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
        # Detect which edge actually moved BEFORE we update the live
        # range — used below to set per-edge locks.  Threshold of
        # 0.5 dB skips dust-jitter (mid-drag emits, settings dialog
        # rewriting identical values, etc.).
        old_lo = self._spectrum_min_db
        old_hi = self._spectrum_max_db
        floor_moved = abs(lo - old_lo) > 0.5
        ceiling_moved = abs(hi - old_hi) > 0.5
        self._spectrum_min_db, self._spectrum_max_db = lo, hi
        if from_user:
            # Operator-driven change (drag, slider, settings dialog).
            # Lock whichever edge they actually moved, so the
            # auto-scale tick respects it.  This is what the Settings
            # tooltip has always promised — earlier code lost the
            # behavior when an unrelated pinch-bug fix removed the
            # clamp wholesale (see auto-scale tick for history).
            self._user_range_min_db = lo
            self._user_range_max_db = hi
            if floor_moved:
                self._user_floor_locked = True
            if ceiling_moved:
                self._user_ceiling_locked = True
            self._save_current_band_range()
        self.spectrum_db_range_changed.emit(lo, hi)

    def reset_spectrum_db_locks(self) -> None:
        """Clear both floor + ceiling edge locks so auto-scale fully
        recomputes the range on the next tick.  Called from the
        right-click "Reset display range" item on the dB scale zone.
        Also clears the saved range from the current band's memory
        so a future band switch doesn't drag the old preference back.

        v0.1.0-pre3 guard (2026-05-13):  same RX2-panadapter-source
        check as ``_save_current_band_range``.  Right-clicking
        "Reset display range" on an RX2 panadapter shouldn't clear
        RX1's saved band-memory locks (which are keyed off
        ``_freq_hz``, RX1's frequency).  Live-display clear and
        auto-scale kick still happen regardless of source — the
        operator's "reset what I'm looking at" intent is honored.
        """
        self._user_floor_locked = False
        self._user_ceiling_locked = False
        if self._panadapter_source_rx == 0:
            # Drop the per-band saved bounds so the band defaults take
            # over on next recall.  (Skipped on RX2 panadapter to keep
            # RX1's band memory intact — see docstring.)
            try:
                band = band_for_freq(self._freq_hz)
            except Exception:
                band = None
            if band is not None:
                existing = self._band_memory.get(band.name, {})
                existing.pop("range_min_db", None)
                existing.pop("range_max_db", None)
                existing.pop("floor_locked", None)
                existing.pop("ceiling_locked", None)
                if existing:
                    self._band_memory[band.name] = existing
                else:
                    self._band_memory.pop(band.name, None)
        # Force the next auto-scale tick to fire immediately so the
        # operator gets visible feedback rather than waiting ~2 sec.
        self._auto_scale_tick_counter = self.AUTO_SCALE_INTERVAL_TICKS

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

    def set_waterfall_db_range(self, min_db: float, max_db: float,
                                *, from_user: bool = True):
        """Apply a new waterfall heatmap dB range.

        ``from_user=True`` (default) means an interactive change
        (Settings → Visuals slider drag).  The value is saved to the
        current band's per-band memory so switching bands restores
        whichever waterfall range you last set there — symmetric
        with the spectrum dB range behavior (operator request
        2026-05-09 to match the per-band semantic).

        Internal callers that drive the waterfall from auto-scale
        (the auto-scale tick in `_process_spec_db`) pass
        ``from_user=False`` so a continuous mirror-from-spectrum
        update doesn't pollute the operator's saved per-band
        manual values.
        """
        lo, hi = float(min_db), float(max_db)
        if hi - lo < 3.0:
            hi = lo + 3.0
        self._waterfall_min_db, self._waterfall_max_db = lo, hi
        if from_user:
            self._save_current_band_range()
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

    # ── Panadapter scroll step ────────────────────────────────────────
    # Operator-facing scroll step for mouse-wheel tuning over the
    # panadapter / waterfall.  Independent of the VFO step (which is
    # a fine-tune control).  Defaults to 1 kHz — fast enough for
    # band-skimming, slow enough to land on signals.
    PANADAPTER_SCROLL_STEPS_HZ = (
        100, 500, 1000, 5000, 10_000, 25_000, 100_000)

    @property
    def panadapter_scroll_step_hz(self) -> int:
        """Mouse-wheel-over-panadapter tune step in Hz.  Distinct
        from the VFO step (Tuning panel) — that's for click-zeroing
        on a signal; this is for skimming across a band."""
        return int(getattr(self, "_panadapter_scroll_step_hz", 1000))

    def set_panadapter_scroll_step_hz(self, hz: int) -> None:
        """Set the panadapter mouse-wheel scroll step.  Persists via
        QSettings.  Clamped to valid presets in
        ``PANADAPTER_SCROLL_STEPS_HZ`` if exact; otherwise accepted
        as-is for forward compatibility with future operator-set
        custom values."""
        step = int(hz)
        if step < 1:
            step = 1
        old = int(getattr(self, "_panadapter_scroll_step_hz", 1000))
        if step == old:
            return
        self._panadapter_scroll_step_hz = step
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("display/panadapter_scroll_step_hz", step)
        except Exception as exc:
            print(f"[Radio] persist panadapter scroll step: {exc}")
        self.panadapter_scroll_step_changed.emit(step)

    def panadapter_scroll_tune(self, delta_units: int) -> None:
        """Tune the VFO by ``delta_units * panadapter_scroll_step_hz``.

        Wheel-over-panadapter handler.  Wheel up (positive delta) =
        freq up (matches physical-radio VFO knob convention).  Step
        size comes from ``panadapter_scroll_step_hz``, settable via
        the Display panel combo.

        When the operator has Exact / Round 100 Hz set to Round, the
        result freq is quantized to the nearest 100 Hz — first wheel
        tick after enabling Round snaps to grid, subsequent ticks
        step cleanly by the chosen step.

        Phase 3.E.1 hotfix v0.19 (2026-05-12): routes to whichever
        RX currently owns the panadapter, mirroring
        ``set_freq_from_panadapter`` (Phase 3.E.1 v0.1).  Operator-
        reported bug 2026-05-12: "RX is highlighted and panadapter
        waterfall follow the Mouse tuning does not follow neither
        does Exact/100Hz option" -- wheel-over-panadapter on the
        RX2 pane still wrote RX1's VFO.  Pre-fix, the operator
        could click-to-tune RX2 fine (that path already routed),
        but wheeling did nothing useful when focused on RX2.
        """
        if delta_units == 0:
            return
        step = self.panadapter_scroll_step_hz
        if self._panadapter_source_rx == 2:
            base = int(self._rx2_freq_hz)
        else:
            base = int(self._freq_hz)
        new_freq = base + int(delta_units) * step
        # Apply Exact / Round 100 Hz preference (no-op when off).
        new_freq = self.round_panadapter_freq(new_freq)
        # Clamp to HL2's tunable range (~0..30 MHz on either DDC).
        new_freq = max(0, min(30_000_000, new_freq))
        if self._panadapter_source_rx == 2:
            self.set_rx2_freq_hz(new_freq)
        else:
            self.set_freq_hz(new_freq)

    def set_freq_from_panadapter(self, hz: int) -> None:
        """Set the VFO frequency from a panadapter-driven gesture
        (click-tune, Shift+click peak-snap, drag-pan).  Single
        chokepoint that applies the operator's Exact / Round 100 Hz
        preference before the actual freq write.

        Use this instead of ``set_freq_hz`` from any UI path that
        derives a freq from panadapter pixel position; direct entry
        / band buttons / memory recall / CAT all bypass rounding by
        calling ``set_freq_hz`` straight.

        Phase 3.E.1 v0.1 (2026-05-12): routes the write to the RX
        that currently owns the panadapter (``panadapter_source_rx``).
        When the operator clicks on a peak in a panadapter that's
        showing RX2's band, the click tunes RX2 -- not RX1 -- so
        the visual feedback matches the operator's mental model
        ("I clicked the signal on the panadapter, that's what I'm
        listening to").
        """
        rounded = self.round_panadapter_freq(int(hz))
        if self._panadapter_source_rx == 2:
            self.set_rx2_freq_hz(rounded)
        else:
            self.set_freq_hz(rounded)

    def autoload_panadapter_scroll_step(self) -> None:
        """Restore the operator's persisted scroll step on startup."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            step = int(s.value(
                "display/panadapter_scroll_step_hz", 1000, type=int))
        except Exception:
            return
        self._panadapter_scroll_step_hz = max(1, step)

    # ── Panadapter freq quantization (Exact / Round 100 Hz) ──────────
    # Operator request 2026-05-09 (tester Brent): when wheel-tuning or
    # click-tuning the panadapter, optionally round the resulting
    # frequency to the nearest 100 Hz so the display lands on a
    # "round" freq instead of pixel-derived values like 7.155.232 MHz.
    # Half-up rounding: 7,155,232 -> 7,155,200 (32 < 50, down);
    # 7,155,251 -> 7,155,300 (51 >= 50, up).  Independent of the
    # Panafall Step setting (which controls per-tick increment); this
    # controls whether the FINAL freq is quantized to a 100 Hz grid.
    # Default OFF — preserves pre-toggle behavior.
    PANADAPTER_ROUND_QUANTUM_HZ = 100

    @property
    def panadapter_round_to_100hz(self) -> bool:
        """When True, panadapter freq-set actions (wheel-tune, click-
        tune, drag-pan, peak-snap) round the resulting freq to the
        nearest 100 Hz grid.  When False, the freq is set exactly as
        derived from the gesture.

        Direct freq entry, memory recall, band buttons, TIME / GEN /
        Memory presets, and CAT-driven freq writes all bypass this —
        only panadapter pixel-driven tuning is affected.
        """
        return bool(getattr(self, "_panadapter_round_to_100hz", False))

    def set_panadapter_round_to_100hz(self, on: bool) -> None:
        """Toggle the Exact / Round 100 Hz quantization for panadapter
        freq-set actions.  Persists via QSettings.  Emits
        ``panadapter_round_to_100hz_changed`` so the Display panel
        toolbutton stays in sync."""
        on = bool(on)
        if on == self.panadapter_round_to_100hz:
            return
        self._panadapter_round_to_100hz = on
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("display/panadapter_round_to_100hz", on)
        except Exception as exc:
            print(f"[Radio] persist panadapter round-to-100hz: {exc}")
        self.panadapter_round_to_100hz_changed.emit(on)

    def round_panadapter_freq(self, hz: int) -> int:
        """Apply the operator's Exact / Round 100 Hz preference to a
        candidate freq.  Returns the freq unchanged if rounding is
        off, else the half-up 100 Hz round.

        Used as a single chokepoint by ``panadapter_scroll_tune`` and
        the four panadapter UI freq-emit sites (click-tune, wheel-
        tune, drag-pan, Shift+click peak-snap).  Centralizing here
        prevents per-call-site drift if the rule ever changes (e.g.
        per-mode quantum someday).
        """
        if not self.panadapter_round_to_100hz:
            return int(hz)
        q = int(self.PANADAPTER_ROUND_QUANTUM_HZ)
        # Half-up to nearest q.  +q/2 then integer-divide handles the
        # 5 → 10 case the operator's request specifies (251 → 300).
        return ((int(hz) + q // 2) // q) * q

    def autoload_panadapter_round_to_100hz(self) -> None:
        """Restore the operator's persisted round-to-100Hz flag on
        startup.  Default False (Exact mode)."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            on = bool(s.value(
                "display/panadapter_round_to_100hz", False, type=bool))
        except Exception:
            return
        self._panadapter_round_to_100hz = on

    # ── NCDXF beacon auto-follow ──────────────────────────────────────
    # When enabled, Lyra auto-tunes its VFO to whichever band the
    # tracked NCDXF station is currently transmitting on.  The 18-
    # station NCDXF rotation cycles every 10 sec across 5 bands
    # (20m / 17m / 15m / 12m / 10m), and a regular transceiver
    # operator would have to manually band-change every 10 sec to
    # follow one station around — Lyra just does it.  Operator-set
    # via the Propagation panel's Follow dropdown.

    @property
    def ncdxf_follow_station(self) -> Optional[str]:
        """Currently followed NCDXF station callsign, or None if off."""
        return getattr(self, "_ncdxf_follow_station", None)

    def set_ncdxf_follow_station(self, callsign: Optional[str]) -> None:
        """Start / stop auto-following an NCDXF station.

        Pass a callsign (e.g. "W6WX") to start; pass None to stop.
        Validates the callsign against the known station list — an
        unknown callsign silently turns follow off rather than
        chasing a phantom.

        Persists across sessions via QSettings.
        """
        from lyra.propagation import NCDXF_STATIONS
        valid = {s[0] for s in NCDXF_STATIONS}
        if callsign and callsign in valid:
            self._ncdxf_follow_station = callsign
        else:
            self._ncdxf_follow_station = None
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            s.setValue("propagation/ncdxf_follow_station",
                       self._ncdxf_follow_station or "")
        except Exception as exc:
            print(f"[Radio] persist NCDXF follow: {exc}")
        self._ncdxf_follow_changed.emit(self._ncdxf_follow_station or "")
        # Start / stop the 1 Hz pump timer based on follow state.
        if self._ncdxf_follow_station:
            if not self._ncdxf_follow_timer.isActive():
                self._ncdxf_follow_timer.start()
            self._ncdxf_follow_pump()      # immediate kick on activation
        else:
            if self._ncdxf_follow_timer.isActive():
                self._ncdxf_follow_timer.stop()

    def _ncdxf_follow_pump(self) -> None:
        """Re-tune the VFO to the followed station's current band.

        Called by ``set_ncdxf_follow_station`` (immediate kick) and by
        the 10-sec timer that fires once per slot transition.  No-op
        when follow is off.
        """
        call = getattr(self, "_ncdxf_follow_station", None)
        if not call:
            return
        from lyra.propagation import (
            NCDXF_STATIONS, NCDXF_BANDS,
            ncdxf_current_slot, ncdxf_station_for_band,
        )
        try:
            target_idx = next(
                i for i, s in enumerate(NCDXF_STATIONS) if s[0] == call)
        except StopIteration:
            return
        slot = ncdxf_current_slot()
        for band_idx, (_, freq_khz) in enumerate(NCDXF_BANDS):
            if ncdxf_station_for_band(band_idx, slot) == target_idx:
                # Followed station is on this band right now —
                # tune to it.  Set mode to CWU first so the operator
                # actually hears the CW callsign + tones.  Under
                # v0.0.9.8's carrier-freq VFO convention the listed
                # NCDXF freq (= the beacon's transmitted carrier)
                # is exactly what ``set_freq_hz`` accepts — the
                # DDS-vs-VFO offset is applied centrally inside the
                # radio.  No per-call-site CW pitch math (the
                # v0.0.9.7.1 fix was reverted with the convention
                # switch).
                try:
                    if self._mode != "CWU":
                        self.set_mode("CWU")
                    self.set_freq_hz(freq_khz * 1000)
                except Exception as exc:
                    print(f"[Radio] NCDXF auto-follow tune: {exc}")
                return
        # Followed station isn't on any of the 5 bands right now
        # (silent slot) — leave the VFO where it is until the next
        # slot transition.

    def autoload_ncdxf_follow(self) -> None:
        """Restore the persisted NCDXF follow station on startup.

        Empty string means follow is off (operator's last state was
        not following anyone).
        """
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
            call = str(s.value(
                "propagation/ncdxf_follow_station", "", type=str) or "")
        except Exception:
            return
        if call:
            self.set_ncdxf_follow_station(call)

    # ── Phase 4 v0.1 (2026-05-12) RX2 state persistence ────────────
    def autoload_rx2_state(self) -> None:
        """Restore persisted RX2 state from QSettings on startup.

        Loads (in order):
          * RX2 per-mode RX BW dict (``rx2/rx_bw_by_mode``)
          * RX2 mode (``rx2/mode``) -- via ``set_mode(target_rx=2)``
          * RX2 freq (``rx2/freq_hz``) -- via ``set_rx2_freq_hz``;
            applies CW pitch offset via the per-target DDS path
          * RX2 AF gain (``rx2/af_gain_db``)
          * RX2 volume (``rx2/volume``)
          * RX2 muted (``rx2/muted``)
          * RX2 AGC profile (``rx2/agc_profile``)
          * RX2 AGC threshold dBFS (``rx2/agc_threshold``)
          * Focused RX (``radio/focused_rx``)
          * SUB / dispatch.rx2_enabled (``dispatch/rx2_enabled``)

        SUB state is restored LAST under ``_suppress_sub_mirror=True``
        so the rising-edge mirror in ``set_rx2_enabled`` doesn't
        clobber the just-loaded RX2 vol/mute/AF gain.

        All steps are individually wrapped in try/except so a
        single bad value doesn't blank the whole restore.  The
        defaults the constructor populated stay in place for any
        missing key.
        """
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
        except Exception:
            return
        # Per-mode RX BW dict — JSON since QSettings doesn't
        # natively round-trip a dict.  Restore BEFORE mode so the
        # set_mode call sees the correct per-mode BW.
        if s.contains("rx2/rx_bw_by_mode"):
            import json
            try:
                _bw_dict = json.loads(str(s.value("rx2/rx_bw_by_mode")))
                if isinstance(_bw_dict, dict):
                    for _m, _bw in _bw_dict.items():
                        try:
                            self.set_rx_bw(str(_m), int(_bw), target_rx=2)
                        except (TypeError, ValueError):
                            pass
            except (ValueError, TypeError):
                pass
        if s.contains("rx2/mode"):
            try:
                self.set_mode(str(s.value("rx2/mode")), target_rx=2)
            except Exception as exc:
                print(f"[Radio] autoload rx2/mode: {exc}")
        if s.contains("rx2/freq_hz"):
            try:
                self.set_rx2_freq_hz(int(s.value("rx2/freq_hz")))
            except (TypeError, ValueError):
                pass
        if s.contains("rx2/af_gain_db"):
            try:
                self.set_af_gain_db(
                    int(s.value("rx2/af_gain_db")), target_rx=2)
            except (TypeError, ValueError):
                pass
        if s.contains("rx2/volume"):
            try:
                self.set_volume(
                    float(s.value("rx2/volume")), target_rx=2)
            except (TypeError, ValueError):
                pass
        if s.contains("rx2/muted"):
            try:
                self.set_muted(
                    s.value("rx2/muted", False, type=bool),
                    target_rx=2)
            except Exception:
                pass
        if s.contains("rx2/agc_profile"):
            try:
                self.set_agc_profile(
                    str(s.value("rx2/agc_profile")), target_rx=2)
            except Exception:
                pass
        if s.contains("rx2/agc_threshold"):
            try:
                v = float(s.value("rx2/agc_threshold"))
                # Same dBFS-vs-legacy-linear guard as RX1's load.
                if not (-1.0 < v < 1.0):
                    self.set_agc_threshold(v, target_rx=2)
            except (TypeError, ValueError):
                pass
        # v0.1.0-pre3 (2026-05-13 operator UX choice): Lyra always
        # starts in RX1-focused mode regardless of last session's
        # focused_rx.  The persisted value at ``radio/focused_rx``
        # is left intact for future flexibility but intentionally
        # NOT applied at startup — operator preference is to always
        # land on RX1 so the panadapter / waterfall / save-restore
        # paths (which key off ``_freq_hz``, RX1's frequency) are
        # always in agreement with what the operator sees.
        # if s.contains("radio/focused_rx"):
        #     try:
        #         self.set_focused_rx(int(s.value("radio/focused_rx")))
        #     except (TypeError, ValueError):
        #         pass
        # SUB state LAST + mirror suppressed so persisted RX2
        # vol/mute/AF gain don't get smashed by the rising-edge
        # mirror in ``set_rx2_enabled``.
        if s.contains("dispatch/rx2_enabled"):
            try:
                want = s.value(
                    "dispatch/rx2_enabled", False, type=bool)
                self._suppress_sub_mirror = True
                try:
                    self.set_rx2_enabled(bool(want))
                finally:
                    self._suppress_sub_mirror = False
            except Exception as exc:
                print(f"[Radio] autoload SUB: {exc}")

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
        """Multi-row push per FFT tick for a fast-scroll effect.
        Earlier versions duplicated the same row N times (visible
        vertical blockiness); current implementation linearly
        interpolates between the previous and current FFT so the M
        rows form a smooth gradient. Range 1..30 (1 = normal, 30 =
        30× visual speed). Cap raised 2026-04-29: at low spec rates
        (5-20 fps) the previous 10× cap meant rows-per-second was
        too slow for digital-mode hunting, where operators want
        rapid scroll to see FT8 cycles distinctly."""
        m = max(1, min(30, int(m)))
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
        fire spot_activated. Tune the radio there. Returns True on hit.

        Under v0.0.9.8's carrier-freq VFO convention the spot's
        stored freq (= the cluster's reported carrier) is exactly
        what ``set_freq_hz`` accepts — the DDS-vs-VFO offset for
        CW spots is applied centrally inside the radio.  No
        per-call-site CW pitch math (the v0.0.9.7.2 fix that added
        it was reverted with the convention switch).

        Phase 3.E.1 hotfix v0.7 (2026-05-12): routes the freq write
        to the panadapter-source RX (= focused RX by default), so
        a click on a cluster/RBN spot in the panadapter tunes the
        same VFO the operator is looking at -- not always RX1.
        Mode comes from the spot record (cluster/RBN reports it);
        passes through ``tune_preset`` for atomic mode+freq write.
        """
        if not self._spots:
            return False
        best = min(self._spots.values(), key=lambda s: abs(s["freq_hz"] - freq_hz))
        if abs(best["freq_hz"] - freq_hz) > tolerance_hz:
            return False
        target_rx = int(self._panadapter_source_rx)
        spot_mode = str(best.get("mode") or "USB")
        try:
            self.tune_preset(
                int(best["freq_hz"]), spot_mode, target_rx=target_rx)
        except Exception:
            # Best-effort fallback to legacy RX1 path so a tune
            # never silently no-ops -- the spot_activated signal
            # must fire for the TCI round-trip.
            self.set_freq_hz(int(best["freq_hz"]))
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
        if self._dsp_worker is not None:
            # B.5 — worker mode: build the new sink on main, hand it
            # to the worker, and let the worker close the old one
            # AFTER it stops writing to it.  No 30 ms sleep needed
            # because the worker serializes close() with its run
            # loop (the slot can't fire mid-block).
            new_sink = self._make_sink() if self._stream else NullSink()
            # B.9: channel reset routed through worker (between blocks).
            self._request_dsp_reset_channel_only()
            self._audio_sink = new_sink
            self._push_balance_to_sink()
            self.worker_audio_sink_changed.emit(new_sink)
            # §15.7 timing -- record which audio sink is active so the
            # next [TIMING] line carries the right ``sink=...`` context.
            if self._timing_stats is not None:
                self._timing_stats.set_context(
                    "sink", str(self._audio_output))
        else:
            # Single-thread (default) path — close-then-rebuild on
            # the main thread, with the small WASAPI grace sleep.
            try:
                self._audio_sink.close()
            except Exception:
                pass
            self._request_dsp_reset_channel_only()
            # 30 ms — long enough for PortAudio/WASAPI to fully
            # release the device handle, short enough to be
            # imperceptible to the operator. Tested across AK4951↔PC
            # swaps with no recurrence of the robotic-sound symptom.
            import time as _time
            _time.sleep(0.030)
            self._audio_sink = (
                self._make_sink() if self._stream else NullSink())
            # New sink starts at default L/R (equal-power center) —
            # push the operator's current balance so the new sink
            # picks up the pan immediately, not on the next
            # set_balance.
            self._push_balance_to_sink()
        self.audio_output_changed.emit(output)

    # ── Stream lifecycle ──────────────────────────────────────────────
    def start(self):
        if self._stream:
            return
        try:
            self._stream = HL2Stream(self._ip, sample_rate=self._rate)
            # Phase 1 v0.1 (2026-05-11): wire RX2 dispatch + the
            # dispatch-state provider per consensus plan §4.2 + §4.2.x.
            #
            # * ``on_samples`` -> RX_AUDIO_CH0 (DDC0, RX1 audio chain;
            #   v0.0.9.x-compatible).
            # * ``on_rx2_samples`` -> RX_AUDIO_CH2 (DDC1, RX2 audio
            #   chain; Phase 1 stub counts samples for §4.4 bench
            #   verification, Phase 2 wires the real audio path).
            # * ``dispatch_state_provider`` -> Radio.snapshot_dispatch_state
            #   (Phase 0 surface).  HL2Stream._rx_loop reads this
            #   once per UDP datagram per the §4.2.x threading
            #   model (Qt main thread is sole writer; RX-loop +
            #   DSP-worker threads are readers).
            self._stream.start(
                on_samples=self._stream_cb,
                rx_freq_hz=self._freq_hz,
                lna_gain_db=self._gain_db,
                on_rx2_samples=self._stream_cb_rx2,
                dispatch_state_provider=self.snapshot_dispatch_state,
            )
            # Phase 1 v0.1: push the cached RX2 freq so bench-test
            # iterations across stream restarts don't lose the
            # operator's last-set VFO B value.  Safe to call before
            # any real "RX2 enabled" toggle exists -- the DDC1
            # gateware accepts the write and just tunes a receiver
            # whose audio isn't routed anywhere in Phase 1.
            try:
                self._stream._set_rx2_freq(self._rx2_freq_hz)  # noqa: SLF001
            except Exception as e:
                # Non-fatal: freq tunes on next set_rx2_freq_hz call.
                self.status_message.emit(
                    f"RX2 initial freq push failed: {e}", 3000,
                )
            # v0.2 Phase 2 commit 7-redo (2026-05-15): open TX channel
            # + start the dedicated TX DSP worker thread.  Replaces
            # the broken inline-dispatch path from commit 7 and the
            # LYRA_ENABLE_TX_DISPATCH env var from commit 7.1.
            #
            # The worker absorbs the ~10 ms blocking cost of every
            # ``fexchange0`` call on its own thread, so the producer
            # threads (RX-loop for HL2 jack, PortAudio for PC sound
            # card) never block.  Mic data flows continuously even
            # on RX (keeps WDSP TXA chain in steady state); the I/Q
            # output is dropped on the floor until Phase 3 PTT state
            # machine flips ``HL2Stream.inject_tx_iq=True`` on MOX=1
            # edge.  Wire bytes are byte-identical to v0.1.1 until
            # that PTT flip lands.
            self._open_tx_channel()
            if self._tx_channel is not None:
                try:
                    from lyra.dsp.mox_edge_fade import MoxEdgeFade
                    from lyra.dsp.tx_dsp_worker import TxDspWorker
                    from lyra.dsp.tx_iq_tap import Sip1Tap
                    # v0.2 Phase 2 commit 9: construct sip1 TX I/Q
                    # tap.  Producer-only for v0.2; v0.3 PS calcc
                    # thread will read snapshots.
                    self._tx_iq_tap = Sip1Tap()
                    # v0.2 Phase 2 commit 10: construct MOX-edge
                    # fade.  Stays in OFF state until Phase 3 PTT
                    # state machine calls start_fade_in().
                    self._mox_edge_fade = MoxEdgeFade()
                    self._tx_dsp_worker = TxDspWorker(
                        self._tx_channel, self._stream,
                        iq_tap=self._tx_iq_tap,
                        mox_edge_fade=self._mox_edge_fade,
                    )
                    self._tx_dsp_worker.start()
                    self._wire_mic_source()
                except Exception as exc:  # noqa: BLE001
                    print(f"[Radio] TxDspWorker start failed: {exc}")
                    self._tx_dsp_worker = None
                    self._tx_iq_tap = None
                    self._mox_edge_fade = None
        except Exception as e:
            self.status_message.emit(f"Start failed: {e}", 5000)
            self._stream = None
            return
        self._audio_sink = self._make_sink()
        self._push_balance_to_sink()
        # §15.7 timing: stamp the sink kind + active latency-tune
        # env-var values so [TIMING] lines record which experimental
        # config produced them (operator running a tune-down session
        # can cross-reference TIMING numbers with the env settings).
        if self._timing_stats is not None:
            self._timing_stats.set_context(
                "sink", str(self._audio_output))
            try:
                tx_lat = getattr(self._stream, "_tx_latency_ms", 40)
                self._timing_stats.set_context("hl2_txlat_ms", tx_lat)
            except Exception:
                pass
            try:
                import os as _os_t
                rl = _os_t.environ.get(
                    "LYRA_RMATCH_RING_MS", "").strip()
                if rl:
                    self._timing_stats.set_context(
                        "rmatch_ring_ms", rl)
                else:
                    self._timing_stats.set_context(
                        "rmatch_ring_ms", "400")
            except Exception:
                pass
        # B.5 — in worker mode, hand the freshly-built sink to the
        # worker so it writes to it directly (and closes the
        # previous NullSink seed) without the main-thread close
        # race.  No-op in single-thread mode.
        if self._dsp_worker is not None:
            self.worker_audio_sink_changed.emit(self._audio_sink)
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
        if self._dsp_worker is not None:
            # B.5 — worker mode: install NullSink on Radio and hand
            # it to the worker, which closes the old (real) sink
            # between blocks.  Avoids close-while-writing race.
            new_sink = NullSink()
            self._audio_sink = new_sink
            # §15.21 bug 3 fix (§15.24 plan item B): synchronously
            # barrier on the worker applying THIS swap before stop()
            # returns.  Without it, a rapid stop()->start() could
            # leave the stale NullSink swap queued and delivered
            # AFTER the next start()'s real sink -> worker keeps the
            # NullSink -> silent audio until the next swap.  This is
            # the ONLY emit site that clears+waits; start() /
            # set_audio_output / PC-device-change deliberately do
            # NOT (they must not block on the worker event loop).
            # Bounded 1.0 s wait -> never hangs stop() even if the
            # worker loop is wedged (it then proceeds to tear the
            # worker down anyway).
            try:
                self._dsp_worker._sink_swap_done.clear()  # noqa: SLF001
            except Exception:
                pass
            self.worker_audio_sink_changed.emit(new_sink)
            try:
                self._dsp_worker._sink_swap_done.wait(  # noqa: SLF001
                    timeout=1.0)
            except Exception:
                pass
        else:
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
        # v0.2 Phase 2 commit 7-redo (2026-05-15): tear down TX path
        # in producer -> worker -> consumer order so nothing fires
        # into a freed object.
        #
        #   1. Clear the HL2 mic_callback so the RX-loop stops
        #      submitting (RX-loop itself is still alive until
        #      _stream.stop() below).
        #   2. Stop the PC mic source so PortAudio stops submitting.
        #   3. Stop + join the TX DSP worker (drains remaining queue,
        #      worker can no longer call TxChannel.process or
        #      HL2Stream.queue_tx_iq).
        #   4. Close TX channel (now safe -- no producer of
        #      .process() calls remains).
        #   5. _stream.stop() further down handles RX-loop teardown
        #      + final STOP_IQ + socket close (the HL2 wedge fix is
        #      orthogonal to TX teardown -- see Phase 1 commit 6.1).
        if self._stream is not None:
            try:
                self._stream.register_mic_consumer(None)
            except Exception:
                pass
        if (self._pc_mic_source is not None
                and self._pc_mic_source.is_running):
            try:
                self._pc_mic_source.stop()
            except Exception as exc:
                print(f"[Radio] PC mic stop failed: {exc}")
        if self._tx_dsp_worker is not None:
            try:
                self._tx_dsp_worker.stop(timeout=1.0)
            except Exception as exc:
                print(f"[Radio] TxDspWorker stop failed: {exc}")
            self._tx_dsp_worker = None
        # v0.2 Phase 2 commit 9: clear + drop the sip1 tap.  Worker
        # is already stopped above, so no producer remains.  Any
        # v0.3 PS consumer that reads after this point will see an
        # empty snapshot (which is correct -- stream is stopping).
        if self._tx_iq_tap is not None:
            try:
                self._tx_iq_tap.clear()
            except Exception:
                pass
            self._tx_iq_tap = None
        # v0.2 Phase 2 commit 10: drop the MOX-edge fade.  Worker
        # has stopped so no apply() calls remain.  State will
        # reset to OFF on next construction in start().
        self._mox_edge_fade = None
        self._close_tx_channel()
        if self._stream:
            self._stream.stop()
            self._stream = None
        with self._ring_lock:
            self._sample_ring.clear()
        # B.9: channel reset routed through worker in worker mode
        # (worker also clears its own sample ring + FFT counter).
        self._request_dsp_reset_channel_only()
        self._lna_peaks = []
        self._lna_rms = []
        self.stream_state_changed.emit(False)

    def close(self) -> None:
        """Tear everything down before app exit.

        Wired to ``QApplication.aboutToQuit`` so it always runs on a
        clean shutdown — operator hits the X, picks Quit, or the
        process gets a graceful SIGTERM.  Idempotent: safe to call
        twice.

        Order matters:
          1. Stop the HL2 stream + audio sink (via stop()).  Without
             this, the radio keeps streaming UDP frames at our last
             known TX state until C&C times out, and the AK4951 can
             be left in a buzzy half-state for ~1 sec on next launch.
          2. Shut down the DSP worker thread cleanly so its event
             loop drains and the QThread joins.
          3. Flush QSettings so band memory, last frequency, and
             panel layout writes from the last few seconds before
             quit actually hit disk.
        """
        # Step 1 — stream + audio.  stop() handles the worker-mode
        # vs single-thread-mode sink-close ordering correctly.
        # is_streaming is a @property (line 1054), so no parens.
        try:
            if self.is_streaming:
                self.stop()
        except Exception as exc:
            print(f"[Radio.close] stop() raised: {exc}")
        # Step 2 — DSP worker.  shutdown_dsp_worker is idempotent
        # (no-op if no worker was ever started).
        try:
            self.shutdown_dsp_worker()
        except Exception as exc:
            print(f"[Radio.close] shutdown_dsp_worker raised: {exc}")
        # Step 2.25 — AudioMixer thread, if running.  Disabled by
        # default in v0.0.9.6 (see Radio.__init__ for rationale);
        # stop() is a no-op when mixer is None.
        try:
            if self._audio_mixer is not None:
                self._audio_mixer.stop()
        except Exception as exc:
            print(f"[Radio.close] audio_mixer.stop raised: {exc}")
        # Step 2.5 — Weather worker.  Same idempotent pattern.
        try:
            if self._wx_worker is not None:
                self._wx_worker.request_stop()
                self._wx_worker.wait(2000)   # 2-second grace window
        except Exception as exc:
            print(f"[Radio.close] wx_worker shutdown raised: {exc}")
        # Step 3 — flush QSettings so the operator's most recent
        # changes (volume tweak, freq change in the last 100 ms,
        # captured-profile autoload bookkeeping) actually persist.
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").sync()
        except Exception as exc:
            print(f"[Radio.close] QSettings sync raised: {exc}")

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

    # ── Phase 3.B B.4: DSP worker thread lifecycle ───────────────────
    def _build_and_start_dsp_worker(self) -> None:
        """Construct the DspWorker + its QThread and start the worker
        running.  Called from __init__ when ``_dsp_threading_mode_at_startup``
        is ``DSP_THREADING_WORKER``.

        After this returns:
        - ``self._dsp_worker`` is a live DspWorker on its own thread
        - The worker's run_loop is draining its input queue
        - The worker's process_block uses Radio's DSP machinery via
          the back-reference set by attach_to_radio()
        - The worker idles when no samples are enqueued (queue
          empty); CPU cost while no stream is active is negligible

        Pattern: QObject + moveToThread (modern Qt-recommended over
        QThread.run override).

        **Parent=None is REQUIRED** — Qt refuses to move a QObject
        that has a parent ("QObject::moveToThread: Cannot move
        objects with a parent" warning, then the move silently
        fails and the worker stays on the source thread).  When
        run_loop then runs on the main thread instead of the
        worker thread, its blocking ``queue.get(timeout=...)`` call
        hangs the UI.  This was a latent bug all the way back to
        Phase 3.B B.1 (the worker-mode shell) -- never observed in
        production because the QSettings ordering bug fixed in
        Commit 1 prevented worker mode from actually being entered.

        Cleanup is still handled: ``Radio.close()`` is wired to
        ``QApplication.aboutToQuit``, calls ``shutdown_dsp_worker``,
        which joins the worker thread cleanly.  The earlier "parent
        for cleanup" defense was redundant with the explicit
        teardown path.
        """
        from PySide6.QtCore import QThread
        from lyra.dsp.worker import DspWorker
        # Construct worker WITHOUT a parent so moveToThread below
        # can actually move it.  See docstring for why.
        self._dsp_worker = DspWorker(parent=None)
        self._dsp_worker.attach_to_radio(self)
        # Seed the worker's config from Radio's current state so the
        # worker has correct AF / Vol / Mute / BIN values from frame
        # zero.  These config slots are vestigial post-Phase-6.A:
        # the worker calls ``radio._do_demod_wdsp`` directly, and
        # WDSP applies AGC / NR / ANF / output filter inside the
        # engine — Volume + Mute are applied by ``_do_demod_wdsp``
        # itself reading off Radio.  The worker setters just keep
        # the config dataclass current for any future re-use.
        self._dsp_worker.set_agc_profile(self._agc_profile)
        self._dsp_worker.set_af_gain_db(self._af_gain_db)
        self._dsp_worker.set_volume(self._volume)
        self._dsp_worker.set_muted(self._muted)
        self._dsp_worker.set_bin_enabled(self._bin_enabled)
        self._dsp_worker.set_bin_depth(self._bin_depth)
        # B.5 — seed the worker's audio sink reference so it has a
        # valid sink from the very first IQ block (before stream
        # start, the sink is NullSink — write() is a no-op).  Direct
        # attribute assignment is safe here because moveToThread
        # hasn't run yet, so the worker still lives on the main
        # thread for this brief construction window.
        self._dsp_worker._audio_sink = self._audio_sink
        # Move to dedicated thread.  parent=None for the QThread is
        # required by Qt — moveToThread fails if the source thread
        # owns a parented object that's also being moved.  We track
        # the thread on Radio to keep it alive.
        self._dsp_worker_thread = QThread()
        self._dsp_worker.moveToThread(self._dsp_worker_thread)
        # Wire Radio setter signals to worker config slots.  These
        # are cross-thread (Radio lives on main, worker on its own
        # thread) so Qt automatically uses QueuedConnection — slot
        # calls land on the worker's event loop between blocks, no
        # locking needed (see threading.md §6).
        from PySide6.QtCore import Qt as _Qt
        _qc = _Qt.QueuedConnection
        self.agc_profile_changed.connect(
            self._dsp_worker.set_agc_profile, _qc)
        self.bin_enabled_changed.connect(
            self._dsp_worker.set_bin_enabled, _qc)
        self.bin_depth_changed.connect(
            self._dsp_worker.set_bin_depth, _qc)
        # Phase 3.E.1 v0.1: flush the worker's FFT sample ring when
        # panadapter source switches RX1 <-> RX2.  Without this, the
        # first FFT after the switch would be a mix of old + new
        # source samples and render as a garbage frame.
        self.panadapter_source_changed.connect(
            self._dsp_worker.flush_fft_ring, _qc)
        # B.5 — sink swap channel.  When Radio rebuilds the audio
        # sink (start/stop, set_audio_output, PC device change), the
        # worker swaps its local reference between blocks AND closes
        # the old sink (so PortAudio/AK4951 close() never runs while
        # the worker is mid-write to that same object).
        self.worker_audio_sink_changed.connect(
            self._dsp_worker._on_audio_sink_changed, _qc)
        # B.6 — LNA peak / RMS feed from worker.  The single-thread
        # path computes peak/RMS in _on_samples_main_thread (which
        # is bypassed in worker mode); the worker computes them in
        # its process_block and emits via lna_peak_update.  Main-
        # thread slot appends to the same _lna_peaks / _lna_rms
        # lists that Auto-LNA + the toolbar readout already consume.
        self._dsp_worker.lna_peak_update.connect(
            self._on_worker_lna_peak, _qc)
        # B.8 — raw spectrum feed from worker.  The single-thread
        # path runs FFT on a wall-clock QTimer (_fft_timer) reading
        # _sample_ring directly.  In worker mode the worker owns
        # its own sample ring and runs FFT block-counter-driven;
        # it emits spectrum_raw_ready (just the spec_db array) and
        # this main-thread slot runs everything downstream
        # (_process_spec_db: S-meter, noise floor, auto-scale,
        # zoom, panadapter + waterfall emits).  _fft_timer keeps
        # firing so _radio_debug_maybe_print stays alive but its
        # FFT body short-circuits in worker mode.
        self._dsp_worker.spectrum_raw_ready.connect(
            self._on_worker_spectrum_raw, _qc)
        # AF Gain, Volume, Muted aren't currently exposed as Qt
        # signals on Radio (the audio path reads them directly from
        # ``_af_gain_db`` / ``_volume`` / ``_muted`` each block).
        # Worker mode calls ``radio._do_demod_wdsp`` which reads the
        # same attributes, so live updates work for free.  AF Gain
        # routing into WDSP's PanelGain is logged as Phase 9.5 Item 4
        # — see CLAUDE.md §14.9.
        # The worker's slot @run_loop runs once (until exit), driven
        # by the thread's started signal.
        self._dsp_worker_thread.started.connect(self._dsp_worker.run_loop)
        self._dsp_worker_thread.start()
        print(f"[Radio] DSP worker thread started "
              f"(queue depth {self._dsp_worker.INPUT_QUEUE_DEPTH})")

    def shutdown_dsp_worker(self, timeout_ms: int = 1500) -> None:
        """Cleanly stop the DSP worker thread, if any.  Idempotent;
        safe to call multiple times.  Called from MainWindow.closeEvent
        before Radio.stop() so the worker drains in flight before
        the audio sink closes.

        Bounded wait — the worker's run_loop blocks on its input
        queue with a short timeout, so stop_requested takes effect
        within the queue's poll interval (~100 ms).  A 1.5-second
        wait is generous and protects against hangs at app exit.
        """
        if self._dsp_worker is None:
            return
        try:
            self._dsp_worker.request_stop()
            if self._dsp_worker_thread is not None:
                self._dsp_worker_thread.quit()
                exited = self._dsp_worker_thread.wait(timeout_ms)
                if not exited:
                    print("[Radio] DSP worker thread did not exit "
                          f"within {timeout_ms} ms — forcing terminate")
                    self._dsp_worker_thread.terminate()
                    self._dsp_worker_thread.wait(500)
        except Exception as exc:
            print(f"[Radio] shutdown_dsp_worker error: {exc}")
        # Drop references so any second call is a no-op
        self._dsp_worker = None
        self._dsp_worker_thread = None

    def _request_dsp_reset_full(self) -> None:
        """Reset the full audio chain: rx_channel + binaural + AGC
        envelope + S-meter running average.  Used at freq / mode /
        rate change — any operator action that introduces a
        legitimate audio discontinuity.

        Worker mode (B.9): defers to the worker, which performs the
        same reset between blocks (no race with worker's
        process_block).  Single-thread mode: runs synchronously
        on the calling (main) thread, identical to v0.0.5
        behavior.
        """
        if self._dsp_worker is not None:
            # Worker performs ALL the resets between blocks; main
            # thread doesn't touch DSP state directly.
            self._dsp_worker.request_reset()
            return
        # Single-thread path — synchronous reset on main.
        self._rx_channel.reset()
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.reset()
            except Exception as exc:
                print(f"[Radio] WDSP rx reset error: {exc}")
        # NOTE: legacy `self._wdsp_agc.reset()` removed Phase 6.A —
        # WDSP's AGC state lives inside the DLL and resets via the
        # _wdsp_rx.reset() above.
        self._smeter_avg_lin = 0.0
        self._smeter_peak_hold_lin = 0.0
        # WDSP SSQL state lives inside the DLL and resets via WDSP's
        # internal flush_ssql when the channel is reopened.  No
        # Python-side state to clear here.
        self._binaural.reset()
        # Leveler envelope follower (_env_db) — without this reset,
        # NOTE: legacy `self._leveler.reset()` removed Phase 4.

    def _request_dsp_reset_channel_only(self) -> None:
        """Reset just the rx_channel — drops the in-flight audio
        buffer + forces decimator rebuild.  Used at sink swap +
        stream stop, where AGC envelope and binaural state should
        be preserved across the discontinuity.

        Worker mode (B.9): worker's request_reset is currently
        coarse-grained (always does the full reset).  That over-
        resets AGC + binaural on sink swap — operator-noticeable
        as a brief AGC re-attack, but not a regression to safety
        (the swap was already an audio discontinuity).  A finer-
        grained worker reset can land if field testing surfaces
        the AGC re-attack as objectionable.

        Single-thread mode: runs synchronously, identical to v0.0.5.
        """
        if self._dsp_worker is not None:
            self._dsp_worker.request_reset()
            return
        self._rx_channel.reset()
        # NOTE: legacy `self._leveler.reset()` removed Phase 4.

    def _on_worker_lna_peak(self, peak: float, rms: float) -> None:
        """Slot for ``DspWorker.lna_peak_update`` (B.6).

        Runs on the main thread because the connection is queued.
        Mirrors the per-block append that
        ``_on_samples_main_thread`` does in single-thread mode, so
        Auto-LNA logic + the toolbar peak/RMS readout sees the same
        history regardless of threading mode.

        Cheap — two list appends + bounded trim.  Called at IQ-batch
        cadence (~tens to low-hundreds of Hz), well within the main
        thread's signal-handling capacity.
        """
        self._lna_peaks.append(float(peak))
        self._lna_rms.append(float(rms))
        if len(self._lna_peaks) > self._lna_peaks_max:
            self._lna_peaks.pop(0)
        if len(self._lna_rms) > self._lna_peaks_max:
            self._lna_rms.pop(0)

    # ── Internal: sample flow ─────────────────────────────────────────
    def _stream_cb(self, samples, _stats):
        """RX-thread callback. Accumulate into a batch; route the
        batch through whichever DSP path the operator selected at
        startup (single-thread Qt main, or worker thread).

        Single-thread (default): emit via _SampleBridge whose
        `samples_ready` signal hops to Radio._on_samples_main_thread
        on the Qt main thread, where _do_demod runs the audio chain.

        Worker thread (BETA, opt-in): push directly to the worker's
        bounded queue. Worker's run_loop drains the queue and calls
        process_block on its own thread.  Drop-oldest behavior on
        queue overflow keeps memory bounded if the worker can't
        keep up.
        """
        with self._rx_batch_lock:
            self._rx_batch.extend(samples.tolist())
            if len(self._rx_batch) >= self._rx_batch_size:
                batch = np.asarray(self._rx_batch, dtype=np.complex64)
                self._rx_batch = []
            else:
                return
        # TCI IQ tap (v0.0.9.1+).  Emit at the post-batch boundary
        # (~46 emits/sec at 192 kHz IQ with the default batch size)
        # rather than per-EP6-frame, to keep Qt event-queue traffic
        # manageable.  Slot is a no-op early-return when no clients
        # are subscribed, so cost when nobody is listening is just
        # the signal emit itself.  Cross-thread emit (RX thread →
        # Qt main thread) is auto-handled by Qt.QueuedConnection.
        try:
            self.iq_for_tci_emit.emit(batch, self._rate)
        except Exception:
            pass

        # Route the batch to the DSP path selected at startup. We
        # check the at-startup mode (not the persisted preference) —
        # this is the mode currently RUNNING this session.  If the
        # operator changes the preference mid-session, it takes
        # effect on the next restart, not now.
        if (self._dsp_threading_mode_at_startup ==
                self.DSP_THREADING_WORKER and self._dsp_worker is not None):
            self._dsp_worker.enqueue_iq(batch)
        else:
            self._bridge.samples_ready.emit(batch)

    def _stream_cb_rx2(self, samples, _stats):
        """RX2 IQ consumer.

        Registered on ``HL2Stream`` as the ``RX_AUDIO_CH2`` consumer
        via ``Radio.start()``.  Called on the RX-loop thread per UDP
        datagram with the DDC1 (RX2) sample batch (38 samples per
        UDP at nddc=4 / 192 kHz).

        Phase 1 (v0.1) -- this was a stub that only counted samples
        for §4.4 bench verification.

        Phase 2 (v0.1, 2026-05-11) -- mirrors ``_stream_cb`` for
        RX2.  Accumulates samples into ``_rx2_batch`` until the
        same threshold as RX1 (``_rx_batch_size``), then enqueues
        the batch onto the DSP worker's RX2 sibling queue.
        Worker pairs it with the RX1 batch produced by
        ``_stream_cb`` for the same UDP-datagram window and
        invokes ``_do_demod_wdsp_dual`` for the stereo combine.

        Single-thread mode (no DspWorker) does NOT route RX2 audio
        in Phase 2 -- worker mode is the canonical path and is
        what every operator runs by default (per
        ``_dsp_threading_mode_at_startup``).  Single-thread mode
        will get RX2 audio in a follow-up if any operator actually
        uses it.

        Continues to maintain Phase 1 diagnostic counters + the
        bench-test ring buffer so the RX2 Bench Test dialog still
        works alongside live audio.
        """
        n = int(samples.shape[0])
        self._rx2_datagrams_received += 1
        self._rx2_samples_received += n

        # ── Phase 1 bench-test ring buffer (gated on dialog open) ──
        if n != 0 and self._rx2_bench_active:
            with self._rx2_iq_ring_lock:
                ring = self._rx2_iq_ring
                ring_size = ring.shape[0]
                pos = self._rx2_iq_ring_pos
                end = pos + n
                if end <= ring_size:
                    ring[pos:end] = samples
                else:
                    first = ring_size - pos
                    ring[pos:] = samples[:first]
                    ring[: n - first] = samples[first:]
                self._rx2_iq_ring_pos = end % ring_size

        # ── Phase 2 audio path: accumulate batch + enqueue to worker ──
        # Mirror of ``_stream_cb`` for RX2.  Per nddc=4 design, the
        # per-UDP sample count here equals the RX1 sample count,
        # so the two batch lists fill in lock-step and the worker
        # naturally pairs them.
        if n == 0:
            return
        with self._rx2_batch_lock:
            self._rx2_batch.extend(samples.tolist())
            if len(self._rx2_batch) >= self._rx_batch_size:
                batch = np.asarray(self._rx2_batch, dtype=np.complex64)
                self._rx2_batch = []
            else:
                return
        # Enqueue to the worker's RX2 queue ONLY in worker mode.
        # Single-thread mode would need a Qt-signal hop to main
        # thread + a stereo combiner there; that path isn't wired
        # in Phase 2 (operator default is worker mode).
        #
        # Phase 3.E.1 hotfix v0.1 (2026-05-12): the f6470ae enqueue
        # gate (skip when ``rx2_enabled`` is False) blocked RX2
        # samples from reaching the FFT pipeline when SUB was off,
        # which broke the Phase 3.E.1 "panadapter follows focus"
        # behavior -- operator focused RX2 with SUB off, panadapter
        # center freq updated but spectrum data stayed on RX1's
        # band (RX2 samples never made it to the worker).  Gate
        # removed: the worker's audio dispatch (7923b94) is the
        # real safety belt -- it gates RX2 audio dual-demod on
        # ``rx2_enabled``, so silence is guaranteed when SUB is off
        # regardless of whether samples are queued.  The cost of
        # always queuing is negligible (drop-oldest policy + small
        # numpy arrays); the benefit is that the FFT pipeline can
        # pull RX2 IQ on demand for panadapter-source switches.
        if (self._dsp_threading_mode_at_startup ==
                self.DSP_THREADING_WORKER
                and self._dsp_worker is not None):
            self._dsp_worker.enqueue_iq_rx2(batch)

    # ── Phase 1 RX2 bench-test surface ────────────────────────────────
    @property
    def rx2_freq_hz(self) -> int:
        """Current RX2 (DDC1 / VFO B) tuned frequency in Hz.

        Phase 1 v0.1 -- the diagnostic surface for §4.4 step 2
        bench testing.  Phase 3 replaces this with the dual-VFO
        focus model + VFO B LED display.
        """
        return self._rx2_freq_hz

    def set_rx2_freq_hz(self, hz: int) -> None:
        """Set the RX2 (DDC1) NCO frequency.

        Phase 1 v0.1 (2026-05-11) bench-test surface per consensus
        plan §4.4 step 2.  Writes the C&C register via
        ``HL2Stream._set_rx2_freq`` (which packs into the C0=0x06
        register and lets the EP2 writer's round-robin propagate
        the value).  Phase 3 wires the operator UI focus-model
        equivalent (VFO B LED + ``set_freq_hz`` routing by focused
        receiver).

        Safe to call before ``start()``; freq is cached and pushed
        to the stream on next start.  Safe to call mid-stream;
        propagation is imperceptibly fast (a few EP2 round-robin
        ticks, < 25 ms).

        Args:
            hz: VFO B frequency in Hz (0..30,000,000 reasonable for
                HL2).  Out-of-range values are clamped by the HL2
                gateware -- Lyra does not pre-validate.
        """
        new_hz = int(hz)
        self._rx2_freq_hz = new_hz
        if self._stream is not None:
            try:
                # Phase 3.E.1 hotfix v0.8 (2026-05-12): apply CW
                # pitch offset to the DDS write so the carrier
                # lands in WDSP RX2's filter passband when mode is
                # CWU/CWL.  Mirrors the RX1 ``set_freq_hz`` path
                # (line ~2340) that has done this since v0.0.9.8's
                # carrier-freq VFO convention switch.
                dds_hz = self._compute_dds_freq_hz(
                    new_hz, target_rx=2)
                self._stream._set_rx2_freq(dds_hz)  # noqa: SLF001
            except Exception as e:
                self.status_message.emit(
                    f"RX2 freq write failed: {e}", 3000,
                )
        self.rx2_freq_changed.emit(new_hz)
        # Phase 3.E.1 hotfix v0.12 (2026-05-12): when the
        # panadapter is sourced from RX2, the marker tracks
        # RX2's VFO.  Re-emit so the spectrum widget repositions
        # the marker for the new freq.
        if self._panadapter_source_rx == 2:
            self._emit_marker_offset()

    # ── Phase 3.D v0.1: VFO transfer helpers (A->B / B->A / Swap) ─
    # Per consensus plan §6.8 working-group decision: when RX2 is
    # ENABLED, A->B / B->A / Swap copy the FULL state (freq + mode
    # + RX BW); when RX2 is DISABLED, they only move VFO B's shadow
    # frequency.  Implementation reads from the per-RX state fields
    # added in Phase 3.A and writes through the per-target setters
    # from Phase 3.C so signals fire correctly and panels rebind.
    def vfo_a_to_b(self) -> None:
        """Copy VFO A (RX1) state onto VFO B (RX2).

        When ``rx2_enabled``: full state copy (freq + mode + RX BW
        for the destination mode).  When ``rx2_enabled`` is False:
        freq-only copy (VFO B is just a "shadow" freq for SPLIT TX).
        """
        a_freq = int(self._freq_hz)
        self.set_rx2_freq_hz(a_freq)
        if self._dispatch_state.rx2_enabled:
            a_mode = self._mode
            self.set_mode(a_mode, target_rx=2)
            a_bw = self.rx_bw_for(a_mode)
            self.set_rx_bw(a_mode, a_bw, target_rx=2)

    def vfo_b_to_a(self) -> None:
        """Copy VFO B (RX2) state onto VFO A (RX1).  Mirror of
        ``vfo_a_to_b`` -- full state when ``rx2_enabled``, freq-only
        otherwise."""
        b_freq = int(self._rx2_freq_hz)
        self.set_freq_hz(b_freq)
        if self._dispatch_state.rx2_enabled:
            b_mode = self._mode_rx2
            self.set_mode(b_mode, target_rx=0)
            b_bw = self._rx_bw_by_mode_rx2.get(b_mode, 2400)
            self.set_rx_bw(b_mode, b_bw, target_rx=0)

    def vfo_swap(self) -> None:
        """Swap VFO A and VFO B in one atomic update.  Full state
        when ``rx2_enabled``, freq-only otherwise."""
        a_freq = int(self._freq_hz)
        b_freq = int(self._rx2_freq_hz)
        if self._dispatch_state.rx2_enabled:
            a_mode = self._mode
            b_mode = self._mode_rx2
            a_bw = self.rx_bw_for(a_mode)
            b_bw = self._rx_bw_by_mode_rx2.get(b_mode, 2400)
            # Apply RX2 side first so RX1 reads can stay valid until
            # the moment of swap.
            self.set_mode(a_mode, target_rx=2)
            self.set_rx_bw(a_mode, a_bw, target_rx=2)
            self.set_mode(b_mode, target_rx=0)
            self.set_rx_bw(b_mode, b_bw, target_rx=0)
        # Freq swap last so any mode-change passband re-emits don't
        # alias the wrong VFO's freq.
        self.set_rx2_freq_hz(a_freq)
        self.set_freq_hz(b_freq)

    def read_rx2_iq_snapshot(self) -> np.ndarray:
        """Return a time-ordered copy of the most-recent RX2 IQ
        samples (Phase 1 bench-test accessor).

        Returns a ``(N,) complex64`` array, where N is the ring
        buffer size (16384).  Samples are reordered so index 0 is
        the OLDEST sample and index -1 is the NEWEST (i.e. the
        wraparound is fixed up so an FFT sees a contiguous
        time-domain window).

        Returns an empty array if no RX2 samples have arrived yet.
        Safe to call from the Qt main thread.
        """
        with self._rx2_iq_ring_lock:
            pos = self._rx2_iq_ring_pos
            ring = self._rx2_iq_ring
            if self._rx2_samples_received == 0:
                return np.zeros(0, dtype=np.complex64)
            # Reorder so oldest sample is first.
            return np.concatenate((ring[pos:], ring[:pos]))

    def read_rx2_diagnostics(self) -> dict:
        """Return a dict snapshot of Phase 1 RX2 bench-test counters.

        Keys:
            datagrams_total: int -- total UDP datagrams seen with
                RX2 samples since stream start (or last reset).
            samples_total: int -- total DDC1 complex samples seen.
            current_freq_hz: int -- current RX2 NCO frequency.
            iq_rate_hz: int -- current wire IQ rate (samples/sec
                per DDC).  Per CLAUDE.md §3.6 HL2 P1 caveat, this
                is shared across all DDCs; DDC1 streams at the
                same rate as DDC0.
        """
        return {
            "datagrams_total": int(self._rx2_datagrams_received),
            "samples_total": int(self._rx2_samples_received),
            "current_freq_hz": int(self._rx2_freq_hz),
            "iq_rate_hz": int(self._rate),
        }

    def _on_samples_main_thread(self, samples):
        if self._radio_debug:
            import time as _rdtime
            _rd_t0 = _rdtime.perf_counter()
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
        if self._radio_debug:
            _rd_dt_ms = (_rdtime.perf_counter() - _rd_t0) * 1000.0
            self._dbg_samples_calls += 1
            self._dbg_samples_total_ms += _rd_dt_ms
            if _rd_dt_ms > self._dbg_samples_max_ms:
                self._dbg_samples_max_ms = _rd_dt_ms

    # ── WDSP TX engine integration (v0.2 Phase 2 commits 7 + 8) ────────
    #
    # ``_tx_channel`` is a ``lyra.dsp.wdsp_tx_engine.TxChannel`` instance,
    # sibling of ``_wdsp_rx``.  Lazy-opened when the stream starts, closed
    # when the stream stops.  Channel index = 4 per consensus §2.2.
    #
    # ``_tx_dsp_worker`` is a ``lyra.dsp.tx_dsp_worker.TxDspWorker`` --
    # a dedicated DSP thread that absorbs the blocking cost of
    # ``TxChannel.process`` (~10 ms per in_size=512 block via WDSP
    # ``fexchange0``) so producer threads never block.
    #
    # Mic-source-aware dispatch wiring:
    #
    # * ``mic_source = "hl2_jack"``: HL2Stream's mic_callback fires per
    #   UDP datagram on the RX-loop thread with int16 BE-decoded mic
    #   samples.  Adapter ``_on_hl2_mic`` converts to float32 mono
    #   [-1, 1] and calls ``_tx_dsp_worker.submit`` (non-blocking).
    # * ``mic_source = "pc_soundcard"``: ``SoundDeviceMicSource`` fires
    #   per PortAudio callback with float32 mono samples already in
    #   the right format.  Consumer = ``_tx_dsp_worker.submit`` directly.
    #
    # Both producer paths cross only one thread boundary
    # (producer -> worker via ``queue.Queue``).  The worker thread
    # itself drains the queue, calls ``TxChannel.process``, and pushes
    # the resulting complex64 I/Q to ``HL2Stream.queue_tx_iq`` -- gated
    # by ``HL2Stream.inject_tx_iq`` which Phase 3 PTT state machine
    # flips on MOX=1 edge.
    #
    # During RX (``inject_tx_iq=False``, default), mic data flows
    # through the worker continuously, ``TxChannel.process`` runs to
    # keep the WDSP TXA chain in steady state, and the resulting I/Q
    # is silently dropped on the floor by the worker -- cheap, and
    # avoids ALC/leveler integrator surprises at the first PTT edge
    # after long idle.
    #
    # Replaced Phase 2 commit 7's direct-call dispatch (RX-loop thread
    # blocked for the duration of every ``fexchange0``, starving RX
    # audio + spectrum + telemetry) and Phase 2 commit 7.1's
    # ``LYRA_ENABLE_TX_DISPATCH`` env-var gate (which kept commit 7's
    # broken path in tree but disabled by default).

    def _open_tx_channel(self) -> None:
        """Lazy-construct the TX WDSP channel on stream start.

        Idempotent -- safe to call multiple times.  Mirrors the
        existing ``_open_wdsp_rx`` lifecycle pattern; opens with
        TxConfig defaults (in_size=512, in_rate=48000, dsp_rate=
        96000, out_rate=48000 -- HL2 EP2 audio path).

        Called by ``start()`` after the stream is established.
        ``_tx_dsp_worker`` start happens immediately after this
        method returns (when ``_tx_channel`` is not None).
        """
        if self._tx_channel is not None:
            return
        try:
            from lyra.dsp.wdsp_tx_engine import TxChannel, TxConfig
            self._tx_channel = TxChannel(channel=4, cfg=TxConfig())
            self._tx_channel.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[Radio] TxChannel open failed: {exc}")
            self._tx_channel = None

    def _close_tx_channel(self) -> None:
        """Tear down the TX channel on stream stop.  Idempotent.

        Caller MUST have already stopped + joined ``_tx_dsp_worker``
        (so no in-flight ``TxChannel.process`` calls remain).  See
        ``stop()`` for the full teardown order.
        """
        if self._tx_channel is None:
            return
        try:
            self._tx_channel.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[Radio] TxChannel close failed: {exc}")
        self._tx_channel = None

    def _on_hl2_mic(self, mic_int16: "np.ndarray", stats) -> None:
        """HL2 mic_callback adapter.

        Converts int16 BE-decoded mic samples (from EP6 byte slot
        24-25 via HL2Stream's mic_callback hook) to float32 mono
        [-1, 1] and submits to the TX DSP worker (non-blocking).

        Runs on the RX-loop thread.  Mic samples arrive in 38-sample
        blocks per UDP datagram at 48 kHz wire rate (HL2 codec).
        Submit is a ``queue.Queue.put_nowait`` -- O(1), no blocking;
        the worker thread does the heavy lifting on its own context.
        """
        if mic_int16.size == 0:
            return
        if self._tx_dsp_worker is None:
            return
        # int16 BE -> float32 [-1, 1].  Sample slot is 16-bit signed,
        # full-scale = ±32767.  Use 32768.0 divisor for symmetric
        # range; the 1-LSB asymmetry is below operator hearing
        # threshold.
        mic_f32 = (mic_int16.astype("float32") / 32768.0)
        self._tx_dsp_worker.submit(mic_f32)

    def _wire_mic_source(self) -> None:
        """Wire (or rewire) the active mic-source path to the TX worker.

        Called when mic_source changes (via ``set_mic_source``) or when
        the stream starts.  Idempotent: re-wiring with the same source
        is a no-op at the wire-level (just clears + re-sets the
        callbacks).

        Routing matrix:

        * ``source = "hl2_jack"``:
          - ``HL2Stream.register_mic_consumer(_on_hl2_mic)``
          - PC mic source stopped (handled by ``set_mic_source``
            tear-down)
        * ``source = "pc_soundcard"``:
          - ``HL2Stream.register_mic_consumer(None)`` -- clears HL2 path
          - ``PcMicSource.start(_tx_dsp_worker.submit)``

        Both paths submit to the same TX worker, which is the single
        threading boundary between producers and the WDSP TXA chain.
        """
        if self._stream is None:
            return
        source = self._mic_source
        if source == "hl2_jack":
            self._stream.register_mic_consumer(self._on_hl2_mic)
            return
        # source == "pc_soundcard" -- the operator's hardware choice
        # stands.  Standard HL2 operators (no AK4951 codec) MUST be
        # here; we never silently fall back to hl2_jack on failure.
        self._stream.register_mic_consumer(None)
        if (self._pc_mic_source is None
                or self._pc_mic_source.is_running
                or self._tx_dsp_worker is None):
            return
        try:
            self._pc_mic_source.start(self._tx_dsp_worker.submit)
            # Success: reset the log-once latch so a future failure
            # (e.g., USB headset unplugged mid-session) gets a fresh
            # toast.
            self._pc_mic_failure_logged = False
        except Exception as exc:  # noqa: BLE001
            # Log ONCE per session per device config -- don't flood
            # the console on every stop/start cycle.  Operator sees
            # one toast on first failure; silent retries afterward
            # let hot-plug recovery work without status-bar churn.
            # Flag is reset to False on successful start (above) and
            # in set_pc_mic_device / set_pc_mic_channel so a config
            # change re-arms the log path.
            if not self._pc_mic_failure_logged:
                self.status_message.emit(
                    f"PC mic unavailable: {exc}. "
                    f"Check Settings -> Audio -> Mic input.",
                    8000,
                )
                print(f"[Radio] PC mic start failed: {exc}")
                self._pc_mic_failure_logged = True

    # ── WDSP RX engine integration (v0.0.9.6) ─────────────────────────
    #
    # ``_wdsp_rx`` is a `lyra.dsp.wdsp_engine.RxChannel` instance backed by
    # the bundled wdsp.dll. When active it replaces the entire Python DSP
    # chain (decim → notch → demod → NR → ANF → AGC → leveler → binaural)
    # with a single ``rx.process(iq)`` call that returns 48 kHz stereo
    # audio. Lyra applies volume / mute / TCI tap on top.
    #
    # Lifecycle:
    #   * Constructed in ``__init__`` (WDSP is the only DSP path
    #     as of v0.0.9.6's cleanup arc — see CLAUDE.md §14.9).
    #   * Re-opened on rate change (``set_in_rate`` calls ``_open_wdsp_rx``).
    #   * Mode / filter / AGC pushed via Radio's UI setters.

    def _open_wdsp_rx(self, in_rate: int) -> None:
        """Open (or re-open) the WDSP RX channel for ``in_rate``.

        Called from ``__init__`` and on ``set_in_rate`` rate changes.
        Closes any existing channel before opening so we never leak a
        stale WDSP channel across rate changes.

        §14.6 v0.0.9.9 lock fix: the teardown step (close + None
        assignment) runs under ``_iq_capture_lock``.  Without this,
        the worker thread (running ``_do_demod_wdsp``) could call
        ``self._wdsp_rx.process(iq)`` between the close() and the
        ``= None`` assignment, which is a cffi call into a freed
        C-side channel handle → Windows access violation → silent
        process death (no Python traceback because Python isn't on
        the call stack).  The ``_do_demod_wdsp`` companion fix
        takes the same lock around its WDSP process() call, so
        worker and main thread cannot collide on ``_wdsp_rx``.
        RLock allows the inner ``with self._iq_capture_lock:``
        block (engine swap) and the recursive
        ``set_nr_use_captured_profile(False)`` call below to
        re-enter from this same thread.
        """
        from lyra.dsp.wdsp_engine import RxChannel, RxConfig
        with self._iq_capture_lock:
            # Tear down the old ones if any.  Both RX1 and RX2 are
            # constructed and torn down together so an in-flight
            # rate change can't leave a half-state where one channel
            # is at the old rate and the other at the new.
            if self._wdsp_rx is not None:
                try:
                    self._wdsp_rx.close()
                except Exception as exc:
                    print(f"[Radio] WDSP rx close error: {exc}")
                self._wdsp_rx = None
            if self._wdsp_rx2 is not None:
                try:
                    self._wdsp_rx2.close()
                except Exception as exc:
                    print(f"[Radio] WDSP rx2 close error: {exc}")
                self._wdsp_rx2 = None
        # Pick an in_size that keeps the per-call audio block within
        # Lyra's existing 512-sample audio cadence. WDSP returns
        # in_size * out_rate / in_rate audio frames per call:
        #
        #   192 kHz IQ + in_size=1024 -> 256 audio frames (5.33 ms)
        #    96 kHz IQ + in_size=512  -> 256 audio frames (5.33 ms)
        #    48 kHz IQ + in_size=256  -> 256 audio frames (5.33 ms)
        #   384 kHz IQ + in_size=2048 -> 256 audio frames (5.33 ms)
        #
        # Constant 5.33 ms audio block latency across the supported IQ
        # rates. dsp_size always 4096 (Thetis default).
        if in_rate >= 48000:
            in_size = max(256, int(in_rate * 256 / 48000))
        else:
            in_size = 256
        cfg = RxConfig(
            in_size=in_size,
            dsp_size=4096,
            in_rate=int(in_rate),
            dsp_rate=48000,
            out_rate=48000,
        )
        self._wdsp_rx = RxChannel(channel=0, cfg=cfg)
        self._wdsp_rx_in_rate = int(in_rate)
        # Phase 2 v0.1 (2026-05-11): construct the RX2 WDSP channel
        # at the same in_rate / in_size / out_rate so audio output
        # block alignment matches RX1's (sums cleanly in
        # ``_do_demod_wdsp_dual`` for stereo combine).  HL2 nddc=4
        # delivers both DDCs at the same wire rate per CLAUDE.md §3.6
        # so no per-channel rate divergence is possible on this
        # hardware family.  ANAN P2 (v0.4) would set RX2's in_rate
        # separately; that's a multi-radio refactor concern.
        self._wdsp_rx2 = RxChannel(channel=2, cfg=cfg)

        # Captured-profile IQ-domain engine (§14.6, v0.0.9.9).
        # Tied to the WDSP channel's lifetime — same in_rate, same
        # close+reopen cadence on rate change.  Profiles are
        # rate-specific; if a profile was loaded from disk and the
        # rate matches, callers can reload it via load_profile()
        # after this method returns.  We don't auto-reload here
        # because the profile name+folder lives at the Radio
        # facade level, not in the WDSP-channel layer.
        #
        # Build the new engine OUTSIDE the lock (construction
        # touches numpy/FFT setup; no shared state mutation
        # until we assign it), then take the lock briefly to
        # swap.  This minimizes worker-thread blocking on rate
        # change.
        try:
            new_engine = CapturedProfileIQ(
                rate_hz=int(in_rate),
                fft_size=self._iq_capture_fft_size,
                gain_smoothing=self._iq_capture_gain_smoothing,
            )
        except Exception as exc:
            print(f"[Radio] iq_capture init: {exc}")
            new_engine = None

        with self._iq_capture_lock:
            had_iq_capture = self._iq_capture is not None
            self._iq_capture = new_engine

        # On a recreate (rate change), clear the active-profile
        # name + meta + emit the changed signal so the DSP+Audio
        # panel badge stops showing a "loaded" profile that the
        # new engine doesn't actually have.  The persisted-via-
        # QSettings profile name on disk is preserved (we don't
        # call _save_active_profile_name_setting("") here) so the
        # operator could switch back to the original rate and
        # reload manually from the manager dialog.  We don't
        # auto-reload because the rate-change path may also be
        # the wrong band/mode for the persisted profile — let
        # the operator decide.  Signal emit is outside the lock.
        #
        # ALSO flip the source toggle OFF so the operator's UI
        # mental model stays consistent: the new engine has no
        # profile, so "use captured" can't actually do anything.
        # Leaving the toggle ON would show a checked checkbox
        # while the apply path is silently a passthrough — exactly
        # the kind of stale-state confusion the Phase 4 joint
        # audit flagged as P1.  Operator re-flips the toggle ON
        # after manually reloading a profile from the manager.
        if had_iq_capture and self._active_captured_profile_name:
            self._active_captured_profile_name = ""
            self._active_captured_profile_meta = None
            try:
                self.noise_active_profile_changed.emit("")
            except Exception:
                pass
            if self._nr_use_captured_profile:
                # Recursive call goes through the full setter path
                # (UI signal emit + QSettings persistence).  RLock
                # allows the re-entry from this thread.  Also
                # resets apply streaming state — redundant here
                # since the engine is brand new, but harmless.
                self.set_nr_use_captured_profile(False)

        # Push current operator state into the new channel.
        try:
            self._wdsp_rx.set_mode(self._wdsp_mode_for(self._mode))
            low, high = self._wdsp_filter_for(self._mode)
            self._wdsp_rx.set_filter(low, high)
            self._wdsp_rx.set_agc(self._wdsp_agc_for(self._agc_profile))
            # Phase 6.A3: push AGC parameters that wcpAGC.c's
            # create_wcpagc (RXA.c lines 353-378) sets at engine
            # creation time but Lyra had been leaving at the
            # create-time defaults.
            #
            # Why this matters: wcpAGC.c's defaults assume signals
            # at typical-Thetis-tuned levels.  When Lyra's audio
            # path delivers signals at different levels (we have a
            # different gain stage chain — see CLAUDE.md §13.2),
            # the AGC may stay parked at max_gain (=10000 linear
            # / 80 dB) and never engage on real signals, which is
            # the operator-reported "AGC profiles all sound the
            # same / gain meter doesn't move" symptom.
            #
            # We push only what's NOT already covered by
            # SetRXAAGCMode.  Per wcpAGC.c::SetRXAAGCMode (lines
            # 384-407), Mode already sets hangtime + tau_decay
            # per profile and recomputes coefficients via
            # loadWcpAGC, so per-profile Decay/Hang pushes are
            # redundant.
            #
            # Threshold push: writes the same engine field
            # (max_gain) that SetRXAAGCTop writes — calling both
            # would have the second clobber the first.  We push
            # Threshold here (Slope-aware computation) and DO
            # NOT push Top.  See wcpAGC.c::SetRXAAGCThresh for
            # the math.
            try:
                # Slope — drives ``var_gain`` via
                # ``var_gain = pow(10, slope / 200)``.
                # SetRXAAGCThresh's noise_offset calculation uses
                # ``var_gain`` to compute ``max_gain``.
                #
                # WDSP create-time default is ``var_gain = 1.5``,
                # which is what other major HF SDR applications use
                # (and what produces the soft-knee AGC character
                # operators expect from the FAST/MED/SLOW/LONG
                # presets).  ``var_gain = 1.5`` corresponds to
                # ``slope = 200 * log10(1.5) ≈ 35``.  Lyra was
                # passing slope=0 (var_gain=1.0, hard-knee) before
                # the v0.0.9.8.1 polish — flatter, more limiter-
                # like character.  Switching to 35 matches the
                # canonical WDSP / industry convention.
                #
                # Note SetRXAAGCSlope's parameter is `int slope`
                # (0.1 dB units per WDSP source); the v0.0.9.8 cffi
                # binding fix made the int-vs-double calling
                # convention right — see lyra/dsp/wdsp_native.py.
                self._wdsp_rx.set_agc_slope(35)
                # WDSP AGC "threshold" parameter — dBFS-domain
                # value that, with the bandpass-width noise_offset
                # WDSP computes internally, sets ``max_gain`` via
                #
                #     max_gain = out_target /
                #                (var_gain * 10^((thresh + noise_offset)/20))
                #
                # This is conceptually a "noise floor reference"
                # for the AGC to boost weak signals up FROM, NOT
                # an output-level target.  Operator-typical values
                # are around -100 to -130 dBFS (similar to Thetis
                # /PowerSDR/EESDR's AGC threshold sliders).  Lyra's
                # ``_agc_target`` field is a legacy 0..1 audio-level
                # target from the pre-WDSP Python AGC engine; it
                # is NOT the same concept and must NOT be
                # converted to a WDSP threshold (doing so makes
                # max_gain ≈ 1.0 → AGC clamped → very quiet audio,
                # operator-reported during the v0.0.9.8 fix
                # iteration).
                #
                # Use a Thetis-default-ish -100 dBFS here so AGC
                # has ~70 dB of headroom to boost weak signals
                # AND so the per-mode tau_decay differences (Fast
                # 50ms / Med 250ms / Slow 500ms / Long 2000ms set
                # by SetRXAAGCMode) are audible because the gain
                # is actually free to move.  When Lyra grows a
                # proper Settings → DSP → AGC threshold slider
                # (dBFS-domain), it will replace this constant.
                self._wdsp_rx.set_agc_threshold(
                    float(self._agc_target), 4096, in_rate)
            except Exception as exc:
                print(f"[Radio] WDSP AGC init-state push: {exc}")
            # AF Gain → WDSP PanelGain1 (Thetis-style pre-AGC makeup
            # gain).  Phase 6.A1 (v0.0.9.6): wired up after Phase 6.A
            # surfaced that the operator's AF slider was silently
            # inert in WDSP mode (the legacy _apply_agc_and_volume
            # was the only consumer and had been orphan since
            # Phase 4).  WDSP's SetRXAPanelGain1 takes a LINEAR
            # multiplier; we convert from the operator's integer dB.
            self._wdsp_rx.set_panel_gain(self.af_gain_linear)
            # Patch panel binaural mode — set to FALSE (mono on both
            # channels).  Without this call, WDSP defaults to copy=0
            # (L=I, R=Q at panel output), which works for SSB but
            # silences the right channel for AM/FM/DSB whenever EMNR
            # is enabled (EMNR zeroes Q on output, and the post-EMNR
            # BP1 has a symmetric passband for those modes so it
            # can't reconstruct Q).  set_panel_binaural(False) sets
            # panel.copy=1 so the panel always copies I to Q at its
            # output, giving mono on both channels regardless of
            # upstream Q-zeroing.  Same call Thetis makes at channel
            # init.  Lyra's BIN feature is implemented as a Python
            # post-WDSP BinauralFilter in BinauralFilter, so we don't
            # need WDSP's own binaural mode.  See CLAUDE.md §14.10.
            self._wdsp_rx.set_panel_binaural(False)
            # NB + manual notches: only meaningful after init_blankers /
            # the notchdb exists.  RxChannel.__init__ does init_blankers
            # for us; the notchdb is created with the channel.
            self._push_wdsp_nb_state()
            self._wdsp_rx.set_notch_tune_frequency(float(self._freq_hz))
            self._push_wdsp_notches()
            # APF (CW peaking) — mode-gated on/off.  Push current state
            # so a freshly-opened channel inherits the operator's
            # toggle (which gets activated only in CWU/CWL anyway).
            self._push_wdsp_apf_state()
            # All-mode squelch — route to the right WDSP module
            # (FM SQ / AM SQ / SSQL) based on current mode and apply
            # the operator's persisted enable + threshold.
            self._push_wdsp_squelch_state()
            # Push initial NR state — sets gain_method per backend
            # (NR1=MMSE-LSA, NR2=Wiener) and emnr/anr run flags.
            self._push_wdsp_nr_state()
            # Push the operator's persisted LMS strength so the
            # WDSP ANR step size matches the slider's saved value
            # from session start (otherwise WDSP runs at its
            # internal default 0.0001 until operator touches the
            # slider).
            try:
                lms_strength = getattr(
                    self._rx_channel, "lms_strength", 0.5)
                self.set_lms_strength(float(lms_strength))
            except Exception as exc:
                print(f"[Radio] WDSP LMS initial-strength push: {exc}")
            # Phase 6.A4 — push the remaining Thetis-init-pattern
            # parameters that wcpAGC.c / anf.c / amsq.c create-time
            # defaults leave at values Lyra would prefer to override.
            #
            # ANF Vals: anf.c::create_anf defaults are n_taps=64,
            # delay=16, two_mu=1e-4, gamma=0.001.  The gamma=0.001
            # default is too low — anf.c's own comment in
            # SetRXAANFVals body suggests "try gamma = 0.10".  We
            # push the operator's persisted μ value as two_mu and
            # use 0.10 for gamma.  Without this, ANF runs with the
            # too-low leakage and adapts more aggressively than
            # operators expect.
            try:
                two_mu = float(getattr(
                    self._rx_channel, "anf_mu", 1.5e-4))
                self._wdsp_rx.set_anf_vals(
                    taps=64, delay=16,
                    gain=two_mu, leakage=0.10,
                )
            except Exception as exc:
                print(f"[Radio] WDSP ANF initial-vals push: {exc}")
            # AM squelch max tail — amsq.c default leaves it at
            # the create-time value (1.5 s per RXA.c).  Lyra's
            # AM SQ behavior felt "too long" on field testing;
            # 0.5 s is a more operator-friendly tail without
            # cutting off the natural carrier-decay sound on
            # weak AM signals.
            try:
                self._wdsp_rx.set_am_squelch_max_tail(0.5)
            except Exception as exc:
                print(f"[Radio] WDSP AM SQ tail init: {exc}")

            # Phase 3.D hotfix v0.1 (2026-05-12) — initial RX1 pan
            # tracks current SUB state.  When SUB is OFF, pan=0.5
            # (center, full output on both L+R channels per WDSP's
            # sin-π pan curve).  When SUB is ON, pan=0.0 (hard-left
            # for the stereo split with RX2 at pan=1.0 hard-right).
            # Live updates from operator SUB toggles flow through
            # ``_apply_rx2_routing`` -- see ``set_rx2_enabled``.
            try:
                self._apply_rx2_routing()
            except Exception as exc:
                print(f"[Radio] WDSP pan init: {exc}")
        except Exception as exc:
            print(f"[Radio] WDSP rx initial-state push error: {exc}")

        # ── Phase 2 v0.1: push initial state into RX2 ───────────────
        # RX2 mirrors RX1's mode / filter / AGC / panel_gain.  Per-
        # module DSP state (NR, NB, ANF, manual notches, APF,
        # squelch, LMS, AM SQ tail) is left at WDSP defaults (off)
        # in Phase 2 -- Phase 3 wires per-RX UI for these.
        #
        # Mid-session operator state changes (set_mode, set_filter,
        # set_agc_profile, set_af_gain_db) fan out to BOTH channels
        # via the corresponding setter call sites (see the per-setter
        # ``# Phase 2: also push to RX2`` lines).  Until Phase 3
        # gives RX2 its own UI surface, RX2 is functionally a clone
        # of RX1 with a different NCO frequency.
        #
        # Phase 3.A (2026-05-12): read the RX2 state from the per-RX
        # state fields (``_mode_rx2``, ``_agc_profile_rx2``, etc.)
        # rather than from RX1's fields directly.  In Phase 3.A the
        # values are identical (the fan-out setters keep them in
        # lock-step), but reading from the per-RX fields makes the
        # state-routing explicit + sets up Phase 3.B to swap the
        # fan-out for target_rx semantics.
        #
        # TODO Phase 3.B: ``_wdsp_filter_for`` currently reads
        # ``self._rx_bw_by_mode`` (RX1's dict).  When per-RX BW
        # divergence lands, refactor it to take a target_rx
        # parameter so RX2's push reads ``_rx_bw_by_mode_rx2``.
        # Safe in Phase 3.A because the dicts are kept identical.
        try:
            af_gain_rx2_linear = 10.0 ** (self._af_gain_db_rx2 / 20.0)
            self._wdsp_rx2.set_mode(self._wdsp_mode_for(self._mode_rx2))
            low, high = self._wdsp_filter_for(self._mode_rx2)
            self._wdsp_rx2.set_filter(low, high)
            self._wdsp_rx2.set_agc(self._wdsp_agc_for(self._agc_profile_rx2))
            try:
                self._wdsp_rx2.set_agc_slope(35)
                self._wdsp_rx2.set_agc_threshold(
                    float(self._agc_target_rx2), 4096, in_rate)
            except Exception as exc:
                print(f"[Radio] WDSP RX2 AGC init-state push: {exc}")
            self._wdsp_rx2.set_panel_gain(af_gain_rx2_linear)
            self._wdsp_rx2.set_panel_binaural(False)
            # RX2 pan tracks SUB state -- see ``_apply_rx2_routing``.
            # Phase 3.D hotfix v0.1 (2026-05-12): conditional on
            # rx2_enabled rather than always hard-right.
            self._apply_rx2_routing()
        except Exception as exc:
            print(f"[Radio] WDSP RX2 initial-state push error: {exc}")

        try:
            self._wdsp_rx.start()
        except Exception as exc:
            print(f"[Radio] WDSP rx start error: {exc}")
        try:
            self._wdsp_rx2.start()
        except Exception as exc:
            print(f"[Radio] WDSP rx2 start error: {exc}")

    def _wdsp_mode_for(self, mode: str) -> str:
        """Map Lyra's mode string to a WDSP mode name."""
        # Lyra modes: USB, LSB, AM, FM, CWU, CWL, DSB, DIGU, DIGL, SAM, DRM, SPEC
        # WDSP rxaMode: USB, LSB, AM, FM, CWU, CWL, DSB, DIGU, DIGL, SAM, DRM, SPEC
        # 1:1 mapping today.
        return mode

    def _wdsp_filter_for(
        self, mode: str, target_rx: Optional[int] = None,
    ) -> tuple[float, float]:
        """Translate Lyra's per-mode RX bandwidth into WDSP bandpass
        edges (low_hz, high_hz).

        Phase 3.C v0.1 (2026-05-12) added ``target_rx`` parameter:
        when ``target_rx == 2`` the lookup reads from
        ``self._rx_bw_by_mode_rx2`` (RX2's per-mode dict) so RX2's
        filter can diverge from RX1's.  None or 0 reads RX1's dict
        (default; preserves all existing call sites).  Resolves the
        Phase 3.A TODO marker.
        """
        if target_rx == 2:
            bw_dict = self._rx_bw_by_mode_rx2
        else:
            bw_dict = self._rx_bw_by_mode
        bw = int(bw_dict.get(mode, 2700))
        if mode in ("USB", "DIGU"):
            return (200.0, float(bw))
        if mode in ("LSB", "DIGL"):
            return (-float(bw), -200.0)
        if mode == "CWU":
            pitch = float(self._cw_pitch_hz)
            half = bw / 2.0
            return (pitch - half, pitch + half)
        if mode == "CWL":
            pitch = -float(self._cw_pitch_hz)
            half = bw / 2.0
            return (pitch - half, pitch + half)
        # AM / DSB / FM / SAM — symmetric around DC
        half = bw / 2.0
        return (-half, half)

    def _wdsp_agc_for(self, profile: str) -> str:
        """Map Lyra's AGC profile to a WDSP AGC mode name."""
        # Lyra: off / fast / med / slow / long / auto / custom
        # WDSP: FIXED / LONG / SLOW / MED / FAST / CUSTOM
        return {
            "off":    "FIXED",
            "fast":   "FAST",
            "med":    "MED",
            "slow":   "SLOW",
            "long":   "LONG",
            "auto":   "MED",       # auto rides MED today; auto-threshold tracking deferred
            "custom": "CUSTOM",
        }.get(profile.lower() if profile else "med", "MED")

    # NOTE: a previous Phase 6.A3 attempt added a per-profile
    # _WDSP_AGC_TIMINGS dict + _push_wdsp_agc_timings helper that
    # called SetRXAAGCDecay / SetRXAAGCHang explicitly on each
    # profile change.  Reading WDSP source (wcpAGC.c lines 384-407,
    # SetRXAAGCMode switch statement) confirmed those calls are
    # REDUNDANT — SetRXAAGCMode itself sets `hangtime` and
    # `tau_decay` per profile and calls loadWcpAGC to recompute
    # coefficients.  The redundant push was harmless but added
    # confusion; removed in the Phase 6.A3 fix-up.
    #
    # Real init-state pushes that ARE needed (to override
    # WDSP's wcpAGC.c create-time defaults set in RXA.c
    # create_wcpagc): SetRXAAGCThresh OR SetRXAAGCTop (NOT both —
    # they write the same engine field), and optionally
    # SetRXAAGCSlope.  See _open_wdsp_rx for the live-call sequence.

    # ── WDSP NB profile ↔ threshold mapping ───────────────────────
    #
    # Lyra's NB profile slider maps to a single WDSP NOB threshold
    # value.  Lower threshold → more aggressive blanking (more
    # noise samples cross the impulse-detection bar) at the cost of
    # more false positives chewing into wanted signal transients.
    # The numbers below are starting points calibrated against the
    # Thetis Console default of 3.3; field-tuning may move them.
    _WDSP_NB_THRESHOLDS = {
        "off":    None,
        "light":  10.0,
        "medium": 5.0,
        "heavy":  2.5,
        "custom": None,        # uses _rx_channel.nb_threshold instead
    }

    def _push_wdsp_nb_state(self) -> None:
        """Push the current Lyra NB profile to the WDSP NOB blanker.

        The legacy ``_rx_channel`` is the source of truth for the
        current profile + custom threshold.  We mirror its state
        into the DLL's NOB module via ``set_nob`` + ``set_nob_threshold``.
        """
        if self._wdsp_rx is None:
            return
        profile = (self._rx_channel.nb_profile or "off").lower()
        if profile == "off":
            try:
                self._wdsp_rx.set_nob(False)
                self._wdsp_rx.set_anb(False)
            except Exception as exc:
                print(f"[Radio] WDSP NB push (off): {exc}")
            return
        if profile == "custom":
            thresh = float(self._rx_channel.nb_threshold)
        else:
            thresh = self._WDSP_NB_THRESHOLDS.get(profile, 5.0) or 5.0
        try:
            self._wdsp_rx.set_nob_threshold(thresh)
            self._wdsp_rx.set_nob(True)
            # ANB stays off by default — NOB alone covers the
            # narrowband impulse-noise case Lyra's UI is targeting.
            # Heavy operator demand for broadband blanking would
            # justify a separate "NB2" UI option that toggles ANB.
            self._wdsp_rx.set_anb(False)
        except Exception as exc:
            print(f"[Radio] WDSP NB push: {exc}")

    def _push_wdsp_notches(self) -> None:
        """Push Lyra's manual notch list to WDSP's RX notch database.

        Tile changes (active flag, width, depth, cascade) all trigger
        a fresh push.  Inactive notches are still added to the
        database with active=0 — that way operator-toggled visibility
        survives the round-trip.
        """
        if self._wdsp_rx is None:
            return
        # Init-order guard: _open_wdsp_rx runs before self._notches /
        # self._notch_enabled are populated in __init__.  An empty
        # push at that point is harmless (no operator notches yet),
        # but the attribute lookup would AttributeError.
        notches_list = getattr(self, "_notches", None)
        notch_enabled = getattr(self, "_notch_enabled", False)
        if notches_list is None:
            return
        # WDSP's NotchDB takes ABSOLUTE RF center frequencies.  It
        # subtracts ``tunefreq`` internally, which we set on every
        # VFO change via set_notch_tune_frequency.  Width is positive.
        # Lyra's "deep + cascade" model collapses onto WDSP's single
        # notch — a deep+cascade notch becomes one wider+lower
        # notch by approximation.  Operators who care about depth
        # tune the width; the cascade integer doesn't have a direct
        # WDSP analog.  Future: stack multiple WDSP notches for
        # cascade > 1.
        notches: list[tuple[float, float, bool]] = []
        for n in notches_list:
            try:
                notches.append((
                    float(n.abs_freq_hz),
                    float(max(1.0, n.width_hz)),
                    bool(n.active),
                ))
            except Exception:
                continue
        try:
            self._wdsp_rx.set_notches(notches, master_run=notch_enabled)
        except Exception as exc:
            print(f"[Radio] WDSP notch push: {exc}")

    def _do_demod(self, iq):
        """Route IQ through the WDSP RX engine into the audio sink.

        WDSP is the only DSP path (Phase 3 of legacy cleanup, 2026-05-08).
        ``_do_demod_wdsp`` handles the full chain: WDSP fexchange0 →
        Lyra volume/mute → capture-feed → BIN post-processor → sink →
        TCI tap.  WDSP itself runs in its own C thread inside the DLL,
        so the heavy DSP work is GIL-free even though this function is
        called from a Python thread.
        """
        mode = self._mode
        if mode == "Off":
            return
        if mode == "Tone":
            self._emit_tone(len(iq))
            return
        # `_wdsp_rx is None` should never occur in practice: __init__
        # raises a clear RuntimeError if the WDSP DLL fails to load.
        # The defensive check here protects against transient teardown
        # windows (e.g., between channel close + reopen on rate change).
        if self._wdsp_rx is not None:
            self._do_demod_wdsp(iq)

    # NOTE: an earlier Python-side spectrum-SNR squelch gate lived
    # here (and even earlier, an audio-domain RMS gate).  Both were
    # superseded 2026-05-07 by WDSP's native SSQL — see
    # `_push_wdsp_squelch_state` and CLAUDE.md §14.8 for the full
    # design history.  WDSP SSQL operates pre-AGC inside the RXA
    # pipeline, exactly where Pratt designed it; no Python-side
    # gate can match that integration.

    def _do_demod_wdsp(self, iq):
        """WDSP-engine RX audio path (v0.0.9.6).

        Pushes IQ through ``_wdsp_rx`` and writes the resulting 48 kHz
        stereo audio to the audio sink. WDSP handles the entire RX chain
        internally — decim, notches, demod, AGC, NR, ANF, output filter —
        in its own C thread (no GIL contention with Python's writer / sink
        threads). Lyra applies output-stage volume / mute and the TCI tap.

        Note on the HL2 spectrum mirror:

        Lyra's pure-Python demods (lyra/dsp/demod.py:42-47) compensate for
        HL2's mirrored baseband by building flipped per-mode bandpass
        phasors.  WDSP — which Thetis also uses with HL2 — doesn't need
        any compensation because WDSP's INTERNAL filter convention is
        also flipped: WDSP's USB filter at (+200,+3100) actually selects
        negative-baseband content, and WDSP's LSB filter at (-3100,-200)
        selects positive-baseband content.  The two mirrors (HL2's and
        WDSP's) cancel out, so we hand WDSP raw HL2 IQ untouched and
        get correct sideband selection.  Confirmed empirically with a
        synthetic tone test: a +1500 Hz baseband tone demodulates clean
        in WDSP LSB mode, a -1500 Hz tone demodulates clean in USB mode.

        ``_wdsp_rx.process()`` accepts variable-length IQ and buffers
        internally to whole ``in_size`` blocks; returns 0+ complete output
        blocks per call. Empty result = no full block ready yet, which is
        expected on the first few calls after a freq / mode change.
        """
        # Early-out if channel has been torn down (e.g., between
        # the worker's last block and a rate change on the main
        # thread).  This is the "outside the lock" guard; the
        # inside-the-lock re-check below is the load-bearing one
        # (worker can race past this top-of-method check and
        # then find the channel closed inside the lock window).
        if self._wdsp_rx is None:
            return

        # ── §14.6 pre-WDSP IQ taps for captured-profile ──
        # IQ-domain capture (Phase 3) and apply (Phase 4) both run
        # BEFORE WDSP's RXA chain, sidestepping the AGC-mismatch
        # that broke three rounds of post-WDSP audio-domain
        # attempts in v0.0.9.6 (see CLAUDE.md §14.6).
        #
        # Order matters:
        #   1. CAPTURE accumulates from RAW iq (so the profile
        #      reflects the band's actual noise spectrum, NOT a
        #      cleaned version).
        #   2. APPLY runs Wiener-from-profile on the same raw iq
        #      and produces ``iq_for_wdsp`` for downstream demod.
        # The two passes can run simultaneously (operator
        # re-capturing while listening to the previous profile)
        # — the engine has separate input buffers for each path
        # so they don't collide.  See Phase 2 review test #10
        # for verification.
        #
        # Lock the engine for the entire window so UI-thread
        # calls (begin_capture / cancel / load / clear) can't
        # mutate engine state mid-call.  Emit the capture-done
        # signal OUTSIDE the lock so signal handlers don't run
        # under the engine lock (PySide6 emit is fast but slot
        # invocation may be DirectConnection on the same thread).
        fired_done = False
        iq_for_wdsp = iq
        audio = None
        with self._iq_capture_lock:
            # ── §14.6 v0.0.9.9 lock fix (companion to _open_wdsp_rx) ──
            # Re-check ``_wdsp_rx is None`` INSIDE the lock.  The
            # top-of-method check passed when we entered, but a
            # rate change on the main thread could have set it to
            # None between then and now.  With main thread holding
            # this same lock during close+None (see _open_wdsp_rx
            # step 1), this re-check is the worker's safe gate:
            # if None, rate change is in flight, drop the block.
            if self._wdsp_rx is None:
                return
            if self._iq_capture is not None:
                # Capture pass — no-op unless state == "capturing".
                prev_state = self._iq_capture.state
                try:
                    self._iq_capture.accumulate(iq)
                except Exception as exc:
                    print(f"[Radio] IQ capture accumulate: {exc}")
                else:
                    # Detect capture-done state transition.  This
                    # method runs ~188 times/sec at 192 kHz IQ +
                    # in_size=1024, so the latency between
                    # capture-done and signal-emit is well under
                    # 6 ms (replaces the nr.py done-callback the
                    # legacy path used).
                    if (prev_state == "capturing"
                            and self._iq_capture.state == "ready"):
                        fired_done = True

                # Apply pass — runs only when the operator's
                # source toggle is on AND a profile is loaded.
                # ``apply()`` returns variable-length output:
                # zero on the first call after profile load
                # (algorithm warmup, ~one frame fills the
                # overlap buffer); ~len(iq) samples per call in
                # steady state.  WDSP buffers internally and is
                # fine with variable-length input — total bytes
                # balance over time, with a constant
                # ``fft_size - hop`` sample pipeline delay
                # (~5.3 ms at 192 kHz IQ + fft_size=2048).
                if (self._nr_use_captured_profile
                        and self._iq_capture.has_profile):
                    try:
                        iq_for_wdsp = self._iq_capture.apply(iq)
                    except Exception as exc:
                        print(f"[Radio] IQ apply: {exc}")
                        # Fall back to raw IQ on error so audio
                        # keeps flowing — operator gets logspam
                        # but no audio gap.
                        iq_for_wdsp = iq

            # WDSP process — UNDER THE SAME LOCK so main thread
            # can't close+null _wdsp_rx while we're mid-process().
            # Cost: lock is held for ~5 ms (one WDSP block at
            # 192 kHz IQ in_size=1024) — operator-imperceptible.
            # Main thread rate change waits at most one block
            # before the swap can begin.
            try:
                audio = self._wdsp_rx.process(iq_for_wdsp)
            except Exception as exc:
                print(f"[Radio] WDSP rx process error: {exc}")
                audio = None

        # Signal emit OUTSIDE the lock so slot handlers don't run
        # under the engine lock (PySide6 emit may be DirectConnection
        # on the same thread).
        if fired_done:
            try:
                # Signal is declared Signal(str); the legacy
                # "verdict" arg is always "" post-v0.0.9.5
                # (smart-guard removed).  See declaration at the
                # top of Radio class + the matching emit in
                # _on_nr_capture_done.
                self.noise_capture_done.emit("")
            except Exception:
                pass

        if audio is None or audio.size == 0:
            return

        # Squelch is handled inside WDSP (FM SQ / AM SQ / SSQL all-
        # mode) via `_push_wdsp_squelch_state` — no Python-side audio
        # gating needed here.  WDSP's SSQL operates pre-AGC on the
        # IQ envelope, so AGC compression doesn't blind it the way
        # earlier audio-domain RMS / spectrum-SNR Python gates did.
        # See `set_squelch_enabled` and `_push_wdsp_squelch_state`.

        # Output-stage volume + mute. WDSP's own PanelGain1 handles the
        # operator-level gain; volume and mute live above the engine so
        # quick mute/unmute transitions don't ride through WDSP's slew
        # envelope (which is sized for mode/freq transitions, not for
        # operator finger-on-mute).
        if self._muted:
            audio = audio * 0.0
        else:
            v = float(self._volume)
            if v != 1.0:
                audio = audio * np.float32(v)

        # ── Phase 3.D safety clamp (2026-05-12) ─────────────────
        # Hard-clamp the post-volume audio to [-1.0, +1.0] so any
        # upstream gain anomaly (AGC misconfig, AF gain extreme,
        # WDSP transient) cannot send unbounded floats to the
        # codec.  This is purely a safety net to protect the
        # operator's hardware; in normal operation the audio is
        # already inside the unit range from WDSP's AGC.
        np.clip(audio, -1.0, 1.0, out=audio)

        # ── §14.6 IQ-domain captured-profile path (LIVE) ─────────
        # Capture and apply both happen pre-WDSP in the IQ domain
        # at the top of this method (search for "Phase 3" /
        # "Phase 4" tags above the lock-held block).  The legacy
        # post-WDSP audio capture-feed that used to live HERE
        # (feeding nr.SpectralSubtractionNR.process(enabled=False)
        # with WDSP's output audio) was removed in Phase 3.  See
        # CLAUDE.md §14.6 for the full architectural rationale.
        # WDSP returns mono-equivalent stereo (L == R) so we hand the
        # left channel to BinauralFilter, which on enabled returns a
        # genuinely-different (N, 2) Hilbert-pair stereo that the
        # operator hears as widened.  CPU is negligible (~63-tap FIR
        # at 48 kHz) and Python-side work runs after WDSP's hot path
        # so it doesn't compete for the GIL with the EP2 writer.
        # When BIN is disabled the filter returns mono untouched and
        # we keep WDSP's stereo as-is.
        if self._bin_enabled and self._binaural.enabled:
            try:
                mono_in = audio[:, 0] if audio.ndim == 2 else audio
                bin_out = self._binaural.process(mono_in)
                if bin_out.ndim == 2 and bin_out.shape[1] == 2:
                    audio = bin_out.astype(np.float32, copy=False)
            except Exception as exc:
                print(f"[Radio] WDSP BIN error: {exc}")

        try:
            self._audio_sink.write(audio)
        except Exception as exc:
            print(f"[Radio] WDSP audio sink error: {exc}")
        # TCI audio tap — same contract as the legacy path so any
        # subscribed TCI client gets the exact audio the operator hears.
        try:
            self.audio_for_tci_emit.emit(audio)
        except Exception:
            pass
        # AGC gain readout — WDSP's AGC runs inside the engine.
        # Throttled to one update per several blocks (~6 Hz) since
        # the meter repaints at that rate anyway and per-block reads
        # add a critical-section take per call.
        try:
            self._wdsp_agc_meter_skip = (
                getattr(self, "_wdsp_agc_meter_skip", 0) + 1)
            if self._wdsp_agc_meter_skip >= 8:
                self._wdsp_agc_meter_skip = 0
                self.agc_action_db.emit(self._wdsp_rx.get_agc_gain_db())
        except Exception:
            pass

    def _do_demod_wdsp_rx2_only(self, rx2_iq) -> None:
        """Phase 3.E.1 hotfix v0.2 (2026-05-12) — RX2-only audio path.

        Called when SUB (rx2_enabled) is OFF but the operator has
        focused RX2 (clicked VFO B's LED, hit Ctrl+2, or middle-
        clicked the panadapter onto VFO B).  The operator's mental
        model: "SUB off = mono, focused VFO is what I hear."  So
        focusing RX2 with SUB off should route RX2's demod audio
        to the sink (center / mono), not RX1's.

        RX2's WDSP pan is already 0.5 (center) when SUB is off
        per ``_apply_rx2_routing`` -- so RX2's WDSP output is
        already mono-on-stereo and this method just hands it
        through the same output stage as ``_do_demod_wdsp`` does
        for RX1.  No pan changes needed; the routing helper
        already covered the (rx2_enabled=False) case.

        Skips the §14.6 captured-profile IQ-domain pre-pass --
        that engine lives on RX1 only in Phase 3 (operator UI for
        a per-RX2 profile is a Phase 4 deliverable).  RX2 audio
        runs clean through WDSP.

        Uses RX2's own volume / mute state (``_volume_rx2`` /
        ``_muted_rx2``) so the Vol-A slider doesn't surreptitiously
        gate audio that's now sourced from RX2.  In the SUB-off
        path, Vol-A on the panel actually represents "main audio
        volume" -- whichever RX is focused -- so the per-RX state
        mirror from Phase 3.D's SUB-rising-edge handler keeps the
        two in sync until the operator enables SUB.
        """
        if self._wdsp_rx2 is None:
            return
        try:
            audio = self._wdsp_rx2.process(rx2_iq)
        except Exception as exc:
            print(f"[Radio] WDSP rx2-only process error: {exc}")
            return
        if audio is None or audio.size == 0:
            return
        # Use RX2's volume/mute -- focused RX dictates which level
        # state applies in the SUB-off single-source path.
        if self._muted_rx2:
            audio = audio * 0.0
        else:
            v = float(self._volume_rx2)
            if v != 1.0:
                audio = audio * np.float32(v)
        # Safety clamp -- same rationale as ``_do_demod_wdsp``.
        np.clip(audio, -1.0, 1.0, out=audio)
        # BIN runs on the focused-RX audio path too.
        if self._bin_enabled and self._binaural.enabled:
            try:
                mono_in = audio[:, 0] if audio.ndim == 2 else audio
                bin_out = self._binaural.process(mono_in)
                if bin_out.ndim == 2 and bin_out.shape[1] == 2:
                    audio = bin_out.astype(np.float32, copy=False)
            except Exception as exc:
                print(f"[Radio] WDSP BIN error (rx2-only): {exc}")
        try:
            self._audio_sink.write(audio)
        except Exception as exc:
            print(f"[Radio] WDSP audio sink error (rx2-only): {exc}")
        try:
            self.audio_for_tci_emit.emit(audio)
        except Exception:
            pass
        # AGC gain readout from the channel actually feeding the
        # speakers, so the meter reflects what the operator hears.
        try:
            self._wdsp_agc_meter_skip = (
                getattr(self, "_wdsp_agc_meter_skip", 0) + 1)
            if self._wdsp_agc_meter_skip >= 8:
                self._wdsp_agc_meter_skip = 0
                self.agc_action_db.emit(self._wdsp_rx2.get_agc_gain_db())
        except Exception:
            pass

    def _do_demod_wdsp_dual(self, rx1_iq, rx2_iq) -> None:
        """Phase 2 v0.1 — dual-channel RX audio path.

        Processes BOTH WDSP RX channels and sums their stereo output
        for the audio sink.  Each channel has its own pan applied
        internally via WDSP's ``SetRXAPanelPan`` (RX1 default 0.0 =
        hard-left, RX2 default 1.0 = hard-right per consensus plan
        §6.1), so the summed output is naturally a stereo split with
        RX1 on the left ear and RX2 on the right.

        Captured-profile IQ-domain pre-pass (§14.6) applies ONLY to
        RX1 in Phase 2 -- RX2 has no per-RX profile selector UI
        yet.  Phase 3's per-RX focused-panel design will add it.

        Volume / mute / BIN apply to the COMBINED output for Phase 2.
        Per-RX volume + balance + mute sliders are a Phase 3 UI
        deliverable (Thetis-style operator UX) -- the engine
        already supports them via independent per-channel
        ``set_panel_gain`` and ``set_panel_pan``, just no operator
        surface yet.

        Called by ``DspWorker.process_block`` when an RX2 batch is
        paired with the RX1 batch in ``run_loop``.  Falls back to
        ``_do_demod_wdsp`` (RX1-only) when RX2 queue is empty at
        pair time (startup race, rate change).
        """
        # Early-out: either channel torn down (rate-change race).
        if self._wdsp_rx is None or self._wdsp_rx2 is None:
            return

        # ── RX1 path (with captured-profile pre-pass) ───────────────
        # Mirrors the head of ``_do_demod_wdsp`` -- accumulate +
        # apply under the engine lock so a rate-change main-thread
        # close+null can't collide with our ``process()``.
        fired_done = False
        iq_for_wdsp = rx1_iq
        audio_rx1 = None
        with self._iq_capture_lock:
            if self._wdsp_rx is None:
                return
            if self._iq_capture is not None:
                prev_state = self._iq_capture.state
                try:
                    self._iq_capture.accumulate(rx1_iq)
                except Exception as exc:
                    print(f"[Radio] IQ capture accumulate (dual): {exc}")
                else:
                    if (prev_state == "capturing"
                            and self._iq_capture.state == "ready"):
                        fired_done = True
                if (self._nr_use_captured_profile
                        and self._iq_capture.has_profile):
                    try:
                        iq_for_wdsp = self._iq_capture.apply(rx1_iq)
                    except Exception as exc:
                        print(f"[Radio] IQ apply (dual): {exc}")
                        iq_for_wdsp = rx1_iq
            try:
                audio_rx1 = self._wdsp_rx.process(iq_for_wdsp)
            except Exception as exc:
                print(f"[Radio] WDSP rx1 process error (dual): {exc}")
                audio_rx1 = None

        if fired_done:
            try:
                self.noise_capture_done.emit("")
            except Exception:
                pass

        # ── RX2 path (no captured-profile in Phase 2) ───────────────
        # RX2 has its own engine lock domain (same _iq_capture_lock
        # is held only for RX1's capture engine -- RX2 doesn't share
        # state with it).  Phase 3 may introduce an _iq_capture_rx2
        # mirror; for now RX2 just runs clean through WDSP.
        audio_rx2 = None
        try:
            audio_rx2 = self._wdsp_rx2.process(rx2_iq)
        except Exception as exc:
            print(f"[Radio] WDSP rx2 process error: {exc}")
            audio_rx2 = None

        # ── Pre-sum per-RX volume + mute (Phase 3.D v0.1) ───────────
        # Per consensus plan §6.8: when SUB (rx2_enabled) is on, the
        # operator sees Vol-A / Vol-B and Mute-A / Mute-B sliders.
        # We apply those gains here BEFORE summing so each RX's
        # stereo half (RX1 in L, RX2 in R via the SetRXAPanelPan
        # 0.0 / 1.0 split) gets its own trim.  When RX2 is OFF the
        # mixer doesn't feed _do_demod_wdsp_dual at all -- this
        # path is unreachable in the SUB-off case.
        rx1_vol = 0.0 if self._muted else float(self._volume)
        rx2_vol = 0.0 if self._muted_rx2 else float(self._volume_rx2)
        if audio_rx1 is not None and audio_rx1.size > 0 and rx1_vol != 1.0:
            audio_rx1 = audio_rx1 * np.float32(rx1_vol)
        if audio_rx2 is not None and audio_rx2.size > 0 and rx2_vol != 1.0:
            audio_rx2 = audio_rx2 * np.float32(rx2_vol)

        # ── Combine RX1 + RX2 audio ─────────────────────────────────
        # WDSP returns (N, 2) float32 stereo.  RX1's pan=0 produced
        # (L=signal, R=0); RX2's pan=1 produced (L=0, R=signal).
        # Sum -> (L=RX1, R=RX2) -- the stereo split.  Lengths may
        # briefly differ across rate-change transients; align to
        # the shorter buffer to avoid out-of-bounds.
        if audio_rx1 is None or audio_rx1.size == 0:
            if audio_rx2 is None or audio_rx2.size == 0:
                return
            audio = audio_rx2.copy()
        elif audio_rx2 is None or audio_rx2.size == 0:
            audio = audio_rx1.copy()
        else:
            n = min(audio_rx1.shape[0], audio_rx2.shape[0])
            if n == 0:
                return
            audio = audio_rx1[:n] + audio_rx2[:n]

        # ── Output stage (BIN / sink / TCI) ─────────────────────────
        # Per-RX volume + mute already applied above (Phase 3.D);
        # no post-sum trim here.  BAL stays single per consensus
        # plan §6.8 (it's combined-output stereo balance).

        if self._bin_enabled and self._binaural.enabled:
            try:
                mono_in = audio[:, 0] if audio.ndim == 2 else audio
                bin_out = self._binaural.process(mono_in)
                if bin_out.ndim == 2 and bin_out.shape[1] == 2:
                    audio = bin_out.astype(np.float32, copy=False)
            except Exception as exc:
                print(f"[Radio] WDSP BIN error (dual): {exc}")

        # Phase 3.D safety clamp (2026-05-12) -- protect codec from
        # any upstream gain anomaly.  Same rationale as the
        # single-RX path; see ``_do_demod_wdsp``.
        if audio is not None and audio.size > 0:
            np.clip(audio, -1.0, 1.0, out=audio)

        try:
            self._audio_sink.write(audio)
        except Exception as exc:
            print(f"[Radio] WDSP audio sink error (dual): {exc}")

        try:
            self.audio_for_tci_emit.emit(audio)
        except Exception:
            pass

        # AGC gain readout -- still reads from RX1 only (RX1 is the
        # "focused" receiver per CLAUDE.md §6.2 hybrid UI model).
        # Phase 3 may add a per-RX meter readout when the focused
        # RX is RX2.
        try:
            self._wdsp_agc_meter_skip = (
                getattr(self, "_wdsp_agc_meter_skip", 0) + 1)
            if self._wdsp_agc_meter_skip >= 8:
                self._wdsp_agc_meter_skip = 0
                self.agc_action_db.emit(self._wdsp_rx.get_agc_gain_db())
        except Exception:
            pass

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
            # TCI audio tap (v0.0.9.1+) -- mirror of _do_demod path
            # so tone mode also feeds TCI subscribers (useful for
            # client-side audio-path verification).
            try:
                self.audio_for_tci_emit.emit(audio)
            except Exception:
                pass
        except Exception:
            pass

    # NOTE: _apply_agc_and_volume removed in Phase 6.A
    # (v0.0.9.6).  Originally the post-demod audio chain (AF Gain →
    # AGC → Volume → APF → stream-gap fade → tanh limiter) for the
    # legacy DSP path.  WDSP took over all of these stages
    # internally as of Phase 3 — AF Gain via SetRXAPanelGain, AGC
    # via WDSP's wcpAGC C engine, APF via WDSP's SPEAK biquad,
    # volume + mute applied directly inside _do_demod_wdsp.  The
    # method became orphan dead code in Phase 4 and was originally
    # scheduled for Phase 8 deletion; an audit during Phase 6
    # confirmed zero callers (only docstring / comment references
    # remained) so the deletion landed earlier than planned.
    # See git history for the prior body if anyone needs to
    # recover the per-sample AGC tracker or the stream-gap fade
    # logic.

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

    def set_agc_profile(self, name: str, target_rx: Optional[int] = None):
        """Set the AGC profile (off / fast / med / slow / long /
        auto / custom) for ``target_rx`` (default = focused RX).

        Phase 3.C v0.1: ``target_rx`` semantics replace Phase 2's
        fan-out.  Profile is independent per RX -- operator can
        run RX1 on FAST while RX2 is on SLOW.

        Auto-threshold timer fires regardless of which RX changed
        profile; the timer's tracker pushes to BOTH channels (see
        ``auto_set_agc_threshold``) because the single noise-floor
        measurement covers both until Phase 4's split panadapter.
        """
        name = name.lower().strip()
        if name not in (*self.AGC_PRESETS, "custom"):
            name = "med"

        rx_id, _suffix = self._resolve_rx_target(target_rx)

        if rx_id == 2:
            if name == self._agc_profile_rx2:
                # Idempotent on RX2 -- but still kick the auto-
                # threshold timer in case operator wants a fresh
                # tracker push (e.g., after band change).
                self.auto_set_agc_threshold()
                if not self._agc_auto_timer.isActive():
                    self._agc_auto_timer.start()
                return
            self._agc_profile_rx2 = name
            if self._wdsp_rx2 is not None:
                try:
                    self._wdsp_rx2.set_agc(self._wdsp_agc_for(name))
                except Exception as exc:
                    print(f"[Radio] WDSP rx2 agc-change error: {exc}")
            # Auto-threshold push covers both channels via the
            # auto-tracker; firing it here keeps RX2's threshold
            # fresh after a profile change.
            self.auto_set_agc_threshold()
            if not self._agc_auto_timer.isActive():
                self._agc_auto_timer.start()
            self.agc_profile_changed_rx2.emit(name)
            return

        # RX1 path -- existing behavior.
        self._agc_profile = name
        # AGC_PRESETS is kept as a UI label set; WDSP has its own
        # internal preset table (mode-specific tau_decay / hangtime
        # / hang_thresh) and applies them via _wdsp_rx.set_agc().
        # The legacy Python `_wdsp_agc.set_mode()` call was removed
        # Phase 6.A.
        # Mirror the preset's release/hang values onto Radio's
        # advisory state fields so the Settings AGC tab sliders
        # reflect the active profile (operators expect to SEE the
        # profile change reflected in the sliders).  Custom is
        # excluded so the operator's manual values aren't clobbered.
        preset = self.AGC_PRESETS.get(name)
        if preset is not None and name != "custom":
            self._agc_release = float(preset.get("release", self._agc_release))
            self._agc_hang_blocks = int(preset.get("hang_blocks", self._agc_hang_blocks))
        # WDSP native engine — AGC mode lives inside the engine.
        # SetRXAAGCMode (wcpAGC.c) sets the per-profile hangtime
        # and tau_decay AND calls loadWcpAGC() to recompute
        # coefficients, so a single mode push is sufficient.
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_agc(self._wdsp_agc_for(name))
            except Exception as exc:
                print(f"[Radio] WDSP rx agc-change error: {exc}")
        # Phase 1 follow-up: auto-track the threshold REGARDLESS of
        # profile choice (so operator-set profiles still get fresh
        # threshold values).  Phase 3.C: auto-tracker now pushes
        # to BOTH channels via the per-target setter, see
        # auto_set_agc_threshold's implementation.
        self.auto_set_agc_threshold()
        if not self._agc_auto_timer.isActive():
            self._agc_auto_timer.start()
        self.agc_profile_changed.emit(name)

    def set_agc_custom(self, release: float, hang_blocks: int):
        """Set AGC custom-slider values and switch profile to
        'custom'.  WDSP owns the live AGC engine (Phase 6.A
        deleted the legacy Python WdspAgc wrapper); WDSP applies
        canonical mode presets through ``_wdsp_rx.set_agc(mode)``.
        These slider values are kept for UI persistence and may
        map back to operator-facing WDSP knobs (attack/decay/hang
        in seconds) in a future Settings panel.  For now, picking
        'custom' produces the same audio behavior as 'med'.

        Release range [0.0, 0.300] covers all preset values
        (Fast=0.30, Med=0.158, Slow=0.083, Long=0.04) so
        round-tripping through the slider doesn't clamp values."""
        self._agc_release = max(0.0, min(0.300, float(release)))
        self._agc_hang_blocks = max(0, min(200, int(hang_blocks)))
        self._agc_profile = "custom"
        self.agc_profile_changed.emit("custom")

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
        # v0.0.9.8 carrier-freq VFO convention: the operator's marker
        # IS the carrier (= where the audio comes from), so the CW
        # Zero indicator line is redundant — it would draw on top of
        # the marker.  Always return 0 here so the spectrum widget
        # hides the line; the marker itself carries the signal-
        # position information now.  Property + signal kept for API
        # compatibility (widget still subscribes to the signal) but
        # the value is always 0.
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
        # WDSP RX engine — the CW filter is centred on the pitch, so a
        # pitch change requires a filter re-push when the active mode is
        # CWU or CWL.
        if self._wdsp_rx is not None and self._mode in ("CWU", "CWL"):
            try:
                low, high = self._wdsp_filter_for(self._mode)
                self._wdsp_rx.set_filter(low, high)
                # APF center frequency tracks CW pitch.  Push freq
                # without disturbing run state — set_apf_freq is safe
                # to call while APF is on.
                self._wdsp_rx.set_apf_freq(float(new_pitch))
            except Exception as exc:
                print(f"[Radio] WDSP rx cw-pitch error: {exc}")
        # Phase 3.E.1 hotfix v0.8 (2026-05-12): same filter +
        # APF re-push for RX2 when it's on CW.  Pitch is shared
        # (single operator-ear preference) so both RXes track
        # the same value.
        if (self._wdsp_rx2 is not None
                and self._mode_rx2 in ("CWU", "CWL")):
            try:
                low, high = self._wdsp_filter_for(
                    self._mode_rx2, target_rx=2)
                self._wdsp_rx2.set_filter(low, high)
                self._wdsp_rx2.set_apf_freq(float(new_pitch))
            except Exception as exc:
                print(f"[Radio] WDSP rx2 cw-pitch error: {exc}")
        # Re-push DDS freq when in CW mode — pitch change shifts the
        # DDS-vs-VFO offset (DDS = VFO ± pitch), so the actual hardware
        # tuning needs to follow.  Without this, dialing pitch from
        # 650 to 800 Hz mid-CW would leave the DDS at the old
        # carrier - 650 offset and the operator would hear the tone
        # at 650 Hz audio instead of the new 800 Hz audio.  No-op in
        # non-CW modes (offset is identity there).
        if self._stream and self._mode in ("CWU", "CWL"):
            try:
                self._stream._set_rx1_freq(self._compute_dds_freq_hz())  # noqa: SLF001
            except Exception as exc:
                print(f"[Radio] cw-pitch DDS re-push error: {exc}")
        # Phase 3.E.1 hotfix v0.8 (2026-05-12): mirror DDS re-push
        # for RX2 -- pitch dial affects DDC1's DDS-vs-VFO offset
        # too whenever RX2 is on CW.
        if self._stream and self._mode_rx2 in ("CWU", "CWL"):
            try:
                self._stream._set_rx2_freq(  # noqa: SLF001
                    self._compute_dds_freq_hz(target_rx=2))
            except Exception as exc:
                print(f"[Radio] cw-pitch RX2 DDS re-push error: {exc}")
        # Recompute + re-emit passband so the panadapter overlay
        # shifts to the new CW position immediately.
        self._emit_passband()
        self.cw_pitch_changed.emit(new_pitch)
        self._emit_cw_zero()
        # Pitch change shifts the DDS-vs-VFO offset by the pitch
        # delta — re-emit so the spectrum's marker tracks it.
        self._emit_marker_offset()

    # ── RIT (Receiver Incremental Tuning, v0.1.1) ──────────────────
    @property
    def rit_enabled(self) -> bool:
        """Whether RIT is currently shifting the RX1 DDC freq."""
        return bool(self._rit_enabled)

    @property
    def rit_offset_hz(self) -> int:
        """RIT offset in Hz (signed, -9999..+9999)."""
        return int(self._rit_offset_hz)

    def set_rit_enabled(self, enabled: bool) -> None:
        """Turn RIT on or off.  When toggled, the DDC re-tunes so
        the offset takes effect immediately (not on next operator
        freq change).  Persists across sessions via QSettings."""
        flag = bool(enabled)
        if flag == self._rit_enabled:
            return
        self._rit_enabled = flag
        try:
            from PySide6.QtCore import QSettings as _QS
            _QS("N8SDR", "Lyra").setValue("radio/rit_enabled", flag)
        except Exception:
            pass
        # Re-push DDS so the offset turns on/off live.  Mirrors the
        # idiom in ``set_cw_pitch_hz`` — the operator-displayed VFO
        # is unchanged, only the DDS-vs-VFO offset shifts.  No-op
        # when ``_rit_offset_hz == 0`` (toggling on with zero offset
        # produces no audible change, which is correct).
        self._repush_rx1_dds_after_rit_change()
        self.rit_enabled_changed.emit(flag)
        self._emit_marker_offset()

    def set_rit_offset_hz(self, offset_hz: int) -> None:
        """Set the RIT offset (clamped to -9999..+9999 Hz).  Only
        affects the audible DDC tuning when RIT is enabled.
        Persists across sessions."""
        new_off = int(max(-9999, min(9999, int(offset_hz))))
        if new_off == self._rit_offset_hz:
            return
        self._rit_offset_hz = new_off
        try:
            from PySide6.QtCore import QSettings as _QS
            _QS("N8SDR", "Lyra").setValue("radio/rit_offset_hz", new_off)
        except Exception:
            pass
        # Only re-push DDS when RIT is actually live; otherwise the
        # offset is stored for the next time the operator toggles RIT
        # on but produces no audible change right now.
        if self._rit_enabled:
            self._repush_rx1_dds_after_rit_change()
            self._emit_marker_offset()
        self.rit_offset_changed.emit(new_off)

    def _repush_rx1_dds_after_rit_change(self) -> None:
        """Mirrors the DDS re-push idiom in ``set_freq_hz`` and
        ``set_cw_pitch_hz`` -- write the corrected DDS freq to the
        protocol layer + flush the panadapter ring so the next
        waterfall row reflects the new center.  Audio chain reset
        is intentionally skipped: RIT offsets are small (kHz-scale,
        typical use within a few hundred Hz) so the full DSP-reset
        sledgehammer that ``set_freq_hz`` uses for cross-band tuning
        would be overkill and would briefly mute the operator just
        for spinning RIT.  Sample ring clear gives the spectrum +
        waterfall a clean transition without disturbing audio."""
        if self._stream is None:
            return
        try:
            self._stream._set_rx1_freq(self._compute_dds_freq_hz())  # noqa: SLF001
        except Exception as exc:
            print(f"[Radio] RIT DDS re-push error: {exc}")
            return
        with self._ring_lock:
            self._sample_ring.clear()
        # Reset waterfall tick counter so the next row arrives
        # promptly at the new center rather than inheriting timing
        # state from the pre-RIT center.
        self._waterfall_tick_counter = 0

    def autoload_rit_settings(self) -> None:
        """Restore RIT state from QSettings on startup.  Loads the
        offset FIRST (so when ``set_rit_enabled`` flips on it sees
        the correct stored offset, not zero), then the enabled flag.
        Order matters: with enabled-first, the toggle would re-push
        DDS at offset 0 before the real offset got restored.

        Defaults: RIT off, offset 0 -- matches the constructor seed
        so this autoload is a no-op for fresh installs."""
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
        except Exception:
            return
        if s.contains("radio/rit_offset_hz"):
            try:
                self.set_rit_offset_hz(int(s.value("radio/rit_offset_hz")))
            except (TypeError, ValueError):
                pass
        if s.contains("radio/rit_enabled"):
            try:
                self.set_rit_enabled(
                    s.value("radio/rit_enabled", False, type=bool))
            except Exception as exc:
                print(f"[Radio] autoload radio/rit_enabled: {exc}")

    # ── Mic input source (v0.2 Phase 2 commit 5) ───────────────────
    @property
    def mic_source(self) -> str:
        """Current mic-input source: 'hl2_jack' or 'pc_soundcard'."""
        return self._mic_source

    @property
    def pc_mic_device(self) -> Optional[int]:
        """PortAudio input device index for PC mic; None = host-API
        default device."""
        return self._pc_mic_device

    @property
    def pc_mic_channel(self) -> str:
        """PC mic channel select: 'L' / 'R' / 'BOTH'."""
        return self._pc_mic_channel

    def set_mic_source(self, source: str) -> None:
        """Switch between HL2 mic-jack and PC sound card input.

        ``source`` must be ``'hl2_jack'`` (AK4951 codec via EP6) or
        ``'pc_soundcard'`` (sounddevice InputStream).  Idempotent --
        setting the current value is a no-op.

        Side effects:
        * When switching TO 'pc_soundcard': lazily creates a
          ``SoundDeviceMicSource`` configured with the operator's
          stored device + channel + host-API choices.  Does NOT
          start capture -- caller (TX dispatcher in commit 7) starts
          it when the TX path activates.
        * When switching AWAY from 'pc_soundcard': stops the
          ``SoundDeviceMicSource`` if running and clears the
          reference.
        * The HL2 EP6 ``mic_callback`` registration is symmetric --
          when source is 'hl2_jack' the callback gets wired by the
          TX dispatcher; on 'pc_soundcard' the callback gets cleared
          so EP6 mic samples drop on the floor (preserving v0.1
          behaviour for standard-HL2 operators who don't have a
          radio mic input anyway).

        Persisted to ``radio/mic_source`` QSettings.  Emits
        ``mic_source_changed`` signal so Settings UI can mirror.
        """
        new = str(source)
        if new not in ("hl2_jack", "pc_soundcard"):
            raise ValueError(
                f"mic_source must be 'hl2_jack' or 'pc_soundcard', "
                f"got {source!r}"
            )
        if new == self._mic_source:
            return
        self._mic_source = new
        # Tear down PC mic source if leaving the pc_soundcard path.
        if new != "pc_soundcard" and self._pc_mic_source is not None:
            try:
                self._pc_mic_source.stop()
            except Exception as exc:
                print(f"[Radio] PC mic source stop failed: {exc}")
            self._pc_mic_source = None
        # Lazily build the source object when entering pc_soundcard
        # path.  Capture is NOT started here -- TX dispatcher (commit
        # 7) starts when operator keys up.  Construction must succeed
        # before we persist + emit so a sounddevice-import failure
        # rolls the operator's choice back.
        if new == "pc_soundcard" and self._pc_mic_source is None:
            try:
                from lyra.dsp.audio_sink import SoundDeviceMicSource
                self._pc_mic_source = SoundDeviceMicSource(
                    rate=48_000,
                    device=self._pc_mic_device,
                    channel_select=self._pc_mic_channel,
                )
            except Exception as exc:
                print(f"[Radio] PC mic source construction failed: {exc}")
                # Roll back to hl2_jack on construction failure --
                # better to leave operator on a working path than
                # silently fail.
                self._mic_source = "hl2_jack"
                new = "hl2_jack"
        try:
            from PySide6.QtCore import QSettings as _QS
            _QS("N8SDR", "Lyra").setValue("radio/mic_source", new)
        except Exception:
            pass
        # v0.2 Phase 2 commit 7-redo (2026-05-15): rewire the mic
        # source now that producer paths feed the dedicated TX DSP
        # worker thread.  No env-var gate needed -- the worker
        # absorbs the blocking cost of TxChannel.process on its
        # own thread, so dispatch is safe at any time the stream
        # is running.  Skips silently when the stream isn't running
        # (state + persistence still update; the rewire happens on
        # next start()).
        try:
            self._wire_mic_source()
        except Exception as exc:  # noqa: BLE001
            print(f"[Radio] mic source rewire failed: {exc}")
        self.mic_source_changed.emit(new)

    def set_pc_mic_device(self, device: Optional[int]) -> None:
        """Set the PortAudio input device index for PC mic capture.

        ``device=None`` means "use the host-API default input device".
        Idempotent; persists to QSettings.  If a PC mic source is
        currently running, restart it with the new device.
        """
        if device == self._pc_mic_device:
            return
        self._pc_mic_device = device
        # Re-arm the PC-mic failure log latch: the operator just
        # picked a new device, so if the new one also fails we want
        # the toast to fire again (see commit 7.2 polish notes).
        self._pc_mic_failure_logged = False
        # Restart the source if running on the pc_soundcard path so
        # the new device takes effect.  Stop -> rebuild -> caller
        # starts again at next MOX edge.
        if self._pc_mic_source is not None:
            try:
                self._pc_mic_source.stop()
            except Exception:
                pass
            try:
                from lyra.dsp.audio_sink import SoundDeviceMicSource
                self._pc_mic_source = SoundDeviceMicSource(
                    rate=48_000,
                    device=device,
                    channel_select=self._pc_mic_channel,
                )
            except Exception as exc:
                print(f"[Radio] PC mic rebuild failed: {exc}")
                self._pc_mic_source = None
        try:
            from PySide6.QtCore import QSettings as _QS
            _QS("N8SDR", "Lyra").setValue(
                "radio/pc_mic_device",
                device if device is not None else -1,
            )
        except Exception:
            pass
        self.pc_mic_device_changed.emit(device)

    def set_pc_mic_channel(self, channel: str) -> None:
        """Set PC mic channel select: 'L', 'R', or 'BOTH'."""
        new = str(channel).upper()
        if new not in ("L", "R", "BOTH"):
            raise ValueError(
                f"pc_mic_channel must be 'L', 'R', or 'BOTH', got {channel!r}"
            )
        if new == self._pc_mic_channel:
            return
        self._pc_mic_channel = new
        # Re-arm the PC-mic failure log latch (see set_pc_mic_device).
        self._pc_mic_failure_logged = False
        # Same rebuild dance as device-change.
        if self._pc_mic_source is not None:
            try:
                self._pc_mic_source.stop()
            except Exception:
                pass
            try:
                from lyra.dsp.audio_sink import SoundDeviceMicSource
                self._pc_mic_source = SoundDeviceMicSource(
                    rate=48_000,
                    device=self._pc_mic_device,
                    channel_select=new,
                )
            except Exception as exc:
                print(f"[Radio] PC mic rebuild failed: {exc}")
                self._pc_mic_source = None
        try:
            from PySide6.QtCore import QSettings as _QS
            _QS("N8SDR", "Lyra").setValue("radio/pc_mic_channel", new)
        except Exception:
            pass
        self.pc_mic_channel_changed.emit(new)

    def autoload_mic_source_settings(self) -> None:
        """Restore mic-source state from QSettings on startup.

        Loads device + channel FIRST so when set_mic_source flips to
        pc_soundcard it builds the source with the operator's stored
        choices.  Defaults: hl2_jack source, no device override,
        channel=L -- matches the constructor seed so this autoload
        is a no-op for fresh installs.
        """
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("N8SDR", "Lyra")
        except Exception:
            return
        if s.contains("radio/pc_mic_device"):
            try:
                v = int(s.value("radio/pc_mic_device", -1))
                self._pc_mic_device = v if v >= 0 else None
            except (TypeError, ValueError):
                pass
        if s.contains("radio/pc_mic_channel"):
            try:
                ch = str(s.value("radio/pc_mic_channel", "L")).upper()
                if ch in ("L", "R", "BOTH"):
                    self._pc_mic_channel = ch
            except Exception:
                pass
        if s.contains("radio/mic_source"):
            try:
                src = str(s.value("radio/mic_source", "hl2_jack"))
                if src in ("hl2_jack", "pc_soundcard"):
                    self.set_mic_source(src)
            except Exception as exc:
                print(f"[Radio] autoload radio/mic_source: {exc}")

    # ── AGC threshold (WDSP SetRXAAGCThresh parameter, dBFS) ───────
    @property
    def agc_threshold(self) -> float:
        """AGC threshold in dBFS — see ``set_agc_threshold``."""
        return self._agc_target

    def set_agc_threshold(
        self, threshold_dbfs: float,
        target_rx: Optional[int] = None,
    ):
        """Set the AGC threshold in dBFS for ``target_rx``
        (default = focused RX).  This is WDSP's
        ``SetRXAAGCThresh`` parameter — the noise-floor reference
        used to compute ``max_gain`` (the AGC's gain ceiling).

        Lower threshold → larger ``max_gain`` → AGC has more room
        to boost weak signals (good for weak-signal / DX work).
        Higher threshold → smaller ``max_gain`` → AGC starts
        compressing earlier (good for strong-signal / broadcast
        listening).

        Operator-typical values:
          -130 dBFS → very quiet band, weak-signal hunting
          -100 dBFS → normal HF operation (default)
           -90 dBFS → mid-strength signals, less AGC boost
           -60 dBFS → broadcast / strong-signal listening only

        Range clamped to ``_AGC_THRESH_MIN_DBFS``..
        ``_AGC_THRESH_MAX_DBFS`` (-150..-40 dBFS).

        Phase 3.C v0.1 (2026-05-12): ``target_rx`` semantics
        replace Phase 2's fan-out.  ``auto_set_agc_threshold``
        explicitly pushes to BOTH channels (single noise-floor
        measurement covers both until Phase 4's split panadapter
        adds per-RX NF).

        v0.0.9.8 fix history: this method previously took a
        legacy 0..1 linear "audio output target" value, never
        pushed to WDSP, and was responsible for the operator-
        reported "AGC profiles all sound the same" symptom (max_gain
        clamped at WDSP create-time default since the v0.0.9.6
        cleanup arc, no headroom for the per-mode tau_decay
        differences to be audible).  Switched to the proper
        dBFS-domain semantic + direct WDSP push so the slider
        actually drives the engine.
        """
        v = max(self._AGC_THRESH_MIN_DBFS,
                min(self._AGC_THRESH_MAX_DBFS,
                    float(threshold_dbfs)))

        rx_id, _suffix = self._resolve_rx_target(target_rx)

        if rx_id == 2:
            self._agc_target_rx2 = v
            if self._wdsp_rx2 is not None:
                try:
                    self._wdsp_rx2.set_agc_threshold(
                        v, 4096, int(self._rate))
                except Exception as exc:
                    print(f"[Radio] WDSP AGC rx2 threshold push: {exc}")
            self.agc_threshold_changed_rx2.emit(v)
            return

        # RX1 path.
        self._agc_target = v
        if self._wdsp_rx is not None:
            try:
                self._wdsp_rx.set_agc_threshold(
                    v, 4096, int(self._rate))
            except Exception as exc:
                print(f"[Radio] WDSP AGC threshold push: {exc}")
        self.agc_threshold_changed.emit(v)

    def auto_set_agc_threshold(self, margin_db: float = 18.0) -> float:
        """Calibrate AGC threshold to sit ``margin_db`` above the
        current rolling noise floor (in dBFS).  Bound to the AGC
        right-click "Auto" action and the Settings → DSP → AGC
        Threshold "Auto" button.  Returns the new threshold (dBFS).

        UNCOMMITTED EXPERIMENT (v0.1 Phase 1 follow-up, 2026-05-11):
        ``margin_db`` default bumped from 5 dB → 18 dB to match
        the CLAUDE.md §14.2 documentation ("calibrate ~18 dB above
        the rolling noise floor").  The 5 dB margin produced
        threshold values too close to noise floor, causing AGC to
        engage on noise itself and over-amplify on AM mode (with
        carrier component sitting near baseband DC).  Operator
        symptom: "audio low like attenuated; NR acts as pre-amp on
        AM" — AGC was riding the entire envelope including noise.
        With 18 dB margin, threshold lands at nf+18 (typically
        -110 to -120 dBFS for HL2 at LNA +12 dB), so AGC only
        engages on real signals well above noise.

        ``margin_db`` was historically +5 dB — places the
        threshold just above the noise so AGC engages on actual
        signals while still letting the noise floor itself ride
        through at full max_gain.  Replaced 2026-05-11.

        v0.0.9.8 fix: now reads the live FFT-derived noise floor
        (``_noise_floor_db`` — 20th-percentile of the spectrum,
        rolling-averaged + EMA-smoothed in the FFT pipeline at
        ``radio.py:8258``).  Previously read ``_noise_baseline``
        which was hardcoded to ``1e-4`` (-80 dBFS) and never
        updated since the legacy Python noise-floor tracker was
        deleted in the v0.0.9.6 cleanup arc — so Auto always
        produced -75 dBFS regardless of actual band conditions
        ("Auto sounds the same as Med" symptom).  When the
        spectrum noise-floor estimate is unavailable (operator
        disabled the NF reference line OR the FFT pipeline
        hasn't accumulated enough history yet), falls back to a
        sensible -100 dBFS default."""
        nf_db = self._noise_floor_db
        if nf_db is None:
            # No live estimate available — use a sensible default
            # so Auto still produces a usable threshold.
            nf_db = -100.0
        target_dbfs = float(nf_db) + float(margin_db)
        target_dbfs = max(self._AGC_THRESH_MIN_DBFS,
                          min(self._AGC_THRESH_MAX_DBFS,
                              target_dbfs))
        # Phase 3.C v0.1: explicitly push to BOTH channels.  The
        # single FFT-derived noise-floor estimate covers both
        # receivers until Phase 4's split panadapter adds per-RX
        # NF tracking.  Each per-target call emits its own
        # ``agc_threshold_changed`` / ``agc_threshold_changed_rx2``
        # signal so per-RX UI bindings stay in sync.
        self.set_agc_threshold(target_dbfs, target_rx=0)
        self.set_agc_threshold(target_dbfs, target_rx=2)
        self.status_message.emit(
            f"AGC auto-threshold: {target_dbfs:+.0f} dBFS "
            f"(noise floor {nf_db:+.0f} dBFS + {margin_db:.0f} dB margin)",
            3000)
        return target_dbfs

    # Note: _decimate_to_48k and _rebuild_demods used to live here,
    # then moved to self._rx_channel.  Phase 5 (v0.0.9.6) deleted
    # both — WDSP performs decimation + demod inside the cffi engine,
    # and self._rx_channel is now a thin state container for module
    # instances Radio still uses directly (post-AGC APF, captured-
    # noise capture on NR1).  See lyra/dsp/channel.py docstring.

    # NOTE: _make_notch_filter removed in Phase 6.B (v0.0.9.6).
    # Built per-mode peaking-EQ + DC-blocker biquads on the
    # legacy NotchFilter class; that class lived in
    # lyra/dsp/demod.py which got deleted alongside this method.
    # WDSP receives notch parameters as plain (abs_freq_hz,
    # width_hz, active) tuples via _push_wdsp_notches and runs
    # the actual filtering inside the cffi engine — no Lyra-side
    # biquad design needed.

    @property
    def pc_audio_host_api(self) -> str:
        """Operator-selected PortAudio host API for the PC Soundcard
        sink (v0.0.9.6).  See ``lyra/dsp/audio_sink.py::
        enumerate_host_apis()`` for the available labels."""
        return self._pc_audio_host_api

    def set_pc_audio_host_api(self, label: str) -> None:
        """Set the host API label for the PC Soundcard sink.  Triggers
        a sink rebuild if PC Soundcard is currently active so the
        change takes effect immediately.  Same swap-cleanup pattern
        as ``set_pc_audio_device_index``."""
        new_label = str(label) if label else "Auto"
        if new_label == self._pc_audio_host_api:
            return
        self._pc_audio_host_api = new_label
        self.pc_audio_host_api_changed.emit(new_label)
        if self._audio_output != "AK4951" and self._stream:
            if self._dsp_worker is not None:
                new_sink = self._make_sink()
                self._request_dsp_reset_channel_only()
                self._audio_sink = new_sink
                self._push_balance_to_sink()
                self.worker_audio_sink_changed.emit(new_sink)
            else:
                try:
                    self._audio_sink.close()
                except Exception:
                    pass
                self._request_dsp_reset_channel_only()
                import time as _time
                _time.sleep(0.030)
                self._audio_sink = self._make_sink()
                self._push_balance_to_sink()

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
            if self._dsp_worker is not None:
                # B.5 — worker mode: build new, hand off, worker
                # closes the old between blocks.
                new_sink = self._make_sink()
                # B.9: channel reset routed through worker.
                self._request_dsp_reset_channel_only()
                self._audio_sink = new_sink
                self._push_balance_to_sink()
                self.worker_audio_sink_changed.emit(new_sink)
            else:
                try:
                    self._audio_sink.close()
                except Exception:
                    pass
                self._request_dsp_reset_channel_only()
                import time as _time
                _time.sleep(0.030)
                self._audio_sink = self._make_sink()
                self._push_balance_to_sink()

    def _radio_debug_maybe_print(self):
        """Once per ~5 seconds, print a one-line Radio-side diagnostic
        summary: FFT timer ticks, spectrum emits, IQ-batch counts, and
        cumulative DSP main-thread time. Called from _tick_fft so it
        piggybacks on an already-running timer (no extra clock work
        when LYRA_PAINT_DEBUG is off — the caller checks the flag)."""
        import time as _rdtime
        now = _rdtime.perf_counter()
        if self._dbg_t0_window == 0.0:
            self._dbg_t0_window = now
            return
        if (now - self._dbg_t0_window) < 5.0:
            return
        elapsed = now - self._dbg_t0_window
        ticks = self._dbg_fft_ticks
        emits = self._dbg_fft_emits
        s_calls = self._dbg_samples_calls
        s_total = self._dbg_samples_total_ms
        s_max   = self._dbg_samples_max_ms
        s_pct   = (s_total / (elapsed * 1000.0)) * 100.0
        # DSP-load classifier — if _on_samples_main_thread is using more
        # than 50% of wall-clock time on the main thread, the timer +
        # paint + UI events are all going to be starved.
        verdict = "ok"
        if s_pct > 80.0:
            verdict = "SATURATED"
        elif s_pct > 50.0:
            verdict = "HEAVY"
        elif s_pct > 25.0:
            verdict = "warm"
        # Per-stage diagnostic — pinpoints which DSP stage is the
        # spike. "max" is the worst single call this window; "tot" is
        # cumulative ms spent in that stage. The stage with the
        # biggest max is the one to optimize.
        sm = self._dbg_stage_max
        st = self._dbg_stage_total
        stage_str = (
            f"channel(max={sm['channel']:.1f}ms tot={st['channel']:.0f}ms) "
            f"agc(max={sm['agc']:.1f}ms tot={st['agc']:.0f}ms) "
            f"bin(max={sm['bin']:.1f}ms tot={st['bin']:.0f}ms) "
            f"sink(max={sm['sink']:.1f}ms tot={st['sink']:.0f}ms)")
        print(f"[Lyra radio] {verdict}: "
              f"fft_ticks={ticks} ({ticks/elapsed:.1f}/s), "
              f"emits={emits} ({emits/elapsed:.1f}/s), "
              f"iq_batches={s_calls}, "
              f"largest_iq_n={self._dbg_largest_iq_n}, "
              f"dsp_main_thread={s_total:.0f}ms ({s_pct:.0f}% of wall), "
              f"dsp_max_per_batch={s_max:.1f}ms")
        if s_calls > 0:
            print(f"[Lyra radio] stages: {stage_str}")
        # Reset window
        self._dbg_t0_window = now
        self._dbg_fft_ticks = 0
        self._dbg_fft_emits = 0
        self._dbg_samples_calls = 0
        self._dbg_samples_total_ms = 0.0
        self._dbg_samples_max_ms = 0.0
        self._dbg_largest_iq_n = 0
        for _k in self._dbg_stage_max:
            self._dbg_stage_max[_k] = 0.0
            self._dbg_stage_total[_k] = 0.0

    def _make_sink(self):
        # v0.0.9.6: sinks register their outbound on the shared
        # AudioMixer.  Constructing a new sink automatically takes
        # over the mixer's outbound (replacing the previous sink's
        # callback).  ``mixer.set_outbound`` is called inside each
        # sink's __init__.
        if self._audio_output == "AK4951":
            return AK4951Sink(self._stream, self._audio_mixer)
        try:
            return SoundDeviceSink(
                self._audio_mixer,
                rate=48000,
                device=self._pc_audio_device_index,
                host_api_label=self._pc_audio_host_api,
            )
        except Exception as e:
            self.status_message.emit(f"Audio output error: {e}", 6000)
            return NullSink()

    # ── FFT tick → spectrum + S-meter signals ─────────────────────────
    def _tick_fft(self):
        # Radio-side instrumentation: count this tick whether or not
        # we have enough samples to emit. If the timer is firing 30x/s
        # but the ring is short → ring-drain issue; if the timer is
        # only firing 5x/s → main-thread starvation.
        if self._radio_debug:
            self._dbg_fft_ticks += 1
            self._radio_debug_maybe_print()
        # B.8 — in worker mode the FFT runs on the DSP worker thread
        # (driven by IQ block count, not this wall-clock timer).  The
        # worker emits raw spec_db via spectrum_raw_ready; Radio's
        # _on_worker_spectrum_raw slot does the post-FFT processing
        # (S-meter, noise floor, auto-scale, zoom, emits).  We keep
        # this timer running in worker mode so _radio_debug_maybe_print
        # still fires every 5 s — useful for verifying that worker
        # mode bypasses the main-thread DSP path (iq_batches stays 0).
        if self._dsp_worker is not None:
            return
        # Single-thread mode — read ring, compute FFT, post-process.
        spec_db = self._compute_spec_db()
        if spec_db is None:
            return
        self._process_spec_db(spec_db)

    def _compute_spec_db(self):
        """Read the sample ring, run FFT, apply un-mirror + cal,
        return ``spec_db`` (np.float32 array of length ``_fft_size``)
        or ``None`` if the ring isn't yet full enough.

        Refactored out of ``_tick_fft`` (B.8) so the same FFT body
        can run on the DSP worker thread in worker mode.  No state
        is mutated here — purely a read-and-transform of the sample
        ring.  Caller passes the returned array to
        ``_process_spec_db`` for everything downstream.
        """
        with self._ring_lock:
            if len(self._sample_ring) < self._fft_size:
                return None
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
        return spec_db

    def _on_worker_spectrum_raw(self, spec_db) -> None:
        """Slot for ``DspWorker.spectrum_raw_ready`` (B.8).

        Runs on the main thread (queued connection from the worker).
        Receives the raw post-FFT spectrum from the worker and runs
        all the UI-side post-processing through ``_process_spec_db``
        — identical to the back half of ``_tick_fft``.

        Splitting the FFT compute from the UI processing lets the
        worker handle the heavy numerical lift (np.fft.fft on a
        4096-point complex64 array) while keeping all UI / state
        machinery (auto-scale, zoom, S-meter mode, waterfall
        cadence) on the main thread where the rest of the UI lives.
        """
        if spec_db is None or len(spec_db) == 0:
            return
        # _dbg_fft_emits is incremented inside _process_spec_db right
        # after spectrum_ready.emit — no double-counting needed here.
        # §15.7 timing -- main-thread post-FFT processing latency.
        if self._timing_stats is not None:
            import time as _t
            _spec_t0 = _t.monotonic_ns()
            self._process_spec_db(spec_db)
            self._timing_stats.record(
                "spec_main_ms", _t.monotonic_ns() - _spec_t0)
        else:
            self._process_spec_db(spec_db)

    def _process_spec_db(self, spec_db):
        """Post-FFT processing — S-meter, noise floor, auto-scale,
        zoom, panadapter emit, waterfall emit.  Refactored out of
        ``_tick_fft`` (B.8) so the same body runs in both single-
        thread mode (called from ``_tick_fft`` on the main thread
        timer) and worker mode (called from
        ``_on_worker_spectrum_raw`` slot, also on main thread but
        triggered by a worker-thread signal).

        All state mutated here lives on the Radio instance and is
        only read/written from the main thread, so no synchronization
        is needed even in worker mode — Qt's queued connection
        ensures the slot runs on main, serialized with all other
        main-thread events.
        """
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
            lin = 10.0 ** (band / 10.0)            # dB → linear power
            # Cache passband peak (max bin in dBFS) for Auto-LNA
            # pull-up's passband-signal gate. Use the unsmoothed
            # spectrum so the gate reacts to real signal arrivals
            # within one FFT (~150 ms) rather than waiting for
            # smeter EWMA to catch up.
            self._lna_passband_peak_dbfs = float(np.max(band))
            # ── Mode semantics (S-meter cal review v0.0.9.6) ───────
            # Bandwidth dependence is the dominant source of S-meter
            # confusion: integrating power across a wider passband
            # accumulates more noise.  At a 2.4 kHz SSB filter and
            # ~47 Hz bin width, "integrated passband" runs ~+17 dB
            # over single-bin noise; at 8 kHz AM the offset jumps to
            # ~+22 dB; at 500 Hz CW it drops to ~+10 dB.  The
            # operator's +28 dB cal trim was tuned at one specific
            # mode/BW combination, so any mode/BW change makes the
            # reading appear "wildly incorrect" by tens of dB.
            #
            #   "peak"  — single brightest bin in the passband.  This
            #             is the standard ham-radio S-meter convention:
            #             reads the dominant tone level, BW-invariant
            #             for narrowband signals (CW dits, FT8, SSB
            #             voice peaks), tracks panadapter peaks
            #             one-for-one.  Operator can cal once and
            #             expect it to hold across mode/BW changes.
            #
            #   "avg"   — EWMA-smoothed peak.  Same shape as peak but
            #             with a ~1 s time constant for less twitch.
            #             Use for stable copy on noisy bands or to
            #             watch a slowly-fading signal.
            #
            # Earlier (pre-2026-05-07) both modes summed lin across
            # bins.  Switched to single-bin peak so the cal value
            # tracks reality.  Operator may want to nudge the +28 dB
            # cal trim once after this change since the absolute
            # numbers shift by +log10(passband_bins) compared to the
            # old integrated reading.
            peak_lin = float(np.max(lin))
            if self._smeter_mode == "avg":
                if self._smeter_avg_lin <= 0.0:
                    self._smeter_avg_lin = peak_lin
                else:
                    self._smeter_avg_lin = (0.80 * self._smeter_avg_lin
                                            + 0.20 * peak_lin)
                level_db = (10.0 * float(np.log10(max(self._smeter_avg_lin, 1e-20)))
                            + self._smeter_cal_db
                            - float(self._gain_db))
            else:  # "peak" — peak-hold-with-decay
                # Fast attack on rising peaks, slow exponential decay
                # on falling.  Matches analog mechanical S-meters and
                # Thetis's smooth meter feel; eliminates the per-FFT-
                # tick jitter operators perceive on voice content.
                if peak_lin > self._smeter_peak_hold_lin:
                    self._smeter_peak_hold_lin = peak_lin
                else:
                    # Decay toward current peak.  Held value blends
                    # toward the live peak by (1 - decay) per tick.
                    self._smeter_peak_hold_lin = (
                        self._SMETER_PEAK_DECAY * self._smeter_peak_hold_lin
                        + (1.0 - self._SMETER_PEAK_DECAY) * peak_lin
                    )
                level_db = (10.0 * float(np.log10(max(self._smeter_peak_hold_lin, 1e-20)))
                            + self._smeter_cal_db
                            - float(self._gain_db))
            self.smeter_level.emit(level_db)

        # Noise-floor estimate — 20th percentile rejects the upper 80%
        # of bins (which likely contain signals), leaving the ambient
        # noise.  Rolling-averaged over ~1 s to damp out FFT-to-FFT
        # jitter.  Emitted at ~6 Hz rather than every tick.
        #
        # v0.1 Phase 1 follow-up (2026-05-11) -- decouple noise-floor
        # MEASUREMENT from the NF reference-line DISPLAY gate.
        # Pre-fix behavior: the entire measurement block was gated
        # on ``self._noise_floor_enabled``, the same flag that
        # controls whether the dim NF reference line is drawn in
        # the panadapter.  When the operator hid the NF marker via
        # Settings → Visuals (or hadn't enabled it after a fresh
        # install), ``_noise_floor_db`` froze at whatever stale
        # value it last had.  ``auto_set_agc_threshold`` reads
        # ``_noise_floor_db`` every 3 s and pushes ``nf + margin``
        # to WDSP as the AGC threshold, so the AGC auto-threshold
        # silently sat at a stale value across band / antenna /
        # mode changes for hours.  Operator-visible symptom: AGC
        # threshold static at -130 dBFS, AGC over-amplifying noise,
        # "audio low + NR acts as pre-amp on AM" complaint.
        #
        # Fix: measurement always runs (cheap: one np.percentile
        # call); the display emit is gated separately below.
        pct20 = float(np.percentile(spec_db, 20))

        # Update rolling buffer + EMA -- always runs so AGC auto-
        # threshold sees a fresh nf value even when NF display is
        # hidden.
        self._noise_floor_history.append(pct20)
        if len(self._noise_floor_history) > self._noise_floor_history_max:
            self._noise_floor_history.pop(0)
        avg = float(np.mean(self._noise_floor_history))

        # Exponential smoothing on top of the rolling average for
        # extra stability — reference-line should feel rock-steady.
        # Always runs so AGC auto-threshold sees a fresh value even
        # when NF display is hidden.
        if self._noise_floor_db is None:
            self._noise_floor_db = avg
        else:
            self._noise_floor_db = 0.85 * self._noise_floor_db + 0.15 * avg
        # Display-only emit -- the reference line stays hidden when
        # the operator has it disabled.
        if self._noise_floor_enabled:
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
                # Per-edge user locks (2026-05-08).  See __init__
                # comment for the design rationale.  Earlier rev
                # ignored the user range entirely to dodge a pinch
                # bug; now we lock per-edge instead of the whole
                # window, so the floor (which has no auto-driven
                # reason to ever change) honors the operator's drag
                # while the ceiling still rises to fit signals.
                #
                #   Floor lock: hard.  target_lo = operator's value.
                #     A strong signal can't push the floor down; that
                #     would make no sense (the noise floor sets the
                #     visual reference, not the peaks).
                #
                #   Ceiling lock: soft.  target_hi = max(operator,
                #     peak + headroom).  If a strong signal arrives
                #     we still show it; if everything's weak, the
                #     ceiling sits at the operator's preferred
                #     headroom so weak signals don't get squeezed
                #     into the bottom of the display.
                #
                # The original "pinch" failure case (operator
                # accidentally narrows to -121..-109) becomes
                # benign: the floor stays at -121, but the ceiling
                # rises to peak+15 if signals exceed -109.  No more
                # off-scale clipping.
                if self._user_floor_locked:
                    target_lo = self._user_range_min_db
                else:
                    target_lo = nf - self.AUTO_SCALE_NOISE_HEADROOM_DB
                peak_target = pk_max + self.AUTO_SCALE_PEAK_HEADROOM_DB
                if self._user_ceiling_locked:
                    target_hi = max(self._user_range_max_db, peak_target)
                else:
                    target_hi = peak_target
                # Guarantee a comfortably wide scale even on bands
                # with vanishingly small dynamic range (very weak
                # signals on a quiet noise floor).
                if target_hi - target_lo < self.AUTO_SCALE_MIN_SPAN_DB:
                    target_hi = target_lo + self.AUTO_SCALE_MIN_SPAN_DB
                # Final safety clamp guards against pathological
                # values (corrupt persistence, etc.).
                target_lo = max(-150.0, min(-3.0, target_lo))
                target_hi = max(target_lo + 3.0, min(0.0, target_hi))
                # Internal call — `from_user=False` updates only the
                # live display range, NOT the user bounds.
                self.set_spectrum_db_range(
                    target_lo, target_hi, from_user=False)
                # Mirror the same range to the waterfall so its
                # heatmap fits the band's actual dynamic range too —
                # but ONLY if the operator has waterfall auto-scale
                # enabled (default).  Some operators prefer a fixed
                # darker waterfall so weaker signals 'pop' against a
                # near-black background; turning waterfall auto-scale
                # off in Settings lets them keep that look while the
                # spectrum still auto-fits.
                #
                # v0.0.9.8.1 fix: also skip the auto-mirror when the
                # current band has a per-band MANUALLY-SET waterfall
                # range in band_memory.  Without this skip, the auto-
                # tick (every ~2 sec) would overwrite the operator's
                # restored manual values 2 seconds after every
                # startup → "Waterfall Min-Max isn't staying where
                # set with manual when you restart Lyra" (operator-
                # reported 2026-05-10).  The from_user=False arg to
                # set_waterfall_db_range was supposed to prevent
                # this but only protects the band_memory write —
                # the live ``_waterfall_min_db / _max_db`` fields
                # (and the visible heatmap) still got overwritten.
                if self._waterfall_auto_scale:
                    cur_band = band_for_freq(int(self._freq_hz))
                    has_manual_wf = (
                        cur_band is not None
                        and "waterfall_min_db" in self._band_memory.get(
                            cur_band.name, {}))
                    if not has_manual_wf:
                        self.set_waterfall_db_range(
                            target_lo, target_hi, from_user=False)
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

        # Emit DDS freq (= VFO ± cw_pitch in CW modes) as the
        # spectrum's center, since that's where the FFT data is
        # actually centered.  Spectrum widget handles marker
        # placement separately via marker_offset_hz so the
        # operator's tuned freq lands on the carrier visually
        # under v0.0.9.8's carrier-freq VFO convention.
        #
        # Phase 3.E.1 v0.1 (2026-05-12): when panadapter source is
        # RX2, emit RX2's DDS freq as the spectrum center so the
        # widget retunes to the new band.
        #
        # Phase 3.E.1 hotfix v0.12 (2026-05-12): use
        # ``_compute_dds_freq_hz(target_rx=2)`` instead of raw
        # ``_rx2_freq_hz``.  Since hotfix v0.8 (commit ccfc76e)
        # the RX2 DDS is offset by ±pitch in CWU/CWL the same way
        # RX1's is, so the spectrum bins are centered on
        # DDS_RX2 = VFO_RX2 ∓ pitch -- NOT on VFO_RX2 itself.
        # Pre-fix, the widget thought bins were centered on
        # VFO_RX2, so click-to-tune on a CW peak landed off by
        # exactly one pitch (~650 Hz at default).  Operator
        # report 2026-05-12: "RX2 clicking on CW in panadapter
        # doesn't tune the same way RX1 does, RX2 seems off
        # (almost like not accounting for the CW pitch perhaps?)".
        # The widget's ``marker_offset_hz`` mechanic shifts the
        # visible marker back to the operator's tuned carrier --
        # same way RX1's path already works.
        if self._panadapter_source_rx == 2:
            center_hz = float(self._compute_dds_freq_hz(target_rx=2))
        else:
            center_hz = float(self._compute_dds_freq_hz())
        # §15.7 timing -- measure spectrum→waterfall emit gap.
        # Should be sub-millisecond (Tech #3 confirmed both fire
        # atomically from same function); this is the regression
        # marker.  Captured around the spectrum emit so the delta
        # is computed at waterfall emit time below.
        if self._timing_stats is not None:
            import time as _t
            _wf_spec_emit_t = _t.monotonic_ns()
        else:
            _wf_spec_emit_t = 0
        self.spectrum_ready.emit(spec_out, center_hz, eff_rate)
        if self._radio_debug:
            self._dbg_fft_emits += 1

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
                # §15.7 timing -- record gap on first emit only.
                if self._timing_stats is not None and _wf_spec_emit_t > 0:
                    import time as _t
                    self._timing_stats.record(
                        "wf_offset_ms",
                        _t.monotonic_ns() - _wf_spec_emit_t)
                for _ in range(mult):
                    self.waterfall_ready.emit(
                        spec_out, float(self._compute_dds_freq_hz()), eff_rate)
            else:
                # Hot path: emit M interpolated frames spanning
                # prev → spec_out. The kth (1-based) frame is
                # prev*(1 - k/M) + spec_out*(k/M), so the LAST
                # frame is the actual current spectrum and the
                # earlier frames bridge the gap from prev.
                # §15.7 timing -- record gap on first emit only.
                if self._timing_stats is not None and _wf_spec_emit_t > 0:
                    import time as _t
                    self._timing_stats.record(
                        "wf_offset_ms",
                        _t.monotonic_ns() - _wf_spec_emit_t)
                for k in range(1, mult + 1):
                    t = k / mult
                    frame = (prev * (1.0 - t) + spec_out * t).astype(
                        spec_out.dtype, copy=False)
                    self.waterfall_ready.emit(
                        frame, float(self._compute_dds_freq_hz()), eff_rate)
            # Snapshot for next tick's interpolation. Use a copy so
            # the consumer side doesn't see future mutations.
            self._wf_prev_spec = np.array(spec_out, copy=True)
