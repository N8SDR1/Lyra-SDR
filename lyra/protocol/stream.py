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
        data: 504 bytes payload (see Phase 1 layout below)

Phase 1 (v0.1, 2026-05-11) — nddc=4 sample-set layout per
CLAUDE.md §3.3:
    504 bytes = 19 sample-slots × 26 bytes/slot (10 trailing
    bytes unused).  Per 26-byte slot:
        bytes 0..2:   DDC0 I (BE 24-bit signed)
        bytes 3..5:   DDC0 Q
        bytes 6..8:   DDC1 I
        bytes 9..11:  DDC1 Q
        bytes 12..14: DDC2 I (gateware-disabled on HL2: zeros)
        bytes 15..17: DDC2 Q (gateware-disabled on HL2: zeros)
        bytes 18..20: DDC3 I (gateware-disabled on HL2: zeros)
        bytes 21..23: DDC3 Q (gateware-disabled on HL2: zeros)
        bytes 24..25: mic sample (BE 16-bit signed)

Pre-Phase-1 v0.0.9.x ran at nddc=1 with a 504 / 8 = 63-slot
single-DDC layout (I=3, Q=3, mic=2).  The wire flip is governed
by ``_config_c4`` bit-field per CLAUDE.md §3.2: 0x04 = duplex +
nddc=1, 0x1C = duplex + nddc=4.  See ``_decode_iq_samples`` and
the ``HL2Stream.__init__`` C4 setup comment for the rationale.

C&C write register selectors (host -> radio in EP2):
    C0=0x00: general settings (C1 sample-rate code, C2 OC pins +
             CW EER, C3 BPF/ADC/RX-input routing, C4 antenna +
             duplex + nddc + diversity).  v0.2 Phase 1: composed
             from state via ``_compose_frame_0``.
    C0=0x02: TX VFO NCO freq (HL2 nddc=4 also mirrors TX freq to
             DDC2 and DDC3 -- see C0=0x08 and 0x0a).
    C0=0x04: RX1 NCO freq (DDC0)
    C0=0x06: RX2 NCO freq (DDC1).  Note: the HPSDR P1 "case 3"
             round-robin case-INDEX is sometimes mistaken for the
             C0 byte 0x03; the actual gateware-decoded C0 byte for
             RX2 NCO is 0x06 (sibling of RX1's 0x04).  See
             ``_set_rx2_freq`` for the verification trail.
    C0=0x08: DDC2 NCO freq -- HL2 nddc=4 carries TX freq here so
             v0.3 PureSignal feedback DDCs sit at TX freq when
             cntrl1=4 PA-coupler routing engages.
    C0=0x0a: DDC3 NCO freq -- always TX freq on HL2.
    C0=0x12: frame 10 -- drive_level, PA bias enable, mic/line-in
             routing, BPF/LPF filter-board selectors.  v0.2 Phase
             1: composed via ``_compose_frame_10``.
    C0=0x14: frame 11 -- preamps, mic switches, line-in gain,
             puresignal_run bit, user_dig_out pins, step
             attenuator (MOX-gated: TX value during transmit, RX
             value otherwise).  Composed via ``_compose_frame_11``.
    C0=0x1C: frame 4 -- ADC routing + 5-bit redundant
             tx_step_attn write.  Composed via ``_compose_frame_4``.
    C0=0x2e: frame 17 -- TX latency + PTT hang time (HL2 reg 0x17,
             per §15.7 latency-tuning work).
    C0=0x74: frame 18 -- reset_on_disconnect safety flag.  Default
             1 so a Lyra crash mid-TX doesn't leave silent carrier
             on air -- gateware auto-reverts to RX on host link loss.

C0 bit 0 (LSB) of EVERY emitted frame carries the operator's MOX
intent (sourced from ``dispatch_state.mox`` via the per-datagram
snapshot helper).  Same value applied to both USB blocks of each
UDP datagram for per-frame coherence.
"""
from __future__ import annotations

import ctypes
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import numpy as np

from lyra.radio_state import ConsumerID, DispatchState

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
    # v0.2 Phase 1 (9/10): HL2 status bits from ControlBytesIn.
    # PTT-in / dot-in / dash-in carry hardware-PTT and CW-key state
    # at bits 0-2 of C0 of every frame's C&C status block.  ADC
    # overload at bit 0 of C1 -- HL2 uses single-frame-assign
    # semantics (transient may vanish before the next frame), so
    # Lyra OR-until-cleared on the host side: the field stays True
    # once set until the consumer reads-and-clears.  All four
    # default False; consumers (Radio.tx_active state machine in
    # Phase 3, Radio's 1 Hz telemetry tick) flip the field via
    # the standard read-then-clear pattern.
    ptt_in: bool = False
    dot_in: bool = False
    dash_in: bool = False
    adc_overload: bool = False


def _build_start_stop_packet(flags: int) -> bytes:
    pkt = bytearray(64)
    pkt[0] = 0xEF
    pkt[1] = 0xFE
    pkt[2] = 0x04
    pkt[3] = flags & 0xFF
    return bytes(pkt)


# ── EP6 sample-set decoder (nddc=4 layout, Phase 1) ───────────────
#
# Per CLAUDE.md §3.3 EP6 receive frame layout at nddc=4:
#
#   Per UDP datagram: 2 × 512-byte USB frames.
#   Per USB frame:
#     bytes [0:3] = 0x7F 0x7F 0x7F sync
#     bytes [3:8] = C0..C4 (radio→host C&C: PTT, ADC overload,
#                   telemetry rotation, optional I2C readback)
#     bytes [8:512] = 504 bytes sample-set payload
#
# Per 26-byte slot (504 / 26 = 19 slots; trailing 10 bytes unused):
#   bytes 0..2:   DDC0 I (BE 24-bit signed)
#   bytes 3..5:   DDC0 Q
#   bytes 6..8:   DDC1 I
#   bytes 9..11:  DDC1 Q
#   bytes 12..14: DDC2 I
#   bytes 15..17: DDC2 Q
#   bytes 18..20: DDC3 I
#   bytes 21..23: DDC3 Q
#   bytes 24..25: mic sample (BE 16-bit signed)
#
# Each DDC therefore receives 19 samples per USB block × 2 blocks
# per UDP datagram = 38 complex samples per UDP per DDC.  This is
# ~3.3× fewer than the pre-Phase-1 nddc=1 path (which delivered
# 126 per datagram per DDC), so at a fixed wire IQ rate the
# datagram rate goes up correspondingly -- 5053 datagrams/sec at
# 192 kHz nddc=4 vs 1524 at 192 kHz nddc=1.  Downstream consumers
# in radio.py accumulate samples into batches, so the smaller
# per-callback chunks are transparent to them.

_NDDC = 4
_SLOTS_PER_BLOCK = 19
_SLOT_STRIDE = 26    # bytes per slot at nddc=4 (= 6 * nddc + 2)
_MIC_OFFSET = 24     # byte offset of mic sample within slot (= 8 * nddc - 8)
_USED_SLOT_BYTES = _SLOTS_PER_BLOCK * _SLOT_STRIDE  # = 494 of the 504 payload bytes
_IQ_SCALE = np.float32(1.0 / (1 << 23))


def _decode_iq_samples(block_data: bytes) -> Dict[int, np.ndarray]:
    """Decode one 504-byte EP6 sample-set block into per-DDC IQ + mic.

    Phase 1 (v0.1) — nddc=4 layout per CLAUDE.md §3.3 and plan §4.2.

    Returns a dict shaped::

        {
            0: ndarray((19,), dtype=complex64),    # DDC0 IQ
            1: ndarray((19,), dtype=complex64),    # DDC1 IQ
            2: ndarray((19,), dtype=complex64),    # DDC2 IQ (gateware-disabled on HL2 -> zeros)
            3: ndarray((19,), dtype=complex64),    # DDC3 IQ (gateware-disabled on HL2 -> zeros)
            "mic": ndarray((19,), dtype=int16),    # mic samples (sign-extended)
        }

    Performance: fully vectorized across all 4 DDCs and both I/Q
    streams in a single shot.  Per-UDP nddc=4 EP6 cadence at 192
    kHz wire rate is ~5053 datagrams/sec (vs ~1524 at nddc=1), so
    each per-datagram microsecond matters -- the initial Phase 1
    parser iterated per-DDC and triggered audio underruns from
    per-numpy-op overhead on (19,)-shape arrays.  The optimized
    path uses ~6 numpy ops total instead of ~28 (~4× speedup on
    small arrays where per-op overhead dominates), eliminating
    that bottleneck.

    Memory layout the optimization exploits: each 26-byte slot is
    8 consecutive 3-byte BE triplets (DDC0_I, DDC0_Q, DDC1_I,
    DDC1_Q, DDC2_I, DDC2_Q, DDC3_I, DDC3_Q) followed by 2 mic
    bytes.  Reshaping the 19×24 IQ bytes as ``(19, 8, 3)`` lets
    one bitshift+OR sequence build all 152 24-bit raw integers
    at once, then a single ``np.where`` sign-extends, a single
    multiply scales, and slicing splits into per-DDC views.

    Note ``block_data`` is the 504-byte payload AFTER the 8-byte
    USB-block header (sync + C0..C4).  Caller is responsible for
    stripping the header before calling this decoder.

    Args:
        block_data: 504-byte payload of one USB block within an
            EP6 UDP datagram.  Must be exactly 504 bytes or longer
            (only the first 494 = 19*26 are read; trailing 10
            unused bytes are ignored to match the wire format).

    Returns:
        Dict mapping DDC index (0..3) to a (19,) complex64 ndarray
        with samples normalized to [-1, 1), plus ``"mic"`` -> (19,)
        int16 mic samples (sign-extended from BE 16-bit on the wire).

    Raises:
        ValueError: If ``block_data`` is shorter than 494 bytes.
    """
    if len(block_data) < _USED_SLOT_BYTES:
        raise ValueError(
            f"_decode_iq_samples needs >= {_USED_SLOT_BYTES} bytes "
            f"(19 slots * 26 bytes/slot at nddc=4); got {len(block_data)}"
        )
    arr = np.frombuffer(
        block_data[:_USED_SLOT_BYTES], dtype=np.uint8
    ).reshape(_SLOTS_PER_BLOCK, _SLOT_STRIDE)

    # ── Vectorized 24-bit BE decode across all 8 IQ triplets ──
    # Bytes [0:24] of each slot are 8 × 3-byte triplets, one per
    # DDC I/Q channel in order (D0I, D0Q, D1I, D1Q, D2I, D2Q, D3I,
    # D3Q).  Reshape to (slots, 8, 3) so each axis-2 triple is one
    # BE 24-bit sample.
    iq_bytes = arr[:, :_NDDC * 6].reshape(
        _SLOTS_PER_BLOCK, _NDDC * 2, 3,
    )
    # Build 24-bit unsigned ints via 3 bitshifts + ORs.  One op per
    # byte position covers all 152 samples in the block at once.
    raw = (
        (iq_bytes[:, :, 0].astype(np.int32) << 16)
        | (iq_bytes[:, :, 1].astype(np.int32) << 8)
        | iq_bytes[:, :, 2].astype(np.int32)
    )
    # Sign-extend 24-bit → 32-bit signed.  ``raw`` shape (19, 8).
    raw = np.where(raw & 0x800000, raw - 0x1000000, raw)
    # Scale to float [-1, 1).  Single multiply across (19, 8).
    scaled = raw.astype(np.float32) * _IQ_SCALE

    # Split into per-DDC complex arrays.  ``scaled[:, 2*ddc]`` is
    # the DDC's I channel and ``scaled[:, 2*ddc + 1]`` is Q.
    # Building complex64 directly via empty + view is slightly
    # faster than ``i + 1j*q`` (which allocates an intermediate
    # complex128 from the Python ``1j`` scalar then downcasts).
    out: Dict[int, np.ndarray] = {}
    for ddc in range(_NDDC):
        complex_arr = np.empty(_SLOTS_PER_BLOCK, dtype=np.complex64)
        complex_view = complex_arr.view(np.float32).reshape(
            _SLOTS_PER_BLOCK, 2,
        )
        complex_view[:, 0] = scaled[:, ddc * 2]      # I
        complex_view[:, 1] = scaled[:, ddc * 2 + 1]  # Q
        out[ddc] = complex_arr

    # Mic: bytes 24..25 of each slot, BE int16 signed.  View as
    # big-endian int16 via dtype reinterpretation, then cast to
    # native int16 so downstream consumers don't have to deal with
    # byte-order metadata on a tiny (19,) array.
    mic_slice = arr[:, _MIC_OFFSET:_MIC_OFFSET + 2].tobytes()
    mic_be = np.frombuffer(mic_slice, dtype=">i2")  # signed, big-endian
    out["mic"] = mic_be.astype(np.int16)

    return out


def twist(samples_a: np.ndarray, samples_b: np.ndarray) -> np.ndarray:
    """Interleave two per-DDC complex streams into a 4-channel
    real-valued buffer.

    Phase 1 helper matching Thetis ``twist()`` semantics at
    ``ChannelMaster\\networkproto1.c:263-274``.  Used by the
    dispatcher when a host channel consumes a *pair* of DDCs as
    one logical 4-channel I/Q stream — diversity reception on
    ANAN, or PS feedback on ANAN P1 5-DDC (HL2 PS feedback uses
    DDC0/DDC1 sync-paired and does NOT route through twist per
    CLAUDE.md §3.8 corrected entry; the gateware delivers I on
    DDC0 and Q on DDC1 directly with cntrl1=4 routing).

    On HL2 RX-only and HL2 MOX-no-PS states the dispatch table in
    ``ddc_map(state)`` doesn't reference any twist target (DDC2/
    DDC3 carry zeros and are always DISCARD), so this helper is
    inert for HL2 v0.1.  It's defined now to match the plan §4.2
    Phase 1 deliverable surface and to be ready for v0.4 ANAN
    multi-radio work without another protocol-layer edit.

    Output shape ``(N, 4)`` float32 with column layout
    ``[I_a, Q_a, I_b, Q_b]`` -- matches Thetis xrouter source
    semantics for 4-channel destinations.

    Args:
        samples_a: First DDC's complex samples, shape ``(N,)``.
        samples_b: Second DDC's complex samples, shape ``(N,)``.
            Must match ``samples_a`` length.

    Returns:
        ``(N, 4)`` float32 ndarray with columns
        ``[I_a, Q_a, I_b, Q_b]``.

    Raises:
        ValueError: If sample-array lengths differ.
    """
    if samples_a.shape[0] != samples_b.shape[0]:
        raise ValueError(
            f"twist: length mismatch a={samples_a.shape[0]} "
            f"b={samples_b.shape[0]}"
        )
    n = samples_a.shape[0]
    out = np.empty((n, 4), dtype=np.float32)
    out[:, 0] = samples_a.real
    out[:, 1] = samples_a.imag
    out[:, 2] = samples_b.real
    out[:, 3] = samples_b.imag
    return out


def _parse_iq_frame(
    data: bytes,
) -> Optional[tuple[int, Dict[int, np.ndarray], np.ndarray, bytes, bytes]]:
    """Parse one EP6 UDP datagram into per-DDC IQ + mic + telemetry.

    Phase 1 (v0.1) — nddc=4 layout.

    Returns:
        ``(seq, per_ddc, mic, cc_block0, cc_block1)`` or ``None``
        if the datagram is malformed.

        * ``seq``: 32-bit sequence number from the HPSDR P1 frame
          header.
        * ``per_ddc``: Dict mapping DDC index (0..3) to a (38,)
          complex64 ndarray (= 19 samples per USB block × 2
          blocks per UDP datagram, concatenated).
        * ``mic``: (38,) int16 mic samples (concatenated from the
          two USB blocks).
        * ``cc_block0`` / ``cc_block1``: 5-byte slices (C0..C4)
          for each USB block.  C0 carries the telemetry address
          in bits[7:3] and live state flags in bits[2:0] for
          HPSDR P1 EP6 frames.
    """
    if len(data) != 1032:
        return None
    if data[0] != 0xEF or data[1] != 0xFE or data[2] != 0x01 or data[3] != 0x06:
        return None
    seq = struct.unpack(">I", data[4:8])[0]

    blocks = (data[8:520], data[520:1032])
    cc_parts: list[bytes] = []
    per_ddc_parts: Dict[int, list[np.ndarray]] = {0: [], 1: [], 2: [], 3: []}
    mic_parts: list[np.ndarray] = []
    for b in blocks:
        if b[0] != 0x7F or b[1] != 0x7F or b[2] != 0x7F:
            return None
        cc_parts.append(bytes(b[3:8]))
        decoded = _decode_iq_samples(b[8:])
        for ddc in range(_NDDC):
            per_ddc_parts[ddc].append(decoded[ddc])
        mic_parts.append(decoded["mic"])

    per_ddc: Dict[int, np.ndarray] = {
        ddc: np.concatenate(per_ddc_parts[ddc]) for ddc in range(_NDDC)
    }
    mic = np.concatenate(mic_parts)
    return seq, per_ddc, mic, cc_parts[0], cc_parts[1]


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
    # v0.2 Phase 1 (9/10): I2C-response gate.  When bit 7 of C0 is
    # set, the 5-byte block is an I2C-readback response from the
    # HL2 expansion-header I2C bus, NOT the telemetry rotation.
    # Low bits of C0 in that case carry I2C device-address / data
    # bits rather than the PTT/dot/dash/address-rotation fields.
    # Bail before any field decode to avoid mis-interpreting I2C
    # bytes as telemetry values.
    #
    # Lyra doesn't currently initiate I2C reads (no expansion-board
    # support in v0.1/v0.2 RX-only scope), so this guard fires
    # rarely.  Becomes load-bearing when v0.4+ adds N2ADR filter
    # board / external attenuator board / other I2C-attached
    # peripherals.
    if cc[0] & 0x80:
        return
    # v0.2 Phase 1 (9/10): always-present status bits at bits 0-2 of
    # C0.  These fields refresh every frame (not address-rotation-
    # gated like fwd_pwr_adc / temp_adc / etc.).
    stats.ptt_in  = bool(cc[0] & 0x01)
    stats.dot_in  = bool(cc[0] & 0x02)
    stats.dash_in = bool(cc[0] & 0x04)
    # v0.2 Phase 1 (9/10): ADC-overload bit at bit 0 of C1.  HL2
    # uses single-frame-assign semantics (the bit clears in the
    # gateware as soon as the overload sample passes); without
    # OR-until-cleared on the host side, a transient overload that
    # exists for just one frame would vanish before the 1 Hz UI
    # tick reads it.  Lyra's consumer (Radio.tx_active state
    # machine + UI ADC-LED indicator) reads-and-clears -- the
    # field stays True from the moment of overload until the
    # consumer acknowledges.
    if cc[1] & 0x01:
        stats.adc_overload = True
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
        # independent RX/TX frequency control. C4[6:3] = NDDC - 1 (4-bit
        # field per CLAUDE.md §3.2 / IM-1, nddc-1 ranges 0..15).
        #
        # Phase 1 v0.1 (2026-05-11) — nddc=4 flip per consensus plan §4.1:
        #
        # * Main-loop value 0x1C = (nddc-1)<<3 | duplex = 0x18 | 0x04.
        #   Used in the C&C register table (``_cc_registers[0x00]``)
        #   so the EP2 writer's round-robin emits 0x1C on every
        #   general-settings frame.  Per CLAUDE.md §3.2: "the duplex
        #   bit is added in the main-loop case-0 path (``C4 |= 0x04``
        #   at networkproto1.c:967) AFTER priming completes."
        #
        # * Priming value 0x18 = (nddc-1)<<3, NO duplex bit.  Used
        #   only by ``_send_config`` for the initial one-shot UDP
        #   emission that triggers gateware to begin streaming EP6
        #   at the requested rate + DDC count.  Per CLAUDE.md §3.2
        #   "the priming function ``ForceCandCFrames``... emits
        #   priming with the hard-coded layout C4 = (nddc-1) << 3 =
        #   0x18 (no duplex bit)."  Gateware accepts priming VFO
        #   writes regardless of the duplex bit, but matching
        #   Thetis exactly is what plan §4.4 step 6 wireshark
        #   bench-test gate checks.
        #
        # Pre-Phase-1 (v0.0.9.x) value was 0x04 (NDDC=1 + duplex).
        # Switching to nddc=4 changes the EP6 sample-set stride
        # from 8 bytes/slot (single DDC) to 26 bytes/slot (four
        # DDCs interleaved + mic) — see ``_decode_iq_samples`` and
        # CLAUDE.md §3.3 for the new layout.  Datagram rate at the
        # same wire IQ rate goes up ~3.3x (each datagram carries 38
        # samples per DDC instead of 126); the EP6 parser handles
        # the higher cadence transparently.
        self._priming_c4 = 0x18  # priming: nddc=4, no duplex bit
        self._config_c4 = 0x1C   # main-loop: nddc=4 + duplex bit
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
        # ── §15.7 latency tune-down hook (2026-05-13) ─────────────────
        # HL2 TX-latency register (gateware 0x17) defaults to 40 ms.
        # That value was chosen during the v0.0.9.6 audio quiet-pass
        # to give Python/Qt scheduling jitter plenty of headroom.
        # Post-v0.0.9.6 we have MMCSS Pro Audio + 1 ms timer + GIL
        # switchinterval + producer-paced EP2 cadence — the original
        # 40 ms margin is likely larger than needed.
        #
        # Operator-tunable via ``LYRA_HL2_TXLATENCY_MS=N`` env var for
        # bench experimentation.  Range-clamped to 5..127 (gateware
        # is a 7-bit field; 5 is the safe floor before audio jitter
        # could cause underruns).  Default 15 = post-§15.7 production
        # floor (validated 2026-05-13 on RX path; HL2 jack stable
        # with same brief startup hiccup as 25 ms).  Was 40 ms
        # pre-§15.7 — set LYRA_HL2_TXLATENCY_MS=40 to revert.
        # Other HPSDR clients (Thetis, EESDR3) typically run this
        # register at ~10 ms because C/C++ doesn't have Python's
        # scheduling overhead.  TX-side validation pending TX bring-up.
        import os as _os_txlat
        _txlat_raw = _os_txlat.environ.get(
            "LYRA_HL2_TXLATENCY_MS", "").strip()
        if _txlat_raw:
            try:
                self._tx_latency_ms = max(5, min(127, int(_txlat_raw)))
                print(f"[HL2Stream] §15.7 override: "
                      f"TX-latency register = {self._tx_latency_ms} ms "
                      f"(LYRA_HL2_TXLATENCY_MS; default 15)")
            except (TypeError, ValueError):
                self._tx_latency_ms = 15
        else:
            self._tx_latency_ms = 15
        # ── C&C round-robin (v0.2 Phase 1 refactor) ─────────────────
        # Authoritative cycle is ``_cc_cycle`` (an ordered list of
        # C0-byte register addresses).  The writer at
        # ``_ep2_writer_loop`` walks this list using ``_cc_rr_idx``
        # to pick the next entry per UDP datagram.
        #
        # Why this replaces the v0.1 "sorted(_cc_registers.keys())"
        # pattern: it lets Phase 1 add new registers in HPSDR P1
        # case-N ordering AND it unblocks the MOX-edge jump-to-
        # frame-2 retune behaviour (forces a DDC0 freq update on
        # every TX/RX state edge -- impossible to express under a
        # sorted-keys scheme since there's no notion of "case
        # index" to jump to).  Phase 1 keeps the cycle list
        # populated lazily (setters call ``_register_cc_slot`` to
        # add a new C0 the first time they write it); future phases
        # can wire the MOX-edge jump-index behaviour by inspecting
        # ``_cc_cycle`` ordering.
        self._cc_registers: dict[int, tuple[int, int, int, int]] = {
            0x00: (SAMPLE_RATES[sample_rate], 0x00, 0x00,
                   self._config_c4),                # general settings
            0x2e: (0, 0, 12 & 0x1F,                 # TX latency (HL2 reg 0x17)
                   self._tx_latency_ms & 0x7F),
        }
        # Mutable list (was a tuple in v0.1 — never actually read by
        # the writer; Agent L Round 3 finding 2026-05-14).  Phase 1
        # setters extend this via ``_register_cc_slot``.
        self._cc_cycle: list[int] = [
            0x00,  # general settings (always first in the cycle)
            0x2e,  # frame 17 — TX latency + ptt_hang (HL2 reg 0x17)
        ]
        self._cc_rr_idx: int = 0
        self._cc_lock = threading.Lock()
        self._send_lock = threading.Lock()

        # ── Phase 1: per-DDC consumer registration (v0.1) ───────────
        #
        # Each EP6 datagram parsed by ``_decode_iq_samples`` produces
        # per-DDC IQ ndarrays (DDC0..DDC3 + mic).  The dispatcher
        # ``dispatch_ddc_samples`` reads a ``DispatchState`` snapshot
        # via ``_dispatch_state_provider``, calls
        # ``lyra.protocol.ddc_map(state)`` to get the per-DDC →
        # ``ConsumerID`` routing for the current state, and fans out
        # to the consumer callback registered at each ``ConsumerID``
        # slot.
        #
        # Consumers are registered via ``HL2Stream.register_consumer``
        # (one-shot, before ``start()``) or via the ``on_samples`` /
        # ``on_rx2_samples`` kwargs to ``start()`` (Phase 1 back-compat:
        # they map to ``RX_AUDIO_CH0`` and ``RX_AUDIO_CH2`` slots).
        # Per CLAUDE.md §6.7 discipline #6: this routing table is the
        # ONE place ``if ddc_idx == N:`` logic is correct.  All other
        # call sites read ``radio.protocol.ddc_map(state)`` so the
        # family-specific routing is a single source of truth.
        #
        # The ``DISCARD`` slot has no live callback in Phase 1; the
        # dispatcher skips it (treats as no-op).  Same for any slot
        # with ``None`` callback -- a "drop on the floor" sentinel
        # that's deliberately legal for Phase 1's "wire dispatched
        # but consumer not yet implemented" state.
        self._consumers: Dict[ConsumerID, Optional[Callable[..., None]]] = {
            cid: None for cid in ConsumerID
        }
        self._dispatch_state_provider: Optional[
            Callable[[], DispatchState]
        ] = None

        # v0.2 Phase 2 (3/N): mic-input callback.  EP6 datagrams carry
        # mic samples in the 24..25 byte slot of each 26-byte sample
        # slot (per CLAUDE.md §3.3).  ``_parse_iq_frame`` extracts the
        # full mic block (38 int16 samples per datagram at nddc=4) and
        # returns it as the third tuple element.  Phase 0 / Phase 1
        # discarded those samples; Phase 2 plumbs them to the TX path:
        #
        #   HL2Stream._rx_loop  (this thread)
        #     -> mic_callback(int16 ndarray, FrameStats)
        #     -> Radio.dispatch_tx  (via QueuedConnection: DSP worker)
        #     -> TxChannel.process(float32 mono)
        #     -> queue_tx_iq(complex64)
        #     -> HL2Stream EP2 writer (Thread 4)
        #     -> EP2 frame TX I/Q bytes (commit 4 wires this end)
        #
        # Callback receives the raw mic samples as int16 BE-decoded
        # ndarray + the current FrameStats so consumers can read
        # ``ptt_in`` / ``adc_overload`` for the same datagram in one
        # callback rather than racing through a separate signal.
        # Consumers MUST treat the callback as RX-loop-thread context
        # and hand off to DSP-worker-thread via QueuedConnection if
        # they do any non-trivial processing.
        self._mic_callback: Optional[
            Callable[[np.ndarray, "FrameStats"], None]
        ] = None

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

        # ── v0.2 Phase 2 commit 8: TX I/Q queue (EP2 bytes 4..7 per slot) ──
        # Sibling of _tx_audio above.  Complex64 samples at 48 kHz; EP2
        # frames consume 126 samples per cadence tick (~381 Hz).  Producer
        # is Radio.dispatch_tx → TxChannel.process → queue_tx_iq, currently
        # gated behind LYRA_ENABLE_TX_DISPATCH=1 (see Phase 2 commit 7.1).
        #
        # The consumer side (_pack_audio_bytes_pairs columns 2..3 packing)
        # is wired even when no producer feeds the queue -- when the queue
        # is empty AND inject_tx_iq is False (default), columns stay zero
        # so wire-level RX-only behavior is byte-identical to v0.1.  Phase
        # 2 commit 7-redo flips inject_tx_iq=True on PTT edges.
        #
        # maxlen=48000 = ~1 sec at 48 kHz, same cap as _tx_audio.
        self._tx_iq: deque = deque(maxlen=48000)
        self._tx_iq_lock = threading.Lock()
        self.tx_iq_gain: float = 1.0
        # Opt-in: pack TX I/Q into EP2 frame columns 2..3.  When False
        # (default), columns stay zero regardless of queue contents.
        # Flipped True on MOX=1 edge by Phase 2 commit 7-redo / Phase 3
        # PTT state machine.
        self.inject_tx_iq: bool = False
        # Diagnostics counters (mirror tx_audio_*).  Underrun: EP2 wanted
        # 126 IQ samples and the queue had fewer; padded with zeros (=
        # baseband silence on the TX I/Q wire, harmless during RX since
        # MOX=0, audible spectral glitch during TX).  Overrun: producer
        # pushed faster than EP2 cadence drains; deque silently dropped
        # the OLDEST samples.  Both counters surface in the UI status bar
        # once Phase 3 wires the readout.
        self.tx_iq_underruns: int = 0
        self.tx_iq_overruns: int = 0
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

        # ── v0.2 TX bring-up Phase 0 scaffolding ────────────────────
        # TX center frequency in Hz for DDC2/DDC3 NCO writes (C0=0x02,
        # 0x08, 0x0a per HPSDR P1 networkproto1.c:949-1001).  Phase 0
        # stores the attribute only -- no setter, no wire emission.
        # Phase 1 adds ``_set_tx_freq(hz)`` that pushes the value
        # through the three C&C registers above.  None means "operator
        # has not set a TX freq yet -- use VFO A (= self._rx1_freq_hz)
        # as the default" during Phase 1 wiring; Lyra UI later wires
        # the SPLIT toggle to drive this independently of VFO A.
        self._tx_freq_hz: Optional[int] = None
        # PureSignal-run flag for C&C frames 11 + 16 (bit 6 of C2 per
        # CLAUDE.md §3.7).  Phase 0 stores the attribute only; the
        # C&C frame builder still emits the bit as zero on the wire.
        # v0.3 PureSignal work wires the setter + the frame emission +
        # the gateware cntrl1=4 PA-coupler routing per §3.8.  Phase 0
        # initialization here ensures v0.2/v0.3 consumers can read
        # the flag without an AttributeError.
        self._puresignal_run: bool = False

        # ── HPSDR P1 frame 11 (register 0x14) state (v0.2 Phase 1) ──
        # Frame 11 carries six axes of HL2 hardware state in one
        # register, all four bytes meaningful:
        #
        # * C1: rx0/rx1/rx2 preamp bits + mic-switch bits
        #       (mic_bias/mic_ptt/mic_trs)
        # * C2: line_in_gain (5-bit) | (puresignal_run bit 6)
        # * C3: user_dig_out (4-bit, drives HL2 expansion-header digital
        #       output pins)
        # * C4: step attenuator -- TX value when MOX asserted, RX value
        #       otherwise.  Encoded as (db + 12) & 0x3F with override
        #       bit 6 set.  HL2 range -12..+48 dB for RX, -28..+31 dB
        #       for TX (per capabilities.tx_attenuator_range).
        #
        # v0.1 wrote only C4 from set_lna_gain_db and left C1/C2/C3
        # at zero.  That worked "by accident" because the operator's
        # other axes (preamps, mic switches, line-in gain, dig-out)
        # also defaulted to zero.  v0.2 Phase 1 refactors frame 11
        # to compose all four bytes from state via _compose_frame_11()
        # so subsequent Phase 1 commits (drive level, TX step-attn,
        # MOX bit emission) don't trample these axes when they write
        # the same register.
        #
        # All defaults zero/False -- matches HL2 fresh-power-up state
        # and preserves v0.1 wire behaviour for fresh installs.
        self._rx_preamp_bits: int = 0        # 3-bit: rx0/rx1/rx2 preamps
        self._mic_bias_enabled: bool = False
        self._mic_ptt_enabled: bool = False
        self._mic_trs_enabled: bool = False
        self._line_in_gain: int = 0          # 5-bit, 0..31
        self._user_dig_out: int = 0          # 4-bit, 0..15
        self._rx_step_attn_db: int = 0       # operator-tunable via
                                             # set_lna_gain_db (Lyra calls
                                             # this "LNA gain" historically)
        self._tx_step_attn_db: int = 0       # operator-tunable in v0.2.x
                                             # via drive-power slider

        # ── HPSDR P1 frame 10 (register 0x12) state (v0.2 Phase 1) ──
        # Frame 10 carries PA drive + analog routing + filter-board
        # selectors:
        #
        # * C1: drive_level (8-bit, 0..255) -- primary TX-power-slider
        #       wire surface
        # * C2: mic_boost bit 0 | line_in_route bit 1 | 0x40 constant
        #       (HL2 carries the 0x40 bit unconditionally; it was an
        #       Apollo-board flag on legacy hardware that HL2 inherits)
        # * C3: bpf_filter_bits (7-bit BPF selector, bits 0-6) |
        #       (pa_on << 7) -- PA bias enable lives at bit 7 here,
        #       NOT in its own register
        # * C4: lpf_filter_bits (8-bit LPF selector)
        #
        # v0.2 Phase 1 defaults are all zero/False -- meaning fresh
        # install ships with PA bias OFF (pa_on=False).  This is
        # deliberate: even when MOX bit emission lands in Phase 1
        # item 8, the PA cannot accidentally key because its bias
        # is off.  Operator opts into PA via Settings -> TX in
        # Phase 3 UI work (sets _pa_on = True after they confirm
        # the antenna / dummy-load setup).
        self._tx_drive_level: int = 0        # 8-bit, 0..255 (operator
                                             # TX-power slider 0..100% →
                                             # 0..255 wire scaling)
        self._mic_boost_enabled: bool = False     # frame 10 C2 bit 0
        self._line_in_route_enabled: bool = False # frame 10 C2 bit 1
        self._pa_on: bool = False            # frame 10 C3 bit 7 (PA bias
                                             # enable; default OFF for
                                             # safety -- no accidental RF)
        self._bpf_filter_bits: int = 0       # 7-bit, frame 10 C3 bits 0-6
        self._lpf_filter_bits: int = 0       # 8-bit, frame 10 C4

        # ── HPSDR P1 frame 0 (register 0x00) state (v0.2 Phase 1) ───
        # Frame 0 carries general-settings axes that span CW mode,
        # 7-bit OC output pins (PA TX/RX relay control + BPF board
        # selectors), ADC dither/random, BPF expansion-board input
        # routing, antenna select, duplex, nddc, and diversity.
        #
        # v0.1 wrote only C1 (sample-rate code) and C4 (a static
        # ``_config_c4 = 0x1C`` = duplex + nddc-1<<3); C2 and C3 stayed
        # zero.  Works on HL2 fresh-install (no BPF board, single
        # antenna, no diversity) but BLOCKER for v0.2 PA TX/RX relay
        # work (which drives oc_output bit 0 to switch the antenna
        # between RX and TX on PTT) and LATENT for v0.4 ANAN (which
        # uses every byte of this register).
        #
        # All defaults zero/False -- matches HL2 fresh-power-up state.
        # Fresh-install wire bytes after this refactor: C1=rate_code,
        # C2=0x00, C3=0x00, C4=0x1C.  Identical to v0.1 wire output.
        self._oc_output: int = 0             # 7-bit (frame 0 C2 bits 1-7)
        self._cw_eer_enabled: bool = False   # frame 0 C2 bit 0
        # C3 axes (BPF expansion board state; HL2 mostly ignores)
        self._atten_10db_enabled: bool = False
        self._atten_20db_enabled: bool = False
        self._rx0_preamp_enabled: bool = False  # Anan; HL2 ignores
        self._adc_dither_enabled: bool = False
        self._adc_random_enabled: bool = False
        self._rx_1_out_enabled: bool = False
        # RX-input routing (mutually exclusive; priority order
        # matches Thetis: XVTR > RX_1_In > RX_2_In, first-match wins)
        self._xvtr_rx_in_enabled: bool = False  # C3 bits 5-6 = 0b11
        self._rx_1_in_enabled: bool = False     # C3 bits 5-6 = 0b01
        self._rx_2_in_enabled: bool = False     # C3 bits 5-6 = 0b10
        # C4 axes (HL2 mostly ignores; Anan uses)
        self._antenna_select: int = 0        # 0=ANT1, 1=ANT2, 2=ANT3
        self._diversity_enabled: bool = False

        # ── HPSDR P1 frame 18 (register 0x74) state (v0.2 Phase 1) ──
        # Reset-on-disconnect: HL2-only safety register.  When set,
        # the gateware auto-reverts to RX state if the host TCP link
        # drops -- prevents silent carrier on air after a Lyra crash
        # mid-TX.
        #
        # DEFAULT FLIPPED TO FALSE 2026-05-15 (Phase 1 commit 6.1):
        # operator-bench-confirmed that True wedges the HL2 on clean
        # stop+restart cycles.  Gateware treats our deliberate stop()
        # as a "disconnect" and enters reset, ignoring the next
        # START_IQ until reset completes (non-deterministic timing).
        # Bisect: v0.1.1 (pre-aef0106) stop+restart rock solid; DEV
        # (post-aef0106) hangs after first stop.  Composer + cycle
        # slot + eager registration stay in place; Phase 3 will wire
        # the Settings UI toggle + a "write 0 before stop()" handshake
        # so the safety can be re-enabled without the wedge.  Until
        # then, RX-only v0.2.0 doesn't need the safety (no TX yet).
        self._reset_on_disconnect: bool = False

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

        # v0.2 Phase 1 (3/10): seed frame 10 with the fresh-install
        # default (PA off, drive 0, no filter selections, mic-boost +
        # line-in routing both off).  This eagerly registers 0x12 in
        # the C&C cycle so the HL2 gateware always sees Lyra's
        # PA-bias-OFF intent rather than inheriting power-up state.
        # Future Phase 1 setters (drive level, pa_on toggle from
        # Settings UI) re-call _refresh_frame_10 after mutating state.
        self._refresh_frame_10()

        # v0.2 Phase 1 (4/10): seed frame 4 (register 0x1C) with the
        # fresh-install default (0x00, 0x00, 0x00, 0x00).  Eagerly
        # registers 0x1C in the cycle so the 5-bit redundant
        # tx_step_attn write stays in sync with the 6-bit copy in
        # frame 11 C4.  Operator-facing setter for the TX step
        # attenuator (Phase 3 UI) MUST call both _refresh_frame_4
        # and _refresh_frame_11.
        self._refresh_frame_4()

        # v0.2 Phase 1 (5/10): re-seed frame 0 via the composer so
        # C2/C3 reflect operator state attributes instead of the
        # v0.1 static zeros.  Fresh-install bytes for HL2 unchanged
        # at (rate_code, 0x00, 0x00, 0x1C); structural fix lets
        # future setters for oc_output / cw_eer / atten / dither /
        # antenna select / diversity drive the wire bytes without
        # requiring a re-write of set_sample_rate or _send_config.
        self._refresh_frame_0()

        # v0.2 Phase 1 (6/10): seed frame 18 (register 0x74) with
        # reset_on_disconnect = 1 (safety default).  HL2-only
        # register; gateware uses this to auto-revert to RX state
        # if the host TCP link drops, preventing silent carrier
        # on air after a Lyra crash mid-TX.  Eager registration
        # ensures HL2 receives the safe value from cycle 1 rather
        # than inheriting power-up state.
        self._refresh_frame_18()

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

        # v0.2 Phase 1 (8/10): snapshot MOX once per UDP datagram so
        # both USB blocks carry the same C0 bit 0 value.  Today
        # _snapshot_mox_bit returns 0 (no PTT state machine yet);
        # Phase 3 UI wires it via Radio.set_mox().
        mox_bit = self._snapshot_mox_bit()

        for block_idx, block_off in enumerate((8, 520)):
            frame[block_off + 0] = 0x7F
            frame[block_off + 1] = 0x7F
            frame[block_off + 2] = 0x7F
            frame[block_off + 3] = (c0 & 0xFE) | mox_bit
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

    # ── v0.2 Phase 2 commit 8: TX I/Q queue API ──────────────────────
    # Sibling of queue_tx_audio / clear_tx_audio / _pack_audio_bytes.
    # Complex64 samples at 48 kHz feed EP2 frame columns 2..3 (TX I,
    # TX Q) at the writer's ~381 Hz cadence (126 samples per frame).
    # Producer side comes online in Phase 2 commit 7-redo (queue-based
    # DSP worker thread crossing replacing today's env-gated direct
    # call); until then queue_tx_iq is callable but inject_tx_iq stays
    # False by default so the consumer ignores the queue and EP2 bytes
    # 4..7 stay zero (wire-identical to v0.1 RX-only behavior).

    def queue_tx_iq(self, iq) -> None:
        """Push complex IQ samples into the EP2 TX I/Q queue.

        Accepts a 1D ndarray convertible to ``dtype=np.complex64``
        (real and imag in [-1, 1]; values outside are clipped at
        quantization time).  EP2 frames drain 126 samples per
        cadence tick; queue is capped at ``maxlen=48000`` (~1 sec
        at 48 kHz).  Overflow drops the OLDEST samples and bumps
        ``tx_iq_overruns``.

        Threading: producer-safe.  Holds ``_tx_iq_lock`` for the
        deque mutation.  Mirror of ``queue_tx_audio``.
        """
        import numpy as np
        arr = np.asarray(iq, dtype=np.complex64).ravel()
        with self._tx_iq_lock:
            free = (self._tx_iq.maxlen or 0) - len(self._tx_iq)
            if arr.size > free:
                self.tx_iq_overruns += int(arr.size - free)
            self._tx_iq.extend(arr)

    def clear_tx_iq(self) -> None:
        """Drop all queued TX I/Q samples.

        Called on PTT release / mode change / sink swap to prevent
        stale baseband samples from leaking into the next TX cycle.
        Mirror of ``clear_tx_audio``.
        """
        with self._tx_iq_lock:
            self._tx_iq.clear()

    def _drain_tx_iq_be(self, n_samples: int):
        """Pop up to ``n_samples`` complex64 from ``_tx_iq``, zero-pad
        on underrun, return (i_be, q_be) BE int16 component arrays.

        Caller: ``_pack_audio_bytes_pairs`` (the EP2 frame composer).
        Both component arrays are shape ``(n_samples,)`` dtype ``'>i2'``
        so they slot directly into the BE int16 (126, 4) frame view
        used for the L/R columns.

        Threading: drains ``_tx_iq`` under ``_tx_iq_lock``.

        On underrun (queue had < n_samples), increments
        ``tx_iq_underruns`` and pads with ``0+0j`` so the EP2 frame
        is always full-length.  Padding bytes quantize to 0x0000
        (baseband DC, harmless on RX since MOX=0 keeps the PA off).
        """
        import numpy as np
        with self._tx_iq_lock:
            avail = min(len(self._tx_iq), n_samples)
            pulled = [self._tx_iq.popleft() for _ in range(avail)]
        if avail < n_samples:
            self.tx_iq_underruns += 1
            pulled.extend([complex(0.0, 0.0)] * (n_samples - avail))
        iq = np.asarray(pulled, dtype=np.complex64)
        # Apply scalar gain to both components.
        iq = iq * np.complex64(self.tx_iq_gain)
        # Clip per-component then quantize round-to-nearest.  No TPDF
        # dither here -- the L/R path uses TPDF because the AK4951
        # codec output goes to a human ear and quantization grain is
        # audible at low volumes.  TX I/Q feeds the HL2 modulator /
        # upsampler / RF DAC; -96 dBFS quantization stairsteps are
        # buried below typical TX SNR and don't merit the extra ~10
        # us per frame.
        real = np.clip(iq.real, -1.0, 1.0) * 32767.0
        imag = np.clip(iq.imag, -1.0, 1.0) * 32767.0
        return (np.round(real).astype(">i2"),
                np.round(imag).astype(">i2"))

    def _register_cc_slot(self, c0: int) -> None:
        """Ensure ``c0`` is present in ``_cc_cycle`` so the writer
        round-robin will visit this register.  Idempotent.

        Caller MUST be holding ``_cc_lock``.  Appends to the cycle
        in insertion order -- the writer walks the list in the
        order setters first register their slots.  When callers
        register slots in HPSDR P1 case-N order, the resulting
        cycle preserves that ordering for the writer.

        v0.2 Phase 1 helper.  Replaces the v0.1 ``sorted(keys)``
        anti-pattern.  See ``__init__`` block at line ~632 for
        rationale.
        """
        if c0 not in self._cc_cycle:
            self._cc_cycle.append(c0)

    def _compose_frame_11(self) -> tuple[int, int, int, int]:
        """Compose HPSDR P1 frame 11 (register 0x14) from current
        state.  Returns the (C1, C2, C3, C4) tuple ready for the
        EP2 writer's round-robin.

        Layout:
        * C1: rx0/rx1/rx2 preamp bits (bits 0-2) + mic-switch bits
              (mic_bias bit 4, mic_ptt bit 5, mic_trs bit 6)
        * C2: line_in_gain (5-bit) | (puresignal_run << 6)
        * C3: user_dig_out (4-bit) -- HL2 expansion-header dig-out
        * C4: step attenuator with override-enable bit 6 set.  TX
              value when ``dispatch_state.mox`` asserted, RX value
              otherwise.  Encoded as ``0x40 | ((db + 12) & 0x3F)``.

        Thread-safe to call from any thread (reads atomic int/bool
        attributes + a snapshot of the frozen dispatch_state via
        the provider).  Does NOT acquire any locks itself -- caller
        is responsible for taking ``_cc_lock`` if they intend to
        write the result into ``_cc_registers``.  ``_refresh_frame_11``
        is the lock-acquiring sibling.

        v0.2 Phase 1: MOX is always False today (no PTT state
        machine wired yet -- item 8 of Phase 1).  Composer reads
        the live state regardless so the TX-step-attn path is
        ready the moment MOX wires up.
        """
        # C1 -- preamps and mic switches
        c1 = self._rx_preamp_bits & 0x07
        if self._mic_bias_enabled:
            c1 |= 0x10
        if self._mic_ptt_enabled:
            c1 |= 0x20
        if self._mic_trs_enabled:
            c1 |= 0x40
        # C2 -- line-in gain and PureSignal run bit
        c2 = self._line_in_gain & 0x1F
        if self._puresignal_run:
            c2 |= 0x40
        # C3 -- user digital output pins
        c3 = self._user_dig_out & 0x0F
        # C4 -- MOX-gated step attenuator with override-enable.
        # Read dispatch_state via the provider (set by Radio when
        # the stream is bound; defaults None during early init).
        mox = False
        if self._dispatch_state_provider is not None:
            try:
                mox = bool(self._dispatch_state_provider().mox)
            except Exception:
                mox = False
        step_attn_db = (self._tx_step_attn_db if mox
                        else self._rx_step_attn_db)
        c4 = 0x40 | ((int(step_attn_db) + 12) & 0x3F)
        return (c1, c2, c3, c4)

    def _refresh_frame_11(self) -> None:
        """Recompute frame 11 and cache it in ``_cc_registers[0x14]``.

        Call this from any setter that mutates frame-11 state
        (set_lna_gain_db, future mic-bias / line-in / user-dig-out
        setters, future TX-step-attn setter, future MOX-edge handler).
        Idempotent w.r.t. cycle-list membership.

        Caller MUST NOT be holding ``_cc_lock`` -- this method
        acquires it.
        """
        c1, c2, c3, c4 = self._compose_frame_11()
        with self._cc_lock:
            self._cc_registers[0x14] = (c1, c2, c3, c4)
            self._register_cc_slot(0x14)

    def _compose_frame_10(self) -> tuple[int, int, int, int]:
        """Compose HPSDR P1 frame 10 (register 0x12) from current
        state.  Returns the (C1, C2, C3, C4) tuple ready for the
        EP2 writer's round-robin.

        Layout:
        * C1: tx_drive_level (8-bit, 0..255) -- primary PA drive
        * C2: mic_boost bit 0 | line_in_route bit 1 | 0x40 constant
              (HL2 inherits the 0x40 bit unconditionally from the
              legacy Apollo-board flag position)
        * C3: bpf_filter_bits (7-bit, bits 0-6) | (pa_on << 7) --
              PA bias enable lives at bit 7
        * C4: lpf_filter_bits (8-bit, low-pass filter selector)

        Thread-safe to call from any thread (reads atomic int/bool
        attributes only).  Does NOT acquire any locks itself --
        ``_refresh_frame_10`` is the lock-acquiring sibling.

        v0.2 Phase 1 defaults: drive=0, all toggles False, pa_on=False.
        Wire output: (0x00, 0x40, 0x00, 0x00).  PA bias is OFF so
        even when MOX bit emission lands (item 8), no RF can be
        keyed until operator opts in via Phase 3 Settings UI.
        """
        c1 = self._tx_drive_level & 0xFF
        c2 = 0x40  # HL2 constant (legacy Apollo-flag bit position)
        if self._mic_boost_enabled:
            c2 |= 0x01
        if self._line_in_route_enabled:
            c2 |= 0x02
        c3 = self._bpf_filter_bits & 0x7F
        if self._pa_on:
            c3 |= 0x80
        c4 = self._lpf_filter_bits & 0xFF
        return (c1, c2, c3, c4)

    def _refresh_frame_10(self) -> None:
        """Recompute frame 10 and cache it in ``_cc_registers[0x12]``.

        Call this from any setter that mutates frame-10 state
        (future set_tx_drive, set_pa_on, set_mic_boost, BPF/LPF
        setters).  Idempotent w.r.t. cycle-list membership.

        v0.2 Phase 1 calls this once at the end of ``__init__`` to
        register 0x12 in the cycle with the fresh-install default
        bytes (0x00, 0x40, 0x00, 0x00).  Unlike frame 11 (which
        registers lazily on first set_lna_gain_db call), frame 10
        has no existing v0.1 setter -- eager registration ensures
        the HL2 gateware always sees Lyra's PA-off intent rather
        than inheriting power-up state.

        Caller MUST NOT be holding ``_cc_lock`` -- this method
        acquires it.
        """
        c1, c2, c3, c4 = self._compose_frame_10()
        with self._cc_lock:
            self._cc_registers[0x12] = (c1, c2, c3, c4)
            self._register_cc_slot(0x12)

    def _compose_frame_4(self) -> tuple[int, int, int, int]:
        """Compose HPSDR P1 frame 4 (register 0x1C) -- the ADC
        assignment / TX-step-attenuator register.

        Layout:
        * C1: ADC routing lower-8 bits (Orion-specific; HL2 ignores)
        * C2: ADC routing upper bits (Orion-specific; HL2 ignores)
        * C3: tx_step_attn & 0x1F (5-bit redundant write of the
              TX step attenuator -- HL2 reads this in parallel with
              the 6-bit+override copy in frame 11 C4)
        * C4: 0

        Why both frames carry tx_step_attn: HL2 firmware reads both
        registers; the consensus behaviour is that both wire copies
        must be coherent.  Any operator-facing setter that mutates
        ``_tx_step_attn_db`` MUST call BOTH ``_refresh_frame_4`` and
        ``_refresh_frame_11`` so the two encodings stay in sync.
        Encodings differ:
        * Frame 4 C3 -- unsigned 5-bit mask: ``db & 0x1F``
        * Frame 11 C4 -- 6-bit + override: ``0x40 | ((db + 12) & 0x3F)``

        Lock-free.  ``_refresh_frame_4`` is the lock-acquiring sibling.

        v0.2 Phase 1: ``_tx_step_attn_db`` defaults to 0; frame 4
        ships as (0x00, 0x00, 0x00, 0x00) for fresh installs.
        """
        # C1 / C2 ADC routing -- HL2 doesn't use; zero placeholders
        # so the gateware sees an explicit value rather than power-up
        # state.
        c1 = 0
        c2 = 0
        # C3 -- 5-bit unsigned masking of tx_step_attn_db.  HL2 firmware
        # reads this register in parallel with frame 11 C4's extended-
        # range encoding.
        c3 = int(self._tx_step_attn_db) & 0x1F
        c4 = 0
        return (c1, c2, c3, c4)

    def _refresh_frame_4(self) -> None:
        """Recompute frame 4 and cache it in ``_cc_registers[0x1C]``.

        Call this whenever ``_tx_step_attn_db`` changes (must also
        call ``_refresh_frame_11`` so frame-11 C4 stays coherent).
        Future ADC-routing setters (Orion / Anan v0.4) would also
        trigger this refresh.

        v0.2 Phase 1 calls this once at the end of ``__init__`` for
        eager registration -- frame 4 has no existing v0.1 setter,
        so without eager registration HL2 inherits power-up ADC
        state.

        Caller MUST NOT be holding ``_cc_lock`` -- this method
        acquires it.
        """
        c1, c2, c3, c4 = self._compose_frame_4()
        with self._cc_lock:
            self._cc_registers[0x1C] = (c1, c2, c3, c4)
            self._register_cc_slot(0x1C)

    def _compose_frame_0(self) -> tuple[int, int, int, int]:
        """Compose HPSDR P1 frame 0 (register 0x00) -- general settings.

        Layout:
        * C1: SampleRateIn2Bits & 3 -- HPSDR P1 sample-rate code
              (48k=0, 96k=1, 192k=2, 384k=3 per SAMPLE_RATES)
        * C2: cw_eer bit 0 | (oc_output << 1) bits 1-7
              (7-bit open-collector output pins drive PA TX/RX relay
              + BPF board selectors on HL2 expansion header)
        * C3: 10dB-atten bit 0 | 20dB-atten bit 1 | rx0_preamp bit 2 |
              adc_dither bit 3 | adc_random bit 4 |
              rx-input route bits 5-6 | rx_1_out bit 7
        * C4: antenna_select bits 0-1 | duplex bit 2 |
              (nddc-1) << 3 | diversity bit 7

        RX-input routing priority (Thetis convention): XVTR > RX_1_In
        > RX_2_In; first-match wins.  HL2 ignores these (BPF
        expansion-board state); Anan v0.4 uses them.

        Main-loop emission ALWAYS has the duplex bit set.  Priming
        emission uses ``_priming_c4`` (static; HL2-only) which is
        the same byte with duplex cleared.  v0.4 work that adds
        operator-tunable antenna select will revisit _priming_c4
        to derive from state too.

        Lock-free.  ``_refresh_frame_0`` is the lock-acquiring sibling.

        v0.2 Phase 1 default wire bytes: (rate_code, 0x00, 0x00,
        0x1C).  Identical to v0.1 output for fresh installs.
        """
        c1 = SAMPLE_RATES[self.sample_rate] & 0xFF
        # C2 -- CW EER + 7-bit OC output pins
        c2 = (1 if self._cw_eer_enabled else 0)
        c2 |= (int(self._oc_output) << 1) & 0xFE
        # C3 -- BPF / ADC / RX-input routing
        c3 = 0
        if self._atten_10db_enabled:
            c3 |= 0x01
        if self._atten_20db_enabled:
            c3 |= 0x02
        if self._rx0_preamp_enabled:
            c3 |= 0x04
        if self._adc_dither_enabled:
            c3 |= 0x08
        if self._adc_random_enabled:
            c3 |= 0x10
        # RX-input routing -- mutually exclusive, first-match wins
        if self._xvtr_rx_in_enabled:
            c3 |= 0x60   # bits 5-6 = 0b11
        elif self._rx_1_in_enabled:
            c3 |= 0x20   # bits 5-6 = 0b01
        elif self._rx_2_in_enabled:
            c3 |= 0x40   # bits 5-6 = 0b10
        if self._rx_1_out_enabled:
            c3 |= 0x80
        # C4 -- antenna select + duplex + nddc + diversity
        c4 = 0
        if self._antenna_select == 2:
            c4 |= 0x02   # ANT3
        elif self._antenna_select == 1:
            c4 |= 0x01   # ANT2
        # antenna_select == 0 -> ANT1 -> both bits clear
        c4 |= 0x04       # duplex (always set in main-loop emission)
        c4 |= ((4 - 1) << 3) & 0x78  # nddc=4 -> bits 3-6 = 0x18
        if self._diversity_enabled:
            c4 |= 0x80
        return (c1, c2, c3, c4)

    def _refresh_frame_0(self) -> None:
        """Recompute frame 0 and cache it in ``_cc_registers[0x00]``.

        Call this whenever any frame-0 state attribute changes
        (sample_rate change, future setters for oc_output / antenna_
        select / atten / preamp / dither / random / diversity).
        Future Phase 1 work wires the OC-pins setter when the
        operator-facing PA TX/RX relay control lands.

        v0.2 Phase 1 calls this once at the end of ``__init__`` so
        the cached frame 0 reflects composed state from
        ``self.sample_rate`` and the frame-0 axes (rather than the
        v0.1 static C2/C3 = 0x00 hardcodes).

        Caller MUST NOT be holding ``_cc_lock`` -- this method
        acquires it.
        """
        c1, c2, c3, c4 = self._compose_frame_0()
        with self._cc_lock:
            self._cc_registers[0x00] = (c1, c2, c3, c4)
            self._register_cc_slot(0x00)

    def _compose_frame_18(self) -> tuple[int, int, int, int]:
        """Compose HPSDR P1 frame 18 (register 0x74) -- reset-on-
        disconnect safety control.

        Layout:
        * C1 = C2 = C3 = 0 (unused)
        * C4 = reset_on_disconnect (1 = gateware auto-reverts to
          RX on host TCP link loss; 0 = stay in whatever state
          the operator left it)

        HL2-only register.  Lyra defaults to 1 so a crash mid-TX
        doesn't leave the radio transmitting silent carrier; the
        gateware drops PTT within a few seconds of losing the host
        connection.  Operator can opt out via Settings -> TX in
        Phase 3 UI for advanced use cases.

        Lock-free.  ``_refresh_frame_18`` is the lock-acquiring
        sibling.
        """
        c4 = 1 if self._reset_on_disconnect else 0
        return (0, 0, 0, c4)

    def _refresh_frame_18(self) -> None:
        """Recompute frame 18 and cache it in ``_cc_registers[0x74]``.

        Call this when ``_reset_on_disconnect`` changes (future
        Settings UI setter).  v0.2 Phase 1 calls this once at the
        end of ``__init__`` for eager registration -- without it,
        a fresh-install Lyra connecting to an HL2 with stale
        reset-on-disconnect state could leave the radio in an
        unsafe configuration.

        Caller MUST NOT be holding ``_cc_lock`` -- this method
        acquires it.
        """
        c1, c2, c3, c4 = self._compose_frame_18()
        with self._cc_lock:
            self._cc_registers[0x74] = (c1, c2, c3, c4)
            self._register_cc_slot(0x74)

    def _snapshot_mox_bit(self) -> int:
        """Read the current MOX state from the dispatch-state
        provider and return the C0 bit-0 value (0 or 1).

        v0.2 Phase 1: HL2 gateware reads C0 bit 0 of every frame-0
        emission as the operator's MOX intent.  Lyra's
        ``dispatch_state.mox`` is the canonical source -- written
        only by Qt main thread via ``Radio.set_mox(bool)``; readable
        from any thread (GIL makes the frozen-dataclass attribute
        read atomic).

        Snapshot ONCE per UDP datagram and apply the same value to
        both USB blocks of that datagram (gateware reads each
        block's C0 independently; a partial-frame MOX mismatch
        would corrupt state).  Callers in
        ``_build_ep2_frame`` / ``_build_ep2_frame_with_audio`` call
        this method once before their ``for block_idx`` loop.

        Defaults to 0 (RX) when no dispatch-state provider is
        registered (pre-Radio-binding init window) or when the
        provider raises -- defensive against partial init order.

        v0.2 Phase 1 has no live MOX setter caller (PTT state
        machine wires in Phase 3 UI work).  Wire-level output
        today: bit stays 0 unconditionally because
        ``dispatch_state.mox`` defaults False.  Identical to v0.1
        wire output -- this commit is the gate-passing fix that
        unblocks Phase 3.
        """
        if self._dispatch_state_provider is None:
            return 0
        try:
            return 1 if self._dispatch_state_provider().mox else 0
        except Exception:
            return 0

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
        # Phase 1: also register the slot in the cycle list so the
        # writer actually visits it.
        with self._cc_lock:
            self._cc_registers[c0] = (c1, c2, c3, c4)
            self._register_cc_slot(c0)
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
                #
                # v0.2 Phase 1: cycle walks ``_cc_cycle`` (the
                # operator-ordered list) instead of ``sorted(keys)``.
                # Setters that mutate ``_cc_registers`` also call
                # ``_register_cc_slot(c0)`` so the cycle stays in
                # sync.  The writer never falls through to the
                # "empty registers" branch in practice (the dict
                # is seeded at __init__ with 0x00 and 0x2e), but
                # the fallback stays as belt-and-suspenders.
                try:
                    with self._cc_lock:
                        if self._cc_cycle:
                            c0 = self._cc_cycle[
                                self._cc_rr_idx % len(self._cc_cycle)]
                            c1, c2, c3, c4 = self._cc_registers[c0]
                            self._cc_rr_idx = (
                                self._cc_rr_idx + 1) % len(self._cc_cycle)
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

        # v0.2 Phase 1 (8/10): snapshot MOX once per UDP datagram so
        # both USB blocks carry the same C0 bit 0 value.  See
        # _snapshot_mox_bit docstring + sibling site in
        # _build_ep2_frame.  Today this returns 0 (no PTT state
        # machine yet); Phase 3 UI wires it via Radio.set_mox().
        mox_bit = self._snapshot_mox_bit()

        for block_idx, block_off in enumerate((8, 520)):
            frame[block_off + 0] = 0x7F
            frame[block_off + 1] = 0x7F
            frame[block_off + 2] = 0x7F
            frame[block_off + 3] = (c0 & 0xFE) | mox_bit
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
        # column 0 = L, column 1 = R, columns 2 = TX I, column 3 = TX Q.
        # Row-major .tobytes() gives the exact 1008-byte layout the
        # gateware expects with zero per-row Python work.
        out_arr = np.zeros((126, 4), dtype=">i2")
        out_arr[:, 0] = int16_be[:, 0]
        out_arr[:, 1] = int16_be[:, 1]

        # v0.2 Phase 2 commit 8: TX I/Q packing.  When inject_tx_iq is
        # True (flipped by Phase 3 PTT state machine on MOX=1 edge),
        # drain 126 complex samples from the _tx_iq queue, quantize
        # per-component to BE int16, write into columns 2..3.  When
        # False (default), columns stay zero -- wire-identical to v0.1
        # RX-only behavior.  Underrun pads with zeros + bumps
        # tx_iq_underruns counter (see _drain_tx_iq_be docstring).
        if self.inject_tx_iq:
            iq_real_be, iq_imag_be = self._drain_tx_iq_be(126)
            out_arr[:, 2] = iq_real_be
            out_arr[:, 3] = iq_imag_be

        return out_arr.tobytes()

    def _send_config(self):
        """Priming send: one-shot UDP emission with the priming C4.

        Per CLAUDE.md §3.2 (Phase 1 + Round 1 IM-1 entry) + plan
        §4.4 step 6 fail-mode (a): the priming general-settings
        frame matches Thetis ``ForceCandCFrames`` semantics --
        ``C4 = (nddc-1) << 3 = 0x18`` (NO duplex bit).  The duplex
        bit is added only in the main-loop case-0 path emitted by
        the EP2 writer's round-robin from ``_cc_registers[0x00]``
        (which was seeded with ``_config_c4 = 0x1C`` in
        ``__init__``).

        Pre-Phase-1 v0.0.9.x logic called ``_send_cc(... self._config_c4)``
        which both (a) updated ``_cc_registers[0x00]`` to the
        priming value AND (b) emitted one UDP frame.  Both legs
        carried the same value (0x04 = NDDC=1 + duplex), so there
        was no priming/main-loop distinction to make.  Phase 1
        nddc=4 introduces the distinction (priming 0x18 vs
        main-loop 0x1C), so this method now does an explicit
        direct UDP emit with ``_priming_c4`` and leaves the
        register table alone -- the writer's round-robin continues
        to emit ``_config_c4`` from the table seeded in __init__.
        """
        if self._sock is None:
            return
        rate_code = SAMPLE_RATES[self.sample_rate]
        with self._send_lock:
            frame = self._build_ep2_frame(
                0x00, rate_code, 0x00, 0x00, self._priming_c4,
            )
            self._sock.sendto(frame, (self.radio_ip, DISCOVERY_PORT))

    # -- public API ---------------------------------------------------------
    def register_consumer(
        self,
        consumer_id: ConsumerID,
        callback: Optional[Callable[[np.ndarray, "FrameStats"], None]],
    ) -> None:
        """Register (or clear) the consumer callback for one ConsumerID.

        Phase 1 (v0.1) — plan §4.2 deliverable 3.  Per-DDC samples
        are routed by ``dispatch_ddc_samples`` based on the result
        of ``lyra.protocol.ddc_map(state)``; the routing maps each
        DDC index to a ``ConsumerID``, and this dict carries the
        actual callback to invoke for that ConsumerID.

        Pass ``callback=None`` to clear a slot (== DISCARD
        semantics: dispatched samples are silently dropped).

        Threading: writes to ``self._consumers`` are not locked;
        callers MUST register before ``start()`` or accept that an
        in-flight datagram may still see the previous callback.
        The dict slot read in the dispatcher is GIL-atomic under
        CPython, so the worst case is one extra datagram landing on
        the old callback during a hot-swap -- never a torn read or
        crash.
        """
        if consumer_id not in self._consumers:
            raise KeyError(
                f"unknown ConsumerID {consumer_id!r}; valid keys are "
                f"{list(self._consumers.keys())!r}"
            )
        self._consumers[consumer_id] = callback

    def register_mic_consumer(
        self,
        callback: Optional[Callable[[np.ndarray, "FrameStats"], None]],
    ) -> None:
        """Register (or clear) the mic-samples callback.

        Sibling of ``register_consumer`` but for the mic-input path
        (which is NOT a DDC -- it's a separate byte slot in every EP6
        datagram).  Per CLAUDE.md §3.3 + ``_parse_iq_frame``
        deconstruction: each datagram carries 38 mic samples at
        nddc=4 (one per 26-byte slot, two USB blocks per datagram x
        19 slots per block).  HL2 mic rate is fixed at 48 kHz by
        the AK4951 codec.

        Callback signature:
            mic_callback(samples: np.ndarray[int16], stats: FrameStats)

        Pass ``callback=None`` to clear.  Threading model identical
        to ``register_consumer``: dict-attribute write is GIL-atomic
        under CPython; worst case is one extra datagram on the old
        callback during a hot-swap.

        v0.2 Phase 2: Radio's TX-dispatcher hooks here in commit 5+
        once TxChannel mic-input path is wired end-to-end.  Until
        then, default is None and mic samples continue to drop on
        the floor (preserves v0.1 behaviour).
        """
        self._mic_callback = callback

    def set_dispatch_state_provider(
        self,
        provider: Optional[Callable[[], DispatchState]],
    ) -> None:
        """Configure the function the RX loop calls per datagram to
        get the current ``DispatchState``.

        Plan §4.2.x threading-model summary:
        * Qt main thread is sole writer of ``Radio._dispatch_state``.
        * ``HL2Stream._rx_loop`` reads once per UDP datagram via
          this provider.
        * No locking required -- CPython GIL makes the reference
          read atomic.

        When ``provider`` is ``None`` (or not configured), the
        dispatcher synthesizes a default ``DispatchState()``
        (mox=False, ps_armed=False, rx2_enabled=False, family=HL2)
        on every datagram.  This is the back-compat path for
        callers that haven't wired a real Radio-side state source
        yet.
        """
        self._dispatch_state_provider = provider

    def dispatch_ddc_samples(
        self,
        state: DispatchState,
        decoded: Dict[int, np.ndarray],
        stats: "FrameStats",
    ) -> None:
        """Route per-DDC samples to registered consumers per
        ``ddc_map(state)``.

        Phase 1 (v0.1) — plan §4.2 deliverable 3.  Pure dispatcher:
        looks up the family + state-product specific routing table,
        then for each DDC index fans out to the consumer callback
        registered at the resolved ``ConsumerID``.  No DSP, no I/O.

        Per CLAUDE.md §6.7 discipline #6 ("any ``if ddc_idx == N:``
        in non-protocol code is wrong"): this method IS the
        protocol layer's per-DDC fan-out, so the per-DDC iteration
        is correct here.  Callers downstream MUST NOT branch on
        DDC index -- they consume the resolved ``ConsumerID`` slot
        only.

        Errors in a single consumer callback are caught + logged
        so one buggy consumer doesn't take down the whole RX loop
        (Phase 1 deliberately tolerates per-DDC consumer failures
        rather than dropping the whole datagram; a crash in one
        DDC's pipeline should not silence the other DDCs).

        Args:
            state: Snapshot from the dispatch-state provider (or
                a default ``DispatchState()`` if no provider is
                configured).
            decoded: Output of ``_parse_iq_frame`` -- dict of
                ``{ddc_idx: ndarray}`` for DDCs 0..3.
            stats: Live ``FrameStats`` reference passed to each
                consumer (back-compat with the v0.0.9.x
                ``on_samples(samples, stats)`` signature).
        """
        # Local import to avoid module-load cycle: lyra.protocol's
        # __init__ imports radio_state, which has no dependency on
        # stream.py, but stream.py wants ddc_map at runtime only.
        from lyra.protocol import ddc_map as _ddc_map

        try:
            routing = _ddc_map(state)
        except NotImplementedError:
            # Defensive: an unknown family snapshot should not crash
            # the RX loop.  Treat as RX-only HL2.  Logged at WARN.
            _log.warning(
                "ddc_map raised NotImplementedError for family "
                "%s; falling back to HL2 RX-only routing.",
                state.family,
            )
            from lyra.radio_state import RadioFamily
            from dataclasses import replace as _dc_replace
            state = _dc_replace(state, family=RadioFamily.HL2)
            routing = _ddc_map(state)

        for ddc_idx in range(_NDDC):
            consumer_id = routing.get(ddc_idx, ConsumerID.DISCARD)
            cb = self._consumers.get(consumer_id)
            if cb is None:
                continue
            try:
                cb(decoded[ddc_idx], stats)
            except Exception:
                _log.exception(
                    "Consumer %s raised on DDC%d samples; other "
                    "DDCs continue.",
                    consumer_id, ddc_idx,
                )

    def start(
        self,
        on_samples: Optional[Callable[[np.ndarray, "FrameStats"], None]] = None,
        rx_freq_hz: Optional[int] = None,
        lna_gain_db: Optional[int] = None,
        *,
        on_rx2_samples: Optional[
            Callable[[np.ndarray, "FrameStats"], None]
        ] = None,
        dispatch_state_provider: Optional[
            Callable[[], DispatchState]
        ] = None,
    ):
        """Start the EP6 RX loop + EP2 writer thread.

        Phase 1 v0.1 (2026-05-11): added keyword-only
        ``on_rx2_samples`` (DDC1 consumer) and
        ``dispatch_state_provider`` (per-datagram DispatchState
        source).  Back-compat: passing only ``on_samples`` keeps
        v0.0.9.x behavior -- DDC0 samples flow to the legacy
        callback, DDC1/2/3 are dispatched but with no consumer
        registered (== silently discarded).

        ``on_samples`` registers as the ``RX_AUDIO_CH0`` consumer
        (RX1 audio chain).  ``on_rx2_samples`` registers as
        ``RX_AUDIO_CH2`` (RX2 audio chain).  Both go through the
        dispatcher exactly the same way -- ``ddc_map(state)``
        decides which DDC's samples end up in each slot.
        """
        if self._thread and self._thread.is_alive():
            raise RuntimeError("stream already running")
        # Register the back-compat consumers BEFORE the RX loop
        # starts so the very first datagram sees them.
        if on_samples is not None:
            self.register_consumer(ConsumerID.RX_AUDIO_CH0, on_samples)
        if on_rx2_samples is not None:
            self.register_consumer(ConsumerID.RX_AUDIO_CH2, on_rx2_samples)
        self.set_dispatch_state_provider(dispatch_state_provider)

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
            target=self._rx_loop, args=(), daemon=True,
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

    def _set_rx2_freq(self, hz: int):
        """Write RX2 (DDC1) NCO frequency to the C&C register table.

        Phase 0 v0.1 (2026-05-11) per consensus-plan §3.1.x item 5.
        Sibling of ``_set_rx1_freq`` -- same packing, different C0
        register slot.  Phase 0 leaves no live caller: Radio's RX2
        VFO ("VFO B shadow freq" per plan §3.2) is operator-tunable
        already but Phase 1 RX2 wires the call from
        ``Radio._on_vfo_b_changed`` onward.

        **Plan-text divergence (flagged Phase 0):** the consensus
        plan §3.1.x item 5 literally said "writes
        ``_cc_registers[0x03]``", but ``0x03`` is Thetis's
        round-robin case-INDEX comment (``networkproto1.c:995``
        ``case 3: //RX2 VFO (DDC1) 0x03``), NOT the C0 byte.  The
        actual HPSDR P1 C0 byte the gateware decodes for RX2 NCO is
        ``0x06`` (``C0 |= 6`` at ``networkproto1.c:996``), a sibling
        of RX1 NCO's ``0x04`` (used by ``_set_rx1_freq`` above and
        documented at HPSDR P1 reg map).  Lyra's ``_cc_registers``
        dict is keyed by raw C0 byte (``0x00`` general settings,
        ``0x04`` RX1 NCO, ``0x2e`` TX latency), so the correct key
        for the RX2 NCO write is ``0x06``.  Writing to ``0x03``
        would clobber the "TX NCO with PTT bit set" slot, which
        would be wrong.  Plan defect slipped through all six review
        rounds; we implement the byte that's correct against the
        Thetis source-of-truth rather than the byte the plan
        literally typed.
        """
        c0 = 0x06  # RX2 NCO freq (DDC1) -- HPSDR P1 standard, matches
                   # Thetis networkproto1.c:996 (C0 |= 6 for case 3 RX2).
        c1 = (hz >> 24) & 0xFF
        c2 = (hz >> 16) & 0xFF
        c3 = (hz >> 8) & 0xFF
        c4 = hz & 0xFF
        # Same Path C.2 audio-pop discipline as _set_rx1_freq: no
        # direct _send_cc; let the EP2 writer thread re-emit the
        # updated register on its next round-robin tick.  (Phase 0
        # has no live caller for this method, but the discipline is
        # baked in upfront so Phase 1 RX2 callers don't have to
        # rediscover it.)
        with self._cc_lock:
            self._cc_registers[c0] = (c1, c2, c3, c4)
            self._register_cc_slot(c0)

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
            self._register_cc_slot(c0)

    def _set_tx_freq(self, hz: int) -> None:
        """Write TX frequency to all three HL2 NCO registers atomically.

        HPSDR P1 on HL2 nddc=4 writes the TX frequency to three
        C&C registers, all carrying the same big-endian 4-byte
        encoding:
        * 0x02 (case 1, TX VFO NCO)
        * 0x08 (case 5, DDC2 -- HL2 mirrors TX freq here on nddc=4
          so v0.3 PureSignal feedback DDCs sit at TX freq when
          cntrl1=4 PA-coupler routing engages)
        * 0x0a (case 6, DDC3 -- always TX freq)

        All three writes happen under one ``_cc_lock`` acquisition
        so the round-robin never sees a partial update.  Mirrors
        ``_set_rx1_freq`` / ``_set_rx2_freq`` byte composition.

        Path C.2 audio-pop discipline: no direct _send_cc here --
        the EP2 writer thread re-emits the new registers on the
        next round-robin tick (~few ms).  See _set_rx1_freq for
        the full rationale.

        v0.2 Phase 1 adds the wire-level setter only.  Operator-
        facing TX freq state (Radio.tx_freq_hz with SPLIT-aware
        routing to VFO A or VFO B) lands in Phase 3 UI work; until
        then this method has no live caller and TX freq stays at 0
        on the wire (frame 0 emission carries duplex bit so HL2
        DDC2/DDC3 are wire-enabled but receive zeros from a
        zero-tuned NCO -- harmless on RX-only).
        """
        hz = int(hz)
        self._tx_freq_hz = hz
        c1 = (hz >> 24) & 0xFF
        c2 = (hz >> 16) & 0xFF
        c3 = (hz >> 8) & 0xFF
        c4 = hz & 0xFF
        nco_bytes = (c1, c2, c3, c4)
        with self._cc_lock:
            for c0 in (0x02, 0x08, 0x0a):
                self._cc_registers[c0] = nco_bytes
                self._register_cc_slot(c0)

    def set_sample_rate(self, rate: int):
        """Change sample rate on a running stream.

        v0.2 Phase 1: routes through ``_refresh_frame_0`` so every
        byte of register 0x00 carries coherent state (C1=new rate
        code, plus C2/C3/C4 from the operator's other frame-0 axes
        -- OC pins, atten, dither, antenna select, diversity).  v0.1
        wrote only C1 + a static C4 and zeroed C2/C3 -- works on
        fresh installs but would trample any operator state set by
        future setters in subsequent Phase 1 commits.
        """
        if rate not in SAMPLE_RATES:
            raise ValueError(f"rate must be one of {list(SAMPLE_RATES)}")
        if self._sock is None:
            raise RuntimeError("stream not started")
        self.sample_rate = rate
        # Reset EP2 cadence counter so the new rate's keepalive
        # cadence starts clean. Without this, switching from 192 k to
        # 48 k would carry stale counter modulo state.
        self._ep6_count = 0
        # Path C.2 (audio-pop fix): NO direct _send_cc here -- it
        # would drain 126 audio samples without consuming a
        # semaphore signal and click the AK4951.  See _set_rx1_freq
        # for the full rationale.  Composer reads sample_rate from
        # the just-updated attribute.
        self._refresh_frame_0()

    def set_lna_gain_db(self, gain_db: int):
        """Set HL2 RX step attenuator (operator-facing "LNA gain") in dB.
        Range -12..+48.

        Updates the cached frame 11 (HPSDR P1 register 0x14) via the
        composer so all four bytes carry coherent state.  The C4
        byte encodes the value as ``0x40 | ((db + 12) & 0x3F)``
        with the override-enable bit 6 set; C1/C2/C3 carry the
        operator's preamp / mic-switch / line-in / dig-out state
        (defaults to zero on a fresh install).

        Auto-LNA fires set_lna_gain_db roughly 1-2x per second under
        normal band-noise variation.  Path C.2 audio-pop discipline:
        no direct _send_cc here -- the EP2 writer thread re-emits
        the cached register on its next round-robin tick (~few ms)
        so the new value reaches the gateware imperceptibly fast.
        See _set_rx1_freq for full rationale.
        """
        if not -12 <= gain_db <= 48:
            raise ValueError("gain_db must be in -12..+48")
        if self._sock is None:
            raise RuntimeError("stream not started")
        self._rx_step_attn_db = int(gain_db)
        self._refresh_frame_11()

    def stop(self):
        self._stop_event.set()
        # Wake the EP2 writer thread if it's blocked waiting for
        # first EP6 (e.g., radio never streamed).  Setting the
        # event here lets it observe _stop_event and exit cleanly
        # rather than waiting out its 5 s timeout.
        self._first_ep6_event.set()
        # §15.21 bug 1 + 2 fix (§15.24 plan item B, 2026-05-15).
        # Teardown order is RACE-CRITICAL:
        #   1. send STOP (socket still open);
        #   2. join the EP2 writer FIRST -- it is timer-driven and
        #      self-exits within one cadence tick (~3 ms) of
        #      _stop_event (woken via _first_ep6_event if blocked
        #      pre-stream).  It must die BEFORE we close the socket,
        #      else its guarded `sendto` (stream.py ~2291) could
        #      fire on a freed/None socket -- a race the OLD
        #      close-after-all-joins order avoided and that a naive
        #      close-first reorder would REINTRODUCE;
        #   3. close + null the socket -- this is the bug-1 win:
        #      the RX loop is blocked in `recvfrom` (settimeout
        #      0.5 s); closing makes it raise OSError immediately,
        #      caught by `_rx_loop`'s existing `except OSError:
        #      break` (~stream.py:2978 -- A5's "must widen except"
        #      prerequisite was verified ALREADY met, the break
        #      predates this fix), so the RX thread exits at once
        #      instead of the main thread waiting out the join /
        #      recv timeout;
        #   4. join + null the RX thread (bug 2: clear the stale
        #      ref the old code never nulled).
        # Pre-7-redo this whole path was effectively never hit
        # (env-gated TX dispatch); post-7-redo TX teardown is
        # unconditional so every stop() runs it -- hence the
        # promotion from §15.21 "latent/parked" to this fix.
        if self._sock is not None:
            try:
                self._sock.sendto(
                    _build_start_stop_packet(STOP), (self.radio_ip, DISCOVERY_PORT)
                )
            except OSError:
                pass
        if self._ep2_writer_thread is not None:
            self._ep2_writer_thread.join(timeout=1.0)
            self._ep2_writer_thread = None
        if self._sock is not None:
            # Writer is now dead; safe to close.  Unblocks the RX
            # recvfrom immediately (bug 1).
            with self._send_lock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None          # bug 2: clear stale ref

    # -- internal -----------------------------------------------------------
    def _rx_loop(self) -> None:
        """Pure RX loop (Phase 1 v0.1, nddc=4).

        Reads EP6 datagrams, parses (nddc=4 26-byte slot stride),
        decodes telemetry, dispatches per-DDC samples to registered
        consumers via ``dispatch_ddc_samples``.  Does NOT send EP2
        frames -- that work lives on the dedicated EP2 writer
        thread to decouple EP2 send cadence from bursty UDP arrival
        timing.

        Sets ``_first_ep6_event`` on the first valid EP6 datagram
        so the writer thread knows the gateware has finished its
        initialization and is streaming.

        Phase 1 changes from v0.0.9.x ``_rx_loop``:

        * No ``on_samples`` argument -- consumers are registered
          via ``register_consumer`` / ``start()`` kwargs and looked
          up in ``self._consumers`` keyed by ``ConsumerID``.
        * Per-datagram ``DispatchState`` snapshot via
          ``self._dispatch_state_provider``; defaults to
          ``DispatchState()`` (RX-only HL2) when no provider is
          configured.
        * ``_parse_iq_frame`` now returns per-DDC dicts instead of
          a single concatenated samples array (see CLAUDE.md §3.3
          for the new 26-byte slot layout).
        * Stats ``samples`` counter tracks DDC0 sample count
          (back-compat with the legacy single-DDC counter that
          downstream UI reads).
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
            # v0.2 Phase 2 (3/N): mic samples are now consumed.  Earlier
            # phases discarded with ``_mic`` underscore-prefix; now
            # routed to the registered mic_callback below.
            seq, per_ddc, mic, cc0, cc1 = parsed

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
            # Sample counter tracks DDC0 sample count for back-compat
            # with v0.0.9.x consumers; at nddc=4 each DDC carries 38
            # samples per datagram (vs 126 at nddc=1).
            self.stats.samples += per_ddc[0].shape[0]
            self.stats.last_c1_c4 = cc1

            # Fold both C&C blocks into the rolling telemetry slots.
            # Each EP6 frame carries two C&C blocks and the radio
            # rotates the C0 telemetry address across blocks AND
            # frames — hitting both blocks here halves the latency
            # to the next refresh of any given field.
            _decode_hl2_telemetry(cc0, self.stats)
            _decode_hl2_telemetry(cc1, self.stats)

            # v0.2 Phase 2 (3/N): fire the mic-input callback per
            # datagram.  Runs BEFORE dispatch_ddc_samples so consumers
            # can read ptt_in / adc_overload from the same FrameStats
            # that the just-completed telemetry decode populated.
            # Independent of dispatch_state -- mic is always present in
            # every datagram regardless of mox / ps_armed / rx2_enabled.
            # Wrapped in try/except so a broken consumer (raising or
            # taking too long) doesn't kill the RX loop -- worst case,
            # one datagram's mic samples are dropped.
            mic_cb = self._mic_callback
            if mic_cb is not None:
                try:
                    mic_cb(mic, self.stats)
                except Exception as exc:
                    _log.warning("mic_callback raised: %s", exc)

            # Snapshot the dispatch state ONCE per datagram per the
            # plan §4.2.x "Reader semantics" contract.  Mid-datagram
            # MOX edges are coalesced to the next datagram boundary
            # (~1 ms at 192 kHz).
            provider = self._dispatch_state_provider
            state = provider() if provider is not None else DispatchState()

            self.dispatch_ddc_samples(state, per_ddc, self.stats)
