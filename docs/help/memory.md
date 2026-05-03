# Memory presets (GEN buttons + Mem button)

*(Tip: click [this link](panel:bands) to flash the BANDS panel —
the **GEN1 / GEN2 / GEN3** and **Mem** buttons are on the right
side.)*

Lyra has two flavors of saved frequency:

- **GEN1 / GEN2 / GEN3** — three quick-access slots, always visible
  on the BANDS panel.  Use these for the three frequencies you tune
  to most often.
- **Mem** — a dropdown of up to **20 named memory presets**.  Use
  these for everything else — net frequencies, contest watering
  holes, beacon frequencies, "the SW broadcaster I check after
  dinner."

Both flavors save four things together: **frequency**, **mode**,
**filter**, and an optional **name**.

---

## GEN1 / GEN2 / GEN3

### Recall

Click **GEN1** (or 2, or 3).  Lyra jumps to the saved frequency,
mode, and filter for that slot.  If the slot is empty, the button
is greyed out.

### Save (right-click)

Right-click **GEN1**.  A confirm dialog asks whether you want to
overwrite the current preset with your current state:

> Save current frequency, mode, and filter to GEN1?
> 7.125.000 USB 2.4 kHz

Click **Save**.  The slot now holds your current radio state.
Right-clicking again later overwrites — there's no undo, so name
your favorite frequencies in the **Mem** bank if you want them
permanent.

### Defaults

Out of the box:

- **GEN1** = 7.255.000 LSB 2.4 kHz (40m SSB)
- **GEN2** = 14.230.000 USB 2.4 kHz (20m SSTV calling)
- **GEN3** = 28.400.000 USB 2.4 kHz (10m SSB)

You're meant to change these to match your own habits.

---

## Mem button (20-slot named bank)

### Recall

Click **Mem**.  A dropdown opens with all your saved presets,
showing name, frequency, mode, and filter.  Click an entry — Lyra
tunes there.

### Save (the "+" entry at the top)

Open the dropdown and click **+ Save current as new memory…**.  A
dialog asks for a name (e.g. "WSPR 14.0956", "OMISS Net 7.185").
The current frequency / mode / filter is saved with that name.

You can save up to 20 entries.  When the bank is full, the **+**
entry is hidden and the dialog explains how to make room
(use **Manage presets…**).

### Manage presets…

Last entry in the dropdown.  Opens **Settings → Bands → Memory**,
where you can:

- **Rename** an entry — double-click the name column.
- **Delete** an entry — select a row and press **Delete**.
- **Move** an entry up/down to reorder the dropdown.
- **Import / Export CSV** — share your presets with another
  Lyra install, or back them up.  CSV format:
  `name,frequency_hz,mode,filter_hz` — one entry per line.
- **Reset to defaults** — drops all 20 entries and reseeds with
  Lyra's starter set (a handful of common nets and propagation
  beacons).

The Memory tab shows up to date in real time — saving a memory
from the Mem dropdown adds it here without needing to reload.

---

## CSV format (for backup / sharing)

Exported file is plain UTF-8 with a header row:

```
name,frequency_hz,mode,filter_hz
"OMISS Net 40m",7185000,LSB,2400
"WSPR 20m",14095600,USB,200
"30m beacon",10144000,USB,500
```

Import skips malformed rows and reports any errors at the bottom of
the dialog.  The 20-entry cap is enforced — extras in a 30-line
CSV are dropped, and you'll see a notice.

---

## Persistence

GEN1/2/3 + Mem entries live in QSettings under
`HKEY_CURRENT_USER\Software\N8SDR\Lyra\Memory\`.  They survive
upgrades, but if you reinstall Windows or reset Lyra's settings,
export your CSV first.

---

## Related topics

- [Time Stations (TIME button)](time_stations.md) — the TIME
  button sits between GEN3 and Mem on the BANDS panel.
- [Tuning](tuning.md) — frequency display, mouse wheel, Step
  combo, click-to-tune.
