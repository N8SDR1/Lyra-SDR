"""W0 — in-process correctness of the barrier-bearing SPSC ring.

Pins: FIFO order, wrap-around, full-without-drop returns False,
drop-oldest, generation discard (D5), clean-close → RingClosed,
bounded-acquire-timeout → RingPeerLost (A1), payload-cap guard,
depth.  Two-process tear/reorder + kill-writer-holding-lock gates
are in test_ring_twoproc.py.
"""
from __future__ import annotations

import threading
import unittest

from lyra.ipc import Ring, RingPeerLost, RingClosed


class RingInProcessTest(unittest.TestCase):
    def _ring(self, slot_size=64, n_slots=4, drop_oldest=False):
        r = Ring.create(slot_size, n_slots, drop_oldest=drop_oldest)
        self.addCleanup(r.close)
        return r

    def test_fifo_roundtrip(self):
        r = self._ring()
        self.assertTrue(r.put(b"alpha", type_id=7))
        self.assertTrue(r.put(b"beta"))
        seq0, gen0, t0, p0 = r.get(timeout=1.0)
        seq1, gen1, t1, p1 = r.get(timeout=1.0)
        self.assertEqual((p0, t0), (b"alpha", 7))
        self.assertEqual(p1, b"beta")
        self.assertLess(seq0, seq1)               # monotonic
        self.assertIsNone(r.get(timeout=0.05))    # drained

    def test_wraparound_many_cycles(self):
        r = self._ring(n_slots=4)
        for i in range(40):                       # 10× the ring
            self.assertTrue(r.put(str(i).encode()))
            _s, _g, _t, p = r.get(timeout=1.0)
            self.assertEqual(p, str(i).encode())

    def test_full_without_drop_returns_false(self):
        r = self._ring(n_slots=3, drop_oldest=False)
        self.assertTrue(r.put(b"a"))
        self.assertTrue(r.put(b"b"))
        self.assertTrue(r.put(b"c"))
        self.assertFalse(r.put(b"d"))             # full, not dropped
        self.assertEqual(r.get(timeout=1.0)[3], b"a")  # oldest intact

    def test_drop_oldest(self):
        r = self._ring(n_slots=3, drop_oldest=True)
        for c in (b"a", b"b", b"c", b"d", b"e"):
            self.assertTrue(r.put(c))             # never blocks/fails
        got = []
        while (rec := r.get(timeout=0.1)) is not None:
            got.append(rec[3])
        self.assertEqual(got, [b"c", b"d", b"e"])  # oldest two dropped
        # seqs still strictly monotonic across the drop
        r2 = self._ring(n_slots=2, drop_oldest=True)
        for i in range(6):
            r2.put(str(i).encode())
        seqs = []
        while (rec := r2.get(timeout=0.1)) is not None:
            seqs.append(rec[0])
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_generation_discard(self):
        r = self._ring(n_slots=8)
        r.put(b"old")                              # gen 0
        r.set_generation(5)
        r.put(b"new")                              # gen 5
        # consumer expecting gen 5 silently drops the gen-0 straggler
        rec = r.get(timeout=1.0, expected_generation=5)
        self.assertEqual(rec[3], b"new")
        self.assertEqual(rec[1], 5)
        self.assertIsNone(r.get(timeout=0.05, expected_generation=5))

    def test_clean_close_raises_ringclosed_when_drained(self):
        r = self._ring()
        r.put(b"last")
        r.mark_closed()
        self.assertEqual(r.get(timeout=1.0)[3], b"last")
        with self.assertRaises(RingClosed):
            r.get(timeout=1.0)

    def test_bounded_acquire_timeout_raises_peerlost(self):
        # Simulate the peer dying while holding the lock: a thread
        # grabs the underlying lock and never releases it.  A
        # bounded get() must raise RingPeerLost promptly, NOT hang.
        r = self._ring()
        held = threading.Event()
        stop = threading.Event()

        def hog():
            r._lock.acquire()
            held.set()
            stop.wait(5.0)
            r._lock.release()

        t = threading.Thread(target=hog, daemon=True)
        t.start()
        self.assertTrue(held.wait(2.0))
        import time
        t0 = time.monotonic()
        with self.assertRaises(RingPeerLost):
            r.get(timeout=0.2)
        self.assertLess(time.monotonic() - t0, 1.5)   # bounded, no hang
        stop.set()
        t.join(2.0)

    def test_get_requires_finite_positive_timeout(self):
        r = self._ring()
        with self.assertRaises(ValueError):
            r.get(timeout=0)
        with self.assertRaises(ValueError):
            r.get(timeout=None)                    # type: ignore[arg-type]

    def test_payload_capacity_guard(self):
        r = self._ring(slot_size=32)               # cap = 32-24 = 8
        self.assertEqual(r.payload_capacity, 8)
        self.assertTrue(r.put(b"12345678"))
        with self.assertRaises(ValueError):
            r.put(b"123456789")                    # 9 > 8

    def test_depth(self):
        r = self._ring(n_slots=8)
        self.assertEqual(r.depth(), 0)
        r.put(b"x")
        r.put(b"y")
        self.assertEqual(r.depth(), 2)
        r.get(timeout=1.0)
        self.assertEqual(r.depth(), 1)


if __name__ == "__main__":
    unittest.main()
