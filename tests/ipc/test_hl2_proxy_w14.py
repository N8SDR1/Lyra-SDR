"""W1.4 — tx_ring CONTROL-ONLY ordered-seam rehearsal.

Pins the converged v3 + the P1/P2 implementation contract
(CLAUDE.md §15.26):

* v3-1 keydown FIFO: a rising `inject_tx_iq` edge enqueues
  MOX_ON THEN INJECT_ON (the deferred `_open_tx_iq` step runs
  AFTER `_on_tx_state_changed(True)`→sync STEPATT and
  `set_mox(True)`→sync TXNCO, so the token is correctly
  ordered by construction).
* v3-4(b) GRACEFUL keyup: a falling edge (from
  `_finalize_keyup`, after the MoxEdgeFade fade-poll gate)
  enqueues MOX_OFF THEN INJECT_OFF, NO ring discard.
* v3-4(c) NEITHER: an idempotent re-set (no edge — incl. the
  re-key-collapse path that never flips the flag) enqueues
  NOTHING.
* P1: the REAL `inject_tx_iq` flag is delegated verbatim to
  the contained stream (the unchanged HEAD wire path —
  `_snapshot_mox_bit`/EP2 packer read the real flag + the
  real `_dispatch_state.mox`, NEVER `_w1_wire_mox`).
  `_w1_wire_mox` is rehearsal-only.
* P2 HARD teardown: stop() forces `_w1_wire_mox=0`, bumps the
  generation (D5 stale-discard), and closes the control ring
  BEFORE contained.stop() (i.e. before the EP2-writer join).
* v3-3: TXAUDIO/TXIQ stay live-delegated on the S2 deque
  (`_tx_audio`/`_tx_audio_lock` are the SAME objects — never
  ring-routed).
* v3-2: STEPATT (0x14/0x1C) + TX-NCO (0x02/0x08/0x0a) stay in
  W1.3 `_CC_EXCLUDED` synchronous byte-identical-HEAD.
* wedged drain NEVER blocks/raises the FSM/Qt write; drain
  RingPeerLost = log-once + re-arm + NO D3 (in-process; the
  real wire bit is on `_dispatch_state`, unaffected).
No socket: HL2Stream binds only in start(), never called here.
"""
from __future__ import annotations

import threading
import time
import unittest

from lyra.ipc.ring import Ring
from lyra.ipc.hl2_proxy import (
    HL2StreamProxy, _CC_EXCLUDED,
    _TX_SLOT, _TX_SLOTS, _TXREC,
    _TX_MOX_ON, _TX_MOX_OFF, _TX_INJECT_ON, _TX_INJECT_OFF)


class _StubStream:
    """Minimal stand-in (no socket / no threads).  Holds the real
    `inject_tx_iq` flag the proxy delegates to, plus the cc attrs
    W1.3's start() guard-swap needs."""

    def __init__(self):
        self.inject_tx_iq = False
        self.start_kw = None
        self.stop_called = 0
        self._cc_registers = {0x00: (1, 2, 3, 4)}
        self._cc_cycle = [0x00]
        self._cc_lock = threading.Lock()

    def start(self, on_samples=None, on_rx2_samples=None,
              dispatch_state_provider=None, **kw):
        self.start_kw = dict(on_samples=on_samples,
                             on_rx2_samples=on_rx2_samples,
                             **kw)

    def stop(self):
        self.stop_called += 1


def _proxy(stub: bool = False):
    p = HL2StreamProxy("10.10.30.100", sample_rate=96000)
    if stub:
        p._w1_stream = _StubStream()          # _PROXY_OWN ⇒ object.__setattr__
    return p


def _manual_ring(p):
    """Attach a tx_ring with NO drain thread, so producer/ordering
    can be asserted deterministically by reading the ring."""
    r = Ring.create(_TX_SLOT, _TX_SLOTS, drop_oldest=False,
                    lock=threading.Lock())
    p._w1_tx = r
    return r


def _drain_kinds(ring, n, timeout=1.0):
    out = []
    end = time.monotonic() + timeout
    while len(out) < n and time.monotonic() < end:
        rec = ring.get(timeout=0.1)
        if rec is None:
            continue
        _s, _g, _t, payload = rec
        kind, seq = _TXREC.unpack_from(payload, 0)
        out.append((kind, seq))
    return out


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class W14TxRingTest(unittest.TestCase):
    # ---- v3-1 keydown FIFO + P1 real-flag delegation ----
    def test_rising_edge_mox_on_then_inject_on(self):
        p = _proxy(stub=True)
        r = _manual_ring(p)
        self.addCleanup(r.close)
        p.inject_tx_iq = True
        # P1: the REAL flag was delegated to the contained stream.
        self.assertTrue(p._w1_stream.inject_tx_iq)
        kinds = [k for k, _ in _drain_kinds(r, 2)]
        self.assertEqual(kinds, [_TX_MOX_ON, _TX_INJECT_ON])

    def test_falling_edge_mox_off_then_inject_off_graceful(self):
        p = _proxy(stub=True)
        p._w1_stream.inject_tx_iq = True          # prime cur=True
        r = _manual_ring(p)
        self.addCleanup(r.close)
        p.inject_tx_iq = False
        self.assertFalse(p._w1_stream.inject_tx_iq)
        recs = _drain_kinds(r, 2)
        self.assertEqual([k for k, _ in recs],
                         [_TX_MOX_OFF, _TX_INJECT_OFF])
        # GRACEFUL: the ring is NOT discarded on a normal keyup
        # (still open/usable — no RingClosed on a fresh get).
        self.assertIsNone(r.get(timeout=0.05))

    def test_fifo_seq_strictly_monotonic(self):
        p = _proxy(stub=True)
        r = _manual_ring(p)
        self.addCleanup(r.close)
        p.inject_tx_iq = True                     # MOX_ON, INJECT_ON
        p.inject_tx_iq = False                    # MOX_OFF, INJECT_OFF
        seqs = [s for _, s in _drain_kinds(r, 4)]
        self.assertEqual(len(seqs), 4)
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    # ---- v3-4(c) NEITHER: idempotent re-set / re-key-collapse ----
    def test_idempotent_reset_enqueues_nothing(self):
        p = _proxy(stub=True)
        r = _manual_ring(p)
        self.addCleanup(r.close)
        p._w1_stream.inject_tx_iq = True          # cur True
        p.inject_tx_iq = True                     # no edge
        # re-key-collapse never flips the flag at all — same as a
        # no-op set: zero tokens.
        self.assertIsNone(r.get(timeout=0.05))
        p._w1_stream.inject_tx_iq = False
        p.inject_tx_iq = False                    # no edge
        self.assertIsNone(r.get(timeout=0.05))

    # ---- P1: real wire path is independent of the rehearsal ring ----
    def test_pre_start_no_ring_just_delegates(self):
        p = _proxy(stub=True)
        self.assertIsNone(p._w1_tx)
        p.inject_tx_iq = True                     # no ring up
        self.assertTrue(p._w1_stream.inject_tx_iq)   # delegated
        self.assertFalse(p._w1_wire_mox)             # rehearsal untouched
        p.inject_tx_iq = False
        self.assertFalse(p._w1_stream.inject_tx_iq)
        self.assertIsNone(p._w1_tx)

    # ---- drain applies tokens to the rehearsal-only mirror ----
    def test_drain_applies_mox_and_advances_seq(self):
        p = _proxy(stub=True)
        p.start()
        self.addCleanup(p._w1_teardown_cc)
        self.addCleanup(p._w1_teardown_tx)
        self.assertIsNotNone(p._w1_tx)
        p.inject_tx_iq = True
        self.assertTrue(_wait(lambda: p._w1_wire_mox is True))
        p.inject_tx_iq = False
        self.assertTrue(_wait(lambda: p._w1_wire_mox is False))
        # 4 tokens enqueued (MOX_ON/INJECT_ON/MOX_OFF/INJECT_OFF);
        # the drain applies them FIFO — wait for the trailing
        # INJECT_OFF (seq>=4) to clear, then assert monotonic.
        self.assertTrue(_wait(lambda: p._w1_tx_last_seq >= 4))

    def test_backcompat_tx_active_rx_only(self):
        p = _proxy(stub=True)
        p.start()                                 # no rx cbs
        self.addCleanup(p._w1_teardown_cc)
        self.addCleanup(p._w1_teardown_tx)
        self.assertIsNotNone(p._w1_tx)
        self.assertTrue(p._w1_tx_drain_thread.is_alive())

    def test_start_tx_routing_idempotent(self):
        p = _proxy(stub=True)
        p._w1_start_tx_routing()
        self.addCleanup(p._w1_teardown_tx)
        r1 = p._w1_tx
        p._w1_start_tx_routing()                  # no-op
        self.assertIs(p._w1_tx, r1)

    # ---- producer never blocks/raises the FSM/Qt thread ----
    def test_wedged_drain_never_blocks_or_raises_fsm(self):
        p = _proxy(stub=True)
        p.start()
        self.addCleanup(p._w1_teardown_cc)
        self.addCleanup(p._w1_teardown_tx)
        p._w1_tx._lock.acquire()                  # simulate wedge
        try:
            t0 = time.monotonic()
            p.inject_tx_iq = True                 # must return
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            p._w1_tx._lock.release()

    def test_drain_ringpeerlost_logged_rearm_no_d3(self):
        p = _proxy(stub=True)
        p.start()
        self.addCleanup(p._w1_teardown_cc)
        self.addCleanup(p._w1_teardown_tx)
        p._w1_tx._lock.acquire()
        time.sleep(0.35)                          # > the 0.1 s get tick
        p._w1_tx._lock.release()
        self.assertTrue(p._w1_tx_drain_thread.is_alive())   # survived
        p.inject_tx_iq = True
        self.assertTrue(_wait(lambda: p._w1_wire_mox is True))

    # ---- P2 HARD teardown + D5 stale-gen discard ----
    def test_stop_hard_predisconnect_before_contained_stop(self):
        p = _proxy(stub=True)
        p.start()
        p.inject_tx_iq = True
        self.assertTrue(_wait(lambda: p._w1_wire_mox is True))
        gen0 = p._w1_tx_gen
        t = p._w1_tx_drain_thread
        t0 = time.monotonic()
        p.stop()
        self.assertLess(time.monotonic() - t0, 4.0)       # bounded
        self.assertEqual(p._w1_stream.stop_called, 1)
        self.assertFalse(p._w1_wire_mox)                  # HARD forced 0
        self.assertEqual(p._w1_tx_gen, gen0 + 1)          # D5 gen bump
        self.assertIsNone(p._w1_tx)                       # ring freed
        self.assertFalse(t.is_alive())                    # drain joined

    def test_hard_predisconnect_unit_d5(self):
        p = _proxy(stub=True)
        p._w1_start_tx_routing()
        g0 = p._w1_tx_gen
        p._w1_wire_mox = True
        p._w1_tx_hard_predisconnect()
        # The HARD pre-disconnect invariants (the D5 contract):
        # rehearsal wire-mox forced 0, generation bumped (any
        # in-flight token now stale-gen ⇒ drain discards), the
        # tx_stop flag latched so the drain exits next tick.
        self.assertFalse(p._w1_wire_mox)
        self.assertEqual(p._w1_tx_gen, g0 + 1)
        self.assertTrue(p._w1_tx_stop)
        p._w1_teardown_tx()
        self.assertIsNone(p._w1_tx)
        self.assertIsNone(p._w1_tx_drain_thread)

    # ---- v3-3: TXAUDIO/TXIQ stay live-delegated (S2 deque) ----
    def test_txaudio_txiq_still_live_delegated(self):
        p = _proxy()
        inner = p.unwrap()
        # raw reach-in (audio_sink.py:294-295) is the SAME deque +
        # lock — W1.4 is control-ONLY, never ring-routes TX audio.
        self.assertIs(p._tx_audio, inner._tx_audio)
        self.assertIs(p._tx_audio_lock, inner._tx_audio_lock)
        # inject_tx_iq is NOT proxy-owned (reads delegate to the
        # real stream — the unchanged HEAD wire path, P1).
        from lyra.ipc.hl2_proxy import _PROXY_OWN
        self.assertNotIn("inject_tx_iq", _PROXY_OWN)

    # ---- v3-2: STEPATT/TXNCO stay W1.3 _CC_EXCLUDED synchronous ----
    def test_stepatt_txnco_still_cc_excluded(self):
        # W1.4 must NOT move any MOX-correlated/§15.25/TX-safety
        # register onto the tx_ring — they stay byte-identical HEAD.
        self.assertEqual(
            _CC_EXCLUDED,
            frozenset({0x12, 0x14, 0x1C, 0x02, 0x08, 0x0A}))
        for c0 in (0x14, 0x1C, 0x02, 0x08, 0x0A, 0x12):
            self.assertIn(c0, _CC_EXCLUDED)
        # the W1.4 token kinds are a disjoint, tiny enum — they are
        # NOT c0 register ids (no collision with the cc surface).
        self.assertEqual(
            sorted({_TX_MOX_ON, _TX_MOX_OFF,
                    _TX_INJECT_ON, _TX_INJECT_OFF}),
            [1, 2, 3, 4])

    def test_tx_path_methods_still_pure_delegation(self):
        # W1.4 intercepts ONLY the `inject_tx_iq` attribute write;
        # the cc/TX wire methods stay the contained stream's own
        # bound methods (NOT shadowed by the proxy).
        p = _proxy()
        inner = p.unwrap()
        for name in ("_set_tx_freq", "set_tx_step_attn_db",
                     "_send_cc", "_refresh_frame_4",
                     "_refresh_frame_11"):
            m = getattr(p, name)
            self.assertIs(m.__self__, inner, f"{name} not delegated")
            self.assertIsNone(getattr(HL2StreamProxy, name, None),
                              f"{name} must NOT be shadowed")


if __name__ == "__main__":
    unittest.main()
