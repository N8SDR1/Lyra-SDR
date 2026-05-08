# BIN — Binaural Pseudo-Stereo

The **BIN** button creates a stereo soundstage from Lyra's mono
demodulator output by routing a 90°-phase-shifted copy of the audio
to one ear and the in-phase original to the other. Your brain
interprets the difference as spatial position — the "inside-the-head"
effect that ham operators recognize from PowerSDR / Thetis-class
clients.

It works on every mode, but earns its keep on:

- **CW** — weak CW signals lift out of the noise more easily when
  they have a perceived spatial position. This is the classic
  "binaural CW" technique.
- **SSB on headphones** — voice gains a sense of space and
  separation that's especially helpful in busy band conditions.

## Toggling on/off

The **BIN** button on the [DSP & AUDIO panel](panel:dsp). Lit = BIN
engaged; dim = bypass. Hover for current depth readout.

> **Headphones only.** BIN is a stereo-channel-difference effect —
> on speakers, the L/R signals partially cancel acoustically and you
> lose most of the spatial cue. Use headphones (any kind) for the
> intended experience.

## Right-click — quick depth presets

Right-click the BIN button to set depth without opening Settings:

- 25 % — subtle widening
- 50 % — moderate spatial cue
- **70 % — default (strong, comfortable)**
- 85 % — wider
- 100 % — full Hilbert pair (maximum spatial separation)

The active value is checked so you always know where you are.

## Full controls — Settings → DSP → CW

Open **DSP Settings** (or File → DSP… in the menubar) for the full
BIN controls:

| Control | Range | Default | Notes |
|---|---|---|---|
| Enable | on/off | off | Master toggle |
| Depth | 0 – 100 % | 70 % | Spatial separation amount |

## How it sounds

**Off:** mono, both ears identical.

**On at 70 %:** the audio source feels "in the middle of your head"
rather than "in your ears." Background noise feels diffuse and the
signal you're chasing has a more defined position. CW dits/dahs
keep their crisp shape.

**100 %:** maximum spatial split. Some operators prefer this on
weak CW; others find it too "hollow" for ragchews. Try 70 % first,
adjust to taste.

## Equal-loudness across depth

Lyra normalizes BIN's output so changing depth does not change
perceived volume. Without normalization, 100 % depth would sound
~3 dB louder than 0 % (orthogonal Hilbert pairs add in quadrature
when summed back to mono in the listener's brain).

## Where BIN sits in the audio chain

```
IQ → demod → notches → NR → APF → AGC → AF → Volume → tanh → BIN → sink
                                                              ▲
                                                              mono → stereo
```

BIN runs **last** — after AGC has leveled the signal, after the
tanh limiter has prevented clipping. The Hilbert phase transform
operates on the operator's already-listening-ready audio, so BIN
never interferes with AGC tracking or peak-meter math.

The audio sinks (PC Soundcard, HL2 audio jack) accept either
mono or already-stereo input. When BIN is off, Lyra sends mono
and the sink duplicates to L/R as before. When BIN is on, the
sink takes the (N, 2) stereo array directly and applies your
**Balance** slider on top.

## Tips

- **Pair with NR** — BIN + NR Light is excellent on busy bands;
  the spatial cue makes it easier to focus on the station you're
  copying while NR knocks the broadband hiss down.
- **Pair with APF on CW** — APF lifts the CW tone above the noise
  floor, BIN gives it a position in space. Combined they make weak
  CW remarkably copyable.
- **Speakers? Skip it.** Without channel separation (i.e., headphones
  or an in-ear setup) the L/R signals cancel partially and the
  effect collapses. Lyra doesn't auto-disable BIN on speakers — it
  has no way to tell what you're plugged into — so just leave the
  button off when you're listening on speakers.
