"""Blitzortung public lightning-strike feed.

Polls Blitzortung's regional GeoJSON endpoints, filters strikes by
haversine distance from the operator's location, and returns the
list of strikes within range.

Algorithm and endpoint details ported from SDRLogger+
(`_fetch_blitzortung` at main.py:5347).  Lyra's version takes the
operator location as arguments rather than reading globals, and uses
``urllib.request`` instead of ``requests`` (no extra dependency).

Blitzortung's TOS:
    "The data are free for personal and non-commercial use ..."
    See https://www.blitzortung.org/en/cover_your_area.php

The response format is a flat list of `[lon, lat, ts, ...]` arrays
per regional endpoint.  Lyra polls regions covering the Americas by
default (7, 12, 13); operators in other regions can override.
"""
# Lyra-SDR — Blitzortung adapter
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License v3
# or later.  See LICENSE in the project root for the full terms.
from __future__ import annotations

import json
import logging
import math
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


# Region codes per Blitzortung's regional split:
#   1 = Europe              7 = Central Americas
#   2 = Oceania             12 = East Americas (US east of Mississippi)
#   4 = East Asia           13 = West Americas (US west of Mississippi)
#   6 = South America
# Default covers the contiguous US + Canada + Caribbean.
DEFAULT_REGIONS = (7, 12, 13)
URL_TEMPLATE = "https://map.blitzortung.org/GEOjson/getjson.php?f=s&n={region:02d}"
HTTP_TIMEOUT_SEC = 8.0
HTTP_HEADERS = {
    "Referer": "https://map.blitzortung.org/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Lyra-SDR",
}


def _haversine_km(lat1: float, lon1: float,
                   lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two points."""
    lat1r, lon1r, lat2r, lon2r = map(
        math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float,
                  lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees from North."""
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(lat2r)
    x = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def fetch_strikes(my_lat: float, my_lon: float, range_km: float,
                  regions: Optional[tuple[int, ...]] = None
                  ) -> list[tuple[float, float]]:
    """Return strikes within ``range_km`` of the operator's point.

    Each entry is a ``(distance_km, bearing_deg)`` tuple.  Empty list
    on failure or when no strikes are within range.

    The Blitzortung endpoints don't require an API key but do enforce
    rate-limiting and User-Agent / Referer checks; the constants at
    the top of this module match what Pratt's WDSP-clients use.
    """
    if regions is None:
        regions = DEFAULT_REGIONS
    strikes: list[tuple[float, float]] = []
    for region in regions:
        url = URL_TEMPLATE.format(region=region)
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                if resp.status != 200:
                    logger.debug(
                        "Blitzortung region %02d HTTP %d", region, resp.status)
                    continue
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.debug("Blitzortung region %02d error: %s", region, exc)
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not (isinstance(item, list) and len(item) >= 2):
                continue
            try:
                s_lon = float(item[0])
                s_lat = float(item[1])
            except (ValueError, TypeError):
                continue
            dist = _haversine_km(my_lat, my_lon, s_lat, s_lon)
            if dist <= range_km:
                brg = _bearing_deg(my_lat, my_lon, s_lat, s_lon)
                strikes.append((dist, brg))
    return strikes
