# Lyra — Qt6 SDR Transceiver for Hermes Lite 2 / 2+

**Current version: 0.0.7 — "Polish Pass"**

Modern PySide6 desktop SDR for Steve Haynal's Hermes Lite 2 and HL2+.
Native Python HPSDR Protocol 1, TCI v1.9 server, glassy UI with
analog-look meters, a band-plan overlay with landmark click-to-tune,
GPU-accelerated panadapter + waterfall, a CW-focused audio toolkit
(APF audio peaking filter + BIN binaural pseudo-stereo), and a deep
noise-toolkit drawing on Warren Pratt's WDSP — adaptive line
enhancer, Martin-statistics MMSE-LSA noise reduction, all-mode
squelch.  Built-in weather alerts watch the operator's local
conditions across multiple data sources (Blitzortung, NWS, Ambient,
Ecowitt) and raise toolbar + toast notifications for lightning and
high-wind events.

![Lyra](assets/logo/Lyra-SDR.png)

## Status

Pre-alpha — RX is functional; TX is in progress. Developed and tested
against a Hermes Lite 2+ board.

The version string above is the single source of truth maintained in
`lyra/__init__.py` and surfaces in:

- The window title bar
- The Help → About Lyra dialog
- A permanent label on the right side of the status bar
- The User Guide's About section (rendered live from package metadata)

Bumping the version is a one-line edit in `lyra/__init__.py`; every
display surface follows automatically.

## What's in 0.0.7 — "Polish Pass"

A focused tester-feedback release.  No new DSP or radio features — every
change is an operator-visible UI fix from feedback on the v0.0.6 install.

### UI polish

- **Three-column Noise Settings tab** — was two columns, the right
  column was getting tall enough to scroll on 1080p heights.  Three
  columns rebalanced after tester feedback that the middle column had
  too much weight: `Cap + Squelch | NB + ANF | NR2 + Method + LMS`.
- **Brighter checkboxes + radio buttons** — tick-box borders now use
  the dusty-blue text-muted color against the dark recess instead of
  the near-invisible BORDER tone.  Also bumped 14 → 16 px for visual
  weight.
- **Global font 10pt → 11pt** — slight bump for readability on the
  dense Settings tabs.
- **Tuning panel: vertical breathing room** — the MHz / Step labels
  were being clipped against the freq-display digits; added 10 px
  spacing between the freq row and the MHz/Step controls.
- **Tuning panel: vertical resize works again** — the panel was
  height-locked because the `FrequencyDisplay` widget shipped with
  `QSizePolicy.Fixed` vertical, which made Qt's row layout refuse
  extra height.  Fixed by overriding the policy and giving the panel
  an explicit MinimumExpanding policy with a 180 px floor.
- **DSP+Audio panel: AGC + notch readouts fixed-height** — the
  `@notches` / AGC labels were stretching to fill the row when the
  panel grew taller, while the buttons next to them stayed put.  Now
  they all behave the same way.
- **Lock panels actually locks all panels** — the v0.0.6
  implementation disabled splitter handles but missed the QMainWindow
  internal dock-area separator.  Third lock layer added (per-dock
  fixed-size pin) and gated so it only fires when actually
  transitioning between locked / unlocked states.

### Update notifications

- **Pre-release + full-release parity** — the silent update checker
  was hitting `/releases/latest`, which by GitHub's design hides
  pre-releases.  Switched to `/releases` and pick the highest semver
  tag ourselves so testers on a pre-release get notified of newer
  pre-releases AND any subsequent full release.
- **Toolbar update indicator** — when an update is available, a
  small orange "🆕 vX.Y.Z available" pill now appears centered
  between the clocks and the HL2 telemetry block on the header
  toolbar.  Click to open Help → Check for Updates.

### Other

- Neural NR work formally **deferred until after RX2 + TX** —
  `onnxruntime` / DeepFilterNet exploration code removed (~1,100
  lines) and the menu entry left in place as a `(deferred)`
  placeholder.  WDSP-derived NR1 / NR2 / NR3 (LMS) / ANF / NB /
  Squelch all stay.
- Self-compile error messages on the DSP+Audio device list
  distinguish "sounddevice not installed" vs "PortAudio failed to
  load" vs "no devices reported by Windows," with copy-pasteable
  pip install hints.

## What was in 0.0.6 — "Operator Awareness"

The deepest DSP refresh since the 0.0.x series began plus the
introduction of all-station awareness features.  Five WDSP modules
ported with proper attribution under Lyra's new GPL v3+ license,
all-mode squelch, LMS adaptive line enhancer, full Martin-statistics
upgrade for NR2, and built-in weather alerts (lightning + high wind).

### Noise toolkit upgrades

- **NR1 — continuous strength slider (0–100)** replaces the old
  Light/Medium/Heavy radio buttons.  Behind the scenes, dead VAD-gated
  noise tracker replaced with min-statistics (Martin 2001).
- **NR2 — full WDSP-equivalent stack.**  Martin minimum-statistics
  noise PSD, AEPF median-smoothing post-filter, speech-presence
  probability soft mask (witchHat), and a runtime Wiener-vs-MMSE-LSA
  gain-function picker (right-click the strength slider).  Aggression
  range expanded to 0–200 — the new machinery makes the upper range
  listenable without speech distortion.
- **LMS Line Enhancer (NR3)** — new.  Adaptive predictor that pulls
  weak CW out of broadband hiss.  Slots ANF → LMS → NR in the chain;
  works alongside NR2 for best weak-signal results.  Block-LMS
  optimization keeps CPU around 4% real-time.
- **All-Mode Squelch** — new.  RMS + auto-tracked noise floor with
  hysteresis and hang-time.  Mutes between transmissions on every
  modulation type (SSB / AM / FM / CW).  Per-condition hang time
  bridges natural speech pauses without chopping consonants.

### Weather Alerts (new)

Three toolbar indicators between the ADC RMS readout and the clocks:
- ⚡ Lightning — closest strike + distance + bearing, color-coded by
  proximity (yellow > 25 mi, orange < 25 mi, red < 10 mi)
- 💨 Wind — sustained / gust speed, three severity tiers
- ⚠ NWS severe weather warning (red, hidden when no warning active)

Indicators auto-hide on quiet days.  Desktop toasts fire on tier-
crossing events with 15-minute hysteresis to prevent spam.  Operator-
selectable sources: Blitzortung, NWS, Ambient Weather PWS, Ecowitt
PWS.  Disclaimer-gated — alerts are informational only, not a safety
system.  Settings → Weather (last tab in the dialog).

### Operator / Station globals

Callsign + Maidenhead grid square + manual lat/lon backup live in
Radio Settings now.  Consumed by TCI spots, weather alerts, and any
future feature that needs your station location.  Migrates from the
older TCI-only callsign field on first run.

### License change

Lyra v0.0.6 onward is **GNU General Public License v3 or later**
(was MIT for v0.0.5 and earlier).  The relicense was made
specifically to enable WDSP integration — see `NOTICE.md` for the
full attribution and license history.

### Neural noise reduction — deferred

AI-based neural noise reduction is on the roadmap but **deferred
until after RX2 + TX work lands**.  v0.0.6 dev briefly explored
PyTorch / DeepFilterNet and onnxruntime / NSNet2 paths; both are
viable but introduce dependency-management friction (Python-version
lag, Rust toolchain requirements, model-file distribution) that's
better tackled when the broader transceiver functionality is in
place.  The "Neural" entry stays in the right-click NR backend
menu as a `(deferred — pending RX2 + TX)` placeholder so operators
know it's planned.

### Minimum Windows version

Lyra requires **Windows 10 build 17763 (1809, October 2018) or later**.
This is the official baseline for winrt modern toast notifications
(weather alerts) and PySide6 6.5+.  Older Windows installs are
rejected by the installer with a clear error message.

## What was in 0.0.5 — "Listening Tools"

A meaningful audio-DSP and panadapter release driven by extended
field testing on the operator's HL2+. Two new CW DSP tools, full
GPU panadapter feature parity, an audio chain rebuild that fixes
several long-standing stability issues, and an auto-update check
so testers don't get stranded on old builds.

### New listening tools

- **APF — Audio Peaking Filter (CW)** — narrow peaking biquad
  centered on the operator's CW pitch. Boosts weak CW tones above
  the noise floor without the ringing tail of a brick-wall narrow
  filter. Right-click for BW/Gain quick presets. CW-only (button
  preserved across mode switches but only audibly affects CWU/CWL).
- **BIN — Binaural pseudo-stereo (headphones)** — Hilbert phase
  split puts the audio "in the middle of the head" for spatial CW
  perception and SSB voice widening. Adjustable depth 0–100 %, equal
  loudness normalized. Works on all modes.

### GPU panadapter — full feature parity (BETA)

Everything the QPainter widget does, now on the GPU:
- Band plan overlay (sub-band segments + landmark click-to-tune)
- Peak markers (line / dots / triangles, optional dB readout)
- DX/contest spots with multi-row collision packing + age-fade
- Notch markers, drag-to-resize, right-click menu
- Click-to-tune, Y-axis drag, wheel zoom, RX-BW edge drag
- Passband overlay, noise-floor reference, VFO marker, CW Zero line
- Grid toggle (operator preference)

GPU mode is opt-in via Settings → Visuals → Graphics backend.
Testers should compare against the QPainter backend to validate
parity on their GPU.

### Audio chain rebuild

- **AGC profiles recalibrated** — Fast/Med/Slow release time
  constants were ~20× too slow (audio stayed clamped for many
  seconds after a peak). Retuned to standard SDR-client conventions:
  Fast τ≈120 ms / hang 130 ms, Med τ≈250 ms / hang 0, Slow τ≈500 ms /
  hang 1 s.
- **AGC OFF audibility** — was 14 dB quieter than AGC ON because
  AGC's typical gain (~5×) wasn't compensated. Fixed with a constant
  +14 dB makeup so toggling AGC produces only a slight loudness
  delta as the chain design intended.
- **Mode = Tone hang** — the test-tone generator was producing
  samples at IQ rate (192k) but feeding a 48k sink, causing
  back-pressure → GUI lockup on output swap. Fixed.
- **Audio output rate-sticky bug** — switching between AK4951 and
  PC Soundcard could lock the audio path until rate cycle. Fixed.
- **WWV ↔ FT8 stuck audio** — big freq/mode jumps could leave audio
  silent until rate cycle. Root cause was C&C register staleness;
  fixed with round-robin keepalive at the protocol layer.

### S-meter overhaul

- **LNA-invariant S-meter** — moving the LNA slider no longer
  changes meter reading (matches the operator-set S9 calibration
  point). dBFS → dBm conversion subtracts LNA gain from the raw
  level so the S-meter shows actual antenna signal strength.
- **Auto-LNA pull-up** — opt-in. Auto button now optionally raises
  LNA when the band is sustained-quiet, in addition to the existing
  back-off-on-clipping behavior. Two-tier ceiling (+24 dB on quiet
  bands, +15 dB when passband signal is present) plus a passband
  margin gate keep it out of the IMD zone.

### Other refinements

- **Spot prefixes** — switched from regional-indicator emoji flags
  (Windows can't render them) to plain-text 2-letter ISO codes.
  Spot boxes now show e.g. "US N8SDR" or "JA JA1XYZ" with consistent
  rendering on every platform.
- **Settings → DSP → CW group** — pitch + APF + BIN settings live
  here together (renamed from "CW pitch").
- **Auto-update check on startup** — silent background check of the
  GitHub releases API. If a newer Lyra is published, the operator
  sees a status-bar message and a "🆕 Update available" badge on
  the Help menu's Check for Updates entry.

See the in-app User Guide (F1) for the full APF and BIN topic
docs, plus updated AGC, audio, and spectrum coverage.

## What was in 0.0.4 — "Discovery & Scale Polish"

- **Auto-scale = clamp, not disable** — dragging the dB-range scale
  on the spectrum no longer turns auto-scale OFF. Manual range
  becomes the BOUNDS that auto-scale stays inside.
- **Per-band scale memory** — each band remembers its own scale
  bounds, with sensible factory defaults (160 m bottom-heavy,
  6 m top-heavy) so band-swapping just works.
- **Multi-NIC discovery fix** — auto-discover broadcasts on every
  local network interface in parallel. Fixes the "tester with
  Wi-Fi + Ethernet couldn't find the HL2" failure mode.
- **Help → Network Discovery Probe** — diagnostic dialog with
  per-interface probes and a copy-to-clipboard log.
- **OpenGL upgrade nag** — fixed timing so the suggestion popup
  isn't hidden behind the main window on slow boots.

## What was in 0.0.3 — "First Tester Build"

The first packaged installer release. Notable additions since 0.0.2:

- **True dBFS spectrum calibration** — FFT math fixed so 0 dBFS is a
  full-scale tone; per-rig cal trim slider for known path losses
- **S-meter cal + Peak/Average response mode** — right-click meter
  for one-click "Calibrate to S9 (-73 dBm)" + steady time-averaged
  reading
- **Lit-Arc meter widget** — segmented arc-bar meter with no needle
  (less jittery than analog dial), three modes (S / dBm / AGC)
- **Top-banner toolbar** — large local + UTC clocks, live HL2
  hardware telemetry (T / V), CPU% (matches Task Manager), GPU%
  (NVIDIA via NVML or any vendor via Win32 PDH)
- **Settings backup / import / export + auto-snapshots** — JSON
  snapshot of every preference taken on each launch, last 10 kept;
  one-click rollback via File → Snapshots
- **Layout safeguards** — Lock Panels (Ctrl+L), always-factory
  Reset Panel Layout, sanity check refusing to save degenerate
  layouts on close
- **Click-and-drag spectrum tuning** — pan the panadapter like a
  Google Maps view
- **Fine-zoom slider** + click-the-scale-label gestures
- **Stereo balance slider** with center detentation, working on both
  PC Soundcard and AK4951 outputs
- **HL2 Telemetry Probe** dialog under Help — diagnose firmware-
  variant decode mismatches against your specific HL2

Plus extensive performance work to eliminate spectrum/waterfall
stutter (slider debounce, hidden meter timer pause, waterfall
bilinear smoothing, spectrum FPS press/release pattern).

See `docs/help/getting-started.md` for the full guided tour or
press F1 inside the app for the in-app User Guide.

## Features so far

**RX signal chain**
- Native HPSDR P1 discovery + streaming (UDP, port 1024)
- Spectrum-correct panadapter (HL2 baseband mirror correction applied)
- AGC with Fast / Medium / Slow / Auto / Custom profiles
- Per-band auto-LNA (overload-protection mode, capped +31 dB)
- Manual notch filters — multi-notch, per-notch Q, live cut-depth
  visualization on the spectrum
- Spectral-subtraction noise reduction (Light / Medium / Aggressive)
- Noise-floor reference line with auto-threshold feeding AGC
- Passband overlay with draggable edges for live RX BW tweaks
- Peak markers (Line / Dots / Triangles, in-passband only)

**Bands and modes**
- IARU regional band plans (US / R1 / R3 / NONE)
- Colored sub-band segments + FT8 / FT4 / WSPR / PSK landmark
  triangles — click a triangle to tune and switch modes
- SSB (USB/LSB), CW, AM, FM, DIGU / DIGL

**UI**
- Docked-panel workspace (drag to float / tab / reset layout)
- Analog S-meter with LED-bar alternative (right-click to switch)
- Waterfall with eight palettes (Classic / Inferno / Viridis /
  Plasma / Rainbow / Ocean / Night / Grayscale)
- Click-label color picker in Settings → Visuals (text of each field
  painted in that field's current color + bolded for at-a-glance
  configuration view)
- Optional OpenGL rasterization backend so resize/fullscreen doesn't
  pause audio
- Y-axis drag-to-rescale on the spectrum's right edge
- Two-way sync between front-panel View sliders and Settings

**Integration**
- TCI v1.9 server — drives SDRLogger+, DX clusters, CAT clients
- DX spot rendering with age fade and multi-row collision packing
- Per-session notch bank, per-band frequency memory

**Audio out**
- AK4951 (HL2's onboard codec) or PC soundcard
- Automatic fallback when the stream rate exceeds AK4951's 48 kHz

## Stack

- **UI:** PySide6 (Qt6)
- **Protocol:** Native Python HPSDR Protocol 1 (UDP, port 1024)
- **DSP:** NumPy / SciPy (C++ core via pybind11 planned post-RX-stable)
- **Control:** TCI v1.9 server
- **Audio:** sounddevice (portaudio), optional AK4951 passthrough via
  the HL2's EP2 frames
- **Target OS:** Windows-first

## Running from source

Requires Python 3.11+ on Windows.

**Quickstart:**

```
pip install -r requirements.txt
python -m lyra.ui.app
```

Or double-click `LYRA.bat`.

**Step-by-step install for non-developer testers:**
see [`INSTALL.md`](INSTALL.md) — covers Python installation, Git
setup, dependency install, common gotchas, and feedback channels.
A printable Word version is also at
[`docs/Lyra-SDR-Install-Guide.docx`](docs/Lyra-SDR-Install-Guide.docx).

On first launch, Lyra tries to discover an HL2 on the local network.
If the board is reachable it'll show up in the connection panel; if
not, check firewall, cabling, and that the HL2 has power. Full
troubleshooting guide in the in-app User Guide (press **F1**).

## Hardware references

- Hermes Lite 2: http://hermeslite.com/
- Hermes Lite 2+: https://www.hermeslite2plus.com/

## Relationship to Thetis / WDSP / openHPSDR

Lyra v0.0.5 and earlier (under MIT) were a clean-room implementation
referencing only protocol documentation and operator-visible UI
behavior — no Thetis source was incorporated.

Starting with v0.0.6 (under GPL v3 or later), Lyra is in full
license compatibility with the openHPSDR ecosystem. Future releases
may directly incorporate or link with GPL'd ham-radio libraries
(notably WDSP for PureSignal, CESSB, and advanced TX). All such
incorporations preserve upstream copyright + GPL terms; see
`NOTICE.md` for ongoing third-party disclosures.

ExpertSDR3 is closed-source commercial software from Expert
Electronics — referenced from published manuals as a design
inspiration only, no code involvement.

## Backlog

Tracked in `docs/backlog.md`. High-priority open items: TX path,
per-band notch memory, neural NR integration, installer for beta
testers.

## License

**GNU General Public License v3.0 or later** — see `LICENSE`.

Lyra was originally released under the MIT License up through
**v0.0.5 ("Listening Tools")**. Starting with v0.0.6, Lyra is
relicensed under **GPL v3 or later** to match the licensing of the
broader openHPSDR / WDSP ecosystem and to enable future integration
with WDSP-based features (PureSignal, CESSB, advanced TX). Past
releases (≤ v0.0.5) remain under their original MIT terms; the
relicense applies only to v0.0.6 and later.

What this means in practice:

- You can use Lyra for any purpose, including commercial use
- You can modify Lyra freely
- You can redistribute Lyra and your modifications — but the result
  must also be GPL v3 (or later), and you must make source available

What it does NOT change:

- Donations are still welcome (PayPal, etc.) — GPL doesn't restrict
  receiving payment for the project
- Operators can run Lyra free of charge, no strings attached
- The complete source remains public on GitHub

For the canonical GPL v3 text, see `LICENSE` in this repository or
<https://www.gnu.org/licenses/gpl-3.0.html>.

© 2026 Rick Langford (N8SDR)
