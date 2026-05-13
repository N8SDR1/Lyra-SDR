# RX2 — Dual Receiver

*(Tip: click [this link](panel:tuning) to flash the TUNING panel —
where most of the dual-RX controls live.)*

Lyra v0.1 adds a second receiver (**RX2**) running on the HL2's
DDC1 hardware channel.  You can listen to two frequencies at once,
swap them, copy state between them, and have either VFO be the
"active" tuning target without losing the other's settings.

The Hermes Lite 2 has a single ADC + shared front-end filter
bank, so RX2 isn't a completely independent receiver — it sees the
same RF the front-end is letting through.  But within that
constraint, you get true simultaneous dual-channel digital tuning
+ demodulation + audio.

## The two VFO LEDs

The **TUNING panel** shows two large frequency readouts side by side:

```
┌────────────── TUNING ────────────────────────────────────────────┐
│                                                                  │
│  RX1                      [LYRA LOGO]                      RX2  │
│  ┌──────────────┐                                ┌─────────────┐│
│  │  7.074.000   │                                │ 14.205.000  ││
│  └──────────────┘                                └─────────────┘│
│                                                                  │
│       Step [1 kHz▾]  Mode [USB▾]   Step [1 kHz▾]  Mode [USB▾]   │
│                                                                  │
│              CW Pitch [650 Hz▾]  SUB  1→2  2→1  ⇄                │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

Each LED is a full **FrequencyDisplay** widget:

* **Double-click + type** to enter a frequency directly.
* **Mouse-wheel on the digits** to tune (uses the per-VFO Step
  combo's value).
* **Click anywhere on the LED** to focus that receiver — see
  Focus below.

The two readouts behave identically.  Whichever one has the
**green border** is the *focused VFO* — the one your controls
operate on right now.

## Focus

At any moment one of the two VFOs is **focused** — RX1 by default.
The focused VFO is what the rest of the app considers "the
receiver you're working with."  This affects:

* The MODE+FILTER panel's mode picker shows the focused VFO's
  mode.  Changing it updates that VFO only.
* The DSP+AUDIO panel's AF Gain / AGC sliders read and write the
  focused VFO.
* The panadapter and waterfall show the focused VFO's band — click
  on a peak and that VFO tunes to it.
* Mouse-wheel tuning over the panadapter moves the focused VFO.
* Band buttons (40m / 20m / etc.), GEN1/2/3, TIME, Memory recalls,
  FT8 / NCDXF marker clicks, cluster spot clicks — all retune the
  focused VFO.

**Ways to change focus:**

* **Click on the other VFO's LED** — most direct.
* **Ctrl+1** → focus RX1; **Ctrl+2** → focus RX2.
* Middle-click on the panadapter swaps focus between VFOs (future
  build).

The **green border** marks the currently-focused LED.  RED is
reserved for the actively-transmitting VFO once TX ships in v0.2.

### Why focus instead of two panadapter panes?

Two panes were considered but rejected for v0.1.  A single
panadapter that follows focus is operationally simpler — your
eye is already at the VFO you're working, the spectrum is right
there.  Switching focus retunes the pane.  Most operators find
this less cognitively expensive than splitting their visual
attention across two simultaneous waterfalls.

A split-panadapter view may land in a later version if tester
feedback shows a real workflow need.  For most dual-RX use
cases — listening to two bands, working SPLIT, monitoring a net
while chasing DX — focus-driven single pane is enough.

## The inter-VFO control row

Centered under the Lyra logo:

### CW Pitch

Operator-tuned audio tone for CW signals (200..1500 Hz).  **Shared
across RX1 and RX2** — it's an ear-preference setting, not a
per-receiver state.  Drag the value or click the up/down arrows.
Lyra retunes both DDC0 and DDC1 in real time so the CW signal
stays in the filter passband at the new tone.

Always visible.  In non-CW modes the value is stored but has no
audible effect.

### SUB button

Enable dual-RX operation:

* **Off (default)** — Single-receiver mode.  Only the focused
  VFO is audible (mono, centered).  The other VFO is still
  *running* in hardware (its frequency / mode / BW are alive),
  just not feeding audio.
* **On** — Stereo split.  RX1 routes hard-left, RX2 hard-right.
  Both VFOs audible simultaneously.  The DSP+AUDIO panel's
  Vol RX1 / Vol RX2 sliders each control their own RX
  independently.

When SUB rises (off→on), Lyra **mirrors** RX1's current volume,
mute, and AF gain onto RX2 so you don't get a surprise blast from
RX2's last-saved levels.  You can adjust RX2 independently after.

### 1→2  /  2→1  buttons

Copy state between VFOs:

* **1→2** — Copy RX1 *to* RX2.  When SUB is **on**, full state copy
  (freq + mode + RX BW for the destination mode).  When SUB is
  off, frequency only (since RX2 is just a "shadow freq" for
  future SPLIT TX in that state).
* **2→1** — Mirror of 1→2 in the other direction.

### ⇄ button

Swap VFOs.  Full state swap when SUB is on; frequency-only swap
when SUB is off.  Classic VFO A/B exchange for switching which
band you're calling on.

## Per-VFO controls

Below each LED:

### Step combo

Wheel step size for that VFO's LED.  Eight presets from 1 Hz to
10 kHz.  Each VFO has its own — handy when you want RX1 stepping
in 1 kHz hops on a band-sweep while RX2 holds at 1 Hz on a
specific zero-beat target.

### Mode combo

Per-VFO mode picker.  Set RX1 to DIGU for FT8, RX2 to CWU for a
CW QSO on a different band — both run simultaneously, both feed
audio when SUB is on.

## SUB-off audio behavior

When SUB is **off** and you flip focus from RX1 to RX2, the
audible RX switches to RX2 immediately — no clicks, no jumps.
The Vol slider's value carries forward (RX2's volume is mirrored
from RX1's just before the switch) so audible level stays
consistent.

This is exactly what you want for "I'm chasing a station, let me
flip over to my other VFO for a second."  Click VFO B — the
panadapter retunes, the audio routes to RX2, the AF/AGC sliders
now operate on RX2.  Click VFO A — everything snaps back.

## Persistence

All RX2 state survives Lyra restarts:

* RX2 frequency, mode, per-mode bandwidth dictionary
* Vol RX2, Mute RX2, AF Gain RX2
* AGC profile RX2, AGC threshold RX2
* SUB on/off state
* Which VFO had focus

Quit Lyra in any configuration and relaunch — everything comes
back exactly as you left it.

## Keyboard shortcuts

| Key | Action |
|---|---|
| **Ctrl+1** | Focus RX1 |
| **Ctrl+2** | Focus RX2 |

The full keyboard reference is in the **Keyboard Shortcuts** topic.

## Limitations on the HL2

* **Same front-end filter bank.**  Both VFOs share the HL2's
  band-pass filters and LNA setting.  Tuning RX1 to 40m and RX2 to
  20m is fine *electrically*, but signal strength on the
  non-filter-band VFO will reflect roll-off.  Operating across
  adjacent bands (e.g. 20m and 17m) works well.
* **Same sample rate.**  HL2 protocol 1 ties the wire rate to RX1
  — both DDCs deliver at whatever you set for the main RX rate.
  Lyra handles per-DDC decimation host-side, so this is mostly
  invisible.
* **PureSignal interaction.**  When PS lands in v0.3, the HL2
  gateware re-routes DDC1 to the PA coupler during MOX+PS.  Lyra
  will pause RX2 reception during MOX+PS automatically and
  resume on PTT release.  This won't affect your saved RX2
  state.

## TX and SPLIT

TX is v0.2 work; SPLIT operation is on the roadmap.  When SPLIT
ships, you'll be able to transmit on VFO B while listening on
VFO A — the classic DX pile-up workflow.  The current SUB / 1→2 /
2→1 / ⇄ cluster will likely grow a SPLIT mode (operator UX
discussion ongoing).
