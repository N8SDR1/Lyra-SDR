# AGC — Automatic Gain Control

## What it does

AGC keeps audio output at a consistent level despite signal
fluctuation.  Lyra's AGC runs entirely inside the **WDSP** DSP
engine — the same look-ahead, state-machine, soft-knee design
used by Thetis, PowerSDR, and other openHPSDR-class SDR clients.
You're listening to Warren Pratt NR0V's reference implementation
through Lyra's UI.

Key behaviors:

- **Look-ahead ring buffer** delays output by a few milliseconds
  so attack ramps complete BEFORE a loud sample reaches the
  speaker — no "blast" on transients (CW dits, lightning crashes,
  signal arrival on a quiet band).
- **Multi-state state machine** separates attack, fast-decay,
  hang, normal decay, and hang-decay regimes so each behaves
  correctly without one bleeding into another.
- **Soft-knee compression curve** keeps the gain change smooth
  around the threshold (no audible discontinuity on signals
  riding the knee, like SSB voice envelopes).
- **Hang threshold** lets hang state engage only on real signals
  above background — noise alone never triggers hang, so the
  noise floor stays smooth.

## Profiles

The **AGC** cluster on the DSP & AUDIO panel shows the active
profile.  **Right-click** the cluster to change profile without
opening Settings.

| Profile  | Behaviour                                           | Best for                              |
|----------|-----------------------------------------------------|---------------------------------------|
| **Off**  | No automatic gain — Volume scales raw demod output | Digital modes (FT8/FT4/RTTY)         |
| **Fast** | Quick attack/decay, no hang                         | CW, weak-signal work                  |
| **Med**  | Moderate decay, no hang (default)                   | SSB / ragchew                         |
| **Slow** | Longer decay with short hang                        | DX nets, steady AM broadcast          |
| **Long** | Long decay with long hang                           | Beacons, steady-carrier listening     |
| **Auto** | Med time-constants + auto-threshold tracking        | Strong fading signals, condition shifts |
| **Cust** | Persisted UI sliders (advisory only)                | (Future direct-WDSP control)          |

The time constants come from WDSP's canonical mode presets, so
the behaviour is consistent with what operators have heard on
HF SDR applications for over a decade.

Label color on the panel tells you which profile is active at
a glance:

- **Gray** = Off
- **Amber** = Fast / Med / Slow / Long (static)
- **Cyan** = Auto (Med time-constants + threshold-tracks-noise-floor)
- **Magenta** = Cust (UI sliders persisted, advisory only)

## Threshold

The **thr** value (in dBFS) on the panel cluster shows the AGC
threshold — the noise-floor reference WDSP uses to compute
``max_gain`` (the AGC's gain ceiling).  Lower values give more
AGC headroom (boosts weak signals harder); higher values
compress earlier.

* **Default**: -100 dBFS (~70 dB AGC headroom; comfortable for
  normal HF operation).
* **Adjust via**: Settings → DSP → AGC → **Auto** button.  Click
  it on a quiet patch of band; Lyra recalibrates the threshold
  to ~5 dB above the current rolling noise floor so AGC engages
  on real signals while letting noise itself ride through at
  full max_gain.
* **Persistence**: the value persists across Lyra launches.

Operator-typical settings:

| Threshold | Use |
|-----------|-----|
| -130 dBFS | Quiet band, weak-signal / DX hunting |
| -100 dBFS | Normal HF operation (default) |
| -80 dBFS  | Moderate signals, less AGC boost |
| -60 dBFS  | Broadcast / strong-signal listening |

The Settings → DSP → AGC row used to have a 0..1 linear "audio
target" slider; it was removed in v0.0.9.8.1 because that legacy
field had different semantics from WDSP's actual threshold and
was never wired to the engine.  The label readout + Auto button
covers the common cases.  Power users wanting direct dBFS
control can edit the QSettings registry key
``HKCU\Software\N8SDR\Lyra\agc\threshold`` between sessions, or
ask for a slider to be re-added.

## Live gain readout

The **gain** value next to the threshold shows the current AGC
gain action in dB, color-coded by magnitude:

- **Green** — |gain| < 3 dB (AGC barely working)
- **Amber** — 3 – 10 dB (normal operation)
- **Red**   — > 10 dB (hitting hard — strong signal or heavy
  expansion on a very weak one)

The number reads back from WDSP's internal AGC meter at ~6 Hz to
match the panel repaint cadence; per-block reads would only add
overhead the eye can't see.

## Front-panel controls

The **AGC** cluster on the [**DSP & AUDIO** panel](panel:dsp)
shows, left to right (click the panel link to flash it in the
main window):

```
AGC  <PROFILE>  thr <-NN dBFS>  gain <±N.N dB>
```

- **Left-click digits / labels** — no action (read-only display).
- **Right-click** anywhere on the cluster — pops a profile menu:
  Off / Fast / Med / Slow / Long / Auto / Custom. Checked radio =
  current profile.

Deeper configuration — Release / Hang sliders + Threshold
label + Auto button — lives on **DSP Settings…** (the button on
the right side of the DSP & AUDIO panel, or File → DSP… in the
menubar).  The Threshold value is wired to WDSP via the Auto
button (and the persisted default).  The Release / Hang sliders
are still **advisory** in WDSP mode — see "Custom profile"
below.

## Custom profile

The **Release** and **Hang** sliders in DSP Settings are
persisted across restarts but are not currently pushed to the
WDSP engine — WDSP uses its own canonical
seconds-form parameters per mode preset, and Lyra hasn't yet
exposed the WDSP-side knobs (attack ms / decay ms / hang ms /
hang threshold) one-to-one.  Selecting **Custom** today produces
the same audio behavior as **Medium**.

A future Settings panel will route the operator knobs directly
to WDSP parameters so Custom regains full operator control.

## Tips

- **Pumping on FT8?** — Use **Off**. FT8 / FT4 / RTTY want fixed
  gain; AF Gain on the panel is your "station loudness" knob.
- **Pumping on AM with strong fades?** — **Slow** or **Long**
  (Long has the longest decay + hang — good for steady-carrier
  listening like AM broadcast or beacon monitoring).
- **CW echo / distortion?** — **Fast**. Let each dit/dah settle.
- **AM broadcast fading?** — **Slow** + a healthy AF Gain dial.
- **Stronger station punches through?** — **Auto** uses Med
  time-constants and continuously re-calibrates the threshold to
  ~18 dB above the rolling noise floor, so as the band shifts
  the threshold moves with it.
