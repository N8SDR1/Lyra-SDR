"""Phase 3.B v0.1 — dual VFO LED + focus model wiring tests.

Per consensus plan §6.1 (hybrid focus model) and §6.7 (active-VFO
indicators).  Phase 3.B replaces the static RX2 LED mockup with a
live FrequencyDisplay wired to ``radio.rx2_freq_hz`` + the
``rx2_freq_changed`` signal, adds click-to-focus on both VFO
LEDs, adds the orange focus-border indicator that reacts to
``Radio.focused_rx_changed``, and wires Ctrl+1 / Ctrl+2 hotkeys
in ``app.py``.

This test module verifies the wiring works end-to-end at the
``TuningPanel`` level (we don't construct the full MainWindow
because the hotkeys live there and need Qt event-loop integration
for live keyboard testing; that's a manual / smoke-test concern).

Run from repo root::

    python -m unittest tests.ui.test_phase3b_dual_vfo -v
"""
from __future__ import annotations

import sys
import unittest


class Phase3bDualVfoTest(unittest.TestCase):
    """Live RX2 LED + focus model wiring."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import TuningPanel
        self.radio = Radio()
        self.panel = TuningPanel(self.radio)

    # ── Live RX2 LED ───────────────────────────────────────────────

    def test_rx2_led_is_enabled(self) -> None:
        """The RX2 LED is no longer a disabled mockup -- it's a
        live FrequencyDisplay in Phase 3.B."""
        # ``set_vfo_enabled(True, "")`` sets ``_enabled = True``.
        self.assertTrue(
            self.panel.freq_display_rx2._enabled,
            "RX2 LED should be enabled in Phase 3.B (was the Phase 1 "
            "static mockup before).",
        )

    def test_rx2_led_initial_freq_matches_radio(self) -> None:
        """LED initial freq reads from ``Radio.rx2_freq_hz``."""
        self.assertEqual(
            self.panel.freq_display_rx2.freq_hz,
            int(self.radio.rx2_freq_hz),
        )

    def test_radio_rx2_freq_change_updates_led(self) -> None:
        """Setting RX2 freq on Radio propagates to the LED via the
        ``rx2_freq_changed`` signal."""
        self.radio.set_rx2_freq_hz(14_205_000)
        self.assertEqual(self.panel.freq_display_rx2.freq_hz, 14_205_000)
        self.radio.set_rx2_freq_hz(10_000_000)
        self.assertEqual(self.panel.freq_display_rx2.freq_hz, 10_000_000)

    def test_led_freq_changed_updates_radio_rx2(self) -> None:
        """Operator-facing edit on the LED emits ``freq_changed`` ->
        ``radio.set_rx2_freq_hz`` propagates back."""
        # Simulate the LED emitting a freq change (as if the operator
        # had double-clicked + typed a new freq).
        self.panel.freq_display_rx2.freq_changed.emit(7_074_000)
        self.assertEqual(self.radio.rx2_freq_hz, 7_074_000)

    # ── Focus model + visual border ─────────────────────────────────

    def test_initial_focus_indicator_on_rx1(self) -> None:
        """Phase 3.A default focus = RX1.  RX1's LED has the GREEN
        active border; RX2's has the transparent inactive border.
        Color changed orange -> green per operator UX 2026-05-12;
        red is reserved for TX (see led_freq.set_tx_active)."""
        rx1_style = self.panel.freq_display.styleSheet()
        rx2_style = self.panel.freq_display_rx2.styleSheet()
        # Active border style contains the green hex.
        self.assertIn("#00e676", rx1_style)
        # Inactive border is transparent.
        self.assertIn("transparent", rx2_style)

    def test_focus_change_to_rx2_moves_border(self) -> None:
        self.radio.set_focused_rx(2)
        rx1_style = self.panel.freq_display.styleSheet()
        rx2_style = self.panel.freq_display_rx2.styleSheet()
        self.assertIn("transparent", rx1_style)
        self.assertIn("#00e676", rx2_style)

    def test_focus_change_back_to_rx1_restores_border(self) -> None:
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(0)
        rx1_style = self.panel.freq_display.styleSheet()
        rx2_style = self.panel.freq_display_rx2.styleSheet()
        self.assertIn("#00e676", rx1_style)
        self.assertIn("transparent", rx2_style)

    # ── Click-to-focus ──────────────────────────────────────────────

    def test_click_rx2_led_focuses_rx2(self) -> None:
        """Press event on RX2 LED first calls ``set_focused_rx(2)``."""
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        # Construct a simulated left-button press event over the LED.
        evt = QMouseEvent(
            QEvent.MouseButtonPress,
            QPointF(10, 10),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        # Pre-condition: focus on RX1.
        self.assertEqual(self.radio.focused_rx, 0)
        self.panel.freq_display_rx2.mousePressEvent(evt)
        # Click should have flipped focus to RX2.
        self.assertEqual(self.radio.focused_rx, 2)

    def test_click_rx1_led_focuses_rx1(self) -> None:
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        # Set focus to RX2 first so the click is a real transition.
        self.radio.set_focused_rx(2)
        self.assertEqual(self.radio.focused_rx, 2)
        evt = QMouseEvent(
            QEvent.MouseButtonPress,
            QPointF(10, 10),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        self.panel.freq_display.mousePressEvent(evt)
        self.assertEqual(self.radio.focused_rx, 0)


class Phase3eFocusAndTxColorTest(unittest.TestCase):
    """Phase 3.E hook (2026-05-12) -- ``FrequencyDisplay.set_tx_active
    (bool)`` is wired now so the API surface is stable through
    Phase 3.E TX integration.  The MOX state machine will hook this
    when TX work begins; today the method is callable but no live
    caller invokes it."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.ui.led_freq import FrequencyDisplay
        self.led = FrequencyDisplay()

    def test_tx_active_default_false(self) -> None:
        self.assertFalse(self.led._tx_active)

    def test_set_tx_active_toggles_flag(self) -> None:
        self.led.set_tx_active(True)
        self.assertTrue(self.led._tx_active)
        self.led.set_tx_active(False)
        self.assertFalse(self.led._tx_active)

    def test_set_tx_active_idempotent(self) -> None:
        """Repeat True call doesn't no-op-reset state."""
        self.led.set_tx_active(True)
        self.led.set_tx_active(True)
        self.assertTrue(self.led._tx_active)

    def test_focus_independent_of_tx(self) -> None:
        """Focus + TX are independent state flags so the paintEvent
        can pick precedence (red TX > green focus)."""
        self.led.set_focus_active(True)
        self.led.set_tx_active(True)
        self.assertTrue(self.led._focus_active)
        self.assertTrue(self.led._tx_active)
        self.led.set_tx_active(False)
        self.assertTrue(self.led._focus_active)
        self.assertFalse(self.led._tx_active)


class Phase3dPerVfoControlsTest(unittest.TestCase):
    """Phase 3.D cleanup -- per-VFO MHz + Step + Mode controls
    symmetric under each LED."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import TuningPanel
        self.radio = Radio()
        self.panel = TuningPanel(self.radio)

    def test_per_vfo_widgets_present(self) -> None:
        """Phase 3.E.1 hotfix v0.10 (2026-05-12): per-VFO MHz
        spinners removed -- the LED's double-click-to-edit covers
        direct freq entry.  Step + Mode combos remain under each
        LED."""
        # RX1 side.
        self.assertTrue(hasattr(self.panel, "step_combo"))
        self.assertTrue(hasattr(self.panel, "vfo_mode_combo"))
        # RX2 side.
        self.assertTrue(hasattr(self.panel, "step_combo_rx2"))
        self.assertTrue(hasattr(self.panel, "vfo_mode_combo_rx2"))
        # Per the v0.10 cleanup, the MHz spinners should NOT exist.
        self.assertFalse(hasattr(self.panel, "freq_spin"))
        self.assertFalse(hasattr(self.panel, "freq_spin_rx2"))

    def test_rx2_mode_combo_writes_rx2(self) -> None:
        orig_rx1 = self.radio._mode
        self.panel.vfo_mode_combo_rx2.setCurrentText("AM")
        self.assertEqual(self.radio._mode_rx2, "AM")
        self.assertEqual(self.radio._mode, orig_rx1)

    def test_rx1_mode_combo_writes_rx1(self) -> None:
        orig_rx2 = self.radio._mode_rx2
        self.panel.vfo_mode_combo.setCurrentText("LSB")
        self.assertEqual(self.radio._mode, "LSB")
        self.assertEqual(self.radio._mode_rx2, orig_rx2)


if __name__ == "__main__":
    unittest.main()
