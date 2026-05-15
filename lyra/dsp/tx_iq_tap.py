"""Sip1 TX I/Q tap (v0.2 Phase 2 commit 9, consensus plan §8.2).

Captures recent outgoing TX I/Q samples in a thread-safe ring
buffer so v0.3 PureSignal's calcc thread can align them against
the feedback I/Q delivered via DDC0+DDC1 (HL2 PS routing per
CLAUDE.md §3.8 corrected entry) and build the predistortion
model.

For v0.2 only the producer side is wired -- ``TxDspWorker`` writes
each processed block when ``HL2Stream.inject_tx_iq`` is True
(i.e., the block actually went on the air, not RX-time WDSP
warm-up).  No consumer exists yet; v0.3 PS calcc thread adds the
snapshot reader.

Why wire the tap as a v0.2 no-op (consensus §8.5 Round 5):
adding it now means v0.3 PureSignal work can focus purely on PS
algorithm + UI without re-validating every TX sub-mode (SSB / CW
/ AM / FM) for tap-point correctness.  The cost of writing
samples into a bounded deque that nobody reads is negligible
(microseconds per call); the cost of retrofitting the tap point
across four already-shipped TX sub-modes during v0.3 work would
be considerably larger.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Optional

import numpy as np


class Sip1Tap:
    """Bounded ring buffer of recent TX I/Q samples.

    Single-writer (``TxDspWorker`` thread) / single-reader (v0.3
    PS calcc thread, when it exists) pattern.  Lock-guarded for
    snapshot integrity -- a snapshot taken while a write is in
    progress sees either the pre-write or post-write state, never
    a partial write.

    Default capacity: 8192 complex64 samples ≈ 170 ms of TX I/Q.

    The 170 ms figure assumes the HL2 TX I/Q wire rate of 48 kHz
    (gateware-fixed by the AK4951 codec lock per CLAUDE.md §3.5;
    NOT the operator-selectable RX IQ rate of 96k/192k/384k --
    those are two independent rates).  WDSP TXA emits at
    out_rate=48000 to match the wire, so what the tap captures is
    48 kHz regardless of what RX is doing.

    8192 samples / 48 kHz ≈ 170 ms.  Plenty of headroom for
    PureSignal's typical 50-100 ms PA round-trip + correlation
    window.

    Memory cost at default capacity:
        8192 samples × 8 bytes (complex64) ≈ 65 KB
    Negligible at HL2 operator scale; dwarfed by the WDSP
    fexchange0 working set anyway.
    """

    DEFAULT_CAPACITY = 8192  # complex64 samples

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = int(capacity)
        self._buf: "deque[complex]" = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        # Diagnostic counters.  Single-writer-per-counter pattern;
        # cumulative ints are GIL-atomic to read from any thread.
        self.samples_written: int = 0   # cumulative writes
        self.snapshots_taken: int = 0   # cumulative snapshot calls
        self.cleared: int = 0           # cumulative clear() calls

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def depth(self) -> int:
        """Current sample count in the ring (may be < capacity
        before steady-state, == capacity once filled past the
        ``maxlen`` watermark)."""
        with self._lock:
            return len(self._buf)

    def write(self, iq: np.ndarray) -> None:
        """Append complex IQ samples to the ring.

        ``iq`` is a 1D ndarray convertible to ``dtype=np.complex64``.
        If ``iq.size > capacity``, the deque's maxlen semantics
        retain only the most recent ``capacity`` samples (drops
        oldest from prior writes AND drops oldest from this write
        to fit).
        """
        arr = np.asarray(iq, dtype=np.complex64).ravel()
        if arr.size == 0:
            return
        with self._lock:
            self._buf.extend(arr.tolist())
            self.samples_written += int(arr.size)

    def snapshot(
        self, n_samples: Optional[int] = None,
    ) -> np.ndarray:
        """Return the most-recent ``n_samples`` from the ring as
        a complex64 ndarray in oldest-first order.

        If ``n_samples`` is None, returns the entire current
        contents.  If the ring contains fewer than ``n_samples``,
        returns whatever's there (no zero-padding -- caller can
        check ``snap.size`` to detect short reads).

        The returned ndarray is a fresh copy; safe to mutate after
        the lock releases.  Concurrent writes between snapshot()
        return and caller's use will NOT race against this copy.
        """
        with self._lock:
            self.snapshots_taken += 1
            if n_samples is None:
                return np.array(self._buf, dtype=np.complex64)
            n = min(int(n_samples), len(self._buf))
            if n == 0:
                return np.zeros(0, dtype=np.complex64)
            # deque doesn't slice; convert to list once then slice.
            # For typical snapshot sizes (~5000 samples) this is
            # ~20 us -- well under the v0.3 PS calcc cadence (~1 Hz).
            tail = list(self._buf)[-n:]
            return np.array(tail, dtype=np.complex64)

    def clear(self) -> None:
        """Drop all samples from the ring.

        Phase 3 PTT state machine call sites:
        * MOX=1 edge (entering TX from RX) -- discard stale RX-time
          contents (the tap was empty anyway since writes are
          gated on inject_tx_iq, but be defensive).
        * After v0.3 PS calibration completes -- decouple next
          calibration cycle from previous one.

        Idempotent; safe to call on an empty ring.
        """
        with self._lock:
            self._buf.clear()
            self.cleared += 1
