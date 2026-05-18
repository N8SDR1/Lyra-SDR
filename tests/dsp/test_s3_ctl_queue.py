"""S3 — DSP worker control-plane decoupled from the Qt event pump.

The per-iteration ``QCoreApplication.processEvents()`` is gone; the
5 inbound controls (agc_profile / bin_enabled / bin_depth /
flush_fft_ring / sink-swap) now ENQUEUE on the main thread and the
worker APPLIES them between blocks via ``_drain_ctl``.  These pin:
enqueue-not-immediate (race-free between-blocks invariant), drain
applies, sink-swap-via-queue still closes old + sets the §15.21
bug-3 barrier, bounded-snapshot drain (a command posting another
doesn't run in the same pass), per-command error isolation, and the
dead-as-slots config writers stay immediate (direct, not enqueued).
"""
from __future__ import annotations

import sys
import unittest


class S3CtlQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def _worker(self):
        from lyra.dsp.worker import DspWorker
        return DspWorker()

    def test_config_setters_enqueue_not_immediate(self) -> None:
        w = self._worker()
        w._config.agc_profile = "med"
        w._config.bin_enabled = False
        w._config.bin_depth = 0.0
        w.set_agc_profile("fast")
        w.set_bin_enabled(True)
        w.set_bin_depth(0.7)
        # Enqueued — NOT applied until the worker drains (proves the
        # never-mid-block invariant + no Qt-pump dependency).
        self.assertEqual(w._config.agc_profile, "med")
        self.assertFalse(w._config.bin_enabled)
        self.assertEqual(w._config.bin_depth, 0.0)
        w._drain_ctl()
        self.assertEqual(w._config.agc_profile, "fast")
        self.assertTrue(w._config.bin_enabled)
        self.assertAlmostEqual(w._config.bin_depth, 0.7)

    def test_sink_swap_via_queue_closes_old_and_sets_barrier(self):
        w = self._worker()

        class _Sink:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        old, new = _Sink(), _Sink()
        w._audio_sink = old
        w._sink_swap_done.clear()
        w._on_audio_sink_changed(new)
        # Enqueued — old NOT closed, barrier NOT set, sink unchanged.
        self.assertFalse(old.closed)
        self.assertIs(w._audio_sink, old)
        self.assertFalse(w._sink_swap_done.is_set())
        w._drain_ctl()
        self.assertIs(w._audio_sink, new)
        self.assertTrue(old.closed)
        self.assertTrue(w._sink_swap_done.is_set())  # §15.21 bug-3

    def test_dead_as_slots_are_immediate_direct_writes(self) -> None:
        # Not connected anywhere; pre-moveToThread seed calls them
        # directly — they must still write _config immediately.
        w = self._worker()
        w.set_volume(0.33)
        w.set_muted(True)
        w.set_af_gain_db(7)
        self.assertAlmostEqual(w._config.volume, 0.33)
        self.assertTrue(w._config.muted)
        self.assertEqual(w._config.af_gain_db, 7)

    def test_drain_is_bounded_snapshot(self) -> None:
        # A command that posts another command must NOT have the
        # second run in the same drain pass (snapshot, not
        # re-entrant) — else a self-feeding burst could stall the
        # worker loop.
        w = self._worker()
        order = []

        def first():
            order.append("first")
            w._post_ctl(lambda: order.append("second"))

        w._post_ctl(first)
        w._drain_ctl()
        self.assertEqual(order, ["first"])     # second deferred
        w._drain_ctl()
        self.assertEqual(order, ["first", "second"])

    def test_command_error_isolated(self) -> None:
        w = self._worker()
        hits = []
        w._post_ctl(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        w._post_ctl(lambda: hits.append(1))
        w._drain_ctl()                         # must not raise
        self.assertEqual(hits, [1])            # later cmd still ran

    def test_no_qcoreapplication_processevents_in_run_loop(self):
        # The pump removal is the whole point — guard against a
        # regression re-adding it.  Check the executable code (drop
        # the docstring, which legitimately *names* the removed
        # call when explaining S3) for the import + the call.
        import ast
        import inspect
        import textwrap
        from lyra.dsp.worker import DspWorker
        src = textwrap.dedent(inspect.getsource(DspWorker.run_loop))
        tree = ast.parse(src)
        fn = tree.body[0]
        # Strip the docstring node so prose mentions don't count.
        body = fn.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]
        code = "\n".join(ast.unparse(n) for n in body)
        self.assertNotIn("processEvents", code)
        self.assertNotIn("QCoreApplication", code)
        self.assertIn("_drain_ctl", code)


if __name__ == "__main__":
    unittest.main()
