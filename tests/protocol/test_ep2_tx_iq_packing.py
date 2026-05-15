"""EP2 frame TX I/Q byte packing tests (v0.2 Phase 2 commit 8).

Validates that the EP2 audio-slot columns 2..3 (TX I, TX Q) are
correctly packed from the ``HL2Stream._tx_iq`` queue when
``inject_tx_iq`` is True, AND that the default behavior (queue
ignored, columns stay zero) is byte-identical to v0.1 RX-only
behavior.

Critical regression gate: any future change to the EP2 packer must
keep the inject_tx_iq=False path bit-exact, since that is what
RX-only Lyra (the shipping v0.1.1 GA and the v0.2.0 pre-TX path)
emits on the wire.
"""
from __future__ import annotations

import struct

import numpy as np

from lyra.protocol.stream import HL2Stream


def _make_stream() -> HL2Stream:
    """Construct an HL2Stream without opening sockets / threads.

    HL2Stream.__init__ does not touch the network; start() is what
    binds sockets and spawns threads.  Safe for byte-packing unit
    tests.
    """
    # IQ rate doesn't matter for byte-packing tests (the 48 kHz audio
    # cadence is independent); pick the smallest valid value.
    return HL2Stream("10.10.10.1", sample_rate=96000)


def test_default_tx_iq_stays_zero():
    """When inject_tx_iq is False (default), EP2 columns 2..3 stay
    zero regardless of what's in the queue.  Wire-identical to v0.1."""
    s = _make_stream()
    # Even if a stray producer queues IQ, default path ignores it.
    s.queue_tx_iq(np.full(126, 0.5 + 0.5j, dtype=np.complex64))
    # 126 mono samples of 0.5 audio
    pairs = [(0.5, 0.5)] * 126
    out = s._pack_audio_bytes_pairs(pairs)
    assert len(out) == 1008  # 126 * 8 bytes
    # Per-slot: bytes 4..7 (TX I msb/lsb, TX Q msb/lsb) MUST be zero.
    for i in range(126):
        slot = out[i * 8:i * 8 + 8]
        assert slot[4:8] == b"\x00\x00\x00\x00", (
            f"Slot {i} TX I/Q bytes leaked: {slot[4:8].hex()}"
        )


def test_inject_tx_iq_packs_into_columns_2_3():
    """When inject_tx_iq is True and queue has data, columns 2..3
    carry the BE int16 quantization of the complex samples."""
    s = _make_stream()
    s.inject_tx_iq = True
    iq = np.full(126, 0.5 + 0.25j, dtype=np.complex64)
    s.queue_tx_iq(iq)
    pairs = [(0.0, 0.0)] * 126
    out = s._pack_audio_bytes_pairs(pairs)
    expected_i = int(round(0.5 * 32767))   # 16384
    expected_q = int(round(0.25 * 32767))  # 8192
    for i in range(126):
        slot = out[i * 8:i * 8 + 8]
        got_i = struct.unpack(">h", slot[4:6])[0]
        got_q = struct.unpack(">h", slot[6:8])[0]
        assert got_i == expected_i, (
            f"Slot {i} I mismatch: {got_i} != {expected_i}"
        )
        assert got_q == expected_q, (
            f"Slot {i} Q mismatch: {got_q} != {expected_q}"
        )


def test_tx_iq_negative_values_round_correctly():
    """Negative real/imag quantize to negative int16 BE (two's-complement)."""
    s = _make_stream()
    s.inject_tx_iq = True
    iq = np.full(126, -0.5 - 0.25j, dtype=np.complex64)
    s.queue_tx_iq(iq)
    pairs = [(0.0, 0.0)] * 126
    out = s._pack_audio_bytes_pairs(pairs)
    expected_i = int(round(-0.5 * 32767))   # -16384
    expected_q = int(round(-0.25 * 32767))  # -8192
    for i in range(126):
        got_i = struct.unpack(">h", out[i * 8 + 4:i * 8 + 6])[0]
        got_q = struct.unpack(">h", out[i * 8 + 6:i * 8 + 8])[0]
        assert got_i == expected_i, f"Slot {i} negative I: {got_i}"
        assert got_q == expected_q, f"Slot {i} negative Q: {got_q}"


def test_tx_iq_clips_at_full_scale():
    """Values outside [-1, 1] clip rather than wrap."""
    s = _make_stream()
    s.inject_tx_iq = True
    # 2.0 + 1.5j -> clip both to ±1 -> ±32767
    iq = np.full(126, 2.0 + 1.5j, dtype=np.complex64)
    s.queue_tx_iq(iq)
    pairs = [(0.0, 0.0)] * 126
    out = s._pack_audio_bytes_pairs(pairs)
    for i in range(126):
        got_i = struct.unpack(">h", out[i * 8 + 4:i * 8 + 6])[0]
        got_q = struct.unpack(">h", out[i * 8 + 6:i * 8 + 8])[0]
        assert got_i == 32767, f"Slot {i} I should clip to +32767, got {got_i}"
        assert got_q == 32767, f"Slot {i} Q should clip to +32767, got {got_q}"


def test_tx_iq_underrun_increments_counter():
    """Drain with empty queue → padded with zeros + counter ticks."""
    s = _make_stream()
    s.inject_tx_iq = True  # enable consumer
    # No queue_tx_iq call → queue is empty
    pairs = [(0.0, 0.0)] * 126
    assert s.tx_iq_underruns == 0
    out = s._pack_audio_bytes_pairs(pairs)
    assert s.tx_iq_underruns == 1
    # Bytes still zero (graceful underrun)
    for i in range(126):
        slot = out[i * 8:i * 8 + 8]
        assert slot[4:8] == b"\x00\x00\x00\x00", (
            f"Underrun slot {i} should have zero IQ: {slot[4:8].hex()}"
        )


def test_tx_iq_overrun_drops_at_maxlen():
    """queue_tx_iq with > maxlen samples bumps overrun counter and
    silently drops oldest (deque maxlen semantics)."""
    s = _make_stream()
    # Fill to capacity (48000 samples = ~1 sec at 48 kHz)
    iq_full = np.zeros(48000, dtype=np.complex64)
    s.queue_tx_iq(iq_full)
    assert s.tx_iq_overruns == 0
    # Add 100 more — should overflow
    iq_extra = np.zeros(100, dtype=np.complex64)
    s.queue_tx_iq(iq_extra)
    assert s.tx_iq_overruns == 100
    # Capacity unchanged
    assert len(s._tx_iq) == 48000


def test_tx_iq_clear():
    """clear_tx_iq empties the queue."""
    s = _make_stream()
    iq = np.zeros(126, dtype=np.complex64)
    s.queue_tx_iq(iq)
    assert len(s._tx_iq) == 126
    s.clear_tx_iq()
    assert len(s._tx_iq) == 0


def test_tx_iq_gain_applied():
    """tx_iq_gain scales both real and imag before quantization."""
    s = _make_stream()
    s.inject_tx_iq = True
    s.tx_iq_gain = 0.5
    iq = np.full(126, 1.0 + 0.0j, dtype=np.complex64)
    s.queue_tx_iq(iq)
    pairs = [(0.0, 0.0)] * 126
    out = s._pack_audio_bytes_pairs(pairs)
    # 1.0 * 0.5 = 0.5 → 16384
    expected_i = int(round(0.5 * 32767))
    for i in range(126):
        got_i = struct.unpack(">h", out[i * 8 + 4:i * 8 + 6])[0]
        assert got_i == expected_i, f"Slot {i} gain-scaled I: {got_i}"


def test_lr_audio_unaffected_by_iq_injection():
    """Verify that flipping inject_tx_iq=True doesn't disturb the L/R
    bytes -- the two paths are independent."""
    s = _make_stream()
    s.inject_tx_iq = True
    s.queue_tx_iq(np.full(126, 0.3 + 0.7j, dtype=np.complex64))
    # Use a recognizable non-zero L/R signal
    pairs = [(0.25, -0.5)] * 126
    out = s._pack_audio_bytes_pairs(pairs)
    # tx_audio_gain default is 0.5 → 0.25 * 0.5 = 0.125 → quant = 4096
    # but TPDF dither adds ±1 LSB noise so allow a tolerance.
    for i in range(126):
        got_l = struct.unpack(">h", out[i * 8 + 0:i * 8 + 2])[0]
        got_r = struct.unpack(">h", out[i * 8 + 2:i * 8 + 4])[0]
        # 0.25 * 0.5 * 32767 = 4095.875 → ~4096 ± 1
        assert abs(got_l - 4096) <= 1, f"L drifted: {got_l}"
        # -0.5 * 0.5 * 32767 = -8191.75 → ~-8192 ± 1
        assert abs(got_r - (-8192)) <= 1, f"R drifted: {got_r}"
