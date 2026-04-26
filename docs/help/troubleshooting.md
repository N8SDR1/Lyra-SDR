# Troubleshooting

## Auto-discover doesn't find my HL2

**Open Help → Network Discovery Probe…** That dialog:

- Lists every IPv4 network interface on your PC (so you can see
  what subnet your Lyra machine is on)
- Runs discovery with full diagnostic logging — shows which
  interfaces it broadcast on, whether any replies came back, and
  parses any HL2 responses
- Lets you try a **unicast probe** to a specific IP (bypasses
  broadcast entirely — useful when you know the HL2's IP from
  the rig's display or from your router)
- Has a **Copy log to clipboard** button so you can paste the
  diagnostic output into a bug report

Common causes (and what the probe shows):

| Symptom | Likely cause | Fix |
|---|---|---|
| No replies on broadcast, but unicast to known IP works | PC and HL2 on different subnets, OR Wi-Fi-vs-Ethernet routing mismatch | Move both to same subnet, or tell Lyra the IP directly via Settings → Radio |
| No replies even on unicast | Firewall blocking inbound UDP 1024, or HL2 not powered, or HL2 on a separate VLAN | Allow `python.exe` / `Lyra.exe` in Windows Defender Firewall; check HL2 power + cable |
| Reply with `BUSY` flag | Another SDR client (Thetis, SparkSDR) already connected | Close the other client first |
| Reply but wrong board name | Multiple HL2-family devices on the network | Use unicast to target the specific one you want |

Lyra now broadcasts on **every** local IP interface in parallel
(fixed in v0.0.4 — earlier builds only used the OS's preferred
interface, which broke multi-NIC laptops with Wi-Fi + Ethernet).
If you previously had to manually enter an IP, try ▶ Start now —
auto-discover should work.

## No signal / blank spectrum after Start

1. **Status dot green?** — if still gray, the HL2 isn't replying.
   Check the IP in Settings → Radio; try **Discover** or open
   **Help → Network Discovery Probe** for diagnostics.
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

## Lyra started up looking weird (panels hidden, scale off-screen, can't drag splitters)

Three escape hatches, in order of preference:

### 1. Toolbar → "Reset Panel Layout"  *(preferred — one click)*

Always restores the **factory** arrangement (Tuning + Mode + View
on top, Band + Meters split, DSP+Audio at bottom). Never tries to
load a saved layout — so even if your saved layout is corrupted,
this works. The status bar will say "Panel layout reset to factory
defaults" when it fires.

Lyra also has a **sanity check on auto-save**: if the layout is
broken at close-time (any panel < 80×50 px, central widget < 200×120
px, or main window < 600×400 px), Lyra refuses to overwrite the
saved `dock_state` with the broken one. So a single bad close can no
longer trap you on the next launch — the previous good state is
preserved.

### 2. File → Snapshots ▸ → "yesterday at HH:MM"

If Reset Layout isn't enough (e.g., your color picks went weird, or
some non-layout setting got hosed too), pick an automatic snapshot
from before the breakage. Lyra takes one every launch and keeps the
last 10. A safety snapshot of your CURRENT state is taken first so
the rollback is reversible.

### 3. Manual QSettings nuke  *(last resort)*

If neither of the above works (very rare — possible if QSettings
itself got corrupted), close Lyra and run this from a Command Prompt:

```bat
python -c "from PySide6.QtCore import QSettings; s=QSettings('N8SDR','Lyra'); [s.remove(k) for k in ('dock_state','center_split','user_default_dock_state','user_default_center_split','geometry')]; s.sync(); print('Layout keys cleared - relaunch Lyra')"
```

That deletes only the 5 layout-related keys; everything else
(IP, audio device, AGC profile, color picks, balance, cal trim,
etc.) is untouched. Relaunch Lyra and you'll get a clean factory
layout you can re-customize.

### Preventing the panic in the first place

Two View-menu features help avoid layout breakage:

- **View → Lock panels (Ctrl+L)** — freezes panel title bars so
  you can't drag a panel by accident while reaching for some other
  control. Splitter resize between adjacent panels still works.
- **View → Save current layout as my default** — captures your
  preferred arrangement. Use **View → Restore my saved layout** to
  return to it any time (separate from Reset, which always goes to
  factory). Saving refuses to capture a degenerate layout, so you
  can't accidentally save a broken one as your default.

## Something else is broken

Save a **per-session log** (backlog feature — not yet implemented)
and file a bug. For now: console output + `mem` + screenshot.

If the issue is configuration-related and you can repro it on
demand, **export your settings via File → Export settings…** and
attach the JSON to your bug report — saves a lot of back-and-forth
diagnosing.
