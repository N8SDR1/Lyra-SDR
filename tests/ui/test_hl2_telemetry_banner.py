"""HL2 telemetry banner renders PA current (Phase 3, §15.26).

Commit A decoded PA current into the telemetry payload but the
banner label only rendered T (temp) and V (supply) -- the
operator had no PA-current visual, which is the observable for
the first-RF bench + the Phase-3-EXIT kill-test.  This pins the
banner formatting (the real ``MainWindow._update_hl2_telemetry``
called as an unbound method against a tiny shim, so no heavy
main-window construction): PA shows a 2-dp amps value, and NaN
fields render 'n/a' (not a false 0).
"""
from __future__ import annotations

import unittest

from lyra.ui.app import MainWindow


class _LabelStub:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, s: str) -> None:
        self.text = s


class _Shim:
    def __init__(self) -> None:
        self.hl2_telem_label = _LabelStub()


class Hl2TelemetryBannerTest(unittest.TestCase):
    def _render(self, payload: dict) -> str:
        shim = _Shim()
        MainWindow._update_hl2_telemetry(shim, payload)
        return shim.hl2_telem_label.text

    def test_pa_current_rendered_no_vdd(self) -> None:
        # §15.26 Corr-3-FINAL: banner shows PA current (2 dp).
        # No VDD field -- there is no PA-volts slot on this
        # gateware (the old "VDD" was mis-routed PA current).
        txt = self._render(
            {"temp_c": 47.0, "supply_v": 12.3, "pa_a": 0.21})
        self.assertIn("PA", txt)
        self.assertIn("0.21 A", txt)
        self.assertNotIn("VDD", txt)
        self.assertIn("47.0", txt)
        self.assertIn("12.3", txt)

    def test_nan_pa_renders_na_not_zero(self) -> None:
        txt = self._render(
            {"temp_c": 47.0, "supply_v": 12.3,
             "pa_a": float("nan")})
        self.assertIn("PA", txt)
        self.assertIn("n/a", txt)
        self.assertNotIn("0.00 A", txt)

    def test_missing_pa_key_is_na(self) -> None:
        # Defensive: a payload without pa_a must not raise.
        txt = self._render({"temp_c": 47.0, "supply_v": 12.3})
        self.assertIn("PA", txt)
        self.assertIn("n/a", txt)


if __name__ == "__main__":
    unittest.main()
