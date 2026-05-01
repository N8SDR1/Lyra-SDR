"""Ecowitt PWS adapter — wind + lightning via the v3 Cloud API.

Operator provides three credentials from their ecowitt.net account
(API Setting page):
    - Application Key
    - API Key
    - Gateway MAC address (e.g. 34:94:54:AB:CD:EF)

The free tier is rate-limited to 1 request / minute / MAC, so this
module includes a 30-second cache shared across the lightning + wind
helpers — both pull from the same realtime endpoint.

Algorithm and contracts ported from SDRLogger+
(`_fetch_ecowitt_lastdata`, `_fetch_ecowitt_lightning`,
`_fetch_ecowitt_wind`).  Lyra's version takes credentials as args
and uses urllib instead of requests.
"""
# Lyra-SDR — Ecowitt adapter
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


HTTP_TIMEOUT_SEC = 12.0
URL_REALTIME = "https://api.ecowitt.net/api/v3/device/real_time"
CACHE_TTL_SEC = 30.0   # respect their 1-call/min/MAC free-tier limit

# Module-level cache shared across all callers (lightning + wind +
# any future ecowitt consumer).  Keyed by MAC so multiple stations
# don't collide in a single Lyra instance.
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _http_get_json(url: str, params: dict) -> Optional[dict]:
    """Fetch + parse JSON from Ecowitt v3 endpoint."""
    try:
        full_url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                logger.debug("Ecowitt HTTP %d", resp.status)
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("Ecowitt request error: %s", exc)
        return None


def _flatten(data: dict) -> dict:
    """Convert Ecowitt v3's nested {time, unit, value} structure into
    a flat (Ambient-shaped) dict so downstream consumers don't have
    to know the v3 schema details.

    Returns a dict with keys matching Ambient's flat schema:
        windspeedmph, windgustmph, winddir,
        lightning_distance_km, lightning_hour, lightning_day
    Missing sensors yield None / 0 in their respective slots.
    """
    def _leaf(d: dict, *path: str) -> Optional[object]:
        cur: object = d
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
            if cur is None:
                return None
        if isinstance(cur, dict):
            cur = cur.get("value")
        return cur

    def _num(v: object) -> Optional[float]:
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    flat = {
        "windspeedmph": _num(_leaf(data, "wind", "wind_speed")),
        "windgustmph":  _num(_leaf(data, "wind", "wind_gust")),
        "winddir":      _num(_leaf(data, "wind", "wind_direction")),
        # Ecowitt v3 reports lightning distance in km on most firmware
        # — we expose both km and a derived mile equivalent so consumers
        # can pick whichever they want.
        "lightning_distance_km":
            _num(_leaf(data, "lightning", "distance")),
        "lightning_hour":
            _num(_leaf(data, "lightning", "count_hour")),
        "lightning_day":
            _num(_leaf(data, "lightning", "count")),
    }
    if flat["lightning_distance_km"] is not None:
        flat["lightning_distance_mi"] = (
            flat["lightning_distance_km"] / 1.60934)
    return flat


def fetch_pws_data(app_key: str, api_key: str, mac: str
                   ) -> Optional[dict]:
    """Fetch the gateway's realtime sensor blob (cached 30s).

    Returns the flattened dict described in ``_flatten`` or None on
    failure.  Gateway is identified by ``mac`` so each station has
    its own cache entry.

    Caller must provide all three credentials (App Key, API Key,
    MAC).  Missing credentials return None silently.
    """
    if not (app_key and api_key and mac):
        return None
    mac_norm = mac.strip().upper()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(mac_norm)
        if cached and (now - cached[0]) < CACHE_TTL_SEC:
            return cached[1]
    params = {
        "application_key": app_key,
        "api_key":         api_key,
        "mac":             mac_norm,
        "call_back":       "all",
        "temp_unitid":     "2",   # °F
        "pressure_unitid": "4",   # inHg
        "wind_unitid":     "9",   # mph (matches Ambient native)
        "rainfall_unitid": "13",  # in
    }
    j = _http_get_json(URL_REALTIME, params)
    if not isinstance(j, dict):
        return None
    if str(j.get("code", "")) not in ("0",):
        logger.debug("Ecowitt API code=%s msg=%s",
                     j.get("code"), j.get("msg"))
        return None
    data = j.get("data") or {}
    flat = _flatten(data if isinstance(data, dict) else {})
    with _cache_lock:
        _cache[mac_norm] = (now, flat)
    return flat


def fetch_lightning(app_key: str, api_key: str, mac: str
                    ) -> tuple[Optional[float], int]:
    """Return ``(distance_km, strikes_per_hour)`` from the Ecowitt
    lightning detector, or ``(None, 0)`` when no lightning sensor /
    no recent strikes.
    """
    flat = fetch_pws_data(app_key, api_key, mac)
    if not flat:
        return None, 0
    dist_km = flat.get("lightning_distance_km")
    hour = flat.get("lightning_hour") or 0
    if dist_km is not None and hour and hour > 0:
        try:
            return float(dist_km), int(hour)
        except (TypeError, ValueError):
            return None, 0
    return None, 0


def fetch_wind(app_key: str, api_key: str, mac: str
               ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(sustained_mph, gust_mph, dir_deg)`` from the Ecowitt
    anemometer, or all-None on failure."""
    flat = fetch_pws_data(app_key, api_key, mac)
    if not flat:
        return None, None, None
    return (flat.get("windspeedmph"),
            flat.get("windgustmph"),
            flat.get("winddir"))
