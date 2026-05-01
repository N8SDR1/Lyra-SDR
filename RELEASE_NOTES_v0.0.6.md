# Lyra-SDR v0.0.6 — "Operator Awareness"

Released: pending tester pass

This release is a deep DSP refresh and the introduction of all-station
awareness features.  Headlines:

- **Five WDSP modules ported with attribution.**  Lyra is now GPL v3+
  (since v0.0.6 — see `NOTICE.md`); this release exercises the
  license-compatibility opening with substantial work pulled from
  Warren Pratt's WDSP.
- **All-mode squelch (SSQL-style).**  Voice-presence detector that
  works on SSB, AM, FM, and CW.  Mutes between transmissions on any
  modulation type.
- **LMS adaptive line enhancer (NR3).**  Pulls weak CW out of broadband
  hiss — the algorithmic class WDSP calls "ANR".  Block-LMS optimization
  drops CPU to ~4% real-time.
- **Weather Alerts.**  Toolbar indicator + desktop toast for lightning
  and high-wind conditions.  Sources: Blitzortung, NWS, Ambient WS-2000,
  Ecowitt — pulled from the author's sister projects (WX-Dashboard,
  SDRLogger+).
- **NR2 deep upgrades.**  Added Martin (2001) minimum-statistics noise
  PSD, speech-presence-probability soft mask (witchHat), AEPF
  median-smoothing post-filter, and a Wiener-vs-MMSE-LSA gain-function
  selector.  Replaces our v0.0.5 simplified Ephraim-Malah with the full
  WDSP-equivalent stack.
- **Operator/Station globals.**  Callsign + Maidenhead grid square +
  manual lat/lon backup live in Radio Settings now, consumed by
  TCI spots, weather alerts, and any future feature that needs your
  station's location.

## DSP changes

### Noise Reduction
- **NR1 (spectral subtraction)** — replaced dead-on-arrival VAD-gated
  noise tracker with min-statistics (Martin 2001).  Continuous-strength
  slider (0–100) replaces the old Light/Medium/Heavy radio buttons.
- **NR2 (Ephraim-Malah)** — Martin minimum-statistics noise PSD,
  AEPF post-filter, speech-presence probability soft mask, and a
  runtime Wiener-vs-MMSE-LSA gain-function picker (right-click the
  NR2 strength slider).
- **NR1 + NR2** captured-noise profiles work with the full new stack;
  Martin tracker still runs in the background as live-mode fallback.

### LMS Line Enhancer (new)
- Pratt-style normalized LMS with adaptive leakage — port from WDSP
  `anr.c` with attribution.
- Slots ANF → LMS → NR in the audio chain, independent enable.
- Strength slider and right-click presets on the DSP+Audio panel.
- Block-LMS optimization (block size = decorrelation delay) gives
  ~5× speedup at zero quality loss.

### All-Mode Squelch (new)
- RMS + auto-tracked noise floor with hysteresis.  Replaces the
  initial WDSP-SSQL FTOV port after on-air testing showed the
  zero-crossing detector mis-classified stable harmonics.
- Per-condition hang time bridges natural speech pauses without
  closing the gate mid-syllable.
- Floor frozen during gate-open so long transmissions don't drag
  the threshold up.
- SQ button on the DSP+Audio panel; threshold slider + activity
  dot appear when enabled.

## Weather Alerts (new)

Three toolbar indicators between the ADC RMS readout and clocks:
- ⚡ Lightning — closest strike distance + bearing (yellow > 25 mi,
  orange < 25 mi, red < 10 mi)
- 💨 Wind — sustained / gust speed (yellow / orange / red tiers)
- ⚠ NWS severe weather warning (red, hidden when none active)

Each indicator hides when its tier is "none" so the toolbar stays
clean.  Desktop toasts fire on tier-crossing events with 15-minute
hysteresis.

Sources (all operator-selectable):
- **Blitzortung** — global lightning network, free, no key
- **NWS** — severe storm + wind alerts (US only)
- **Ambient Weather PWS** — wind + WH31L lightning sensor
- **Ecowitt PWS** — wind + WH57 lightning sensor

Disclaimer-gated — operator must acknowledge that alerts are
informational only before enabling.  Settings → Weather (last tab).

## UX

- **Operator/Station group** in Radio settings (callsign + grid
  square + manual lat/lon).  Migrates the older TCI-only callsign
  field on first run.
- **Two-column layouts** for the Noise and Weather settings tabs
  (mirroring what Visuals already did).  Cuts vertical scrolling
  roughly in half.
- **NR2 strength slider** range expanded from 0–150 to 0–200 — the
  WDSP-port machinery (Martin + SPP + AEPF) makes the higher
  range listenable without speech distortion.

## Attribution / License

This is the first release where Lyra incorporates substantial
WDSP-derived code.  The relicense to GPL v3+ at v0.0.6 was made
specifically to enable this — see `NOTICE.md`.

Modules with WDSP-derived algorithm content:
- `lyra/dsp/lms.py` — port of `anr.c` (Pratt 2012, 2013)
- `lyra/dsp/nr2.py` — Martin minimum-statistics + AEPF + SPP + Wiener
  gain LUT, all derived from `emnr.c` (Pratt 2015, 2025)

Modules ported from sister projects (also Lyra-author):
- `lyra/wx/sources/blitzortung.py` — from SDRLogger+
- `lyra/wx/sources/nws.py` — from SDRLogger+
- `lyra/wx/sources/ambient.py` — from SDRLogger+ + WX-Dashboard
- `lyra/wx/sources/ecowitt.py` — from SDRLogger+

The captured-noise-profile workflow remains a Lyra original.

## Known issues

- Weather Alerts: API credentials are stored unencrypted in
  QSettings (Windows registry).  Will move to OS-keyring in a
  future release.
- LMS line enhancer is most effective on steady-tone signals (CW);
  on SSB voice the effect is subtle.

## Coming next

- Neural noise reduction (RNNoise / DeepFilterNet) — WDSP's `rnnr.c`
  port plus a DeepFilterNet runtime option.  Slot already exists in
  the NR backend picker.
- More on-air listening data to drive the next tuning pass on
  squelch + NR2.
