# AGC — Automatic Gain Control

## What it does

AGC keeps the audio output at a consistent level despite signal
fluctuation. Lyra's AGC is a **peak-tracker with hang time**: the
envelope peak is captured instantly (attack = 0), and released toward
zero at a profile-specific rate after an optional hang period.

## Profiles

The **AGC** cluster on the DSP & AUDIO panel shows the active profile.
**Right-click** the cluster to change profile without opening Settings.

| Profile | Decay (τ)       | Hang time  | Use                             |
|---------|------------------|------------|----------------------------------|
| **Off**  | —               | —          | Volume scales raw demod output  |
| **Fast** | ~120 ms         | ~130 ms    | CW, weak signals                |
| **Med**  | ~250 ms         | 0          | SSB / ragchew (default)         |
| **Slow** | ~500 ms         | ~1 s       | DX nets, steady AM broadcast    |
| **Auto** | same as Med     | same as Med | Threshold tracks noise floor    |
| **Cust** | user-defined    | user-defined | Set release + hang in Settings |

Decay constants were calibrated against Thetis 2.10.3.13 / WDSP
wcpAGC reference values so recovery speeds feel similar between
the two clients. Fast keeps a short hang (~130 ms) because Lyra's
peak-tracker AGC is simpler than Thetis's WDSP implementation
(which has additional attack-envelope and hang-index smoothing) —
without that hang, Fast would pump on AM voice envelopes. Med has
zero hang for fluid SSB recovery, and Slow keeps a 1 s hang for
steady-carrier listening (AM broadcast, DX nets). The original
Lyra profiles had release coefficients ~20× too slow which made
audio stay clamped for many seconds after a peak — that's been
corrected.

Label color on the panel tells you which mode is active at a glance:

- **Gray** = Off
- **Amber** = Fast / Med / Slow (static)
- **Cyan** = Auto (actively tracking)
- **Magenta** = Cust (user parameters)

## Threshold

The **thr** value (in dBFS) is the target audio level AGC aims to hold
signals at. Below threshold → no gain reduction. Above threshold →
AGC reduces gain proportionally.

**Manual set** — Settings → DSP → Threshold slider.

**Auto-calibrate** — pick **Auto** profile. The threshold is set to
roughly 18 dB above the current noise floor, then re-sampled every
3 seconds so it follows changing band conditions automatically.

## Live gain readout

The **gain** value next to the threshold shows the current AGC gain
action in dB, color-coded by magnitude:

- **Green** — |gain| < 3 dB (AGC barely working)
- **Amber** — 3 – 10 dB (normal operation)
- **Red**   — > 10 dB (hitting hard — strong signal or heavy expansion
  on a very weak one)

The number tracks peak-hold-with-decay so it stays readable on fast
signals (UI refresh ~6 Hz, updated from every demod block internally).

## Front-panel controls

The **AGC** cluster on the [**DSP & AUDIO** panel](panel:dsp) shows,
left to right (click the panel link to flash it in the main window):

```
AGC  <PROFILE>  thr <-NN dBFS>  gain <±N.N dB>
```

- **Left-click digits / labels** — no action (read-only display).
- **Right-click** anywhere on the cluster — pops a profile menu:
  Off / Fast / Med / Slow / Auto / Custom. Checked radio = current
  profile.
- **Profile label color** tells you mode at a glance:
  gray (Off), amber (Fast/Med/Slow), cyan (**Auto** = tracking),
  magenta (**Cust** = your parameters in effect).

Deeper configuration — Custom release/hang, manual threshold slider,
full label/tooltip layout — lives on **DSP Settings…** (the button
on the right side of the DSP & AUDIO panel, or File → DSP… in the
menubar).

## Custom profile

Open **DSP Settings → AGC** and pick **Custom**. Release and Hang
sliders become active:

- **Release** — 0.001 .. 0.100 per block (lower = slower release)
- **Hang** — 0 .. 100 blocks (1 block ≈ 43 ms at 48 kHz)

The moment you move a slider, the profile switches to **Cust** and
the front-panel label turns magenta.

## Tips

- **Pumping on FT8?** — Slow profile. FT8 bursts decay cleanly with a
  long hang.
- **CW echo / distortion?** — Fast profile. Let each dit/dah settle.
- **AM broadcast fading?** — Slow profile + manual threshold.
- **Stronger station punches through?** — Auto profile; it'll adjust.
