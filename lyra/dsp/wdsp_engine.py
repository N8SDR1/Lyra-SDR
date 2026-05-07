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

The channel index space is shared with WDSP's internal model:

    0  — RX1 (main)
    1  — RX2 (sub-receiver)
    2  — TX
    ...

For now this module exposes RX channels only.

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

        self._open()

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

    def set_panel_gain(self, gain: float) -> None:
        """Linear gain on the post-DSP audio (0..1+, 1.0 = unity)."""
        with self._lock:
            self._lib.SetRXAPanelGain1(self.channel, float(gain))

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

    def set_nob(self, run: bool) -> None:
        """Noise-OFF blanker — narrowband impulse blanker on raw IQ
        before the RXA chain.  Use for clicks / popcorn impulse noise."""
        with self._lock:
            self._lib.SetEXTNOBRun(self.channel, int(bool(run)))

    def set_anb(self, run: bool) -> None:
        """Advanced noise blanker — broadband impulse blanker on raw IQ
        before the RXA chain.  Use for switching power-supply hash and
        similar broadband impulse interference."""
        with self._lock:
            self._lib.SetEXTANBRun(self.channel, int(bool(run)))

    def set_fm_squelch(self, run: bool) -> None:
        with self._lock:
            self._lib.SetRXAFMSQRun(self.channel, int(bool(run)))

    def set_am_squelch(self, run: bool, threshold_db: float = -100.0) -> None:
        with self._lock:
            self._lib.SetRXAAMSQRun(self.channel, int(bool(run)))
            self._lib.SetRXAAMSQThreshold(self.channel, float(threshold_db))

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

        # Process. With block=1, this blocks until the DSP thread has
        # produced the next out_size frames at out_rate.
        self._lib.fexchange0(
            self.channel, self._in_buff, self._out_buff, self._err
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
