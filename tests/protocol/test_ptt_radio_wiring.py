"""Radio↔PttStateMachine wiring tests (v0.2.0 Phase 3 commit 3b).

Radio owns the FSM (constructed in __init__, before any stream),
exposes request_mox/release_mox pass-throughs (UI never reaches
into _ptt_fsm -- §6.7 facade), provides the _on_tx_state_changed
no-op auto-mute hook, and binds/unbinds runtime refs.  These
tests cover the facade + ownership + the 3a↔3b↔commit-2
integration without needing a real stream/start().
"""
from __future__ import annotations

import sys
import unittest

from lyra.ptt import PttState, PttStateMachine


class _StubStream:
    def __init__(self) -> None:
        self.tx_freq_calls: list[int] = []
        self.inject_tx_iq = False

    def _set_tx_freq(self, hz: int) -> None:
        self.tx_freq_calls.append(int(hz))

    def _set_rx1_freq(self, hz: int) -> None:
        pass

    def _set_rx2_freq(self, hz: int) -> None:
        pass


class _FakeFade:
    def __init__(self) -> None:
        self._off = True
        self.fade_in_calls = 0
        self.fade_out_calls = 0

    def start_fade_in(self) -> None:
        self.fade_in_calls += 1
        self._off = False

    def start_fade_out(self) -> None:
        self.fade_out_calls += 1

    def is_off(self) -> bool:
        return self._off

    def set_off(self, v: bool) -> None:
        self._off = bool(v)


class PttRadioWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        from lyra.ptt import TrSequencing
        self.radio = Radio()
        # Unit determinism: the production FSM default is now the
        # non-zero HW-T/R settle (mox 10 / ptt_out 20 ms); force
        # all-zero so the keyup tail runs inline without an event
        # loop.  These tests exercise wiring/facade, not TR timing.
        self.radio._ptt_fsm._tr = TrSequencing(
            mox_delay_ms=0, ptt_out_delay_ms=0)

    def test_radio_owns_fsm(self) -> None:
        self.assertIsInstance(self.radio._ptt_fsm, PttStateMachine)
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.RX)
        self.assertFalse(self.radio._last_hw_ptt)

    def test_passthroughs_drive_fsm_unbound(self) -> None:
        # No bind_runtime yet -> FSM tracks state, skips DSP, no crash.
        self.radio.request_mox()
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.MOX_TX)
        self.radio.release_mox()
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.RX)

    def test_on_tx_state_changed_is_safe_noop(self) -> None:
        # Phase 3 no-op; must accept (is_tx, state) and return None.
        self.assertIsNone(
            self.radio._on_tx_state_changed(True, PttState.MOX_TX))
        self.assertIsNone(
            self.radio._on_tx_state_changed(False, PttState.RX))

    def test_bound_passthrough_drives_real_set_mox(self) -> None:
        """3a↔3b↔commit-2 integration: with runtime bound, the UI
        pass-through → FSM keydown → real Radio.set_mox(True),
        which (commit 2) pushes tx_freq before flipping the
        dispatch MOX bit."""
        stub, fade = _StubStream(), _FakeFade()
        self.radio._stream = stub
        self.radio.set_freq_hz(14_250_000)
        stub.tx_freq_calls.clear()
        self.radio._ptt_fsm.bind_runtime(
            radio=self.radio, stream=stub, mox_edge_fade=fade,
            on_tx_state_changed=self.radio._on_tx_state_changed)
        self.radio.request_mox()
        # FSM keydown ran the real Radio.set_mox(True): commit-2
        # pushed the RIT-free tx_freq before the dispatch flip...
        self.assertEqual(stub.tx_freq_calls, [14_250_000])
        self.assertTrue(self.radio._dispatch_state.mox)
        # ...and the keydown chain opened the TX I/Q gate + fade-in.
        self.assertTrue(stub.inject_tx_iq)
        self.assertEqual(fade.fade_in_calls, 1)
        # Keyup: gated on fade completing before MOX clears.
        self.radio.release_mox()
        self.assertTrue(self.radio._dispatch_state.mox)  # not yet
        fade.set_off(True)
        self.radio._ptt_fsm._on_fade_poll()
        self.assertFalse(self.radio._dispatch_state.mox)  # now
        self.assertFalse(stub.inject_tx_iq)

    def test_unbind_runtime_safe(self) -> None:
        stub, fade = _StubStream(), _FakeFade()
        self.radio._ptt_fsm.bind_runtime(
            radio=self.radio, stream=stub, mox_edge_fade=fade,
            on_tx_state_changed=self.radio._on_tx_state_changed)
        self.radio._ptt_fsm.unbind_runtime()
        # Post-unbind a keyup must not crash (no fade bound).
        self.radio.request_mox()
        self.radio.release_mox()
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.RX)


if __name__ == "__main__":
    unittest.main()
