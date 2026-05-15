"""Sip1 TX I/Q tap tests (v0.2 Phase 2 commit 9, consensus §8.2).

Validates:
1. Basic write/snapshot round-trip with order preserved.
2. ``snapshot(n)`` returns the most-recent N samples.
3. Ring drops oldest when capacity is exceeded.
4. ``clear()`` empties the ring and bumps the cleared counter.
5. Diagnostic counters tick correctly.
6. Concurrent write/snapshot from multiple threads is safe (no
   partial-write tearing of complex64 samples).
7. TxDspWorker, when given an iq_tap, writes to it ONLY when
   ``inject_tx_iq`` is True (matches the EP2 forward gate).

The tap is the v0.3 PureSignal calibration tap point; for v0.2
only the producer is wired.  No consumer exists yet.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from lyra.dsp.tx_iq_tap import Sip1Tap


def test_default_capacity():
    """Default capacity = 8192 (matches the docstring claim)."""
    tap = Sip1Tap()
    assert tap.capacity == 8192
    assert tap.depth == 0
    assert tap.samples_written == 0


def test_write_and_snapshot_round_trip():
    """Write a sequence, snapshot returns identical samples in order."""
    tap = Sip1Tap(capacity=100)
    samples = np.array(
        [1 + 2j, 3 + 4j, 5 + 6j, 7 + 8j],
        dtype=np.complex64,
    )
    tap.write(samples)
    snap = tap.snapshot()
    assert snap.size == 4
    assert np.array_equal(snap, samples)


def test_snapshot_with_n_returns_most_recent():
    """``snapshot(n)`` returns the LAST n samples in oldest-first order."""
    tap = Sip1Tap(capacity=100)
    tap.write(np.arange(10, dtype=np.complex64))
    snap = tap.snapshot(n_samples=3)
    assert snap.size == 3
    # Most-recent 3 samples = indices 7, 8, 9
    expected = np.array([7 + 0j, 8 + 0j, 9 + 0j], dtype=np.complex64)
    assert np.array_equal(snap, expected)


def test_snapshot_n_larger_than_depth_returns_what_exists():
    """If caller asks for more samples than the ring contains,
    return whatever's there (no zero-padding -- caller checks size)."""
    tap = Sip1Tap(capacity=100)
    tap.write(np.arange(3, dtype=np.complex64))
    snap = tap.snapshot(n_samples=100)
    assert snap.size == 3


def test_snapshot_empty_ring_returns_zero_length_array():
    """Empty ring → zero-length ndarray (not None, not error)."""
    tap = Sip1Tap()
    snap = tap.snapshot()
    assert isinstance(snap, np.ndarray)
    assert snap.size == 0
    assert snap.dtype == np.complex64

    snap2 = tap.snapshot(n_samples=10)
    assert snap2.size == 0


def test_ring_drops_oldest_when_capacity_exceeded():
    """Write more samples than capacity → oldest drop, newest stay."""
    tap = Sip1Tap(capacity=5)
    tap.write(np.arange(8, dtype=np.complex64))
    # Capacity 5, wrote 8 → drops samples 0,1,2; retains 3,4,5,6,7
    assert tap.depth == 5
    snap = tap.snapshot()
    expected = np.arange(3, 8, dtype=np.complex64)
    assert np.array_equal(snap, expected)
    # Cumulative counter reflects total writes (NOT current depth)
    assert tap.samples_written == 8


def test_clear_empties_ring_and_bumps_counter():
    """``clear()`` drops contents and increments the cleared counter."""
    tap = Sip1Tap()
    tap.write(np.arange(100, dtype=np.complex64))
    assert tap.depth == 100
    tap.clear()
    assert tap.depth == 0
    assert tap.cleared == 1
    # Idempotent
    tap.clear()
    assert tap.cleared == 2


def test_snapshot_counter_increments():
    """Every snapshot() call bumps snapshots_taken."""
    tap = Sip1Tap()
    tap.write(np.array([1 + 0j], dtype=np.complex64))
    assert tap.snapshots_taken == 0
    tap.snapshot()
    assert tap.snapshots_taken == 1
    tap.snapshot(n_samples=1)
    assert tap.snapshots_taken == 2


def test_snapshot_returns_copy_not_view():
    """Mutating a returned snapshot does NOT affect the ring."""
    tap = Sip1Tap()
    tap.write(np.array([1 + 0j, 2 + 0j, 3 + 0j], dtype=np.complex64))
    snap = tap.snapshot()
    snap[0] = 99 + 0j
    snap2 = tap.snapshot()
    # Original ring untouched
    assert snap2[0] == 1 + 0j


def test_zero_length_write_is_noop():
    """Writing an empty array does NOT bump samples_written."""
    tap = Sip1Tap()
    tap.write(np.zeros(0, dtype=np.complex64))
    assert tap.depth == 0
    assert tap.samples_written == 0


def test_capacity_validation():
    """capacity < 1 raises."""
    import pytest
    with pytest.raises(ValueError):
        Sip1Tap(capacity=0)
    with pytest.raises(ValueError):
        Sip1Tap(capacity=-1)


def test_concurrent_writes_and_snapshots():
    """Multi-threaded write/snapshot is lock-safe -- no partial
    writes visible to readers."""
    tap = Sip1Tap(capacity=10000)
    stop = threading.Event()

    def _writer():
        i = 0
        while not stop.is_set():
            tap.write(np.array(
                [complex(i, i)], dtype=np.complex64,
            ))
            i += 1
            if i > 5000:
                break

    def _snapshotter():
        for _ in range(200):
            snap = tap.snapshot(n_samples=50)
            # Each sample is of form complex(k, k) for some int k.
            # Verify no torn writes: real == imag for every sample.
            for s in snap:
                assert s.real == s.imag, (
                    f"Torn write detected: real={s.real} imag={s.imag}"
                )
            time.sleep(0.001)

    writer_threads = [
        threading.Thread(target=_writer) for _ in range(3)
    ]
    snap_threads = [
        threading.Thread(target=_snapshotter) for _ in range(2)
    ]
    for t in writer_threads + snap_threads:
        t.start()
    for t in snap_threads:
        t.join(timeout=5.0)
    stop.set()
    for t in writer_threads:
        t.join(timeout=5.0)

    # Ring should have many samples by now
    assert tap.depth > 0


# ─── TxDspWorker integration tests ────────────────────────────────


class _FakeTxChannel:
    """Stand-in for ``lyra.dsp.wdsp_tx_engine.TxChannel``.

    process() returns same-length complex64 (mock baseband)."""
    def __init__(self):
        self.calls = []

    def process(self, mic):
        self.calls.append(int(mic.size))
        return np.full(mic.size, 0.5 + 0.25j, dtype=np.complex64)


class _FakeHL2Stream:
    """Stand-in for ``lyra.protocol.stream.HL2Stream``."""
    def __init__(self):
        self.inject_tx_iq = False
        self.queued = []

    def queue_tx_iq(self, iq):
        self.queued.append(int(iq.size))


def _await(predicate, timeout_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_worker_writes_tap_when_inject_tx_iq_true():
    """When ``inject_tx_iq`` is True AND iq_tap is set, the worker
    writes processed I/Q to the tap (in addition to forwarding it
    to HL2Stream)."""
    from lyra.dsp.tx_dsp_worker import TxDspWorker

    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    hl2.inject_tx_iq = True  # simulate Phase 3 MOX=1 edge
    tap = Sip1Tap(capacity=1024)
    worker = TxDspWorker(tx, hl2, iq_tap=tap)
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: tap.depth == 38), (
            f"Tap never received samples (depth={tap.depth})"
        )
        # Worker also forwarded to HL2Stream (commit 8 path)
        assert hl2.queued == [38]
        # tap_writes counter ticked
        assert worker.tap_writes == 1
    finally:
        worker.stop()


def test_worker_does_not_write_tap_during_rx():
    """When ``inject_tx_iq`` is False, the tap is NOT written --
    tap content is only what actually went on the air."""
    from lyra.dsp.tx_dsp_worker import TxDspWorker

    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    hl2.inject_tx_iq = False  # RX mode
    tap = Sip1Tap(capacity=1024)
    worker = TxDspWorker(tx, hl2, iq_tap=tap)
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        # Give worker time to (not) write to tap
        assert _await(lambda: tx.calls == [38])
        time.sleep(0.05)
        assert tap.depth == 0, (
            f"Tap was written during RX (depth={tap.depth})"
        )
        assert worker.tap_writes == 0
        # HL2Stream forward also skipped (consistent gate)
        assert hl2.queued == []
    finally:
        worker.stop()


def test_worker_without_tap_still_functions():
    """``iq_tap=None`` (default) -- worker behaves exactly like
    pre-commit-9 (forward to HL2Stream, no tap path)."""
    from lyra.dsp.tx_dsp_worker import TxDspWorker

    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    hl2.inject_tx_iq = True
    worker = TxDspWorker(tx, hl2)  # iq_tap omitted = None
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: hl2.queued == [38])
        assert worker.tap_writes == 0
    finally:
        worker.stop()
