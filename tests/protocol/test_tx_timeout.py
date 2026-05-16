"""TX safety-timeout tests (v0.2.0 Phase 3 commit 3.5, §15.20).

Host-side single-shot timer armed on the MOX keydown edge /
cancelled on keyup (inside ``Radio.set_mox`` -- the single MOX
funnel).  On expiry it force-releases TX + toasts.  Bypassable
for long AM/CW.  Range clamped 60..1200 s (1..20 min).
"""
from __future__ import annotations

import sys
import unittest


class TxTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_defaults(self) -> None:
        self.assertEqual(self.radio.tx_timeout_seconds, 600)
        self.assertFalse(self.radio.tx_timeout_bypass)
        self.assertFalse(self.radio._tx_timeout_timer.isActive())

    def test_arms_on_keydown_cancels_on_keyup(self) -> None:
        self.radio.set_mox(True)
        self.assertTrue(self.radio._tx_timeout_timer.isActive())
        self.radio.set_mox(False)
        self.assertFalse(self.radio._tx_timeout_timer.isActive())

    def test_bypass_never_arms(self) -> None:
        self.radio.set_tx_timeout_bypass(True)
        self.radio.set_mox(True)
        self.assertFalse(self.radio._tx_timeout_timer.isActive())
        self.radio.set_mox(False)

    def test_bypass_toggle_mid_tx_cancels_then_rearms(self) -> None:
        self.radio.set_mox(True)
        self.assertTrue(self.radio._tx_timeout_timer.isActive())
        self.radio.set_tx_timeout_bypass(True)        # mid-TX bypass
        self.assertFalse(self.radio._tx_timeout_timer.isActive())
        self.radio.set_tx_timeout_bypass(False)       # un-bypass mid-TX
        self.assertTrue(self.radio._tx_timeout_timer.isActive())
        self.radio.set_mox(False)

    def test_set_seconds_clamps_persists_signals(self) -> None:
        seen: list[int] = []
        self.radio.tx_timeout_seconds_changed.connect(seen.append)
        self.radio.set_tx_timeout_seconds(30)         # < 60 -> 60
        self.assertEqual(self.radio.tx_timeout_seconds, 60)
        self.radio.set_tx_timeout_seconds(99999)      # > 1200 -> 1200
        self.assertEqual(self.radio.tx_timeout_seconds, 1200)
        self.radio.set_tx_timeout_seconds(1200)       # idempotent
        self.assertEqual(seen, [60, 1200])
        from PySide6.QtCore import QSettings
        self.assertEqual(
            int(QSettings("N8SDR", "Lyra").value("tx/timeout_seconds")),
            1200)

    def test_seconds_change_mid_tx_rearms(self) -> None:
        self.radio.set_mox(True)
        self.radio.set_tx_timeout_seconds(120)        # 2 min
        self.assertTrue(self.radio._tx_timeout_timer.isActive())
        # remaining interval reflects the new limit
        self.assertLessEqual(
            self.radio._tx_timeout_timer.remainingTime(), 120_000)
        self.radio.set_mox(False)

    def test_fire_force_releases_and_toasts(self) -> None:
        from lyra.ptt import PttState
        calls: list[bool] = []
        self.radio.force_release_all = (              # type: ignore
            lambda: calls.append(True))
        toasts: list[str] = []
        self.radio.status_message.connect(
            lambda msg, ms: toasts.append(msg))
        self.radio.set_mox(True)
        self.radio._on_tx_timeout_fired()             # simulate expiry
        self.assertEqual(calls, [True])               # stood down
        self.assertTrue(toasts and "timeout" in toasts[-1].lower())

    def test_autoload_restores(self) -> None:
        from PySide6.QtCore import QSettings
        s = QSettings("N8SDR", "Lyra")
        s.setValue("tx/timeout_seconds", 300)
        s.setValue("tx/timeout_bypass", True)
        seen_s: list[int] = []
        seen_b: list[bool] = []
        self.radio.tx_timeout_seconds_changed.connect(seen_s.append)
        self.radio.tx_timeout_bypass_changed.connect(seen_b.append)
        self.radio.autoload_tx_timeout_settings()
        self.assertEqual(self.radio.tx_timeout_seconds, 300)
        self.assertTrue(self.radio.tx_timeout_bypass)
        self.assertEqual(seen_s[-1], 300)
        self.assertTrue(seen_b[-1])
        s.setValue("tx/timeout_bypass", False)        # tidy for other tests


if __name__ == "__main__":
    unittest.main()
