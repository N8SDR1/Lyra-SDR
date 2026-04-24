# Troubleshooting

## No signal / blank spectrum after Start

1. **Status dot green?** — if still gray, the HL2 isn't replying.
   Check the IP in Settings → Radio; try **Discover**.
2. **Firewall** — Windows Defender may be blocking inbound UDP 1024
   for `python.exe`. Allow it.
3. **Duplex bit** — Lyra sets C4 bit 2 (full-duplex) automatically.
   If you're seeing "tuning has no effect", something may have
   stomped this. Stop, restart the stream.
4. **Gateware** — if you have an HL2+ with a very old gateware, the
   AK4951 features won't work. Flash the current HL2+ gateware.

## AK4951 audio is distorted / chopped

Check the **Rate** combo on Mode+Filter. AK4951 output only works at
**48 kHz IQ rate**. At 96 / 192 / 384 k the EP2 audio drain rate
outruns the 48 kHz demod output and the gap fills with zeros → the
audible result is a square-wave chop. Drop Rate to 48 k.

Recent Lyra builds automatically route audio to PC Soundcard when
Rate > 48 k, and block manual AK4951 selection in that state — but
if you saved a config before that guard was in place, you might
still see the old behavior. Just drop the rate, then pick AK4951.

## Audio silent but spectrum is alive

- **MUTE lit** — the MUTE button on the DSP & AUDIO panel is
  checked (orange). Click to un-mute.
- **Mode is "Off"** — set a real demod mode.
- **Volume slider at 0** — DSP & AUDIO panel, check Volume.
- **Wrong output device** — Settings → Audio → Output Device.
- **AGC off + weak signal** — bring volume up or switch AGC to Med.

## Audio stutters during window resize

Known issue — the demod runs on the Python main thread, which blocks
during Qt paint events. Workarounds:

- Don't resize while listening to weak signals.
- Close the waterfall panel (**View → Waterfall**) to cut FFT CPU load.

Permanent fix is on the backlog: OpenGL/Vulkan panadapter backend
and/or threaded demod.

## AGC is pumping

- On FT8 or fast-decaying signals, switch to **Slow** profile.
- On CW, switch to **Fast** profile.
- On a strong fading signal, try **Auto** — the threshold will
  follow the envelope.

## Notches don't work

- Check the **NF** button (or the separate **Notch** button on DSP +
  Audio) is lit. When it's off, notches are bypassed in the DSP path
  — they're still saved, but don't attenuate anything.
- Check you haven't accidentally removed them with Shift + right-click
  (the quick-remove gesture, active only when NF is on).
- Notches are per-session right now — closing and reopening Lyra
  drops them. Per-band notch memory is on the backlog.
- If **right-click** on the spectrum doesn't show the notch menu,
  turn on Notch first — right-click is gated on NF state so the
  gesture stays free for other spectrum features when NF is off.

## TCI client can't connect

- Settings → Network/TCI — verify the server is enabled.
- Port 40001 not already used? Change to 40002+ if needed.
- Windows firewall may be blocking localhost WebSocket. Allow
  `python.exe` for inbound connections.

## USB-BCD toggle is greyed out

Lyra needs to see an FTDI FT232R device present. Check:

- Cable plugged in and recognized by Windows (check Device Manager
  for an FTDI entry).
- FTDI D2XX driver installed (`ftd2xx` Python package depends on
  FTDI's native driver, not VCP).
- Try unplugging and replugging the cable, then restart Lyra.

## Signals appearing on the "wrong side" of the carrier

Fixed 2026-04-24 — the HL2's baseband IQ stream is spectrum-mirrored
relative to sky frequency (USB signals deliver as negative baseband
bins). Earlier Lyra builds fed that straight to the panadapter, so
USB signals showed to the LEFT of the carrier instead of the right.
If you saw FT8 (USB mode) appearing to the left of 7.074, that's why.

The demod path always handled the mirror correctly for audio via
SSBDemod's sign-flip, but the panadapter display was uncorrected.
Current builds flip the FFT after `fftshift` so the panadapter
matches sky-frequency convention and the RX filter passband overlay
sits over the signals it's actually filtering.

If you upgrade and your previously-placed notches visually jump to
the opposite side of the carrier, delete them and re-place on the
corrected display — they were set against the mirrored view.

## Strong local AM station bleeding in

If you're near a high-power AM broadcast transmitter, its 5th
harmonic often lands on 40 m (N8SDR's station, for example, has a
5th harmonic at 7.250 MHz from a local BCB carrier). Mitigations:

- Enable the **N2ADR filter board** if you have one — the low-pass
  chain for 40 m blocks out-of-band BC energy.
- Drop the LNA slider on the [DSP & AUDIO panel](panel:dsp) — HL2
  ADC overloads cause spurious
  products all across the spectrum.
- Place a **notch** on the offending carrier.

## Something else is broken

Save a **per-session log** (backlog feature — not yet implemented)
and file a bug. For now: console output + `mem` + screenshot.
