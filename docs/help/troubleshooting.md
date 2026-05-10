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

## HL2 audio jack distorted / chopped

Rare in current builds.  The HL2+'s onboard AK4951 codec is a
48 kHz audio chip (matches the HPSDR EP2 audio protocol slot,
also 48 kHz), and Lyra demodulates to 48 kHz audio regardless
of which IQ spectrum rate (48 / 96 / 192 / 384 k) you have
selected.  Audio path is independent of spectrum rate.

If you DO hear chopping or distortion on the HL2 audio-jack
output:

- Check that gateware is current (older HL2+ gateware had bugs
  in the EP2 audio routing).  Flash the current HL2+ gateware.
- Check **Volume** isn't past the saturation point on the
  DSP & AUDIO panel.
- Try briefly switching to **PC Soundcard** out, confirm the
  audio itself sounds clean there.  If PC is also chopped, the
  problem is in the demod path, not the HL2 jack specifically.
- Pre-v0.0.5 Lyra builds had broken auto-routing logic that
  flipped audio output between HL2 jack and PC Soundcard
  whenever you changed IQ rate or band.  If you're seeing
  inexplicable output flipping, upgrade to v0.0.5+.

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

## Audio sounds "pumped" — AGC is doing it

If you hear AGC pumping (the gain visibly riding speech
syllables or breaths), try a slower AGC profile:

1. Right-click the **AGC** cluster on the DSP & AUDIO panel.
2. Pick **Slow** for SSB, or **Long** for AM broadcast / steady
   carrier listening.  Faster profiles (Fast / Med) react too
   quickly on speech.
3. For digital modes (FT8 / FT4 / RTTY) where AGC pumping is
   especially distracting, just turn AGC **Off** — set AF Gain
   high enough to bring weak signals up to your decoder's
   level; AGC isn't doing you any favors there.

## ANF is killing my CW signal

CW dits are tones too — ANF's LMS predictor can lock onto them
just like a heterodyne. Aggressive μ values will eat fast keying.

**Fix:**

1. Right-click the **ANF** button on the DSP+Audio panel.
2. Pick **Light** — slow adapt rate, fast keying outpaces it.
3. Or simply **Off** — for CW you usually want
   [APF](./apf.md) to *boost* the pitch, not ANF to null it.

Rule of thumb: if you can hear ANF chewing on your CW, it's on
the wrong setting for that mode. Light or Off when listening
to CW you actually care about.

## ANF doing nothing on an obvious heterodyne

Two common causes:

1. **Adapt rate too slow** — try **Heavy** (μ ≈ 4×10⁻⁴), or
   pick **Custom** in Settings → Noise → μ slider and dial up.
2. **Heterodyne is too wide** — ANF works on narrow tones. A
   "carrier" with significant audio modulation looks broadband
   to the LMS predictor. Use a manual notch filter instead.

If the tone is brief/transient (under ~100 ms), even Heavy
profile may not have time to lock on. Brief tones aren't usually
worth notching — they're gone before you'd notice.

## ANF makes voice sound muffled / hollow

ANF is too aggressive — it's nulling vowel formants (the tonal
peaks in vowel sounds).

**Fix:**

1. Try **Medium** profile (μ ≈ 1.5×10⁻⁴) — fast enough for
   real heterodynes, slow enough that vowels survive.
2. If Medium still feels off, try **Light** (μ ≈ 5×10⁻⁵).
3. Or **Off** — manually notch known carriers via the spectrum
   right-click menu.

The tradeoff is fundamental: faster adapt rate kills more tones
but takes more bites out of speech. The Medium preset is
calibrated for typical SSB voice; only step up to Heavy on
bands where heterodynes are appearing/disappearing rapidly.

## NB is clipping my CW signal

Aggressive NB at low threshold can mistake the leading edge of a
strong CW dit for an impulse — especially if your band background
is very quiet and the signal is much stronger than the surrounding
noise.

**Fix:** raise the threshold or pick a gentler profile.

1. Right-click the **NB** button on the DSP+Audio panel.
2. Pick **Light** (12× background) — the highest preset.
3. If Light still clips, switch to **Custom** and use Settings →
   Noise → Threshold to dial in a value above 12 (try 15–20).

If Light + threshold ≥15 still clips, NB probably isn't right
for the situation — try turning it Off and using narrow filters
+ APF instead.

## NB is doing nothing on impulse noise I can clearly hear

Two common causes:

1. **Threshold too high** — try **Aggressive** (3× background) or
   pick **Custom** and dial the slider down.
2. **Impulses are wider than NB's blanking window** — NB caps
   consecutive blanked samples at 25 ms to protect legitimate
   signal. Storms with fast-repeating crashes (rapid CW-like
   QRN) can sometimes look continuous to the bg tracker. NR or
   a captured-noise profile may help instead/in addition.

If you switch to Aggressive and STILL hear no NB action, double-
check NB is actually on — the DSP-row NB button should be lit
(not dim).

## NB makes audio sound thin / hollow

NB is over-active. Either:
- Threshold is too low (catching too many normal noise excursions)
- Continuous wide-band crud is being interpreted as impulses

**Fix:**

1. Try **Light** profile first.
2. If Light still feels thin, switch NB **Off** and assess whether
   the band noise is genuinely impulsive or just sandpapery.
   Sandpapery / steady noise is NR's territory, not NB's.
3. Birdies from local PC hardware can persistently trigger NB.
   Notch them with a manual notch filter instead.

## Captured noise profile is muting real signals

> **v0.0.9.9 note:** captured-profile apply is now LIVE in the
> WDSP engine — the IQ-domain rebuild landed in v0.0.9.9
> (capture taps raw IQ pre-WDSP; apply runs Wiener-from-profile
> spectral subtraction also pre-WDSP, before WDSP's RXA chain
> sees the IQ).  The "use captured" toggle now actually does
> something.  Profiles captured in v0.0.9.6 / v0.0.9.7 / v0.0.9.8
> use the legacy v1 audio-domain format and will refuse to load
> with a "recapture in v0.0.9.9+" hint — recapture them in
> v0.0.9.9 to migrate.

If a captured profile makes your signal-of-interest quieter than
NR-off, the captured noise model probably contains some signal
energy. Most likely cause: a signal was actually present during
the capture window.

**Fix:** re-capture on a noise-only frequency.

1. Tune to a quiet patch on the band (5–10 kHz from any active
   station, or wait for a transmission gap).
2. Right-click the **📷 Cap** button → **Manage profiles…** →
   select the bad profile → **Re-capture**.
3. **Listen during the 2-second capture window** and watch the
   waterfall.  If you hear a signal pass through or see a
   spectrum spike, re-capture.  (Lyra used to flag this with a
   "smart-guard" check; it was removed in v0.0.9.5 because
   field testing showed it was unreliable — your ear is the
   better filter.)

You can also disable the captured profile temporarily by clicking
the **source badge** below the DSP buttons row to flip back to
**Live (VAD)**.  The captured profile stays loaded; you can flip
back to it later by clicking the badge again.

## Source badge stuck on Live, can't switch to Captured

The badge below the DSP buttons row only enables when a captured
profile is **loaded** in NR.

- If the badge text says **"no captured profile"** — capture one
  via the **📷 Cap** button, or load an existing one from the
  Manage Profiles dialog.
- If you previously deleted the active profile, the source toggle
  auto-flipped back to Live and the badge greyed out as a safety
  measure.
- If you have profiles on disk but none loaded (e.g. after a Lyra
  reinstall), use **Manage profiles…** → select one → **Use Selected**.

## Profile manager shows greyed/strikethrough profiles

A profile is shown greyed-out with strikethrough when Lyra can't
load it. Two reasons:

- **Legacy v1 audio-domain profile** (captured in v0.0.7 –
  v0.0.9.8). Hover the strikethrough name and the tooltip
  reads: *"Recapture in v0.0.9.9+ to use the new IQ-domain
  noise-reduction engine."* The v1 format isn't applicable in
  the new IQ-domain pipeline, and Lyra refuses to load it
  rather than producing nonsense output. Recapture on the same
  band you used for the old profile and you're back in
  business.
- **Unknown schema** — if a JSON in your profile folder claims
  a schema version Lyra doesn't recognize (e.g., a future
  build's profile copied into your library), it's also greyed
  with a "schema not supported" tooltip.

**Rate / FFT-size mismatches** (v2 profile from a different IQ
rate or a different FFT-size setting) are NOT greyed in the
manager — they look loadable, but clicking **Use Selected**
produces a clean error message ("captured at X Hz, current
rate is Y Hz, switch back or recapture") instead of silently
plausible-but-wrong output. Switch the radio to the matching
IQ rate or recapture at the current rate.

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
