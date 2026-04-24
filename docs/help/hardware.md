# External Hardware

## N2ADR filter board

The N2ADR filter board is a low-pass filter bank driven by the HL2's
Open-Collector (OC) outputs. When enabled, Lyra automatically sets
the correct 7-bit OC pattern for the current RX frequency.

**Enable:** Settings → **Hardware** → N2ADR filter board toggle.

**Per-band OC patterns** follow N2ADR's standard assignment:

| Band | OC bits (LSB = bit 1) |
|------|-----------------------|
| 160 m | bit 1 |
| 80 m  | bits 2 + 7 |
| 60/40 m | bit 3 |
| 30/20 m | bit 4 |
| 17/15 m | bit 5 |
| 12/10 m | bit 6 |
| 6 m   | bit 7 |

The current pattern is shown at the bottom of the Hardware tab as
both raw bits and a human-readable label.

## USB-BCD for linear amplifier band switching

**⚠ Safety warning**: the HL2 has **no native BCD output**. Linear
amplifiers that use Yaesu-standard 4-bit BCD for automatic band
selection need an **FTDI FT232R-based USB-BCD cable** that Lyra
drives via FTDI's D2XX bit-bang interface.

**Without this cable, your amp will not auto-switch.** Transmitting
into the wrong filter/PA matching network at high power can destroy
LDMOS devices and output filters. **Always verify the amp is on the
correct band before keying up.**

### Enabling USB-BCD

Settings → **Hardware** → **USB-BCD for linear amp**.

The toggle is **disabled** unless Lyra can see an FTDI device — no
cable = no toggle. This is a safety interlock.

When the stream stops, the cable is reset to BCD=0 (all bits low) so
a powered amp won't remain in a stale band state.

### BCD mapping (Yaesu standard)

| Band  | BCD (bits 3..0) |
|-------|------------------|
| 160 m | 0001 |
| 80 m  | 0010 |
| 40 m  | 0011 |
| 30 m  | 0100 |
| 20 m  | 0101 |
| 17 m  | 0110 |
| 15 m  | 0111 |
| 12 m  | 1000 |
| 10 m  | 1001 |
| 6 m   | 1010 |

### 60 meters

60 m was never part of the original Yaesu BCD standard. The **60 m
uses 40 m BCD** toggle (default on) makes 60 m operation use 40 m's
BCD code — most amps cover both bands with the same input filter.
Turn it off if your amp has a dedicated 60 m setting.

## AK4951 audio (HL2+)

See **Audio Routing** for how RX audio flows through the AK4951 and
into the PC line-in instead of being decoded on the PC.
