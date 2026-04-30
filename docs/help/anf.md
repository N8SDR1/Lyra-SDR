# Auto Notch Filter (ANF)

Lyra's ANF is an **LMS adaptive predictor** that surgically nulls
narrow tonal interference from your audio — heterodynes, BFO
whistles, single-frequency carriers, RTTY spurs, BFO leakage from
nearby strong signals, intermodulation tones. Operator turns it
on, walks away; the filter learns whatever tones are present and
keeps them nulled without taking out genuine speech.

## How it differs from manual notches

- **Manual notches** ([notches.md](./notches.md)) — operator
  places a notch at a specific frequency. You see the notch on
  the spectrum, you control the width and depth, and it stays
  exactly where you put it. Use these for **known, persistent**
  carriers.

- **Auto Notch Filter (this)** — operator just turns it on. ANF
  finds and nulls *whatever* tones it can predict, including
  ones that drift over time. Use this for **unknown** tones, or
  when you don't want to bother manually placing a notch for
  every transient hetorodyne.

You can run both simultaneously. Manual notches catch the
specific carriers you've identified; ANF mops up everything else.

## How it differs from NR

- **NR** ([nr.md](./nr.md)) — broadband noise reduction. Works
  on all audio frequencies at once. Doesn't surgically remove
  tones; just reduces noise level statistically.

- **ANF** — narrowband, surgical. Removes specific tonal energy
  from the audio without affecting broadband content (speech,
  noise floor).

ANF and NR are often run together: ANF first to remove the
tones, then NR to reduce the broadband residual.

## Toggling on/off

The **ANF** button on the [DSP & AUDIO panel](panel:dsp).

- **Left-click** — toggle between Off and your last non-Off
  profile (default Standard)
- **Right-click** — pick a profile (Off / Gentle / Standard /
  Aggressive / Custom)

Lit = ANF engaged; dim = bypass.

## Profiles

| Profile | μ (adapt rate) | Use it for |
|---|---|---|
| **Off** | — | Bypass; cheapest path |
| **Gentle** | 5×10⁻⁵ | Slow lock — only catches prolonged steady tones. Best when you're listening for transient signals (CW, FT8) and don't want ANF interfering with the signal of interest. |
| **Standard** *(default)* | 1.5×10⁻⁴ | Balanced — typical heterodyne is gone in ~200 ms without chewing on speech consonants. The sweet spot for SSB voice. |
| **Aggressive** | 4×10⁻⁴ | Fast lock — locks onto any tonal energy quickly. May briefly null short speech tones (vowel formants) but recovers in a few hundred ms. Good for very busy bands with multiple carriers. |
| **Custom** | operator-set | Hand-tune via Settings → Noise → μ slider (log scale, 10⁻⁵ to 10⁻³). |

The μ ("mu") parameter is the adaptation step size. Higher μ =
faster lock onto new tones but noisier residual; lower μ =
slower lock but cleaner output.

## How to dial it in

Start with **Standard**. On a quiet band you should hear no
audible difference. On a band with hetorodynes or RTTY spurs,
the tones should fade out within ~200 ms of turning ANF on.

If ANF seems slow to lock onto an obvious tone:
- Try **Aggressive** — faster adapt rate
- Or pick **Custom** and dial μ above 5×10⁻⁴

If ANF seems to be eating speech (consonants sounding muffled,
vowels briefly dipping):
- Try **Gentle** — slower adapt rate, tones take longer to learn
  but speech is left alone
- Or pick **Custom** and dial μ below 1×10⁻⁴

## Position in the audio chain

ANF runs AT 48 kHz audio rate, post-demod, between the
demodulator output and the broadband NR processor:

```
IQ → NB → decimate → notches (manual) → demod → ANF → NR → APF
```

Rationale (canonical ham-SDR noise-toolkit chain):

1. **NB** removes IQ-domain impulses (pre-decimation so they
   stay narrow).
2. **Manual notches** zap KNOWN carriers operator has placed.
3. **Demod** does its thing.
4. **ANF** catches UNKNOWN tones the operator didn't manually
   notch.
5. **NR** sees a tone-free residual, so its broadband noise-
   floor estimator isn't fooled by tonal energy.
6. **APF** sharpens the CW pitch (CW only).

## Settings → Noise → Auto Notch Filter

The Noise tab in Settings exposes:

- **Profile** — same Off / Gentle / Standard / Aggressive /
  Custom choices as the DSP-row right-click menu, with radio-
  button UX.
- **μ slider** — 10⁻⁵ to 10⁻³, logarithmic scale, 0.1-step
  precision. Active only when Custom is selected; presets show
  their value but greyed.

Settings persist across Lyra restarts via QSettings keys
`noise/anf_profile` and `noise/anf_mu`.

## Internals (for the curious)

ANF is a leaky-LMS adaptive predictor. For each audio sample at
48 kHz, the filter:

1. Predicts the current sample from a window of past samples
   (one delay-line lookup per tap, 64 taps).
2. Computes the residual error: `e[n] = x[n] − ŷ[n]`.
3. Outputs the residual as the audio sample.
4. Updates the weight vector based on the error and the input
   window (NLMS — normalized step size — so adaptation rate is
   independent of signal amplitude).
5. Applies leakage to the weights so they don't drift on
   stationary input.

Tones are highly predictable from past samples (a sinusoid is
fully determined by any few of its prior samples), so they end
up in `ŷ` and not in `e`. Speech and noise are not predictable
across short windows, so they survive in `e` largely intact.

State preserved across audio blocks: a circular delay-line buffer
of the last (delay + n_taps) samples, plus the adaptive weight
vector. `reset()` zeros both — called on freq/mode changes
where any tones the filter learned belong to a band you've
left.

Implementation: `lyra/dsp/anf.py` (`AutoNotchFilter` class).
Pure Python per-sample loop with locally-cached state — at 48
kHz audio rate this runs in well under a millisecond per
2048-sample block.

## What ANF doesn't do

- **Wide tones / chirps / FM signals** — ANF is a narrow-band
  predictor; it can null pure sinusoids and slowly-drifting
  tones, but it won't null wide-bandwidth signals (an SSB voice
  off in the passband, an AM carrier with audio modulation, an
  FM signal). Use [notch filters](./notches.md) or filter
  bandwidth instead.
- **Broadband noise** — ANF only nulls predictable structure.
  Noise is unpredictable. Use [NR](./nr.md) for that.
- **Impulsive interference** — ANF works on tones, not impulses.
  Impulses look like white noise to the LMS predictor. Use
  [NB](./nb.md) for impulse blanking.
- **The signal you're listening to** — sometimes. CW signals
  are tones too; ANF can null fast CW dits if μ is high enough.
  Use Gentle profile (or Off) when listening to CW you actually
  want to hear; use [APF](./apf.md) instead to *boost* CW at
  pitch.

## ANF + manual notches + NR — order of operations

The audio chain runs (in order):

1. **NB** (IQ-domain impulse blanker)
2. **Decimator** (input rate → 48 kHz)
3. **Manual notches**
4. **Demodulator** (mode-specific)
5. **ANF** ← this filter
6. **NR**
7. **APF** (CW only)

Each step's job is clear: kill localized impulses → kill known
carriers → demodulate → kill unknown tones → reduce broadband
residual → boost CW pitch.

## Tips by mode

- **SSB voice** — Standard is right for most situations. The
  filter locks on in ~200 ms which means brief tones disappear
  almost as soon as they appear; speech consonants survive.
  Switch to Gentle if the band is very chirpy and ANF is causing
  audible dropouts on speech transients.
- **CW** — be careful. CW signals are tones. ANF in Aggressive
  mode WILL null your CW dits if it's listening for predictable
  energy. Either run Gentle (slow lock — fast keying outpaces
  the adapt rate) or run Off and use [APF](./apf.md) to *boost*
  the CW pitch instead.
- **FT8 / digital** — Off is usually fine; the decoder doesn't
  care about narrow tones in its window. ANF won't hurt either,
  though.
- **AM broadcast** — Standard. AM carriers ARE the tone we want
  to hear, but they're constant DC after demodulation; ANF
  doesn't see them as predictable narrow audio energy. The
  audio modulation passes through. Heterodynes and adjacent-
  channel beats get nulled.
- **FM** — Off is usually fine. FM demod produces broadband
  audio with no narrow tones to null.

## When NOT to use ANF

- You're listening to a tone you actually want to hear (CW
  signal of interest, AM tuning whistle, single-tone
  identification beacon). Turn ANF off or use Gentle and
  carefully verify it's not eating your signal.
- Band is very quiet — ANF isn't doing any work and just adds
  a tiny bit of computational overhead. Off is the cheapest
  path.
