"""Lyra IPC primitives — process-isolation foundation (stage W0).

This package exists for the operator-locked process-isolated wire/
radio architecture (CLAUDE.md §15.26 ARCHITECTURE CHARTER + the
converged design v3).  W0 is the smallest safe stage: a single
barrier-bearing single-producer / single-consumer shared-memory
ring + its two-process correctness gates.  It is IN-PROCESS-only
infrastructure — nothing here is wired into the radio/wire path
yet (that is W1/W2).  Fully revertable, zero wire risk.

Design pins (v3, red-team-converged):
* CPython exposes no cross-process atomic / release-acquire
  barrier (the D1 finding that killed the original "lock-free
  ring" proof).  The ring publishes its indices under a real
  kernel primitive (``multiprocessing.Lock``) whose acquire /
  release IS the cross-process barrier — SPSC so uncontended.
* Every consumer wait is BOUNDED (A1).  ``multiprocessing.Lock``
  is a semaphore with no owner: if the producer process dies
  while holding it, it is NEVER released, so a bounded
  ``acquire(timeout=…)`` returning False is the authoritative
  child-death signal — surfaced as :class:`RingPeerLost`.  No
  spin-retry.
* Records carry a uint64 generation (D5).  ``get`` can discard
  mismatched-generation records so post-STOP in-flight data is
  dropped, not applied.
* Producer-side drop-oldest (the rx_iq overrun policy) — never
  blocks, never back-pressures the producer.
"""

from .ring import Ring, RingPeerLost, RingClosed

__all__ = ["Ring", "RingPeerLost", "RingClosed"]
