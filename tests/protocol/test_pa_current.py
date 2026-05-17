"""HL2 PA bias-current telemetry decode + conversion
(§15.26 Correction-3-FINAL, 2026-05-17 -- gateware-RTL-decisive,
anchored on the proven temp+supply decode).

The HL2 EP6 4-slot status rotation (gateware control.v iresp,
indexed by resp_addr = (C0>>3)&0x1F):

  addr 1 (0x08): C1:C2 = die temperature ; C3:C4 = fwd power
  addr 2 (0x10): C1:C2 = reverse power   ; C3:C4 = PA bias
                                            current (12-bit)
  addr 3 (0x18): C1:C2 = 0               ; C3:C4 = debug (junk)

Supply = the proven addr-0 C1:C2>>4 fallback (untouched).
There is NO PA-volts ADC slot (the prior "VDD" was this same
bias current mis-routed -> removed).  Decode-only; no wire
change (§3.9 inert).  Temp + supply decode is byte-identical
and is the anchor proving the rotation/addr extraction.
"""
from __future__ import annotations

import sys
import unittest

from lyra.protocol.stream import FrameStats, _decode_hl2_telemetry


class PaCurrentDecodeTest(unittest.TestCase):
    def test_addr1_temp_and_fwd_UNCHANGED(self) -> None:
        # The proven anchor: addr 1 C1:C2 = temp (must not regress).
        st = FrameStats()
        _decode_hl2_telemetry(
            bytes([0x08, 0x0A, 0xBC, 0x12, 0x34]), st)
        self.assertEqual(st.temp_adc, 0x0ABC)
        self.assertEqual(st.fwd_pwr_adc, 0x1234)
        self.assertEqual(st.pa_current_adc, 0)      # NOT here

    def test_addr2_is_revpwr_and_PA_CURRENT(self) -> None:
        st = FrameStats()
        # C0=0x10 -> addr 2.  C1:C2 = rev power; C3:C4 = the
        # 12-bit PA bias current.
        _decode_hl2_telemetry(
            bytes([0x10, 0x12, 0x34, 0x0A, 0xBC]), st)
        self.assertEqual(st.rev_pwr_adc, 0x1234)
        self.assertEqual(st.pa_current_adc, 0x0ABC)

    def test_addr3_is_inert_not_supply(self) -> None:
        st = FrameStats()
        # C0=0x18 -> addr 3.  C1:C2=0, C3:C4=debug (junk).
        # Must NOT write pa_current or supply.
        _decode_hl2_telemetry(
            bytes([0x18, 0x05, 0x67, 0x1F, 0xFE]), st)
        self.assertEqual(st.pa_current_adc, 0)
        self.assertEqual(st.supply_adc, 0)

    def test_addr0_supply_fallback_UNCHANGED(self) -> None:
        # The proven live supply path: addr 0 C1:C2 >> 4.
        st = FrameStats()
        _decode_hl2_telemetry(
            bytes([0x00, 0x0F, 0xF0, 0x00, 0x00]), st)
        self.assertEqual(st.supply_adc_alt, 0x0FF0 >> 4)

    def test_i2c_response_block_decodes_nothing(self) -> None:
        st = FrameStats()
        _decode_hl2_telemetry(
            bytes([0x90, 0x12, 0x34, 0x0A, 0xBC]), st)
        self.assertEqual(st.pa_current_adc, 0)

    def test_no_pa_volts_field(self) -> None:
        # pa_volts_adc removed -- no PA-volts slot on this gateware.
        self.assertFalse(hasattr(FrameStats(), "pa_volts_adc"))


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

    def test_no_pa_volts_property(self) -> None:
        self.assertFalse(hasattr(self.radio, "pa_volts"))

    def test_verified_hl2_amps_formula(self) -> None:
        class _S:
            pa_current_adc = 0
        class _Stub:
            stats = _S()
        self.radio._stream = _Stub()
        self.radio._stream.stats.pa_current_adc = 2048   # mid-scale
        expect = (((3.26 * (2048 / 4096.0)) / 50.0) / 0.04
                  / (1000.0 / 1270.0))
        self.assertAlmostEqual(self.radio.pa_current_amps,
                               expect, places=6)

    def test_full_tune_yardstick_in_range(self) -> None:
        # Operator anchor: full tune into dummy ~1.8 A; the raw
        # for that must be a valid 12-bit value (<4096).
        raw = 1.8 * 0.04 * 50.0 * (1000.0 / 1270.0) / 3.26 * 4096.0
        self.assertLess(raw, 4096)
        self.assertGreater(raw, 0)

    def test_telemetry_payload_carries_pa_a_no_pa_v(self) -> None:
        got: list = []
        self.radio.hl2_telemetry_changed.connect(got.append)
        self.radio._emit_hl2_telemetry()                 # no stream
        self.assertIn("pa_a", got[-1])
        self.assertNotIn("pa_v", got[-1])


if __name__ == "__main__":
    unittest.main()
