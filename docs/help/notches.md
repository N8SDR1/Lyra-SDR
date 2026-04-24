# Notch Filters

Narrow-band IIR notches for killing carriers, birdies, and local
heterodynes without touching the receive bandwidth.

## Enable the Notch Filter

The **Notch** button on the DSP + Audio panel (same as the **NF**
toggle on the DSP button row) is the master switch. All notch gestures
on the spectrum and waterfall are gated on this button:

- **NF ON** — right-click opens the full notch menu; shift+right-click
  quick-removes the nearest notch.
- **NF OFF** — right-click opens a tiny menu whose only option is
  "Enable Notch Filter". No notches can be added, removed, or modified
  while NF is off — but **existing notches are not deleted**, they're
  just bypassed in the DSP path. Turn NF back on and your notches
  return exactly as you left them.

This gating keeps the right-click gesture free for other spectrum
features (drag-to-tune, spot menus, landmark picks) whenever you're
not actively working notches.

## Placing a notch

With NF on, **right-click** anywhere on the spectrum or waterfall.
A context menu appears at the click site:

- **Add notch at X.XXXX MHz** — drops a notch at the right-click
  frequency.
- **Remove nearest notch** — deletes the closest existing notch
  (disabled if none exist).
- **Clear ALL notches** — removes every notch in one shot (also
  disabled when none exist).
- **Default Q for new notches ▸** — submenu with five Q presets
  (10 / 30 / 60 / 100 / 200). The current default is shown with a
  leading checkmark.
- **Disable Notch Filter** — quick off-switch without leaving the
  spectrum view.

**Shift + right-click** (NF must be on) is a fast "remove nearest"
gesture — the same as the menu's Remove-nearest action but without
opening the menu. Preserved for operators who learned it from other
SDR clients.

## The notch "well" overlay

Each active notch renders as a red V-shaped **well** descending from
the top of the panadapter. The well's contour follows the notch
filter's actual biquad magnitude response, so you can see at a glance
both **how wide** the notch is (horizontal extent) and **how deeply**
it attenuates at center (how far the well descends).

- A biquad band-reject has theoretically infinite null at exact
  center, so the displayed depth is capped at **−40 dB** with a label
  drawn just below the well bottom.
- At the well's edges the depth tapers smoothly back to 0 dB (no
  cut), matching the real filter's response.
- A thin red hairline down the center of the well keeps very narrow
  notches (high Q) grabbable even when the fill is only a pixel or
  two wide.

As you adjust Q (see below), the well visibly narrows/widens —
higher Q = narrower well with sharper sides, lower Q = broader bowl.

## Adjusting Q

Each notch has its own Q (width). The DEFAULT Q for **newly placed**
notches is Q=30, changeable via the right-click "Default Q for new
notches" submenu. Existing notches keep their Q unless you adjust
them individually:

- **Mouse wheel** over a notch → adjusts that notch's Q.
- **Left-drag vertically** over a notch → fine-grained Q control.
  Drag up to raise Q (narrow), down to lower Q (widen). A small
  dead-zone prevents micro-movements from drifting Q.

Why a default-Q setting at all? When you right-click-Add, the new
notch starts at the current default Q. So if you're about to drop
five narrow notches on tight carriers, set Default Q to 100 first
and they'll all start life narrow — saves five Q adjustments.

### Q cheat-sheet

| Q    | Width at 5 kHz offset | Good for                       |
| ---: | :-------------------- | :----------------------------- |
| 10   | ~500 Hz               | Rattly broadcast splatter      |
| 30   | ~170 Hz               | General CW / carrier kill      |
| 60   | ~80 Hz                | Surgical on one carrier        |
| 100  | ~50 Hz                | Isolates a single tone         |
| 200  | ~25 Hz                | Pinpoint beacon notch          |

(The actual −3 dB bandwidth is `f_baseband / Q`, so widths scale with
how far off-tune the notch sits from the VFO center.)

## Front-panel notch counter

The DSP + Audio panel shows a compact counter next to the Notch
button:

```
Notch   3 notches (Q=30, 45, 100)
```

Hovering either the counter or the Notch button brings up a tooltip
summarising every spectrum/waterfall gesture. No cluttered legend
occupying half the row.

## Multi-notch

Unlimited notches in theory; practically ~10 before CPU matters.
Every notch is an independent stateful IIR filter operating at the
baseband rate in the RX chain.

## Notch + AGC

Notches run **post-demod**, **pre-AGC**. So removing a heterodyne
with a notch immediately drops AGC drive — useful when a loud birdie
is pumping AGC and choking the signal you actually want.

## Persistence

Notches are per-session right now (not saved on close). Per-band
notch memory is on the backlog.
