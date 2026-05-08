# Notch Filters

Narrow-band notches for killing carriers, birdies, heterodynes,
and local interference without touching the receive bandwidth.
Notch filtering runs inside WDSP's RXA NotchDB engine; Lyra
gives WDSP the operator's notch list (one entry per notch with
a center frequency and width) and WDSP applies them in real
time.

## Enable the Notch Filter

The **NF** button on the DSP button row is the master switch.
All notch gestures on the spectrum and waterfall are gated on
this button:

- **NF ON** — right-click opens the full notch menu;
  shift+right-click quick-removes the nearest notch.
- **NF OFF** — right-click opens a tiny menu whose only option
  is "Enable Notch Filter".  No notches can be added, removed,
  or modified while NF is off — but **existing notches are not
  deleted**, they're just bypassed in the DSP path.  Turn NF
  back on and your notches return exactly as you left them.

This gating keeps the right-click gesture free for other
spectrum features (drag-to-tune, spot menus, landmark picks)
whenever you're not actively working notches.

## What WDSP receives

For each notch you place, WDSP gets three values:

- **Center frequency** (absolute Hz, your VFO-relative
  position is computed from the current freq).
- **Width** (Hz) — the kill region's horizontal extent.
- **Active flag** — true if this notch should run, false
  if you've toggled it off.

WDSP handles the rest: filter design, click-free
coefficient swaps when you drag, automatic re-derivation
when you change VFO frequency, all done internally inside
the cffi engine.

## Width — your primary control

Width is the operator-meaningful parameter.  Pick the
horizontal extent of the kill region you want; WDSP designs a
filter that hits roughly that width's worth of attenuation.

Width presets are on the right-click menu under **Default
width for new notches ▸**:

| Width | Use case |
|--------:|:---------|
| **20 Hz**  | Pinpoint single tone (CW carrier, beacon, single FT8 lane) |
| **50 Hz**  | Surgical CW carrier kill, narrow heterodyne |
| **80 Hz**  | Covers FT8 / FT4 (47 Hz spread) in one notch |
| **150 Hz** | RTTY pair, drifty CW signal |
| **300 Hz** | Broadband heterodyne, splatter from a strong adjacent SSB |
| **600 Hz** | Blanket of QRM, AM-broadcast bleed within passband |

Default is **40 Hz**.  Existing notches keep their individual
widths; the default only affects newly placed notches.

## Depth and Cascade — currently advisory

Each notch also carries a **depth_db** value (default −50 dB)
and a **cascade** integer (1–4 stages).  These are visible on
the right-click "Notch profile" menu (Normal / Deep / Surgical)
and persisted across restarts.

> **Status (v0.0.9.6):** depth_db and cascade are persisted
> per notch but are **advisory only** in WDSP mode.  WDSP's
> NotchDB applies a single notch per entry with width-driven
> attenuation; the depth and cascade values aren't pushed to
> the engine yet.  Operators picking "Deep" or "Surgical" today
> get the same notch as "Normal" — width and active are the
> only parameters that change behavior.

A future build will route depth and cascade to WDSP either by
stacking multiple WDSP notches per Lyra notch (giving real
cascade behavior) or by mapping depth onto WDSP's per-notch
attenuation parameter.  In the meantime the operator-facing UI
is preserved so saved settings carry forward when the wiring
lands.

The right-click submenu still shows **Normal / Deep / Surgical**
because they're a useful operator-mental-model framing — pick
"Deep" for the noisiest birdies, "Surgical" for narrow kills
between close signals — and your selection is recorded for the
moment those values become live.

## Saved notch banks

You can save your current notch setup under a name and reload
it later.  Right-click on the spectrum and pick **Notch banks
(saved presets) ▸**:

- **Save current bank as...** — dialog asks for a name.  The
  current notches (each with its width / depth / cascade /
  active flag) are saved to disk.  If a bank by that name
  already exists it'll prompt before overwriting.
- **Load saved bank ▸** — submenu listing every saved bank.
  One click replaces the current notches with the saved ones.
- **Delete saved bank ▸** — submenu with per-bank confirm
  dialogs.

Banks persist across Lyra restarts (stored in QSettings).

Use cases:

- "My 40m setup" — your usual 7.250 MHz broadcast suppression
  notches, ready to load when you tune to 40m.
- "Contest weekend" — every notch you've placed during a busy
  contest, ready next contest.
- "Local QRM template" — your station's known-bad frequencies
  (neighborhood plasma TV, switching supply, etc.).

There is no auto-load on band change — the operator's call
which bank to recall.  Keeps the feature predictable and
avoids surprise notches appearing when you tune across a
band edge.

## Visualization

Each notch renders as a **filled red rectangle** spanning its
full width, with a thin red center line for precise targeting.
The rectangle appears identically on the panadapter and the
waterfall.

- **Active notches** — saturated red fill, bright red center
  line, width label in Hz drawn next to the notch when there's
  room.
- **Inactive notches** — desaturated grey fill and grey center
  line.  Visible but obviously bypassed; WDSP skips them.

The minimum visible width is roughly 14 px so even very narrow
notches (5–20 Hz at high zoom) stay grabbable.

## Placing a notch

With NF on, **right-click** anywhere on the spectrum or
waterfall.  A context menu appears at the click site:

- **Add notch at X.XXXX MHz** — drops a notch at the
  right-click frequency using the current default width.
- **Disable / Enable this notch** — appears only when right-
  clicking near an existing notch.  Toggles its active flag
  without removing the placement (great for A/B testing).
- **Notch profile ▸** — Normal / Deep / Surgical.  Currently
  advisory (see "Depth and Cascade — currently advisory"
  above).
- **Remove nearest notch** — deletes the closest existing notch.
- **Clear ALL notches** — removes every notch in one shot.
- **Default width for new notches ▸** — width preset submenu.
- **Default profile for new notches ▸** — preset profile
  submenu (also currently advisory).
- **Notch banks (saved presets) ▸** — save / load / delete
  named notch banks.
- **Disable Notch Filter** — quick off-switch.

**Shift + right-click** (NF must be on) is a fast "remove
nearest" gesture — same as the menu's Remove-nearest action
but skips the menu.  Preserved for operators who learned it
from other SDR clients.

## Identifying a notch — hover callout

Hovering the cursor over any notch raises a tooltip showing
the notch's frequency, current width, and active flag:

```
Notch  7.0741 MHz
Width  80 Hz
```

The cursor also changes to a vertical-resize shape so the
operator knows that the notch is draggable in the vertical
direction.

## Adjusting an existing notch

- **Mouse wheel** over a notch → adjusts its width.
  - Wheel **up** = narrower (smaller Hz)
  - Wheel **down** = wider (larger Hz)
  - Each tick is a 15% multiplicative change.
- **Left-drag vertically** over a notch → fine-grained width
  control.
  - Drag **up** = narrower
  - Drag **down** = wider
  - 1.5% per pixel of motion after a small dead-zone.
- **Right-click on a notch** → menu includes "Disable this
  notch" to bypass without removing, plus the profile submenu.

Drag and wheel changes are smooth in WDSP — its internal
filter design handles the swap without the audio click trail
the legacy Python notch had to compensate for.

## Front-panel notch counter

The DSP & AUDIO panel shows a compact counter next to the
**NF** button:

```
NF   3 notches  [50, 80, 200 Hz]
```

Hovering the NF button or the counter shows a tooltip with
the gesture summary.

## Multi-notch

WDSP NotchDB scales to many notches with negligible per-notch
CPU cost.  The bookkeeping is on Lyra's side (operator's
notch list); the actual filtering is done by WDSP in C, so
adding a 10th notch isn't measurably more expensive than
adding a 5th.  Use as many as the band needs.

## Notch + AGC

Notches run **inside WDSP, before AGC** in the RXA chain.
Removing a heterodyne with a notch immediately drops AGC
drive — useful when a loud birdie is pumping AGC and choking
the signal you actually want.

If AGC is on, AGC will also compensate when a notch removes a
strong signal — the audio level may stay similar even when a
notch is doing its job.  To verify a notch is actually
attenuating, switch AGC off briefly: with AGC off, the
notched signal should clearly drop in level when the notch is
added or enabled.

## Persistence

- **Active notches** are per-session — closing Lyra clears
  them.
- **Saved banks** persist across restarts via QSettings.  Save
  your setup as a bank if you want it back next session.
