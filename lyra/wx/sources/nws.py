"""US National Weather Service active alerts + METAR observations.

Three fetch functions:
    fetch_storm_warnings(lat, lon)  -> headline string ('' if none)
    fetch_wind_alerts(lat, lon)     -> (headline, is_extreme)
    fetch_metar(station)            -> (sustained_mph, gust_mph, dir_deg)

The NWS API requires a User-Agent identifying the requesting app.
NWS alert endpoints accept point coordinates directly; METAR uses
ICAO station identifiers (e.g. ``KLUK``) — the operator picks their
nearest in Settings.

Algorithm and contracts ported from SDRLogger+
(`_fetch_noaa_warnings`, `_fetch_nws_wind_alerts`, `_fetch_nws_metar`).
"""
# Lyra-SDR — NWS adapter
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


HTTP_TIMEOUT_SEC = 10.0
USER_AGENT = "Lyra-SDR (Hermes Lite 2 client; contact: n8sdr@arrl.net)"

# Wind-alert event names that should drive the wind-alert tier.
# Matched case-insensitively as a substring.
WIND_PRODUCTS = (
    "high wind warning",
    "high wind watch",
    "wind advisory",
    "extreme wind warning",
)


def _http_get_json(url: str) -> Optional[dict]:
    """Fetch + parse JSON from NWS, with the required User-Agent.
    Returns parsed JSON or None on any failure."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                logger.debug("NWS HTTP %d for %s", resp.status, url)
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("NWS request error: %s", exc)
        return None


def fetch_storm_warnings(my_lat: float, my_lon: float) -> str:
    """Return the active severe thunderstorm / lightning warning
    headline for the operator's point, or ``""`` if none.

    Only returns alerts whose ``event`` field contains 'thunderstorm'
    or 'lightning' (case-insensitive).  For more general severe
    weather, see ``fetch_wind_alerts`` which covers the wind family.
    """
    url = (f"https://api.weather.gov/alerts/active"
           f"?point={my_lat:.4f},{my_lon:.4f}")
    data = _http_get_json(url)
    if not data:
        return ""
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        event = (props.get("event", "") or "").lower()
        if "thunderstorm" in event or "lightning" in event:
            return props.get("event", "") or ""
    return ""


def fetch_wind_alerts(my_lat: float, my_lon: float) -> tuple[str, bool]:
    """Return ``(headline, is_extreme)`` for active wind alerts at
    the operator's point.

    ``is_extreme`` is True when the alert is "High Wind Warning" or
    "Extreme Wind Warning" (the red-tier triggers).  Less severe
    advisories / watches return ``(headline, False)``.

    Empty headline means no wind alerts active.
    """
    url = (f"https://api.weather.gov/alerts/active"
           f"?point={my_lat:.4f},{my_lon:.4f}")
    data = _http_get_json(url)
    if not data:
        return "", False
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        event = (props.get("event", "") or "").lower()
        for prod in WIND_PRODUCTS:
            if prod in event:
                is_extreme = ("high wind warning" in event
                              or "extreme wind" in event)
                return props.get("event", "") or "", is_extreme
    return "", False


def _ms_to_mph(value_ms: float) -> float:
    return float(value_ms) * 2.23694


def fetch_metar(station: str
                ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(sustained_mph, gust_mph, dir_deg)`` from the latest
    METAR observation at the named ICAO station.

    Any field can be None when not reported (e.g. calm winds report
    no direction).  Returns all-None on error or when the station
    identifier is malformed.

    Use ``fetch_wind_alerts`` for alert-grade wind data; this
    function is for live observation when no alert is active and
    you want a numeric reading.
    """
    if not station or len(station) < 3:
        return None, None, None
    url = (f"https://api.weather.gov/stations/"
           f"{station.upper()}/observations/latest")
    data = _http_get_json(url)
    if not data:
        return None, None, None
    props = data.get("properties", {}) or {}
    # NWS reports speeds in m/s in the API regardless of station's
    # human-readable METAR string; convert to mph for our internal
    # representation.
    sustained_ms = ((props.get("windSpeed") or {}).get("value"))
    gust_ms = ((props.get("windGust") or {}).get("value"))
    direction = ((props.get("windDirection") or {}).get("value"))
    sustained_mph = (
        _ms_to_mph(sustained_ms) if sustained_ms is not None else None)
    gust_mph = _ms_to_mph(gust_ms) if gust_ms is not None else None
    direction_deg = (
        float(direction) if direction is not None else None)
    return sustained_mph, gust_mph, direction_deg
