# Lyra-SDR Contributors

Lyra-SDR is built by amateur radio operators, for amateur radio operators.
Contributors below have written code, tested releases, contributed to design
decisions, or otherwise helped move the project forward.

License: GPL v3 or later (since v0.0.6).  Each contributor retains copyright
on their own contributions and licenses them to the project under the GPL
v3+.  See `LICENSE` for full terms and `NOTICE.md` for third-party
attribution.

## Active contributors

- **Rick Langford** — N8SDR — <https://www.qrz.com/db/N8SDR>
  Project lead and original author.  All work through v0.0.9.

- **Brent Crier** — N9BC — <https://www.qrz.com/db/N9BC>
  Joined as co-contributor 2026-05-03.  Hermes Lite 2+ and ANAN G2 owner;
  brings the future-roadmap protocol-2 / multi-radio testing perspective.
  See `docs/onboarding/contributor-discussion-2026-05-03.md` for the
  ground-rules conversation we had on his arrival.

## Roadmap context

- v0.0.x — v0.0.9 ("Memory & Stations") and v0.0.9.1 (audio-architecture
  fixes) are HL2/HL2+ only, single-author work by N8SDR with N9BC joining
  during v0.0.9.1 testing.
- v0.1 — RX2 (dual receive) on HL2.  Joint work begins here.
- v0.2 — TX (SSB → CW/AM/FM, leveler).
- v0.3 — PureSignal.
- v0.4 — Multi-radio refactor + Protocol 2 + ANAN family.  N9BC's ANAN G2
  becomes the primary test bench for v0.4.

See `CLAUDE.md` §7 for the current authoritative roadmap.

## How to contribute

We work via the standard GitHub fork/PR flow with branch protection on
`main` and `feature/threaded-dsp`.  See
`docs/onboarding/contributor-discussion-2026-05-03.md` for the workflow,
commit conventions, and the WDSP/Thetis license-line rules contributors
must understand before their first PR.

Want to help?  Open an issue first to discuss; small PRs (typo fixes,
doc improvements) welcome at any time.
