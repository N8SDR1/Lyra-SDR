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
        if on_samples is None and on_rx2_samples is None:
            # Nothing to route — straight delegate (back-compat).
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

    def stop(self):
        # Signal the drain thread, tear down the contained stream
        # (its §15.21-ordered teardown joins the rx-loop = the
        # rx_iq/tele producer), THEN join the drain threads bounded,
        # THEN free the rings (no one touches them after the join).
        self._w1_stop = True
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
