"""Ambient Weather PWS adapter — wind + lightning sensor data.

Operator provides their Ambient API Key and Application Key; both
are required and obtained from
https://ambientweather.net/account/keys

Lightning data is from the optional WH31L lightning detector (an
AS3935-based unit clipped onto the Ambient gateway).  When the
add-on is present, the gateway returns ``lightning_distance``
(in miles, regardless of UI units) and ``lightning_hour`` (strikes
in the last hour).

Algorithm and contracts ported from SDRLogger+
(`_fetch_ambient_weather`).  Lyra's version takes credentials as
function args rather than reading globals.
"""
# Lyra-SDR — Ambient Weather adapter
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


HTTP_TIMEOUT_SEC = 10.0
URL_DEVICES = "https://rt.ambientweather.net/v1/devices"


def _http_get_json(url: str) -> Optional[object]:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            if resp.status == 429:
                logger.warning("Ambient: rate limited (HTTP 429)")
                return None
            if resp.status != 200:
                logger.debug("Ambient HTTP %d", resp.status)
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("Ambient request error: %s", exc)
        return None


def fetch_pws_data(api_key: str, app_key: str) -> Optional[dict]:
    """Fetch the most recent ``lastData`` blob from the operator's
    primary Ambient device.

    Returns a flat dict of sensor readings or None on error.
    Operator's Ambient account may have multiple devices; we use the
    first one in the response (Ambient's API returns devices in a
    stable order — primary station first).
    """
    if not api_key or not app_key:
        return None
    url = (f"{URL_DEVICES}"
           f"?apiKey={urllib.parse.quote(api_key)}"
           f"&applicationKey={urllib.parse.quote(app_key)}")
    devices = _http_get_json(url)
    if not isinstance(devices, list) or not devices:
        return None
    last = devices[0].get("lastData") or {}
    return last if isinstance(last, dict) else None


def fetch_lightning(api_key: str, app_key: str
                    ) -> tuple[Optional[float], int]:
    """Return ``(distance_km, strikes_per_hour)`` from the Ambient
    lightning sensor, or ``(None, 0)`` when no lightning add-on is
    installed / configured / detecting.

    Ambient reports distance in miles natively; we convert to km so
    the rest of Lyra works in a single unit internally and converts
    for display.
    """
    last = fetch_pws_data(api_key, app_key)
    if not last:
        return None, 0
    dist_mi = last.get("lightning_distance")
    hour_count = last.get("lightning_hour", 0)
    if dist_mi is not None and hour_count and hour_count > 0:
        try:
            dist_km = float(dist_mi) * 1.60934
            return dist_km, int(hour_count)
        except (TypeError, ValueError):
            return None, 0
    return None, 0


def fetch_wind(api_key: str, app_key: str
               ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(sustained_mph, gust_mph, dir_deg)`` from the Ambient
    anemometer, or all-None on failure / missing fields.

    Ambient reports wind in mph natively (matches our internal
    representation).
    """
    last = fetch_pws_data(api_key, app_key)
    if not last:
        return None, None, None
    sustained = last.get("windspeedmph")
    gust = last.get("windgustmph")
    direction = last.get("winddir")

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return _num(sustained), _num(gust), _num(direction)
