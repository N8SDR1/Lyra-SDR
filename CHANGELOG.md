# Changelog

All notable changes to Lyra-SDR will be documented in this file, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/) loosely.
Lyra uses pre-1.0 semver: `0.<minor>.<patch>` where `<minor>` bumps for
user-facing feature batches and `<patch>` bumps for bug-fix-only releases
between feature batches.  See `lyra/__init__.py` for the canonical version
string.

License history: through v0.0.5, Lyra was MIT-licensed.  Starting with
v0.0.6, Lyra is GPL v3 or later (see `NOTICE.md`).

---

## [0.0.9.2-pre4] — 2026-05-04 — "Audio Architecture Rebuild — Dedicated EP2 writer + AGC"

**Pre-release.**  Major architectural rewrite that supersedes the
abandoned Commit 3 / Commit 3-fixup attempts.  Two distinct click
sources fixed in one drop because they're orthogonal:

- **Discrete clicks/pops** -- caused by EP2 audio underruns when
  the host->radio frame send was tied to bursty UDP arrival in
  ``_rx_loop``, plus a producer/consumer queue that silently
  dropped on overrun and zero-padded on underrun.  Replaced
  with a **dedicated EP2 writer thread** running at the codec's
  steady audio rate (~380 Hz) on its own scheduling priority slot.
- **Continuous "scratchy / dirty record player" texture in the
  noise floor** -- caused by Lyra's per-sample AGC peak tracker
  triggering ~100 attacks/sec on Gaussian noise with a static
  PEAK_FLOOR.  Replaced with a **dynamic PEAK_FLOOR clamped to
  K*noise_baseline** so noise samples no longer cross peak.
  Co-investigated by N9BC + external senior-engineering review.

### EP2 writer architecture rewrite

In ``lyra/protocol/stream.py``:

- **New dedicated thread ``_ep2_writer_loop``** owns host->radio
  EP2 frame send.  Drift-corrected timer cadence at 2.625 ms /
  380.95 Hz (= 48 kHz audio / 126 samples per frame), independent
  of UDP arrival cadence.  Each iteration: pull one 126-sample
  audio block from producer queue (non-blocking), pick next C&C
  round-robin register, build EP2 frame, send via UDP.  When no
  audio is queued, sends a C&C-only frame to keep the HL2
  watchdog satisfied.
- **MMCSS Pro Audio priority** elevation on Windows (ctypes -> avrt.dll)
  for both the EP2 writer and the RX loop threads.  Best-effort;
  silently no-op on other platforms or if the API is unavailable.
  Bumps thread priority above default user-thread class so audio
  isn't starved by UI / GC / generic background work during
  scheduling jitter events.
- **Producer/consumer handshake** via a small bounded queue
  (``_ep2_audio_queue``, 8 blocks deep = 21 ms buffer) protected
  by ``_ep2_audio_cond``.  Producer (``submit_audio_block``)
  waits with bounded timeout when queue is full; consumer
  (writer thread) ``notify_all``'s after every pop so the
  producer is naturally paced to consumer cadence.  No silent-
  drop, no zero-pad-on-underrun.
- **EP2 send removed from ``_rx_loop``.**  RX loop now only
  receives UDP datagrams, parses EP6 frames, decodes telemetry,
  dispatches samples to the host -- no longer responsible for
  C&C round-robin or audio cadence.

In ``lyra/dsp/audio_sink.py``:

- **AK4951Sink simplified.**  ``write()`` slices incoming audio
  into 126-sample blocks and submits each via
  ``stream.submit_audio_block``.  No more 100 ms pre-fill, no
  more fade-on-close ceremony -- the writer thread sees the
  ``inject_audio_tx`` flag flip and switches to C&C-only frames
  on its next cadence tick (within ~3 ms), which the codec
  experiences as a sample-aligned silence transition (no click).
  Net latency from operator action to speaker drops by ~95 ms.

### AGC dynamic peak floor

In ``lyra/radio.py::_apply_agc_and_volume``:

- **Replaced static ``PEAK_FLOOR = 1e-4``** with
  ``max(1e-4, K * self._noise_baseline)`` where K = 1.5.
  Noise samples can no longer cross peak during noise-only
  listening, eliminating the spurious-attack-driven AM modulation
  of the noise floor.  Real signals (>>1.5x noise) still cross
  peak and trigger AGC at full speed; CW dit edges and SSB
  consonant onsets unaffected.

### What this commit does NOT yet fix

- Loud volume bursts on the AK4951 path (Rick-only) -- still on
  the gateware-replay-on-EP2-underrun hypothesis.  With EP2
  underruns now structurally rare (steady writer cadence +
  backpressure), bursts should drop dramatically as a side
  effect.  Wireshark capture during deliberate UI stress is the
  empirical confirmation; deferred to its own commit.
- v0.0.9.1's "always-applied" gap-fade bug in
  ``_apply_agc_and_volume`` -- separate small fix; deferred.

### What was reverted from earlier rebuild attempts

- Commit 3 (``threading.Condition`` backpressure on the
  ``_tx_audio`` deque) -- reverted via two ``git revert`` commits
  before this rewrite.  Tuning across two iterations made
  underruns worse than Commit 2 alone (49/min -> 66/min ->
  93/min equivalent).  Diagnosis: bolting backpressure onto a
  deque-between-producer-and-rx-loop architecture didn't address
  the actual problem (UDP arrival burstiness driving consumer
  cadence).  This commit replaces the architecture entirely
  rather than tuning the wrong design.

### Tester checklist

- ``un=`` should drop dramatically -- target near zero in
  multi-minute runs.  EP2 underruns are now structurally rare
  (writer thread always has audio in queue except on
  catastrophic producer stall).
- ``ov=`` will tick up modestly when the producer pushes faster
  than the writer drains (= producer being naturally paced
  down by backpressure).  This is healthy and not a click.
- ``DSP`` heartbeat in telemetry should still sit near 95 Hz
  at all IQ rates (worker rate; unchanged from Commit 2).
- The continuous "scratchy" noise-floor texture should be
  noticeably reduced or gone.  Slow vs Med AGC presets should
  now sound nearly identical on noise-only audio (the dynamic
  peak floor brings them into line).

### Recovery

If anything goes wrong, install
``Lyra-Setup-0.0.9.1.exe`` from
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.1>.

---

## [0.0.9.2-pre2] — 2026-05-04 — "Audio Architecture Rebuild — Commit 2"

**Pre-release.**  Second of six audio rebuild commits.  Where
Commit 1 isolated the DSP worker from the Qt main thread, this
commit aligns the producer/consumer cadence at the deque
interface so the underrun mechanism that produces clicks
becomes structurally improbable rather than common.

### What changed in this commit

- **Cadence-matched IQ batching.**  ``Radio._rx_batch_size`` is
  now rate-aware (`_compute_rx_batch_size`): sized so each batch
  produces exactly **126 audio samples** after decimation, at any
  IQ rate.  Cadence at the deque interface goes from "bursty 23
  Hz at 48k IQ" or "bursty 94 Hz at 192k IQ" to a steady
  **381 Hz at every supported rate** -- exactly matching the EP2
  consumer's 380 Hz pull rate, one push to one pull.
  ```
  IQ rate   decim   rx_batch   audio/batch   batch Hz
   48 kHz    1        126         126         381
   96 kHz    2        252         126         381
  192 kHz    4        504         126         381
  384 kHz    8       1008         126         381
  ```
- **Channel block_size 512 → 126.**  ``Radio._audio_block`` and
  ``PythonRxChannel.__init__`` default both move to 126.  Aligns
  the channel's internal DSP-loop block size with the EP2
  consumer's pull size, so producer per-block work matches
  consumer per-frame work.  Verified 2026-05-04 against every
  shipped DSP module (NR1, NR2, ANF, LMS, Squelch, all six
  demods) -- length-preserving contracts honored at the new
  block size.
- **DspWorker queue depth 10 → 40.**  At the new 381 Hz batch
  cadence the previous 10-deep queue was only ~26 ms of headroom
  for transient stalls (GC pauses, OS scheduling hiccups).  40
  gives ~100 ms.  Will become less critical once Commit 3's
  real backpressure replaces drop-oldest with producer-blocks-
  on-full.
- **Rate-change refresh.**  ``Radio.set_rate()`` now recomputes
  ``_rx_batch_size`` and drops any partial batch atomically so
  cadence stays matched across operator-driven rate changes.

### What this commit does NOT yet fix

- Producer-side backpressure -- a producer running ahead of the
  consumer can still pile up samples in the deque (drop-oldest
  on overflow).  Commit 3 (`threading.Condition` backpressure)
  is the structural fix.
- Loud volume spikes on Rick's AK4951 -- still on the gateware-
  replay-on-EP2-underrun hypothesis, falsifiable in Commit 5
  Wireshark.  Commit 2 should make EP2 underruns rare enough
  that the spike rate drops as a side effect.

### Tester checklist for this pre-release

- Watch the audio telemetry indicator: ``un=`` should drop
  significantly compared to v0.0.9.2-pre1.  ``deque H/`` high-
  water should hover near the consumer block size (126) rather
  than near 0 or near the 4800 pre-fill.
- ``DSP NN Hz`` should now read **~380 Hz** at all IQ rates
  (was rate-dependent before -- 23 at 48k, 94 at 192k).  This
  is the visible signature that cadence is matched.
- CPU may rise modestly (more invocations per second; same
  total work).  Acceptable: from 2.9 % observed in pre1 to
  perhaps 6-8 % in pre2.  If CPU goes >30 % please file an
  issue.
- If audio is audibly worse than v0.0.9.2-pre1 in any way,
  flip Settings → DSP → Threading to "Single-thread (legacy)",
  restart, file an issue.

### Recovery

If anything goes wrong, install
`Lyra-Setup-0.0.9.1.exe` from
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.1>.

---

## [0.0.9.2-pre1] — 2026-05-04 — "Audio Architecture Rebuild — Commit 1"

**Pre-release.**  First of six pre-releases that incrementally
rebuild Lyra's audio production architecture per the senior-
engineering audit in `docs/architecture/audio_rebuild_v0.1.md`.
Each pre-release is independently flight-testable; if any one
regresses, it can be reverted without disturbing the others.
`main` stays at v0.0.9.1 throughout the rebuild — the v0.0.9.1
installer remains the published stable release until both Rick
and Brent sign off on the full rebuild.

### What changed in this commit

- **DSP worker thread is now the default mode** (was: opt-in
  BETA).  Audio DSP no longer competes with UI paint events,
  mouse handling, and signal/slot dispatch on the Qt main
  thread.  This is part 1 of fixing the universal click-pop
  bug documented in v0.0.9.1's "residual clicks parked" note.
- **Operator escape hatch:** Settings → DSP → Threading combo
  now offers "Worker thread (default)" and "Single-thread
  (legacy)".  If the new default regresses on any rig, flip
  back to legacy, restart Lyra, and please file an issue with
  what you observed.
- **QSettings ordering bug fixed.**  Previously the persisted
  `dsp/threading_mode` preference was loaded AFTER Radio
  initialization, so opting into worker mode set the flag but
  never started the worker thread.  Loaded earlier now, before
  the worker-construction decision.  Operators who had opted
  into worker mode in earlier versions and saw no change
  should now see the worker actually running.
- **Audio telemetry indicator** added to the status bar
  showing `DSP NN Hz | deque H/MAX | un=X ov=Y`.  Lets
  operators (and us) see objective rebuild progress vs vibes.
  Color-coded: green when healthy, amber when underrun /
  overrun counters tick, red if the DSP worker stalls
  outright.  Removed in v0.0.9.2 full release once the
  rebuild lands clean.
- **DSP worker heartbeat counter** (`DspWorker.blocks_processed`)
  + **TX audio deque high-water tracker** (`HL2Stream.read_tx_audio_high_water`)
  added to support the telemetry above.  Both are read-only
  diagnostic surfaces; no operator-facing knobs.

### Known issues / what this commit does NOT fix

- Click-pop frequency is expected to **decrease** but not
  vanish in this commit.  The full fix needs Commit 2 (block
  size match) + Commit 3 (real backpressure).  Use the
  underrun/overrun counters in the status bar to see whether
  the rate has actually moved.
- Loud volume spikes on AK4951 (Rick-only) hypothesis untested
  yet — gated on Commit 5 Wireshark capture.

### Tester checklist for this pre-release

- Run for 30+ minutes on RX, normal listening.
- Watch the audio telemetry indicator in the status bar.
  Heartbeat (DSP NN Hz) should tick steadily.  un + ov counters
  should stay at lower numbers than v0.0.9.1.
- If audio is audibly worse than v0.0.9.1 in any way: open
  Settings → DSP → Threading, flip to "Single-thread (legacy)",
  restart Lyra, and file an issue with details.
- Confirm Settings → DSP → Threading shows "Worker thread
  (default)" as currently selected on first launch after
  upgrade.

### Recovery

If anything goes wrong, install
`Lyra-Setup-0.0.9.1.exe` from
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.1>.
Operator data (settings, memory bank, noise profiles) is
preserved across the downgrade.

---

## [0.0.9.1] — 2026-05-03 — "Memory & Stations"

Bug-fix + feature patch on top of v0.0.9.  Two headline items:
**audio click reduction** for the long-standing pops/ticks symptom,
and **TCI audio + IQ binary streaming** so digital-mode apps can
talk to Lyra without a Virtual Audio Cable.  Plus N9BC joining as
co-contributor.

### Audio click reduction

Operators reported sustained ~1.5 clicks/sec on both AK4951 and
PC-soundcard output paths going back to v0.0.7.  This release
substantially reduces them via three independent fixes:

- **UDP RX buffer bumped to 4 MB** on the HPSDR P1 receive socket
  (`lyra/protocol/stream.py`).  Default Windows UDP RCVBUF is
  ~64-208 KB; at 192 kHz IQ rate the HL2 streams ~1.5 MB/sec of
  EP6 frames, so the kernel buffer could fill in under a second
  of CPU stall and silently drop frames.  4 MB ≈ 2.6 sec of
  headroom — covers any plausible Python GC pause or Windows
  context-switch storm.  Verified: stream-error counter typically
  stays at 0 across long sessions where the prior version
  accumulated dozens of drops.
- **10 ms audio fade-in on detected sequence gap**
  (`lyra/radio.py::_apply_agc_and_volume`).  When `seq_errors`
  ticks up, the next audio block gets a 0→1 linear ramp on the
  first 480 samples to mask the IQ discontinuity that would
  otherwise reach the speaker as a step.
- **Stream-error indicator in the status bar** — permanent
  widget showing "Stream OK" / "Stream: N errors" so operators
  can correlate audible pops with the underlying mechanism.
- **Audio block size reduced 2048 → 512** (`lyra/radio.py`).
  Worker bursts now 10.7 ms instead of 43 ms; tighter
  producer/consumer cadence at the audio sink interface, which
  reduces underrun frequency.

### TCI audio + IQ streaming (new feature)

`lyra/control/tci.py` extended to implement TCI v2.0 binary stream
support per the EESDR Expert Electronics specification §3.4.

- **Binary frame infrastructure**: `Stream` struct packing
  (64-byte little-endian header + payload), per-client subscription
  state, command handlers for `AUDIO_START` / `AUDIO_STOP` /
  `AUDIO_SAMPLERATE` / `AUDIO_STREAM_SAMPLE_TYPE` /
  `AUDIO_STREAM_CHANNELS` / `IQ_START` / `IQ_STOP` /
  `IQ_SAMPLERATE` / `LINE_OUT_*`.
- **Radio-side audio + IQ taps**: `audio_for_tci_emit` and
  `iq_for_tci_emit` signals fired per audio block / IQ batch from
  the worker thread, queued to TCI server on the main thread for
  binary-message dispatch.  Independent of the AK4951 / PortAudio
  sink choice — TCI is a third parallel audio path.
- **TCI Settings UI rewrite**: 3-column layout (Server / Audio + IQ
  Streaming / Spots) matching the canonical Thetis TCI panel.  New
  controls: master toggles for audio + IQ, "Always stream"
  options, swap-IQ toggle, mode-name mapping flags
  (CWL/CWU↔CW), Emulate ExpertSDR3 protocol, CW spot sideband
  forcing, flash-spots + own-call color pickers, currently-streaming
  client list.
- **Validated** against MSHV (FT8 decoder) — TCI binary audio
  produces decodable FT8 traffic on 7.074 MHz with no VAC
  intermediary.

  Setup recipes for WSJT-X / JS8Call / MSHV / FLDIGI / log4OM in
  the in-app User Guide (`docs/help/tci.md`).

### Audio architecture investigation (parked)

A larger architectural rewrite was attempted on this branch
(Thetis-style backpressure + sample-and-hold + AGC look-ahead) but
**reverted** when operator flight-test confirmed it produced a new
"thumping" symptom worse than the original click problem.  The
sample-and-hold fallback turned out to be worse than zero-pad for
tonal audio (CW especially); the backpressure timing parameters
needed more design work than a hot-patch could safely deliver.
v0.1's RX2 work will revisit this with proper thread-architecture
design and a wider testing window.  Operator-instrumented data
(audio under/over counters added during investigation, then UI-hidden
per operator UX call) confirmed the click cause is at the
producer/consumer interface between worker and audio sink, not in
the DSP chain itself — which TCI audio (taps the chain directly,
no sink interface) demonstrates by being completely click-free.

### Other fixes

- **AGC smooth-attack reverted** (was `[0.0.9.1] §A.5`).  Operator
  validated that `peak <- mag` instant attack (the v0.0.7.1 quiet-
  pass design) produces less audible artifact than the smooth-attack
  attempt, which created tanh-saturation bursts during the 2.5 ms
  attack ramp on hard CW transients.
- **TCI binary stream `length` field** corrected per spec: total
  scalar values in payload, not frame count.  Without this fix,
  stereo audio decoded as half-rate (wrong pitch, undecodable for
  FT8).  Found in MSHV flight-test.
- **TCI sample-conversion in-place mutation** fixed.  Format
  conversion was modifying the audio array shared with the
  AK4951 / PortAudio sink, occasionally clipping the sink output
  when AGC briefly drove > 1.0 amplitude.

### Click-free workflow

Side effect of the TCI streaming infrastructure: the `audio_for_tci_emit`
signal posted from the worker thread to the main thread on every
audio block acts as a regularizing pacemaker that smooths the
worker's per-block cadence.  **Result: with TCI server enabled (with or
without TCI clients connected), the AK4951 and PC soundcard output
paths run substantially cleaner.**  Lyra's TCI server defaults to
enabled; operators get this benefit automatically.  Architectural
explanation in CLAUDE.md §9.6.

### Contributors

- **Brent Crier (N9BC)** joined as co-contributor 2026-05-03 during
  v0.0.9.1 testing.  Independent flight-test on PC soundcard +
  ANAN G2 (future v0.4 test rig).  See `CONTRIBUTORS.md`.

---

## [0.0.9] — 2026-05-02 — "Memory & Stations"

Pre-RX2 polish release driven by operator wishlist.  Four feature
batches plus tooltip + URL-fallback hardening.  No breaking
changes; QSettings additions for new persistence (memory bank,
GEN customization, EiBi paths) are forward-only.

The original v0.0.9 milestone was RX2; that work shifted to v0.1
when the operator captured the memory / time-station / SW-database
items as gating UX before the RX2 build-out.  RX2 now follows in
**v0.1**, TX in **v0.2**, PureSignal in **v0.3** — see CLAUDE.md
§7 for the updated roadmap.

### Added — TIME button (HF time-station cycle)

- **TIME button** on the BANDS panel between **GEN3** and **Mem**.
  Cycles through 9 HF time / standard-frequency broadcasters:
  WWV / WWVH on 2.5 / 5 / 10 / 15 / 20 / 25 MHz, CHU on 3.330 /
  7.850 / 14.670 MHz.  Mode auto-set to AM, filter to 6 kHz —
  the right defaults for double-sideband AM time signals.
- **Country-aware ordering.**  Lyra reads operator callsign from
  Settings → Operator, looks up DXCC country code, and starts the
  cycle from the closest stations to the operator's country
  (US calls -> WWV first, Canadian calls -> CHU first, others ->
  ascending frequency).  First press lands on a station you can
  most likely hear from your QTH instead of a fading-out daytime
  signal.
- New `lyra/data/time_stations.py` with the 9-station table.
- See `docs/help/time_stations.md` for the operator topic.

### Added — GEN1 / GEN2 / GEN3 customization

- **Right-click GEN1/2/3** to save the current frequency / mode /
  filter into the slot.  Confirm dialog shows the proposed
  overwrite ("Save current frequency, mode, and filter to GEN1?
  7.125.000 USB 2.4 kHz") so accidental clicks don't blow away a
  saved preset.
- Defaults retained as starter values — operators are meant to
  remap to their own habits.
- Persistence via QSettings under
  `HKEY_CURRENT_USER\Software\N8SDR\Lyra\GEN\` (one subkey per
  slot, four leaf values: freq / mode / filter / name).

### Added — Memory bank (Mem button + 20 named presets)

- **New Mem button** on the BANDS panel (right of GEN3 + TIME).
  Opens a dropdown of named operator memories with name, freq,
  mode, and filter columns.  Click any entry to recall.
- **+ Save current as new memory…** entry at the top of the
  dropdown opens a name-prompt dialog and saves the current radio
  state with that label.  20-entry cap with friendly explanation
  when the bank is full.
- **Manage presets…** entry at the bottom of the dropdown opens
  Settings → **Bands → Memory** with full CRUD: rename
  (double-click name column), delete (Del key), reorder (move
  up/down), CSV import/export, reset to defaults.
- New `lyra/memory.py` module with the bank model + persistence.
- New Settings → Bands tab containing **Memory** sub-tab with the
  full management UI.
- CSV format: `name,frequency_hz,mode,filter_hz` — UTF-8, one
  entry per line, header row required, malformed rows skipped
  with error report.
- See `docs/help/memory.md` for the operator topic.

### Added — Shortwave broadcaster overlay (EiBi)

- **EiBi station-ID overlay** on both the CPU (QPainter) and GPU
  (QOpenGL / shader) panadapter widgets.  Renders broadcaster
  name / language / target region as a stacked label above each
  on-air signal in the SW broadcast bands (49m, 41m, 31m, 25m,
  22m, 19m, 16m, 13m, 11m).
- **Auto-detection via existing `lyra.band_plan.find_band`.**
  Overlay is suppressed inside the operator's region's amateur
  allocations (US 40m amateur 7.000-7.300 MHz wins over 41m
  broadcast 7.200-7.450 MHz; Settings flip restores everywhere).
  Region = NONE shows labels everywhere.
- **Schedule-aware.**  Each entry has start / stop time + day-of-
  week mask; Lyra checks the current UTC moment against each
  station's schedule and only paints labels for stations
  currently on the air.
- **Multi-row label stacking** — up to 4 rows greedy-pack
  collision-avoiding (mirrors the TCI spots renderer).  Bands
  like 31m at 5pm UTC stay readable instead of becoming a wall
  of overlapping text.
- **EiBi data layer** — new `lyra/swdb/` package:
  - `eibi_parser.py` — CSV parser with column-layout handling,
    time-window math, day-of-week field, power-class default,
    malformed-row skip.
  - `store.py` — `EibiStore` with sorted-by-frequency binary
    search, power filter, on-air filter.
  - `time_filter.py` — UTC-aware schedule check including
    wrap-around windows (2300-0100 UTC).
  - `overlay_gate.py` — region / band-plan / force-on logic.
  - `downloader.py` — background HTTPS downloader with QThread
    worker, season-filename auto-compute, URL fallback chain.
- **Settings → Bands → SW Database** tab — file-status display,
  master enable, min-power filter, "show in ham bands too"
  override, manual-install workflow buttons (**Open EiBi
  website**, **Copy URL**, **Reload file**), and the
  **Update database now** background fetch.
- See `docs/help/sw_database.md` for the operator topic.

### Changed

- **Tooltip font 11pt → 13pt** globally (operator readability
  feedback).  Applied in both `theme.py` QSS and `app.py`
  `QToolTip.setFont()`.
- **Settings dialog** gained a top-level **Bands** tab containing
  three sub-tabs (Memory / Time Stations / SW Database).
- **"Manage presets…"** from the Mem dropdown now navigates
  directly to Bands → Memory at construction time (was opening
  Settings on the previously-visible tab and requiring an extra
  click).

### Fixed (during v0.0.9 development)

- **EiBi season filename was uppercase.**  Server is case-
  sensitive — `sked-A26.csv` returned 404, must be lowercase
  `sked-a26.csv`.  `season_filename()` now lowercases the season
  letter on URL formatting; the rest of the code keeps `'A'` /
  `'B'` as canonical uppercase identifiers.
- **EiBi URL fallback chain.**  As of 2026-05, `www.eibispace.de`
  presents a TLS cert issued for the apex domain, producing SSL
  hostname-mismatch errors.  Default base URL now `eibispace.de`
  (apex); downloader iterates through 4 fallback URLs (apex/www
  × HTTPS/HTTP) and reports which combination succeeded.  HTTP at
  the end of the chain is acceptable since EiBi is freely
  published broadcast-schedule data with no auth or sensitive
  payload.
- **EiBi labels too small + overlapping.**  Bumped label font
  8pt → 10pt and added multi-row greedy stacking
  (MAX_EIBI_ROWS=4).

### Operator-experience notes

- The original v0.0.9 plan was a SW-Database **button** alongside
  GEN/TIME/Mem.  Operator simplification request during design:
  no SW button — auto-detect by band-plan, keep the BANDS row
  uncluttered.  Final button order: **GEN1 GEN2 GEN3 TIME Mem**.
- The original Memory plan was a sidebar list.  Operator
  preference: dropdown off a **Mem** button — same button
  vocabulary as GEN1/2/3, no extra panel real-estate.

---

## [0.0.8.1] — 2026-05-02

### Fixed

- **Auto-update notification now fires reliably after a release.**
  Operator-reported: v0.0.8 dropped, v0.0.7-binary operators didn't
  see the toolbar update indicator until they manually clicked
  Help → Check for Updates.  Three compounding causes:

  1. **24-hour throttle was too long.**  Operator launched on Day 1
     (no update yet), v0.0.8 dropped on Day 2, operator launched
     within the 24 h cache window so the silent check was skipped
     entirely.  Reduced to **4 hours** -- still well below the
     GitHub-API rate-limit envelope, much faster discovery.
  2. **No version-aware cache bypass.**  After a local upgrade the
     cache stayed valid even though the local version changed.
     Now the throttle is **bypassed when local version differs**
     from what was last checked.
  3. **Cached "update available" state wasn't surfaced unless a
     fresh check just ran.**  The toolbar indicator now shows
     **immediately on every launch** if QSettings holds a cached
     newer-tag, even before the fresh network check runs.  If the
     cached tag is no longer newer than local (operator just
     upgraded), it's cleared.

  Net result: the toolbar indicator lights up the moment a newer
  release is known about (cached or freshly checked) and stays
  visible until the operator upgrades.

---

## [0.0.8] — 2026-05-02 — "Quiet & Polish Pass"

Substantial DSP + UX upgrade on top of v0.0.7.  Three
operator-driven feature batches plus the post-v0.0.7 NR-stack
hardening that already landed on the dev branch:

  1. **Audio quiet pass** — eliminate the loud / random
     pops & clicks that the v0.0.7 audio chain produced.
  2. **Notch v2** — manual notches now actually kill carriers
     across their visible kill region instead of leaking 3 dB
     at the edges.  Operator-tunable depth + cascade + saved
     banks.
  3. **Click-to-tune v1** — Shift+click snaps the VFO to the
     nearest spectrum peak with a hover-preview reticle.
     Plain click + drag-to-pan unchanged.

### Changed — audio quiet pass

- **AGC per-sample envelope tracker** (was block-scalar).  Pre-
  fix, the AGC gain could change abruptly at every block boundary
  (~21 ms cadence) which produced sample-domain steps audible as
  loud pops on signal arrival and during release recovery.  Now
  tracks peak / hang / release per audio sample with the same
  operator-facing time constants — boundary discontinuity
  eliminated by construction.  Bench: boundary step 0.029 → 0.0041
  (= natural sine slope at AGC target).  CPU: ~0.11 ms/block,
  0.5% of the 21 ms budget.  See
  `docs/architecture/audio_pops_audit.md` §3 P0.1.
- **Decimator state preservation across freq/mode change.**  v0.0.7
  rebuilt the anti-alias FIR from zeros on every channel reset,
  producing a 1.35 ms ramp-up transient = audible click on every
  tune.  Fix: keep filter state across reset (rate-dependent
  coefficients are unchanged anyway).  Bench: tune-boundary step
  0.100 → 0.013 (= natural slope), recovery 1.35 ms → 0 ms.  See
  `audio_pops_audit.md` §3 P0.2.
- **AK4951 sink-swap fade-out.**  Switching audio output (or
  closing on shutdown) used to flip `inject_audio_tx` instantly,
  cutting EP2 audio bytes from real samples to zeros in one frame
  — click at the AK4951 codec.  Fix: 5 ms linear fade tail
  injected before the cut, then ~7 ms drain wait, then disable
  injection.  See `audio_pops_audit.md` §3 P0.3.

### Changed — notch v2

- **NotchFilter rewritten** as parametric peaking-EQ biquad (RBJ
  Audio EQ Cookbook) with operator-set depth_db parameter.
  Replaces `scipy.signal.iirnotch` whose kill region only achieved
  −3 dB at the visible width edges.  Default depth −50 dB.  Range
  −20 to −80 dB.
- **Cascade integer (1-4 stages) replaces deep:bool.**  Each stage
  gets `depth/cascade` so total at center matches operator's
  setting; more stages = sharper transition shoulders within the
  kill region AND faster fall-off outside.  Default cascade=2.
- **3-preset profile submenu (Normal / Deep / Surgical) on
  right-click.**  One-click to set both depth and cascade for an
  existing notch.  Parallel "Default profile for new notches"
  submenu sets what newly-placed notches start as.
- **Two-filter crossfade on coefficient swap.**  When operator
  drags a notch's freq / width / depth / cascade, the old filter
  and the new filter both run for 5 ms with their outputs
  linearly mixed.  Eliminates the drag-tick clicks that the
  pre-fix code produced on every parameter change.  Bench:
  boundary step ratio 0.98× of natural input slope = zero
  swap-induced transient.
- **`Notch` dataclass migrated**: `deep: bool` replaced with
  `depth_db: float` + `cascade: int`.  `n.deep` retained as a
  derived property (`cascade > 1`) for backward-compat with
  existing readers.  `notch_details` emits 6-tuples
  `(freq, width, active, deep, depth_db, cascade)`.

### Added — notch v2

- **Saved notch banks (operator-named presets).**  Right-click →
  Notch banks → Save current bank as... saves the current set of
  notches under a name ('My 40m setup', 'Contest weekend').
  Submenu lists saved banks for one-click restore; per-bank
  delete with confirm dialog.  Persists across Lyra restarts via
  QSettings under `notches/banks/<name>`.

### Added — click-to-tune v1

- **Shift+click → snap to nearest peak** when the peak is at
  least 6 dB above the rolling noise floor.  Sub-bin precision
  via parabolic peak interpolation.  Snap range scales with zoom
  (effective `max(200 Hz, 80 px × hz_per_px)`) so clicking within
  ~80 pixels of a peak snaps at any zoom level.  Falls through
  to literal click-to-tune when no qualifying peak is in the
  snap window.
- **Hover preview reticle.**  While Shift is held the panadapter
  shows a cyan vertical-tick + crosshair + Hz-offset label at
  the snap target position.  Operator sees where the next click
  will land before committing.  Disappears when no peak is in
  range.  Active on both QPainter and GPU panadapter backends.
- **Atomic press-time latch.**  Snap target is captured at
  press time (when Shift state and cursor position are both
  known) and committed unchanged on release.  Operator can
  release Shift any time before the mouse without losing the
  snap.  Same gesture model on both panadapter backends.
- **Drag-to-pan rate-limited to 30 Hz.**  Click+drag horizontally
  pans the band end-to-end; emits are throttled to 33 ms minimum
  gap with 1 Hz minimum freq delta so the HL2 C&C / notch /
  spectrum pipeline doesn't fall behind the cursor.

  *Operator UX flow:*
    * **Plain left-click** → literal tune to cursor freq.
    * **Shift + left-click** → snap to nearest spectrum peak.
    * **Left-click and drag** → pan the panadapter across a band.

### Changed — major NR audio improvements

- **NR2 voice quality dramatically improved.**  FFT_SIZE bumped
  256 → 1024 (hop 128 → 512).  Bin spacing went from 187.5 Hz to
  46.9 Hz at 48 kHz audio.  Voice formants now resolve cleanly
  where they previously smeared across 1-2 bins.  Internal
  latency rises 2.7 ms → 10.7 ms — well below audible threshold.
  NR1 stays at FFT=256 (handles its own capture pipeline);
  cross-size profile loads into NR2 transparently auto-resample.
- **Captured profile + NR2 mode no longer broken.**  Was
  mathematically incorrect since v0.0.6 — frozen captured profile
  defeated the decision-directed musical-noise damping in
  MMSE-LSA.  NR2 now takes a closed-form Wiener filter path when
  captured-source mode is on, mathematically optimal for
  known-noise-PSD scenarios.  Captured-source NR2 should now
  produce noticeably cleaner output than captured-source NR1.
- **DSP chain order corrected: LMS → ANF → SQ → NR → APF**
  (was ANF → SQ → LMS → NR).  ANF was stripping exactly the
  periodic content LMS was trying to predict; LMS+ANF together
  produced less effect than expected.  New order: LMS lifts
  periodic content, ANF cleans residual whistles, SQ gates
  silence, NR cleans broadband.  Operators using LMS+ANF
  together should hear a meaningful improvement.
- **LMS strength slider has actual perceptual swing now.**
  Previously controlled only adaptation parameters (transient
  behavior); steady-state output was nearly identical at any
  slider position.  Now drives FIVE parameters in concert: tap
  count (32→128), step size, leakage, AND wet/dry output mix.
  Bench-validated swing: ~10 dB residual-noise reduction
  difference between min and max on stable signals.

### Fixed

- **ANF + Squelch CPU bottleneck removed.**  Both had per-sample
  Python loops eating ~7.5 ms of every 10.7 ms audio block at
  48 kHz.  ANF rewritten as block-LMS (sub-block size = decorr
  delay = 10 samples); Squelch's RMS computation vectorized via
  cumsum.  Total chain CPU dropped from ~93% utilization to
  ~25-30% on a single thread.  Bench: ANF processes 1 sec audio
  in 55 ms (18× realtime); Squelch in 10 ms (102× realtime).
- **Smart-guard upgrade — catches contamination it used to
  miss.**  Legacy total-power-CV check passed CW/SSB
  contamination concentrated in just a few bins (because
  total-power averages across all bins).  Added per-bin variance
  anomaly check that catches single-bin contamination via
  median+MAD outlier detection.  Stable powerline harmonics still
  pass cleanly (low per-bin CV); intermittent signals flagged.
  6/6 correct on bench validation suite.
- **STFT buffer flush on capture begin.**  Subtle bug: leftover
  samples from a previous block could contaminate the first STFT
  frame of a new capture.  Operator-visible only when captures
  happened back-to-back without multi-second gaps.  Now flushed.
- **Captured profile FFT-bin-resampling on load.**  Profiles
  saved at any historical FFT_SIZE auto-resample to the loading
  processor's bin count via linear interpolation.  Unblocked the
  NR2 FFT_SIZE bump without invalidating saved profiles.

### Added

- **Captured-profile staleness notification.**  Every ~133 ms
  while a profile is loaded, Lyra computes a scale-invariant
  shape-distance between the live noise spectrum and the loaded
  profile.  When drift exceeds threshold (default 10 dB) for
  sustained period, status-bar toast: *"⚠ Noise profile drifted
  X dB from current band conditions — consider recapturing."*
  Hysteresis prevents spam (at most one fire per stale event,
  re-arm after band conditions stabilize).  Default ON; toggle
  via Settings → Noise.  Passive notification ONLY — operator
  decides whether to recapture.

### Documentation

- New `docs/architecture/audio_pops_audit.md` — pre-implementation
  audit ranking all audio-pop suspects with bench-test plan;
  three P0 fixes shipped, P1/P2 noted as future work.
- New `docs/architecture/notch_filter_audit.md` — first-pass
  audit of manual-notch shortcomings.
- New `docs/architecture/notch_v2_design.md` — senior-engineering
  deep-dive on notch math, WDSP architecture comparison, RBJ
  Cookbook biquad design, stability analysis, crossfade design,
  10 operator-locked decisions.
- New `docs/architecture/click_to_tune_plan.md` — design doc for
  snap-to-peak + drag-to-pan UX.
- Updated `docs/help/notches.md` — full v2 docs: depth, cascade,
  3-preset profiles, saved banks.
- Updated `docs/help/spectrum.md` — Shift+click snap-to-peak,
  hover reticle, drag-to-pan section.
- New `docs/architecture/nr_audit.md` — comprehensive NR-stack
  audit identifying what shipped, what was broken, and what
  could be improved.
- New `docs/architecture/implementation_playbook.md` —
  senior-engineering pass on RX2 / TX / PureSignal architecture
  for v0.0.8+ work.
- New `docs/architecture/v0.0.8_rx2_plan.md`,
  `rx2_research_notes.md`, `hl2_puresignal_audio_research.md` —
  RX2 and PureSignal planning docs.
- Updated `docs/help/nr.md` — current FFT sizes, two-layer
  smart-guard, Wiener-from-profile NR2 mode, staleness toast,
  full chain order.
- New `docs/help/lms.md` — dedicated LMS line-enhancer help with
  multi-parameter slider documentation.
- Updated `docs/help/anf.md` — current chain order (ANF after
  LMS, before SQ/NR).
- New `CLAUDE.md` — project context loaded by Claude across
  sessions; section 9.6 documents the v0.0.8 audio pop fixes
  + parked residual-click investigation.

### Decisions explicitly recorded

- **Auto-select feature deferred indefinitely.**  Earlier audit
  flagged "library auto-select" as a P1 feature; senior-engineering
  review and operator-led discussion concluded that captured
  profiles are operator-curated by design and Lyra shouldn't
  algorithmically override operator choice.  Recorded in
  `docs/architecture/nr_audit.md` §4.3(a) and `CLAUDE.md` §9.5.
- **WDSP-style FIR-integrated notches deferred to v0.1.**  Reading
  WDSP's `nbp.c` revealed manual notches are integrated into the
  demod's bandpass FIR (single FIR convolution does both bandpass
  and notches).  Mathematically superior to per-notch IIR but
  requires demod refactor + has RX2 implications.  Out of
  v0.0.8 scope; `notch_v2_design.md` §2.2 has the full
  reasoning.
- **Notch presets: Scope A only (operator-named banks).**  Scope
  B (band-aware auto-load) considered and rejected — operators
  prefer to choose which bank to load rather than have Lyra
  guess based on the tuned frequency.
- **Residual audio-click investigation parked.**  After the three
  P0 audio fixes shipped, operator flight-tested as "noticeably
  better, loud spikes gone, but occasional smaller pops remain
  even with all DSP off into a dummy load."  Diagnosis points at
  HL2 hardware/gateware glitches, Python GIL/GC pauses, or
  per-sample AGC + Rayleigh tail edge cases.  Diagnostic
  instrumentation already in place (`LYRA_AUDIO_DEBUG=1` env var
  enables a step-event logger in `_apply_agc_and_volume`).  Will
  pick up later with operator-collected log data.  See
  `CLAUDE.md` §9.6.

---

## [0.0.7] — "Polish Pass" — 2026-05-01

A focused tester-feedback release.  No new DSP or radio features — every
change is an operator-visible UI fix from feedback on the v0.0.6 install.

Drop-in over v0.0.6: same QSettings, same captured noise-profile format,
same dock layout, same license, same minimum Windows.

### Changed — UI polish

- **Three-column Noise Settings tab, rebalanced.**  Was two columns in
  v0.0.5, became three early in v0.0.6.x; testers flagged that the middle
  column (NB + ANF + LMS + Squelch) was driving the page height.  Now:
  `Cap + Squelch | NB + ANF | NR2 + Method + LMS` — even weight, no
  scrolling at 1080p.
- **Brighter checkboxes / radio buttons.**  Tick-box borders use the
  dusty-blue `TEXT_MUTED` color against the dark recess instead of the
  near-invisible `BORDER` tone.  Bumped 14 → 16 px for visual weight.
- **Global font 10pt → 11pt** for readability on dense Settings tabs.
- **Tuning panel: vertical breathing room** — added 10 px between the
  freq-display row and the MHz / Step controls (was clipping).
- **DSP+Audio panel: AGC + notch readouts now fixed-height** like the
  buttons next to them (was stretching when the panel grew).

### Fixed

- **Tuning panel vertical resize.**  Operator feedback: *"I can widen
  Tuning but not change its height."*  Root cause: `FrequencyDisplay`
  shipped with `QSizePolicy.Fixed` vertical, which made Qt's row-layout
  refuse extra height.  Fixed by overriding the freq-display vertical
  policy to Preferred and giving the panel an explicit
  `MinimumExpanding` policy with a 180 px floor.
- **Lock panels actually locks all panels.**  v0.0.6 implementation
  disabled splitter handles but missed the QMainWindow internal
  dock-area separator.  Third lock layer added (per-dock `setFixedSize`
  pin); gated so unlock side-effects only fire when transitioning out of
  a real lock state.
- **Update notifications: pre-release + full-release parity.**  The
  silent update checker was hitting GitHub's `/releases/latest`
  endpoint, which by design hides pre-releases.  Switched to `/releases`
  and pick the highest semver tag ourselves.  Testers on a pre-release
  now get notified of newer pre-releases AND any subsequent full
  release.
- **Self-compile device-list errors.**  DSP+Audio device dropdown now
  distinguishes "sounddevice not installed" vs "PortAudio failed to
  load" vs "no devices reported by Windows" with copy-pasteable
  `pip install` hints.

### Added

- **Toolbar update indicator.**  When an update is available, a small
  orange "🆕 vX.Y.Z available" pill appears centered between the clocks
  and the HL2 telemetry block on the header toolbar.  Click to open
  Help → Check for Updates.

### Removed

- **Neural NR exploration code (~1,100 lines).**  `onnxruntime` /
  DeepFilterNet integration removed; the menu entry stays as a
  `(deferred — pending RX2 + TX)` placeholder.  WDSP-derived NR1, NR2,
  NR3 (LMS), ANF, NB, and Squelch are unchanged from v0.0.6.

---

## [0.0.6] — "Operator Awareness" — 2026-04-26

The deepest DSP refresh since the 0.0.x series began plus the
introduction of all-station awareness features.  Five WDSP modules
ported with proper attribution under Lyra's new GPL v3+ license,
all-mode squelch, LMS adaptive line enhancer, full Martin-statistics
upgrade for NR2, and built-in weather alerts (lightning + high wind).

### Headlines

- **Five WDSP modules ported with attribution.**  Lyra is now GPL v3+
  (since v0.0.6 — see `NOTICE.md`); this release exercises the
  license-compatibility opening with substantial work pulled from
  Warren Pratt's WDSP.
- **All-mode squelch (SSQL-style).**  Voice-presence detector that
  works on SSB, AM, FM, and CW.  Mutes between transmissions on any
  modulation type.
- **LMS adaptive line enhancer (NR3).**  Pulls weak CW out of broadband
  hiss — the algorithmic class WDSP calls "ANR".  Block-LMS optimization
  drops CPU to ~4% real-time.
- **Weather Alerts.**  Toolbar indicator + desktop toast for lightning
  and high-wind conditions.  Sources: Blitzortung, NWS, Ambient WS-2000,
  Ecowitt — pulled from the author's sister projects (WX-Dashboard,
  SDRLogger+).
- **NR2 deep upgrades.**  Added Martin (2001) minimum-statistics noise
  PSD, speech-presence-probability soft mask (witchHat), AEPF
  median-smoothing post-filter, and a Wiener-vs-MMSE-LSA gain-function
  selector.  Replaces v0.0.5 simplified Ephraim-Malah with the full
  WDSP-equivalent stack.
- **Operator/Station globals.**  Callsign + Maidenhead grid square +
  manual lat/lon backup live in Radio Settings, consumed by TCI spots,
  weather alerts, and any future feature that needs station location.

### DSP

#### Noise Reduction

- **NR1 (spectral subtraction)** — replaced dead-on-arrival VAD-gated
  noise tracker with min-statistics (Martin 2001).  Continuous-strength
  slider (0–100) replaces the old Light/Medium/Heavy radio buttons.
- **NR2 (Ephraim-Malah)** — Martin minimum-statistics noise PSD, AEPF
  post-filter, speech-presence probability soft mask, and a runtime
  Wiener-vs-MMSE-LSA gain-function picker (right-click the NR2 strength
  slider).
- **NR1 + NR2** captured-noise profiles work with the full new stack;
  Martin tracker still runs in the background as live-mode fallback.

#### LMS Line Enhancer (new)

- Pratt-style normalized LMS with adaptive leakage — port from WDSP
  `anr.c` with attribution.
- Slots ANF → LMS → NR in the audio chain, independent enable.
- Strength slider and right-click presets on the DSP+Audio panel.
- Block-LMS optimization (block size = decorrelation delay) gives ~5×
  speedup at zero quality loss.

#### All-Mode Squelch (new)

- RMS + auto-tracked noise floor with hysteresis.  Replaces the initial
  WDSP-SSQL FTOV port after on-air testing showed the zero-crossing
  detector mis-classified stable harmonics.
- Per-condition hang time bridges natural speech pauses without closing
  the gate mid-syllable.
- Floor frozen during gate-open so long transmissions don't drag the
  threshold up.
- SQ button on the DSP+Audio panel; threshold slider + activity dot
  appear when enabled.

### Weather Alerts (new)

Three toolbar indicators between the ADC RMS readout and the clocks:

- ⚡ Lightning — closest strike + distance + bearing, color-coded by
  proximity (yellow > 25 mi, orange < 25 mi, red < 10 mi)
- 💨 Wind — sustained / gust speed, three severity tiers
- ⚠ NWS severe weather warning (red, hidden when no warning active)

Indicators auto-hide on quiet days.  Desktop toasts fire on tier-
crossing events with 15-minute hysteresis to prevent spam.  Operator-
selectable sources: Blitzortung, NWS, Ambient Weather PWS, Ecowitt PWS.
Disclaimer-gated — alerts are informational only, not a safety system.
Settings → Weather (last tab in the dialog).

### UX

- **Operator/Station group** in Radio settings (callsign + grid square +
  manual lat/lon).  Migrates the older TCI-only callsign field on first
  run.
- **Two-column layouts** for the Noise and Weather settings tabs
  (mirroring what Visuals already did).  Cuts vertical scrolling roughly
  in half.
- **NR2 strength slider** range expanded from 0–150 to 0–200 — the new
  WDSP-port machinery (Martin + SPP + AEPF) makes the higher range
  listenable without speech distortion.

### License

Lyra v0.0.6 onward is **GNU General Public License v3 or later** (was
MIT for v0.0.5 and earlier).  The relicense was made specifically to
enable WDSP integration — see `NOTICE.md` for the full attribution and
license history.

### Attribution

Modules with WDSP-derived algorithm content:

- `lyra/dsp/lms.py` — port of `anr.c` (Pratt 2012, 2013)
- `lyra/dsp/nr2.py` — Martin minimum-statistics + AEPF + SPP + Wiener
  gain LUT, all derived from `emnr.c` (Pratt 2015, 2025)

Modules ported from sister projects (also Lyra-author):

- `lyra/wx/sources/blitzortung.py` — from SDRLogger+
- `lyra/wx/sources/nws.py` — from SDRLogger+
- `lyra/wx/sources/ambient.py` — from SDRLogger+ + WX-Dashboard
- `lyra/wx/sources/ecowitt.py` — from SDRLogger+

The captured-noise-profile workflow remains a Lyra original.

### Compatibility

- **Minimum Windows: build 17763 (1809, October 2018) or later.**  This
  is the official baseline for WinRT modern toast notifications
  (weather alerts) and PySide6 6.5+.  Older Windows installs are
  rejected by the installer with a clear error message.

### Neural NR — deferred

AI-based neural noise reduction is on the roadmap but **deferred until
after RX2 + TX work lands**.  v0.0.6 dev briefly explored PyTorch /
DeepFilterNet and onnxruntime / NSNet2 paths; both are viable but
introduce dependency-management friction (Python-version lag, Rust
toolchain requirements, model-file distribution) that's better tackled
when the broader transceiver functionality is in place.  The "Neural"
entry stays in the right-click NR backend menu as a `(deferred —
pending RX2 + TX)` placeholder.

### Known issues

- Weather Alerts: API credentials are stored unencrypted in QSettings
  (Windows registry).  Will move to OS-keyring in a future release.
- LMS line enhancer is most effective on steady-tone signals (CW); on
  SSB voice the effect is subtle.

---

## [0.0.5] — "Listening Tools" — 2026-03

A meaningful audio + panadapter release.  Two new CW DSP tools, full
GPU panadapter feature parity, an audio chain rebuild that fixes
several stability issues, and an auto-update check so testers don't
get stranded on old builds.

### Added

- **APF — Audio Peaking Filter (CW)** — narrow peaking biquad centered
  on your CW pitch.  Boosts weak CW above the noise floor without the
  ringing tail of a brick-wall filter.  Right-click the APF button for
  BW / Gain quick presets.
- **BIN — Binaural pseudo-stereo (headphones)** — Hilbert phase split
  for spatial CW perception and SSB voice widening.  Adjustable depth
  0–100%, equal-loudness normalized.
- **GPU panadapter — full feature parity (BETA).**  Everything the
  QPainter widget does, now on the GPU: band plan, peak markers, spots,
  notches, click-to-tune, Y-axis drag, wheel zoom, RX-BW edge drag,
  passband overlay, noise floor, VFO marker, CW Zero, grid toggle.
  Opt-in via Settings → Visuals → Graphics backend → GPU panadapter
  (beta).
- **Auto-update check on startup** — silent background check; shows
  status-bar message + Help menu badge when a newer release is
  available.

### Changed — audio chain rebuild

- AGC profiles recalibrated — Fast / Med / Slow time constants were
  ~20× too slow.  Now match standard SDR-client conventions.
- AGC OFF audibility fixed — was 14 dB quieter than AGC ON, now level.
- Mode = Tone hang fixed (was producing wrong-rate samples).
- Audio output rate-sticky bug fixed (AK4951 ↔ PC Soundcard switching
  could lock the audio path).
- WWV ↔ FT8 stuck audio fixed (round-robin C&C keepalive at the
  protocol layer).

### Changed — S-meter overhaul

- LNA-invariant readings — moving the LNA slider no longer changes the
  meter.  dBm display now reflects actual antenna signal level.
- Auto-LNA pull-up (opt-in) — Auto button can now also raise gain on
  sustained-quiet bands, with a two-tier ceiling to stay out of the
  IMD zone.

### Changed — Quality of life

- Spot prefixes now show plain-text 2-letter ISO codes (e.g. `US N8SDR`,
  `JA JA1XYZ`) — replaces regional-indicator emoji flags that Windows
  can't render.
- Settings → DSP → CW group consolidates pitch, APF, and BIN controls.

---

## [0.0.4] — "Discovery & Scale Polish"

### Changed

- **Auto-scale = clamp, not disable** — dragging the dB-range scale on
  the spectrum no longer turns auto-scale OFF.  Manual range becomes
  the BOUNDS that auto-scale stays inside.

### Added

- **Per-band scale memory** — each band remembers its own scale bounds,
  with sensible factory defaults (160 m bottom-heavy, 6 m top-heavy) so
  band-swapping just works.
- **Help → Network Discovery Probe** — diagnostic dialog with
  per-interface probes and a copy-to-clipboard log.

### Fixed

- **Multi-NIC discovery** — auto-discover broadcasts on every local
  network interface in parallel.  Fixes the "tester with Wi-Fi +
  Ethernet couldn't find the HL2" failure mode.
- **OpenGL upgrade nag** — fixed timing so the suggestion popup isn't
  hidden behind the main window on slow boots.

---

## [0.0.3] — "First Tester Build"

The first packaged installer release.  Notable additions since 0.0.2:

### Added

- **True dBFS spectrum calibration** — FFT math fixed so 0 dBFS is a
  full-scale tone; per-rig cal trim slider for known path losses.
- **S-meter cal + Peak/Average response mode** — right-click meter for
  one-click "Calibrate to S9 (-73 dBm)" + steady time-averaged reading.
- **Lit-Arc meter widget** — segmented arc-bar meter with no needle
  (less jittery than analog dial), three modes (S / dBm / AGC).
- **Top-banner toolbar** — large local + UTC clocks, live HL2 hardware
  telemetry (T / V), CPU% (matches Task Manager), GPU% (NVIDIA via NVML
  or any vendor via Win32 PDH).
- **Settings backup / import / export + auto-snapshots** — JSON
  snapshot of every preference taken on each launch, last 10 kept;
  one-click rollback via File → Snapshots.
- **Layout safeguards** — Lock Panels (Ctrl+L), always-factory Reset
  Panel Layout, sanity check refusing to save degenerate layouts on
  close.
- **Click-and-drag spectrum tuning** — pan the panadapter like a Google
  Maps view.
- **Fine-zoom slider** + click-the-scale-label gestures.
- **Stereo balance slider** with center detentation, working on both PC
  Soundcard and AK4951 outputs.
- **HL2 Telemetry Probe** dialog under Help — diagnose firmware-variant
  decode mismatches against your specific HL2.

Plus extensive performance work to eliminate spectrum/waterfall stutter
(slider debounce, hidden meter timer pause, waterfall bilinear
smoothing, spectrum FPS press/release pattern).
