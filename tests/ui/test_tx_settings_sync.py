"""TxSettingsTab <-> TxPanel sync (v0.2.0 Phase 3 commit 3.4).

Both the dockable TX panel and the Settings -> TX power section
expose a TX-drive stepper.  They share ONE Radio setter/signal
(set_tx_power_pct / tx_power_pct_changed); moving either must move
both, with no feedback loop, and the Settings tab must contain no
inert (empty) group box beyond the one real section.
"""
from __future__ import annotations

import sys
import unittest


class TxSettingsSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import TxPanel
        from lyra.ui.settings_dialog import TxSettingsTab
        self.radio = Radio()
        self.panel = TxPanel(self.radio)
        self.tab = TxSettingsTab(self.radio)

    def test_panel_change_propagates_to_settings(self) -> None:
        self.panel.tx_drive_stepper.setValue(100)
        self.assertEqual(
            int(self.tab.tx_drive_stepper.value()), 100)

    def test_settings_change_propagates_to_panel(self) -> None:
        self.tab.tx_drive_stepper.setValue(0)
        self.assertEqual(
            int(self.panel.tx_drive_stepper.value()), 0)

    def test_no_feedback_loop(self) -> None:
        seen: list[int] = []
        self.radio.tx_power_pct_changed.connect(seen.append)
        self.panel.tx_drive_stepper.setValue(100)
        # One logical change -> one emit (the Settings mirror's
        # guarded setValue must not bounce back and re-emit).
        self.assertEqual(seen.count(100), 1)

    def test_settings_tab_has_no_inert_groupbox(self) -> None:
        from PySide6.QtWidgets import QGroupBox
        boxes = self.tab.findChildren(QGroupBox)
        # Exactly the one real "TX Power & Drive" section -- future
        # sections are comment anchors, not empty boxes (no-inert-UI).
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].title(), "TX Power & Drive")


if __name__ == "__main__":
    unittest.main()
