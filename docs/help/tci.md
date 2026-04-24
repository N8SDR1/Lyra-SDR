# TCI Server

TCI (**T**ransceiver **C**ontrol **I**nterface) is ExpertSDR3's
WebSocket-based rig-control protocol. Lyra implements a TCI v1.9
subset — enough for logging software and DX-cluster markers.

## Enabling

Settings → **Network / TCI** → "TCI Server Running" checkbox, port
40001 default.

Fastest way in: **File → Network / TCI…** from the main menubar, or
click the **TCI indicator** on the toolbar (the colored dot labeled
"TCI off" / "TCI ready" / "TCI N clients").

Once enabled, client apps connect to `ws://localhost:40001`.

## Toolbar TCI indicator

The colored status pill next to the Start/Stop button shows TCI
health at a glance:

| Indicator           | Meaning                                           |
|---------------------|---------------------------------------------------|
| **`◌ TCI off`** (gray) | Server stopped                                 |
| **`● TCI ready`** (soft green) | Running, no clients connected          |
| **`● TCI N clients`** (bright green) | Running with N active clients    |

Click anywhere on the indicator to jump to the Network/TCI settings.

Verified clients (at least partial support):

- **[SDRLogger+](https://github.com/N8SDR1/SDRLoggerPlus/releases)**
  — companion logging / DX-cluster app by the same author as Lyra.
  Pushes spots into the panadapter (with per-mode filtering), forwards
  rig state, integrates rotator + POTA / SOTA / satellite tracking.
  The two apps are designed to work together out of the box.
- **log4OM** — CAT, TX/RX status, frequency sync
- **N1MM+** — contesting
- **JS8Call** — weak-signal digital
- **MixW** — digimode apps
- **DX-cluster spotters** — display spots as markers on the
  panadapter

## Spots on the panadapter

When a TCI client pushes a spot (`spot` command), it appears as a
labeled box on the spectrum: callsign, mode, color. **Click a spot**
to tune the radio to it (frequency + mode both applied).

### Anti-clutter rendering

Dense bands (especially FT8/FT4) can push dozens of spots into the
same few kHz. Lyra draws spots with two anti-clutter tricks:

- **Collision-aware row stacking (4 rows max)** — spots are packed
  into the lowest non-overlapping row (up to 4 rows). Newest spots
  get the top row; older ones cascade down. If a spot can't fit in
  any of the 4 rows this frame, it's held in memory (still clickable
  at its exact frequency) but not drawn, so the panadapter stays
  readable.
- **Age-fade** — spots fade linearly from 100 % alpha (just received)
  to 30 % alpha (just before expiry). The freshest spots pop; the
  about-to-expire ones recede into the background.

### Spot limits — Settings → Network / TCI → Spots

- **Max spots** (0 – 100, default 30) — memory cap. Oldest evicted
  (LRU) when exceeded. Keep this low on dense digital bands.
- **Lifetime** — seconds before a spot is considered stale. Preset
  buttons: **5 / 10 / 15 / 30 min**. Or type any value up to 24 h.
  `0` = never expire. Drives both the age-fade curve and the
  expiry-removal timing.
- **Mode filter** — comma-separated list of modes to render on the
  panadapter. Empty = show all. Case-insensitive. Matches the
  SDRLogger+ idiom exactly — if you already have a mode list there,
  it ports over verbatim.
  - Examples: `FT8` — only FT8 spots shown; `FT8,FT4,RTTY` — all
    three digimodes; `CW,SSB` — phone + CW only.
  - `SSB` auto-expands to match `SSB` + `USB` + `LSB` since cluster
    spots are almost always tagged `USB`/`LSB`, not the generic
    `SSB`. You'll get all three by typing just `SSB`.
  - Filter is render-side only — filtered spots stay in memory, so
    emptying the filter brings them back instantly without waiting
    for a re-push.
- **Clear All Spots** — nuke the spot list and let the next client
  push rebuild it from scratch.

A new spot for the same callsign refreshes its timestamp (resets
fade + expiry).

## Outbound commands

Lyra sends state changes (freq, mode, PTT state, etc.) to all
connected clients automatically — this is how log4OM and N1MM+ stay
synced with the radio.

## Inbound commands (currently supported)

- `vfo` — set frequency
- `modulation` — set mode
- `rx_enable` / `rx_frequency` — RX control
- `spot` / `spot_delete` / `spot_clear` — DX spots
- `trx` — PTT (placeholder until TX path ships)

Full command list will be expanded as the TCI v2.0 spec becomes more
widely deployed.

## Troubleshooting

- **Client can't connect** — Windows Firewall may be blocking
  localhost WebSocket. Allow `python.exe` for inbound connections.
- **Spots not appearing** — verify the client is actually sending
  them. Some loggers only push spots when a DX cluster connection is
  active inside the logger itself.
