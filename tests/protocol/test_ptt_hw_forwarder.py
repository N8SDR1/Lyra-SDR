"""HW-PTT forwarder + force_release_all tests (Phase 3 commit 3c).

The forwarder lives in Radio._on_hl2_mic (RX-loop thread): it
edge-detects stats.ptt_in vs _last_hw_ptt and, on a real edge,
QMetaObject.invokeMethod(_ptt_fsm, "set_hardware_ptt",
QueuedConnection).  Tests cover: edge-detect bookkeeping (fires
only on transitions, before the mic-empty/worker-None guards),
the queued invoke actually reaching the FSM (pumped via
QApplication.processEvents), and the §15.20 force_release_all
hook.
"""
from __future__ import annotations

import sys
import unittest

import numpy as np

from lyra.ptt import PttState


class _PttStub:
    def __init__(self) -> None:
        self.ptt_in = False


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

    def start_fade_in(self) -> None:
        self.fade_in_calls += 1
        self._off = False

    def start_fade_out(self) -> None:
        pass

    def is_off(self) -> bool:
        return self._off

    def set_off(self, v: bool) -> None:
        self._off = bool(v)


class PttHwForwarderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()
        from lyra.ptt import TrSequencing
        # Unit determinism: force all-zero TR (production default
        # is now the non-zero HW-T/R settle) so the keyup tail is
        # inline without an event loop.
        self.radio._ptt_fsm._tr = TrSequencing(
            mox_delay_ms=0, ptt_out_delay_ms=0)
        # rf_delay hard-floored at 50 (amp safety) -> inline
        # _deferred so the keydown/keyup chain stays synchronous
        # for these forwarder tests (no event loop).
        self.radio._ptt_fsm._deferred = lambda ms, fn: fn()
        # v0.2.0 Phase 3 hotfix (§15.25 / §10 Q#1): the EP6 ptt_in
        # forwarder is OPT-IN now (default OFF -- some HL2+/AK4951
        # units carry a non-zero ptt_in at RX rest, which the
        # always-on forwarder mis-keyed as MOX).  These tests
        # exercise the *enabled* path, so opt in explicitly.
        self.radio._hw_ptt_input_enabled = True
        self.empty = np.zeros(0, dtype=np.int16)

    def test_forwarder_gated_off_by_default(self) -> None:
        """Regression guard for the 2026-05-16 phantom-TX surge:
        a fresh Radio defaults _hw_ptt_input_enabled=False, so an
        asserted ptt_in is IGNORED -- no latch, no FSM key, RX
        stays RX (byte-identical to pre-commit-3c)."""
        from lyra.radio import Radio
        from lyra.ptt import PttState
        radio = Radio()
        self.assertFalse(radio._hw_ptt_input_enabled)  # default OFF
        st = _PttStub()
        st.ptt_in = True                 # foot-switch "pressed"
        radio._on_hl2_mic(self.empty, st)
        self.assertFalse(radio._last_hw_ptt)            # not latched
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        self.assertEqual(radio._ptt_fsm.current_state,
                         PttState.RX)                   # never keyed
        # Opting in re-enables the forwarder.
        radio.set_hw_ptt_input_enabled(True)
        self.assertTrue(radio._hw_ptt_input_enabled)
        radio._on_hl2_mic(self.empty, st)
        self.assertTrue(radio._last_hw_ptt)             # now latched

    def _pump(self) -> None:
        # Deliver the QueuedConnection invokeMethod to the FSM.
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()

    def test_edge_detect_only_on_transition(self) -> None:
        st = _PttStub()
        # ptt_in stays False -> no edge, _last_hw_ptt stays False.
        self.radio._on_hl2_mic(self.empty, st)
        self.assertFalse(self.radio._last_hw_ptt)
        # Rising edge -> latch True.
        st.ptt_in = True
        self.radio._on_hl2_mic(self.empty, st)
        self.assertTrue(self.radio._last_hw_ptt)
        # Held True across datagrams -> no further edge (latch stays).
        self.radio._on_hl2_mic(self.empty, st)
        self.assertTrue(self.radio._last_hw_ptt)
        # Falling edge -> latch False.
        st.ptt_in = False
        self.radio._on_hl2_mic(self.empty, st)
        self.assertFalse(self.radio._last_hw_ptt)

    def test_forward_runs_before_mic_guards(self) -> None:
        # Empty mic + no TX worker: the mic path early-returns, but
        # the PTT forward (placed BEFORE those guards) still latched.
        st = _PttStub()
        st.ptt_in = True
        self.assertIsNone(self.radio._tx_dsp_worker)  # no worker
        self.radio._on_hl2_mic(self.empty, st)
        self.assertTrue(self.radio._last_hw_ptt)

    def test_queued_invoke_reaches_fsm_and_keys(self) -> None:
        """End-to-end: HW-PTT edge → queued invoke → FSM
        set_hardware_ptt → (bound) real Radio.set_mox keydown."""
        stub, fade = _StubStream(), _FakeFade()
        self.radio._stream = stub
        self.radio.set_freq_hz(7_074_000)
        stub.tx_freq_calls.clear()
        self.radio._ptt_fsm.bind_runtime(
            radio=self.radio, stream=stub, mox_edge_fade=fade,
            on_tx_state_changed=self.radio._on_tx_state_changed)
        st = _PttStub()
        st.ptt_in = True
        self.radio._on_hl2_mic(self.empty, st)   # queues the invoke
        # Not yet delivered (QueuedConnection needs the event loop).
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.RX)
        self._pump()
        # Delivered: FSM keyed, commit-2 pushed RIT-free tx_freq.
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.MOX_TX)
        self.assertEqual(stub.tx_freq_calls, [7_074_000])
        self.assertTrue(self.radio._dispatch_state.mox)
        self.assertTrue(stub.inject_tx_iq)

    def test_force_release_all_hook(self) -> None:
        stub, fade = _StubStream(), _FakeFade()
        self.radio._ptt_fsm.bind_runtime(
            radio=self.radio, stream=stub, mox_edge_fade=fade,
            on_tx_state_changed=self.radio._on_tx_state_changed)
        self.radio.request_mox()
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.MOX_TX)
        fade.set_off(False)
        self.radio.force_release_all()           # §15.20 hook
        fade.set_off(True)
        self.radio._ptt_fsm._on_fade_poll()      # complete the gate
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.RX)

    def test_force_release_all_noop_when_rx(self) -> None:
        self.radio.force_release_all()           # no crash, no-op
        self.assertEqual(self.radio._ptt_fsm.current_state,
                         PttState.RX)


if __name__ == "__main__":
    unittest.main()
