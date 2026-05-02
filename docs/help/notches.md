# Notch Filters

Narrow-band IIR notches for killing carriers, birdies, heterodynes,
and local interference without touching the receive bandwidth.

> v0.0.7.1 quiet+polish: notches were rebuilt around a parametric
> peaking-EQ biquad with operator-controllable depth, an integer
> cascade for sharper shoulders, and a click-free coefficient-swap
> crossfade.  See "What changed in v0.0.7.1" at the bottom for the
> short version.

## Enable the Notch Filter

The **NF** button on the DSP button row is the master switch.  All
notch gestures on the spectrum and waterfall are gated on this
button:

- **NF ON** — right-click opens the full notch menu; shift+right-click
  quick-removes the nearest notch.
- **NF OFF** — right-click opens a tiny menu whose only option is
  "Enable Notch Filter".  No notches can be added, removed, or
  modified while NF is off — but **existing notches are not deleted**,
  they're just bypassed in the DSP path.  Turn NF back on and your
  notches return exactly as you left them.

This gating keeps the right-click gesture free for other spectrum
features (drag-to-tune, spot menus, landmark picks) whenever you're
not actively working notches.

## Three knobs per notch: width, depth, profile

Each notch carries three operator-meaningful parameters:

- **Width** (Hz) — the visible kill region's horizontal extent.
  Operators think in absolute Hz, not in dimensionless Q values.
- **Depth** (dB, negative) — how much the notch attenuates at its
  center frequency.  Default −50 dB.  Higher (more negative) =
  deeper kill.
- **Profile** — Normal / Deep / Surgical preset that bundles depth
  and shoulder sharpness into one click.  See "Profiles" below.

You don't have to touch the depth slider — the three profiles cover
~95% of operator use cases.

## Profiles (the right-click submenu)

Right-click on a notch and pick **Notch profile ▸** to set its
character in one click:

| Profile | Cascade | Depth | Best for |
|---|---|---|---|
| **Normal** *(default)* | 2-stage | −50 dB | balanced — steep shoulders, predictable kill across the visible width |
| **Deep** | 2-stage | −70 dB | stubborn carriers — broadcast harmonics, strong birdies that leak through Normal |
| **Surgical** | 4-stage | −50 dB | narrow kill with very sharp shoulders — slot a notch between two close signals |

The current preset is shown in the right-click menu's per-notch
heading (e.g. "Notch profile (currently: Deep)").  If you've nudged
a notch's depth manually it shows as **Custom** — picking a preset
snaps back to that preset's values.

### What does cascade actually do?

A 2-stage cascade runs the same biquad twice in sequence with
half the depth per stage.  At the center the result is the same
total depth as a 1-stage notch at full depth — but the **shape**
differs:

- **More stages = sharper shoulders inside the kill region**:
  cleaner attenuation across the operator-set width.
- **More stages = faster fall-off outside the kill region**:
  less passband disturbance immediately past the notch edges.

That's why **Surgical** (4-stage) is genuinely surgical: deep and
flat within ±width/2 of center, near-transparent the moment you
step outside.  The trade-off: at very narrow widths Surgical can
sound "too cleaned up" — Normal usually feels more natural for
voice work.

## Width presets

The right-click menu's **Default width for new notches ▸** submenu
sets the width newly-placed notches start at.  Existing notches
keep their individual widths.

| Width | Use case |
|--------:|:---------|
| **20 Hz**  | Pinpoint single tone (CW carrier, beacon, single FT8 lane) |
| **50 Hz**  | Surgical CW carrier kill, narrow heterodyne |
| **80 Hz**  | Covers FT8 / FT4 (47 Hz spread) in one notch |
| **150 Hz** | RTTY pair, drifty CW signal |
| **300 Hz** | Broadband heterodyne, splatter from a strong adjacent SSB |
| **600 Hz** | Blanket of QRM, AM-broadcast bleed within passband |

Default is **40 Hz** in v0.0.7.1 (was 80 Hz pre-v0.0.7.1).  At
typical heterodyne center frequencies (1-3 kHz) this gives Q ≈ 25-75
— narrow enough to surgically remove a whistle without taking out
adjacent voice content.

## Default profile for new notches

The right-click menu also exposes **Default profile for new notches
▸** with the same Normal / Deep / Surgical choices.  Pick "Surgical"
once and every notch you place will start with that profile until
you change it.

## Saved notch banks

You can save your current notch setup under a name and reload it
later.  Right-click on the spectrum and pick **Notch banks (saved
presets) ▸**:

- **Save current bank as...** — dialog asks for a name.  The
  current notches (each with its width / depth / cascade / active
  flag) are saved to disk.  If a bank by that name already exists
  it'll prompt before overwriting.
- **Load saved bank ▸** — submenu listing every saved bank.  One
  click replaces the current notches with the saved ones.
- **Delete saved bank ▸** — submenu with per-bank confirm dialogs.

Banks persist across Lyra restarts (stored in QSettings).

Use cases:

- "My 40m setup" — your usual 7.250 MHz broadcast suppression
  notches, ready to load when you tune to 40m.
- "Contest weekend" — every notch you've placed during a busy
  contest, ready next contest.
- "Local QRM template" — your station's known-bad frequencies
  (neighborhood plasma TV, switching supply, etc.).

There is no auto-load on band change — the operator's call which
bank to recall.  Keeps the feature predictable and avoids surprise
notches appearing when you tune across a band edge.

## Visualization

Each notch renders as a **filled red rectangle** spanning its full
−3 dB-from-peak bandwidth, with a thin red center line for precise
targeting.  The rectangle appears identically on the panadapter and
the waterfall.

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
  frequency using the current default width and default profile.
- **Disable / Enable this notch** — appears only when right-clicking
  near an existing notch.  Toggles its active flag without removing
  the placement (great for A/B testing whether a notch is helping).
- **Notch profile ▸** — Normal / Deep / Surgical for this specific
  notch (described above).
- **Remove nearest notch** — deletes the closest existing notch.
- **Clear ALL notches** — removes every notch in one shot.
- **Default width for new notches ▸** — width preset submenu.
- **Default profile for new notches ▸** — preset profile submenu.
- **Notch banks (saved presets) ▸** — save / load / delete named
  notch banks.
- **Disable Notch Filter** — quick off-switch.

**Shift + right-click** (NF must be on) is a fast "remove nearest"
gesture — same as the menu's Remove-nearest action but skips the
menu.  Preserved for operators who learned it from other SDR
clients.

## Identifying a notch — hover callout

Hovering the cursor over any notch raises a tooltip showing the
notch's frequency, current width, and active flag:

```
Notch  7.0741 MHz
Width  80 Hz
```

The cursor also changes to a vertical-resize shape so the operator
knows that the notch is draggable in the vertical direction.

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
  to bypass without removing, plus the profile submenu.

Slider drags and wheel ticks now use a **5 ms two-filter crossfade**
under the hood, so the audio doesn't tick on every drag step (the
v0.0.7 pre-fix behavior was to rebuild the IIR cold every change,
which left an audible click trail during fast dragging).

## Carrier-on-VFO (DC) handling

When you click *exactly* on the VFO center to notch a carrier
sitting at DC baseband (WWV, an AM station tuned in zero-beat),
the standard narrow-notch math degenerates near DC.  Lyra detects
this case and automatically switches to a **4th-order Butterworth
high-pass** for the DC region.  The visible rectangle still
represents the kill region; the operator doesn't need to know the
underlying filter type changed.

In DC-blocker mode, Depth and Profile have no effect (Butterworth
order is fixed at 4); the kill is always ~24 dB/octave roll-off
below the corner.  Adequate for typical carrier-suppression use.

## Front-panel notch counter

The DSP + Audio panel shows a compact counter next to the **NF**
button:

```
NF   3 notches  [50, 80, 200 Hz]
```

Hovering the NF button or the counter shows a tooltip with the
gesture summary.

## Multi-notch

Unlimited notches in theory; practically ~10 before CPU matters.
Each notch is an independent stateful IIR cascade operating at the
audio sample rate (48 kHz).  Cost per active notch: ~0.06 ms /
audio block at cascade=2 — well below the 21 ms block budget.

## Notch + AGC

Notches run **post-demod, pre-AGC**.  Removing a heterodyne with a
notch immediately drops AGC drive — useful when a loud birdie is
pumping AGC and choking the signal you actually want.

If AGC is on, AGC will also compensate when a notch removes a
strong signal — the audio level may stay similar even when a notch
is doing its job.  To verify a notch is actually attenuating, switch
AGC off briefly: with AGC off, the notched signal should clearly
drop in level when the notch is added or enabled.

## Persistence

- **Active notches** are per-session — closing Lyra clears them.
- **Saved banks** persist across restarts via QSettings.  Save your
  setup as a bank if you want it back next session.

## What changed in v0.0.7.1

In short: **notches finally kill carriers as well as the
visualization suggests.**

- Old notches used `scipy.signal.iirnotch` — infinite depth at the
  exact center but only **−3 dB at the visible width edges**.  Most
  of the kill region leaked.
- New notches use a **parametric peaking-EQ biquad** with
  operator-controllable depth (default −50 dB).  Attenuation is
  uniform-ish across the kill region.
- New **cascade** parameter (1-4 stages) replaces the binary "deep"
  toggle.  More stages = sharper shoulders.
- New **3-preset profile** (Normal / Deep / Surgical) bundles
  depth + cascade into one-click choices.
- New **two-filter crossfade** during coefficient swaps eliminates
  the drag-tick clicks that the pre-fix code introduced on every
  parameter change.
- New **operator-named saved banks** ("My 40m setup") via
  right-click submenu.

For the design rationale and bench numbers see
`docs/architecture/notch_v2_design.md`.

## What's NOT in v0.0.7.1

- **Per-stage depth tuning** (asymmetric notches with different
  depths per cascade stage).  Out of scope.
- **Band-aware auto-load** of saved banks (operator-named only).
  Operator chooses which bank to load — keeps the feature
  predictable.
- **Settings → Notches tab** with depth + cascade sliders.  Right-
  click presets cover most uses; if you need truly custom depth /
  cascade values, a future commit will add the sliders.
