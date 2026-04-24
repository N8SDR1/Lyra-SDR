"""Ham band definitions + per-band lookup.

HL2's standard filter set covers roughly 100 kHz to 55 MHz. Defaults
are chosen as sensible "tune here first" points: voice sub-band center
for SSB bands, popular FT8 freqs for digital-heavy bands, CW watering
holes where no voice applies.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    name: str          # "40m", "20m", etc.
    label: str         # short button label, "40", "20"
    lo_hz: int
    hi_hz: int
    default_hz: int
    default_mode: str  # "USB", "LSB", "CWU", "AM", etc.


# Amateur bands (160m through 6m — what HL2 covers natively)
AMATEUR_BANDS: list[Band] = [
    Band("160m", "160",   1800000,   2000000,   1840000, "LSB"),
    Band("80m",  "80",    3500000,   4000000,   3750000, "LSB"),
    Band("60m",  "60",    5330000,   5407000,   5368000, "USB"),
    Band("40m",  "40",    7000000,   7300000,   7200000, "LSB"),
    Band("30m",  "30",   10100000,  10150000,  10136000, "CWU"),
    Band("20m",  "20",   14000000,  14350000,  14200000, "USB"),
    Band("17m",  "17",   18068000,  18168000,  18140000, "USB"),
    Band("15m",  "15",   21000000,  21450000,  21200000, "USB"),
    Band("12m",  "12",   24890000,  24990000,  24940000, "USB"),
    Band("10m",  "10",   28000000,  29700000,  28400000, "USB"),
    Band("6m",   "6",    50000000,  54000000,  50125000, "USB"),
]

# Shortwave broadcast bands — useful for BCB listening. AM mode default.
# Short labels (just meter number) so they fit the button; row is
# already labeled "BC" so no ambiguity with amateur-band entries.
BROADCAST_BANDS: list[Band] = [
    Band("120m bc", "120",  2300000,   2495000,   2400000, "AM"),
    Band("90m bc",  "90",   3200000,   3400000,   3300000, "AM"),
    Band("75m bc",  "75",   3900000,   4000000,   3950000, "AM"),
    Band("60m bc",  "60",   4750000,   5060000,   4900000, "AM"),
    Band("49m bc",  "49",   5900000,   6200000,   5975000, "AM"),
    Band("41m bc",  "41",   7200000,   7450000,   7255000, "AM"),
    Band("31m bc",  "31",   9400000,   9900000,   9580000, "AM"),
    Band("25m bc",  "25",  11600000,  12100000,  11870000, "AM"),
    Band("22m bc",  "22",  13570000,  13870000,  13700000, "AM"),
    Band("19m bc",  "19",  15100000,  15830000,  15400000, "AM"),
    Band("16m bc",  "16",  17480000,  17900000,  17690000, "AM"),
    Band("13m bc",  "13",  21450000,  21850000,  21600000, "AM"),
]

ALL_BANDS: list[Band] = AMATEUR_BANDS + BROADCAST_BANDS


# General-coverage memory slots. Each GEN button is a free-tune
# "anywhere" slot that remembers the last freq/mode tuned while it was
# active — useful for utility listening, WWV, MW BCB, beacons, anything
# outside the structured band allocations.
GEN_SLOTS: list[Band] = [
    Band("GEN1", "GEN1",       100000,  55000000,  10000000, "AM"),   # WWV 10 MHz default
    Band("GEN2", "GEN2",       100000,  55000000,  15000000, "AM"),   # WWV 15 MHz default
    Band("GEN3", "GEN3",       100000,  55000000,   1000000, "AM"),   # 1 MHz MW slot
]


def band_for_freq(hz: int) -> Band | None:
    """Return the band whose range contains `hz`, preferring amateur
    allocations when ambiguity exists (e.g., 7.2 MHz is in both 40m ham
    and 41m BC — 40m ham wins so the button highlight matches operator
    intent)."""
    for b in AMATEUR_BANDS:
        if b.lo_hz <= hz <= b.hi_hz:
            return b
    for b in BROADCAST_BANDS:
        if b.lo_hz <= hz <= b.hi_hz:
            return b
    return None
