"""Phase 3.E.1 v0.1 -- panadapter follows focused RX.

Operator UX (2026-05-12): when the operator clicks VFO B's LED or
hits Ctrl+2, the panadapter should retune to RX2's band so they
can see the signal they're listening to.  Adds a
``panadapter_source_rx`` state on Radio that auto-tracks
``focused_rx`` by default.

Phase 3.E.2 will add a "TX override" path (panadapter stays on
TX VFO during MOX regardless of focus); for now the source
strictly follows focus.

Run from repo root::

    python -m unittest tests.ui.test_phase3e1_panadapter_source -v
"""
from __future__ import annotations

import sys
import unittest


class Phase3e1PanadapterSourceTest(unittest.TestCase):
    """``panadapter_source_rx`` state + auto-track behavior."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        from lyra.radio import Radio
        self.radio = Radio()

    # ── State surface ──────────────────────────────────────────────

    def test_default_source_is_rx1(self) -> None:
        self.assertEqual(self.radio.panadapter_source_rx, 0)

    def test_set_panadapter_source_rx_valid(self) -> None:
        self.radio.set_panadapter_source_rx(2)
        self.assertEqual(self.radio.panadapter_source_rx, 2)
        self.radio.set_panadapter_source_rx(0)
        self.assertEqual(self.radio.panadapter_source_rx, 0)

    def test_set_panadapter_source_rx_validates(self) -> None:
        for bad in (1, 3, -1, 99):
            with self.subTest(rx_id=bad):
                with self.assertRaises(ValueError):
                    self.radio.set_panadapter_source_rx(bad)

    def test_set_panadapter_source_rx_idempotent(self) -> None:
        """Setting to current value must NOT emit the signal."""
        seen: list[int] = []
        self.radio.panadapter_source_changed.connect(seen.append)
        self.radio.set_panadapter_source_rx(0)  # already 0
        self.assertEqual(seen, [])

    def test_panadapter_source_changed_emits_on_transition(self) -> None:
        seen: list[int] = []
        self.radio.panadapter_source_changed.connect(seen.append)
        self.radio.set_panadapter_source_rx(2)
        self.assertEqual(seen, [2])
        self.radio.set_panadapter_source_rx(0)
        self.assertEqual(seen, [2, 0])

    # ── Auto-track from focused_rx ─────────────────────────────────

    def test_focus_change_to_rx2_updates_panadapter_source(self) -> None:
        self.radio.set_focused_rx(2)
        self.assertEqual(self.radio.panadapter_source_rx, 2)

    def test_focus_change_back_to_rx1_restores_source(self) -> None:
        self.radio.set_focused_rx(2)
        self.radio.set_focused_rx(0)
        self.assertEqual(self.radio.panadapter_source_rx, 0)

    def test_focus_emits_both_signals(self) -> None:
        """A focus change should emit BOTH focused_rx_changed AND
        panadapter_source_changed -- panels that bind to either
        signal stay in sync."""
        focus_seen: list[int] = []
        source_seen: list[int] = []
        self.radio.focused_rx_changed.connect(focus_seen.append)
        self.radio.panadapter_source_changed.connect(source_seen.append)
        self.radio.set_focused_rx(2)
        self.assertIn(2, focus_seen)
        self.assertIn(2, source_seen)

    # ── Click-to-tune routes to source RX ──────────────────────────

    def test_click_to_tune_routes_to_source_rx2(self) -> None:
        self.radio.set_panadapter_source_rx(2)
        orig_rx1 = self.radio.freq_hz
        # Round to a value that survives Exact rounding (it should
        # round to itself).
        self.radio.set_freq_from_panadapter(7_200_000)
        self.assertEqual(self.radio.rx2_freq_hz, 7_200_000)
        self.assertEqual(self.radio.freq_hz, orig_rx1)

    def test_click_to_tune_routes_to_source_rx1(self) -> None:
        self.radio.set_panadapter_source_rx(0)
        orig_rx2 = self.radio.rx2_freq_hz
        self.radio.set_freq_from_panadapter(14_205_000)
        self.assertEqual(self.radio.freq_hz, 14_205_000)
        self.assertEqual(self.radio.rx2_freq_hz, orig_rx2)


class Phase3e1WorkerFlushTest(unittest.TestCase):
    """Worker has a ``flush_fft_ring`` method that the source-change
    signal triggers so the next FFT frame is clean."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_worker_has_flush_method(self) -> None:
        from lyra.dsp.worker import DspWorker
        self.assertTrue(hasattr(DspWorker, "flush_fft_ring"))

    def test_flush_is_safe_when_ring_uninitialised(self) -> None:
        """Worker lazy-initializes the ring on first sample; flush
        before that must be a no-op (no AttributeError)."""
        from lyra.dsp.worker import DspWorker
        w = DspWorker()
        try:
            w.flush_fft_ring()  # should not raise
        finally:
            try:
                w.stop()
            except Exception:
                pass

    def test_flush_clears_ring_and_block_counter(self) -> None:
        from lyra.dsp.worker import DspWorker
        from collections import deque
        w = DspWorker()
        try:
            w._sample_ring = deque(range(10), maxlen=100)
            w._fft_block_counter = 5
            w.flush_fft_ring()
            self.assertEqual(len(w._sample_ring), 0)
            self.assertEqual(w._fft_block_counter, 0)
        finally:
            try:
                w.stop()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
