"""W1.0 — transparent pass-through ``HL2Stream`` proxy.

First sub-stage of the operator-locked process-isolation
architecture (CLAUDE.md §15.26 charter + the W1 design CONVERGED
& LOCKED through 3 red-team rounds / 4 senior agents).

**W1.0 is wire-identical BY CONSTRUCTION.**  This class is a
pure delegating shim: it contains one real ``HL2Stream`` and
forwards *every* attribute read, attribute write, attribute
delete and method call straight to it via ``__getattr__`` /
``__setattr__`` / ``__delattr__``.  It introduces NO rings, NO
extra thread, NO behaviour, NO ordering change — the contained
``HL2Stream`` runs exactly as it does today on its own internal
threads (``_rx_loop`` / ``_ep2_writer_loop``).  The only change
vs HEAD is that ``Radio`` constructs ``HL2StreamProxy(...)``
instead of ``HL2Stream(...)``; the delegation is total so the
EP2 cadence, the S2 timer-paced writer, the S3 ``_ctl_q``, the
§15.25 keydown/keyup ordering and the §15.21 teardown are
byte-identical.

It exists so the W1.1+ sub-stages can interpose ring routing at
the **stream-internal `_cc_registers`/`_tx_audio` mutation
boundary** (NOT at public method names — the red-team D-W1b
silent-W2-landmine finding) one path at a time, each
independently A/B-gated and revertable, with W1 remaining the
fallback for the W2 cross-process move.

Note (W1.0 ONLY): the contained stream IS exposed as a live
object via transparent delegation — that is correct and
required here for wire-identity.  The "do NOT expose
`_tx_audio`/`_cc_registers` as live objects; use guard objects"
requirement (red-team amendment v3-3 / A1) lands at **W1.4**,
when the tx_ring/cc_cmd routing is interposed.  W1.0 changes
nothing.
"""
from __future__ import annotations

import struct
import threading

import numpy as np

from lyra.protocol.stream import HL2Stream
from lyra.ipc.ring import Ring, RingClosed, RingPeerLost

# ── W1.1 rx_iq ring framing ──────────────────────────────────────
# Per-DDC per-datagram RX batch (nddc=4/192k ≈ 38 complex64 ≈
# 304 B).  Slot 4 KB ⇒ ≤511 complex64 headroom (real batches are
# tens); 128 slots, drop-oldest (W0-proven, producer-side) ≈ a
# generous read-back cushion that can never back-pressure the
# rx-loop.  Header is 8 B (aligned for complex64): route, n.
_RX_SLOT = 4096
_RX_SLOTS = 128
_RXHDR = struct.Struct("<II")          # route_id, n_samples (8 B, aligned)
_RX_ROUTE_CH0 = 0
_RX_ROUTE_CH2 = 1

# ── W1.2 tele ring framing ───────────────────────────────────────
# Mic int16 (≈38/datagram @ 48 kHz codec ≈ 76 B) + the per-datagram
# FrameStats fields the mic consumer needs.  Verified
# `Radio._on_hl2_mic` reads ONLY `stats.ptt_in` (and only when the
# opt-in `_hw_ptt_input_enabled` is set, default OFF); ptt_in /
# dot_in / dash_in are snapshotted AT PRODUCE TIME (rx-loop,
# coherent with THIS datagram's mic — the live shared
# `stream.stats` would be a later datagram's value by delivery,
# and is W2-incompatible).  Header 8 B (int16-aligned): n, ptt,
# dot, dash.  Drop-oldest like rx_iq (the rx-loop must NEVER
# block); ptt edge-detect is LEVEL-driven so a dropped record can
# only delay/coalesce an edge, never invert it — under extreme
# overrun a brief pulse loss is no-worse-than-HEAD (a GIL stall
# drops the same datagrams; HW-PTT is opt-in; foot-switch
# hardening is its own §15.26 item).  FrameStats *pull*
# (`proxy.stats`) stays delegated in-process — cross-process
# FrameStats shipping is a W2 concern this ring preps.
_TELE_SLOT = 2048                      # mic ≈76 B/datagram + 8 hdr
_TELE_SLOTS = 256                      # drop-oldest read-back cushion
_TELEHDR = struct.Struct("<IBBBx")     # n_samples, ptt, dot, dash (8 B)


class _TeleStats:
    """Minimal per-datagram stats snapshot the mic consumer sees
    (W1.2 routes mic + the ptt/dot/dash edges only — full
    FrameStats is a W2 tele concern)."""
    __slots__ = ("ptt_in", "dot_in", "dash_in")

    def __init__(self, ptt_in: bool, dot_in: bool, dash_in: bool):
        self.ptt_in = ptt_in
        self.dot_in = dot_in
        self.dash_in = dash_in


# ── W1.3 cc_cmd ring framing + guard objects ─────────────────────
# In-process rehearsal of the W2 cc_cmd boundary.  The contained
# stream's `_cc_registers`/`_cc_cycle` are replaced by guard
# objects that delegate ALL reads to a real private backing and
# route MUTATIONS by C0:
#   EXCLUDED  {0x12,0x14,0x1C,0x02,0x08,0x0a} — MOX-correlated /
#     §15.25-ordered / TX-safety (PA-enable+drive, ATT-on-TX,
#     TX-NCO).  Guard does a TRANSPARENT in-place pass-through to
#     the real backing — NO ring, NO lock (the writer already
#     holds `_cc_lock`; threading.Lock is non-reentrant).
#     Byte-identical to HEAD (v3-A; closes D-W13b/d/e).
#   ROUTED    {0x04,0x06,0x74,0x00} — genuinely-idempotent
#     latest-value (RX1/RX2 NCO, reset_on_disconnect, frame-0
#     general-settings; 0x00 is the G1 tripwire — safe TODAY
#     because no caller mutates it on a MOX/TX-safety edge).
#     Guard enqueues a cc_cmd record; the proxy cc-drain applies
#     `real_registers[c0]=tuple` AND the idempotent
#     `_register_cc_slot` append as ONE atomic pair under the
#     contained `_cc_lock`, in ring FIFO — exactly mirroring
#     HEAD's atomic store+append (v2-1; closes D-W13a, no
#     EP2-reader KeyError).  cc_cmd is NON-drop (idempotent but a
#     distinct-c0 loss would stick); generous, log-once on full
#     (unreachable at the operator-event cc rate).
_CC_EXCLUDED = frozenset({0x12, 0x14, 0x1C, 0x02, 0x08, 0x0A})
_CC_SLOT = 64
_CC_SLOTS = 256
_CCREC = struct.Struct("<BBBBB")       # c0, c1, c2, c3, c4


class _GuardCcRegisters:
    """Drop-in for `HL2Stream._cc_registers` (a dict).  Reads
    delegate to the real backing; `[c0]=v` routes by C0."""
    __slots__ = ("_real", "_ring", "_route_cb")

    def __init__(self, real: dict, ring, route_cb):
        self._real = real
        self._ring = ring
        self._route_cb = route_cb       # (c0, (c1,c2,c3,c4)) -> None

    # reads → real backing (EP2 round-robin reads `[c0]`; defensive
    # full delegation so any uncovered op fails LOUD, never silently
    # bypasses — the D-W1b discipline).
    def __getitem__(self, k):
        return self._real[k]

    def __contains__(self, k):
        return k in self._real

    def __len__(self):
        return len(self._real)

    def __iter__(self):
        return iter(self._real)

    def get(self, k, default=None):
        return self._real.get(k, default)

    def keys(self):
        return self._real.keys()

    def items(self):
        return self._real.items()

    def values(self):
        return self._real.values()

    # the ONLY mutation form (verified exhaustive: no update/pop/
    # clear/setdefault on _cc_registers anywhere in stream.py).
    def __setitem__(self, c0, value):
        if c0 in _CC_EXCLUDED:
            # transparent pass-through — runs INSIDE the writer's
            # already-held `_cc_lock`; byte-identical to HEAD.
            self._real[c0] = value
        else:
            self._route_cb(c0, value)   # enqueue cc_cmd record


class _GuardCcCycle:
    """Drop-in for `HL2Stream._cc_cycle` (a list).  Reads delegate
    to the real backing; `append(c0)` of a ROUTED c0 is swallowed
    (the cc-drain registers the slot atomically with the value, so
    a routed c0 is never in `_cc_cycle` before its
    `_cc_registers` value = no EP2-reader KeyError).  An EXCLUDED
    c0's append passes straight through (byte-identical HEAD)."""
    __slots__ = ("_real",)

    def __init__(self, real: list):
        self._real = real

    def __contains__(self, c0):
        return c0 in self._real

    def __getitem__(self, i):
        return self._real[i]

    def __len__(self):
        return len(self._real)

    def __iter__(self):
        return iter(self._real)

    def append(self, c0):
        if c0 in _CC_EXCLUDED:
            self._real.append(c0)
        # else: routed — the cc-drain does the atomic
        # value+`_register_cc_slot` apply; swallow here so the
        # routed c0 cannot enter the cycle ahead of its value.

#: Instance attributes owned by the proxy itself (everything
#: else is delegated to the contained stream).
_PROXY_OWN = frozenset({
    "_w1_stream",
    # W1.1 rx_iq routing state:
    "_w1_rx1_cb", "_w1_rx2_cb", "_w1_rx_iq",
    "_w1_rx_drain_thread", "_w1_stop", "_w1_rx_lost_logged",
    # W1.2 tele routing state:
    "_w1_mic_cb", "_w1_tele", "_w1_tele_drain_thread",
    "_w1_tele_lost_logged", "_w1_tele_stop",
    # W1.3 cc_cmd routing state:
    "_w1_cc", "_w1_cc_real_regs", "_w1_cc_real_cycle",
    "_w1_cc_drain_thread", "_w1_cc_stop", "_w1_cc_lost_logged",
    "_w1_cc_gen",
})


class HL2StreamProxy:
    """Transparent delegate around one real :class:`HL2Stream`.

    Forwards the complete public *and* private surface (the
    `_tx_audio` / `_tx_audio_lock` / `_cc_registers` /
    `_set_tx_freq` / `inject_audio_tx` / `inject_tx_iq` raw
    reach-ins ``Radio`` / ``audio_sink`` / ``ptt`` perform are
    delegated verbatim).  Constructed with the exact same
    signature as ``HL2Stream``."""

    def __init__(self, *args, **kwargs) -> None:
        # object.__setattr__ to bypass our forwarding __setattr__
        # (and because the contained stream does not exist yet).
        object.__setattr__(self, "_w1_stream",
                            HL2Stream(*args, **kwargs))
        # W1.1 rx_iq routing state (inert until start()).
        object.__setattr__(self, "_w1_rx1_cb", None)
        object.__setattr__(self, "_w1_rx2_cb", None)
        object.__setattr__(self, "_w1_rx_iq", None)
        object.__setattr__(self, "_w1_rx_drain_thread", None)
        object.__setattr__(self, "_w1_stop", False)
        object.__setattr__(self, "_w1_rx_lost_logged", False)
        # W1.2 tele routing state (inert until start()).
        object.__setattr__(self, "_w1_mic_cb", None)
        object.__setattr__(self, "_w1_tele", None)
        object.__setattr__(self, "_w1_tele_drain_thread", None)
        object.__setattr__(self, "_w1_tele_lost_logged", False)
        object.__setattr__(self, "_w1_tele_stop", False)
        # W1.3 cc_cmd routing state (inert until start()).
        object.__setattr__(self, "_w1_cc", None)
        object.__setattr__(self, "_w1_cc_real_regs", None)
        object.__setattr__(self, "_w1_cc_real_cycle", None)
        object.__setattr__(self, "_w1_cc_drain_thread", None)
        object.__setattr__(self, "_w1_cc_stop", False)
        object.__setattr__(self, "_w1_cc_lost_logged", False)
        object.__setattr__(self, "_w1_cc_gen", 1)

    # ── total delegation ─────────────────────────────────────
    def __getattr__(self, name: str):
        # __getattr__ is only called when normal lookup misses;
        # `_w1_stream` lives in __dict__ so this never recurses.
        # A missing attr on the real stream raises AttributeError
        # naturally, so hasattr()/getattr(default) keep working.
        return getattr(self._w1_stream, name)

    def __setattr__(self, name: str, value) -> None:
        if name in _PROXY_OWN:
            object.__setattr__(self, name, value)
        else:
            # e.g. `proxy.inject_audio_tx = True`,
            # `proxy.inject_tx_iq = True` must hit the real stream.
            setattr(self._w1_stream, name, value)

    def __delattr__(self, name: str) -> None:
        if name in _PROXY_OWN:
            object.__delattr__(self, name)
        else:
            delattr(self._w1_stream, name)

    # ── W1.1: rx_iq read-back routing ────────────────────────
    # The inbound EP6 RX path (radio→host read-back) is decoupled
    # from the operator's RX1/RX2 consumer callbacks via a W0
    # drop-oldest ring.  Producer = the contained stream's
    # rx-loop thread (it calls the proxy's producer shims, which
    # it received as the RX_AUDIO_CH0/CH2 consumers).  Consumer =
    # the proxy's OWN drain thread, which invokes the operator's
    # real callbacks.  EP2 EGRESS / WIRE CADENCE / MOX / §15.25 /
    # §15.21 are UNTOUCHED — rx_iq is inbound read-back only; this
    # only changes which thread the RX callbacks run on + adds a
    # W0-proven drop-oldest cushion that can never back-pressure
    # the rx-loop.  Per the locked v3 design + v3-4 (no D3 on the
    # rx path) + fix #5 (per-ring threading.Lock).

    def start(self, on_samples=None, on_rx2_samples=None,
              dispatch_state_provider=None, **kw):
        # W1.3: install the cc_cmd guard interpose BEFORE
        # contained.start() spins the EP2 writer (so the writer
        # reads through the guard from its first tick).  Independent
        # of the rx-cb path.
        self._w1_start_cc_routing()
        if on_samples is None and on_rx2_samples is None:
            # No rx cbs — cc routing still active; rx straight-delegate.
            return self._w1_stream.start(
                on_samples=on_samples, on_rx2_samples=on_rx2_samples,
                dispatch_state_provider=dispatch_state_provider, **kw)
        shim0, shim2 = self._w1_start_rx_routing(on_samples,
                                                 on_rx2_samples)
        return self._w1_stream.start(
            on_samples=shim0, on_rx2_samples=shim2,
            dispatch_state_provider=dispatch_state_provider, **kw)

    def _w1_start_rx_routing(self, on_samples, on_rx2_samples):
        """Stand up the rx_iq ring + drain thread; return the
        producer shims to register on the contained stream as the
        RX_AUDIO_CH0 / CH2 consumers.  Separated from ``start`` so
        it is unit-testable without binding a socket."""
        self._w1_rx1_cb = on_samples
        self._w1_rx2_cb = on_rx2_samples
        self._w1_stop = False
        self._w1_rx_lost_logged = False
        self._w1_rx_iq = Ring.create(_RX_SLOT, _RX_SLOTS,
                                     drop_oldest=True,
                                     lock=threading.Lock())
        t = threading.Thread(target=self._w1_rx_iq_drain_loop,
                              name="lyra-w1-rxiq-drain", daemon=False)
        self._w1_rx_drain_thread = t
        t.start()
        shim0 = self._w1_rx1_producer if on_samples is not None else None
        shim2 = self._w1_rx2_producer if on_rx2_samples is not None else None
        return shim0, shim2

    # producer shims — called on the contained stream's rx-loop
    # thread with (samples: complex64 ndarray, stats: FrameStats)
    def _w1_rx1_producer(self, samples, _stats):
        self._w1_rx_put(_RX_ROUTE_CH0, samples)

    def _w1_rx2_producer(self, samples, _stats):
        self._w1_rx_put(_RX_ROUTE_CH2, samples)

    def _w1_rx_put(self, route, samples):
        try:
            arr = np.ascontiguousarray(samples, dtype=np.complex64)
            payload = _RXHDR.pack(route, arr.size) + arr.tobytes()
            ring = self._w1_rx_iq
            if ring is None:
                return
            if len(payload) > ring.payload_capacity:
                # real per-datagram batches are tens of samples;
                # never expected. Drop, never block the rx-loop.
                self._w1_log_rx_once(
                    f"rx_iq payload {len(payload)} > cap; dropped")
                return
            # drop_oldest: put never blocks/fails on a full ring.
            # bounded acquire so a wedged consumer can't stall the
            # rx-loop; RingPeerLost ⇒ drop (degraded RX, NEVER a
            # wire/TX-safety event — v3-4: no D3 on the rx path).
            ring.put(payload, type_id=route, timeout=0.5)
        except RingPeerLost:
            self._w1_log_rx_once(
                "rx_iq put: consumer wedged; sample dropped")
        except Exception as exc:               # never break rx-loop
            self._w1_log_rx_once(f"rx_iq put error: {exc!r}")

    def _w1_rx_iq_drain_loop(self):
        ring = self._w1_rx_iq
        while not self._w1_stop:
            try:
                rec = ring.get(timeout=0.1)
            except RingClosed:
                break
            except RingPeerLost:
                # W1 is SAME-PROCESS: nothing can actually die —
                # this is lock contention (a Qt/GIL stall holding
                # the lock).  v3-4: D3 is gated on liveness and the
                # rx_iq path is NEVER a TX-safety event — log-once,
                # re-arm, continue.  force_release_all / FSM→RX is
                # NOT triggered here (no-worse-than-HEAD: a wedge
                # just delays RX, exactly as a GIL stall does today).
                self._w1_log_rx_once(
                    "rx_iq get: lock contention; re-armed")
                continue
            except Exception as exc:
                self._w1_log_rx_once(f"rx_iq get error: {exc!r}")
                continue
            if rec is None:
                continue                      # ring empty (RX idle)
            _seq, _gen, _tid, payload = rec
            try:
                r, n = _RXHDR.unpack_from(payload, 0)
                samples = np.frombuffer(
                    payload, dtype=np.complex64, count=n,
                    offset=_RXHDR.size).copy()
            except Exception as exc:
                self._w1_log_rx_once(f"rx_iq decode error: {exc!r}")
                continue
            cb = self._w1_rx1_cb if r == _RX_ROUTE_CH0 else self._w1_rx2_cb
            if cb is None:
                continue
            try:
                # stats unused by RX1/RX2 cbs (verified
                # _stream_cb/_stream_cb_rx2 take `_stats`); the full
                # FrameStats/ptt_in goes via the tele ring (W1.2).
                cb(samples, None)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "W1.1 rx consumer raised; other RX continues")

    def _w1_log_rx_once(self, msg: str) -> None:
        if not self._w1_rx_lost_logged:
            self._w1_rx_lost_logged = True
            import logging
            logging.getLogger(__name__).warning("[W1.1 rx_iq] %s", msg)

    # ── W1.2: tele (mic + ptt/dot/dash) read-back routing ────
    # The mic PUSH callback is decoupled from the operator's mic
    # consumer (`Radio._on_hl2_mic`) via a W0 drop-oldest ring,
    # exactly as W1.1 does for rx_iq.  Mic lifecycle is INDEPENDENT
    # of start()/stop() (driven by register_mic_consumer, like the
    # real HL2Stream API), with its OWN stop flag so clearing the
    # mic consumer never disturbs the rx_iq drain.  ptt_in/dot_in/
    # dash_in are snapshotted at PRODUCE time (rx-loop, coherent
    # with THIS datagram's mic) and shipped in the tele record;
    # the drain rebuilds a _TeleStats shim for the consumer.
    # EP2 EGRESS / WIRE / MOX / §15.25 / §15.21 UNTOUCHED.  v3-5:
    # the keyup MOX-off ACK is a RESERVED tele record type for
    # W1.4 (when tx_ring carries MOX) — W1.2 does NOT touch
    # ptt.py / the keyup path, so ptt.py:389-406 ordering+timing
    # is byte-identical by construction (nothing modified there).

    def register_mic_consumer(self, callback):
        if callback is None:
            self._w1_mic_cb = None
            # Stop the producer at the source FIRST (≤1 in-flight
            # datagram may still hit the shim — its put is bounded
            # + try/excepted, never breaks the rx-loop), then tear
            # down the tele drain + ring.
            try:
                self._w1_stream.register_mic_consumer(None)
            finally:
                self._w1_teardown_tele()
            return
        self._w1_mic_cb = callback
        self._w1_ensure_tele()
        # The contained stream calls the proxy's shim on its
        # rx-loop thread; the proxy's drain thread invokes the
        # operator's real callback.
        self._w1_stream.register_mic_consumer(self._w1_mic_producer)

    def _w1_ensure_tele(self) -> None:
        if self._w1_tele is not None:
            return                            # idempotent
        self._w1_tele_stop = False
        self._w1_tele_lost_logged = False
        self._w1_tele = Ring.create(_TELE_SLOT, _TELE_SLOTS,
                                    drop_oldest=True,
                                    lock=threading.Lock())
        t = threading.Thread(target=self._w1_tele_drain_loop,
                             name="lyra-w1-tele-drain", daemon=False)
        self._w1_tele_drain_thread = t
        t.start()

    def _w1_teardown_tele(self) -> None:
        self._w1_tele_stop = True
        t = self._w1_tele_drain_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)              # bounded (0.1 s get tick)
        self._w1_tele_drain_thread = None
        ring = self._w1_tele
        self._w1_tele = None
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass

    def _w1_mic_producer(self, mic_int16, stats):
        # rx-loop thread.  If tele isn't up (registered before
        # start / already torn down) fall back to the real cb
        # inline = exact HEAD behaviour (no rx-loop exists
        # pre-start anyway; purely defensive).
        ring = self._w1_tele
        if ring is None:
            cb = self._w1_mic_cb
            if cb is not None:
                try:
                    cb(mic_int16, stats)
                except Exception:
                    pass
            return
        try:
            arr = np.ascontiguousarray(mic_int16, dtype=np.int16)
            hdr = _TELEHDR.pack(
                arr.size,
                1 if getattr(stats, "ptt_in", False) else 0,
                1 if getattr(stats, "dot_in", False) else 0,
                1 if getattr(stats, "dash_in", False) else 0)
            payload = hdr + arr.tobytes()
            if len(payload) > ring.payload_capacity:
                self._w1_log_tele_once(
                    f"tele payload {len(payload)} > cap; dropped")
                return
            ring.put(payload, type_id=0, timeout=0.5)
        except RingPeerLost:
            self._w1_log_tele_once(
                "tele put: consumer wedged; mic datagram dropped")
        except Exception as exc:               # never break rx-loop
            self._w1_log_tele_once(f"tele put error: {exc!r}")

    def _w1_tele_drain_loop(self):
        ring = self._w1_tele
        while not self._w1_tele_stop:
            try:
                rec = ring.get(timeout=0.1)
            except RingClosed:
                break
            except RingPeerLost:
                # Same-process lock contention — log-once, re-arm,
                # NEVER D3/force_release_all (v3-4; tele/mic is not
                # a TX-safety event; no-worse-than-HEAD).
                self._w1_log_tele_once(
                    "tele get: lock contention; re-armed")
                continue
            except Exception as exc:
                self._w1_log_tele_once(f"tele get error: {exc!r}")
                continue
            if rec is None:
                continue                      # idle — normal
            _seq, _gen, _tid, payload = rec
            try:
                n, ptt, dot, dash = _TELEHDR.unpack_from(payload, 0)
                mic = np.frombuffer(payload, dtype=np.int16,
                                    count=n,
                                    offset=_TELEHDR.size).copy()
            except Exception as exc:
                self._w1_log_tele_once(f"tele decode error: {exc!r}")
                continue
            cb = self._w1_mic_cb
            if cb is None:
                continue
            try:
                # The mic consumer reads stats.ptt_in (gated by the
                # opt-in _hw_ptt_input_enabled); the per-datagram
                # snapshot preserves edge coherence.  Edge-detect is
                # LEVEL-driven so FIFO order (no reorder/dup — W0
                # guaranteed) keeps transitions identical to HEAD.
                cb(mic, _TeleStats(bool(ptt), bool(dot), bool(dash)))
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "W1.2 mic consumer raised; tele drain continues")

    def _w1_log_tele_once(self, msg: str) -> None:
        if not self._w1_tele_lost_logged:
            self._w1_tele_lost_logged = True
            import logging
            logging.getLogger(__name__).warning("[W1.2 tele] %s", msg)

    # ── W1.3: cc_cmd C&C-register read-back/control routing ──
    # In-process rehearsal of the W2 cc_cmd boundary at the
    # stream-INTERNAL `_cc_registers`/`_cc_lock` mutation
    # chokepoint (fix#1/RA-1 — NOT public method names).  Guard
    # objects replace the contained stream's `_cc_registers`/
    # `_cc_cycle`; EXCLUDED safety/MOX/§15.25 c0 are synchronous
    # pass-through (byte-identical HEAD), ROUTED idempotent c0 go
    # through the cc_cmd ring + the proxy cc-drain (atomic
    # value+slot apply under the contained `_cc_lock`).  No
    # stream.py modification (the W1.x revertable property).

    def _w1_start_cc_routing(self) -> None:
        if self._w1_cc is not None:
            return                            # idempotent
        s = self._w1_stream
        # Snapshot the contained stream's __init__-seeded
        # registers/cycle (0x00 general-settings, 0x2e TX-latency,
        # …) into the REAL backing so they don't vanish (G1/seed
        # preserve), then swap in the guards BEFORE contained
        # .start() spins the EP2 writer.
        self._w1_cc_real_regs = dict(s._cc_registers)
        self._w1_cc_real_cycle = list(s._cc_cycle)
        self._w1_cc_stop = False
        self._w1_cc_lost_logged = False
        ring = Ring.create(_CC_SLOT, _CC_SLOTS, drop_oldest=False,
                           lock=threading.Lock())
        ring.set_generation(self._w1_cc_gen)
        self._w1_cc = ring
        s._cc_registers = _GuardCcRegisters(
            self._w1_cc_real_regs, ring, self._w1_cc_route)
        s._cc_cycle = _GuardCcCycle(self._w1_cc_real_cycle)
        t = threading.Thread(target=self._w1_cc_drain_loop,
                             name="lyra-w1-cc-drain", daemon=False)
        self._w1_cc_drain_thread = t
        t.start()

    def _w1_cc_route(self, c0, value) -> None:
        # Called from the guard `__setitem__` for a ROUTED c0,
        # WHILE the writer holds the contained `_cc_lock`.  Only
        # enqueues (the cc_cmd ring has its OWN lock — NEVER takes
        # `_cc_lock`; the drain takes `_cc_lock` later, after W0
        # Ring.get has released the ring-lock in its finally:
        # lock-order acyclic, no AB/BA, no re-entrancy).
        ring = self._w1_cc
        if ring is None:
            return
        try:
            c1, c2, c3, c4 = value
            rec = _CCREC.pack(c0 & 0xFF, c1 & 0xFF, c2 & 0xFF,
                              c3 & 0xFF, c4 & 0xFF)
            if not ring.put(rec, type_id=c0 & 0xFF, timeout=0.5):
                # NON-drop ring full — unreachable at the
                # operator-event cc rate; idempotent so a later
                # write of the same c0 re-sends.  Log-once.
                self._w1_log_cc_once(
                    f"cc_cmd ring full; c0=0x{c0:02X} write dropped")
        except RingPeerLost:
            self._w1_log_cc_once(
                f"cc_cmd put: drain wedged; c0=0x{c0:02X} dropped")
        except Exception as exc:               # never break a writer
            self._w1_log_cc_once(f"cc_cmd put error: {exc!r}")

    def _w1_cc_drain_loop(self):
        ring = self._w1_cc
        regs = self._w1_cc_real_regs
        cyc = self._w1_cc_real_cycle
        cc_lock = self._w1_stream._cc_lock
        gen = self._w1_cc_gen
        while not self._w1_cc_stop:
            try:
                rec = ring.get(timeout=0.1, expected_generation=gen)
            except RingClosed:
                break
            except RingPeerLost:
                # Same-process lock contention — log-once, re-arm.
                # cc_cmd is idempotent latest-value, NOT a
                # TX-safety edge (those are EXCLUDED/synchronous)
                # → NEVER D3/force_release_all (v3-4 class).
                self._w1_log_cc_once(
                    "cc_cmd get: lock contention; re-armed")
                continue
            except Exception as exc:
                self._w1_log_cc_once(f"cc_cmd get error: {exc!r}")
                continue
            if rec is None:
                continue                      # idle — normal
            _seq, _g, _tid, payload = rec
            try:
                c0, c1, c2, c3, c4 = _CCREC.unpack_from(payload, 0)
            except Exception as exc:
                self._w1_log_cc_once(f"cc_cmd decode error: {exc!r}")
                continue
            # W0 Ring.get released the ring-lock in its finally
            # BEFORE returning → taking `_cc_lock` here is NOT
            # nested in any ring-lock scope (acyclic).  Apply the
            # value-store AND the idempotent slot-append as ONE
            # atomic pair under the contained `_cc_lock` — exactly
            # mirroring HEAD's `with _cc_lock: regs[c0]=t;
            # _register_cc_slot(c0)` (v2-1; no EP2-reader KeyError).
            with cc_lock:
                regs[c0] = (c1, c2, c3, c4)
                if c0 not in cyc:
                    cyc.append(c0)

    def _w1_log_cc_once(self, msg: str) -> None:
        if not self._w1_cc_lost_logged:
            self._w1_cc_lost_logged = True
            import logging
            logging.getLogger(__name__).warning("[W1.3 cc_cmd] %s", msg)

    def _w1_teardown_cc(self) -> None:
        self._w1_cc_stop = True
        t = self._w1_cc_drain_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)              # bounded (0.1 s get tick)
        self._w1_cc_drain_thread = None
        ring = self._w1_cc
        self._w1_cc = None
        if ring is not None:
            try:
                ring.close()
            except Exception:
                pass

    def stop(self):
        # Signal the drain threads, tear down the contained stream
        # (its §15.21-ordered teardown joins the EP2 writer + the
        # rx-loop = the rx_iq/tele/cc producers), THEN join the
        # proxy drain threads bounded (cc-drain join is AFTER the
        # EP2-writer join — contained.stop() completes in the try
        # before this finally), THEN free the rings (no one touches
        # them after the join).
        self._w1_stop = True
        self._w1_cc_stop = True
        try:
            return self._w1_stream.stop()
        finally:
            t = self._w1_rx_drain_thread
            if t is not None and t.is_alive():
                t.join(timeout=2.0)           # bounded: 0.1 s get +
                # _w1_stop check ⇒ exits within ~one tick, no hang.
            self._w1_rx_drain_thread = None
            ring = self._w1_rx_iq
            self._w1_rx_iq = None
            if ring is not None:
                try:
                    ring.close()
                except Exception:
                    pass
            # cc-drain join AFTER the EP2-writer join (which
            # completed inside contained.stop() in the try) — v2-4
            # teardown order; bounded, then free the cc ring.
            self._w1_teardown_cc()
            # tele is normally torn down by register_mic_consumer
            # (None) BEFORE stop() (Radio teardown order) — but
            # tear it down here too, defensively + bounded.
            self._w1_teardown_tele()

    # ── explicit, non-forwarded helpers (W1.1+ use these) ────
    def unwrap(self) -> HL2Stream:
        """The contained real :class:`HL2Stream`.  W1.1+ stages
        and tests reach the concrete stream through this rather
        than relying on delegation."""
        return self._w1_stream

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<HL2StreamProxy W1.0 wrapping {self._w1_stream!r}>"
