# Lyra — Qt6 SDR Transceiver for Hermes Lite 2 / 2+

**Current version: 0.0.9.3 — "WDSP AGC"**

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

## Latest release — see [CHANGELOG.md](./CHANGELOG.md)

The current release is **0.0.9.3 — "WDSP AGC"** (2026-05-05).
The audio-quality follow-up to v0.0.9.2's host-side cadence rebuild,
focused on the AGC + APF chain operators interact with directly.

- **WDSP-pattern AGC engine** — Lyra's legacy single-state peak
  tracker is retired in favor of a Python port of Warren Pratt's
  WDSP wcpAGC.  Look-ahead ring buffer, 5-state machine, soft-knee
  compression curve, hang threshold gating.  Smoother noise floor,
  fast recovery after transients (~25 ms vs ~500 ms in legacy),
  no scratchy modulation, no post-attack volume surges.  Operator-
  facing presets (Off / Fast / Med / Slow / Auto) are unchanged
  in name; the audio character is consistent with what operators
  hear on Thetis and PowerSDR-class clients.
- **APF moved post-AGC** — the Audio Peaking Filter (CW only) now
  applies its boost AFTER AGC + Volume, so the +18 dB max gain
  produces a literal audible loudness boost on the CW tone instead
  of being absorbed by AGC compensation.  Default bandwidth bumped
  from 80 → 100 Hz so the boost catches signals even when slightly
  off-zero-beat.  Right-click the APF button to pick BW / Gain
  presets; the operator-tunable range (30-200 Hz, 0-18 dB) is
  unchanged.
- **SoundDeviceSink device-info diagnostic** — when an operator
  switches Out: combo to PC Soundcard, the console now logs the
  device PortAudio actually picked, host API in use, and the
  negotiated sample rate vs requested.  Added to diagnose ring-
  overrun reports from PortAudio chains where Windows is doing
  shared-mode resampling silently.
- **Workflow housekeeping** — version bumps, CHANGELOG, install
  guide refresh, and the WDSP integration attribution chain
  (Pratt's GPL v2+ → Lyra's GPL v3+) is fully documented for the
  ported AGC engine.

For the previous v0.0.9.2 release (host → radio EP2 cadence
rewrite, band-change fixes, 48 k IQ rate retirement) and the
v0.0.9 / v0.0.9.1 batch (Memory & Stations, TIME button, EiBi
overlay, etc.):

- **TIME button (HF time-station cycle).**  Press TIME on the BANDS
  panel to jump to WWV / WWVH / CHU.  Press again to step through
  9 time-signal frequencies (2.5, 3.330, 5, 7.850, 10, 14.670, 15,
  20, 25 MHz) in country-aware order — closest stations to your
  callsign first.  Mode + filter set automatically.
- **GEN1 / GEN2 / GEN3 customization.**  Right-click a GEN slot to
  save your current frequency / mode / filter.  Confirm dialog
  prevents accidental overwrites.  Defaults are sensible
  (40m / 20m SSTV / 10m), but every slot is yours to remap.
- **Memory bank — 20 named presets.**  New **Mem** button next to
  GEN3 opens a dropdown of named operator memories (e.g. "OMISS Net
  7.185", "30m beacon").  Add from the dropdown, manage from
  Settings → Bands → Memory: rename, reorder, delete, CSV
  import / export.
- **Shortwave broadcaster overlay (EiBi).**  Lyra now paints
  station IDs on the panadapter — name + language + target region
  — for the broadcaster currently on-air at any visible frequency
  inside the SW broadcast bands (49m through 11m).  Auto-suppressed
  inside your region's amateur allocations so it doesn't clutter
  ham bands.  Pulled from the EiBi seasonal CSV with a one-click
  background updater (auto-update + manual install paths both
  supported).  Multi-row label stacking when bands get crowded.

Plus: tooltip font bumped to 13 pt for readability, Settings dialog
gained a **Bands** tab containing all of the above.

For the full version history (0.0.3 → 0.0.9), see
[CHANGELOG.md](./CHANGELOG.md).

See `docs/help/getting-started.md` for the full guided tour or press
F1 inside the app for the in-app User Guide.

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

© 2026 Rick Langford (N8SDR), Brent Crier (N9BC),
and Lyra-SDR contributors — see [CONTRIBUTORS.md](./CONTRIBUTORS.md)
