"""W0 ACCEPTANCE GATE — two-process correctness of the ring.

These are the gates the red-team mandated (CLAUDE.md §15.26 v3):

  1. tear / reorder fuzz: a SEPARATE process hammers the ring;
     the consumer asserts every record is internally intact
     (crc, no torn payload) and strictly monotonic (no reorder,
     no duplication) — under genuine cross-process load.

  2. kill-the-writer-while-it-holds-the-lock (the NEW hazard the
     D1 barrier fix introduces — Windows multiprocessing.Lock is
     an ownerless semaphore): the consumer's bounded acquire MUST
     escape within the timeout as RingPeerLost and NOT hang.

In-process unit coverage is in test_ring.py.  Nothing here is
wired into the radio/wire path — W0 is standalone.
"""
from __future__ import annotations

import multiprocessing as _mp
import struct
import time
import unittest
import zlib

from lyra.ipc import Ring, RingPeerLost, RingClosed

_CTX = _mp.get_context("spawn")
_HDR = struct.Struct("<QI")          # seq, crc32(body)


# ── spawned children (module-level so spawn can import them) ──────
def _fuzz_writer(shm_name: str, lock, n: int) -> None:
    r = Ring.attach(shm_name, lock, drop_oldest=True)
    try:
        for i in range(n):
            body = (b"%d:" % i) * ((i % 11) + 1)
            payload = _HDR.pack(i, zlib.crc32(body)) + body
            # bounded so a wedged consumer can't hang the writer
            try:
                r.put(payload, type_id=i & 0xFF, timeout=5.0)
            except RingPeerLost:
                return
        r.mark_closed(timeout=5.0)
    finally:
        r.close()


def _lock_hog(shm_name: str, lock, held_evt) -> None:
    r = Ring.attach(shm_name, lock)
    r._lock.acquire()                # grab + never release == "died holding"
    held_evt.set()
    while True:
        time.sleep(1.0)


class RingTwoProcessTest(unittest.TestCase):
    def test_tear_reorder_fuzz_cross_process(self):
        n = 4000
        r = Ring.create(128, 256, drop_oldest=True, lock=_CTX.Lock())
        w = _CTX.Process(target=_fuzz_writer,
                         args=(r.shm_name, r.lock, n))
        w.start()
        try:
            got = 0
            last_seq = -1
            deadline = time.monotonic() + 30.0
            while True:
                if time.monotonic() > deadline:
                    self.fail("fuzz consumer timed out")
                try:
                    rec = r.get(timeout=1.0)
                except RingClosed:
                    break
                if rec is None:
                    if not w.is_alive():
                        # writer done; one more drain pass then stop
                        try:
                            rec = r.get(timeout=0.2)
                        except RingClosed:
                            break
                        if rec is None:
                            break
                    else:
                        continue
                seq, sgen, type_id, payload = rec
                hseq, hcrc = _HDR.unpack(payload[:_HDR.size])
                body = payload[_HDR.size:]
                # no torn payload
                self.assertEqual(zlib.crc32(body), hcrc,
                                 "torn payload across the process boundary")
                # the slot seq is the producer's free-running index:
                # strictly increasing => no reorder, no duplication
                self.assertGreater(seq, last_seq,
                                   "reorder/duplication across the boundary")
                last_seq = seq
                self.assertEqual(type_id, hseq & 0xFF)
                got += 1
            w.join(5.0)
            self.assertFalse(w.is_alive())
            # drop_oldest is allowed to lose records; it must NEVER
            # corrupt or reorder them, and must deliver a healthy
            # fraction under load.
            self.assertGreater(got, n // 10,
                               f"only {got}/{n} survived — suspicious loss")
        finally:
            if w.is_alive():
                w.terminate()
                w.join(5.0)
            r.close()

    def test_kill_writer_holding_lock_does_not_hang_consumer(self):
        r = Ring.create(64, 8, lock=_CTX.Lock())
        held = _CTX.Event()
        w = _CTX.Process(target=_lock_hog,
                         args=(r.shm_name, r.lock, held))
        w.start()
        try:
            self.assertTrue(held.wait(10.0), "child never grabbed the lock")
            w.terminate()                      # die WHILE holding the lock
            w.join(5.0)
            self.assertFalse(w.is_alive())
            # The ownerless semaphore is now never released.  A
            # bounded get() MUST surface RingPeerLost within ~the
            # timeout and not hang (A1: timeout == peer-death).
            t0 = time.monotonic()
            with self.assertRaises(RingPeerLost):
                r.get(timeout=0.3)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 3.0,
                            f"consumer hung {elapsed:.2f}s on a dead peer")
        finally:
            if w.is_alive():
                w.terminate()
                w.join(5.0)
            r.close()


if __name__ == "__main__":
    unittest.main()
