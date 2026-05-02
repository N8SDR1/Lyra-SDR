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
