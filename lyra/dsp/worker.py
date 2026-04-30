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
        # Lifecycle flags — set by request_*() and read by run_loop.
        # Plain attribute reads/writes; no locks needed because the
        # Python GIL serializes single-attribute access and we don't
        # need atomic compound updates.
        self._stop_requested: bool = False
        self._reset_requested: bool = False
        # Phase 3.B B.3 — back-reference to Radio for the audio chain.
        # The worker calls radio._rx_channel.process(), radio._apply_
        # agc_and_volume(), radio._binaural.process() from worker thread
        # when worker mode is active.  Future sub-tasks (B.6 / B.8 / B.9)
        # progressively migrate the ownership of LNA peak tracking,
        # FFT, and reset state from Radio to the worker.  Wired by
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

        Phase 3.B B.3 — mirrors ``Radio._do_demod`` body, running on
        the worker thread instead of the main thread.  Calls Radio's
        existing DSP machinery (channel, AGC, BIN, sink) via the
        back-reference set by ``attach_to_radio()``.

        Future sub-tasks migrate ownership of these objects directly
        into the worker; B.3 only shifts WHERE the calls happen, not
        WHERE the state lives.

        What this commit (B.3) covers:

        - Mode dispatch (Off / Tone / regular)
        - Notch state push to channel (matches single-thread cadence)
        - ``rx_channel.process(iq)`` — full RX channel pipeline
          (decim → notches → demod → NR → APF → audio)
        - AGC + AF Gain + Volume + tanh limiter (via
          ``radio._apply_agc_and_volume``)
        - BIN — Hilbert phase split (via ``radio._binaural.process``)
        - Audio sink write (via ``radio._audio_sink.write``)

        What's NOT in B.3 (covered later):

        - LNA peak / RMS tracking + ``lna_peak_update`` emit (B.6)
        - Sample-ring update + FFT + ``spectrum_ready`` emit (B.8)
        - S-meter linear-power averaging + ``smeter_reading`` (B.4/5)
        - Reset/flush via ``request_reset()`` (B.9)

        Errors at any stage are logged but never crash the worker
        thread — operator hears a single block of silence at worst,
        and the next block proceeds normally.
        """
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

        # Push current notch state to the channel each block —
        # matches the cadence Radio._do_demod uses.  Cheap (just
        # stores references); ensures channel sees fresh state
        # without us tracking 8+ call sites.
        try:
            radio._rx_channel.set_notches(
                radio._notches, radio._notch_enabled)
        except Exception as exc:
            print(f"[DspWorker] notch update error: {exc}")
            # Continue — old notch state is fine for one block.

        # Stage 1 — channel runs decim → notch → demod → NR → APF
        try:
            audio = radio._rx_channel.process(samples)
        except Exception as exc:
            print(f"[DspWorker] channel.process error: {exc}")
            return
        if audio.size == 0:
            # No complete demod block ready yet (channel buffers
            # partial blocks across calls).  Next call may produce.
            return

        # Stage 2 — AGC + AF Gain + Volume + tanh limiter
        try:
            audio = radio._apply_agc_and_volume(audio)
        except Exception as exc:
            print(f"[DspWorker] agc/volume error: {exc}")
            return

        # Stage 3 — BIN (Hilbert phase split for headphone listening).
        # No-op pass-through when bin_enabled == False, returns
        # (N, 2) stereo when active.  Both audio sinks accept either
        # mono or stereo input.
        try:
            audio = radio._binaural.process(audio)
        except Exception as exc:
            print(f"[DspWorker] binaural error: {exc}")
            # Continue with whatever audio we had — better than
            # silence.

        # Stage 4 — write to audio sink (AK4951 or PC Soundcard).
        # B.5: use the worker's OWN sink reference, kept in sync with
        # Radio's via ``worker_audio_sink_changed``.  Falls back to
        # Radio's reference for any narrow window before the first
        # signal lands (defensive — in practice Radio seeds the
        # reference before moveToThread, so it's never None here).
        sink = self._audio_sink if self._audio_sink is not None \
            else radio._audio_sink
        try:
            sink.write(audio)
        except Exception as exc:
            print(f"[DspWorker] sink write error: {exc}")
            # Continue; next block may succeed.

        # Stage 5 — FFT cadence (B.8).  Append IQ to worker-owned
        # sample ring; every N blocks (where N tracks the operator's
        # FPS preference + sample rate), run the FFT and emit the
        # raw spectrum to Radio's main-thread post-processing slot.
        # Errors here NEVER stop audio; they'd just freeze the
        # panadapter for a frame.
        try:
            self._maybe_run_fft(samples)
        except Exception as exc:
            print(f"[DspWorker] fft error: {exc}")

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
        # AGC + S-meter are plain Python attributes on radio; direct
        # writes are GIL-safe and the consumer (_apply_agc_and_volume)
        # reads them inside process_block on this same worker thread,
        # so the writes here are serialized w.r.t. the next block.
        try:
            radio._agc_peak = 1e-4
            radio._agc_hang_counter = 0
            radio._smeter_avg_lin = 0.0
        except AttributeError as exc:
            print(f"[DspWorker] AGC/smeter reset error: {exc}")
