# Third-party notices

Lyra-SDR (versions ≥ v0.0.6) is released under the **GNU General
Public License v3.0 or later** (see `LICENSE`). It depends on
and/or was designed with reference to the following third-party
projects. Their licenses apply to their respective components.

Lyra v0.0.5 and earlier were released under the MIT License.
Past releases retain their original license terms; the GPL
relicense applies only to v0.0.6 and later.

## Runtime dependencies (dynamically linked / imported)

| Project       | License           | Role                                                  |
|---------------|-------------------|--------------------------------------------------------|
| **Python**    | PSF License       | Interpreter                                           |
| **PySide6 / Qt 6** | LGPL-3.0 (Qt) / LGPL-3.0 (PySide6) | GUI toolkit. Used dynamically — users may substitute their own Qt. |
| **NumPy**     | BSD-3-Clause      | Numerical arrays, FFT                                 |
| **SciPy**     | BSD-3-Clause      | DSP (IIR filters, resampling, windows)                |
| **sounddevice** | MIT             | PC audio output (PortAudio wrapper)                   |
| **websockets** | BSD-3-Clause     | TCI WebSocket server                                  |
| **ftd2xx**    | BSD-style         | FTDI D2XX Python bindings — USB-BCD cable driver      |

### Platform-native components (not bundled)

- **FTDI D2XX driver** — proprietary driver from Future Technology
  Devices International Ltd. Required only if the USB-BCD cable is
  used. Must be installed separately by the user from
  <https://ftdichip.com/drivers/d2xx-drivers/>.
- **PortAudio** — MIT, bundled inside the `sounddevice` wheel on most
  platforms. No separate install needed.

## HPSDR Protocol 1

Lyra-SDR implements the openHPSDR **Protocol 1** (USB/Ethernet) as
documented by the open HPSDR project. Protocol specifications are
open; no third-party code is included.

- HL2 hardware and gateware: Steve Haynal, KF7O —
  <https://github.com/softerhardware/Hermes-Lite2>
- Protocol reference: <https://openhpsdr.org/>

## Design references and ecosystem peers

The following projects informed Lyra-SDR's user-interface design and
DSP chain architecture. As of v0.0.6 (under GPL v3 or later), Lyra
is in license-compatible territory with the openHPSDR family and
may directly incorporate or link with these projects in future
releases. For releases up through v0.0.5 (MIT), Lyra was a
clean-room implementation referencing only documentation.

- **Thetis** (openHPSDR PC client) — GPL v2 or later. Referenced
  for: AGC profile names and timing constants; filter-preset
  bandwidths; EQ / NB / NR user-interface labeling; USB-BCD cable
  integration idiom; TCI protocol examples. As of Lyra v0.0.6, code
  contributions from / shared work with Thetis are licensing-
  compatible.
- **WDSP** (Warren Pratt, NR0V — DSP library used by Thetis /
  PowerSDR) — GPL v2 or later. License is compatible with Lyra's
  GPL v3+.  As of v0.0.6, Lyra incorporates the following modules
  with WDSP-derived algorithm content (per-file attribution in each
  source file):
    - `lyra/dsp/lms.py` — Normalized LMS adaptive line enhancer
      with adaptive leakage.  Port of WDSP `anr.c`
      (Copyright (C) 2012, 2013 Warren Pratt, NR0V).
    - `lyra/dsp/nr2.py` — Martin (2001) minimum-statistics noise
      PSD, AEPF median-smoothing post-filter, speech-presence
      probability soft mask, MMSE-LSA + Wiener gain LUT.  All
      derived from WDSP `emnr.c` (Copyright (C) 2015, 2025
      Warren Pratt, NR0V).
  Future releases may add: RNNoise neural NR (`rnnr.c`), PureSignal
  predistortion, CESSB.
- **ExpertSDR3** — closed-source commercial software from Expert
  Electronics. Referenced for: RX audio chain layout from the
  published v3 user manual (pages 70–95); panadapter visual design;
  meter face / dial concepts. No code involvement.
- **SDRLogger+** (by the same author as Lyra-SDR, separate project)
  — Referenced for: TCI spot mode-filter CSV idiom; DX-cluster
  integration patterns; weather-alerts disclaimer text.  As of
  v0.0.6, Lyra also lifts the following weather-source adapters
  from SDRLogger+ (also Lyra-author, GPL):
    - Blitzortung lightning network adapter (`lyra/wx/sources/blitzortung.py`)
    - NWS active-alerts + METAR adapter (`lyra/wx/sources/nws.py`)
    - Ambient Weather PWS adapter (`lyra/wx/sources/ambient.py`)
    - Ecowitt v3 PWS adapter (`lyra/wx/sources/ecowitt.py`)
- **WX-Dashboard** (by the same author as Lyra-SDR, separate project)
  — GPL.  Original home of the modular weather-source API contracts
  Lyra now uses for its all-station weather-alerts feature.

## Standards, specs, and public data referenced

- Yaesu-standard 4-bit BCD amplifier-band codes (industry standard,
  no copyright)
- N2ADR filter-board OC-output mapping (open hardware reference)
- FCC amateur radio band allocations (US government public data)
- ITU Region 1/2/3 allocations (public data)

## License history

| Version range | License | Notes |
|---|---|---|
| v0.0.1 – v0.0.5 | MIT | Original license; clean-room implementation |
| v0.0.6 onward | GPL v3 or later | Relicensed to align with openHPSDR / WDSP ecosystem and enable future WDSP integration |

Past MIT-licensed releases remain available under their original
terms; the relicense is forward-only.

## Authors and contributors

Lyra-SDR is jointly developed by amateur radio operators.  Each
contributor retains copyright on their own contributions and
licenses them to the project under the GPL v3+ on submission.

- Rick Langford (N8SDR) — project lead, original author, all work
  through v0.0.9
- Brent Crier (N9BC) — joined 2026-05-03 during v0.0.9.1 testing
- Timmy Davis (KC8TYK) — joined during v0.1.0-pre2 tester flight

See `CONTRIBUTORS.md` at the project root for the full active
contributor roster and onboarding history.

---

Last updated: 2026-05-13 (v0.1.0-pre — RX2 release polish +
KC8TYK onboarding)
