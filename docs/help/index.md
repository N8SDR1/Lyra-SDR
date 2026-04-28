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
- **APF (Audio Peaking Filter)** — narrow CW boost at the operator's pitch
- **Spectrum & Waterfall** — pan, zoom, drag, palettes
- **S-Meter** — analog vs LED styles
- **External Hardware** — N2ADR filter board, USB-BCD for linear amps
- **TCI Server** — integration with log4OM, N1MM+, JS8Call, etc.
- **Audio Routing** — HL2 vs HL2+ paths, AK4951, sounddevice
- **Keyboard Shortcuts** — all the hotkeys
- **Troubleshooting** — common issues and their fixes

*(At the end of the topic list)*

- **Support Lyra** — donate, file bugs, contribute
- **License** — MIT (full text + third-party attributions)

## About

**Version:** {{ version_full }}
**Project:** [{{ repo_url }}]({{ repo_url }})
**Built on:** PySide6 / Qt6, NumPy, SciPy, sounddevice

The version string above is rendered live from the running app, so
this page always shows the build you launched — handy for
attaching to bug reports.

## License

Lyra is released under the **MIT License**.
See the `LICENSE` file at the project root for the full terms.

Lyra is an independent, clean-room implementation. The code is
not derived from any other SDR client's source. Other established
HL2 client programs are referenced only as protocol cross-checks
during development. If you find anything in Lyra that appears to
copy code from a third-party project, please file an issue so it
can be investigated.

The TCI server protocol implemented by Lyra (Help → Settings →
Network/TCI) was created and is maintained by EESDR Expert
Electronics as an open specification; Lyra implements it from the
public TCI v1.9 / v2.0 documentation.
