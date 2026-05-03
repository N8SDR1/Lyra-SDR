# License — GPL v3 or later

Lyra (v0.0.6 and onward) is released under the **GNU General
Public License, version 3 or any later version**.

Lyra v0.0.5 and earlier were released under the MIT License. Past
MIT releases stay MIT; the GPL relicense applies forward only.

## What it means in plain English

- **You can use Lyra** for anything — contesting, DXing, listening,
  experimenting, even commercial purposes. There's no fee, no
  activation, no license server.
- **You can modify it.** Patch, hack, customize, fork — all fine.
- **You can redistribute it** with your modifications. **But:** the
  redistributed version must also be GPL v3 (or later), and you
  must make the source code available to anyone who gets the
  binary. This is the "share-alike" / "copyleft" mechanism.
- **No warranty.** If Lyra accidentally turns your radio into a
  brick (extremely unlikely — Lyra is host software that talks to
  the HL2 over Ethernet), the author isn't legally responsible.
- **No license server, no activation, no phone-home.** Lyra runs
  entirely offline. The optional auto-update check on startup is
  the only outbound network call, and it's a single GET to GitHub's
  public releases API.

## Why GPL?

Lyra was originally MIT — the most permissive of the popular
open-source licenses. Starting v0.0.6, Lyra is GPL v3 to align with
the wider openHPSDR / WDSP ecosystem:

- **WDSP** (Warren Pratt's DSP library) is GPL v2 or later
- **Thetis** (the major openHPSDR PC client) is GPL v2 or later
- **PowerSDR** and most other openHPSDR-derived clients are GPL

Joining the GPL family means future Lyra releases can directly
incorporate WDSP for advanced features (PureSignal, CESSB, mature
TX-side DSP) without licensing complications. It also means
improvements anyone makes to Lyra come back to the ham community
rather than getting absorbed into closed-source commercial
products.

In ham radio open source, GPL is the norm. Lyra now follows that
norm.

## Donations + commercial use

GPL doesn't restrict money. Specifically:

- ✅ Donating to support Lyra's development (via PayPal etc.) is
  fine
- ✅ Selling support, services, or custom features is fine
- ✅ Using Lyra in a commercial setting (Lyra-running base station,
  contest setup, training facility) is fine
- ✅ Bundling Lyra in a "complete shack package" you sell is fine,
  as long as you provide source to the buyer

GPL just says: if you give someone the binary, they have a right
to the source code. That's the whole bargain.

## Full license text

The complete GPL v3 text lives in the `LICENSE` file at the root of
the Lyra repository. It's also available canonically from the FSF
at <https://www.gnu.org/licenses/gpl-3.0.html>.

The short legal summary that goes on top of that text:

```
Lyra-SDR — Qt6 SDR transceiver for Hermes Lite 2 / 2+

Copyright (C) 2026 Rick Langford (N8SDR), Brent Crier (N9BC),
                   and Lyra-SDR contributors (see CONTRIBUTORS.md)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

## Third-party components

Lyra builds on these open-source libraries (each with its own
license; all are GPL-compatible):

| Library | Used for | License |
|---|---|---|
| **PySide6** (Qt6 bindings) | UI framework | LGPL v3 |
| **NumPy** | IQ math, FFT, AGC | BSD-3-Clause |
| **SciPy** | Filter design | BSD-3-Clause |
| **sounddevice** | PortAudio output | MIT |
| **websockets** | TCI server | BSD-3-Clause |
| **psutil** *(optional)* | CPU% indicator | BSD-3-Clause |
| **nvidia-ml-py** *(optional)* | NVIDIA GPU% | BSD-3-Clause |
| **pywin32** *(optional, Windows)* | non-NVIDIA GPU% via PDH | PSF-2.0 |
| **ftd2xx** *(optional)* | USB-BCD external linear amp control | BSD-style |

LGPL, BSD, MIT, and PSF licenses are all compatible with GPL v3.
See `NOTICE.md` in the repository root for the full third-party
attribution table.

## Protocol attributions

- **HPSDR Protocol 1** — open community spec from the OpenHPSDR
  project. Lyra implements the host side from the public spec; the
  HL2 firmware itself is a separate project by Steve Haynal.
- **TCI v1.9 / v2.0** — open spec maintained by **EESDR Expert
  Electronics**. Lyra implements the server side from the public
  protocol documentation. (Lyra is not affiliated with or derived
  from any EESDR product.)

## Want to support Lyra?

See the next help topic — [Support Lyra](support.md) — for ways to
chip in if Lyra has been useful for your station.
