"""TxPanel wiring tests (v0.2.0 Phase 3 commit 3.4).

The TX panel is the first operator surface that can key the
radio.  These tests prove:

  * MOX button press/release funnels through the Radio facade
    (request_mox / release_mox) -- the bool must branch, not be
    passed positionally.
  * The button is a SLAVE of true TX state: tx_active_changed
    mirrors it (so a hardware foot-switch / any TX source keeps
    it truthful) WITHOUT re-firing request/release.
  * TUN ships disabled (no silent-dead-carrier trap until the
    tune-carrier generator lands) but present (final layout).
  * TX-drive stepper <-> Radio.set_tx_power_pct, and the
    tx_power_pct_changed mirror doesn't feed back.
"""
from __future__ import annotations

import sys
import unittest


class TxPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ui.panels import TxPanel
        self.radio = Radio()
        self.panel = TxPanel(self.radio)

    def test_mox_enabled_after_rx_release(self) -> None:
        """MOX is enabled now that the keydown chain stands the RX
        audio path down (PART B).  Before PART B this asserted
        DISABLED (the safety gate); flipped here in the same
        commit that landed RX release."""
        self.assertTrue(self.panel.mox_btn.isEnabled())

    def test_keydown_mutes_rx_without_touching_operator_mute(self) -> None:
        from lyra.ptt import PttState
        self.assertFalse(self.radio._tx_rx_muted)
        self.assertFalse(self.radio._muted)          # operator mute
        self.radio._on_tx_state_changed(True, PttState.MOX_TX)
        self.assertTrue(self.radio._tx_rx_muted)     # RX stood down
        self.assertFalse(self.radio._muted)          # operator mute UNTOUCHED

    def test_keyup_holds_gated_through_flush_then_finish_releases(self):
        """TX->RX must NOT un-gate at the hook (the wire MOX bit
        just cleared; the RX chain rings across the T/R IQ
        discontinuity).  It stays gated + flushes the RX DSP; only
        the deferred settle-end (_finish_tx_rx_resume, fired by a
        single-shot QTimer in the live app) un-gates + arms the
        cos² fade.  Keydown does not arm the fade."""
        from lyra.ptt import PttState
        resets: list[bool] = []
        self.radio._request_dsp_reset_full = (          # type: ignore
            lambda: resets.append(True))
        self.assertEqual(self.radio._tx_resume_fade_pos, -1)
        self.radio._on_tx_state_changed(True, PttState.MOX_TX)   # key down
        self.assertTrue(self.radio._tx_rx_muted)
        self.assertEqual(self.radio._tx_resume_fade_pos, -1)     # not armed
        self.assertEqual(resets, [])                             # no flush yet
        self.radio._on_tx_state_changed(False, PttState.RX)      # key up
        # Still gated + flushed; fade NOT armed (deferred to settle).
        self.assertTrue(self.radio._tx_rx_muted)
        self.assertEqual(resets, [True])                         # RX DSP flushed
        self.assertEqual(self.radio._tx_resume_fade_pos, -1)
        # Settle end (simulating the QTimer firing) un-gates + fades.
        self.radio._finish_tx_rx_resume()
        self.assertFalse(self.radio._tx_rx_muted)                # un-gated
        self.assertEqual(self.radio._tx_resume_fade_pos, 0)      # fade armed

    def test_resume_fade_ramps_then_releases(self) -> None:
        """The fade ramps 0->1 across blocks then self-disarms
        (-1), and the first resumed sample is heavily attenuated
        (curve starts at 0) so there is no abrupt edge."""
        import numpy as np
        self.radio._tx_resume_fade_pos = 0
        N = self.radio._TX_RESUME_FADE_SAMPLES
        blk = np.ones(N // 2, dtype=np.float32)
        out1 = self.radio._apply_tx_resume_fade(blk.copy())
        self.assertLess(float(out1[0]), 0.05)            # ~silent at start
        self.assertGreater(self.radio._tx_resume_fade_pos, 0)    # mid-fade
        # second block finishes the curve -> disarms
        self.radio._apply_tx_resume_fade(blk.copy())
        self.assertEqual(self.radio._tx_resume_fade_pos, -1)
        # inactive -> pass-through unchanged
        same = np.full(64, 0.7, dtype=np.float32)
        out = self.radio._apply_tx_resume_fade(same.copy())
        self.assertTrue(np.allclose(out, 0.7))

    def test_keyup_restores_rx_and_preserves_operator_mute(self) -> None:
        from lyra.ptt import PttState
        self.radio.set_muted(True)                   # operator chose mute
        self.assertTrue(self.radio._muted)
        self.radio._on_tx_state_changed(True, PttState.MOX_TX)   # key down
        self.radio._on_tx_state_changed(False, PttState.RX)      # key up
        self.radio._finish_tx_rx_resume()            # settle completes
        self.assertFalse(self.radio._tx_rx_muted)    # RX released
        self.assertTrue(self.radio._muted)           # operator mute SURVIVES

    def test_mox_button_drives_facade(self) -> None:
        calls: list[str] = []
        self.radio.request_mox = lambda: calls.append("req")   # type: ignore
        self.radio.release_mox = lambda: calls.append("rel")   # type: ignore
        self.panel.mox_btn.setChecked(True)
        self.assertEqual(calls, ["req"])
        self.panel.mox_btn.setChecked(False)
        self.assertEqual(calls, ["req", "rel"])

    def test_tx_active_mirrors_button_without_refire(self) -> None:
        calls: list[str] = []
        self.radio.request_mox = lambda: calls.append("req")   # type: ignore
        self.radio.release_mox = lambda: calls.append("rel")   # type: ignore
        # Radio declares TX from some other source (HW PTT / FSM).
        self.radio.tx_active_changed.emit(True)
        self.assertTrue(self.panel.mox_btn.isChecked())
        self.radio.tx_active_changed.emit(False)
        self.assertFalse(self.panel.mox_btn.isChecked())
        # Mirroring must NOT have called the facade (no re-fire).
        self.assertEqual(calls, [])

    def test_tun_present_but_disabled(self) -> None:
        self.assertIsNotNone(self.panel.tun_btn)
        self.assertFalse(self.panel.tun_btn.isEnabled())

    def test_drive_stepper_drives_radio(self) -> None:
        seen: list[int] = []
        self.radio.tx_power_pct_changed.connect(seen.append)
        self.panel.tx_drive_stepper.setValue(100)
        self.assertEqual(self.radio.tx_power_pct, 100)
        self.assertEqual(seen[-1], 100)

    def test_radio_change_mirrors_stepper_no_feedback(self) -> None:
        # Seed is 0 (idempotent no-op at 0); move to 100 first so
        # the subsequent ->0 is a genuine change.
        self.radio.set_tx_power_pct(100)
        seen: list[int] = []
        self.radio.tx_power_pct_changed.connect(seen.append)
        self.radio.set_tx_power_pct(0)
        self.assertEqual(int(self.panel.tx_drive_stepper.value()), 0)
        # Exactly one edge -> the mirror didn't bounce back through
        # set_tx_power_pct and re-emit.
        self.assertEqual(seen.count(0), 1)


if __name__ == "__main__":
    unittest.main()
