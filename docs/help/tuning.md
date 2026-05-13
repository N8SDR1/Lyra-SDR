# Tuning

*(Tip: click [this link](panel:tuning) to flash the TUNING panel in
the main window — handy if you're not sure where it is among your
docked panels.)*

> **Two receivers, one panel.**  Starting in v0.1, the TUNING
> panel hosts BOTH receivers' frequency displays side by side
> (RX1 left, RX2 right, Lyra logo center).  Most of this page is
> written from RX1's perspective — everything applies to RX2 too,
> just on the right-hand LED.  See the **RX2 (Dual Receiver)**
> topic for the dual-receiver workflow specifics: focus model,
> SUB stereo split, VFO bridge buttons, and per-VFO mode/step.

## The frequency displays

Each receiver gets its own big amber 7-segment readout — RX1 on
the left, RX2 on the right.  Format is `MMM.kkk.hhh`
(MHz.kHz.Hz). Dots are thousand-separators (rig-style) — so
`7.125.000` reads "seven million one hundred twenty-five thousand
Hz" = 7.125 MHz.

The **focused VFO** has a green border around its LED.  Click the
other LED (or press **Ctrl+1** / **Ctrl+2**) to swap focus.
Frequency-entry surfaces (mode picker, panadapter click-to-tune,
band buttons, GEN/Memory recalls, etc.) all operate on whichever
VFO is currently focused.

**What the LED represents.**  In every mode — SSB, CW, AM, FM,
digital — the LED reads the **carrier frequency** of the signal
you're tuned to.  In CW modes (CWU / CWL), Lyra automatically
shifts the actual hardware DDS by your configured CW pitch
offset on the receive side so the carrier lands inside the CW
filter and you hear it as a tone at the configured pitch.  This
applies to both VFOs independently.

## Mouse wheel — three-tier behavior

The wheel uses the most precise specifier available, in this order:

1. **Hovering a specific digit** → that digit's place value wins.
   Hover the kHz digit and wheel = 1 kHz per click. Hover the Hz
   digit = 1 Hz per click. Use this for precision aim.
2. **Hovering anywhere else on the display** → uses the **Step**
   combo's current value. Pick "100 Hz" in the Step dropdown and
   any wheel scroll on the LED body steps 100 Hz per click. This
   is your "default tuning resolution".
3. **No Step combo set, but a digit is currently selected** (from a
   previous click) → uses that digit's place value as a fallback.

Combo as default tuning resolution, per-digit hover as
override-by-precision-aim — the standard SDR-client convention.

## Step combo

Each VFO has its own **Step** dropdown directly under its LED.
Eight presets:

| Step | Use |
|---|---|
| **1 Hz** | CW zero-beat, WSPR precise tune |
| **10 Hz** | CW QSOs, fine SSB |
| **50 Hz** | Sub-band navigation |
| **100 Hz** | SSB voice tune-around |
| **500 Hz** | SSB hop |
| **1 kHz** *(default)* | Quick band sweeping |
| **5 kHz** | Channel-style hopping |
| **10 kHz** | Cross-band scanning |

Picking a step here drives the wheel-on-empty-space behavior (see
above) for that VFO's LED.  Each VFO's Step combo is independent
— useful when RX1 is on a band-sweep at 1 kHz while RX2 holds
zero-beat on a CW target at 1 Hz.

## Keyboard

Click a digit to select it (a small tick appears above), then:

- **↑ / ↓ arrows** — increment/decrement the selected digit's place
- **← / → arrows** — move selection one place left/right
- **Page Up / Page Down** — jumps ten steps at the selected digit's
  place value

## Direct frequency entry — three options

### Option A: Double-click the LED display

Double-click anywhere on the big amber readout. A black inline text
input appears with cyan border, pre-filled with the current
frequency. Type a new frequency in any reasonable format:

| Type this | Get this |
|---|---|
| `7.125` | 7.125 MHz |
| `7,125` | 7.125 MHz (Euro decimal style) |
| `7.125.000` | 7.125 MHz (display format — dots as thousand separators) |
| `7,125,000` | 7.125 MHz (commas as thousand separators) |
| `7125000` | 7.125 MHz (raw Hz) |
| `7125` | 7.125 MHz (mid-range bare → kHz) |
| `7` | 7 MHz (small bare → MHz) |

Press **Enter** to commit, **Esc** to cancel. Click outside the
field also commits — anything you typed gets applied (unless it's
unparseable, in which case the entry is silently ignored and the
freq stays put).

The flexible parser means you can type the same format the LED
displays — see "7.200.000", retype "7.125.000", press Enter, done.

Works on **either** LED — double-click RX1 to enter an RX1 freq,
double-click RX2 to enter an RX2 freq.

### Option B: Click-to-tune on the panadapter

Left-click anywhere on the spectrum or waterfall and the
currently-focused VFO retunes to that frequency. Best for visual
band hunting — you can SEE the signal you want to land on before
clicking it.

### Option C: Click-and-drag the spectrum (pan tuning)

Press and hold left button on empty spectrum (or waterfall), then
drag horizontally — the spectrum slides under your cursor like
dragging a Google Maps view. Drag right and lower frequencies come
into view from the left; drag left and higher frequencies appear
from the right. Release to settle on the new center.

The cursor changes to a hand to telegraph "you're in pan mode." A
small dead-zone (≈5 px) lets a quick click still snap-tune to the
exact cursor frequency without entering pan mode by accident.

Pan-tune is gated on left-click in **empty** spectrum — clicks on
notches, landmark triangles, the dB-scale strip, or passband edges
keep their existing drag-to-resize behavior.

## Bands

The **BAND** panel has quick-pick buttons for every amateur HF band
plus common broadcast segments.  Clicking a band jumps the
**currently focused VFO** to the last frequency you were on in
that band (per-band memory). If you haven't visited a band before,
you get a reasonable default (middle of the phone sub-band, FT8
frequency for digital, etc.).

Same logic applies to **GEN1 / GEN2 / GEN3**, **TIME**, and the
**Mem** memory bank — they all retune the focused VFO.  Click the
other VFO's LED first if you want the band button to retune that
receiver instead.

## Mouse wheel on the panadapter

- **Over empty spectrum** → tunes the **focused VFO** by the
  **Panafall Step** set on the Display panel (default 1 kHz; presets
  100 Hz / 500 Hz / 1 kHz / 5 kHz / 10 kHz / 25 kHz / 100 kHz).
  Wheel up = freq up.  Click the other LED to switch which VFO
  the wheel moves.
- **Ctrl + wheel over empty spectrum** → zooms bandwidth (escape
  hatch for the legacy zoom gesture; same 1× / 2× / 4× / 8× / 16×
  preset levels as the Display panel Zoom combo)
- **Over a notch rectangle** → adjusts that notch's width (down =
  wider, up = narrower)
- **Over the LED freq display** → tunes (see "three-tier behavior"
  above)

### Exact / 100 Hz quantization

The toggle button next to the Panafall Step combo controls whether
panadapter freq-set actions land on a clean grid:

- **Exact** (default): wheel-tune, click-tune, drag-pan, and
  Shift+click peak-snap set the VFO to the exact freq derived from
  the gesture (e.g. 7.155.232 MHz).
- **100 Hz**: the result freq rounds to the nearest 100 Hz grid
  using half-up rounding:
    - 7.155.232 → 7.155.200 (32 < 50, down)
    - 7.155.251 → 7.155.300 (51 ≥ 50, up)
    - 7.155.250 → 7.155.300 (exact .50, up)

Independent of the Panafall Step combo — Step controls the per-tick
increment, this controls whether the FINAL freq lands on a 100 Hz
grid.  First wheel tick after enabling 100 Hz snaps to grid;
subsequent ticks step cleanly.

Direct freq entry, memory recall, band buttons, and CAT writes are
NOT affected — exact-precision tuning paths stay exact regardless
of this toggle.

## CW Pitch, SUB, and VFO bridge buttons

Centered under the Lyra logo on the TUNING panel:

* **CW Pitch** — operator-tuned audio tone for CW signals
  (200..1500 Hz).  Shared across both receivers since it's an
  ear-preference setting.  Always visible.  See the **RX2** topic
  for the full per-RX CW pitch + DDS handling.
* **SUB** — toggle dual-RX audio routing.  Off = mono, focused
  VFO is audible.  On = stereo split, RX1 left + RX2 right with
  independent Vol/Mute on the DSP+AUDIO panel.  See **RX2**.
* **1→2** — copy RX1 to RX2.
* **2→1** — copy RX2 to RX1.
* **⇄** — swap RX1 and RX2 (frequency-only when SUB is off, full
  state when SUB is on).

## VFO lock / split / RIT / XIT

Lock / RIT / XIT are not yet implemented; on the backlog for when
the TX path goes in.  SPLIT (TX on VFO B while RX on VFO A) is
also TX-side work — operator UX discussion ongoing for whether to
fold it into the existing SUB button as a tri-state SUB/SPLIT/OFF
control.
