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

1. Open **⚙ Settings…** → **Radio** tab, enter your HL2's IP
   address (click **Discover** if you don't know it).
2. Close Settings, click **▶ Start** in the toolbar.  The
   status dot should turn green.
3. Click a digit on the main frequency display and scroll the
   mouse wheel to tune — or type a frequency in MHz directly.
4. Right-click the **AGC** cluster on the DSP & AUDIO panel →
   pick **Med** for SSB, **Slow** or **Long** for AM, or **Off**
   for digital modes (FT8 / FT4).
5. Pick a mode (LSB / USB / CWL / CWU / AM / FM / DIG) in the
   MODE & FILTER panel.

## Topic index

- **Introduction** — what Lyra is, why it's called Lyra, project
  philosophy, who's behind it
- **Getting Started** — first-time setup, connecting to the HL2
- **Tuning** — frequency display, bands, VFO memory
- **RX2 (Dual Receiver)** — second receiver on DDC1, focus model,
  stereo split (SUB), VFO bridge buttons (1→2 / 2→1 / ⇄)
- **Time Stations** — TIME button cycles through WWV / WWVH / CHU
  HF time-signal broadcasters; country-aware ordering puts your
  closest stations first
- **Memory presets** — GEN1/2/3 quick slots + 20-entry named
  Memory bank with CSV import/export, all on the BANDS panel
- **Shortwave broadcaster overlay (EiBi)** — auto-detected EiBi
  station labels (name + language + target region) painted on the
  panadapter inside SW broadcast bands; suppressed inside your
  region's ham bands by default
- **Modes & Filters** — demodulation modes, bandwidth presets
- **AGC** — profiles (Off / Fast / Med / Slow / Long / Auto /
  Custom), live gain readout, WDSP-driven engine
- **Notch Filters** — placing, adjusting, multi-notch, saved
  notch banks
- **Noise Reduction** — WDSP EMNR with mode 1-4 picker, AEPF
  anti-musical-noise post-filter, NPE noise estimator,
  captured noise profile library
- **LMS Line Enhancer** — predictive NR3 stage that lifts CW
  carriers and voice formants above broadband noise; the
  inverse of NR (where NR removes what isn't signal, LMS
  amplifies what looks periodic)
- **Noise Blanker** — IQ-domain impulse suppression
  (pre-decimation), profiles Off / Light / Medium / Heavy /
  Custom
- **Auto Notch Filter** — adaptive notch for unknown
  heterodynes / carriers / RTTY spurs, profiles Off / Light /
  Medium / Heavy / Custom
- **APF (Audio Peaking Filter)** — narrow CW boost at the
  operator's pitch
- **BIN (Binaural)** — pseudo-stereo headphone listening for
  CW + SSB
- **Propagation** — slim panel showing live solar numbers
  (SFI / A / K), color-coded band-conditions heatmap, and
  NCDXF International Beacon Project auto-follow
- **Weather Alerts** — background watcher for lightning
  (Blitzortung), high wind + storm warnings (NWS), and personal
  weather station integration (Ambient WS-2000, Ecowitt).
  Toolbar indicator + desktop toasts.
- **Spectrum & Waterfall** — pan, zoom, drag, palettes
- **S-Meter** — Lit-Arc vs LED-bar styles (chip-row picker in Meters header)
- **External Hardware** — N2ADR filter board, USB-BCD for
  linear amps
- **TCI Server** — integration with log4OM, N1MM+, JS8Call,
  etc.
- **Audio Routing** — HL2 audio jack vs PC Soundcard, gain
  chain, AF Gain wiring
- **Keyboard Shortcuts** — all the hotkeys
- **Troubleshooting** — common issues and their fixes

*(At the end of the topic list)*

- **Support Lyra** — donate, file bugs, contribute
- **License** — GPL v3+ (full text + third-party attributions)

## About

**Version:** {{ version_full }}
**Project:** [{{ repo_url }}]({{ repo_url }})
**Built on:** PySide6 / Qt6, NumPy, SciPy, sounddevice

The version string above is rendered live from the running app, so
this page always shows the build you launched — handy for
attaching to bug reports.

## License

Lyra (v0.0.6 and onward) is released under the **GNU General Public
License v3 or later**. v0.0.5 and earlier were released under the
MIT License; that history is preserved.

See the `LICENSE` file at the project root for the full terms, or
the in-app **License** topic for a plain-English summary.

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
