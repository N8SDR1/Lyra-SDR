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

## 4. Thread responsibilities

| Thread | Owns | Reads (cross-thread) | Writes (cross-thread) |
|---|---|---|---|
| **HPSDR rx** | UDP socket, frame parser, C&C registers, TX audio queue | none | IQ samples → worker queue |
| **DSP worker** | rx_channel, audio_sink, AGC state, BIN state, sample_ring, FFT context, LNA peaks history | config snapshot from main (mode, freq, AGC profile, APF/BIN params, NR profile) | spectrum data, S-meter, LNA readings → main |
| **Qt main** | All UI state, panels, settings, operator preferences, band memory, snapshots | meter readings, spectrum data | config changes → worker, freq/mode → stream |
| **PortAudio cb** | OutputStream's internal buffer | audio frames the worker wrote | (none — feeds OS audio) |

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

## 6. The atomic-config-snapshot pattern

To avoid mid-block parameter tearing (e.g., demod mid-block while
mode flips from USB to LSB), the worker reads a **consistent
snapshot** of its local config at the start of each `process_block`.
The worker maintains its own `_config: WorkerConfig` dataclass that
mirrors the Radio's operator-facing parameters. Main thread updates
fire Qt signals; worker's slots update `_config` between blocks.

```python
@dataclass
class WorkerConfig:
    mode: str
    freq_hz: int
    rate: int
    af_gain_db: int
    volume: float
    muted: bool
    agc_profile: str
    agc_release: float
    agc_target: float
    agc_hang_blocks: int
    apf_enabled: bool
    apf_bw_hz: int
    apf_gain_db: float
    bin_enabled: bool
    bin_depth: float
    notches: tuple[Notch, ...]
    notch_enabled: bool
    nr_enabled: bool
    nr_profile: str
    cw_pitch_hz: int
    rx_bw_by_mode: dict[str, int]
```

Updates from main → worker:
```python
# In Radio.set_apf_enabled (main thread):
self._apf_enabled = bool(on)
self.apf_enabled_changed.emit(on)
# Worker's slot connected via QueuedConnection:
def _on_apf_enabled(self, on):
    self._config.apf_enabled = bool(on)
    self._rx_channel.set_apf_enabled(on)  # also flows down into channel
```

Two important properties of QueuedConnection:
1. The slot runs on the **receiver's** thread (worker), not the
   emitter's (main). No raw threading needed.
2. Events queue in order — if main emits `set_freq` then `set_mode`,
   worker processes them in that order, with `process_block` calls
   interleaved at block boundaries.

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

## 10. AK4951 sink consideration

`AK4951Sink.write()` queues stereo samples into the HL2Stream's TX
audio queue (`stream.queue_tx_audio()`). The HL2Stream's TX thread
then drains this queue into EP2 frames sent to the radio. So:

- DSP worker calls `audio_sink.write(audio)` (worker thread)
- `audio_sink` = `AK4951Sink`, internally calls
  `stream.queue_tx_audio(stereo)` (still worker thread)
- `stream.queue_tx_audio` is currently uses `deque` operations
  which are thread-safe in CPython for append/popleft, but we need
  to verify any non-trivial state there is locked.

**Action item for Phase 3.B:** audit `HL2Stream.queue_tx_audio`
and `clear_tx_audio` for thread safety.

## 11. SoundDeviceSink consideration

`SoundDeviceSink.write()` calls `OutputStream.write(stereo)`
(blocking, but PortAudio handles its own thread synchronization).
PortAudio's OutputStream is documented as thread-safe for write
operations from a single thread — which matches our pattern (DSP
worker is the single producer).

**No change needed for SoundDeviceSink.** ✓

## 12. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Audio dropouts under heavy DSP load | Medium | Medium (operator hears clicks) | Bounded drop-oldest queue + measurable in stress test |
| Mode-switch race (worker processes new-mode sample with old filter state) | Medium | Low (one block of weird audio at switch) | Atomic config snapshot at block start; reset called on mode change |
| Notch list mid-update (worker sees half-applied notch list) | Low | Low | `notches_changed` signal carries an immutable tuple snapshot |
| Auto-LNA logic stale (reads old LNA peaks because signal hasn't fired yet) | Low | Low | Auto-LNA is already heuristic; ~100 ms staleness in peak history is fine |
| Reset/flush deadlock (main waits for worker, worker waits for main) | Low | High | All cross-thread is fire-and-forget signal/slot; no synchronous waits |
| QTimer logic on main expects sample ring to be there | High at first | Low (visual stall) | Phase 3.B explicitly removes `_fft_timer` and moves FFT into worker |
| Existing locks (`_rx_batch_lock`, `_ring_lock`) become redundant or wrong | High at first | Low | Audit and remove in 3.B |

## 13. Migration checklist (Phase 3.B sub-tasks)

In order. Each is a separate commit. Stop and test between any
two if anything looks wrong.

- [ ] B.1 — Create `lyra/dsp/worker.py` with `DspWorker(QThread)`
  shell + `WorkerConfig` dataclass. No DSP yet, just lifecycle.
- [ ] B.2 — Move `_rx_channel`, `_audio_sink`, `_binaural` ownership
  into worker. Stub `process_block` that just routes IQ through the
  same chain currently in `_on_samples_main_thread`.
- [ ] B.3 — Add input queue + `enqueue_iq` method called from
  `_stream_cb` (rx thread). Worker drains the queue in its run
  loop. Main thread no longer touches `_on_samples_main_thread`.
- [ ] B.4 — Migrate AGC + AF/Vol + tanh into worker. Add `agc_*`
  config update slots. Keep a fallback path until B.5 is verified.
- [ ] B.5 — Migrate LNA peak/RMS tracking into worker. Add
  `lna_peak_update` cross-thread signal. Main-thread Auto-LNA logic
  consumes from a local copy.
- [ ] B.6 — Move `_sample_ring` ownership into worker. Move FFT into
  worker. Remove main-thread `_fft_timer`.
- [ ] B.7 — Audit + remove redundant locks (`_rx_batch_lock`,
  `_ring_lock`). Verify HL2Stream TX queue thread safety.
- [ ] B.8 — Reset / flush request mechanism — worker-side handler
  for the existing reset operations.
- [ ] B.9 — Remove the dead `_on_samples_main_thread` and any
  scaffolding from B.1-B.7.
- [ ] B.10 — End-to-end smoke test: all modes, all sample rates,
  all sink combinations, mode/freq/rate changes under load,
  Settings panel changes during stream.

Each sub-task lands as its own commit on `feature/threaded-dsp` (new
branch off `main`). Branch merges to main only after Phase 3.C
(stress test) signs off.

## 14. Phase 3.C — verification

Stress-test combinations to verify no regressions:

- 384k IQ rate + Aggressive NR + APF + BIN at depth 1.0 + every
  notch active — heaviest combo we ship today
- WWV ↔ FT8 alternation every 5 seconds for 5 minutes — verifies
  reset path under load
- Settings panel walk-through during stream: change AGC profile,
  toggle BIN, switch rate, switch mode, etc. — verify no audio
  dropouts during config flux
- Both sinks: AK4951 and PC Soundcard, swap between them under load
- Both backends: QPainter and GPU panadapter — verify FFT cadence
  is correct on both

Pass criteria:
- No audio dropouts in any combination above
- No frame drops on rx thread (operator-visible: ADC peak indicator
  should update smoothly)
- CPU utilization across cores improves vs single-thread baseline
- No deadlocks, no hangs, no QThread cleanup warnings on close
- Memory footprint stable over 30-minute idle stream

## 15. Rollback plan

Phase 3.B is a single architectural change with multiple commits.
If it produces field-test issues:

- Each B.x commit is independently revertable. We can roll back to
  whichever sub-task last passed.
- Worst case: revert the entire `feature/threaded-dsp` branch,
  cherry-pick any small bugfixes that were unrelated, keep going
  on `main` with single-thread DSP.
- Captured-noise-profile and the rest of the noise toolkit work
  doesn't strictly require Phase 3 — it would just be slower
  without it. So Phase 3 rollback delays performance, not features.

## 16. Open questions

- **Should we use `QThread.run()` override, or worker QObject +
  `moveToThread`?** Latter is the modern Qt-recommended pattern,
  cleaner for signal/slot. Going with QObject + moveToThread.
- **Single worker or multiple (e.g., one per RX channel)?** Single
  for v0.0.6. Multi-worker is a Phase 4 concern when RX2 + TX both
  ship.
- **Block size for worker — same as today (2048)?** Probably yes,
  but stress test will reveal whether smaller (lower latency) or
  larger (better cache) is better.
- **Watchdog?** Should we add a "worker hasn't processed a block in
  N ms" detector that warns the operator? Phase 3.D consideration,
  not required for B.

---

## Sign-off

**Operator (N8SDR):** [pending review]
**Lead:** Claude

When operator agrees with this design, we proceed to Phase 3.B.
