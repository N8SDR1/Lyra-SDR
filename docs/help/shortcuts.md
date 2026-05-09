# Keyboard Shortcuts

## Global

| Key          | Action                                     |
|--------------|--------------------------------------------|
| **F1**       | Open this user guide                       |
| **Ctrl + ,** | Open Settings dialog                       |
| **Ctrl + Q** | Exit Lyra                                |
| **Ctrl + S** | Toggle Start/Stop                          |
| **Ctrl + R** | (In User Guide) Reload markdown files      |
| **Ctrl + F** | (In User Guide) Focus the search box       |
| **Ctrl + P** | (In User Guide) Print the current topic    |
| **F3**       | (In User Guide) Jump to next search match  |
| **Esc**      | (In User Guide) Close the guide window     |

## User Guide navigation

- **Search box** (top of the guide) — filters topics by content; the
  rendered view jumps to and highlights the first match in the
  selected topic. Clear the box to see all topics again.
- **Print…** — any topic can be printed via the system print dialog
  (including "Microsoft Print to PDF" on Windows to save a copy).
- **? badges** — every main-window panel has a small cyan `?` in the
  top-right corner of its header. Click it to open the User Guide
  directly to the relevant topic.
- **`panel:xxx` links** — some topics contain links like "[DSP panel
  ](panel:dsp)" which flash the corresponding panel in the main
  window so you can locate it at a glance.

## Menu jumps

- **File → Network / TCI…** — straight to TCI settings (bypasses dock)
- **File → Hardware…** — N2ADR / USB-BCD setup
- **File → DSP…** — AGC / NR / NB / ANF / LMS / SQ / APF
- **Toolbar TCI indicator** — click to open Network/TCI settings

## Frequency display (LED readout)

| Action                              | What it does                                           |
|-------------------------------------|--------------------------------------------------------|
| **Click a digit**                   | Select it (small tick appears); arrow keys + wheel will tune at that digit's place value |
| **Double-click anywhere on LED**    | Open inline edit field — type a freq + Enter to commit; Esc cancels |
| **Mouse wheel (over a digit)**      | Tune ±1 at THAT digit's place value (precision aim wins) |
| **Mouse wheel (off the digits)**    | Tune by the **Step combo's** current value (default 1 kHz) |
| **↑ / ↓ keys**                      | Tune ±1 at the selected digit                          |
| **Page Up / Page Down**             | Tune ±10 at the selected digit                         |
| **← / → keys**                      | Move selection to next/prev digit                      |
| **Home / End**                      | Jump selection to MHz / Hz digit                       |

### Direct entry formats

The double-click edit field accepts these formats — type whatever
matches your habit:

| Type | Result |
|---|---|
| `7.125` | 7.125 MHz (single dot = decimal) |
| `7,125` | 7.125 MHz (Euro decimal) |
| `7.125.000` | 7.125 MHz (display format — multi-dot = thousand sep) |
| `7,125,000` | 7.125 MHz (multi-comma = thousand sep) |
| `7125000` | 7.125 MHz (raw Hz) |
| `7125` | 7.125 MHz (mid-range bare → kHz) |
| `7` | 7 MHz (small bare → MHz) |

## Spectrum / waterfall (mouse)

| Action                                   | What it does                                                     |
|------------------------------------------|------------------------------------------------------------------|
| Left-click                               | Tune to that frequency                                           |
| Left-click a band-plan landmark triangle | Tune + switch to the landmark's suggested mode (FT8 → DIGU, etc.) |
| Left-click a TCI spot box                | Tune + switch mode + fire TCI `spot_activated`                   |
| Right-click (**NF on**)                  | Notch context menu (Add / Remove nearest / Clear all / Default Q / Disable) |
| Right-click (**NF off**)                 | Minimal menu — "Enable Notch Filter" only (right-click reserved for future features) |
| Shift + Right-click (**NF on**)          | Quick-remove nearest notch                                       |
| Mouse wheel (open spectrum)              | Zoom BW (1× / 2× / 4× / 8× / 16×)                                |
| Mouse wheel (over a notch rectangle)     | Adjust that notch's width (up = narrower, down = wider)           |
| Left-drag vertically on a notch rectangle | Fine-tune that notch's width (up = narrower, down = wider)        |
| Left-drag on a passband edge             | Adjust current mode's RX BW live                                 |
| Left-drag in rightmost 50 px strip       | Rescale Y-axis — top = `max_db`, middle = pan, bottom = `min_db` |

## Panels

| Panel              | Right-click action                                          |
|--------------------|--------------------------------------------------------------|
| AGC cluster (DSP panel) | Pick AGC profile (Off / Fast / Med / Slow / Auto / Custom) |
| NR button (DSP panel)   | Pick NR Mode (1 / 2 / 3 / 4) + AEPF on/off + NPE method (OSMS / MCRA / etc.) |
| Meter              | Switch style (Analog / LED)                                  |
| BAND               | Per-band memory context menu                                 |
| Spectrum/Waterfall | Notch context menu when NF on; Enable-NF-only menu when NF off |
| Color-field labels (Settings → Visuals → Colors) | Reset that one field to its factory default |

## Docking

All panels are `QDockWidget`s:

- **Drag the title bar** — undock / move / float / tab
- **Double-click the title bar** — toggle float / dock
- **Close button (×)** — hide (toggle back via View menu)
- **View menu** — show/hide any panel
- **View → Reset Panel Layout** — restore defaults

*(If a shortcut listed above isn't wired yet, it's a planned feature
— please file it as a bug if you think it should work and doesn't.)*
