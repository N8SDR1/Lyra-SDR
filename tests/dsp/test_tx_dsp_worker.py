"""TxDspWorker thread tests (v0.2 Phase 2 commit 7-redo).

Validates:
1. Producer threads (RX-loop, PortAudio) never block on ``submit``.
2. Worker thread drains the queue and calls ``TxChannel.process``.
3. ``HL2Stream.queue_tx_iq`` is called only when ``inject_tx_iq`` is True.
4. Diagnostic counters (submitted / dropped / processed / errors /
   queued_iq_blocks) track expected events.
5. Queue overflow drops oldest + bumps ``dropped``.
6. ``stop()`` joins the worker thread cleanly within timeout.

Uses minimal fake TxChannel + fake HL2Stream so tests don't depend
on WDSP cffi or real UDP sockets.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from lyra.dsp.tx_dsp_worker import TxDspWorker


class _FakeTxChannel:
    """Stand-in for ``lyra.dsp.wdsp_tx_engine.TxChannel``.

    ``process(mic_float32)`` returns ``len(mic)`` complex64 samples
    (mock baseband -- not real WDSP output, just length-matched so
    the worker's I/Q forwarding path is exercised).  Records every
    call for assertion.
    """

    def __init__(self, *, raise_on_call: bool = False,
                 process_delay_s: float = 0.0) -> None:
        self.raise_on_call = raise_on_call
        self.process_delay_s = process_delay_s
        self.calls: list[int] = []   # records each call's input length
        self._lock = threading.Lock()

    def process(self, mic: np.ndarray) -> np.ndarray:
        if self.process_delay_s > 0:
            time.sleep(self.process_delay_s)
        if self.raise_on_call:
            raise RuntimeError("simulated TxChannel.process failure")
        with self._lock:
            self.calls.append(int(mic.size))
        # Return matching-length complex64 (mock baseband)
        return np.full(
            mic.size, 0.1 + 0.0j, dtype=np.complex64,
        )


class _FakeHL2Stream:
    """Stand-in for ``lyra.protocol.stream.HL2Stream``.

    Only needs ``inject_tx_iq`` flag + ``queue_tx_iq(iq)`` method
    for the worker's purposes.  Records every queue_tx_iq call.
    """

    def __init__(self) -> None:
        self.inject_tx_iq: bool = False
        self.queued: list[int] = []  # records each queued chunk's size
        self._lock = threading.Lock()

    def queue_tx_iq(self, iq) -> None:
        with self._lock:
            self.queued.append(int(iq.size))


def _await(predicate, timeout_s: float = 2.0,
           poll_interval_s: float = 0.005) -> bool:
    """Poll ``predicate`` until True or timeout.  Returns whether
    predicate was True at exit (useful for assertion messages)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_interval_s)
    return predicate()


def test_worker_processes_submitted_samples():
    """Submit a chunk → worker processes it → TxChannel.process called."""
    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2)
    worker.start()
    try:
        samples = np.zeros(38, dtype=np.float32)
        worker.submit(samples)
        # Worker should drain within a few ms
        assert _await(lambda: len(tx.calls) == 1), (
            f"TxChannel.process never called (calls={tx.calls})"
        )
        assert tx.calls[0] == 38
        assert worker.submitted == 1
        assert worker.processed == 1
        assert worker.dropped == 0
        assert worker.errors == 0
    finally:
        worker.stop()


def test_worker_queues_iq_only_when_inject_tx_iq_true():
    """When inject_tx_iq is False, queue_tx_iq is NOT called even
    though TxChannel.process was called -- the I/Q is dropped on
    the floor.  When True, queue_tx_iq IS called."""
    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    hl2.inject_tx_iq = False
    worker = TxDspWorker(tx, hl2)
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: len(tx.calls) == 1)
        # Give the worker a moment to (not) forward
        time.sleep(0.05)
        assert len(hl2.queued) == 0, (
            f"queue_tx_iq fired during RX: {hl2.queued}"
        )
        assert worker.queued_iq_blocks == 0

        # Flip the flag and submit again
        hl2.inject_tx_iq = True
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: len(hl2.queued) == 1), (
            f"queue_tx_iq did not fire during TX (queued={hl2.queued})"
        )
        assert hl2.queued[0] == 38
        assert worker.queued_iq_blocks == 1
    finally:
        worker.stop()


def test_submit_does_not_block_producer():
    """Even with a slow process() call, submit returns immediately.

    Validates the core architectural guarantee: producer threads
    (RX-loop, PortAudio) MUST NOT be blocked by DSP latency.
    """
    tx = _FakeTxChannel(process_delay_s=0.1)  # 100 ms per call -- slow
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2)
    worker.start()
    try:
        # Time several submits; each must be << process_delay_s
        for _ in range(5):
            t0 = time.monotonic()
            worker.submit(np.zeros(38, dtype=np.float32))
            dt = time.monotonic() - t0
            # Sub-millisecond tolerance.  Real submits are
            # ~microseconds; 10 ms is a very generous CI-tolerant cap.
            assert dt < 0.010, (
                f"submit blocked for {dt*1000:.1f} ms "
                f"(should be <10 ms regardless of DSP latency)"
            )
    finally:
        worker.stop(timeout=2.0)


def test_queue_overflow_drops_oldest():
    """Fill the queue beyond maxsize -- oldest drops, dropped counter
    increments, newest replaces.
    """
    # Use a tiny queue + a slow processor to guarantee overflow
    tx = _FakeTxChannel(process_delay_s=0.5)
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2, queue_maxsize=2)
    worker.start()
    try:
        # First submit gets pulled from queue by worker, blocks in
        # process_delay.  Next 5 submits fill + overflow the queue.
        for i in range(6):
            worker.submit(np.array([float(i)], dtype=np.float32))
            time.sleep(0.001)  # give worker time to pull #1 from queue
        # At least some drops should have occurred (queue size 2, 6
        # submits, only 1-2 can be in queue at a time)
        time.sleep(0.05)  # let counters settle
        assert worker.dropped > 0, (
            f"No drops occurred (submitted={worker.submitted}, "
            f"dropped={worker.dropped}) -- queue overflow path "
            "did not trigger"
        )
    finally:
        worker.stop(timeout=2.0)


def test_worker_handles_process_exception():
    """TxChannel.process raising doesn't crash the worker; errors
    counter increments + loop continues."""
    tx = _FakeTxChannel(raise_on_call=True)
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2)
    worker.start()
    try:
        worker.submit(np.zeros(38, dtype=np.float32))
        worker.submit(np.zeros(38, dtype=np.float32))
        worker.submit(np.zeros(38, dtype=np.float32))
        assert _await(lambda: worker.errors == 3), (
            f"errors counter didn't reach 3 (got {worker.errors})"
        )
        # Worker is still alive
        assert worker.is_running
        assert worker.processed == 0  # nothing succeeded
    finally:
        worker.stop()


def test_stop_joins_thread_within_timeout():
    """stop() returns within timeout even if no submits ever came."""
    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2)
    worker.start()
    assert worker.is_running
    t0 = time.monotonic()
    worker.stop(timeout=1.0)
    dt = time.monotonic() - t0
    assert dt < 0.5, (
        f"stop() took {dt*1000:.0f} ms (should be <500 ms via "
        "sentinel + 0.1 s get timeout)"
    )
    assert not worker.is_running


def test_stop_is_idempotent():
    """stop() called twice is harmless."""
    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2)
    worker.start()
    worker.stop()
    worker.stop()  # should not raise
    assert not worker.is_running


def test_start_is_idempotent():
    """start() called twice is harmless -- second call is no-op."""
    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    worker = TxDspWorker(tx, hl2)
    worker.start()
    first_thread = worker._thread  # noqa: SLF001
    worker.start()  # should not replace the running thread
    assert worker._thread is first_thread  # noqa: SLF001
    worker.stop()


def test_submit_from_multiple_threads():
    """Concurrent submits from many threads all land in the queue;
    none get lost; counters add up correctly."""
    tx = _FakeTxChannel()
    hl2 = _FakeHL2Stream()
    # Big queue so no overflow; we're testing concurrent producer safety
    worker = TxDspWorker(tx, hl2, queue_maxsize=10000)
    worker.start()

    def _producer(n: int):
        for _ in range(n):
            worker.submit(np.zeros(1, dtype=np.float32))

    try:
        n_threads = 8
        n_per_thread = 100
        threads = [
            threading.Thread(target=_producer, args=(n_per_thread,))
            for _ in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        # Wait for worker to drain
        expected_total = n_threads * n_per_thread
        assert _await(
            lambda: worker.submitted + worker.dropped >= expected_total,
            timeout_s=3.0,
        ), (
            f"submitted+dropped ({worker.submitted}+{worker.dropped})"
            f" never reached {expected_total}"
        )
        # All submits counted (no losses)
        assert worker.submitted + worker.dropped == expected_total
        # Worker still alive
        assert worker.is_running
    finally:
        worker.stop(timeout=2.0)
