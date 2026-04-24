# Third-party notices

HL2SDR is released under the MIT License (see `LICENSE`). It depends on
and/or was designed with reference to the following third-party
projects. Their licenses apply to their respective components; MIT
terms cover only the original HL2SDR source in this repository.

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

HL2SDR implements the openHPSDR **Protocol 1** (USB/Ethernet) as
documented by the open HPSDR project. Protocol specifications are
open; no third-party code is included.

- HL2 hardware and gateware: Steve Haynal, KF7O —
  <https://github.com/softerhardware/Hermes-Lite2>
- Protocol reference: <https://openhpsdr.org/>

## Design references (NOT derivative works)

The following projects informed the HL2SDR user-interface design and
DSP chain architecture. **No code from these projects is included
in HL2SDR.** They are cited for design inspiration and protocol
cross-reference only. If you find any passage of HL2SDR code that
appears derived from one of these projects, please file an issue so
it can be investigated and cleaned up.

- **Thetis** (openHPSDR PC client) — GPL-3.0. Referenced for: AGC
  profile names and timing constants; filter-preset bandwidths;
  EQ / NB / NR user-interface labeling; USB-BCD cable integration
  idiom; TCI protocol examples. No Thetis source has been copied
  into HL2SDR.
- **ExpertSDR3** — closed-source commercial software from Expert
  Electronics. Referenced for: RX audio chain layout from the
  published v3 user manual (pages 70–95); panadapter visual design;
  meter face / dial concepts.
- **SDRLogger+** (by the same author as HL2SDR, separate MIT project)
  — Referenced for: TCI spot mode-filter CSV idiom; DX-cluster
  integration patterns. Both projects are the same author's work and
  share MIT licensing.

## Standards, specs, and public data referenced

- Yaesu-standard 4-bit BCD amplifier-band codes (industry standard,
  no copyright)
- N2ADR filter-board OC-output mapping (open hardware reference)
- FCC amateur radio band allocations (US government public data)
- ITU Region 1/2/3 allocations (public data)

---

Last updated: 2026-04-23
