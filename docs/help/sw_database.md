# Shortwave broadcaster overlay (EiBi)

Lyra can show shortwave broadcaster station IDs as labels on the
panadapter — name, language, and target region — pulled from the
EiBi (Eike Bierwirth) seasonal CSV.  This is the same database used
by most premium SDR clients.

## What you'll see

When EiBi data is loaded and you tune to a shortwave broadcast
band (49m, 41m, 31m, 25m, 22m, 19m, 16m, 13m, 11m), Lyra paints a
small label above the panadapter spectrum at each broadcaster's
center frequency.  The label shows:

> **VOA · E · Eu**

(station "Voice of America", language English, target region
Europe).  Only stations currently on the air at the moment you're
tuned are shown — Lyra checks each broadcaster's schedule and
day-of-week mask before drawing.

When the panadapter gets crowded, labels stack on up to four rows
to keep them readable.

## When the overlay is suppressed

Lyra **auto-detects** when you're inside an amateur band for your
region (Settings → Operator → Region) and hides EiBi labels there.
The 41m broadcast band (7.200-7.450 MHz) overlaps the US 40m
amateur allocation (7.000-7.300 MHz), so on a US license the
overlay disappears between 7.000 and 7.300 — your ham band, your
spots.  Above 7.300 MHz the broadcasters return.

You can override this if you want labels everywhere — Settings →
Bands → SW Database → "Show EiBi labels in ham bands too."  Operators
outside the regulated regions (region = NONE) always see all labels.

## Loading the EiBi database

EiBi publishes a fresh seasonal CSV twice a year (March/October
transitions).  The file naming is:

- `sked-a26.csv` — A season (April-October 2026, summer DST)
- `sked-b26.csv` — B season (November 2026 - March 2027, winter)

### Try the auto-update first

Settings → Bands → SW Database → **Update database now**.  Lyra
downloads the current season's CSV in the background and reloads
the overlay.  No restart required.

### Manual install (if auto-update fails)

EiBi's server has had on-and-off TLS cert issues; if "Update
database now" fails, do this:

1. Click **Open EiBi website** in the SW Database tab.  Your
   browser opens `http://www.eibispace.de/dx/`.  You may see a
   "Not secure" warning — that's expected; the EiBi server doesn't
   serve over HTTPS.  Click through.
2. Right-click the link to the current season file (e.g.
   `sked-a26.csv`) and choose **Save link as…**
3. Save it to the directory shown in the SW Database tab — usually
   `C:\Users\<you>\AppData\Roaming\N8SDR\Lyra\eibi\`.  Lyra reloads
   automatically when the file appears.

The **Copy URL** button copies the direct CSV URL to your
clipboard if you prefer to paste it into your browser bar.

## Filters (Settings → Bands → SW Database)

| Filter | Effect |
|---|---|
| **Master enable** | Turn the entire overlay on/off |
| **Min power class** | Hide weak / local broadcasters (P=0) and show only regional / international (P≥1, 2, 3) |
| **Show in ham bands** | Override auto-suppression — show labels even inside your amateur allocation |
| **Show off-air stations** | Show labels for stations whose schedule is not currently active (greyed out) |

## Attribution

EiBi data is © Eike Bierwirth, free for non-commercial use, attribution
required.  Lyra surfaces the attribution string in Settings → Bands
→ SW Database.  We do **not** bundle the CSV in the Lyra installer
— you download it once on first use.

Project page: **<https://eibispace.de/>**

## Troubleshooting

**Labels don't appear even though file is loaded.**  Check the
status line at the bottom of the SW Database tab — it should read
`Loaded N entries from sked-a26.csv`.  If N is zero, the file may
be incomplete; re-download.

**"Update database now" fails on every URL.**  EiBi's HTTPS cert
sometimes rejects modern Python's TLS validation.  Use the manual
install path above.

**Labels look stale (off-air stations showing as on-air).**  EiBi
schedules update twice a year.  If you're crossing a season
boundary (late March / late October), the previous season's file
still works for ~a week of overlap, then click **Update database
now** to pull the new season.

**Wrong country code in label.**  EiBi data uses ITU country codes
(USA, GBR, CHN…).  Lyra renders them verbatim — these are the
authoritative codes for international broadcast schedules and may
differ from your DXCC habit.

## Related topics

- [Spectrum & Waterfall](spectrum.md) — display options, palettes,
  click-to-tune.
- [Tuning](tuning.md) — frequency display + Step combo + mouse wheel.
