"""Phase 1 EP6 parser correctness — nddc=4 26-byte slot stride.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §4.2
deliverable 1:

  > **Rewrite ``lyra/protocol/stream.py::_decode_iq_samples``** to
  > handle the nddc=4 26-byte slot stride per CLAUDE.md §3.3.
  > Output: per-DDC IQ ndarrays ``{0: ndarray, 1: ndarray, 2:
  > ndarray, 3: ndarray}`` with mic samples in a separate slot.
  > Each ndarray has 19 complex samples per USB block × 2 blocks
  > = 38 complex per UDP datagram per DDC.  The existing
  > single-DDC 8-byte decode path is retired.

This module synthesizes valid EP6 datagrams with known per-DDC
values, runs them through the parser, and asserts the values
land in the right slot.  Catches the §4.3 "byte stride math
off-by-one is the most common first-try failure mode" pitfall
deterministically rather than waiting for a real-radio symptom.

Plus a couple of focused tests for the new dispatch helpers
(``twist``, ``HL2Stream.dispatch_ddc_samples``).

Run from repo root::

    python -m unittest tests.protocol.test_ep6_parser_nddc4 -v
"""
from __future__ import annotations

import struct
import unittest
from typing import Dict, List, Tuple

import numpy as np

from lyra.protocol.stream import (
    FrameStats,
    HL2Stream,
    _decode_iq_samples,
    _parse_iq_frame,
    twist,
)
from lyra.radio_state import ConsumerID, DispatchState, RadioFamily


# ──────────────────────────────────────────────────────────────────────
# Synthetic EP6 datagram builder
# ──────────────────────────────────────────────────────────────────────

_NDDC = 4
_SLOTS_PER_BLOCK = 19
_SLOT_STRIDE = 26


def _make_24bit_be(value: int) -> bytes:
    """Pack a signed 24-bit int into 3 big-endian bytes."""
    if value < 0:
        value += 1 << 24
    return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])


def _make_16bit_be(value: int) -> bytes:
    """Pack a signed 16-bit int into 2 big-endian bytes."""
    if value < 0:
        value += 1 << 16
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


def _build_usb_block(
    cc0: int, cc1: int, cc2: int, cc3: int, cc4: int,
    ddc_i: Dict[int, List[int]],
    ddc_q: Dict[int, List[int]],
    mic: List[int],
) -> bytes:
    """Build one 512-byte USB block with the given C&C + per-slot values.

    Each per-DDC value list must be length 19 (slots per block).
    """
    assert all(len(ddc_i[d]) == _SLOTS_PER_BLOCK for d in range(_NDDC))
    assert all(len(ddc_q[d]) == _SLOTS_PER_BLOCK for d in range(_NDDC))
    assert len(mic) == _SLOTS_PER_BLOCK
    parts = bytearray()
    # sync
    parts += b"\x7F\x7F\x7F"
    # C0..C4
    parts += bytes([cc0, cc1, cc2, cc3, cc4])
    # 19 slots × 26 bytes
    for s in range(_SLOTS_PER_BLOCK):
        for d in range(_NDDC):
            parts += _make_24bit_be(ddc_i[d][s])
            parts += _make_24bit_be(ddc_q[d][s])
        parts += _make_16bit_be(mic[s])
    # Pad up to 512 bytes (trailing 10 bytes after slots are
    # unused on the wire; just zero-pad).
    parts += b"\x00" * (512 - len(parts))
    assert len(parts) == 512, len(parts)
    return bytes(parts)


def _build_ep6_datagram(
    seq: int,
    block0: bytes,
    block1: bytes,
) -> bytes:
    """Assemble the 1032-byte EP6 UDP datagram."""
    assert len(block0) == 512
    assert len(block1) == 512
    header = b"\xEF\xFE\x01\x06" + struct.pack(">I", seq)
    out = header + block0 + block1
    assert len(out) == 1032
    return out


# ──────────────────────────────────────────────────────────────────────
# Decoder tests
# ──────────────────────────────────────────────────────────────────────

class DecodeIqSamplesTest(unittest.TestCase):
    """``_decode_iq_samples`` per-DDC slot-stride correctness."""

    def test_per_ddc_isolation(self) -> None:
        """Each DDC's bytes land in its own array, with no
        cross-talk into siblings.  Catches stride off-by-one."""
        # Make each DDC's I value distinctively encode (ddc_idx,
        # slot_idx) so any misroute is caught visually.
        ddc_i = {
            d: [(d + 1) * 100000 + s for s in range(_SLOTS_PER_BLOCK)]
            for d in range(_NDDC)
        }
        ddc_q = {
            d: [-(d + 1) * 100000 - s for s in range(_SLOTS_PER_BLOCK)]
            for d in range(_NDDC)
        }
        mic = [s * 10 for s in range(_SLOTS_PER_BLOCK)]
        block = _build_usb_block(
            0, 0, 0, 0, 0, ddc_i, ddc_q, mic,
        )
        decoded = _decode_iq_samples(block[8:])

        scale = 1.0 / (1 << 23)
        for d in range(_NDDC):
            expected_i = np.array(ddc_i[d], dtype=np.int32) * scale
            expected_q = np.array(ddc_q[d], dtype=np.int32) * scale
            with self.subTest(ddc=d):
                np.testing.assert_allclose(
                    decoded[d].real.astype(np.float64),
                    expected_i.astype(np.float64),
                    rtol=0, atol=1e-7,
                )
                np.testing.assert_allclose(
                    decoded[d].imag.astype(np.float64),
                    expected_q.astype(np.float64),
                    rtol=0, atol=1e-7,
                )

    def test_mic_decode(self) -> None:
        """Mic samples land in the ``"mic"`` slot as int16 with
        proper sign extension."""
        zeros = {d: [0] * _SLOTS_PER_BLOCK for d in range(_NDDC)}
        mic = [12345, -23456, 0, 32767, -32768] + [s for s in range(_SLOTS_PER_BLOCK - 5)]
        block = _build_usb_block(0, 0, 0, 0, 0, zeros, zeros, mic)
        decoded = _decode_iq_samples(block[8:])
        np.testing.assert_array_equal(
            decoded["mic"], np.array(mic, dtype=np.int16),
        )

    def test_negative_24bit_sign_extension(self) -> None:
        """24-bit signed sign-extension works for all-negative
        samples (catches the ``np.where(... & 0x800000)`` flag bit)."""
        # Set every slot of every DDC to -8388608 (min 24-bit signed
        # value) on I and +8388607 (max) on Q.
        min_24 = -(1 << 23)        # -8388608
        max_24 = (1 << 23) - 1     # +8388607
        scale = 1.0 / (1 << 23)
        i_vals = {d: [min_24] * _SLOTS_PER_BLOCK for d in range(_NDDC)}
        q_vals = {d: [max_24] * _SLOTS_PER_BLOCK for d in range(_NDDC)}
        block = _build_usb_block(
            0, 0, 0, 0, 0, i_vals, q_vals,
            [0] * _SLOTS_PER_BLOCK,
        )
        decoded = _decode_iq_samples(block[8:])
        for d in range(_NDDC):
            with self.subTest(ddc=d):
                # Real part = min_24 * scale = -1.0 exactly.
                self.assertAlmostEqual(
                    float(decoded[d].real[0]), -1.0, places=6,
                )
                # Imag part = max_24 * scale = (2^23 - 1) / 2^23.
                self.assertAlmostEqual(
                    float(decoded[d].imag[0]), max_24 * scale,
                    delta=1e-6,
                )

    def test_short_buffer_raises(self) -> None:
        with self.assertRaises(ValueError):
            _decode_iq_samples(b"\x00" * 400)

    def test_output_dtype(self) -> None:
        zeros = {d: [0] * _SLOTS_PER_BLOCK for d in range(_NDDC)}
        block = _build_usb_block(0, 0, 0, 0, 0, zeros, zeros, [0] * _SLOTS_PER_BLOCK)
        decoded = _decode_iq_samples(block[8:])
        for d in range(_NDDC):
            self.assertEqual(decoded[d].dtype, np.complex64)
            self.assertEqual(decoded[d].shape, (_SLOTS_PER_BLOCK,))
        self.assertEqual(decoded["mic"].dtype, np.int16)
        self.assertEqual(decoded["mic"].shape, (_SLOTS_PER_BLOCK,))


class ParseIqFrameTest(unittest.TestCase):
    """``_parse_iq_frame`` UDP datagram framing + concatenation."""

    def test_valid_datagram_returns_per_ddc(self) -> None:
        ddc_i = {d: [d * 1000 + s for s in range(_SLOTS_PER_BLOCK)] for d in range(_NDDC)}
        ddc_q = {d: [d * 1000 + s + 500 for s in range(_SLOTS_PER_BLOCK)] for d in range(_NDDC)}
        mic = [0] * _SLOTS_PER_BLOCK
        block0 = _build_usb_block(0x08, 1, 2, 3, 4, ddc_i, ddc_q, mic)
        block1 = _build_usb_block(0x10, 5, 6, 7, 8, ddc_i, ddc_q, mic)
        dg = _build_ep6_datagram(seq=42, block0=block0, block1=block1)
        parsed = _parse_iq_frame(dg)
        self.assertIsNotNone(parsed)
        seq, per_ddc, mic_out, cc0, cc1 = parsed
        self.assertEqual(seq, 42)
        for d in range(_NDDC):
            with self.subTest(ddc=d):
                # 38 samples per UDP per DDC (19 per block * 2 blocks)
                self.assertEqual(per_ddc[d].shape, (38,))
                self.assertEqual(per_ddc[d].dtype, np.complex64)
        self.assertEqual(mic_out.shape, (38,))
        self.assertEqual(cc0, bytes([0x08, 1, 2, 3, 4]))
        self.assertEqual(cc1, bytes([0x10, 5, 6, 7, 8]))

    def test_wrong_length_returns_none(self) -> None:
        self.assertIsNone(_parse_iq_frame(b"\x00" * 999))

    def test_wrong_magic_returns_none(self) -> None:
        zeros = {d: [0] * _SLOTS_PER_BLOCK for d in range(_NDDC)}
        block = _build_usb_block(0, 0, 0, 0, 0, zeros, zeros, [0] * _SLOTS_PER_BLOCK)
        dg = bytearray(_build_ep6_datagram(seq=0, block0=block, block1=block))
        dg[0] = 0xAA  # corrupt EFFE
        self.assertIsNone(_parse_iq_frame(bytes(dg)))

    def test_wrong_sync_returns_none(self) -> None:
        zeros = {d: [0] * _SLOTS_PER_BLOCK for d in range(_NDDC)}
        block = bytearray(_build_usb_block(0, 0, 0, 0, 0, zeros, zeros, [0] * _SLOTS_PER_BLOCK))
        block[0] = 0x00  # corrupt 7F 7F 7F sync
        dg = _build_ep6_datagram(seq=0, block0=bytes(block), block1=bytes(block))
        self.assertIsNone(_parse_iq_frame(dg))


# ──────────────────────────────────────────────────────────────────────
# Dispatch helpers
# ──────────────────────────────────────────────────────────────────────

class TwistHelperTest(unittest.TestCase):
    """``twist`` 4-channel interleave."""

    def test_basic_interleave(self) -> None:
        a = np.array([1 + 2j, 3 + 4j], dtype=np.complex64)
        b = np.array([5 + 6j, 7 + 8j], dtype=np.complex64)
        out = twist(a, b)
        self.assertEqual(out.shape, (2, 4))
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_array_equal(
            out, np.array([[1, 2, 5, 6], [3, 4, 7, 8]], dtype=np.float32),
        )

    def test_length_mismatch_raises(self) -> None:
        a = np.zeros(5, dtype=np.complex64)
        b = np.zeros(3, dtype=np.complex64)
        with self.assertRaises(ValueError):
            twist(a, b)


class DispatchDdcSamplesTest(unittest.TestCase):
    """``HL2Stream.dispatch_ddc_samples`` per-ConsumerID fan-out."""

    def setUp(self) -> None:
        # Build a stream without opening a socket (we never call start()).
        self.stream = HL2Stream("0.0.0.0", sample_rate=96000)
        # 38-sample dummy per-DDC arrays (one datagram's worth at nddc=4).
        self.decoded: Dict[int, np.ndarray] = {
            d: np.full(38, float(d) + 0j, dtype=np.complex64)
            for d in range(_NDDC)
        }
        # Recording consumers.
        self.received: Dict[ConsumerID, List[np.ndarray]] = {
            cid: [] for cid in ConsumerID
        }
        for cid in ConsumerID:
            self.stream.register_consumer(
                cid,
                (lambda samples, _stats, c=cid:
                    self.received[c].append(samples.copy())),
            )

    def test_rx_only_routes_ddc0_to_rx1_and_ddc1_to_rx2(self) -> None:
        state = DispatchState(family=RadioFamily.HL2)
        self.stream.dispatch_ddc_samples(state, self.decoded, FrameStats())
        self.assertEqual(len(self.received[ConsumerID.RX_AUDIO_CH0]), 1)
        self.assertEqual(len(self.received[ConsumerID.RX_AUDIO_CH2]), 1)
        # DDC0's marker value (real=0) lands at RX1.
        np.testing.assert_array_equal(
            self.received[ConsumerID.RX_AUDIO_CH0][0],
            self.decoded[0],
        )
        # DDC1's marker value (real=1) lands at RX2.
        np.testing.assert_array_equal(
            self.received[ConsumerID.RX_AUDIO_CH2][0],
            self.decoded[1],
        )
        # DDC2/DDC3 -> DISCARD slot (registered consumer logs them
        # but the dispatch table never routes anything to them on
        # HL2 RX-only).  Verify by checking DISCARD did NOT fire
        # for DDC2/DDC3-from-ps slots in this state.
        # Actually -- DISCARD IS the ConsumerID for DDC2 and DDC3
        # in this state, so it WILL fire twice (once each).
        self.assertEqual(len(self.received[ConsumerID.DISCARD]), 2)

    def test_mox_ps_routes_to_ps_feedback(self) -> None:
        state = DispatchState(
            family=RadioFamily.HL2, mox=True, ps_armed=True,
        )
        self.stream.dispatch_ddc_samples(state, self.decoded, FrameStats())
        self.assertEqual(len(self.received[ConsumerID.PS_FEEDBACK_I]), 1)
        self.assertEqual(len(self.received[ConsumerID.PS_FEEDBACK_Q]), 1)
        # RX audio consumers should NOT have fired in this state.
        self.assertEqual(len(self.received[ConsumerID.RX_AUDIO_CH0]), 0)
        self.assertEqual(len(self.received[ConsumerID.RX_AUDIO_CH2]), 0)
        # DDC0 -> PS_FEEDBACK_I (PA coupler via cntrl1=4)
        np.testing.assert_array_equal(
            self.received[ConsumerID.PS_FEEDBACK_I][0],
            self.decoded[0],
        )
        # DDC1 -> PS_FEEDBACK_Q (sync-paired to DDC0)
        np.testing.assert_array_equal(
            self.received[ConsumerID.PS_FEEDBACK_Q][0],
            self.decoded[1],
        )

    def test_unregistered_consumer_silently_skipped(self) -> None:
        """ConsumerID with no callback registered is a legal
        Phase 1 state -- the dispatcher must not crash."""
        # Clear the RX1 callback specifically.
        self.stream.register_consumer(ConsumerID.RX_AUDIO_CH0, None)
        state = DispatchState(family=RadioFamily.HL2)
        # Should not raise.
        self.stream.dispatch_ddc_samples(state, self.decoded, FrameStats())
        # And the other slot still fires.
        self.assertEqual(len(self.received[ConsumerID.RX_AUDIO_CH2]), 1)

    def test_consumer_exception_does_not_propagate(self) -> None:
        """If one consumer raises, the others still fire (RX1
        crashing must not silence RX2)."""
        def bad(_samples, _stats):
            raise RuntimeError("synthetic")
        self.stream.register_consumer(ConsumerID.RX_AUDIO_CH0, bad)
        state = DispatchState(family=RadioFamily.HL2)
        # Should not raise.
        self.stream.dispatch_ddc_samples(state, self.decoded, FrameStats())
        # RX2 callback still fired.
        self.assertEqual(len(self.received[ConsumerID.RX_AUDIO_CH2]), 1)

    def test_register_consumer_rejects_unknown_id(self) -> None:
        with self.assertRaises(KeyError):
            self.stream.register_consumer("not-a-consumer-id", lambda *a: None)  # type: ignore[arg-type]


class DispatchStateProviderTest(unittest.TestCase):
    """The dispatch-state provider hook is read per-datagram in
    ``_rx_loop`` -- belt-and-suspenders unit test."""

    def test_default_is_used_when_provider_unset(self) -> None:
        stream = HL2Stream("0.0.0.0", sample_rate=96000)
        self.assertIsNone(stream._dispatch_state_provider)

    def test_set_dispatch_state_provider_round_trip(self) -> None:
        stream = HL2Stream("0.0.0.0", sample_rate=96000)
        sentinel_state = DispatchState(
            family=RadioFamily.HL2, mox=True, ps_armed=True,
        )
        stream.set_dispatch_state_provider(lambda: sentinel_state)
        self.assertIs(stream._dispatch_state_provider(), sentinel_state)


# ──────────────────────────────────────────────────────────────────────
# Plan §4.1 / §3.2 wire-byte assertions
# ──────────────────────────────────────────────────────────────────────

class WireByteFlipTest(unittest.TestCase):
    """Verify the Phase 1 C4 byte flip from 0x04 to 0x1C +
    priming/main-loop split per CLAUDE.md §3.2 + plan §4.1."""

    def test_config_c4_is_main_loop_value(self) -> None:
        stream = HL2Stream("0.0.0.0", sample_rate=96000)
        self.assertEqual(
            stream._config_c4, 0x1C,
            f"Phase 1 main-loop C4 must be 0x1C (nddc=4 + duplex); "
            f"got 0x{stream._config_c4:02X}",
        )

    def test_priming_c4_has_nddc4_no_duplex(self) -> None:
        stream = HL2Stream("0.0.0.0", sample_rate=96000)
        self.assertEqual(
            stream._priming_c4, 0x18,
            f"Phase 1 priming C4 must be 0x18 (nddc=4, no duplex) "
            f"per CLAUDE.md §3.2; got 0x{stream._priming_c4:02X}",
        )

    def test_register_table_seeds_main_loop_c4(self) -> None:
        """``_cc_registers[0x00]`` C4 byte must be the main-loop
        value so the EP2 writer's round-robin emits 0x1C, not the
        priming 0x18."""
        stream = HL2Stream("0.0.0.0", sample_rate=96000)
        c0_entry = stream._cc_registers[0x00]
        self.assertEqual(c0_entry[3], 0x1C)


class Phase1RadioStreamC4SyncTest(unittest.TestCase):
    """Phase 1 bug-fix regression: ``Radio._config_c4`` MUST match
    ``HL2Stream._config_c4`` so ``Radio._send_full_config`` (which
    fires on every band change with filter-board enabled) doesn't
    write a stale nddc bit-field to the wire and downgrade the
    radio's DDC count mid-session.

    The initial Phase 1 patch flipped only the stream's ``_config_c4``
    to 0x1C (= nddc=4 + duplex) but left ``Radio._config_c4`` at
    the v0.0.9.x default 0x04 (= nddc=1 + duplex).  Operator
    symptom was: switch bands → blood-red waterfall + S9+72 meter
    + pulsing audio + ADC errors.  Root cause was the parser
    expected 26-byte slots (nddc=4) but the gateware had been
    told to send 8-byte slots (nddc=1) by the Radio-side stale
    constant.

    This regression test runs without instantiating ``Radio``
    (which would pull in Qt + WDSP + audio device); we just check
    the class-attribute literal in the source matches the stream
    constant.  Cheap, fast, prevents drift.
    """

    def test_radio_config_c4_matches_stream_config_c4(self) -> None:
        stream = HL2Stream("0.0.0.0", sample_rate=96000)
        # ``Radio.__init__`` sets ``self._config_c4 = 0x1C`` as an
        # instance attribute, so we read the literal from the
        # source rather than instantiating Radio.  The source line
        # must contain ``0x1C`` and not ``0x04``.
        import re
        from pathlib import Path
        radio_src = (
            Path(__file__).resolve().parents[2]
            / "lyra" / "radio.py"
        ).read_text(encoding="utf-8")
        # Find the Radio _config_c4 assignment line.
        match = re.search(
            r"self\._config_c4 = (0x[0-9A-Fa-f]+)\b[^\n]*",
            radio_src,
        )
        self.assertIsNotNone(
            match,
            "Could not find self._config_c4 assignment in radio.py",
        )
        radio_c4 = int(match.group(1), 16)
        self.assertEqual(
            radio_c4, stream._config_c4,
            f"Radio._config_c4 (0x{radio_c4:02X}) must match "
            f"HL2Stream._config_c4 (0x{stream._config_c4:02X}); "
            f"otherwise _send_full_config writes a stale C4 to the "
            f"wire on every band change with filter-board enabled "
            f"and downgrades the radio's DDC count mid-session.",
        )


if __name__ == "__main__":
    unittest.main()
