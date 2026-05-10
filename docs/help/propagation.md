# Propagation

A slim status panel that pulls live solar data and shows you, at a
glance, **what bands are open right now** — plus an auto-follow
control for the NCDXF International Beacon Project.

The panel is operator-toggleable like every other Lyra dock.  Show
it via View menu / toolbar; drag it wherever in the layout fits
your style; hide it when you don't care.

## What's on the panel

```
┌─ PROP ─────────────────────────────────────────────────────────┐
│  SFI 130   A 5   K 2  │  160 80 40 30 20 17 15 12 10 6  │  ▾ Follow │
└─────────────────────────────────────────────────────────────────┘
```

Three glance-readable groups, separated by thin dividers.

### Solar numbers

| Number | What it is | Why you care |
|--------|-----------|--------------|
| **SFI** | Solar Flux Index (10.7 cm radio noise from the Sun) | Higher = better HF propagation; ≥100 is good for 10/12/15m, ≥150 for full HF, <80 means quiet bands |
| **A** | A-index (24-hour geomagnetic activity) | Lower = quieter geomag = cleaner propagation; ≥20 means heavy storms, signals are flaky |
| **K** | K-index (3-hour rolling geomagnetic) | Same direction as A but more responsive; K ≤ 2 is great, K ≥ 4 is rough |

Color coding (green / yellow / red) uses operator-validated thresholds.
Hover the panel for SSN / X-Ray / solar wind / last-update timestamp.

### Band heatmap

Ten band labels (160 / 80 / 40 / 30 / 20 / 17 / 15 / 12 / 10 / 6),
each colored Good / Fair / Poor based on HamQSL's prediction for
**right now** (Day vs Night picked automatically based on your QTH).

- **Green** — band is open / signals propagate well
- **Yellow** — marginal / edge of the propagation window
- **Red** — band is poor / not expected to be useful
- **Gray** — HamQSL doesn't predict this band (160m and 6m don't
  appear in the public solar feed; check by ear)

The colors update every minute as the sun crosses the horizon at
your location.  Set your grid square in **Settings → Radio** for
Day/Night to flip at the right time; without a grid, the panel
defaults to Day-rating.

### Follow dropdown

The NCDXF International Beacon Project is a network of 18 worldwide
stations, each transmitting on a fixed schedule across five bands
(20m / 17m / 15m / 12m / 10m).  Each station's slot lasts 10 seconds;
the cycle repeats every 3 minutes.

> An SDR-only superpower: **Lyra can auto-follow one station around
> the rotation.**  Pick a station from the Follow dropdown and Lyra
> auto-tunes 14.100 → 18.110 → 21.150 → 24.930 → 28.200 MHz every
> 10 seconds, so you can hear that one station's signal across all
> five bands without touching anything.  A regular knob radio
> operator would have to mash band-change every 10 seconds to do
> the same thing.

Pick "Off" to disable auto-follow.

The Follow setting persists across Lyra restarts.

## NCDXF spectrum markers

Lyra also paints a "NCDXF" landmark on the spectrum at each of the
five fixed beacon frequencies (14.100 / 18.110 / 21.150 / 24.930 /
28.200 MHz).  Hover the marker and a tooltip shows you the **current
station callsign** for that band — updates every 10 seconds as the
rotation cycles.

This is where you go when you want to know "I'm hearing a CW signal
on 14.100 — who is it?"  Hover the marker, see the callsign, instantly
know which DXCC entity you're hearing and how strong the propagation
is to that station's QTH.

Click the marker to QSY to that NCDXF frequency (mode auto-switches
to CWU).  The VFO LED reads the beacon's actual carrier (e.g.
14.100.000), and Lyra handles the CW pitch offset on the receive
side automatically — you hear the beacon's keyed callsign as a
clean CW tone at whatever pitch you've set in Settings → DSP
(default 650 Hz).  Auto-follow does the same on every band switch.

## How the data is gathered

| Data | Source | Cadence |
|------|--------|---------|
| Solar numbers + band conditions | hamqsl.com/solarxml.php (public feed) | Cached 15 min; panel re-checks every 60 sec |
| NCDXF station schedule | Pure math (NTP-synced via your system clock) | Computed on every panel refresh + every 10 sec for follow |
| Sunrise/sunset for Day/Night | NOAA-style algorithm using your QTH lat/lon | Recomputed every 60 sec |

No background threads.  No hidden network calls.  When the HamQSL
feed is unreachable (rare), the panel keeps showing the last good
data — operator sees stale rather than blank.

## Tips

- **Set your grid square** in Settings → Radio.  Day/Night auto-pick
  needs your latitude/longitude to know when sunrise/sunset is at
  your QTH.
- **Check the band heatmap before tuning** — if 17m shows red and
  20m shows green, save yourself a band switch and start on 20.
- **Use auto-follow for antenna testing** — pick a known-distant
  station (W6WX from the East Coast, for instance) and watch which
  band your antenna actually hears it on as the sun moves.
- **Hover the panel** for an extended tooltip with SSN / X-Ray /
  solar wind / last-update — those numbers are useful for diagnosing
  geomagnetic storms but don't need permanent screen real estate.

## Solar value ranges (for reference)

| Range | SFI | A-index | K-index |
|-------|-----|---------|---------|
| Excellent | 200+ | 0–4 | 0–1 |
| Good | 100–200 | 5–7 | 2 |
| Fair | 80–100 | 8–19 | 3 |
| Poor | <80 | 20+ | 4+ |

These are rough guides; the actual band response also depends on
seasonal effects, time of day, and your specific antenna's
performance toward the target QTH.

## Clock accuracy (important for NCDXF Follow)

NCDXF beacon slots are 10 seconds long, and Lyra computes which
station is on which band purely from your PC clock — there's no
audio decoding involved.  If your clock drifts more than ~3 seconds
off real UTC, the spectrum-marker tooltips and Follow-mode tuning
will identify the **wrong** station.

To check / fix:

- **Right-click either toolbar clock** (local time or UTC) → pick
  **Check clock drift now…**.  Lyra queries a public NTP server
  (Cloudflare, NTP Pool, Google, Microsoft — first that answers
  wins) and reports the offset.
- If drift is significant, the UTC clock gets a ⚠ prefix as a
  glance-readable warning until you re-check.
- On Windows, the same right-click menu has **Sync time now
  (Windows w32time)** which shells out to ``w32tm /resync`` —
  works on a stock Windows install if Windows Time service is
  running.  Otherwise the dialog gives you the manual command
  sequence to run from an elevated Command Prompt.

This check is purely outgoing UDP/123 — no account, no key, no
data sent except the timestamp.  Your firewall has to allow
outbound NTP (most do by default).

## What this panel doesn't do

- **Predict tomorrow's propagation** — the data is a snapshot of
  current conditions, not a forecast.  For forecasts, voacap.com is
  the operator-trusted resource.
- **Show real-time signal strength** to each NCDXF station — that
  would require Lyra to listen for and decode each beacon's signal
  level (Faros / BeaconSee do this if you want it; Lyra can run
  alongside them).
- **Operate without internet** — the HamQSL fetch needs network
  access; the NCDXF schedule + band heatmap will fall back to "no
  data" if the feed has never succeeded.  Once cached, stale data
  serves until the feed returns.
