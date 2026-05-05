# AGC — Automatic Gain Control

## What it does

AGC keeps the audio output at a consistent level despite signal
fluctuation.  Lyra's AGC engine (since v0.0.9.3) is a Python port
of the WDSP **wcpAGC** reference implementation by Warren Pratt
NR0V — the same look-ahead, state-machine, soft-knee design used
by every serious openHPSDR-class SDR client.  Architecturally:

- **Look-ahead ring buffer** delays output by ~4 ms so attack
  ramps complete BEFORE a loud sample reaches the speaker — no
  "blast" on transients (CW dits, lightning crashes, signal
  arrival on a quiet band).
- **5-state state machine** separates attack, fast-decay (post-
  pop transient recovery), hang, normal decay, and hang-decay
  regimes so each behaves correctly without one bleeding into
  another.
- **Soft-knee compression curve** keeps the gain change smooth
  around the threshold (no audible discontinuity on signals
  riding the knee, like SSB voice envelopes).
- **Hang threshold** lets hang state engage only on real signals
  above background — noise alone never triggers hang, so the
  noise floor stays smooth.

Pre-v0.0.9.3 Lyra used a simpler single-state peak tracker that
exhibited several limitations the WDSP design eliminates by
construction (scratchy noise floor, post-impulse audio mute, no
look-ahead handling of transients).  The v0.0.9.2 → v0.0.9.3 swap
is a one-way upgrade — no operator-facing API changes, just
better-sounding audio across the board.

## Profiles

The **AGC** cluster on the DSP & AUDIO panel shows the active profile.
**Right-click** the cluster to change profile without opening Settings.

| Profile | Decay (τ)       | Hang time  | Use                              |
|---------|------------------|------------|----------------------------------|
| **Off**  | —               | —          | Volume scales raw demod output  |
| **Fast** | 50 ms           | 0          | CW, weak signals                |
| **Med**  | 250 ms          | 0          | SSB / ragchew (default)         |
| **Slow** | 500 ms          | 1 s        | DX nets, steady AM broadcast    |
| **Long** | 2 s             | 2 s        | Steady-carrier listening, beacon work |
| **Auto** | same as Med     | same as Med | (auto-threshold tracking is parked in v0.0.9.3) |
| **Cust** | UI-tracked      | UI-tracked | Persisted slider values; future Settings panel will route them through to WDSP knobs |

Time constants come straight from the WDSP reference (Pratt's
SetRXAAGCMode), so behavior is consistent with what operators
have been using on Thetis and PowerSDR-class clients for over a
decade.

Label color on the panel tells you which mode is active at a glance:

- **Gray** = Off
- **Amber** = Fast / Med / Slow (static)
- **Cyan** = Auto (actively tracking)
- **Magenta** = Cust (user parameters)

## Threshold

The **thr** value (in dBFS) is the target audio level AGC aims to
hold signals at.  Operator-tunable via Settings → DSP → Threshold
slider.

**Auto-calibrate** — picking **Auto** profile previously re-sampled
the threshold every 3 seconds against a tracked noise floor.  In
v0.0.9.3 (with the WDSP engine swap) the auto-tracking is currently
a no-op — Auto behaves the same as Medium.  A future Settings
panel will expose the WDSP-equivalent **hang threshold** parameter
which provides the same operator outcome (signal-above-noise
discrimination) using WDSP's own internal mechanisms.

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

The **Custom** profile in v0.0.9.3 is a UI-state holdover from the
legacy AGC engine — Release and Hang sliders persist their values,
but the WDSP engine doesn't currently consume them (it uses its
own canonical seconds-form parameters per mode preset).  Selecting
"Custom" produces the same audio behavior as Medium for now.

A future Settings panel will route the operator-facing knobs
(Attack ms / Decay ms / Hang ms / Hang threshold) directly to WDSP
parameters so Custom regains its full operator control.

## Tips

- **Pumping on FT8?** — Slow profile. FT8 bursts decay cleanly with a
  long hang.
- **CW echo / distortion?** — Fast profile. Let each dit/dah settle.
- **AM broadcast fading?** — Slow profile + manual threshold.
- **Stronger station punches through?** — Auto profile; it'll adjust.
