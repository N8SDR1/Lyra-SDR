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
| BW (-3 dB) | 30 – 200 Hz | 80 Hz | Lower = sharper peak |
| Gain | 0 – 18 dB | +12 dB | Boost amount at the pitch |

Center frequency follows your **CW Pitch** automatically — you don't
set it separately. When you change pitch, APF retunes with you.

## How it sounds (and how to tune it)

**Start with defaults** (80 Hz BW, +12 dB). On a known weak signal,
toggle APF on/off. The signal should sound louder and clearer; the
rest of the passband should still be there but quieter relative.

**Too much ring on dits?** Widen BW (try 100–150 Hz). Below ~30 Hz
the filter starts to act like a resonator and dits develop a tail.

**Not enough boost?** Raise Gain. Above ~14 dB you may notice AGC
pumping — the filter raises the signal level above where AGC can
clamp it neatly. Back off gain or switch to AGC Slow to settle it.

**Boost too aggressive on already-strong stations?** Toggle APF off
when the station is loud enough without it — APF earns its keep on
the weak end.

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

```
IQ → demod → notches → NR → APF → AGC → AF Gain → Volume → output
                            ▲
                            CW-only stage (your enable + pitch + BW + gain)
```

APF runs **before AGC**, on purpose. With APF first, the CW tone
becomes the loudest thing in the AGC window — AGC chases it, and
the operator hears the boosted signal at AGC target level. If APF
ran after AGC, the boost would just get clamped back down (defeating
the point).

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
