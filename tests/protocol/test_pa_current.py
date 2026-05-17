"""HL2 PA current/volts telemetry decode + conversion
(§15.26 Correction-3, 2026-05-17).

Triple-verified (Thetis networkproto1.c:330-353 + HL2 wiki +
operator PA-bias ground truth): Commit A wrongly read PA
"current" from EP6 slot 0x10 (addr 2) C3:C4 -- that is HL2
``user_adc0`` = PA *Volts*/VDD, NOT amps.  The real combined
push-pull PA *current* = ``user_adc1`` = slot 0x18 (addr 3)
C1:C2.  Re-map:

  addr 2 (0x10): C1:C2 -> rev_pwr_adc ; C3:C4 -> pa_volts_adc
  addr 3 (0x18): C1:C2 -> pa_current_adc ; C3:C4 -> supply_adc

Decode-only; no wire change (§3.9 inert).  Validation
yardstick: correctly-decoded current ~0.2 A at idle bias.
"""
from __future__ import annotations

import sys
import unittest

from lyra.protocol.stream import FrameStats, _decode_hl2_telemetry


class PaCurrentDecodeTest(unittest.TestCase):
    def test_slot_0x10_is_rev_power_and_pa_VOLTS(self) -> None:
        st = FrameStats()
        # C0=0x10 -> addr 2.  C1:C2 = rev power; C3:C4 = user_adc0
        # = PA VOLTS (NOT current -- the Commit-A bug).
        _decode_hl2_telemetry(
            bytes([0x10, 0x12, 0x34, 0x0A, 0xBC]), st)
        self.assertEqual(st.rev_pwr_adc, 0x1234)
        self.assertEqual(st.pa_volts_adc, 0x0ABC)
        self.assertEqual(st.pa_current_adc, 0)      # NOT here

    def test_slot_0x18_is_pa_CURRENT_and_supply(self) -> None:
        st = FrameStats()
        # C0=0x18 -> addr 3.  C1:C2 = user_adc1 = PA AMPS (the
        # real combined drain current); C3:C4 = supply volts.
        _decode_hl2_telemetry(
            bytes([0x18, 0x05, 0x67, 0x1F, 0xFE]), st)
        self.assertEqual(st.pa_current_adc, 0x0567)
        self.assertEqual(st.supply_adc, 0x1FFE)
        self.assertEqual(st.pa_volts_adc, 0)        # NOT here

    def test_i2c_response_block_decodes_nothing(self) -> None:
        st = FrameStats()
        # C0 bit7 set => I2C readback, bail before field decode.
        _decode_hl2_telemetry(
            bytes([0x98, 0x05, 0x67, 0x1F, 0xFE]), st)
        self.assertEqual(st.pa_current_adc, 0)
        self.assertEqual(st.pa_volts_adc, 0)

    def test_other_slots_leave_pa_fields_untouched(self) -> None:
        st = FrameStats()
        _decode_hl2_telemetry(            # addr 1 (0x08): temp/fwd
            bytes([0x08, 0x01, 0x02, 0x03, 0x04]), st)
        self.assertEqual(st.pa_current_adc, 0)
        self.assertEqual(st.pa_volts_adc, 0)

    def test_field_proven_temp_supply_decode_untouched(self) -> None:
        # The re-map must NOT disturb the operator-confirmed
        # temp (addr 1 C1:C2) / supply (addr 3 C3:C4) decode.
        st = FrameStats()
        _decode_hl2_telemetry(
            bytes([0x08, 0x0A, 0xBC, 0x00, 0x00]), st)
        self.assertEqual(st.temp_adc, 0x0ABC)


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
        self.assertTrue(math.isnan(self.radio.pa_volts))

    def test_verified_hl2_amps_formula(self) -> None:
        class _S:
            pa_current_adc = 0
            pa_volts_adc = 0
        class _Stub:
            stats = _S()
        self.radio._stream = _Stub()
        self.radio._stream.stats.pa_current_adc = 2048   # mid-scale
        expect = (((3.26 * (2048 / 4096.0)) / 50.0) / 0.04
                  / (1000.0 / 1270.0))
        self.assertAlmostEqual(self.radio.pa_current_amps,
                               expect, places=6)

    def test_pa_volts_formula(self) -> None:
        class _S:
            pa_current_adc = 0
            pa_volts_adc = 0
        class _Stub:
            stats = _S()
        self.radio._stream = _Stub()
        self.radio._stream.stats.pa_volts_adc = 2200
        expect = (2200 / 4095.0) * 5.0 * (23.0 / 1.1)
        self.assertAlmostEqual(self.radio.pa_volts,
                               expect, places=6)

    def test_telemetry_payload_carries_pa_a_and_pa_v(self) -> None:
        got: list = []
        self.radio.hl2_telemetry_changed.connect(got.append)
        self.radio._emit_hl2_telemetry()                 # no stream
        self.assertIn("pa_a", got[-1])
        self.assertIn("pa_v", got[-1])


if __name__ == "__main__":
    unittest.main()
