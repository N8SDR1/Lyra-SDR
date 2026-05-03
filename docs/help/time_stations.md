# Time Stations (TIME button)

*(Tip: click [this link](panel:bands) to flash the BANDS panel — the
**TIME** button sits between **GEN3** and **Mem**.)*

## What it does

Press **TIME** to jump to a known HF time / standard-frequency
broadcaster.  Press it again to advance to the next station in the
cycle.  When you reach the end of the list, the next press wraps
back to the start.

Time stations are useful as test signals — they're on the air around
the clock, modulated with voice ("At the tone, twelve hours,
twenty-three minutes, Coordinated Universal Time...") and a tick
every second, so you have an easy way to verify your RX chain end
to end.

## The cycle (9 stations)

| Freq (kHz) | Station | Country | Notes |
|---:|---|---|---|
| 2500   | WWV / WWVH | USA / Hawaii | Limited daytime; best at night |
| 3330   | CHU        | Canada       | French/English voice |
| 5000   | WWV / WWVH | USA / Hawaii | Universal coverage |
| 7850   | CHU        | Canada       | Best Canadian coverage |
| 10000  | WWV / WWVH | USA / Hawaii | Strongest US time signal |
| 14670  | CHU        | Canada       | Daytime DX |
| 15000  | WWV / WWVH | USA / Hawaii | Daytime DX |
| 20000  | WWV        | USA          | Day/limited; solar conditions |
| 25000  | WWV        | USA          | Solar-dependent reception |

Mode is automatically set to **AM** with a **6 kHz** filter — the
correct settings for these double-sideband AM time signals.

## Country-aware ordering

Lyra reads your operator callsign (Settings → Operator) and uses the
DXCC database to map it to a country code.  The cycle then starts
from the closest station to your country first:

- **US callsigns** (W / K / N / A prefixes) start the cycle on
  WWV 5 MHz and step through US stations first, then CHU.
- **Canadian callsigns** (VE / VA prefixes) start on CHU 7850 and
  step through Canadian stations first, then WWV.
- **All other callsigns** fall back to ascending frequency order.

This way, the first **TIME** press is most likely to land on a
station you can actually hear from your QTH, instead of dropping
you on a fading-out 2500 kHz daytime signal.

## When TIME is most useful

- **Verifying your RX chain after a hardware change.** Tune the next
  TIME station, hear the tick — you know audio path is intact end
  to end.
- **Checking propagation in a hurry.** Press TIME a few times across
  bands; if 5 MHz is loud and 15 MHz is silent, you have a quick
  read on the day's HF conditions.
- **Calibrating your radio's frequency reference.** WWV's carrier
  is held to better than 1 part in 10¹². If you can hear the carrier
  cleanly, your frequency display is showing the truth.

## Related buttons

- **GEN1 / GEN2 / GEN3** — three customizable presets you can save
  any frequency / mode / filter to (right-click to save current
  state).
- **Mem** — recall one of 20 named memory presets (your saved
  favorites — see the [Memory presets](memory.md) topic).
