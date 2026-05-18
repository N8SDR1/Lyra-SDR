"""S4a — worker→main lna_peak_update coalescing.

The per-block (~90 Hz) cross-thread Qt emit was a GIL-contending
storm.  S4a accumulates over ``_LNA_EMIT_BLOCKS`` (≈10 Hz) and
emits one interval reduction: MAX peak (Auto-LNA back-off reads
max() — max-of-maxes is lossless), MAX rms (``_evaluate_pullup``
wants the worst case), MEAN rms (the toolbar quadratic-mean would
read high off the max).  These pin: the block-count clock, no
emit before the interval completes, lossless max, mean-vs-max
rms separation, the MOX→RX accumulator reset (a TX-coupled max
must not survive into post-keyup Auto-LNA), no spurious 0.0
emit, and the radio-side coalesced append + retuned toolbar
window.
"""
from __future__ import annotations

import sys
import unittest

import numpy as np


class S4aWorkerCoalesceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def _worker(self):
        from lyra.dsp.worker import DspWorker
        return DspWorker()

    def _blk(self, mag: float):
        # Constant-magnitude complex block: peak == rms == mag.
        return np.full(64, mag, dtype=np.complex64)

    def test_no_emit_before_interval_completes(self) -> None:
        w = self._worker()
        for _ in range(w._LNA_EMIT_BLOCKS - 1):
            self.assertIsNone(w._feed_lna(self._blk(0.5), mox=False))

    def test_emits_once_per_interval_with_reductions(self) -> None:
        w = self._worker()
        mags = [0.1, 0.9, 0.2, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3]
        # exactly _LNA_EMIT_BLOCKS samples
        mags = (mags + [0.3] * w._LNA_EMIT_BLOCKS)[: w._LNA_EMIT_BLOCKS]
        out = None
        for m in mags:
            out = w._feed_lna(self._blk(m), mox=False) or out
        self.assertIsNotNone(out)
        peak_max, rms_max, rms_mean = out
        # peak/rms per block == mag (constant block).
        self.assertAlmostEqual(peak_max, max(mags), places=5)
        self.assertAlmostEqual(rms_max, max(mags), places=5)
        self.assertAlmostEqual(rms_mean, sum(mags) / len(mags), places=5)
        # Accumulator reset after emit — next interval is independent.
        self.assertEqual(w._lna_acc_n, 0)

    def test_peak_max_over_interval_is_lossless(self) -> None:
        # max(emitted interval-maxes) == max(all per-block peaks):
        # the Auto-LNA back-off invariant the red-team proved.
        w = self._worker()
        per_block = []
        interval_maxes = []
        rng = np.random.default_rng(42)
        for _ in range(5 * w._LNA_EMIT_BLOCKS):
            m = float(rng.uniform(0.0, 1.0))
            per_block.append(m)
            out = w._feed_lna(self._blk(m), mox=False)
            if out is not None:
                interval_maxes.append(out[0])
        self.assertAlmostEqual(max(interval_maxes), max(per_block),
                               places=5)

    def test_mox_resets_accumulator_and_suppresses_emit(self) -> None:
        w = self._worker()
        # Partial loud interval...
        for _ in range(w._LNA_EMIT_BLOCKS - 1):
            w._feed_lna(self._blk(0.99), mox=False)
        self.assertGreater(w._lna_acc_n, 0)
        # ...keydown: accumulator dropped, nothing emitted.
        self.assertIsNone(w._feed_lna(self._blk(0.99), mox=True))
        self.assertEqual(w._lna_acc_n, 0)
        self.assertEqual(w._lna_acc_peak, 0.0)
        # Post-keyup the loud pre-key max is GONE — a fresh quiet
        # interval emits the quiet value, not the poisoned 0.99.
        out = None
        for _ in range(w._LNA_EMIT_BLOCKS):
            out = w._feed_lna(self._blk(0.01), mox=False) or out
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out[0], 0.01, places=5)

    def test_no_spurious_zero_emit(self) -> None:
        # Empty blocks never publish a 0.0 sample.
        w = self._worker()
        empty = np.zeros(0, dtype=np.complex64)
        for _ in range(3 * w._LNA_EMIT_BLOCKS):
            self.assertIsNone(w._feed_lna(empty, mox=False))

    def test_signal_is_three_arg(self) -> None:
        from lyra.dsp.worker import DspWorker
        w = DspWorker()
        got = []
        w.lna_peak_update.connect(lambda a, b, c: got.append((a, b, c)))
        for _ in range(w._LNA_EMIT_BLOCKS):
            out = w._feed_lna(self._blk(0.4), mox=False)
        if out is not None:
            w.lna_peak_update.emit(*out)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(got[0]), 3)


class S4aRadioConsumerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def _radio(self):
        from lyra.radio import Radio
        return Radio()

    def test_append_coalesced_trims_all_three_lists(self) -> None:
        r = self._radio()
        for i in range(r._lna_peaks_max + 20):
            r._append_lna_coalesced(0.5 + i * 1e-4, 0.4, 0.3)
        self.assertEqual(len(r._lna_peaks), r._lna_peaks_max)
        self.assertEqual(len(r._lna_rms), r._lna_peaks_max)
        self.assertEqual(len(r._lna_rms_mean), r._lna_peaks_max)

    def test_window_retuned_for_10hz_feed(self) -> None:
        # Was 120 (≈1.3 s @ ~90 Hz); at the coalesced ~10 Hz it must
        # shrink to keep ≈1.3 s (mandatory red-team correction #1).
        r = self._radio()
        self.assertLessEqual(r._lna_peaks_max, 15)
        self.assertGreaterEqual(r._lna_peaks_max, 12)
        self.assertEqual(r._LNA_TOOLBAR_WIN, 4)

    def test_on_worker_slot_is_three_arg(self) -> None:
        r = self._radio()
        r._on_worker_lna_peak(0.7, 0.6, 0.5)
        self.assertEqual(r._lna_peaks[-1], 0.7)
        self.assertEqual(r._lna_rms[-1], 0.6)        # MAX → _evaluate_pullup
        self.assertEqual(r._lna_rms_mean[-1], 0.5)   # MEAN → toolbar

    def test_emit_peak_reading_uses_rms_mean_not_max(self) -> None:
        # _evaluate_pullup keeps max(_lna_rms); the toolbar quadratic
        # mean must read _lna_rms_mean so it is NOT systematically
        # high (mandatory correction #2).
        r = self._radio()
        captured = {}
        r.lna_rms_dbfs.connect(lambda v: captured.setdefault("rms_db", v))
        for _ in range(r._LNA_TOOLBAR_WIN):
            r._append_lna_coalesced(0.5, 0.9, 0.1)  # rms_max≫rms_mean
        r._emit_peak_reading()
        # 10*log10(mean^2) with mean=0.1  →  ~ -20 dB; the MAX (0.9)
        # would give ~ -0.9 dB.  Assert it tracked the MEAN.
        self.assertLess(captured["rms_db"], -15.0)


if __name__ == "__main__":
    unittest.main()
