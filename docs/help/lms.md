# LMS Adaptive Line Enhancer (NR3)

Lyra's LMS line enhancer is a **predictive** noise reducer — the
inverse of NR1 / NR2.  Where NR estimates and *subtracts* noise,
LMS estimates and *amplifies* the periodic part of the signal.
Whatever's predictable (CW carriers, voice formants, narrow
tonal content) gets lifted; whatever's unpredictable (broadband
hiss, atmospheric noise) falls out.

This makes LMS the operator's go-to for **weak CW buried in band
hiss** and **stable carrier extraction**.  It's complementary to
NR — running both gives you a "lift the periodic content + clean
the residual hiss" stack that's hard to beat for weak DX.

## When to reach for LMS

| Situation | LMS | Notes |
|---|---|---|
| Weak CW DX, S0-S2 | ✅ | Lift the carrier above noise floor |
| Stable carrier extraction | ✅ | E.g., recovering a barely-audible beacon |
| Voice in dense broadband noise | ⚠ | Helps modestly; NR2 wins for voice |
| AM broadcast | ❌ | Chops the carrier; use NR instead |
| FM | ❌ | FM has no periodic structure to predict |
| Killing a known stable tone | ❌ | Use ANF (= the inverse of LMS) |

## Toggling on/off

The **LMS** button on the [DSP & AUDIO panel](panel:dsp).  Lit =
LMS engaged; dim = bypass.

When LMS is enabled, a strength slider appears alongside it for
on-the-fly intensity adjustment.

## Strength slider

Range 0–100 %.  Drives **five algorithm parameters in concert**
to give a meaningful perceptual swing across the slider:

| Slider | Tap count | Step size (2μ) | Leakage | Wet / dry mix |
|---|---|---|---|---|
| 0 | 32 | 5e-5 | 0.05 | 50 % |
| 50 | 80 | 1.75e-4 | 0.125 | 75 % |
| 100 | 128 | 3e-4 | 0.20 | 100 % |

What the parameters do:

- **Tap count** — the predictor's filter length.  More taps =
  more selective predictor = harder rejection of broadband
  content.  This is the biggest perceptual change across the
  slider.
- **Step size (2μ)** — adaptation rate.  Higher = filter learns
  new periodic content faster, but more sensitive to transient
  noise.
- **Leakage (γ)** — how aggressively weights decay when the
  predictor isn't converging.  Higher = filter forgets faster
  when the signal goes away.
- **Wet/dry mix** — fraction of the LMS prediction in the output
  vs the original input.  At 50 % wet the operator hears half
  input + half prediction, which sounds smoother and more natural
  than pure prediction (which can sound "artificial" on voice).

At slider = 50 the algorithm parameters land on Pratt's WDSP
defaults — the operator-validated "classic ANR" tuning.  At
slider = 100 the result is bit-exact identical to running pure
LMS prediction (for operators who want the legacy behavior, max
the slider).

**Bench-validated swing**:
- Stable 800 Hz tone in white noise: ~10.5 dB residual-noise
  reduction across the slider (4.9 dB at min vs 15.4 dB at max)
- Voice-like multi-formant signal: ~4.3 dB swing

## Right-click menu

Quick presets without opening Settings:

- **Off** — bypass.  Same as toggling the button off.
- **Light** — slider 25 %.  Gentle predictor, mostly original
  signal.  Good starting point for SSB voice.
- **Medium** — slider 50 %.  WDSP-default tuning.
- **Heavy** — slider 100 %.  Aggressive predictor, pure
  prediction output.  For desperate weak-CW work.
- **Custom** — leaves slider at whatever the operator dragged it
  to.

## Position in the audio chain

```
demod → LMS → ANF → SQ → NR → APF → audio out
```

LMS runs **first** in the post-demod chain.  Why:

- LMS is a **predictor** — it lifts periodic content (CW
  carriers, voice formants) above broadband noise.  It needs to
  see the FULL periodic spectrum to learn from.
- ANF is a **remover** — it cancels periodic content (whistles,
  heterodynes).  Running ANF before LMS would feed LMS the
  residual *with the periodic content already removed*, which
  defeats LMS's predictor entirely.
- Therefore: LMS → ANF.  LMS lifts what's periodic, ANF removes
  any residual whistles, NR cleans up broadband.

This is a **change from earlier Lyra versions** (v0.0.6 and
earlier ran ANF → LMS).  Operators who run LMS+ANF together
should hear a meaningful improvement on the new chain — LMS
finally has the periodic content it needs to predict.

## Settings → Noise → LMS

The Noise tab in Settings exposes:

- **Enable LMS** checkbox
- **Strength slider** (0–100, same as DSP-row slider)

Settings persist across Lyra restarts via QSettings keys
`noise/lms_enabled` and `noise/lms_strength`.

## Internals (for the curious)

LMS is a **normalized-LMS line enhancer with adaptive leakage**.
For each input sample x[n] at 48 kHz, the filter:

1. Pushes the sample into a circular delay line.
2. Reads a window of `n_taps` samples starting `delay` samples
   back from the current position.  The decorrelation gap means
   the prediction sees only signal content that's *correlated
   across the delay* — broadband noise (uncorrelated) doesn't
   contribute.
3. Computes the prediction `y[n] = Σ w[j] · d[idx_j]` (weighted
   sum of the delay-line window).
4. Computes the residual error `e[n] = x[n] − y[n]` — this is
   the noise (what could NOT be predicted).
5. Outputs `wet · y[n] + (1−wet) · x[n]` — the wet/dry blend
   between prediction and input.
6. Updates the filter weights via NLMS:
   `w[j] ← (1 − 2μγ) · w[j] + (2μ · e[n] / σ²) · d[idx_j]`

The **adaptive leakage** (Pratt's enhancement) auto-tunes γ
based on per-sample prediction error.  More leakage when the
signal is unstable (tracking transients), less leakage when the
signal is steady (preserving lock on a stable CW tone).

The implementation uses **block-LMS** with a sub-block size
equal to the decorrelation delay (default 16 samples).  Within a
sub-block, weights are frozen and outputs are computed in a
single vectorized gather + dot-product.  Weight update happens
once per sub-block.  This is ~20× faster than per-sample LMS at
the cost of a marginal convergence-rate reduction (still ~3 kHz
weight updates at 48 kHz audio — well above ham-band signal
dynamics).

State preserved across audio blocks: circular delay-line ring
buffer (size 2048, power of 2 for fast bitmask wraparound) and
adaptive weight vector.  `reset()` zeros both — called on
freq/mode changes where any periodic content the filter learned
belongs to a band you've left.

Implementation: `lyra/dsp/lms.py` (`LineEnhancerLMS` class).
Vectorized NumPy block-LMS — ~0.3 ms internal latency, well
under a millisecond per 2048-sample block.

## Attribution

Algorithm and parameter defaults derived from WDSP's `anr.c`
(Adaptive Noise Reduction — Normalized LMS line enhancer with
adaptive leakage), Copyright (C) 2012, 2013 Warren Pratt NR0V,
licensed under GPL v2 or later.  Lyra-SDR's port re-expresses
the algorithm in idiomatic NumPy with an integrated wet/dry
output blend, but the core math — NLMS update, adaptive-leakage
tracking, parameter defaults — follows Pratt's reference
implementation directly.

Lyra-SDR is GPL v3+ (since v0.0.6) which is license-compatible
with WDSP's GPL v2+.

## What LMS doesn't do

- **Broadband noise** — LMS predicts periodic structure.
  Broadband noise has no structure.  Use [NR](./nr.md) for that.
- **Impulse noise** — pre-decimation [NB](./nb.md) handles
  that.  Impulses look like white noise to the LMS predictor.
- **Wide-bandwidth signals** — LMS works at the audio level on a
  short delay window, so it captures narrow-band correlated
  content.  AM broadcast carriers are constant DC after demod
  (no audio-level periodic structure); FM demod produces no
  periodic content; SSB voice is broadband within its passband.
  Don't expect LMS to "boost" any of these.

## Tips by mode

- **CW** — LMS shines.  Run with strength slider 50–100 %
  depending on signal-to-noise.  For weak DX, max it; for normal
  rag-chew, 50 % keeps the audio sounding natural.
- **SSB voice** — modest help.  Try slider 25–50 % alongside
  NR2.  At higher settings the prediction can sound robotic on
  speech (the predictor locks too aggressively on formant
  patterns and over-emphasizes them).
- **AM** — turn LMS off.  AM carriers vanish under LMS predictor
  (it tries to predict the carrier as a periodic signal and the
  output becomes mostly the prediction = the carrier with no
  modulation).
- **FM** — turn LMS off.  Nothing to predict.
- **Digital modes (FT8, RTTY, PSK)** — generally off.  The
  decoders prefer raw audio with full noise visible.  Some
  operators report LMS at slider 25 % helps FT8 decode rate on
  noisy bands; opinions differ.

## Tips for weak-CW DX

The classic chain for chasing weak CW:

1. **APF** ([APF help](./apf.md)) on, centered at your CW pitch
   — narrow audio peaking around the CW tone.
2. **LMS** on, strength 75–100 % — predict and lift the carrier.
3. **NR** on, NR2 with captured profile of your local band noise
   — clean the residual hiss.
4. (Optional) Manual notches on any nearby heterodynes; **ANF**
   on for chirp-y conditions.

This stack can pull a CW signal a real 6–10 dB out of the noise
floor.  At our station's typical HF QRM, that's the difference
between "I can't tell if there's a station there" and "easy
copy."
