# Modes & Filters

## Supported modes

| Mode | Use                                              |
|------|--------------------------------------------------|
| LSB  | Lower Sideband — 160/80/60/40 m phone            |
| USB  | Upper Sideband — 30 m and up phone, DX           |
| CWL  | CW with LSB-side filter (BFO above the signal)   |
| CWU  | CW with USB-side filter (BFO below the signal)   |
| DSB  | Double Sideband (suppressed carrier) — uncommon  |
| AM   | Amplitude Modulation — broadcast, AM nets        |
| FM   | Narrow FM — 10 m and 6 m repeaters               |
| DIGU | FT8, FT4, PSK31, RTTY on USB — same path as USB  |
| DIGL | Same as DIGU but lower-side (rare)               |
| Tone | Test tone generator (useful for TX alignment)    |
| Off  | No audio, panadapter only (backlog: true SPEC mode) |

Not yet implemented (on the backlog): **SAM**, **DRM**, **AM_LSB**,
**AM_USB**, true **SPEC** panadapter-only.

## Bandwidth presets

Each mode has its own BW preset list in the **MODE & FILTER** panel.
Examples:

- **SSB**: 1500, 1800, 2100, 2400, 2700, 3000, 3600, 4000, 6000, 8000 Hz
- **CW**:  50, 100, 150, 250, 400, 500, 750, 1000 Hz
- **AM**:  3000 — 12000 Hz
- **FM**:  6000 — 15000 Hz
- **DIG**: 1500 — 6000 Hz

Click **BW** to open the preset list, pick one. The change is live —
no restart needed.

**Bandwidth lock** — when on, switching mode keeps the current BW
instead of snapping to that mode's default. Useful if you have a
preferred width.

## User-named BW presets

On the backlog: 4 user-named filter BW presets per mode with custom
Low/High edges (reference SDR clients style). Current UI is preset-list only.

## Filter edge dragging

On the backlog: drag the filter passband edges on the spectrum
(translucent overlay) + mouse-wheel shift keys for Lo/Hi cutoff. For
now, BW is symmetric around the carrier.
