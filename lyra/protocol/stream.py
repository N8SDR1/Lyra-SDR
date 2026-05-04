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

        # ── EP2 writer architecture (v0.0.9.2 audio rebuild) ─────────
        # A dedicated EP2 writer thread drives the host->radio frame
        # send at the AK4951 codec's audio rate (~380 Hz = 48 kHz / 126
        # samples per frame), independent of EP6 (radio->host) UDP
        # arrival cadence.  This isolates the codec-facing frame
        # cadence from upstream network burstiness and gives the
        # writer thread its own scheduling priority slot (MMCSS Pro
        # Audio class on Windows) for jitter immunity.
        #
        # Producer/consumer coordination uses a small bounded queue of
        # 126-sample stereo audio blocks (8 deep = ~21 ms buffer).
        # Producer (DSP path -> AK4951Sink.write -> submit_audio_block)
        # waits when the queue is full -- this is the backpressure
        # half of the handshake and bounds memory + latency.  Consumer
        # (the EP2 writer thread, draining one block per cadence tick)
        # never waits on emptiness; if no audio is queued it sends a
        # C&C-only frame to keep the HL2 watchdog satisfied.
        #
        # Counter semantics:
        #   tx_audio_underruns  -- EP2 frame fired without audio while
        #                          inject_audio_tx was True (= producer
        #                          stalled past one cadence tick).
        #                          Each one is an audible click on
        #                          the AK4951 codec.
        #   tx_audio_overruns   -- producer hit the queue-full gate
        #                          and waited.  Healthy in steady
        #                          state at ~95 Hz worker rate
        #                          (producer pushes 4 blocks per
        #                          worker iteration; queue holds them
        #                          until writer drains).
        #   tx_audio_high_water -- max queue depth (samples) observed
        #                          since the last UI read.  Telemetry.
        #   tx_audio_producer_waits -- count of waited submits.
        from collections import deque
        EP2_QUEUE_DEPTH = 8   # 8 blocks * 126 = 1008 samples = 21 ms
        self._ep2_audio_queue: deque = deque(maxlen=EP2_QUEUE_DEPTH)
        self._ep2_audio_lock = threading.Lock()
        self._ep2_audio_cond = threading.Condition(self._ep2_audio_lock)
        self._ep2_writer_thread: Optional[threading.Thread] = None
        self.tx_audio_gain = 0.5
        # Opt-in: pack audio into EP2 frames. When False (default), the
        # writer sends C&C-only frames (audio slots stay zero).  Turn
        # this on only for AK4951 output.
        self.inject_audio_tx = False
        # Diagnostics
        self.tx_audio_underruns: int = 0
        self.tx_audio_overruns: int = 0
        self.tx_audio_high_water: int = 0
        self.tx_audio_producer_waits: int = 0

    def read_tx_audio_high_water(self) -> int:
        """Atomically read + reset the EP2 audio queue high-water mark
        (in samples).  UI status tick reads this for telemetry."""
        with self._ep2_audio_cond:
            hw = self.tx_audio_high_water
            self.tx_audio_high_water = len(self._ep2_audio_queue) * 126
            return hw

    # ── Producer-side: submit a 126-sample stereo audio block ──────────
    def submit_audio_block(self, block_l_r):
        """Producer-side: deposit one 126-sample stereo audio block
        into the EP2 writer's queue.  Blocks (with bounded timeout)
        when the queue is at capacity -- this is the producer-side of
        the producer/consumer handshake and naturally paces the
        producer down to consumer cadence (~380 Hz).

        Parameter:
            block_l_r -- iterable of 126 (left, right) float pairs in
                         range [-1, 1].

        Behavior:
            - If queue has room: append, notify, return immediately.
            - If queue at maxlen: wait on the condition (50 ms
              timeout) until the writer thread pops a block, then
              append.  ``tx_audio_producer_waits`` increments.
            - If the stream is shutting down: drop the block silently
              and return (avoid pushing into a queue about to be
              cleared).

        The producer never silently drops audio -- a busy producer is
        held at the gate until the writer drains.  This is the
        critical difference from a maxlen=N deque-with-extend, which
        would silently drop oldest samples on overflow and inject
        sample-time discontinuities into the audio stream.
        """
        if self._stop_event.is_set():
            return
        # Convert to a list of float tuples once.  Cheap (126 items).
        # Accept anything iterable of length-2 pairs; the AK4951 sink
        # produces shape (126, 2) numpy arrays.
        try:
            pairs = [(float(l), float(r)) for l, r in block_l_r]
        except (TypeError, ValueError):
            return  # malformed input; drop block
        if not pairs:
            return
        with self._ep2_audio_cond:
            waited = False
            while (len(self._ep2_audio_queue)
                   >= self._ep2_audio_queue.maxlen
                   and not self._stop_event.is_set()):
                waited = True
                self._ep2_audio_cond.wait(timeout=0.050)
            if waited:
                self.tx_audio_producer_waits += 1
                self.tx_audio_overruns += 1
            if self._stop_event.is_set():
                return
            self._ep2_audio_queue.append(pairs)
            depth_samples = len(self._ep2_audio_queue) * 126
            if depth_samples > self.tx_audio_high_water:
                self.tx_audio_high_water = depth_samples
            # Wake any other waiters (rare; only if multiple producer
            # threads share this stream).
            self._ep2_audio_cond.notify_all()

    def clear_audio_queue(self):
        """Drain any pending audio blocks.  Called by AK4951Sink on
        init/close to prevent stale audio from a previous session
        leaking into a new session.  Notifies producers so any waiter
        wakes promptly."""
        with self._ep2_audio_cond:
            self._ep2_audio_queue.clear()
            self._ep2_audio_cond.notify_all()

    # Backward-compat aliases (old name kept so external callers don't
    # break across the rebuild; both delegate to the new methods).
    def clear_tx_audio(self):
        self.clear_audio_queue()

    def queue_tx_audio(self, audio):
        """Legacy entry point: accepts mono (N,) or stereo (N, 2)
        ndarrays of arbitrary length, slices into 126-sample blocks,
        and submits each via ``submit_audio_block``.

        Sinks should prefer to call ``submit_audio_block`` directly
        with already-126-shaped blocks (cheaper, fewer allocations);
        this wrapper exists so older code paths still work without
        rewrites.
        """
        import numpy as np
        a = np.asarray(audio, dtype=np.float32)
        if a.ndim == 1:
            stereo = np.stack((a, a), axis=1)
        elif a.ndim == 2 and a.shape[1] == 2:
            stereo = a
        else:
            stereo = np.stack((a.reshape(-1), a.reshape(-1)), axis=1)
        n = stereo.shape[0]
        for i in range(0, n, 126):
            chunk = stereo[i:i + 126]
            if chunk.shape[0] < 126:
                # Pad partial trailing chunk with silence so the
                # writer always sees a full 126-sample frame.  Cost:
                # a few zero samples at the very end of an audio
                # write; inaudible.
                pad = np.zeros((126 - chunk.shape[0], 2),
                                dtype=np.float32)
                chunk = np.concatenate((chunk, pad), axis=0)
            self.submit_audio_block(chunk)

    # No-op stub for v0.0.9.1's fade-on-close path.  In the new
    # design the writer thread sees inject_audio_tx=False after
    # AK4951Sink.close() and stops emitting audio bytes within one
    # cadence tick (2.6 ms).  No queue tail to fade; close path is
    # simpler.  Stub returns 0 so any caller doing the post-fade
    # sleep just sleeps for nothing.
    def fade_and_replace_tx_audio(self, fade_ms: float = 5.0) -> int:
        return 0

    # -- EP2 frame builder + audio packer ---------------------------------
    def _build_ep2_frame(self, c0: int, c1: int, c2: int, c3: int, c4: int,
                          audio_bytes: Optional[bytes] = None) -> bytes:
        """Build an EP2 frame: 8-byte header + 2 USB blocks (512 bytes each).

        Each USB block carries the same C&C write (c0..c4) and 504 bytes
        of LRIQ audio (63 8-byte tuples).  ``audio_bytes`` is 1008 bytes
        of pre-packed LRIQ audio (126 samples * 8 bytes); split across
        the two USB blocks.  When ``audio_bytes`` is None, the audio
        slots stay zero (frame is C&C-only).
        """
        frame = bytearray(1032)
        frame[0] = 0xEF
        frame[1] = 0xFE
        frame[2] = 0x01
        frame[3] = 0x02  # EP2
        struct.pack_into(">I", frame, 4, self._tx_seq)
        self._tx_seq = (self._tx_seq + 1) & 0xFFFFFFFF

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

    def _pack_audio_pairs(self, pairs) -> bytes:
        """Convert a 126-element list of (L, R) float tuples to 1008 bytes
        of LRIQ-packed audio for the EP2 audio slots.  TX I/Q stays zero
        (RX-only)."""
        import numpy as np
        lr = np.asarray(pairs, dtype=np.float32)        # (126, 2)
        lr = lr * self.tx_audio_gain                     # apply final trim
        np.clip(lr, -1.0, 1.0, out=lr)
        int16 = (lr * 32767.0).astype(">i2")             # big-endian int16
        left_bytes  = int16[:, 0].tobytes()
        right_bytes = int16[:, 1].tobytes()
        out = bytearray(126 * 8)
        for i in range(126):
            out[i * 8 + 0:i * 8 + 2] = left_bytes [i * 2:i * 2 + 2]
            out[i * 8 + 2:i * 8 + 4] = right_bytes[i * 2:i * 2 + 2]
            # bytes 4..7 stay zero (TX I/Q slots; not transmitting)
        return bytes(out)

    # ── EP2 writer thread + MMCSS priority helper ─────────────────────
    @staticmethod
    def _maybe_apply_mmcss_pro_audio(profile_name: str = "Pro Audio"):
        """Elevate the calling thread to the MMCSS Pro Audio task class
        on Windows.  No-op (and silently absorbs any error) on other
        platforms or if the AVRT API is unavailable.

        Real-time audio threads on Windows are scheduled by the Multi-
        Media Class Scheduler Service (MMCSS) when registered via
        ``AvSetMmThreadCharacteristicsW``.  This bumps thread
        scheduling priority above default user-thread priority,
        preventing UI thread / GC / generic background work from
        starving the audio path during scheduling jitter events.

        Safe failure mode: if the call fails we just run at default
        priority -- one operator-perceptible result is occasional
        scheduling-jitter audio glitches under heavy CPU load, which
        is the pre-Commit-4 status quo anyway.
        """
        try:
            import sys
            if not sys.platform.startswith("win"):
                return
            import ctypes
            avrt = ctypes.WinDLL("avrt", use_last_error=True)
            AvSetMmThreadCharacteristicsW = avrt.AvSetMmThreadCharacteristicsW
            AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
            AvSetMmThreadCharacteristicsW.argtypes = [
                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
            AvSetMmThreadPriority = avrt.AvSetMmThreadPriority
            AvSetMmThreadPriority.restype = ctypes.c_int
            AvSetMmThreadPriority.argtypes = [
                ctypes.c_void_p, ctypes.c_int]
            task_index = ctypes.c_ulong(0)
            handle = AvSetMmThreadCharacteristicsW(
                profile_name, ctypes.byref(task_index))
            if handle:
                # AVRT_PRIORITY_CRITICAL = 2 (highest defined).
                AvSetMmThreadPriority(handle, 2)
                _log.info(
                    "MMCSS '%s' priority elevated for thread %s",
                    profile_name, threading.current_thread().name)
            else:
                _log.warning(
                    "AvSetMmThreadCharacteristicsW returned NULL; "
                    "thread will run at default priority")
        except Exception as e:  # noqa: BLE001
            _log.warning("MMCSS priority elevation failed: %s", e)

    def _ep2_writer_loop(self):
        """Dedicated thread that drives EP2 frame send cadence at the
        AK4951 audio rate (~380 Hz = 48 kHz / 126 samples per frame).

        Each iteration:
          1. Sleeps to the next cadence tick (drift-corrected timer).
          2. Pops the oldest 126-sample audio block from the producer
             queue (if any; non-blocking).
          3. Picks the next C&C round-robin register entry.
          4. Builds the 1032-byte EP2 frame (control header + 2 USB
             blocks each carrying C&C + audio).
          5. Sends via UDP.

        Steady cadence isolates the codec-side framing from upstream
        UDP burstiness (which the previous design inherited by sending
        EP2 from inside ``_rx_loop``).  When ``inject_audio_tx`` is
        False (PortAudio sink active) the writer keeps firing C&C-only
        frames so HL2's keepalive watchdog stays satisfied.
        """
        EP2_PERIOD = 126.0 / 48000.0  # 2.625 ms = 380.95 Hz

        # Elevate scheduling priority on Windows.  Best-effort.
        self._maybe_apply_mmcss_pro_audio("Pro Audio")

        next_fire = time.monotonic() + EP2_PERIOD
        while not self._stop_event.is_set():
            # Wait until next cadence tick.  time.sleep is fine here
            # since the cadence is steady; we do not need event-
            # driven wakeup (producers don't accelerate the writer).
            now = time.monotonic()
            delay = next_fire - now
            if delay > 0:
                time.sleep(delay)
            next_fire += EP2_PERIOD
            # Resync if we fell badly behind (e.g. system suspend).
            now = time.monotonic()
            if next_fire < now:
                next_fire = now + EP2_PERIOD

            # Pull one audio block from producer queue (non-blocking).
            audio_pairs = None
            with self._ep2_audio_cond:
                if self._ep2_audio_queue:
                    audio_pairs = self._ep2_audio_queue.popleft()
                    # Wake any producer waiting on a full queue.
                    self._ep2_audio_cond.notify_all()

            audio_bytes: Optional[bytes] = None
            if audio_pairs is not None and self.inject_audio_tx:
                try:
                    audio_bytes = self._pack_audio_pairs(audio_pairs)
                except Exception as e:  # noqa: BLE001
                    _log.warning("EP2 audio packing error: %s", e)
                    audio_bytes = None
            elif self.inject_audio_tx:
                # AK4951 active but producer didn't supply a block in
                # time -> underrun.  Send a C&C-only frame; codec sees
                # silence for one frame.
                self.tx_audio_underruns += 1

            # Pick C&C round-robin register entry.
            try:
                with self._cc_lock:
                    if self._cc_registers:
                        keys = sorted(self._cc_registers.keys())
                        c0 = keys[self._cc_rr_idx % len(keys)]
                        c1, c2, c3, c4 = self._cc_registers[c0]
                        self._cc_rr_idx = (
                            self._cc_rr_idx + 1) % len(keys)
                    else:
                        c0 = c1 = c2 = c3 = 0
                        c4 = self._config_c4
                frame = self._build_ep2_frame(
                    c0, c1, c2, c3, c4, audio_bytes)
                with self._send_lock:
                    if self._sock is not None:
                        self._sock.sendto(
                            frame, (self.radio_ip, DISCOVERY_PORT))
            except OSError:
                # Socket likely closed by stop().  Exit cleanly.
                break
            except Exception as e:  # noqa: BLE001
                _log.warning("EP2 writer iteration error: %s", e)

    def _send_cc(self, c0: int, c1: int, c2: int, c3: int, c4: int):
        """Send one C&C write via EP2 (one-shot, used by start/config
        path before the writer thread is up).  Thread-safe."""
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
            target=self._rx_loop, args=(on_samples,), daemon=True,
            name="hl2-rx-loop",
        )
        self._thread.start()

        # Start the dedicated EP2 writer thread.  This thread owns the
        # host->radio frame send cadence at the AK4951 audio rate,
        # decoupled from the bursty UDP-arrival cadence the rx_loop
        # sees.  See _ep2_writer_loop for the full design.
        self._ep2_writer_thread = threading.Thread(
            target=self._ep2_writer_loop, daemon=True,
            name="hl2-ep2-writer",
        )
        self._ep2_writer_thread.start()

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
        # Wake any producer held at the queue-full backpressure gate
        # so they can exit promptly instead of timing out their wait.
        try:
            with self._ep2_audio_cond:
                self._ep2_audio_cond.notify_all()
        except Exception:  # noqa: BLE001
            pass
        if self._sock is not None:
            try:
                self._sock.sendto(
                    _build_start_stop_packet(STOP), (self.radio_ip, DISCOVERY_PORT)
                )
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._ep2_writer_thread is not None:
            # Writer thread is timer-driven so it self-exits within one
            # cadence tick (~3 ms) of _stop_event being set.  Bounded
            # join.
            self._ep2_writer_thread.join(timeout=1.0)
            self._ep2_writer_thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- internal -----------------------------------------------------------
    def _rx_loop(self, on_samples: Callable[[np.ndarray, FrameStats], None]):
        """Pure RX loop: receive UDP datagrams, parse EP6 IQ frames,
        decode telemetry, dispatch samples to the host.

        EP2 (host->radio) frame send is owned by ``_ep2_writer_loop``
        on its own thread now; rx_loop is RX-only.  This decouples
        host->radio cadence from the bursty UDP-arrival cadence the
        receive socket sees.
        """
        assert self._sock is not None

        # Best-effort priority elevation -- the RX loop is also a
        # real-time path (UDP recv + IQ parse must keep up with HL2's
        # ~5 kHz datagram rate at 192 kHz IQ to avoid kernel buffer
        # backup -> seq_errors).
        self._maybe_apply_mmcss_pro_audio("Pro Audio")

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
