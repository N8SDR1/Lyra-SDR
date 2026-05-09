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

## [0.0.9.7.1] — 2026-05-09 — "Display Polish" (NCDXF tuning fix)

Bug-fix patch on top of v0.0.9.7.  No new features — same WDSP
audio path, same Display panel additions.

### Fixed

* **NCDXF beacon tuning now applies the CW pitch offset.**  When
  clicking an NCDXF triangle marker on the panadapter or engaging
  auto-follow, the VFO previously tuned to the listed carrier
  frequency exactly (e.g. 14.100.000 MHz).  Because Lyra's CWU
  filter sits offset from the VFO marker by `+cw_pitch_hz`, the
  beacon's carrier landed at the marker and fell outside the
  filter window — the operator heard zero-beat instead of a
  proper CW tone at their configured pitch.  Both paths now apply
  the same offset that `_on_click` already does for click-to-tune
  on a visible CW signal: VFO tunes to `(carrier − pitch)` for
  CWU, `(carrier + pitch)` for CWL.  Result: NCDXF beacons audibly
  identify themselves at the operator's preferred CW pitch tone,
  matching what manual click-to-tune on the same signal already
  produced.
* The fix is in two places: `_on_landmark_clicked` in
  `lyra/ui/panels.py` (covers the marker-click path AND any
  future CWU/CWL landmark category) and `_ncdxf_follow_pump` in
  `lyra/radio.py` (covers the 1-sec auto-follow timer that
  re-tunes when the followed station moves to a different band).

---

## [0.0.9.7] — 2026-05-09 — "Display Polish"

Operator-driven UX polish release on the spectrum / waterfall /
Display panel surface, plus Settings dialog stability hardening
and a documentation accuracy pass.  No DSP-engine or protocol
changes — same WDSP-cffi audio path that v0.0.9.6 introduced.

### New on the Display panel

* **Peak Hold combo** — eight modes: Off / Live / 1 / 2 / 5 / 10 /
  30 sec / Hold.  Live tracks the spectrum bin-for-bin (no
  freeze, no fade); timed modes capture max → freeze for the
  chosen window → fade at the chosen Decay rate; Hold captures
  max and never fades.  Default Live on fresh install.
* **Decay combo** — three fade speeds for the timed Hold modes:
  **Fast** (~2 s for a 60 dB peak, 30 dB/s) / **Med** (~5 s,
  12 dB/s, default) / **Slow** (~10 s, 6 dB/s).
* **Clear button** — instantly drops the held peak buffer.
  Useful in Hold mode (where peaks would otherwise stay frozen
  forever).
* **Exact / 100 Hz** quantization toggle — when enabled, all
  panadapter tuning gestures (wheel-tune, click-tune, drag-pan,
  Shift+click peak-snap) round the resulting freq to the nearest
  100 Hz grid.  Useful for SSB voice tune-around without
  needing to babysit the LED digit.  LED-readout wheel-tuning
  paths stay exact regardless.
* **Spec/WF zoom slider live-preview** — the front-panel Spec
  and WF rate sliders now update spectrum / waterfall *while you
  drag*, debounced to ~10 commits/sec.  Release just locks in
  whatever you'd already settled on — no more "drag, release,
  see if you got it right, drag again" loop.

### Spectrum trace fill — operator control

* **Master toggle** in Settings → Visuals → Signal range →
  "Fill area under spectrum trace (gradient)".  Default on.
  When off, only the trace line is drawn — useful for a cleaner
  "bare line" look or to see content behind the trace (landmark
  triangles, peak markers in Live mode, TCI spot ticks).
* **Custom fill color** in Settings → Visuals → Colors →
  **Spectrum fill** field.  Empty (default) = derive from the
  current trace color.  Pick a different color to make the fill
  stand out from the trace line itself.
* Both controls work identically on QPainter and GPU panadapter
  backends and persist across launches.

### Waterfall collapse toggle

* Small **▾** triangle button in the Waterfall panel header (next
  to the help `?` badge) collapses the waterfall content area to
  free vertical space for the spectrum view above.  Click again
  to expand back to the previous size.  State is remembered
  between sessions.

### Per-band waterfall persistence

* The waterfall min / max dB range now travels with the per-band
  bounds memory.  Switch from 40 m to 20 m and back — the
  waterfall dynamic range you'd dialed in for each band recalls
  automatically alongside the spectrum bounds you'd already had
  per-band.  No new UI; happens automatically.
* Fixes a related restart-recall regression where bands with
  drag-set ranges (but no recent freq write) were being dropped
  from band memory on restore.

### Settings dialog hardening

* **Dead-widget guards** — `_safe_mirror` helper + the
  `@_swallow_dead_widget` decorator (applied to ~24 slot
  methods) catch the "Internal C++ object already deleted"
  PySide6 crashes that could fire when a dialog was closed
  mid-mirror-update.  Bounded layer underneath: section §15.3
  in CLAUDE.md captures the deeper disconnect-on-close refactor
  that's parked for v0.1.
* **Wrapped-label squeeze fix** — multi-paragraph QLabel intros
  on the Noise tab (and others) were rendering with overlapping
  lines when the tab was first opened.  Fixed with a
  `_force_wrap_height()` helper that pins the label's vertical
  size policy, plus a structural change splitting the long
  Captured Noise Profile intro into three short labels.
* **Dialog size** bumped 1100×760 → 1280×880 to fit all tabs
  without scroll/clip on a typical Windows desktop.
* **Custom color button width** 120 → 140 px to fix Windows
  clipping the leading "C" on the "Custom color…" label.

### Documentation pass

* `docs/help/spectrum.md` — new sections for trace fill, peak
  markers (Display panel quick controls + Settings → Visuals
  appearance), waterfall collapse toggle.
* `docs/help/tuning.md` — new section for Exact / 100 Hz
  quantization.
* `docs/help/bin.md` — audio-chain diagram redrawn to match the
  WDSP-mode reality (engine handles decim → notches → NR → ANF →
  AGC → APF → bandpass → demod internally; no more legacy
  Python `tanh` stage).
* `docs/help/agc.md` — Auto profile description corrected
  (auto-threshold tracking is wired, not parked); Long profile
  removed from operator-facing docs (infrastructure exists in
  `radio.py` but isn't in the right-click menu list — see
  CLAUDE.md §15.5 to re-add).
* `docs/help/troubleshooting.md` — ANF profile names corrected
  (Gentle/Standard/Aggressive → Light/Medium/Heavy); v0.0.9.6
  captured-profile-INERT-in-WDSP-mode caveat added to the
  recipe; AGC AM-fade tip simplified.
* `docs/help/shortcuts.md` — NR right-click menu rewritten from
  pre-v0.0.9.6 "Light/Medium/Aggressive/Neural" wording to the
  current Mode 1..4 + AEPF + NPE picker; EQ removed from the
  DSP-menu jump (no operator-facing EQ exists; parametric EQ
  port is v0.2 work).
* `docs/help/introduction.md` + `support.md` — author
  attribution reconciled to match `CONTRIBUTORS.md` (N8SDR is
  project lead and sole developer through v0.0.9.x; N9BC joined
  as co-contributor during v0.0.9.1 testing; joint development
  begins at v0.1).

### Internal architecture / refactor notes

* CLAUDE.md §15 — new Documentation backlog section captures the
  internal-doc cleanup items the audit surfaced but didn't fix
  in this release (CLAUDE.md current-version line, §14.2 wired /
  inert lists update, RX2-plan leveler references after Audio
  Leveler deletion, Settings dialog disconnect-on-close deeper
  fix, `_AGC_PROFILES` Long re-add).

---

## [0.0.9.6] — 2026-05-08 — "Audio Foundation"

The biggest single release Lyra has shipped to date.  Headline
change is a wholesale pivot from pure-Python DSP to **cffi calls
into the WDSP DSP engine** for the RX1 audio chain — driven by
extended audio-quality troubleshooting on the pure-Python port
path (per-sample numpy work in agc / nr / nr2 / anf / demod /
channel was producing GIL contention with the EP2 writer thread,
manifesting as HL2 audio-jack clicks and PC Soundcard
motorboating).  WDSP's DLLs are bundled at `lyra/dsp/_native/` —
no external installs, no operator-side dependencies.

Around that core change: a six-week cleanup arc retiring ~6,800
lines of legacy DSP code, a series of operator-driven UX
improvements, and a final-day push that landed Propagation /
NCDXF / clock-sync / per-edge auto-scale locks.

### RX1 audio chain — native engine via cffi

* **`lyra/dsp/wdsp_native.py`** + **`lyra/dsp/wdsp_engine.py`** —
  cdef declarations + high-level Python wrapper.  Bundled
  binaries: `wdsp.dll`, `libfftw3-3.dll`, `libfftw3f-3.dll`,
  `rnnoise.dll`, `specbleach.dll` (~16 MB total) at
  `lyra/dsp/_native/`.
* **RX1 audio path now end-to-end through WDSP**: decimator,
  bandpass, demod (USB/LSB/AM/FM/CWU/CWL/DSB/SAM/DIGU/DIGL/DRM/
  SPEC), AGC, EMNR (NR), ANR (NR), ANF, LMS, AM/FM squelch,
  SSQL all-mode squelch, APF (CW peaking).
* AGC gain readout wired (GetRXAMeter / RXA_AGC_GAIN, throttled
  to ~6 Hz) so the panel meter actually moves.
* CW pitch refilters when active mode is CWU / CWL.

### NR-mode UX overhaul (operator-driven)

The legacy NR1/NR2 backend dropdown + dual strength sliders were
confusing in WDSP mode (sliders mostly inert; backend NR1/NR2
sounded similar even though we set different gain methods).
Replaced with a Thetis-inspired but Lyra-tuned model on the
DSP+Audio panel:

* **NR slider** → 4-position MODE selector (1 / 2 / 3 / 4)
  mapping to WDSP gain methods 0..3 (Wiener+SPP / Wiener simple
  / **MMSE-LSA default** / Trained adaptive)
* **AEPF checkbox** → anti-musical-noise post-filter (clear
  audible difference; previously hidden in Thetis)
* **NPE dropdown** (OSMS / MCRA / etc.) — new operator-tunable
  control on the DSP+Audio panel.  Lyra exposes more WDSP NR
  knobs for direct on-air tuning than Thetis / SparkSDR /
  PowerSDR.

### SSQL all-mode squelch

Mode-routed squelch:
* FM → `SetRXAFMSQRun` + Threshold (logarithmic slider mapping)
* AM/SAM/DSB → `SetRXAAMSQRun` + Threshold + MaxTail (0.5 sec)
* SSB/CW/DIG/SPEC → **NEW** `SetRXASSQLRun` (WDSP's Single-mode
  Squelch Level — operator slider scaled 0.65× to put the
  comfortable zone just below WU2O's tested-good 0.16 default;
  TauMute 0.7 sec / TauUnMute 0.1 sec)
* Squelch master-off bug fixed (AM/FM gates were stuck on when
  SQ was toggled off — fixed in Phase 6.A4 fix-up)

### AM/FM/DSB right-channel-silent — fixed

WDSP's EMNR explicitly zeroes the Q channel on output.  For SSB,
the asymmetric BP1 passband acts as a Hilbert restorer and stereo
content survives.  For AM/FM/DSB, the symmetric passband leaves
Q at zero — so the patch panel's default `copy=0` produced silence
on the right channel.  Fixed by calling `SetRXAPanelBinaural(0)`
at channel init (Thetis does this; Lyra had been inheriting the
WDSP create-time default).  Operator-verified across all modes
with EMNR active: L_rms == R_rms within 0.001 on every mode.

### TPDF dither + audio polish

* **TPDF dither** added to the float→int16 quantization in
  `_quantize_to_int16_be` — operator-confirmed harshness gone.
* **S-meter peak-hold smoothing** with fast-attack /
  slow-release (~500 ms decay) — eliminates twitch on weak
  signals.
* **PC Soundcard mode** now CPU-comparable to HL2-jack mode via
  cffi-wrapped `WdspRMatch` (in-tree `lyra/dsp/rmatch.py`
  formerly pure-Python is the fallback).
* **Noise blanker** wired (`xnobEXT` / `xanbEXT` actually splice
  into the IQ path now; previously `SetEXTNOBRun(1)` alone was
  a no-op since the EXT-blanker objects weren't created).
* **Manual notches** wired through WDSP NotchDB.
* **APF** (CW peaking) via WDSP biquad — operator-confirmed
  +12 dB measured at +12.2 dB.
* **BIN (binaural)** Python post-processor on top of WDSP's
  stereo output; works in both HL2-jack and PC Soundcard paths.
* **Audio Leveler** retired — WDSP AGC subsumes it.

### AGC plumbing fixes (operator-reported, all in Phase 6.A)

1. **AF Gain inert in live audio** — wired
   `set_af_gain_db` → `_wdsp_rx.set_panel_gain(af_gain_linear)`.
2. **AGC Settings sliders didn't follow profile changes** —
   `set_agc_profile` now reads the preset table to update
   advisory `_agc_release` / `_agc_hang_blocks`.
3. **AGC threshold push missing** — `_open_wdsp_rx` now wires
   `set_agc_slope(0)` + `set_agc_threshold` at init.
4. **FM SQ slider had no effect** — added logarithmic mapping
   `10^(-2·v)`.
5. **ANF μ slider was advisory-only** — added `SetRXAANFVals`
   binding + push.
6. **AM SQ tail too long** — pushed 0.5 sec at init.

### Cleanup arc (Phases 3–9)

Retired ~6,800 lines of legacy pure-Python DSP code now that
WDSP is the single audio engine:

| Module deleted | Lines | Replaced by |
|---|---|---|
| `lyra/dsp/leveler.py` | 355 | feature retired (WDSP AGC subsumes) |
| `lyra/dsp/agc_wdsp.py` | 746 | WDSP cffi `SetRXAAGCMode` directly |
| `lyra/dsp/apf.py` | 251 | WDSP SPEAK biquad |
| `lyra/dsp/demod.py` | 528 | WDSP RXA chain |
| `lyra/dsp/nb.py` | 477 | `_NBState` dataclass + WDSP NOB |
| `lyra/dsp/lms.py` | 459 | `_LMSState` dataclass + WDSP ANR |
| `lyra/dsp/anf.py` | 395 | `_ANFState` dataclass + WDSP ANF |
| `lyra/dsp/squelch.py` | 419 | `_SquelchState` dataclass + WDSP SSQL/FMSQ/AMSQ |
| `lyra/dsp/nr2.py` | 1496 | `_NR2State` dataclass + WDSP EMNR |
| `LYRA_USE_LEGACY_DSP=1` env-var fallback | ~57 | gone — WDSP is the only path |

The `DspChannel` ABC is kept for forward compatibility (a future
DSP backend could subclass it) but its `process()` abstractmethod
is gone — channels are state containers now, not DSP drivers.

### Operator-driven UX additions (final-day push)

* **Panadapter wheel-tune** — mouse wheel over empty spectrum now
  tunes the VFO (wheel up = freq up).  Ctrl+wheel keeps the legacy
  zoom gesture for power users.  **Panafall Step** combo on the
  Display panel picks the per-tick step (100 Hz / 500 Hz / 1 kHz /
  5 kHz / 10 kHz / 25 kHz / 100 kHz).
* **Propagation panel** (View → Propagation) — slim status strip
  with live solar numbers (SFI / A / K, color-coded), 10-band
  HamQSL conditions heatmap (Day/Night-aware via QTH grid square),
  and an NCDXF Beacon Auto-Follow dropdown.  Pick one of 18
  worldwide stations and Lyra auto-tunes through its 5-band
  rotation (20m → 17m → 15m → 12m → 10m every 10 sec) — an
  SDR-only superpower a knob radio can't match.
* **NCDXF spectrum markers** — cyan triangles at the 5 NCDXF
  frequencies (14.100 / 18.110 / 21.150 / 24.930 / 28.200 MHz).
  Hover for the live callsign of whichever of the 18 stations is
  on that band right now.  Independent show/hide toggle separate
  from the digimode landmarks (FT8/FT4/WSPR/PSK).  New
  **Settings → Propagation** tab + sibling checkbox under Bands.
* **Clock drift / sync** — right-click either toolbar clock (Local
  or UTC) to:
    * Check drift against an NTP server (Cloudflare / NTP Pool /
      Google / Microsoft — first that answers wins, raw UDP/123)
    * Sync time on Windows via `w32tm /resync`
    * Read why this matters (NCDXF beacon Follow accuracy
      depends on it — 10-sec slots → drift > 3 sec mis-IDs the
      station)
  ⚠ prefix appears on the UTC clock if the last drift check came
  back warn/bad.
* **dB-scale per-edge auto-scale locks** — the long-standing
  "I dragged the floor and it climbed back" complaint, fixed:
    * **Drag the FLOOR** → auto stops moving it.  Hard lock.
    * **Drag the CEILING** → auto won't fall below it but still
      RISES if a strong signal arrives.  Soft lock.
    * **Drag pan (middle)** → both edges shift + lock together.
  Locks are saved per-band so switching bands restores them.
  Right-click the dB scale → **Reset display range** to clear
  (menu shows which edges are locked).

### Bug fixes (UI + cosmetic)

* **Toolbar clock right-click crash** — dynamic class-with-Signal
  pattern crashed PySide6's meta-object compiler on second
  invocation.  Fixed by hoisting workers to module scope +
  layered try/except.
* **Tooltip cascade** — bare-stylesheet rules on QLabel widgets
  (`color: ...; font-size: 22px;` with no selector) cascaded to
  the QToolTip popup spawned by the same widget, making tooltips
  render at the label's huge bold styling.  Fixed on the toolbar
  clocks + AGC labels by wrapping all rules in `QLabel { ... }`
  selectors.

### Help docs / Settings tooltip refresh

* `docs/help/spectrum.md` — rewrote the auto-scale section to
  match the actual per-edge-lock behavior (the old text promised
  "drag = bounds for auto" which a prior pinch-bug fix had
  silently broken).
* `docs/help/propagation.md` — new file covering the panel,
  NCDXF rotation math, clock-accuracy caveat, NTP drift check.
* `docs/help/index.md` — added Propagation entry to the topic
  index.
* Settings tab tooltips updated (auto-scale, NR mode, AEPF, NPE,
  NCDXF marker, propagation tab) — all match current code.

### Tags + portable bundles

| Tag | What it covers |
|---|---|
| `v0.0.9.6-rx1-working-r3` | Pre-cleanup baseline |
| `v0.0.9.6-rx1-working-r4` | + AM right-channel-silent fix |
| `v0.0.9.6-rx1-working-r5` | + Audio Leveler delete |
| `v0.0.9.6-rx1-working-r6` | + channel.py slim |
| `v0.0.9.6-rx1-working-r7` | + AF Gain fix |
| `v0.0.9.6-rx1-working-r8` | + AGC plumbing |
| `v0.0.9.6-rx1-working-r9` | Cleanup arc COMPLETE |

Each has a matching `_backups/lyra-2026-05-NN-rx1-working-rN.bundle`
for portable archaeology.

### Known issues / deferred

* **Captured-profile apply path is INERT in WDSP mode.**  Capture
  works, profiles save / load / persist; toggling "use captured
  profile" fires a status-bar warning.  Three rounds of fixes
  (post-WDSP nr2 pass + temporal smoothing + auto-VAD) couldn't
  eliminate audible artifacts because of WDSP-AGC-vs-static-
  reference mismatch.  Architectural rebuild planned for next
  release: tap IQ pre-WDSP, apply spectral subtraction in IQ
  domain, hand cleaned IQ to WDSP.  Mode-independent + cleaner
  math.  Captured profiles in legacy mode (`LYRA_USE_LEGACY_DSP=1`)
  are unaffected — but legacy mode is gone in v0.0.9.6, so this
  feature is effectively shelved until the IQ-domain rebuild.

### Coming next (v0.1)

* **RX2** — second receiver, stereo split via EP2 LR bytes through
  the AK4951 codec.  Foundation already laid: `lyra/dsp/mix.py`
  ports WDSP `aamix.c`, the audio mixer plumbing is in tree but
  not yet driven by a second WDSP channel.

---

## [0.0.9.5] — 2026-05-05 — "Captured-Profile UX"

A focused UX polish release for the captured-noise-profile feature.
No DSP-path changes; no protocol-path changes.  Headline change
is the **removal of the smart-guard** capture-quality check after
operator field testing showed it gave both false positives and
false negatives that calibration tuning couldn't bridge.  Plus
two operator-visible improvements (tunable staleness threshold,
live drift readout) and one stability fix (TCI server lambda
crash race).

### Smart-guard removed

The "smart-guard" capture-quality check (per-frame total-power
CV + per-bin variance anomaly detection — added in v0.0.7.x and
recalibrated several times since) has been **fully removed** in
v0.0.9.5 after operator field testing showed the algorithm gave
both:

- **False positives**: 12 consecutive captures flagged as suspect
  across different parts of the band on a real ham QTH (clean
  noise in every case, verified by ear)
- **False negatives**: FT8 captures passing as "clean" when the
  signal was clearly contaminating the capture window

Calibration tuning couldn't bridge both failure modes — the
underlying detector model didn't separate "real signal
contamination" from "real-world noise with legitimate amplitude
modulation" (powerline arcing envelope at 120 Hz US / 100 Hz EU,
BCB carrier modulation, atmospheric crashes, HF propagation
breathing).  An unreliable algorithmic check is worse than no
check because it produces false confidence on captures that
pass.

**The operator's ear + waterfall during the capture window are
the actual filter.**  Operators already do this naturally — they
listen during the 2-second capture and don't save the profile
if they heard a signal go by.  The algorithm was duplicating
their judgment poorly.

What's gone:

- ``smart_guard_verdict()`` / ``smart_guard_reason()`` APIs
  (NR1, Channel, Radio)
- ``_evaluate_capture_quality()`` algorithm and supporting
  ``GUARD_*`` thresholds
- Per-frame power tracking + sum-of-squares accumulator in
  ``_accumulate_capture_frame``
- Three-way Save anyway / Recapture / Cancel suspect-save
  dialog (added briefly earlier in the v0.0.9.5 dev branch
  before removal)
- "Detect signal during capture (smart-guard)" Settings
  checkbox
- ``noise_capture_done`` signal payload still ``Signal(str)``
  for slot-signature compatibility but always emits empty string

What stays: the basic capture → name → save flow, exactly as it
was before the smart-guard was added.

### Operator-tunable staleness threshold

The captured-profile staleness check (which fires the "profile
drifted X dB" status-bar toast) was previously hardcoded at
10 dB.  Now operator-tunable via **Settings → Noise → Profile
staleness**, range 3-25 dB.

- **Tighten to 5-7 dB** on a very stable QTH where the noise
  floor doesn't move much; gets you earlier warning when
  conditions shift
- **Loosen to 15-20 dB** if the default fires too readily on
  natural band drift over the day

Rearm threshold (when the toast can fire again after drift drops
back) automatically tracks at 70% of the fire threshold — single
operator knob.  Persists to QSettings; autoloaded on startup.

### Live drift readout in profile manager

The profile manager dialog now shows a live drift indicator at
the top of the window, refreshed once per second:

  > Live drift: +6.2 dB  (threshold 10 dB — tracking normally)

Color-graded:

- Green when well under threshold (< 50%)
- Light grey while tracking normally (50-85%)
- Amber as it approaches (85-100%)
- Red when over threshold

Diagnostic only — operator decides whether to recapture.  Idle
text "No captured profile loaded — drift readout idle" when no
profile is active.

### What this release does NOT change

- DSP path unchanged (NR1, NR2, AGC, APF, LMS, ANF, NB, SQ all
  carry forward from v0.0.9.4)
- HL2 protocol path unchanged
- All UX queue items already done in earlier work (capture freq
  in profile names, "Save anyway" dialog wording, source badge
  hover affordance) verified correct — no changes needed

### Tester checklist

- **Capture flow:** capture a profile in any band; verify it
  goes straight to the "Capture complete. Save as: [name]"
  prompt with no warnings or three-button dialogs (smart-guard
  is gone)
- **Staleness threshold:** open Settings → Noise; verify the
  Profile staleness spinbox shows 10 dB by default and persists
  across launches; smart-guard checkbox should NOT be present
- **Drift readout:** load a captured profile, open the profile
  manager, verify the live drift line at top updates every
  ~1 sec
- **TCI stability:** with TCI clients connected, open Settings,
  switch tabs, close Settings — should not produce "wrapped C++
  object" tracebacks in the console

### Recovery

If anything regresses, install [Lyra-Setup-0.0.9.4.exe](https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.4).
Operator settings carry forward unchanged.

---

## [0.0.9.4] — 2026-05-05 — "Polish & Notifications"

A focused polish + bug-fix release on top of v0.0.9.3.  No new
operator features in the radio path; targets visible cosmetic
defects, dialog stability, and the easy-to-miss update-
notification flow that operators reported missing in v0.0.9.x.
Folds in the "v0.1.0-pre1 polish items" tagged in the consensus
plan §3.1 since they're operator-visible and the rebuild was
already happening.

### Lyra constellation watermark — bundled in installer

- **Fix:** the Lyra/lyre constellation watermark image
  (`lyra/assets/watermarks/lyra-watermark.jpg`) was missing from
  the v0.0.9.3 PyInstaller bundle — the spec's ``datas`` list
  shipped icons and shaders but not the watermark folder.
  Operators running from the release installer saw meteors but
  no constellation; operators running from the source tree saw
  both, which masked the build-only nature of the bug.
- **What changed:** added the watermarks folder to
  ``build/lyra.spec`` so the JPG ships with the .exe.  No code
  changes; rerunning ``build/build.cmd`` produces an installer
  that includes it.

### First-time-per-version update modal

- **What changed:** when Lyra's silent startup update check
  detects a NEW release tag for the first time, it now opens a
  modal dialog with the release notes inline and three buttons:
  - **Open release page** — launches GitHub release in browser
  - **Remind me later** — closes modal; toolbar indicator + toast
    still appear on this and future launches
  - **Skip this version** — fully silences notifications for
    this exact tag (toolbar indicator + toast suppressed too)
- **Why:** the v0.0.8.1 / v0.0.9.x non-modal flow (status-bar
  toast + toolbar indicator + Help-menu badge) was easy to miss
  if the operator stepped away during startup or wasn't looking
  at the toolbar.  Three operators reported missing prior
  releases entirely and re-downloading manually weeks later.
  The modal is shown ONCE per new tag — subsequent launches
  with the same un-skipped tag fall back to the existing
  non-modal flow, so this isn't nagware.
- **State persistence:** uses two QSettings keys —
  ``update_check/skipped_versions`` (existing, full-silence
  list) and ``update_check/modal_seen_versions`` (new,
  modal-shown-once list).  Both survive across launches.

### Toolbar update indicator: 5-second pulse on first appearance

- **What changed:** the orange "🆕 v0.X.Y available" indicator
  in the toolbar (between the clocks and HL2 telemetry block)
  now pulses for ~5 seconds the first time it appears in a
  session — 5 cycles of opacity fade (1.0 → 0.4 → 1.0) at 1 Hz.
  Cache-replay paths on subsequent launches don't re-pulse.
- **Why:** static indicator was easy to miss against the
  toolbar's busy layout.  Brief animated entrance draws the eye
  without becoming permanent visual noise.
- **Implementation:** ``QGraphicsOpacityEffect`` +
  ``QPropertyAnimation`` — Qt-native, auto-cleans on finished,
  no QTimer juggling.

### Console log on silent update check finding new release

- **What changed:** when ``SilentUpdateChecker`` detects a newer
  release in the background, it now prints one line to stdout:
  ``Lyra: silent update check found newer release v0.X.Y
  (running v0.0.9.4)``.  Useful for diagnostic A/B without the
  toolbar UI in view.
- **Side effect:** the ``update_available`` signal signature
  expanded from ``(tag, url)`` to ``(tag, url, body)`` so the
  modal can render release notes inline.  The cache-replay
  path passes empty string for body (cached state doesn't
  store body — modal still renders gracefully with a "no notes
  attached" message).

### Settings dialog lambda crash fixes

- **Fix:** two "wrapped C/C++ object of type X has been
  deleted" RuntimeError crashes that both Brent and Rick hit
  when closing the Settings dialog while the radio was
  streaming.  Race condition: signals fire during/after the
  dialog's C++ side is being torn down → connected lambdas
  reach for a zombie wrapper → exception propagates up
  through Radio's signal infrastructure.
- **What changed:**
  - Inline lambda on ``radio.apf_enabled_changed`` →
    ``_on_radio_apf_enabled_changed`` named method with
    ``try/except RuntimeError`` guard.
  - ``_refresh_agc_action_label`` and ``_on_action_db`` body
    wrapped in ``try/except RuntimeError`` (called from
    ``stream_state_changed`` and ``agc_profile_changed`` lambdas
    that suffer the same race).
- **Why ``try/except`` not signal-disconnect-on-close:** the
  defensive guard catches the rare race in any path; explicit
  disconnect would require giving each lambda a name and
  threading disconnect logic through dialog teardown — bigger
  surface area for negligible additional safety.

### Help guide

- New §8 "Staying current — update notifications" in
  ``docs/help/getting-started.md`` explains the modal flow,
  toolbar indicator, and skip/remind semantics.  Existing
  Backups & snapshots renumbered to §9.

### What this release does NOT change

- HL2 protocol path — wire format, frame layout, codec rate
  all unchanged.
- DSP path — AGC, NR1, NR2, APF, LMS, ANF, NB, SQ all
  unchanged from v0.0.9.3.
- Settings persistence — operator preferences carry forward
  unchanged.

### Tester checklist

- Visual: launch installer, confirm constellation watermark
  renders behind the panadapter (both meteors and lyre image
  visible).
- Update flow: with a v0.0.9.3 install + this build available
  on GitHub, confirm the modal pops on first launch, indicator
  pulses, "Skip this version" hides indicator on next launch,
  "Remind me later" keeps it visible.
- Settings stability: open Settings, change AGC profile or
  toggle APF a few times, close dialog, repeat — should not
  produce "wrapped C++ object" tracebacks.
- Console diag: run from a console window
  (``python -m lyra.ui.app``) and confirm the silent-check
  log line appears when an update is available.

### Recovery

If anything regresses, install ``Lyra-Setup-0.0.9.3.exe`` from
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.3>.
Operator data is preserved across the downgrade.

---

## [0.0.9.3] — 2026-05-05 — "WDSP AGC"

The audio-quality follow-up to v0.0.9.2's host-side cadence
rebuild.  v0.0.9.2 fixed the EP2 cadence + band-change issues at
the protocol level; v0.0.9.3 fixes the AGC + APF audio-chain
issues operators interact with directly.  Operator-flight-tested
across CW (zero-beat tone tracking), SSB ragchews, and AM
broadcast on Hermes Lite 2+.

### WDSP AGC engine — full port + swap

- **Legacy single-state peak tracker is retired.**  Lyra's AGC is
  now a Python port of Warren Pratt NR0V's WDSP **wcpAGC** —
  the same look-ahead, state-machine, soft-knee design used by
  every serious openHPSDR-class SDR client (Thetis, PowerSDR
  mRX-PS).  Lives at ``lyra/dsp/agc_wdsp.py`` with full GPL
  v2+ → GPL v3+ attribution chain documented per
  ``docs/architecture/wdsp_integration.md``.

- **Architectural changes operators will hear:**
  - **Look-ahead ring buffer** (4 ms at default attack tau)
    delays output so attack ramps complete BEFORE a loud sample
    reaches the speaker.  No more transient distortion or
    post-impulse audio mute on lightning crashes / impulse
    interference.
  - **5-state machine** (NORMAL / FAST_DECAY / HANG / DECAY /
    HANG_DECAY) separates the gain regimes the legacy single-
    state tracker conflated.  Pop-detection (state 1) gives
    fast recovery after transients without making the operator-
    facing decay constant overly aggressive.
  - **Soft-knee log-domain compression curve** replaces the
    legacy hard-threshold ``gain = target / peak``.  Smooth
    around the threshold — no AGC pumping on signals riding
    the knee (typical SSB voice envelopes, slow-QSB DX).
  - **Hang threshold gating** means hang state engages only on
    real signals above background.  Noise alone never triggers
    hang — the noise floor stays smooth.  This eliminates the
    "scratchy / dirty record player" texture some operators
    diagnosed in v0.0.9.x.

- **Two prior surgical-fix attempts on the legacy engine had
  failed** for documented mathematical reasons (see
  ``docs/architecture/audio_rebuild_v0.1.md`` §10.1, attempts 5
  and 6).  Lesson: the legacy engine had hidden invariants between
  PEAK_FLOOR / noise tracking / attack rate that emerged from
  interaction; any modification broke one of them and surfaced as
  a different audible regression.  Surgical patching was
  structurally inadequate.  WDSP's invariants are explicit by
  construction — the port resolves all three failure modes at
  once.

- **Operator-facing API unchanged.**  AGC profile names
  (Off / Fast / Med / Slow / Auto / Custom) and the right-click
  menu work identically; time constants come straight from
  Pratt's SetRXAAGCMode reference (Fast=50ms decay, Med=250ms,
  Slow=500ms with 1s hang).  The "Auto" profile's noise-floor
  threshold tracking is currently a no-op (it behaves like
  Medium); will return as a Settings-controlled WDSP hang_thresh
  slider in a future release.

- **Custom profile** is also currently a UI-state holdover —
  Release / Hang sliders persist in QSettings but don't reach
  the WDSP engine for now.  Future Settings panel will expose
  WDSP's canonical operator knobs (Attack ms / Decay ms / Hang ms
  / Hang threshold) which gives Custom its full range back.

### APF moved post-AGC, default bandwidth widened

- **APF call-site moved.**  Previously APF ran inside Channel
  (post-NR but pre-AGC), so the +18 dB tone boost was applied
  first and then AGC compensated by reducing gain to keep the
  output at target.  Operators perceived only a subtle SNR
  improvement — overall loudness was unchanged.

- **New chain:** ``demod → LMS → ANF → SQ → NR → AF → AGC → Vol →
  APF → leveler → tanh``.  Post-AGC placement gives the operator-
  facing boost matching expectation: APF on = literally louder
  CW tone, leveler + tanh catch any excursion above headroom.
  Matches how PowerSDR / modern Thetis place the equivalent CW
  peaking filter.

- **Default bandwidth bumped 80 → 100 Hz** (Q ≈ 6.5 at 650 Hz
  pitch).  At the legacy default of 80 Hz / Q ≈ 7.5, the boost
  band missed the signal if the operator was even ±50 Hz off-
  zero-beat.  100 Hz is forgiving enough that the boost lands on
  the signal even with a few dozen Hz of mistuning.  Operators
  who want razor-sharp filtering can manually drop to 30-60 Hz;
  operators on messy bands can widen up to 200 Hz.  Range and
  right-click presets unchanged.

- **APF object still lives on Channel** (so its center frequency
  continues to follow the operator's CW pitch automatically); only
  the ``.process()`` call site moved to ``Radio._apply_agc_and_volume``.
  Wired into both the AGC-ON and AGC-OFF paths so operators
  running CW with AGC off get consistent APF behavior.

### Audio-chain visibility diagnostic

- **SoundDeviceSink logs device + host API + negotiated sample
  rate** at PC Soundcard sink open.  When operators report ring-
  buffer overruns on PC Soundcard mode, this line tells us whether
  Windows is doing shared-mode resampling silently, whether
  PortAudio picked the wrong default device (Bluetooth, virtual
  cable), or whether the device genuinely supports 48 kHz.

  ```
  [Lyra audio] SoundDeviceSink: device=[Speakers (Realtek)]
    host=Windows WASAPI requested_rate=48000 actual_rate=48000
    latency=21.3ms channels=2
  ```

  If actual_rate ≠ requested_rate, a warning prints below it
  pointing at WASAPI shared-mode resampling as the likely cause.

### What this release does NOT change

- HL2 codec audio output rate stays 48 kHz (hardware-fixed).
- HPSDR Protocol 1 wire format unchanged.
- Operator's QSettings (memory bank, captured noise profiles,
  band memory, etc.) carry forward unchanged.
- AGC / APF profile presets persist across the upgrade.

### Tester checklist

- Run for 30+ minutes on RX across CW, SSB, AM modes.
- AGC: switch Fast / Med / Slow / Long profiles on real signals.
  Expect smoother noise floor on Slow than v0.0.9.2; expect fast
  recovery (≤25 ms) after lightning crashes / impulse hits.
- APF (CW only): toggle on/off on a known weak CW signal; the
  on-state should produce a noticeably louder tone than off,
  not just an SNR improvement.
- PC Soundcard mode: watch the console for the device-info
  diagnostic line; report it if the actual rate ≠ requested.

### Recovery

If anything regresses, install ``Lyra-Setup-0.0.9.2.exe`` from
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.2>.
Operator data is preserved across the downgrade.

---

## [0.0.9.2] — 2026-05-04 — "Audio Rebuild"

A focused bug-fix release that rewrites the host → radio audio
cadence on the EP2 path and resolves several long-standing
symptoms: the universal click-pop, the deque-saturation
"wobbling" audio, and band-change display freezes.  Operator
flight-tested across 96 k / 192 k / 384 k IQ rates plus AK4951
codec output and PC Soundcard output.

### Audio cadence rewrite

- **Producer-paced EP2 cadence.**  The host-side EP2 frame
  writer is now driven by the codec's actual sample rate (via a
  counting semaphore released once per 126 audio samples
  produced) instead of a host-clock timer.  The writer's
  wake-up cadence is locked to the DSP audio output rate, which
  is locked to the EP6 input rate, which is locked to the HL2's
  own codec crystal — eliminating PC-vs-codec clock drift and
  the "deque slowly saturates over minutes" symptom that drove
  most pre-fix click/pop reports.

- **Heartbeat / keepalive split.**  The writer's `acquire()`
  timeout is now a dual-mode fallback:
  * Recent send (<50 ms gap): skip the iteration to avoid
    inserting silent C&C-only frames between real audio frames
    (each silent frame was one click on the AK4951 codec).
  * Long gap (≥50 ms): emit a C&C-only keepalive frame.
    Required because the HL2 expects continuous EP2 traffic;
    going silent during a long DSP reset (band change,
    mode change, sink swap) caused the radio to halt EP6
    streaming.

- **Stop register writes from stealing audio.**  The legacy
  immediate-emit path for register writes (`_send_cc`) was
  draining 126 audio samples from the EP2 queue per call
  without coordinating with the writer thread.  Auto-LNA
  pull-ups firing 1-2× per second under normal band-noise
  variation were the dominant pop source pre-fix.  Three
  runtime setters (`_set_rx1_freq`, `set_sample_rate`,
  `set_lna_gain_db`) now update the round-robin C&C register
  table only; `_send_cc` itself is inject-aware and skips the
  immediate UDP emit when audio is flowing.

### Band-change fixes

- **Stop band changes from secretly dropping the radio to 48 k
  IQ.**  `Radio._config_c1` was initialized from the
  constructor default sample rate and never updated by
  `set_rate`.  Any band change with the filter board enabled
  recomposed register 0x00 with this stale 48 k rate code,
  which the round-robin then propagated to the HL2.  The radio
  silently dropped to 48 k while the rate selector still
  showed the operator's choice, manifesting as a 4× DSP
  throttle, narrow panadapter span, and audio drag.  Fix:
  read `self._rate` fresh in `_send_full_config`, and keep
  `_config_c1` synced in `set_rate` for defense-in-depth.

### 48 k IQ rate dropped from operator-selectable options

At 48 k each DSP block produces a full ~43 ms of audio per
producer call (1:1 IQ-to-audio mapping, no decimation), causing
16 EP2 frames to burst out in <1 ms followed by a 42 ms gap.
The HL2 gateware FIFO can't absorb that pattern cleanly —
audible clicks/pops and occasional volume bursts.  Higher rates
produce smaller bursts (8 frames at 96 k, 4 at 192 k) that the
FIFO tolerates.  Rather than maintain a known-bad option, 48 k
is removed from `SAMPLE_RATES` and the rate combo.  The HL2
codec's 48 kHz **audio** output rate is unchanged (different
concept — codec output is hardware-fixed at 48 kHz regardless
of IQ rate).  Operators who had 48 k saved in QSettings get
auto-migrated to 96 k on first launch with a one-line console
notice.  96 k is the new minimum; 192 k is the recommended
mode; 384 k stays available for operators with the CPU
headroom.

### Tester checklist

- Run for 30+ minutes on RX at 96 k, 192 k, and 384 k.
- Watch the status bar — `un` and `ov` counters should stay at
  0 in steady state.  A small bump on sink swap (Out: AK4951
  ↔ PC Soundcard) is normal and harmless.
- Switch bands repeatedly — display should stay live, no
  4× throttle, no need to Stop/Start to recover.
- Confirm rate combo offers `96 k`, `192 k`, `384 k` only.
- If you previously had 48 k saved, watch the launch console
  for the migration message.
- Audio output should be audibly cleaner than v0.0.9.1,
  particularly during Auto-LNA pull-up events.

### Recovery

If anything goes wrong, install `Lyra-Setup-0.0.9.1.exe` from
<https://github.com/N8SDR1/Lyra-SDR/releases/tag/v0.0.9.1>.
Operator data (settings, memory bank, noise profiles) is
preserved across the downgrade.  The 48 k rate option will
return on downgrade (if you actually need it for some reason);
this is fine.

---

## [0.0.9.2-pre1] — 2026-05-04 — "Audio Architecture Rebuild — Commit 1"

**Pre-release.**  Superseded by the full v0.0.9.2 release above.
Originally the first of six planned pre-releases that
incrementally rebuilt Lyra's audio production architecture per
the senior-engineering audit in
`docs/architecture/audio_rebuild_v0.1.md`.  In practice the
full rebuild landed in the single v0.0.9.2 release rather than
the planned six pre-release sequence.  The pre1 changes (DSP
worker thread default, Settings → DSP → Threading combo,
QSettings ordering fix, audio telemetry status bar indicator)
all carried forward into v0.0.9.2.

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
