# Audio Routing

Lyra supports two audio paths, depending on which hardware variant
you have.

## Plain HL2 — PC audio

1. HL2 sends IQ samples over EP6 (Ethernet UDP).
2. Lyra demodulates in Python/NumPy.
3. Audio goes to the PC sound system via `sounddevice`.

Destination is picked via the **Out** dropdown on the
[DSP & AUDIO panel](panel:dsp) (top row), with options for the
default system device or a specific device by name — useful if you
have multiple audio cards.

**Volume** — the **Vol** slider at the top of the same panel (next
to the **LNA** slider) attenuates the final output. LNA and Volume
were previously split across two panels; they now live together
since they're both "how much signal" controls and belong next to
the AGC readout that affects both.

### AK4951 audio requires 48 kHz sample rate

The AK4951 audio path on the HL2+ requires the HL2 IQ sample rate to
be exactly **48 kHz**. At higher rates (96 / 192 / 384 kHz) the EP2
audio queue gets drained faster than the 48 kHz demod can fill it,
and the missing samples get zero-padded → chopped / distorted audio.

Lyra guards against this two ways:

1. **Auto-switch**: changing Rate above 48 kHz automatically routes
   audio to PC Soundcard (with a status-bar toast) and restores your
   AK4951 preference the moment Rate returns to 48 kHz.
2. **Veto on manual selection**: if you try to pick AK4951 while
   Rate is above 48 kHz, the selection is refused with a clear
   status message ("AK4951 requires 48 k sample rate — staying on
   PC Soundcard"). Drop the rate to 48 kHz and AK4951 is yours.

If you need both wider span (192 / 384 k panadapter) AND hardware
AK4951 audio, use the PC Soundcard path at higher rates — it's a
well-tested 48 kHz audio output that plays over whatever system
device you've selected.

The slider uses a **perceptual (quadratic) curve** so each tick
yields a roughly equal loudness change — unity gain lands near the
71% mark, 100% is +6 dB of headroom.

**MUTE** — the MUTE button next to the Vol slider silences output
**without changing the volume slider position**, so you can hit it
for a quick "hold" during a phone call or knock at the door and
resume at exactly the volume you left. Mute is Radio-side, meaning
it also survives TCI volume commands that might otherwise un-mute
inadvertently.

## Auto-LNA

The **Auto** button next to the LNA slider engages continuous RF
gain adjustment. A 1.5 s control loop watches the ADC peak
magnitude and walks LNA gain up or down to keep it near −15 dBFS
(±3 dB deadband, ±3 dB max change per step). You can still drag
the LNA slider to override — Auto will walk back toward the target
on the next tick.

Useful when band conditions are changing rapidly (solar noise
opening, QRN coming up, strong local station keying up on the
adjacent frequency) — keeps the ADC from clipping without forcing
you to chase the LNA slider.

Turn off for quiet / marginal operation where you want to manually
push the LNA toward max gain to squeeze out weak signals.

Latency is PC-dependent, typically 20–50 ms with the default
sounddevice settings. WASAPI / ASIO / WDM-KS driver selection is on
the backlog for tighter latency.

## HL2+ — AK4951 hardware audio

The HL2+ add-in board includes an **AK4951 codec** with:

- A **line-level RX audio jack** — HL2 gateware routes received audio
  here directly via EP2, bypassing the PC entirely for RX monitoring.
- A **microphone input** — used by the TX path (when TX ships) for
  SSB/AM/FM modulation.

**RX audio chain on HL2+:**

```
Antenna → ADC → DDC → EP2 → AK4951 → phones/line jack → PC line-in
```

The PC line-in capture is what you hear. This gives hardware-level
latency for monitoring, with the PC still able to record or process
the audio downstream.

**TX path (future):**

```
PC mic / AK4951 mic → modulator → DUC → DAC → PA → antenna
```

Mic input selection (AK4951 vs PC mic) will be a user toggle when TX
ships. User's preferred path: AK4951 mic.

## Which one am I?

If you have the small piggyback board with a 3.5 mm jack labeled
"AUDIO" (or similar) on the HL2, you're HL2+. The gateware must also
be the HL2+ build for AK4951 routing to work.

If you're unsure, start in **plain HL2 mode** (sounddevice output) —
it works regardless.

## Routing to VAC / virtual cables

Not yet implemented. Planned: pick any PC audio device by name so
demod audio can be routed to VB-Cable, VAC, or similar for JS8Call /
WSJT-X / FLDIGI without needing a hardware loopback.
