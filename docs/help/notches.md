# Notch Filters

Narrow-band IIR notches for killing carriers, birdies, heterodynes,
and local interference without touching the receive bandwidth.

## Enable the Notch Filter

The **NF** button on the DSP button row is the master switch. All
notch gestures on the spectrum and waterfall are gated on this
button:

- **NF ON** — right-click opens the full notch menu; shift+right-click
  quick-removes the nearest notch.
- **NF OFF** — right-click opens a tiny menu whose only option is
  "Enable Notch Filter". No notches can be added, removed, or
  modified while NF is off — but **existing notches are not deleted**,
  they're just bypassed in the DSP path. Turn NF back on and your
  notches return exactly as you left them.

This gating keeps the right-click gesture free for other spectrum
features (drag-to-tune, spot menus, landmark picks) whenever you're
not actively working notches.

## Width-based, not Q-based

Lyra describes each notch by its **−3 dB bandwidth in Hz**, not by a
dimensionless Q value. That matches how operators naturally think
about notches ("kill that 100 Hz wide
chunk" vs. "make a Q=30 notch and hope it's wide enough").

Each notch carries:

- A **center frequency** (where you clicked)
- A **width in Hz** (the visible rectangle's horizontal extent)
- An **active** flag (per-notch enable/bypass without losing placement)

## Visualization

Each notch renders as a **filled red rectangle** spanning its full
−3 dB bandwidth, with a thin red center line for precise targeting.
The rectangle appears identically on the panadapter and the
waterfall.

- **Active notches** — saturated red fill, bright red center line,
  width label in Hz drawn next to the notch when there's room.
- **Inactive notches** — desaturated grey fill and grey center line.
  Visible but obviously bypassed; the DSP loop skips them.

The minimum visible width is roughly 14 px so even very narrow
notches (5–20 Hz at high zoom) stay grabbable.

## Placing a notch

With NF on, **right-click** anywhere on the spectrum or waterfall.
A context menu appears at the click site:

- **Add notch at X.XXXX MHz** — drops a notch at the right-click
  frequency using the current default width.
- **Disable / Enable this notch** — appears only when right-clicking
  near an existing notch. Toggles its active flag without removing
  the placement (great for A/B testing whether a notch is helping).
- **Remove nearest notch** — deletes the closest existing notch.
- **Clear ALL notches** — removes every notch in one shot.
- **Default width for new notches ▸** — submenu with six width
  presets (20, 50, 80, 150, 300, 600 Hz). The current default is
  shown with a leading checkmark.
- **Disable Notch Filter** — quick off-switch without leaving the
  spectrum view.

**Shift + right-click** (NF must be on) is a fast "remove nearest"
gesture — same as the menu's Remove-nearest action but skips the
menu. Preserved for operators who learned it from other SDR clients.

## Width presets and what they're for

| Width   | Use case |
|--------:|:---------|
| **20 Hz**  | Pinpoint single tone (CW carrier, single FT8 lane, beacon) |
| **50 Hz**  | Surgical CW carrier kill, narrow heterodyne |
| **80 Hz** *(default)* | Covers FT8 / FT4 (47 Hz spread) in one notch |
| **150 Hz** | RTTY pair, drifty CW signal |
| **300 Hz** | Broadband heterodyne, splatter from a strong adjacent SSB |
| **600 Hz** | Blanket of QRM, AM-broadcast bleed within passband |

The default starts at **80 Hz** which covers all 8 FT8 tones (which
span 47 Hz at 6.25 Hz spacing). For narrow-CW work, drop the default
to 20–50 Hz before placing notches; for broadband interference,
bump to 300+ Hz.

## Adjusting an existing notch

- **Mouse wheel** over a notch → adjusts its width.
  - Wheel **up** = narrower (smaller Hz)
  - Wheel **down** = wider (larger Hz)
  - Each tick is a 15% multiplicative change.
- **Left-drag vertically** over a notch → fine-grained width control.
  - Drag **up** = narrower
  - Drag **down** = wider
  - 1.5% per pixel of motion after a small dead-zone.
- **Right-click on a notch** → menu includes "Disable this notch"
  to bypass without removing.

## Carrier-on-VFO (DC) handling

When you click *exactly* on the VFO center to notch a carrier sitting
at DC baseband (WWV, an AM station tuned in zero-beat), the standard
narrow-notch IIR filter design can't catch DC — its bandwidth
collapses to zero as frequency approaches 0. Lyra detects this case
and automatically switches to a **butterworth high-pass** for the
DC region (notches whose visible extent crosses zero baseband). The
visible rectangle still represents the kill region; the operator
doesn't need to know the underlying filter type changed.

## Front-panel notch counter

The DSP + Audio panel shows a compact counter next to the **NF**
button:

```
NF   3 notches  [50, 80*, 200 Hz]  (1 off)
```

- Numbers in brackets are the per-notch widths.
- An asterisk (`*`) marks an inactive notch.
- The trailing "(N off)" appears only when one or more notches are
  inactive.

Hovering the NF button or the counter shows a tooltip with the full
gesture summary.

## Multi-notch

Unlimited notches in theory; practically ~10 before CPU matters. Each
notch is an independent stateful IIR (or DC-blocker high-pass for
the DC case) operating at the audio sample rate (48 kHz).

## Notch + AGC

Notches run **post-demod, pre-AGC**. So removing a heterodyne with a
notch immediately drops AGC drive — useful when a loud birdie is
pumping AGC and choking the signal you actually want.

If AGC is on, the AGC will also compensate when a notch removes a
strong signal — the audio level may stay similar even when a notch
is doing its job. To verify a notch is actually attenuating, switch
AGC off briefly: with AGC off, the notched signal should clearly
drop in level when the notch is added or enabled.

## Persistence

Notches are per-session right now (not saved on close). Per-band
notch memory is on the backlog.
