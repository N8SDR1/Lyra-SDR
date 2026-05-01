# Audio Leveler

Lyra's audio leveler is a **soft-knee compressor** at the very end
of the audio chain. It tames sudden bursts (audio pops, transient
amplitude spikes, single-syllable shouts in voice) while keeping
quieter content audible — what TVs call "Late Night Mode" or
"Loudness Leveling."

The motivating use case: that **brief loud spike → settle pattern**
operators sometimes hear during NR transitions or atmospheric
crashes. The leveler catches the burst before it reaches your
speakers.

## How it differs from AGC

This is the most important distinction:

- **AGC** operates on the RF/IF envelope at second-scale time
  constants. It compensates for slow band-noise vs. strong-signal
  envelope swings (operator tunes from a strong signal to a quiet
  band; AGC re-balances over a few seconds).

- **Audio Leveler** operates on the demodulated audio at ~100 ms
  time constants. It catches within-signal dynamics — a guy
  talking quietly then yelling, an audio pop spike, a sudden
  noise crash.

They do different things on different time scales. Both run in
series. AGC handles "where in the band am I tuned"; the leveler
handles "what's happening in this audio right now."

## Where it sits

```
... → demod → ANF → NR → APF → AGC → Volume → [Leveler] → tanh → speakers
```

The leveler is the second-to-last stage, just before the final
tanh safety limiter. tanh stays in the chain as a hard safety
catch (it triggers on anything the leveler doesn't fully
suppress, and is the only stage active when the leveler is off).

## Toggling on/off

Settings → Audio → Audio Leveler.

Profile picker (radio buttons):

| Profile | Threshold | Ratio | Makeup | Use it for |
|---|---|---|---|---|
| **Off** | — | 1:1 | — | Bypass; current tanh limiter still catches hard clips. Default. |
| **Light** | -18 dBFS | 2.5:1 | +3 dB | Gentle peak rounding. Preserves dynamics; smooths obvious bursts. |
| **Medium** | -22 dBFS | 4:1 | +6 dB | Standard speech compression. Pops caught, quiet content lifted. Most operators' everyday setting. |
| **Late Night** | -28 dBFS | 8:1 | +10 dB | Aggressive leveling. Quiet content rises above ambient room noise; strong peaks heavily squashed. Won't wake the family during 80m DX. |
| **Custom** | operator-set | operator-set | operator-set | Hand-tuned threshold/ratio/makeup via Settings sliders. |

## How the parameters work

- **Threshold** (dBFS) — level above which compression engages.
  Below threshold, audio is unaffected. Lower threshold = compression
  starts earlier and affects more content.

- **Ratio** (N:1) — for every N dB of input above threshold,
  output rises by 1 dB. So at 4:1, an input that's 8 dB over
  threshold produces output that's only 2 dB over threshold.
  Higher ratio = harder compression / more peak-squashing.

- **Makeup gain** (dB) — post-compression gain to bring the
  overall loudness back up after compression knocked the peaks
  down. Higher makeup = louder output (and quieter content
  becomes more audible since it's not being compressed).

Combined: lower threshold + higher ratio + higher makeup = more
"levelling effect" (less dynamic range, more average loudness).
Higher threshold + lower ratio + minimal makeup = more "peak
limiter" (preserves dynamics, only catches obvious peaks).

## How to dial it in

Start with **Medium**. On a normal QSO it should:
- Catch the louder syllables in voice
- Lift the noise floor and quieter speech segments slightly
- Sound natural — like the operator is speaking at a more even
  level than they actually are

If you want the audio more even (TV-late-night feel):
- Try **Late Night**

If voice sounds "squashed" or "pumping" (loudness rises and
falls audibly with each syllable):
- Step down to **Light**

If you want to fine-tune:
- Pick **Custom** and adjust sliders. Start by lowering the
  threshold (more content gets compressed) before raising the
  ratio.

## What the leveler addresses

- **Sudden audio pops / spikes** — caught and squashed within
  ~5 ms of the transient
- **Loud syllables in voice** ("HELLO" then quiet talking) —
  evened out over ~150 ms
- **NR-induced transient changes** — when NR's gain shifts
  abruptly (rare but possible during mode switches), the
  leveler smooths the level change
- **Background noise during quiet passages** — Makeup gain lifts
  the quiet floor so you can hear the band even when no one's
  talking, without strong signals blasting

## What it doesn't do

- **Replace AGC** — they handle different time scales. Always
  run AGC; leveler is supplemental.
- **Eliminate hard clips** — that's still the tanh safety
  limiter's job. Leveler is the "smart" compression layer in
  front of tanh.
- **Reduce noise** — that's NR's job. The leveler operates on
  amplitude only; it doesn't know speech from noise.
- **Add latency** *(currently)* — the basic compressor has zero
  added latency. A future "lookahead" enhancement is on the
  polish list — would add ~5 ms of delay in exchange for
  catching very fast transients (under the 5 ms attack period).

## Settings → Audio → Audio Leveler

The Audio tab in Settings exposes:

- **Profile** — radio buttons for the four presets + Custom.
- **Threshold slider** — -50 to -3 dBFS, 1 dB steps. Active
  only when Custom is selected.
- **Ratio slider** — 1.0:1 to 20.0:1, 0.1 steps. 1:1 = no
  compression; >10:1 ≈ limiter behavior.
- **Makeup gain slider** — 0 to +24 dB, 1 dB steps.

Settings persist across Lyra restarts via QSettings keys
`audio/leveler_profile`, `audio/leveler_threshold_db`,
`audio/leveler_ratio`, `audio/leveler_makeup_db`.

## Internals (for the curious)

The leveler is a feed-forward soft-knee compressor with
asymmetric attack/release envelope detection:

1. **Level detection**: instantaneous magnitude → dB
2. **Envelope follower**: 1-pole IIR with fast-attack (5 ms) /
   slow-release (150 ms) asymmetry — catches peaks fast,
   recovers naturally
3. **Soft-knee gain curve**: cubic-blend transition over a 6 dB
   knee zone around the threshold (no audible "kink" at threshold)
4. **Linear gain application**: `gain_lin = 10^((gain_red + makeup) / 20)`

For stereo input, the leveler uses **linked compression** —
both channels see the same gain reduction (computed from the
peak-of-channels), so center-panned content stays balanced when
one channel triggers compression. This is the standard
broadcast / mastering compressor behavior.

Implementation: `lyra/dsp/leveler.py` (`AudioLeveler` class).
Algorithm references: Reiss & McPherson "Audio Effects" (2014)
Chapter 6, Zölzer "DAFX" (2nd ed., 2011) Chapter 4.

## Tips by mode

- **SSB voice** — Medium is the sweet spot. Switch to Late
  Night for casual ragchew listening with kids in the room.
- **CW** — Off or Light. Aggressive compression on CW will
  squash the dits' transients and sound unnatural.
- **AM broadcast** — Medium or Late Night, depending on how
  consistent you want loudness across stations.
- **FT8 / digital** — Off. The decoder doesn't care about
  output dynamics; compression just adds a slight CPU cost.
- **FM** — Off or Light. FM has its own pre-emphasis behavior
  that interacts oddly with audio compression on weak signals.
