"""Radio.tx_freq_hz tests (v0.2.0 Phase 3 commit 1, §15.25).

Critical regression guard: the §15.25 2-agent Thetis
verification found a HIGH trap §15.24 missed -- RIT must NOT
leak into the TX frequency (Thetis applies RIT to rx_freq only,
console.cs:32502-32503; XIT to tx_freq only, :32508-32509).
Lyra's _compute_dds_freq_hz adds _rit_offset_hz; tx_freq_hz is
a SEPARATE RIT-free computation.  These tests lock that.
"""
from __future__ import annotations

import unittest


class TxFreqHzTest(unittest.TestCase):
    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_tx_freq_is_main_vfo(self) -> None:
        self.radio.set_freq_hz(14_250_000)
        self.assertEqual(self.radio.tx_freq_hz, 14_250_000)

    def test_tx_freq_tracks_vfo_changes(self) -> None:
        self.radio.set_freq_hz(7_074_000)
        self.assertEqual(self.radio.tx_freq_hz, 7_074_000)
        self.radio.set_freq_hz(21_300_000)
        self.assertEqual(self.radio.tx_freq_hz, 21_300_000)

    def test_rit_does_not_shift_tx_freq(self) -> None:
        """THE §15.25 regression guard.  RIT engaged shifts the RX
        DDS (dds_freq_hz) but tx_freq_hz must stay on the raw
        carrier -- reusing _compute_dds_freq_hz here would put
        every RIT-engaged transmission off-frequency."""
        self.radio.set_freq_hz(14_200_000)
        self.radio.set_rit_offset_hz(1200)
        self.radio.set_rit_enabled(True)
        # RX DDS is shifted by RIT...
        self.assertEqual(self.radio.dds_freq_hz, 14_201_200)
        # ...but TX freq is NOT.
        self.assertEqual(self.radio.tx_freq_hz, 14_200_000)
        # Negative offset too.
        self.radio.set_rit_offset_hz(-2500)
        self.assertEqual(self.radio.dds_freq_hz, 14_197_500)
        self.assertEqual(self.radio.tx_freq_hz, 14_200_000)

    def test_rit_disabled_still_no_tx_shift(self) -> None:
        self.radio.set_freq_hz(10_136_000)
        self.radio.set_rit_offset_hz(500)
        self.radio.set_rit_enabled(False)
        self.assertEqual(self.radio.tx_freq_hz, 10_136_000)

    def test_cw_pitch_not_applied_to_tx_in_phase3(self) -> None:
        """Phase 3 is SSB-only; even if mode is CW, tx_freq_hz
        returns the raw carrier (CW TX = v0.2.2, and it needs
        Thetis's cw_fw_keyer-GATED pitch, NOT the RX
        unconditional offset -- so the Phase-3 contract is: no
        CW offset on TX).  dds_freq_hz DOES shift for CW (RX
        path); tx_freq_hz must not."""
        self.radio.set_freq_hz(7_030_000)
        self.radio.set_mode("CWU")
        # RX DDS shifts by -cw_pitch (CWU convention)...
        self.assertNotEqual(self.radio.dds_freq_hz, 7_030_000)
        # ...TX stays raw carrier.
        self.assertEqual(self.radio.tx_freq_hz, 7_030_000)

    def test_rit_plus_cw_compound_still_no_tx_shift(self) -> None:
        """Belt-and-suspenders: RIT + CW both engaged (the
        compounding _compute_dds_freq_hz path) still leaves
        tx_freq_hz on the raw carrier."""
        self.radio.set_freq_hz(3_573_000)
        self.radio.set_mode("CWL")
        self.radio.set_rit_offset_hz(300)
        self.radio.set_rit_enabled(True)
        self.assertEqual(self.radio.tx_freq_hz, 3_573_000)


if __name__ == "__main__":
    unittest.main()
