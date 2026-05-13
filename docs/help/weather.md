# Weather Alerts

Lyra watches the operator's local conditions in the background
and surfaces threat information in the toolbar — so you find out
about a nearby lightning strike or a wind gust before you find out
about it from the antenna falling over.

The feature is **opt-in** — disabled by default until you configure
at least one data source.

## What it watches

- **Lightning** — strikes within an operator-configured radius via
  Blitzortung (community lightning network, no account needed).
- **High wind** — sustained wind speed + gusts via NWS METAR
  reports for your nearest station, or via your personal weather
  station (Ambient WS-2000 or Ecowitt) if you have one.
- **Severe storm warnings** — NWS official watches / warnings for
  your latitude / longitude.

## Toolbar indicator

A small group of icons sits in the toolbar header, near the clocks:

| Icon | Meaning |
|---|---|
| ⚡ (gray) | Lightning monitoring active, no strikes within range |
| ⚡ (yellow) | Strikes detected within outer ring (advisory) |
| ⚡ (orange) | Strikes within mid ring (warning) |
| ⚡ (red) | Strikes within close ring (severe — disconnect antenna territory) |
| 💨 (gray / yellow / orange / red) | Wind tier — calm / breezy / windy / severe |
| ⚠ (red) | Active NWS warning for your location |

Click the indicator to open Settings → Weather Alerts where the
thresholds + data-source credentials live.

## Desktop toasts

When a tier crosses upward (e.g., lightning goes from gray → orange),
Lyra fires a Windows toast notification.  Tier-crossing has a
**15-minute hysteresis per condition** so you don't get spammed
during an active storm — once a toast fires for "close lightning,"
the next close-lightning toast is suppressed for 15 minutes.

Toasts are independent of the toolbar indicator — you can disable
toasts and keep the icons, or vice versa, in Settings → Weather
Alerts → Notifications.

## Data sources

Configure one or more in Settings → Weather Alerts.  Each source
has its own enable toggle and credentials.

### Blitzortung (lightning)

- No account or API key required.  Community-funded network.
- Operator sets: home location (lat/lon), three concentric range
  rings in km.
- Polling: every 60 seconds by default.

### NWS (storms + wind)

- US-only (continental + Alaska + Hawaii + territories).
- No account required — uses the public NWS API.
- Operator sets: home location (lat/lon).  Lyra finds the nearest
  reporting station automatically.
- Polling: every 15 minutes for METAR, every 5 minutes for active
  warnings.

### Ambient Weather (PWS, optional)

- Requires an Ambient Weather account + API key + App key.
- Pulls live data from your own WS-2000 / WS-2902 / WS-5000.
- More accurate for your specific QTH than NWS METAR (which is
  usually a nearby airport, not your roof).

### Ecowitt (PWS, optional)

- Requires an Ecowitt account + Application key + API key + MAC
  address of your gateway.
- Same idea as Ambient — pulls from your own hardware.

## Disclaimer

The weather alerts feature is **informational, not life-safety**.
Lightning detection has inherent latency (Blitzortung's network
takes 30–60 seconds to localize a strike).  NWS warnings can lag
the actual weather by 5–15 minutes.  Don't rely on Lyra as your
only severe-weather alert — keep a NOAA Weather Radio, a phone
with emergency alerts enabled, and your own eyes on the sky.

Disconnect the antenna when you can hear thunder, regardless of
what the icons say.

73 and stay grounded.
