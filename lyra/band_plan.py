"""Per-region ham band plans — sub-segments, mode-type coloring, and
optional landmarks (watering holes like FT8 / FT4 / WSPR).

**Design**: a single dict keyed by region ID. Each region has a list of
`Band` dicts, each of which has a list of `Segment` dicts with their
sub-type (CW / DIG / SSB / FM / MIX) and the color to paint. A second
list of `landmarks` marks specific "everyone hangs out here" frequencies
per band.

The HL2 hardware itself is **unlocked** — nothing stops you tuning to
any frequency it can receive. The band plan is purely advisory: the
panadapter paints segments; `Radio.band_plan_status()` returns an
out-of-band warning when the current tune leaves all the allocations.
When region == "NONE" the whole thing is a no-op (no strip, no warning).

**Scope caveat**: these are amateur-band allocations, with a conservative
subset of common sub-bands. Exact sub-band boundaries vary by license
class (US Extra vs General vs Tech), country, and calendar (WRC
updates). If you operate near an edge, verify against your region's
current regulator, not this table. The strip is a *navigation* aid, not
a legal reference.
"""
from __future__ import annotations

from typing import Literal, TypedDict


# Segment type → default color. Used by SpectrumWidget when drawing
# the colored strip. Adjust in one place to change the whole palette.
SEGMENT_COLORS: dict[str, str] = {
    "CW":   "#3c5a9c",    # deep blue — keyed modes
    "DIG":  "#9c3c9c",    # magenta — digital / data
    "SSB":  "#3c9c6a",    # green — voice
    "FM":   "#c47a2a",    # orange — FM phone
    "MIX":  "#5c8caa",    # teal — mixed-mode segments
    "BC":   "#7a7a3a",    # olive — broadcast (not amateur)
}


class Segment(TypedDict):
    low: int            # Hz, inclusive
    high: int           # Hz, exclusive
    kind: str           # "CW" / "DIG" / "SSB" / "FM" / "MIX"
    label: str          # human label (drawn in strip when room permits)


class Landmark(TypedDict):
    freq: int           # Hz
    label: str          # "FT8" / "FT4" / "WSPR" / "PSK31" / etc
    mode: str           # suggested demod mode for click-to-tune


class Band(TypedDict):
    name: str
    low: int
    high: int
    segments: list[Segment]


class Region(TypedDict):
    name: str           # display name
    bands: list[Band]
    landmarks: list[Landmark]


# ── US (FCC / IARU Region 2) — General / Extra class ─────────────────
_US_BANDS: list[Band] = [
    {"name": "160m", "low": 1_800_000, "high": 2_000_000, "segments": [
        {"low": 1_800_000, "high": 1_850_000, "kind": "CW",  "label": "CW/DIG"},
        {"low": 1_850_000, "high": 2_000_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "80m", "low": 3_500_000, "high": 4_000_000, "segments": [
        {"low": 3_500_000, "high": 3_600_000, "kind": "CW",  "label": "CW"},
        {"low": 3_570_000, "high": 3_600_000, "kind": "DIG", "label": "DIG"},
        {"low": 3_600_000, "high": 4_000_000, "kind": "SSB", "label": "SSB"},
    ]},
    # 60m US: 5 discrete channels. Paint each as a narrow SSB segment.
    {"name": "60m", "low": 5_330_500, "high": 5_406_500, "segments": [
        {"low": 5_330_000, "high": 5_331_000, "kind": "SSB", "label": "CH1"},
        {"low": 5_346_500, "high": 5_347_500, "kind": "SSB", "label": "CH2"},
        {"low": 5_357_000, "high": 5_358_000, "kind": "SSB", "label": "CH3"},
        {"low": 5_371_500, "high": 5_372_500, "kind": "SSB", "label": "CH4"},
        {"low": 5_403_500, "high": 5_404_500, "kind": "SSB", "label": "CH5"},
    ]},
    {"name": "40m", "low": 7_000_000, "high": 7_300_000, "segments": [
        {"low": 7_000_000, "high": 7_125_000, "kind": "CW",  "label": "CW"},
        {"low": 7_040_000, "high": 7_100_000, "kind": "DIG", "label": "DIG"},
        {"low": 7_125_000, "high": 7_300_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "30m", "low": 10_100_000, "high": 10_150_000, "segments": [
        {"low": 10_100_000, "high": 10_150_000, "kind": "DIG", "label": "CW/DIG"},
    ]},
    {"name": "20m", "low": 14_000_000, "high": 14_350_000, "segments": [
        {"low": 14_000_000, "high": 14_150_000, "kind": "CW",  "label": "CW"},
        {"low": 14_070_000, "high": 14_112_000, "kind": "DIG", "label": "DIG"},
        {"low": 14_150_000, "high": 14_350_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "17m", "low": 18_068_000, "high": 18_168_000, "segments": [
        {"low": 18_068_000, "high": 18_110_000, "kind": "CW",  "label": "CW/DIG"},
        {"low": 18_110_000, "high": 18_168_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "15m", "low": 21_000_000, "high": 21_450_000, "segments": [
        {"low": 21_000_000, "high": 21_200_000, "kind": "CW",  "label": "CW"},
        {"low": 21_070_000, "high": 21_110_000, "kind": "DIG", "label": "DIG"},
        {"low": 21_200_000, "high": 21_450_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "12m", "low": 24_890_000, "high": 24_990_000, "segments": [
        {"low": 24_890_000, "high": 24_930_000, "kind": "CW",  "label": "CW/DIG"},
        {"low": 24_930_000, "high": 24_990_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "10m", "low": 28_000_000, "high": 29_700_000, "segments": [
        {"low": 28_000_000, "high": 28_300_000, "kind": "CW",  "label": "CW"},
        {"low": 28_070_000, "high": 28_120_000, "kind": "DIG", "label": "DIG"},
        {"low": 28_300_000, "high": 29_500_000, "kind": "SSB", "label": "SSB"},
        {"low": 29_500_000, "high": 29_700_000, "kind": "FM",  "label": "FM"},
    ]},
    {"name": "6m", "low": 50_000_000, "high": 54_000_000, "segments": [
        {"low": 50_000_000, "high": 50_100_000, "kind": "CW",  "label": "CW"},
        {"low": 50_100_000, "high": 50_500_000, "kind": "SSB", "label": "SSB"},
        {"low": 50_500_000, "high": 54_000_000, "kind": "FM",  "label": "FM"},
    ]},
]

# Common digimode watering holes — these are the same globally by
# gentleman's agreement, so a single list can be referenced from each
# region's landmarks. If a region has a uniquely shifted FT8 freq we
# override in that region's list below.
_COMMON_LANDMARKS: list[Landmark] = [
    # FT8 — primary digimode, highest density
    {"freq": 1_840_000,  "label": "FT8",  "mode": "DIGU"},
    {"freq": 3_573_000,  "label": "FT8",  "mode": "DIGU"},
    {"freq": 7_074_000,  "label": "FT8",  "mode": "DIGU"},
    {"freq": 10_136_000, "label": "FT8",  "mode": "DIGU"},
    {"freq": 14_074_000, "label": "FT8",  "mode": "DIGU"},
    {"freq": 18_100_000, "label": "FT8",  "mode": "DIGU"},
    {"freq": 21_074_000, "label": "FT8",  "mode": "DIGU"},
    {"freq": 24_915_000, "label": "FT8",  "mode": "DIGU"},
    {"freq": 28_074_000, "label": "FT8",  "mode": "DIGU"},
    {"freq": 50_313_000, "label": "FT8",  "mode": "DIGU"},
    # FT4 — faster cousin of FT8
    {"freq": 3_575_000,  "label": "FT4",  "mode": "DIGU"},
    {"freq": 7_047_500,  "label": "FT4",  "mode": "DIGU"},
    {"freq": 10_140_000, "label": "FT4",  "mode": "DIGU"},
    {"freq": 14_080_000, "label": "FT4",  "mode": "DIGU"},
    {"freq": 18_104_000, "label": "FT4",  "mode": "DIGU"},
    {"freq": 21_140_000, "label": "FT4",  "mode": "DIGU"},
    {"freq": 28_180_000, "label": "FT4",  "mode": "DIGU"},
    # WSPR — weak-signal propagation beacons
    {"freq": 1_838_100,  "label": "WSPR", "mode": "DIGU"},
    {"freq": 3_568_600,  "label": "WSPR", "mode": "DIGU"},
    {"freq": 7_038_600,  "label": "WSPR", "mode": "DIGU"},
    {"freq": 10_138_700, "label": "WSPR", "mode": "DIGU"},
    {"freq": 14_095_600, "label": "WSPR", "mode": "DIGU"},
    {"freq": 18_104_600, "label": "WSPR", "mode": "DIGU"},
    {"freq": 21_094_600, "label": "WSPR", "mode": "DIGU"},
    {"freq": 24_924_600, "label": "WSPR", "mode": "DIGU"},
    {"freq": 28_124_600, "label": "WSPR", "mode": "DIGU"},
    {"freq": 50_293_000, "label": "WSPR", "mode": "DIGU"},
    # PSK31 — common phone/digital bridge
    {"freq": 7_070_000,  "label": "PSK",  "mode": "DIGU"},
    {"freq": 14_070_000, "label": "PSK",  "mode": "DIGU"},
    {"freq": 21_070_000, "label": "PSK",  "mode": "DIGU"},
    {"freq": 28_120_000, "label": "PSK",  "mode": "DIGU"},
    # NCDXF International Beacon Project — 5 fixed CW frequencies
    # where 18 worldwide stations rotate every 10 seconds.  The
    # marker is a static frequency label; the live "currently
    # transmitting: <callsign>" info appears in the hover tooltip
    # (driven by lyra.propagation.ncdxf_station_for_freq_khz at
    # paint time).
    {"freq": 14_100_000, "label": "NCDXF", "mode": "CWU"},
    {"freq": 18_110_000, "label": "NCDXF", "mode": "CWU"},
    {"freq": 21_150_000, "label": "NCDXF", "mode": "CWU"},
    {"freq": 24_930_000, "label": "NCDXF", "mode": "CWU"},
    {"freq": 28_200_000, "label": "NCDXF", "mode": "CWU"},
]


# IARU Region 1 (Europe/Africa/Middle East) — close to US but not
# identical. Where they match, same segments; where they differ,
# override. Source: IARU R1 HF band plan, condensed.
_R1_BANDS: list[Band] = [
    {"name": "160m", "low": 1_810_000, "high": 2_000_000, "segments": [
        {"low": 1_810_000, "high": 1_838_000, "kind": "CW",  "label": "CW"},
        {"low": 1_838_000, "high": 1_843_000, "kind": "DIG", "label": "DIG"},
        {"low": 1_843_000, "high": 2_000_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "80m", "low": 3_500_000, "high": 3_800_000, "segments": [
        {"low": 3_500_000, "high": 3_580_000, "kind": "CW",  "label": "CW"},
        {"low": 3_580_000, "high": 3_620_000, "kind": "DIG", "label": "DIG"},
        {"low": 3_600_000, "high": 3_800_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "40m", "low": 7_000_000, "high": 7_200_000, "segments": [
        {"low": 7_000_000, "high": 7_040_000, "kind": "CW",  "label": "CW"},
        {"low": 7_040_000, "high": 7_060_000, "kind": "DIG", "label": "DIG"},
        {"low": 7_060_000, "high": 7_200_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "30m", "low": 10_100_000, "high": 10_150_000, "segments": [
        {"low": 10_100_000, "high": 10_150_000, "kind": "DIG", "label": "CW/DIG"},
    ]},
    {"name": "20m", "low": 14_000_000, "high": 14_350_000, "segments": [
        {"low": 14_000_000, "high": 14_070_000, "kind": "CW",  "label": "CW"},
        {"low": 14_070_000, "high": 14_099_000, "kind": "DIG", "label": "DIG"},
        {"low": 14_099_000, "high": 14_350_000, "kind": "SSB", "label": "SSB"},
    ]},
    # R1 segments for higher bands roughly match R2 (US); reuse.
    {"name": "17m", "low": 18_068_000, "high": 18_168_000, "segments": [
        {"low": 18_068_000, "high": 18_095_000, "kind": "CW",  "label": "CW"},
        {"low": 18_095_000, "high": 18_110_000, "kind": "DIG", "label": "DIG"},
        {"low": 18_110_000, "high": 18_168_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "15m", "low": 21_000_000, "high": 21_450_000, "segments": [
        {"low": 21_000_000, "high": 21_070_000, "kind": "CW",  "label": "CW"},
        {"low": 21_070_000, "high": 21_151_000, "kind": "DIG", "label": "DIG"},
        {"low": 21_151_000, "high": 21_450_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "12m", "low": 24_890_000, "high": 24_990_000, "segments": [
        {"low": 24_890_000, "high": 24_915_000, "kind": "CW",  "label": "CW"},
        {"low": 24_915_000, "high": 24_930_000, "kind": "DIG", "label": "DIG"},
        {"low": 24_930_000, "high": 24_990_000, "kind": "SSB", "label": "SSB"},
    ]},
    {"name": "10m", "low": 28_000_000, "high": 29_700_000, "segments": [
        {"low": 28_000_000, "high": 28_070_000, "kind": "CW",  "label": "CW"},
        {"low": 28_070_000, "high": 28_190_000, "kind": "DIG", "label": "DIG"},
        {"low": 28_190_000, "high": 29_520_000, "kind": "SSB", "label": "SSB"},
        {"low": 29_520_000, "high": 29_700_000, "kind": "FM",  "label": "FM"},
    ]},
    {"name": "6m", "low": 50_000_000, "high": 54_000_000, "segments": [
        {"low": 50_000_000, "high": 50_100_000, "kind": "CW",  "label": "CW"},
        {"low": 50_100_000, "high": 50_500_000, "kind": "SSB", "label": "SSB"},
        {"low": 50_500_000, "high": 54_000_000, "kind": "FM",  "label": "FM"},
    ]},
]


# IARU Region 3 (Asia/Pacific). Close to R1 on HF, with 40m allocation
# truncated to 7.000–7.200 MHz (like R1, not the US 7.000–7.300).
_R3_BANDS: list[Band] = [
    # Reuse R1 for most; 60m / 160m vary per-CEPT/admin but we keep it
    # simple with the common admin-unrestricted values. If a user
    # reports a specific admin mismatch we can break out a separate
    # entry later.
    b for b in _R1_BANDS
]


REGIONS: dict[str, Region] = {
    "US": {
        "name": "United States (FCC / IARU R2)",
        "bands": _US_BANDS,
        "landmarks": _COMMON_LANDMARKS,
    },
    "IARU_R1": {
        "name": "IARU Region 1 (Europe / Africa / Middle East)",
        "bands": _R1_BANDS,
        "landmarks": _COMMON_LANDMARKS,
    },
    "IARU_R3": {
        "name": "IARU Region 3 (Asia / Pacific)",
        "bands": _R3_BANDS,
        "landmarks": _COMMON_LANDMARKS,
    },
    "NONE": {
        "name": "No band plan (HL2 unlocked — no strip, no warnings)",
        "bands": [],
        "landmarks": [],
    },
}


DEFAULT_REGION = "US"


# ── Query helpers used by Radio + SpectrumWidget ─────────────────────

def get_region(region_id: str) -> Region:
    """Look up a region. Falls back to the default if unknown."""
    return REGIONS.get(region_id, REGIONS[DEFAULT_REGION])


def find_band(region_id: str, freq_hz: int) -> Band | None:
    """Return the Band object containing `freq_hz`, or None if the
    frequency falls outside every allocated amateur band in the region."""
    reg = get_region(region_id)
    for b in reg["bands"]:
        if b["low"] <= freq_hz < b["high"]:
            return b
    return None


def visible_segments(region_id: str,
                     center_hz: float, span_hz: float) -> list[tuple[Segment, int, int]]:
    """Yield segments that intersect the visible window and return them
    as (segment_dict, clipped_low_hz, clipped_high_hz) triples so the
    widget can paint without re-clipping."""
    if span_hz <= 0 or region_id == "NONE":
        return []
    lo = int(center_hz - span_hz / 2)
    hi = int(center_hz + span_hz / 2)
    out: list[tuple[Segment, int, int]] = []
    for b in get_region(region_id)["bands"]:
        if b["high"] <= lo or b["low"] >= hi:
            continue
        for seg in b["segments"]:
            if seg["high"] <= lo or seg["low"] >= hi:
                continue
            out.append((seg, max(seg["low"], lo), min(seg["high"], hi)))
    return out


def visible_landmarks(region_id: str,
                      center_hz: float, span_hz: float) -> list[Landmark]:
    if span_hz <= 0 or region_id == "NONE":
        return []
    lo = center_hz - span_hz / 2
    hi = center_hz + span_hz / 2
    return [m for m in get_region(region_id)["landmarks"]
            if lo <= m["freq"] <= hi]
