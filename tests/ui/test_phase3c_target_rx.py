"""Phase 3.C v0.1 -- target_rx semantics on setters + panel
focus-aware binding tests.

Per consensus plan and the working-group decision: Phase 3.A's
fan-out setters are replaced by per-target dispatch.  Each
setter accepts a ``target_rx`` parameter (0 = RX1, 2 = RX2,
None = focused RX) and only writes to that channel.

Phase 3.C panels (``ModeFilterPanel`` + ``DspPanel``) listen to
both the RX1 and the new RX2 sibling signals and re-bind their
display on ``focused_rx_changed``.

Run from repo root::

    python -m unittest tests.ui.test_phase3c_target_rx -v
"""
from __future__ import annotations

import sys
import unittest


class Phase3cSettersTest(unittest.TestCase):
    """Setter routing via ``target_rx``."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    # ── set_mode ───────────────────────────────────────────────────

    def test_set_mode_target_rx1_writes_rx1_only(self) -> None:
        orig_rx2 = self.radio._mode_rx2
        self.radio.set_mode("AM", target_rx=0)
        self.assertEqual(self.radio._mode, "AM")
        self.assertEqual(self.radio._mode_rx2, orig_rx2)

    def test_set_mode_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio._mode
        self.radio.set_mode("LSB", target_rx=2)
        self.assertEqual(self.radio._mode_rx2, "LSB")
        self.assertEqual(self.radio._mode, orig_rx1)

    def test_set_mode_none_follows_focus_rx1(self) -> None:
        self.radio.set_focused_rx(0)
        orig_rx2 = self.radio._mode_rx2
        self.radio.set_mode("CWU")
        self.assertEqual(self.radio._mode, "CWU")
        self.assertEqual(self.radio._mode_rx2, orig_rx2)

    def test_set_mode_none_follows_focus_rx2(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx1 = self.radio._mode
        self.radio.set_mode("FM")
        self.assertEqual(self.radio._mode_rx2, "FM")
        self.assertEqual(self.radio._mode, orig_rx1)

    def test_set_mode_emits_rx2_signal_for_rx2_path(self) -> None:
        seen: list[str] = []
        self.radio.mode_changed_rx2.connect(seen.append)
        self.radio.set_mode("AM", target_rx=2)
        self.assertIn("AM", seen)

    def test_set_mode_does_not_emit_rx2_signal_for_rx1_path(self) -> None:
        seen: list[str] = []
        self.radio.mode_changed_rx2.connect(seen.append)
        self.radio.set_mode("AM", target_rx=0)
        self.assertEqual(seen, [])

    # ── set_rx_bw ──────────────────────────────────────────────────

    def test_set_rx_bw_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio.rx_bw_for("USB")
        self.radio.set_rx_bw("USB", 1800, target_rx=2)
        self.assertEqual(self.radio._rx_bw_by_mode_rx2.get("USB"), 1800)
        self.assertEqual(self.radio.rx_bw_for("USB"), orig_rx1)

    def test_set_rx_bw_emits_rx2_signal(self) -> None:
        seen: list[tuple] = []
        self.radio.rx_bw_changed_rx2.connect(
            lambda m, bw: seen.append((m, bw)))
        self.radio.set_rx_bw("USB", 1500, target_rx=2)
        self.assertIn(("USB", 1500), seen)

    # ── set_af_gain_db ─────────────────────────────────────────────

    def test_set_af_gain_db_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio._af_gain_db
        self.radio.set_af_gain_db(42, target_rx=2)
        self.assertEqual(self.radio._af_gain_db_rx2, 42)
        self.assertEqual(self.radio._af_gain_db, orig_rx1)

    def test_set_af_gain_db_emits_rx2_signal(self) -> None:
        seen: list[int] = []
        self.radio.af_gain_db_changed_rx2.connect(seen.append)
        self.radio.set_af_gain_db(33, target_rx=2)
        self.assertIn(33, seen)

    # ── set_agc_profile ────────────────────────────────────────────

    def test_set_agc_profile_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio._agc_profile
        self.radio.set_agc_profile("fast", target_rx=2)
        self.assertEqual(self.radio._agc_profile_rx2, "fast")
        self.assertEqual(self.radio._agc_profile, orig_rx1)

    def test_set_agc_profile_emits_rx2_signal(self) -> None:
        seen: list[str] = []
        self.radio.agc_profile_changed_rx2.connect(seen.append)
        self.radio.set_agc_profile("slow", target_rx=2)
        self.assertIn("slow", seen)

    # ── set_agc_threshold ──────────────────────────────────────────

    def test_set_agc_threshold_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio._agc_target
        self.radio.set_agc_threshold(-95.0, target_rx=2)
        self.assertAlmostEqual(self.radio._agc_target_rx2, -95.0)
        self.assertEqual(self.radio._agc_target, orig_rx1)

    def test_set_agc_threshold_emits_rx2_signal(self) -> None:
        seen: list[float] = []
        self.radio.agc_threshold_changed_rx2.connect(seen.append)
        self.radio.set_agc_threshold(-110.0, target_rx=2)
        self.assertTrue(any(abs(v - -110.0) < 0.01 for v in seen))

    def test_auto_set_agc_threshold_pushes_to_both_channels(self) -> None:
        """``auto_set_agc_threshold`` runs a single noise-floor
        measurement and pushes to BOTH channels (single panadapter
        covers both until Phase 4)."""
        # Seed noise floor so the auto-tracker has something usable.
        self.radio._noise_floor_db = -125.0
        seen_rx1: list[float] = []
        seen_rx2: list[float] = []
        self.radio.agc_threshold_changed.connect(seen_rx1.append)
        self.radio.agc_threshold_changed_rx2.connect(seen_rx2.append)
        target = self.radio.auto_set_agc_threshold(margin_db=18.0)
        self.assertAlmostEqual(self.radio._agc_target, target)
        self.assertAlmostEqual(self.radio._agc_target_rx2, target)
        self.assertTrue(seen_rx1)
        self.assertTrue(seen_rx2)


class Phase3cQueryAccessorsTest(unittest.TestCase):
    """Per-RX query helpers used by panels."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_mode_for_rx_returns_per_target_state(self) -> None:
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_mode("LSB", target_rx=2)
        self.assertEqual(self.radio.mode_for_rx(0), "USB")
        self.assertEqual(self.radio.mode_for_rx(2), "LSB")

    def test_mode_for_rx_default_follows_focus(self) -> None:
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_mode("AM", target_rx=2)
        self.radio.set_focused_rx(0)
        self.assertEqual(self.radio.mode_for_rx(), "USB")
        self.radio.set_focused_rx(2)
        self.assertEqual(self.radio.mode_for_rx(), "AM")

    def test_af_gain_db_for_rx_returns_per_target_state(self) -> None:
        self.radio.set_af_gain_db(15, target_rx=0)
        self.radio.set_af_gain_db(45, target_rx=2)
        self.assertEqual(self.radio.af_gain_db_for_rx(0), 15)
        self.assertEqual(self.radio.af_gain_db_for_rx(2), 45)

    def test_agc_profile_for_rx(self) -> None:
        self.radio.set_agc_profile("fast", target_rx=0)
        self.radio.set_agc_profile("slow", target_rx=2)
        self.assertEqual(self.radio.agc_profile_for_rx(0), "fast")
        self.assertEqual(self.radio.agc_profile_for_rx(2), "slow")

    def test_rx_bw_for_rx(self) -> None:
        self.radio.set_rx_bw("USB", 2400, target_rx=0)
        self.radio.set_rx_bw("USB", 1800, target_rx=2)
        self.assertEqual(self.radio.rx_bw_for_rx("USB", 0), 2400)
        self.assertEqual(self.radio.rx_bw_for_rx("USB", 2), 1800)


class Phase3cModeFilterPanelTest(unittest.TestCase):
    """``ModeFilterPanel`` rebinds to focused RX state."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import ModeFilterPanel
        self.radio = Radio()
        self.panel = ModeFilterPanel(self.radio)

    def test_initial_mode_combo_reads_rx1(self) -> None:
        self.assertEqual(
            self.panel.mode_combo.currentText(),
            self.radio.mode_for_rx(0),
        )

    def test_focus_switch_to_rx2_updates_mode_combo(self) -> None:
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_mode("AM", target_rx=2)
        self.radio.set_focused_rx(2)
        self.assertEqual(self.panel.mode_combo.currentText(), "AM")
        self.radio.set_focused_rx(0)
        self.assertEqual(self.panel.mode_combo.currentText(), "USB")

    def test_combo_edit_routes_to_focused_rx(self) -> None:
        # Focus = RX2 → mode combo edit should write to RX2 only.
        self.radio.set_focused_rx(2)
        original_rx1 = self.radio._mode
        self.panel.mode_combo.setCurrentText("AM")
        self.assertEqual(self.radio._mode_rx2, "AM")
        self.assertEqual(self.radio._mode, original_rx1)


class Phase3cDspPanelTest(unittest.TestCase):
    """``DspPanel`` rebinds AGC + AF Gain to focused RX state."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import DspPanel
        self.radio = Radio()
        self.panel = DspPanel(self.radio)

    # §15.17/§15.24: AF Gain QSlider was replaced by a dB
    # StepperReadout (af_gain_stepper).  .value() now returns
    # float dB; int()-wrap for the int-dB comparisons.

    def test_af_gain_stepper_initial_reads_rx1(self) -> None:
        self.assertEqual(
            int(self.panel.af_gain_stepper.value()),
            int(self.radio.af_gain_db_for_rx(0)),
        )

    def test_focus_switch_updates_af_gain_stepper(self) -> None:
        self.radio.set_af_gain_db(20, target_rx=0)
        self.radio.set_af_gain_db(50, target_rx=2)
        self.radio.set_focused_rx(2)
        self.assertEqual(int(self.panel.af_gain_stepper.value()), 50)
        self.radio.set_focused_rx(0)
        self.assertEqual(int(self.panel.af_gain_stepper.value()), 20)

    def test_af_gain_stepper_writes_to_focused_rx(self) -> None:
        self.radio.set_focused_rx(2)
        original_rx1 = self.radio._af_gain_db
        self.panel.af_gain_stepper.setValue(37)
        self.assertEqual(self.radio._af_gain_db_rx2, 37)
        self.assertEqual(self.radio._af_gain_db, original_rx1)


if __name__ == "__main__":
    unittest.main()
