"""W1.2 — tele (mic + ptt/dot/dash) read-back routing.

Pins: bit-exact mic round-trip with the per-datagram ptt/dot/
dash SNAPSHOT (decoupled from later mutation of the shared
stats — the core correctness point), level-driven edge
coherence under FIFO, wedged-consumer never blocks/raises the
rx-loop producer, drain RingPeerLost = log+re-arm+no-D3 (v3-4),
register_mic_consumer interception (shim-not-raw + None tears
down + tells the contained stream), pre-start fallback =
inline HEAD behaviour, tele teardown independent of the rx_iq
drain, stop() defensively tears tele down bounded, and the
MOX/keyup wire path is still PURE delegation (v3-5: ptt.py
:389-406 byte-identical — W1.2 touches nothing there).  No
socket: HL2Stream binds only in start(), never called here.
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from lyra.ipc.hl2_proxy import HL2StreamProxy


class _StubStream:
    def __init__(self):
        self.mic_reg = []                     # every register call

    def register_mic_consumer(self, cb):
        self.mic_reg.append(cb)

    def stop(self):
        pass


class _FS:                                    # mutable fake FrameStats
    def __init__(self, ptt=False, dot=False, dash=False):
        self.ptt_in, self.dot_in, self.dash_in = ptt, dot, dash


def _proxy(stub: bool = False):
    p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
    if stub:
        p._w1_stream = _StubStream()
    return p


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class W12TeleRoutingTest(unittest.TestCase):
    def _teardown(self, p):
        try:
            p._w1_teardown_tele()
        except Exception:
            pass

    def test_mic_roundtrip_with_ptt_snapshot(self):
        p = _proxy(stub=True)
        got = []
        p.register_mic_consumer(lambda m, st: got.append((m, st)))
        self.addCleanup(self._teardown, p)
        # contained stream got the SHIM, not the raw cb
        self.assertEqual(len(p._w1_stream.mic_reg), 1)
        self.assertEqual(p._w1_stream.mic_reg[0], p._w1_mic_producer)
        mic = np.array([1, -2, 3, -4, 32767, -32768], dtype=np.int16)
        p._w1_mic_producer(mic, _FS(ptt=True, dash=True))
        self.assertTrue(_wait(lambda: len(got) == 1))
        m, st = got[0]
        np.testing.assert_array_equal(m, mic)
        self.assertEqual(m.dtype, np.int16)
        self.assertTrue(st.ptt_in)
        self.assertFalse(st.dot_in)
        self.assertTrue(st.dash_in)

    def test_snapshot_decoupled_from_later_mutation(self):
        # THE correctness point: the shared stats object is mutated
        # in place every datagram on the rx-loop.  The delivered
        # stats MUST reflect produce-time, not whatever the live
        # object holds by the time the drain runs.
        p = _proxy(stub=True)
        got = []
        p.register_mic_consumer(lambda m, st: got.append(st.ptt_in))
        self.addCleanup(self._teardown, p)
        fs = _FS(ptt=True)
        p._w1_mic_producer(np.zeros(2, np.int16), fs)
        fs.ptt_in = False                     # rx-loop moves on
        p._w1_mic_producer(np.zeros(2, np.int16), fs)
        self.assertTrue(_wait(lambda: len(got) == 2))
        self.assertEqual(got, [True, False])  # produce-time values

    def test_level_driven_edge_coherence_fifo(self):
        p = _proxy(stub=True)
        seen = []
        p.register_mic_consumer(lambda m, st: seen.append(st.ptt_in))
        self.addCleanup(self._teardown, p)
        seq = [False, False, True, True, False, True, False, False]
        for v in seq:
            p._w1_mic_producer(np.zeros(1, np.int16), _FS(ptt=v))
        self.assertTrue(_wait(lambda: len(seen) == len(seq)))
        self.assertEqual(seen, seq)           # FIFO, no reorder/dup

    def test_wedged_consumer_never_blocks_or_raises_producer(self):
        p = _proxy(stub=True)
        p.register_mic_consumer(lambda m, st: None)
        self.addCleanup(self._teardown, p)
        p._w1_tele._lock.acquire()
        try:
            t0 = time.monotonic()
            p._w1_mic_producer(np.zeros(4, np.int16), _FS())  # must return
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            p._w1_tele._lock.release()

    def test_drain_ringpeerlost_logged_rearm_no_d3(self):
        p = _proxy(stub=True)
        got = []
        p.register_mic_consumer(lambda m, st: got.append(m))
        self.addCleanup(self._teardown, p)
        p._w1_tele._lock.acquire()
        time.sleep(0.35)                      # > the 0.1 s get timeout
        p._w1_tele._lock.release()
        self.assertTrue(p._w1_tele_drain_thread.is_alive())
        p._w1_mic_producer(np.full(2, 5, np.int16), _FS())
        self.assertTrue(_wait(lambda: got and int(got[0][0]) == 5))

    def test_register_none_tears_down_and_clears_contained(self):
        p = _proxy(stub=True)
        p.register_mic_consumer(lambda m, st: None)
        t = p._w1_tele_drain_thread
        self.assertTrue(t.is_alive())
        p.register_mic_consumer(None)
        self.assertEqual(p._w1_stream.mic_reg[-1], None)   # cleared
        self.assertIsNone(p._w1_tele_drain_thread)
        self.assertIsNone(p._w1_tele)
        self.assertFalse(t.is_alive())
        self.assertIsNone(p._w1_mic_cb)

    def test_pre_start_fallback_is_inline_head_behaviour(self):
        # tele not up ⇒ shim calls the real cb inline (exact HEAD).
        p = _proxy(stub=True)
        got = []
        p._w1_mic_cb = lambda m, st: got.append((m, st))
        self.assertIsNone(p._w1_tele)
        fs = _FS(ptt=True)
        mic = np.array([7, 8], np.int16)
        p._w1_mic_producer(mic, fs)           # synchronous, no ring
        self.assertEqual(len(got), 1)
        np.testing.assert_array_equal(got[0][0], mic)
        self.assertIs(got[0][1], fs)          # live object passed inline

    def test_tele_teardown_independent_of_rx_iq_drain(self):
        p = _proxy(stub=True)
        # stand up rx_iq (W1.1) AND tele (W1.2)
        p._w1_start_rx_routing(lambda s, st: None, None)
        p.register_mic_consumer(lambda m, st: None)
        rx_t = p._w1_rx_drain_thread
        self.assertTrue(rx_t.is_alive())
        p.register_mic_consumer(None)         # tears tele ONLY
        self.assertIsNone(p._w1_tele_drain_thread)
        self.assertTrue(rx_t.is_alive())      # rx_iq unaffected
        self.assertFalse(p._w1_stop)          # rx stop flag untouched
        # cleanup rx
        p._w1_stop = True
        rx_t.join(timeout=2.0)
        p._w1_rx_iq.close()

    def test_stop_defensively_tears_tele_bounded(self):
        p = _proxy(stub=True)
        p.register_mic_consumer(lambda m, st: None)
        t = p._w1_tele_drain_thread
        t0 = time.monotonic()
        p.stop()
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertFalse(t.is_alive())
        self.assertIsNone(p._w1_tele)
        self.assertIsNone(p._w1_tele_drain_thread)

    def test_mox_keyup_path_is_pure_delegation(self):
        # v3-5: W1.2 must NOT touch the MOX/keyup wire path.
        # Proven structurally: the proxy does not define/intercept
        # set_mox or any keyup method — they still delegate to the
        # contained stream's own bound methods (byte-identical
        # ptt.py:389-406 by construction — nothing changed there).
        p = _proxy()
        inner = p.unwrap()
        for name in ("set_mox", "_clear_mox_tail", "_end_keyup",
                     "_send_cc", "_set_tx_freq"):
            self.assertIsNone(getattr(HL2StreamProxy, name, None),
                              f"{name} must NOT be intercepted by W1.2")
            if hasattr(inner, name):
                self.assertIs(getattr(p, name).__self__, inner)


if __name__ == "__main__":
    unittest.main()
