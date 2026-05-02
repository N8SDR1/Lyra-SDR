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
