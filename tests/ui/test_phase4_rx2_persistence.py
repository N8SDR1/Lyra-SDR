"""Phase 4 v0.1 (2026-05-12) -- RX2 state persistence across
QSettings save / load cycles.

Tests use an isolated QSettings location (per-test scope) so they
don't clobber the operator's real QSettings.  Each test seeds the
QSettings keys, constructs a fresh Radio, calls
``autoload_rx2_state``, and asserts the restored state matches.

Save-side coverage is via a round-trip through the real
``QSettings`` write API rather than reaching into app.py's
``_save_settings`` (which is bound to the MainWindow).

Run from repo root::

    python -m unittest tests.ui.test_phase4_rx2_persistence -v
"""
from __future__ import annotations

import sys
import unittest


class Phase4Rx2PersistenceTest(unittest.TestCase):
    """Round-trip per-RX state through QSettings."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtCore import QCoreApplication, QSettings
        # Required for QSettings to use IniFormat under a custom
        # path (avoids polluting the operator's real registry).
        cls._app = (
            QCoreApplication.instance() or QCoreApplication(sys.argv))
        # Use a per-test-suite IniFormat scratch file so we don't
        # touch HKCU.  QSettings.setPath is global state -- restore
        # after the class.
        cls._scratch_org = "N8SDR-test-rx2-persist"
        cls._scratch_app = "Lyra"

    def _qs(self):
        from PySide6.QtCore import QSettings
        # Use the SAME N8SDR/Lyra path the Radio.autoload uses,
        # but the test runs against an isolated scope via clear().
        s = QSettings(self.__class__._scratch_org,
                      self.__class__._scratch_app)
        return s

    def setUp(self) -> None:
        from lyra.radio import Radio
        # Clear scratch settings before each test.
        s = self._qs()
        s.clear()
        s.sync()
        self.radio = Radio()
        # Patch the test scratch values directly into QSettings
        # under the production org/app name so autoload finds them.
        # We restore by clearing in tearDown.
        from PySide6.QtCore import QSettings
        self._prod = QSettings("N8SDR", "Lyra")
        # Snapshot any operator state under rx2/* so we can restore
        # it after the test (so a real operator running unit tests
        # doesn't wake up with a wiped RX2 config).
        self._snapshot: dict[str, object] = {}
        for k in self._prod.allKeys():
            if (k.startswith("rx2/") or k.startswith("dispatch/")
                    or k == "radio/focused_rx"):
                self._snapshot[k] = self._prod.value(k)
                self._prod.remove(k)
        self._prod.sync()

    def tearDown(self) -> None:
        # Remove any keys this test wrote.
        for k in list(self._prod.allKeys()):
            if (k.startswith("rx2/") or k.startswith("dispatch/")
                    or k == "radio/focused_rx"):
                self._prod.remove(k)
        # Restore the operator's pre-test snapshot.
        for k, v in self._snapshot.items():
            self._prod.setValue(k, v)
        self._prod.sync()

    def _seed(self, **pairs) -> None:
        for k, v in pairs.items():
            self._prod.setValue(k, v)
        self._prod.sync()

    def test_autoload_rx2_freq(self) -> None:
        self._seed(**{"rx2/freq_hz": 14_205_000})
        self.radio.autoload_rx2_state()
        self.assertEqual(self.radio.rx2_freq_hz, 14_205_000)

    def test_autoload_rx2_mode(self) -> None:
        self._seed(**{"rx2/mode": "CWU"})
        self.radio.autoload_rx2_state()
        self.assertEqual(self.radio._mode_rx2, "CWU")

    def test_autoload_rx2_volume_and_muted(self) -> None:
        self._seed(**{"rx2/volume": 0.42, "rx2/muted": True})
        self.radio.autoload_rx2_state()
        self.assertAlmostEqual(self.radio._volume_rx2, 0.42, places=6)
        self.assertTrue(self.radio._muted_rx2)

    def test_autoload_rx2_af_gain(self) -> None:
        self._seed(**{"rx2/af_gain_db": 17})
        self.radio.autoload_rx2_state()
        self.assertEqual(self.radio._af_gain_db_rx2, 17)

    def test_autoload_rx2_bw_by_mode(self) -> None:
        import json
        self._seed(**{
            "rx2/rx_bw_by_mode": json.dumps(
                {"USB": 1800, "CWU": 250, "AM": 8000})})
        self.radio.autoload_rx2_state()
        self.assertEqual(
            self.radio._rx_bw_by_mode_rx2.get("USB"), 1800)
        self.assertEqual(
            self.radio._rx_bw_by_mode_rx2.get("CWU"), 250)
        self.assertEqual(
            self.radio._rx_bw_by_mode_rx2.get("AM"), 8000)

    def test_autoload_focused_rx(self) -> None:
        self._seed(**{"radio/focused_rx": 2})
        self.radio.autoload_rx2_state()
        self.assertEqual(self.radio.focused_rx, 2)

    def test_autoload_sub_enabled(self) -> None:
        self._seed(**{"dispatch/rx2_enabled": True})
        self.radio.autoload_rx2_state()
        self.assertTrue(self.radio.dispatch_state.rx2_enabled)

    def test_sub_mirror_suppressed_during_autoload(self) -> None:
        """Persisted RX2 vol/mute must survive the SUB rising-edge
        mirror in ``set_rx2_enabled`` when the dispatch state is
        being restored from QSettings.  Pre-fix the mirror would
        smash RX2's loaded vol with RX1's default vol."""
        # Pre-condition: RX1 vol differs from RX2 vol.
        self.radio.set_volume(0.3, target_rx=0)
        self._seed(**{
            "rx2/volume": 0.85,
            "rx2/muted": True,
            "dispatch/rx2_enabled": True,
        })
        self.radio.autoload_rx2_state()
        # SUB came on.
        self.assertTrue(self.radio.dispatch_state.rx2_enabled)
        # RX2 vol survived the SUB rising edge.
        self.assertAlmostEqual(self.radio._volume_rx2, 0.85, places=6)
        self.assertTrue(self.radio._muted_rx2)

    def test_sub_mirror_still_fires_on_operator_toggle(self) -> None:
        """Outside autoload, the SUB rising-edge mirror MUST still
        fire so an operator-initiated SUB click gets the safety
        net.  Regression marker for the v0.4 suppression flag."""
        self.radio.set_volume(0.2, target_rx=0)
        self.radio.set_volume(0.9, target_rx=2)  # diverge
        # Operator clicks SUB (no suppression).
        self.radio.set_rx2_enabled(True)
        # Mirror fired: RX2 vol now matches RX1 vol.
        self.assertAlmostEqual(self.radio._volume_rx2, 0.2, places=6)

    def test_autoload_load_order_preserves_cw_pitch_offset(self) -> None:
        """Loading mode=CWU then freq must produce the correct
        DDS offset on the wire.  Regression marker for the v0.8
        fix -- DDS write order matters."""
        seen_dds_writes: list[int] = []

        class _MockStream:
            def _set_rx2_freq(self, hz):
                seen_dds_writes.append(int(hz))

            def _set_rx1_freq(self, hz):
                pass

        self.radio._stream = _MockStream()
        self.radio._cw_pitch_hz = 700
        self._seed(**{
            "rx2/mode": "CWU",
            "rx2/freq_hz": 7_030_000,
        })
        self.radio.autoload_rx2_state()
        # DDS should land at carrier - pitch for CWU.
        self.assertIn(7_030_000 - 700, seen_dds_writes)


if __name__ == "__main__":
    unittest.main()
