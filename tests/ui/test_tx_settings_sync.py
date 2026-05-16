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

    def test_settings_tab_sections_are_real_no_inert_ui(self) -> None:
        from PySide6.QtWidgets import (
            QGroupBox, QSpinBox, QCheckBox)
        from lyra.ui.widgets.stepper_readout import StepperReadout
        boxes = {b.title(): b for b in
                 self.tab.findChildren(QGroupBox)}
        # Exactly the two shipped sections -- both functional;
        # later sections remain comment anchors, not empty boxes
        # (the no-inert-UI rule).
        self.assertEqual(set(boxes), {"TX Power & Drive", "TX Safety"})
        # TX Power & Drive carries a live drive control.
        self.assertTrue(boxes["TX Power & Drive"].findChildren(
            StepperReadout))
        # TX Safety carries a live timeout spin + bypass checkbox.
        self.assertTrue(boxes["TX Safety"].findChildren(QSpinBox))
        self.assertTrue(boxes["TX Safety"].findChildren(QCheckBox))

    def test_tx_timeout_settings_round_trip(self) -> None:
        # Spin/checkbox <-> Radio, both directions, guarded.
        self.tab.tx_timeout_spin.setValue(15)
        self.assertEqual(self.radio.tx_timeout_seconds, 15 * 60)
        self.radio.set_tx_timeout_bypass(True)
        self.assertTrue(self.tab.tx_timeout_bypass_chk.isChecked())
        # bypass disables the (now-meaningless) minutes spin
        self.assertFalse(self.tab.tx_timeout_spin.isEnabled())
        self.radio.set_tx_timeout_bypass(False)


if __name__ == "__main__":
    unittest.main()
