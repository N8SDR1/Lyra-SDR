"""Phase 1 verification step 7 — ``ddc_map(state)`` state-product
abstraction test.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §4.4 step 7
(numbering corrected Round 5):

  > **Dispatch-state-product abstraction test.**  Unit test (no
  > real hardware required): programmatically toggle
  > ``radio.dispatch_state`` via ``radio.set_mox(True/False)`` and
  > ``radio.set_ps_armed(True/False)``.  After each toggle, call
  > ``radio.protocol.ddc_map(radio.snapshot_dispatch_state())``
  > and verify the returned ``{ddc_idx: ConsumerID}`` map matches
  > the expected HL2 dispatch table in §4.2.x.  **Pass:** all 4
  > state-product cells (``(mox, ps_armed)`` × ``{F,F}, {F,T},
  > {T,F}, {T,T}``) route per the table.

This catches table-driven dispatch bugs before v0.2 wires real
consumers -- the fail mode is an intermittent PS-feedback-during-
non-PS routing bug that's extremely hard to debug from operator-
symptom side.

Run from repo root::

    python -m unittest tests.protocol.test_ddc_map -v
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from lyra.protocol import ddc_map
from lyra.radio_state import ConsumerID, DispatchState, RadioFamily


# Expected HL2 dispatch table per consensus plan §4.2.x.  The four
# state-product cells (mox, ps_armed) -> {ddc_idx: ConsumerID}.
# Pinned-by-test so any future refactor that changes the table
# without updating the spec fails here AND the consensus plan
# row.
_HL2_EXPECTED = {
    (False, False): {
        0: ConsumerID.RX_AUDIO_CH0,
        1: ConsumerID.RX_AUDIO_CH2,
        2: ConsumerID.DISCARD,
        3: ConsumerID.DISCARD,
    },
    (False, True): {
        # PS armed but no MOX: still RX-only routing.  PS feedback
        # only kicks in on the MOX edge.
        0: ConsumerID.RX_AUDIO_CH0,
        1: ConsumerID.RX_AUDIO_CH2,
        2: ConsumerID.DISCARD,
        3: ConsumerID.DISCARD,
    },
    (True, False): {
        # MOX engaged, PS disarmed (e.g., key-down without PS feedback
        # path armed): operator's MuteRXOnVFOBTX gate handles audio at
        # the AAmixer level (audio_architecture.md §2.4); the protocol
        # layer still delivers RX-band content.
        0: ConsumerID.RX_AUDIO_CH0,
        1: ConsumerID.RX_AUDIO_CH2,
        2: ConsumerID.DISCARD,
        3: ConsumerID.DISCARD,
    },
    (True, True): {
        # MOX + PS armed -- gateware reroutes PA coupler to DDC0 via
        # cntrl1=4 and sync-pairs DDC1 at TX freq.  DDC2/DDC3 stay
        # gateware-disabled (zeros) per CLAUDE.md §3.8 corrected
        # entry.
        0: ConsumerID.PS_FEEDBACK_I,
        1: ConsumerID.PS_FEEDBACK_Q,
        2: ConsumerID.DISCARD,
        3: ConsumerID.DISCARD,
    },
}


class DdcMapHl2Test(unittest.TestCase):
    """4-cell state-product table verification per §4.4 step 7."""

    def test_hl2_rx_only(self) -> None:
        state = DispatchState(family=RadioFamily.HL2)
        self.assertEqual(ddc_map(state), _HL2_EXPECTED[(False, False)])

    def test_hl2_ps_armed_no_mox(self) -> None:
        state = DispatchState(family=RadioFamily.HL2, ps_armed=True)
        self.assertEqual(ddc_map(state), _HL2_EXPECTED[(False, True)])

    def test_hl2_mox_no_ps(self) -> None:
        state = DispatchState(family=RadioFamily.HL2, mox=True)
        self.assertEqual(ddc_map(state), _HL2_EXPECTED[(True, False)])

    def test_hl2_mox_and_ps_armed(self) -> None:
        state = DispatchState(family=RadioFamily.HL2, mox=True, ps_armed=True)
        self.assertEqual(ddc_map(state), _HL2_EXPECTED[(True, True)])

    def test_hl2_plus_uses_same_table(self) -> None:
        """HL2_PLUS shares the HL2 dispatch table per capabilities.py
        comment + plan §3.1.x: same wire-protocol + same DDC count
        + same PS-mod gateware behavior."""
        for mox in (False, True):
            for ps in (False, True):
                state = DispatchState(
                    family=RadioFamily.HL2_PLUS, mox=mox, ps_armed=ps
                )
                with self.subTest(mox=mox, ps_armed=ps):
                    self.assertEqual(
                        ddc_map(state), _HL2_EXPECTED[(mox, ps)],
                        f"HL2_PLUS routing diverged from HL2 at "
                        f"(mox={mox}, ps_armed={ps})",
                    )

    def test_rx2_enabled_does_not_affect_routing(self) -> None:
        """``rx2_enabled`` is a UI / consumer-side concern; the
        wire-protocol dispatch is identical regardless."""
        for rx2 in (False, True):
            state = DispatchState(family=RadioFamily.HL2, rx2_enabled=rx2)
            with self.subTest(rx2_enabled=rx2):
                self.assertEqual(ddc_map(state), _HL2_EXPECTED[(False, False)])

    def test_pure_function_no_side_effects(self) -> None:
        """``ddc_map`` must be a pure function -- callable twice
        with the same input returns equal (or identical) output,
        no globals mutated."""
        state = DispatchState(family=RadioFamily.HL2, mox=True, ps_armed=True)
        a = ddc_map(state)
        b = ddc_map(state)
        self.assertEqual(a, b)

    def test_replace_pattern_drives_toggles(self) -> None:
        """The §4.2.x mutation pattern (``dataclasses.replace``)
        produces a new state instance and the function follows
        the new value -- belt-and-suspenders for the
        frozen-dataclass contract."""
        state = DispatchState(family=RadioFamily.HL2)
        self.assertEqual(
            ddc_map(state)[0], ConsumerID.RX_AUDIO_CH0
        )
        state = replace(state, mox=True, ps_armed=True)
        self.assertEqual(
            ddc_map(state)[0], ConsumerID.PS_FEEDBACK_I
        )
        state = replace(state, ps_armed=False)
        self.assertEqual(
            ddc_map(state)[0], ConsumerID.RX_AUDIO_CH0
        )


class DdcMapUnimplementedFamilyTest(unittest.TestCase):
    """v0.4 placeholders raise NotImplementedError until their
    dispatch tables land."""

    def test_anan_p1_5ddc_raises(self) -> None:
        state = DispatchState(family=RadioFamily.ANAN_P1_5DDC)
        with self.assertRaises(NotImplementedError):
            ddc_map(state)

    def test_anan_p2_raises(self) -> None:
        state = DispatchState(family=RadioFamily.ANAN_P2)
        with self.assertRaises(NotImplementedError):
            ddc_map(state)


if __name__ == "__main__":
    unittest.main()
