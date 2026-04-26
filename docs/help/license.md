# License — MIT

Lyra is released under the **MIT License**. Short, permissive,
ham-radio-friendly.

## What it means in plain English

- **You can use Lyra** for anything — contesting, DXing, listening,
  experimenting, even commercial purposes.
- **You can modify it.** Patch, hack, customize, fork — all fine.
- **You can redistribute it** with your modifications, in source or
  binary form.
- **The only requirement** when redistributing: include the original
  copyright notice + the permission notice (a single short paragraph).
- **No warranty.** If Lyra accidentally turns your radio into a
  brick, the author isn't legally responsible. (Realistically, Lyra
  reads from the HL2; it's hard to imagine a way it could damage
  hardware. But the legal disclaimer covers it anyway.)
- **No license server, no activation, no phone-home.** Lyra runs
  entirely offline.

## Full license text

```
MIT License

Copyright (c) 2026 Rick Langford (N8SDR)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

## Third-party components

Lyra builds on these open-source libraries (each with its own
license, all permissive and compatible with MIT):

| Library | Used for | License |
|---|---|---|
| **PySide6** (Qt6 bindings) | UI framework | LGPL v3 / Commercial |
| **NumPy** | IQ math, FFT, AGC | BSD-3-Clause |
| **SciPy** | Filter design | BSD-3-Clause |
| **sounddevice** | PortAudio output | MIT |
| **websockets** | TCI server | BSD-3-Clause |
| **psutil** *(optional)* | CPU% indicator | BSD-3-Clause |
| **nvidia-ml-py** *(optional)* | NVIDIA GPU% | BSD-3-Clause |
| **pywin32** *(optional, Windows)* | non-NVIDIA GPU% via PDH | PSF-2.0 |
| **ftd2xx** *(optional)* | USB-BCD external linear amp control | BSD-style |

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
