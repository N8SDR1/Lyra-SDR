"""Lyra WDSP TX engine -- high-level TX channel wrapper.

Sibling of ``wdsp_engine.RxChannel``.  Sits on top of ``wdsp_native``
and exposes a clean Python interface to WDSP's TXA chain:

    from lyra.dsp.wdsp_tx_engine import TxChannel, TxConfig

    tx = TxChannel(channel=4, cfg=TxConfig())
    tx.set_mode("USB")
    tx.start()

    # Per audio block:
    iq = tx.process(mic_block)  # mic_block: float32 [in_size]  mono mic
                                # iq:        complex64 [out_size] TX I/Q

The channel index space is shared with WDSP's internal model and
matches the locked host-channel-ID convention from consensus plan
§2.2:

    0  = RX1 main
    1  = RX1 sub (reserved, post-v0.3)
    2  = RX2 main
    3  = (HL2: PS-feedback alias) / (5-DDC ANAN: RX2-sub)
    4  = TX main (TXA)      <-- this module
    5  = PS feedback A on 5-DDC ANAN (future)
    6  = PS feedback B on 5-DDC ANAN (future)

Buffer layout
-------------
``fexchange0`` operates on interleaved double-precision buffers in
both directions.  For the TX channel:

    in_buff   = 2 * in_size doubles, [mic, 0, mic, 0, ...]
                I slot carries mic samples (mono); Q slot is zero
                because ``SetTXAPanelSelect(2)`` selects mic-from-I.
                NOTE: SetTXAPanelSelect is operator-opt-in per Agent M
                Round 3 audit -- TxChannel does NOT call it at init
                time.  WDSP's default panel-select behaviour on a
                fresh TXA channel reads I as mic, which is what
                Lyra wants.

    out_buff  = 2 * out_size doubles, [I, Q, I, Q, ...]
                Complex TX baseband I/Q at the operator's IQ rate
                (48 kHz on HL2 EP2).

On HL2 the in and out rates are both 48 kHz, so ``out_size ==
in_size`` and one mic frame in = one I/Q frame out.  On future
ANAN P2 hardware (v0.4+) the out_rate may be 192 kHz; WDSP handles
the rate conversion inside ``fexchange0``.

Channel-init sequence (IM-5 audit compliance)
---------------------------------------------
Per consensus §8.5 IM-5 "highest-risk-missed setters" audit, the
following setters MUST be called at channel-open time -- skipping
any of them produces silent-failure-class bugs like the v0.0.9.6
RX-side §14.10 "right-channel silent" defect.  ``TxChannel.__init__``
calls them all:

  1. ``SetTXAPanelGain1(1.0)`` overrides WDSP's create_txa default
     of 4.0 (= +12 dB hot mic).  Lyra applies operator mic gain
     after this baseline.  (Agent M Round 3 finding N-1.)
  2. ``SetTXAPHROTRun(1)`` for SSB -- consensus §8.5 IM-5 #1.
     Default OFF in create_txa; skipping = PEP-PAR ~3-4 dB worse
     than industry baseline.  Setter names use UPPERCASE PHROT
     (case-sensitive C symbols per iir.c:665-697).
  3. ``SetTXABandpassFreqs`` + ``SetTXABandpassRun(1)`` for the
     mode-appropriate filter -- consensus §8.5 IM-5 #2.  USB
     uses positive freqs, LSB negative (WDSP mirrored-baseband,
     same gotcha as RX side §14.2).
  4. ``SetTXAALC*`` all five setters -- consensus §8.5 IM-5 #4.
     Defaults aren't set by create_txa; without explicit push
     ALC kicks in weirdly.  NOTE: there is NO SetTXAALCThresh
     setter -- ALC dynamic-range ceiling is governed by
     SetTXAALCMaxGain alone (consensus plan corrected 2026-05-15
     after Phase 2 commit 1 cffi-audit found this).
  5. ``SetTXACFIRRun(0)`` for HL2 P1 -- consensus §8.5 row 2069.
     CFIR is for P2 CIC paths; on HL2 P1 it's meaningful only
     to set OFF.

Threading model
---------------
* ``process()`` is the only hot-path call and is NOT thread-safe
  within a single channel.  Call from one thread (the DSP worker
  per CLAUDE.md §5 threading model).
* Setters take an internal lock so they don't race with start/stop
  or each other.

Multiple TxChannel instances on different channel indices are
independent.  v0.2.0 has one TX channel (host ID 4); v0.3 PS may
add a sibling on host ID 5/6 for ANAN feedback alignment.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

from lyra.dsp import wdsp_native


# ---------------------------------------------------------------------------
# Mode string → integer mapping (TxaMode subset for v0.2.0 SSB-only)
# ---------------------------------------------------------------------------

_MODE_BY_NAME = {
    "LSB":   wdsp_native.TxaMode.LSB,
    "USB":   wdsp_native.TxaMode.USB,
    "DSB":   wdsp_native.TxaMode.DSB,
    "CWL":   wdsp_native.TxaMode.CWL,    # plumbed; modulator wires in v0.2.2
    "CWU":   wdsp_native.TxaMode.CWU,    # plumbed; modulator wires in v0.2.2
    "FM":    wdsp_native.TxaMode.FM,     # plumbed; modulator wires in v0.2.2
    "AM":    wdsp_native.TxaMode.AM,     # plumbed; modulator wires in v0.2.2
    "DIGU":  wdsp_native.TxaMode.DIGU,
    "DIGL":  wdsp_native.TxaMode.DIGL,
    "SAM":   wdsp_native.TxaMode.SAM,
    "DRM":   wdsp_native.TxaMode.DRM,
    "SPEC":  wdsp_native.TxaMode.SPEC,
}


# ---------------------------------------------------------------------------
# TxConfig
# ---------------------------------------------------------------------------

@dataclass
class TxConfig:
    """Per-TX-channel sample rates and buffer size.

    Defaults match HL2 EP2 audio path -- 48 kHz mic in, 48 kHz I/Q
    out.  WDSP's internal DSP rate runs at 96 kHz for headroom (TXA
    convention).  v0.4 ANAN P2 work may bump out_rate to 192 kHz;
    WDSP handles the rate conversion inside fexchange0.
    """
    in_size: int = 512          # frames per process() call (mic block size)
    dsp_size: int = 4096        # internal DSP buffer size
    in_rate: int = 48_000       # mic input rate (HL2 EP6 mic = 48 kHz)
    dsp_rate: int = 96_000      # WDSP TXA internal DSP rate
    out_rate: int = 48_000      # TX I/Q output rate (HL2 EP2 = 48 kHz)
    # Slew envelope (avoids click on start/stop -- same as RxChannel)
    tdelayup: float = 0.010
    tslewup: float = 0.025
    tdelaydown: float = 0.000
    tslewdown: float = 0.010
    # "block until output available" — 1 = fexchange0 blocks until DSP has
    # produced the next output buffer. Required for a steady cadence.
    block: int = 1


# ---------------------------------------------------------------------------
# TxChannel
# ---------------------------------------------------------------------------

class TxChannel:
    """A single WDSP transmitter channel.

    Threading model:
      * ``process()`` is NOT thread-safe within a channel.  Call from
        one thread (the DSP worker).
      * Configuration setters take an internal lock so they don't
        race with each other or with start/stop.

    Channel index convention (consensus §2.2 -- locked v0.1 Phase 0):
      4 = TX main (TXA).  Other values (1/5/6) reserved for future
      use; Lyra v0.2.0 instantiates one TxChannel at index 4.
    """

    def __init__(self, channel: int = 4, cfg: Optional[TxConfig] = None):
        self.channel = int(channel)
        self.cfg = cfg or TxConfig()

        self._lib = wdsp_native.load()
        self._ffi = wdsp_native.ffi()

        # Output block size.  TX is symmetric on HL2 (in_rate == out_rate),
        # but on future ANAN the out_rate may be higher; mirror the
        # RxChannel formula so we're forward-compatible.
        if self.cfg.in_rate >= self.cfg.out_rate:
            ratio = self.cfg.in_rate // self.cfg.out_rate
            self.out_size = self.cfg.in_size // ratio
        else:
            ratio = self.cfg.out_rate // self.cfg.in_rate
            self.out_size = self.cfg.in_size * ratio

        # Pre-allocate I/O buffers on the C side.
        # Input  is in_size complex frames  = 2 * in_size  doubles.
        # Output is out_size complex frames = 2 * out_size doubles.
        self._in_buff = self._ffi.new(f"double[{2 * self.cfg.in_size}]")
        self._out_buff = self._ffi.new(f"double[{2 * self.out_size}]")
        self._err = self._ffi.new("int*")

        # Zero-copy numpy views over the cffi buffers.
        self._in_view = np.frombuffer(
            self._ffi.buffer(self._in_buff), dtype=np.float64
        )
        self._out_view = np.frombuffer(
            self._ffi.buffer(self._out_buff), dtype=np.float64
        )

        # Mic input accumulator for variable-length process_stream() calls.
        # Mirrors RxChannel's _in_accum pattern: callers can pass any
        # number of mic frames per call; the wrapper buffers and pulls
        # whole in_size blocks.
        self._in_accum = np.empty(0, dtype=np.float32)

        self._lock = threading.Lock()
        self._opened = False
        self._running = False

        # Open + apply IM-5 init sequence.
        self._open()
        self._apply_init_setters()

    # -- lifecycle -------------------------------------------------------

    def _open(self) -> None:
        if self._opened:
            return
        c = self.cfg
        # type=1 means TX channel (RX is type=0).
        self._lib.OpenChannel(
            self.channel,
            c.in_size, c.dsp_size,
            c.in_rate, c.dsp_rate, c.out_rate,
            1,                    # type = TX
            0,                    # state = stopped (start() will run it)
            c.tdelayup, c.tslewup,
            c.tdelaydown, c.tslewdown,
            c.block,
        )
        self._opened = True

    def _apply_init_setters(self) -> None:
        """IM-5 audit compliance: setters that MUST fire at channel
        open or the TX chain produces silent failure-class bugs.
        See module docstring for the full audit list.
        """
        ch = self.channel
        with self._lock:
            # 1. Override WDSP create_txa Gain1 default of 4.0 with
            #    1.0 (= 0 dB).  Operator mic-gain slider applies on
            #    top of this baseline.  (Agent M Round 3 N-1.)
            self._lib.SetTXAPanelGain1(ch, 1.0)
            self._lib.SetTXAPanelRun(ch, 1)
            # 2. Default mode = USB.  Operator can change via set_mode.
            self._lib.SetTXAMode(ch, wdsp_native.TxaMode.USB)
            # 3. Bandpass for USB (positive freqs per WDSP mirrored-
            #    baseband convention; LSB negates).
            self._lib.SetTXABandpassFreqs(ch, 200.0, 3100.0)
            self._lib.SetTXABandpassRun(ch, 1)
            # 4. PHROT enable for SSB PEP-PAR -- consensus IM-5 #1.
            #    Skipping = ~3-4 dB worse PEP-PAR than industry
            #    baseline.  Default Corner 338 Hz / 8 stages per
            #    Thetis radio.cs.
            self._lib.SetTXAPHROTCorner(ch, 338.0)
            self._lib.SetTXAPHROTNstages(ch, 8)
            self._lib.SetTXAPHROTRun(ch, 1)
            # 5. ALC always-on -- consensus IM-5 #4.  Thetis defaults:
            #    Attack 1 ms / Decay 10 ms / Hang 500 ms / MaxGain
            #    +3 dB.  Attack/Decay/Hang are INT ms per wcpagc.c
            #    (NOT double sec -- consensus plan corrected 2026-05-15
            #    after row-by-row cdef audit caught the defect).
            #    There is NO SetTXAALCThresh setter -- ceiling is
            #    governed by SetTXAALCMaxGain alone.
            self._lib.SetTXAALCAttack(ch, 1)
            self._lib.SetTXAALCDecay(ch, 10)
            self._lib.SetTXAALCHang(ch, 500)
            self._lib.SetTXAALCMaxGain(ch, 3.0)
            self._lib.SetTXAALCSt(ch, 1)
            # 6. Leveler defaults set but state OFF (operator opt-in
            #    via set_leveler).  Same Thetis defaults from
            #    radio.cs: 5 ms attack / 250 ms decay / 500 ms hang
            #    / +5 dB top.
            self._lib.SetTXALevelerAttack(ch, 5)
            self._lib.SetTXALevelerDecay(ch, 250)
            self._lib.SetTXALevelerHang(ch, 500)
            self._lib.SetTXALevelerTop(ch, 5.0)
            self._lib.SetTXALevelerSt(ch, 0)
            # 7. CFIR OFF for HL2 P1 -- consensus row 2069.  CFIR is
            #    for P2 CIC compensation; meaningful only on P2 paths.
            self._lib.SetTXACFIRRun(ch, 0)

    def start(self) -> None:
        """Begin processing on this channel."""
        with self._lock:
            if not self._opened:
                self._open()
            if not self._running:
                self._lib.SetChannelState(self.channel, 1, 0)
                self._running = True

    def stop(self) -> None:
        """Stop processing without destroying state.  Output slews
        down cleanly per the slew envelope in TxConfig."""
        with self._lock:
            if self._opened and self._running:
                self._lib.SetChannelState(self.channel, 0, 0)
                self._running = False

    def close(self) -> None:
        """Stop and free the channel."""
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
        """Set TXA mode (USB/LSB/etc.)  v0.2.0 wires SSB only; other
        modes land with their respective modulator setters in v0.2.x.
        """
        m = _MODE_BY_NAME[mode.upper()] if isinstance(mode, str) else int(mode)
        with self._lock:
            self._lib.SetTXAMode(self.channel, m)
            # For SSB modes, bandpass sign convention follows WDSP
            # mirrored-baseband (consensus §8.5 IM-5 #2):
            #   USB -> positive freqs  (e.g. +200, +3100)
            #   LSB -> negative freqs  (e.g. -3100, -200)
            # We don't auto-flip here -- callers manage via set_filter()
            # so they can pick non-default bandwidths.  Init defaults
            # to USB positive freqs.

    def set_filter(self, low: float, high: float) -> None:
        """Set TX bandpass filter cutoffs in Hz.

        WDSP mirrored-baseband sign convention applies (consensus §8.5
        IM-5 #2): USB uses positive freqs (e.g. +200..+3100), LSB
        uses negative (e.g. -3100..-200).  Get this wrong → wrong
        sideband transmitted.
        """
        with self._lock:
            self._lib.SetTXABandpassFreqs(self.channel, float(low), float(high))
            self._lib.SetTXABandpassRun(self.channel, 1)

    def set_mic_gain(self, gain: float) -> None:
        """Set mic-input panel gain (linear, not dB).

        Operator slider 0..+40 dB maps to linear 1.0..100.0 via
        10**(db/20).  Sits on top of the IM-5 init baseline of 1.0
        (= 0 dB).
        """
        with self._lock:
            self._lib.SetTXAPanelGain1(self.channel, float(gain))

    def set_alc(self,
                attack_ms: int = 1,
                decay_ms: int = 10,
                hang_ms: int = 500,
                maxgain_db: float = 3.0,
                running: bool = True) -> None:
        """Set ALC (xwcpagc) parameters.

        ALC is the load-bearing splatter limiter -- ``running``
        defaults to True and operators should NOT disable it.
        Attack/Decay/Hang are integer milliseconds (per wcpagc.c
        signatures); MaxGain is dB (double).

        There is no separate threshold setter -- the dynamic-range
        ceiling is governed by MaxGain alone.
        """
        with self._lock:
            self._lib.SetTXAALCAttack(self.channel, int(attack_ms))
            self._lib.SetTXAALCDecay(self.channel, int(decay_ms))
            self._lib.SetTXAALCHang(self.channel, int(hang_ms))
            self._lib.SetTXAALCMaxGain(self.channel, float(maxgain_db))
            self._lib.SetTXAALCSt(self.channel, 1 if running else 0)

    def set_leveler(self,
                    running: bool,
                    attack_ms: int = 5,
                    decay_ms: int = 250,
                    hang_ms: int = 500,
                    top_db: float = 5.0) -> None:
        """Set TX leveler (wcpagc mode 5 TX side) parameters.

        Off by default; operator opts in via Settings → TX.  Same
        wcpagc engine as RX AGC, with TX-specific defaults per
        Thetis radio.cs.
        """
        with self._lock:
            self._lib.SetTXALevelerAttack(self.channel, int(attack_ms))
            self._lib.SetTXALevelerDecay(self.channel, int(decay_ms))
            self._lib.SetTXALevelerHang(self.channel, int(hang_ms))
            self._lib.SetTXALevelerTop(self.channel, float(top_db))
            self._lib.SetTXALevelerSt(self.channel, 1 if running else 0)

    def set_phrot(self,
                  running: bool,
                  corner_hz: float = 338.0,
                  nstages: int = 8) -> None:
        """Set PHROT (SSB PEP-PAR reduction) parameters.

        IM-5 #1 mandates this be enabled for SSB modes; init defaults
        to ON.  Operators in extreme ESSB workflows may disable.
        Setter names use UPPERCASE PHROT (case-sensitive C symbols).
        """
        with self._lock:
            self._lib.SetTXAPHROTCorner(self.channel, float(corner_hz))
            self._lib.SetTXAPHROTNstages(self.channel, int(nstages))
            self._lib.SetTXAPHROTRun(self.channel, 1 if running else 0)

    def set_gen0(self,
                 running: bool,
                 mode: int = 0,
                 tone_freq_hz: float = 1000.0,
                 tone_mag: float = 0.5) -> None:
        """Set input-side signal generator (gen0) state.

        Used for bench-test self-test routes -- operator dials up a
        1 kHz known-amplitude tone and verifies the modulator + ALC
        + leveler chain produces clean SSB at the antenna port
        without needing to talk into a mic.  OFF in normal operation.
        """
        with self._lock:
            self._lib.SetTXAPostGenMode(self.channel, int(mode))
            self._lib.SetTXAPostGenToneFreq(self.channel, float(tone_freq_hz))
            self._lib.SetTXAPostGenToneMag(self.channel, float(tone_mag))
            self._lib.SetTXAPostGenRun(self.channel, 1 if running else 0)

    # -- meter accessors -------------------------------------------------

    def _get_meter(self, mt: int) -> float:
        """Read a TXA meter value (linear)."""
        return float(self._lib.GetTXAMeter(self.channel, int(mt)))

    @property
    def mic_pk_linear(self) -> float:
        """Mic input peak (linear); convert to dBFS via 20*log10()."""
        return self._get_meter(wdsp_native.TxaMeterType.MIC_PK)

    @property
    def lvlr_gain_linear(self) -> float:
        """Leveler gain reduction (linear)."""
        return self._get_meter(wdsp_native.TxaMeterType.LVLR_GAIN)

    @property
    def alc_gain_linear(self) -> float:
        """ALC gain reduction (linear).  Operators care about this
        as the "ALC working" indicator + splatter-protection check.
        """
        return self._get_meter(wdsp_native.TxaMeterType.ALC_GAIN)

    @property
    def out_pk_linear(self) -> float:
        """Final TX output peak (linear); drives the operator OUT
        meter row on the §8.4 LED-bar layout.
        """
        return self._get_meter(wdsp_native.TxaMeterType.OUT_PK)

    # -- hot path -------------------------------------------------------

    def process_block(self, mic: np.ndarray) -> np.ndarray:
        """Push EXACTLY ``in_size`` mic frames through WDSP TXA, return
        one block of TX baseband I/Q at ``out_rate``.

        Input length must be exactly ``cfg.in_size`` -- use
        :meth:`process` for the streaming variant that buffers ragged
        input.

        Mic samples (float32 mono) go into the I slot of WDSP's
        complex-double input buffer; Q slot stays zero (WDSP's
        TXA default panel-select reads I as mic).

        Parameters
        ----------
        mic : np.ndarray
            Mono mic samples, shape ``(in_size,)``, float32.  Range
            ±1.0; the IM-5 init baseline applies Gain1 = 1.0 so the
            caller's amplitude survives to the modulator.

        Returns
        -------
        np.ndarray
            TX baseband I/Q.  Shape ``(out_size,)``, dtype complex64.
            Sample rate is ``cfg.out_rate``.

        Not thread-safe within a single channel.
        """
        n = self.cfg.in_size
        if mic.shape != (n,):
            raise ValueError(
                f"process_block() expects shape ({n},), got {mic.shape}"
            )

        # Interleave mic into the C buffer's I slots; zero the Q slots.
        v = self._in_view
        v[0::2] = mic.astype(np.float64, copy=False)
        v[1::2] = 0.0

        # Process.  With block=1, fexchange0 blocks until the DSP
        # thread has produced the next out_size frames at out_rate.
        self._lib.fexchange0(
            self.channel, self._in_buff, self._out_buff, self._err
        )

        # De-interleave the I/Q output into a complex64 array.
        out = np.empty(self.out_size, dtype=np.complex64)
        out.real = self._out_view[0::2].astype(np.float32, copy=False)
        out.imag = self._out_view[1::2].astype(np.float32, copy=False)
        return out

    def process(self, mic: np.ndarray) -> np.ndarray:
        """Push variable-length mic samples through WDSP TXA, return
        all complete I/Q blocks ready to date.

        Streaming-friendly variant.  Mirrors RxChannel.process()
        pattern: callers pass any number of mic frames per call;
        the wrapper accumulates internally and pulls out whole
        ``in_size`` blocks.  Returned I/Q length is a multiple of
        ``out_size`` frames; empty array when fewer than ``in_size``
        mic frames have accumulated.

        Parameters
        ----------
        mic : np.ndarray
            Mono mic samples, any length.  float32 recommended.

        Returns
        -------
        np.ndarray
            TX baseband I/Q.  Shape ``(k * out_size,)``, dtype
            complex64, where k = number of complete in_size blocks
            consumed this call.  k = 0 returns an empty array.
        """
        if mic.dtype != np.float32:
            mic = mic.astype(np.float32, copy=False)
        # Accumulate.
        if self._in_accum.size == 0:
            buf = mic
        else:
            buf = np.concatenate((self._in_accum, mic))

        n = self.cfg.in_size
        k = buf.size // n
        if k == 0:
            self._in_accum = buf
            return np.empty(0, dtype=np.complex64)

        out_chunks = []
        for i in range(k):
            block = buf[i * n:(i + 1) * n]
            out_chunks.append(self.process_block(block))
        # Carry remainder.
        self._in_accum = buf[k * n:].copy()
        if k == 1:
            return out_chunks[0]
        return np.concatenate(out_chunks)
