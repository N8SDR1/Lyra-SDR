"""DSP worker thread — Phase 3.B BETA scaffolding.

Implements the threaded DSP path described in
``docs/architecture/threading.md``. **This file is the SHELL only**
— no DSP behavior runs through it yet. Subsequent commits
(B.2 → B.10) progressively migrate audio / AGC / sink / spectrum
work into the worker.

The worker is an OPT-IN BETA toggle controlled by the operator via
**Settings → DSP → Threading**. Default is "Single-thread (current)"
(unchanged from v0.0.5). When the operator selects "Worker (BETA)",
the audio path routes IQ through this worker thread instead of the
current main-thread ``Radio._on_samples_main_thread``.

Until Phase 3.B is complete, this module exists but is not wired
into Radio's audio path. Phase 3.A's design doc covers the full
migration plan; this is sub-task B.1.

Lifecycle
---------
QObject + ``moveToThread`` pattern (modern Qt-recommended over
``QThread.run`` override). Pseudo-code::

    worker = DspWorker()                 # construct on main thread
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)
    thread.start()                       # worker now alive

    # ... operator runs streaming; rx thread calls worker.enqueue_iq ...

    worker.request_stop()                # signal exit
    thread.quit()
    thread.wait(1000)                    # bounded shutdown

Cross-thread communication
--------------------------
- **Main → worker**: setter signals from Radio (e.g.
  ``apf_enabled_changed``) wire to the worker's slots via
  ``Qt.QueuedConnection``. The slot runs on the worker thread,
  serialized with ``run_loop``'s block processing — no locks needed.
- **rx thread → worker**: ``enqueue_iq()`` is called from the HPSDR
  rx thread; uses a thread-safe queue with drop-oldest behavior.
- **Worker → main**: emits ``spectrum_ready``, ``smeter_reading``,
  ``lna_peak_update`` as Qt signals which traverse to the main
  thread's slots via the queued connection.

State migration map (B.2 onwards)
---------------------------------
What this commit (B.1) provides:
- ``WorkerConfig`` dataclass (just the fields not encapsulated by
  ``PythonRxChannel`` — AGC envelope config, AF/Vol/Mute, BIN
  enable + depth)
- Bounded input queue with drop-oldest policy
- Lifecycle (start, stop, reset request)
- ``process_block`` stub that does NOTHING yet
- Config-update slots (will be wired by Radio in B.2+)

What this commit DOES NOT do:
- Actual DSP routing — ``process_block`` is a no-op
- ``rx_channel`` ownership migration (B.3)
- Audio sink ownership migration (B.5)
- LNA peak/RMS tracking migration (B.6)
- FFT migration (B.8)
- Reset/flush wiring (B.9)
- Settings toggle integration (B.2)

Progressive migration is intentional. Each B.x commit is small
enough to be reverted independently if anything goes wrong in
field testing.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot


# ── Worker-owned config snapshot ──────────────────────────────────────
@dataclass
class WorkerConfig:
    """Worker-owned config mirror.

    Operator-driven config lives on Radio (main thread). Setters there
    fire ``*_changed`` signals; the worker's slots (running on the
    worker thread via ``Qt.QueuedConnection``) update this dataclass.
    The worker reads ``self._config`` directly inside ``process_block``
    — Qt's event loop interleaves slot delivery between blocks, so no
    locks are needed.

    **Only state that the worker DIRECTLY computes against goes here.**
    Mode / freq / rate / RX BW / notches / NR / APF / CW pitch all
    flow through the rx_channel's existing setter methods, not through
    this dataclass. Keeping ``WorkerConfig`` small reduces the
    cross-thread surface area we have to keep in sync.
    """

    # ── AGC envelope tracker (config) ────────────────────────────
    # Mutable per-block AGC state (peak, hang_counter) lives on
    # the DspWorker instance directly, not in this config dataclass —
    # the dataclass holds operator-tunable parameters only.
    agc_profile: str = "med"        # off / fast / med / slow / auto / custom
    agc_release: float = 0.158      # exponential release per block
    agc_target: float = 0.0316      # linear amplitude target (~ -30 dBFS)
    agc_hang_blocks: int = 0        # blocks to hold peak before decay

    # ── Audio chain post-AGC ─────────────────────────────────────
    af_gain_db: int = 25            # 0..+80 dB pre-AGC makeup
    volume: float = 0.5             # 0.0..1.0 final trim
    muted: bool = False

    # ── BIN — Hilbert phase split for headphone listening ────────
    bin_enabled: bool = False
    bin_depth: float = 0.7          # 0.0..1.0 spatial separation


# ── Worker class ──────────────────────────────────────────────────────
class DspWorker(QObject):
    """Phase 3.B DSP worker thread (BETA, opt-in).

    Construct on the main thread, then ``moveToThread(qt_thread)`` to
    migrate ownership. Wire ``qt_thread.started`` to ``run_loop``.
    The worker stays alive for the duration of the radio session and
    exits cleanly on ``request_stop()``.

    See module docstring for the full lifecycle and migration plan.
    """

    # ── Signals (worker → main thread) ──────────────────────────
    # These are delivered to main-thread slots via Qt's queued
    # connection mechanism (the default for cross-thread signals).

    spectrum_raw_ready = Signal(object)
    """Raw post-FFT spectrum (np.float32 array, length _fft_size).

    Carries ONLY the spec_db array — center_hz / rate / zoom /
    waterfall cadence / S-meter / auto-scale all stay on the main
    thread (read live from Radio in ``_process_spec_db``).  The
    worker does the heavy numerical lift (FFT itself), main does
    the small-but-stateful UI work.

    The signal name was ``spectrum_ready`` in the B.1 shell — renamed
    to ``spectrum_raw_ready`` in B.8 to make the contract explicit
    (Radio's spectrum_ready, which the UI subscribes to, is still
    emitted from main thread inside ``_process_spec_db`` after this
    raw spectrum is post-processed)."""

    smeter_reading = Signal(float)
    """Linear-power running average for S-meter, sampled at meter
    cadence (~6 Hz today).  Migrated from Radio in B.4/B.5."""

    lna_peak_update = Signal(float, float)
    """peak_dbfs, rms_dbfs — per-block IQ peak + RMS for the
    Auto-LNA logic on the main thread.  Migrated in B.6."""

    state_changed = Signal(str)
    """Lifecycle observability: emits "running", "stopped" on
    transitions. Settings → DSP can show the worker's current state."""

    # ── Configuration ───────────────────────────────────────────
    INPUT_QUEUE_DEPTH = 10
    """Bounded input-queue depth in batches. At the typical 2048-
    sample IQ batch, 10 deep ≈ 1 second of buffered IQ at 48 kHz —
    plenty for transient main-thread stalls (UI repaints, GC pauses)
    without unbounded memory growth.  Drop-oldest beyond this."""

    RUN_LOOP_TIMEOUT_S = 0.1
    """Block briefly on the input queue so the loop can exit
    promptly when ``request_stop()`` is set without busy-waiting."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._config = WorkerConfig()
        # Bounded queue — producer is the rx thread, consumer is
        # this worker's run_loop.  Python's ``queue.Queue`` is
        # thread-safe; we use put_nowait/get_nowait to avoid any
        # blocking surprises.
        self._input_queue: Queue = Queue(maxsize=self.INPUT_QUEUE_DEPTH)
        # Phase 2 v0.1 (2026-05-11): sibling queue for RX2 (DDC1) IQ
        # batches.  Filled by ``Radio._stream_cb_rx2`` lock-step with
        # the main ``_input_queue``; drained by ``run_loop`` with a
        # non-blocking ``get_nowait`` immediately after the RX1
        # blocking get, so the worker pairs (rx1, rx2) batches per
        # iteration.  Since RX1 and RX2 per-DDC sample counts are
        # equal at nddc=4 (per CLAUDE.md §3.6 HL2 P1 caveat) and
        # ``_stream_cb`` / ``_stream_cb_rx2`` accumulate at the same
        # rate, the queues stay synchronized in normal operation.
        # If RX2 queue is empty when RX1 has a batch (transient
        # startup race), the worker falls back to RX1-only
        # processing for that iteration (no audible glitch).
        self._input_queue_rx2: Queue = Queue(maxsize=self.INPUT_QUEUE_DEPTH)
        # Lifecycle flags — set by request_*() and read by run_loop.
        # Plain attribute reads/writes; no locks needed because the
        # Python GIL serializes single-attribute access and we don't
        # need atomic compound updates.
        self._stop_requested: bool = False
        self._reset_requested: bool = False
        # TX keydown/keyup: stop/start the WDSP RX channel itself
        # (not just gate audio).  None = no pending request, True =
        # start, False = stop-with-flush.  Applied between blocks
        # like _reset_requested so it never races process_block.
        self._rx_chan_req: "bool | None" = None
        # Phase 3.B B.3 — back-reference to Radio for the audio chain.
        # The worker calls radio._do_demod_wdsp() (the WDSP cffi
        # engine), radio._emit_tone(), and reads radio._mode +
        # radio._wdsp_rx from the worker thread.  Phase 5 (v0.0.9.6)
        # eliminated the legacy radio._rx_channel.process() /
        # radio._apply_agc_and_volume() / radio._binaural.process()
        # call sites — WDSP handles those internally.  Wired by
        # Radio after construction via ``attach_to_radio()``.
        self._radio = None
        # Phase 3.B B.5 — worker-owned audio sink reference.  Seeded
        # by Radio just before ``moveToThread`` so the worker has a
        # valid sink from frame zero, then updated via signal+slot on
        # every sink swap (start / stop / output change / PC device
        # change).  The worker writes audio to THIS reference, not to
        # ``radio._audio_sink``, so a main-thread sink swap can never
        # close the underlying object mid-write.
        self._audio_sink = None
        # §15.21 bug 3 fix (§15.24 plan item B, 2026-05-15):
        # one-shot barrier the worker SETS at the end of every
        # _on_audio_sink_changed.  Radio.stop() clears it before
        # emitting the NullSink swap and waits (bounded) on it
        # afterward, so a rapid stop()->start() can't deliver the
        # stale NullSink swap AFTER the new real sink is installed
        # (worker would otherwise close/keep the wrong sink ->
        # transient silence).  Non-stop emit paths (start,
        # set_audio_output, PC device change) DON'T wait, so they
        # never block on the worker event loop -- the worker just
        # sets an Event nobody is waiting on (harmless).
        self._sink_swap_done = threading.Event()
        # Phase 3.B B.8 — worker-owned sample ring + FFT cadence.
        # When worker mode is active the ring lives here (not on
        # Radio) so there's no cross-thread contention with the
        # main-thread FFT timer (which is a no-op in worker mode).
        # Capacity matches Radio's: ``_fft_size * 4`` so the latest
        # ``_fft_size`` samples are always available even if FFT
        # cadence drifts.  Lazy-allocated in ``process_block`` once
        # we can read ``radio._fft_size`` (avoids a hard dependency
        # on attach order).
        self._sample_ring = None  # type: Optional[deque]
        # Block counter — increments every IQ batch process_block
        # is called with; FFT runs when it crosses
        # ``_fft_block_threshold``, computed from current rate +
        # operator's FPS preference (re-evaluated each block so FPS
        # / rate changes take effect immediately).
        self._fft_block_counter: int = 0
        # Heartbeat counter (v0.0.9.2 audio rebuild Commit 1) —
        # monotonic count of IQ blocks the worker has processed
        # since startup.  Read by the UI's 1 Hz status tick to
        # display "DSP worker: N Hz" in the status bar.  If the
        # number stops incrementing while audio is supposed to be
        # playing, the worker has stalled and audio is dead.
        # Plain int; GIL-protected for atomic single-attribute
        # read/write between worker thread and UI thread.  No lock
        # needed.
        self._blocks_processed: int = 0

    # ── Public API: producer-side (rx thread, main thread) ─────

    def enqueue_iq(self, samples: np.ndarray) -> None:
        """Push a batch of complex64 IQ samples onto the input queue.
        Called from the HPSDR rx thread.

        Drop-oldest policy: when the queue is full (worker can't
        keep up — e.g., peak DSP load on a slower machine), evict
        the oldest queued batch and push the new one. The operator
        hears at most a single block of audio dropout, but the
        application stays bounded in memory.
        """
        try:
            self._input_queue.put_nowait(samples)
        except Full:
            # Queue is full — drop the oldest, then push the new one.
            try:
                self._input_queue.get_nowait()
            except Empty:
                # Drained between the put_nowait and get_nowait — fine,
                # rare race that just means the queue's now empty so
                # the next put will succeed.
                pass
            try:
                self._input_queue.put_nowait(samples)
            except Full:
                # Lost the race a second time — give up; the new
                # samples are dropped.  Operator logs would show a
                # stutter pattern; fix is to investigate why the
                # worker is so far behind.  Extremely rare under
                # normal operating conditions.
                pass

    def enqueue_iq_rx2(self, samples: np.ndarray) -> None:
        """Push a batch of RX2 (DDC1) IQ samples onto the sibling
        input queue.  Phase 2 v0.1.

        Same drop-oldest policy as :meth:`enqueue_iq`.  ``run_loop``
        pairs RX1 and RX2 batches per iteration via a non-blocking
        ``get_nowait`` on this queue immediately after the blocking
        ``get`` on the main RX1 queue; if RX2 is empty at that
        moment the worker processes RX1 alone for that iteration
        (single-channel fallback — operator gets RX1 audio with no
        stereo split for one ~5 ms block, sub-perceptual).
        """
        try:
            self._input_queue_rx2.put_nowait(samples)
        except Full:
            try:
                self._input_queue_rx2.get_nowait()
            except Empty:
                pass
            try:
                self._input_queue_rx2.put_nowait(samples)
            except Full:
                pass

    def request_reset(self) -> None:
        """Request the worker to flush its DSP state on the next
        block boundary.  Called from the main thread on freq, mode,
        or rate change — any operator action that introduces a
        legitimate audio discontinuity."""
        self._reset_requested = True

    def request_rx_channel(self, on: bool) -> None:
        """Request the worker to start (on=True) or stop-with-flush
        (on=False) the WDSP RX channel on the next block boundary.
        Called from the main thread on TX keydown (stop) / the
        post-T/R-settle keyup point (start) so the receive chain
        does not process the keyed period or the T/R-transition
        IQ — applied between blocks, never racing process_block."""
        self._rx_chan_req = bool(on)

    def flush_fft_ring(self) -> None:
        """Phase 3.E.1 v0.1 (2026-05-12): flush the FFT sample ring
        so the next emitted spec_db is clean (no mixed-source bins).
        Called by the main thread when ``panadapter_source_rx``
        changes -- the ring currently holds samples from the
        previous source RX, and FFT-ing across a mix produces a
        single garbage frame.  Cheap operation (ring is a deque,
        clear is O(1) amortized)."""
        ring = self._sample_ring
        if ring is not None:
            ring.clear()
        self._fft_block_counter = 0

    def request_stop(self) -> None:
        """Request the worker to exit ``run_loop`` cleanly.  Called
        from the main thread on radio stop or Lyra shutdown."""
        self._stop_requested = True

    def attach_to_radio(self, radio) -> None:
        """Phase 3.B B.3 — link the worker to the Radio that owns the
        DSP objects (rx_channel, audio_sink, binaural, agc state).

        Called once by Radio just after worker construction.  Worker
        then references Radio's DSP machinery from its own thread
        when worker mode is active.

        This is a transitional pattern. Subsequent sub-tasks (B.5+)
        will migrate sink + LNA + FFT ownership directly into the
        worker; B.3 just shifts WHERE the existing Radio methods get
        called (worker thread instead of main).
        """
        self._radio = radio

    @property
    def config(self) -> WorkerConfig:
        """Read-only view of the current worker config.  Useful for
        diagnostics / Settings display.  Not for cross-thread
        mutation — main thread should fire setter signals to update
        the worker's config rather than poking the dataclass."""
        return self._config

    @property
    def blocks_processed(self) -> int:
        """Monotonic count of IQ batches the worker has processed
        since startup (v0.0.9.2 audio rebuild Commit 1).  Read by
        the main-thread UI tick to derive a "DSP worker Hz" readout
        from the delta between successive samples.  GIL-protected
        for cross-thread atomic read; no lock required."""
        return self._blocks_processed

    # ── Config update slots (called via Qt.QueuedConnection) ──
    # These slots run on the WORKER thread because of the queued
    # connection from main-thread Radio setters.  Updates between
    # ``process_block`` calls are safe — Qt serializes slot delivery
    # on the worker's event loop, not in the middle of a block.

    @Slot(str)
    def set_agc_profile(self, name: str) -> None:
        self._config.agc_profile = str(name)

    @Slot(float)
    def set_agc_release(self, release: float) -> None:
        self._config.agc_release = float(release)

    @Slot(float)
    def set_agc_target(self, target: float) -> None:
        self._config.agc_target = float(target)

    @Slot(int)
    def set_agc_hang_blocks(self, blocks: int) -> None:
        self._config.agc_hang_blocks = int(blocks)

    @Slot(int)
    def set_af_gain_db(self, db: int) -> None:
        self._config.af_gain_db = int(db)

    @Slot(float)
    def set_volume(self, vol: float) -> None:
        self._config.volume = float(vol)

    @Slot(bool)
    def set_muted(self, muted: bool) -> None:
        self._config.muted = bool(muted)

    @Slot(bool)
    def set_bin_enabled(self, on: bool) -> None:
        self._config.bin_enabled = bool(on)

    @Slot(float)
    def set_bin_depth(self, depth: float) -> None:
        self._config.bin_depth = float(depth)

    # ── Audio-sink ownership (B.5) ─────────────────────────────
    # Sink lifecycle in worker mode: main thread CONSTRUCTS the sink
    # (PortAudio device open, AK4951 stream wiring) and hands it to
    # the worker via this slot.  The worker swaps its local reference
    # AND closes the old sink — that close is safe because it runs on
    # the worker thread, BETWEEN process_block calls (Qt's queued
    # connection serializes slot delivery with the run-loop body).
    # Main thread therefore never closes a sink that the worker might
    # still be writing to, eliminating the "PortAudio close-while-
    # writing" race.

    @Slot(object)
    def _on_audio_sink_changed(self, new_sink) -> None:
        """Replace the worker's audio-sink reference.

        Called via Qt::QueuedConnection from
        ``Radio.worker_audio_sink_changed`` whenever Radio constructs
        a new sink (start, stop=NullSink, set_audio_output, PC device
        change).  Runs on the WORKER thread between blocks — no race
        with ``process_block``'s ``self._audio_sink.write(audio)``.

        Steps:
        1. Save the current sink reference as ``old``.
        2. Install the new sink.
        3. Close ``old`` if it's a different instance — drains any
           internal buffers (PortAudio CallbackStream stop, AK4951
           inject_audio_tx=False + clear_tx_audio).

        Errors during close are logged but never propagate; a half-
        closed sink is acceptable (worst case: a sliver of stale
        audio finishes draining; new sink starts clean).
        """
        old = self._audio_sink
        self._audio_sink = new_sink
        if old is not None and old is not new_sink:
            try:
                old.close()
            except Exception as exc:
                print(f"[DspWorker] old sink close error: {exc}")
        # §15.21 bug 3: signal the (possibly-waiting) Radio.stop()
        # that this swap has been fully applied on the worker
        # thread.  Set unconditionally for every swap -- only
        # stop() ever clears+waits on it; other callers ignore it.
        self._sink_swap_done.set()

    # ── Run loop (worker thread) ───────────────────────────────

    @Slot()
    def run_loop(self) -> None:
        """Worker-thread main loop.  Called once via ``thread.started``
        when the operator activates worker mode.

        Blocks briefly on the input queue, then calls ``process_block``
        on each batch.  Exits when ``request_stop()`` is set.

        **Qt event-loop pumping (v0.0.9.2 audio rebuild Commit 1
        fixup):** every iteration we call
        ``QCoreApplication.processEvents()`` so QueuedConnection
        slots delivered to this worker (sink swap, AGC profile
        change, BIN config change) actually run.  Without this
        pump, run_loop hogs the worker thread's event loop and
        signals queue up indefinitely — meaning the sink-swap
        signal that hands the real audio sink to the worker on
        ``Radio.start()`` never delivers, the worker keeps writing
        to the initial NullSink seed, and the operator gets
        silence.  Latent since Phase 3.B B.5 (sink ownership
        migration); never observed because the QSettings ordering
        bug fixed earlier in Commit 1 prevented worker mode from
        actually running.

        SHELL ONLY in B.1 — ``process_block`` is a no-op stub.
        Subsequent sub-tasks (B.3 onwards) wire up actual DSP."""
        from PySide6.QtCore import QCoreApplication
        self.state_changed.emit("running")
        try:
            while not self._stop_requested:
                # Pump queued slot deliveries from main thread BEFORE
                # processing the next block, so any pending sink-swap
                # / config-update signals take effect on this iteration
                # rather than the next one.  Cheap when no events are
                # queued (returns immediately).
                QCoreApplication.processEvents()
                try:
                    samples = self._input_queue.get(
                        timeout=self.RUN_LOOP_TIMEOUT_S)
                except Empty:
                    # Periodic wake — gives ``request_stop()`` a chance
                    # to exit the loop without indefinite blocking on
                    # an empty queue (which would happen if the radio
                    # stream is paused but the worker thread is
                    # still alive).
                    continue

                # Phase 2 v0.1: try to pair an RX2 batch with this
                # RX1 batch.  Non-blocking -- if the queues fall
                # out of sync briefly (startup, rate change) we
                # fall back to RX1-only processing for one block.
                rx2_samples = None
                try:
                    rx2_samples = self._input_queue_rx2.get_nowait()
                except Empty:
                    pass

                if self._reset_requested:
                    self._reset_requested = False
                    self._reset()

                if self._rx_chan_req is not None:
                    want_on = self._rx_chan_req
                    self._rx_chan_req = None
                    radio = self._radio
                    rx = getattr(radio, "_wdsp_rx", None) if radio else None
                    if rx is not None:
                        try:
                            if want_on:
                                rx.start()
                            else:
                                # Non-blocking stop: clean slew-down
                                # without parking this worker in a
                                # blocking DLL flush at the keydown
                                # instant.  A blocking flush here was
                                # implicated in keydown T/R-relay
                                # chatter (the HL2 gateware is
                                # sensitive to any wire-cadence hiccup
                                # at the MOX edge -- §15.21/§15.26).
                                # The keyup path fully restarts the RX
                                # channel on clean post-T/R IQ, so a
                                # perfectly-flushed stop is not needed.
                                rx.stop(blocking=False)
                        except Exception as exc:
                            print(f"[DspWorker] rx channel "
                                  f"{'start' if want_on else 'stop'} "
                                  f"error: {exc}")

                try:
                    self.process_block(samples, rx2_samples)
                except Exception as exc:
                    # DSP errors must NEVER kill the worker thread.
                    # Log and continue — operator hears a single
                    # block of silence at worst.  Persistent errors
                    # would show up as a torrent of identical log
                    # lines, which is the right diagnostic signal.
                    print(f"[DspWorker] process_block error: {exc}")
        finally:
            self.state_changed.emit("stopped")

    def process_block(
        self,
        samples: np.ndarray,
        rx2_samples: Optional[np.ndarray] = None,
    ) -> None:
        """Run DSP on one block of complex64 IQ samples.

        Phase 2 v0.1 (2026-05-11) signature added ``rx2_samples`` as
        an optional second IQ batch.  When non-None the worker
        invokes ``radio._do_demod_wdsp_dual(rx1, rx2)`` which
        processes BOTH WDSP channels and sums their stereo output
        for the audio sink (RX1 hard-left + RX2 hard-right by
        default via per-channel ``SetRXAPanelPan``).  When None
        (single-channel fallback) the worker invokes the legacy
        ``radio._do_demod_wdsp(rx1)`` path -- preserves v0.0.9.x
        behavior for the rare case where the RX2 queue is empty
        at the moment the RX1 batch is pulled (startup race).

        Originally (Phase 3.B B.3) this mirrored ``Radio._do_demod``
        body running on the worker thread instead of the main thread,
        wrapping the legacy DSP chain (channel.process → AGC → BIN →
        sink).  Phase 3 (v0.0.9.6) deleted that legacy chain and
        Phase 5 finished retiring the channel.process() entry point
        — what remains here is just:

        - Mode dispatch (Off / Tone / regular)
        - LNA peak / RMS tracking on the IQ block (B.6 carryover)
        - ``radio._do_demod_wdsp(samples)`` or _dual — WDSP cffi
          engine (handles decimation + notches + demod + NR + AGC +
          audio internally; result lands in radio._audio_sink
          directly)
        - Sample-ring update + FFT + ``spectrum_ready`` emit (B.8)

        Errors at any stage are logged but never crash the worker
        thread — operator hears a single block of silence at worst,
        and the next block proceeds normally.
        """
        # Heartbeat — increment FIRST so a process_block error
        # later still shows "we tried" to the UI.  If the worker
        # has truly stalled (deadlock, infinite loop), this counter
        # stops; the status-bar readout drops to 0 Hz and the
        # operator sees something is wrong.
        self._blocks_processed += 1
        radio = self._radio
        if radio is None:
            # Not yet attached — nothing to do.  This shouldn't
            # happen in production (Radio attaches after construction)
            # but the guard makes worker-in-isolation tests safer.
            return
        # Mode dispatch — matches Radio._do_demod's first 5 lines.
        # Reads radio._mode directly; Python attribute access is
        # GIL-protected so we get a coherent value (no torn write).
        # Worker may see a slightly stale mode if main thread is
        # mid-set_mode() — at most one block of wrong-mode audio,
        # and Radio.set_mode() also fires reset_requested via signal
        # (B.9) which clears the queue.
        try:
            mode = radio._mode
        except AttributeError:
            return
        if mode == "Off":
            return

        # B.6 — LNA peak / RMS tracking (was on main thread in
        # _on_samples_main_thread).  In worker mode the main-thread
        # path is bypassed (rx-thread routes IQ straight to the
        # worker queue), so the worker has to do this measurement
        # or Auto-LNA goes blind.  Cheap: two scalar reductions
        # over the IQ block.  Result is emitted to the main thread
        # via ``lna_peak_update`` so Radio's existing _lna_peaks /
        # _lna_rms history (read by Auto-LNA + toolbar readout)
        # stays current.
        try:
            if samples.size > 0:
                mag_sq = (samples.real * samples.real
                          + samples.imag * samples.imag)
                peak = float(np.sqrt(np.max(mag_sq)))
                rms = float(np.sqrt(np.mean(mag_sq)))
                self.lna_peak_update.emit(peak, rms)
        except Exception as exc:
            print(f"[DspWorker] lna peak/rms error: {exc}")
            # Never block DSP on a measurement glitch.

        if mode == "Tone":
            # Tone generation lives on Radio (uses radio._tone_phase
            # state).  Worker calls it from worker thread; Radio's
            # _emit_tone is the only writer of _tone_phase, so no
            # race.  Will eventually move into the worker if/when
            # tone testing benefits from threading isolation.
            try:
                radio._emit_tone(len(samples))
            except Exception as exc:
                print(f"[DspWorker] tone error: {exc}")
            return

        # §15.7 sync instrumentation (LYRA_TIMING_DEBUG=1) -- record
        # worker-thread audio + spectrum processing latency per
        # batch.  Cheap when env var is off: one ``getattr`` to
        # ``_timing_stats``, which is None unless gated on.
        _timing = getattr(radio, "_timing_stats", None)
        if _timing is not None:
            import time as _t
            _audio_t0 = _t.monotonic_ns()
        else:
            _audio_t0 = 0

        # WDSP engine path — only DSP path as of Phase 3 (2026-05-08).
        # The DSP heavy work happens inside the WDSP DLL's C thread, so
        # even though we're on the Python worker thread, the GIL is
        # released during fexchange0 and the EP2 writer / sink threads
        # don't compete for it.
        #
        # CRITICAL: run the FFT stage in addition to WDSP demod —
        # the panadapter taps the RAW IQ stream BEFORE any demod, so
        # its plumbing is independent of audio rendering.  Skipping
        # the FFT path here would freeze the spectrum widget.
        if getattr(radio, "_wdsp_rx", None) is not None:
            # Phase 2 v0.1: dual-channel path when an RX2 batch was
            # paired with this RX1 batch in run_loop.  Falls back to
            # the single-channel ``_do_demod_wdsp`` when RX2 queue
            # was empty at pair time (startup race, rate change).
            # Both paths write the final audio to ``radio._audio_sink``
            # internally; the dual path sums RX1+RX2 outputs for the
            # stereo split first.
            #
            # Phase 3.D hotfix v0.1 (2026-05-12): gate the dual path
            # on ``dispatch_state.rx2_enabled``.  ``_wdsp_rx2`` is
            # opened unconditionally at stream start (Phase 2 bench-
            # test legacy), so without this gate the worker would
            # always dump RX2 audio into the right channel even when
            # SUB is off -- operator hears whatever DDC1 happens to
            # be receiving (often a strong broadcaster or RFI) and
            # the Vol-A slider has no effect on it.  See
            # ``CLAUDE.md`` §6.2 / §6.8 SUB semantics.
            # Phase 3.E.1 hotfix v0.2 (2026-05-12): three-way
            # dispatch based on (rx2_enabled, focused_rx):
            #   SUB on              -> dual demod (L=RX1, R=RX2)
            #   SUB off, focus RX1  -> RX1 mono-center (legacy)
            #   SUB off, focus RX2  -> RX2 mono-center (NEW)
            # Operator UX (Rick 2026-05-12): "if SUB is off and I
            # click RX2 I expect to HEAR RX2, not still hear RX1."
            # RX2's pan was set to 0.5 (center) by
            # ``_apply_rx2_routing`` when SUB went off, so RX2's
            # WDSP output is already mono-on-stereo -- the
            # ``_do_demod_wdsp_rx2_only`` path just hands it
            # through the same output stage RX1 uses.
            try:
                state = radio.snapshot_dispatch_state()
                focused = int(getattr(radio, "focused_rx", 0))
                rx2_chan_ready = (
                    rx2_samples is not None
                    and getattr(radio, "_wdsp_rx2", None) is not None)
                if state.rx2_enabled and rx2_chan_ready:
                    radio._do_demod_wdsp_dual(samples, rx2_samples)
                elif focused == 2 and rx2_chan_ready:
                    radio._do_demod_wdsp_rx2_only(rx2_samples)
                else:
                    radio._do_demod_wdsp(samples)
            except Exception as exc:
                print(f"[DspWorker] WDSP demod error: {exc}")
            # §15.7 timing -- record audio path total (worker enter
            # to sink write returned).  Does NOT include sink-internal
            # buffering (HL2 gateware FIFO 40 ms, PC Soundcard rmatch
            # 200 ms) -- those are constants documented in §15.7.
            if _timing is not None and _audio_t0 > 0:
                import time as _t
                _timing.record(
                    "audio_worker_ms",
                    _t.monotonic_ns() - _audio_t0)
                # Queue depth context updated per record so the
                # next flush includes the latest snapshot.
                try:
                    _timing.set_context(
                        "q_rx1", self._input_queue.qsize())
                    _timing.set_context(
                        "q_rx2", self._input_queue_rx2.qsize())
                except Exception:
                    pass
        # FFT cadence (B.8): append IQ to the worker-owned sample
        # ring; every N blocks compute one FFT and emit raw spec_db.
        # Errors NEVER stop audio — at worst the panadapter freezes
        # for a frame.
        #
        # Phase 3.E.1 v0.1 (2026-05-12): pick samples based on
        # ``radio.panadapter_source_rx`` so the FFT follows whatever
        # VFO the operator focused.  Falls back to RX1 samples when
        # source = RX2 but rx2_samples aren't paired this batch
        # (startup race, rate-change transient) -- one stale frame
        # is better than a frozen panadapter.
        try:
            src = getattr(radio, "panadapter_source_rx", 0)
            if (src == 2 and rx2_samples is not None
                    and getattr(radio, "_wdsp_rx2", None) is not None):
                self._maybe_run_fft(rx2_samples)
            else:
                self._maybe_run_fft(samples)
        except Exception as exc:
            print(f"[DspWorker] fft error: {exc}")
        # §15.7 timing -- record total worker block time (audio +
        # FFT cadence + emit).  Difference (fft_worker_ms minus
        # audio_worker_ms) shows the FFT slice's contribution; note
        # FFTs only emit on cadence ticks (~once every 2 batches at
        # 192k) so this metric's ``n`` will be less than
        # ``audio_worker_ms``'s ``n``.
        if _timing is not None and _audio_t0 > 0:
            import time as _t
            _timing.record(
                "fft_worker_ms", _t.monotonic_ns() - _audio_t0)

    def _maybe_run_fft(self, samples: np.ndarray) -> None:
        """Append IQ to the worker-owned sample ring; if the FFT
        cadence threshold is crossed, compute one FFT and emit the
        raw spec_db via ``spectrum_raw_ready`` (B.8).

        Cadence math
        ------------
        The single-thread path uses a wall-clock QTimer firing every
        ``radio._fft_interval_ms``.  In worker mode we instead count
        IQ blocks and fire when the elapsed-block count corresponds
        to that same interval at the current sample rate:

            blocks_per_fft = rate * interval_ms / (batch_size * 1000)

        At 96k IQ + 2048 batch + 25 ms interval (40 fps) that's ~1
        block per FFT (every batch).  At 384k IQ + 2048 batch + 25
        ms that's ~5 blocks per FFT.  ``max(1, ...)`` guards against
        divide-by-zero / corner cases where rate or interval are
        unset.

        Re-evaluated every block so operator FPS / rate changes
        take effect on the next block boundary — same UX as the
        wall-clock timer.
        """
        radio = self._radio
        if radio is None:
            return
        # Lazy-init the ring once we know radio._fft_size.
        if self._sample_ring is None:
            try:
                fft_size = int(radio._fft_size)
            except (AttributeError, TypeError, ValueError):
                return
            self._sample_ring = deque(maxlen=fft_size * 4)
        # Append current IQ batch.  Iterates over ndarray — cheap
        # for a 2048-sample batch (batch is much smaller than the
        # ring's capacity).  Storing complex64 elements; deque is
        # GIL-protected so worker-thread reads/writes are coherent
        # without an explicit lock (no other thread touches it).
        self._sample_ring.extend(samples)

        # Cadence check.
        self._fft_block_counter += 1
        try:
            rate = int(radio._rate)
            interval_ms = int(radio._fft_interval_ms)
            batch_size = int(radio._rx_batch_size)
        except (AttributeError, TypeError, ValueError):
            return
        if rate <= 0 or interval_ms <= 0 or batch_size <= 0:
            return
        blocks_per_fft = (rate * interval_ms) / (batch_size * 1000.0)
        threshold = max(1, int(round(blocks_per_fft)))
        if self._fft_block_counter < threshold:
            return
        self._fft_block_counter = 0

        # FFT body — mirrors Radio._compute_spec_db.  Reads window /
        # win_norm / spectrum_cal_db / fft_size from radio directly;
        # those are set once at radio __init__ and never mutated, so
        # cross-thread reads are safe (no lock, no race).
        try:
            fft_size = int(radio._fft_size)
            window = radio._window
            win_norm = float(radio._win_norm)
            cal_db = float(radio._spectrum_cal_db)
        except (AttributeError, TypeError, ValueError):
            return
        if len(self._sample_ring) < fft_size:
            return
        arr = np.fromiter(self._sample_ring, dtype=np.complex64,
                          count=len(self._sample_ring))
        seg = arr[-fft_size:] * window
        f = np.fft.fftshift(np.fft.fft(seg))
        # HL2 baseband is spectrum-mirrored relative to sky frequency
        # (see _compute_spec_db on Radio for the full rationale).
        f = f[::-1]
        spec_db = (10.0 * np.log10((np.abs(f) ** 2) / win_norm + 1e-20)
                   + cal_db)
        self.spectrum_raw_ready.emit(spec_db)

    def _reset(self) -> None:
        """Flush in-flight DSP state.  Triggered by
        ``request_reset()`` from the main thread on freq, mode,
        rate change, or sink swap.

        Runs on the WORKER thread between blocks (driven by the
        ``_reset_requested`` flag check in ``run_loop``), so it
        can safely mutate channel / binaural / AGC state that
        ``process_block`` also touches — Qt's run-loop ordering
        guarantees these don't interleave.

        Resets cover:
        - Worker-internal: input queue, sample ring, FFT counter
        - Radio-side DSP state (called via back-reference): rx
          channel, binaural, AGC peak / hang counter, S-meter
          running average

        Operator-side reset of UI / hardware state (notch rebuild,
        waterfall counter, OC pattern) stays on the main thread —
        it's not in the worker's audio path.
        """
        # Drain queued IQ so we don't process stale-mode samples
        # against new-mode state.
        while True:
            try:
                self._input_queue.get_nowait()
            except Empty:
                break
        # B.8 — clear sample ring + reset FFT cadence so the next
        # FFT after a freq/rate/mode change is built from fresh
        # post-reset samples.
        if self._sample_ring is not None:
            self._sample_ring.clear()
        self._fft_block_counter = 0
        # B.9 — reset Radio-owned DSP state via the back-reference.
        # These are the same calls the single-thread path makes from
        # main thread; in worker mode they run here, between blocks,
        # so process_block never sees a half-reset channel / binaural
        # / AGC.
        radio = self._radio
        if radio is None:
            return
        try:
            radio._rx_channel.reset()
        except Exception as exc:
            print(f"[DspWorker] channel reset error: {exc}")
        try:
            radio._binaural.reset()
        except Exception as exc:
            print(f"[DspWorker] binaural reset error: {exc}")
        # S-meter reset.  AGC state lives inside the WDSP DLL and
        # gets reset by _wdsp_rx.reset() on the main path; the
        # legacy Python WdspAgc wrapper that also needed flushing
        # here was deleted Phase 6.A.  S-meter is a plain attribute
        # on radio (GIL-safe direct write).
        try:
            radio._smeter_avg_lin = 0.0
        except AttributeError as exc:
            print(f"[DspWorker] smeter reset error: {exc}")
