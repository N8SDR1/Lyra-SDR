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

Note: W1.3 swapped `_cc_registers`/`_cc_cycle` for guard objects
(the cc_cmd boundary).  **W1.4 is CONTROL-ONLY** (the converged
v3 + P1/P2 — §15.26): the tx_ring carries ONLY rehearsal
ordering tokens (MOX_ON / MOX_OFF / INJECT_IQ_ON / INJECT_IQ_OFF)
that prove the W2 ordered-seam FIFO + exercise the D5 stale-gen
discard + are the W2 fallback seam.  `_tx_audio`/`_tx_iq` STAY
live-delegated on the S2 deque path UNCHANGED — v3-3 explicitly
deleted all TXAUDIO/TXIQ-on-ring (the 2026-05-18 S4a revert
empirically proved in-process touching the TX-audio deque is
dangerous; the hard cross-process TX-audio transport is scoped
to W2 with its own bench gate, NOT skimped).  STEPATT (0x14/
0x1C) + TX-NCO (0x02/0x08/0x0a) stay W1.3 `_CC_EXCLUDED`
synchronous byte-identical-HEAD.  **P1: the wire MOX bit STAYS
100% on `_dispatch_state.mox` MAIN-direct in W1** — the tokens
are pure rehearsal; nothing in W1.4 reads `_w1_wire_mox` into
`_snapshot_mox_bit` (we make NO stream.py change, so this holds
by construction).
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

# ── W1.4 tx_ring framing (CONTROL-ONLY) ──────────────────────────
# In-process rehearsal of the W2 ordered wire-egress seam.  The
# tx_ring carries ONLY control ordering tokens — NEVER TX audio/IQ
# (v3-3: TXAUDIO/TXIQ stay on the S2 `_tx_audio` deque, untouched;
# the S4a revert proved touching it in-process is dangerous) and
# NEVER the MOX-correlated/§15.25/TX-safety registers (v3-2:
# 0x14/0x1C/0x02/0x08/0x0a stay W1.3 `_CC_EXCLUDED` synchronous
# byte-identical-HEAD).  Tokens are generation-tagged FIFO records
# enqueued at the proxy `__setattr__` `inject_tx_iq` edge — the
# ONLY non-invasive seam (ptt.py writes `stream.inject_tx_iq` at
# exactly two sites: True in `_open_tx_iq` = the DEFERRED keydown
# step AFTER `_on_tx_state_changed(True)`→synchronous
# `_apply_att_on_tx`→STEPATT and AFTER `set_mox(True)`→synchronous
# `_set_tx_freq`→TXNCO, so the rising-edge token is correctly
# ordered TXNCO(sync)→STEPATT(sync)→MOX_ON→INJECT_ON = v3-1; False
# in `_finalize_keyup` = AFTER the MoxEdgeFade fade-poll gate so
# the faded tail is already on the S2 deque ahead = v3-4(b)
# GRACEFUL; re-key-collapse never flips it ⇒ no edge ⇒ no token =
# v3-4(c) NEITHER — all BY CONSTRUCTION, no stream.py/ptt.py
# change).  drop_oldest=False (a control token must NEVER be
# silently lost — a full ring is a TX-safety event; unreachable
# at the operator-keying cadence, the shipped W1.3 cc_cmd
# argument).  P1: the drain applies tokens ONLY to the
# rehearsal-only `_w1_wire_mox` (proves the W2 seam + the FIFO
# seq monotonic); the REAL wire MOX bit stays 100% on
# `_dispatch_state.mox` MAIN-direct (`_snapshot_mox_bit`,
# stream.py — UNCHANGED; we make no stream.py edit so this holds
# by construction).  Wiring the ring into `_snapshot_mox_bit`
# would reintroduce an unordered wire-MOX path = BLOCKS-SHIP.
# Slot must exceed the W0 24-byte slot header; the record itself
# is only 5 B (kind+seq) — 64 B is plenty and matches cc_cmd.
_TX_SLOT = 64
_TX_SLOTS = 256
_TXREC = struct.Struct("<BI")          # kind, producer_seq (5 B)
_TX_MOX_ON = 1
_TX_MOX_OFF = 2
_TX_INJECT_ON = 3
_TX_INJECT_OFF = 4

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
    # W1.4 tx_ring (control-only) routing state:
    "_w1_tx", "_w1_tx_drain_thread", "_w1_tx_stop",
    "_w1_tx_lost_logged", "_w1_tx_gen", "_w1_tx_seq",
    "_w1_wire_mox", "_w1_tx_last_seq",
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
        # W1.4 tx_ring control-only routing state (inert until start()).
        object.__setattr__(self, "_w1_tx", None)
        object.__setattr__(self, "_w1_tx_drain_thread", None)
        object.__setattr__(self, "_w1_tx_stop", False)
        object.__setattr__(self, "_w1_tx_lost_logged", False)
        object.__setattr__(self, "_w1_tx_gen", 1)
        object.__setattr__(self, "_w1_tx_seq", 0)
        object.__setattr__(self, "_w1_wire_mox", False)
        object.__setattr__(self, "_w1_tx_last_seq", 0)

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
            return
        if name == "inject_tx_iq":
            # ── W1.4 control-only seam ──────────────────────────
            # ptt.py writes `stream.inject_tx_iq` at exactly two
            # sites (verified): True in `_open_tx_iq` (the deferred
            # keydown step), False in `_finalize_keyup` (after the
            # fade-poll gate).  Edge-detect vs the REAL stream's
            # current value, delegate the real write FIRST (P1: the
            # actual flag drives the unchanged HEAD wire path —
            # `_snapshot_mox_bit`/EP2 packer read the real
            # `inject_tx_iq` + the real `_dispatch_state.mox`,
            # never `_w1_wire_mox`), THEN enqueue the rehearsal
            # ordering token on a true edge only (idempotent
            # re-set ⇒ no edge ⇒ no churn; re-key-collapse never
            # flips it ⇒ no token = v3-4(c) NEITHER).
            try:
                cur = bool(getattr(self._w1_stream,
                                   "inject_tx_iq", False))
            except Exception:
                cur = False
            nv = bool(value)
            setattr(self._w1_stream, name, value)   # P1: real path
            if nv and not cur:
                # keydown _open_tx_iq — deferred, AFTER
                # _on_tx_state_changed(True)→sync STEPATT and
                # set_mox(True)→sync TXNCO.  v3-1 FIFO order.
                self._w1_tx_enqueue(_TX_MOX_ON)
                self._w1_tx_enqueue(_TX_INJECT_ON)
            elif cur and not nv:
                # keyup _finalize_keyup — AFTER the MoxEdgeFade
                # fade-poll gate (faded tail already on the S2
                # deque ahead).  v3-4(b) GRACEFUL: ordered
                # MOX_OFF/INJECT_OFF, NO ring discard.
                self._w1_tx_enqueue(_TX_MOX_OFF)
                self._w1_tx_enqueue(_TX_INJECT_OFF)
            return
        # e.g. `proxy.inject_audio_tx = True` must hit the real
        # stream (delegated verbatim — wire path unchanged).
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
        # W1.4: stand up the control-only tx_ring + drain.
        # Unconditional (like cc) — rehearses the W2 ordered-seam +
        # is the W2 fallback regardless of rx cbs.  Inert on the
        # wire until the FSM flips inject_tx_iq (P1: tokens are
        # rehearsal-only; the real wire path is unchanged HEAD).
        self._w1_start_tx_routing()
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

    # ── W1.4: tx_ring CONTROL-ONLY ordered-seam rehearsal ────
    # The converged v3 + the P1/P2 implementation contract
    # (§15.26).  tx_ring carries ONLY MOX_ON/MOX_OFF/INJECT_ON/
    # INJECT_OFF ordering tokens enqueued at the proxy
    # `inject_tx_iq` edge; the drain applies them to the
    # rehearsal-only `_w1_wire_mox` + tracks FIFO seq monotonicity
    # (proves the W2 ordered-seam + the D5 stale-gen discard +
    # is the W2 fallback seam).  NO stream.py/ptt.py/radio.py
    # change ⇒ independently revertible; the real wire path is
    # byte-identical HEAD (P1).

    def _w1_start_tx_routing(self) -> None:
        if self._w1_tx is not None:
            return                            # idempotent
        self._w1_tx_stop = False
        self._w1_tx_lost_logged = False
        ring = Ring.create(_TX_SLOT, _TX_SLOTS, drop_oldest=False,
                           lock=threading.Lock())
        ring.set_generation(self._w1_tx_gen)
        self._w1_tx = ring
        t = threading.Thread(target=self._w1_tx_drain_loop,
                             name="lyra-w1-tx-drain", daemon=False)
        self._w1_tx_drain_thread = t
        t.start()

    def _w1_tx_enqueue(self, kind: int) -> None:
        # Called from the proxy `__setattr__` inject_tx_iq edge
        # (Qt-main / FSM thread).  Only enqueues (the tx_ring has
        # its OWN lock; never takes `_cc_lock`; the drain takes
        # nothing wire-affecting — pure rehearsal).  Defensive: if
        # the ring isn't up (pre-start / torn down) just no-op —
        # exact HEAD (the real flag was already delegated).
        ring = self._w1_tx
        if ring is None:
            return
        try:
            self._w1_tx_seq = (self._w1_tx_seq + 1) & 0xFFFFFFFF
            rec = _TXREC.pack(kind & 0xFF, self._w1_tx_seq)
            if not ring.put(rec, type_id=kind & 0xFF, timeout=0.5):
                # NON-drop control ring full — a control token must
                # NEVER be silently lost (TX-safety event).
                # Unreachable at the operator-keying cadence (the
                # shipped W1.3 cc_cmd argument); log-once.
                self._w1_log_tx_once(
                    f"tx_ring full; control token {kind} dropped")
        except RingPeerLost:
            self._w1_log_tx_once(
                f"tx_ring put: drain wedged; token {kind} dropped")
        except Exception as exc:               # never break the FSM
            self._w1_log_tx_once(f"tx_ring put error: {exc!r}")

    def _w1_tx_drain_loop(self):
        ring = self._w1_tx
        while not self._w1_tx_stop:
            try:
                # Read self._w1_tx_gen fresh each iter so a HARD
                # pre-disconnect gen-bump (D5) takes effect — a
                # stale in-flight token is discarded, not applied.
                rec = ring.get(timeout=0.1,
                               expected_generation=self._w1_tx_gen)
            except RingClosed:
                break
            except RingPeerLost:
                # W1 is SAME-PROCESS: nothing can die — this is
                # lock contention (a Qt/GIL stall holding the
                # lock).  v3-4: in-process the safety mechanism is
                # the HARD teardown via stop() (D3 is a W2
                # cross-process concern); the REAL wire MOX bit is
                # on `_dispatch_state` (P1), unaffected by a
                # rehearsal-drain stall — NEVER D3/force_release_all
                # here (no-worse-than-HEAD: HEAD has no rehearsal
                # layer at all).  Log-once, re-arm, continue.
                self._w1_log_tx_once(
                    "tx_ring get: lock contention; re-armed")
                continue
            except Exception as exc:
                self._w1_log_tx_once(f"tx_ring get error: {exc!r}")
                continue
            if rec is None:
                continue                      # idle — normal
            _seq, _g, _tid, payload = rec
            try:
                kind, pseq = _TXREC.unpack_from(payload, 0)
            except Exception as exc:
                self._w1_log_tx_once(f"tx_ring decode error: {exc!r}")
                continue
            # Rehearsal-only application (proves the W2 ordered
            # seam).  NOT read by `_snapshot_mox_bit` (P1: the real
            # wire MOX bit stays 100% on `_dispatch_state.mox`
            # MAIN-direct — we make NO stream.py change so this
            # holds by construction).  The seq monotonic check is
            # the A/B-gate's FIFO proof.
            if kind == _TX_MOX_ON:
                self._w1_wire_mox = True
            elif kind == _TX_MOX_OFF:
                self._w1_wire_mox = False
            # INJECT_ON/OFF: ordering tokens — the real
            # inject_tx_iq flag was already delegated in
            # __setattr__; here they only advance the FIFO seq.
            self._w1_tx_last_seq = pseq

    def _w1_log_tx_once(self, msg: str) -> None:
        if not self._w1_tx_lost_logged:
            self._w1_tx_lost_logged = True
            import logging
            logging.getLogger(__name__).warning("[W1.4 tx_ring] %s", msg)

    def _w1_tx_hard_predisconnect(self) -> None:
        # P2 HARD teardown — invoked from stop() BEFORE
        # `self._w1_stream.stop()` (i.e. before the EP2-writer
        # join).  Force the rehearsal wire-mox to 0, bump the
        # generation (D5: stale-discard any in-flight token —
        # cb58bcb come-up-not-keyed itself rests on the MAIN-direct
        # `_dispatch_state.mox=False` path, UNCHANGED; this is the
        # W2-seam rehearsal of "no stale token survives a stop"),
        # and close the control ring so the drain's get raises
        # RingClosed and it cannot apply a stale token while the
        # EP2 writer drains its final frames.  Re-key-collapse and
        # normal/force_release_all/§15.20 keyups are GRACEFUL
        # (v3-4(b)/(c)) and do NOT call this — HARD is stop()/D3/
        # fault ONLY (a HARD hook on a graceful keyup would
        # reintroduce the D-W14f tail-chop).
        self._w1_tx_stop = True
        self._w1_wire_mox = False
        self._w1_tx_gen = (self._w1_tx_gen + 1) & 0xFFFFFFFF
        ring = self._w1_tx
        if ring is not None:
            try:
                ring.set_generation(self._w1_tx_gen)
            except Exception:
                pass
            try:
                ring.close()
            except Exception:
                pass

    def _w1_teardown_tx(self) -> None:
        self._w1_tx_stop = True
        t = self._w1_tx_drain_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)              # bounded (0.1 s get tick)
        self._w1_tx_drain_thread = None
        ring = self._w1_tx
        self._w1_tx = None
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
        self._w1_tx_stop = True
        # P2 HARD tx pre-disconnect BEFORE contained.stop() — force
        # the rehearsal wire-mox to 0, bump generation (D5
        # stale-discard), and close the control ring so no stale
        # token can be applied while the EP2 writer drains its
        # final frames (i.e. before the EP2-writer join inside
        # contained.stop()).  cb58bcb come-up-not-keyed stays on
        # the MAIN-direct `_dispatch_state.mox=False` path.
        self._w1_tx_hard_predisconnect()
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
            # tx-drain join AFTER the EP2-writer join too.  The
            # control ring was already closed in the P2 HARD
            # pre-disconnect (before contained.stop()), so the
            # drain's get already raised RingClosed and it exits
            # ~immediately; just join bounded + null the refs.
            self._w1_teardown_tx()
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
