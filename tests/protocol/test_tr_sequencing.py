"""TR-sequencing capability-sourcing + rf_delay amp-safety floor
(v0.2.0 Phase 3 Commit B, §15.26).

rf_delay is the MOX→RF settle that prevents hot-switching an
external 1 kW linear; it has a HARD 50 ms floor enforced in
TrSequencing so no caller / corrupt QSetting can defeat the
amplifier protection.  Operators may RAISE delays.
"""
from __future__ import annotations

import sys
import unittest

from lyra.ptt import TrSequencing
from lyra.protocol.capabilities import HL2_CAPABILITIES


def _clear_tr_qs():
    from PySide6.QtCore import QSettings
    qs = QSettings("N8SDR", "Lyra")
    for n in ("mox", "ptt_out", "rf", "space_mox", "key_up"):
        qs.remove(f"tx/tr_{n}_ms")


class TrSequencingFloorTest(unittest.TestCase):
    def test_capability_values(self) -> None:
        # (mox, ptt_out, rf, space_mox, key_up) — operator-verified
        self.assertEqual(HL2_CAPABILITIES.tr_delays_ms,
                         (15, 20, 50, 13, 10))

    def test_rf_delay_adjustable_range(self) -> None:
        # Operator-adjustable 1..75 ms (default 50 = hot-switch-
        # safe).  Clamped only to the sane hardware range -- the
        # operator's amp/risk call, not a paternalistic lockout.
        self.assertEqual(TrSequencing.RF_DELAY_MIN_MS, 1)
        self.assertEqual(TrSequencing.RF_DELAY_MAX_MS, 75)
        self.assertEqual(TrSequencing(rf_delay_ms=0).rf_delay_ms, 1)
        self.assertEqual(TrSequencing(rf_delay_ms=1).rf_delay_ms, 1)
        self.assertEqual(TrSequencing(rf_delay_ms=10).rf_delay_ms, 10)
        self.assertEqual(TrSequencing(rf_delay_ms=75).rf_delay_ms, 75)
        self.assertEqual(TrSequencing(rf_delay_ms=200).rf_delay_ms, 75)
        self.assertEqual(TrSequencing().rf_delay_ms, 50)   # default


class TrSequencingRadioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        _clear_tr_qs()
        from lyra.radio import Radio
        self.radio = Radio()

    def tearDown(self) -> None:
        _clear_tr_qs()

    def test_fsm_gets_capability_values(self) -> None:
        tr = self.radio._ptt_fsm._tr
        self.assertEqual(tr.mox_delay_ms, 15)
        self.assertEqual(tr.ptt_out_delay_ms, 20)
        self.assertEqual(tr.rf_delay_ms, 50)
        self.assertEqual(tr.space_mox_delay_ms, 13)
        self.assertEqual(tr.key_up_delay_ms, 10)
        self.assertEqual(self.radio.tr_delays["rf"], 50)

    def test_set_tr_delay_persists_rebuilds_signals(self) -> None:
        seen: list = []
        self.radio.tr_sequencing_changed.connect(seen.append)
        self.radio.set_tr_delay("mox", 30)
        self.assertEqual(self.radio._ptt_fsm._tr.mox_delay_ms, 30)
        self.assertEqual(seen[-1]["mox"], 30)
        # persisted -> a fresh Radio picks it up
        from lyra.radio import Radio
        r2 = Radio()
        self.assertEqual(r2._ptt_fsm._tr.mox_delay_ms, 30)

    def test_rf_delay_operator_adjustable_clamped_to_range(self):
        self.radio.set_tr_delay("rf", 5)               # operator's call
        self.assertEqual(self.radio.tr_delays["rf"], 5)   # honoured
        self.radio.set_tr_delay("rf", 1)
        self.assertEqual(self.radio.tr_delays["rf"], 1)
        self.radio.set_tr_delay("rf", 999)             # clamp to max
        self.assertEqual(self.radio.tr_delays["rf"], 75)

    def test_unknown_name_ignored(self) -> None:
        self.radio.set_tr_delay("bogus", 999)          # no crash/effect
        self.assertEqual(self.radio.tr_delays["rf"], 50)


if __name__ == "__main__":
    unittest.main()
