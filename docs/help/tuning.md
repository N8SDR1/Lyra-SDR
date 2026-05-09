# Tuning

*(Tip: click [this link](panel:tuning) to flash the TUNING panel in
the main window — handy if you're not sure where it is among your
docked panels.)*

## The frequency display

The big amber 7-segment readout on the [TUNING panel](panel:tuning)
is the RX frequency in `MMM.kkk.hhh` format (MHz.kHz.Hz). Dots are
thousand-separators (rig-style) — so `7.125.000` reads "seven
million one hundred twenty-five thousand Hz" = 7.125 MHz.

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

Dropdown next to the MHz spinbox on the TUNING panel. Eight presets:

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
above) AND sets the MHz spinbox's step size to the same value.

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

### Option B: The MHz spinbox

Small editable field next to the LED display. Accepts a value in
**MHz** with up to 6 decimals. Type `14.074050` → Enter to jump to
14,074,050 Hz. Has up/down spinner buttons that step by the
currently-selected Step combo value.

### Option C: Click-to-tune on the panadapter

Left-click anywhere on the spectrum or waterfall and the radio
re-tunes to that frequency. Best for visual band hunting — you can
SEE the signal you want to land on before clicking it.

### Option D: Click-and-drag the spectrum (pan tuning)

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
plus common broadcast segments. Clicking a band jumps to the last
frequency you were on in that band (per-band memory). If you haven't
visited a band before, you get a reasonable default (middle of the
phone sub-band, FT8 frequency for digital, etc.).

## Mouse wheel on the panadapter

- **Over empty spectrum** → tunes the VFO by the **Panafall Step**
  set on the Display panel (default 1 kHz; presets 100 Hz / 500 Hz /
  1 kHz / 5 kHz / 10 kHz / 25 kHz / 100 kHz).  Wheel up = freq up.
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

## VFO lock / split / RIT / XIT

Not yet implemented. On the backlog for when the TX path goes in.
