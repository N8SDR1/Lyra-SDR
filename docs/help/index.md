# Lyra User Guide

![Lyra SDR](assets/logo/lyra-icon-256.png)

Welcome. Lyra is a Qt-based SDR transceiver for Steve Haynal's
**Hermes Lite 2** and **Hermes Lite 2+** (the "+" adds an AK4951 audio
add-in board with a line-level jack for RX audio and a microphone path
for TX).

This guide lives inside the app — press **F1** anywhere or use
**Help → User Guide** to open it. Pick a topic from the tree on the
left.

The source markdown files are in `docs/help/` in the project folder —
edit them in any editor and hit **Reload** in the help window to pick
up changes.

## Quick start

1. Open **⚙ Settings…** → **Radio** tab, enter your HL2's IP address
   (click **Discover** if you don't know it).
2. Close Settings, click **▶ Start** in the toolbar. The status dot
   should turn green.
3. Click a digit on the main frequency display and scroll the mouse
   wheel to tune — or type a frequency in MHz directly.
4. Right-click the **AGC** cluster on the DSP & AUDIO panel → pick
   **Auto**. The threshold will follow the band noise floor.
5. Pick a mode (LSB / USB / CWL / CWU / AM / FM / DIG) in the
   MODE & FILTER panel.

## Topic index

- **Introduction** — what Lyra is, why it's called Lyra, project
  philosophy, who's behind it
- **Getting Started** — first-time setup, connecting to the HL2
- **Tuning** — frequency display, bands, VFO memory
- **Modes & Filters** — demodulation modes, bandwidth presets
- **AGC** — profiles (Off / Fast / Med / Slow / Auto / Custom),
  threshold, auto-tracking
- **Notch Filters** — placing, adjusting, multi-notch
- **Noise Reduction** — spectral-subtraction NR, profiles, neural roadmap
- **Spectrum & Waterfall** — pan, zoom, drag, palettes
- **S-Meter** — analog vs LED styles
- **External Hardware** — N2ADR filter board, USB-BCD for linear amps
- **TCI Server** — integration with log4OM, N1MM+, JS8Call, etc.
- **Audio Routing** — HL2 vs HL2+ paths, AK4951, sounddevice
- **Keyboard Shortcuts** — all the hotkeys
- **Troubleshooting** — common issues and their fixes

## About

Version: 0.x (alpha, under active development)
Project: [github.com](https://github.com/)  *(update URL)*
Built on: PySide6 / Qt6, NumPy, SciPy, sounddevice

## License

Lyra is released under the **MIT License**.
See the `LICENSE` file at the project root for the full terms.

Third-party dependencies and design references are documented in
`NOTICE.md`. In particular: Lyra is **not** derived from Thetis (the
openHPSDR reference client) or ExpertSDR3 source code — those projects
are cited only as design
references and protocol cross-checks. If you find anything in
Lyra that appears to copy code from a third-party project, please
file an issue so it can be investigated.
