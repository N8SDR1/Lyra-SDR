# Noise Blanker (NB)

Lyra's NB is an **IQ-domain impulse blanker**.  It sits at the
front of WDSP's RXA chain, before bandpass filtering and
demodulation, and surgically removes narrow time-domain
impulses from the IQ stream.  Unlike NR (which works on the
audio after it's already been spread across the passband), NB
catches impulses while they're still localized in time.

The live blanker runs inside WDSP — Lyra's NB button drives
WDSP's noise-blanker run flag and profile selection.

## What NB targets

NB is the right tool for:

- **Vehicle ignition noise** — sharp spikes from spark plugs
  in cars, lawn mowers, ATVs, snowmobiles
- **Power-line crashes** — arcing insulators, faulty
  transformers, intermittent line-tap connections
- **Lightning crashes** — distant atmospheric strikes
  (thunderstorms can be hundreds of miles away yet still wreck
  reception on HF)
- **Switching power supply hash** — cheap LED bulbs, wall
  warts, PV inverters, battery chargers
- **Brush-motor whine** — refrigerator compressors, hair
  dryers, vacuums
- **Plasma TVs / monitors** — buzzing across the band

NB is **not** the right tool for:

- **Steady band noise / hiss** — that's [NR](./nr.md)'s job
- **Specific carriers / heterodynes** — use
  [notch filters](./notches.md) or [ANF](./anf.md)
- **Wide-band crud that's continuously above the noise
  floor** — no impulse blanker can fix that; it's not
  impulsive

## Toggling on/off

The **NB** button on the [DSP & AUDIO panel](panel:dsp).

- **Left-click** — toggle between Off and your last non-Off
  profile (default Medium)
- **Right-click** — pick a profile (Off / Light / Medium /
  Heavy / Custom)

Lit = NB engaged; dim = bypass.

## Profile menu

| Profile | Meaning today | Notes |
|---------|---------------|-------|
| **Off**       | NB bypassed                  | Cheapest path |
| **Light**     | NB on (gentle threshold)     | Currently same audio behavior as Medium |
| **Medium** *(default)* | NB on                | The comfortable default |
| **Heavy**     | NB on (aggressive threshold) | Currently same audio behavior as Medium |
| **Custom**    | NB on (operator-set persisted) | Currently same audio behavior as Medium |

> **Status (v0.0.9.6):** WDSP's noise blanker is a single
> on/off engine.  The profile names and the threshold slider
> in Settings → Noise are persisted across restarts as
> operator preference, but they currently all map to the same
> WDSP enable.  A future build will route the persisted
> threshold to a WDSP parameter so the profiles regain audible
> distinction.  In the meantime the operator-facing UI is
> preserved so saved settings carry forward when the wiring
> lands.

For day-to-day use the simplest mental model is "**NB on**" or
"**NB off**".  Pick a profile if you want your preference
remembered for when the profiles become distinct.

## How to dial it in

Start with **Medium** (or just NB on).  Listen for a minute on
a band you know has some impulse activity.  You should hear a
clear reduction in the "sandpaper" character of the noise;
legitimate signals should be unaffected.

If the impulse cleanup isn't enough or NB is doing too much
(rare, since the WDSP default is well-tuned), it's currently
an "all or nothing" choice.  Fine-grained tuning returns when
the threshold mapping lands.

## Why pre-decimation matters

Lyra's IQ chain decimates the HL2's input rate (96k / 192k /
384k) down to 48 kHz inside WDSP before demodulation.  An
impulse at the input rate is typically only a few samples
wide.  After decimation, the same energy gets spread across
many more output samples by the filter — by the time the demod
sees it, you can't tell which audio samples are impulse and
which are signal.

Running NB at the input rate, before decimation, keeps
impulses narrow and easy to detect.  This is the canonical
HF-receiver-design position for an impulse blanker; it's
where every effective hardware NB has lived since the 1960s,
and it's where WDSP runs its blanker too.

## Settings → Noise → Noise Blanker

The Noise tab in Settings exposes:

- **Profile** — same Off / Light / Medium / Heavy / Custom
  choices as the DSP-row right-click menu, with radio-button
  UX.
- **Threshold slider** — operator-tunable advisory value
  (currently persisted but not pushed to WDSP — see the
  status note above).

Settings persist across Lyra restarts.

## What NB doesn't do

- **Continuous broadband noise** — NB only acts on samples
  that exceed the threshold.  Steady noise doesn't trigger
  it.  Use [NR](./nr.md) for that.
- **Specific carriers / heterodynes** — NB doesn't know about
  frequency content.  Use [notch filters](./notches.md) or
  [ANF](./anf.md).
- **Audio-domain artifacts** — NB runs pre-demod; if your
  speakers/headphones have hum or distortion, NB won't help.
- **Steady birdies from your own PC** — those can sometimes
  be stronger than NB's threshold expects, and you'll get
  persistent blanking that audibly thins the audio.  Better
  fix: notch the birdie, or move the offending hardware.

## NB + NR + notches — order of operations

WDSP runs the chain (conceptually):

```
IQ → NB → notches → demod → NR → ANF → AGC → APF (CW) → audio out
```

NB kills localized impulses first (they'd otherwise spread
during decimation), then manual notches kill known carriers,
then demod runs at 48 kHz audio rate, and the broadband NR /
ANF stages clean up the residual.

## Tips by mode

- **SSB voice** — NB on for most situations.  Off if you're
  hearing speech artifacts.
- **CW** — be careful with NB on fast keying.  The leading
  edge of a strong dit can trigger the blanker if your
  background is much quieter than the signal.  Test on real
  signals; turn off if you hear weird "chip" artifacts on dits.
- **AM broadcast** — NB on.  Power-line and switching-supply
  hash is what you'll typically be fighting on the BC bands.
- **FT8 / digital** — NB on.  FT8 decoders cope well with
  mild blanking artifacts; eliminating impulse crud is usually
  a net win for decode rate on noisy bands.
- **FM** — NB is less critical for FM (the FM demodulator
  already rejects amplitude modulation) but it's still useful
  at the margins, especially for weak-signal FM.  NB on.
