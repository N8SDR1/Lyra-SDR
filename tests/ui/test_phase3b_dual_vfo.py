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
        """Phase 3.A default focus = RX1.  RX1's LED has the orange
        active border; RX2's has the transparent inactive border."""
        rx1_style = self.panel.freq_display.styleSheet()
        rx2_style = self.panel.freq_display_rx2.styleSheet()
        # Active border style contains the orange hex.
        self.assertIn("#c2702a", rx1_style)
        # Inactive border is transparent.
        self.assertIn("transparent", rx2_style)

    def test_focus_change_to_rx2_moves_border(self) -> None:
        self.radio.set_focused_rx(2)
        rx1_style = self.panel.freq_display.styleSheet()
        rx2_style = self.panel.freq_display_rx2.styleSheet()
        self.assertIn("transparent", rx1_style)
        self.assertIn("#c2702a", rx2_style)

    def test_focus_change_back_to_rx1_restores_border(self) -> None:
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(0)
        rx1_style = self.panel.freq_display.styleSheet()
        rx2_style = self.panel.freq_display_rx2.styleSheet()
        self.assertIn("#c2702a", rx1_style)
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


if __name__ == "__main__":
    unittest.main()
