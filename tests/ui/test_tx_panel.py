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

    def test_auto_lna_frozen_during_tx(self) -> None:
        """Auto-LNA must NOT touch the RX front-end gain while
        transmitting (operator-reported 2026-05-16: LNA was
        adjusting in TX)."""
        self.radio._lna_auto = True
        self.radio._lna_peaks = [0.99, 0.99, 0.99]   # 'overload'
        g0 = self.radio.gain_db
        self.radio.set_mox(True)                      # keyed
        self.radio._adjust_lna_auto()                 # would back off
        self.assertEqual(self.radio.gain_db, g0)      # frozen in TX
        self.radio.set_mox(False)                     # back to RX
        self.radio._adjust_lna_auto()                 # now allowed
        self.assertLess(self.radio.gain_db, g0)       # backed off

    def test_keydown_stops_rx_channel_keyup_starts_it(self) -> None:
        """Thetis-faithful: keydown STOPS the WDSP RX channel
        (blocking flush) so it never processes the keyed period;
        the keyup hook (which the FSM fires only at _end_keyup,
        AFTER the HW-T/R settle) STARTS it again on clean antenna
        IQ.  No envelope fade machinery -- the channel itself is
        off through TX."""
        from lyra.ptt import PttState
        reqs: list[bool] = []
        self.radio._request_rx_channel = (              # type: ignore
            lambda on: reqs.append(on))
        self.radio._on_tx_state_changed(True, PttState.MOX_TX)   # key down
        self.assertTrue(self.radio._tx_rx_muted)        # backup gate on
        self.assertEqual(reqs, [False])                 # RX channel STOPPED
        self.radio._on_tx_state_changed(False, PttState.RX)      # key up
        self.assertFalse(self.radio._tx_rx_muted)       # gate released
        self.assertEqual(reqs, [False, True])           # RX channel STARTED

    def test_keyup_hook_fires_after_ptt_out_settle_not_before(self):
        """The FSM must call the keyup hook from _end_keyup (past
        the ptt_out_delay HW-T/R settle), NOT from _clear_mox_tail
        (before it).  With a non-zero ptt_out_delay the QTimer
        won't fire without an event loop, so the hook must NOT
        have been called yet at that point."""
        from lyra.ptt import PttState, TrSequencing
        fsm = self.radio._ptt_fsm
        fsm._tr = TrSequencing(mox_delay_ms=0, ptt_out_delay_ms=50)
        events: list[bool] = []
        # bind a recording hook (radio runtime not needed here)
        fsm._on_tx_state_changed = lambda is_tx, st: events.append(is_tx)
        self.radio.request_mox()
        self.assertEqual(events, [True])                 # keydown fired
        self.radio.release_mox()
        # fade gate completes; _clear_mox_tail runs inline (mox=0)
        # but _end_keyup is deferred by ptt_out=50 -> hook NOT yet
        fsm._on_fade_poll()
        self.assertEqual(events, [True])                 # NO keyup hook yet
        self.assertEqual(fsm.current_state, PttState.MOX_TX)

    def test_keyup_preserves_operator_mute(self) -> None:
        from lyra.ptt import PttState
        self.radio.set_muted(True)                   # operator chose mute
        self.assertTrue(self.radio._muted)
        self.radio._on_tx_state_changed(True, PttState.MOX_TX)   # key down
        self.radio._on_tx_state_changed(False, PttState.RX)      # key up
        self.assertFalse(self.radio._tx_rx_muted)    # TX gate released
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
