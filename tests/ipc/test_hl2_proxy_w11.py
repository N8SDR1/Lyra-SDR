"""W1.1 — rx_iq read-back routing through the proxy.

Pins: producer-shim→ring→drain→real-callback round-trip
(bit-exact, stats=None per the verified contract), monotonic /
non-corrupt under drop-oldest overrun, a wedged consumer NEVER
blocks/raises into the rx-loop producer, a RingPeerLost on the
drain is log-once + re-arm (NO D3 / NO force_release_all — v3-4,
no-worse-than-HEAD), start() registers the SHIMS (not the raw
cbs) on the contained stream, stop() joins the drain bounded +
frees the ring, back-compat (no cbs ⇒ straight delegate), and
the not-yet-routed TX path is still pure delegation.  No socket:
HL2Stream binds only in start(), never called here.
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from lyra.ipc.hl2_proxy import HL2StreamProxy


class _StubStream:
    """Minimal stand-in for the contained HL2Stream (no socket /
    no threads).  Records start/stop and the kwargs it received."""

    def __init__(self):
        self.start_kw = None
        self.stop_called = 0

    def start(self, on_samples=None, on_rx2_samples=None,
              dispatch_state_provider=None, **kw):
        self.start_kw = dict(on_samples=on_samples,
                             on_rx2_samples=on_rx2_samples,
                             dispatch_state_provider=dispatch_state_provider,
                             **kw)

    def stop(self):
        self.stop_called += 1


def _proxy(stub: bool = False):
    p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
    if stub:
        p._w1_stream = _StubStream()          # _PROXY_OWN ⇒ object.__setattr__
    return p


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class W11RxIqRoutingTest(unittest.TestCase):
    def _teardown_routing(self, p):
        p._w1_stop = True
        t = p._w1_rx_drain_thread
        if t is not None:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())
        if p._w1_rx_iq is not None:
            p._w1_rx_iq.close()

    def test_roundtrip_bitexact_stats_none(self):
        p = _proxy()
        got = []
        shim0, shim2 = p._w1_start_rx_routing(
            lambda s, st: got.append((s, st)), None)
        self.addCleanup(self._teardown_routing, p)
        self.assertIsNone(shim2)              # no rx2 cb ⇒ no shim
        arrs = [np.arange(i, i + 8, dtype=np.complex64) + 1j * i
                for i in range(5)]
        for a in arrs:
            shim0(a, object())                # rx-loop-thread call
        self.assertTrue(_wait(lambda: len(got) == 5))
        for (s, st), a in zip(got, arrs):
            self.assertIsNone(st)             # stats not routed in W1.1
            np.testing.assert_array_equal(s, a)
            self.assertEqual(s.dtype, np.complex64)

    def test_two_routes_independent(self):
        p = _proxy()
        g1, g2 = [], []
        shim0, shim2 = p._w1_start_rx_routing(
            lambda s, st: g1.append(s), lambda s, st: g2.append(s))
        self.addCleanup(self._teardown_routing, p)
        self.assertIsNotNone(shim2)
        shim0(np.full(4, 1, np.complex64), None)
        shim2(np.full(4, 2, np.complex64), None)
        self.assertTrue(_wait(lambda: g1 and g2))
        self.assertEqual(g1[0][0], 1 + 0j)
        self.assertEqual(g2[0][0], 2 + 0j)

    def test_monotonic_non_corrupt_under_overrun(self):
        # Feed far faster than the drain; drop-oldest may lose
        # records but delivered ones must be intact + strictly
        # ordered (no reorder/dup/tear across the ring).
        p = _proxy()
        got = []
        shim0, _ = p._w1_start_rx_routing(lambda s, st: got.append(s), None)
        self.addCleanup(self._teardown_routing, p)
        n = 2000
        for i in range(n):
            shim0(np.array([i], dtype=np.complex64), None)
        self.assertTrue(_wait(lambda: len(got) > 0))
        time.sleep(0.2)
        vals = [int(s[0].real) for s in got]
        self.assertEqual(vals, sorted(vals))          # monotonic
        self.assertEqual(len(vals), len(set(vals)))    # no dup
        self.assertTrue(all(0 <= v < n for v in vals))  # no tear

    def test_wedged_consumer_never_blocks_or_raises_producer(self):
        # Consumer (drain) stalled holding the ring lock ⇒ the
        # producer shim (rx-loop thread) must drop+log, NEVER block
        # or raise (the rx-loop must never break).
        p = _proxy()
        shim0, _ = p._w1_start_rx_routing(lambda s, st: None, None)
        self.addCleanup(self._teardown_routing, p)
        p._w1_rx_iq._lock.acquire()           # simulate wedged consumer
        try:
            t0 = time.monotonic()
            shim0(np.zeros(4, np.complex64), None)   # must return
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            p._w1_rx_iq._lock.release()

    def test_drain_ringpeerlost_is_logged_rearm_no_d3(self):
        # A RingPeerLost on the drain (lock contention) must NOT
        # kill the drain thread, NOT raise, NOT trigger any
        # D3/force-release — log-once + re-arm, then resume.
        p = _proxy()
        got = []
        shim0, _ = p._w1_start_rx_routing(lambda s, st: got.append(s), None)
        self.addCleanup(self._teardown_routing, p)
        # Hold the lock long enough to force ≥1 drain get-timeout.
        p._w1_rx_iq._lock.acquire()
        time.sleep(0.35)                      # > the 0.1 s get timeout
        p._w1_rx_iq._lock.release()
        self.assertTrue(p._w1_rx_drain_thread.is_alive())   # survived
        shim0(np.full(2, 7, np.complex64), None)
        self.assertTrue(_wait(lambda: got and got[0][0] == 7 + 0j))

    def test_start_registers_shims_not_raw_cbs(self):
        p = _proxy(stub=True)
        user0, user2 = (lambda s, st: None), (lambda s, st: None)
        p.start(on_samples=user0, on_rx2_samples=user2,
                rx_freq_hz=7000000, lna_gain_db=20,
                dispatch_state_provider=lambda: None)
        self.addCleanup(self._teardown_routing, p)
        kw = p._w1_stream.start_kw
        self.assertIsNotNone(kw)
        self.assertIsNot(kw["on_samples"], user0)         # shim, not raw
        self.assertIsNot(kw["on_rx2_samples"], user2)
        self.assertEqual(kw["rx_freq_hz"], 7000000)       # passthrough
        self.assertEqual(kw["lna_gain_db"], 20)
        self.assertTrue(callable(kw["dispatch_state_provider"]))
        # the registered shim still delivers to the real user cb
        got = []
        p._w1_rx1_cb = lambda s, st: got.append(s)
        kw["on_samples"](np.full(3, 9, np.complex64), object())
        self.assertTrue(_wait(lambda: got and got[0][0] == 9 + 0j))

    def test_backcompat_no_cbs_straight_delegate(self):
        p = _proxy(stub=True)
        p.start(rx_freq_hz=14000000)
        self.assertIsNone(p._w1_rx_drain_thread)          # no routing
        self.assertIsNone(p._w1_rx_iq)
        self.assertEqual(p._w1_stream.start_kw["on_samples"], None)
        self.assertEqual(p._w1_stream.start_kw["rx_freq_hz"], 14000000)

    def test_stop_joins_drain_bounded_and_frees_ring(self):
        p = _proxy(stub=True)
        p.start(on_samples=lambda s, st: None)
        t = p._w1_rx_drain_thread
        self.assertTrue(t.is_alive())
        t0 = time.monotonic()
        p.stop()
        self.assertLess(time.monotonic() - t0, 3.0)       # bounded
        self.assertEqual(p._w1_stream.stop_called, 1)
        self.assertFalse(t.is_alive())
        self.assertIsNone(p._w1_rx_drain_thread)
        self.assertIsNone(p._w1_rx_iq)

    def test_tx_path_still_pure_delegation(self):
        # W1.1 only routes rx_iq; the TX/cc path is untouched
        # (W1.3/W1.4).  Those methods must still be the contained
        # stream's own bound methods.
        p = _proxy()
        inner = p.unwrap()
        # callables: a fresh bound-method object per getattr, so
        # compare __self__/__func__ (delegates to the inner stream).
        for name in ("queue_tx_audio", "queue_tx_iq", "_send_cc",
                     "_set_tx_freq", "set_pa_on"):
            m = getattr(p, name)
            self.assertIs(m.__self__, inner, f"{name} not delegated")
            self.assertIs(m.__func__, getattr(type(inner), name))
            self.assertIsNone(getattr(HL2StreamProxy, name, None),
                              f"{name} must NOT be shadowed by the proxy")
        # raw object reach-in is the SAME deque (stable identity)
        self.assertIs(p._tx_audio, inner._tx_audio)


if __name__ == "__main__":
    unittest.main()
