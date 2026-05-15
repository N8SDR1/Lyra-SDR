"""Dedicated TX DSP worker thread (v0.2 Phase 2 commit 7-redo).

Owns the threading boundary between mic-input producers (RX-loop
thread for HL2 jack path, PortAudio audio thread for PC sound
card path) and the WDSP TXA cffi chain (which blocks for ~10 ms
per in_size=512 block of mic samples processed via fexchange0).

Replaces Phase 2 commit 7's broken direct-call dispatch where
``mic_callback -> dispatch_tx -> TxChannel.process -> fexchange0
(block=1)`` blocked the RX-loop thread for the duration of every
DSP call, starving the entire RX path (audio + spectrum +
telemetry).

Architecture (CLAUDE.md §5 threading model):

    HL2Stream._rx_loop  (Thread 1, RX path)
        -> mic_callback(int16 samples, FrameStats)
        -> TxDspWorker.submit(float32 samples)     [non-blocking]

    SoundDeviceMicSource  (PortAudio audio thread)
        -> consumer(float32 samples)
        -> TxDspWorker.submit(float32 samples)     [non-blocking]

    TxDspWorker._run_loop  (Thread NEW)
        -> queue.get(timeout)
        -> TxChannel.process(float32)              [fexchange0 blocks HERE]
        -> HL2Stream.queue_tx_iq(complex64)        [gated by inject_tx_iq]

    HL2Stream._ep2_writer_loop  (Thread 4)
        -> _pack_audio_bytes_pairs(...)
        -> _drain_tx_iq_be(126)
        -> EP2 frame bytes on the wire

The worker is always running while the stream is up; it processes
mic data continuously even on RX (keeps the WDSP TXA chain in
steady state).  The result is only forwarded to ``HL2Stream`` when
``hl2_stream.inject_tx_iq`` is True (flipped by Phase 3 PTT state
machine on MOX=1 edge).  During RX the I/Q output is silently
dropped -- cheap, and avoids spectral state surprises at the first
PTT after long idle.
"""
from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from lyra.dsp.tx_iq_tap import Sip1Tap
    from lyra.dsp.wdsp_tx_engine import TxChannel
    from lyra.protocol.stream import HL2Stream


class TxDspWorker:
    """Background DSP worker that pulls mic samples from a bounded
    queue, processes them through ``tx_channel`` (a WDSP TXA cffi
    wrapper), and pushes the resulting I/Q to
    ``hl2_stream.queue_tx_iq`` when transmit is active.

    Lifecycle:

        worker = TxDspWorker(tx_channel, hl2_stream)
        worker.start()              # spawn the thread
        ...
        worker.submit(samples)      # producer side (non-blocking)
        ...
        worker.stop()               # signal + join thread cleanly

    Threading: ``submit`` is producer-safe and callable from any
    thread (RX-loop, PortAudio audio thread, etc.).  ``start`` /
    ``stop`` are NOT thread-safe; call from Radio's owning thread
    (Qt main).
    """

    # Queue cap: ~50 mic chunks.  Mic chunks arrive from HL2 EP6
    # at 38 samples per UDP datagram (~0.79 ms each) -- 50 chunks
    # = ~40 ms buffered worst case.  Larger queue = larger TX
    # latency spike if worker stalls; smaller queue = more frequent
    # overflow under bursty producers.  50 is a comfortable middle
    # for typical EP6 cadence; tunable via constructor kwarg.
    _DEFAULT_QUEUE_MAXSIZE = 50

    def __init__(
        self,
        tx_channel: "TxChannel",
        hl2_stream: "HL2Stream",
        iq_tap: "Optional[Sip1Tap]" = None,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._tx_channel = tx_channel
        self._hl2_stream = hl2_stream
        # v0.2 Phase 2 commit 9: optional sip1 TX I/Q tap for v0.3
        # PureSignal calibration.  When set, the worker writes each
        # processed I/Q block into the tap's ring buffer (after the
        # HL2Stream.queue_tx_iq forward, gated on the same
        # inject_tx_iq flag so the tap only contains "what actually
        # went on the air" history).  Consumer (v0.3 PS calcc
        # thread) reads via tap.snapshot().
        self._iq_tap = iq_tap
        self._queue: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(
            maxsize=queue_maxsize,
        )
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Diagnostic counters (read by Phase 3 UI / status bar).
        # All write sites are inside the worker thread or producer
        # threads -- single-writer-per-counter pattern, GIL-atomic
        # int reads from any other thread.
        self.submitted: int = 0     # successful submits
        self.dropped: int = 0       # submits dropped due to queue full
        self.processed: int = 0     # successful tx_channel.process calls
        self.errors: int = 0        # exceptions caught in run loop
        self.queued_iq_blocks: int = 0  # I/Q chunks forwarded to HL2Stream
        self.tap_writes: int = 0    # I/Q chunks written to sip1 tap

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Spawn the worker thread.  Idempotent."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="TxDspWorker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Signal the worker thread to exit and join.

        Sends a sentinel (None) through the queue so the blocking
        ``get()`` wakes immediately; sets ``_stop_event`` so the
        loop also notices on its next iteration.  Joins with timeout
        to avoid blocking the Qt main thread if the worker is wedged
        on a long DSP call.

        Idempotent.  Safe to call multiple times.
        """
        if not self.is_running:
            self._thread = None
            return
        self._stop_event.set()
        # Sentinel to wake the get() blocking call.  put_nowait so
        # we don't block the caller; if the queue is full the
        # stop_event check + 0.1s get timeout handles eventual
        # termination within ~100 ms.
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None

    def submit(self, samples: np.ndarray) -> None:
        """Producer-side non-blocking enqueue of mic samples.

        ``samples`` is float32 mono (any length).  Callable from any
        thread -- typically the RX-loop thread (HL2 jack path) or
        the PortAudio audio thread (PC sound card path).

        On queue overflow, drops the OLDEST sample chunk to make
        room (mirror of ``deque(maxlen=...)`` semantics; preserves
        freshness over completeness) and bumps the ``dropped``
        counter.  Best-effort: racing producers may see the drop
        unsuccessful, which is fine -- the queue will catch up on
        the next call.
        """
        try:
            self._queue.put_nowait(samples)
            self.submitted += 1
            return
        except queue.Full:
            pass
        # Queue is full -- drop the oldest, retry once.
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(samples)
        except queue.Full:
            pass
        self.dropped += 1

    def _run_loop(self) -> None:
        """Drain the queue, process each chunk through TxChannel,
        push resulting I/Q to HL2Stream when transmit is active.
        """
        while not self._stop_event.is_set():
            try:
                samples = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if samples is None:
                # Sentinel -- stop was called.
                break
            try:
                iq = self._tx_channel.process(samples)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                print(f"[TxDspWorker] process error: {exc}")
                continue
            self.processed += 1
            # Gate I/Q forwarding on the HL2Stream's inject flag.
            # When False (default, RX-only), the WDSP chain stayed
            # warm but the result is dropped on the floor.  Phase 3
            # PTT state machine flips inject_tx_iq=True on MOX=1
            # edge, and the I/Q starts flowing to the EP2 writer
            # AND to the sip1 tap (when present).
            if iq.size > 0 and self._hl2_stream.inject_tx_iq:
                try:
                    self._hl2_stream.queue_tx_iq(iq)
                    self.queued_iq_blocks += 1
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    print(f"[TxDspWorker] queue_tx_iq error: {exc}")
                # v0.2 Phase 2 commit 9: sip1 tap (when wired) gets
                # a copy of the same I/Q that went on the wire.  v0.3
                # PS calcc thread snapshots this for time-alignment
                # against the DDC0+DDC1 feedback path.  Tap writes
                # are independent of queue_tx_iq success/failure so
                # a transient HL2Stream queue overrun doesn't
                # corrupt the PS calibration history.
                if self._iq_tap is not None:
                    try:
                        self._iq_tap.write(iq)
                        self.tap_writes += 1
                    except Exception as exc:  # noqa: BLE001
                        self.errors += 1
                        print(f"[TxDspWorker] iq_tap.write error: {exc}")
