"""Phase 3.D v0.1 -- SUB toggle + VFO transfer helpers + per-RX
volume / mute UI.

Per consensus plan §6.7 / §6.8 working-group decisions: the
SUB button enables RX2 (rx2_enabled dispatch axis), A->B / B->A
/ Swap copy state between VFOs (full state when SUB on, freq-
only otherwise), and Vol-A / Vol-B / Mute-A / Mute-B sliders
surface on the DSP+Audio panel only when SUB is enabled.

Run from repo root::

    python -m unittest tests.ui.test_phase3d_sub_and_per_rx_vol -v
"""
from __future__ import annotations

import sys
import unittest


class Phase3dPerRxVolumeTest(unittest.TestCase):
    """``set_volume`` / ``set_muted`` accept ``target_rx``."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_set_volume_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio._volume
        self.radio.set_volume(0.30, target_rx=2)
        self.assertAlmostEqual(self.radio._volume_rx2, 0.30)
        self.assertAlmostEqual(self.radio._volume, orig_rx1)

    def test_set_volume_emits_rx2_signal(self) -> None:
        seen: list[float] = []
        self.radio.volume_changed_rx2.connect(seen.append)
        self.radio.set_volume(0.65, target_rx=2)
        self.assertTrue(any(abs(v - 0.65) < 1e-6 for v in seen))

    def test_set_volume_default_targets_focused_rx(self) -> None:
        self.radio.set_focused_rx(2)
        orig_rx1 = self.radio._volume
        self.radio.set_volume(0.42)
        self.assertAlmostEqual(self.radio._volume_rx2, 0.42)
        self.assertAlmostEqual(self.radio._volume, orig_rx1)

    def test_set_muted_target_rx2_writes_rx2_only(self) -> None:
        orig_rx1 = self.radio._muted
        self.radio.set_muted(True, target_rx=2)
        self.assertTrue(self.radio._muted_rx2)
        self.assertEqual(self.radio._muted, orig_rx1)

    def test_set_muted_emits_rx2_signal(self) -> None:
        seen: list[bool] = []
        self.radio.muted_changed_rx2.connect(seen.append)
        self.radio.set_muted(True, target_rx=2)
        self.assertIn(True, seen)

    def test_toggle_muted_target_rx2(self) -> None:
        self.radio.set_muted(False, target_rx=2)
        self.radio.toggle_muted(target_rx=2)
        self.assertTrue(self.radio._muted_rx2)
        self.radio.toggle_muted(target_rx=2)
        self.assertFalse(self.radio._muted_rx2)

    def test_query_accessors(self) -> None:
        self.radio.set_volume(0.2, target_rx=0)
        self.radio.set_volume(0.7, target_rx=2)
        self.assertAlmostEqual(self.radio.volume_for_rx(0), 0.2)
        self.assertAlmostEqual(self.radio.volume_for_rx(2), 0.7)
        self.radio.set_muted(True, target_rx=0)
        self.radio.set_muted(False, target_rx=2)
        self.assertTrue(self.radio.muted_for_rx(0))
        self.assertFalse(self.radio.muted_for_rx(2))


class Phase3dVfoTransferTest(unittest.TestCase):
    """A->B / B->A / Swap with and without SUB enabled."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_a_to_b_freq_only_when_sub_off(self) -> None:
        self.radio.set_rx2_enabled(False)
        self.radio.set_freq_hz(14_205_000)
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_mode("LSB", target_rx=2)
        self.radio.vfo_a_to_b()
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)
        # Mode of RX2 must NOT have flipped to USB.
        self.assertEqual(self.radio._mode_rx2, "LSB")

    def test_a_to_b_full_state_when_sub_on(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.radio.set_freq_hz(7_074_000)
        self.radio.set_mode("AM", target_rx=0)
        self.radio.set_rx_bw("AM", 6000, target_rx=0)
        self.radio.vfo_a_to_b()
        self.assertEqual(self.radio.rx2_freq_hz, 7_074_000)
        self.assertEqual(self.radio._mode_rx2, "AM")
        self.assertEqual(self.radio._rx_bw_by_mode_rx2.get("AM"), 6000)

    def test_b_to_a_freq_only_when_sub_off(self) -> None:
        self.radio.set_rx2_enabled(False)
        self.radio.set_rx2_freq_hz(10_000_000)
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_mode("LSB", target_rx=2)
        self.radio.vfo_b_to_a()
        self.assertEqual(self.radio.freq_hz, 10_000_000)
        self.assertEqual(self.radio._mode, "USB")

    def test_b_to_a_full_state_when_sub_on(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.radio.set_rx2_freq_hz(21_300_000)
        self.radio.set_mode("CWU", target_rx=2)
        self.radio.set_rx_bw("CWU", 500, target_rx=2)
        self.radio.vfo_b_to_a()
        self.assertEqual(self.radio.freq_hz, 21_300_000)
        self.assertEqual(self.radio._mode, "CWU")
        self.assertEqual(self.radio.rx_bw_for("CWU"), 500)

    def test_swap_freq_only_when_sub_off(self) -> None:
        self.radio.set_rx2_enabled(False)
        self.radio.set_freq_hz(14_205_000)
        self.radio.set_rx2_freq_hz(7_074_000)
        a_mode = self.radio._mode
        b_mode = self.radio._mode_rx2
        self.radio.vfo_swap()
        self.assertEqual(self.radio.freq_hz, 7_074_000)
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)
        # Modes unchanged because SUB was off.
        self.assertEqual(self.radio._mode, a_mode)
        self.assertEqual(self.radio._mode_rx2, b_mode)

    def test_swap_full_state_when_sub_on(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.radio.set_freq_hz(14_205_000)
        self.radio.set_mode("USB", target_rx=0)
        self.radio.set_rx_bw("USB", 2700, target_rx=0)
        self.radio.set_rx2_freq_hz(7_074_000)
        self.radio.set_mode("LSB", target_rx=2)
        self.radio.set_rx_bw("LSB", 2400, target_rx=2)
        self.radio.vfo_swap()
        self.assertEqual(self.radio.freq_hz, 7_074_000)
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)
        self.assertEqual(self.radio._mode, "LSB")
        self.assertEqual(self.radio._mode_rx2, "USB")
        self.assertEqual(self.radio.rx_bw_for("LSB"), 2400)
        self.assertEqual(self.radio._rx_bw_by_mode_rx2.get("USB"), 2700)


class Phase3dModeFilterPanelTest(unittest.TestCase):
    """SUB + 1->2 / 2->1 / Sync buttons.

    Phase 3.E.1 hotfix v0.10 (2026-05-12): widgets moved from
    ModeFilterPanel to TuningPanel's Row 3 alongside CW Pitch.
    Class name kept for git-history continuity; ``setUp`` now
    instantiates TuningPanel.  Swap button renamed "⇄" -> "Sync".
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import TuningPanel
        self.radio = Radio()
        self.panel = TuningPanel(self.radio)

    def test_sub_button_present(self) -> None:
        self.assertTrue(hasattr(self.panel, "sub_btn"))
        self.assertEqual(self.panel.sub_btn.text(), "SUB")
        self.assertTrue(self.panel.sub_btn.isCheckable())

    def test_sub_button_toggles_dispatch_state(self) -> None:
        self.assertFalse(self.radio.dispatch_state.rx2_enabled)
        self.panel.sub_btn.setChecked(True)
        self.assertTrue(self.radio.dispatch_state.rx2_enabled)
        self.panel.sub_btn.setChecked(False)
        self.assertFalse(self.radio.dispatch_state.rx2_enabled)

    def test_sub_button_mirrors_external_state(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.assertTrue(self.panel.sub_btn.isChecked())
        self.radio.set_rx2_enabled(False)
        self.assertFalse(self.panel.sub_btn.isChecked())

    def test_transfer_buttons_present(self) -> None:
        self.assertTrue(hasattr(self.panel, "ab_btn"))
        self.assertTrue(hasattr(self.panel, "ba_btn"))
        self.assertTrue(hasattr(self.panel, "swap_btn"))

    def test_swap_button_keeps_arrow_glyph(self) -> None:
        """Phase 3.E.1 hotfix v0.11 (2026-05-12): the operator's
        "Sync" phrasing in their mockup was descriptive text only
        ("CW Pitch Sub 1-2 2-1 Sync") -- the button keeps the
        ``⇄`` glyph it has worn since Phase 3.D."""
        self.assertEqual(self.panel.swap_btn.text(), "⇄")

    def test_ab_button_click_invokes_radio_helper(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.radio.set_freq_hz(14_205_000)
        self.panel.ab_btn.click()
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)

    def test_swap_button_click_invokes_radio_helper(self) -> None:
        self.radio.set_freq_hz(7_000_000)
        self.radio.set_rx2_freq_hz(14_000_000)
        self.panel.swap_btn.click()
        self.assertEqual(self.radio.freq_hz, 14_000_000)
        self.assertEqual(self.radio.rx2_freq_hz, 7_000_000)

    def test_mode_filter_panel_no_longer_has_cluster_widgets(self) -> None:
        """Regression marker -- if a refactor accidentally
        re-introduces SUB / transfer buttons on ModeFilterPanel,
        flag the duplication."""
        from lyra.ui.panels import ModeFilterPanel
        mf = ModeFilterPanel(self.radio)
        self.assertFalse(hasattr(mf, "sub_btn"))
        self.assertFalse(hasattr(mf, "ab_btn"))
        self.assertFalse(hasattr(mf, "ba_btn"))
        self.assertFalse(hasattr(mf, "swap_btn"))


class Phase3dSubMirrorTest(unittest.TestCase):
    """Phase 3.D hotfix (2026-05-12) -- on SUB rising edge,
    RX2 mirrors RX1's current volume + AF gain + mute so the
    operator's level calibration carries over.  Prevents the
    "click SUB → speaker blast" failure mode from the original
    Phase 3.D bench pass."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_sub_on_mirrors_volume(self) -> None:
        self.radio.set_volume(0.10, target_rx=0)  # very quiet
        # RX2 default = 0.5, deliberately different.
        self.radio._volume_rx2 = 0.5
        self.radio.set_rx2_enabled(True)
        self.assertAlmostEqual(self.radio._volume_rx2, 0.10,
                               msg="SUB should mirror Vol-A to Vol-B")

    def test_sub_on_mirrors_af_gain(self) -> None:
        self.radio.set_af_gain_db(5, target_rx=0)
        self.radio._af_gain_db_rx2 = 25
        self.radio.set_rx2_enabled(True)
        self.assertEqual(self.radio._af_gain_db_rx2, 5,
                         "SUB should mirror AF Gain RX1 to RX2")

    def test_sub_on_mirrors_mute(self) -> None:
        self.radio.set_muted(True, target_rx=0)
        self.radio._muted_rx2 = False
        self.radio.set_rx2_enabled(True)
        self.assertTrue(self.radio._muted_rx2)

    def test_sub_on_emits_rx2_signals(self) -> None:
        """SUB-on must emit volume + mute rx2 signals so UI binds
        update to the mirrored values."""
        self.radio.set_volume(0.20, target_rx=0)
        vol_seen: list[float] = []
        muted_seen: list[bool] = []
        self.radio.volume_changed_rx2.connect(vol_seen.append)
        self.radio.muted_changed_rx2.connect(muted_seen.append)
        self.radio.set_rx2_enabled(True)
        self.assertTrue(vol_seen, "Vol-B sibling signal must fire")
        self.assertTrue(any(abs(v - 0.20) < 1e-6 for v in vol_seen))

    def test_sub_off_does_not_mirror(self) -> None:
        """SUB rising edge mirrors; SUB falling edge leaves state
        alone so RX2's independent values persist for the next
        SUB-on."""
        self.radio.set_rx2_enabled(True)
        self.radio.set_volume(0.7, target_rx=2)
        self.radio.set_rx2_enabled(False)
        # Vol-B must NOT have been clobbered by SUB-off.
        self.assertAlmostEqual(self.radio._volume_rx2, 0.7)


class Phase3dHotfixPanRoutingTest(unittest.TestCase):
    """Phase 3.D hotfix (2026-05-12) -- the WDSP pan routing
    follows ``rx2_enabled`` rather than being unconditionally
    hard-left/right from Phase 2 stream-open."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_apply_rx2_routing_no_wdsp_channels_safe(self) -> None:
        """``_apply_rx2_routing`` is safe to call before WDSP
        channels exist (stream not started)."""
        # _wdsp_rx / _wdsp_rx2 are None pre-start; method must not
        # raise.
        self.radio._apply_rx2_routing()  # noqa: SLF001

    def test_set_rx2_enabled_invokes_routing(self) -> None:
        """Toggling SUB calls ``_apply_rx2_routing``."""
        called = {"n": 0}
        orig = self.radio._apply_rx2_routing
        def spy():
            called["n"] += 1
            orig()
        self.radio._apply_rx2_routing = spy
        self.radio.set_rx2_enabled(True)
        self.assertGreaterEqual(called["n"], 1)
        self.radio.set_rx2_enabled(False)
        self.assertGreaterEqual(called["n"], 2)


class Phase3dDspPanelConditionalUITest(unittest.TestCase):
    """DspPanel per-RX Vol/Mute UI visibility tracks SUB state."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import DspPanel
        self.radio = Radio()
        self.panel = DspPanel(self.radio)

    def test_per_rx_widgets_present(self) -> None:
        self.assertTrue(hasattr(self.panel, "vol_b_slider"))
        self.assertTrue(hasattr(self.panel, "mute_b_btn"))

    def test_vol_b_visible_when_sub_off(self) -> None:
        """Phase 3.E.1 hotfix v0.16 (2026-05-12): both Vol sliders +
        MUTE buttons are now ALWAYS visible regardless of SUB
        state.  Operator UX call: "two volume sliders and mutes
        always visible".  Vol-A binds to RX1, Vol-B to RX2 --
        direct addressing, no focus-based routing."""
        self.radio.set_rx2_enabled(False)
        self.assertFalse(self.panel.vol_b_slider.isHidden())
        self.assertFalse(self.panel.mute_b_btn.isHidden())
        self.assertEqual(self.panel.vol_label_caption.text(), "Vol-A")
        self.assertEqual(self.panel.mute_btn.text(), "MUTE")

    def test_vol_b_visible_when_sub_on(self) -> None:
        self.radio.set_rx2_enabled(True)
        self.assertFalse(self.panel.vol_b_slider.isHidden())
        self.assertFalse(self.panel.mute_b_btn.isHidden())
        self.assertEqual(self.panel.vol_label_caption.text(), "Vol-A")
        # MUTE button text stays "MUTE" in both SUB states --
        # position (right of each Vol slider) carries the
        # per-RX meaning, no -A / -B suffix needed.
        self.assertEqual(self.panel.mute_btn.text(), "MUTE")
        self.assertEqual(self.panel.mute_b_btn.text(), "MUTE")

    def test_vol_a_slider_writes_rx1(self) -> None:
        # The Vol slider always targets RX1 (target_rx=0).
        orig_rx2 = self.radio._volume_rx2
        # Use a value that produces a recognisable slider position.
        self.panel.vol_slider.setValue(50)
        # 50% slider → ((50/100)**2) * 1.0 = 0.25 multiplier.
        self.assertAlmostEqual(self.radio._volume, 0.25, places=4)
        self.assertAlmostEqual(self.radio._volume_rx2, orig_rx2)

    def test_vol_b_slider_writes_rx2(self) -> None:
        orig_rx1 = self.radio._volume
        self.panel.vol_b_slider.setValue(50)
        self.assertAlmostEqual(self.radio._volume_rx2, 0.25, places=4)
        self.assertAlmostEqual(self.radio._volume, orig_rx1)

    def test_mute_a_button_writes_rx1(self) -> None:
        orig_rx2 = self.radio._muted_rx2
        self.panel.mute_btn.setChecked(True)
        self.assertTrue(self.radio._muted)
        self.assertEqual(self.radio._muted_rx2, orig_rx2)

    def test_mute_b_button_writes_rx2(self) -> None:
        orig_rx1 = self.radio._muted
        self.panel.mute_b_btn.setChecked(True)
        self.assertTrue(self.radio._muted_rx2)
        self.assertEqual(self.radio._muted, orig_rx1)


if __name__ == "__main__":
    unittest.main()
