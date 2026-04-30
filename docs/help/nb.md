# Noise Blanker (NB)

Lyra's NB is an **IQ-domain impulse blanker**. It sits at the very
front of the audio chain, before bandpass filtering and demodulation,
and surgically removes narrow time-domain impulses from the IQ
stream. Unlike NR (which works on the audio after it's already
been spread across the passband), NB catches impulses while they're
still localized in time.

## What NB targets

NB is the right tool for:

- **Vehicle ignition noise** — sharp spikes from spark plugs in
  cars, lawn mowers, ATVs, snowmobiles
- **Power-line crashes** — arcing insulators, faulty transformers,
  intermittent line-tap connections
- **Lightning crashes** — distant atmospheric strikes (thunderstorms
  can be hundreds of miles away yet still wreck reception on HF)
- **Switching power supply hash** — cheap LED bulbs, wall warts,
  PV inverters, battery chargers
- **Brush-motor whine** — refrigerator compressors, hair dryers,
  vacuums
- **Plasma TVs / monitors** — buzzing across the band

NB is **not** the right tool for:

- **Steady band noise / hiss** — that's [NR](./nr.md)'s job
- **Specific carriers / heterodynes** — use
  [notch filters](./notches.md) or (when shipped) ANF
- **Wide-band crud that's continuously above the noise floor** —
  no impulse blanker can fix that; it's not impulsive

## Toggling on/off

The **NB** button on the [DSP & AUDIO panel](panel:dsp).

- **Left-click** — toggle between Off and your last non-Off
  profile (default Medium)
- **Right-click** — pick a profile (Off / Light / Medium /
  Aggressive / Custom)

Lit = NB engaged; dim = bypass.

## Profiles

| Profile | Threshold | Use it for |
|---|---|---|
| **Off** | — | Bypass; cheapest path |
| **Light** | 12× background | Lightning crashes, obvious high-energy spikes only. Lowest risk of clipping legitimate signal transients. |
| **Medium** *(default)* | 6× background | Typical ignition + power-line + supply hash. Most operators leave it here. |
| **Aggressive** | 3× background | Subtle impulse activity too — but more likely to clip the leading edge of fast CW dits or sharp keying transients. |
| **Custom** | operator-set | Hand-tune via Settings → Noise → Threshold slider. |

The threshold is a multiplier on a 20 ms exponentially-smoothed
**background-power reference**. When an IQ sample's instantaneous
power exceeds `threshold × background`, that sample is flagged as
an impulse and replaced with the most recent clean sample
("hold-last-clean"), with a 4-sample cosine-slewed transition at
each edge of the blanked region to avoid creating click artifacts
of our own.

## How to dial it in

Start with **Medium**, listen for a minute on a band you know has
some impulse activity. You should hear a clear reduction in the
"sandpaper" character of the noise; legitimate signals should be
unaffected.

If NB seems to be missing impulses that you can clearly hear:
- Try **Aggressive** — catches lower-energy impulses
- Or pick **Custom** in Settings → Noise → Threshold and dial
  the slider down (4×, 3×, 2× — gentler steps)

If NB seems to be clipping legitimate signal transients
(particularly fast CW dits or AM transient peaks):
- Try **Light** — only catches obvious crashes
- Or pick **Custom** and dial the threshold up (8×, 10×, 15×)

The Custom slider in Settings → Noise → Noise Blanker covers the
full operator-tunable range (1.5× to 50× background).

## Why pre-decimation matters

Lyra's IQ chain decimates the HL2's input rate (96k / 192k / 384k)
down to 48 kHz internally before demodulation. An impulse at the
input rate is typically only a few samples wide. After decimation,
the same energy gets spread across many more output samples by the
filter — by the time the demod sees it, you can't tell which
audio samples are impulse and which are signal.

Running NB at the input rate, before decimation, keeps impulses
narrow and easy to detect. This is the canonical HF-receiver-design
position for an impulse blanker; it's where every effective
hardware NB has lived since the 1960s.

## Settings → Noise → Noise Blanker

The Noise tab in Settings exposes:

- **Profile** — same Off / Light / Medium / Aggressive / Custom
  choices as the DSP-row right-click menu, with radio-button UX.
- **Threshold slider** — 1.5× to 50× background, 0.1× resolution.
  Active only when Custom is selected; presets show their value
  but greyed.

Settings persist across Lyra restarts via QSettings keys
`noise/nb_profile` and `noise/nb_threshold`.

## Internals (for the curious)

NB runs at the operator's chosen IQ input rate (96k / 192k / 384k —
operators can swap rates without restart). Per-sample work:

```
p[n]    = |x[n]|²                           # instantaneous power
bg[n]   = α · bg[n-1] + (1-α) · p[n]        # 1-pole exp-smooth
                                            # background reference
hit[n]  = (p[n] > threshold · bg[n])        # impulse detection
out[n]  = (last_clean if hit[n] else x[n])  # hold-replace
```

Then a cosine-window slew is applied to the few samples on each
side of every contiguous run of hits, so the transitions between
clean and replaced are C¹-smooth (no first-derivative
discontinuity that the bandpass filter would ring on).

A **consecutive-blank cap** (25 ms by default) prevents the
blanker from "locking on" to a continuous strong carrier — once
the cap is hit, the run is forced back to clean and the bg
tracker absorbs the signal so subsequent samples no longer
trigger.

Implementation lives in `lyra/dsp/nb.py` (`ImpulseBlanker` class).
Vectorized via `scipy.signal.lfilter` for the bg tracker plus
NumPy `where`/`maximum.accumulate` for the forward-fill
replacement; only the consecutive-blank-cap walk is in pure
Python (and only runs when impulses are actually detected).

## What NB doesn't do

- **Continuous broadband noise** — NB only acts on samples that
  exceed the threshold. Steady noise doesn't trigger it. Use
  [NR](./nr.md) for that.
- **Specific carriers / heterodynes** — NB doesn't know about
  frequency content. Use [notch filters](./notches.md) or (when
  shipped) ANF.
- **Audio-domain artifacts** — NB runs pre-demod; if your
  speakers/headphones have hum or distortion, NB won't help.
- **Steady birdies from your own PC** — those can sometimes be
  stronger than NB's threshold expects, and you'll get persistent
  blanking that audibly thins the audio. Better fix: notch the
  birdie, or move the offending hardware.

## NB + NR + notches — order of operations

Lyra's audio chain runs (in order):

1. **NB** (this — IQ-domain impulse blanker, pre-decimation)
2. **Decimator** (input rate → 48 kHz)
3. **Notches** (manual frequency-domain notches)
4. **Demodulator** (mode-specific)
5. **NR** (post-demod spectral subtraction)
6. **APF** (audio-rate peaking filter, CW only)
7. **AGC + Volume**

This ordering is canonical: kill localized impulses first (they'd
otherwise spread in step 2), then known carriers in step 3, then
demodulate, then handle the residual broadband noise in step 5.

## Tips by mode

- **SSB voice** — Medium is right for most situations. Light if
  you're hearing speech artifacts; Aggressive if there's
  noticeable impulse crud you'd like gone.
- **CW** — be careful with Aggressive on fast keying. The leading
  edge of a strong dit can trigger the blanker if your background
  is much quieter than the signal. Start at Light and step up
  only if you genuinely hear impulses.
- **AM broadcast** — Medium. Power-line and switching-supply hash
  is what you'll typically be fighting on the BC bands.
- **FT8 / digital** — Medium or Aggressive. FT8 decoders cope
  well with mild blanking artifacts; eliminating impulse crud is
  usually a net win for decode rate on noisy bands.
- **FM** — NB is less critical for FM (the FM demodulator already
  rejects amplitude modulation) but it's still useful at the
  margins, especially for weak-signal FM. Medium.
