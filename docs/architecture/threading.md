# Lyra Threading Architecture

**Status:** DESIGN DOC — Phase 3.A. No code lands until this is reviewed
and accepted. Fold operator feedback in here, then drive Phase 3.B
from this document.

**Author:** N8SDR + Claude
**Date:** 2026-04-29
**Target version:** v0.0.6 — "Threaded DSP" (or whatever fits the
release-name pattern at the time)

---

## 1. Why we're doing this now

After v0.0.5 ("Listening Tools"), the audio chain has 5 stages
(decim → notch → demod → NR → APF) plus AGC, AF/Vol, BIN. Coming up
in v0.0.6+:

- NR2 (minimum-statistics noise estimator) — heavier than current NR
- Captured-noise-profile NR — operator-recorded reference, larger FFTs
- ANF (LMS adaptive notch)
- NB (impulse blanker, IQ-domain — runs at IQ rate, not audio rate)
- Eventually: neural NR (RNNoise / DeepFilterNet)
- Eventually: TX path running concurrently with RX
- Eventually: RX2 second receiver

All of those add main-thread-bound DSP cost on a thread that's
already doing UI rendering, GL paint, FFT, and operator interaction.
Refactoring DSP into a worker thread is **easier with what we have
today** (5 stages) than it'll be later (8+ stages). Doing it now
also means the noise-toolkit features land on the new threading
from day one — no re-architecture later.

The reasoning is the same as the GPU/QPainter panadapter split:
separate the heavy work, give it the right execution context, ship
it. We've done that pattern once; we know it works.

## 2. Current architecture (as of v0.0.5)

```
┌─────────────────────┐
│   HPSDR rx thread   │   (lyra/protocol/stream.py:_rx_loop)
│   - recv UDP        │
│   - parse P1 frames │
│   - C&C round-robin │
│   - emit IQ samples │
└──────────┬──────────┘
           │ Radio._stream_cb (still on rx thread)
           │ → batch into _rx_batch under _rx_batch_lock
           │ → when batch full, _bridge.samples_ready.emit(batch)
           │
           │ (Qt::QueuedConnection — signal hops to main thread)
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│                      Qt main thread                           │
│                                                                │
│  Radio._on_samples_main_thread(samples):                       │
│    - sample_ring.extend(samples) under _ring_lock              │
│    - LNA peak/RMS tracking (_lna_peaks, _lna_rms)              │
│    - _do_demod(samples):                                        │
│        - _rx_channel.process(iq)                                │
│            decim → notch → demod → NR → APF → audio            │
│        - _apply_agc_and_volume(audio)                           │
│            AF gain → AGC → Volume → tanh                       │
│        - _binaural.process(audio)                               │
│            mono → stereo (when active)                          │
│        - _audio_sink.write(audio)                               │
│                                                                │
│  Radio._tick_fft (QTimer @ 30 Hz):                              │
│    - read _sample_ring under _ring_lock                         │
│    - apply window, FFT, abs, dB, calibration                    │
│    - emit spectrum_ready                                        │
│                                                                │
│  All UI:                                                        │
│    - paintGL / paintEvent                                       │
│    - QPainter overlays                                          │
│    - signal handlers from Settings, panels, etc.                │
│    - QTimer callbacks for AGC auto, LNA auto, telemetry         │
└──────────────────────────────────────────────────────────────┘

           │  audio  via _audio_sink.write
           ▼
┌─────────────────────┐
│   Audio sink        │
│   - AK4951: queue   │
│     into TX EP2     │
│     (consumed by    │
│     stream tx       │
│     thread)         │
│   - SoundDeviceSink:│
│     write to        │
│     sounddevice's   │
│     OutputStream    │
└─────────────────────┘
```

Plus three short-lived/auxiliary threads (no impact on DSP):
- `_DiscoveryWorker` (QThread) — auto-discovery probe
- `_ReleaseFetchWorker` + `SilentUpdateChecker` (QThread) — GitHub
  releases API check
- HL2Stream's TX thread for queued EP2 audio frames

## 3. Target architecture (Phase 3.B)

```
┌─────────────────────┐
│   HPSDR rx thread   │   (unchanged — lyra/protocol/stream.py)
│   - recv UDP        │
│   - parse P1 frames │
│   - emit IQ samples │
└──────────┬──────────┘
           │ Radio._stream_cb (rx thread)
           │ → push IQ batch onto worker's input queue
           │   (drop-oldest if queue full)
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│                    DSP worker thread                          │
│                    (lyra/dsp/worker.py — NEW)                  │
│                                                                │
│  while running:                                                │
│    iq = input_queue.get()                                      │
│    config = self._snapshot_config()  — atomic state read       │
│                                                                │
│    DspWorker.process_block(iq, config):                        │
│      - LNA peak/RMS tracking (lna_state.update)                │
│      - sample_ring.extend(iq)                                  │
│      - rx_channel.process(iq)                                  │
│          decim → notch → demod → NR → APF                      │
│      - _apply_agc_and_volume(audio)                            │
│      - binaural.process(audio)                                 │
│      - audio_sink.write(audio)                                 │
│                                                                │
│  every N blocks (FFT cadence):                                 │
│    - extract latest fft_size from sample_ring                  │
│    - window, FFT, abs, dB, cal                                 │
│    - emit spectrum_ready (cross-thread to main)                │
│                                                                │
│  every M blocks (meter cadence):                               │
│    - emit smeter_reading, lna_peak_reading (cross-thread)      │
└──────────────────────────────────────────────────────────────┘

           │ all worker→main communication via Qt signals
           │ (Qt::QueuedConnection — main pulls from event loop)
           ▼
┌──────────────────────────────────────────────────────────────┐
│                      Qt main thread                           │
│                                                                │
│  - paintGL / paintEvent                                        │
│  - QPainter overlays                                           │
│  - Settings dialog, panels, all UI                             │
│  - QTimer callbacks (AGC auto, LNA auto, telemetry)            │
│  - Slot for spectrum_ready → render                            │
│  - Slot for smeter_reading → meter widget                      │
│                                                                │
│  Operator-driven setters (set_freq_hz, set_mode,               │
│  set_apf_enabled, etc.) emit `config_changed` signals          │
│  that worker's slot updates its local config copy.             │
└──────────────────────────────────────────────────────────────┘
```

## 4. Thread responsibilities (worker mode active)

When operator opts into the worker-thread backend (Settings →
DSP → Threading: Worker (BETA)):

| Thread | Owns | Reads (cross-thread) | Writes (cross-thread) |
|---|---|---|---|
| **HPSDR rx** | UDP socket, frame parser, C&C registers, TX audio queue | none | IQ samples → worker queue (worker mode) or _bridge signal (single mode) |
| **DSP worker** | rx_channel, audio_sink (when in worker mode), AGC state, BIN state, sample_ring, FFT context, LNA peak history | own copy of WorkerConfig (AGC + AF/Vol/BIN + Muted) — updated via slots | spectrum data, S-meter, LNA peak updates → main |
| **Qt main** | All UI state, panels, settings, operator preferences, band memory, snapshots, single-thread DSP path | meter readings, spectrum data | config changes → worker (queued slots), freq/mode → stream |
| **PortAudio cb** | OutputStream's internal buffer | audio frames the writer thread wrote | (none — feeds OS audio) |

When operator stays on the single-thread default: the DSP worker
runs but stays idle; all DSP happens on the Qt main thread as
today. No behavior change vs v0.0.5.

## 5. State ownership table

Every piece of mutable state in Radio + channel + sink, with its
post-Phase-3 home.

### State that moves to DSP worker (worker reads + writes)

| State | Type | Notes |
|---|---|---|
| `_rx_channel` | `PythonRxChannel` | Already self-contained DSP state (decimator, demods, NR processor, APF biquad, audio buf, channel notches list) |
| `_agc_peak`, `_agc_hang_counter` | float, int | AGC envelope tracker — modified every block |
| `_smeter_avg_lin` | float | Linear-power running average — modified every block |
| `_lna_peaks`, `_lna_rms` | list[float] | Per-block IQ peak/RMS history (worker fills, main reads via signal for Auto-LNA logic) |
| `_lna_current_peak_dbfs` | float | Latest peak — emitted to UI via signal |
| `_lna_passband_peak_dbfs` | float \| None | Passband-peak measurement — used by Auto-LNA pull-up gate |
| `_noise_baseline`, `_noise_history` | float, list | Auto-AGC noise floor tracker |
| `_binaural` | `BinauralFilter` | Hilbert FIR zi, delay buffer — modified every audio block |
| `_audio_sink` | `AudioSink` | Writes happen on worker thread |
| `_sample_ring` | deque (now mutex-protected) | Becomes worker-internal — main no longer reads it directly. FFT moves into worker. |
| `_audio_buf` (inside channel) | list | Already worker-owned via channel encapsulation |
| `_tone_phase` | float | Mode=Tone phase tracker |

### State that stays on main thread (operator-driven config)

| State | Type | Notes |
|---|---|---|
| `_freq_hz` | int | Set via operator; pushed to stream + emitted to worker |
| `_mode` | str | Same |
| `_rate` | int | Same |
| `_gain_db` | int | LNA gain — pushed to stream + worker |
| `_af_gain_db` | int | Audio chain config |
| `_volume`, `_balance`, `_muted` | float, float, bool | Same |
| `_agc_profile`, `_agc_release`, `_agc_target`, `_agc_hang_blocks` | str/float/float/int | AGC config — pushed to worker on change |
| `_apf_enabled`, `_apf_bw_hz`, `_apf_gain_db` | bool/int/float | Pushed to worker on change |
| `_bin_enabled`, `_bin_depth` | bool/float | Pushed to worker on change |
| `_nr_profile`, `_nr_enabled` (via channel) | str/bool | Pushed to worker on change |
| `_notches`, `_notch_enabled` | list/bool | Pushed to worker on change |
| `_lna_auto`, `_lna_auto_pullup`, all LNA auto config | mixed | Auto-LNA logic stays on main, **reads** LNA peak history from worker via signal |
| `_band_plan_*`, `_peak_markers_*`, `_show_*`, `_segment_colors` | mixed | Pure UI/visualization state — never touched by DSP worker |
| `_band_memory`, `_oc_preset`, `_usb_bcd_*` | various | Operator preferences + external hardware state |
| All of `_spectrum_*`, `_waterfall_*`, color overrides | various | Visuals config |

### Cross-thread shared state (read-only on consumer side)

| State | Producer | Consumer | Sync mechanism |
|---|---|---|---|
| Operator config (mode/freq/AGC/etc.) | Main thread (operator setter) | DSP worker | Qt signal `config_changed` with payload, slot updates worker's local copy |
| Spectrum FFT result | DSP worker | Main thread | Qt signal `spectrum_ready` (existing) |
| S-meter linear power | DSP worker | Main thread | Qt signal `peak_changed` (existing) |
| LNA peak history | DSP worker | Main thread Auto-LNA | Qt signal `lna_peak_update`; main thread copies into Auto-LNA's local buffer |
| Notch state | Main thread | DSP worker | Qt signal `notches_changed` with snapshot list |

**No raw locks.** All cross-thread sharing is via Qt's signal/slot
machinery with `QueuedConnection`. The only existing locks
(`_rx_batch_lock`, `_ring_lock`) get evaluated for removal in
Phase 3.B.

## 6. Config replication via Qt slots (no atomic snapshots needed)

To avoid mid-block parameter tearing (e.g., demod mid-block while
mode flips from USB to LSB), we rely on Qt's event loop ordering
rather than locks or per-block snapshots:

- The worker holds a small `_config: WorkerConfig` dataclass for
  the few parameters it owns directly (AGC, AF/Vol, BIN).
- Operator-driven setters on Radio (main thread) emit
  `*_changed` signals.
- The worker has slots wired to those signals via
  `QueuedConnection`, so each slot runs on the **worker** thread
  between `process_block` calls — never during one.
- Each `process_block` reads `self._config` directly. No snapshot,
  no lock — the slot calls and the block calls interleave at the
  Qt event loop's natural boundaries.

This works because Python's GIL plus Qt's queued slot dispatch
guarantee that a slot won't preempt the middle of `process_block`.
The worker's event loop drains pending slots before pulling the
next IQ block off the input queue.

### What the worker actually owns directly

Most operator-facing config flows **through** the channel via its
existing `set_*` methods. The channel encapsulates mode, sample
rate, RX BW, CW pitch, notches, NR, and APF state. The worker
just needs to call `_rx_channel.set_mode(new_mode)` etc. when the
operator changes them — no separate WorkerConfig field for those.

Only the items NOT inside the channel need worker-side mirrors:

```python
@dataclass
class WorkerConfig:
    # AGC envelope tracker config (mutable state lives here too)
    agc_profile: str           # off / fast / med / slow / custom
    agc_release: float
    agc_target: float
    agc_hang_blocks: int
    # Audio chain post-AGC
    af_gain_db: int            # 0..+80 dB
    volume: float              # 0.0..1.0
    muted: bool
    # BIN — Hilbert phase split (CW + headphone widening)
    bin_enabled: bool
    bin_depth: float           # 0.0..1.0
```

Mutable per-block state owned by worker but **not** in WorkerConfig:
- `_agc_peak`, `_agc_hang_counter` (AGC envelope tracker)
- `_smeter_avg_lin` (S-meter linear running average)
- `_lna_peaks`, `_lna_rms` (rolling history for Auto-LNA)
- `_noise_baseline`, `_noise_history` (Auto-AGC noise floor)
- `_binaural` (the `BinauralFilter` instance with FIR zi + delay buf)
- `_rx_channel` (the channel itself, with all its internal state)

### Example update flow

```python
# In Radio.set_apf_enabled (main thread):
def set_apf_enabled(self, on):
    self._apf_enabled = bool(on)
    # APF lives in the channel — emit signal that channel's slot
    # (running on worker thread) picks up
    self.apf_enabled_changed.emit(bool(on))

# Worker subscribes to the signal once at construction:
self._radio.apf_enabled_changed.connect(
    self._on_apf_enabled, Qt.QueuedConnection)

# Worker's slot — runs on worker thread between process_block calls:
def _on_apf_enabled(self, on):
    # No WorkerConfig field for APF; flows straight into channel
    self._rx_channel.set_apf_enabled(on)
```

For AGC change (the worker DOES hold AGC config in `_config`):

```python
# Main thread:
def set_agc_profile(self, name):
    # update Radio's local copy (for UI display)
    self._agc_profile = name
    # tell worker about the new profile
    self.agc_profile_changed.emit(name)

# Worker slot:
def _on_agc_profile(self, name):
    self._config.agc_profile = name
    # also recompute the derived release/target/hang from preset table
    preset = AGC_PRESETS[name]
    self._config.agc_release = preset["release"]
    self._config.agc_hang_blocks = preset["hang_blocks"]
    # ...
```

### Two important properties of QueuedConnection

1. The slot runs on the **receiver's** thread (worker), not the
   emitter's (main). No raw threading needed.
2. Events queue in order — if main emits `set_freq` then `set_mode`,
   the worker processes them in that order, never interleaved with
   the body of a `process_block` call.

## 7. Reset / flush sequencing

These are the operations where DSP state must be cleared cleanly.
Already handled today by `Radio.reset()` calling
`_rx_channel.reset()`. In the threaded model, Reset becomes a
worker operation:

```python
# Main thread:
def reset(self):
    # Drain the input queue so the worker doesn't process stale IQ
    # against new mode state. Push a sentinel that the worker
    # interprets as "stop, flush, reset, then resume".
    self._worker.request_reset()
    self._agc_peak = 1e-4   # main-thread state, reset directly
    self._smeter_avg_lin = 0.0
    self._binaural.reset()  # if main owned it; in Phase 3 this moves
                            #  to worker too
```

The worker's reset op (running on worker thread):
```python
def _on_reset_requested(self):
    self._input_queue.clear()
    self._rx_channel.reset()
    self._agc_peak = 1e-4
    self._sample_ring.clear()
    # ... etc.
```

Reset is invoked on:
- Frequency change (`set_freq_hz`)
- Mode change (`set_mode`)
- Sample-rate change (`set_rate`)
- Stream restart
- Sink swap

Each of these is a legitimate audio discontinuity, so the user
already expects a momentary silence — clearing buffers is safe.

## 8. Bounded queues + drop-oldest

The worker's input queue has a bounded depth so a slow worker
doesn't grow unbounded memory. If the worker can't keep up:

- **Drop-oldest** — when the queue is full and a new IQ batch
  arrives from the rx thread, evict the oldest queued batch and
  push the new one. Operator hears a tiny audio dropout, but the
  app doesn't run out of memory.
- **Log on drop** — print a warning so we can see drops in tester
  reports.

Choice rationale: dropping old samples is better than dropping new
ones (audio stays current; no growing latency).

Queue depth: ~10 batches at the current 2048-sample batch size =
~1 second of buffered IQ at 48k. Plenty for transient stalls,
small enough that catastrophic worker stalls produce a noticeable
dropout (which is what we want — silent slowdown is worse).

## 9. FFT migration

FFT currently runs on the main thread via `_fft_timer` reading
`_sample_ring`. In Phase 3.B:

- The sample ring becomes worker-internal (no cross-thread access)
- FFT runs **on the worker thread**, gated by a tick counter inside
  `process_block`:
  ```python
  self._fft_tick_counter += 1
  if self._fft_tick_counter >= self._fft_tick_threshold:
      self._fft_tick_counter = 0
      spec_db = self._compute_fft()
      self.spectrum_ready.emit(spec_db, freq, rate)  # to main
  ```
- The QTimer on main thread is removed; cadence is sample-driven
  not wall-clock. Slight UX improvement: FFT updates correlate with
  data flow rather than wall clock, so on rate changes the
  panadapter responds immediately rather than waiting for the next
  timer fire.

## 10. AK4951 sink — gating thread-safety audit

`AK4951Sink.write()` queues stereo samples into the HL2Stream's TX
audio queue (`stream.queue_tx_audio()`). The HL2Stream's TX thread
then drains this queue into EP2 frames sent to the radio.

In Phase 3:
- DSP worker calls `audio_sink.write(audio)` from the **worker
  thread** (instead of the current main thread)
- `audio_sink` = `AK4951Sink`, internally calls
  `stream.queue_tx_audio(stereo)`
- HL2Stream's TX thread is the consumer — same as today

**This crosses an additional thread boundary that didn't exist
before.** Today: main → tx thread (1 boundary). Phase 3: worker →
tx thread (still 1 boundary, but a different producer thread).
The audit must confirm:

1. `stream.queue_tx_audio()` uses thread-safe operations end to
   end (deque.append is GIL-protected, but length checks +
   conditional drops need explicit locks).
2. `stream.clear_tx_audio()` (called on sink swap and shutdown)
   doesn't race with concurrent writes from the worker.
3. The `inject_audio_tx` flag (set/cleared on sink lifecycle) is
   ordered correctly w.r.t. queued samples.

**Status: BLOCKING for Phase 3.B activation.** Sub-task B.7
explicitly audits this. If unsafe operations are found, they must
be wrapped in locks BEFORE the worker thread starts driving the
sink. Audio corruption from this kind of race is the worst-case
failure mode — operator hears garbled / digitized audio with no
obvious cause.

## 11. SoundDeviceSink consideration

`SoundDeviceSink.write()` calls `OutputStream.write(stereo)`
(blocking write — PortAudio buffers internally and the OS audio
callback drains).

The thread-safety constraint is **single-producer**, not "any
thread can call write." PortAudio supports a single producer
thread driving the stream; concurrent writes from multiple threads
are undefined behavior.

What this means for Phase 3:

- **Today:** stream is created on the main thread (in
  `Radio.__init__` when `set_audio_output("PC Soundcard")` runs)
  and `write()` is called from the main thread. ✓ Single producer.
- **Phase 3:** stream is created on the main thread but `write()`
  must come exclusively from the **worker** thread.
- **Hand-off** — the OutputStream object is fine to use from a
  thread that didn't create it, as long as creation finishes
  before the first write. Lifecycle:
  ```
  main creates OutputStream → main hands reference to worker →
  worker calls write() exclusively → close() can come from either
  ```

**Sink lifecycle in Phase 3:**

1. Operator changes audio output (Settings → Audio → Output device)
   on the main thread
2. Main thread creates the new sink, hands it to the worker via
   a Qt signal carrying the sink object as payload
3. Worker's slot replaces its current sink reference
4. Worker keeps writing audio without interruption (or with one
   block of silence — acceptable; sink swap is operator-driven)

**Sub-task B.4 covers this** — the sink-swap signal must serialize
so the worker doesn't try to write to a half-constructed sink.

## 12. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Audio dropouts under heavy DSP load | Medium | Medium (operator hears clicks) | Bounded drop-oldest queue + stress test in 3.C |
| Mode-switch race — between main sending reset signal and worker processing it, rx thread enqueues 1-2 more IQ batches that then run with old config | Medium | Low (one block of weird audio at switch — already happens in single-thread code due to channel's internal buffer) | Worker's reset handler clears the input queue before resetting state; matches single-thread behavior so no UX regression |
| Notch list mid-update (worker sees half-applied notch list) | Low | Low | `notches_changed` signal carries an immutable tuple snapshot |
| Auto-LNA logic stale (reads old LNA peaks because signal hasn't fired yet) | Low | Low | Auto-LNA is already heuristic; ~100 ms staleness in peak history is fine |
| Reset/flush deadlock (main waits for worker, worker waits for main) | Low | High | All cross-thread is fire-and-forget signal/slot; no synchronous waits |
| QTimer logic on main expects sample ring to be there | High at first | Low (visual stall) | Phase 3.B explicitly removes `_fft_timer` and moves FFT into worker |
| Existing locks (`_rx_batch_lock`, `_ring_lock`) become redundant or wrong | High at first | Low | Audit and remove in 3.B |
| HL2Stream TX queue not fully thread-safe under new producer | Medium | High (audio corruption) | Sub-task B.7 audits; gating before activation |
| OutputStream created on main thread but written from worker | Low | Medium | Standard PortAudio pattern; verified in B.4 with single-producer lifecycle |
| Sink swap mid-stream causes worker to write to half-constructed sink | Low | Medium | Sink swap goes via signal — worker swaps reference between blocks |
| Backend toggle (single ↔ worker) confuses operator | Low | Low | Default = single-thread (current behavior). Operator opts into BETA explicitly. |

## 13. Migration strategy: Settings-toggle backend (BETA → default)

**Approach revised after design review.** Instead of a rip-and-
replace migration that flips the audio path wholesale, threading
follows the same pattern that worked for the GPU panadapter
backend:

```
Settings → DSP → Threading: [● Single-thread (default)
                              ○ Worker thread (BETA)]
```

Both code paths live in the tree. Operator picks one. If the worker
backend has a problem we missed in design, **flip a switch** to
fall back. We keep both paths until the worker backend has many
release cycles of field testing.

Why this is worth the extra effort:

- **Zero-risk rollout.** Single-thread DSP is what every existing
  v0.0.5 install runs. New install or upgrade defaults to that
  same behavior. Tester opts into BETA explicitly via Settings.
- **Field-testable in parallel.** Multiple operators can run on
  different backends and we get comparison data.
- **Easy rollback.** No revert commit needed if a tester hits a
  worker-thread bug — they flip the toggle, file an issue, and
  keep running their station.
- **Eventual deprecation path.** When we have 6+ months of clean
  worker-backend testing, we flip the default. Single-thread stays
  as a fallback, just like QPainter still does for the panadapter.

The cost: maintaining two routing paths in `Radio._on_samples_*`
for a release or two. Acceptable.

### Migration checklist (Phase 3.B sub-tasks)

In order. Each is a separate commit on a `feature/threaded-dsp`
branch off `main`. Stop and test between any two if anything looks
wrong.

- [ ] **B.1 — DspWorker shell + WorkerConfig.**
  Create `lyra/dsp/worker.py` with the `DspWorker` class
  (QObject subclass, `moveToThread` pattern), `WorkerConfig`
  dataclass, lifecycle methods (`start`, `stop`). No DSP behavior
  yet. Worker exists but isn't connected to anything.

- [ ] **B.2 — Settings toggle + persistence.**
  Add Settings → DSP → "Threading: [Single | Worker (BETA)]"
  combo. Wire to a new `Radio.dsp_threading_mode` property +
  QSettings key (`dsp/threading_mode`, default `"single"`).
  At startup, Lyra logs which mode is active. **Switching modes
  requires Lyra restart** (cleanest, avoids mid-flight migration
  edge cases).

- [ ] **B.3 — Worker process_block (audio path).**
  Implement worker's `process_block(iq)` mirroring
  `_on_samples_main_thread` body: notches/demod/NR/APF (via
  `_rx_channel.process`), AGC, AF/Vol/Mute/tanh, BIN, sink write.
  Move `_binaural` ownership into the worker. The single-thread
  path still works exactly as before; worker only runs when
  Settings selects it.

- [ ] **B.4 — Worker input queue + activation.**
  Worker exposes `enqueue_iq(samples)`. When worker mode is
  active, `Radio._stream_cb` (rx thread) routes batches to
  `worker.enqueue_iq` instead of `_bridge.samples_ready.emit`.
  Worker's run loop drains the queue and calls `process_block`.
  Drop-oldest behavior on queue overflow. Single-thread path
  remains unchanged for default users.

- [ ] **B.5 — Sink lifecycle for cross-thread use.**
  When worker mode is active, audio sink (AK4951 or
  SoundDeviceSink) is owned by the worker. Sink swaps on
  Settings change route through a signal so the worker swaps
  references between blocks (no half-constructed sink writes).

- [ ] **B.6 — LNA peak/RMS tracking moves to worker.**
  When worker mode is active, the per-block IQ peak/RMS
  computation runs on the worker. Worker emits `lna_peak_update`
  signal; main-thread Auto-LNA logic consumes from a local
  buffer it maintains from those updates.

- [ ] **B.7 — HL2Stream TX queue thread-safety audit (gating).**
  Audit `queue_tx_audio`, `clear_tx_audio`, `inject_audio_tx`
  for thread safety. Add locks where needed. **Must complete
  before B.4 enables operator-facing worker mode** — audio
  corruption from a missed lock is the worst-case bug.

- [ ] **B.8 — FFT migration to worker.**
  When worker mode is active, the sample ring is worker-internal
  and FFT runs inside `process_block` on a tick counter. Worker
  emits `spectrum_ready` (existing signal). Main-thread
  `_fft_timer` is disabled when worker mode is active. Verify
  panadapter cadence is identical between modes.

- [ ] **B.9 — Reset / flush via signal.**
  When worker mode is active, `Radio.reset()` emits a
  `reset_requested` signal to the worker. Worker's slot clears
  its input queue, resets `_rx_channel`, AGC peak, sample ring,
  binaural. Main-thread state (operator config, UI) reset
  in-place as today.

- [ ] **B.10 — End-to-end smoke test (per Section 14).**
  All modes, all sample rates, both sinks, both backends, mode/
  freq/rate changes under load. Operator-facing field test
  follows.

After B.10 passes: worker-thread backend is BETA-stable. The
**default** stays single-thread until at least one full release
cycle of operator field testing has passed without significant
issues. Promotion to default happens in a future release with a
one-line change to the `dsp/threading_mode` default key.

### Long-term: when do we remove single-thread?

Not soon. The single-thread path:
- Is the rollback for any worker-thread issue
- Is the simpler code path for new contributors to read
- Has zero deps on Qt threading correctness in the field

We remove it when:
- Worker thread has 6+ months of clean operator field reports
- We can no longer cleanly maintain both paths (some new feature
  forces a fork)
- We need the worker thread for a feature single-thread can't
  support (unlikely; even RX2 + TX could in principle run on the
  main thread, just slowly)

Until then: **both paths supported, worker is BETA, single is
default.**

## 13b. Thread lifecycle (start, stop, shutdown)

When worker mode is active, here's exactly what happens at each
operator action:

### Lyra startup
1. `MainWindow.__init__` constructs `Radio` (main thread)
2. `Radio.__init__` constructs the worker:
   - Creates `DspWorker` QObject
   - Creates `QThread`, calls `worker.moveToThread(thread)`
   - Connects worker config slots to Radio's `*_changed` signals
   - `thread.start()` — worker now has its own event loop running
3. Worker is **idle** — it's running but no IQ samples are being
   produced (radio not started yet)

### Operator clicks Start (begin streaming)
1. Main thread tells `HL2Stream` to start
2. Stream's `_rx_loop` begins receiving UDP packets in its own
   thread (unchanged)
3. Stream's callback (`_stream_cb`) routes IQ batches:
   - **Single-thread mode:** to `_bridge.samples_ready.emit` (today's path)
   - **Worker mode:** to `worker.enqueue_iq` (new path)
4. Worker's run loop pulls batches off the queue and processes them

### Operator clicks Stop
1. Main thread tells `HL2Stream` to stop
2. Stream's rx thread exits cleanly
3. Worker's input queue drains naturally (no more IQ arriving)
4. Worker stays **alive but idle** — ready to resume on next Start

### Operator changes mode/freq/rate (during stream)
1. Main thread updates Radio's local copies, emits
   `*_changed` signals
2. Worker's slots (running on worker thread) update the channel
   via existing `_rx_channel.set_*` calls
3. For freq/mode/rate: Radio also emits `reset_requested` →
   worker clears queue + resets channel state
4. Operator hears momentary discontinuity (same as today)

### Operator changes audio sink (Settings → Audio → Output)
1. Main thread constructs the new sink object
2. Main thread emits `audio_sink_changed(sink)` signal
3. Worker's slot replaces its sink reference between blocks
4. Old sink's `close()` is called — drains internal buffer

### Operator changes Settings → DSP → Threading mode
1. Setting persists to QSettings
2. **Restart required** (cleanest — avoids mid-flight migration)
3. Restart picks up the new mode

### Lyra shutdown (operator closes the window)
1. `MainWindow.closeEvent` triggers Radio shutdown
2. Radio tells stream to stop
3. Radio tells worker to stop:
   - Sets a `_shutdown_requested` flag
   - Worker's run loop sees flag, drains pending blocks, exits
   - `thread.quit()` then `thread.wait(1000)` — bounded wait
4. Audio sink `close()` runs on worker before exit
5. Qt main loop exits; process terminates

**No deadlocks possible** because:
- Main never waits synchronously on the worker (only `thread.wait`
  with a timeout, and only at shutdown)
- Worker never waits synchronously on the main (signals are
  fire-and-forget)
- Qt's queued signal/slot machinery handles cleanup of pending
  events when threads exit

## 14. Phase 3.C — verification

Stress-test the **worker** backend against the **single-thread**
backend on the same machine, same operator session. Both must
behave identically (audio + UI + meter readings).

Test matrix:

| Test | Single-thread | Worker | Notes |
|---|---|---|---|
| 96k IQ + USB + AGC Med | Baseline | Match | Most common operator config |
| 192k IQ + Aggressive NR + APF + BIN d=1.0 | Baseline | Match | Heavy DSP load |
| 384k IQ + every notch active + Aggressive NR | Baseline | Match | Heaviest combo we ship |
| WWV ↔ FT8 alternation every 5s for 5min | Baseline | Match | Reset path under load |
| Mode/freq/rate changes during stream | Baseline | Match | Config flux without dropouts |
| AK4951 ↔ PC Soundcard swap during stream | Baseline | Match | Sink swap correctness |
| QPainter ↔ GPU panadapter | Baseline | Match | FFT cadence on both |
| Lyra start → Start → Stop → close | Baseline | Match | Lifecycle correctness |

Pass criteria for the **worker** backend:

- ✓ No audio dropouts across the matrix above
- ✓ No frame drops on rx thread (ADC peak indicator updates
  smoothly)
- ✓ CPU utilization across cores improves vs single-thread (Task
  Manager: main thread % drops, worker thread % rises; total stays
  similar or improves)
- ✓ No deadlocks, no hangs, no QThread cleanup warnings on close
- ✓ Memory footprint stable over 30-minute idle stream
- ✓ S-meter, peak markers, panadapter, all overlays render
  identically to single-thread mode
- ✓ Operator field test confirms "feels right" — no subtle
  perceptual differences in audio, no laggy controls, no
  unexpected resets

If any fail: bug fix on the worker path before the BETA toggle
ships in a release. Single-thread path is unaffected.

## 15. Rollback plan

The Settings-toggle approach makes rollback trivial:

**Field-test rollback (operator):**
- Operator on BETA hits a problem
- They flip Settings → DSP → Threading back to "Single-thread"
- Restart Lyra
- Single-thread path runs exactly as before — zero regression
- They file an issue, we fix on the worker path

**Release rollback (us):**
- Phase 3.B's individual commits are still independently revertable
  if a sub-task introduces a regression even in single-thread mode
- Worst case: revert the entire `feature/threaded-dsp` branch,
  cherry-pick any unrelated fixes, ship single-thread-only
- The published v0.0.5 installer is unaffected; we just don't ship
  the BETA toggle in the next release

**Long-term rollback:**
- If after months of testing the worker backend has a fundamental
  flaw, we keep the single-thread path and quietly remove the
  Settings toggle. No operator who didn't opt into BETA notices.

Phase 3.D features (captured-noise-profile, NR2, etc.) are designed
to work on **either** backend so they don't depend on the threading
work succeeding. Building them on top of the single-thread path
during Phase 3 BETA is fine — they get the threading benefit
automatically once an operator switches.

## 16. Decided design choices + open questions

### Decided (this revision)

- **QThread.run() override vs QObject + moveToThread?**
  Going with **QObject + moveToThread**. Modern Qt-recommended
  pattern, cleaner for signal/slot.
- **Single worker or multiple?**
  **Single for v0.0.6.** Multi-worker is a future concern when
  RX2 + TX both ship and benefit from parallelism.
- **Migration approach — rip-and-replace or Settings toggle?**
  **Settings toggle (BETA)** — both code paths coexist; operator
  picks. See Section 13.

### Still open — operator input welcome

- **Block size for worker — same as today (2048)?** Probably yes,
  but stress test in 3.C will reveal whether smaller (lower
  latency) or larger (better cache) is better.
- **Watchdog?** Should we add a "worker hasn't processed a block
  in N ms" detector that warns the operator? Phase 3.D
  consideration, not required for B.
- **When do we promote worker → default?** After how many release
  cycles of clean field testing? Suggest minimum 2 releases (so
  v0.0.8 or v0.0.9 if we ship Phase 3 in v0.0.6).
- **Should we expose a "DSP thread CPU usage" metric on the
  toolbar?** Nice-to-have for diagnosing performance issues; not
  blocking.

---

## Sign-off

**Operator (N8SDR):** Reviewed 2026-04-29 — approved direction.
Settings-toggle (BETA) migration approach accepted; worker thread
will be opt-in via Settings → DSP → Threading until enough field
testing supports promotion to default.
**Lead:** Claude

**Status:** Design doc is the source of truth for Phase 3.B
implementation. Both this doc and the companion `wdsp_integration.md`
are coherent post-relicense (Lyra is GPL v3+ effective v0.0.6;
WDSP integration is unblocked but follows operator-priority order
in the release roadmap).

Next action: Phase 3.B sub-tasks per §13. B.1 starts when the
operator gives the go-ahead.
