# S-Meter

*(Tip: [click here](panel:meters) to flash the Meters panel if you
can't find it among your docked windows.)*

## Styles

Three styles, switchable via the chip-row in the Meters panel header
(`Lit-Arc | LED | Analog`). Click any chip to swap instantly. Choice
persists across launches.

### Lit-Arc *(default)*
Curved analog-style face with **NO needle** — instead a row of ~80
small radial segments traces the arc and lights up cumulatively from
the left up to the current value. The lit/unlit boundary IS the
"needle position", but with explicit segment-count accuracy and zero
needle-tremor on fast-updating signals.

A brighter "peak hold" segment lingers at the highest recent value
and decays back over ~1.5 seconds, so transient spikes are clearly
visible.

**Mode switching by clicking the chips at the top of the meter** —
three modes available now:

| Chip | Scale | Use |
|---|---|---|
| **S** | S0..S9+30 (S-units) | The classic ham reading. Each unit = 6 dB; S9+30 means 30 dB above S9. |
| **dBm** | −127..−43 dBm | Same data, dBm scale label. S9 = −73 dBm reference is at the same arc position. |
| **AGC** | 0..60 dB | Current AGC compression amount in dB. Useful for diagnosing whether AGC is doing useful work or just sitting idle. |

Each mode has its own color palette so a glance at the meter tells
you which mode it's in without reading the label:

- **S / dBm** — deep green → bright green → amber → red as signal
  strength climbs the arc
- **AGC** — deep blue → cyan → near-white-blue (cool palette,
  visually distinct from the warm signal-strength palette)

A large amber LCD-style **numeric readout** at the bottom of the
meter shows the exact value (e.g. `S9+12`, `−61 dBm`, `+18.5 dB`).

### LED bar-graph
Modern Icom/Yaesu aesthetic — segmented colored bars. Stacked rows
for different meter types (S-meter during RX; PWR, SWR, ALC, MIC
during TX when the TX path ships).

### Analog needle *(legacy — slated for removal)*
Classic Kenwood/Yaesu aesthetic — shallow-arc dial with concentric
scales, cream face with lit-amber markings on black. Single white
needle tracks the signal level. **Will be removed in a future
release** once the new Lit-Arc style is settled in operator hands;
kept now as a fallback during the transition.

All three styles share the same underlying data feed — switching is
purely visual and does not affect the meter's data path.

## Calibration

S-meter follows the standard **S1 = −121 dBm, 6 dB per S-unit**
convention above the preamp stage. HL2 gain setting is compensated
so S-readings are consistent across different RF gain values.

### S-meter cal trim

A per-rig dB offset can be applied to the meter reading independent
of the spectrum scale. Two ways to set it:

**Settings → Visuals → Spectrum calibration → S-meter slider**
A −40..+40 dB slider with double-click to recenter. Useful when
you know the offset you want from prior measurement.

**Right-click on the meter face → "Calibrate to current = …"**
The fast operator workflow:

1. Pipe a known-amplitude signal into the antenna (signal generator,
   or a known reference signal on-air like WWV at 10 MHz).
2. Right-click the meter and pick the matching reference:
   - "Calibrate so current reads S9 (−73 dBm)"
   - "Calibrate so current reads S5 (−97 dBm)"
   - "Calibrate so current reads S3 (−109 dBm)"
   - "Calibrate so current reads S1 (−121 dBm)"
   - or **"Calibrate to specific dBm…"** for any other value
3. The offset auto-adjusts so the next reading matches the reference.

Right-click → **"Reset cal to 0 dB"** to clear the trim back to
default. The current trim is shown in the menu so you can see what's
applied at any time.

### S-meter cal vs spectrum cal — when to use which

**Spectrum cal** (Settings → Visuals → Spectrum calibration → Cal
slider) shifts the WHOLE spectrum scale and the S-meter reading
together. Use it to compensate for known signal-path losses
(preselector, antenna efficiency, cable loss).

**S-meter cal** shifts ONLY the meter reading, NOT the spectrum
scale. Use it for the final S-meter calibration after spectrum cal
is set — gets the meter to read S9 = −73 dBm without re-shifting
the spectrum y-axis you just calibrated.

## Response mode — Peak vs Average

The S-meter has two response modes, switchable via right-click on
the meter face → **Response mode** submenu:

| Mode | Computation | Best for |
|---|---|---|
| **Peak** *(default)* | Instantaneous maximum FFT bin in the RX passband | Quick read of the strongest signal — shows transients (CW dits, FT8 tones, lightning pops) clearly. |
| **Average** | Mean of all passband bins in linear power (then back to dB), smoothed with a ~5-frame EWMA | Steadier reading more representative of the signal level the AGC sees. Better for setting AF gain or comparing band noise floors over time. |

Why average is computed in **linear power** rather than averaging
dB values directly: dB averaging would weight dim bins too heavily.
A strong signal in noise would barely register above the
band-mean. Linear-power averaging gives a meter reading that
tracks actual energy in the passband.

The mode persists across launches via QSettings.

## Moving / resizing the meter

The S-Meter panel is a **dockable widget**. Drag the title bar to
undock it into a floating window. Resize by dragging the edges. Snap
back by dragging the title bar into any dock area.

**View → S-Meter** toggles its visibility.

## TX-side meters (future)

When TX ships, the meter will auto-switch to show:

- **PWR** — forward power, calibrated watts
- **SWR** — reflected/forward ratio
- **ALC** — automatic level control headroom
- **MIC** — mic input level (for mic gain setup)
- **PROC / MONI / CH1** — indicators for speech processor / side-tone

All sharing the same multi-meter data feed and user-selectable style.
