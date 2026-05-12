"""Propagation data sources — HamQSL solar feed + NCDXF beacon schedule.

This module is the data layer behind the Propagation panel and the
NCDXF spectrum markers.  Three concerns:

1. ``HamQslSolarCache`` — polls https://www.hamqsl.com/solarxml.php
   on a 15-minute cadence, returns SFI / SSN / A / K / X-Ray + per-
   band Day/Night condition predictions.

2. ``ncdxf_*`` helpers — pure-math NCDXF International Beacon
   Project schedule.  Given a UTC datetime, returns which of the 18
   stations is transmitting on each of the 5 bands right now.  No
   network — schedule is GPS/NTP-synced via the system clock.

3. ``is_daylight`` — cheap NOAA-style sunrise/sunset check.  Used
   by the panel to pick HamQSL's Day or Night rating per band based
   on the operator's QTH + current UTC.

All three are independent — the panel can render solar without
beacons, beacons without solar, etc.  The module has no Qt /
Lyra dependencies, so it's importable and unit-testable in
isolation.
"""
from __future__ import annotations

import math
import ssl
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional


# ──────────────────────────────────────────────────────────────────────
# HamQSL solar feed
# ──────────────────────────────────────────────────────────────────────

class HamQslSolarCache:
    """In-memory cache for HamQSL's solarxml feed.

    The HamQSL XML at hamqsl.com/solarxml.php publishes solar /
    geomagnetic numbers every ~15 minutes, plus per-band-pair Day
    and Night condition predictions ("Good" / "Fair" / "Poor").

    Cache TTL matches the publishing cadence.  On fetch failure we
    serve stale data rather than churning — the operator gets the
    last-known values until the next successful poll.

    Thread-safe: a lock guards the cache fields so the panel can
    read from the Qt main thread while a background timer triggers
    refreshes from a worker thread.

    Returned dict shape::

        {
          "sfi":      "130",
          "sunspots": "80",
          "aindex":   "5",
          "kindex":   "2",
          "xray":     "B1.2",
          "solarwind":"380",
          "updated":  "08 May 2026 1700 GMT",
          "bands": {
            "80m-40m":  {"day": "Good", "night": "Fair"},
            "30m-20m":  {"day": "Good", "night": "Good"},
            "17m-15m":  {"day": "Fair", "night": "Poor"},
            "12m-10m":  {"day": "Fair", "night": "Poor"},
          }
        }

    Numbers are returned as strings (matches the XML directly);
    callers that need int/float should parse defensively because
    HamQSL occasionally returns "N/A" or "?" for unreachable values.
    """

    URL = "https://www.hamqsl.com/solarxml.php"
    CACHE_TTL_SEC = 15 * 60   # 15 minutes — matches HamQSL publishing
    FETCH_TIMEOUT_SEC = 10

    def __init__(self) -> None:
        self._data: Optional[dict] = None
        self._timestamp: float = 0.0
        self._lock = threading.Lock()
        # Last fetch exception captured for diagnostics.  Operator-
        # visible via the PropagationPanel's tooltip + status bar
        # when present.  Cleared on a successful fetch.  See
        # `last_error` property.
        self._last_error: Optional[str] = None

    @property
    def last_error(self) -> Optional[str]:
        """Most recent fetch error text (short form), or None.

        Surfaced by the propagation panel so operators behind a
        firewall / SSL block / DNS issue see WHY the panel is
        blank instead of staring at endless "—" placeholders.
        Cleared once a fetch succeeds.
        """
        with self._lock:
            return self._last_error

    def get(self, force_refresh: bool = False) -> Optional[dict]:
        """Return cached solar data, refreshing if stale or forced.

        Returns None only if no fetch has ever succeeded — once a
        fetch lands the cache holds onto it across network errors.

        Phase 3.E.1 hotfix v0.18 (2026-05-12): captures the last
        fetch exception in ``_last_error`` so it can be surfaced
        to the operator via the panel tooltip.  Operator-reported
        2026-05-12: tester "Timmy"'s propagation panel was blank
        even with callsign/grid set + different video drivers
        tried (since the EiBi backend-mix-up that Brent hit was
        ruled out).  Most likely cause: synchronous HTTPS fetch
        to hamqsl.com failing silently (firewall / SSL / DNS /
        antivirus interception).  Now the exception is logged AND
        readable from the UI so the operator can pin it down.
        """
        now = time.time()
        with self._lock:
            stale = (
                self._data is None
                or (now - self._timestamp) >= self.CACHE_TTL_SEC
            )
            if not (force_refresh or stale):
                return self._data

        try:
            data = self._fetch()
        except Exception as exc:
            # Network glitch or HamQSL hiccup — serve stale cache
            # rather than blanking the operator's display.  Capture
            # the exception text + log to console (crash.log on the
            # PyInstaller build) so operators have something to
            # report when the panel is silently stuck.
            err = f"{type(exc).__name__}: {exc}"
            print(f"[HamQslSolarCache] fetch failed: {err}")
            with self._lock:
                self._last_error = err
                return self._data

        with self._lock:
            self._data = data
            self._timestamp = now
            self._last_error = None
            return data

    def _fetch(self) -> dict:
        """Hit hamqsl.com, parse the XML, return the normalized dict.

        Phase 3.E.1 hotfix v0.21 (2026-05-12): SSL cert verification
        disabled to match SDRLogger+'s posture for this same feed
        (``requests.get(..., verify=False)`` at hamlog/main.py:4983).

        Rationale: this is a read-only fetch of public solar data
        from a fixed URL.  No credentials, no PII, no operator
        action triggered by the response (just numeric display).
        Cert verification was providing zero practical security
        for this specific call -- a MITM could only inject wrong
        sunspot numbers, which the operator would notice
        immediately (it's a known band).  Meanwhile cert
        verification was the root cause of tester "Timmy"'s
        blank propagation panel (Rick 2026-05-12): same machine
        where SDRLogger+ fetches the same URL fine, Lyra's
        urllib stdlib-default verification was rejecting
        hamqsl.com's cert chain (likely an intermediate-CA
        mismatch between Python's bundled certifi and what
        hamqsl.com served).

        If we ever extend this fetcher to APIs with operator
        credentials or actionable side effects, switch back to
        verified context AND add a ``truststore``-based fallback
        for cert-chain issues.
        """
        req = urllib.request.Request(
            self.URL,
            headers={"User-Agent": "Lyra-SDR/1.0"},
        )
        # Unverified SSL context -- see method docstring for the
        # security rationale (read-only public solar data feed).
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(
                req,
                timeout=self.FETCH_TIMEOUT_SEC,
                context=ctx) as resp:
            xml_bytes = resp.read()

        root = ET.fromstring(xml_bytes)
        # Some XML revisions wrap the data in <solardata>; others
        # return at root.  Find ".//solardata" handles both.
        sd = root.find(".//solardata")
        if sd is None:
            sd = root

        def gf(tag: str) -> str:
            el = sd.find(tag)
            if el is not None and el.text:
                return el.text.strip()
            return ""

        bands: dict[str, dict[str, str]] = {}
        for b in sd.findall(".//calculatedconditions/band"):
            name = b.get("name", "")
            time_of = (b.get("time", "") or "").lower()
            val = (b.text or "").strip()
            if name and time_of:
                bands.setdefault(name, {})[time_of] = val

        return {
            "sfi":       gf("solarflux"),
            "sunspots":  gf("sunspots"),
            "aindex":    gf("aindex"),
            "kindex":    gf("kindex"),
            "xray":      gf("xray"),
            "solarwind": gf("solarwind"),
            "updated":   gf("updated"),
            "bands":     bands,
        }


# ──────────────────────────────────────────────────────────────────────
# NCDXF International Beacon Project schedule
# ──────────────────────────────────────────────────────────────────────

# 18 stations in slot order (slot 0 → 17).  Each station owns a 10-
# second window per cycle.  The cycle is the same on all 5 bands
# but offset by band index — see station_for_band() below.
#
# Source: NCDXF official schedule https://www.ncdxf.org/beacon/
# Coordinates are approximate site lat/lon for the world-map display.
# KH6RS (Maui) is listed even though the physical beacon was
# decommissioned in 2010 — the slot is officially "reserved" and
# operators using sequence-aware tools still see it cycle through.

NCDXF_STATIONS: list[tuple[str, str, float, float]] = [
    # (callsign,  description,                     lat,    lon)
    ("4U1UN",    "United Nations, NY",            40.75,  -73.97),
    ("VE8AT",    "Eureka, NU, Canada",            79.99,  -85.94),
    ("W6WX",     "Mt Umunhum, CA",                37.18, -121.90),
    ("KH6RS",    "Maui, HI (decommissioned)",     20.78, -156.46),
    ("ZL6B",     "Masterton, NZ",                -41.05,  175.65),
    ("VK6RBP",   "Rolystone, Perth, Australia",  -32.10,  116.04),
    ("JA2IGY",   "Mt Asama, Japan",               34.45,  136.79),
    ("RR9O",     "Novosibirsk, Russia",           55.00,   83.00),
    ("VR2HK6",   "Hong Kong",                     22.32,  114.17),
    ("4S7B",     "Colombo, Sri Lanka",             6.85,   79.87),
    ("ZS6DN",    "Pretoria, South Africa",       -25.91,   28.20),
    ("5Z4B",     "Kilimambogo, Kenya",            -1.13,   37.20),
    ("4X6TU",    "Tel Aviv, Israel",              32.05,   34.78),
    ("OH2B",     "Lohja, Finland",                60.25,   24.39),
    ("CS3B",     "Madeira, Portugal",             32.65,  -16.91),
    ("LU4AA",    "Buenos Aires, Argentina",      -34.61,  -58.43),
    ("OA4B",     "Lima, Peru",                   -12.05,  -77.04),
    ("YV5B",     "Caracas, Venezuela",            10.49,  -66.91),
]

# Operator-facing band index → frequency (kHz) at which NCDXF
# transmits on that band.  Band order is fixed (20m → 17m → 15m
# → 12m → 10m) and matters: it's the offset used by station_for_band.
NCDXF_BANDS: list[tuple[str, int]] = [
    ("20m", 14_100),
    ("17m", 18_110),
    ("15m", 21_150),
    ("12m", 24_930),
    ("10m", 28_200),
]


def ncdxf_current_slot(when_utc: Optional[datetime] = None) -> int:
    """Return the active NCDXF slot (0..17) at ``when_utc``.

    The cycle resets every 3 minutes (18 stations × 10 sec).  Slot 0
    starts at second 0 of any UTC minute whose ``minute % 3 == 0``.

    If ``when_utc`` is None we use ``datetime.now(timezone.utc)``.
    """
    if when_utc is None:
        when_utc = datetime.now(timezone.utc)
    secs_in_cycle = (when_utc.minute % 3) * 60 + when_utc.second
    return secs_in_cycle // 10


def ncdxf_station_for_band(band_idx: int,
                           slot: Optional[int] = None) -> int:
    """Return the station index (0..17) transmitting on ``band_idx``.

    Bands are in NCDXF_BANDS order (0 = 20m through 4 = 10m).  The
    cycle on each band is offset by the band index, so when station 0
    is on 20m, station 17 is on 17m, station 16 is on 15m, etc.  This
    is what makes 5 stations transmit simultaneously, each on a
    different band.
    """
    if slot is None:
        slot = ncdxf_current_slot()
    return (slot - band_idx) % len(NCDXF_STATIONS)


def ncdxf_seconds_until_next_slot(
        when_utc: Optional[datetime] = None) -> int:
    """Seconds remaining until the current 10-sec slot ends (1..10)."""
    if when_utc is None:
        when_utc = datetime.now(timezone.utc)
    secs_in_cycle = (when_utc.minute % 3) * 60 + when_utc.second
    return 10 - (secs_in_cycle % 10)


def ncdxf_station_for_freq_khz(
        freq_khz: int,
        when_utc: Optional[datetime] = None,
        ) -> Optional[tuple[str, str]]:
    """Look up the current beacon at one of the 5 NCDXF frequencies.

    Returns ``(callsign, description)`` if ``freq_khz`` matches a
    NCDXF band frequency, else None.  Used by the spectrum-marker
    tooltip — pass the marker's frequency and we tell you who's on
    it right now.
    """
    for band_idx, (_, f) in enumerate(NCDXF_BANDS):
        if abs(freq_khz - f) <= 1:   # ±1 kHz tolerance
            slot = ncdxf_current_slot(when_utc)
            station_idx = ncdxf_station_for_band(band_idx, slot)
            call, desc, _, _ = NCDXF_STATIONS[station_idx]
            return (call, desc)
    return None


# ──────────────────────────────────────────────────────────────────────
# Sunrise / sunset — cheap NOAA-ish algorithm
# ──────────────────────────────────────────────────────────────────────

def is_daylight(lat: float,
                lon: float,
                when_utc: Optional[datetime] = None) -> bool:
    """Return True if the sun is above the horizon at (lat, lon, t).

    Uses the standard NOAA solar-position formula simplified to
    sunrise/sunset hour angles only.  Accurate to ±2 minutes —
    plenty for "is the band in Day or Night propagation mode" lookups.

    Polar-night and polar-day cases are handled.  Pure stdlib (no
    astropy / pyephem dependency).
    """
    if when_utc is None:
        when_utc = datetime.now(timezone.utc)

    # Day of year (1..366).
    yday = (when_utc - datetime(when_utc.year, 1, 1,
                                tzinfo=timezone.utc)).days + 1

    # Solar declination (radians) — sin approximation accurate to
    # within ~0.5 deg, sufficient for sunrise/sunset to ±2 min.
    decl = math.radians(23.44) * math.sin(
        math.radians(360.0 / 365.0 * (284 + yday)))

    lat_rad = math.radians(lat)
    cos_h = -math.tan(lat_rad) * math.tan(decl)
    if cos_h > 1.0:
        # Sun never rises (polar night).
        return False
    if cos_h < -1.0:
        # Sun never sets (polar day).
        return True

    h_deg = math.degrees(math.acos(cos_h))   # hour angle, degrees
    # Solar noon at this longitude (UTC hours).  Negative lon → west,
    # so noon UTC at lon=-75 is at 17:00 UTC.
    solar_noon_utc = 12.0 - lon / 15.0
    sunrise_h = (solar_noon_utc - h_deg / 15.0) % 24.0
    sunset_h = (solar_noon_utc + h_deg / 15.0) % 24.0

    cur_h = (when_utc.hour
             + when_utc.minute / 60.0
             + when_utc.second / 3600.0)

    if sunrise_h < sunset_h:
        return sunrise_h <= cur_h <= sunset_h
    # Day-wraparound (e.g. high-latitude near solstice with
    # sunset → sunrise crossing midnight UTC).
    return cur_h >= sunrise_h or cur_h <= sunset_h


# ──────────────────────────────────────────────────────────────────────
# HamQSL band-condition lookup (Day/Night auto-pick)
# ──────────────────────────────────────────────────────────────────────

# Lyra ham band → HamQSL "calculatedconditions/band[name]" key.
# HamQSL groups bands in pairs and doesn't predict 160m or 6m, so
# those entries return None and the panel renders them gray.
_HAMQSL_BAND_GROUP: dict[str, Optional[str]] = {
    "160": None,         # not predicted
    "80":  "80m-40m",
    "40":  "80m-40m",
    "30":  "30m-20m",
    "20":  "30m-20m",
    "17":  "17m-15m",
    "15":  "17m-15m",
    "12":  "12m-10m",
    "10":  "12m-10m",
    "6":   None,         # not predicted
}


def hamqsl_rating_for_band(
        band: str,
        bands_dict: dict,
        is_day: bool) -> Optional[str]:
    """Look up HamQSL's predicted rating for a Lyra ham band.

    ``band`` — Lyra band label ("160", "80", "40", ...).
    ``bands_dict`` — the "bands" sub-dict from HamQslSolarCache.get().
    ``is_day`` — pick the Day rating if True, Night rating if False.

    Returns "Good" / "Fair" / "Poor" / something HamQSL provides,
    or None if HamQSL doesn't predict this band.
    """
    group = _HAMQSL_BAND_GROUP.get(band)
    if group is None:
        return None
    entry = bands_dict.get(group, {})
    return entry.get("day" if is_day else "night")


def rating_color_hex(rating: Optional[str]) -> str:
    """Map a HamQSL rating string to a stable hex color.

    Operators tested these colors against real-band-condition feedback
    in SDRLogger+ and they read well against Lyra's dark theme too.
    None / unknown maps to a muted gray.
    """
    r = (rating or "").strip().lower()
    if r == "good":
        return "#4caf50"     # green
    if r == "fair":
        return "#f0c040"     # yellow
    if r == "poor":
        return "#e05c5c"     # red
    return "#5a6573"         # muted gray for "no data"
