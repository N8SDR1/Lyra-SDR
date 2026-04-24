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

import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

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
        # frequency writes (RX1 freq gets slaved to TX freq). reference clients always
        # sets this bit. C4[5:3] = NDDC - 1 (0 = 1 receiver).
        self._config_c4 = 0x04  # duplex=1, NDDC=1
        self._keepalive_cc: tuple[int, int, int, int, int] = (
            0x00, SAMPLE_RATES[sample_rate], 0x00, 0x00, self._config_c4
        )
        self._send_lock = threading.Lock()

        # TX audio queue. Demod pipeline pushes float samples [-1, 1]; the
        # RX loop drains 126 samples per outgoing EP2 frame into the Left/
        # Right audio slots (gateware routes to AK4951 line-out).
        from collections import deque
        self._tx_audio: deque = deque(maxlen=48000)  # ~1 s buffer at 48 kHz
        self._tx_audio_lock = threading.Lock()
        self.tx_audio_gain = 0.5
        # Opt-in: pack audio into EP2 frames. When False (default), the TX
        # audio slots are left at zero. Turn this on only for AK4951 output.
        self.inject_audio_tx = False

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

    def _pack_audio_bytes(self, n_samples: int) -> bytes:
        """Dequeue up to n_samples, pad with zeros, pack as HPSDR TX stereo."""
        import numpy as np
        with self._tx_audio_lock:
            avail = min(len(self._tx_audio), n_samples)
            pulled = [self._tx_audio.popleft() for _ in range(avail)]
        if avail < n_samples:
            pulled.extend([0.0] * (n_samples - avail))
        arr = np.asarray(pulled, dtype=np.float32) * self.tx_audio_gain
        arr = np.clip(arr, -1.0, 1.0)
        int16 = (arr * 32767.0).astype(">i2")  # big-endian int16
        # Each sample: Left + Right + TX_I(0) + TX_Q(0)  (all 16-bit BE)
        left = int16.tobytes()
        right = int16.tobytes()
        tx_iq = (b"\x00\x00" * 2) * n_samples
        # Interleave:  L R I Q   per sample
        out = bytearray(n_samples * 8)
        for i in range(n_samples):
            out[i * 8 + 0:i * 8 + 2] = left[i * 2:i * 2 + 2]
            out[i * 8 + 2:i * 8 + 4] = right[i * 2:i * 2 + 2]
            # bytes 4..7 already zero
        return bytes(out)

    def queue_tx_audio(self, audio: "np.ndarray"):
        """Push float audio samples (range [-1, 1]) into the EP2 TX queue.

        NOTE ON >48 kHz OPERATION: the EP2 audio-slot interpretation is
        sample-rate-dependent in the HL2 gateware and we don't fully
        characterize it yet. Earlier nearest-neighbor upsample attempt
        introduced audible imaging, so we don't touch the audio buffer
        here. Instead, Radio.set_rate auto-switches the audio output to
        the PC sound device at rates >48 k. AK4951 output is only
        guaranteed reliable at 48 k.
        """
        with self._tx_audio_lock:
            self._tx_audio.extend(audio.tolist())

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
        c0 = 0x04  # RX1 NCO freq
        c1 = (hz >> 24) & 0xFF
        c2 = (hz >> 16) & 0xFF
        c3 = (hz >> 8) & 0xFF
        c4 = hz & 0xFF
        print(f"[stream] set RX1 freq {hz} Hz  C&C={c0:02X} {c1:02X} {c2:02X} {c3:02X} {c4:02X}")
        self._send_cc(c0, c1, c2, c3, c4)
        self._keepalive_cc = (c0, c1, c2, c3, c4)

    def set_sample_rate(self, rate: int):
        """Change sample rate on a running stream. Keepalive picks up the new code."""
        if rate not in SAMPLE_RATES:
            raise ValueError(f"rate must be one of {list(SAMPLE_RATES)}")
        if self._sock is None:
            raise RuntimeError("stream not started")
        self.sample_rate = rate
        rate_code = SAMPLE_RATES[rate]
        self._send_cc(0x00, rate_code, 0x00, 0x00, self._config_c4)
        self._keepalive_cc = (0x00, rate_code, 0x00, 0x00, self._config_c4)

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

            on_samples(samples, self.stats)

            # Keepalive: one EP2 frame per received EP6 frame, or the radio's
            # TX queue underruns and it halts the stream after ~N seconds.
            try:
                c0, c1, c2, c3, c4 = self._keepalive_cc
                self._send_cc(c0, c1, c2, c3, c4)
            except OSError:
                break
