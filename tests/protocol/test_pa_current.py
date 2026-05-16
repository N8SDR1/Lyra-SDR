"""HL2 PA-current telemetry decode + conversion (Phase 3 commit A,
§15.26).  EP6 status slot 0x10 (addr 2): C1:C2 = reverse power,
C3:C4 = HL2 user-ADC0 = PA-current sense (raw ADC).  Verified HL2
sense-amp conversion in Radio.  Decode-only; no wire change.
Makes the Phase-3-EXIT kill-test observable.
"""
from __future__ import annotations

import sys
import unittest

from lyra.protocol.stream import FrameStats, _decode_hl2_telemetry


class PaCurrentDecodeTest(unittest.TestCase):
    def test_slot_0x10_decodes_pa_current_and_rev_power(self) -> None:
        st = FrameStats()
        # C0=0x10 -> addr 2, bit7 clear (telemetry not I2C).
        # C1:C2 = rev power 0x1234 ; C3:C4 = PA-cur raw 0x0ABC.
        _decode_hl2_telemetry(
            bytes([0x10, 0x12, 0x34, 0x0A, 0xBC]), st)
        self.assertEqual(st.rev_pwr_adc, 0x1234)
        self.assertEqual(st.pa_current_adc, 0x0ABC)

    def test_i2c_response_block_does_not_set_pa_current(self) -> None:
        st = FrameStats()
        # C0 bit7 set => I2C readback, must bail before field decode.
        _decode_hl2_telemetry(
            bytes([0x90, 0x12, 0x34, 0x0A, 0xBC]), st)
        self.assertEqual(st.pa_current_adc, 0)

    def test_other_slots_leave_pa_current_untouched(self) -> None:
        st = FrameStats()
        _decode_hl2_telemetry(            # addr 1 (0x08): temp/fwd
            bytes([0x08, 0x01, 0x02, 0x03, 0x04]), st)
        self.assertEqual(st.pa_current_adc, 0)


class PaCurrentConversionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    def test_nan_when_no_data(self) -> None:
        import math
        self.assertTrue(math.isnan(self.radio.pa_current_amps))

    def test_verified_hl2_formula(self) -> None:
        class _S:
            pa_current_adc = 0
        class _Stub:
            stats = _S()
        self.radio._stream = _Stub()
        self.radio._stream.stats.pa_current_adc = 2048   # mid-scale
        # ((3.26*(2048/4096))/50)/0.04 / (1000/1270)
        expect = (((3.26 * (2048 / 4096.0)) / 50.0) / 0.04
                  / (1000.0 / 1270.0))
        self.assertAlmostEqual(self.radio.pa_current_amps,
                               expect, places=6)

    def test_telemetry_payload_carries_pa_a(self) -> None:
        got: list = []
        self.radio.hl2_telemetry_changed.connect(got.append)
        self.radio._emit_hl2_telemetry()                 # no stream
        self.assertIn("pa_a", got[-1])


if __name__ == "__main__":
    unittest.main()
