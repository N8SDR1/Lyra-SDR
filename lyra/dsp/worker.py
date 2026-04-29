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

    spectrum_ready = Signal(object, float, int)
    """spec_db (np.ndarray), center_hz (float), rate (int).

    Same shape as Radio.spectrum_ready so existing UI slots wire up
    1:1 once Phase 3.B B.8 migrates FFT into the worker."""

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
        # Lifecycle flags — set by request_*() and read by run_loop.
        # Plain attribute reads/writes; no locks needed because the
        # Python GIL serializes single-attribute access and we don't
        # need atomic compound updates.
        self._stop_requested: bool = False
        self._reset_requested: bool = False

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

    def request_reset(self) -> None:
        """Request the worker to flush its DSP state on the next
        block boundary.  Called from the main thread on freq, mode,
        or rate change — any operator action that introduces a
        legitimate audio discontinuity."""
        self._reset_requested = True

    def request_stop(self) -> None:
        """Request the worker to exit ``run_loop`` cleanly.  Called
        from the main thread on radio stop or Lyra shutdown."""
        self._stop_requested = True

    @property
    def config(self) -> WorkerConfig:
        """Read-only view of the current worker config.  Useful for
        diagnostics / Settings display.  Not for cross-thread
        mutation — main thread should fire setter signals to update
        the worker's config rather than poking the dataclass."""
        return self._config

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

    # ── Run loop (worker thread) ───────────────────────────────

    @Slot()
    def run_loop(self) -> None:
        """Worker-thread main loop.  Called once via ``thread.started``
        when the operator activates worker mode.

        Blocks briefly on the input queue, then calls ``process_block``
        on each batch.  Exits when ``request_stop()`` is set.

        SHELL ONLY in B.1 — ``process_block`` is a no-op stub.
        Subsequent sub-tasks (B.3 onwards) wire up actual DSP."""
        self.state_changed.emit("running")
        try:
            while not self._stop_requested:
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

                if self._reset_requested:
                    self._reset_requested = False
                    self._reset()

                try:
                    self.process_block(samples)
                except Exception as exc:
                    # DSP errors must NEVER kill the worker thread.
                    # Log and continue — operator hears a single
                    # block of silence at worst.  Persistent errors
                    # would show up as a torrent of identical log
                    # lines, which is the right diagnostic signal.
                    print(f"[DspWorker] process_block error: {exc}")
        finally:
            self.state_changed.emit("stopped")

    def process_block(self, samples: np.ndarray) -> None:
        """Run DSP on one block of complex64 IQ samples.

        SHELL — does nothing in B.1.  Subsequent sub-tasks fill
        this in with the audio chain that currently lives in
        ``Radio._do_demod`` and ``Radio._on_samples_main_thread``.

        Final shape (per ``threading.md`` §3) will be:

        1.  LNA peak / RMS tracking → emit ``lna_peak_update``
            (B.6)
        2.  ``rx_channel.process(iq)`` runs the existing chain:
            decim → notches → demod → NR → APF → audio (B.3)
        3.  AGC + AF gain + Volume + tanh limiter (B.4)
        4.  ``binaural.process(audio)`` for BIN, when enabled (B.5)
        5.  ``audio_sink.write(audio)`` to AK4951 or
            SoundDeviceSink (B.5)
        6.  Sample-ring update + periodic FFT emit
            ``spectrum_ready`` (B.8)
        7.  S-meter linear power running average — emit
            ``smeter_reading`` at meter cadence (B.4 / B.5)
        """
        # Stub — no behavior yet. Each B.x sub-task adds one stage.
        pass

    def _reset(self) -> None:
        """Flush in-flight DSP state.  Triggered by
        ``request_reset()`` from the main thread on freq, mode, or
        rate change.

        SHELL — only drains the input queue in B.1.  Subsequent
        sub-tasks add:

        - ``rx_channel.reset()``  (B.3)
        - AGC peak / hang counter zero  (B.4)
        - Binaural FIR state + delay buffer reset  (B.5)
        - Sample-ring clear  (B.8)
        """
        # Drain queued IQ so we don't process stale-mode samples
        # against new-mode state.  Subsequent sub-tasks reset the
        # downstream DSP objects too.
        while True:
            try:
                self._input_queue.get_nowait()
            except Empty:
                break
