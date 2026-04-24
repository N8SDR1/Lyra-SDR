"""HL2 Open-Collector (OC) output patterns for external filter boards.

The HL2 can drive 7 OC outputs on its I/O connector (J16 pins 1-7).
These outputs are commonly wired to external band-pass filter boards.
The N2ADR HL2 filter board (https://james.ahlstrom.name/hl2filter/) is
the canonical one — a 7-relay I²C-controlled filter board where the
HL2 relays the OC bits to the board via I²C.

J16 pin → N2ADR relay mapping:
    Pin 1 (bit 0) — 160 m LPF
    Pin 2 (bit 1) — 80 m LPF
    Pin 3 (bit 2) — 60 / 40 m LPF
    Pin 4 (bit 3) — 30 / 20 m LPF
    Pin 5 (bit 4) — 17 / 15 m LPF
    Pin 6 (bit 5) — 12 / 10 m LPF
    Pin 7 (bit 6) — 3 MHz HPF (RX only, on bands above 80 m)

Patterns below mirror the reference client's "N2ADR Filter / Hercules Amp" preset
for HERMESLITE (setup.cs `chkHERCULES_CheckedChanged`). RX patterns
include the HPF bit for bands above 160 m; TX patterns do not.
"""
from __future__ import annotations


def _bits(*pins: int) -> int:
    """Build a bit-mask from 1-based J16 pin numbers."""
    out = 0
    for pin in pins:
        out |= 1 << (pin - 1)
    return out


# Per-band (rx_pattern, tx_pattern) tuples of J16 pin bits.
# Band names match lyra/bands.py (`Band.name`).
N2ADR_PRESET: dict[str, tuple[int, int]] = {
    "160m": (_bits(1),      _bits(1)),
    "80m":  (_bits(2, 7),   _bits(2)),
    "60m":  (_bits(3, 7),   _bits(3)),
    "40m":  (_bits(3, 7),   _bits(3)),
    "30m":  (_bits(4, 7),   _bits(4)),
    "20m":  (_bits(4, 7),   _bits(4)),
    "17m":  (_bits(5, 7),   _bits(5)),
    "15m":  (_bits(5, 7),   _bits(5)),
    "12m":  (_bits(6, 7),   _bits(6)),
    "10m":  (_bits(6, 7),   _bits(6)),
    "6m":   (0,             0),     # N2ADR doesn't cover 6 m; pass-through
}


def n2adr_pattern_for_band(band_name: str, transmitting: bool = False) -> int:
    """Return the 7-bit OC pattern for the given band name."""
    if band_name not in N2ADR_PRESET:
        return 0
    rx, tx = N2ADR_PRESET[band_name]
    return tx if transmitting else rx


def format_bits(pattern: int) -> str:
    """Human-readable '1.2.7' style list of lit pins, or '(none)'."""
    pins = [str(i + 1) for i in range(7) if pattern & (1 << i)]
    return ".".join(pins) if pins else "(none)"
