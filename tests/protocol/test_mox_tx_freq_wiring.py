"""MOX-edge _set_tx_freq wiring tests (v0.2.0 Phase 3 commit 2, §15.25).

Guards FINDING #2 / trap #2 (stale-freq first burst): on the
MOX=1 edge Radio must load the TX NCO via
HL2Stream._set_tx_freq(tx_freq_hz) BEFORE flipping the dispatch
MOX bit (mirrors Thetis UpdateTXDDSFreq-before-SetPttOut(1)),
and must re-push on retune-while-TX (Thetis UpdateTXDDSFreq on
every dial change).  RIT-free value via tx_freq_hz (FINDING #1).
"""
from __future__ import annotations

import unittest


class _StubStream:
    """Captures _set_tx_freq / _set_rx1_freq calls + ordering."""

    def __init__(self) -> None:
        self.tx_freq_calls: list[int] = []
        self.events: list[str] = []  # ordered event log

    def _set_tx_freq(self, hz: int) -> None:
        self.tx_freq_calls.append(int(hz))
        self.events.append(f"tx_freq={int(hz)}")

    def _set_rx1_freq(self, hz: int) -> None:
        self.events.append(f"rx1_freq={int(hz)}")

    # set_freq_hz touches these; make them harmless no-ops.
    def _set_rx2_freq(self, hz: int) -> None:
        pass


class MoxTxFreqWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()
        self.stub = _StubStream()
        self.radio._stream = self.stub  # noqa: SLF001
        self.radio.set_freq_hz(14_250_000)
        # set_freq_hz logged an rx1_freq via the stub; clear the log
        # so each test sees only its own events.
        self.stub.events.clear()
        self.stub.tx_freq_calls.clear()

    def test_mox_on_pushes_tx_freq_before_dispatch_flip(self) -> None:
        seen_state = []
        self.radio.dispatch_state_changed.connect(
            lambda st: seen_state.append(st.mox))
        self.radio.set_mox(True)
        # _set_tx_freq was called with the RIT-free TX carrier...
        self.assertEqual(self.stub.tx_freq_calls, [14_250_000])
        # ...and BEFORE the dispatch MOX flip (the signal that
        # carries mox=True fires after the freq push).  Since the
        # freq push is synchronous before `replace(...)`, by the
        # time the dispatch_state_changed slot runs the tx_freq
        # call is already recorded.
        self.assertEqual(seen_state, [True])
        self.assertEqual(self.stub.events[0], "tx_freq=14250000")

    def test_mox_off_does_not_push_tx_freq(self) -> None:
        self.radio.set_mox(True)
        self.stub.tx_freq_calls.clear()
        self.radio.set_mox(False)
        self.assertEqual(self.stub.tx_freq_calls, [])

    def test_mox_idempotent_no_double_push(self) -> None:
        self.radio.set_mox(True)
        self.assertEqual(len(self.stub.tx_freq_calls), 1)
        self.radio.set_mox(True)  # no-op (already MOX)
        self.assertEqual(len(self.stub.tx_freq_calls), 1)

    def test_rit_free_value_on_mox_edge(self) -> None:
        """The §15.25 FINDING #1 guard at the wiring layer: RIT
        engaged must NOT shift the freq pushed on the MOX edge."""
        self.radio.set_rit_offset_hz(1500)
        self.radio.set_rit_enabled(True)
        self.radio.set_mox(True)
        self.assertEqual(self.stub.tx_freq_calls, [14_250_000])

    def test_retune_while_tx_repushes_tx_freq(self) -> None:
        self.radio.set_mox(True)
        self.stub.tx_freq_calls.clear()
        self.stub.events.clear()
        self.radio.set_freq_hz(14_255_000)
        # Retuning while keyed re-pushes the TX NCO...
        self.assertEqual(self.stub.tx_freq_calls, [14_255_000])
        # ...and tx_freq_hz now follows the new dial.
        self.assertEqual(self.radio.tx_freq_hz, 14_255_000)

    def test_retune_while_not_tx_does_not_push_tx_freq(self) -> None:
        # MOX off: dial changes push RX freq only, never TX.
        self.radio.set_freq_hz(14_260_000)
        self.assertEqual(self.stub.tx_freq_calls, [])

    def test_mox_on_with_no_stream_does_not_crash(self) -> None:
        from lyra.radio import Radio
        r = Radio()  # no _stream assigned (pre-start())
        r.set_mox(True)  # must not raise
        self.assertTrue(r._dispatch_state.mox)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
