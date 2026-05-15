# CLAUDE.md — Lyra-SDR project notes for Claude

This file is auto-loaded into Claude's context when working in the
Lyra-SDR repo.  It captures the core logic, key decisions, and
workflow rules so we don't have to re-research from scratch each
session.  Keep it concise — long-form research lives in
`docs/architecture/`.

When in doubt, consult:
- `docs/architecture/implementation_playbook.md` — current authoritative
  spec for RX2 / TX / PureSignal (v0.0.9 / v0.1 / v0.2).
- **`docs/architecture/v0.1_rx2_consensus_plan.md`** — AUTHORITATIVE
  consensus plan from the 2026-05-05 three-engineer review (protocol,
  DSP, UI) with two rounds of cross-validation.  **Open this FIRST**
  for any v0.1 / v0.2 / v0.3 implementation question.  Supersedes
  `v0.0.9_rx2_plan.md` and `rx2_research_notes.md`.  Contains: locked
  channel ID convention, phase-by-phase plan, mandatory bench-test
  gates for v0.3 PS, errors corrected from round 1 to round 2,
  patterns Lyra adopts vs deliberate Thetis divergences.
- `docs/architecture/v0.0.9_rx2_plan.md` — superseded.  Historical.
- `docs/architecture/hl2_puresignal_audio_research.md` — HL2-specific
  PureSignal + audio chain research.
- `docs/architecture/rx2_research_notes.md` — superseded by the
  consensus plan above.  Historical first-pass research.

**Version-numbering note (2026-05-03):** version numbering has
shifted twice during the v0.0.7 → v0.0.9 cycle.  Current state:

- **v0.0.8** "Quiet & Polish Pass" — audio quiet pass + notch v2
  + click-to-tune (shipped 2026-05-02).
- **v0.0.8.1** — auto-update notification fix (2026-05-02).
- **v0.0.9** "Memory & Stations" — operator-driven pre-RX2
  polish: TIME button, GEN customization, Memory bank, EiBi
  shortwave broadcaster overlay (shipped 2026-05-02).
- **v0.1**   = RX2 (was originally v0.0.9; shifted when the
  Memory & Stations batch landed).
- **v0.2**   = TX.
- **v0.3**   = PureSignal.
- **v0.4**   = Multi-radio refactor + Protocol 2 + ANAN family
  (operator decision 2026-05-03; see §7 + §6.7).

References to the old numbering in commit history / older doc
revisions are historical and intentionally not back-edited.  Doc
content below has been mass-renumbered to the new scheme.

**Subsequent releases (2026-05-05 onward):**

- **v0.1.1** "Polish & Audio Routing" (2026-05-14) — polish
  batch on top of v0.1.0 GA per the §15.16 scope lock.  Five
  items shipped together: (1) RIT (Receiver Incremental Tuning)
  on the TUNING panel — click toggles, Shift+click zeros and
  disables, inline `[−] value [+]` StepperReadout (§15.17 idiom)
  for offset adjustment that materializes when RIT is on,
  ±9999 Hz range, 1 Hz click / 10 Hz Shift+click / right-click
  for typed entry, persists across sessions, RX1 only (per-RX
  deferred); (2) TCI RX2 channel — `channel_count:2;` advertised,
  `DDS:1` / `VFO:0,1` / `IF:0,1` / `MODULATION:1` route to RX2,
  outbound rx2_freq + mode_changed_rx2 signals broadcast as ch1
  updates, SDRLogger+ (and any TCI client) can drive both VFOs;
  (3) device-list grouping by host API on Settings → Audio (same
  physical device once per WASAPI / WDM-KS / DirectSound / MME /
  ASIO with section dividers); (4) VAC digital-modes workflow doc
  (`docs/help/audio.md`); (5) WASAPI Exclusive mode — audit
  closure (already shipped in v0.0.9.6, was mistakenly logged as
  parked).  Also bundles: TCI broadcast rate-limit key fix
  (per-(command, channel) instead of per-command — RX1/RX2
  updates no longer starve each other), Auto-LNA tooltip
  refresh for pull-up reality.  Three-push sequence: feature
  branch + tag + main fast-forward.  Test count: 225/225 green
  + 11 new TCI RX2 routing assertions + 6 RIT-math assertions +
  UI bench validation.  XIT (TX-mirror RIT) renders disabled in
  v0.1.1, ships in v0.2 (~2 hr enable on top of RIT plumbing).
  v0.1.1 ALSO ships the §15.17 stepper redesign that landed on
  the feature branch between v0.1.0 GA and v0.1.1 (DSP+Audio top
  row: three QSlider replaced by `[−] value [+]` StepperReadout
  widgets for Vol RX1 / Vol RX2 / AF Gain; "Out" picker relocated
  from header to levels row as icon-only QToolButton; cyan QSS
  for stepper buttons matching panel accent palette).  Original
  §15.17 plan was to hold for v0.1.2; rolled into v0.1.1 because
  RIT's offset stepper reuses the same widget, making §15.17 a
  hard dependency for v0.1.1 release.  v0.2 TX bring-up is next.

- **v0.1.0** "RX2 Dual Receiver — production GA"
  (2026-05-14) — production GA of the v0.1 line after the
  pre2/pre3 tester flight (Brent + Timmy + N8SDR).  All pre2
  RX2 work + pre3 audio-path latency win (PC Soundcard ear-
  lag 434 → 172 ms via §15.7 rmatch ring + HL2 TX-latency
  register cuts) + pre3 doc refresh ship in this release.
  GA-specific finishing touches: diagnostic overlay 3-state
  toggle (Settings → Radio → Full/Minimal/Off) per §15.11,
  "Show HL2 telemetry on toolbar" Settings checkbox (separate
  from the 3-state because HL2 T/V is genuinely useful during
  TX), QToolBar QWidgetAction-aware slot collapse fix
  (capturing the action returned by ``addWidget()`` so hiding
  ADC / HL2 / CPU chips actually removes the slot instead of
  leaving a gap), robust ``_find_main_window()`` lookup
  sidestepping the QTabWidget reparenting that quietly broke
  live-apply in earlier attempts.  Three-push release sequence
  hit clean for the first time since v0.0.9.5 (feature branch
  + tag + main fast-forward — the step v0.0.9.6 through
  v0.0.9.9 each skipped).  Test count: 225/225 green incl.
  Phase 0 RX1 byte-identical null gate.  v0.1.1 scope locked
  per §15.16 (RIT + TCI RX2 + WASAPI Exclusive + VAC doc +
  host-API grouping bundled); v0.2 TX bring-up waits behind
  that polish release.

- **v0.1.0-pre3** "RX2 Dual Receiver — latency + polish"
  (2026-05-13) — second tester pre-release.  Headline: §15.7
  audio-path latency investigation closed.  rmatch ring
  default 400 → 150 ms, HL2 TX-latency register 40 → 15 ms,
  net −275 ms PC Soundcard ear-lag while holding clean under
  heavy DSP load (NR Mode 4 + LMS + AGC Fast on voice peaks).
  Env-var overrides retained (``LYRA_RMATCH_RING_MS``,
  ``LYRA_HL2_TXLATENCY_MS``, ``LYRA_TIMING_DEBUG``).  Triple-
  agent documentation audit: README + audio/shortcuts/rx2/
  spectrum/troubleshooting help docs refreshed for v0.1
  reality; install guide regenerated with GPL v3+ posture +
  ``pip install -r requirements.txt`` command.  Middle-click
  panadapter focus swap actually wired (pre2 advertised it
  but the handler was never plumbed).  Tester Timmy Davis
  (KC8TYK) credited as co-contributor across CONTRIBUTORS /
  NOTICE / README / in-app help.  MultiMeterPanel Analog
  style retired (Lit-Arc + LED-bar only).

- **v0.1.0-pre2** "RX2 Dual Receiver — tester pre-release"
  (2026-05-12) — first v0.1 line pre-release.  Lands the full
  RX2 dual-receiver feature stack: second receiver on HL2
  DDC1, stereo-split audio routing (SUB toggle), focus model
  with green-border indicator, per-VFO Step + Mode combos,
  inter-VFO bridge buttons (1→2 / 2→1 / ⇄), CW Pitch moved
  from MODE+FILTER to TUNING panel, panadapter follows
  focused RX (click-tune, wheel-tune, Exact/100Hz, marker,
  passband overlay, CW pitch offset all RX-aware), GEN slot
  owner tracking, full QSettings persistence for every RX2
  state field.  Also bundles two propagation-panel fixes
  surfaced by tester Timmy's blank panel: SSL cert verify
  skipped to match SDRLogger+'s posture (which works on the
  same machine where Lyra failed), and ``print()`` redirected
  to ``crash.log`` on the PyInstaller windowed build so
  diagnostic output is no longer silently dropped to a None
  ``sys.stderr``.  Test count: 225/225 green incl. Phase 0
  RX1 byte-identical null gate.  Tester pre-release for
  Brent + Timmy + N8SDR bench flight; production v0.1.0
  follows operator confirmation on real-band sessions.


- **v0.0.9.4** "Polish & Notifications" — watermark bundling fix,
  first-time-per-version update modal, toolbar pulse, Settings
  dialog lambda crash fixes.
- **v0.0.9.5** "Captured-Profile UX" — smart-guard removed (false
  positives + false negatives in field testing), tunable staleness
  threshold, live drift readout in profile manager, TCI server +
  profile manager dialog stability fixes.
- **v0.0.9.6** "Audio Foundation" (shipped 2026-05-08) — wholesale
  pivot from pure-Python DSP to cffi calls into the WDSP DSP engine
  for the RX1 audio chain.  Per-sample numpy work in legacy modules
  (agc_wdsp / nr / nr2 / anf / demod / channel) was producing GIL
  contention with the EP2 writer thread, manifesting as HL2
  audio-jack clicks and PC Soundcard motorboating.  WDSP DLLs
  bundled at `lyra/dsp/_native/`.  Cleanup arc retired ~6,800
  lines of legacy DSP code (Audio Leveler, agc_wdsp, apf, demod,
  nb, lms, anf, squelch, nr2, PythonRxChannel.process, etc.).
  See §13 (audio architecture), §14 (WDSP-DLL integration), §14.9
  (cleanup arc).
- **v0.0.9.9.1** "Launch Hotfix" (2026-05-10) — emergency
  patch over v0.0.9.9.  Two fixes:
  (1) ``faulthandler.enable()`` added in v0.0.9.9 raised
  ``RuntimeError: sys.stderr is None`` at import time on the
  PyInstaller ``--windowed`` build (Lyra.exe ships with
  ``console=False``, so sys.stderr is None).  Anyone who
  downloaded v0.0.9.9 from GitHub couldn't launch Lyra.
  Bench didn't catch it because source-tree runs have a real
  stderr.  Fix routes crash output to
  ``%APPDATA%\Lyra\crash.log`` (operator-visible artifact for
  bug reports); falls back to sys.stderr if the file can't be
  opened; silent no-op if neither works (won't crash launch).
  (2) Brent reported EiBi overlay missing on Software /
  OpenGL graphics backends.  The QPainter SpectrumPanel setup
  was missing the four-signal EiBi wiring block that the GPU
  SpectrumPanel had — the renderer was correct, but
  ``_refresh_eibi_overlay`` was never connected so
  ``set_eibi_entries(...)`` was never called.  Fix mirrors the
  GPU section's wiring into ``_setup_qpainter_panadapter``;
  operator confirmed working on all three backends.  v0.0.9.9
  GitHub release retracted after v0.0.9.9.1 publishes.
- **v0.0.9.9** "IQ Captured Profiles" (2026-05-10) — §14.6
  IQ-domain captured-profile rebuild lands LIVE.  Replaces the
  v0.0.9.6-era "capture works, apply is INERT in WDSP mode"
  state with a full pre-WDSP IQ-domain pipeline: capture taps
  raw IQ at the operator's native rate, apply runs Wiener-from-
  profile spectral subtraction on raw IQ before WDSP's RXA
  chain.  Sidesteps the AGC-mismatch that broke three rounds
  of post-WDSP audio-domain attempts.  Schema bump to v2
  (rate-specific full complex-FFT magnitudes); v1 audio-domain
  profiles refused on load with recapture hint.  New STFT
  engine (sqrt-Hann WOLA, COLA-1 exact, Wiener gain with
  temporal smoothing) in ``lyra/dsp/captured_profile_iq.py``;
  10/10 synthetic-bench validation.  Phase 5 UX: Switch
  profile right-click submenu (single-click reload), gain
  smoothing slider (live-tunable, default γ=0.6 ~10 ms time
  constant), FFT size dropdown (1024/2048/4096), badge
  tooltip refresh.  Crash fix: ``_iq_capture_lock`` extended
  to cover WDSP close+null and worker's ``_wdsp_rx.process()``
  call, closing the TOCTOU race that produced silent crashes
  on rapid rate-change cycles.  ``faulthandler.enable()``
  added permanently for general crash forensics.  Operator
  field-tested through 3+ rate cycles with captured profile;
  watery character "light, becomes inaudible after a minute"
  per operator with γ=0.6 default.
- **v0.0.9.8.1** "AGC + persistence patch" (2026-05-10) —
  substantial bug-fix patch over v0.0.9.8.  Headline: a
  latent ``SetRXAAGCSlope`` cffi binding bug from v0.0.9.6
  was caught by an audit of every cffi binding's parameter
  types vs. the WDSP C source — only one mismatch found
  (the binding declared ``double slope`` while the C
  function is ``int slope``, producing a register-class
  calling-convention bug on Windows x86_64 → garbage
  ``var_gain`` → ``max_gain`` pinned at random value →
  AGC profile time constants masked).  Fix made
  AGC profiles audibly distinct for the first time since
  v0.0.9.6.  Plus per-band waterfall + spectrum scale
  persistence repair (apply_current_band_range public
  method + spectrum autoload from_user=False + auto-scale
  waterfall protection); per-mode RX bandwidth
  persistence (was never saved/loaded); AGC threshold UX
  modernization (legacy 0..1 linear field repurposed as
  dBFS, Settings slider replaced by label + Auto button,
  Auto reads live noise floor); AGC slope default 0 → 35
  (industry soft-knee convention); ``Long`` AGC profile
  restored to UI menu; click-to-tune snap polish (SNR
  threshold 6→8 dB, 2 kHz effective-range cap); CLAUDE.md
  §15.1/§15.5 closed, §9.8 withdrawn.
- **v0.0.9.8** "Display Polish" (CW VFO convention switch,
  2026-05-10) — operator-visible behaviour change for CW
  operators: the VFO LED now shows the **carrier frequency**
  of the tuned signal, matching the standard convention used
  across major HF SDR applications.  This replaces the v0.0.9.7.x
  filter-zero convention where the LED showed (carrier − pitch)
  for CWU and various tuning surfaces (click-to-tune, NCDXF
  marker click, NCDXF auto-follow, TCI spot click) each had to
  apply the CW pitch offset themselves.  v0.0.9.8 puts the
  offset CENTRALLY in radio.py (``_compute_dds_freq_hz`` helper
  called by ``set_freq_hz`` / ``set_mode`` / ``set_cw_pitch_hz``)
  so every freq write to the protocol layer is automatically
  offset for CW; all per-call-site offsets are reverted.  The
  spectrum widget receives DDS as its center_hz, and a new
  ``marker_offset_hz`` (= VFO − DDS) shifts the orange marker
  line to the operator's tuned carrier — visually right of
  center for CWU, left for CWL, at center for non-CW.  CW Zero
  white reference line removed (redundant under new
  convention).  v0.0.9.7.2 was committed and tagged but NOT
  released to GitHub — its TCI-spot per-call-site fix was
  superseded by this convention switch.  Saved CW freqs from
  v0.0.9.7.x will display ``pitch`` Hz off until retuned once;
  no auto-migration (operators in active testing retune
  naturally).
- **v0.0.9.7.2** "Display Polish" (TCI CW spot tuning fix,
  2026-05-10) — patch over v0.0.9.7.1.  Companion to the NCDXF
  fix; same class of issue, different tuning surface.  TCI CW
  spots forwarded by SDRLogger+ (and every cluster / RBN /
  Skimmer source it upstreams from) carry the **carrier**
  frequency — clicking them in CWU/CWL previously landed at
  zero-beat.  ``radio.activate_spot_near`` now subtracts pitch
  for CWU / bare "CW" spots and adds pitch for CWL spots; non-
  CW spots untouched.  ``spot_activated`` signal still emits
  the original carrier freq so TCI round-trip is preserved.
  Verified SDRLogger+ source at ``Y:/Claude local/hamlog/
  main.py:3808+`` does no mode-aware adjustment — passes
  upstream cluster freq through unchanged, locking the
  carrier-freq convention between the two sibling apps.
  Convention documented in `docs/help/tci.md`.
- **v0.0.9.7.1** "Display Polish" (NCDXF tuning fix, 2026-05-09)
  — patch over v0.0.9.7.  Bug fix only: NCDXF beacon tuning
  (marker click + auto-follow) now applies the CW pitch offset,
  so the operator hears the beacon at their configured pitch
  tone instead of zero-beat.  Two surgical edits to
  `_on_landmark_clicked` (panels.py) and `_ncdxf_follow_pump`
  (radio.py) following the same offset pattern `_on_click`
  already used for plain click-to-tune.
- **v0.0.9.7** "Display Polish" (2026-05-09) — operator-driven UX
  polish on spectrum/waterfall/Display panel surfaces, plus
  Settings dialog stability hardening.  New operator-facing
  features: Peak Hold combo (Off/Live/timed/Hold) + Decay
  (Fast/Med/Slow) + Clear button on the Display panel; Exact /
  100 Hz tuning quantization toggle; Spec/WF zoom slider
  live-preview during drag; spectrum trace fill master toggle +
  custom fill color picker; waterfall collapse toggle; per-band
  waterfall min/max persistence (sister to per-band spectrum
  bounds).  Bug fixes: dB-lock recall on restart, Settings dialog
  dead-widget guards (`_safe_mirror`, `_swallow_dead_widget`),
  wrapped-label squeeze fix on Noise + Visuals tabs, dialog size
  bump 1100×760 → 1280×880, custom-color button width 120 → 140
  px.  Documentation pass aligned help docs with the NR-mode UX
  overhaul + AGC profile + ANF profile-name corrections from the
  v0.0.9.6 cleanup arc.  See §15 for the residual doc backlog
  parked for future cleanup.

---

## 1. Project at a glance

**Lyra-SDR** is a Qt6 / PySide6 desktop SDR transceiver for the Hermes
Lite 2 / 2+, written in Python.  Native HPSDR Protocol 1.

- **Target hardware (current, v0.0.x → v0.3)**: Hermes Lite 2 / 2+
  ONLY.  Don't add ANAN / Orion / Hermes / Hermes II code paths
  during this phase — but **do** write hardware-agnostic code
  wherever feasible (see §6.7).
- **Future hardware support (v0.4)**: Protocol 2 + ANAN family
  (G2 / G2-1K / 7000DLE / 8000) is on the long-term roadmap per
  operator decision 2026-05-03.  v0.1 / v0.2 / v0.3 stay
  HL2-only by scope, but the hardware-abstraction discipline in
  §6.7 prevents painting into a corner.
- **Author**: Rick Langford (N8SDR).  Memory note: nearby AM
  broadcaster causes 5th-harmonic interference on 7.250 MHz; factors
  into AGC / NR / notch defaults.
- **Audio testing methodology (operator note 2026-05-06):** Rick
  has very good hearing/ears.  For RX-side audio A/B he runs
  Windows with **all "audio enhancements" disabled** (no loudness
  equalization, bass boost, virtual surround, etc.) — no Windows-
  side coloration.  His standard is "what's produced is what
  should be heard naturally."  When he reports coloration on a
  Lyra audio path, it's coming from Lyra (or the audio device
  itself), NOT from Windows enhancements.  Important context for
  audio-quality investigations: don't reach for "check your
  Windows enhancements" as a first hypothesis.
- **License**: GPL v3+ (since v0.0.6).  Was MIT through v0.0.5.
  Relicensed specifically to enable WDSP-derived code integration.
  See `NOTICE.md`.
- **Repo**: <https://github.com/N8SDR1/Lyra-SDR>.  Branches: `main`
  is the published release branch; `feature/threaded-dsp` is the
  active development trunk (kept fast-forward-able with `main`).
- **Current version**: see `lyra/__init__.py` for the canonical
  ``__version__`` + ``__version_name__`` strings.  The
  version-numbering history near the top of this file lists the
  delivered releases through to today.  Bump in one place
  (``__init__.py`` + ``build/installer.iss``); everything else —
  About dialog, status bar, installer filename, GitHub release
  tag — follows.

## 2. License posture for WDSP ports

WDSP (by Warren Pratt NR0V, GPL v3+) is the openHPSDR DSP engine.
**Lyra is GPL-compatible with WDSP.**  Implications:

- We **may** port WDSP source directly into Lyra (Python or C
  extension).  Always include attribution comment with file path +
  line numbers.  See `docs/architecture/wdsp_integration.md` for the
  attribution template.
- We **may not** copy from Thetis's C# `Console\` code or
  `ChannelMaster\` C code — that's protocol/UI glue we should write
  Lyra-native, modeled on the pattern but not character-for-character.
- The line: WDSP DSP algorithms = port directly with attribution.
  Everything else = study the pattern, then write Lyra-native.

Already-ported WDSP modules in Lyra:
- `lyra/dsp/nr.py` (NR1 — spectral subtraction with Martin
  minimum-statistics, derived from `wdsp/anr.c` + `wdsp/emnr.c`)
- `lyra/dsp/nr2.py` (Ephraim-Malah / MMSE-LSA, derived from
  `wdsp/emnr.c`)
- `lyra/dsp/lms.py` (LMS adaptive line enhancer, derived from
  `wdsp/anr.c` Pratt 2012/2013 algorithm)
- `lyra/dsp/anf.py` (auto-notch filter, derived from `wdsp/anf.c`)
- `lyra/dsp/nb.py` (noise blanker)
- `lyra/dsp/squelch.py` (RMS + auto-tracked noise floor squelch)

## 3. HL2 protocol critical facts (don't forget these)

These are the gotchas that cost real debugging time when missed.

### 3.1 HL2 advertises `nddc = 4` on the wire

Even though HL2 silicon has only 2 physical DDC engines, the gateware
exposes 4 logical DDCs to the host.  Mapping:

```
DDC0 = RX1 frequency (VFO A)
DDC1 = RX2 frequency (VFO B)
DDC2 = TX frequency (used for PureSignal feedback during PS+TX)
DDC3 = TX frequency (used for PureSignal feedback during PS+TX)
```

For all RX2 work and beyond, `nddc=4` is the Lyra default for HL2.
The Hermes II `nddc=2` PS path is dead code on HL2 — don't add
special-case branches for it.

### 3.2 Frame 0 C4 byte mandatory bits

The "general settings" C&C frame (C0=0x00) C4 byte:

- bits[1:0] = antenna select (HL2 = 00, irrelevant)
- **bit 2 = duplex bit, set on every MAIN-LOOP frame-0 emission**
  (HL2 quirk — without it, post-priming RX freq updates don't
  apply).  **Important nuance caught by Round 1 2026-05-11
  agent A:** the priming function `ForceCandCFrames`
  (networkproto1.c:111-127) does NOT set the duplex bit — priming
  emits `C4 = (nddc-1) << 3 = 0x18` (no bit-2).  The gateware
  accepts the priming VFO writes regardless.  The duplex bit
  becomes required only for MAIN-LOOP freq updates after priming
  completes; it's added in `WriteMainLoop_HL2` case-0 path at
  line 967 (`C4 |= 0x04`).  Lyra's priming function must emit
  0x18, and Lyra's main-loop frame-0 emission must emit 0x1C.
- bits[6:3] = `nddc - 1` (4-bit field; `nddc-1` ranges 0..15).
  For nddc=4: `(4-1) << 3 = 0x18`.
- bit 7 = diversity (HL2 = 0)

Combined for main-loop emission: `c4 = 0x1C` for nddc=4 + duplex
bit set.  Priming emission: `c4 = 0x18` (no duplex bit).

### 3.3 EP6 receive frame layout (nddc=4)

Per UDP datagram: 2 × 512-byte USB frames.  Per USB frame:

- bytes [0:3] = `0x7F 0x7F 0x7F` sync
- bytes [3:8] = C0..C4 (radio→host status: PTT, ADC overload, fwd/rev
  power, AIN voltages, optional I2C readback for HL2)
- bytes [8:512] = 504 bytes = 19 sample-slots × **26 bytes/slot**

Per 26-byte slot:
- bytes 0..2:  DDC0 I (BE 24-bit signed)
- bytes 3..5:  DDC0 Q
- bytes 6..8:  DDC1 I
- bytes 9..11: DDC1 Q
- bytes 12..14: DDC2 I
- bytes 15..17: DDC2 Q
- bytes 18..20: DDC3 I
- bytes 21..23: DDC3 Q
- bytes 24..25: mic sample (BE 16-bit signed)

Lyra's parser must skip DDC2/DDC3 bytes when PS is off (they're noise,
not useful).  The parser dispatches per-DDC into a callback like
`on_ddc_samples(ddc_idx, samples)`.

### 3.4 EP2 audio frame layout (host→radio)

Per UDP datagram: 2 × 512-byte USB frames.  Per USB frame:

- bytes [0:8] = control header
- bytes [8:512] = 504 bytes = 63 LRIQ tuples × **8 bytes/tuple**

Per 8-byte tuple:
- bytes 0..1: L audio (BE 16-bit signed)
- bytes 2..3: R audio
- bytes 4..5: TX I (BE 16-bit signed)
- bytes 6..7: TX Q

Quantization: `int16 = round(sample * 32767)` with explicit
floor/ceil for round-to-nearest.

### 3.5 HL2 audio rate is fixed at 48 kHz

The on-board AK4951 codec is hard-locked at 48 kHz by the gateware.
EP2 LRIQ tuples produce one set of L/R audio + I/Q TX per USB frame.
HL2's TX I/Q rate is also 48 kHz (no resampling needed in TX path).

### 3.6 RX I/Q rates can differ between DDCs

Per Thetis's `cmaster.c::SetDDCRate(i, rate)`, each DDC can run at
its own rate (48k / 96k / 192k / 384k).  Lyra's existing decimator
in `lyra/dsp/channel.py` already handles arbitrary input rates →
fixed audio rate, so per-DDC rate independence is "free" for v0.0.9
(no new code needed).

**HL2 P1 caveat (L-2 Round 1 2026-05-11):** the per-DDC-rate
flexibility is a Protocol 2 feature.  On HL2 P1 specifically,
`netInterface.c:1328` proves only `id == 0` (DDC0/RX1) sets the
global `SampleRateIn2Bits` wire-protocol rate field.  All four
HL2 DDCs deliver samples at the same on-wire rate (the rate
RX1 selects).  Host-side post-receive decimation is still
arbitrary, but the WIRE rate is shared across DDCs.  ANAN P2
operators get true per-DDC rate independence via the P2 command
structure; HL2 P1 operators do not.

### 3.7 PureSignal is one bit (well, three)

To enable PureSignal:
- `nddc = 4` (HL2 default already)
- frame 0 C4 bit 2 = 1 (duplex bit, always set anyway)
- frame 11 C2 bit 6 = 1 (`puresignal_run`)
- frame 16 C2 bit 6 = 1 (`puresignal_run`)

Thetis sets the bit, then trusts the gateware to deliver feedback
samples on DDC2/DDC3.  No protocol-level handshake or status
read-back.  HL2 community gateware variant + hardware mod handles
the rest.

### 3.8 HL2 quirks vs ANAN

- **TX attenuator range = -28..+31 dB** (not 0..31).  Negative
  values are gain rather than attenuation.  Used for both normal TX
  gain and PS auto-attenuator state machine.
- **CW state bits on HL2 — TX I-sample bytes are repurposed during
  CW transmit (L-5 Round 1 2026-05-11 prose clarification).**
  Per networkproto1.c:1247-1259, when `cw_enable && j == 1` the
  outer loop sets `temp = (cwx_ptt << 3 | dot << 2 | dash << 1 |
  cwx) & 0x0f` and writes this directly to the I-sample's two
  bytes — **OVERWRITING** the normal modulator I/Q.  HL2 has 4
  CW state bits (cwx_ptt at bit 3 + dot/dash/cwx); non-HL2 has 3
  (no cwx_ptt).  Practical implication: during CW transmit on HL2,
  Lyra's TX SSB modulator's I output is replaced by CW state
  bytes on the wire.  Pitfall: an SSB-on-CW-key combo would
  produce wild bits if not protocol-gated.
- **L/R audio channels can be swapped** by some HL2 firmware revs.
  Add a `swap_lr_audio` Settings option to compensate.
- **HL2 read-loop handles I2C readback inline** — when C0 has bit 7
  set, frame data is I2C response, not ADC overload status.
- **ADC overload semantics divergence (L-3 Round 1 2026-05-11):**
  HL2 read loop does single-frame assignment
  (`adc_overload = ControlBytesIn[1] & 0x01;` at line 502), NOT
  the OR-until-cleared pattern that the standard read loop uses
  (`adc_overload = adc_overload || (ControlBytesIn[1] & 0x01);`
  at line 338).  Glitches that don't persist into the next
  telemetry frame can be missed on HL2.  If Lyra implements
  polling with the "OR-until-cleared" semantic on the host side,
  no behavior change.  If Lyra reads it as live signal, HL2 will
  under-report transient ADC overloads.
- **PS sample rate during PS+TX** = `rx1_rate` (whatever user
  selected), NOT the 192 kHz `ps_rate` ANAN uses.  Thetis comment:
  "HL2 can work at a high sample rate."
- **PS feedback DDC routing (corrected Round 1 2026-05-11 — was
  wrong in earlier docs):** HL2 PS+TX enables ONLY DDC0+DDC1,
  with `cntrl1=4` routing the PA coupler ADC to DDC0 and DDC1
  sync-paired to DDC0 at TX freq.  DDC2 and DDC3 are
  **gateware-disabled** during HL2 PS+TX — those EP6 slots are
  zeros, NOT feedback samples.  PS calcc consumer must read from
  host channels 0 and 2 (DDC0/DDC1) during MOX+PS state, not from
  the DDC2/DDC3 twist dispatch.  This is a state-product reroute
  on `(mox, ps_armed)` — see v0.1 plan §2.2.
- **PS auto-attenuate recalibrate trigger**: `FeedbackLevel > 181 ||
  (FeedbackLevel <= 128 && cur_att > -28)`.

## 4. WDSP port strategy (concrete)

### 4.1 Port directly with attribution

**REWRITTEN Round 1 2026-05-11 (CR-5):** the table below
reflects post-v0.0.9.6 cffi-pivot reality.  Earlier draft listed
`compress.c` / `cfcomp.c` / `wcpagc.c` (= leveler / ALC) /
`patchpanel.c` / `varsamp.c` / `rmatch.c` for pure-Python NumPy
ports — but those all already live in WDSP's cffi engine and
Lyra calls them via cffi bindings.  No port needed.  iqc.c and
calcc.c stay on the port list because their algorithm is wrapped
in operator-tunable PS lifecycle state (FSMs, snapshot capture,
attestation checkbox) that justifies Lyra-Python wrappers
around the WDSP-cffi call sites — those wrappers handle
operator-facing PS dialog state, not the math itself.

| WDSP file | Lyra target | Port approach | Phase |
|---|---|---|---|
| `patchpanel.c::SetRXAPanelPan` (50 LOC) | `wdsp_engine.RxChannel.set_panel_pan` cffi binding | cffi wrap, no port | v0.0.9.6 (shipped) |
| `compress.c` (~150 LOC) | `wdsp_tx_engine.TxChannel.set_compressor_*` cffi | cffi wrap, no port | v0.2.1 |
| `cfcomp.c` (~600 LOC) | `wdsp_tx_engine.TxChannel.set_cfcomp_*` cffi | cffi wrap, no port | v0.2.1 |
| `osctrl.c` (CESSB, ~200 LOC) | `wdsp_tx_engine.TxChannel.set_osctrl_run` cffi | cffi wrap, no port | v0.2.1 (NEW Round 1 — was missing) |
| `wcpagc.c` mode 5 (leveler) | `wdsp_tx_engine.TxChannel.set_leveler_*` cffi | cffi wrap, no port | v0.2.1 |
| `wcpagc.c` mode 5 (ALC) | `wdsp_tx_engine.TxChannel.set_alc_*` cffi | cffi wrap, no port | v0.2.0 |
| `lmath.c::xbuilder` (~200 LOC) | `lyra/dsp/ps_xbuilder.py` | port (used by Python-side calcc orchestration) | v0.3 |
| `delay.c` (~80 LOC) | `lyra/dsp/delay_line.py` | port (TX/feedback time-alignment in Python) | v0.3 |
| `iqc.c` (315 LOC) — application | cffi via WDSP's TXA channel (`SetTXAiqcRun`, `SetTXAiqcSwap`) | cffi for math, Python wrap for 5-state lifecycle (RUN, BEGIN, SWAP, END, DONE) | v0.3 |
| `calcc.c` (1164 LOC) — calibration | cffi via WDSP's TXA channel (calcc thread driven by semaphore) | cffi for math, Python wrap for 8-state PS FSM + 3-state attenuator FSM + PSDialog UI | v0.3 |

### 4.2 Write Lyra-native (don't port)

These are Thetis-specific glue or trivially small:

- `TXA.c`, `RXA.c` — channel scaffolding.  Lyra has its own.
- `channel.c` — buffer mgmt.  Python's GIL handles it.
- `aamix.c` — mixer.  Lyra-native dispatcher thread in
  `lyra/dsp/audio_mixer.py` (NOT a NumPy port of aamix; the
  Python port `lyra/dsp/mix.py` was retracted Phase 0 per
  v0.1 plan §5.1 IM-4 to avoid double-pan with WDSP cffi).
- `analyzer.c` — spectrum.  Lyra has its own GPU widget.
- `main.c` — Win32 thread mgmt.  Use Python threading.

### 4.3 cffi + WDSP DLL — adopted 2026-05-06

**Earlier guidance** in this section said: "Don't reach for cffi /
WDSP DLL until profiling forces it. Pure Python with NumPy
comfortably handles 192k I/Q + 48k audio per RX." That guidance
turned out to be optimistic.  Profiling DID force it: per-sample
work in agc_wdsp / nr / nr2 / anf / demod / channel produced GIL
contention with the EP2 writer thread that surfaced as audio
clicks and motorboating.  We tried surgical fixes for several
rounds before pivoting.

**Current direction (v0.0.9.6+):** the RX (and eventually TX, RX2,
PureSignal) DSP chain is implemented as cffi calls into the WDSP
DSP engine — Lyra-relevant entry points declared in
`lyra/dsp/wdsp_native.py`, high-level wrapper in
`lyra/dsp/wdsp_engine.py`, native binaries bundled at
`lyra/dsp/_native/` so installs don't depend on any other radio
program being present on the operator's machine.

**License posture:** Lyra is GPL-3.0-or-later, the bundled DSP
engine is also GPL-3.0-or-later — link-compatible.

**Wheel-build complexity worry:** the bundled-DLL approach
sidesteps it entirely. The five DLLs ship with Lyra; cffi loads
them at runtime. No compiler invocation at install or runtime.

**The pure-Python DSP modules in `lyra/dsp/` stay in tree** as a
LYRA_USE_LEGACY_DSP=1 fallback and as the basis for DSP layers
that don't overlap WDSP (the spectrum widget, captured noise
profiles UX, click-to-tune, etc.).  Cleanup pass after the native
engine is solid through TX + PureSignal.

See §14 below for the actual integration architecture.

## 5. Lyra threading model

Five threads across the v0.0.9 / v0.1 / v0.2 roadmap:

```
Thread 1: HL2Stream._rx_loop          (recvfrom loop)
Thread 2: DSP worker                   (RX1 + RX2 chains, audio sink, TX chain in v0.1)
Thread 3 (NEW in v0.2): PS calc thread (semaphore-driven, runs calc())
Thread 4: HL2Stream TX writer          (drains TX queue at EP2 cadence)
Thread 5: Qt main thread               (UI; signals/slots only)
```

**No MMCSS / OS thread priority** for v0.0.9.  Python's GIL is the
binding constraint, not OS priority.  Add MMCSS only if profiling
shows audio drops.

**Buffer flow contract** (RX side, v0.0.9):

```
HL2Stream._rx_loop  → parser splits to {0,1,2,3}
                    → on_ddc_samples(ddc=0, ...) → Radio.dispatch_rx1
                    → on_ddc_samples(ddc=1, ...) → Radio.dispatch_rx2
                    → on_ddc_samples(ddc=2, ...) → drop (v0.0.9) / PS feedback (v0.2)

Radio.dispatch_rx*  → DspChannel[k].process(iq) → audio_k
                    → AudioMixer.add_input(stream_id=k, audio_k)
                    → mixer thread paces wire cadence
                    → outbound(stereo) → audio_sink.write(stereo)
```

dispatch_rx1 and dispatch_rx2 fire on the **same parser invocation**
in sequence.  Both produce equal-length audio (decimators map any IQ
rate → fixed audio rate).  No queueing latency, no cross-thread
fan-out.

## 6. Core architecture decisions (settled)

### 6.1 RX2 audio routing

**Stereo split via EP2 LR bytes through the AK4951 codec.**  RX1
hard-left, RX2 hard-right.  Auto-applied when RX2 enables.

- Per-RX `pan` parameter, default 0.5.  When RX2 enables: RX1.pan=0,
  RX2.pan=1.
- Pan curve: WDSP sin-π rule (port from `wdsp/patchpanel.c`).  At
  pan=0.5, both channels at unity (6 dB louder than endpoints).
  Don't use Lyra's existing equal-power Balance rule; use WDSP's.
- L/R swap option in Settings (HL2 firmware-rev compensation).
- No host-side sounddevice path for v0.0.9 — AK4951 is the canonical
  HL2 audio route.

### 6.2 RX2 UI model — hybrid

- Each RX has its own freq display + panadapter region with
  read-only status badges (mode, filter, AGC).
- Single MODE+FILTER and DSP+AUDIO panels operate on the **focused
  RX**.
- Click any freq display to focus.  Hotkeys: Ctrl+1 → RX1, Ctrl+2 →
  RX2.
- Focus indicator: colored border on focused freq display + matching
  control panel header tint.

**Working-group round refinements (2026-05-12 — operationalize the
focus model with explicit visual + interaction cues).** Full spec
in `docs/architecture/v0.1_rx2_consensus_plan.md` §6.7 + §6.8.
Summary:

* **Two TX indicators per VFO LED** (red = active TX, gray =
  inactive).  SPLIT auto-moves red → VFO B; click gray to manually
  swap.  Operators never compute which VFO they're about to
  transmit on from current state — display directly.
* **Middle-click on the panadapter** swaps focused/active VFO
  (verified unbound today; left/right/wheel are reserved for
  tuning / notches / zoom).
* **`TUNE A` / `TUNE B` tooltip** follows the cursor on the
  panadapter so the operator never has to look up to confirm
  which VFO the wheel currently tunes.
* **Right-click the SPLIT button** (in the MODE+FILTER strip —
  NOT the panadapter, whose right-click is reserved for notches)
  = per-mode shift-offset menu (operator-set default for AM /
  LSB / USB / CW each remembered).
* **SUB button** is the primary RX2 enable toggle, sibling of
  SPLIT in the MODE+FILTER strip.
* **A→B / B→A / SWAP** buttons same strip — full state copy
  when RX2 enabled, freq-only when disabled.
* **Per-RX Vol-A / Vol-B + Mute-A / Mute-B sliders are always
  visible** (Phase 3.E.1 hotfix v0.16, 2026-05-12 — superseded the
  original "only when SUB is enabled" plan).  Operator UX feedback
  on the conditional-visibility version: predictable layout beats
  conditional widgets — operators were reaching for sliders that
  weren't there.  Implementation pinned in `lyra/ui/panels.py`
  around L2040–L2114 (`Vol-A`/`Vol-B`/`Mute-A`/`Mute-B` mirror
  RX1/RX2 in both SUB-on and SUB-off modes).  Balance + AF Gain
  stay single (combined-output and pre-AGC reference respectively).
  Rationale for per-RX vol: ear-balance is the one control that
  genuinely needs per-RX independence in real dual-RX use — the
  two receivers produce wildly different signal levels.  Phase 2's
  `_do_demod_wdsp_dual` already supports per-channel volume.

### 6.3 SPLIT semantics

- VFO A = RX1 freq (always).
- VFO B = RX2 freq when RX2 is enabled, otherwise a "shadow" freq.
- SPLIT toggle: TX freq = VFO B's freq when ON, VFO A's when OFF.
- VFO B lock toggle prevents accidental tuning during pile-up
  listening.
- Buttons: A→B, B→A, Swap.
- TX cursor renders on whichever RX shows the TX VFO (in v0.0.9 even
  before TX itself ships).

### 6.4 DDC frequency-source abstraction

```python
ddc[0].freq_source = "VFOA"   # RX1 — always VFOA
ddc[1].freq_source = "VFOB"   # RX2 — always VFOB
ddc[2].freq_source = "TX"     # PS feedback in v0.2; static TX in v0.0.9
ddc[3].freq_source = "TX"     # Same
```

DDC2/DDC3 always carry TX freq in C&C frames 5/6 regardless of PS
state.  Parser must always skip those bytes.  When v0.2 lands and
sets `puresignal_run=True`, the same freq writes become "PS feedback
freq" — no protocol redesign.

### 6.5 PureSignal posture

- Plumb the protocol surface in v0.0.9 (`puresignal_run` flag in C&C
  writer, DDC freq-source abstraction).  Inert in v0.0.9.
- v0.2 = port `calcc.c` + `iqc.c` + supporting modules.
- Operator self-attestation that they have the HL2 PS hardware mod
  installed.  Settings checkbox: "I have the PureSignal hardware mod
  installed."  Default OFF; until checked, PS controls disabled with
  explanatory tooltip.
- N8SDR runs PS on HL2/HL2+ with appropriate gateware + mod.  This is
  the working configuration.

### 6.6 PTT state machine (v0.1)

States: RX → MOX_TX (UI button or CAT) → CW_TX (key down) → TUN_TX
(low-power tune) → VOX_TX (deferred to v0.2).

- RX-mute fade ~50 ms when MOX→TX (no clicks).
- Hardware PTT input via HL2 EP6 status bytes (`prn->ptt_in =
  ControlBytesIn[0] & 0x1`).
- State machine in `lyra/radio/ptt.py`.  Qt signal `mox_changed
  (bool)` for UI.

### 6.7 Hardware abstraction discipline (for v0.4 ANAN work)

Operator decision 2026-05-03: ANAN family + Protocol 2 support
is planned for v0.4.  v0.1-v0.3 stay HL2-only by scope, but the
following five disciplines apply during HL2 work to avoid an
expensive retrofit later.  Future Claude sessions: enforce these
on every PR.

1. **`nddc` is a runtime value, not a magic constant.**  Read
   from `radio.protocol.nddc`, never hard-code `4`.  P2 ANAN
   models have varying DDC counts (G2 = 4, 7000DLE = 7).  The
   abstraction is free if added now; expensive to retrofit.

2. **`Radio` facade is hardware-agnostic.**  Public methods
   accept logical units (Hz, dB, mode names).  Hardware-specific
   conversions (e.g. HL2's TX attenuator -28..+31 dB ↔ a generic
   "TX drive" range) live inside `lyra/protocol/p1_hl2.py` and
   eventually `p2_anan.py`.  Smell test: if a method name
   contains "hl2", it's in the wrong layer.

3. **Don't kill the sounddevice audio path permanently.**  §6.1
   says "no host-side sounddevice path for v0.0.9" — that's right
   for HL2 (AK4951 is canonical) but wrong as a permanent
   architectural choice.  ANAN audio comes back via P2 over
   Ethernet to the host; sounddevice (or sibling) renders it.
   `AudioSink` interface stays clean so re-adding sounddevice is
   one new file in `lyra/audio/`, not a refactor.

4. **PureSignal posture conditional on radio capabilities.**
   HL2 PS = hardware mod required (operator self-attestation
   per §6.5).  ANAN G2 PS = built into stock gateware.  v0.3
   should branch on `radio.capabilities.puresignal_requires_mod`,
   not hardcode the attestation checkbox into the UI.  The
   capabilities object is a per-radio-class struct populated in
   the protocol module.

5. **TX hardware quirks live in protocol module, not DSP.**  HL2:
   TX attn -28..+31 dB, CWX PTT bit at I-LSB bit 3.  ANAN G2: TX
   attn 0..31 dB, standard CWX bit positions.  None of this leaks
   into `lyra/dsp/tx_*.py` — DSP produces baseband I/Q at the
   rate the protocol layer asks for, full stop.  All hardware
   quirks belong in `lyra/protocol/p1_hl2.py` (today) and
   `lyra/protocol/p2_anan.py` (v0.4).

6. **DDC-index → host-channel mapping is family-specific AND
   state-product-dependent (Amendment A3 2026-05-11 + CR-3
   correction Round 1 2026-05-11).**  The mapping between
   wire-protocol DDC indices (0..N-1) and Lyra's host-side DSP
   channel IDs is NOT identity, varies per radio family, AND
   varies by `(mox, ps_armed)` state product within a family.

   **HL2 (4-DDC) — verified Round 1 Agent A at console.cs:8469-8488
   + networkproto1.c:549-553:**
   ```
   RX-only state (no MOX, or MOX without PS):
     DDC0 → wire-protocol xrouter source 0 → host ch 0 (RX1)
     DDC1 → xrouter source 2 → host ch 2 (RX2)
     DDC2+DDC3 twist → source 1 → host ch 3 (idle; gateware does
       not enable DDC2/DDC3 on HL2 — slots carry zeros)

   MOX+PS state (PS hardware-mod required, cntrl1=4 routing):
     DDC0 → xrouter source 0 → host ch 0 (now PS feedback I via
       gateware ADC-mux switch from antenna to PA coupler)
     DDC1 → xrouter source 2 → host ch 2 (now PS feedback Q, sync-
       paired to DDC0 at TX freq)
     DDC2+DDC3 still gateware-disabled (zeros)
     [RX2's actual VFO B band is NOT being received in this state;
      operator UI shows "PS-paused" badge on RX2 per v0.1 plan
      §2.2 CR-1]
   ```

   **ANAN P1 5-DDC — verified Round 1 Agent A at
   networkproto1.c:554-558:**
   ```
   RX-only state:
     DDC0+DDC1 twist → xrouter source 0 → host ch 0 (RX1 main +
       diversity-sync pair if diversity enabled)
     DDC2 → xrouter source 2 → host ch 2 (RX2 main, independent
       freq; ANAN's structural advantage over HL2)
     DDC3+DDC4 twist → xrouter source 1 → host ch 3 (idle when
       PS not armed)

   MOX+PS state (ANAN with cntrl1=0x08 routing via (rx_adc_ctrl1
   & 0xf3) | 0x08):
     DDC0+DDC1 twist → source 0 → host ch 0 (now PS feedback I/Q
       via cntrl1=0x08 PA-coupler routing)
     DDC2 → source 2 → host ch 2 (RX2 STAYS LIVE — ANAN advantage)
     DDC3+DDC4 twist → source 1 → host ch 3 (potential additional
       feedback path; ANAN-family-specific)
   ```

   **ANAN P2 family:** dispatch happens through P2's discovery-
   advertised DDC count + per-family routing table.  Populated
   in `lyra/protocol/p2_anan.py` in v0.4.4.

   **Implementation discipline:** the mapping table lives in
   `lyra/protocol/<family>.py` next to the capability struct
   (per audio_architecture.md §13.4) — NOT in `radio.py` or
   DSP modules.  Dispatch helpers (`twist`, per-DDC
   demultiplexer) consume the table from
   `radio.protocol.ddc_map(state)` at runtime, where `state` is a
   `lyra.radio_state.DispatchState` snapshot (4 axes: mox,
   ps_armed, rx2_enabled, family).  Signature pinned v0.1 Phase 0
   2026-05-11 per consensus-plan §4.2.x R5-3 — the earlier draft
   in this section used `ddc_map(mox, ps_armed)` but R3-3 added
   the rx2_enabled + family axes so the function now takes the
   full state snapshot for forward-compatibility with v0.4
   multi-radio.  Phase 1 `stream.py` MUST already be
   table-driven on MOX edges (per v0.1 plan §9.5 architectural
   implication) — hard-coded `if ddc==0: → RX1; if ddc==1: → RX2`
   is a smell.  Smell tests:
   - Any `if ddc_idx == N:` in non-protocol code is wrong.
   - Any `isinstance(radio.protocol, HL2)` in non-protocol code
     is wrong — use capabilities struct.
   - Any DDC→host-channel mapping that doesn't account for the
     full `DispatchState` (mox, ps_armed, rx2_enabled, family)
     is wrong — the same wire dispatch routes to different
     consumers depending on state.

When v0.4 starts, the protocol module gets split:

```
lyra/protocol/
├── __init__.py
├── stream.py            # current — rename to p1.py + thin shim
├── p1.py                # NEW — HPSDR Protocol 1 base
├── p1_hl2.py            # NEW — HL2-specific quirks (mostly today's stream.py)
├── p2.py                # NEW v0.4 — HPSDR Protocol 2 base
├── p2_anan.py           # NEW v0.4 — ANAN-specific quirks
└── capabilities.py      # NEW v0.4 — radio-class capability struct
```

The §3 "HL2 protocol critical facts" reference stays under that
heading; v0.4 adds §3b "ANAN P2 critical facts."

## 7. Phased delivery roadmap

### v0.0.9 — Memory & Stations (SHIPPED 2026-05-02)

Pre-RX2 polish release.  TIME button (HF time-station cycle),
GEN1/2/3 customization, 20-slot Memory bank with CSV import/export,
EiBi shortwave broadcaster overlay with auto-detection.  See
`CHANGELOG.md` [0.0.9].

### v0.1 — RX2 (next)

- Phase 0: multi-channel refactor (no behavior change).
- Phase 1: protocol RX2 enablement (nddc=4, EP6 parser rewrite).
- Phase 2: stereo split audio routing.
- Phase 3: UI integration (focus model, hotkeys, A↔B/Swap/Lock buttons).
- Phase 4: split panadapter (vertical splitter in central widget).
- Phase 5: polish, persistence, docs.
- Rolling pre-releases per phase.

### v0.2 — TX (post-RX2)

**Cadence resynced 2026-05-14 to match the consensus plan §8.5
implementation cadence (Round 5 verified).**  Previous CLAUDE.md
§7 entry (v0.2.0=SSB bare, v0.2.3=leveler+EQ) predated the
consensus plan and put load-bearing dynamic-range blocks (ALC,
leveler) in the last sub-release, which the Round 5 review
caught as a "post-PA splatter on day one" risk.  The order
below puts the splatter-bound blocks in v0.2.0 with the
modulator itself.

- **v0.2.0: SSB basics + dynamic-range bounds.**  Modulator
  (SSB), EP2 TX I/Q packing, MOX/PTT state machine, TX power
  control (HL2 step attenuator -28..+31 dB via capabilities
  struct), mic gain, **leveler reuse** (WDSP `wcpagc` mode 5
  cffi — shared binding with RX AGC), **ALC** (xwcpagc on
  TXA.c line 579 — 1 ms attack / 10 ms decay / -3 dBFS thresh
  per Thetis radio.cs; the load-bearing limiter that prevents
  post-PA splatter), **RX/TX RTA scaffolding** (audio-domain
  FFT widgets render with live data + taps in place, no EQ
  yet), **§8.2 sip1 TX I/Q tap** (mandatory in v0.2 — adds
  the v0.3 PureSignal calibration tap point now so v0.3 can
  focus purely on PS work without re-validating every TX
  sub-mode), §15.9 red-on-air visual rule, §15.14 auto-mute-
  on-TX Settings + behavior, §15.15 AAmixer state badge
  (partial — TX state strings meaningful, PS strings stay
  placeholder until v0.3).
- **v0.2.1: EQ + dynamics.**  WDSP `eqp.c` port → 10-band
  parametric EQ for both RX and TX, EQ dialog with RTA-driven
  live preview (the RTA widgets from v0.2.0 now show live
  data + EQ overlay).  WDSP `compress.c` cffi binding goes
  live (TX speech compressor + paired bp1).  §15.13 COMP chip
  (MODE_COMP source-switching meter) lands — `tx_lvlr_db_changed`
  signal now has a real signal source.
- **v0.2.2: CW + AM + FM.**  CW modulator with internal keyer
  + sidetone + CWX PTT bit per CLAUDE.md §3.8 (HL2 has 4 CW
  state bits — cwx_ptt + dot + dash + cwx — encoded in TX
  I-sample LSBs during CW transmit).  AM modulator (DSB +
  SAM + carrier-restore).  FM modulator with deviation control
  + pre-emphasis position-1 + CTCSS.  WDSP `cfcomp.c` cffi
  binding for the 5-band speech processor that contest
  operators want.
- **v0.2.3: Polish.**  Per-band EQ memory, custom EQ preset
  save/load, meter calibration UX (per-band 3-point forward-
  power cal per §8.4(a)), monitor level / sidetone tuning,
  MOX-edge audio fade tuning, XIT enable (the disabled UI from
  v0.1.1 just needs `tx_freq += xit_offset_hz` in
  `_compute_tx_dds_hz`; ~2 h).

### v0.3 — PureSignal

- Port `calcc.c` + `iqc.c` + `xbuilder` + `delay.c`.
- New `PSDialog` UI modeled on Thetis's `PSForm.cs`.
- Auto-attenuator state machine (HL2-specific bounds).
- Coefficient persistence to `~/.config/lyra/ps_corrections/`.
- Operator self-attestation checkbox (HL2; ANAN G2 won't need it).

### v0.4 — Multi-radio refactor + Protocol 2 + ANAN (long-term)

Operator decision 2026-05-03: ANAN family support is a real
future direction.  Approach:

- v0.4.0: Protocol module split per §6.7 file layout (no
  behavior change for HL2 operators).  Capability struct
  populated for HL2; ANAN capability struct stubbed but inert.
- v0.4.1: Protocol 2 base implementation (`p2.py`) — discovery,
  framing, command structure.  Tested against an ANAN G2 unit.
- v0.4.2: ANAN-specific gateware quirks (`p2_anan.py`) — radio
  model detection, per-model DDC count, PS-without-attestation,
  TX attenuator range, audio routing via sounddevice (since ANAN
  has no AK4951 codec).
- v0.4.3: Settings UI — radio-model picker (auto-discover then
  select if multiple).  Documentation pass for ANAN operators.
- v0.4.4: Polish, second-radio testing on ANAN-7000DLE Mk2 (P1
  *or* P2 mode), older ANAN-100/200/8000 (P1-only — corrected
  Round 1 2026-05-11 Agent A: these run **nddc=5 not nddc=4**
  with a different DDC enable mask and `cntrl1=0x08` PS routing.
  "Should already work via the HL2 path with minor capability
  differences" understates the work — it's a new protocol module
  variant (`p1_anan.py`?) sibling to `p1_hl2.py`, NOT a tweak.
  Revised v0.4.4 timeline accordingly when the work is scoped).

**Brick SDR (L-6 Round 1 2026-05-11; decided Round 3 2026-05-11
per R3-8 — non-blocking for v0.1 Phase 0):** operator mentioned
Brick SDR as a v0.4 candidate during the Round 1 amendment
sequence on 2026-05-11.  Brick is **not** in Thetis 2.10.3.13
source (Agent A confirmed — greppable for "brick" across the
entire Thetis tree returns nothing relevant).

**Round 3 scope decision (2026-05-11):** Brick is **non-blocking
for v0.1 Phase 0 and the broader v0.1-v0.3 sequence.** The v0.4
hardware-abstraction discipline in §6.7 (esp. discipline #6
DDC mapping is family-specific + state-product-dependent) is
sufficient foundation to absorb Brick when its scope solidifies.
No Phase 0 deliverables wait on Brick.

**Pending operator action (deferrable until v0.4 work starts):**
specify which "Brick" (HiQSDR's Brick SDR? Some other vendor?)
AND what protocol it speaks (HPSDR P1 like HL2/older ANAN,
HPSDR P2 like ANAN G2, or a vendor-specific protocol Lyra has
never seen).

- If Brick is HL2-class (HermesLite derivative): drops cleanly
  into `p1_hl2.py` (or sibling) with a different capability
  struct.  v0.4 additive.
- If Brick is ANAN-class P1: falls into the ANAN-100/200/8000
  branch above.  v0.4 additive.
- If Brick is P2: falls into the G2/7000DLE branch.  v0.4
  additive.
- If Brick is vendor-specific (not HPSDR): **NOT** in v0.4
  scope.  Push to v0.5+ "third protocol" work — would require a
  new `lyra/protocol/<vendor>.py` module + discovery + audio
  routing decisions + UI capability extensions.  Six-month
  scope on its own.

The five hardware-abstraction disciplines in §6.7 govern PRs
during v0.1-v0.3 to keep this milestone tractable.  Without that
discipline, v0.4 becomes a six-month rewrite; with it, v0.4 is
a focused two-month push (assuming Brick falls into one of the
existing HPSDR classes).

## 8. File path conventions

```
lyra/
├── __init__.py                    # version source of truth
├── radio.py                       # Radio class — channel dict + facades
├── protocol/
│   └── stream.py                  # HPSDR P1 — nddc=4, per-DDC freq, etc.
├── dsp/
│   ├── channel.py                 # per-RX DSP chain (existing)
│   ├── audio_mixer.py             # mixer-dispatcher thread (pan lives in WDSP cffi via SetRXAPanelPan)
│   ├── tx_channel.py              # NEW v0.1 — TX DSP chain
│   ├── ssb_mod.py                 # NEW v0.1 — SSB modulator
│   ├── cw_keyer.py                # NEW v0.1.1
│   ├── tx_compressor.py           # NEW v0.1.1 — port from compress.c
│   ├── ps_calcc.py                # NEW v0.2 — port from calcc.c
│   ├── ps_iqc.py                  # NEW v0.2 — port from iqc.c
│   ├── ps_xbuilder.py             # NEW v0.2 — cubic-spline coef builder
│   └── delay_line.py              # NEW v0.2
├── radio/
│   └── ptt.py                     # NEW v0.1 — PTT state machine
├── ui/
│   ├── panels.py                  # extend for RX2/TX/PS controls
│   ├── spectrum.py                # add split-vertical mode for dual pan
│   └── ps_dialog.py               # NEW v0.2 — modeled on PSForm.cs

docs/architecture/                  # research + plans (this conversation)
├── implementation_playbook.md     # AUTHORITATIVE — start here
├── v0.0.9_rx2_plan.md
├── hl2_puresignal_audio_research.md
├── rx2_research_notes.md
├── threading.md                   # existing
├── noise_toolkit.md               # existing
└── wdsp_integration.md            # existing — attribution patterns
```

## 9. Reference paths in Thetis source tree

When I need to verify a protocol detail mid-implementation:

```
D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\Project Files\Source\
├── ChannelMaster\
│   ├── networkproto1.c            # HL2 read/write loops, EP2/EP6 packing
│   ├── cmaster.c                  # WDSP per-receiver setup
│   └── network.h                  # struct definitions, bit fields
├── Console\                       # C# UI + radio control (DON'T copy code)
│   ├── console.cs                 # UpdateDDCs, AAmixer states
│   ├── PSForm.cs                  # PS state machine, HL2 attenuator bounds
│   ├── radio.cs                   # WDSP channel ID convention
│   └── HPSDR\IoBoardHl2.cs        # I/O board context
└── wdsp\                          # GPL v3+, OK to port
    ├── calcc.c, calcc.h           # PS calibration
    ├── iqc.c, iqc.h               # PS predistortion application
    ├── patchpanel.c               # pan curve — called live via WDSP cffi (SetRXAPanelPan); no Python port (v0.1 plan §5.1 IM-4)
    ├── compress.c                 # TX compressor (port for v0.1.1)
    ├── lmath.c                    # xbuilder cubic-spline (port for v0.2)
    ├── delay.c                    # delay line (port for v0.2)
    └── (137 other files)          # consult as needed
```

Specific landmarks worth remembering:

- `networkproto1.c::WriteMainLoop_HL2` lines 869–1201 — full C&C
  frame schedule
- `networkproto1.c::MetisReadThreadMainLoop_HL2` lines 422–586 —
  EP6 receive parsing
- `networkproto1.c::sendProtocol1Samples` lines 1204–1267 — EP2
  audio packing
- `console.cs::UpdateDDCs` lines 8214–8577 — DDC enable / sample-rate
  per model
- `console.cs::UpdateAAudioMixerStates` lines 28217–28333 — audio mix
  routing
- `PSForm.cs::timer1code` lines 553–727 — PS state machine
- `PSForm.cs::timer2code` lines 728–820 — auto-attenuator (HL2-specific)
- `PSForm.cs::NeedToRecalibrate_HL2` line 1142 — HL2 recal threshold
- `wdsp/patchpanel.c::SetRXAPanelPan` lines 158–176 — pan curve
- `wdsp/calcc.c::calc()` lines 324–483 — predistortion math
- `wdsp/iqc.c::xiqc()` lines 122–203 — predistortion application

## 9.5. NR audit follow-up notes (operator-confirmed)

From the NR audit (`docs/architecture/nr_audit.md`) §9 open questions:

- ~~**AC mains frequency at N8SDR's QTH: 60 Hz** (US standard).
  When cyclostationary 60/120 Hz powerline modeling lands (audit
  §4.3(c)), it must be operator-configurable...~~  **OBSOLETE —
  cyclostationary modeling is NOT being pursued.**  See next bullet.

- **CYCLOSTATIONARY POWERLINE MODELING (P2) NOT PURSUED
  (2026-05-02).**  Reviewed after the P1.3 auto-select deferral
  and dropped on operator judgment: "got us into some hopes that
  won't pan out in real-world operator mode."  Reality check —
  AC mains drift (60 ±0.05 Hz under load), the lack of a direct
  line-phase reference at 48 kHz audio, the actual non-coherence
  of typical powerline noise sources (arcing contacts, motor
  commutators, dimmer SCRs each on their own phase), and the
  operator-tunes-around behavior all conspire against the
  audit's optimistic 10-20 dB gain estimate.  Real gain probably
  3-5 dB over the existing Wiener-from-profile path that already
  ships in v0.0.7.x.  Not worth the complexity / schema-bump /
  profile-invalidation risk.  See `docs/architecture/nr_audit.md`
  §4.3(c) STATUS block for the full reasoning.

- **NR polish strategy chosen: P1 (auto-select / staleness /
  smart-guard) → P2 (cyclostationary) → P3 trickles in.**  Skipping
  ML-based VAD (i) since auto-select reduces live-source usage.
  Skipping (j) cross-channel validation pending RX2.

- **AUTO-SELECT EXPLICITLY DEFERRED INDEFINITELY (2026-05-02).**
  Operator decision after senior-engineering review of the
  proposed implementation: captured profiles are operator-curated
  by design (each station / location / operator is unique;
  operator ears pick up things the algorithm can't).  Algorithmic
  auto-select — even in "suggest" mode — overrides operator
  choice with a spectral-distance metric and creates UX noise
  without delivering value.  See `docs/architecture/nr_audit.md`
  §4.3(a) STATUS block for the full reasoning.

  What stays in scope for the captured-profile feature:
    * Operator-driven explicit blending (manual slider in manager)
    * Diagnostic readouts ("this profile is X dB different from
      current band noise") — informational, operator decides
    * Smart-guard improvements (already shipped P1.1)
    * Staleness toast notifications (already shipped P1.2 —
      passive notification, operator decides whether to recapture)

  Out of scope:
    * Any feature where Lyra picks a profile FOR the operator
    * Suggestion toasts the algorithm initiates
    * The math module `lyra/dsp/noise_profile_match.py` was
      prototyped briefly and **removed** as part of the same
      decision — keeps the "no auto-comparison code" principle
      enforced at the file-system level.

## 9.6. Audio-pops quiet-pass v0.0.7.1 (shipped 2026-05-02)

Operator-reported "consistent random pops, some many dB above
audio level."  Senior-engineering audit produced
`docs/architecture/audio_pops_audit.md`; three P0 fixes shipped on
`feature/v0.0.7.1-quiet-pass`:

- **P0.1** AGC per-sample envelope tracker (eb437ae) — replaces
  block-scalar AGC.  Eliminated the loud multi-dB pops.  See
  `_apply_agc_and_volume` + `_refresh_agc_per_sample_constants`
  in `lyra/radio.py`.  Bench: 1 kHz step-amplitude sine, boundary
  step dropped from 0.029 -> 0.0041 (= natural sine slope).
  CPU: ~0.11 ms/block (0.5% of 21 ms budget).
- **P0.2** Preserve decimator state across `channel.reset()`
  (3d0ba70) — was rebuilding the FIR from zeros on every
  freq/mode change, producing a click on every tune.  Bench:
  boundary step 0.100 -> 0.013, recovery 1.35 ms -> 0 ms.
- **P0.3** AK4951 sink-swap 5 ms fade-out (244a8b2) — added
  `HL2Stream.fade_and_replace_tx_audio()` and updated
  `AK4951Sink.close()` to fade gracefully instead of flipping
  `inject_audio_tx = False` instantly.

**Operator flight-test result (2026-05-02):** "noticeably better,
loud spikes gone, but occasional pops/clicks slightly louder than
the rest of audio still happen."

### Residual clicks — PARKED for future investigation

Diagnosis state at park time:
- Reproducible with **all DSP off** (NB / NR / ANF / LMS / SQ /
  APF) at 192 kHz LSB / 2.4 kHz filter.
- **Reproducible into a 50-ohm dummy load** (no antenna), so it's
  not atmospheric / RF / lightning / static.
- Network ruled out: dedicated direct-wired NIC to HL2, lowest
  Windows route metric, no WiFi.
- Most likely remaining sources (in priority order):
  * **HL2 hardware/gateware glitches** — ADC sample dropouts,
    DDC numerical edges, USB-to-ethernet bridge buffer hiccups.
    Specific to N8SDR's HL2+ unit; may differ on other boards.
  * **Python GIL / GC pauses** starving the audio thread, causing
    EP2 underrun and audible step at the underrun-recovery
    boundary.  Plausible but unverified.
  * **Per-sample AGC + Rayleigh noise tail** — the new instant-
    attack tracker can briefly clamp gain on random thermal-
    noise envelope excursions; subsequent samples then show a
    drop in output.  Step is small (~0.02 amplitude) but maybe
    audible on a quiet listening session.

Diagnostic instrumentation already in place
(`set LYRA_AUDIO_DEBUG=1` env var, commit e535db7):
`Radio._diagnose_audio_step` prints one rate-limited log line per
audio block whenever the post-AGC output has a sample-to-sample
step exceeding 0.05 amplitude.  Includes index, prev/curr output,
input mag, peak, gain, and peak ratio at the offending sample.
Use this when picking the investigation back up — operator runs
with the env var, we correlate timestamps with audible clicks,
then implement the targeted fix (e.g., look-ahead AGC, GIL hold-
off, gateware-version triage).

When circling back: read this section, then
`docs/architecture/audio_pops_audit.md` §3 (P1 / P2 suspects we
explicitly didn't ship in v0.0.7.1 but may revisit here).

## 9.7. Click-to-tune v1 — partially shipped, needs refinement

Shipped across v0.0.7.1 → v0.0.7.4:
- Plain click → literal tune (always worked, unchanged from v0.0.7).
- Click+drag → drag-to-pan (rate-limited to ~30 Hz emit cadence
  to avoid backend-pipeline overload).  Working OK per operator
  flight test.
- Shift+click → snap to nearest spectrum peak.  Reticle preview
  on hover.  **Operator verdict (2026-05-02): "a little better —
  needs work."**

What got fixed across the four patch tags:
- `v0.0.7.2` 7b1c79c... `v0.0.7.4`: GPU widget had no drag state
  machine (committed click on press), no `setMouseTracking(True)`
  (hover never fired), and the snap range was a fixed 200 Hz
  (only ~3 px wide at typical wide zoom).  Plus snap target was
  recomputed on RELEASE checking `event.modifiers()` -- if the
  operator released Shift before the mouse the click fell through
  to literal-tune.
- v0.0.7.4 final fix: latch snap target at PRESS time so the
  commit is atomic w.r.t. Shift state.

Known refinement candidates (parked for next session):
1. **"Click misses" residual.**  After the press-time-latch fix,
   operator still reports "a little better, needs work."  Specific
   symptom not yet collected -- possibilities:
   - Parabolic peak interpolation might be off by a few Hz at
     wide zoom (FFT bin width ~47 Hz at 192 kHz / 4096 bins).
     The interpolation gives sub-bin precision but bins are
     finite-width to begin with.
   - Reticle might be drawn at a slightly different position
     from where the snap commits.  The reticle position uses
     the CURRENT span/center (live as you hover) but the snap
     target is a frequency captured at press time -- if span
     changes between hover and press, the visual position can
     drift.
   - Snap might find sidelobes or artifacts instead of true
     peak center.  The argmax inside the search window is the
     local maximum but doesn't validate it's a "real" signal
     vs a noise blip or filter ringing.
2. **Snap range could be smarter.**  Current effective range is
   `max(snap_tune_range_hz=200, SNAP_PIXEL_RADIUS=80 * hz_per_px)`.
   At 192 kHz / 1500 px that's 10240 Hz -- might be too wide
   (snaps to a stronger nearby signal instead of the one
   operator pointed at).  Could cap the pixel radius at e.g.
   3000 Hz to keep snap "closest peak you pointed at" rather
   than "anything strong nearby."
3. **Snap might benefit from a stronger SNR test.**  Current
   threshold is `peak_db - noise_floor_db >= 6 dB`.  Noise floor
   is the 20th percentile of the spectrum, so 6 dB above that is
   a low bar -- weak ambient peaks can pass.  Could raise to
   10 dB or use median + N*MAD instead.
4. **Reticle could drag-track better.**  Currently updates every
   mouseMoveEvent that has Shift held -- which works, but at
   wide zoom tiny cursor jitter can flicker the reticle between
   adjacent peaks.  Could add a small position-stability hold
   so the reticle doesn't twitch.
5. **Settings → Spectrum tab.**  No operator-facing controls for
   snap range / SNR threshold / modifier choice / reticle
   visibility.  Defaults are baked in.  Once the algorithm feels
   right, expose the knobs.

Operator-facing UX is documented in `docs/help/spectrum.md`
("Click-to-tune" section) and `docs/architecture/click_to_tune_plan.md`
(design proposal).

When circling back: ask operator what specifically still feels
wrong (which test case fails -- weak signal? wide zoom? CW
sidelobe pickup?) before tweaking the algorithm.  Each candidate
above has a different fix.

## 9.8. Speaker-selective audio attenuator — WITHDRAWN 2026-05-10

Operator removed from the backlog 2026-05-10: post-WDSP audio
chain (NR Mode 1-4 + AEPF + NPE + ANF + NB + APF + per-band
SQ) handles the original use cases well enough that the
"selectively attenuate one voice in a roundtable" feature is no
longer needed.  Section retained below as historical record so
anyone reading old docs that reference §9.8 can find context,
but no implementation work expected.

**Original entry preserved below, marked WITHDRAWN.**

**Operator-suggested 2026-05-02:** in a roundtable QSO, attenuate
ONE specific operator's voice while keeping the others audible.
Use case: leave the radio on while away from the desk, want
to hear the conversation but skip the operator you don't enjoy.

**Captured-noise-profile feature WILL NOT do this.**  Spectral
subtraction works for stationary noise; voices are non-stationary
and share broad spectral characteristics across speakers.
Subtracting "Bob's voice profile" produces a generic voice-band
EQ cut applied to ALL voices, not Bob-specific suppression.
This is a math constraint, not a tuning issue.

**The right architecture (if we build it):**

- VAD-gated **per-turn classification** — detect voice onset, run
  classifier once 750 ms into the turn, latch decision for the
  rest of the turn (resets on detected silence).
- **Probabilistic attenuator** -- output is
  ``attenuation = score × max_atten_db`` smooth-ramped.  Operator
  hears unwanted voice fading from full level to ~-15 dB; doesn't
  go to silence.  False positives produce mild ducking instead of
  catastrophic Alice-was-muted-during-her-turn outcomes -- crucial
  for the "operator walks away" use case.
- **Multi-profile** -- match against a list of "skip these voices"
  (roundtables often have 2-3 operators to skip).
- **"Tag this voice" hotkey** -- operator hears Bob, presses key,
  current 5-10 sec captured as profile, auto-engaged.  No prep
  required.

**Two scorer options:**

1. Pure NumPy multi-feature (LTAS + F0 stats + spectral tilt +
   formants + speaking rate).  ~1 week dev.  70-85% accuracy on
   same-gender / similar-mic speakers.  Borderline for
   "leave-the-radio-on" reliability.
2. Pretrained ONNX speaker embedding (ECAPA-TDNN-class).  ~2-3
   weeks dev including license / bundling.  90-98% accuracy.
   Adds onnxruntime dep + ~10 MB ONNX model in assets/.
   Recommended for unattended-listening use case.

**Why we're NOT building this now:**

- Not a v0.0.x quiet-pass tweak; substantial new feature surface
  (capture UX, profile management, scoring engine, attenuator
  gate).
- Right after RX2 (v0.0.9) and TX (v0.1) is the natural slot
  -- earlier and we're spreading bandwidth too thin.
- Unattended-listening reliability bar is high enough that
  Approach 2 (ML) is the right target, which means the licensing
  + ONNX dep work is a hard prerequisite.
- Operator categorized this as "niche, might be interesting"
  rather than blocking pain.

**Status:** parked.  No design doc written yet.  When circling
back, write `docs/architecture/speaker_filter_design.md` first
(the probabilistic-attenuator math, ECAPA-TDNN evaluation
checklist, license review for permissive ONNX models, capture +
profile UX flows).  THEN implement on a feature branch alongside
v0.2 work or as a v0.2.x post-PureSignal feature.

Pre-park reasoning lives in this conversation thread (operator
asked, I gave the senior-engineering analysis, operator parked).
If reading this in a future session, the analysis was: spectral
subtraction is the wrong tool, per-turn classification with
probabilistic attenuation is the right tool, ECAPA-TDNN-class
embeddings are the right scorer, ham radio's stable-mic property
makes accuracy slightly better than general-voice benchmarks
suggest.

## 10. Open empirical questions (need HL2+ bench testing)

These weren't answered by code-reading; we'll find out on N8SDR's
hardware:

1. **HL2 mic samples in EP6 with AK4951 audio active** — value or
   zero?  Affects v0.1 mic-input source choice.
2. **DDC2/DDC3 sample rate during PS+TX** — Thetis sets RX1 rate but
   actual gateware delivery is TBD.  Wireshark a PS+TX session.
3. **HL2 PA-on bit power-up default** — is `pa & 1` set by gateware
   on power-up, or do we need to assert it?
4. **PA fwd/rev power calibration constants** — vary per HL2 board
   revision.  Operator self-cal in Settings → TX is the right answer.
5. **N8SDR's specific HL2+ gateware version** — document for future
   reference.
6. **AK4951 EP2 cadence behavior** — does HL2 gateware drop or buffer
   EP2 frames over the 48 kHz cadence?  Affects TX queue throttling.

## 11. Workflow conventions

### Branching

- `main` = published release branch, fast-forward-able with
  feature/threaded-dsp.
- `feature/threaded-dsp` = active dev trunk.
- New feature work: create `feature/<topic>` off
  feature/threaded-dsp; merge back when stable.

### Commits

- Use conventional summary line ("RX2: ...", "TX: ...", "PS: ...")
  for easy grep.
- Include "Co-Authored-By: Claude Opus 4.7" trailer per existing
  pattern.

### Releases

Numbered steps so nothing slips through the cracks — this list
exists because the v0.0.9.6 through v0.0.9.9 releases all
skipped step 8 (push to main), leaving anyone tracking
``origin/main`` pulling v0.0.9.5 code while four feature releases
piled up on the feature branch.

1. **Bump version** in two places: `lyra/__init__.py`
   (`__version__`, `__version_name__`, and flip `__build_date__`
   from ``"dev"`` to today's `YYYY-MM-DD`) and
   `build/installer.iss` (`LyraVersion`, `LyraVersionName`,
   `LyraBuildDate`).
2. **Update `CHANGELOG.md`** — new dated entry at the top
   (consolidated; replaces per-version RELEASE_NOTES files).
3. **Update `CLAUDE.md`** version-numbering history near the top
   of the file.
4. **Commit** the version bump.
5. **Annotated tag** with release notes:
   `git tag -a v0.0.X -m "..."`.
6. **Build** via `build/build.cmd` (PyInstaller + Inno Setup).
   Verify installer lands at `dist/installer/Lyra-Setup-X.Y.Z.exe`.
7. **Push feature branch + tag**:
   `git push origin <feature-branch>` then
   `git push origin v0.0.X`.
8. **Push to main** (the step that was missing from v0.0.9.6
   through v0.0.9.9): `git push origin <feature-branch>:main`,
   which fast-forwards `origin/main` to the release commit
   without needing a local main checkout.  If you skip this,
   GitHub's web UI shows the release correctly but
   `git pull origin main` returns stale code — anyone tracking
   main is reading v0.0.9.5 while installers up through v0.0.9.9
   ship.
9. **Create GitHub Release** manually (or via `gh release create`
   if the CLI is installed): tag = `v0.0.X`, title = `v0.0.X —
   <Version Name>`, body = release notes, attach the
   `Lyra-Setup-X.Y.Z.exe` from `dist/installer/`.

``build/build.cmd`` prints a reminder of this sequence after the
build completes — if a step is missed, the cmd-window output is
the place to spot it.

### Pre-releases for tester feedback

- Cut pre-releases per phase during long features (worked well for
  v0.0.6 / v0.0.7).
- v0.0.9 phases: 0 (refactor), 1 (protocol), 2 (audio), 3 (UI),
  4 (panadapter), 5 (polish).  One pre-release per phase.

## 12. How to point Claude back to these docs

When starting a new session for RX2/TX/PS implementation work, you
can prompt me with any of:

- **"Read CLAUDE.md"** — auto-loaded, but you can ask me to re-read
  it explicitly if you want me to refresh.
- **"Read docs/architecture/implementation_playbook.md"** — full
  authoritative spec.
- **"Read the RX2 research notes"** / **"Read the PS research"** —
  the longer-form research documents.
- **"What does Thetis do for X in HL2?"** — I'll either remember from
  these docs or grep the Thetis tree at
  `D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\`.
- **"Show me the WDSP source for X"** — I'll read from
  `D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13\Project Files\Source\wdsp\`.

For specific implementation work, give me the phase number from §7
and I'll know what's in scope.  For example: "Start v0.0.9 Phase 0"
means multi-channel refactor with no behavior change.

When something I do conflicts with this doc, **trust this doc over
my session memory** — this is the consolidated source of truth.  If
this doc is wrong, we update it explicitly.

---

## 13. Audio architecture (locked 2026-05-06)

After multiple deep dives that kept circling, an operator review
of the Thetis source tree + Thetis settings database produced the
canonical answer.  See `docs/architecture/audio_architecture.md`
for the full reasoning trail; below is the operative summary.

### 13.1 The two audio paths

**Path A — HL2 onboard codec via EP2 (DEFAULT for HL2 hardware).**

```
HL2 IQ  →  Lyra DSP chain  →  L/R audio in EP2 frames  →  back to HL2  →  onboard codec  →  headphone jack
```

This is the path Thetis defaults to for HermesLite hardware
(`audioCodecId = HERMES`, `cmsetup.c:75`).  Single crystal (the
HL2's), zero clock drift, no resampler needed.  Lyra has called
this "AK4951 mode" through v0.0.9.5; **v0.0.9.6 renames it to
"HL2 audio jack"** since not all HL2 revisions use the AK4951
specifically but all use the same EP2 codec path.

**Path B — Host PC sound card via SoundDeviceSink.**

```
HL2 IQ  →  Lyra DSP chain  →  WDSP rmatch (PI loop) → varsamp →  ring buffer  →  WASAPI/PortAudio  →  PC speakers
```

Required for:
- HL2 operators who can't or don't want to use the codec path
- ANAN family (v0.4) which has no onboard codec at all
- Audio routing to other apps (digital mode software, recording)

### 13.2 Why two paths

- Thetis's primary audio path (HermesLite) is HERMES-only.  It
  doesn't even *implement* WASAPI for output (`netInterface.c:
  1757-1759 — case WASAPI: // not implemented`).  Thetis's
  PC-soundcard support is ASIO via `cmasio.c`, which uses the
  same rmatch/varsamp adaptive resampler chain that Path B
  needs.
- The HL2 onboard codec path is single-crystal, so there's no
  rate mismatch to compensate for.  Operators who can use it
  get glitch-free audio for free, no DSP overhead.
- The PC sound card path has fundamental two-clock drift
  (HL2 crystal vs sound card crystal, both nominally 48 kHz,
  both ±50 ppm tolerance).  Without an adaptive resampler the
  ring buffer fills (overrun) or drains (underrun) over time.
  This is what produced operator-reported audio glitches in
  Lyra v0.0.9.x PC Soundcard mode.

### 13.3 The WDSP-port-not-Thetis-copy principle (restated)

Lyra is GPL v3+, WDSP is GPL v3+.  License-compatible.  WDSP
is its own GPL'd DSP project that Thetis happens to use; Lyra
ports directly from WDSP with attribution.  **This is not
"ripping from Thetis."**  Same pattern as `agc_wdsp.py` (port
of `wcpAGC.c`), `nr.py` (`anr.c`/`emnr.c`), `nr2.py` (`emnr.c`),
`lms.py`, `anf.py`, `nb.py` — all already shipped.

What we DO port (with attribution comment per
`docs/architecture/wdsp_integration.md`):

| When | WDSP file | Lyra target | LOC | Unblocks |
|---|---|---|---|---|
| **v0.0.9.6** | `aamix.c` | (not ported) `lyra/dsp/audio_mixer.py` is a Lyra-native dispatcher thread, NOT a NumPy port — `mix.py` retracted Phase 0 per v0.1 plan §5.1 IM-4 | — | RX1+RX2 mix routing (dispatcher only; mixing math = WDSP) |
| **v0.0.9.6** | `varsamp.c` | `lyra/dsp/varsamp.py` | ~400 | PC sound card drift, ANAN audio |
| **v0.0.9.6** | `rmatch.c` | `lyra/dsp/rmatch.py` | ~700 | PI control loop on top of varsamp |
| **v0.0.9.6** | `patchpanel.c::SetRXAPanelPan` | (cffi-only) `wdsp_engine.RxChannel.set_panel_pan` | — | RX2 stereo pan curve (NOT ported to Python — see v0.1 plan §5.1 IM-4) |
| v0.2 | `compress.c` | `lyra/dsp/tx_compressor.py` | ~150 | TX compressor |
| v0.2 | `eqp.c` | `lyra/dsp/eq.py` | ~300 | Parametric EQ (RX + TX) |
| v0.2 | `delay.c` | `lyra/dsp/delay_line.py` | ~80 | TX delay matching, PS feedback |
| v0.3 | `iqc.c` | `lyra/dsp/ps_iqc.py` | ~315 | PS predistortion application |
| v0.3 | `calcc.c` | `lyra/dsp/ps_calcc.py` | ~1164 | PS calibration math |
| v0.3 | `lmath.c::xbuilder` | `lyra/dsp/ps_xbuilder.py` | ~200 | Cubic-spline PS coefficient |

What we DO NOT copy (these are Thetis-specific glue, not WDSP
algorithms):

- `Console/console.cs` — study `UpdateDDCs` etc. as reference,
  write Lyra-native equivalents.
- `Console/PSForm.cs` — study the state machine, write Lyra-
  native (`lyra/ui/ps_dialog.py`).
- `ChannelMaster/networkproto1.c`, `cmaster.c`, `network.h` —
  study the protocol bit layouts in CLAUDE.md §3, write Lyra-
  native (`lyra/protocol/stream.py`).
- `Console/HPSDR/IoBoardHl2.cs` — study HL2 I/O quirks, write
  Lyra-native.

What we DO NOT port from WDSP because Python+NumPy+Qt does it
natively or differently:

- `analyzer.c` — Lyra has its own GPU spectrum widget.
- `channel.c` — buffer mgmt; GIL handles it.
- `main.c` — Win32 thread mgmt; Python threading.
- `RXA.c`/`TXA.c` — channel scaffolding; Lyra has its own.

### 13.4 Hardware capability struct (extends §6.7)

The hardware-abstraction discipline in §6.7 needs an audio
field added when v0.4 work begins:

```python
@dataclass
class RadioCapabilities:
    nddc: int                        # advertised DDC count
    has_onboard_codec: bool          # HL2 = True, ANAN = False
    default_audio_path: AudioPath    # HL2 = HL2_CODEC, ANAN = PC_SOUND
    puresignal_requires_mod: bool    # HL2 = True, ANAN G2 = False
    tx_attenuator_range: tuple[int, int]   # HL2 = (-28, 31), ANAN = (0, 31)
    cwx_ptt_bit_position: int        # HL2 = 3, ANAN = standard
    # ...
```

When Lyra opens a connection, the protocol module populates
this struct.  UI defaults read from it.  Settings UI lets the
operator override per-radio (e.g., HL2 operator who prefers PC
sound card despite having a codec).

### 13.5 What this changes about RX2 / TX / PureSignal plans

- **RX2 (v0.1):** No change.  Stereo split via EP2 LR bytes
  through HL2 codec, exactly as planned in §6.1.  The
  `aamix.c` port for v0.0.9.6 is the prerequisite that makes
  RX2 work when it lands.
- **TX (v0.2):** No change.  Default mic input is HL2 mic jack
  via EP6 (single crystal, no drift).  PC mic becomes opt-in
  for ANAN-class hardware in v0.4 — that path uses the same
  rmatch+varsamp from v0.0.9.6 for input-side rate matching.
- **PureSignal (v0.3):** No change.  HL2 PS feedback is on
  DDC2/DDC3 at `rx1_rate` per §3.8 — single crystal, no drift.
  Different DDC rates (e.g., ANAN's 192 kHz `ps_rate` vs the
  user-selected RX rate) is a rate-conversion problem solved
  by the v0.0.9.6 varsamp port.

The audio infrastructure ships once (v0.0.9.6) and gets used
three more times (RX2 stereo, TX mic input on ANAN, PS rate
conversion).

---

## 14. WDSP-DLL integration architecture (added 2026-05-06)

The audio-quality work in v0.0.9.6 pivoted from "port WDSP modules
into Python" to "call into the WDSP DSP engine via cffi."  This
section is the operative reference for how that's wired.

### 14.1 Files that matter

| File | Role |
| --- | --- |
| `lyra/dsp/_native/` | Bundled native DLLs (~16 MB total): `wdsp.dll`, `libfftw3-3.dll`, `libfftw3f-3.dll`, `rnnoise.dll`, `specbleach.dll` |
| `lyra/dsp/wdsp_native.py` | cffi cdef declarations + DLL loader. Search order: explicit `dll_dir` arg → `LYRA_WDSP_DIR` env var → bundled `_native/` → fallback Thetis-HL2 install dirs (dev convenience only). |
| `lyra/dsp/wdsp_engine.py` | High-level Python wrapper: `RxChannel`, `RxConfig`, `RxaMode`, `AgcMode`, `MeterType`. Stable API surface for Radio. |
| `lyra/radio.py` `_open_wdsp_rx`, `_do_demod_wdsp`, `_wdsp_*_for` helpers | Integration into Radio. Default ON; `LYRA_USE_LEGACY_DSP=1` falls back. |
| `lyra/dsp/worker.py` `process_block` | Worker-mode dispatch into `_do_demod_wdsp` + still calls `_maybe_run_fft` so panadapter is fed. |
| `scratch/wdsp_port_status.md` | Living status doc. |
| `scratch/test_wdsp_poc.py` | Standalone PoC. Run to verify the engine path is healthy without launching the full app. |

### 14.2 What's wired vs what's pending

**Wired (works in WDSP mode):**
- RX1 audio: IQ in → WDSP RXA → 48 kHz stereo audio → audio sink
- Mode: USB / LSB / AM / FM / CWU / CWL / DSB / SAM / DIGU / DIGL / DRM / SPEC
- RX bandwidth (per-mode, propagates filter freqs to NBP0 + BP1)
- Rate change (closes + reopens WDSP channel at new in_rate)
- AGC mode + the operator picker (Off / Fast / Med / Slow / Auto /
  Custom) via SetRXAAGCMode.  ``"long"`` is fully wired in
  ``radio.py`` but currently NOT exposed in the ``_AGC_PROFILES``
  right-click menu — see §15.5 to re-add (one-line change in
  ``panels.py``).  Auto profile additionally runs
  ``auto_set_agc_threshold`` on a 1-sec timer to re-calibrate
  ~18 dB above the rolling noise floor.
- AGC gain readout (GetRXAMeter / RXA_AGC_GAIN, throttled to ~6 Hz)
- AGC threshold + AF gain wiring (SetRXAAGCThresh + WDSP PanelGain1
  per Phase 6.A1/A3 fixes during the v0.0.9.6 cleanup arc)
- **NR-mode UX**: 4-position picker (Mode 1 / 2 / 3 / 4) mapping
  to WDSP gain methods 0..3 (Wiener+SPP / Wiener simple / MMSE-LSA
  default / Trained adaptive) + AEPF anti-musical post-filter
  + NPE method picker (OSMS / MCRA / etc.).  See §14.7.
- ANF (auto-notch) — profile picker + μ slider mapped to
  ``SetRXAANFVals`` (Phase 6.A4).
- LMS (independent toggle, μ slider drives WDSP ANR step size).
- All-mode squelch via WDSP SSQL (SSB/CW/DIG/SPEC), FMSQ (FM),
  AMSQ (AM/SAM/DSB) — see §14.8.  Threshold sliders mapped
  per-module.
- Manual notches (right-click on spectrum) — wired via
  ``RXANBPAddNotch`` / ``DeleteNotch`` / ``SetNotchesRun`` /
  ``SetTuneFrequency`` (Phase 6.A4).
- NB (noise blanker) — ``create_nob`` / ``create_anb`` initialized
  in ``RxChannel.__init__``; profile picker drives NOB threshold
  via ``_push_wdsp_nb_state`` (xnobEXT / xanbEXT splice into the
  IQ path).
- Binaural (BIN) Hilbert phase split — runs as Python post-
  processor on WDSP's stereo output, both HL2-jack and
  PC-Soundcard paths.
- APF (CW peaking, mode-gated to CWU/CWL) via WDSP SetRXABiQuad
  SPEAK biquad — center freq tracks ``cw_pitch_hz`` in audio
  domain.
- CW pitch (refilters BP1 + NBP0 + SNBA collectively via
  RXASetPassband when active mode is CWU/CWL; under v0.0.9.8's
  carrier-freq VFO convention also re-pushes the DDS-vs-VFO
  offset so the operator's tuned carrier stays inside the
  passband at the new pitch).
- Volume + mute (applied in Python after WDSP).
- TCI audio tap (applied in Python after WDSP).
- TPDF dither on float→int16 quantization for HL2 audio jack.
- S-meter peak-hold smoothing (~500 ms decay) — Python-side
  fast-attack / slow-release on the FFT-derived meter.
- Spectrum / panadapter / waterfall + per-band bounds memory
  (incl. waterfall min/max as of v0.0.9.7) + carrier-freq VFO
  convention with central DDS offset (v0.0.9.8 — see §15.6
  trailer / version-numbering history).
- Captured noise profile capture + apply (v0.0.9.9 §14.6 Phase 4
  IQ-domain rebuild) — both halves run pre-WDSP via
  ``CapturedProfileIQ`` (``lyra/dsp/captured_profile_iq.py``);
  v2 schema with rate-specific full complex-FFT magnitudes
  (``lyra/dsp/noise_profile_store.py`` SCHEMA_VERSION = 2);
  v1 audio-domain profiles refused on load with recapture hint;
  cross-rate / cross-FFT-size profiles refused with
  operator-friendly errors.

**Inert in WDSP mode (deferred):**
- NR3 (RNNoise) and NR4 (Spectral Bleach).  ``rnnoise.dll`` and
  ``specbleach.dll`` are bundled but no operator UI is wired
  yet.  Adding a fifth and sixth NR mode to the picker is a
  small task once a tester asks for it.
- Audio Leveler — DELETED in the v0.0.9.6 cleanup arc (Phase 4).
  WDSP AGC subsumed its dynamic-range function; the
  ``lyra/dsp/leveler.py`` source is gone.  RX2 plan §7.x still
  references it at a few spots — see §15.2 backlog item.
- TX (Phase v0.2) and PureSignal (Phase v0.3) — entire chains
  are out of scope for the v0.0.9.x line; first TX work begins
  with v0.1 RX2 finished.

**Crucial gotcha — WDSP filter convention:**
WDSP's USB filter at `(+200, +3100)` selects content from the
**negative** baseband, and LSB filter at `(-3100, -200)` selects
**positive** baseband. (Internal NCO/demod sign flip in WDSP.)
HL2 baseband is mirrored: USB-RF lands at negative baseband, LSB
at positive. The two flips cancel out. So we hand HL2 IQ to WDSP
**unmodified** and get correct sideband selection — the same way
Thetis does. An earlier `np.conjugate(iq)` "compensation" was
WRONG and produced reversed sidebands; do not re-add it without
re-verifying with the synthetic-tone PoC.

**Crucial gotcha — bandpass dispatch:**
WDSP has TWO bandpass filters in the RXA chain. `BP1`
(`SetRXABandpassFreqs`) is post-NR and only RUNS when AM/SAM/EMNR/
ANR/ANF/SNBA is on. `NBP0` (`RXANBPSetFreqs`) is front-of-chain
and always runs. SSB sideband selection lives in NBP0. The
`RXASetPassband` collective updates BOTH (plus the SNBA output
filter) and is what we call from `RxChannel.set_filter()`.
**Do not** call `SetRXABandpassFreqs` directly for sideband
selection — with all DSP off, BP1 is bypassed and the call is
silently ignored.

**Crucial gotcha — OpenChannel "block" parameter:**
The 13th parameter to `OpenChannel` is a "block until output
available" flag, not a CW BFO offset. Pass 1, not 0. The WDSP
source comment is `// block until output available`. Passing 0
makes `fexchange0` non-blocking and the output buffer can return
stale data.

**Crucial gotcha — output buffer size:**
`fexchange0` writes `out_size = in_size * out_rate / in_rate`
frames, NOT `in_size` frames. With in_size=1024 IQ at 192 kHz
and out_rate=48 kHz, the output buffer holds 256 frames of audio,
not 1024. Allocating 1024 leaves uninitialized memory in the
trailing 768 slots and produces a buzzing "electrocuted" sound
at the block rate. `RxChannel.__init__` computes `out_size`
correctly; don't override it.

### 14.3 Threading model with WDSP

Same as §5 except the per-RX DSP heavy work moves into WDSP's
own internal thread (created by `_beginthreadex` inside the DLL,
not visible to Python). The Python worker thread (B.x changes)
still runs `process_block` per IQ batch but the actual DSP
arithmetic is GIL-free C now. That's the architectural fix that
ended the click / motorboat saga: Python's writer / sink threads
no longer compete with the DSP for the GIL.

### 14.4 Deferred / open work — RX1 polish push 2026-05-07 status

**Items done this session (2026-05-07 RX1 polish):**

1. ~~**PC Soundcard CPU optimization.**~~ ✓ DONE — `WdspRMatch`
   class in `lyra/dsp/rmatch.py` cffi-wraps the bundled DLL's
   `xrmatchIN`/`xrmatchOUT`.  `SoundDeviceSink` picks it
   automatically when WDSP loads, falls back to pure-Python
   `RMatch` otherwise.  Operator-confirmed CPU very close to
   HL2-jack mode.

2. ~~**NB (noise blanker) wiring.**~~ ✓ DONE — `create_nob`/
   `create_anb` cffi bindings added; `RxChannel.init_blankers`
   runs in `__init__`; `xnobEXT`/`xanbEXT` actually splice into
   the IQ path before `fexchange0` (the `SetEXTNOBRun(1)` flag
   alone is just a marker).  Profile mapping (off/light/medium/
   heavy/custom) drives NOB threshold via `_push_wdsp_nb_state`.

3. ~~**Manual notches.**~~ ✓ DONE — `RXANBPAddNotch` /
   `RXANBPDeleteNotch` / `RXANBPSetNotchesRun` /
   `RXANBPSetTuneFrequency` wired through
   `RxChannel.set_notches` / `set_notches_master_run` /
   `set_notch_tune_frequency`, hooked into `notches_changed`
   signal in radio.py.

4. **Captured noise profile + APF + Leveler + BIN — split decisions.**
   - **APF** ✓ WIRED via WDSP `SetRXABiQuad*` (the SPEAK biquad).
     Mode-gated to CWU/CWL.  Operator-confirmed "+12 dB measured
     at +12.2 dB" working.
   - **BIN** ✓ WIRED as Python post-processing on WDSP's stereo
     output, in BOTH HL2 audio jack and PC Sound paths.  PC Sound
     required complex-rmatch routing (L into I, R into Q) so
     channels survive rate-matching independent.
   - **Leveler** ✓ DROPPED — WDSP AGC subsumes it.
   - **Captured noise profile** ✓ **WIRED — IQ-domain (v0.0.9.9
     §14.6 Phase 4)**.  Both capture and apply run pre-WDSP on
     raw IQ; the operator-driven "use captured" toggle now
     enables real spectral subtraction at the IQ layer.  Operator
     hears noise floor drop ~6-12 dB.  Three earlier post-WDSP
     audio-domain attempts in v0.0.9.6 produced AGC-mismatch
     artifacts and were reverted; the IQ-domain rebuild
     sidesteps that interaction (see §14.6 below for the full
     trail).

5. **Cleanup pass.** Once RX/TX/PS are all on the native engine,
   audit `lyra/dsp/agc_wdsp.py`, `nr.py`, `anf.py`, `lms.py`,
   `demod.py`, `channel.py`, `leveler.py`, `apf.py` for what's
   still doing real work vs dead code reachable only via
   `LYRA_USE_LEGACY_DSP=1`.  Modules to KEEP regardless:
   `wdsp_native.py`, `wdsp_engine.py`, `audio_sink.py`,
   `audio_mixer.py`, `binaural.py`, `rmatch.py`, `varsamp.py`,
   `noise_profile_store.py`, `nr2.py` (used by capture path),
   `worker.py`, `squelch.py`.  (`mix.py` was on this keep-list
   through v0.0.9.9 but was retracted Phase 0 of v0.1 per plan
   §5.1 IM-4 — see §13.3 port-table row for `patchpanel.c`.)
   See `docs/architecture/measurements_and_cleanup.md` for the
   four-phase plan.

**Items still pending — not started:**

6. **TPDF dither on HL2 audio quantization.** ✓ DONE 2026-05-07
   — `_quantize_to_int16_be` in `lyra/protocol/stream.py`.
   Operator-confirmed harshness gone.

7. **S-meter peak-hold smoothing.** ✓ DONE 2026-05-07 —
   fast-attack/slow-release with ~500 ms decay constant.
   Operator-tunable via `_SMETER_PEAK_DECAY`.

8. **WDSP-native S-meter switch.** Bigger structural fix per
   Thetis A/B research: drop the FFT-derived meter, use
   `_wdsp_rx.get_meter(MeterType.S_PK) + cal + LNA`.  Cal trim
   would drop from ~+28 dB → ~+1 dB (Thetis HL2 default 0.98).
   Operator's manual cal of 59.5 dB to match Thetis on WWV is
   working well enough that this is now optional.  Documented
   in `docs/architecture/measurements_and_cleanup.md`.

### 14.4.1 Hot points to investigate when picking back up

* See §14.6 for the captured-profile-apply known issue + the
  IQ-domain architectural plan (NEW — replaces the failed
  post-WDSP audio-domain attempts).
* See §14.7 for the NR-mode UX overhaul status (in operator
  testing as of 2026-05-07 evening).
* RX2 work (v0.1) needs the audio-mixer plumbing already in
  `audio_mixer.py` to be exercised — we built the dispatcher
  thread but haven't driven a second WDSP channel through it.
  Per-channel pan / mute / gain math lives in WDSP cffi
  (`SetRXAPanelPan`, `SetRXAPanelGain1/2`), not in Python.
* TX (v0.2) will need a sibling `wdsp_tx_engine.py` modeled on
  `wdsp_engine.py`, plus the protocol-layer power scaling per
  `docs/architecture/measurements_and_cleanup.md` §2.2.

### 14.5 Where to look when something's off

* **Engine won't load** — DLL set missing or wrong arch. Check
  `lyra/dsp/_native/`. Confirm five files: `wdsp.dll`,
  `libfftw3-3.dll`, `libfftw3f-3.dll`, `rnnoise.dll`,
  `specbleach.dll`. cffi error message names the missing DLL.
* **Audio is silent** — `LYRA_USE_LEGACY_DSP` set inadvertently?
  Check `Radio._use_wdsp_engine` is True. Then check
  `_wdsp_rx is not None`.
* **USB and LSB swapped** — someone re-added the conjugation.
  Don't.
* **Panadapter is dead but audio works** — worker mode bypassed
  the FFT stage. `worker.py` `process_block`'s WDSP branch must
  fall through to `_maybe_run_fft(samples)` before returning.
* **Buzzing tone, no usable audio** — output buffer size wrong.
  Confirm `RxChannel.out_size` matches `in_size * out_rate /
  in_rate` (when in_rate ≥ out_rate).

### 14.6 Captured-profile IQ-domain rebuild (v0.0.9.9)

**Status as of v0.0.9.9 Phase 4 (2026-05-10):** the IQ-domain
rebuild is **LIVE in WDSP mode**.  Capture taps raw IQ pre-WDSP
(``Radio._do_demod_wdsp`` → ``CapturedProfileIQ.accumulate``),
apply runs Wiener-from-profile spectral subtraction on raw IQ
also pre-WDSP (``CapturedProfileIQ.apply``), the cleaned IQ goes
to ``_wdsp_rx.process``.  Operator-perceptible noise reduction
~6-12 dB depending on band conditions and mask floor (default
-12 dB).  Phase 5 still pending: Settings → DSP FFT-size dropdown
(1024/2048/4096) and DSP+Audio panel badge polish for the v2
metadata.

**Schema:** profiles are v2 (``noise_profile_store.SCHEMA_VERSION
= 2``), domain ``"iq"``, full complex-FFT magnitudes (``fft_size``
floats), with per-profile ``rate_hz`` field.  v1 audio-domain
profiles from before v0.0.9.6's WDSP cleanup arc are refused on
load with a clear "recapture in v0.0.9.9+" hint
(``noise_profile_store.load_profile``).

**Historical context — what this rebuild replaced** (preserved
below for reference; the post-WDSP audio-domain path described
here is gone):

In WDSP mode the operator could capture noise profiles
(Cap button worked, profiles saved / loaded / persisted), but
enabling "use captured profile" did NOT apply spectral subtraction
to the audio.  Capture half worked, apply half didn't.  Operator
saw a status-bar warning at the moment of toggle.

**What we tried (2026-05-07 evening):**
1. First pass — wired `nr2.process()` as a Python post-WDSP audio
   stage, gated on `is_using_captured_source()`.  Operator reported
   crackle / pop on voice content.
2. Added temporal smoothing on the Wiener-from-profile gain mask
   (gated on the existing `musical_noise_smoothing` toggle).
   Modest improvement; operator still heard artifacts.
3. Added auto-VAD (`speech_aware = True`) for the WDSP captured-
   profile path.  Per-block flip caused UI readback inversion
   (NR1/NR2 labels swapping with VAD/captured), and operator still
   heard a steady tick + tonal drift even with all NR backends off
   — proving the artifacts are structural in the path, not parameter-
   tunable.

**Why fixes didn't stick:**

WDSP's AGC operates inside `fexchange0`.  Audio coming out of WDSP
is post-AGC, with dynamic levels driven by AGC's gain loop.  When
we apply spectral subtraction on top of that audio using the
captured profile (which represents noise levels at capture time),
the captured noise reference is mismatched against the live audio's
AGC-modulated noise floor.  The Wiener-from-profile gain math
swings rapidly per FFT frame in response — that's the tick.  No
amount of post-processing smoothing fully fixes it because the
underlying mismatch is between a static captured reference and a
dynamic live noise floor.

**The right architecture (operator-confirmed direction
2026-05-07 evening):** feed the captured profile into the IQ
chain BEFORE WDSP's AGC, NOT as a post-WDSP audio-domain pass.
Specifically — pre-WDSP IQ-domain spectral subtraction:

* **Capture path:** at capture time, FFT raw IQ blocks (192k or
  whatever rate is active), accumulate per-bin magnitudes, store
  as the captured profile.  This captures the IQ baseband noise,
  NOT audio-domain noise.
* **Apply path:** at runtime, FFT each IQ block in `_do_demod_wdsp`
  (or before WDSP's `process()`), subtract the captured profile in
  IQ-magnitude domain via Wiener-from-profile gain, IFFT back to
  IQ time domain, then hand the cleaned IQ to `fexchange0`.  This
  happens BEFORE WDSP's AGC and demod — sidesteps the AGC-mismatch
  that broke the post-WDSP audio-domain attempts.
* **Bonus:** IQ-domain captures are MODE-INDEPENDENT (same profile
  works for SSB/CW/AM/FM since the baseband noise pattern is the
  same regardless of demod choice).
* **Cost:** profiles become RATE-SPECIFIC (192k vs 96k vs 48k all
  need their own profile, since baseband bin structure differs).
  Profile storage needs a rate field.  `noise_profile_store.py`
  schema-bump.
* **Implementation cost:** ~2-3 days of focused work:
  1. New capture flow tapping IQ pre-WDSP instead of audio post-WDSP
  2. Apply flow with proper STFT overlap-add to avoid block-boundary
     artifacts
  3. Profile storage update for rate-specificity
  4. Testing across modes / rates / bands

**Operator's preference (2026-05-07 evening):** keep the captured-
profile feature alive in WDSP mode — it's a Lyra niche they value.
Park the apply path until we can do IQ-domain properly.  In the
meantime:

* Cap button still records data + saves profiles to QSettings.
* Profile manager still loads/lists them.
* Use-captured-profile toggle fires status-bar warning in WDSP mode.
* In legacy mode (`LYRA_USE_LEGACY_DSP=1`), captured-profile applies
  normally as before.

**Other paths previously considered (not preferred):**

1. Patch WDSP to expose `SetRXAEMNRNoiseProfile(channel, mag, n)`
   or similar.  Requires maintaining a Lyra-flavored WDSP build —
   ongoing maintenance burden.
2. Skip captured-profile entirely in WDSP mode permanently.
   Rejected by operator — feature is wanted.

**When IQ-domain implementation work begins:**

* Read `scratch/wdsp_port_status.md` first for per-attempt fix
  history (3 failed approaches today) so we don't redo failed
  paths.
* See `_do_demod_wdsp` in `radio.py` for where the apply pass
  USED to live (post-WDSP, audio domain — failed approach).
* See `Radio.set_nr_use_captured_profile` for the existing
  runtime status-bar warning.
* New path: tap IQ in `_do_demod_wdsp` BEFORE `_wdsp_rx.process(iq)`,
  apply spectral subtraction, hand cleaned IQ to WDSP.  Capture
  path needs equivalent IQ tap.
* Block-boundary handling: STFT with 50% overlap-add (Hann window,
  COLA-perfect reconstruction) — same pattern as `nr2.py`'s
  audio-domain implementation.

**Operator-visible behavior in v0.0.9.9 (Phase 4 LIVE):**

* Capture button works (countdown, save dialog) — captures raw
  IQ pre-WDSP at the operator's current rate.
* Captured profiles persist across sessions.
* Toggle "use captured" on → spectral subtraction is applied
  to the IQ stream BEFORE WDSP's RXA chain.  Operator hears
  the noise floor drop ~6-12 dB depending on band conditions.
* INERT status warning REMOVED — apply path is no longer inert.
* Cross-rate profile load → refused with operator-friendly
  "captured at X Hz, current rate is Y Hz, switch back or
  recapture" message (v2 profiles are rate-specific by design).
* Cross-FFT-size profile load → similar refusal message.
* Legacy mode (``LYRA_USE_LEGACY_DSP=1``) — env var no longer
  has any effect (cleanup arc deleted the legacy DSP path).
  v1 audio-domain profiles on disk from pre-v0.0.9.6 → refused
  on load with clear "recapture in v0.0.9.9+" hint.

#### Toggle-pattern UX for §14.6 (operator design lens, 2026-05-09)

When the IQ-domain rebuild lands, the **operator-facing UX should
mirror the NPE picker** — a Settings checkbox or two-way switch on
the DSP+Audio panel:

```
Settings → DSP → Noise reference
  ( ) Off — use WDSP's built-in noise tracker (default)
  ( ) Use captured profile — your QTH-specific spectrum
       Profile: [WX-2026-05-08-7250kHz-quiet ▾]
```

Same as NPE: operator picks "stock algorithm" or "their thing"
depending on which sounds better at the current band conditions.
The captured profile is genuinely operator-specific data (your QTH's
noise floor at that band, that time of day, that antenna), so
flipping the toggle produces a real audible difference — unlike a
hypothetical "trained vs untrained zetaHat" toggle which would be
theater (those datasets are bit-exact identical; see investigation
below).

**Why this is the right framing:**

Operators already understand the NPE pattern (Mode 1-4 mode-of-the-
gain-function picker + AEPF on/off + NPE method picker — three
operator-tunable knobs over WDSP's stock algorithm).  Adding
"reference profile picker" as a fourth knob fits the same mental
model: pick the noise model that matches your situation.

**Implementation hook:**

A `Radio._noise_reference_mode` enum-ish setting:
* `"stock"` — WDSP's noise tracker (current behavior)
* `"captured"` — apply pre-WDSP IQ-domain spectral subtraction
  using the operator-selected captured profile

The `set_nr_use_captured_profile` method already exists and fires
the status-bar warning today.  Rewire it to: "stock" → no IQ-
domain pre-pass; "captured" → enable the IQ-domain pre-pass with
the active profile.  No status-bar warning needed once the
implementation is real.

**Settings persistence:**

Same QSettings keys we already have for the captured profile
selection (`nr/use_captured_profile`, `nr/active_profile_name`).
No schema bump.

#### Forward-compatibility with TX (v0.2) and PureSignal (v0.3)

Operator asked at the start of Phase 2 (2026-05-10) whether the
IQ-domain pre-pass would interfere with TX or PureSignal work
landing in v0.2 / v0.3.  Recorded answer so future sessions don't
re-derive it.

**The pre-pass is wired exclusively into ``Radio._do_demod_wdsp``,
which only ever sees DDC0 (RX1's receive IQ stream).**  TX, PS
feedback, and self-monitoring are independent code paths that
share none of the new code:

**CORRECTED Round 1 2026-05-11 (CR-1 + L-9):** the table below
contains the previous draft's reasoning, corrected for the
DDC routing facts established by Round 1 Agent A.  The earlier
"DDC0 keeps running RX1 normally during PS+TX" statement was
**wrong** — Thetis source proves DDC0 is at TX freq via cntrl1=4
mux during HL2 MOX+PS.  This corrects the table and adds the
bypass requirement.

| Concern | Status (corrected Round 1) |
|---|---|
| §14.6 affects PureSignal calibration math? | **Yes, indirectly — IQ pre-pass MUST be bypassed during MOX+PS state.**  PS feedback on HL2 lives in DDC0+DDC1 with cntrl1=4 routing the PA coupler to DDC0 (NOT DDC2/DDC3 as the earlier draft claimed — see CR-1).  If captured-profile pre-pass is still running on DDC0 during MOX+PS, it applies an RX-band noise mask to PA-feedback IQ samples — pure garbage going into calcc.  The pre-pass must be **disabled** by the `(mox, ps_armed)` state hook (same hook driving the panadapter source switch per v0.1 plan §9.5). |
| §14.6 affects TX modulation chain? | **No** — TX is mic → WDSP TXA cffi → baseband I/Q → EP2 framing → HL2 PA, totally independent of any RX path. |
| §14.6 affects RX1 self-monitoring during TX? | **N/A — there is no RX1 self-monitoring during HL2 MOX+PS.**  DDC0 is at TX freq, not at RX1's tuned freq.  Operator's RX1 band content is not being received in this state.  Self-monitor visualization comes from the §9.5 source-switch matrix (TX baseband → panadapter during MOX-no-PS; PA-feedback → panadapter during MOX+PS), NOT from a continuing DDC0 RX-band feed. |
| §14.6 affects duplex / ``puresignal_run`` flags? | **No** — C4 bit 2 (duplex) and frame 11/16 C2 bit 6 (``puresignal_run``) are protocol-layer concerns in ``stream.py``; §14.6 doesn't touch the protocol layer at all. |
| §14.6 affects RX2 (v0.1)? | **Yes, per-channel duplication + per-channel bypass.**  RX2 gets its OWN ``CapturedProfileIQ`` instance for its band's noise spectrum.  On HL2 MOX+PS, DDC1 is at TX freq (sync-paired to DDC0) — RX2's pre-pass MUST be bypassed in this state since DDC1 isn't carrying RX2-band content either.  On ANAN 5-DDC MOX+PS, DDC2 stays on RX2 freq — RX2's pre-pass keeps running.  State-product-dependent per family. |
| Pre-pass behavior on PTT release? | **Reset spectral statistics.**  When PS+TX ends and DDC1 returns to RX2's true freq, the pre-pass on RX2 has been bypassed for the MOX duration — its STFT overlap-buffer is stale.  Reset on resume.  Operator MAY see a brief (~200 ms) noise-floor adjustment as the new live samples populate the rolling buffer.  Surface as a faint "RX2 resuming" badge if operator-visible. |

**On full duplex during PS:** operator was correct that PS needs
the duplex bit set + ``puresignal_run`` flags + nddc=4 — already
documented in §3.2 and §3.7.  None of that protocol surface is
touched by §14.6.  **Important correction:** during HL2 PS+TX,
the gateware re-routes DDC0/DDC1 to TX freq via cntrl1=4 (see
§3.8 corrected entry).  Captured-profile pre-pass must therefore
bypass on MOX+PS edges.  DDC2/DDC3 are gateware-disabled on HL2
PS+TX — those slots are zeros, not feedback samples (the earlier
draft's claim that "DDC2/DDC3 feedback enters ``_do_demod_wdsp``"
was wrong — that path doesn't exist on HL2).

**Bonus side-property:** when v0.1 RX2 lands, each RX channel can
have its own captured profile (operator listening to 40m on RX1
and 20m on RX2 might want band-specific QTH noise subtraction
on each).  The per-WDSP-channel pre-pass model from Phase 4
naturally supports this — just instantiate a second
``CapturedProfileIQ`` for RX2's IQ stream.

#### Companion investigations (parked alongside §14.6)

These came up while operator was researching the captured-noise
feature.  Cross-linked here so they don't get rediscovered.

**A. Thetis `zetaHat.bin` is identical to WDSP's C-baked default
(verified 2026-05-09).**

The Gemini-style summary the operator was reading suggested
Thetis ships a "trained" gain table file derived from "72 hours
of band noise" — implying a meaningful difference vs the WDSP
default.  Bit-exact diff on Thetis 2.10.3.13:

* `zetaHat.bin` (43,240 bytes) at
  `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/lib/Thetis-resources/zetaHat.bin`
* `CzetaHat[]` baked into WDSP source at
  `D:/sdrprojects/OpenHPSDR-Thetis-2.10.3.13/Project Files/Source/wdsp/zetaHat.c`

All 3,600 doubles match to 1e-12 (worst real-cell delta = 0.0).
All 3,600 zetaValid integers match exactly.

What the file actually is: a 60×60 lookup table of MMSE-LSA gain
values indexed by (γ, ξ) — the a-posteriori / a-priori SNR pair.
NOT a noise spectrum.  NOT QTH-specific.  Generic algorithm
tuning.  When WDSP's `readZetaHat()` (in `wdsp/emnr.c:207`) can't
find a `zetaHat.bin` in CWD, it falls back to the C-baked array;
when it finds one, it loads from the file.  Either way, ham
operators downstream get the same data because Thetis ships the
same data both ways.

**Implication:** there's no shippable variant of `zetaHat.bin` to
toggle between in stock WDSP.  Modes 3 / 4 in our NR-mode picker
already use this gain table via `gain_method=2` / `gain_method=3`.
Don't waste cycles building a "use Thetis trained table" toggle —
nothing would change.

(The file COULD be regenerated offline by replicating NR0V's
training pipeline, but that's a research project, not a feature.)

**B. Line-synchronous blanking (LSB) — KA7OEI-style.**

Operator-attached doc 2026-05-09 covered software LSB: PLL-locked
time-domain blanker that targets mains-synchronous impulsive noise
(SCR dimmers, switching supplies) at 100/120 Hz.  Linrad's the
canonical reference implementation.

Status: parked, NOT a separate feature.

Reasons:
1. Targets a noise type (mains-locked impulses) that the operator
   doesn't currently report as a top issue.  N8SDR's worst case
   is the nearby AM broadcaster's 5th harmonic on 7.250 MHz — an
   RF interferer, not mains-locked, which LSB does nothing for.
2. WDSP NB at "Heavy" handles impulsive noise reasonably for
   typical operator situations.  No tester has yet reported "WDSP
   NB doesn't kill my dimmer buzz."
3. The IQ-domain captured-profile rebuild (§14.6) is a strict
   superset: capture + replay any periodic spectral pattern,
   mains-locked or otherwise.  An LSB-style PLL variant could
   layer on top of §14.6 ("sync profile to mains") if a real
   need surfaces, but standalone is duplicative.

**If a tester reports unmissable mains-locked impulses that WDSP
NB Heavy + IQ-domain captured-profile both fail to suppress**, then
revisit.  Implementation outline at that point:

* PLL-track the dominant 100/120 Hz pulse train in pre-WDSP IQ
* Compute predicted next-pulse timestamp at sample-clock resolution
* Time-domain gate that zeroes ~50-100 µs around each predicted
  pulse
* Avoids the "static profile vs dynamic AGC" mismatch that killed
  cyclostationary spectral subtraction (§9.5) — different
  domain, different failure modes
* CPU: cheap (a few hundred µs per second of audio)
* UX: "Off / Light / Heavy" picker on DSP+Audio, similar to NB

**C. Modify-WDSP-C-source path for "captured noise as LMS
reference" — REJECTED (per the 2026-05-09 Gemini-doc analysis).**

The Gemini summary the operator forwarded suggested editing
`Thetis/DSP.cs`, `Thetis/WDSP.cs`, AND the WDSP C source itself
to add a "noise-only reference buffer" input to the LMS adaptive
filter.  This path is explicitly out of scope for Lyra:

* Maintaining a Lyra-flavored WDSP fork = ongoing burden every
  time NR0V ships a new WDSP version
* Loses the bundle-the-stock-DLL property of v0.0.9.6 (which we
  picked specifically to avoid compile-chain complexity in
  installs)
* §14.6 IQ-domain pre-WDSP approach achieves the same end-result
  without forking: tap IQ before `_wdsp_rx.process(iq)`, apply
  spectral subtraction in IQ-magnitude domain using the captured
  profile, hand cleaned IQ to WDSP — WDSP sees nothing different
  about its input.  Same NR effect, zero WDSP-source touches.

The Gemini doc is well-written but its recommended path is the
expensive one for our architecture.  Do not pursue.

### 14.7 NR-mode UX overhaul (2026-05-07 evening — IN OPERATOR TESTING)

**Background:** operator-driven UX redesign after extensive A/B
testing showed the legacy NR1/NR2 backend dropdown + dual strength
sliders was confusing in WDSP mode (sliders mostly inert; backend
NR1/NR2 sounded similar even though we set different gain methods).

**New model — Thetis-inspired but Lyra-tuned:**

* **NR enable button** → master on/off (existing button repurposed)
* **NR slider** → 4-position MODE selector (1..4) — replaces the
  legacy "strength" semantics on the same slider widget
* **AEPF checkbox** → anti-musical-noise post-filter (new control)
* **NR2 aggression slider** → HIDDEN entirely in WDSP UI (still
  constructed for legacy code paths)

**Mode mapping** (see `Radio._NR_MODE_TO_GAIN_METHOD`):

| Mode | gain_method (WDSP) | Character |
|---|---|---|
| 1 | 0 (Wiener + SPP) | Smooth, mid-aggressive |
| 2 | 1 (Wiener simple) | Edgier, more raw subtraction |
| 3 | 2 (MMSE-LSA) | WDSP default, smoothest **(default)** |
| 4 | 3 (Trained adaptive) | Most aggressive |

**Files touched:**

* `lyra/radio.py` — `set_nr_mode`, `set_aepf_enabled`,
  `_push_wdsp_nr_state` rewrite, `autoload_nr_mode_settings`,
  signals `nr_mode_changed` + `aepf_enabled_changed`.
* `lyra/dsp/wdsp_engine.py` — already had EMNR/ANR knob methods
  from earlier Option B work this afternoon.
* `lyra/dsp/wdsp_native.py` — already had cffi bindings.
* `lyra/ui/panels.py` — repurposed `nr1_strength_slider` (range
  changed from 0..100 to 1..4, label "Mode:" instead of "NR
  strength:"), added `aepf_checkbox`, hid `nr2_agg_slider`
  layout-wise, slot handlers `_on_nr_mode_slider`,
  `_on_aepf_checkbox`, `_on_nr_mode_signal`,
  `_on_aepf_enabled_signal`.
* `lyra/ui/app.py` — new `autoload_nr_mode_settings` call at
  startup.

**QSettings migration:**

* `nr/profile = nr2` → `noise/nr_mode = 1`
* `nr/profile = nr1` (or anything else) → `noise/nr_mode = 3`
* AEPF defaults ON (`noise/aepf_enabled = True`)
* Old keys preserved for legacy mode

**NPE dropdown — DONE 2026-05-07 evening.**  Initial design proposed
"per-mode npe_method differentiation" (each Mode 1-4 fixed to one
NPE method) but operator pushed for the better answer: surface NPE
as an OPERATOR-TUNABLE control on the DSP+Audio panel.  Now
operator picks Mode + AEPF + NPE independently → Lyra exposes more
WDSP knobs for direct on-air tuning than Thetis / SparkSDR /
PowerSDR (all hide NPE).  Real differentiator.  Operator-confirmed
audible difference between OSMS and MCRA.

**Future polish ideas (still on the table):**

1. **Settings → DSP → NR Advanced panel** — expose `ae_zeta_thresh`,
   `ae_psi`, additional fine-tuning knobs.  Thetis hides these in
   registry; Lyra could expose them in advanced settings.
   v0.0.9.6.x or v0.1 polish.

2. **Mode names instead of numbers** — "Smooth/Raw/Default/
   Aggressive" labels in the UI.  Or numbers + character hint in
   tooltip (currently does this).

**Operator-confirmed status as of 2026-05-07 late-evening:**

* New UX wired + tested + working on real signals
* AEPF checkbox = clear audible difference (operator: "no wonder
  it's hidden and on")
* NPE dropdown = clear audible difference between OSMS and MCRA
* Modes 1-2 sound similar (both Wiener variants); Mode 3 = MMSE-LSA
  smoothest; Mode 4 = "FM-like for SSB" (aggressive trained
  adaptive — useful but distinctive)
* LMS slider works (controls ANR step size mu logarithmically)
* APF works (CW-only, mode-gated)
* Captured-profile both capture AND apply paths work in WDSP
  mode (v0.0.9.9 §14.6 Phase 4 — IQ-domain rebuild landed)

### 14.8 All-mode squelch — WDSP SSQL native (2026-05-07 night)

**TL;DR:** the SQ button in WDSP mode now drives WDSP's native
SSQL ("Single-mode Squelch Level") for SSB/CW/DIG, plus the
existing FM-SQ and AM-SQ modules for those modes.  This is the
WDSP-port-not-Thetis-copy principle in §13.3 applied to squelch:
WDSP ships SSQL; Lyra calls into it via cffi.  Other WDSP
consumers happen to use the same module the same way — they're
sibling consumers of WDSP, not Lyra's reference.  No Python-side
audio-domain gate — multiple attempts at one all failed because
WDSP's AGC compresses voice/noise dynamic range to ~1.5-2×
post-AGC, blinding any audio-RMS gate.

**The journey** (preserved here so future sessions don't repeat
it):

| Attempt | Approach | Failure mode |
|---|---|---|
| 1 | Hand-rolled dBFS RMS gate, slider→absolute threshold | Pre-vs-post-volume position couldn't be calibrated |
| 2 | Move pre-volume + widen dBFS map to -75..-25 | Loose at top — gate stayed open on noise floor |
| 3 | Delegate to legacy `AllModeSquelch` (auto-tracked floor + ratio) | Erratic on real signals; floor seeding broke when SQ enabled mid-signal |
| 4 | Tighten K_OPEN constants for AGC-compressed audio | Closed gate mid-syllable on S9 signals at slider=0.7 |
| 5 | Smarter seed (1-sec min-window) + reverted track-up tau | Better but still hit-and-miss; root cause was AGC compression in audio domain |
| 6 | Spectrum-domain SNR gate (pre-AGC FFT signal vs noise floor) | Worked, but operator pointed out WDSP already ships SSQL for exactly this — call WDSP's instead of building parallel |
| 7 (final) | WDSP SSQL via cffi (`SetRXASSQLRun`/`Threshold`/`TauMute`/`TauUnMute`) | Operator-confirmed working |

**Final config** (in `lyra/radio.py`):

* `_SSQL_SCALE = 0.65` — slider 0..1 multiplied by 0.65 before
  passing to `SetRXASSQLThreshold`.  WDSP's WU2O-tested-good
  default is 0.16; with this scale, slider=0.20 → SSQL=0.13
  (just below WU2O default — comfortable), slider=0.30 → SSQL=0.20
  (slightly tight).  Direct 1:1 mapping put the operator's
  typical slider zone above WU2O default = perceived as tight.
* `_SSQL_TAU_MUTE = 0.7s` — vs WDSP `create_ssql` default 0.1s.
  WDSP's source comment notes "reasonable wide range is 0.1 to
  2.0".  WDSP's window detector (`wdaverage`) has a hardcoded
  0.5s adaptation tau; on quasi-stationary signals (continuous
  SSB conversation, digital modes) the average converges to the
  signal level within 1-2 sec → SSQL flags "no signal" → trigger
  voltage rises toward mute.  With the WDSP default
  tau_mute=0.1s, that false flag becomes a clamp in 134 ms.  At
  0.7s, trigger rise is ~940 ms — long enough that brief window-
  detector convergences don't clamp the gate while genuine end-
  of-transmission still mutes within ~1 sec of speech ending.
  Operator-tuned through 1.0s → 0.7s.
* `_SSQL_TAU_UNMUTE = 0.1s` — matches WDSP default.  Snappy
  speech-onset response.

**Routing** (`_push_wdsp_squelch_state` in radio.py):

* Mode FM → `SetRXAFMSQRun` (existing FM SQ)
* Mode AM/SAM/DSB → `SetRXAAMSQRun` + threshold (existing, dB-scaled)
* Mode SSB/CW/DIG/SPEC → `SetRXASSQLRun` + threshold (NEW)
* Disables the inactive modules to prevent crosstalk
* Called from `set_squelch_enabled` (operator toggle),
  `set_mode` (handoff between FM ↔ AM ↔ SSQL on mode change),
  and `_open_wdsp_rx` (initial state on stream start)

**Cffi bindings** (`lyra/dsp/wdsp_native.py`): `SetRXASSQLRun`,
`SetRXASSQLThreshold`, `SetRXASSQLTauMute`, `SetRXASSQLTauUnMute`.
**Engine wrappers** (`lyra/dsp/wdsp_engine.py`): `RxChannel.set_ssql_*`
methods on the `RxChannel` class.

**Files no longer in WDSP audio path** (legacy fallback —
**DEPRECATED**, see §14.9 below):

* `lyra/dsp/squelch.py` (`AllModeSquelch`) — only runs when
  `LYRA_USE_LEGACY_DSP=1`.  Constants reverted to original
  `K_OPEN_BASE=1.5 / K_OPEN_RANGE=6.0` / 150 ms seed.

**Hot points to remember if it comes back up:**

* Don't reach for a Python-side audio-domain gate.  The whole
  arc proved this can't work — AGC compresses signal/noise to
  the point that no audio-RMS threshold reliably distinguishes.
* If operator perception drifts again, the knobs are
  `_SSQL_SCALE` (overall slider feel), `_SSQL_TAU_MUTE` (clamp
  delay on convergence transients), `_SSQL_TAU_UNMUTE` (unmute
  responsiveness).  WDSP's `wdtau` (window-detector adaptation
  speed) is hardcoded inside the DLL at 0.5 sec — would need a
  WDSP rebuild to change.

### 14.9 Legacy pure-Python DSP path — DELETED (cleanup arc complete, 2026-05-08)

**Status: complete.**  v0.0.9.6 retired the pure-Python DSP path
in favor of WDSP cffi as the single audio engine.  Cleanup
landed across the `feature/v0.0.9.6-audio-foundation` branch in
~16 commits between 2026-05-07 night and 2026-05-08 evening.

#### What got deleted

| Module / file | Lines | Replaced by |
|---------------|-------|-------------|
| `lyra/dsp/leveler.py` (`AudioLeveler`) | 355 | feature retired (WDSP AGC handles dynamic range) |
| `lyra/dsp/agc_wdsp.py` (`WdspAgc`) | 746 | WDSP cffi `SetRXAAGCMode` directly |
| `lyra/dsp/apf.py` (`AudioPeakFilter`) | 251 | WDSP SPEAK biquad via `_push_wdsp_apf_state` |
| `lyra/dsp/demod.py` (5 demod classes + `NotchFilter`) | 528 | WDSP RXA chain (decim + demod + notches inside cffi engine) |
| `lyra/dsp/nb.py` (`ImpulseBlanker`) | 477 | `_NBState` dataclass + WDSP NOB |
| `lyra/dsp/lms.py` (`LineEnhancerLMS`) | 459 | `_LMSState` dataclass + WDSP ANR |
| `lyra/dsp/anf.py` (`AutoNotchFilter`) | 395 | `_ANFState` dataclass + WDSP ANF |
| `lyra/dsp/squelch.py` (`AllModeSquelch`) | 419 | `_SquelchState` dataclass + WDSP SSQL/FMSQ/AMSQ |
| `lyra/dsp/nr2.py` (`EphraimMalahNR`) | 1496 | `_NR2State` dataclass + WDSP EMNR |
| `lyra/dsp/channel.py::PythonRxChannel.process()` | ~600 | WDSP `RxChannel.process()` via `_do_demod_wdsp` |
| `Radio._apply_agc_and_volume` | ~168 | volume / mute applied in `_do_demod_wdsp` directly; AGC/AF Gain/APF live in WDSP |
| `LYRA_USE_LEGACY_DSP=1` env-var fallback dispatch | ~57 | gone — WDSP is the only path |
| 2 diag scripts (`diag_agc_wdsp_*.py`) | ~300 | obsolete (Python AGC port deleted) |
| Settings dialog: NR2 group + NR2 Gain Function picker + LMS-strength duplicate | ~290 | DSP+Audio panel (NR Mode 1-4 + AEPF + NPE + LMS strength) covers it |
| `panels.py` orphan NR2 strength slider + gain-method right-click menu | ~120 | same — Mode 1-4 picker is the live surface |
| `block_size` kwarg on `PythonRxChannel.__init__` | trivial | unused after `process()` deletion |

**Cumulative**: ~6,800 lines of legacy code removed.

#### Bugs fixed during cleanup (operator-reported, all in r3 baseline)

1. **AF Gain inert in live audio** — `_apply_agc_and_volume` had been the only consumer of `af_gain_linear` for live signal, and that method had been orphan since Phase 4.  Fixed in Phase 6.A1 by wiring `set_af_gain_db` to `_wdsp_rx.set_panel_gain(af_gain_linear)`.
2. **AGC Settings sliders didn't follow profile changes** — `set_agc_profile` updated the profile but never read the preset table to update advisory `_agc_release` / `_agc_hang_blocks`.  Fixed in Phase 6.A2 plus widening the Release slider range (was clamping Fast preset).
3. **AGC threshold push missing** — `_open_wdsp_rx` configured AGC mode but not threshold; engine ran with create-time max_gain default which prevented the gain meter from moving.  Fixed in Phase 6.A3 + fix-up by wiring `set_agc_slope(0)` + `set_agc_threshold(thresh_db, 4096, in_rate)` at init.
4. **FM SQ slider had no effect** — `_push_wdsp_squelch_state` called `SetRXAFMSQRun` but never `SetRXAFMSQThreshold`; FM mode ran at engine create-time threshold (0.750) regardless of slider.  Fixed in Phase 6.A4 with logarithmic mapping `10^(-2·v)`.
5. **ANF μ slider was advisory-only** — operator's μ value was persisted on the dataclass but never reached WDSP.  Fixed in Phase 6.A4 by adding `SetRXAANFVals` binding + wrapper + push from `set_anf_mu` and `_open_wdsp_rx` init.
6. **AM SQ tail too long** — engine default 1.5 s felt unnaturally long.  Fixed in Phase 6.A4 by pushing 0.5 s at `_open_wdsp_rx` init via new `SetRXAAMSQMaxTail` binding.
7. **AM/DSB squelch stuck on master-off** — `_push_wdsp_squelch_state` mode-routing logic skipped disabling the active-mode SQ module when SQ went off (only handled mode-mismatch disables + SSQL).  Fixed in Phase 6.A4 fix-up by pulling the master-off check above the mode-targeted disables.

#### Architecture state now

```
HL2 (HPSDR P1) → UDP IQ → HL2Stream → DspWorker.process_block
    → Radio._do_demod_wdsp (one method, ~120 lines)
        → _wdsp_rx.process(iq)              # decim + notches + demod + NR + ANF + AGC + APF inside cffi
        → volume / mute / capture-feed     # Python-side post-processing
        → BinauralFilter (BIN, optional)
        → audio_sink.write
```

Lyra retains operator-state mirrors on `PythonRxChannel`:
* `_nr` — real `SpectralSubtractionNR` instance (NR1 capture machinery — only nr.py interface still alive; powers the 📷 Cap button).
* `_apf`, `_nb`, `_lms`, `_anf`, `_squelch`, `_nr2` — `_*State` dataclasses (operator-tunable knobs persisted across sessions; pushed to WDSP via `_push_wdsp_*_state` helpers).

The `DspChannel` ABC is kept for forward compatibility (a future DSP backend could subclass it), but its `process()` abstractmethod is gone — channels are state containers now, not DSP drivers.

#### Tags + bundles for archaeology

| Tag | What it covers |
|-----|---------------|
| `v0.0.9.6-rx1-working-r3` | Pre-cleanup baseline (operator-verified WDSP working) |
| `v0.0.9.6-rx1-working-r4` | + AM right-channel-silent fix (§14.10) |
| `v0.0.9.6-rx1-working-r5` | + Phase 4 (Audio Leveler delete) |
| `v0.0.9.6-rx1-working-r6` | + Phase 5 (channel.py slim to state container) |
| `v0.0.9.6-rx1-working-r7` | + Phase 6.A + 6.A1 (orphan delete + AF Gain fix) |
| `v0.0.9.6-rx1-working-r8` | + Phase 6.B/C + Sweep 1 + Phase 7 + AGC plumbing |
| `v0.0.9.6-rx1-working-r9` | Cleanup arc COMPLETE (Phase 8 + Phase 9 polish + 7 operator-reported bug fixes) |

Each tag has a matching portable bundle in `_backups/lyra-2026-05-08-rx1-working-rN.bundle`.  Restore via `git clone _backups/<bundle> restored-lyra`.

If anyone needs to recover a deleted file by name (e.g. the spectral-subtraction port for a future captured-profile IQ-domain rebuild), `git show <tag>:lyra/dsp/<file>.py` walks the tree at any tag's snapshot.

#### Follow-ups still open (NOT part of cleanup arc)

* ~~**§14.6 Captured-profile IQ-domain rebuild**~~ **CLOSED 2026-05-10 (v0.0.9.9):** IQ-domain rebuild landed across Phases 1-4.  Schema v2 (rate-specific full complex-FFT magnitudes), `CapturedProfileIQ` STFT engine in `lyra/dsp/captured_profile_iq.py`, capture + apply both wired in `_do_demod_wdsp` pre-WDSP.  v1 audio-domain profiles refused on load with recapture hint.  Still pending: Phase 5 (Settings FFT-size dropdown + DSP panel badge polish) and Phase 6 (operator A/B test matrix).
* **§14.10 _open_wdsp_rx audit (partially closed)** — Phase 6.A3 + 6.A4 wired the AGC + FM SQ + ANF + AM SQ gaps the audit found.  Lower-priority gaps (FM Deviation, FM Limiter, FM AF Filter, CTCSS, AM DSBMode, AM Fade, NR3-RNNoise, NR4-SpectralBleach, EMNR Position, ANR Position, Pan, etc.) deferred until operator surfaces specific need.
* ~~**HL2 audio smoothing regression check** — Phase 9.5 Item 2.  A "less harsh" smoothing change landed during the v0.0.9.6 audio rebuild on 2026-05-07 may have been dropped during a subsequent revert chain.  Worth a `git log -p lyra/dsp/audio_sink.py` review.~~  **CLOSED 2026-05-09: NO regression.**  The smoothing change in question was Option Z (commit `022d1fd`, half-cosine slewed-silence-fill on EP2 underrun, 2026-05-06 12:47).  It was deliberately reverted (`f29f53d`, 12:56) when the real root cause was found 19 minutes later: HL2 command 0x17 (`config_txbuffer`) was never being sent, so the FPGA's TX-side audio buffer ran at the gateware default 10 ms and underran with Python-side jitter.  The actual fix landed in `c7916bc` (13:15) and lives at `lyra/protocol/stream.py:356` as the `0x2e` register entry (`(0, 0, 12 & 0x1F, 40 & 0x7F)` = 12 ms PTT hang, **40 ms TX latency**), pushed at startup via the standard C&C cycle.  Plus TPDF dither (stream.py:207-260) and S-meter peak-hold smoothing (radio.py:1358 `_SMETER_PEAK_DECAY = 0.85`) are also still in place.  The revert was correct — Option Z would have masked symptoms while c7916bc fixes the cause.  No code action; CLAUDE.md note kept here as the audit trail in case anyone re-reads §14.9 and wonders why the strikethrough.
* **AGC profile A/B at the operator level** — meter movement is verified (Phase 6.A3 fix), but per-time-constant audible differences (Fast vs Slow vs Long on real speech / CW) need operator confirmation when band conditions improve.

### 14.10 AM/FM/DSB right-channel-silent bug — FIXED (2026-05-07 night)

**Operator-reported symptom:** in AM, DSB, and FM modes, only the
LEFT audio channel produced sound; the BAL slider had no effect on
the right (full-right = silence).  SSB (USB/LSB/CWU/CWL) worked
normally.  Affected both HL2 audio jack and PC Soundcard paths.
Bug was present in `ce70e97` ("RX1 audio foundation milestone")
but had escaped operator verification because the prior test pass
focused on SSB modes.

**Root cause:** WDSP's EMNR (`emnr.c:1247-1248`) explicitly zeroes
the Q channel on output:
```c
a->out[2 * i + 0] = a->outaccum[a->oaoutidx];   // I = noise-reduced audio
a->out[2 * i + 1] = 0.0;                         // Q forced to zero
```

For SSB modes, the post-EMNR `xbandpass(bp1)` stage has an
**asymmetric** passband (USB = positive freq only, LSB = negative
freq only).  A complex bandpass with one-sided passband acts as a
Hilbert restorer — the output Q is reconstructed analytically from
the real input I, and stereo content survives.

For AM/FM/DSB, the post-EMNR BP1 has a **symmetric** passband
(`-W..+W` around DC).  Real input through symmetric complex
bandpass → real output (output Q stays zero).  Q remains zero
through the patch panel and all the way out the audio sink.

The patch panel's behaviour is determined by its `copy` field:
* `copy=0` (default from `create_panel`): no copy.  L = gain1 * I,
  R = gain2Q * Q.  Q=0 → R=silence.
* `copy=1`: copy I to Q at panel output.  L = gain1 * I,
  R = gain2Q * I.  Mono on both channels regardless of upstream Q.

WDSP's `create_panel` defaults to `copy=0`.  Thetis explicitly
calls `SetRXAPanelBinaural(0)` at channel init, which sets
`panel.copy = 1 - 0 = 1` — overriding the create-time default.
Lyra never made that call, so we inherited `copy=0` and AM/FM/DSB
silenced the right channel whenever EMNR was active (which is
"basically always" since NR Mode 1-4 are EMNR variants).

**The fix:**

* `lyra/dsp/wdsp_native.py`: cdef `SetRXAPanelBinaural`
* `lyra/dsp/wdsp_engine.py`: `RxChannel.set_panel_binaural(bool)`
  wrapper.  `False` = mono on both channels (= panel.copy=1,
  matches Thetis's default listening setup).  `True` = no copy
  (= panel.copy=0, raw I/Q routed to L/R, available as an escape
  hatch for raw-IQ binaural listening if anyone ever asks).
* `lyra/radio.py` `_open_wdsp_rx`: call `set_panel_binaural(False)`
  right after `set_panel_gain(1.0)`.  Persists for the life of the
  WDSP channel; mode changes don't disturb it.

**Verified across all modes** with EMNR enabled:

| Mode | L_rms | R_rms | Status |
|---|---|---|---|
| LSB | 0.5325 | 0.5325 | ✓ |
| USB | 0.5507 | 0.5507 | ✓ |
| AM | 0.7701 | 0.7701 | ✓ |
| FM | 0.5454 | 0.5454 | ✓ |
| DSB | 0.7071 | 0.7071 | ✓ (operator confirmed BAL pans cleanly) |
| CWU | 0.5636 | 0.5636 | ✓ |

**Compatibility note for v0.1 RX2 stereo split** (per operator
question 2026-05-07 night): this fix is the *correct* foundation
for split-mode stereo, not a problem for it.  WDSP's per-channel
`SetRXAPanelBinaural` controls intra-RX I/Q-to-L/R routing
(unrelated to multi-RX stereo).  Lyra's RX2 stereo split lives
in `AudioMixer` per §6.1: each RX produces mono-on-stereo, then
the mixer pan-curves RX1 hard-left and RX2 hard-right.  With our
fix, each individual RX channel reliably produces mono output
that the mixer can spatially pan; without our fix, panning RX1
hard-left would lose audio (only the I component would survive,
and EMNR could zero it on the way through).

**Audit reminder:** this bug surfaced because Lyra's
`_open_wdsp_rx` skipped a setter Thetis calls.  There are likely
more.  Future audit: diff the `SetRXA*` calls in our
`_open_wdsp_rx` against Thetis's channel-init sequence in
`Console/radio.cs`, looking for siblings like:

* `SetRXAPanelGain1`/`Gain2` defaults — we set Gain1=1.0, leave
  Gain2I/Gain2Q at create_panel defaults of 1.0 each.  Probably OK.
* FM-deemphasis settings — Thetis sets these per-mode.
* SBNR / RNNR (NR3 / NR4 in Thetis) — we don't bind them at all.
* Notch DB filter coefficients — currently push freqs only;
  Thetis pushes BW + run + tune freq.
* AGC fixed-gain / hang threshold per AGC mode.
* CESSB / CFC TX-side equivalents (when v0.2 TX work begins).

Track in `docs/architecture/measurements_and_cleanup.md` as a
phase before TX work starts.

---

## 15. Documentation backlog from v0.0.9.6.1 audit (2026-05-09)

Two-agent audit during the v0.0.9.6.1 release prep flagged these
items.  High-priority operator-facing fixes landed in the patch
itself (NR right-click menu name fix, AGC profile cleanup of stale
"Long" entry, ANF profile name correction in troubleshooting.md,
captured-profile WDSP-mode INERT caveat, AGC Auto profile docs
correction).  The items below are non-blocking and parked for a
future session.

### 15.1 — Internal architecture doc cleanup (CLOSED 2026-05-10)

All three items closed in the v0.0.9.8.x doc cleanup pass:

* **`CLAUDE.md` "Current version" line** — replaced with a
  pointer to ``lyra/__init__.py`` so the line doesn't go stale
  again.
* **§14.2 "Wired" / "Inert" lists** — rewritten.  Wired list
  reflects the v0.0.9.6 NR-mode UX overhaul (Mode 1-4 + AEPF +
  NPE), v0.0.9.6 manual-notches / NB UI / BIN / APF wiring, and
  v0.0.9.8's central DDS offset for the carrier-freq VFO
  convention.  Inert list pruned to just the genuinely-deferred
  items: captured-profile apply (IQ-domain rebuild per §14.6),
  NR3/NR4 (DLLs bundled but no UI), TX/PS chains (Phase v0.2/v0.3).
  Audio Leveler removed entirely (deleted, not parked).
* **"Last updated" trailer** — refreshed to 2026-05-10 with the
  v0.0.9.7 → v0.0.9.7.1 → v0.0.9.7.2 → v0.0.9.8 sprint summary
  + §15 backlog pointers.

### 15.2 — RX2 plan leveler references (`docs/architecture/v0.1_rx2_consensus_plan.md`)

Multiple lines (422, 426, 753-757, 789, 804, 1091) still reference
`leveler` as part of the RX/TX audio chain or as a tap point for
the Lit-Arc `MODE_COMP` indicator.  Audio Leveler was DELETED in
the v0.0.9.6 cleanup arc (`lyra/dsp/leveler.py`, 355 lines, see
§14.9).  Action when v0.1 work begins:

1. Update RX/TX chain diagrams to drop the `→ leveler` step (or
   replace with explicit `Vol → APF → sink` to match current
   reality).
2. Re-think `MODE_COMP` signal source — `radio._leveler._env_db`
   no longer exists.  Options: (a) read AGC gain magnitude from
   `radio.agc_action_db` as a proxy for compression; (b) tap APF
   peak gain when active; (c) port WDSP `compress.c` for v0.2 TX
   first then re-use for RX MODE_COMP.
3. TX chain table row (line 1091) `| leveler | lyra/dsp/leveler.py
   (existing RX leveler reused) | ...` — needs either re-port
   from WDSP `compress.c` or alternate strategy.

### 15.3 — Settings dialog connection-tracking refactor closure

`v0.1_rx2_consensus_plan.md` §7.x parks the dead-widget refactor.
The v0.0.9.6.1 sweep landed the partial fix (`_safe_mirror`,
`_swallow_dead_widget`, three-paragraph intro split).  Section
should note that the noise-suppression layer is in but the
DEEPER fix (actual disconnect-on-close) is still parked, with a
pointer to the present helpers as the "current state of the
art."

### 15.4 — Help-doc minor polish (CLOSED in v0.0.9.6.1)

All three items closed during the v0.0.9.6.1 doc audit:

* **Live-preview during zoom slider drag** — added a paragraph to
  `docs/help/spectrum.md` "Update rates and zoom" section noting
  that Spec / WF sliders commit ~10 times per second while held,
  not just on release.
* **`docs/help/bin.md` audio-chain diagram** — redrawn to show the
  WDSP-mode reality (engine handles decim → notches → NR → ANF →
  AGC → APF → bandpass → demod internally; Python layer does
  mute → Volume → BIN → sink).  No more `tanh` stage (that was
  legacy pure-Python).
* **Author attribution** — reconciled to match `CONTRIBUTORS.md`
  authoritative list:
    * `introduction.md` — N8SDR is project lead and sole developer
      through v0.0.9.x; N9BC joined as co-contributor during
      v0.0.9.1 testing; **joint development begins at v0.1**.
    * `support.md` — "primarily built by N8SDR, with N9BC joining
      as co-contributor" (was "built by one person").
    * `license.md` — already had both names in copyright; left
      as-is.

### 15.5 — `_AGC_PROFILES` Long re-add (CLOSED 2026-05-10)

Done.  `panels.py:3835 _AGC_PROFILES` now includes `"long"`
between `"slow"` and `"auto"`; matching entries added to
`_AGC_PROFILE_LABELS`, `_AGC_PROFILE_COLORS`, and
`_AGC_PROFILE_TEXT` (label "Long", amber, text "LONG").  Long
mentions restored in `agc.md` (table row + label color note +
right-click menu list + AM-fade tip), `index.md` (Quick Start
+ Topic index), and `troubleshooting.md` (AGC pumping recipe).
The full radio-side wiring already existed (release time
0.040 s, hang_blocks 46, WDSP mode mapping `"long" → "LONG"`)
since the v0.0.9.6 cleanup arc — only the UI exposure was
missing.

### 15.6 — SPLIT operation UX design (proposed 2026-05-12, NOT yet built)

Operator design discussion 2026-05-12 (Rick): proposed
extending the current binary **SUB** button on the TUNING
panel into a **tri-state SUB / SPLIT / OFF** cycle button as
the v0.1 / v0.2 path for SPLIT TX operation, INSTEAD of (or
in addition to) the split-panadapter pane originally on the
v0.1 plan §7 (Phase 4).

**Proposed behavior:**

| State | RX behavior | TX behavior |
|---|---|---|
| **OFF** | Single RX on focused VFO (current SUB-off) | TX on focused VFO |
| **SUB** | Dual RX stereo split (current SUB-on) | TX on focused VFO |
| **SPLIT** | Single RX on VFO A (DX pile-up workflow) | TX on VFO B |

Plus: in SPLIT, a **TX marker + BW rectangle** drawn on the
existing single panadapter (distinct color from the RX
marker — proposed cyan or amber, NOT red since red is
reserved for TX-active per Phase 3.E
`FrequencyDisplay.set_tx_active`).

**Rationale for tri-state instead of separate SPLIT button:**
the three configurations (single RX, dual RX listening, SPLIT
pile-up) are the operationally common ones.  The fourth combo
(SUB + SPLIT) is rare and can be a right-click extension
later.  Three states cover 95% of operating reality.

**Rationale for "TX marker on existing panadapter" instead of
split-panadapter pane:**

Most operators in SPLIT operation watch the *RX-side* spectrum
(where they're listening for the DX station's response) while
the *TX-side* freq is just "where I'm calling — show me a
marker."  A single panadapter that follows the RX side, with
a separate-colored TX marker + BW box overlay, gives the
operator all the visual feedback they need without doubling
the spectrum widget complexity.

Split-panadapter pane is still possible later if testers ask
for it.  Operator decision 2026-05-12: parked pending Brent +
Timmy bench feedback on whether the tri-state + TX-marker
approach is sufficient.

**Caveats / edges discussed:**

* **Cross-band SPLIT** (e.g. RX1 on 40m, VFO B on 20m for
  cross-band repeater work) — the single panadapter can only
  show one band's spectrum.  Best default: panadapter follows
  the RX side (where operator's listening for the DX);
  off-screen TX marker is fine.
* **State model**: keep `rx2_enabled` + `split_on` as two
  separate dispatch axes on Radio.  The tri-state button is
  just a UX projection over those axes — internal model stays
  orthogonal.  Lets future workflows independently combine
  states without re-architecting.
* **Persistence**: `dispatch/split_on` joins the v0.1 Phase 4
  RX2 persistence keys.
* **TX itself is v0.2.**  The SPLIT button + TX marker can
  ship in v0.1 as **prep work** — button cycles, state
  persists, TX marker draws.  v0.2 TX hooks the actual
  transmit path to the SPLIT-on VFO B selection.  Zero UI
  changes needed when TX lands.

**Implementation scope when greenlit:**

1. `split_on` dispatch axis on Radio + signal + setter
2. Tri-state SUB/SPLIT/OFF button on TuningPanel (cycle on
   click, distinct visual state per mode)
3. Persistence for both axes (extends v0.1 Phase 4 work)
4. TX marker + BW rectangle on the spectrum widget
   (panadapter-source-aware like the existing markers; reads
   `radio.tx_freq_hz` = VFO B in SPLIT, VFO A otherwise)
5. Right-click on the tri-state button → tooltip / help
   dialog explaining the three states (UX clarity for new
   operators)

**Status: PARKED** pending Brent + Timmy bench feedback on the
v0.1.0-pre2 dual-RX UX.  If the focused-VFO single-panadapter
approach feels sufficient in practice, this design likely
ships in v0.2 as the SPLIT pre-work.  If the testers ask for
split-panadapter pane instead, this design gets rolled back
and Phase 4 split-pane comes back on.  See full discussion in
session transcript at
`C:\Users\N8SDR\.claude\projects\...` (session 2026-05-12).

### 15.7 — Sync investigation: waterfall / panadapter / audio delays

**Filed 2026-05-12 by operator (Rick).**  Possibly an
operator-perceptible time skew between the three live
RX-rendered surfaces:

1. **Audio output** — what the operator HEARS
2. **Panadapter spectrum** — what the operator SEES as the
   spectrum
3. **Waterfall** — what the operator SEES as the rolling
   history

Operator's working hypothesis (to confirm on next bench
session): one or more of these may be running with different
latency than the others, such that a "pop heard at moment T"
shows up as a spectrum blip at moment T+Δ₁ and a waterfall
streak at T+Δ₂ — feeling out of sync rather than coherent.

**Background context** (worth investigating from):

* **Audio path latency**: HL2 IQ → EP6 parser → DspWorker
  queue → WDSP cffi process → audio sink.  Audio sink itself
  may add buffering (HL2 audio jack has a hardware FIFO,
  PC Soundcard has WASAPI buffer).  Net ~50-200 ms depending
  on sink.
* **Spectrum path latency**: IQ → DspWorker FFT ring buffer
  → `_maybe_run_fft` cadence (every N IQ blocks based on
  `_fft_interval_ms`) → `_process_spec_db` → `spectrum_ready`
  signal → Qt main thread → spectrum widget repaint.  FFT
  cadence default ~30-60 fps; ring buffer adds 1-2 FFT
  windows of delay.
* **Waterfall path latency**: same FFT source as spectrum
  but emit cadence is divided by `_waterfall_divider`
  (default 2-3) and may also multi-emit per push for fast-
  scroll.  Should be the SAME spectrum frames just displayed
  differently — if there's skew between spectrum and
  waterfall, that's a real bug (not just latency).

**Diagnostic approach when picking this up:**

1. **First confirm the phenomenon.**  Generate a known
   impulse (tap the antenna line, click an SDR tone
   generator) and measure latency between hearing it,
   seeing the spectrum blip, and seeing the waterfall
   streak.  Phone camera at ~60 fps recording the screen +
   speaker audio is plenty.
2. **Compare to expected delays.**  If audio is leading
   spectrum by ~50 ms and waterfall by ~150 ms, that's
   probably just sink buffering + FFT cadence — operationally
   acceptable.  If they're WILDLY apart (>500 ms) something
   structural is wrong.
3. **Check spectrum vs waterfall coherence specifically** —
   they share the same FFT source, so they should be perfectly
   coherent or off by exactly the waterfall divider.  Any
   other skew = bug.
4. **Suspects** (rank when investigating):
   - Waterfall multi-emit interpolation drifting from real
     time (lyra/radio.py:10501+ `_waterfall_tick_counter`)
   - Spectrum widget repaint throttling vs Qt event loop
     pressure under high DSP load
   - HL2 audio jack EP2 buffer depth vs PC Soundcard rmatch
     latency difference

**Status: RESOLVED — 2026-05-13.**  Investigation completed
across two bench sessions.  Findings, methodology, and baked
defaults below.  Revert instructions at end.

#### Resolution summary

The operator-perceived delay between hearing/seeing was real
but **not** a coherence bug between spectrum and waterfall —
those proved coherent to within 0.1–0.6 ms (`wf_offset_ms`
instrumentation, see below).  The root cause was two
Lyra-specific **conservative pre-v0.0.9.6 latency margins**
that other HPSDR clients (Thetis, EESDR3) don't carry:

1. **rmatch ring target** in `lyra/dsp/audio_sink.py`,
   previously 400 ms.  This is the WDSP-style adaptive
   resampler ring depth used on the PC Soundcard path.
2. **HL2 TX-latency register** (gateware reg 0x17, exposed via
   C&C tuple `0x2e`) in `lyra/protocol/stream.py`, previously
   40 ms.  Affects HL2-side TX-buffer depth and indirectly
   the EP2 / C&C polling cadence Lyra has to keep up with.

Combined, these added ~275 ms of needless headroom on the RX
path vs other apps on the same hardware.

#### Baked production defaults (post-§15.7)

| Knob | Old default | **New default** | Savings |
|------|-------------|------------------|---------|
| `_ring_ms` (audio_sink.py) | 400 ms | **150 ms** | −250 ms |
| `self._tx_latency_ms` (stream.py) | 40 ms | **15 ms** | −25 ms |
| **Total RX-path margin removed** |  |  | **−275 ms** |

PC Soundcard ear-lag math: 150 ms ring + 22 ms WASAPI host
latency ≈ **172 ms** total (was ~434 ms).

#### Test matrix (operator bench, 2026-05-13)

All tests: LSB voice, NR Mode 4 + LMS + AGC Fast (heaviest
DSP load currently available), 1–3 minute runs, `[TIMING]`
instrumentation enabled.

| rmatch ring | TX-latency | Sink | Result |
|------|------|------|--------|
| 75 ms  | 40 ms | PC SC | **Below floor** — sustained 1–5 underruns/10s, audible pops on voice peaks under NR4 |
| 100 ms | 40 ms | PC SC | Borderline — clean on plain voice, occasional pop under NR4 |
| 125 ms | 40 ms | PC SC | Edge of floor — 1 slight pop in 2.5 min under NR4 |
| **150 ms** | 40 ms | PC SC | ✅ **Clean** — 1 underrun during PI loop init, zero after |
| 150 ms | 40 ms | HL2 jack | ✅ Clean on RX (ring bypassed; AK4951 path validated) |
| 150 ms | **25 ms** | HL2 jack | ✅ Clean, 1–2 brief startup hiccups (gateware settling) |
| 150 ms | **15 ms** | HL2 jack | ✅ Clean, same brief startup hiccups as 25 ms |

Pushing TX-latency below 15 ms (10, 12) was considered but
deferred — diminishing returns on RX-only validation, and the
register's true floor is governed by TX-side buffer behavior
which can't be validated until TX bring-up.

#### Methodology — env-var override pattern

Both knobs are operator-tunable at runtime via environment
variables, **kept in place after §15.7 resolution** for
future tester diagnostics and easy reproduction of this
investigation:

```cmd
set LYRA_RMATCH_RING_MS=400         :: revert to pre-§15.7 ring
set LYRA_HL2_TXLATENCY_MS=40        :: revert to pre-§15.7 TX-latency
set LYRA_TIMING_DEBUG=1             :: enable [TIMING] instrumentation
python -m lyra.ui.app
```

`LYRA_RMATCH_RING_MS` is clamped to 30..1000 ms.
`LYRA_HL2_TXLATENCY_MS` is clamped to 5..127 ms.

The `[TIMING]` instrumentation lives in `_TimingStats`
(module-level in `lyra/radio.py`) and emits one summary line
per second when `LYRA_TIMING_DEBUG=1`.  Tracks:
- `audio_worker_ms` — DspWorker process_block dispatch time
- `fft_worker_ms` — DspWorker FFT emit time
- `spec_main_ms` — Qt main-thread `_process_spec_db` time
- `wf_offset_ms` — gap between spectrum emit and waterfall
  emit (proves coherence — was always 0.1–0.6 ms across all
  runs, confirming spectrum/waterfall are *not* skewed)
- `q_rx1`, `q_rx2` — DspWorker queue depths
- Context: `hl2_txlat_ms`, `rmatch_ring_ms`, `sink` name

#### How to revert if a tester regression is reported

If a tester reports new audio dropouts post-v0.1 release that
correlate with this change:

1. **First try the env-var override** (fastest, no code
   change):
   ```cmd
   set LYRA_RMATCH_RING_MS=400
   set LYRA_HL2_TXLATENCY_MS=40
   ```
   If that fixes it, the tester's hardware needs more
   headroom than our bench environment showed.  Don't revert
   globally — add an entry to the User Guide pointing at the
   env vars instead.

2. **If full revert is needed**, change two lines each in
   two files (defaults only; keep env-var infrastructure):

   `lyra/dsp/audio_sink.py` (~lines 666, 668):
   ```python
   _ring_ms = 150  →  _ring_ms = 400
   ```

   `lyra/protocol/stream.py` (~lines 626, 628):
   ```python
   self._tx_latency_ms = 15  →  self._tx_latency_ms = 40
   ```

   Also update the banner default labels and comment headers
   (`"default 150"` → `"default 400"`, etc.).

#### Session reference

Full bench session transcript with all timing data captured
in operator session of 2026-05-13 (continued from compaction
of 2026-05-12 session that filed this §15.7 originally).
Latency instrumentation code (`_TimingStats` class, hook
points in `_on_worker_spectrum_raw` and `_process_spec_db`)
remains in `lyra/radio.py` for future use — gated on
`LYRA_TIMING_DEBUG` so zero cost when disabled.

**Not investigated in this session, deferred:**
- HL2 TX-latency floor below 15 ms (needs TX bring-up to
  validate; 10–12 ms might be reachable then)
- Per-band-per-RX ring tuning (single global default is
  fine for v0.1 RX2)
- Linux/macOS rmatch behavior (Windows WASAPI only tested)

### 15.8 — v0.2-era architecture conversation (PARKED 2026-05-13)

Strategic items the operator surfaced during the §15.7 latency
work that are **not latency fixes** but are worth deliberate
v0.2-era architecture decisions.  Recorded here so they don't
get lost between sessions and so the design conversation
happens before TX bring-up shapes the code base around
assumptions that would conflict.

**Why they were deferred during §15.7:**  the rmatch ring
(400 ms pre-§15.7) was 400× the entire CPU-side DSP cost
(~1-2 ms typical).  Moving DSP to GPU would have saved
microseconds while the ring was eating 400 ms.  Bench-tuning
the buffer was the only thing that could move the latency
needle.  Now that latency is bench-validated and shipped in
v0.1.0-pre3 (−275 ms total), these other axes become
legitimate next-conversation items.

#### 1. Vulkan compute path for DSP (FFT / windowing)

* **Motivation:** vendor-neutral GPU compute.  Thetis is
  NVIDIA-only via CUDA; AMD users either use Thetis on CPU
  or don't use Thetis.  Lyra targeting Vulkan (cross-vendor)
  is a genuine differentiator.
* **What it buys:** CPU headroom on weak machines, not
  latency.  Win = Lyra feels snappier under contest load
  (EiBi overlay + captured-profile NR + everything-on)
  without GPU-vendor lock-in.
* **Scope:** clean Vulkan FFT compute shader path is roughly
  2 weeks of focused work.  Don't tackle in v0.1; right
  window is v0.2 alongside TX bring-up.
* **Operator hardware note:** AMD has genuinely caught up
  for compute; pricing/availability often beats NVIDIA in
  mid-range.  Vulkan-friendly path makes Lyra installable
  on AMD-equipped operator shacks without compromise.

#### 2. Dedicated calc thread for PureSignal (v0.3)

* **Motivation:** when PureSignal lands, its IMD-prediction
  calc loop is real compute (Thetis bench shows ~20-40 ms
  per envelope evaluation depending on tap count).  Running
  it on the DSP worker thread would steal cycles from
  realtime RX/TX path.
* **Current state:** Lyra already uses 5 threads — main, DSP
  worker, RX, EP2 writer, plus WDSP's internal C thread.
  Adding a 6th dedicated PS thread is straightforward.
* **Scope:** design happens in v0.2 (when TX-path threading
  is being architected anyway).  Implementation lands in
  v0.3 with PureSignal itself.
* **Watch:** thread affinity / NUMA hints on multi-core
  systems become relevant once we have this many threads.

#### 3. Explicit modern-hardware floor (low-cost cleanup)

* **Motivation:** install guide + README are currently
  vague on minimum requirements.  We assume modern
  SSE/AVX (WDSP cffi requires it) and OpenGL 3.3+ (GPU
  panadapter widget requires it) — but say so nowhere
  user-facing.
* **What's needed:** explicit "Windows 10 / 11, x86-64
  with SSE 4.1+, GPU with OpenGL 3.3+ for accelerated
  panadapter" line in install guide + README.  Inno
  Setup already enforces Windows 10 1809+ baseline (see
  `build/installer.iss` `MinVersion=10.0.17763`).
* **Scope:** small, do anytime.  Probably bundle with
  the next docs-touch commit.

#### 4. Multi-radio refactor groundwork (v0.4)

* **Motivation:** Brent's ANAN G2 needs Protocol 2 + a
  factored radio abstraction that doesn't bake HL2
  assumptions into UI/dispatch.
* **Current state:** capability-driven UI discipline
  (see `lyra/protocol/capabilities.py` and the
  pre-commit hook that bans `isinstance(*, HL2*)`
  checks in `lyra/ui/`) is already paying down this
  debt as we go.
* **Scope:** spread across v0.2 + v0.3 incrementally;
  v0.4 is when ANAN-specific work lands.

#### When to revisit

This section comes off PARKED when any of these triggers
fire:

1. v0.2 TX-path planning kicks off (decisions 2 + 3 + 4
   should inform that design).
2. A tester reports AMD-GPU-specific issues that hint at
   compute-shader needs (decision 1).
3. Install-time confusion from a tester on minimum
   requirements (decision 3 — bump to "do now").

Until then: latency win is shipped, RX2 is in tester hands,
TX is next.  No need to swing at these now.

### 15.9 — TX visual state design (PARKED 2026-05-13, scope: v0.2)

Defines the unified color language for "is Lyra transmitting"
indicators across all RX2 modes (OFF / SUB / SPLIT).  Filed
during a §15.6 follow-up conversation about what happens
visually when the operator goes TX in non-SPLIT mode,
specifically when BW lock is OFF and RX BW differs from TX BW.

#### Core principle: red = "transmitting RIGHT NOW"

Every red UI element tells the operator the same thing: this
is where you are on the air at this moment.  One color, one
meaning, applied uniformly across every visual surface that
could indicate TX state.  Operator's peripheral vision latches
onto whatever is red and reads "I am transmitting" without
needing to track multiple cues.

This rule extends the existing Phase 3.E
``FrequencyDisplay.set_tx_active`` red treatment (the VFO LED
goes red on PTT) into a project-wide convention.

#### What turns red on PTT

| Element | Color while RX | Color while TX-active |
|---------|----------------|------------------------|
| VFO LED (the transmitting VFO) | Normal | **Red** (Phase 3.E, already wired) |
| Passband rectangle (TX VFO) | Cyan (RX BW) | **Red** (TX BW; see below) |
| Audio meter readouts | S-meter | PWR / SWR / ALC |
| SPLIT TX marker (when SPLIT enabled) | Cyan/amber (per §15.6) | **Red** (overrides §15.6 idle color) |
| Status bar accent (optional polish) | Normal | Red accent (deferred to v0.2 polish pass) |

The passband rectangle is the key new behavior.  When BW
lock is OFF and the operator has a different TX BW than RX
BW configured for the current mode, the rectangle width
*changes* on PTT — operator sees the actual TX filter
width, not the RX filter width.  Eliminates the "I thought
I had a wide TX filter but didn't" surprise on ESSB and
the inverse on CW.

#### Cross-mode behavior table

| Mode | RX BW rect | TX marker/rect during TX |
|------|-----------|---------------------------|
| **OFF**, listening | Cyan | (none — same VFO) |
| **OFF**, transmitting | (display swaps) | **Red rectangle at focused VFO BW** |
| **SUB**, listening | Cyan | (none — TX = focused VFO) |
| **SUB**, transmitting | Cyan stays on unfocused RX | **Red rectangle at focused VFO BW** |
| **SPLIT**, listening | Cyan (VFO A) | Cyan/amber marker + BW rect (VFO B, idle) |
| **SPLIT**, transmitting | Cyan stays on VFO A (still RX) | **Red** marker + BW rect (VFO B) |

Nice property: red is always something *new appearing* or
*changing*, never the steady-state RX visualization.  Anything
red on screen = active transmission, full stop.

#### §15.6 reconciliation

§15.6 said the SPLIT TX marker should be "cyan or amber, NOT
red since red is reserved for TX-active."  Under §15.9 that
becomes a clean two-state pattern:

* **SPLIT enabled, not transmitting** → marker is cyan/amber
  ("here's where I'd TX if I keyed up now" — informational)
* **SPLIT enabled, PTT firing** → marker turns **red**
  (everything-red-means-active rule wins)

This is fully consistent with §15.6's intent; §15.9 just
makes the steady-state-vs-active transition explicit.

#### Implementation scope (when greenlit in v0.2)

1. `spectrum.py` / `spectrum_gpu.py` passband rectangle: add
   a "TX active" branch that reads ``radio.tx_bw_for(mode)``
   and renders red instead of cyan.  ~20 LOC across both
   widget classes plus the GPU shader uniform.
2. Wire `radio.tx_active_changed` signal (Phase 3.E already
   defines it) into the spectrum panels so the repaint fires
   on PTT edges, not just on next FFT tick.
3. SPLIT TX marker color logic: extend the existing color
   uniform to flip to red on `tx_active_changed`.
4. Palette in `lyra/ui/palettes.py`: add semantic names
   (`COLOR_TX_ACTIVE`, `COLOR_SPLIT_TX_IDLE`,
   `COLOR_RX_PASSBAND`) so the rule is centralized, not
   sprinkled across widget code.
5. Color picker in Settings → Visuals → Colors should expose
   these three semantic colors so operators with red/green
   colorblindness can override (operator request implicit;
   palette already has the click-label color picker
   infrastructure).

#### When to revisit

Comes off PARKED when v0.2 TX bring-up reaches the point of
needing visual state for PTT.  At that point:

* Verify color palette renders distinguishably on all eight
  waterfall palettes (a red TX rectangle on a Rainbow
  waterfall could be hard to see — may need an outline /
  glow to ensure it always stands out)
* Audit all panadapter overlays for color collisions (peak
  markers, landmark triangles, TCI spot boxes, EiBi labels —
  any of these defaulting to red would muddy the signal)
* Tester-visible polish: brief animated flash or short fade-in
  on the red transition, optional

Until then: idea is captured; v0.1 ships with RX-only path
where none of this is triggerable.

### 15.10 — RIT/XIT controls (PARKED 2026-05-13, scope: v0.1.x or v0.2)

Surfaced during the §15.6 SUB/SPLIT/OFF tri-state design
conversation when operator (Rick) noticed RIT/XIT had been
omitted from that section.  RIT (Receiver Incremental Tuning)
is an RX-only frequency offset typically ±0 to ±9.99 kHz used
to chase a slightly off-frequency DX station without retuning
the main VFO; XIT (Transmitter Incremental Tuning) is the TX
mirror image used for split-style operation without engaging
full SPLIT mode.  Standard on every HF rig built in the last
40 years; conspicuous absence in Lyra.

#### Placement: TUNING panel CW-pitch row

Operator-proposed and locked: extend the existing horizontal
row on the TUNING panel that already holds
``CW Pitch label + spin → SUB → 1→2 → 2→1 → ⇄``
(``lyra/ui/panels.py`` ``self.cw_pitch_row``, L591 onward).

Two new buttons added between ``⇄`` and the trailing
``addStretch(1)``:

```
[ CW Pitch | spin ]   [ SUB ]   [ 1→2 ]   [ 2→1 ]   [ ⇄ ]   [ RIT ]   [ XIT ]
```

Both buttons follow the existing **lit-when-active** idiom
already used for AGC ``AUTO``, NR Mode 1-4, AEPF, LMS, etc.
Operator's mental model: "highlighted = doing something."
Zero new visual vocabulary.

Row is currently sandwiched between two ``addStretch(1)``
spacers, so the row floats centered and can absorb the two
new buttons cleanly at typical window widths.  No combobox
fallback needed.

#### Interaction model (Option 1 from §15.10 design chat)

The buttons themselves are visually minimal — the **offset
value** and **clear** functions are gestures, not separate
widgets on the row.  Keeps the TUNING panel tidy and matches
the AGC right-click + Shift-click pattern operators already
know.

| Gesture | Action |
|---------|--------|
| Click ``RIT`` / ``XIT`` | Toggle on/off (button lights when active) |
| Right-click ``RIT`` / ``XIT`` | Open small popup: spin-box for offset (±0..±9.99 kHz, 10 Hz step) + ``Clear`` button + ``Close`` button |
| Shift-click ``RIT`` / ``XIT`` | Instant zero (offset → 0, button stays lit if it was lit) |
| Hover (when lit) | Tooltip shows live offset (e.g., ``"RIT: +1.20 kHz"``) |

Operators who want a dedicated spin-box surface can use the
right-click popup; operators who want fast in-and-out get
the button + shift-click idiom.

#### Persistence

Two new QSettings keys (under existing ``radio/`` group):

* ``radio/rit_enabled`` (bool, default False)
* ``radio/rit_offset_hz`` (int, default 0, signed)
* ``radio/xit_enabled`` (bool, default False)
* ``radio/xit_offset_hz`` (int, default 0, signed)

RIT/XIT state restores on app launch.  Per-band memory NOT
needed (these are session-level offsets, not band defaults —
matches industry convention).

#### Scope: RIT ships pre-TX, XIT waits for v0.2

* **RIT** is RX-only and can ship in a v0.1.x patch release
  ahead of TX (operator can use it the day it lands — chase
  a drifted DX station, listen 200 Hz off the center of a CW
  signal without retuning, etc.).
* **XIT** only matters when TX exists.  Button renders in v0.1.x
  but **disabled with explanatory tooltip** ("XIT activates
  with TX in v0.2") so the row layout is final and doesn't
  shift when v0.2 lands.

Implementation effort estimate: ~1 day for RIT (button +
popup + Radio offset plumbing + spectrum-marker shift +
QSettings persistence + help docs); XIT enable in v0.2 is
~2 hours on top of that since the UI surface is already
built and tested.

#### Effect on DSP / protocol

* **RIT path:** offsets the **DDC frequency** by the RIT value
  while leaving the displayed VFO unchanged.  Composes with
  the existing v0.0.9.8 ``_compute_dds_freq_hz`` central CW
  pitch offset (already centralizes "displayed VFO → actual
  DDS freq" math).  Add ``+ rit_offset_hz`` inside that helper
  when RIT is enabled.  Spectrum marker stays at displayed
  VFO; DDC center shifts; passband rectangle visually shifts
  the same amount on the panadapter (operator sees "I am
  listening here, but my VFO LED still reads the marked
  freq").
* **XIT path (v0.2):** mirror — offsets the **TX DDS frequency**
  by the XIT value while leaving the displayed TX VFO
  unchanged.  Lands when TX path lands in v0.2.

No protocol-layer changes.  Just one helper line in
``_compute_dds_freq_hz`` for RIT, and the equivalent in the
TX-freq helper when v0.2 ships XIT.

#### Visual feedback on the panadapter (optional polish)

When RIT is lit, a small amber tick-mark + label could be
drawn on the panadapter at the **displayed-VFO** position
(distinct from the orange tuning marker, which would now sit
at the **actual DDC center**).  Lets operator see at a glance
"my VFO marker says here, but I'm actually listening here."

Deferred to a polish pass — first implementation can ship with
just the lit button + tooltip + marker shift, and we add the
panadapter tick-mark if a tester says they wanted it.

#### When to revisit

* **RIT**: any time after v0.1.0 stable ships and tester
  feedback on the dual-RX UX has settled.  Likely v0.1.1 or
  v0.1.2 polish window.
* **XIT**: v0.2 TX bring-up — re-read this section when wiring
  the TX VFO offset, take the ~2 hour enable path.

Until then: row layout decision is final, gestures are
locked, persistence keys reserved.  Operator can stop
mentally tracking "we forgot RIT" — it's captured.

### 15.11 — Diagnostic overlay 3-state toggle (PARKED 2026-05-13, scope: v0.1.x or v0.1.0 GA)

Operator-driven UX polish (Rick, 2026-05-13).  After Brent +
Timmy tester reports came back clean on pre3 ("very few audio
pops" both, "sync much better" from Timmy), operator surfaced
that the on-screen diagnostic surfaces — ADC pk/rms, AGC thr/
gain, AUTO LNA messages, audio stream errors — are useful for
diagnosis but visually busy for routine operating.

#### What the surfaces actually cost

For the record (so future sessions don't re-derive it): the
CPU cost of these surfaces is **negligible** — well under
0.1% of one core continuous, GPU cost is zero.  Breakdown:

| Surface | Mechanism | Cost |
|---------|-----------|------|
| ADC pk/rms (top right) | `FrameStats` parses EP6 status bytes regardless of widget visibility; ~1 emit/sec | ~10-20 µs/sec |
| AGC threshold + gain (top right) | `GetRXAMeter(RXA_AGC_GAIN)` throttled to ~6 Hz | ~50 µs/sec |
| AUTO LNA messages (lower left) | Event-driven only — fires on state change | <1 µs/sec idle |
| Audio stream errors (lower right) | Event-driven only — fires on underrun/overrun | <1 µs/sec when clean |

**Implication: this is a UX feature, NOT a CPU-saving feature.**
Hiding doesn't free measurable compute.  Frame the toggle to
operators as "clean main window" not "save CPU."

#### 3-state spec

Replaces today's "always show" behavior with a 3-position
combobox.  Default `"full"` preserves current behavior on
upgrade.

| Mode | ADC pk/rms | AGC thr/gain | AUTO LNA strip | Audio errors strip |
|------|-----------|--------------|----------------|---------------------|
| **Full** (default) | Visible | Visible | Persistent strip | Persistent strip |
| **Minimal** | Visible | Visible | Toast on event | Toast on event |
| **Off** | Hidden | Hidden | Toast on event | Toast on event |

In Minimal/Off, AUTO LNA state changes + audio underrun events
surface via the existing ``_toast_message`` mechanism (sibling
of weather alerts + band-edge warnings) — operator still gets
notified of events that need attention, just without persistent
real-estate use.

#### Placement

Settings → Radio tab → existing ``QGroupBox("Toolbar readouts")``
in ``lyra/ui/settings_dialog.py:985``.  Rename the group to
**"Toolbar & diagnostic readouts"** and add one row below the
existing ``show_cpu_chk`` checkbox:

```
┌─ Toolbar & diagnostic readouts ─────────────────┐
│  ☐ Show CPU% on toolbar                         │
│                                                 │
│  Diagnostic overlays:   [Full         ▾]        │
│                          • Full                 │
│                          • Minimal              │
│                          • Off                  │
│  ⓘ Controls ADC, AGC, audio status overlays on  │
│     the spectrum widget.                        │
└─────────────────────────────────────────────────┘
```

Combobox over radio buttons — matches the style of other
Settings dropdowns (Step picker, AGC profile picker) and keeps
vertical real estate tight.

#### Persistence

New QSettings key under ``telemetry/`` group:

* ``telemetry/overlay_mode`` (string, default ``"full"``,
  values ``"full"`` / ``"minimal"`` / ``"off"``)

Loaded at ``Radio.__init__`` startup, applied to all four
widget surfaces on first paint, mirrored back on every
combobox change.

#### Implementation effort

* ~30 minutes total:
  * 5 min: Settings dialog combobox + signal wiring
  * 10 min: 4 widget visibility hooks (ADC strip, AGC strip,
    AUTO LNA strip, audio errors strip) reading
    ``telemetry/overlay_mode`` from QSettings
  * 10 min: toast-fallback path for AUTO LNA + audio errors in
    Minimal/Off modes (reuse existing ``_toast_message``)
  * 5 min: QSettings persistence + autoload
* Zero risk to RX/audio paths — pure widget visibility + signal
  routing change.  No protocol or DSP touched.
* Live-switchable; no restart needed (matches existing CPU%
  toggle behavior).

#### Scope decision

Either bundle into v0.1.0 GA (small enough not to risk the
release) or ship in a v0.1.0.x patch shortly after.  Operator's
call — neither blocks anything else.

Status: **PARKED** for implementation when v0.1.0 GA scope is
finalized.

### 15.12 — Windows audio API expansion ladder (PARKED 2026-05-13)

Operator-surfaced 2026-05-13: PC Soundcard path currently runs
on **WASAPI Shared mode** (sounddevice / PortAudio default).
Discussion covered ASIO, WDM-KS, WASAPI Exclusive, and Virtual
Audio Cable.  All four are accessible via sounddevice's host-
API selection — the work is mostly UI surface + opt-in toggles,
not new audio infrastructure.

#### Current state

| Path | Audio backend | Latency |
|------|---------------|---------|
| HL2 audio jack (default) | EP2 → AK4951 codec (no Windows API at all) | Governed by HL2 gateware reg 0x17 — 15 ms post-§15.7 |
| PC Soundcard | sounddevice → WASAPI Shared | ~150 ms rmatch ring + ~22 ms WASAPI host = **~172 ms** post-§15.7 |

#### API expansion comparison

| API | Host latency | Trade-off |
|-----|--------------|-----------|
| **WASAPI Shared** (current) | ~20-25 ms | Other apps share the device |
| **WASAPI Exclusive** | ~3-5 ms | **Blocks other apps** from the device while Lyra runs |
| **ASIO** | ~2-10 ms (driver-dependent) | Requires ASIO driver (ASIO4ALL or native pro-audio card) |
| **WDM-KS** (Kernel Streaming) | ~5-10 ms | Less universal hardware support than WASAPI Exclusive |

WASAPI Exclusive → ~15-20 ms saved on PC Soundcard path
(172 → 155 ms total).  ASIO → similar.  WDM-KS → marginal.

#### Effort estimates

| Feature | Effort | Notes |
|---------|--------|-------|
| WASAPI Exclusive toggle | ~1-2 hr | One checkbox in Settings → Audio, pass ``WasapiSettings(exclusive=True)`` via sounddevice ``extra_settings`` |
| Host-API grouping in device picker | ~2 hr | Group devices by ``sd.query_hostapis()`` in Settings → Audio dropdown ("WASAPI", "ASIO", "MME", etc.). Foundation for ASIO/WDM-KS without yet exposing them. |
| ASIO support | ~4-6 hr | Enumerate ASIO host API devices, add to picker. Test with ASIO4ALL + at least one native ASIO driver. |
| WDM-KS support | ~2-4 hr | Same pattern as ASIO, smaller payoff vs Exclusive. |
| Virtual Audio Cable workflow | **already works** | VAC registers as a regular audio device — operator picks ``"VAC Line 1"`` in the dropdown. Worth documenting + bench-testing, no code work. |

#### Honest reality checks

**ASIO matters more for TX than RX.** Its killer feature is sub-3-ms
round-trip latency for live monitoring (key-down to sidetone, mic-
to-monitor).  For RX listening where we already have ~150 ms total
audio path, the gap between 5 ms ASIO and 22 ms WASAPI Shared is
mostly imperceptible.  **Defer ASIO to v0.2 TX bring-up** — do it
once, do it well, get the monitor-while-talking latency story
right.

**WDM-KS** has the smallest win of the four.  WASAPI Exclusive
covers the same use case with wider hardware support.  Skip
unless a tester surfaces a specific need.

**VAC is operationally the most important** — it's how operators
bridge Lyra audio to WSJT-X / FLDigi / DM780 / etc. for digital
modes.  Already functional today across all host APIs; needs
documentation + a confirming bench test rather than code work.

#### Prioritized ladder

When the v0.1.x window opens (post-GA), pick off in this order
based on operator/tester appetite:

1. **WASAPI Exclusive toggle** — 1-2 hr, immediate ~15 ms PC
   Soundcard win, opt-in (default off → zero regression risk).
   Single best bang-per-hour on the list.
2. **Document VAC digital-modes workflow** — help-doc addition
   covering recommended VAC routing to WSJT-X / FLDigi.  Free
   — code already works.
3. **Host-API grouping in device picker** — 2 hr.  Lays the bones
   for ASIO/WDM-KS without yet exposing them; operators see
   "WASAPI: PC Speakers" / "ASIO: Focusrite Scarlett" naming
   even today and can mentally pick the right path.
4. **v0.2 TX-time: ASIO support** — 4-6 hr in the v0.2 window.
   TX-side monitoring latency justifies it then; doing it
   earlier means writing it once for RX-only benefit and again
   when TX needs the round-trip story.
5. **Maybe never: WDM-KS** — wait for a tester to ask.

#### Settings UI placement (when implemented)

Settings → Audio tab (existing).  Pattern:

* Device picker grouped by host API (step 3)
* Below picker: **"Exclusive mode (lower latency, blocks other
  apps from this device)"** checkbox — default off (step 1)
* ASIO-specific knobs (buffer size, dither setting) appear
  inline below device picker IF an ASIO device is currently
  selected — show/hide based on selected device's host API
  (step 4)

QSettings keys:

* ``audio/host_api`` (string, default ``""`` = auto-pick first
  WASAPI device)
* ``audio/exclusive_mode`` (bool, default False)
* ``audio/asio_buffer_size`` (int, default 0 = driver default —
  step 4)

#### Side benefit worth flagging

Operators on weak machines hitting audio dropouts at 150 ms
rmatch ring could try Exclusive mode FIRST as a remediation
step before reverting to the 400 ms pre-§15.7 default.
Smaller host buffer = lower jitter ceiling = potentially
viable at the new defaults where it wasn't on Shared.  Document
this in the troubleshooting "latency tuning" section when
Exclusive lands.

Status: **PARKED** — items 1-3 are v0.1.x patch candidates,
item 4 is v0.2-bundled, item 5 is wait-and-see.

### 15.13 — Compression-mode Lit-Arc chip moved to v0.2 (DEFERRED 2026-05-13)

The consensus plan §7.1(c) originally targeted the Compression
chip for the v0.1.0 polish pass.  Reviewed at GA pre-flight
(2026-05-13) and **deferred to v0.2** for a concrete reason:

The plan's RX-side signal source was ``Radio.agc_gain_db`` — but
that name was aspirational; the actual implementation uses the
existing ``agc_action_db`` signal (the same one that already
drives the AGC chip).  Shipping a COMP chip in v0.1 GA would
mean **two chips displaying the identical signal** with only a
color/label distinction.  Operators would see them light up in
lockstep with no functional difference — confusing UX, not a
feature.

The chip's design value emerges in v0.2 when TX bring-up adds
``Radio.tx_comp_db_changed`` (sourced from WDSP
``GetTXAMeter(TXA_LVLR_GAIN)``).  At that point the COMP chip
auto-switches signal source on MOX edges per consensus-plan
§8.4 — RX-side AGC gain on receive, TX-side leveler gain on
transmit — and the two chips finally carry distinct meanings.

**Scope when revived in v0.2:**

* Add ``MODE_COMP`` to ``smeter.py`` ``AVAILABLE_MODES`` tuple
* Cool/neutral color gradient (consensus plan §7.1(c) — distinct
  from AGC's blue gradient so operator-distinguishable when both
  visible)
* Wire RX-side: existing ``agc_action_db``
* Wire TX-side: new ``tx_comp_db_changed`` (lands with WDSP
  ``compress.c`` cffi binding in v0.2.1 per CLAUDE.md §4.1)
* MOX-edge auto-switch lives in the chip's signal-routing code
  (read on the dispatch state change, swap connection target)
* Help-doc update: ``docs/help/smeter.md`` gets a COMP section

Status: **DEFERRED to v0.2** — re-read this section when wiring
the TX leveler meter signal chain in v0.2.1.  All other v0.1
GA Phase 4 items proceed as planned.

### 15.14 — Auto-mute-on-TX rules moved to v0.2 (DEFERRED 2026-05-13)

The consensus plan §8.1 / Phase 4 §7 (v0.1.0) targeted operator
settings for ``MuteRX1OnVFOBTX`` and ``MuteRX2OnVFOATX`` — auto-
mute rules that would fire on PTT edges so the operator doesn't
hear their own transmit through the receiver.

A pre-wire implementation landed briefly in v0.1.0 GA prep
(commit ``b8eb8d0``, 2026-05-13) — Radio-side state + signals +
setters + QSettings persistence + two checkboxes on Settings →
Audio.  **Reverted same session** at operator pushback because:

1. **Lyra's UX discipline says "if it's on screen, it does
   something."**  Two checkboxes that explicitly state "doesn't
   activate until v0.2" violate the rule we've been holding
   ourselves to (see NR2 strength slider hidden in WDSP mode,
   Audio Leveler deleted when WDSP AGC subsumed it, Compression
   chip deferral §15.13).
2. **Manual Mute-A / Mute-B buttons already cover the operating
   case.**  Per Phase 3.E.1 hotfix v0.16 (CLAUDE.md §6.2), the
   per-RX mute buttons are always visible on the TUNING panel.
   Operators can manually mute either RX before keying up — the
   auto-mute is convenience, not necessity.
3. **The "pre-configure for v0.2" rationale is weak.** Operators
   will configure it in v0.2 anyway; saving 10 seconds of clicks
   isn't worth months of inert UI.
4. **Natural home is v0.2.**  When the PTT state machine lands
   and the AAmixer auto-mute logic is written, the Settings UI
   + state + persistence all want to land in the same commit as
   the behavior they drive.

**Scope when revived in v0.2:**

* 2 new Radio signals: ``mute_rx1_on_vfob_tx_changed``,
  ``mute_rx2_on_vfoa_tx_changed``
* 2 new state attributes: ``_mute_rx1_on_vfob_tx``,
  ``_mute_rx2_on_vfoa_tx`` (defaults False)
* 2 new setters: ``set_mute_rx1_on_vfob_tx``,
  ``set_mute_rx2_on_vfoa_tx`` (persist immediately to
  ``dual_rx/mute_rx*_on_vfo*_tx`` QSettings keys + emit signal)
* 2 new ``@property`` accessors
* ``autoload_rx2_state`` extension for both prefs
* New "Dual-RX behavior during transmit" ``QGroupBox`` on the
  Audio tab in ``settings_dialog.py`` (after the host API
  picker, before ``v.addStretch(1)``)
* Bidirectional sync (Radio signal ↔ checkbox)
* **Plus the behavior**: AAmixer reads ``_mute_rx*_on_vfo*_tx``
  on MOX edges (via dispatch-state subscriber pattern, NOT
  hardcoded if/else) and routes mute accordingly

Commit ``b8eb8d0`` is in git history if anyone needs to recover
the pre-wire skeleton — it's a 183-line diff that's mostly
correct, just needs the behavior layer added on top.  Read it
with ``git show b8eb8d0`` when picking back up.

QSettings keys ``dual_rx/mute_rx1_on_vfob_tx`` and
``dual_rx/mute_rx2_on_vfoa_tx`` are reserved.

Status: **DEFERRED to v0.2** — re-read this section when
writing the PTT state machine + AAmixer auto-mute path.  GA
Phase 4 punch list shrinks to AAmixer state badge + TCI RX2
channel (items 4 + 5 only).

### 15.15 — AAmixer state indicator badge moved to v0.2 (DEFERRED 2026-05-13)

Consensus plan §1 + §10 + Phase 4 §7 (v0.1.0) targeted a small
visual badge consolidating the 8-way AAmixer state machine
(``Power × MOX × diversity × PS``, plus RX2-enabled and operator-
mute toggles) into a single at-a-glance indicator.  Plan
rationale: Thetis makes operators infer audio-mixing state from
a scatter of independent button states (chkPower / chkMOX /
chkRX2 / PS button); Lyra's UX improvement is one consolidated
badge.

**Reassessed at GA pre-flight (2026-05-13) — same principle as
§15.13 + §15.14: the badge's value emerges when there are
multiple state axes to consolidate.  In v0.1 RX2-only, the
state space collapses to:**

| State | What the badge would show |
|-------|---------------------------|
| Stream stopped | ``OFF`` |
| Stream running, single RX | ``RX1`` |
| Stream running, SUB on | ``SUB`` |

…and every one of those is already visible on existing UI:
* "Stream running" is read from the toolbar Start/Stop button
* "SUB on" is read from the SUB button on the TUNING panel
  (lit when active per Phase 3.E.1 hotfix v0.16)

Shipping the badge in v0.1 means putting a label saying ``SUB``
right next to a lit button labeled ``SUB`` — redundant, not
informative.

**Scope when revived in v0.2 (and beyond):**

* In v0.2 TX: badge picks up ``TX``, ``TX (split)``,
  ``TX (RX1 muted)`` etc. — combinations operators currently
  can't read at a glance because they involve dispatch state
  + auto-mute rule + SPLIT toggle interactions.
* In v0.3 PS: badge picks up ``PS-armed``, ``PS-cal``,
  ``PS-paused (RX2 suspended)`` per consensus plan §2.2 CR-1.
* Color coding tied to §15.9 red-on-air rule — TX-state badges
  go red, RX-state badges stay neutral.

**Placement when implemented:** small badge in the status bar
(left side, near the connection indicator) so it stays peripheral
but always visible.  Click to expand a tooltip explaining the
current full dispatch state.

**Data source:** ``Radio.dispatch_state_changed`` signal already
fires on every relevant edge (MOX, ps_armed, rx2_enabled, family)
— the badge just subscribes and renders a state-name lookup.
No new Radio surface needed; just a UI consumer.

Status: **DEFERRED to v0.2** — re-read this section alongside
§15.9 (red on-air rule) when wiring TX visual state.  GA Phase
4 punch list shrinks to just item 5 (TCI RX2 channel).

### 15.16 — v0.1.1 "Polish & Audio Routing" scope lock (PARKED 2026-05-14)

After v0.1.0 GA shipped (2026-05-14), operator (Rick) proposed
bundling several small parked items into a single follow-on
release rather than spinning each as its own v0.1.0.x patch.
Scope **locked** during the GA post-ship conversation; capture
it here so it survives session compaction.

#### Five items bundled

| # | Item | From | Effort | Value |
|---|------|------|--------|-------|
| 1 | **RIT** (RX-only Receiver Incremental Tuning) | §15.10 | ~1 day | Operator-requested gap from every HF rig in last 40 years |
| 2 | **TCI RX2 channel** | "Parked for v0.2" in v0.1.0 GA | ~1 day | Critical for SDRLogger+ workflow — focused validation |
| 3 | **WASAPI Exclusive toggle** | §15.12 item 1 | ~1–2 hr | ~15 ms PC Soundcard latency win (172 → ~155 ms) |
| 4 | **VAC digital-modes workflow doc** | §15.12 item 2 | ~30 min | Already functional; just document for WSJT-X / FLDigi |
| 5 | **Host-API grouping in device picker** | §15.12 item 3 | ~2 hr | Groups devices by WASAPI / ASIO / MME; ASIO foundation |

#### Discovery 2026-05-14 — item statuses corrected

While starting v0.1.1 work the operator asked how to tell whether
WASAPI is in shared or exclusive mode and noted the device list
in Settings → Audio is unorganized.  Audit results:

* **Item 3 (WASAPI Exclusive toggle):** ✅ **ALREADY DONE since
  v0.0.9.6.**  Settings → Audio has a "PortAudio host API"
  dropdown with seven entries (Auto / WASAPI shared / WASAPI
  exclusive / WDM-KS / DirectSound / MME / ASIO).  Selecting
  "WASAPI exclusive" pipes ``sd.WasapiSettings(exclusive=True)``
  through to PortAudio via ``extra_settings`` (see
  ``lyra/dsp/audio_sink.py`` line 547-557).  Full tooltip already
  explains the trade-off.  Operator-perceived UX gap: the
  dropdown is buried under a separate group titled "PortAudio
  host API (PC Soundcard only)" and not visually paired with the
  device list, so it doesn't read as "WASAPI exclusive checkbox"
  from §15.12 item 1's wording -- but the functionality is there.

* **Item 5 (Host-API grouping in device picker):** **PARTIAL.**
  The host-API SELECTION (dropdown above) is fully done.  The
  OUTPUT DEVICE list directly below it remains a flat list
  sorted by PortAudio index, which interleaves duplicates of
  the same physical device across host APIs (e.g.
  ``Speakers (Realtek)`` appears once per host API).  The label
  format ``[idx] DeviceName  (HostAPI, channels, rate)`` carries
  the host-API name but doesn't visually group.  This is the
  remaining v0.1.1 work item: rewrite ``_populate_devices()`` in
  ``settings_dialog.py:AudioSettingsTab`` to emit grouped output
  with host-API section dividers, so the operator sees:

  ```
  ─── WASAPI shared ───
  [4] Speakers (Realtek)  2ch 48 kHz
  ─── WASAPI exclusive ───
  [4] Speakers (Realtek)  2ch 48 kHz
  ─── WDM-KS ───
  [6] Speakers (Realtek)  2ch 48 kHz
  ─── DirectSound ───
  [1] Speakers (Realtek)  2ch 48 kHz
  ─── MME ───
  [7] Speakers (Realtek)  2ch 48 kHz
  ```

  Same physical device naturally appears once per available host
  API -- the section header makes that explicable instead of
  confusing.

  Implementation effort: ~2 hours.  Pure UI work in
  ``_populate_devices()``; no Radio surface, audio path, or
  QSettings schema changes needed.

* **Items 2 + 4 status unchanged:** VAC doc done (commit
  ``82a8596``); RIT and TCI RX2 still real coding work pending.

**Total**: ~3–4 days of focused work + bench testing.

#### Why bundle vs ship as 5 rapid-fire patches

* **Zero merge conflict surface** — all five touch different
  subsystems (tuning + DDC for RIT, TCI server + spot routing,
  sounddevice WasapiSettings for Exclusive, help-docs for VAC,
  Settings → Audio dropdown for host-API grouping).
* **All RX-only** — no v0.2 TX state-machine entanglement; each
  item is mergeable without waiting on v0.2 work.
* **Single release ritual** — one CHANGELOG entry, one version
  bump, one build, one bench-test pass at the end vs five.
* **Operationally coherent narrative** — "Polish & Audio
  Routing" reads better to testers than 0.1.0.1 / 0.1.0.2 /
  0.1.0.3 / 0.1.0.4 / 0.1.0.5.

#### Explicit deferrals (NOT in v0.1.1)

* **XIT** (§15.10 second half) — renders disabled-but-visible
  in v0.1.1.  Enable lands in v0.2 when TX path exists (~2 hr
  enable on top of v0.1.1 RIT infrastructure).
* **ASIO support** (§15.12 item 4) — wants the TX-side
  monitor-latency story (key-down to sidetone, mic-to-monitor)
  to inform implementation.  Lands in v0.2 alongside TX
  bring-up; host-API grouping (item 5 above) lays the
  foundation so it's a small add then.
* **WDM-KS** (§15.12 item 5) — wait for a tester to ask.

#### Implementation order (suggested when work begins)

1. **VAC doc first** (~30 min, zero-risk) — operator can ship
   the doc-only change to testers immediately if useful.
2. **WASAPI Exclusive toggle** (~1–2 hr) — single Settings
   checkbox + sounddevice ``WasapiSettings(exclusive=True)``;
   smallest code surface, biggest tester latency win.
3. **Host-API grouping** (~2 hr) — Settings → Audio dropdown
   rewrite; lays foundation for ASIO in v0.2.
4. **TCI RX2 channel** (~1 day) — TCI server changes touch
   the SDRLogger+ integration that's N8SDR's daily workflow;
   focused independent validation pass.
5. **RIT** (~1 day) — UX changes (TUNING panel button +
   right-click popup + Shift-click zero) + central
   ``_compute_dds_freq_hz`` offset + persistence + spectrum
   marker shift + help-doc.

Roughly sequenced low-risk → higher-risk so a tester blocker
on the latter items doesn't gate the earlier wins.

#### Implementation refs (when work begins)

* **RIT**: §15.10 has the full spec — TUNING panel ``cw_pitch_row``
  in ``lyra/ui/panels.py`` L591, lit-button idiom matching
  AGC/NR Mode/AEPF/LMS, QSettings keys ``radio/rit_enabled``
  + ``radio/rit_offset_hz``, central ``+ rit_offset_hz`` in
  ``Radio._compute_dds_freq_hz``.
* **TCI RX2**: capability-driven; route ``set_dds(channel,
  freq_hz)`` for channel=1 (RX2) through ``Radio.set_freq_hz(
  target_rx=2, ...)``.  SDRLogger+ source at
  ``Y:/Claude local/hamlog/main.py`` shows current
  RX1-only client wiring.  No protocol changes needed.
* **WASAPI Exclusive**: ``lyra/dsp/audio_sink.py``
  SoundDeviceSink — add ``exclusive=True`` to
  ``sd.WasapiSettings``, gate on ``audio/exclusive_mode``
  QSettings key.
* **VAC doc**: extend ``docs/help/audio.md`` with a "Digital
  modes with VAC" section.  No code.
* **Host-API grouping**: ``lyra/ui/settings_dialog.py``
  AudioSettingsTab device dropdown — group by
  ``sd.query_hostapis()`` results.  ~80 LOC.

#### When to revisit

Any time after v0.1.0 GA settles with operators on real bands
and we have a sense of which (if any) field reports need
faster patching.  No external dependency; can start tomorrow
or wait two weeks.

Status: **CLOSED 2026-05-14** — all five items shipped in
v0.1.1.  See version-numbering history above for the full
release entry.  Bench-validated end-to-end with SDRLogger+
spots-on-RX2 round trip (Lyra tunes RX2 correctly; the
spot_activated echo back to SDRLogger+ has a logger-side
filter gap that operator owns separately).  v0.2 TX work is
next.

### 15.17 — DSP+Audio panel top-row redesign (PARKED 2026-05-14, scope: v0.1.2)

Operator-proposed cosmetic redesign of the DSP+Audio panel's
top row (2026-05-14, post v0.1.0 GA conversation).  Replaces
the three horizontal sliders (Vol RX1, Vol RX2, AF Gain) with
compact "[−] value [+]" stepper-readout widgets, banishes the
"Out" audio-path picker to a small icon-button popup on the
panel header (and ultimately to Settings → Audio for the
set-once posture).  Net effect: DSP+Audio panel top row loses
~150 px of horizontal real estate while gaining numeric
precision and accidental-drag immunity.

**Code is being written this session (2026-05-14) but commits
are HELD on the feature branch until the v0.1.2 release
window.**  No push, no build, no installer.  See "Workflow"
below.

#### Confirmed design decisions

| Decision | Locked value |
|----------|--------------|
| Step size (1 click) | 1 unit (1 dB for Vol + AF Gain) |
| Shift + click step | 5 units (5 dB) |
| Vol RX1 / RX2 unit | **dB** (matches AF Gain idiom) |
| Vol range (UI) | −60 dB ... 0 dB |
| Vol internal storage | unchanged — float 0.0..1.0 linear (UI converts via 20·log10 for display, 10^(dB/20) for set) |
| Vol floor display | "−60 dB" (not "−∞" — below-floor is Mute-A/Mute-B territory) |
| AF Gain range | 0 ... +80 dB (unchanged) |
| Reset-to-default gesture | None (operator: not needed) |
| Mouse-wheel modifier | Single step per notch, no Shift modifier needed |
| Click-and-hold ramp | 1 step immediately → 400 ms pause → 12 Hz repeat |
| Right-click readout | Opens QInputDialog for exact value entry (existing AGC-threshold gesture pattern) |
| "Out" picker placement | **Option B** — small icon-button on DSP+Audio header strip pops a 2-item menu (HL2 jack / PC Soundcard).  Future: 3-item menu when VAC support lands. |
| Mute-A / Mute-B placement | Unchanged — always-visible buttons next to the Vol stepper widgets (per §6.2 hotfix v0.16) |

#### Widget API (new file)

``lyra/ui/widgets/stepper_readout.py`` — reusable Qt widget,
roughly:

```python
class StepperReadout(QWidget):
    valueChanged = Signal(float)          # emits on every change

    def __init__(self,
                 label: str,               # e.g. "Vol RX1"
                 vmin: float, vmax: float,
                 step: float = 1.0,
                 shift_step: float = 5.0,
                 unit: str = "dB",
                 decimals: int = 0,
                 parent=None): ...

    # layout: [label] [−] [value]  [+]
    # children: QPushButton("−"), QLabel(value), QPushButton("+")
    #
    # features:
    #   - click [−]/[+] → step or shift_step (Shift held)
    #   - click-and-hold → ramp (400 ms pause, 12 Hz repeat,
    #     step accelerates after 2 sec to shift_step granularity)
    #   - mouse-wheel over widget → step
    #   - right-click value label → QInputDialog typed entry
    #   - palette-aware (inherits Lyra theme)
    #   - emits valueChanged on every step / typed entry
    #
    # operator-facing API:
    #   value() -> float
    #   setValue(float) -> None    # clamps to [vmin, vmax]
    #   setRange(vmin, vmax) -> None
```

Three instances on the panel: Vol RX1, Vol RX2 (with linear↔dB
shim in the wiring layer), AF Gain (direct dB pass-through).

#### Files affected

1. **NEW** ``lyra/ui/widgets/stepper_readout.py`` (~150 LOC) —
   the reusable widget.
2. ``lyra/ui/widgets/__init__.py`` — may need creating; exports
   StepperReadout for clean imports.
3. ``lyra/ui/panels.py`` — DspPanel top row rewrite.  Replace
   three QSlider instances with three StepperReadout instances.
   Replace "Out" QComboBox with a small QToolButton in the
   header strip that pops a QMenu (HL2 jack / PC Soundcard).
   Remove the now-unused slider/combobox slot handlers; add
   new slot handlers calling the existing
   ``radio.set_volume(..., target_rx=...)`` and
   ``radio.set_af_gain_db(...)`` setters.
4. ``lyra/ui/settings_dialog.py`` — AudioSettingsTab gets an
   "Audio output" row at the top.  Same widget logic as the
   header icon-button; both routes go through one shared
   "set audio output" helper on Radio.
5. ``docs/help/audio.md`` — screenshot refresh + paragraph
   explaining the new stepper-readout idiom (right-click for
   exact, Shift-click for 5 dB, click-and-hold for ramp).
6. ``docs/help/dsp_audio_panel.md`` (if exists) — top-row
   diagram update.

Estimated effort: **~1 day** (widget ~3 hr, panel rewire ~2 hr,
header icon-button ~1 hr, Audio Settings tab row ~30 min,
help docs ~1 hr, bench-test pass ~2 hr).

#### Linear ↔ dB conversion shim

Vol RX1 / RX2 internal storage stays at float 0.0..1.0 (no
QSettings migration, no Radio surface change).  Stepper widget
displays dB; the wiring layer between widget and Radio handles
the conversion:

```python
# panels.py DspPanel
def _on_vol_rx1_db_changed(self, db: float) -> None:
    # Floor: -60 dB clamps to 0.001 linear (-60 dB) so Radio's
    # 0.0..1.0 invariant holds without ever hitting exact 0.0
    # (which is Mute-A/Mute-B's job).
    if db <= -60.0:
        linear = 0.001
    else:
        linear = 10.0 ** (db / 20.0)
    linear = min(linear, 1.0)
    self._radio.set_volume(linear, target_rx=1)

def _on_radio_vol_rx1_changed(self, linear: float) -> None:
    # Inverse: clamp and display
    if linear <= 0.001:
        db = -60.0
    else:
        db = 20.0 * math.log10(max(linear, 1e-6))
    self.vol_rx1_stepper.setValue(db)  # widget rounds to 1 dB
```

Mute-A / Mute-B continue to work in parallel — they set
``_muted_rx*`` independently of ``_volume_rx*``, so muting then
unmuting restores the pre-mute Vol value cleanly.

#### Header icon-button for Out (Option B detail)

Pattern: a small ``QToolButton`` with ``InstantPopup`` style on
the DspPanel header strip (next to the existing panel title /
help button).  Tooltip = "Audio output: [current]".  Click
pops a ``QMenu`` with checkable actions:

* ☑ HL2 audio jack
* ☐ PC Soundcard
* (future: ☐ Virtual Audio Cable — added in §15.12-VAC work)

Icon: a small headphone / speaker glyph that subtly tints to
reflect current output (blue for HL2 jack since it's the
direct-codec path, amber for PC Soundcard since it's the
host-side path).  Same idiom as the existing TCI dot.

Persistence: existing ``audio/output`` QSettings key (no
migration).

#### Workflow — implement now, hold push / build

Operator decision 2026-05-14: implement and commit this work
locally during the same session as the §15.16 scope-lock, but
**hold push to origin and hold build/installer** until the
v0.1.2 release window opens.  Rationale:

* v0.1.0 GA already shipped this morning — pushing
  cosmetic-redesign commits to ``main`` minutes later muddies
  the release timeline.
* v0.1.1 is the §15.16 audio-routing batch — this redesign
  doesn't fit that narrative.
* v0.1.2 is the natural slot — "Polish" release where this
  is the headline feature alongside any other §15.x
  cosmetic items that accumulate.
* Local commits on ``feature/v0.0.9.6-audio-foundation``
  preserve the work + author-time without committing it to
  the published history yet.  If v0.1.1 takes priority later,
  this batch can wait; if v0.1.2 arrives first, push +
  build then.

When v0.1.2 release window opens: ``git push origin
feature/v0.0.9.6-audio-foundation`` carries everything
forward; standard release ritual per CLAUDE.md §11 applies.

#### When to revisit

* When v0.1.2 release window opens (after v0.1.1 ships).
* Or sooner if a tester reports the current sliders are
  causing accidental Vol changes — at which point this
  becomes a v0.1.1.x patch and we re-evaluate the scope.

Status: **PARKED + IN PROGRESS LOCALLY** — code being written
2026-05-14; commits will sit on the feature branch until v0.1.2
push window.

### 15.18 — v0.2.0 "TX Bring-up" scope lock + Phase tracker (IN PROGRESS 2026-05-14)

Locks the v0.2.0 sub-release scope per consensus plan §8.5
(Round 5 verified) and tracks Phase-by-Phase progress on the
``feature/v0.0.9.6-audio-foundation`` branch.  Pattern mirrors
§15.16 (the v0.1.1 scope-lock entry that got "CLOSED" stamped
when v0.1.1 shipped).

#### v0.2.0 scope (locked 2026-05-14, source: consensus §8.5)

SSB-only TX with the load-bearing dynamic-range bounds shipping
in the same release as the modulator itself.  Per the Round 5
review the original CLAUDE.md §7 cadence (which put leveler/EQ
in v0.2.3) would have shipped SSB transmit WITHOUT splatter-
bound limiters for two full sub-releases.  v0.2.0 now bundles:

* **Modulator chain**: SSB (USB/LSB) via WDSP TXA cffi --
  ``wdsp_tx_engine.py`` with ``TxChannel`` class mirroring
  ``wdsp_engine.RxChannel``.  ~40 cffi entries in
  ``wdsp_native.py`` (the v0.1 TX section had 3 stubs marked
  "declared but unused for v0.0.9.6 RX-only PoC").
* **MOX / PTT state machine**: Phase 0 added ``lyra/ptt.py``
  scaffolding; Phase 3 wires the live state machine + hardware
  PTT input forwarder (forwarded from EP6 ``FrameStats.ptt_in``
  via QueuedConnection per CLAUDE.md §5 threading model).
* **TX power control**: HL2 step attenuator -28..+31 dB via
  ``capabilities.tx_attenuator_range``; Phase 1 wired the
  protocol-layer setter (``_tx_step_attn_db`` state attribute
  feeding frame-4 C3 5-bit + frame-11 C4 6-bit-with-override
  redundant writes).  Phase 3 UI exposes the operator-facing
  "TX Power 0..100%" slider that maps through the cap struct.
* **Mic gain**: ``SetTXAPanelGain1`` with operator slider; Phase
  2 cffi binding work.
* **Leveler reuse**: WDSP ``wcpagc`` mode 5 cffi (shared with
  RX AGC binding).  Defaults: 5 ms attack / 250 ms decay / 5 dB
  maxgain per Thetis ``radio.cs``.
* **ALC**: WDSP ``xwcpagc`` block at TXA.c:579 -- the load-
  bearing limiter that prevents post-PA splatter.  Defaults:
  1 ms attack / 10 ms decay / -3 dBFS threshold.  **Always on**
  (no operator opt-out -- this is splatter protection, not a
  taste knob).
* **§8.2 sip1 TX I/Q tap**: ring buffer alloc + fill, no v0.3
  consumer yet.  Mandatory in v0.2 per the Round 5 rationale
  -- adding the tap as a no-op in v0.2 means v0.3 PureSignal
  can focus purely on PS work without re-validating every TX
  sub-mode.
* **RX/TX RTA scaffolding**: audio-domain FFT widgets render
  with live data + taps in place, no EQ yet.  v0.2.1 EQ +
  dynamics fills in the EQ overlay.
* **§15.9 red-on-air visual rule**: VFO LED + TX passband
  rectangle turn red on PTT.  ``tx_active_changed`` signal
  emitter wired in Phase 1 commit 10; Phase 3 wires the UI
  consumers (FrequencyDisplay / SMeter / spectrum widget).
* **§15.14 auto-mute-on-TX**: recover the 183-line skeleton
  from reverted commit ``b8eb8d0`` + add AAmixer
  per-stream-multiplier behavior.
* **§15.15 AAmixer state badge** (partial): TX / TX (split) /
  TX (RX1 muted) state strings live; PS-armed strings stay
  placeholder until v0.3.

**Explicit deferrals from v0.2.0:**

* **XIT** (§15.10 second half) -- renders disabled in v0.1.1,
  enables in v0.2.3 (~2 hr enable on top of RIT plumbing).
* **EQ + compressor**: v0.2.1.
* **CW + AM + FM**: v0.2.2 (CW state bits in TX I-LSB are HL2-
  specific overwrite-style packing per §3.8 L-5; AM/FM
  modulators have their own per-mode setter audits).
* **CFC** (speech processor): v0.2.2.
* **Polish + per-band EQ memory + 3-point PWR calibration UX
  + monitor/sidetone tuning**: v0.2.3.
* **ASIO host API support** (§15.12 item 4): deferred to v0.2
  end alongside TX monitor-latency story.
* **PureSignal**: v0.3 (separate release line entirely).

#### Phase progression

Modeled on v0.1's Phase 0..5 rollout pattern.  Effort is
"focused hours of design + code + test", not calendar.

| Phase | Goal | Effort | Status |
|---|---|---|---|
| **0** | Scaffolding (no behavior change) | ~30 min actual | ✅ shipped commit ``840d507`` |
| **1** | Protocol TX surface (10 sub-items) | ~25 h actual | ✅ shipped commits ``239c721`` through ``9fe0b59`` (10 commits between 2026-05-14) |
| 2 | DSP TX chain (wdsp_tx_engine.py, ~40 cffi entries, ALC + leveler + sip1 tap, mic-input path, 50 ms MOX-edge cos² fade) | ~30 h estimated | pending |
| 3 | UI integration (MOX/TUN buttons, drive slider, LED-bar TX meter layout, §15.9 red-on-air, PTT state machine wiring, hardware PTT forwarder, §15.14 auto-mute) | ~14 h estimated | pending |
| 4 | RTA scaffolding + §15.15 AAmixer badge + cal UX + help docs | ~18 h estimated | pending |
| 5 | Release ritual (CHANGELOG / version bump / tag / build / push) | ~4 h estimated | pending |

**Total estimated: ~96 h focused work** for v0.2.0 alone (Phase
0 + Phase 1 actuals ~26 h, ~70 h remaining for Phases 2-5).

#### Phase 1 deliverables (closed 2026-05-14)

All 10 Phase 1 items landed in commit order matching HPSDR P1
case-N progression where applicable:

1. ``239c721`` -- Round-robin walks ``_cc_cycle`` list (was
   ``sorted(keys)``; unblocks MOX-edge jump-to-frame-2 retune
   behaviour for future v0.2 work)
2. ``190a375`` -- Frame 11 (0x14) composer: all 4 bytes from
   state (was C4-only write trampling preamps / mic switches /
   line-in / puresignal_run / user_dig_out / step_attn)
3. ``4ca8597`` -- Frame 10 (0x12) composer + eager registration
   (drive_level / PA bias / mic boost / line-in route / BPF /
   LPF; ``pa_on`` defaults False for safety)
4. ``4f3e383`` -- Frame 4 (0x1C) composer + eager registration
   (5-bit redundant tx_step_attn write; coherent with frame 11
   C4's 6-bit + override copy)
5. ``1840fc8`` -- Frame 0 (0x00) composer: full 4-byte state
   (was 1-of-4 with C2/C3 hardcoded zeros; OC pins + atten /
   dither / antenna select / diversity all now plumbed)
6. ``aef0106`` -- Frame 18 (0x74) reset_on_disconnect safety
   register (default True; HL2 auto-reverts to RX on host link
   loss -- no silent carrier after crash mid-TX)
7. ``9afda53`` -- ``_set_tx_freq(hz)`` writes 0x02 + 0x08 +
   0x0a atomically (HL2 nddc=4 mirrors TX freq to DDC2 + DDC3
   so v0.3 PS feedback DDCs sit at TX freq under cntrl1=4
   routing)
8. ``1e73996`` -- MOX bit emission via dispatch_state.mox
   snapshot (was ``c0 & 0xFE`` strip-masking; load-bearing fix
   that unblocks all v0.2 TX operation).  Per-datagram snapshot
   so both USB blocks carry coherent MOX state.
9. ``eb5a15f`` -- EP6 telemetry decoding: ``ptt_in`` / ``dot_in``
   / ``dash_in`` / ``adc_overload`` fields on FrameStats; I2C-
   response gate (``cc[0] & 0x80``) prevents mis-decode of I2C
   readback bytes as telemetry; ADC overload uses OR-until-
   cleared host-side semantic
10. ``9fe0b59`` -- ``tx_active_changed`` Signal(bool) emitter
    fires from ``Radio.set_mox()`` on every MOX edge.  Plus
    cleanup: deleted dead ``_keepalive_cc`` (5 write sites + 2
    declarations) and ``reassert_rate_keepalive`` no-op method;
    refreshed ``stream.py:38`` docstring with v0.2 register
    surface; created ``lyra/protocol/p1_hl2.py`` stub for the
    HL2-specific forward-power formula (Phase 3 fills the cal
    coefficients per consensus §8.4(a)).

**Bench-test coverage:** items 1-7 batch + item 8 + item 9 all
hardware-validated on N8SDR's real HL2 between 2026-05-14
afternoon and evening.  HL2 telemetry confirmed updating
correctly (temp 24.7-24.8°C, voltage 12.3 V matching 12.5 V
supply with ~0.2 V cable drop -- physically sensible readings
confirm the I2C-response gate isn't spuriously firing and the
address-rotation decode still works).  No regressions in RX2 +
RIT + sample-rate-switching + auto-LNA paths.

**Wire-level behaviour for RX-only operation after Phase 1:**
byte-identical to v0.1 (every additional register cycling
defaults to HL2 fresh-power-up state values; MOX bit stays 0
because no setter wires ``Radio.set_mox(True)`` until Phase 3
UI lands).  The load-bearing structural fixes are all in place;
Phase 2 begins by wiring the WDSP TXA cffi + Mic-input
dispatcher.

#### Status

**Phase 0 + Phase 1: COMPLETE.**  12 commits on
``feature/v0.0.9.6-audio-foundation`` between v0.1.1 GA
(``0b730f2``) and current HEAD (``9fe0b59``).  Pushed to
origin; main stays at v0.1.1.

**Phase 2: IN PROGRESS 2026-05-15.**  Commit 1 (cffi expansion)
shipped ``135ccbb``; ~25 new TXA cffi entries declared + verified
against bundled ``wdsp.dll``; ``TxaMode`` + ``TxaMeterType`` enums
added.  Remaining: ``TxChannel`` class in new ``wdsp_tx_engine.py``,
mic-input dispatcher, EP2 I/Q packing, ALC + leveler init with
Thetis defaults, §8.2 sip1 ring buffer, 50 ms MOX-edge fade,
IM-5 6-setter audit, bench gate.

#### Pre-cdef audit discipline (NEW 2026-05-15)

**Consensus plan §8.5 contained THREE setter-name defects** that
the row-by-row source verification during commit ``135ccbb``
caught before any cffi could be emitted:

1. ``SetTXAALCThresh`` — does not exist in WDSP.  ALC's dynamic-
   range ceiling is governed by ``SetTXAALCMaxGain`` alone.
   Earlier plan drafts conflated TX ALC (MaxGain-only) with RX
   AGC's ``SetRXAAGCThresh`` (which DOES exist).
2. ``SetTXAPhRotRun`` / ``SetTXAPhRotFreq`` / ``SetTXAPhRotNstages``
   — actual WDSP symbols are ``SetTXAPHROTRun`` / ``SetTXAPHROTCorner``
   / ``SetTXAPHROTNstages`` (UPPERCASE PHROT).  Case-sensitive C
   symbols; case-mismatched names would fail symbol resolution
   at first call.
3. ``SetTXAInRate`` / ``SetTXAOutRate`` — do not exist; rate
   setters are shared with RX in ``channel.c``:
   ``SetInputSamplerate`` / ``SetOutputSamplerate``.

All three defects were fixed in ``v0.1_rx2_consensus_plan.md``
§8.5 commit (2026-05-15) so the doc no longer poisons future
readers.

**Discipline going forward**: before each v0.2.x sub-release adds
new WDSP setters (v0.2.1 EQ + compressor, v0.2.2 AM/FM/CW + CFC,
v0.2.3 polish + XIT), do a row-by-row audit of consensus §8.5
setter names + signatures against the WDSP source files cited
in §8.5.  The shell pattern that catches the three classes of
defect:

```sh
# Check setter exists (catches "Thresh" / "InRate" class):
grep -rn "^void <setter_name>" \
    "$THETIS_SRC/Project Files/Source/wdsp/"

# Check exact casing (catches "PhRot" vs "PHROT" class):
grep -rni "<setter_name_lowercase>" \
    "$THETIS_SRC/Project Files/Source/wdsp/" | head -3

# Check parameter signature (catches "int vs double" class --
# the v0.0.9.8.1 SetRXAAGCSlope register-class bug):
grep -A1 "^void <setter_name>" \
    "$THETIS_SRC/Project Files/Source/wdsp/<file>.c"
```

Where ``$THETIS_SRC = D:\sdrprojects\OpenHPSDR-Thetis-2.10.3.13``.

Audit cost: ~30-60 min per sub-release worth of new setters.
Catches defects at the cdef stage (cheapest fix point) instead
of dlopen / first-call / runtime-bench (each progressively
more expensive).

The pattern is: **every prior audit pass reviewed an adjacent
surface but the row-by-row consensus-vs-source check was a gap.**
Adding it explicitly fills the gap.

Reading-list anchors for Phase 2 resume (unchanged):
consensus §8.5 (TX chain byte-for-byte match to xtxa() execution
order); §8.4 (meter wiring + cal); §15.13/14/15 (cross-cutting
v0.2 deferrals); ``wdsp_engine.py`` (RxChannel pattern for
TxChannel sibling).

---

*Last updated: 2026-05-14 — **v0.2.0 Phase 0 + Phase 1
COMPLETE on feature branch.**  12 commits between v0.1.1 GA
(``0b730f2``) and current HEAD (``9fe0b59``) land the full
protocol-layer surface for v0.2 TX: all 10 HPSDR P1 C&C
register composers, ``_set_tx_freq`` writer, MOX bit emission
gate (was strip-masking pre-Phase-1), EP6 telemetry decoding
expanded with PTT-in / dot_in / dash_in / ADC-overload + I2C-
response gate, ``tx_active_changed`` Signal emitter, plus
~80 lines of dead-code cleanup (``_keepalive_cc`` deleted,
``reassert_rate_keepalive`` no-op deleted, ``stream.py:38``
register-map docstring refreshed).  Wire-level bytes for RX-
only operation: byte-identical to v0.1 (MOX stays 0 because
no Phase 3 UI yet wires the setter).  Three batched bench
tests on N8SDR's real HL2 confirmed no regressions in RX2 +
RIT + sample-rate + auto-LNA + HL2 telemetry decode paths.
Phase 2 (DSP TX chain via WDSP TXA cffi + ``TxChannel`` class
+ mic-input dispatcher + ALC + leveler + §8.2 sip1 tap) is
pending, ~30 h estimated.  See §15.18 for the full Phase
tracker.  Earlier today: **v0.1.1 "Polish & Audio Routing"
SHIPPED.**  Five-item §15.16 batch closed: RIT, TCI RX2 channel,
device-list grouping by host API, VAC digital-modes doc, WASAPI
Exclusive (audit closure — was already shipped).  Plus §15.17
stepper redesign rolled in (RIT's offset stepper reuses the
widget so §15.17 became a hard dependency; original v0.1.2 hold
retracted).  Test count: 225/225 green + 11 TCI RX2 routing
assertions + 6 RIT-math assertions + UI bench validation.  Three-
push sequence executed.  Earlier:
2026-05-14 — **v0.1.0 GA SHIPPED.**  Production
release of the v0.1 line after pre2/pre3 tester flight with
Brent + Timmy + N8SDR.  Headline: RX2 dual receiver +
stereo-split audio + focused-VFO operation + post-§15.7
audio-path latency win + GA-specific diagnostic overlay
3-state toggle + HL2 telemetry checkbox.  Three-push sequence
completed (feature branch + tag + main fast-forward — the step
the v0.0.9.6→9.9 line missed got hit this time).  GitHub
Release published with installer attached.  §15.16 v0.1.1
scope lock added: RIT + TCI RX2 + WASAPI Exclusive + VAC doc
+ host-API grouping bundled for one polish release; XIT + ASIO
explicitly stay deferred to v0.2.  Earlier:
2026-05-11 — Round 3 amendments applied (operator
chose Option A: full sweep of all 9 R3 amendments) on top of
Round 1 synthesis.  Round 3 changes: §7 v0.4 Brick scope marked
non-blocking for v0.1 Phase 0 (R3-8).  Companion v0.1 plan edits:
§8.5 TX chain rewritten to match xtxa() byte-for-byte adding
gen0/gen1 (PS bench gates 4/5 prerequisite) + two-position
preemph + per-stage meters + ALC defaults (R3-1); §3.1.x Phase 0
done-definition added with 13 verifiable items including
file-collision resolution + capability struct + spectrum mixin
+ regression null test (R3-2); §4.2.x dispatch state contract
defining DispatchState dataclass + ConsumerID enum + per-family
ddc_map function + threading model + captured-profile bypass
call site (R3-3); §3.3 M-2 SpectrumSourceMixin replacing the
non-existent base class with new lyra/ui/spectrum_common.py
push-style mixin (R3-4); §5.3 chain diagram order corrected to
APF before patchpanel matching WDSP internal order (R3-5);
§4.2 terminology aligned to xrouter source IDs + §1.1 host
channel 3 row clarification for ANAN (R3-6); §9.3.8 bench-test
infrastructure subsection adding 6 cffi diagnostic accessor
prerequisites for PS Gates 4/6/7 (R3-7); pre-commit hook one-liner
audit gate for capability-driven UI (R3-9).  See
scratch/round2_synthesis.md for the 16-gap → 9-amendment list
that drove this round.  Earlier:
2026-05-11 — Round 1 synthesis amendments applied across
§3.2 (priming vs main-loop duplex bit nuance),
§3.6 (HL2 P1 shared-rate caveat), §3.8 (CW I-LSB prose +
adc_overload semantics + HL2 PS feedback DDC routing
correction), §4.1 (port table rewrite for cffi-pivot reality),
§6.7 discipline #6 (DDC mapping rewrite for HL2/ANAN actual
dispatch), §7 v0.4 scope (ANAN nddc=5 scope correction + Brick
SDR TBD entry), §14.6 forward-compat table (captured-profile
bypass during MOX+PS state per CR-1 routing correction).
Companion changes to v0.1_rx2_consensus_plan.md (CR-1 through
CR-7 critical, IM-1 through IM-6 important, M-1 through M-11
medium, L-1/L-3/L-5/L-7/L-8/L-9 low — see plan §10 errors-
corrected table for one-line summaries with citations) and
audio_architecture.md §2.4 (8-way state machine clarification +
post-mixer operator-mute multipliers).  Earlier:
2026-05-10 — v0.0.9.8 "Display Polish" CW VFO
convention switch shipped.  VFO LED now reads the carrier of the
tuned signal in every mode (matching the standard convention used
across major HF SDR applications); central DDS offset in
``Radio._compute_dds_freq_hz`` replaces the per-call-site offsets
from v0.0.9.7.1 / v0.0.9.7.2.  v0.0.9.7.2 was committed and
tagged but skipped on GitHub release — superseded by the
convention switch.  Earlier 2026-05-10: v0.0.9.7.2 spot-pitch
fix (now reverted), Thetis spot-handling research that informed
the convention switch decision.  2026-05-09: v0.0.9.7 "Display
Polish" main release (Peak Hold combo + Decay + Clear, Exact /
100 Hz quantization, spec/wf zoom slider live-preview, spectrum
trace fill master toggle + custom color, waterfall collapse
toggle, per-band waterfall persistence, Settings dialog
hardening) and v0.0.9.7.1 NCDXF tuning fix.  2026-05-08:
v0.0.9.6 "Audio Foundation" final release + the cleanup arc
finishing up (Phase 4-9: Audio Leveler delete + agc_wdsp / apf /
demod / nb / lms / anf / squelch / nr2 deletion + state-container
dataclasses replacing Python DSP modules + AGC plumbing fixes).
2026-05-07 late night: Phase A of legacy-DSP cleanup landed +
§14.10 AM/FM/DSB right-channel-silent fix + §14.8 WDSP SSQL +
§14.7 NR-mode UX overhaul + §14.6 IQ-domain captured-profile
architectural plan.  2026-05-07: RX1 polish push (APF + BIN-
PC-Sound + dither + S-meter peak-hold + capture-feed + LMS
slider wiring + EMNR gainMethod + AEPF cffi bindings).
2026-05-06: §14 added when RX1 went live on the native engine.
2026-05-06: §13 audio architecture decision.  2026-05-02:
senior-engineering pass that produced `implementation_playbook.md`.

§15 backlog (post-v0.0.9.8):
* ~~§15.2 — RX2 plan leveler refs cleanup~~ **CLOSED Round 1
  2026-05-11** — RX2 plan §5.3 / §7.x chain diagrams updated to
  remove deleted-leveler refs; MODE_COMP signal source switched
  to `Radio.agc_gain_db` (RX) / `Radio.tx_comp_db_changed` (TX-
  side WDSP leveler meter) per the cffi engine reality.
* §15.3 — Settings dialog deeper disconnect-on-close refactor
  (noise-suppression layer landed v0.0.9.6.1 / v0.0.9.7;
  proper fix parked for v0.1)
* ~~§15.5 — ``_AGC_PROFILES`` Long re-add~~ already closed.
* **NEW Round 1 2026-05-11 — Round 2 validation gate** before
  v0.1 RX2 Phase 0 work begins.  After Round 1 synthesis
  amendments commit (this one), spin 2 fresh agents to read the
  patched plan + CLAUDE.md + audio_architecture.md.  Both must
  agree 100% the plan is solid for HL2/HL2+/ANAN P1/ANAN P2/
  Brick (pending Brick scope clarification per §7 L-6 TBD).
  Failure → loop back to a Round 3 amendment cycle.
* v0.1 RX2 Phase 0 (multi-channel refactor, no behavior change)
  — gated on Round 2 unanimous agreement.

Update this file when key decisions change.*
