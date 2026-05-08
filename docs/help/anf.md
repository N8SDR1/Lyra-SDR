# Auto Notch Filter (ANF)

ANF is an **adaptive notch** that surgically nulls narrow tonal
interference from your audio — heterodynes, BFO whistles,
single-frequency carriers, RTTY spurs, intermodulation tones.
Operator turns it on, walks away; ANF learns whatever tones are
present and keeps them nulled without taking out genuine speech.

The live ANF runs inside WDSP (it's a sibling of WDSP's EMNR
noise reducer in the RXA chain).  Lyra exposes it as a single
on/off control with a profile menu retained for forward
compatibility.

## How it differs from manual notches

- **Manual notches** ([notches.md](./notches.md)) — operator
  places a notch at a specific frequency.  You see the notch on
  the spectrum, you control the width, and it stays exactly
  where you put it.  Use these for **known, persistent**
  carriers.

- **Auto Notch Filter (this)** — operator just turns it on.  ANF
  finds and nulls *whatever* tones it can predict, including
  ones that drift over time.  Use this for **unknown** tones,
  or when you don't want to bother manually placing a notch
  for every transient heterodyne.

You can run both simultaneously.  Manual notches catch the
specific carriers you've identified; ANF mops up everything
else.

## How it differs from NR

- **NR** ([nr.md](./nr.md)) — broadband noise reduction.  Works
  on all audio frequencies at once.  Doesn't surgically remove
  tones; just reduces noise level statistically.

- **ANF** — narrowband, surgical.  Removes specific tonal
  energy from the audio without affecting broadband content
  (speech, noise floor).

ANF and NR play well together: ANF removes the discrete tonal
artifacts; NR cleans up the broadband residual.

## Toggling on/off

The **ANF** button on the [DSP & AUDIO panel](panel:dsp).

- **Left-click** — toggle between Off and your last-used
  profile (default Medium).
- **Right-click** — pick a profile (Off / Light / Medium /
  Heavy / Custom).

Lit = ANF engaged; dim = bypass.

## Profile menu

| Profile | Meaning today | Notes |
|---------|--------------|-------|
| **Off**       | ANF bypassed                        | Cheapest path |
| **Light**     | ANF on (mild adaptation persisted)  | Currently same audio behavior as Medium |
| **Medium** *(default)* | ANF on (standard adapt)    | The comfortable default |
| **Heavy**     | ANF on (aggressive adapt persisted) | Currently same audio behavior as Medium |
| **Custom**    | ANF on (operator μ persisted)       | Currently same audio behavior as Medium |

> **Status (v0.0.9.6):** WDSP's ANF is a single on/off engine.
> The profile names and the μ slider in Settings → Noise are
> persisted across restarts as operator preference, but they
> currently all map to the same WDSP enable.  A future build
> will route the persisted μ to a WDSP adapt-rate parameter so
> the profiles regain audible distinction.  In the meantime
> the operator-facing UI is preserved so saved settings carry
> forward when the wiring lands.

For day-to-day use the simplest mental model is "**ANF on**" or
"**ANF off**".  Pick a profile if you want to make sure your
preference (Light / Medium / Heavy) is remembered for when the
profiles become distinct again.

## When ANF helps

- **Steady heterodyne whistles** — broadcast bleed, BFO leakage
  from nearby strong signals.  ANF locks onto them quickly and
  the whistle goes silent.
- **RTTY mark/space spurs** — ANF tracks both tones and nulls
  them, leaving voice behind cleaner than a manual notch pair.
- **Intermod products** — those mysterious birdies that aren't
  on any known frequency but stay put.

## When ANF hurts

- **CW dits and dahs** — these are tones too.  An overly
  aggressive ANF can lock onto your CW signal and null the very
  thing you're trying to hear.  For CW: turn ANF off, or use
  the [APF](./apf.md) instead.
- **FT8 / data tones** — same problem.  Disable ANF for digital
  modes that rely on tonal content carrying meaning.

## Position in the audio chain

ANF runs inside WDSP's RXA chain alongside the noise reducer.
For operator mental-model purposes:

```
IQ → notches → demod → NR → ANF → AGC → APF (CW) → audio out
```

NR cleans broadband noise; ANF mops up the residual narrow
tones; AGC does its level work last so neither stage fights it.

## Settings → Noise → Auto Notch Filter

The Noise tab in Settings exposes:

- **Profile** — same Off / Light / Medium / Heavy / Custom
  choices as the DSP-row right-click menu, with radio-button
  UX.
- **μ slider** — adapt-rate control on a log scale.  Active
  only when Custom is selected.  Currently advisory (see the
  status note above).

These settings persist across Lyra restarts.

## Quick recipes

- **Heterodyne on a clear band** — ANF Medium, leave it.
- **CW operating** — ANF Off.  Use [APF](./apf.md) for tone
  enhancement instead.
- **FT8 / digital** — ANF Off.  The decoder benefits from
  unfiltered tonal content.
- **Crowded contest band with random birdies** — ANF Heavy
  (currently same as Medium; will be more aggressive once the
  profile mapping lands).
