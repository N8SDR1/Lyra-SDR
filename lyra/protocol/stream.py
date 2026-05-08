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

import ctypes
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
#
# NOTE: 48 k IQ rate is intentionally OMITTED from operator-
# selectable rates (was {48000: 0, 96000: 1, 192000: 2, 384000: 3}).
# Reason: at 48 k IQ rate the DSP block produces ~43 ms of audio
# per producer call (16 EP2 frames' worth, since IQ rate equals
# audio rate 1:1 instead of being decimated).  Path C's producer-
# paced semaphore drains all 16 frames in <1 ms and then waits
# 42 ms for the next burst -- the HL2 gateware FIFO can't absorb
# that without audible clicks/pops.  Higher IQ rates produce
# smaller bursts (8 frames at 96 k, 4 at 192 k) that the FIFO
# tolerates cleanly.  Rather than maintain a known-bad option,
# we drop it and start the operator-selectable range at 96 k.
# The HL2 gateware's rate code 0 (= 48 k IQ) is therefore not a
# rate Lyra ever requests; the codec's 48 k AUDIO rate is
# unaffected (different concept -- always 48 k regardless of IQ).
SAMPLE_RATES = {96000: 1, 192000: 2, 384000: 3}


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


# ── TPDF dither for float→int16 audio quantization ────────────────────
#
# The HL2 audio path quantizes Lyra's float32 audio to int16 for the
# AK4951 codec.  Without dither, quantization noise correlates with
# the signal — at low signal levels this manifests as audible
# "graininess" or "harshness," especially on quiet SSB passages and
# near silence (where the 16-bit LSB structure modulates the
# zero-crossing area).
#
# TPDF (Triangular Probability Density Function) dither at ±1 LSB
# peak amplitude decorrelates the quantization error so it becomes
# spectrally flat white noise at -90 dBFS RMS — well below the
# audible threshold on speakers / headphones at any reasonable
# listening level, and inaudible against the band noise the operator
# is actually trying to copy.
#
# Why TPDF specifically: rectangular (1-LSB uniform) dither still
# leaves audible noise modulation correlated with the signal.  TPDF
# (sum of two uniform [-0.5, 0.5] randoms) eliminates the
# modulation entirely — the noise floor is independent of signal
# content.  This is the standard professional-audio recipe (Wannamaker,
# Vanderkooy, Lipshitz) used by every mastering tool since the 90s.
#
# Cost: ~10 microseconds per 126-sample EP2 frame.  Inaudible
# overhead on the ~381 Hz EP2 writer cadence.

_DITHER_LSB_FLOAT = 1.0   # in float-scaled-to-int16 units (i.e. 1.0 = 1 LSB)
# Module-level RNG so we don't pay default_rng() construction cost
# on every call.  Thread-safe enough for our use — the EP2 writer
# is the only consumer.
_dither_rng = np.random.default_rng()


def _quantize_to_int16_be(lr: np.ndarray) -> np.ndarray:
    """Float32 [-1, 1] → big-endian int16 with TPDF dither.

    Input shape: (N, 2) for stereo or (N,) for mono.  Output shape
    matches input dtype=">i2".  Caller is responsible for any gain
    scaling / clipping; this only applies dither and quantizes.
    """
    scaled = lr * 32767.0
    # Triangular dither = sum of two uniform [-0.5, 0.5].  rng.random
    # returns [0, 1); subtract to center.
    n = scaled.size
    d1 = _dither_rng.random(n, dtype=np.float32)
    d2 = _dither_rng.random(n, dtype=np.float32)
    dither = (d1 - d2) * _DITHER_LSB_FLOAT  # range [-1, 1] LSB triangular
    scaled = scaled + dither.reshape(scaled.shape)
    # Clip BEFORE casting so we don't wrap around at full scale.
    np.clip(scaled, -32767.0, 32767.0, out=scaled)
    # Round-to-nearest (default int cast truncates toward zero, which
    # introduces a -0.5 LSB DC offset for negative-half-LSB samples;
    # explicit round avoids that).
    return np.round(scaled).astype(">i2")


class HL2Stream:
    """Open a P1 stream to an HL2, run an RX loop in a background thread.

    Typical use:
        s = HL2Stream("10.10.30.100", sample_rate=96000)
        s.start(on_samples=lambda samples, stats: ...)
        ...
        s.stop()
    """

    def __init__(self, radio_ip: str, sample_rate: int = 96000):
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
        # ── Round-robin C&C register table (Thetis-mirror) ──────────
        # Mirrors Thetis ChannelMaster\\networkproto1.c::WriteMainLoop_HL2
        # case 0..18 verbatim.  Each USB block in an EP2 frame carries
        # one C&C write.  Each UDP datagram carries 2 USB blocks.  One
        # full cycle of 19 registers takes 19/2 = 9.5 datagrams ≈ 25 ms
        # at 380.95 Hz wire cadence -- same as Thetis.
        #
        # The Thetis 19-register cycle was first attempted in v0.0.9.6
        # round 11 (without the audio mixer thread); it destabilized
        # PC Soundcard feed (deque pegged at maxlen, ov=333956 in
        # ~30s).  Root cause was bursty wire cadence from the missing
        # audio mixer thread, NOT the register cycle itself.  With the
        # mixer thread + lockstep gate now in place (v0.0.9.6 round 13),
        # wire cadence is steady 380.95 Hz mirroring Thetis -- safe to
        # re-enable the full register schedule.
        #
        # ── ADDRESSING BUG FIX (round 11) ───────────────────────────
        # Pre-round-11 Lyra had two register entries: 0x00 (correct)
        # and "0x17" (WRONG -- the dict key was the C0 byte literal,
        # which on the wire decodes to register address 0x0b = HL2's
        # CW step-ATT/keyer config, NOT the TX-latency register).
        # TX latency lives at register address 0x17 = C0 byte 0x2e
        # (= 0x17 << 1 with PTT bit 0 clear), per Thetis case 17.
        # Lyra was poking the keyer config at 190 Hz with garbage --
        # any "cmd 0x17 reduced clicks" finding from earlier was
        # coincidence or Python-timing noise.  Fixed below.
        # ────────────────────────────────────────────────────────────
        #
        # Registers Lyra doesn't yet drive (TX freq, RX2 freq, mic,
        # drive level, CW config, EER, BPF2, reset-on-disconnect)
        # carry zeros -- sufficient for HL2 RX-only operation.  When
        # TX (v0.2) lands, those entries get populated with real
        # state via the existing setters (set_lna_gain_db, etc.)
        # plus new ones (set_tx_freq, set_drive_level, ...).
        #
        # 0x2e (TX latency) layout (HL2 register address 0x17):
        #   C3 = ptt_hang & 0x1f  (5-bit, max 31 ms)
        #   C4 = latency  & 0x7f  (7-bit, max 127 ms)
        # Gateware default: 10ms / 4ms.  We ship 40/12 for headroom
        # against Python-side jitter.
        # NOTE 2026-05-06 (round 15): the full 19-register Thetis
        # cycle is REVERTED back to a 2-register baseline because
        # multiple field tests (round 11 with mixer; rounds 14a/b/c
        # with mixer + lockstep) showed PC Sound feed-rate dropping
        # from clean (~100%) to ~95% (with periodic stumbling
        # underrun spikes) immediately on enabling the cycle.  The
        # destabilization isn't on Lyra's side -- it's on HL2's
        # gateware: writing the additional registers to addresses
        # that Lyra doesn't drive coherently appears to cause the
        # gateware to do *something* that interferes with EP6 IQ
        # delivery cadence, which the DSP worker then sees as fewer
        # IQ blocks per second, which surfaces as audio underrun
        # downstream.
        #
        # The 0x17 -> 0x2e address-bug fix is preserved (it's a real
        # bug regardless of the cycle change).  The rest of the
        # Thetis cycle is parked until we figure out which specific
        # register or value is destabilizing HL2.  Bisect candidates
        # for v0.0.9.7: probably 0x1c (ADC assignments -- HL2 has
        # only one ADC but the bit pattern matters), 0x14 (LNA C4
        # = 0x40 override-enable bit being cycled at 20 Hz), or
        # 0x16 (step-ATT enable bit 0x20 being cycled).
        self._cc_registers: dict[int, tuple[int, int, int, int]] = {
            0x00: (SAMPLE_RATES[sample_rate], 0x00, 0x00,
                   self._config_c4),                # general settings
            0x2e: (0, 0, 12 & 0x1F, 40 & 0x7F),     # TX latency (HL2 reg 0x17)
        }
        self._cc_cycle: tuple[int, ...] = (
            0x00,  # general
            0x2e,  # TX latency
        )
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
        self._tx_audio: deque = deque(maxlen=48000)
        self._tx_audio_lock = threading.Lock()
        # EP2 cadence throttle: increments every EP6 frame; keepalive
        # only fires when count % (sample_rate/48000) == 0.
        self._ep6_count = 0
        self.tx_audio_gain = 0.5
        # Opt-in: pack audio into EP2 frames. When False (default), the TX
        # audio slots are left at zero. Turn this on only for AK4951 output.
        self.inject_audio_tx = False
        # AK4951 sink diagnostics — each is a frame-level event counter.
        # Underrun: EP2 frame builder asked for N audio samples and the
        # tx_audio deque had fewer; we padded with zeros (= silent
        # samples = audible click on AK4951 codec).  Overrun: tx_audio
        # deque was already at maxlen=48000 when the producer pushed
        # more; deque silently dropped the OLDEST samples (= sample
        # discontinuity = audible click).  Both counters surfaced in
        # the UI status bar so operators can correlate counter ticks
        # with audible clicks.  v0.0.9.1+
        self.tx_audio_underruns: int = 0
        self.tx_audio_overruns: int = 0
        # ── EP2 writer thread state (v0.0.9.2 Commit 4) ─────────────
        # Dedicated EP2 writer thread runs the host->radio frame send
        # at the codec's audio cadence (~380 Hz = 48 kHz / 126
        # samples per frame), independent of UDP arrival cadence in
        # ``_rx_loop``.  This decouples EP2 send timing from UDP
        # burstiness, which is the root cause of AK4951 click/pop
        # symptoms (gateware FIFO underrun at the AK4951 codec when
        # EP2 frames arrive in irregular bursts).
        #
        # Sequence:
        #   1. ``_rx_loop`` sets ``_first_ep6_event`` when it receives
        #      its first valid EP6 datagram.  This avoids a startup
        #      race where the writer fires EP2 traffic before the
        #      gateware has finished its initialization handshake.
        #   2. Writer thread blocks on ``_first_ep6_event`` (with a
        #      5-second timeout for cases where the radio never
        #      streams).
        #   3. Writer enters its cadence loop, firing EP2 frames at
        #      ~380 Hz with C&C round-robin and (when
        #      ``inject_audio_tx`` is True) audio bytes drained from
        #      ``_tx_audio``.
        # On Windows, the writer thread elevates to MMCSS Pro Audio
        # scheduling priority for jitter immunity (best-effort; no-op
        # if API unavailable).
        self._first_ep6_event = threading.Event()
        self._ep2_writer_thread: Optional[threading.Thread] = None

        # ── EP2 producer-paced cadence (v0.0.9.2 Path C) ────────────
        # HPSDR P1 producer-paced cadence pattern.  Every time the
        # DSP worker pushes 126 audio samples into ``_tx_audio``,
        # ``queue_tx_audio`` releases ``_ep2_send_sem`` once.  The
        # EP2 writer loop blocks on this semaphore, so the writer's
        # wake-up cadence is locked to the DSP's *audio output
        # rate*, which in turn is locked to the EP6 input rate,
        # which is locked to the HL2's own codec crystal.  Result:
        # no PC-vs-HL2 clock drift, no producer overrun, no
        # consumer underrun.
        #
        # ``_unsignaled_audio_samples`` carries the < 126-sample
        # remainder between calls so a producer that pushes 500
        # samples in one call signals the writer 3 times (3 * 126 =
        # 378) and stashes the leftover 122 for next call.
        #
        # The semaphore is drained on ``clear_tx_audio`` /
        # ``fade_and_replace_tx_audio`` so a sink swap doesn't leave
        # stale signals that would cause the writer to spin against
        # an empty deque.
        self._ep2_send_sem = threading.Semaphore(0)
        self._unsignaled_audio_samples: int = 0
        # ── Audio mixer lockstep slot (v0.0.9.6 Thetis-mirror) ──────
        # When the AudioMixer thread (lyra/dsp/audio_mixer.py) is
        # paced by lockstep with the EP2 writer, the mixer's
        # outbound callback releases this slot semaphore at 0
        # (representing "no slot has been drained yet") and waits.
        # Each successful EP2 audio send releases the slot,
        # unblocking the mixer's NEXT outbound dispatch.  Net
        # effect: wire cadence becomes exactly 380.95 Hz steady,
        # mirroring Thetis WaitForSingleObject(hobbuffsRun[1])
        # at network.c:1322.  The slot is unused (always
        # available) when no mixer is connected (PC Sound or
        # NullSink modes); the writer just releases into a
        # semaphore counter no one is waiting on, which is a
        # no-op for any blocked-on-it caller and harmless for
        # the writer.
        self._lockstep_slot: threading.Semaphore = threading.Semaphore(0)
        # Deque high-water mark (v0.0.9.2 audio rebuild Commit 1).
        # Tracks the maximum deque depth observed since the last UI
        # read.  After v0.0.9.2 Commit 3 lands real backpressure,
        # the high-water should hover near the operator's chosen
        # block size; values approaching maxlen=48000 mean the
        # producer is far ahead of the consumer (overrun risk) and
        # values near zero mean the consumer is ahead of the
        # producer (underrun risk).  UI reads via
        # ``read_tx_audio_high_water()`` which atomically samples-
        # and-resets so we get rolling-window data.
        self.tx_audio_high_water: int = 0

    def read_tx_audio_high_water(self) -> int:
        """Atomically read + reset the TX audio deque high-water mark.

        Returns the maximum deque depth observed since the previous
        call.  UI's 1 Hz status tick reads this to drive a "deque
        depth: N" telemetry indicator during the v0.0.9.2 audio
        rebuild pre-release phase.  Lock-protected so the read +
        reset is atomic w.r.t. concurrent ``queue_tx_audio`` and
        ``_pack_audio_bytes`` updates.
        """
        with self._tx_audio_lock:
            hw = self.tx_audio_high_water
            self.tx_audio_high_water = len(self._tx_audio)
            return hw

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
        """Dequeue up to n_samples, pad with zeros, pack as HPSDR TX stereo.

        Queue items are (L, R) float tuples at 48 kHz. EP2 frames are
        rate-throttled at the call site so this function always sees
        a 48 kHz cadence — every slot carries a real sample.
        """
        import numpy as np
        with self._tx_audio_lock:
            avail = min(len(self._tx_audio), n_samples)
            pulled = [self._tx_audio.popleft() for _ in range(avail)]
        if avail < n_samples:
            # Underrun — TX queue had less data than EP2 wants to send.
            # Pad with zeros so the EP2 frame is always the right size,
            # but COUNT the event so the operator can see it in the
            # status bar.  Each underrun = silent samples injected into
            # the AK4951 audio stream = audible click on the codec
            # output.  Counter is read by the UI's 1 Hz status tick
            # (lyra/ui/app.py::_tick_cpu).  v0.0.9.1+
            self.tx_audio_underruns += 1
            pulled.extend([(0.0, 0.0)] * (n_samples - avail))
        # pulled is a list of (L, R) tuples — split into separate arrays.
        lr = np.asarray(pulled, dtype=np.float32)        # shape (N, 2)
        lr *= self.tx_audio_gain
        np.clip(lr, -1.0, 1.0, out=lr)
        # TPDF-dithered quantization to int16 (see module-level
        # _quantize_to_int16_be docstring for rationale).  Replaces
        # bare ``(lr * 32767.0).astype(">i2")`` so quiet AK4951 audio
        # doesn't pick up signal-correlated quantization grain.
        int16 = _quantize_to_int16_be(lr)                # shape (N, 2)
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
        with self._tx_audio_lock:
            # Detect overrun: when the deque is at maxlen and we
            # extend, the oldest elements are silently DROPPED.  Count
            # the would-be-dropped samples as overrun events so the
            # operator can see queue saturation in the status bar.
            # v0.0.9.1+ click investigation.
            # Track high-water mark BEFORE the extend so a producer
            # burst that arrives while the deque is full is captured
            # in the rolling-window observation.
            depth_after = len(self._tx_audio) + len(pairs)
            if depth_after > self.tx_audio_high_water:
                self.tx_audio_high_water = min(
                    depth_after, self._tx_audio.maxlen)
            free_slots = self._tx_audio.maxlen - len(self._tx_audio)
            if len(pairs) > free_slots:
                self.tx_audio_overruns += len(pairs) - free_slots
            self._tx_audio.extend(pairs)

            # Producer-paced EP2 cadence (Path C).  Release one
            # semaphore signal per 126-sample EP2 frame's worth of
            # audio that just became available.  Carry the < 126
            # remainder forward in ``_unsignaled_audio_samples`` so
            # bursty producers (DSP block of 512 samples = 4 EP2
            # frames + 8 leftover) signal the writer the right number
            # of times overall.  Both counters are protected by
            # ``_tx_audio_lock`` (already held here).
            self._unsignaled_audio_samples += len(pairs)
            n_signals = self._unsignaled_audio_samples // 126
            if n_signals > 0:
                self._unsignaled_audio_samples -= n_signals * 126
                for _ in range(n_signals):
                    self._ep2_send_sem.release()

    def clear_tx_audio(self):
        """Drain any pending samples from the TX audio queue. Called
        by AK4951Sink on init/close to prevent stale audio from a
        previous session leaking into a new session — the symptom
        was "digitized robotic" sound right after switching sinks."""
        with self._tx_audio_lock:
            self._tx_audio.clear()
            # Path C: also reset the producer-paced cadence state.
            # If we leave stale signals in the semaphore, the writer
            # would wake N extra times against an empty deque,
            # under-running every iteration until the signals drain.
            self._unsignaled_audio_samples = 0
            while self._ep2_send_sem.acquire(blocking=False):
                pass

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
        with self._tx_audio_lock:
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

            # Path C: reset producer-paced cadence state and re-signal
            # the writer for the fade tail.  Drain whatever stale
            # signals were sitting in the semaphore from the discarded
            # tail, then re-signal once per 126-sample chunk of the
            # fade itself.
            self._unsignaled_audio_samples = 0
            while self._ep2_send_sem.acquire(blocking=False):
                pass
            n_signals = fade_n // 126
            self._unsignaled_audio_samples = fade_n - n_signals * 126
            for _ in range(n_signals):
                self._ep2_send_sem.release()
            return fade_n

    def _send_cc(self, c0: int, c1: int, c2: int, c3: int, c4: int):
        """Send one C&C write via EP2. Thread-safe.

        Used for one-shot direct sends (e.g., initial config push,
        immediate setter response).  The writer thread also acquires
        ``_send_lock`` so the two never collide on the socket.

        Path C.2 (audio-pop fix): when ``inject_audio_tx`` is True,
        the legacy ``_build_ep2_frame`` path drains 126 audio
        samples from ``_tx_audio`` without consuming a semaphore
        signal -- the writer thread then under-runs on its next
        signal-driven drain and clicks the AK4951.  Skip the
        immediate emit in that case and rely on the writer's
        round-robin (which re-emits every register every few ms)
        to propagate the new value.  Always update
        ``_cc_registers`` so the round-robin sees the change.
        """
        if self._sock is None:
            return
        # Always update the register table so the writer thread's
        # round-robin picks up the new value on the next iteration.
        with self._cc_lock:
            self._cc_registers[c0] = (c1, c2, c3, c4)
        # Only do the immediate UDP emit when audio injection is
        # OFF (startup before any AK4951Sink is attached, or
        # PC Soundcard mode).  In that mode ``_build_ep2_frame``
        # doesn't drain audio so it's safe.
        if self.inject_audio_tx:
            return
        with self._send_lock:
            frame = self._build_ep2_frame(c0, c1, c2, c3, c4)
            self._sock.sendto(frame, (self.radio_ip, DISCOVERY_PORT))

    @staticmethod
    def _maybe_set_windows_timer_resolution(period_ms: int = 1):
        """Windows: bump the system timer resolution to ``period_ms``.

        Default Windows scheduler tick is ~15.6 ms (64 Hz).  Standard
        ``time.sleep()`` on Python honors that tick, meaning a request
        for ``sleep(0.0026)`` (2.6 ms) actually sleeps up to 15.6 ms.
        That's catastrophic for audio cadence -- a writer thread
        targeting 380 Hz can end up firing at 60-200 Hz, draining the
        EP2 audio queue too slowly and producing pulsing / garbled
        audio at the AK4951 codec.

        ``timeBeginPeriod(1)`` from winmm.dll requests the system to
        run at 1 ms tick, which lets ``time.sleep()`` honor sub-ms
        sleeps reliably.  This is the standard idiom for real-time-
        audio applications on Windows.

        Process-global; ``timeEndPeriod`` should be called at process
        exit but we don't bother -- Windows resets it when the process
        exits.  Idempotent if called multiple times.

        No-op (and silently absorbs any error) on non-Windows
        platforms or if winmm.dll is unavailable.  Failure is logged
        but non-fatal -- the writer just runs at the default coarser
        granularity, which is the pre-fix status quo.
        """
        try:
            import sys
            if not sys.platform.startswith("win"):
                return
            import ctypes
            winmm = ctypes.WinDLL("winmm", use_last_error=True)
            time_begin_period = winmm.timeBeginPeriod
            time_begin_period.restype = ctypes.c_uint
            time_begin_period.argtypes = [ctypes.c_uint]
            result = time_begin_period(int(period_ms))
            if result == 0:  # TIMERR_NOERROR
                # Use print so operator sees this regardless of
                # logging config -- one-shot startup diagnostic.
                print(f"[HL2Stream] Windows timer resolution "
                      f"bumped to {period_ms} ms")
            else:
                print(f"[HL2Stream] timeBeginPeriod({period_ms}) "
                      f"returned non-zero: {result} "
                      f"(timer resolution unchanged)")
        except Exception as e:  # noqa: BLE001
            print(f"[HL2Stream] Windows timer resolution "
                  f"bump failed: {e}")

    @staticmethod
    def _setup_win32_waitable_timer():
        """Create a Win32 native waitable timer for kernel-precision
        cadence in the EP2 writer thread.

        Returns a dict with ctypes function references and the timer
        handle, or ``None`` on non-Windows platforms or if any of
        the API calls fails.

        Why this exists: Python's ``time.sleep()`` plus busy-wait
        spin can't reliably hit 380 Hz on Windows because the GIL
        switch interval, scheduler granularity, and Python interpreter
        overhead all combine to cap the effective cadence around
        300-370 Hz.  A Win32 WaitableTimer + ``WaitForSingleObject``
        moves the wait into the kernel: the wait releases the GIL
        (so the worker thread can run during the wait), and the
        wake-up happens at exactly the timer's set time -- no
        Python-side scheduling jitter.

        Usage:
            ctx = HL2Stream._setup_win32_waitable_timer()
            if ctx is not None:
                due_time = ctypes.c_longlong(absolute_filetime_100ns)
                ctx['SetWaitableTimer'](
                    ctx['handle'], ctypes.byref(due_time), 0,
                    None, None, False)
                ctx['WaitForSingleObject'](ctx['handle'], 5000)
                # ... eventually:
                ctx['CloseHandle'](ctx['handle'])

        Lyra-native ctypes wrapper; not derived from any external
        codebase.  Mirrors a standard Windows real-time-audio idiom.
        """
        try:
            import sys
            if not sys.platform.startswith("win"):
                return None
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            CreateWaitableTimerW = kernel32.CreateWaitableTimerW
            CreateWaitableTimerW.restype = wintypes.HANDLE
            CreateWaitableTimerW.argtypes = [
                wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

            SetWaitableTimer = kernel32.SetWaitableTimer
            SetWaitableTimer.restype = wintypes.BOOL
            SetWaitableTimer.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_longlong),
                wintypes.LONG,
                wintypes.LPVOID, wintypes.LPVOID,
                wintypes.BOOL]

            WaitForSingleObject = kernel32.WaitForSingleObject
            WaitForSingleObject.restype = wintypes.DWORD
            WaitForSingleObject.argtypes = [
                wintypes.HANDLE, wintypes.DWORD]

            GetSystemTimePreciseAsFileTime = (
                kernel32.GetSystemTimePreciseAsFileTime)
            GetSystemTimePreciseAsFileTime.restype = None
            GetSystemTimePreciseAsFileTime.argtypes = [
                ctypes.POINTER(wintypes.FILETIME)]

            CloseHandle = kernel32.CloseHandle
            CloseHandle.restype = wintypes.BOOL
            CloseHandle.argtypes = [wintypes.HANDLE]

            # Auto-reset timer (signals once per fire, then resets).
            handle = CreateWaitableTimerW(None, False, None)
            if not handle:
                err = ctypes.get_last_error()
                print(f"[HL2Stream] CreateWaitableTimer failed "
                      f"(GetLastError={err}); falling back to "
                      f"time.sleep cadence")
                return None

            print(f"[HL2Stream] EP2 cadence using Win32 WaitableTimer "
                  f"(kernel-precision, GIL-released wait)")
            return {
                "handle": handle,
                "SetWaitableTimer": SetWaitableTimer,
                "WaitForSingleObject": WaitForSingleObject,
                "GetSystemTimePreciseAsFileTime":
                    GetSystemTimePreciseAsFileTime,
                "CloseHandle": CloseHandle,
                "FILETIME": wintypes.FILETIME,
            }
        except Exception as e:  # noqa: BLE001
            print(f"[HL2Stream] Win32 WaitableTimer setup failed: "
                  f"{e} (falling back to time.sleep cadence)")
            return None

    @staticmethod
    def _maybe_apply_mmcss_pro_audio(profile_name: str = "Pro Audio"):
        """Best-effort thread priority elevation on Windows via MMCSS.

        Audio writer threads benefit from priority elevation above
        the default user-thread class so UI / GC / generic background
        work can't starve the EP2 send cadence.  On Windows this is
        done by registering the thread with the Multimedia Class
        Scheduler Service via ``avrt.dll``.  No-op on other platforms
        and silently absorbs any error -- worst-case the thread runs
        at default priority, which is the pre-fix status quo.

        Lyra-native ctypes wrapper; not derived from any external
        codebase.  Mirrors a common Windows real-time-audio idiom.
        """
        try:
            import sys
            if not sys.platform.startswith("win"):
                return
            import ctypes
            avrt = ctypes.WinDLL("avrt", use_last_error=True)
            register = avrt.AvSetMmThreadCharacteristicsW
            register.restype = ctypes.c_void_p
            register.argtypes = [
                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
            set_priority = avrt.AvSetMmThreadPriority
            set_priority.restype = ctypes.c_int
            set_priority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            task_index = ctypes.c_ulong(0)
            handle = register(profile_name, ctypes.byref(task_index))
            if handle:
                # Priority constants: AVRT_PRIORITY_NORMAL=0,
                # _HIGH=1, _CRITICAL=2.  We want CRITICAL.
                set_priority(handle, 2)
                # Use print so operator sees this regardless of
                # logging config -- one-shot startup diagnostic.
                print(f"[HL2Stream] MMCSS '{profile_name}' priority "
                      f"elevated for thread "
                      f"{threading.current_thread().name}")
            else:
                print(f"[HL2Stream] MMCSS registration returned "
                      f"NULL; thread will run at default priority")
        except Exception as e:  # noqa: BLE001
            print(f"[HL2Stream] MMCSS priority elevation "
                  f"failed: {e}")

    def _ep2_writer_loop(self):
        """Dedicated thread that drives EP2 frame send cadence at the
        codec audio rate (~380 Hz = 48 kHz / 126 samples per frame).

        Cadence rationale: the host->radio audio path on the HL2+ is
        a networked codec -- audio samples arrive over UDP into the
        gateware's audio FIFO, which the AK4951 codec drains at a
        steady 48 kHz hardware clock.  Any UDP-arrival jitter on the
        host side translates to FIFO underrun events at the codec
        (audible as clicks, repeats, or motorboating depending on
        gateware behavior).  Pre-Commit-4 Lyra fired EP2 inline with
        EP6 receive in ``_rx_loop``, inheriting UDP burstiness.  This
        thread fires EP2 on a steady wall-clock timer instead, so
        UDP burstiness on the receive side no longer corrupts EP2
        send timing.

        Startup sequence:
          1. Wait for ``_first_ep6_event``.  This avoids a startup
             race where EP2 traffic arrives at the gateware before
             it has finished its EP6 initialization handshake (which
             can prevent the gateware from beginning to stream EP6).
             Bounded 5 s timeout so a never-streaming radio doesn't
             hang the thread forever.
          2. Best-effort MMCSS Pro Audio priority elevation on
             Windows (see ``_maybe_apply_mmcss_pro_audio``).
          3. Enter the cadence loop: every ~2.625 ms, drain one
             126-sample audio block from ``_tx_audio`` (zero-pad
             with underrun count if short), pick the next C&C
             round-robin entry, build a 1032-byte EP2 frame, send
             via UDP.  When ``inject_audio_tx`` is False (PortAudio
             sink active or pre-sink-init), the frame is C&C-only
             with zero audio bytes.

        Drift correction: ``next_fire`` accumulates the period and
        we sleep just long enough each iteration to land on it.  If
        the system suspends or the loop falls badly behind, we
        resync by setting ``next_fire = now + period`` rather than
        rapid-firing to catch up (which would burst-send EP2 and
        defeat the cadence purpose).
        """
        # Wait for gateware to begin streaming EP6.  Bounded.
        if not self._first_ep6_event.wait(timeout=5.0):
            _log.warning(
                "EP2 writer: no EP6 received within 5s; exiting "
                "without firing EP2 traffic")
            return

        # Bump Windows system timer resolution to 1 ms so
        # ``time.sleep()`` can honor sub-ms sleeps.  Critical for
        # audio cadence -- without this the writer fires at the
        # default 15.6 ms scheduler tick.  Process-global; safe
        # to call multiple times.
        self._maybe_set_windows_timer_resolution(1)

        # Elevate scheduling priority on Windows.  Best-effort.
        self._maybe_apply_mmcss_pro_audio("Pro Audio")

        # Reduce Python's GIL switch interval from the default 5 ms
        # to 1 ms.  Even with ``time.sleep()`` honoring 1 ms and the
        # writer thread at MMCSS Critical priority, after sleep
        # returns the writer needs the GIL to continue -- and the
        # worker thread (running DSP) only yields the GIL every
        # ``switchinterval`` seconds.  At 5 ms default, writer
        # wake-up was capped at ~130 Hz cadence, draining the EP2
        # queue too slowly and producing pulsing / garbled audio.
        # 1 ms switchinterval lets the writer reliably fire at
        # ~380 Hz.  Process-global side effect; minimal impact on
        # other threads at the cost of more frequent thread context
        # switches (CPython schedules these efficiently).
        try:
            import sys as _sys
            _sys.setswitchinterval(0.001)
            print(f"[HL2Stream] Python GIL switchinterval bumped "
                  f"to 1 ms")
        except Exception as e:  # noqa: BLE001
            print(f"[HL2Stream] setswitchinterval failed: {e}")

        # ── Path C: producer-paced cadence ─────────────────────────
        # HPSDR P1 producer-paced cadence pattern.  The writer blocks
        # on ``_ep2_send_sem`` which is released once per 126 audio
        # samples queued by the producer (DSP worker -> AK4951Sink ->
        # queue_tx_audio).  The DSP runs at exactly 48 kHz audio out
        # (locked to EP6 input rate, locked to HL2 codec crystal),
        # so the writer fires at exactly 380.95 Hz EP2 cadence -- in
        # phase with the HL2's own clock.  No PC clock involvement,
        # zero drift, zero possibility of producer overrun.
        #
        # The acquire() timeout is the C&C heartbeat fallback for the
        # case where audio is not being injected (e.g., PC Soundcard
        # mode) or DSP has stalled.  When audio is flowing the
        # semaphore signals at 380 Hz so the timeout never trips;
        # when audio stops, we still emit C&C-only frames at
        # ~100 Hz so register state changes (frequency, AGC, etc.)
        # propagate to the radio promptly.
        EP2_HEARTBEAT_TIMEOUT = 0.010  # 10 ms = sem.acquire timeout
        # Path C.3: keepalive fence.  When injecting audio, Path C.1
        # made us skip the heartbeat to avoid silence-frame insertion
        # during the normal ~11 ms inter-DSP-block gap.  But when
        # the producer stops for a longer stretch -- band change
        # triggers DSP reset (100-200 ms), or DSP worker stalls --
        # silently skipping kept the EP2 line completely quiet,
        # which violated the HL2's mandatory EP2-keepalive contract
        # and caused EP6 streaming to halt (operator-visible as
        # "display freezes on band change, only Stop/Start fixes").
        # Fix: only skip the heartbeat while we've sent a frame
        # recently.  Past EP2_KEEPALIVE_MAX_GAP since the last send,
        # let the heartbeat fire C&C-only frames so the HL2 stays
        # connected.  50 ms threshold = comfortably past one DSP
        # block (~11 ms) but well under any plausible HL2 keepalive
        # timeout (HL2 spec doesn't pin a number; field-tested
        # tolerance is at least hundreds of ms, but we don't want
        # to push it).
        EP2_KEEPALIVE_MAX_GAP = 0.050  # 50 ms
        # ── Cadence-gate experiment (round 12, REVERTED) ────────────
        # Tried adding a soft sleep gate inside the writer to enforce
        # a steady 2.625 ms inter-packet interval (mirroring Thetis's
        # WaitForSingleObject(hobbuffsRun) lockstep pattern).  Failed
        # because Windows time.sleep snaps to 1 ms granularity even
        # with timeBeginPeriod(1), so every requested 2.625 ms sleep
        # actually slept ~3 ms.  Wire cadence dropped to ~333 Hz =
        # only 42 kHz output sample rate -- AK4951 drains its FIFO
        # at 48 kHz so the codec output starved cyclically (audible
        # as "horrid pulsing"), AND the producer-side deque hit its
        # 48000 cap with ov=333,956 in ~10 s.  Operator confirmed
        # the symptom + metrics 2026-05-06.
        #
        # If we ever want a real cadence gate it would need either
        # spin-wait (CPU burn), a higher-resolution timer API
        # (CreateWaitableTimerEx with WAITABLE_TIMER_HIGH_RESOLUTION),
        # or a producer-side rewrite to lockstep (matching Thetis's
        # network.c:1287-1339 pattern -- aamix produces 126, signals,
        # blocks on hobbuffsRun until consumer sends, repeats).
        # Lockstep is the cleanest fix but it's a producer-pipeline
        # restructure.  Parked.
        print(f"[HL2Stream] EP2 cadence: producer-paced via "
              f"semaphore; "
              f"{EP2_HEARTBEAT_TIMEOUT*1000:.0f} ms heartbeat "
              f"timeout, {EP2_KEEPALIVE_MAX_GAP*1000:.0f} ms "
              f"keepalive fence")

        last_ep2_send_t = time.monotonic()
        try:
            while not self._stop_event.is_set():
                # ── Wait for audio-ready signal OR heartbeat tick ──
                signaled = self._ep2_send_sem.acquire(
                    timeout=EP2_HEARTBEAT_TIMEOUT)

                # ── Path C.1 / C.3: heartbeat handling under inject
                # When ``inject_audio_tx`` is True and the heartbeat
                # fired (no signal arrived within
                # EP2_HEARTBEAT_TIMEOUT), we have two cases:
                #
                # (a) Recent send (gap < KEEPALIVE_MAX_GAP):
                #     Normal DSP inter-block gap.  Skip the iteration
                #     -- firing a C&C-only frame here would insert
                #     2.625 ms of silence between real-audio frames
                #     and the AK4951 hears it as a click.  This is
                #     the original Path C.1 fix.
                #
                # (b) Long gap (>= KEEPALIVE_MAX_GAP):
                #     Producer is stalled (band change DSP reset,
                #     worker hiccup, etc.).  Fall through to the
                #     send branch and emit a C&C-only frame.  HL2
                #     needs EP2 keepalive to keep streaming EP6;
                #     the audio is already silent so a few zero-
                #     audio frames don't add a click.  This is the
                #     Path C.3 fix.
                #
                # When ``inject_audio_tx`` is False (PC Soundcard
                # mode), heartbeat always fires C&C-only frames --
                # there's no audio to click.
                if not signaled and self.inject_audio_tx:
                    if (time.monotonic() - last_ep2_send_t
                            < EP2_KEEPALIVE_MAX_GAP):
                        continue
                    # else: long gap, fall through to send keepalive

                # ── Drain audio + build + send EP2 frame ───────────
                # If we got an audio signal AND injection is enabled,
                # drain 126 samples and send an audio frame.
                # Otherwise (heartbeat-fired with injection disabled)
                # send a C&C-only frame so register state still
                # propagates to the radio.
                audio_bytes: Optional[bytes] = None
                if signaled and self.inject_audio_tx:
                    with self._tx_audio_lock:
                        avail = min(len(self._tx_audio), 126)
                        pulled = [self._tx_audio.popleft()
                                  for _ in range(avail)]
                    if avail < 126:
                        # Should be impossible under Path C/C.1/C.2
                        # semantics (semaphore signaled => >= 126
                        # samples were queued, no _send_cc drain
                        # path active during injection).  Count it
                        # as a diagnostic if it ever happens (e.g.,
                        # clear_tx_audio raced with a signal that
                        # wasn't drained, or a future code path
                        # introduces a regression).  The Path C.2
                        # diagnostic added in bc5713f -- which
                        # printed each event with a delta-timestamp
                        # so we could measure the underrun rate --
                        # is removed now that we're back to ov=0
                        # un=0 in steady state.  Restore it from
                        # commit bc5713f if a future regression
                        # ever puts un back on the meter.
                        self.tx_audio_underruns += 1
                        pulled.extend([(0.0, 0.0)] * (126 - avail))
                    try:
                        audio_bytes = self._pack_audio_bytes_pairs(
                            pulled)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "EP2 audio packing error: %s", exc)
                        audio_bytes = None

                # Pick next C&C round-robin entry, build the frame,
                # send it.  All wrapped in a single try/except so a
                # transient socket / build error does not kill the
                # writer thread; we just log and continue.
                try:
                    with self._cc_lock:
                        # Round-robin over current dict keys (sorted
                        # for deterministic order).  Round-9 baseline
                        # behaviour.  Setters like ``set_lna_gain_db``
                        # add entries dynamically (e.g. 0x14 for
                        # LNA) and this picks them up automatically.
                        if self._cc_registers:
                            keys = sorted(self._cc_registers.keys())
                            c0 = keys[self._cc_rr_idx % len(keys)]
                            c1, c2, c3, c4 = self._cc_registers[c0]
                            self._cc_rr_idx = (
                                self._cc_rr_idx + 1) % len(keys)
                        else:
                            c0 = c1 = c2 = c3 = 0
                            c4 = self._config_c4
                    frame = self._build_ep2_frame_with_audio(
                        c0, c1, c2, c3, c4, audio_bytes)
                    with self._send_lock:
                        if self._sock is not None:
                            self._sock.sendto(
                                frame,
                                (self.radio_ip, DISCOVERY_PORT))
                    # Path C.3: stamp the send so the keepalive
                    # fence above can measure gap-since-last-send.
                    last_ep2_send_t = time.monotonic()
                    # ── v0.0.9.6 Thetis-mirror lockstep ack ────────
                    # If this iteration drained audio (i.e., the
                    # AudioMixer pushed and is now waiting on
                    # _lockstep_slot), release the slot so the
                    # mixer can dispatch its NEXT outbound frame.
                    # Mirrors Thetis ReleaseSemaphore(hobbuffsRun
                    # [1], 1, 0) at WriteMainLoop_HL2 line 1200.
                    # Net effect: wire cadence becomes exactly
                    # 380.95 Hz, paced by HL2's own audio crystal
                    # via the mixer's blocking outbound.
                    #
                    # Released ONLY for audio-bearing frames
                    # (signaled + inject_audio_tx).  C&C-only
                    # heartbeat frames don't correspond to a
                    # mixer outbound dispatch, so don't release
                    # for those -- otherwise the slot count
                    # would drift up and the lockstep would
                    # stop being lockstep.
                    if signaled and self.inject_audio_tx:
                        self._lockstep_slot.release()
                except OSError:
                    # Socket likely closed during stop().  Exit
                    # cleanly.
                    break
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "EP2 writer iteration error: %s", exc)
        finally:
            # Drain any leftover semaphore signals so a subsequent
            # start() begins from a clean count.  No kernel resources
            # to release under Path C (Win32 WaitableTimer was
            # removed -- semaphore is a pure Python primitive).
            while self._ep2_send_sem.acquire(blocking=False):
                pass

    def _build_ep2_frame_with_audio(
        self, c0: int, c1: int, c2: int, c3: int, c4: int,
        audio_bytes: Optional[bytes],
    ) -> bytes:
        """Build an EP2 frame with optional pre-packed audio bytes.

        Wraps ``_build_ep2_frame`` (which calls ``_pack_audio_bytes``
        internally if ``inject_audio_tx`` is True).  This variant
        accepts already-packed audio bytes (1008 bytes = 126 samples
        x 8 bytes) and bypasses the inline packer -- needed by the
        EP2 writer thread which packs from a pre-pulled list of
        sample pairs rather than re-pulling from the deque inside
        the frame builder.

        When ``audio_bytes`` is None, the frame's audio slots stay
        zero (C&C-only frame).
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
            frame[block_off + 3] = c0 & 0xFE  # bit 0 = MOX (RX = 0)
            frame[block_off + 4] = c1 & 0xFF
            frame[block_off + 5] = c2 & 0xFF
            frame[block_off + 6] = c3 & 0xFF
            frame[block_off + 7] = c4 & 0xFF
            if audio_bytes is not None:
                slot_start = block_off + 8
                src = audio_bytes[
                    block_idx * 504:(block_idx + 1) * 504]
                frame[slot_start:slot_start + 504] = src
            # else: payload bytes stay zero (no audio injected)
        return bytes(frame)

    def _pack_audio_bytes_pairs(self, pairs) -> bytes:
        """Pack a list of 126 (L, R) float tuples into 1008 bytes
        of LRIQ-formatted audio bytes for the EP2 audio slots.

        Layout per HPSDR P1: 8 bytes per sample = L_msb L_lsb R_msb
        R_lsb I_msb I_lsb Q_msb Q_lsb (TX I/Q stays zero on RX).
        L/R values are quantized from float32 [-1, 1] to big-endian
        int16 with the configured ``tx_audio_gain`` applied.

        Implementation note: this runs once per EP2 frame (~381 Hz)
        on the writer thread.  A Python-level interleave loop costs
        ~70 us per call; the numpy ``out_arr`` approach below is
        ~5 us.  The savings matter because every microsecond not
        spent here is a microsecond the writer thread is asleep on
        the Win32 timer instead of holding the GIL.
        """
        # (126, 2) -> apply gain -> clip -> dither + quantize to BE int16
        lr = np.asarray(pairs, dtype=np.float32)
        lr = lr * self.tx_audio_gain
        np.clip(lr, -1.0, 1.0, out=lr)
        # TPDF-dithered quantization — eliminates the signal-correlated
        # quantization grain that operators perceive as "harshness" on
        # the AK4951 audio path.  See module-level _quantize_to_int16_be
        # docstring for the why.  ~10 us added per 126-sample frame.
        int16_be = _quantize_to_int16_be(lr)             # (126, 2)

        # Build the 8-byte slot directly as a (126, 4) BE-int16 view:
        # column 0 = L, column 1 = R, columns 2..3 = TX I/Q (stay 0).
        # Row-major .tobytes() gives the exact 1008-byte layout the
        # gateware expects with zero per-row Python work.
        out_arr = np.zeros((126, 4), dtype=">i2")
        out_arr[:, 0] = int16_be[:, 0]
        out_arr[:, 1] = int16_be[:, 1]
        return out_arr.tobytes()

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

        # Reset the first-EP6 gate before starting threads so a
        # restart on the same HL2Stream instance gets a clean
        # startup race (writer thread waits for the new first
        # EP6, not the previous run's flag).
        self._first_ep6_event.clear()

        self._thread = threading.Thread(
            target=self._rx_loop, args=(on_samples,), daemon=True,
            name="hl2-rx-loop",
        )
        self._thread.start()

        # Launch the dedicated EP2 writer thread (v0.0.9.2 Commit 4).
        # This thread owns the host->radio EP2 frame send cadence,
        # decoupling it from UDP arrival timing in _rx_loop.  The
        # writer waits on _first_ep6_event before firing any EP2
        # frames, so the gateware initialization handshake (which
        # involves _send_config below pushing the initial sample-
        # rate command and the radio sending back its first EP6
        # datagram) completes uninterrupted.
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
        # Path C.2 (audio-pop fix): NO direct _send_cc here.  The
        # legacy _send_cc -> _build_ep2_frame path drains 126 audio
        # samples from _tx_audio without consuming a semaphore
        # signal, which the writer thread then under-runs on its
        # next signal-driven drain (audible click on AK4951).  The
        # register-table update below is enough -- the EP2 writer
        # thread re-emits every register in round-robin within a
        # handful of milliseconds, so the new freq propagates to
        # the gateware imperceptibly fast.
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
        # Path C.2 (audio-pop fix): NO direct _send_cc here -- it
        # would drain 126 audio samples without consuming a
        # semaphore signal and click the AK4951.  See _set_rx1_freq
        # for the full rationale.
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
        # Path C.2 (audio-pop fix): NO direct _send_cc here -- it
        # would drain 126 audio samples without consuming a
        # semaphore signal and click the AK4951.  Auto-LNA fires
        # set_lna_gain_db roughly 1-2x per second under normal
        # band-noise variation, so this is the dominant pop source
        # we observed before the fix.  See _set_rx1_freq for full
        # rationale.
        with self._cc_lock:
            self._cc_registers[0x14] = (0, 0, 0, c4)
        self._keepalive_cc = (0x14, 0, 0, 0, c4)

    def stop(self):
        self._stop_event.set()
        # Wake the EP2 writer thread if it's blocked waiting for
        # first EP6 (e.g., radio never streamed).  Setting the
        # event here lets it observe _stop_event and exit cleanly
        # rather than waiting out its 5 s timeout.
        self._first_ep6_event.set()
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
            # Writer thread is timer-driven so it self-exits within
            # one cadence tick (~3 ms) of _stop_event being set.
            # Bounded join in case it's mid-sleep.
            self._ep2_writer_thread.join(timeout=1.0)
            self._ep2_writer_thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- internal -----------------------------------------------------------
    def _rx_loop(self, on_samples: Callable[[np.ndarray, FrameStats], None]):
        """Pure RX loop (v0.0.9.2 Commit 4).

        Reads EP6 datagrams, parses, decodes telemetry, dispatches
        samples.  Does NOT send EP2 frames -- that work moved to
        ``_ep2_writer_loop`` on its own thread to decouple EP2 send
        cadence from bursty UDP arrival timing.

        Sets ``_first_ep6_event`` on the first valid EP6 datagram
        so the writer thread knows the gateware has finished its
        initialization and is streaming.
        """
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

            # First valid EP6 received -- release the writer thread
            # to enter its cadence loop.  Idempotent (Event.set on
            # an already-set event is a no-op).
            if not self._first_ep6_event.is_set():
                self._first_ep6_event.set()

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
