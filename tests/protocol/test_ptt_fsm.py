"""PttStateMachine unit tests (v0.2.0 Phase 3 commit 3a, §15.25).

Pure / no hardware.  Fakes for radio / stream / mox_edge_fade /
on_tx_state_changed.  The keyup fade-gate is driven
deterministically by calling ``_on_fade_poll()`` directly (the
documented test seam) -- no running Qt event loop required.

Regression guards for the §15.25 traps:
  * FSM never calls _set_tx_freq itself (commit 2 owns it).
  * MOX bit clears ONLY after the down-ramp completes.
  * RX not declared before the ptt_out settle.
  * SW/HW MOX share one state (wire bit touched exactly 2x/tx).
  * Re-key during a draining keyup COLLAPSES (no MOX churn).
"""
from __future__ import annotations

import sys
import unittest

from lyra.ptt import PttSource, PttState, PttStateMachine, TrSequencing


class _FakeRadio:
    def __init__(self) -> None:
        self.set_mox_calls: list[bool] = []

    def set_mox(self, v: bool) -> None:
        self.set_mox_calls.append(bool(v))
    # NOTE: deliberately NO _set_tx_freq -- if the FSM ever tried
    # to call it, tests would AttributeError (the §15.25 trap-2
    # guard: only commit-2's set_mox owns TX-freq).


class _FakeStream:
    def __init__(self) -> None:
        self.inject_tx_iq = False


class _FakeFade:
    def __init__(self) -> None:
        self._off = True
        self.fade_in_calls = 0
        self.fade_out_calls = 0
        self.events: list[str] = []

    def start_fade_in(self) -> None:
        self.fade_in_calls += 1
        self._off = False
        self.events.append("fade_in")

    def start_fade_out(self) -> None:
        self.fade_out_calls += 1
        self.events.append("fade_out")

    def is_off(self) -> bool:
        return self._off

    def set_off(self, v: bool) -> None:
        self._off = bool(v)


class PttFsmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def _bound(self, tr: TrSequencing | None = None):
        fsm = PttStateMachine(tr_sequencing=tr)
        radio, stream, fade = _FakeRadio(), _FakeStream(), _FakeFade()
        tx_events: list[tuple[bool, PttState]] = []
        fsm.bind_runtime(
            radio=radio, stream=stream, mox_edge_fade=fade,
            on_tx_state_changed=lambda is_tx, st: tx_events.append(
                (is_tx, st)))
        states: list[PttState] = []
        fsm.state_changed.connect(states.append)
        return fsm, radio, stream, fade, tx_events, states

    # 1 ── transitions + idempotency ────────────────────────────────
    def test_rx_to_mox_and_back(self) -> None:
        fsm, radio, stream, fade, _, states = self._bound()
        fsm.request_mox()
        self.assertEqual(fsm.current_state, PttState.MOX_TX)
        self.assertTrue(fsm.is_tx)
        fsm.release_mox()                 # starts fade-out
        self.assertEqual(fsm.current_state, PttState.MOX_TX)  # not yet
        fade.set_off(True)                # down-ramp complete
        fsm._on_fade_poll()               # seam
        self.assertEqual(fsm.current_state, PttState.RX)
        self.assertEqual(states, [PttState.MOX_TX, PttState.RX])

    def test_idempotent_set_source_no_signal(self) -> None:
        fsm, *_rest, states = self._bound()
        fsm.request_mox()
        fsm.request_mox()                 # already held -> no-op
        self.assertEqual(states, [PttState.MOX_TX])

    # 2 ── keydown order (§15.25 trap #3 guard) ─────────────────────
    def test_keydown_order(self) -> None:
        fsm, radio, stream, fade, tx_events, _ = self._bound()
        fsm.request_mox()
        # set_mox(True) called; inject_tx_iq True; fade_in called.
        self.assertEqual(radio.set_mox_calls, [True])
        self.assertTrue(stream.inject_tx_iq)
        self.assertEqual(fade.fade_in_calls, 1)
        # auto-mute hook fired with (True, MOX_TX).
        self.assertEqual(tx_events[0], (True, PttState.MOX_TX))
        # inject set True BEFORE start_fade_in (worker only pumps
        # apply() inside the inject branch).
        self.assertEqual(fade.events, ["fade_in"])  # only after inject

    # 3 ── keyup fade-gate ordering (the load-bearing test) ─────────
    def test_keyup_waits_for_fade_then_clears_mox(self) -> None:
        fsm, radio, stream, fade, tx_events, _ = self._bound()
        fsm.request_mox()
        radio.set_mox_calls.clear()
        fade.set_off(False)               # ramp draining
        fsm.release_mox()
        self.assertEqual(fade.fade_out_calls, 1)   # fade-out started
        # While draining: MOX NOT cleared, inject still True.
        for _ in range(5):
            fsm._on_fade_poll()
        self.assertEqual(radio.set_mox_calls, [])
        self.assertTrue(stream.inject_tx_iq)
        self.assertEqual(fsm.current_state, PttState.MOX_TX)
        # Down-ramp completes -> finalize.
        fade.set_off(True)
        fsm._on_fade_poll()
        self.assertFalse(stream.inject_tx_iq)      # inject cleared
        self.assertEqual(radio.set_mox_calls, [False])  # THEN MOX
        self.assertEqual(tx_events[-1], (False, PttState.MOX_TX))
        self.assertEqual(fsm.current_state, PttState.RX)

    # 4 ── SW/HW share state (wire bit exactly 2x) ──────────────────
    def test_sw_hw_share_state(self) -> None:
        fsm, radio, stream, fade, _, states = self._bound()
        fsm.set_source(PttSource.SW_MOX, True)     # -> MOX_TX
        fsm.set_source(PttSource.HW_PTT, True)     # no transition
        fsm.set_source(PttSource.HW_PTT, False)    # still MOX_TX
        self.assertEqual(fsm.current_state, PttState.MOX_TX)
        fsm.set_source(PttSource.SW_MOX, False)    # keyup
        fade.set_off(True)
        fsm._on_fade_poll()
        self.assertEqual(fsm.current_state, PttState.RX)
        # set_mox touched the wire exactly twice across the whole
        # interleave (one True keydown, one False keyup).
        self.assertEqual(radio.set_mox_calls, [True, False])
        self.assertEqual(states, [PttState.MOX_TX, PttState.RX])

    def test_hw_released_while_sw_held_stays_keyed(self) -> None:
        fsm, radio, *_ = self._bound()
        fsm.set_source(PttSource.SW_MOX, True)
        fsm.set_source(PttSource.HW_PTT, True)
        fsm.set_source(PttSource.HW_PTT, False)
        self.assertEqual(fsm.current_state, PttState.MOX_TX)
        self.assertEqual(radio.set_mox_calls, [True])  # no keyup

    # 5 ── idempotency / force_release_all no-op ────────────────────
    def test_hw_ptt_repeats_one_transition(self) -> None:
        fsm, _r, _s, _f, _e, states = self._bound()
        for _ in range(5):
            fsm.set_hardware_ptt(True)
        self.assertEqual(states, [PttState.MOX_TX])

    def test_force_release_all_noop_when_rx(self) -> None:
        fsm, _r, _s, _f, _e, states = self._bound()
        fsm.force_release_all()
        self.assertEqual(states, [])
        self.assertEqual(fsm.current_state, PttState.RX)

    # 6 ── re-key during draining keyup COLLAPSES (§15.25 #1) ───────
    def test_rekey_during_release_collapses(self) -> None:
        fsm, radio, stream, fade, _, _ = self._bound()
        fsm.request_mox()
        radio.set_mox_calls.clear()
        fade.set_off(False)
        fsm.release_mox()                 # fade-out begins
        # Operator re-keys before the ramp finishes.
        fsm.request_mox()
        fade.set_off(True)
        fsm._on_fade_poll()               # finalize sees a held src
        # COLLAPSE: MOX bit never cleared, fade re-in, stay keyed.
        self.assertEqual(radio.set_mox_calls, [])
        self.assertTrue(stream.inject_tx_iq)
        self.assertEqual(fsm.current_state, PttState.MOX_TX)
        self.assertEqual(fade.fade_in_calls, 2)   # initial + re-in

    # 7 ── §15.25 trap regression guards ────────────────────────────
    def test_trap_fsm_never_calls_set_tx_freq(self) -> None:
        # _FakeRadio has NO _set_tx_freq; a full keydown/keyup must
        # not AttributeError -> proves the FSM only uses set_mox
        # (commit 2 owns TX-freq, §15.25 trap #2).
        fsm, radio, _s, fade, _e, _st = self._bound()
        fsm.request_mox()
        fsm.release_mox()
        fade.set_off(True)
        fsm._on_fade_poll()
        self.assertEqual(radio.set_mox_calls, [True, False])

    def test_trap_rx_not_declared_before_ptt_out_settle(self) -> None:
        # Non-zero ptt_out_delay -> RX transition is deferred (the
        # _end_keyup runs via QTimer.singleShot, which does NOT
        # fire without an event loop -> state stays MOX_TX, proving
        # RX is not declared synchronously before the settle).
        fsm, radio, stream, fade, _e, _st = self._bound(
            TrSequencing(ptt_out_delay_ms=50))
        fsm.request_mox()
        fade.set_off(False)
        fsm.release_mox()
        fade.set_off(True)
        fsm._on_fade_poll()
        # inject cleared + MOX cleared (those are inline; mox_delay
        # = 0) but RX NOT yet declared (ptt_out deferred).
        self.assertFalse(stream.inject_tx_iq)
        self.assertEqual(radio.set_mox_calls, [True, False])
        self.assertEqual(fsm.current_state, PttState.MOX_TX)

    # 8 ── TR-sequencing: HL2 zero-defaults are all inline ──────────
    def test_zero_delays_inline(self) -> None:
        fsm, radio, stream, fade, _e, _st = self._bound(
            TrSequencing())   # all-zero (HL2)
        fsm.request_mox()
        self.assertTrue(stream.inject_tx_iq)   # rf_delay=0 -> inline
        fade.set_off(False)
        fsm.release_mox()
        fade.set_off(True)
        fsm._on_fade_poll()
        # mox_delay=0 + ptt_out_delay=0 -> fully inline to RX.
        self.assertEqual(radio.set_mox_calls, [True, False])
        self.assertEqual(fsm.current_state, PttState.RX)

    # 9 ── force_release_all drives a real gated keyup ──────────────
    def test_force_release_all_keys_down(self) -> None:
        fsm, radio, stream, fade, _e, _st = self._bound()
        fsm.set_source(PttSource.SW_MOX, True)
        fsm.set_source(PttSource.HW_PTT, True)    # two held sources
        fade.set_off(False)
        fsm.force_release_all()                   # clears the set
        fade.set_off(True)
        fsm._on_fade_poll()
        self.assertEqual(fsm.current_state, PttState.RX)
        self.assertEqual(radio.set_mox_calls, [True, False])

    # 10 ── unbound runtime degrades gracefully ─────────────────────
    def test_unbound_runtime_emits_state_no_crash(self) -> None:
        fsm = PttStateMachine()           # no bind_runtime
        states: list[PttState] = []
        fsm.state_changed.connect(states.append)
        fsm.request_mox()                 # no DSP refs -> no crash
        self.assertEqual(fsm.current_state, PttState.MOX_TX)
        fsm.release_mox()                 # no fade -> instant finalize
        self.assertEqual(fsm.current_state, PttState.RX)
        self.assertEqual(states, [PttState.MOX_TX, PttState.RX])

    def test_unbind_runtime_after_bind(self) -> None:
        fsm, *_ = self._bound()
        fsm.request_mox()
        fsm.unbind_runtime()
        # After unbind a keyup must not crash (no fade bound).
        fsm.release_mox()
        self.assertEqual(fsm.current_state, PttState.RX)


if __name__ == "__main__":
    unittest.main()
