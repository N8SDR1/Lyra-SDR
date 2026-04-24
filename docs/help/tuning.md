# Tuning

*(Tip: click [this link](panel:tuning) to flash the TUNING panel in
the main window — handy if you're not sure where it is among your
docked panels.)*

## The frequency display

The big amber 7-segment readout on the [TUNING panel](panel:tuning)
is the RX frequency in `MMM.kkk.hhh` format (MHz.kHz.Hz).

Click any digit to **select** it (a small tick appears above the
digit). Then:

- **Mouse wheel** — increment/decrement the selected digit.
- **↑ / ↓ arrows** — same, keyboard-only.
- **Page Up / Page Down** — jumps ten steps at the selected digit
  size.

Click a lower-order digit for finer tuning. The lowest "Hz" digit is
**1 Hz** tuning — useful for CW zero-beat or precise WSPR alignment.

## Typing a frequency

The small editable field above the display accepts a frequency in
**MHz**. Type e.g. `14.074` → Enter to jump straight to FT8 on 20 m.
The display updates and the radio re-tunes.

## Bands

The **BAND** panel has quick-pick buttons for every amateur HF band
plus common broadcast segments. Clicking a band jumps to the last
frequency you were on in that band (per-band memory).

If you haven't visited a band before, you get a reasonable default
(e.g. middle of the phone sub-band).

## Panadapter click-tune

Left-click anywhere on the spectrum or waterfall to tune that
frequency. The clicked point becomes the new center.

Mouse wheel on the spectrum zooms in/out (when not over a notch —
notches claim the wheel for Q adjustment).

## VFO lock / offset

Not yet implemented. On the backlog for when the TX path goes in
(RIT/XIT + split operation).
