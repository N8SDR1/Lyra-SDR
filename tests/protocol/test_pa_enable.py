"""PA-bias enable tests (v0.2.0 Phase 3 commit 3.5, §15.26 PART C).

The PA-enable opt-in: frame-10 (reg 0x12) C3 bit 7.  Default OFF
so MOX produces NO RF until the operator deliberately arms it;
a safety stand-down auto-disarms.  The Apollo-I2C dual-path is
NOT driven here (separate later change) -- capability-flagged so
the UI can warn.
"""
from __future__ import annotations

import sys
import unittest

from lyra.protocol.stream import HL2Stream


class _StubStream:
    def __init__(self) -> None:
        self.pa_calls: list[bool] = []

    def set_pa_on(self, on: bool) -> None:
        self.pa_calls.append(bool(on))

    def _set_rx1_freq(self, hz: int) -> None:
        pass

    def _set_tx_freq(self, hz: int) -> None:
        pass


class PaEnableStreamTest(unittest.TestCase):
    def _stream(self) -> HL2Stream:
        s = HL2Stream("10.10.10.1", sample_rate=96000)
        s._sock = object()        # bypass the not-started guard
        return s

    def test_not_started_guard(self) -> None:
        s = HL2Stream("10.10.10.1", sample_rate=96000)
        s._sock = None
        with self.assertRaises(RuntimeError):
            s.set_pa_on(True)

    def test_default_off_frame10_bit_clear(self) -> None:
        s = self._stream()
        s._refresh_frame_10()
        # Fresh default: C3 bit 7 (PA) clear; C2 keeps the 0x40
        # HL2 constant.  No RF can be keyed.
        self.assertEqual(s._cc_registers[0x12][2] & 0x80, 0)
        self.assertEqual(s._cc_registers[0x12], (0x00, 0x40, 0x00, 0x00))

    def test_enable_sets_bit7_disable_clears(self) -> None:
        s = self._stream()
        s.set_pa_on(True)
        self.assertTrue(s._pa_on)
        self.assertEqual(s._cc_registers[0x12][2] & 0x80, 0x80)
        s.set_pa_on(False)
        self.assertFalse(s._pa_on)
        self.assertEqual(s._cc_registers[0x12][2] & 0x80, 0)


class PaEnableRadioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_default_off(self) -> None:
        self.assertFalse(self.radio.pa_enabled)

    def test_capability_flags_apollo_dual_path(self) -> None:
        self.assertTrue(
            self.radio.capabilities.pa_enable_uses_apollo_i2c)

    def test_set_pushes_persists_signals_idempotent(self) -> None:
        stub = _StubStream()
        self.radio._stream = stub
        seen: list[bool] = []
        self.radio.pa_enabled_changed.connect(seen.append)
        self.radio.set_pa_enabled(True)
        self.assertTrue(self.radio.pa_enabled)
        self.assertEqual(stub.pa_calls, [True])
        self.radio.set_pa_enabled(True)            # idempotent
        self.radio.set_pa_enabled(False)
        self.assertEqual(stub.pa_calls, [True, False])
        self.assertEqual(seen, [True, False])
        from PySide6.QtCore import QSettings
        self.assertFalse(
            QSettings("N8SDR", "Lyra").value("tx/pa_enabled", True)
            in (True, "true", "1"))

    def test_safety_standdown_auto_disarms_pa(self) -> None:
        self.radio.set_pa_enabled(True)
        self.assertTrue(self.radio.pa_enabled)
        self.radio.force_release_all()             # §15.20 / safety
        self.assertFalse(self.radio.pa_enabled)    # auto-disarmed

    def test_tx_timeout_fire_auto_disarms_pa(self) -> None:
        self.radio.set_pa_enabled(True)
        self.radio.set_mox(True)
        self.radio._on_tx_timeout_fired()          # expiry path
        self.assertFalse(self.radio.pa_enabled)

    def test_autoload_default_off_then_restore(self) -> None:
        from PySide6.QtCore import QSettings
        QSettings("N8SDR", "Lyra").remove("tx/pa_enabled")
        self.radio.autoload_pa_enabled_setting()
        self.assertFalse(self.radio.pa_enabled)    # default OFF
        QSettings("N8SDR", "Lyra").setValue("tx/pa_enabled", True)
        self.radio.autoload_pa_enabled_setting()
        self.assertTrue(self.radio.pa_enabled)
        QSettings("N8SDR", "Lyra").setValue("tx/pa_enabled", False)


if __name__ == "__main__":
    unittest.main()
