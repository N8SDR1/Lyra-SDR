# Introduction — Why "Lyra"?

![Lyra SDR](assets/logo/lyra-icon-128.png)

Lyra is a Qt-based SDR transceiver application for Steve Haynal's
**Hermes Lite 2** and **Hermes Lite 2+**. It's a Windows-first
(eventually cross-platform) PC client that talks HPSDR Protocol 1
directly to the radio, decodes the IQ stream in real time, and
provides a modern panadapter / control surface in the spirit of
Thetis and ExpertSDR3 — but built from scratch, licensed MIT,
tailored specifically to the HL2 family.

## The name

The openHPSDR naming tradition leans Greek. The hardware itself is
**Hermes** (the messenger god). Thetis — the venerable reference PC
client — is named after the sea-nymph mother of Achilles. New
projects in this ecosystem have historically reached into the same
mythology for names that share the family.

**Lyra** continues that tradition:

- **Apollo's lyre** was the instrument that turned invisible
  vibrations in the air into music — which is essentially what an
  SDR does: it takes vibrations in the electromagnetic field,
  digitizes them, and turns them back into something you can hear.
- The **constellation Lyra** contains **Vega** — one of the first
  stars whose radio emissions were detected with amateur-grade HF
  equipment, and a star with a long history of amateur-radio
  significance (Vega is the zero point of the astronomical
  magnitude scale and was the first star ever photographed).
- It's short, pronounceable in every language the HPSDR community
  uses, and stays well clear of any existing SDR software or
  hardware product name.

## Project philosophy

Lyra aims to be:

- **Focused on the HL2 family.** Not a universal "works with
  anything" SDR. By targeting one hardware family, the UI can
  expose exactly the right controls (OC outputs, USB-BCD for the
  amp, AK4951 audio routing) without ever becoming a lowest-common-
  denominator panel.
- **Modern to look at.** Glassy / mirror-surface panels, analog
  meter with lit-amber arc on black, 7-segment amber frequency
  readout, dockable everything. Inspired by ExpertSDR3's look-and-
  feel but an independent visual design.
- **Scripting-friendly.** TCI v1.9 server built in from day one so
  log4OM, N1MM+, JS8Call, SDRLogger+, and anything else speaking
  TCI just works.
- **Safe by default.** USB-BCD cable for linear-amp band switching
  stays disabled unless an FTDI device is actually detected — no
  way to key into the wrong filter. Gateware quirks (duplex bit,
  EP2 keepalive, spectrum mirroring) are handled transparently.
- **Small and reviewable.** MIT licensed, small enough that a
  single operator can read and understand the whole pipeline,
  from the first UDP packet off the wire to the last sample
  leaving sounddevice.

## What Lyra is **not**

- Not derived from Thetis (GPL-3.0) or ExpertSDR3 (closed-source).
  Those are cited as design references in `NOTICE.md`; none of
  their code is in Lyra. If you ever spot something that looks
  otherwise, file an issue so it can be investigated.
- Not (yet) a universal panadapter. Other SDR hardware will come
  once the HL2 path is solid.
- Not trying to outdo Thetis feature-for-feature on day one.
  Parity is a long road; the goal is a clean, modern, maintainable
  codebase that grows at a sustainable pace.

## Getting started

See the **Getting Started** topic in this guide (or press **F1**
and pick it from the topic tree). If you want the big picture of
what's implemented, what's in progress, and what's on the
long-term list, the `docs/backlog.md` file in the project folder
is kept current.

## Who's behind Lyra

Lyra is written by **Rick Langford (N8SDR)** and released under the
MIT License. See `LICENSE` for terms and `NOTICE.md` for the full
list of dependencies and design references.

73 and good DX.
