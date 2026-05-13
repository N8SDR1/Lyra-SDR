# Audio Routing

Lyra supports two audio output paths and a layered gain chain
following the standard SDR-client conventions.

## Output sinks

Two output paths are selectable from the **Out** dropdown on the
[DSP & AUDIO panel](panel:dsp), and also from **Settings → Audio**:

| Sink | Where the audio comes out |
|---|---|
| **PC Soundcard** | Your computer's audio output (any selectable device) |
| **HL2 audio jack** | The HL2+'s onboard codec line-out jack (AK4951) |

Switching between the two is robust — neither leaks "digitized
robotic" residue from the previous sink, even if you flip rapidly
back and forth.

## Settings → Audio device picker

Under **Settings → Audio** you'll find:

- **Output sink** — same HL2 audio jack / PC Soundcard pick as the
  front-panel dropdown.
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
modern audio apps on Windows use (DAWs, SDR clients, browsers). Lyra
opens stereo and duplicates mono into both channels so the same
audio path works on analog AND digital outputs.

## The gain chain

Every audio sample passes through this chain before reaching
your speakers:

```
RF in → LNA → ADC → IQ → demod → notches → NR → ANF
                              ↓
                      AF Gain (pre-AGC) → AGC → APF (CW only)
                              ↓
                          Volume → Mute → Bal → sink
```

The DSP stages (notches / NR / ANF / AGC / APF) all run inside
the WDSP engine; the operator-controlled level stages (LNA, AF
Gain, Volume, Mute, Bal) wrap around it.  This matches Thetis
and other openHPSDR-class clients.

Operator-controlled stages, each with a distinct role:

### LNA — RF input gain

Slider on the DSP + Audio panel, range −12 to +31 dB. Sets the
hardware preamp gain on the HL2's AD9866 ADC. This is "how much
signal hits the digitizer" — set it high enough to bring weak
signals above the ADC noise floor, but not so high that strong
signals push the ADC into clipping. Watch the **ADC peak readout**
on the toolbar (color-coded green = sweet spot, orange = hot, red
= clipping).

The LNA dB readout to the right of the slider is **color-coded**
to reflect the AD9866 PGA's linearity:

| Zone | Range | When to use |
|------|-------|-------------|
| **GREEN — sweet spot** | −12 .. +20 dB | Normal HF operating, contests, anywhere there's any decent signal level. Lowest IMD, cleanest dynamic range. |
| **YELLOW — high gain** | +20 .. +28 dB | Quieter bands (10 m, 6 m), weak-signal modes (FT8/WSPR), low-noise antennas. Watch for IMD on bands with strong adjacent signals. |
| **ORANGE — IMD risk** | +28 .. +31 dB | Only when you really need every dB — e.g. EME, weak meteor scatter, very quiet portable setups. The PGA approaches its compression knee here; nearby strong signals can fold into your passband as ghost products. |

Above +31 dB the AD9866 stops giving usable additional gain and
just compresses the ADC, so Lyra hard-caps the slider at +31. You
**can't** accidentally drive the chip into the unusable region.

#### Auto-LNA — overload protection (back-off only)

The **Auto** button next to the LNA slider enables a back-off-only
control loop:

| ADC peak | Auto action |
|---|---|
| > −3 dBFS  | drop 3 dB (urgent — clipping imminent) |
| > −10 dBFS | drop 2 dB (hot — leave headroom) |
| otherwise  | leave the operator's setting alone |

**It does NOT raise gain** — that's deliberate. You set the baseline
LNA for the band you're on; Auto only kicks in when a transient
strong signal threatens to overload the ADC. When it fires, three
visual cues appear so you can see Auto working:

1. The **slider moves** to the new (lower) gain value.
2. A small amber **"↓2 dB  HH:MM:SS"** badge appears next to the
   Auto button showing the most recent event. Hover for a tooltip
   with the ADC peak that triggered the adjustment.
3. The **slider track briefly flashes amber** (~800 ms) so the eye
   catches the change even if you're not looking right at the
   slider.

If you've enabled Auto and never seen it fire, that means your
antenna isn't delivering signals strong enough to need the
protection — which is the common case under normal HF conditions.
A strong AM broadcast bleed, a nearby contest station, or a quiet
band suddenly opening with a big DX signal are typical triggers.

#### Auto-LNA pull-up — bidirectional mode (opt-in)

Settings → DSP → **Auto-LNA pull-up** (default OFF) promotes the
Auto button from back-off-only to **bidirectional**. With pull-up
on, Auto also *raises* gain when the band has been quiet for a
while — useful for digging weak signals out of the noise on quiet
bands without having to ride the slider yourself.

**How pull-up decides to climb (all must hold):**

| Gate | Threshold |
|---|---|
| RMS over recent window | < −50 dBFS |
| Peak over recent window | < −25 dBFS |
| Sustained-quiet streak | 5 consecutive ticks (~7.5 s) |
| Time since last manual gain change | > 5 s |
| Current LNA gain | below the active ceiling (see below) |

**Two-tier soft ceiling.** Climb stops at one of two values
depending on whether there's a real signal in your demod passband:

| Situation | Soft ceiling |
|---|---|
| Passband peak more than +10 dB above noise floor | **+15 dB** |
| Truly quiet passband (only noise) | **+24 dB** |

Below either ceiling, an in-passband signal does **NOT** block
climb — that's exactly the use case pull-up is built for (bringing
weak but present signals up from inaudible). The lower +15 dB
ceiling only kicks in once we're at a gain level where pushing
further could drive the AD9866 PGA into compression with a strong
in-filter signal. Above +15 with signal present, pull-up stops
and the AGC takes over from there.

If pull-up has driven LNA above +12 dB and a strong passband
signal arrives mid-tune, an additional **back-off trigger** drops
2 dB at a time until the PGA is happy — even if the full-IQ peak
is still cool. This catches the "tune onto a strong AM carrier
while pull-up has lifted you" case automatically.

When all gates pass, Auto climbs by **+1 dB**. The next tick
re-evaluates from the new gain. Down-steps stay aggressive (2–3
dB), up-steps stay gentle (1 dB) — the loop reacts fast to
overload, slow to opportunity.

**Self-limiting:** every +1 dB of LNA raises the noise floor by
roughly +1 dB. On a typical clean station the climb naturally
halts when RMS crosses −50 dBFS — usually well before the +24 dB
ceiling. The ceiling is just a hard backstop in case noise floor
stays unusually low.

**Manual override always wins.** Touch the slider and pull-up
defers for 5 seconds, then re-evaluates. If you set LNA manually
above +24 dB, Auto won't pull it back down (back-off still will,
on real overload).

**Why it's opt-in:** an earlier Lyra build had a target-chasing
upward loop that drove LNA to +44 dB on 40 m and produced IMD.
The current pull-up uses RMS detection (not peak chasing), a
much lower ceiling, and slow asymmetric stepping — but until
field-tested across a variety of stations, it stays off by
default. Turn it on when you want to try it; turn it off if you
hear odd mixing products on busy bands.

### AF Gain — pre-AGC makeup gain

Slider on the DSP & AUDIO panel, range 0 to +80 dB.  Pushed
into WDSP's `PanelGain1` stage on every change — the same
"AF Gain" wiring Thetis uses.

AF Gain sits **before** AGC in the chain.  Two practical
implications:

- **AGC ON** — AGC normalizes output to its target regardless
  of how much AF Gain you've dialed in, so AF mostly just
  feeds more signal into AGC.  You'll hear at most a small
  loudness delta when sweeping AF Gain on a strong signal.
  This prevents the "AF + AGC stack and clip" symptom.
- **AGC OFF** (digital modes — FT8 / FT4 / RTTY) — AGC's
  automatic amplification is off, so AF Gain becomes your
  primary level knob between weak signals and audible.
  Sweeping AF Gain produces a dramatic loudness change.

Set AF Gain **once** for your station's typical signal level
and listening preference, then leave it alone.  Most
operators land somewhere between +25 and +50 dB depending on
antenna strength and how much they like to dig into the
noise floor.

The +80 dB top end is there for AGC-OFF digital-mode
operators — without AGC's +60 dB internal amplification, weak
signals can be 30 dB quieter than they would be on AGC ON,
and the extra AF range closes that gap.  Operators who don't
need it simply never visit it.

### Volume — final output trim (Vol RX1 + Vol RX2)

Two sliders on the DSP + Audio panel, range 0 to 100% each.
**Vol RX1** controls RX1's output trim, **Vol RX2** controls
RX2's.  Both are always visible regardless of the SUB toggle
state (operator UX 2026-05-12: predictable layout beats
conditional widgets).  Pure trim of the final output before it
hits the speakers. Uses a **perceptual (quadratic) curve** so
each tick yields roughly equal loudness change — unity gain
(full AF-gained signal) sits at 100%, 71% = −6 dB, 50% = −12 dB,
25% = −24 dB.

In **SUB-off** mode only the focused VFO is audible.  Adjusting
the non-audible RX's slider has no immediate effect — it
pre-sets that VFO's level for when you flip focus or enable SUB.
Lyra also mirrors the previously-focused RX's volume to the
newly-focused RX on every focus flip, so audible level stays
consistent across VFO switches.

In **SUB-on** mode both VFOs are audible (RX1 left, RX2 right)
and the two sliders are independent — drag Vol RX1 down without
touching RX2's level, or vice versa.

### MUTE buttons

Two buttons, one next to each Vol slider.  Multiplies that
receiver's output by 0 without changing its Volume slider
position — quick "hold" during a knock at the door, click again
to resume at exactly the volume you set. Mute state is
Radio-side per RX, so TCI volume commands can't accidentally
un-mute you.

### Bal — stereo balance / pan

Slider on the DSP + Audio panel between **Vol** and **Out**. Pans
the (currently mono) audio between the left and right channels
using an equal-power pan law (cos / sin at π/4) — the perceived
loudness stays constant as you sweep across center, instead of
sagging in the middle the way a naive linear pan would.

**Three ways to find center:**

1. **Visible tick marks** below the slider track — five marks at
   L100 / L50 / **C** / R50 / R100. The center mark is where you
   stop for true mono.
2. **Snap-to-center deadzone** — sweeping within ±3% of center
   automatically locks to true zero. Lets you find center without
   pixel-perfect aim.
3. **Click the L37 / C / R12 label** to the right of the slider
   to instantly recenter. Double-clicking the slider track itself
   does the same thing.

**Works on both output sinks:**

- **PC Soundcard** — applied per-channel before stereo write to
  the WASAPI output device.
- **HL2 audio jack** — the HL2+'s onboard AK4951 codec is a true
  stereo DAC.  The EP2 audio frame has separate Left16 / Right16
  fields that the gateware routes to the AK4951's L/R channels
  independently.  Lyra applies the balance gains and feeds
  proper stereo to both.

**Future expansion (after RX2 ships):** the same Bal slider will
become the RX1 / RX2 mixing control — RX1 to one ear, RX2 to the
other for DX-split listening.

## AGC interactions

AGC sits AFTER AF Gain in the chain (Thetis-style — AF is the
pre-AGC makeup gain).  With AGC **on** (Fast / Med / Slow /
Long / Auto), AGC normalizes whatever AF Gain feeds it to its
target level, so the volume slider has the same useful range
across all AGC profiles.

With AGC **off** (correct setting for digital modes), AGC's
automatic amplification is gone, so **AF Gain becomes your
primary level knob** between demod and Volume.  Sweep AF Gain
to find the level your decoder app or ear wants.

Switching AGC on ↔ off should produce only a small loudness
delta when AF Gain is sensibly set.  If you see a big jump,
either bump AF Gain higher (to bring AGC-off levels closer to
AGC-on) or lower it (to ease back when AGC-on is too loud).

## HL2 audio jack requires 48 kHz sample rate

The HL2 audio jack path requires the IQ sample rate to be
exactly **48 kHz**.  At higher rates (96 / 192 / 384 kHz) the
EP2 audio queue gets drained faster than the 48 kHz demod can
fill it, producing chopped / distorted audio.

Lyra auto-handles this:

- **Above 48 k → auto-switch to PC Soundcard** with a
  status-bar toast.  Your HL2-jack preference is remembered
  and restored when Rate returns to 48 k.
- **Picking HL2 audio jack above 48 k** drops the rate to
  48 k and applies the HL2 jack.  One click, works.

## RX audio chain on HL2+

```
Antenna → ADC → DDC → EP2 → AK4951 codec → phones/line jack → speakers
```

Hardware-level latency for monitoring.  The PC is still in
the loop for spectrum, decoding, TCI, etc. — only the audio
playback path is offloaded.

## Routing to digital decoder apps (WSJT-X, JS8Call, FLDIGI, MSHV, …)

You have two options.  Modern path is much simpler:

### Recommended: TCI audio (no VAC needed)

If your decoder app supports TCI (most modern ones do — WSJT-X
2.5+, JS8Call, FLDIGI, MSHV, log4OM), use TCI for both rig
control AND audio.  Lyra's TCI server streams 48 kHz audio over
the same WebSocket that carries the rig commands.  No Virtual
Audio Cable, no exclusive-mode soundcard juggling.

  1. Settings → Network/TCI → ☑ TCI Server Running
  2. In your decoder app, point its rig to TCI / `127.0.0.1:50001`
  3. Same app: point its audio input/output at "TCI Audio" (or
     equivalent name)

See the [TCI Server](tci.md) topic for per-app setup recipes
(WSJT-X, JS8Call, MSHV, FLDIGI).

**Side benefit:** with TCI audio enabled, the AK4951 codec output
and PC sound card output both run noticeably cleaner — fewer
clicks/ticks during normal listening.  Worth keeping TCI server
on as a default even if you're not using a decoder app.

### Legacy: VAC (Virtual Audio Cable)

For apps that don't speak TCI, install VB-Cable or similar VAC
software, then use the **Settings → Audio → Output device** picker.
The virtual cable appears in the device list (usually under
WASAPI host API).  Pick it and Lyra's audio routes there for the
decoder to consume — no hardware loopback.

VAC has higher latency than TCI audio and requires separate
software install.  Use only when TCI isn't available.

## Latency

PC Soundcard latency is PortAudio-default, typically 20–50 ms.
AK4951 latency is hardware-only (under 5 ms typical). Tighter
PortAudio latency settings are on the backlog.
