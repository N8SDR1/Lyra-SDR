"""HPSDR Protocol 1 RX streaming for Hermes Lite 2 / 2+.

Protocol summary (from HPSDR P1 spec and HL2 wiki):

Start/Stop command (64 bytes, host -> radio:1024):
    [0] 0xEF  [1] 0xFE  [2] 0x04  [3] flags  [4..63] 0x00
    flags: 0x00 = stop, 0x01 = start IQ, 0x03 = start IQ+bandscope.

IQ data frame (1032 bytes, radio -> host on the port the host sent from):
    Header (8 bytes):
        [0] 0xEF  [1] 0xFE  [2] 0x01  [3] 0x06 (ep6)
        [4..7] uint32 sequence number, big-endian
    Two "USB" frames (512 bytes each):
        sync: 0x7F 0x7F 0x7F
        C&C:  5 bytes (C0 .. C4) — radio->host telemetry/feedback
        data: 504 bytes = 63 samples, each 8 bytes:
            I: 3 bytes big-endian signed (24-bit)
            Q: 3 bytes big-endian signed (24-bit)
            mic: 2 bytes big-endian signed (16-bit)

C&C write register selectors (host -> radio in EP2, for later use):
    C0=0x00: speed/config (bit 1:0 of C1 = sample rate index)
    C0=0x02: TX NCO freq, C0=0x04..0x0E: RX1..RX6 NCO freq (32-bit Hz BE)
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

_log = logging.getLogger(__name__)

DISCOVERY_PORT = 1024

# Start/stop flags
STOP = 0x00
START_IQ = 0x01
START_IQ_BANDSCOPE = 0x03

# Sample rate codes for C0=0x00, C1[1:0]
SAMPLE_RATES = {48000: 0, 96000: 1, 192000: 2, 384000: 3}


@dataclass
class FrameStats:
    frames: int = 0
    samples: int = 0
    seq_expected: int = -1
    seq_errors: int = 0
    last_c1_c4: bytes = b""
    # HL2 telemetry — most recent raw 12-bit ADC counts the radio
    # reported via EP6 C0 telemetry addresses (each address rotates
    # in, so any field may briefly lag while we wait for the next
    # cycle of that address to arrive). Engineering-unit conversion
    # (temp °C, supply V, etc.) lives in Radio so the protocol layer
    # stays agnostic to calibration constants.
    #
    # See HL2 wiki "Protocol" page — addresses we decode:
    #   addr 2 (C0 = 0x10): C1+C2 = forward power adc, C3+C4 = reverse power adc
    #   addr 3 (C0 = 0x18): C1+C2 = AIN3 (12V supply via divider),
    #                       C3+C4 = AIN4 (AD9866 on-die temp sensor)
    fwd_pwr_adc: int = 0
    rev_pwr_adc: int = 0
    supply_adc: int = 0
    temp_adc:   int = 0
    # Fallback supply candidate from addr 0 C1:C2 (some HL2 firmware
    # variants pack AIN6 / supply ADC into bits[15:4] of this 16-bit
    # field instead of using addr 3). Radio's _emit_hl2_telemetry
    # uses this when the primary supply_adc slot is empty.
    supply_adc_alt: int = 0


def _build_start_stop_packet(flags: int) -> bytes:
    pkt = bytearray(64)
    pkt[0] = 0xEF
    pkt[1] = 0xFE
    pkt[2] = 0x04
    pkt[3] = flags & 0xFF
    return bytes(pkt)


def _decode_iq_samples(block_data: bytes) -> np.ndarray:
    """Decode 504 bytes into 63 complex64 I/Q samples (mic discarded for now)."""
    # Interpret as 63 groups of 8 bytes. Use uint8 and bit-assemble 24-bit ints.
    arr = np.frombuffer(block_data, dtype=np.uint8).reshape(63, 8)
    i_raw = (
        (arr[:, 0].astype(np.int32) << 16)
        | (arr[:, 1].astype(np.int32) << 8)
        | arr[:, 2].astype(np.int32)
    )
    q_raw = (
        (arr[:, 3].astype(np.int32) << 16)
        | (arr[:, 4].astype(np.int32) << 8)
        | arr[:, 5].astype(np.int32)
    )
    # sign-extend 24-bit to 32-bit
    i_raw = np.where(i_raw & 0x800000, i_raw - 0x1000000, i_raw)
    q_raw = np.where(q_raw & 0x800000, q_raw - 0x1000000, q_raw)
    # normalize to [-1, 1)
    scale = 1.0 / (1 << 23)
    return (i_raw.astype(np.float32) * scale) + 1j * (q_raw.astype(np.float32) * scale)


def _parse_iq_frame(data: bytes) -> Optional[tuple[int, np.ndarray, bytes, bytes]]:
    """Return (seq, samples, cc_block0, cc_block1) or None if invalid.

    Samples are a concatenation of both USB-block halves (126 complex samples).
    cc_block is a 5-byte slice (C0..C4); C0 carries the telemetry
    address in bits[7:3] and live state flags in bits[2:0] for HPSDR
    Protocol 1 EP6 frames.
    """
    if len(data) != 1032:
        return None
    if data[0] != 0xEF or data[1] != 0xFE or data[2] != 0x01 or data[3] != 0x06:
        return None
    seq = struct.unpack(">I", data[4:8])[0]

    blocks = (data[8:520], data[520:1032])
    cc_parts = []
    sample_parts = []
    for b in blocks:
        if b[0] != 0x7F or b[1] != 0x7F or b[2] != 0x7F:
            return None
        cc_parts.append(bytes(b[3:8]))
        sample_parts.append(_decode_iq_samples(b[8:]))

    samples = np.concatenate(sample_parts)
    return seq, samples, cc_parts[0], cc_parts[1]


def _decode_hl2_telemetry(cc: bytes, stats: "FrameStats") -> None:
    """Update HL2 telemetry fields on `stats` from one 5-byte C&C
    block (C0..C4).

    Mapping per the HPSDR Protocol 1 hardware specification for the
    Hermes-Lite-2 (the AD9866-based HL2 telemetry rotation):

        addr  (C0 >> 3)  C0 byte  payload
        ----  ----------  -------  -------
        1     0x08         C1:C2 = AD9866 die temperature (raw ADC)
                          C3:C4 = forward power (raw ADC)
        2     0x10         C1:C2 = reverse power (raw ADC)
        3     0x18         C3:C4 = supply voltage (raw ADC, 12 V rail
                                  via on-board divider)

    All payload values are 16-bit big-endian (high byte first). The
    HL2 firmware rotates which address it reports each frame, so any
    given field refreshes every few frames rather than every one.

    Probe-tap hook: when a HL2Stream has its `_probe_cb` callback set
    on `stats`, we forward (addr, C1, C2, C3, C4) for every decoded
    block — the Help → HL2 Telemetry Probe dialog uses this to verify
    the mapping above against a specific firmware revision.
    """
    if len(cc) < 5:
        return
    addr = (cc[0] >> 3) & 0x1F
    # Probe tap (set by the diagnostic dialog only — None when off)
    probe = getattr(stats, "_probe_cb", None)
    if probe is not None:
        try:
            probe(addr, cc[1], cc[2], cc[3], cc[4])
        except Exception:
            pass
    # Use full 16-bit values here (the HL2 fills all 16 bits in the
    # ADC field, not just the low 12). Engineering-unit conversion in
    # Radio assumes a 12-bit-or-larger range and divides accordingly.
    if addr == 0:
        # Fallback supply slot. Some HL2 firmware variants put AIN6
        # (12 V supply) into addr 0 C1:C2 with the 12-bit ADC value
        # left-shifted by 4 (i.e. occupying bits[15:4] of the 16-bit
        # field). We capture both interpretations here; Radio picks
        # whichever yields a plausible reading.
        word = ((cc[1] << 8) | cc[2]) & 0xFFFF
        stats.supply_adc_alt = word >> 4
    elif addr == 1:
        stats.temp_adc    = ((cc[1] << 8) | cc[2]) & 0xFFFF
        stats.fwd_pwr_adc = ((cc[3] << 8) | cc[4]) & 0xFFFF
    elif addr == 2:
        stats.rev_pwr_adc = ((cc[1] << 8) | cc[2]) & 0xFFFF
    elif addr == 3:
        stats.supply_adc  = ((cc[3] << 8) | cc[4]) & 0xFFFF


class HL2Stream:
    """Open a P1 stream to an HL2, run an RX loop in a background thread.

    Typical use:
        s = HL2Stream("10.10.30.100", sample_rate=48000)
        s.start(on_samples=lambda samples, stats: ...)
        ...
        s.stop()
    """

    def __init__(self, radio_ip: str, sample_rate: int = 48000):
        if sample_rate not in SAMPLE_RATES:
            raise ValueError(f"sample_rate must be one of {list(SAMPLE_RATES)}")
        self.radio_ip = radio_ip
        self.sample_rate = sample_rate
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.stats = FrameStats()
        self._tx_seq = 0
        # Keepalive C&C — sent on every EP6 frame to prevent the radio's TX
        # queue from underrunning and halting the stream.
        # C4 bit 2 = duplex. Without it, HL2 runs simplex and ignores RX1
        # frequency writes (RX1 freq gets slaved to TX freq). The duplex
        # bit is required by HPSDR Protocol 1 for any client that wants
        # independent RX/TX frequency control. C4[5:3] = NDDC - 1
        # (0 = 1 receiver).
        self._config_c4 = 0x04  # duplex=1, NDDC=1
        # Round-robin C&C register table — keyed by c0, value is the
        # (c1,c2,c3,c4) bytes. Each USB block in an EP2 frame can carry
        # one C&C write, so to keep ALL configured registers fresh on
        # the HL2 gateware we cycle through this table across frames.
        # Standard HPSDR P1 host pattern: each frame increments the
        # round-robin index and wraps back to 0 after the last
        # register, so every register is re-asserted within a
        # bounded number of frames. Without this, only the most-
        # recently-modified register stays "fresh" — the HL2 EP6 IQ
        # stream could stall in a way only a manual sample-rate
        # cycle unsticks (because that cycle re-issues C&C 0x00).
        self._cc_registers: dict[int, tuple[int, int, int, int]] = {
            0x00: (SAMPLE_RATES[sample_rate], 0x00, 0x00, self._config_c4),
        }
        self._cc_rr_idx: int = 0
        self._cc_lock = threading.Lock()
        # Legacy fallback — the old code path stored last-sent register
        # here. Kept synchronized with the dict so any external readers
        # (none in current codebase) still see something sensible.
        self._keepalive_cc: tuple[int, int, int, int, int] = (
            0x00, SAMPLE_RATES[sample_rate], 0x00, 0x00, self._config_c4
        )
        self._send_lock = threading.Lock()

        # TX audio queue. Demod pipeline pushes float samples [-1, 1] at
        # 48 kHz. The AK4951 codec on the HL2+ is hard-locked at 48 kHz
        # fs regardless of EP6 IQ rate. To keep the codec from being
        # over-fed at higher IQ rates we throttle EP2 frame emission
        # to a fixed 380 Hz cadence (= 48 kHz audio / 126 samples per
        # frame), decoupled from EP6 cadence. See _rx_loop.
        #
        # Phase 3.B B.7 — thread-safety audit (DSP worker mode).
        # In single-thread mode the producer of this queue (demod
        # writes via AK4951Sink.write → queue_tx_audio) is the Qt
        # main thread.  In worker mode it's the DspWorker thread.
        # The consumer (_pack_audio_bytes called from the EP2
        # frame builder) is HL2Stream's TX thread in BOTH modes.
        # All three call sites (queue_tx_audio, clear_tx_audio,
        # _pack_audio_bytes) acquire ``_tx_audio_lock`` before
        # touching ``_tx_audio``, so the producer-thread switch
        # introduced by worker mode is safe — no additional locks
        # needed.  The only non-locked field consumed cross-thread
        # is ``inject_audio_tx`` (a plain bool used by the EP2
        # frame builder to decide whether to call _pack_audio_bytes
        # at all), which is GIL-safe for atomic load/store and
        # tolerates a one-frame staleness on toggle (worst case:
        # one EP2 frame of zeros instead of audio at the moment of
        # sink swap, which is sub-3 ms and inaudible).
        # AUDIT VERDICT: existing locking is sufficient.
        from collections import deque
        # 1 s buffer at 48 kHz — caps unbounded growth if the demod
        # produces faster than the EP2 builder consumes.
        # ── TX audio queue (RX-audio-out path to AK4951) ──────────
        # Architectural model — Thetis-style backpressure (v0.0.9.1
        # rewrite):
        #
        #   * Bounded deque (max 4096 samples ≈ 85 ms at 48 kHz,
        #     mirroring Thetis's obbuffs ring at OBB_MULT=2 ×
        #     2048 = 4096 complex samples in network.c).
        #   * threading.Condition for blocking handoff:
        #     - Producer (DSP worker thread) calls queue_tx_audio
        #       → blocks (with timeout) when deque is full.  This
        #       is the rate authority: worker can't outrun EP2
        #       drain because EP2 cadence is locked to HL2's
        #       gateware clock.
        #     - Consumer (EP2 builder, runs inside _rx_loop) calls
        #       _pack_audio_bytes → waits (with short timeout)
        #       when deque is short.  On consumer timeout, fall
        #       back to SAMPLE-AND-HOLD (re-emit last sample),
        #       NOT zero-pad.  Sample-and-hold = DC continuity =
        #       no step discontinuity = no audible click.
        #
        # This matches the Thetis pattern documented in
        # ChannelMaster/network.c::sendOutbound (line 1337,
        # WaitForSingleObject(hobbuffsRun[1], INFINITE)).  Thetis
        # has no zero-padding code path on its RX audio output;
        # producer/consumer rendezvous on a semaphore.  Lyra's
        # Python implementation uses Condition + short timeouts to
        # avoid blocking the RX loop indefinitely on a stalled
        # worker (which would cascade into UDP RX buffer overflow).
        #
        # Capacity: 4096 (~85 ms) is large enough to absorb worker
        # block bursts (smaller now after audio_block 2048 → 512
        # change), small enough to keep TX-side latency low when
        # v0.2 lands.
        self._tx_audio: deque = deque()
        self._tx_audio_lock = threading.Lock()
        self._tx_audio_cv = threading.Condition(self._tx_audio_lock)
        self._tx_audio_max = 4096  # ~85 ms at 48 kHz
        # Sample-and-hold state: last (L, R) sample observed by the
        # consumer, repeated when the deque underflows.  Initialized
        # to silence so startup before any audio arrives is silent
        # (not random noise from uninitialised memory).
        self._tx_audio_last: tuple[float, float] = (0.0, 0.0)

        # EP2 cadence throttle: increments every EP6 frame; keepalive
        # only fires when count % (sample_rate/48000) == 0.
        self._ep6_count = 0
        self.tx_audio_gain = 0.5
        # Opt-in: pack audio into EP2 frames. When False (default), the TX
        # audio slots are left at zero. Turn this on only for AK4951 output.
        self.inject_audio_tx = False
        # AK4951 sink diagnostics — frame-level event counters surfaced
        # in the UI status bar.  Underrun: EP2 builder timed out
        # waiting for samples and used sample-and-hold.  Overrun:
        # producer timed out waiting for consumer to drain and the
        # incoming samples were dropped (rare with the bounded-deque
        # backpressure architecture).  Healthy operation: both stay
        # at 0.  v0.0.9.1+
        self.tx_audio_underruns: int = 0
        self.tx_audio_overruns: int = 0

    # -- control frame (EP2) for initial config -----------------------------
    def _build_ep2_frame(self, c0: int, c1: int, c2: int, c3: int, c4: int) -> bytes:
        """Build an EP2 control frame with one C&C write in each USB block,
        plus up to 126 audio samples (63 per block) pulled from the TX queue.

        Sample layout per HPSDR P1: each 8-byte slot is
        Left16(BE) + Right16(BE) + TX_I16(BE) + TX_Q16(BE). We place mono
        audio in both Left and Right; TX_I/Q stays zero while not transmitting.
        """
        frame = bytearray(1032)
        frame[0] = 0xEF
        frame[1] = 0xFE
        frame[2] = 0x01
        frame[3] = 0x02  # EP2
        struct.pack_into(">I", frame, 4, self._tx_seq)
        self._tx_seq = (self._tx_seq + 1) & 0xFFFFFFFF

        audio_bytes = self._pack_audio_bytes(126) if self.inject_audio_tx else None

        for block_idx, block_off in enumerate((8, 520)):
            frame[block_off + 0] = 0x7F
            frame[block_off + 1] = 0x7F
            frame[block_off + 2] = 0x7F
            frame[block_off + 3] = c0 & 0xFE  # bit 0 = MOX (0 for RX)
            frame[block_off + 4] = c1 & 0xFF
            frame[block_off + 5] = c2 & 0xFF
            frame[block_off + 6] = c3 & 0xFF
            frame[block_off + 7] = c4 & 0xFF
            if audio_bytes is not None:
                slot_start = block_off + 8
                src = audio_bytes[block_idx * 504:(block_idx + 1) * 504]
                frame[slot_start:slot_start + 504] = src
            # else: payload bytes stay zero (identical to pre-audio behavior)
        return bytes(frame)

    # Consumer-side wait timeout for the EP2 builder.  EP2 fires at
    # 380 Hz (one frame every ~2.6 ms); we can wait up to ~5 ms for
    # the producer to deliver more samples without blocking the RX
    # loop long enough to overflow the UDP buffer.  On timeout we
    # fall back to SAMPLE-AND-HOLD (NOT zero-pad) -- DC continuity
    # is inaudible where a step to zero is a click.  Mirrors the
    # graceful-degradation property of Thetis's WaitForSingleObject
    # (Thetis uses INFINITE since C++ thread scheduling is
    # deterministic; Python's GIL + GC make a small timeout safer).
    _PACK_WAIT_TIMEOUT_S = 0.005

    def _pack_audio_bytes(self, n_samples: int) -> bytes:
        """Pull n_samples from the TX queue and pack as HPSDR TX
        stereo bytes.  Blocks (with a short timeout) when the queue
        is short, then falls back to sample-and-hold rather than
        zero-padding -- the v0.0.9.1 click fix.

        Queue items are (L, R) float tuples at 48 kHz audio rate.
        Producer/consumer rendezvous on ``self._tx_audio_cv``.
        """
        import numpy as np
        import time as _time
        pulled: list[tuple[float, float]] = []
        with self._tx_audio_cv:
            deadline = _time.monotonic() + self._PACK_WAIT_TIMEOUT_S
            while len(self._tx_audio) < n_samples:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                self._tx_audio_cv.wait(timeout=remaining)
            # Pull whatever's available (could be < n_samples if we
            # timed out -- handled below with sample-and-hold).
            avail = min(len(self._tx_audio), n_samples)
            for _ in range(avail):
                pulled.append(self._tx_audio.popleft())
            if pulled:
                # Remember the most recent real sample for the
                # sample-and-hold fallback on the NEXT underrun.
                self._tx_audio_last = pulled[-1]
            # Notify producer in case it was waiting on a full queue.
            self._tx_audio_cv.notify()
        if avail < n_samples:
            # Underrun — repeat the LAST sample seen instead of
            # injecting zeros.  Sample-and-hold avoids the step
            # discontinuity that zero-pad creates; the operator's
            # ear hears at most a brief DC hold (subaudible) where
            # zero-pad produced an audible click.  Each underrun
            # event still increments the counter so the operator
            # has visibility (status-bar "Audio: N under" indicator).
            self.tx_audio_underruns += 1
            pulled.extend(
                [self._tx_audio_last] * (n_samples - avail))
        # pulled is a list of (L, R) tuples — split into separate arrays.
        lr = np.asarray(pulled, dtype=np.float32)        # shape (N, 2)
        lr *= self.tx_audio_gain
        np.clip(lr, -1.0, 1.0, out=lr)
        int16 = (lr * 32767.0).astype(">i2")             # shape (N, 2) big-endian
        left_bytes  = int16[:, 0].tobytes()
        right_bytes = int16[:, 1].tobytes()
        # Interleave L R I Q per sample (TX_I/TX_Q stay zero on RX).
        out = bytearray(n_samples * 8)
        for i in range(n_samples):
            out[i * 8 + 0:i * 8 + 2] = left_bytes [i * 2:i * 2 + 2]
            out[i * 8 + 2:i * 8 + 4] = right_bytes[i * 2:i * 2 + 2]
            # bytes 4..7 already zero
        return bytes(out)

    def queue_tx_audio(self, audio):
        """Push float audio samples (range [-1, 1]) into the EP2 TX queue.

        Accepts either:
          - 1D mono ndarray  → duplicated to (L, R) for backward compat
          - 2D stereo ndarray of shape (N, 2)  → stored as (L, R) tuples
            so per-channel content (e.g. balance / pan output) survives
            into the AK4951 codec L/R fields of the EP2 audio slot.

        AK4951 OUTPUT IS DECOUPLED FROM IQ RATE
        ---------------------------------------
        Earlier versions tried to upsample audio to match the EP6 IQ
        rate (96/192/384 k) on the assumption that EP2 frames drained
        at the IQ rate. In practice this produced chopped / distorted
        AK4951 audio at every rate above 48 k. Empirically the AK4951
        codec on the HL2 plays at 48 kHz regardless of the EP6 RX
        rate, so we always queue audio at 48 kHz and let the EP2
        frame builder consume it at whatever cadence the gateware
        expects. The spectrum/panadapter view stays at the operator's
        chosen IQ rate; only the audio path is locked to 48 kHz.
        """
        import numpy as np
        a = np.asarray(audio, dtype=np.float32)
        if a.ndim == 1:
            # Mono → duplicate to both channels (legacy behavior).
            pairs = list(zip(a.tolist(), a.tolist()))
        elif a.ndim == 2 and a.shape[1] == 2:
            pairs = [(float(l), float(r)) for l, r in a]
        else:
            # Defensive: flatten anything else as mono so we don't drop
            # audio silently on an unexpected shape.
            flat = a.reshape(-1)
            pairs = list(zip(flat.tolist(), flat.tolist()))
        # Producer-side backpressure: block (with timeout) if the
        # deque is full.  This is the architectural rate-authority
        # property -- the worker thread can't outrun the EP2 drain
        # because the EP2 cadence is locked to HL2's gateware clock.
        # On timeout (worker has been waiting > 50 ms with no drain
        # progress -- shouldn't happen in normal operation), drop
        # the new samples and count it as overrun.  The deque
        # capacity (4096 samples ≈ 85 ms) is large enough to absorb
        # any natural producer/consumer cadence mismatch.
        import time as _time
        with self._tx_audio_cv:
            deadline = _time.monotonic() + 0.050
            while len(self._tx_audio) + len(pairs) > self._tx_audio_max:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    # Timeout — consumer hasn't drained.  Indicates
                    # EP2 thread has stalled (e.g. UDP send blocked,
                    # network unplugged).  Drop these samples; the
                    # alternative is unbounded queue growth.
                    self.tx_audio_overruns += len(pairs)
                    return
                if not self.inject_audio_tx:
                    # Stream is shutting down — abort cleanly.
                    return
                self._tx_audio_cv.wait(timeout=remaining)
            self._tx_audio.extend(pairs)
            self._tx_audio_cv.notify()

    def clear_tx_audio(self):
        """Drain any pending samples from the TX audio queue. Called
        by AK4951Sink on init/close to prevent stale audio from a
        previous session leaking into a new session — the symptom
        was "digitized robotic" sound right after switching sinks."""
        with self._tx_audio_cv:
            self._tx_audio.clear()
            # Reset sample-and-hold so a stale sample from the old
            # session doesn't bleed into the new session.
            self._tx_audio_last = (0.0, 0.0)
            # Wake any waiting producer so it can either re-fill or
            # exit cleanly (the producer sees inject_audio_tx=False
            # on shutdown and returns).
            self._tx_audio_cv.notify_all()

    def fade_and_replace_tx_audio(self, fade_ms: float = 5.0) -> int:
        """Replace the TX audio queue with a short fade-out tail.

        Quiet-pass v0.0.7.1 (audio_pops_audit P0.3): on AK4951 sink
        close the previous behaviour was to flip ``inject_audio_tx``
        instantly, which caused the EP2 frame builder's audio L/R
        bytes to jump from real samples to zero in one frame
        (~2.6 ms cadence at 380 Hz EP2 rate).  At the AK4951 codec
        this presented as a sample-domain step from steady-state
        amplitude to silence — an audible click on every sink swap.

        The fix: replace whatever is currently queued with a short
        linearly-ramped fade-out of the FIRST ``fade_ms`` worth of
        the existing queue.  After this returns, the queue contains
        at most ``fade_ms × 48`` samples ramping from the current
        amplitude down to zero.  Any samples beyond the fade window
        are dropped (operator was closing the session anyway).

        Caller is expected to sleep ``fade_ms + ~2`` ms (so the
        EP2 builder pulls and sends the faded samples) BEFORE
        flipping ``inject_audio_tx = False`` and calling
        ``clear_tx_audio``.

        Returns the number of samples queued in the fade tail
        (useful for the caller to compute the drain wait).
        """
        FADE_SAMPLES = max(1, int(fade_ms * 48.0))
        with self._tx_audio_cv:
            n = len(self._tx_audio)
            if n == 0:
                # Nothing playing to fade out.  No click possible.
                return 0
            # Pull all queued samples; we'll repush only the faded tail.
            # Using the FIRST fade_n samples (head-of-queue) preserves
            # signal continuity — those are the next samples that
            # would have played anyway.  Tail samples (back-of-queue)
            # would arrive later and are dropped on close.
            all_pairs = [self._tx_audio.popleft() for _ in range(n)]
            fade_n = min(FADE_SAMPLES, n)
            for i in range(fade_n):
                l, r = all_pairs[i]
                # Linear ramp 1.0 -> 0.0 across the fade window.
                # A cosine ramp would be smoother but linear is
                # imperceptible at 5 ms — humans don't resolve
                # envelope shape that fast.
                ramp = 1.0 - (float(i) / float(fade_n))
                self._tx_audio.append(
                    (float(l) * ramp, float(r) * ramp))
            return fade_n

    def _send_cc(self, c0: int, c1: int, c2: int, c3: int, c4: int):
        """Send one C&C write via EP2. Thread-safe."""
        if self._sock is None:
            return
        with self._send_lock:
            frame = self._build_ep2_frame(c0, c1, c2, c3, c4)
            self._sock.sendto(frame, (self.radio_ip, DISCOVERY_PORT))

    def _send_config(self):
        rate_code = SAMPLE_RATES[self.sample_rate]
        # C4 bit 2 = duplex (required; otherwise RX1 freq is slaved to TX).
        self._send_cc(0x00, rate_code, 0x00, 0x00, self._config_c4)

    # -- public API ---------------------------------------------------------
    def start(
        self,
        on_samples: Callable[[np.ndarray, FrameStats], None],
        rx_freq_hz: Optional[int] = None,
        lna_gain_db: Optional[int] = None,
    ):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("stream already running")

        self._stop_event.clear()
        self.stats = FrameStats()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bump UDP receive buffer to 4 MB.  Default Windows UDP RCVBUF
        # is ~64-208 KB; at 192 kHz IQ rate the HL2 streams roughly
        # 1.5 MB/sec of EP6 frames, so the kernel buffer fills in
        # under a second of CPU stall and starts silently dropping
        # frames.  Each drop produces a sequence-number gap which the
        # parser counts in seq_errors and (post v0.0.9.1) covers with
        # a fade in the audio path -- but the cheaper fix is just to
        # not drop frames in the first place.  4 MB ≈ 2.6 seconds of
        # buffer headroom, which covers any plausible Python GC pause
        # or context-switch storm.  The kernel may clamp the request
        # to a smaller value (rmem_max sysctl on Linux, registry
        # AFD\Parameters\DefaultReceiveWindow on Windows); we log the
        # actual buffer size so the operator can spot it if needed.
        try:
            self._sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            actual = self._sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF)
            _log.info(
                "UDP RX buffer: requested 4 MB, kernel granted %.1f MB",
                actual / 1024 / 1024)
        except OSError as e:
            # Non-fatal -- if SO_RCVBUF can't be bumped, we still run
            # with default buffer size, just more vulnerable to drops.
            _log.warning("Could not bump SO_RCVBUF: %s", e)
        self._sock.bind(("0.0.0.0", 0))  # ephemeral port; radio will reply here
        self._sock.settimeout(0.5)

        # IMPORTANT: HL2 ignores IQ-start if it has already seen EP2 traffic.
        # Send the start command FIRST, then push config via EP2 once the
        # radio has begun streaming.
        start_pkt = _build_start_stop_packet(START_IQ)
        self._sock.sendto(start_pkt, (self.radio_ip, DISCOVERY_PORT))

        self._thread = threading.Thread(
            target=self._rx_loop, args=(on_samples,), daemon=True
        )
        self._thread.start()

        # Give the radio a moment to begin streaming before we push config.
        time.sleep(0.05)
        self._send_config()
        if rx_freq_hz is not None:
            self._set_rx1_freq(rx_freq_hz)
        if lna_gain_db is not None:
            self.set_lna_gain_db(lna_gain_db)

    def _set_rx1_freq(self, hz: int):
        # NOTE: this method is called on EVERY frequency change —
        # click-to-tune, wheel-zoom, drag-pan, band button, mode
        # change (CW pitch correction), spectrum drag-tune. A
        # `print(...)` here used to log each call; that was useful
        # during early HPSDR P1 bring-up but became a real-time
        # bottleneck once operators were actively working the
        # panadapter (Windows cmd.exe console is notoriously slow
        # under heavy stdout, and Python's main thread blocked on
        # those writes — visible as gradual visual drag on the
        # spectrum / waterfall over a session, with audio unaffected
        # because audio runs on a separate thread/sink).  Removed
        # 2026-04-29.  If we ever need the logging back, gate it
        # behind a LYRA_DEBUG_FREQ env var or send to logging.debug.
        c0 = 0x04  # RX1 NCO freq
        c1 = (hz >> 24) & 0xFF
        c2 = (hz >> 16) & 0xFF
        c3 = (hz >> 8) & 0xFF
        c4 = hz & 0xFF
        self._send_cc(c0, c1, c2, c3, c4)
        with self._cc_lock:
            self._cc_registers[c0] = (c1, c2, c3, c4)
        self._keepalive_cc = (c0, c1, c2, c3, c4)

    def set_sample_rate(self, rate: int):
        """Change sample rate on a running stream. Keepalive picks up the new code."""
        if rate not in SAMPLE_RATES:
            raise ValueError(f"rate must be one of {list(SAMPLE_RATES)}")
        if self._sock is None:
            raise RuntimeError("stream not started")
        self.sample_rate = rate
        # Reset EP2 cadence counter so the new rate's keepalive
        # cadence starts clean. Without this, switching from 192 k to
        # 48 k would carry stale counter modulo state.
        self._ep6_count = 0
        rate_code = SAMPLE_RATES[rate]
        self._send_cc(0x00, rate_code, 0x00, 0x00, self._config_c4)
        with self._cc_lock:
            self._cc_registers[0x00] = (rate_code, 0x00, 0x00, self._config_c4)
        self._keepalive_cc = (0x00, rate_code, 0x00, 0x00, self._config_c4)

    def reassert_rate_keepalive(self):
        """Vestigial — kept for callers from before the round-robin
        C&C cycling landed. Now a no-op because every register
        (including 0x00 sample rate) is re-asserted automatically
        every ~N frames by the round-robin keepalive in _rx_loop.
        Safe to remove once all callers are cleaned up."""
        return

    def set_lna_gain_db(self, gain_db: int):
        """Set HL2 LNA gain in dB. Range -12..+48.

        HL2 gateware: C0=0x14, C4[7:6]=01 (override enable), C4[5:0]=gain_db+12.
        """
        if not -12 <= gain_db <= 48:
            raise ValueError("gain_db must be in -12..+48")
        if self._sock is None:
            raise RuntimeError("stream not started")
        c4 = 0x40 | ((gain_db + 12) & 0x3F)
        self._send_cc(0x14, 0, 0, 0, c4)
        with self._cc_lock:
            self._cc_registers[0x14] = (0, 0, 0, c4)
        self._keepalive_cc = (0x14, 0, 0, 0, c4)

    def stop(self):
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.sendto(
                    _build_start_stop_packet(STOP), (self.radio_ip, DISCOVERY_PORT)
                )
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- internal -----------------------------------------------------------
    def _rx_loop(self, on_samples: Callable[[np.ndarray, FrameStats], None]):
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                data, _addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            parsed = _parse_iq_frame(data)
            if parsed is None:
                continue
            seq, samples, cc0, cc1 = parsed

            if self.stats.seq_expected == -1:
                self.stats.seq_expected = (seq + 1) & 0xFFFFFFFF
            else:
                if seq != self.stats.seq_expected:
                    self.stats.seq_errors += 1
                self.stats.seq_expected = (seq + 1) & 0xFFFFFFFF

            self.stats.frames += 1
            self.stats.samples += samples.shape[0]
            self.stats.last_c1_c4 = cc1

            # Fold both C&C blocks into the rolling telemetry slots.
            # Each EP6 frame carries two C&C blocks and the radio
            # rotates the C0 telemetry address across blocks AND
            # frames — hitting both blocks here halves the latency
            # to the next refresh of any given field.
            _decode_hl2_telemetry(cc0, self.stats)
            _decode_hl2_telemetry(cc1, self.stats)

            on_samples(samples, self.stats)

            # EP2 cadence is decoupled from EP6 cadence and locked to
            # 48 kHz audio rate. At 48 k IQ that's 1:1 with EP6; at
            # 96/192/384 k we send EP2 every 2/4/8 EP6 frames so the
            # AK4951 codec (hard-locked at 48 kHz fs) doesn't get
            # over-fed and crackle. The TX thread is paced by audio
            # rate independent of EP6 cadence. HL2 keepalive watchdog
            # is plenty fast at 380 Hz so this doesn't trigger an
            # underrun-halt.
            n = max(1, self.sample_rate // 48000)
            self._ep6_count = getattr(self, "_ep6_count", 0) + 1
            if self._ep6_count % n != 0:
                continue
            # Round-robin C&C keepalive — pick the next register from
            # the registered set so EVERY one (sample rate, RX1 freq,
            # LNA, etc.) gets re-asserted cyclically. Standard HPSDR
            # P1 host pattern: round-robin index wraps over all
            # registers each frame. Single-register approach used to
            # cause stuck-silence after big freq jumps because once
            # _keepalive_cc held a freq command, sample-rate (C&C
            # 0x00) stopped going out and HL2 EP6 IQ stream could
            # stall.
            try:
                with self._cc_lock:
                    if self._cc_registers:
                        keys = sorted(self._cc_registers.keys())
                        c0 = keys[self._cc_rr_idx % len(keys)]
                        c1, c2, c3, c4 = self._cc_registers[c0]
                        self._cc_rr_idx = (self._cc_rr_idx + 1) % len(keys)
                    else:
                        c0 = c1 = c2 = c3 = 0
                        c4 = self._config_c4
                self._send_cc(c0, c1, c2, c3, c4)
            except OSError:
                break
