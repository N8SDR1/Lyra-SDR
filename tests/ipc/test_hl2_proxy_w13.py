"""W1.3 — cc_cmd C&C-register read-back/control routing.

Pins (the locked v3 + v2-1/v3-A/v3-B + the augmented gate):
EXCLUDED safety/MOX/§15.25 c0 are synchronous pass-through
byte-identical to HEAD (no drain hop — D-W13b/d/e closed);
ROUTED idempotent c0 are deferred and the cc-drain applies
value+slot as ONE atomic pair under the contained `_cc_lock`
in FIFO (D-W13a — a routed c0 is NEVER in `_cc_cycle` before
its `_cc_registers` value, so the EP2 reader can't KeyError);
the ≥1e5-iter mixed-interleave deadlock stress (D-W13c / RA-1,
incl. an excluded-key writer); seed preservation; bounded
teardown; back-compat (cc routing active even with no rx cbs).
No socket: HL2Stream binds only in start(), never called here.
"""
from __future__ import annotations

import threading
import time
import unittest

from lyra.protocol.stream import HL2Stream
from lyra.ipc.hl2_proxy import (
    HL2StreamProxy, _CC_EXCLUDED, _GuardCcRegisters, _GuardCcCycle)


def _wait(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.003)
    return pred()


class _StubStream:
    def __init__(self):
        self._cc_registers = {0x00: (1, 2, 3, 4), 0x2E: (0, 0, 12, 40)}
        self._cc_cycle = [0x00, 0x2E]
        self._cc_lock = threading.Lock()
        self.stop_called = 0

    def start(self, **kw):
        self.start_kw = kw

    def stop(self):
        self.stop_called += 1


class W13CcRoutingTest(unittest.TestCase):
    def _real(self):
        # __init__ binds NO socket (HL2Stream.start does).
        p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
        p._w1_start_cc_routing()
        self.addCleanup(p._w1_teardown_cc)
        return p, p.unwrap()

    # ---- EXCLUDED = synchronous pass-through, HEAD-identical ----
    def test_excluded_keys_are_synchronous_passthrough(self):
        p, s = self._real()
        self.assertIsInstance(s._cc_registers, _GuardCcRegisters)
        self.assertIsInstance(s._cc_cycle, _GuardCcCycle)
        for c0 in (0x12, 0x14, 0x1C, 0x02, 0x08, 0x0A):
            self.assertIn(c0, _CC_EXCLUDED)
            with s._cc_lock:                 # writers self-hold _cc_lock
                s._cc_registers[c0] = (c0, 9, 9, 9)
                if c0 not in s._cc_cycle:
                    s._cc_cycle.append(c0)
            # IMMEDIATELY visible in the real backing — no drain hop
            self.assertEqual(p._w1_cc_real_regs[c0], (c0, 9, 9, 9))
            self.assertIn(c0, p._w1_cc_real_cycle)
            self.assertEqual(s._cc_registers[c0], (c0, 9, 9, 9))

    def test_pa_enable_and_att_disarm_never_deferred(self):
        # D-W13d / D-W13b: 0x12 (PA-enable+drive) and 0x14/0x1C
        # (ATT-on-TX) MUST reach the wire synchronously — a deferred
        # safety-disarm/att-raise would be worse-than-HEAD.
        p, s = self._real()
        with s._cc_lock:
            s._cc_registers[0x12] = (0, 0x4C, 0, 0)   # PA enable
        self.assertEqual(p._w1_cc_real_regs[0x12], (0, 0x4C, 0, 0))
        with s._cc_lock:
            s._cc_registers[0x12] = (0, 0x44, 0, 0)   # PA DISARM
        self.assertEqual(p._w1_cc_real_regs[0x12], (0, 0x44, 0, 0))
        with s._cc_lock:
            s._cc_registers[0x14] = (0, 0, 0, 0x40)   # ATT-on-TX
        self.assertEqual(p._w1_cc_real_regs[0x14], (0, 0, 0, 0x40))

    # ---- ROUTED = deferred, atomic value+slot via cc-drain ----
    def test_routed_real_writer_endtoend(self):
        p, s = self._real()
        s._set_rx1_freq(7_000_000)            # real writer → 0x04 routed
        self.assertTrue(_wait(
            lambda: 0x04 in p._w1_cc_real_regs))
        hz = 7_000_000
        self.assertEqual(p._w1_cc_real_regs[0x04],
                         ((hz >> 24) & 0xFF, (hz >> 16) & 0xFF,
                          (hz >> 8) & 0xFF, hz & 0xFF))
        self.assertIn(0x04, p._w1_cc_real_cycle)   # slot registered
        self.assertEqual(s._cc_registers[0x04],
                         p._w1_cc_real_regs[0x04])  # EP2-style read

    def test_routed_no_keyerror_window(self):
        # A routed c0 must NEVER be in _cc_cycle before its
        # _cc_registers value (else EP2 KeyErrors). Drive routed
        # writes while an EP2-reader emulation runs under _cc_lock.
        p, s = self._real()
        errs = []
        stop = threading.Event()

        def ep2_reader():
            while not stop.is_set():
                with s._cc_lock:
                    cyc = list(s._cc_cycle)
                    for c0 in cyc:
                        try:
                            _ = s._cc_registers[c0]
                        except KeyError as e:
                            errs.append(e)

        rt = threading.Thread(target=ep2_reader, daemon=True)
        rt.start()
        for i in range(3000):
            s._set_rx1_freq(7_000_000 + i)
            s._set_rx2_freq(14_000_000 + i)
        time.sleep(0.2)
        stop.set()
        rt.join(2.0)
        self.assertEqual(errs, [], "EP2-reader KeyError = D-W13a")
        self.assertIn(0x04, p._w1_cc_real_cycle)
        self.assertIn(0x06, p._w1_cc_real_cycle)

    def test_seed_entries_preserved(self):
        # Whatever HL2Stream.__init__ seeds (0x00 general, 0x2e
        # TX-latency, …) must survive the guard swap unchanged
        # (seed-preserve / G1) — capture pre-swap, assert post.
        p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
        s = p.unwrap()
        pre_regs = dict(s._cc_registers)
        pre_cycle = list(s._cc_cycle)
        p._w1_start_cc_routing()
        self.addCleanup(p._w1_teardown_cc)
        self.assertGreaterEqual(len(pre_regs), 1)
        for k, v in pre_regs.items():
            self.assertEqual(s._cc_registers[k], v)
        for c0 in pre_cycle:
            self.assertIn(c0, s._cc_cycle)

    def test_guard_read_delegation(self):
        p, s = self._real()
        g = s._cc_registers
        self.assertEqual(g[0x00], p._w1_cc_real_regs[0x00])
        self.assertIn(0x00, g)
        self.assertEqual(len(g), len(p._w1_cc_real_regs))
        self.assertEqual(set(g.keys()), set(p._w1_cc_real_regs.keys()))
        self.assertEqual(s._cc_cycle[0], p._w1_cc_real_cycle[0])
        self.assertEqual(len(s._cc_cycle), len(p._w1_cc_real_cycle))

    # ---- D-W13c / RA-1: ≥1e5-iter mixed deadlock stress ----
    def test_deadlock_stress_mixed_interleave(self):
        p, s = self._real()
        N_PER = 25_000                        # 4 writers × 25k = 1e5
        errs = []
        stop = threading.Event()

        def routed_writer(base):
            for i in range(N_PER):
                s._set_rx1_freq(base + i)     # _cc_lock → guard → ring.put

        def excluded_writer():
            i = 0
            while not stop.is_set():
                with s._cc_lock:              # pass-through path
                    s._cc_registers[0x12] = (0, (i & 1) and 0x4C or 0x44,
                                             0, 0)
                i += 1

        def ep2_reader():
            while not stop.is_set():
                try:
                    with s._cc_lock:
                        for c0 in list(s._cc_cycle):
                            _ = s._cc_registers[c0]
                except Exception as e:        # incl. KeyError
                    errs.append(e)

        ws = [threading.Thread(target=routed_writer, args=(7_000_000
              + k * 1_000_000,)) for k in range(4)]
        ex = threading.Thread(target=excluded_writer, daemon=True)
        rd = threading.Thread(target=ep2_reader, daemon=True)
        t0 = time.monotonic()
        ex.start(); rd.start()
        for w in ws:
            w.start()
        for w in ws:                          # bounded join = no deadlock
            w.join(timeout=60.0)
            self.assertFalse(w.is_alive(), "DEADLOCK: writer hung")
        stop.set()
        ex.join(5.0); rd.join(5.0)
        self.assertTrue(p._w1_cc_drain_thread.is_alive())  # drain survived
        self.assertEqual(errs, [], "KeyError under stress = D-W13a/c")
        # final convergence: last write of 0x04 lands
        self.assertTrue(_wait(lambda: 0x04 in p._w1_cc_real_regs, 5.0))
        self.assertLess(time.monotonic() - t0, 90.0)


class W13LifecycleTest(unittest.TestCase):
    def test_backcompat_cc_active_even_with_no_rx_cbs(self):
        p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
        p._w1_stream = _StubStream()
        p.start()                              # no rx cbs
        self.addCleanup(p._w1_teardown_cc)
        self.assertIsNotNone(p._w1_cc)         # cc routing active
        self.assertTrue(p._w1_cc_drain_thread.is_alive())
        self.assertIsInstance(p._w1_stream._cc_registers, _GuardCcRegisters)

    def test_stop_joins_cc_drain_bounded_and_frees_ring(self):
        p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
        p._w1_stream = _StubStream()
        p.start()
        t = p._w1_cc_drain_thread
        self.assertTrue(t.is_alive())
        t0 = time.monotonic()
        p.stop()
        self.assertLess(time.monotonic() - t0, 4.0)   # bounded
        self.assertEqual(p._w1_stream.stop_called, 1)
        self.assertFalse(t.is_alive())
        self.assertIsNone(p._w1_cc)
        self.assertIsNone(p._w1_cc_drain_thread)

    def test_cc_routing_idempotent(self):
        p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
        p._w1_stream = _StubStream()
        p._w1_start_cc_routing()
        self.addCleanup(p._w1_teardown_cc)
        ring1 = p._w1_cc
        p._w1_start_cc_routing()               # second call = no-op
        self.assertIs(p._w1_cc, ring1)


if __name__ == "__main__":
    unittest.main()
