# Audio Routing

Lyra supports two audio output paths and a layered gain chain that
matches Thetis / ExpertSDR3 conventions.

## Output sinks

Two output paths are selectable from the **Out** dropdown on the
[DSP & AUDIO panel](panel:dsp), and also from **Settings → Audio**:

| Sink | Where the audio comes out |
|---|---|
| **PC Soundcard** | Your computer's audio output (any selectable device) |
| **AK4951** | The HL2+'s onboard codec line-out jack |

Switching between the two is robust — neither leaks "digitized
robotic" residue from the previous sink, even if you flip rapidly
back and forth.

## Settings → Audio device picker

Under **Settings → Audio** you'll find:

- **Output sink** — same AK4951 / PC Soundcard pick as the front-panel
  dropdown.
- **Output device** — which physical PortAudio device the **PC
  Soundcard** sink uses. Default is **"Auto (WASAPI default)"** which
  picks whatever Windows has set as the default output via the
  WASAPI host API. Override here if your speakers are on a USB audio
  interface, virtual cable, S/PDIF dongle, etc., and Windows's default
  isn't where you want the audio routed.
- **Refresh device list** — re-enumerates PortAudio devices (handy
  after plugging in a new USB sound card without restarting Lyra).

The selection persists across launches via QSettings.

## Why WASAPI (not MME)

Lyra explicitly prefers the **WASAPI** Windows audio API over the
older MME. MME (the system default) is 20+ years old and silently
drops mono frames on S/PDIF / TOSLINK outputs — symptom is "Lyra
opens its audio stream OK but no sound comes out, even though every
other Windows app works fine on the same speakers." WASAPI is what
modern audio apps (DAWs, Thetis, ExpertSDR3, browsers) use. Lyra
opens stereo and duplicates mono into both channels so the same
audio path works on analog AND digital outputs.

## The gain chain

Every audio sample passes through this chain before reaching your
speakers:

```
demod → AGC (if on) → AF Gain → Volume → tanh limiter → sink
```

Three operator-controlled stages, each with a distinct role:

### LNA — RF input gain

Slider on the DSP + Audio panel, range −12 to +31 dB. Sets the
hardware preamp gain on the HL2's AD9866 ADC. This is "how much
signal hits the digitizer" — set it high enough to bring weak
signals above the ADC noise floor, but not so high that strong
signals push the ADC into clipping. Watch the **ADC peak readout**
on the toolbar (color-coded green = sweet spot, orange = hot, red
= clipping).

### AF Gain — makeup gain (post-AGC, pre-Volume)

Slider on the DSP + Audio panel, range 0 to +50 dB. Linear (1 tick =
1 dB). This is the "how much do I need to boost weak signals" knob.
Critical for digital modes where AGC is typically off — set AF Gain
to bring weak FT8/RTTY/PSK signals up to listenable levels without
having to crank Volume.

Set this **once** for your station's typical signal level, then
forget. Most operators land around +20 to +40 dB depending on
antenna strength and band.

### Volume — final output trim

Slider on the DSP + Audio panel, range 0 to 100%. Pure trim of the
final output before it hits the speakers. Uses a **perceptual
(quadratic) curve** so each tick yields roughly equal loudness
change — unity gain (full AF-gained signal) sits at 100%, 71% =
−6 dB, 50% = −12 dB, 25% = −24 dB.

This is the moment-to-moment "louder/quieter" knob.

### Mute

Button next to Vol. Multiplies the final output by 0 without
changing the Volume slider position — quick "hold" during a knock
at the door, click again to resume at exactly the volume you set.
Mute state is Radio-side, so TCI volume commands can't accidentally
un-mute you.

## AGC interactions

AGC sits BEFORE AF Gain in the chain. With AGC **on** (Fast / Med /
Slow / Auto), incoming audio is normalized to a target level
(default −30 dBFS) before AF Gain boosts it further.

With AGC **off** (correct setting for digital modes), AF Gain is
your only level control between demod and Volume — that's what it's
designed for.

Switching AGC on ↔ off produces only a small loudness delta when AF
Gain is sensibly set (Thetis-like behavior). If you see a big jump,
either bump AF Gain higher (to bring AGC-off levels closer to
AGC-on) or lower it (to ease back when AGC-on is too loud).

## AK4951 audio requires 48 kHz sample rate

The AK4951 audio path on the HL2+ requires the IQ sample rate to
be exactly **48 kHz**. At higher rates (96 / 192 / 384 kHz) the EP2
audio queue gets drained faster than the 48 kHz demod can fill it,
producing chopped / distorted audio.

Lyra auto-handles this:

- **Above 48 k → auto-switch to PC Soundcard** with a status-bar
  toast. Your AK4951 preference is remembered and restored when
  Rate returns to 48 k.
- **Picking AK4951 above 48 k** drops the rate to 48 k and applies
  AK4951. One click, works.

## RX audio chain on HL2+

```
Antenna → ADC → DDC → EP2 → AK4951 → phones/line jack → (your speakers)
```

Hardware-level latency for monitoring. The PC is still in the loop
for spectrum, decoding, TCI, etc. — only the audio playback path
is offloaded.

## Routing to VAC / virtual cables

Use the **Settings → Audio → Output device** picker. If you have
VB-Cable / Virtual Audio Cable installed, it appears in the device
list (usually under WASAPI host API). Pick it as the output device
and Lyra's audio routes there for JS8Call / WSJT-X / FLDIGI to
consume — no hardware loopback needed.

## Latency

PC Soundcard latency is PortAudio-default, typically 20–50 ms.
AK4951 latency is hardware-only (under 5 ms typical). Tighter
PortAudio latency settings are on the backlog.
