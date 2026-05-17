"""TX drive / power surface tests (v0.2.0 Phase 3 commit D-1).

Covers the operator TX-power path end to end, post-D-1 (the
Thetis-faithful TX% -> gateware drive-level mapping; see §15.26
corrected Commit-D scope -- Auditor 2's decisive finding that
frame-10 C1 ``drive_level`` is the PRIMARY transmit amplitude
scalar, NOT the AD9866 step attenuator):

  * Radio._tx_pct_to_drive_level  -- the linear percent ->
    8-bit drive-level quantiser (Thetis ``i = int(255 * f)``).
  * Radio.set_tx_power_pct  -- clamp, idempotent, QSettings
    persistence, push to the stream via set_tx_drive_level.
  * Radio.autoload_tx_power_settings  -- fresh-install default =
    0 % (=> drive level 0 => zero RF; fail-safe, PA also OFF).
  * HL2Stream.set_tx_drive_level  -- range guard (0..255),
    not-started guard, frame-10 (0x12) C1 refresh.
  * HL2Stream.set_tx_step_attn_db -- UNCHANGED (still the PS /
    ATT-on-TX layer); the Commit-C (31 - signed_db) frame-4 C3
    + frame-11 C4 mox-gate contract still holds.
"""
from __future__ import annotations

import sys
import unittest

from lyra.protocol.stream import HL2Stream


class _StubStream:
    def __init__(self) -> None:
        self.drive_calls: list[int] = []
        self.tx_attn_calls: list[int] = []

    def set_tx_drive_level(self, level: int) -> None:
        self.drive_calls.append(int(level))

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

    def test_endpoints(self) -> None:
        # 0 % = zero RF = drive 0; 100 % = full = drive 255.
        self.assertEqual(self.radio._tx_pct_to_drive_level(0), 0)
        self.assertEqual(self.radio._tx_pct_to_drive_level(100), 255)

    def test_midpoint_is_round_half_scale(self) -> None:
        # round(255 * 50 / 100) = round(127.5) = 128.
        self.assertEqual(self.radio._tx_pct_to_drive_level(50), 128)

    def test_monotonic_nondecreasing_as_pct_rises(self) -> None:
        prev = None
        for p in range(0, 101, 5):
            lvl = self.radio._tx_pct_to_drive_level(p)
            if prev is not None:
                self.assertGreaterEqual(lvl, prev)
            prev = lvl

    def test_mapping_clamps_out_of_range(self) -> None:
        self.assertEqual(self.radio._tx_pct_to_drive_level(150), 255)
        self.assertEqual(self.radio._tx_pct_to_drive_level(-20), 0)

    def test_set_clamps_and_is_idempotent(self) -> None:
        seen: list[int] = []
        self.radio.tx_power_pct_changed.connect(seen.append)
        self.radio.set_tx_power_pct(150)          # clamp -> 100
        self.assertEqual(self.radio.tx_power_pct, 100)
        self.radio.set_tx_power_pct(100)          # no-op (same)
        self.radio.set_tx_power_pct(-20)          # clamp -> 0
        self.assertEqual(self.radio.tx_power_pct, 0)
        self.assertEqual(seen, [100, 0])          # exactly two edges

    def test_set_pushes_drive_level_to_stream_and_persists(self) -> None:
        stub = _StubStream()
        self.radio._stream = stub
        self.radio.set_tx_power_pct(100)
        # Full drive -> 255 (NOT the step attenuator).
        self.assertEqual(stub.drive_calls, [255])
        self.assertEqual(stub.tx_attn_calls, [])
        # Persisted.
        from PySide6.QtCore import QSettings
        self.assertEqual(
            int(QSettings("N8SDR", "Lyra").value("tx/power_pct")), 100)

    def test_autoload_default_is_zero_drive(self) -> None:
        from PySide6.QtCore import QSettings
        QSettings("N8SDR", "Lyra").remove("tx/power_pct")
        stub = _StubStream()
        self.radio._stream = stub
        self.radio.autoload_tx_power_settings()
        # Fresh install -> 0 % -> drive level 0 (fail-safe).
        self.assertEqual(stub.drive_calls, [0])


class TxDriveLevelStreamTest(unittest.TestCase):
    def _stream(self) -> HL2Stream:
        s = HL2Stream("10.10.10.1", sample_rate=96000)
        s._sock = object()        # bypass the not-started guard
        return s

    def test_range_guard(self) -> None:
        s = self._stream()
        for bad in (-1, 256, 1000):
            with self.assertRaises(ValueError):
                s.set_tx_drive_level(bad)

    def test_not_started_guard(self) -> None:
        s = HL2Stream("10.10.10.1", sample_rate=96000)
        s._sock = None
        with self.assertRaises(RuntimeError):
            s.set_tx_drive_level(128)

    def test_refreshes_frame_10_c1(self) -> None:
        # Frame 10 = register 0x12; C1 (index 0) = drive level.
        s = self._stream()
        s.set_tx_drive_level(200)
        self.assertEqual(s._tx_drive_level, 200)
        self.assertEqual(s._cc_registers[0x12][0], 200)
        s.set_tx_drive_level(0)
        self.assertEqual(s._cc_registers[0x12][0], 0)
        s.set_tx_drive_level(255)
        self.assertEqual(s._cc_registers[0x12][0], 255)
        # C2 still the HL2 0x40 constant (PA bits land in D-2).
        self.assertEqual(s._cc_registers[0x12][1] & 0x40, 0x40)


class TxStepAttnStreamTest(unittest.TestCase):
    """set_tx_step_attn_db is UNCHANGED by D-1 -- it remains the
    PureSignal / ATT-on-TX layer (Commit-C wire contract)."""

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

    def test_refreshes_both_frame_4_with_31_minus_db(self) -> None:
        # Commit C (§15.26 C-REVERIFY): HL2 TX-att wire convention
        # is (31 - signed_db), then frame-4 C3 5-bit-masked.
        s = self._stream()
        s.set_tx_step_attn_db(7)
        self.assertEqual(s._tx_step_attn_db, 7)
        self.assertEqual(s._cc_registers[0x1C][2], (31 - 7) & 0x1F)
        s.set_tx_step_attn_db(-28)                 # max gain
        self.assertEqual(s._cc_registers[0x1C][2], (31 + 28) & 0x1F)
        s.set_tx_step_attn_db(31)                  # max atten
        self.assertEqual(s._cc_registers[0x1C][2], 0)
        # Frame 11 stays a cached coherent 4-tuple.
        self.assertEqual(len(s._cc_registers[0x14]), 4)

    def test_zero_db_encodes_31_minus_0(self) -> None:
        # db=0 -> wire (31-0)=31.  Inert at RX (TX-att; gateware
        # acts on it only during TX).
        s = self._stream()
        s.set_tx_step_attn_db(0)
        self.assertEqual(s._cc_registers[0x1C][2], 31 & 0x1F)


if __name__ == "__main__":
    unittest.main()
