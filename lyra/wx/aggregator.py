"""Combines per-source weather readings into operator-facing tier
classifications.

Collects strikes / wind / severe alerts from any enabled source,
picks the closest / highest reading per category, and assigns a
visual tier:

    Lightning:  none / far / mid / close
    Wind:       none / elevated / high / extreme
    Severe:     none / active

The aggregator is plain-Python — no Qt — so it can be unit-tested
in isolation.  The Qt-facing layer (``WxWorker``) calls
``aggregate()`` periodically and turns the result into Radio
signals.
"""
# Lyra-SDR — Weather aggregator
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Tier tag constants ───────────────────────────────────────────
LIGHTNING_NONE = "none"
LIGHTNING_FAR = "far"
LIGHTNING_MID = "mid"
LIGHTNING_CLOSE = "close"

WIND_NONE = "none"
WIND_ELEVATED = "elevated"
WIND_HIGH = "high"
WIND_EXTREME = "extreme"

SEVERE_NONE = "none"
SEVERE_ACTIVE = "active"


@dataclass
class WxConfig:
    """Operator-tunable thresholds and source toggles.

    Distance-related fields are stored in km internally regardless of
    the operator's display-unit preference; the UI converts on
    display.  Wind speeds are stored in mph internally for the same
    reason.
    """
    # Operator location (None when not set).
    my_lat: Optional[float] = None
    my_lon: Optional[float] = None

    # Lightning thresholds — strikes within range_km trigger far
    # tier; strikes within mid/close cutoffs trigger those tiers.
    lightning_range_km: float = 80.0  # ~50 mi default
    lightning_mid_km: float = 40.0    # ~25 mi
    lightning_close_km: float = 16.0  # ~10 mi

    # Wind thresholds (mph).  Tiers:
    #   elevated:  10 mph below sustained_threshold OR gust_threshold
    #   high:      at/above sustained or gust threshold
    #   extreme:   NWS warning OR sustained ≥ +15 OR gust ≥ +15
    wind_sustained_mph: float = 30.0
    wind_gust_mph: float = 40.0

    # Source enables (per data type).
    src_blitzortung: bool = False
    src_nws: bool = False        # storm warnings + wind alerts
    src_nws_metar: bool = False  # live wind from nearest METAR
    src_ambient: bool = False
    src_ecowitt: bool = False

    # Source credentials (empty when not configured).
    ambient_api_key: str = ""
    ambient_app_key: str = ""
    ecowitt_app_key: str = ""
    ecowitt_api_key: str = ""
    ecowitt_mac: str = ""
    nws_metar_station: str = ""


@dataclass
class LightningState:
    """Aggregated lightning state — what to show on the indicator."""
    tier: str = LIGHTNING_NONE
    closest_km: Optional[float] = None
    closest_bearing_deg: Optional[float] = None
    strikes_recent: int = 0   # count in last hour, summed across sources
    sources_with_data: list[str] = field(default_factory=list)


@dataclass
class WindState:
    tier: str = WIND_NONE
    sustained_mph: Optional[float] = None
    gust_mph: Optional[float] = None
    direction_deg: Optional[float] = None
    nws_alert_headline: str = ""    # populated when NWS alert active
    sources_with_data: list[str] = field(default_factory=list)


@dataclass
class SevereState:
    tier: str = SEVERE_NONE
    headline: str = ""


@dataclass
class WxSnapshot:
    """Single point-in-time aggregation.  WxWorker emits this each
    poll cycle; the indicator widget renders from it directly."""
    lightning: LightningState = field(default_factory=LightningState)
    wind: WindState = field(default_factory=WindState)
    severe: SevereState = field(default_factory=SevereState)
    error: str = ""   # populated when ALL sources failed this cycle


def _classify_lightning(closest_km: Optional[float],
                         cfg: WxConfig) -> str:
    """Map a closest-strike distance (km) to a tier tag."""
    if closest_km is None:
        return LIGHTNING_NONE
    if closest_km <= cfg.lightning_close_km:
        return LIGHTNING_CLOSE
    if closest_km <= cfg.lightning_mid_km:
        return LIGHTNING_MID
    if closest_km <= cfg.lightning_range_km:
        return LIGHTNING_FAR
    return LIGHTNING_NONE


def _classify_wind(sustained_mph: Optional[float],
                    gust_mph: Optional[float],
                    nws_extreme: bool, cfg: WxConfig) -> str:
    """Map wind data + alert status to a tier tag.

    Extreme tier wins on ANY of:
      - NWS High Wind Warning / Extreme Wind Warning active
      - sustained ≥ threshold + 15
      - gust ≥ gust_threshold + 15

    High tier on: sustained ≥ threshold OR gust ≥ threshold.
    Elevated on: sustained or gust within 10 of threshold.
    """
    if nws_extreme:
        return WIND_EXTREME
    s = sustained_mph if sustained_mph is not None else 0.0
    g = gust_mph if gust_mph is not None else 0.0
    if (s >= cfg.wind_sustained_mph + 15.0
            or g >= cfg.wind_gust_mph + 15.0):
        return WIND_EXTREME
    if s >= cfg.wind_sustained_mph or g >= cfg.wind_gust_mph:
        return WIND_HIGH
    if (s >= cfg.wind_sustained_mph - 10.0
            or g >= cfg.wind_gust_mph - 10.0):
        return WIND_ELEVATED
    return WIND_NONE


def aggregate(cfg: WxConfig) -> WxSnapshot:
    """Poll all enabled sources and return a unified snapshot.

    Network failures are non-fatal — sources that error out are
    simply skipped, and the snapshot reflects whatever did succeed.
    The ``error`` field is populated only when every enabled source
    failed (operator should see a soft warning in that case).
    """
    snap = WxSnapshot()
    if cfg.my_lat is None or cfg.my_lon is None:
        snap.error = "operator location not set"
        return snap

    # ── Lightning sources ───────────────────────────────────────────
    closest_km: Optional[float] = None
    closest_brg: Optional[float] = None
    strikes_total = 0
    src_seen: list[str] = []
    sources_attempted = 0
    sources_succeeded = 0

    if cfg.src_blitzortung:
        sources_attempted += 1
        from lyra.wx.sources import blitzortung as bz
        try:
            strikes = bz.fetch_strikes(
                cfg.my_lat, cfg.my_lon, cfg.lightning_range_km)
            sources_succeeded += 1
        except Exception:
            strikes = []
        if strikes:
            src_seen.append("blitzortung")
            strikes_total += len(strikes)
            for dist, brg in strikes:
                if closest_km is None or dist < closest_km:
                    closest_km = dist
                    closest_brg = brg

    if cfg.src_ambient:
        sources_attempted += 1
        from lyra.wx.sources import ambient as amb
        try:
            dist_km, hour = amb.fetch_lightning(
                cfg.ambient_api_key, cfg.ambient_app_key)
            sources_succeeded += 1
        except Exception:
            dist_km, hour = None, 0
        if dist_km is not None and hour > 0:
            src_seen.append("ambient")
            strikes_total += hour
            if closest_km is None or dist_km < closest_km:
                closest_km = dist_km
                # Ambient WH31L doesn't report bearing.

    if cfg.src_ecowitt:
        sources_attempted += 1
        from lyra.wx.sources import ecowitt as eco
        try:
            dist_km, hour = eco.fetch_lightning(
                cfg.ecowitt_app_key, cfg.ecowitt_api_key, cfg.ecowitt_mac)
            sources_succeeded += 1
        except Exception:
            dist_km, hour = None, 0
        if dist_km is not None and hour > 0:
            src_seen.append("ecowitt")
            strikes_total += hour
            if closest_km is None or dist_km < closest_km:
                closest_km = dist_km

    snap.lightning.closest_km = closest_km
    snap.lightning.closest_bearing_deg = closest_brg
    snap.lightning.strikes_recent = strikes_total
    snap.lightning.sources_with_data = src_seen
    snap.lightning.tier = _classify_lightning(closest_km, cfg)

    # ── Wind sources ───────────────────────────────────────────────
    sustained: Optional[float] = None
    gust: Optional[float] = None
    direction: Optional[float] = None
    nws_headline = ""
    nws_extreme = False
    wind_src: list[str] = []

    if cfg.src_nws:
        sources_attempted += 1
        from lyra.wx.sources import nws
        try:
            nws_headline, nws_extreme = nws.fetch_wind_alerts(
                cfg.my_lat, cfg.my_lon)
            sources_succeeded += 1
            if nws_headline:
                wind_src.append("nws")
        except Exception:
            pass

    if cfg.src_nws_metar and cfg.nws_metar_station:
        sources_attempted += 1
        from lyra.wx.sources import nws
        try:
            s, g, d = nws.fetch_metar(cfg.nws_metar_station)
            sources_succeeded += 1
            if s is not None or g is not None:
                wind_src.append(f"metar/{cfg.nws_metar_station.upper()}")
                if sustained is None or (s is not None and s > sustained):
                    sustained = s
                if gust is None or (g is not None
                                     and (gust is None or g > gust)):
                    gust = g
                if direction is None and d is not None:
                    direction = d
        except Exception:
            pass

    if cfg.src_ambient:
        from lyra.wx.sources import ambient as amb
        try:
            s, g, d = amb.fetch_wind(
                cfg.ambient_api_key, cfg.ambient_app_key)
            if s is not None or g is not None:
                wind_src.append("ambient")
                if sustained is None or (s is not None and s > sustained):
                    sustained = s
                if gust is None or (g is not None and g > gust):
                    gust = g
                if direction is None and d is not None:
                    direction = d
        except Exception:
            pass

    if cfg.src_ecowitt:
        from lyra.wx.sources import ecowitt as eco
        try:
            s, g, d = eco.fetch_wind(
                cfg.ecowitt_app_key, cfg.ecowitt_api_key, cfg.ecowitt_mac)
            if s is not None or g is not None:
                wind_src.append("ecowitt")
                if sustained is None or (s is not None and s > sustained):
                    sustained = s
                if gust is None or (g is not None and g > gust):
                    gust = g
                if direction is None and d is not None:
                    direction = d
        except Exception:
            pass

    snap.wind.sustained_mph = sustained
    snap.wind.gust_mph = gust
    snap.wind.direction_deg = direction
    snap.wind.nws_alert_headline = nws_headline
    snap.wind.sources_with_data = wind_src
    snap.wind.tier = _classify_wind(
        sustained, gust, nws_extreme, cfg)

    # ── Severe storm warnings (lightning-flavored NWS alerts) ──────
    if cfg.src_nws:
        sources_attempted += 1
        from lyra.wx.sources import nws
        try:
            warn = nws.fetch_storm_warnings(cfg.my_lat, cfg.my_lon)
            sources_succeeded += 1
            if warn:
                snap.severe.headline = warn
                snap.severe.tier = SEVERE_ACTIVE
        except Exception:
            pass

    if sources_attempted > 0 and sources_succeeded == 0:
        snap.error = "all weather sources unreachable"
    return snap
