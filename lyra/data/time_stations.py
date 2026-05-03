"""HF time-signal station data for Lyra's TIME button.

Static, version-controlled, ships embedded.  Operator never edits
this file directly; if a new station comes online or an existing
one shuts down, the table gets updated in a Lyra release.

Source: operator-supplied "WWV and the likes of.docx"
(2026-05-02), cross-referenced with NIST Time and Frequency
Division publications.

Coverage as of 2026-05:

    Station   Country  Continent  Mode  Frequencies (kHz)
    -------   -------  ---------  ----  ---------------------------
    WWV       US       NA         AM    2500, 5000, 10000, 15000,
                                        20000, 25000
    WWVH      US       OC         AM    2500, 5000, 10000, 15000
    CHU       CA       NA         USB   3330, 7850, 14670
    BPM       CN       AS         AM    2500, 5000, 10000, 15000
    RWM       RU       EU         AM    4996, 9996, 14996
    HLA       KR       AS         AM    5000
    YVTO      VE       SA         AM    5000
    LOL       AR       SA         AM    5000, 10000, 15000
    HD2IOA    EC       SA         AM    3810, 7600

LF stations (JJY, MSF, DCF77, BPC) are documented in the source
docx but are NOT included here -- HL2 / HL2+ has no LF coverage
below ~100 kHz, so they're outside Lyra's reception capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TimeStation:
    """One HF time-signal station.

    Attributes:
        id: short identifier (callsign-like; "WWV", "BPM", "RWM").
        name: display name including site location.
        country_code: ISO-3166-1 alpha-2 (e.g. "US", "CN", "RU").
        continent: short continent code -- "NA", "EU", "AS", "OC",
            "SA", "AF".
        mode: receive mode for this station -- "AM", "USB", or
            (rarely) "LSB".  Most time stations are AM with the
            voice / pulse content as full-carrier modulation;
            CHU is the famous USB exception.
        freqs_khz: list of broadcast frequencies in kHz, ordered
            from lowest to highest.  Cycle order within a station
            walks this list left-to-right.
        schedule: human-readable schedule hint -- "24/7",
            "0600-2200 UTC", "intermittent", "limited", etc.  Shown
            in tooltip / right-click menu so operator can guess
            whether they'll hear it.
        notes: optional free-text remarks (e.g. "Spanish voice",
            "Morse code only").  Shown in tooltip.
    """
    id: str
    name: str
    country_code: str
    continent: str
    mode: str
    freqs_khz: list[int] = field(default_factory=list)
    schedule: str = "24/7"
    notes: str = ""


TIME_STATIONS: list[TimeStation] = [
    TimeStation(
        id="WWV",
        name="WWV (Fort Collins, CO)",
        country_code="US", continent="NA", mode="AM",
        freqs_khz=[2500, 5000, 10000, 15000, 20000, 25000],
        schedule="24/7",
        notes="Voice + tones + 100 Hz subcarrier",
    ),
    TimeStation(
        id="WWVH",
        name="WWVH (Kekaha, HI)",
        country_code="US", continent="OC", mode="AM",
        freqs_khz=[2500, 5000, 10000, 15000],
        schedule="24/7",
        notes="Pacific counterpart to WWV",
    ),
    TimeStation(
        id="CHU",
        name="CHU (Ottawa, ON)",
        country_code="CA", continent="NA", mode="USB",
        freqs_khz=[3330, 7850, 14670],
        schedule="24/7",
        notes="Note: USB mode, not AM",
    ),
    TimeStation(
        id="BPM",
        name="BPM (Pucheng, China)",
        country_code="CN", continent="AS", mode="AM",
        freqs_khz=[2500, 5000, 10000, 15000],
        schedule="24/7",
        notes="One of Asia's most powerful time stations",
    ),
    TimeStation(
        id="RWM",
        name="RWM (Moscow, RU)",
        country_code="RU", continent="EU", mode="AM",
        freqs_khz=[4996, 9996, 14996],
        schedule="24/7",
        notes="Morse code + pulses only -- no voice",
    ),
    TimeStation(
        id="HLA",
        name="HLA (Daejeon, KR)",
        country_code="KR", continent="AS", mode="AM",
        freqs_khz=[5000],
        schedule="24/7",
    ),
    TimeStation(
        id="YVTO",
        name="YVTO (Caracas, VE)",
        country_code="VE", continent="SA", mode="AM",
        freqs_khz=[5000],
        schedule="24/7",
        notes="Spanish voice announcements",
    ),
    TimeStation(
        id="LOL",
        name="LOL (Buenos Aires, AR)",
        country_code="AR", continent="SA", mode="AM",
        freqs_khz=[5000, 10000, 15000],
        schedule="limited",
        notes="Limited schedule, not 24/7",
    ),
    TimeStation(
        id="HD2IOA",
        name="HD2IOA (Guayaquil, EC)",
        country_code="EC", continent="SA", mode="AM",
        freqs_khz=[3810, 7600],
        schedule="intermittent",
    ),
]


# ── Country → Continent mapping for time-station ordering ────────────
# Compact table covering the countries that appear in TIME_STATIONS
# plus the most common operator-home countries.  We don't need the
# full ~250-country ISO list -- if the operator's country isn't here
# we fall through to "no preference" ordering (just use the static
# list order).
_COUNTRY_TO_CONTINENT: dict[str, str] = {
    # North America
    "US": "NA", "CA": "NA", "MX": "NA",
    # Caribbean/Central America (group with NA for HF propagation)
    "PR": "NA", "VI": "NA", "DO": "NA", "JM": "NA", "BS": "NA",
    "GT": "NA", "BZ": "NA", "HN": "NA", "SV": "NA", "NI": "NA",
    "CR": "NA", "PA": "NA", "CU": "NA", "HT": "NA",
    # South America
    "AR": "SA", "BR": "SA", "CL": "SA", "CO": "SA", "PE": "SA",
    "VE": "SA", "EC": "SA", "BO": "SA", "PY": "SA", "UY": "SA",
    "GY": "SA", "SR": "SA",
    # Europe
    "GB": "EU", "DE": "EU", "FR": "EU", "IT": "EU", "ES": "EU",
    "PT": "EU", "NL": "EU", "BE": "EU", "CH": "EU", "AT": "EU",
    "PL": "EU", "CZ": "EU", "SK": "EU", "HU": "EU", "RO": "EU",
    "BG": "EU", "GR": "EU", "TR": "EU", "RU": "EU", "UA": "EU",
    "BY": "EU", "SE": "EU", "NO": "EU", "FI": "EU", "DK": "EU",
    "IE": "EU", "IS": "EU", "EE": "EU", "LV": "EU", "LT": "EU",
    "LU": "EU", "MT": "EU", "CY": "EU", "HR": "EU", "SI": "EU",
    "RS": "EU", "BA": "EU", "MK": "EU", "AL": "EU", "MD": "EU",
    # Asia
    "JP": "AS", "CN": "AS", "KR": "AS", "TW": "AS", "HK": "AS",
    "IN": "AS", "PK": "AS", "BD": "AS", "TH": "AS", "VN": "AS",
    "PH": "AS", "MY": "AS", "ID": "AS", "SG": "AS", "AE": "AS",
    "SA": "AS", "IL": "AS", "IR": "AS", "IQ": "AS", "JO": "AS",
    "LB": "AS", "SY": "AS", "KZ": "AS", "UZ": "AS", "MN": "AS",
    "NP": "AS", "LK": "AS", "MM": "AS", "KH": "AS", "LA": "AS",
    # Oceania
    "AU": "OC", "NZ": "OC", "FJ": "OC", "PG": "OC", "WS": "OC",
    "TO": "OC", "VU": "OC", "SB": "OC",
    # Africa
    "ZA": "AF", "EG": "AF", "MA": "AF", "DZ": "AF", "TN": "AF",
    "LY": "AF", "NG": "AF", "KE": "AF", "ET": "AF", "GH": "AF",
    "TZ": "AF", "UG": "AF", "ZW": "AF", "ZM": "AF", "AO": "AF",
    "MZ": "AF", "MG": "AF", "CM": "AF", "CI": "AF", "SN": "AF",
}


def country_to_continent(iso2: str) -> str:
    """Return the continent code for an ISO-3166-1 alpha-2 country
    code.  Returns empty string if unknown -- caller treats that as
    'no continent preference.'  Matches the continent codes used in
    ``TimeStation.continent`` (NA/EU/AS/OC/SA/AF)."""
    return _COUNTRY_TO_CONTINENT.get((iso2 or "").upper(), "")


# ── Station ordering ────────────────────────────────────────────────

def order_stations(operator_country: str) -> list[TimeStation]:
    """Return ``TIME_STATIONS`` sorted by operator priority.

    Priority order:
      1. Stations in the operator's own country.
      2. Stations on the same continent.
      3. Everything else, in the static-list order.

    When ``operator_country`` is empty or unrecognized, returns
    the static list order unchanged -- the operator gets a sensible
    cycle starting at WWV (which is naturally first because N8SDR
    is the project's primary tester and WWV is the relevant station
    for most North American operators).

    Example::

        >>> stations = order_stations("VE")
        >>> [s.id for s in stations][:3]
        ['YVTO', 'LOL', 'HD2IOA']  # SA stations first for VE op
    """
    op = (operator_country or "").upper()
    op_continent = country_to_continent(op)
    same_country = []
    same_continent = []
    rest = []
    for s in TIME_STATIONS:
        if op and s.country_code == op:
            same_country.append(s)
        elif op_continent and s.continent == op_continent:
            same_continent.append(s)
        else:
            rest.append(s)
    return same_country + same_continent + rest


# ── Cycle iteration ─────────────────────────────────────────────────

def total_cycle_length(stations: list[TimeStation]) -> int:
    """Sum of frequencies across all stations -- the cycle period.
    Useful for the Settings UI to show '12 entries in cycle' etc."""
    return sum(len(s.freqs_khz) for s in stations)


def cycle_entry(stations: list[TimeStation],
                idx: int) -> tuple[TimeStation, int]:
    """Resolve a flat cycle-index into ``(station, freq_khz)``.

    The cycle walks each station's freq list before advancing to
    the next station, so for a STATIONS=[WWV, CHU] order the cycle
    would be (WWV,2500), (WWV,5000), (WWV,10000), ..., (WWV,25000),
    (CHU,3330), (CHU,7850), (CHU,14670), then wrap.

    ``idx`` is taken modulo ``total_cycle_length`` so callers can
    just always increment without worrying about overflow.

    Raises ValueError if ``stations`` is empty (caller is expected
    to provide at least one station).
    """
    if not stations:
        raise ValueError("stations list is empty")
    total = total_cycle_length(stations)
    if total <= 0:
        raise ValueError("no frequencies in any station")
    n = idx % total
    for s in stations:
        L = len(s.freqs_khz)
        if n < L:
            return s, s.freqs_khz[n]
        n -= L
    # Unreachable (modular arithmetic above guarantees a hit), but
    # mypy / static analyzers like an explicit fallback.
    s = stations[0]
    return s, s.freqs_khz[0]
