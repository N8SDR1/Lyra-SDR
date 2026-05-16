"""TX drive / power surface tests (v0.2.0 Phase 3 commit 3.4).

Covers the operator TX-power path end to end:

  * Radio._tx_pct_to_attn_db / _tx_unity_pct  -- the 16-step
    percent->attenuator-dB quantiser + the unity (0 dB) default.
  * Radio.set_tx_power_pct  -- clamp, idempotent, QSettings
    persistence, push to the stream.
  * Radio.autoload_tx_power_settings  -- fresh-install default =
    the unity percent (=> dB 0 => frame-4 (0,0,0,0) wire-identical
    to the Phase-1 default).
  * HL2Stream.set_tx_step_attn_db  -- range guard, not-started
    guard, and the dual frame-4 + frame-11 refresh coherence
    contract.
"""
from __future__ import annotations

import sys
import unittest

from lyra.protocol.stream import HL2Stream


class _StubStream:
    def __init__(self) -> None:
        self.tx_attn_calls: list[int] = []

    def set_tx_step_attn_db(self, db: int) -> None:
        self.tx_attn_calls.append(int(db))

    def _set_rx1_freq(self, hz: int) -> None:
        pass

    def _set_tx_freq(self, hz: int) -> None:
        pass


class TxPowerMappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()
        self.lo, self.hi = self.radio.capabilities.tx_attenuator_range

    def test_endpoints(self) -> None:
        # 100% = most gain = lo (-28); 0% = most attenuation = hi.
        self.assertEqual(
            self.radio._tx_pct_to_attn_db(100, self.lo, self.hi),
            self.lo)
        self.assertEqual(
            self.radio._tx_pct_to_attn_db(0, self.lo, self.hi),
            self.hi)

    def test_monotonic_nonincreasing_db_as_pct_rises(self) -> None:
        prev = None
        for p in range(0, 101, 5):
            db = self.radio._tx_pct_to_attn_db(p, self.lo, self.hi)
            if prev is not None:
                self.assertLessEqual(db, prev)
            prev = db

    def test_unity_pct_quantises_to_zero_db(self) -> None:
        # The fresh-install default must land on 0 dB so frame-4
        # C3 stays 0 -> byte-identical to the Phase-1 wire state.
        up = self.radio._tx_unity_pct(self.lo, self.hi)
        self.assertEqual(
            self.radio._tx_pct_to_attn_db(up, self.lo, self.hi), 0)

    def test_set_clamps_and_is_idempotent(self) -> None:
        seen: list[int] = []
        self.radio.tx_power_pct_changed.connect(seen.append)
        self.radio.set_tx_power_pct(150)          # clamp -> 100
        self.assertEqual(self.radio.tx_power_pct, 100)
        self.radio.set_tx_power_pct(100)          # no-op (same)
        self.radio.set_tx_power_pct(-20)          # clamp -> 0
        self.assertEqual(self.radio.tx_power_pct, 0)
        self.assertEqual(seen, [100, 0])          # exactly two edges

    def test_set_pushes_to_stream_and_persists(self) -> None:
        stub = _StubStream()
        self.radio._stream = stub
        self.radio.set_tx_power_pct(100)
        self.assertEqual(stub.tx_attn_calls, [self.lo])  # full drive
        # Persisted.
        from PySide6.QtCore import QSettings
        self.assertEqual(
            int(QSettings("N8SDR", "Lyra").value("tx/power_pct")), 100)

    def test_autoload_default_is_unity_zero_db(self) -> None:
        from PySide6.QtCore import QSettings
        QSettings("N8SDR", "Lyra").remove("tx/power_pct")
        stub = _StubStream()
        self.radio._stream = stub
        self.radio.autoload_tx_power_settings()
        self.assertEqual(stub.tx_attn_calls, [0])  # unity -> 0 dB


class TxStepAttnStreamTest(unittest.TestCase):
    def _stream(self) -> HL2Stream:
        s = HL2Stream("10.10.10.1", sample_rate=96000)
        s._sock = object()        # bypass the not-started guard
        return s

    def test_range_guard(self) -> None:
        s = self._stream()
        for bad in (-29, 32, 100):
            with self.assertRaises(ValueError):
                s.set_tx_step_attn_db(bad)

    def test_not_started_guard(self) -> None:
        s = HL2Stream("10.10.10.1", sample_rate=96000)
        s._sock = None
        with self.assertRaises(RuntimeError):
            s.set_tx_step_attn_db(0)

    def test_refreshes_both_frame_4_and_frame_11(self) -> None:
        s = self._stream()
        s.set_tx_step_attn_db(7)
        self.assertEqual(s._tx_step_attn_db, 7)
        # Frame 4 C3 = tx_step_attn_db & 0x1F (un-gated).
        self.assertEqual(s._cc_registers[0x1C][2], 7 & 0x1F)
        # Frame 11 is cached/coherent (4-tuple present).
        self.assertEqual(len(s._cc_registers[0x14]), 4)

    def test_zero_keeps_frame_4_clear(self) -> None:
        s = self._stream()
        s.set_tx_step_attn_db(0)
        self.assertEqual(s._cc_registers[0x1C], (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
