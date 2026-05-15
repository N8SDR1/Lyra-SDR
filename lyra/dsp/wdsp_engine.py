"""
Lyra WDSP engine — high-level RX channel wrapper.

Sits on top of `wdsp_native` and gives Lyra a clean Python interface:

    from lyra.dsp.wdsp_engine import RxChannel, RxConfig

    rx = RxChannel(channel=0, cfg=RxConfig(
        in_size=512, in_rate=192_000, dsp_rate=96_000, out_rate=48_000,
    ))
    rx.set_mode("USB")
    rx.set_filter(low=200.0, high=3000.0)
    rx.set_agc("MED")
    rx.start()

    # Per audio block:
    audio_lr = rx.process(iq_block)   # iq_block: complex64 [in_size]
                                       # audio_lr: float32   [in_size, 2]

The channel index space is shared with WDSP's internal model + the
locked host-channel-ID convention from consensus plan §2.2:

    0  — RX1 main
    1  — RX1 sub-receiver (reserved, post-v0.3)
    2  — RX2 main
    3  — (HL2: PS-feedback alias) / (5-DDC ANAN: RX2-sub)
    4  — TX main (TXA)               -- see wdsp_tx_engine.TxChannel
    5  — PS feedback A on 5-DDC ANAN (future)
    6  — PS feedback B on 5-DDC ANAN (future)

(Corrected 2026-05-15: earlier comment listed "2 — TX" which was wrong
per consensus §2.2 Round 2 verification.  Channel 2 is RX2, channel 4
is TX.  Agent M Round 3 audit flagged the stale comment.)

This module exposes RX channels only.  TxChannel sibling lives in
``wdsp_tx_engine.py``.

Buffer layout
-------------
fexchange0 takes interleaved I/Q doubles in (one block of ``in_size`` frames
at ``in_rate``) and returns interleaved L/R doubles out (one block of
``out_size`` frames at ``out_rate``).

The two block sizes are NOT the same — they represent the same real-time
interval at different sample rates. WDSP computes:

    out_size = in_size * out_rate / in_rate    (when in_rate >= out_rate)

So with in_size=1024, in_rate=192000, out_rate=48000:
    out_size = 1024 * 48000 / 192000 = 256 frames out per call.

Both blocks cover the same 1024/192000 = 5.33 ms of real time.

Lyra prefers numpy arrays. This wrapper converts:

    Python:  numpy complex64 [in_size]      ⇄  C: double[2*in_size]  (I,Q,...)
    Python:  numpy float32   [out_size, 2]  ⇄  C: double[2*out_size] (L,R,...)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from lyra.dsp import wdsp_native


# ---------------------------------------------------------------------------
# Mode / AGC string → integer mapping
# ---------------------------------------------------------------------------

_MODE_BY_NAME = {
    "LSB":  wdsp_native.RxaMode.LSB,
    "USB":  wdsp_native.RxaMode.USB,
    "DSB":  wdsp_native.RxaMode.DSB,
    "CWL":  wdsp_native.RxaMode.CWL,
    "CWU":  wdsp_native.RxaMode.CWU,
    "FM":   wdsp_native.RxaMode.FM,
    "AM":   wdsp_native.RxaMode.AM,
    "DIGU": wdsp_native.RxaMode.DIGU,
    "DIGL": wdsp_native.RxaMode.DIGL,
    "SAM":  wdsp_native.RxaMode.SAM,
    "DRM":  wdsp_native.RxaMode.DRM,
    "SPEC": wdsp_native.RxaMode.SPEC,
}

_AGC_BY_NAME = {
    "FIXED": wdsp_native.AgcMode.FIXED,
    "FIXD":  wdsp_native.AgcMode.FIXED,
    "OFF":   wdsp_native.AgcMode.FIXED,
    "LONG":  wdsp_native.AgcMode.LONG,
    "SLOW":  wdsp_native.AgcMode.SLOW,
    "MED":   wdsp_native.AgcMode.MED,
    "MEDIUM": wdsp_native.AgcMode.MED,
    "FAST":  wdsp_native.AgcMode.FAST,
    "CUSTOM": wdsp_native.AgcMode.CUSTOM,
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RxConfig:
    """Per-channel sample rates and buffer size.

    Defaults match the working Thetis HL2 setup: 1024-frame input blocks at
    192 kHz IQ, 4096-sample internal DSP buffer at 48 kHz, 48 kHz audio out.
    """
    in_size: int = 1024       # frames per process() call
    dsp_size: int = 4096      # internal DSP buffer size
    in_rate: int = 192_000    # IQ input rate (HL2 default)
    dsp_rate: int = 48_000    # WDSP internal DSP rate
    out_rate: int = 48_000    # audio output rate
    # Slew envelope (avoids click on start/stop)
    tdelayup: float = 0.010
    tslewup: float = 0.025
    tdelaydown: float = 0.000
    tslewdown: float = 0.010
    # "block until output available" — 1 = fexchange0 blocks until DSP has
    # produced the next output buffer. Required for a steady cadence.
    block: int = 1


# ---------------------------------------------------------------------------
# RxChannel
# ---------------------------------------------------------------------------

class RxChannel:
    """A single WDSP receiver channel.

    Threading model:
      * `process()` is the only hot-path call and is NOT thread-safe within a
        channel. Call it from one thread (the RX worker).
      * Configuration setters (set_mode, set_filter, set_agc, set_panel_gain,
        run flags) ARE safe to call from another thread; they take an internal
        lock so they don't race with each other or with start/stop.

    Multiple RxChannel instances on different `channel` indexes are
    independent and can be processed in parallel.
    """

    def __init__(self, channel: int, cfg: Optional[RxConfig] = None):
        self.channel = int(channel)
        self.cfg = cfg or RxConfig()

        self._lib = wdsp_native.load()
        self._ffi = wdsp_native.ffi()

        # Compute output block size: WDSP picks out_size = in_size * out_rate / in_rate
        # when in_rate >= out_rate, otherwise the inverse. We mirror that here so
        # the output buffer matches what fexchange0 actually writes.
        if self.cfg.in_rate >= self.cfg.out_rate:
            ratio = self.cfg.in_rate // self.cfg.out_rate
            self.out_size = self.cfg.in_size // ratio
        else:
            ratio = self.cfg.out_rate // self.cfg.in_rate
            self.out_size = self.cfg.in_size * ratio

        # Pre-allocate I/O buffers on the C side, sized correctly for each direction.
        # Input  is `in_size` complex frames  = 2 * in_size  doubles.
        # Output is `out_size` complex frames = 2 * out_size doubles.
        self._in_buff = self._ffi.new(f"double[{2 * self.cfg.in_size}]")
        self._out_buff = self._ffi.new(f"double[{2 * self.out_size}]")
        self._err = self._ffi.new("int*")
        # Optional scratch buffer used when an EXT noise blanker is
        # active.  IQ flows _in_buff -> xnobEXT/xanbEXT -> _nb_buff
        # -> fexchange0.  Allocated once; size matches in_buff.
        self._nb_buff = self._ffi.new(f"double[{2 * self.cfg.in_size}]")
        self._nob_running = False
        self._anb_running = False

        # Zero-copy numpy views over the cffi buffers.
        self._in_view = np.frombuffer(
            self._ffi.buffer(self._in_buff), dtype=np.float64
        )
        self._out_view = np.frombuffer(
            self._ffi.buffer(self._out_buff), dtype=np.float64
        )

        # Internal IQ accumulation buffer for variable-length process_stream()
        # calls. Lyra's RX path delivers IQ in batches whose size depends on
        # the HPSDR EP6 frame layout and the operator's batch-size setting,
        # which is generally NOT a multiple of `in_size`. We append into
        # `_in_buf`, pull whole `in_size` blocks out, and carry the remainder.
        self._in_accum = np.empty(0, dtype=np.complex64)

        self._lock = threading.Lock()
        self._opened = False
        self._running = False
        self._blankers_inited = False
        # Active manual notches.  list[(idx, fcenter, fwidth, active)]
        # — populated by set_notches and used to rebuild WDSP's NotchDB
        # on rate-change re-open.
        self._notches: list[tuple[int, float, float, bool]] = []
        self._notches_master_run = False

        self._open()
        # NB EXT-blanker objects must exist before SetEXTNOBRun /
        # SetEXTANBRun is safe to call.  init_blankers is idempotent,
        # creates with run=0 / threshold=20 (silent), and the NB
        # profile setter in Radio drives the run flag + threshold to
        # match the operator's selection.
        try:
            self.init_blankers()
        except Exception as exc:
            # Not fatal — log but keep the channel usable; the NB
            # toggle path checks _blankers_inited and no-ops if init
            # failed.
            print(f"[WDSP] init_blankers failed for ch {self.channel}: {exc}")

    # -- lifecycle -------------------------------------------------------

    def _open(self) -> None:
        if self._opened:
            return
        c = self.cfg
        self._lib.OpenChannel(
            self.channel,
            c.in_size, c.dsp_size,
            c.in_rate, c.dsp_rate, c.out_rate,
            0,                    # type = RX
            0,                    # state = stopped (start() will run it)
            c.tdelayup, c.tslewup,
            c.tdelaydown, c.tslewdown,
            c.block,
        )
        self._opened = True

    def start(self) -> None:
        """Begin processing on this channel."""
        with self._lock:
            if not self._opened:
                self._open()
            if not self._running:
                self._lib.SetChannelState(self.channel, 1, 0)
                self._running = True

    def stop(self) -> None:
        """Stop processing without destroying state. Output slews down cleanly."""
        with self._lock:
            if self._opened and self._running:
                self._lib.SetChannelState(self.channel, 0, 0)
                self._running = False

    def close(self) -> None:
        """Stop and free the channel."""
        # Tear down the EXT blankers BEFORE CloseChannel so the DLL's
        # global blanker table doesn't outlive the channel slot.
        try:
            self.destroy_blankers()
        except Exception:
            pass
        with self._lock:
            if self._opened:
                if self._running:
                    self._lib.SetChannelState(self.channel, 0, 1)
                    self._running = False
                self._lib.CloseChannel(self.channel)
                self._opened = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- configuration setters ------------------------------------------

    def set_mode(self, mode: str | int) -> None:
        m = _MODE_BY_NAME[mode.upper()] if isinstance(mode, str) else int(mode)
        with self._lock:
            self._lib.SetRXAMode(self.channel, m)

    def set_filter(self, low: float, high: float) -> None:
        """Set the RX channel's bandpass (Hz). For SSB this also selects
        the sideband — positive freqs for USB, negative for LSB.

        Routes through ``RXASetPassband`` which updates the NBP0 (front-
        of-chain narrow bandpass — what selects the sideband for SSB
        demod), the BP1 (post-NR bandpass), and the SNBA output filter
        in one call. ``SetRXABandpassFreqs`` alone only updates BP1,
        which is bypassed unless an NR / ANF / AM-demod module is
        running, so the sideband selection wouldn't take effect with all
        DSP off.
        """
        with self._lock:
            self._lib.RXASetPassband(self.channel, float(low), float(high))

    def set_agc(self, mode: str | int) -> None:
        m = _AGC_BY_NAME[mode.upper()] if isinstance(mode, str) else int(mode)
        with self._lock:
            self._lib.SetRXAAGCMode(self.channel, m)

    def set_agc_fixed_gain_db(self, gain_db: float) -> None:
        with self._lock:
            self._lib.SetRXAAGCFixed(self.channel, float(gain_db))

    def set_agc_max_gain_db(self, max_gain_db: float) -> None:
        with self._lock:
            self._lib.SetRXAAGCTop(self.channel, float(max_gain_db))

    # ── Per-parameter AGC setters ─────────────────────────────────────
    #
    # Thin wrappers around WDSP's individual AGC parameter setters
    # (wcpAGC.c).  ``SetRXAAGCMode`` already sets the per-profile
    # ``hangtime`` and ``tau_decay`` fields directly inside its switch
    # statement (wcpAGC.c lines 384-407), so the Decay/Hang setters
    # below are redundant for the canonical FAST/MED/SLOW/LONG
    # presets.  They exist for the Custom profile and for any future
    # operator-facing time-constant sliders.
    #
    # Threshold / Slope / Top / Fixed all write to engine fields that
    # ``SetRXAAGCMode`` does NOT touch — those need explicit pushes if
    # the operator wants to override the wcpAGC.c create-time defaults
    # (see RXA.c ``create_wcpagc`` for the init values).

    def set_agc_attack_ms(self, attack_ms: int) -> None:
        """AGC attack time in milliseconds.  WDSP's wcpAGC.c init
        default is 1 ms (RXA.c create_wcpagc tau_attack=0.001)."""
        with self._lock:
            self._lib.SetRXAAGCAttack(self.channel, int(attack_ms))

    def set_agc_decay_ms(self, decay_ms: int) -> None:
        """AGC decay time in milliseconds.  Redundant with the
        per-profile values inside SetRXAAGCMode (wcpAGC.c case
        statements 1=LONG/2000, 2=SLOW/500, 3=MED/250, 4=FAST/50);
        useful for the Custom profile only."""
        with self._lock:
            self._lib.SetRXAAGCDecay(self.channel, int(decay_ms))

    def set_agc_hang_ms(self, hang_ms: int) -> None:
        """AGC hang time in milliseconds.  Redundant with the
        per-profile values inside SetRXAAGCMode (wcpAGC.c:
        LONG=2000, SLOW=1000, MED=0, FAST=0); useful for the
        Custom profile only."""
        with self._lock:
            self._lib.SetRXAAGCHang(self.channel, int(hang_ms))

    def set_agc_slope(self, slope: int) -> None:
        """AGC compression slope, in 0.1 dB units (WDSP convention).
        Pushes to engine field ``var_gain`` via
        ``var_gain = pow(10, slope / 200)`` — i.e., slope=0 →
        var_gain=1.0; slope=35 → var_gain≈1.5 (WDSP create-time
        default ≈ 3.5 dB).  ``var_gain`` then drives the
        threshold→max_gain calculation in SetRXAAGCThresh.

        v0.0.9.8 fix: parameter type was previously declared as
        ``double`` in the cffi binding but the WDSP C function
        signature is ``int slope``.  On Windows x86_64 that
        mismatch caused a register-class bug — cffi passed the
        value via XMM1 (the double slot) but the C function read
        RDX (the int slot), getting garbage.  Result was a
        randomly-set var_gain at every channel open, max_gain
        therefore wonky, and AGC stuck at whatever max_gain
        happened to land on — which presented as "AGC profiles
        all sound the same" / "gain meter pinned" since v0.0.9.6.
        See lyra/dsp/wdsp_native.py for the binding side of the
        same fix.
        """
        with self._lock:
            self._lib.SetRXAAGCSlope(self.channel, int(slope))

    def set_agc_hang_threshold(self, hang_threshold: int) -> None:
        """AGC hang-engagement threshold (operator-tunable).
        SetRXAAGCMode sets hang_thresh=1.0 for MED/FAST cases;
        SLOW/LONG inherit whatever was last set (operator-tunable
        or create-time default 0.250)."""
        with self._lock:
            self._lib.SetRXAAGCHangThreshold(self.channel, int(hang_threshold))

    def set_agc_threshold(self,
                          thresh_db: float,
                          fft_size: int,
                          sample_rate: int) -> None:
        """AGC threshold dB (where AGC engages above noise floor).
        Computes max_gain = out_target / (var_gain *
        10^((thresh + noise_offset)/20)) where noise_offset
        depends on the active passband, FFT size, and sample
        rate.  See wcpAGC.c::SetRXAAGCThresh for the exact math.

        WARNING: this setter writes the same engine field as
        SetRXAAGCTop (max_gain).  Do not call both unless the
        intent is to override Threshold's calculation; the
        later call wins."""
        with self._lock:
            self._lib.SetRXAAGCThresh(
                self.channel,
                float(thresh_db),
                float(fft_size),
                float(sample_rate),
            )

    def set_panel_binaural(self, binaural: bool) -> None:
        """Set the panel's binaural mode.

        binaural=False (default): panel.copy=1, copies I to Q at the
            panel output → mono on both channels (L=R) regardless
            of any upstream stage that zeroed Q (e.g. EMNR).  This
            is what Thetis uses for the default listening mode and
            what fixes the AM/FM/DSB-only-left-channel bug for
            Lyra (see CLAUDE.md §14.10).
        binaural=True: panel.copy=0, no copy.  L=I, R=Q from the
            panel's input.  Use this only when the upstream chain
            is genuinely producing distinct I/Q audio that the
            operator wants split L/R.  Lyra normally uses Python
            post-WDSP BinauralFilter for the BIN feature instead.
        """
        with self._lock:
            self._lib.SetRXAPanelBinaural(
                self.channel, 1 if bool(binaural) else 0)

    def set_panel_gain(self, gain: float) -> None:
        """Linear gain on the post-DSP audio (0..1+, 1.0 = unity)."""
        with self._lock:
            self._lib.SetRXAPanelGain1(self.channel, float(gain))

    def set_panel_pan(self, pan: float) -> None:
        """Set the panel's L/R pan position via the WDSP sin-π
        equal-power pan curve (``patchpanel.c::SetRXAPanelPan``).

        Phase 2 v0.1 (2026-05-11): used for the RX2 stereo-split
        default routing -- RX1 pan=0.0 (hard left), RX2 pan=1.0
        (hard right), summed in ``Radio._do_demod_wdsp_dual`` for
        the final stereo output.  Per consensus plan §5.1 IM-4 the
        pan math lives in WDSP cffi only -- no Python port.

        Args:
            pan: 0.0..1.0 inclusive.  0.0 = hard left (L=signal,
                R=0), 0.5 = center (both at sin(π/4) ≈ 0.707), 1.0
                = hard right (L=0, R=signal).  Clamped to [0, 1]
                before pushing to WDSP -- out-of-range values would
                produce silence or invert phase, neither of which is
                useful.

        Note this only takes effect when ``set_panel_binaural``
        is False (panel.copy=1 mono-on-both, Lyra's default per
        CLAUDE.md §14.10).  With binaural=True (panel.copy=0,
        L=I/R=Q), the pan curve is applied to I and Q channels
        independently, which is rarely useful for normal listening.
        """
        p = max(0.0, min(1.0, float(pan)))
        with self._lock:
            self._lib.SetRXAPanelPan(self.channel, p)

    # Per-module run flags ------------------------------------------------

    def set_anr(self, run: bool) -> None:
        """LMS adaptive line enhancer (Lyra LMS / WDSP ANR)."""
        with self._lock:
            self._lib.SetRXAANRRun(self.channel, int(bool(run)))

    def set_anf(self, run: bool) -> None:
        """Auto-notch filter."""
        with self._lock:
            self._lib.SetRXAANFRun(self.channel, int(bool(run)))

    def set_emnr(self, run: bool) -> None:
        """Ephraim-Malah / MMSE-LSA spectral noise reduction
        (Lyra NR1 / NR2 / WDSP EMNR)."""
        with self._lock:
            self._lib.SetRXAEMNRRun(self.channel, int(bool(run)))

    # ── EMNR live-tuning knobs ─────────────────────────────────────
    #
    # Exposes WDSP's per-channel EMNR character knobs.  Used by Radio
    # to give the operator's NR backend selector audible meaning
    # (NR1 vs NR2 → different gain method) and to put the AEPF
    # (anti-musical-noise) toggle in the operator's hands.
    #
    # WDSP's defaults (RXA.c create_emnr): gain_method=2, npe_method=0,
    # ae_run=1.  Lyra mirrors these as defaults; the setters below
    # let the operator nudge any of them.

    # gain_method codes (from emnr.c switch in calc_window):
    #   0 — Wiener gain
    #   1 — Magnitude estimator
    #   2 — MMSE-LSA gain (WDSP default)
    EMNR_GAIN_WIENER   = 0
    EMNR_GAIN_MAGNITUDE = 1
    EMNR_GAIN_MMSE_LSA = 2

    def set_emnr_gain_method(self, method: int) -> None:
        """Pick the EMNR gain function — different sonic character.

        ``method`` ∈ {EMNR_GAIN_WIENER, EMNR_GAIN_MAGNITUDE,
        EMNR_GAIN_MMSE_LSA}.  Operator typically picks one of the
        three at the backend-selector level."""
        with self._lock:
            self._lib.SetRXAEMNRgainMethod(self.channel, int(method))

    def set_emnr_npe_method(self, method: int) -> None:
        """Pick the EMNR noise-power estimator (background tracker)."""
        with self._lock:
            self._lib.SetRXAEMNRnpeMethod(self.channel, int(method))

    def set_emnr_aepf(self, run: bool) -> None:
        """Adaptive Equalization Post-Filter — REDUCES musical noise.

        Default ON.  Operator-disable only for diagnostic A/B.  Off
        gives raw EMNR character (more pronounced watery sound on
        spectral subtraction); On smooths the gain mask across
        frequency bins and damps the bin-by-bin transitions that
        cause musical noise."""
        with self._lock:
            self._lib.SetRXAEMNRaeRun(self.channel, int(bool(run)))

    def set_emnr_ae_zeta_thresh(self, thresh: float) -> None:
        """Fine-tune AEPF — operator-tunable threshold (advanced)."""
        with self._lock:
            self._lib.SetRXAEMNRaeZetaThresh(self.channel, float(thresh))

    def set_emnr_ae_psi(self, psi: float) -> None:
        """Fine-tune AEPF — operator-tunable psi (advanced)."""
        with self._lock:
            self._lib.SetRXAEMNRaePsi(self.channel, float(psi))

    # ── ANR (LMS) live-tuning knobs ────────────────────────────────
    #
    # WDSP's ANR is the LMS adaptive line enhancer.  Lyra's "LMS"
    # toggle and strength slider drive it.  Defaults from RXA.c
    # create_anr: taps=64, delay=16, two_mu=0.0001, gamma=0.1.
    # The "Gain" exported setter corresponds internally to two_mu
    # (LMS step size = the operator-perceived "strength").

    def set_anr_vals(self, taps: int, delay: int,
                     gain: float, leakage: float) -> None:
        """Push all four ANR/LMS tuning params atomically."""
        with self._lock:
            self._lib.SetRXAANRVals(
                self.channel, int(taps), int(delay),
                float(gain), float(leakage),
            )

    def set_anr_taps(self, taps: int) -> None:
        with self._lock:
            self._lib.SetRXAANRTaps(self.channel, int(taps))

    def set_anr_delay(self, delay: int) -> None:
        with self._lock:
            self._lib.SetRXAANRDelay(self.channel, int(delay))

    def set_anr_gain(self, gain: float) -> None:
        """LMS step size (the operator-perceived strength).

        WDSP default 0.0001.  Stable upper bound roughly 0.001.
        Higher = more aggressive adaptation but risk of instability.
        """
        with self._lock:
            self._lib.SetRXAANRGain(self.channel, float(gain))

    def set_anr_leakage(self, leakage: float) -> None:
        """LMS leak factor (forgetting rate)."""
        with self._lock:
            self._lib.SetRXAANRLeakage(self.channel, float(leakage))

    def set_anf_vals(self, taps: int, delay: int,
                     gain: float, leakage: float) -> None:
        """Push all four ANF tuning params atomically.

        ``taps``    — filter length (anf.c default 64)
        ``delay``   — decorrelation delay samples (default 16)
        ``gain``    — adaptation step size (anf.c hint: ≈ 1e-4)
        ``leakage`` — leakage factor (anf.c hint: ≈ 0.10)

        Calls ``flush_anf`` internally to clear adaptive state.
        Without this push, ANF runs at WDSP create-time defaults
        (n_taps=64, delay=16, two_mu=1e-4, gamma=0.001) and the
        operator's μ slider in Settings → Noise has no effect.
        """
        with self._lock:
            self._lib.SetRXAANFVals(
                self.channel, int(taps), int(delay),
                float(gain), float(leakage),
            )

    # ── EXT noise-blanker lifecycle ────────────────────────────────
    #
    # The EXT noise blankers (NOB / ANB) are NOT created by
    # OpenChannel — calling SetEXTNOBRun / SetEXTANBRun before
    # ``init_blankers`` segfaults the DLL.  __init__ calls
    # ``init_blankers`` once per channel right after the channel is
    # opened; ``destroy_blankers`` runs from ``close()``.

    # Defaults match Thetis Console radio.cs nb_threshold/nb_tau/
    # nb_advtime/nb_hangtime defaults (3.3 / 50 µs / 50 µs / 50 µs).
    # The very tight tau/advtime/hangtime values are correct for
    # impulse-only behavior — longer values would notch out wanted
    # CW or SSB transients.
    _NB_DEFAULTS_NOB = dict(
        mode=0,
        slewtime=0.0001,
        hangtime=0.0001,
        advtime=0.0001,
        backtau=0.020,
        threshold=20.0,        # initially silent; set_nob_threshold tunes
    )
    _NB_DEFAULTS_ANB = dict(
        tau=0.00005,
        hangtime=0.00005,
        advtime=0.00005,
        backtau=0.020,
        threshold=20.0,
    )

    def init_blankers(self) -> None:
        """Create the EXT-layer NOB + ANB blankers for this channel.

        Idempotent — safe to call again after :meth:`destroy_blankers`.
        Defaults are conservative (run=0, threshold=20) so the channel
        doesn't actually blank until the operator opts in via
        :meth:`set_nob` / :meth:`set_anb` and the appropriate threshold
        setter.
        """
        if self._blankers_inited:
            return
        c = self.cfg
        with self._lock:
            self._lib.create_nobEXT(
                self.channel, 0, self._NB_DEFAULTS_NOB["mode"],
                c.in_size, float(c.in_rate),
                self._NB_DEFAULTS_NOB["slewtime"],
                self._NB_DEFAULTS_NOB["hangtime"],
                self._NB_DEFAULTS_NOB["advtime"],
                self._NB_DEFAULTS_NOB["backtau"],
                self._NB_DEFAULTS_NOB["threshold"],
            )
            self._lib.create_anbEXT(
                self.channel, 0,
                c.in_size, float(c.in_rate),
                self._NB_DEFAULTS_ANB["tau"],
                self._NB_DEFAULTS_ANB["hangtime"],
                self._NB_DEFAULTS_ANB["advtime"],
                self._NB_DEFAULTS_ANB["backtau"],
                self._NB_DEFAULTS_ANB["threshold"],
            )
            self._blankers_inited = True

    def destroy_blankers(self) -> None:
        """Free the EXT-layer NOB + ANB blankers for this channel."""
        if not self._blankers_inited:
            return
        with self._lock:
            try:
                self._lib.destroy_nobEXT(self.channel)
            except Exception:
                pass
            try:
                self._lib.destroy_anbEXT(self.channel)
            except Exception:
                pass
            self._blankers_inited = False

    def set_nob(self, run: bool) -> None:
        """Noise-OFF blanker — narrowband impulse blanker on raw IQ
        before the RXA chain.  Use for clicks / popcorn impulse noise.

        :meth:`init_blankers` must have been called first or the DLL
        will segfault.  ``__init__`` does it for you.

        Note: the EXT blanker is NOT spliced into fexchange0 by the
        DLL — Lyra has to call ``xnobEXT`` on each IQ block before
        handing it to fexchange0.  ``process_block`` does this
        automatically based on the ``_nob_running`` flag mirrored
        below.
        """
        with self._lock:
            if not self._blankers_inited:
                # Defensive — silent no-op rather than segfault.
                return
            run_flag = bool(run)
            self._lib.SetEXTNOBRun(self.channel, int(run_flag))
            self._nob_running = run_flag

    def set_anb(self, run: bool) -> None:
        """Advanced noise blanker — broadband impulse blanker on raw IQ
        before the RXA chain.  Use for switching power-supply hash and
        similar broadband impulse interference."""
        with self._lock:
            if not self._blankers_inited:
                return
            run_flag = bool(run)
            self._lib.SetEXTANBRun(self.channel, int(run_flag))
            self._anb_running = run_flag

    def set_nob_threshold(self, threshold: float) -> None:
        """Adjust NOB impulse-detection threshold (raw signal-to-average
        ratio).  Lower values = more aggressive blanking but more
        false positives.  Typical range 2..20."""
        with self._lock:
            if not self._blankers_inited:
                return
            self._lib.SetEXTNOBThreshold(self.channel, float(threshold))

    def set_anb_threshold(self, threshold: float) -> None:
        """Adjust ANB impulse-detection threshold."""
        with self._lock:
            if not self._blankers_inited:
                return
            self._lib.SetEXTANBThreshold(self.channel, float(threshold))

    def set_fm_squelch(self, run: bool) -> None:
        with self._lock:
            self._lib.SetRXAFMSQRun(self.channel, int(bool(run)))

    def set_fm_squelch_threshold(self, threshold: float) -> None:
        """FM squelch threshold (fmsq.c::SetRXAFMSQThreshold).
        Sets ``tail_thresh`` directly and ``unmute_thresh`` to
        0.9 × threshold.  Higher value = tighter squelch.

        Wired in Phase 6.A4 — previously the operator's squelch
        threshold slider drove SSQL but never reached FM's
        independent threshold, so FM-mode squelch ran at the
        WDSP default regardless of the slider position.
        """
        with self._lock:
            self._lib.SetRXAFMSQThreshold(self.channel, float(threshold))

    def set_am_squelch(self, run: bool, threshold_db: float = -100.0) -> None:
        with self._lock:
            self._lib.SetRXAAMSQRun(self.channel, int(bool(run)))
            self._lib.SetRXAAMSQThreshold(self.channel, float(threshold_db))

    def set_am_squelch_max_tail(self, tail: float) -> None:
        """AM squelch maximum tail-decay time
        (amsq.c::SetRXAAMSQMaxTail).  WDSP clamps internally to
        ``min_tail`` (set at create time).  Wired in Phase 6.A4
        for operator-tunable AM-squelch hold behavior.
        """
        with self._lock:
            self._lib.SetRXAAMSQMaxTail(self.channel, float(tail))

    # ── SSQL — WDSP's all-mode (SSB/CW/DIG) voice-activity squelch ─
    #
    # SSQL = "Single-mode Squelch Level".  Pre-AGC zero-crossing-rate
    # voice detector with hysteresis + raised-cosine ramps.  Same
    # mechanism Thetis uses for the SSB SQ button.  Operator-tunable
    # parameters:
    #   * threshold: 0..1.  Higher = more aggressive muting.  WU2O
    #     calibration default 0.16; 0.20 is common for ham SSB.
    #   * tau_mute / tau_unmute: ramp time constants in seconds.
    #     0.1 default both directions for snappy response without
    #     clicks.  Wider range 0.1..2.0 (mute) and 0.1..1.0 (unmute).
    def set_ssql_run(self, run: bool) -> None:
        """Master enable for the SSQL all-mode squelch."""
        with self._lock:
            self._lib.SetRXASSQLRun(self.channel, int(bool(run)))

    def set_ssql_threshold(self, threshold: float) -> None:
        """SSQL threshold.  Operator slider 0.0..1.0."""
        v = max(0.0, min(1.0, float(threshold)))
        with self._lock:
            self._lib.SetRXASSQLThreshold(self.channel, v)

    def set_ssql_tau_mute(self, seconds: float) -> None:
        """SSQL mute-ramp time constant.  WU2O default 0.1 s."""
        with self._lock:
            self._lib.SetRXASSQLTauMute(self.channel, float(seconds))

    def set_ssql_tau_unmute(self, seconds: float) -> None:
        """SSQL unmute-ramp time constant.  WU2O default 0.1 s."""
        with self._lock:
            self._lib.SetRXASSQLTauUnMute(self.channel, float(seconds))

    # ── APF (CW Audio Peaking Filter) ─────────────────────────────
    #
    # WDSP's APF is the SPEAK stage in the RXA chain — a resonant
    # boost centered on the CW pitch.  Operator-facing parameters
    # (center frequency, bandwidth, gain in dB) match Lyra's legacy
    # AudioPeakFilter so the existing UI panel just routes through.
    # WDSP's gain is LINEAR; the dB→linear conversion happens here
    # so callers stay in operator units.

    def set_apf(self, run: bool) -> None:
        """Enable / disable the CW audio peaking filter.

        Filter shape (center / bandwidth / gain) should be configured
        via :meth:`set_apf_freq` / :meth:`set_apf_bw` / :meth:`set_apf_gain`
        before enabling, otherwise the WDSP defaults apply.
        """
        with self._lock:
            self._lib.SetRXABiQuadRun(self.channel, int(bool(run)))

    def set_apf_freq(self, center_hz: float) -> None:
        """APF center frequency (Hz) — typically the operator's CW pitch."""
        with self._lock:
            self._lib.SetRXABiQuadFreq(self.channel, float(center_hz))

    def set_apf_bw(self, bw_hz: float) -> None:
        """APF -3 dB bandwidth (Hz)."""
        with self._lock:
            self._lib.SetRXABiQuadBandwidth(self.channel, float(bw_hz))

    def set_apf_gain_db(self, gain_db: float) -> None:
        """APF peak gain in dB (operator-facing convention).  WDSP's
        SPEAK takes linear gain internally; we convert here."""
        linear = float(10.0 ** (float(gain_db) / 20.0))
        with self._lock:
            self._lib.SetRXABiQuadGain(self.channel, linear)

    # ── Manual notch DB ────────────────────────────────────────────
    #
    # WDSP's RXA chain owns a ``notchdb`` consulted by the front-of-
    # chain bandpass (NBP0).  Each entry has center/width/active
    # state.  Lyra's manual notches map directly onto this database
    # — operator right-clicks on the spectrum to add a notch, which
    # propagates to one notchdb entry per Lyra notch.
    #
    # The full set is written here in one call rather than diffed,
    # because the DLL's RXANBPSetNotchesRun has been observed to
    # require a full filter rebuild when notches change anyway.

    def set_notches(self, notches: list[tuple[float, float, bool]],
                    master_run: bool = True) -> None:
        """Replace the WDSP notch database with the given list.

        Each tuple is ``(fcenter_hz, fwidth_hz, active)``.  Use
        signed center frequencies to address baseband: positive for
        USB, negative for LSB (matches Lyra's notch model where each
        Notch carries an absolute RF freq + the radio's VFO+mode
        determines the sign in baseband).

        ``master_run`` is the global notch-engine on/off — must be
        True for any individual notches to attenuate.  When False
        (or when the list is empty), the notch engine is disabled
        and CPU returns to baseline.
        """
        with self._lock:
            # Clear the existing set by deleting from the back.
            # RXANBPDeleteNotch shrinks ``nn`` by 1 and shifts the
            # remaining entries; iterating from highest index down
            # avoids index renumbering surprises.
            n_old = self._ffi.new("int*")
            self._lib.RXANBPGetNumNotches(self.channel, n_old)
            for i in range(int(n_old[0]) - 1, -1, -1):
                self._lib.RXANBPDeleteNotch(self.channel, i)
            # Add the new set in order.
            for i, (fc, fw, active) in enumerate(notches):
                self._lib.RXANBPAddNotch(
                    self.channel, i,
                    float(fc), float(max(1.0, fw)), int(bool(active)),
                )
            run_flag = int(bool(master_run) and bool(notches))
            self._lib.RXANBPSetNotchesRun(self.channel, run_flag)
            self._notches = [
                (i, float(fc), float(fw), bool(active))
                for i, (fc, fw, active) in enumerate(notches)
            ]
            self._notches_master_run = bool(master_run)

    def set_notches_master_run(self, run: bool) -> None:
        """Toggle the global notch engine without touching the database
        (for the operator's "Notches enabled" check box)."""
        with self._lock:
            self._notches_master_run = bool(run)
            self._lib.RXANBPSetNotchesRun(
                self.channel,
                int(bool(run) and bool(self._notches)),
            )

    def set_notch_tune_frequency(self, vfo_hz: float) -> None:
        """Tell the notch engine the current absolute RF tune frequency
        so absolute notch frequencies map onto baseband correctly.
        Call this on every freq change."""
        with self._lock:
            self._lib.RXANBPSetTuneFrequency(self.channel, float(vfo_hz))

    # -- hot path --------------------------------------------------------

    def process_block(self, iq: np.ndarray) -> np.ndarray:
        """Push EXACTLY ``in_size`` IQ frames through WDSP, return one block
        of stereo audio at ``out_rate``.

        The input length must be exactly ``cfg.in_size`` — use
        :meth:`process` for the higher-level streaming variant that buffers
        ragged input.

        Returns
        -------
        np.ndarray
            Stereo audio. Shape ``(out_size, 2)``, dtype float32.
            Channel 0 = L, channel 1 = R. Sample rate is ``cfg.out_rate``.

        Not thread-safe within a single channel.
        """
        n = self.cfg.in_size
        if iq.shape != (n,):
            raise ValueError(
                f"process_block() expects shape ({n},), got {iq.shape}"
            )

        # Interleave I/Q into the C buffer. WDSP wants doubles.
        v = self._in_view
        v[0::2] = iq.real.astype(np.float64, copy=False)
        v[1::2] = iq.imag.astype(np.float64, copy=False)

        # Optional EXT noise blanker stage — runs BEFORE fexchange0 on
        # the raw IQ.  The DLL's xnobEXT / xanbEXT take separate in
        # and out buffers; we ping-pong _in_buff <-> _nb_buff as
        # blankers chain.  When neither blanker is active, skip
        # entirely and fexchange0 reads directly from _in_buff.
        active_in = self._in_buff
        if self._nob_running:
            self._lib.xnobEXT(self.channel, active_in, self._nb_buff)
            active_in = self._nb_buff
        if self._anb_running:
            # Reuse _in_buff as the next destination if NOB just
            # wrote into _nb_buff; otherwise overwrite _nb_buff with
            # ANB's output.  Either way fexchange0 reads from the
            # buffer ANB wrote into.
            dst = self._in_buff if active_in is self._nb_buff else self._nb_buff
            self._lib.xanbEXT(self.channel, active_in, dst)
            active_in = dst

        # Process. With block=1, this blocks until the DSP thread has
        # produced the next out_size frames at out_rate.
        self._lib.fexchange0(
            self.channel, active_in, self._out_buff, self._err
        )

        # De-interleave into a (out_size, 2) float32 array.
        out = np.empty((self.out_size, 2), dtype=np.float32)
        out[:, 0] = self._out_view[0::2]
        out[:, 1] = self._out_view[1::2]
        return out

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Push variable-length IQ through WDSP, return all complete audio
        blocks ready to date.

        This is the streaming-friendly variant. The caller can pass any
        number of IQ frames per call; the wrapper accumulates them
        internally and pulls out whole ``in_size`` blocks for fexchange0.
        Returned audio length is a multiple of ``out_size`` frames at
        ``out_rate``; an empty (0, 2) array is returned when fewer than
        ``in_size`` frames have accumulated.

        Parameters
        ----------
        iq : np.ndarray
            Complex IQ samples, any length. ``complex64`` recommended.

        Returns
        -------
        np.ndarray
            Stereo audio, shape ``(k * out_size, 2)``, dtype float32, where
            ``k`` is the number of whole IQ blocks that fit in the
            accumulated stream this call. ``k`` may be 0 (no full block
            ready), 1 (typical), or several (if the call delivers a large
            ragged batch).

        Not thread-safe within a single channel.
        """
        # Append the new IQ to the accumulator. astype(copy=False) avoids
        # a copy when iq is already complex64.
        new = iq.astype(np.complex64, copy=False)
        if self._in_accum.size == 0:
            self._in_accum = new.copy()
        else:
            self._in_accum = np.concatenate([self._in_accum, new])

        n = self.cfg.in_size
        n_blocks = self._in_accum.size // n
        if n_blocks == 0:
            return np.empty((0, 2), dtype=np.float32)

        # Pre-allocate the full output once instead of concatenating per block.
        out = np.empty((n_blocks * self.out_size, 2), dtype=np.float32)
        for k in range(n_blocks):
            block = self._in_accum[k * n : (k + 1) * n]
            seg = self.process_block(block)
            out[k * self.out_size : (k + 1) * self.out_size] = seg

        # Carry the remainder forward.
        consumed = n_blocks * n
        self._in_accum = self._in_accum[consumed:].copy()
        return out

    def reset(self) -> None:
        """Drop any buffered partial-block IQ and ask WDSP to flush its
        internal DSP state. Call on freq / mode / rate change."""
        with self._lock:
            self._in_accum = np.empty(0, dtype=np.complex64)
            if self._opened:
                # SetChannelState(state, dmode=1) flushes WDSP's iobuffs
                # without destroying the channel. We restore prior run
                # state immediately after so audio resumes.
                was_running = self._running
                self._lib.SetChannelState(self.channel, 0, 1)
                if was_running:
                    self._lib.SetChannelState(self.channel, 1, 0)

    # -- meters --------------------------------------------------------

    def get_meter(self, meter_type: int) -> float:
        """Read a meter off this RX channel.

        See ``wdsp_native.MeterType`` for the indices. Most readings
        are linear / dBFS-after-log; AGC_GAIN is a linear multiplier
        (convert to dB with ``20*log10``).
        """
        return float(self._lib.GetRXAMeter(self.channel, int(meter_type)))

    def get_agc_gain_db(self) -> float:
        """AGC current gain, in dB. Negative = AGC is attenuating;
        positive = AGC is boosting; 0 = unity.

        WDSP's RXA meters are stored as dB (e.g. an S-meter peak of
        -6 dBFS reads back as ``-6.02``), NOT linear. We return the
        value directly without further conversion.
        """
        from lyra.dsp.wdsp_native import MeterType
        return self.get_meter(MeterType.AGC_GAIN)


__all__ = ["RxChannel", "RxConfig"]
