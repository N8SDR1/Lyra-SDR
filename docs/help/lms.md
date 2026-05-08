# LMS Adaptive Line Enhancer (NR3)

LMS is a **predictive** noise reducer — the inverse of the
spectral NR engine.  Where NR estimates and *subtracts* noise,
LMS estimates and *amplifies* the periodic part of the signal.
Whatever's predictable (CW carriers, voice formants, narrow
tonal content) gets lifted; whatever's unpredictable (broadband
hiss, atmospheric noise) falls out.

This makes LMS the operator's go-to for **weak CW buried in
band hiss** and **stable carrier extraction**.  It's
complementary to NR — running both gives you a "lift the
periodic content + clean the residual hiss" stack that's hard
to beat for weak DX.

The live LMS runs inside WDSP (it's WDSP's ANR — Adaptive Noise
Reduction module).  Lyra exposes it as an on/off button with a
strength slider.

## When to reach for LMS

| Situation                        | LMS | Notes                                       |
|----------------------------------|-----|---------------------------------------------|
| Weak CW DX, S0-S2                | ✅  | Lift the carrier above noise floor          |
| Stable carrier extraction        | ✅  | Recovering a barely-audible beacon          |
| Voice in dense broadband noise   | ⚠   | Helps modestly; NR mode 3 wins for voice    |
| AM broadcast                     | ❌  | Chops the carrier; use NR instead           |
| FM                               | ❌  | FM has no periodic structure to predict     |
| Killing a known stable tone      | ❌  | Use ANF (= the inverse of LMS)              |

## Toggling on/off

The **LMS** button on the [DSP & AUDIO panel](panel:dsp).  Lit
= LMS engaged; dim = bypass.

When LMS is enabled, a **strength slider** appears alongside it
for on-the-fly intensity adjustment.

## Strength slider

Range 0–100 %.  Controls how aggressively the predictor lifts
periodic content.

- **0 %** — gentle predictor, mostly original signal.  Good
  starting point for SSB voice.
- **50 %** — WDSP's classic ANR tuning.  The comfortable
  default.
- **100 %** — aggressive predictor, maximum lift.  For
  desperate weak-CW work where you'll trade some "naturalness"
  for an extra few dB of carrier visibility.

Higher strength settings can sound "robotic" on speech because
the predictor over-emphasizes formant patterns; lower settings
preserve more of the natural audio while still cleaning up the
hiss between syllables.

## Right-click menu — quick presets

Without opening Settings:

- **Off** — bypass.  Same as toggling the button off.
- **Light** — slider 25 %.  Gentle predictor, mostly original
  signal.  Good starting point for SSB voice.
- **Medium** — slider 50 %.  Default.
- **Heavy** — slider 100 %.  Maximum predictor.
- **Custom** — leaves slider at whatever the operator dragged
  it to.

## Position in the audio chain

LMS runs inside WDSP's RXA chain.  For operator
mental-model purposes:

```
IQ → notches → demod → LMS → NR → ANF → AGC → APF (CW) → audio out
```

LMS goes first among the noise-reduction siblings — it lifts
periodic content so NR sees a cleaner signal-vs-noise picture
and ANF has the residual whistles to chew on.

## Settings → Noise → LMS

The Noise tab in Settings exposes:

- **Enable LMS** checkbox
- **Strength slider** (0–100, same as DSP-row slider)

Settings persist across Lyra restarts.

## What LMS doesn't do

- **Broadband noise** — LMS predicts periodic structure.
  Broadband noise has no structure.  Use [NR](./nr.md) for
  that.
- **Impulse noise** — pre-decimation [NB](./nb.md) handles
  that.  Impulses look like white noise to the LMS predictor.
- **Wide-bandwidth signals** — LMS captures narrow-band
  correlated content.  AM broadcast carriers vanish into a DC
  prediction; FM demod produces no periodic content; SSB voice
  is broadband within its passband.  Don't expect LMS to
  "boost" any of these.

## Tips by mode

- **CW** — LMS shines.  Run with strength 50–100 % depending
  on signal-to-noise.  For weak DX, max it; for normal
  ragchew, 50 % keeps the audio sounding natural.
- **SSB voice** — modest help.  Try strength 25–50 % alongside
  NR mode 3.  At higher settings the prediction can sound
  robotic on speech (predictor locks too aggressively on
  formant patterns and over-emphasizes them).
- **AM** — turn LMS off.  AM carriers vanish under the LMS
  predictor (it tries to predict the carrier as a periodic
  signal and the output becomes mostly the prediction = the
  carrier with no modulation).
- **FM** — turn LMS off.  Nothing to predict.
- **Digital modes (FT8, RTTY, PSK)** — generally off.  The
  decoders prefer raw audio with full noise visible.  Some
  operators report LMS at strength 25 % helps FT8 decode rate
  on noisy bands; opinions differ.

## Tips for weak-CW DX

The classic chain for chasing weak CW:

1. **APF** ([APF help](./apf.md)) on, centered at your CW
   pitch — narrow audio peaking around the CW tone.
2. **LMS** on, strength 75–100 % — predict and lift the
   carrier.
3. **NR** on, mode 3 (MMSE-LSA) with AEPF on — clean the
   residual hiss.
4. (Optional) Manual notches on any nearby heterodynes; **ANF**
   on for chirp-y conditions.

This stack can pull a CW signal several dB out of the noise
floor — the difference between "I can't tell if there's a
station there" and "easy copy."

## Attribution

LMS / ANR is implemented inside WDSP, Copyright (C) Warren
Pratt NR0V, licensed under GPL v2 or later.  Lyra calls into
WDSP through the cffi engine binding; the audio path is the
same one Thetis and PowerSDR have used for years.

Lyra-SDR is GPL v3+ (since v0.0.6) which is license-compatible
with WDSP's GPL v2+.
