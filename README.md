# Lyra — Qt6 SDR Transceiver for Hermes Lite 2 / 2+

**Current version: 0.0.3 — "First Tester Build"** *(2026-04-25)*

Modern PySide6 desktop SDR for Steve Haynal's Hermes Lite 2 and HL2+.
Native Python HPSDR Protocol 1, TCI v1.9 server, glassy UI with
analog-look meters, a band-plan overlay with landmark click-to-tune,
and per-notch cut-depth visualization on the panadapter.

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

## What's in 0.0.3 — "First Tester Build"

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

## Design references (cited, not copied)

Lyra is **not** derived from Thetis (the openHPSDR reference
client) or ExpertSDR3 source code — those projects are consulted
only as design references and protocol cross-checks. See `NOTICE.md`
for full third-party disclosures.

## Backlog

Tracked in `docs/backlog.md`. High-priority open items: TX path,
per-band notch memory, neural NR integration, installer for beta
testers.

## License

MIT — see `LICENSE`.

© Rick Langford (N8SDR)
