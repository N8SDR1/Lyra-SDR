# APF — Audio Peaking Filter (CW)

The **APF** boosts a narrow region of audio at your CW pitch, so a
weak CW signal jumps out of the noise without the harsh ringing of a
brick-wall narrow filter. Other audio in the passband stays audible
— you keep band context (nearby callers, QRM clues) while the
station you're chasing reads cleanly above the noise floor.

## When to use it

- **Weak DX or contest CW** — APF can lift a fragmented signal into
  copyable shape.
- **CW search-and-pounce on a noisy band** — better than narrowing
  your filter further (which adds ringing).
- **Long ragchews on QRN-heavy nights** — gentler than NR, kinder to
  the natural shape of the keying.

APF is **CW-only**. Lyra mode-gates internally — your enable state
is preserved across mode switches, but APF only audibly affects audio
in CWU/CWL. Toggling it during SSB/AM/FM does nothing audible.

## Toggling on/off

The **APF** button on the [DSP & AUDIO panel](panel:dsp). Lit = APF
engaged; dim = bypass. Hover for current BW/Gain readout.

## Right-click — quick presets

Right-click the APF button to pick BW and Gain without opening
Settings. Useful for fast on-the-air adjustments:

- **Bandwidth** — 40 / 60 / 80 / 100 / 150 Hz
- **Gain** — +6 / +9 / +12 / +15 / +18 dB

The currently-active value is checked in the menu so you always
know where you are.

## Full controls — Settings → DSP → CW

Open **DSP Settings** (or File → DSP… in the menubar) for the full
APF controls:

| Control | Range | Default | Notes |
|---|---|---|---|
| Enable | on/off | off | Master toggle |
| BW (-3 dB) | 30 – 200 Hz | 100 Hz | Lower = sharper peak |
| Gain | 0 – 18 dB | +12 dB | Boost amount at the pitch |

Center frequency follows your **CW Pitch** automatically — you don't
set it separately. When you change pitch, APF retunes with you.

The 100 Hz default (Q ≈ 6.5 at a 650 Hz pitch) is wide enough to
catch a CW signal even if you're slightly off zero-beat — about
±50 Hz of mistuning still lands inside the boost band. Operators
who want razor-sharp tone selection can drop BW to 40-60 Hz once
they're zero-beat; operators on messy bands can widen up to 200 Hz.

## How it sounds (and how to tune it)

**Start with defaults** (100 Hz BW, +12 dB). On a known weak signal,
toggle APF on/off. The signal should sound noticeably louder; the
rest of the passband stays audible but quieter relative.

**Too much ring on dits?** Widen BW (try 120–150 Hz). Below ~30 Hz
the filter starts to act like a resonator and dits develop a tail.

**Not enough boost?** Raise Gain. Up to +18 dB. The boost is now
applied AFTER AGC, so it's a literal increase in tone loudness at
the speaker (not just an SNR change AGC compensates for).

**Boost too aggressive on already-strong stations?** Toggle APF off
when the station is loud enough without it — APF earns its keep on
the weak end. Strong signals plus high APF gain can saturate
output; drop Gain or turn APF off for those situations.

**Not on zero-beat and APF feels weak?** Either widen BW (up to
150 Hz) so the boost band catches your slightly-off signal, or
fine-tune your radio frequency a few Hz at a time — there's a sweet
spot where the tone "pops" out. The narrower your BW setting, the
more critical zero-beat becomes.

## Why APF doesn't ring like a narrow filter

A narrow brick-wall filter (say, a 50 Hz CW filter) has a long
impulse response — every dit decays into an audible tail. That's
the "ringing" CW operators describe. APF is a **peaking** filter:
it boosts a narrow band but doesn't sharply cut outside it, so its
impulse response is short. The signal lifts; the dits stay crisp.

It's the same DSP primitive as a parametric EQ "peak" band — just
with a much higher Q centered exactly where you want the tone to
sit.

## Where APF sits in the audio chain

APF is implemented as WDSP's **SPEAK biquad** inside the RXA
chain — a resonant boost centered on your CW pitch.  WDSP
runs the entire audio chain in C, so the operator-tunable
parameters (enable / BW / gain / center) flow into WDSP via
the engine's own initial-state push and mid-session updates.

For operator mental-model purposes:

```
IQ → notches → demod → NR → ANF → AGC → APF (CW only) → audio out
                                          ▲
                                          mode-gated to CWU/CWL
```

APF runs after AGC inside WDSP, so the operator's gain
boost (up to +18 dB) is a literal loudness boost on the CW
tone — not something AGC compensates back to a flat target.

## Tips

- **Pair with NR** — APF + NR Light is the cleanest CW listening
  combo on noisy bands. NR shaves the broadband hiss; APF lifts the
  tone above what's left.
- **Pair with a wider CW filter** — counter-intuitively, APF works
  *better* with a wider RX filter (500 Hz vs 250 Hz). The wider
  filter doesn't ring on its own, and APF supplies the selectivity.
- **CW pitch matters** — APF tracks your pitch, so set pitch to your
  preferred listening tone *first*, then enable APF. Changing pitch
  with APF on is fine (it retunes smoothly), but starting at your
  pitch of choice is one less tweak per session.
