# TCI Server

TCI (**T**ransceiver **C**ontrol **I**nterface) is ExpertSDR3's
WebSocket-based protocol for SDR control + audio + IQ streaming.
Lyra implements TCI v2.0 — text commands for rig control, **plus**
binary audio + IQ streams (new in v0.0.9.1) so digital-mode apps
can send / receive audio over the same WebSocket connection
without needing a Virtual Audio Cable.

## Why TCI

If your logger / decoder app speaks TCI, you get:

- **Rig control**: frequency, mode, filter, PTT (TX path coming in v0.2)
- **Spots on the panadapter**: DX-cluster / skimmer markers, click to tune
- **RX audio over TCI**: 48 kHz mono/stereo audio piped to your decoder
  with no soundcard plumbing
- **IQ over TCI**: full-bandwidth panorama feed for spectrum-analyzer
  apps (SDRLogger+ uses this for its panadapter view)

One WebSocket, no Virtual Audio Cable, no exclusive-mode soundcard
juggling. Recommended path over VAC for any modern TCI-aware app.

## Enabling

Settings → **Network / TCI** → "TCI Server Running" checkbox.
Default port **50001** (HPSDR-family standard).

Fastest way in: **File → Network / TCI…** from the menubar, or click
the **TCI indicator** on the toolbar (the colored dot labeled
"TCI off" / "TCI ready" / "TCI N clients").

Once enabled, client apps connect to `ws://localhost:50001`.

## Toolbar TCI indicator

The colored status pill next to the Start/Stop button shows TCI
health at a glance:

| Indicator                              | Meaning                       |
|----------------------------------------|-------------------------------|
| **`◌ TCI off`** (gray)                 | Server stopped                |
| **`● TCI ready`** (soft green)         | Running, no clients connected |
| **`● TCI N clients`** (bright green)   | Running with N active clients |

Click anywhere on the indicator to jump to the Network/TCI
settings.

## Settings panel layout

Three side-by-side columns:

### Column 1 — TCI Server

- **Bind IP:Port** — defaults to `127.0.0.1:50001` (localhost).
  Change to `0.0.0.0:50001` to allow LAN connections.  IPv4 picker
  helps you fill in a specific interface IP.
- **Rate Limit (ms)** — minimum interval between same-key broadcast
  messages.  Throttles very-fast freq-tune dials so clients don't
  flood.  Default 80 ms (~12 updates/sec) matches Thetis convention.
- **Send initial state on client connect** — broadcasts current
  freq/mode/etc. when a client first connects.  WSJT-X and log4OM
  expect this.  Default ON.
- **CWL/CWU becomes CW (outbound)** — TCI's mode enum doesn't have
  CW sideband distinction historically.  ON = collapse CWL/CWU → CW
  on outbound.  OFF = send CWU/CWL verbatim (newer clients prefer).
  Default ON.
- **CW becomes CWU above 10 MHz (inbound)** — when a client sends
  generic "CW", apply the standard ham convention: CWU above
  10 MHz, CWL below.  Default ON.
- **Emulate ExpertSDR3 protocol** — some legacy clients only
  recognize the ExpertSDR3 protocol/device strings on connect and
  refuse to talk to anything else.  Enable this if your client
  refuses Lyra's native identification.  Default OFF.
- **Log TCI traffic to console** + **Show Log...** — diagnostic
  log of every text command sent / received.  Useful when a
  client isn't behaving and you want to see what it's actually
  asking for.

### Column 2 — Audio + IQ Streaming

- **Allow RX audio over TCI** — master enable.  When off, TCI
  clients receive no audio even if they send `AUDIO_START`.
  Default ON.
- **Allow IQ over TCI** — master enable for the IQ stream
  (panorama / spectrum-analyzer feed).  Default ON.
- **Always stream audio** — auto-start audio streaming for new
  clients without waiting for explicit `AUDIO_START`.  Useful for
  legacy apps.  Default OFF.
- **Always stream IQ** — same for IQ.  Default OFF.
- **Swap IQ on stream** — if your IQ-consuming client expects Q,I
  instead of I,Q (rare).  Default OFF.
- **Currently streaming:** read-only display of every connected
  TCI client and what they're consuming.  Shows the client's IP +
  port + audio config (sample rate / format / channels) + IQ
  config.  Diagnostic — you can see at a glance whether your
  digital app is actually subscribed and what it asked for.

### Column 3 — TCI Spots

- **Max spots** (0 – 100) — memory cap.  Oldest evicted (LRU).
  20–30 is sensible for HF.
- **Lifetime** — seconds before a spot is considered stale.  Preset
  buttons: 5m / 10m / 15m / 30m.  `0` = never expire.
- **Mode filter** — comma-separated modes to render
  (`FT8,CW,SSB`).  Empty = show all.  `SSB` auto-includes USB+LSB.
- **Flash new spots** + color picker — visual flash animation
  when a new spot arrives.
- **Show country flags on spots** — DXCC enrichment.
- **Own callsign** + own-call color picker — when your own
  callsign is spotted by someone, render in this color.
- **CW Spot sideband** — when a client spots a "CW" mode without
  U/L, force how Lyra renders it: `Default` / `Force CWU` /
  `Force CWL`.

## Spot frequency convention

Lyra expects TCI spots to carry the **carrier frequency** of the
spotted signal — same convention used by every DX cluster, the
Reverse Beacon Network (RBN), CW Skimmer Server, and SDRLogger+.
This matches the standard convention across major HF SDR
applications: the displayed VFO frequency IS the signal's carrier,
and the radio handles the CW pitch offset on the receive side
internally so the operator hears the signal as a CW tone at their
configured pitch.

When you click a CW spot, the VFO LED jumps to the spot's listed
carrier and you hear the signal at your CW pitch tone — no
pre-math, no mental subtraction, the LED matches what's on the
air.  Non-CW spots tune to the spot freq exactly, same way.

The ``spot_activated`` signal Lyra emits back to TCI clients
carries the same carrier frequency the operator's VFO is sitting
at, so spot round-trips with SDRLogger+ (and any other listener)
preserve the cluster value.

## Audio over TCI — setup recipes

### WSJT-X (FT8 / FT4 / etc.)

1. **Lyra**: Settings → Network/TCI → ☑ TCI Server Running.
2. **WSJT-X**: File → Settings → **Radio** tab:
   - Rig: **TCI**
   - Network Server: `127.0.0.1:50001`
   - Click **Test CAT** — should turn green
3. **WSJT-X**: **Audio** tab:
   - Soundcard Input: **TCI Audio** (or "TCI Server Input")
   - Soundcard Output: same (TCI for TX too, when v0.2 TX ships)
4. Verify in **Lyra**: Settings → Network/TCI → "Currently
   streaming" should show WSJT-X's address with `audio 48k …`.
5. Click WSJT-X's **Monitor** button.  You should see FT8 decodes
   populating the message list within ~15 seconds (one FT8 cycle).

### JS8Call / FLDIGI

Similar pattern — point the rig setting at TCI / `127.0.0.1:50001`,
point the audio input at TCI.  No VAC needed.

### MSHV (multi-mode digimode app)

Settings dialog → Output Devices: **TCI Client Output**, Input
Devices: **TCI Client Input**.  Connects automatically.

### log4OM (rig control only, no audio)

Configure Lyra in log4OM as a TCI radio.  Frequency / mode sync
both ways.  No audio routing involved.

## IQ over TCI

Apps that want full-bandwidth IQ (spectrum analyzers, SDRLogger+'s
panadapter, alternative SDR clients running as receivers) connect
and send `IQ_START:0;`.  Lyra emits binary IQ frames at the current
panadapter rate (48 / 96 / 192 / 384 kHz, whatever the operator's
**Rate** combo is set to).

The IQ stream is independent of audio — a client can subscribe to
either or both.

## Spots on the panadapter

When a TCI client pushes a spot (`spot` command), it appears as a
labeled box on the spectrum: callsign, mode, color.  **Click a
spot** to tune the radio to it (frequency + mode both applied).

### Anti-clutter rendering

Dense bands (especially FT8/FT4) can push dozens of spots into the
same few kHz.  Lyra draws spots with two anti-clutter tricks:

- **Collision-aware row stacking (4 rows max)** — spots are packed
  into the lowest non-overlapping row.  Newest spots get the top
  row; older ones cascade down.  Spots that can't fit are held in
  memory (still clickable at exact frequency) but not drawn, so
  the panadapter stays readable.
- **Age-fade** — spots fade linearly from 100 % alpha (just
  received) to 30 % alpha (just before expiry).  Freshest spots
  pop; about-to-expire ones recede into the background.

A new spot for the same callsign refreshes its timestamp (resets
fade + expiry).

## Verified clients

- **[SDRLogger+](https://github.com/N8SDR1/SDRLoggerPlus/releases)** —
  companion logging / DX-cluster app by the same author as Lyra.
  Pushes spots, forwards rig state, integrates rotator + POTA /
  SOTA / satellite tracking.  Future versions will use Lyra's TCI
  IQ stream for the in-app panadapter view.
- **WSJT-X** — FT8, FT4, MSK144, JT65, JT9 (rig + audio over TCI)
- **JS8Call** — weak-signal digital (rig + audio)
- **MSHV** — multi-mode digimode (rig + audio)
- **FLDIGI** — many digital modes (rig + audio)
- **log4OM** — CAT, TX/RX status, frequency sync
- **N1MM+** — contesting (rig only)
- **DX-cluster spotters** — display spots as panadapter markers

## Outbound commands

Lyra sends state changes (freq, mode, PTT state, etc.) to all
connected clients automatically — this is how log4OM and N1MM+
stay synced with the radio.

## Inbound commands supported

| Command | Purpose |
|---|---|
| `start` / `stop` | Stream RX on/off |
| `dds` / `vfo` / `if` | Frequency control |
| `modulation` | Mode select |
| `trx` | PTT (returns false until v0.2 TX) |
| `tune` | Tune cycle |
| `rit_enable` / `xit_enable` | RIT/XIT (placeholder) |
| `spot` / `spot_delete` / `spot_clear` | DX spot push |
| `audio_start` / `audio_stop` | RX audio stream subscribe |
| `audio_samplerate` | 8 / 12 / 24 / 48 kHz (default 48) |
| `audio_stream_sample_type` | int16 / int24 / int32 / float32 |
| `audio_stream_channels` | 1 or 2 |
| `iq_start` / `iq_stop` | IQ stream subscribe |
| `iq_samplerate` | 48 / 96 / 192 / 384 kHz |
| `line_out_start` / `line_out_stop` | Aliased to RX_AUDIO |

## Troubleshooting

**Client can't connect** — Windows Firewall may be blocking
localhost WebSocket.  Allow `python.exe` for inbound connections.

**Spots not appearing** — verify the client is actually sending
them.  Some loggers only push spots when a DX-cluster connection
is active inside the logger itself.

**No audio in WSJT-X / decoder** — check Lyra's Settings →
Network/TCI → "Currently streaming" list.  If your client appears
but with the wrong sample rate / format / channels, check the
client's audio settings.  If the client doesn't appear at all,
the client probably isn't speaking TCI yet — check that it's
configured for TCI rig control AND TCI audio (some apps require
both to be enabled separately).

**Decoder shows garbled audio** — usually a sample-rate mismatch.
Most ham digital apps default to 48 kHz which is exactly what
Lyra produces; verify your client isn't requesting a different
rate.

**Audio works but spots don't** — different code paths.  Spots
are text commands; audio is binary frames.  If audio flows but
spots don't, the client is connected for audio but not pushing
DX-cluster data.  Check the client's cluster connection.
