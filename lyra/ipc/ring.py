"""Barrier-bearing single-producer / single-consumer shared-memory
ring (process-isolation stage W0).

Correctness model (red-team-converged v3, CLAUDE.md §15.26):

* **The lock IS the barrier.**  CPython gives no cross-process
  atomic or release/acquire fence, so indices are NOT published
  via bare shared-memory stores.  Every publish and every consume
  happens entirely under one ``multiprocessing.Lock`` (a kernel
  semaphore).  The producer's lock-release pairs with the
  consumer's next lock-acquire to establish happens-before for the
  whole slot (header + payload).  SPSC + uncontended ⇒ a sub-µs
  kernel round-trip per record, NOT a stall.  We deliberately do
  the *entire* put/get under the lock (payload memcpy included):
  it removes the full-ring producer/consumer slot-overlap race by
  construction and the cost was budgeted in the v3 design.

* **Bounded waits only (A1).**  ``multiprocessing.Lock`` is an
  ownerless semaphore: if the peer process dies while holding it
  the lock is never released and a bounded ``acquire(timeout=…)``
  returns False forever-after.  That timeout is the authoritative
  *peer-death* signal → :class:`RingPeerLost`.  Callers MUST pass
  a finite ``timeout`` on the side the architecture marks bounded
  (every MAIN-side acquire).  No spin-retry on RingPeerLost.

* **Generation tagging (D5).**  Every record carries a uint64
  generation.  ``get(expected_generation=…)`` discards records
  whose generation does not match (post-STOP in-flight data is
  dropped, not applied).  ``set_generation`` bumps it.

* **Producer-side drop-oldest** (the rx_iq overrun policy): on a
  full ring the producer drops the oldest record; it never blocks
  and never back-pressures.

Nothing here is wired into the radio/wire path — W0 is standalone,
in-process-testable, fully revertable infrastructure.
"""
from __future__ import annotations

import struct
from multiprocessing import shared_memory
from multiprocessing.synchronize import Lock as LockT

# ── Control region (fixed 64 bytes, little-endian) ───────────────
#   magic | version | slot_size | n_slots | head | tail | gen | closed
_CTRL_FMT = "<4sIQQQQQB"
_CTRL_SIZE = 64
_MAGIC = b"LYR0"
_VERSION = 1

# ── Per-slot header (24 bytes): seq | gen | length | type_id ─────
_SLOT_HDR_FMT = "<QQII"
_SLOT_HDR_SIZE = 24


class RingPeerLost(Exception):
    """A bounded acquire timed out — the peer process is presumed
    dead (it died holding the lock, or is wedged).  The caller must
    route this to the architecture's peer-death path (force FSM→RX,
    alarm, kill+respawn).  Do NOT spin-retry."""


class RingClosed(Exception):
    """The ring is empty AND the producer set the clean-close flag.
    Distinct from :class:`RingPeerLost` — this is an orderly
    shutdown, not a crash."""


class Ring:
    """SPSC shared-memory ring.  Use :meth:`create` (owner) then
    pass ``ring.shm_name`` and ``ring.lock`` to the peer process,
    which calls :meth:`attach`."""

    # ---- construction -------------------------------------------
    def __init__(self, shm, lock: LockT, slot_size: int,
                 n_slots: int, drop_oldest: bool, owner: bool):
        self._shm = shm
        self._lock = lock
        self._slot_size = slot_size
        self._n_slots = n_slots
        self._drop_oldest = drop_oldest
        self._owner = owner
        self._payload_cap = slot_size - _SLOT_HDR_SIZE
        if self._payload_cap <= 0:
            raise ValueError("slot_size must exceed slot header (24B)")

    @classmethod
    def create(cls, slot_size: int, n_slots: int, *,
               drop_oldest: bool = False,
               lock: LockT | None = None) -> "Ring":
        if slot_size < _SLOT_HDR_SIZE + 1 or n_slots < 1:
            raise ValueError("slot_size/n_slots too small")
        if lock is None:
            import multiprocessing as mp
            lock = mp.Lock()
        total = _CTRL_SIZE + slot_size * n_slots
        shm = shared_memory.SharedMemory(create=True, size=total)
        struct.pack_into(_CTRL_FMT, shm.buf, 0,
                         _MAGIC, _VERSION, slot_size, n_slots,
                         0, 0, 0, 0)
        return cls(shm, lock, slot_size, n_slots, drop_oldest, True)

    @classmethod
    def attach(cls, shm_name: str, lock: LockT, *,
               drop_oldest: bool = False) -> "Ring":
        shm = shared_memory.SharedMemory(name=shm_name)
        magic, ver, slot_size, n_slots, _h, _t, _g, _c = \
            struct.unpack_from(_CTRL_FMT, shm.buf, 0)
        if magic != _MAGIC or ver != _VERSION:
            shm.close()
            raise ValueError("shared-memory ring magic/version mismatch")
        return cls(shm, lock, slot_size, n_slots, drop_oldest, False)

    # ---- control-region accessors (call only while holding lock) -
    def _ctrl(self):
        return struct.unpack_from(_CTRL_FMT, self._shm.buf, 0)

    def _set_head_tail_gen_closed(self, head, tail, gen, closed):
        struct.pack_into(_CTRL_FMT, self._shm.buf, 0, _MAGIC, _VERSION,
                         self._slot_size, self._n_slots,
                         head, tail, gen, closed)

    def _slot_off(self, idx_free_running: int) -> int:
        return _CTRL_SIZE + (idx_free_running % self._n_slots) * self._slot_size

    # ---- API ----------------------------------------------------
    def put(self, data: bytes, type_id: int = 0,
            timeout: float | None = None) -> bool:
        """Publish one record.  Returns True on success, False only
        when the ring is full and ``drop_oldest`` is off.  Raises
        :class:`RingPeerLost` if a bounded acquire times out."""
        if len(data) > self._payload_cap:
            raise ValueError(
                f"payload {len(data)} > slot capacity {self._payload_cap}")
        if not self._acquire(timeout):
            raise RingPeerLost("ring.put: peer holds the lock (presumed dead)")
        try:
            _m, _v, _ss, _ns, head, tail, gen, closed = self._ctrl()
            if head - tail >= self._n_slots:           # full
                if not self._drop_oldest:
                    return False
                tail += 1                              # drop oldest
            off = self._slot_off(head)
            struct.pack_into(_SLOT_HDR_FMT, self._shm.buf, off,
                             head, gen, len(data), type_id & 0xFFFFFFFF)
            self._shm.buf[off + _SLOT_HDR_SIZE:
                          off + _SLOT_HDR_SIZE + len(data)] = data
            self._set_head_tail_gen_closed(head + 1, tail, gen, closed)
            return True
        finally:
            self._lock.release()

    def get(self, timeout: float, expected_generation: int | None = None):
        """Pop the oldest record.  Returns ``(seq, gen, type_id,
        bytes)`` or ``None`` if the ring is empty.  Records whose
        generation != ``expected_generation`` (when given) are
        discarded (D5).  Raises :class:`RingPeerLost` on a bounded
        acquire timeout, :class:`RingClosed` if empty and the
        producer set the clean-close flag.

        ``timeout`` is REQUIRED and finite — every consumer wait in
        this architecture is bounded (A1)."""
        if timeout is None or timeout <= 0:
            raise ValueError("get() timeout must be finite and > 0 (A1)")
        if not self._acquire(timeout):
            raise RingPeerLost("ring.get: peer holds the lock (presumed dead)")
        try:
            while True:
                _m, _v, _ss, _ns, head, tail, gen, closed = self._ctrl()
                if head == tail:
                    if closed:
                        raise RingClosed("ring closed by producer, drained")
                    return None
                off = self._slot_off(tail)
                seq, sgen, length, type_id = struct.unpack_from(
                    _SLOT_HDR_FMT, self._shm.buf, off)
                payload = bytes(
                    self._shm.buf[off + _SLOT_HDR_SIZE:
                                  off + _SLOT_HDR_SIZE + length])
                self._set_head_tail_gen_closed(head, tail + 1, gen, closed)
                if (expected_generation is not None
                        and sgen != expected_generation):
                    continue                           # discard stale-gen
                return (seq, sgen, type_id, payload)
        finally:
            self._lock.release()

    def set_generation(self, generation: int,
                        timeout: float | None = None) -> None:
        """Bump the ring generation (W2: STOP/START).  Subsequent
        ``put`` records carry it; consumers filtering on the old
        generation drop any in-flight stragglers."""
        if not self._acquire(timeout):
            raise RingPeerLost("ring.set_generation: peer holds the lock")
        try:
            _m, _v, _ss, _ns, head, tail, _g, closed = self._ctrl()
            self._set_head_tail_gen_closed(head, tail,
                                           generation & 0xFFFFFFFFFFFFFFFF,
                                           closed)
        finally:
            self._lock.release()

    def mark_closed(self, timeout: float | None = None) -> None:
        """Producer: signal an orderly shutdown.  A consumer that
        drains the ring then sees this raises :class:`RingClosed`
        (not RingPeerLost)."""
        if not self._acquire(timeout):
            raise RingPeerLost("ring.mark_closed: peer holds the lock")
        try:
            _m, _v, _ss, _ns, head, tail, gen, _c = self._ctrl()
            self._set_head_tail_gen_closed(head, tail, gen, 1)
        finally:
            self._lock.release()

    def _acquire(self, timeout: float | None) -> bool:
        if timeout is None:
            self._lock.acquire()
            return True
        return bool(self._lock.acquire(timeout=timeout))

    # ---- introspection / lifecycle ------------------------------
    @property
    def shm_name(self) -> str:
        return self._shm.name

    @property
    def lock(self) -> LockT:
        return self._lock

    @property
    def slot_size(self) -> int:
        return self._slot_size

    @property
    def n_slots(self) -> int:
        return self._n_slots

    @property
    def payload_capacity(self) -> int:
        return self._payload_cap

    def depth(self, timeout: float | None = 1.0) -> int:
        """Records currently queued (diagnostic / W1 A-B gate)."""
        if not self._acquire(timeout):
            raise RingPeerLost("ring.depth: peer holds the lock")
        try:
            _m, _v, _ss, _ns, head, tail, _g, _c = self._ctrl()
            return head - tail
        finally:
            self._lock.release()

    def close(self) -> None:
        try:
            self._shm.close()
        finally:
            if self._owner:
                try:
                    self._shm.unlink()
                except FileNotFoundError:
                    pass
