"""Lyra-SDR weather alerts — lightning detection + high-wind notifications.

Watches the operator's local conditions via configurable data sources
(Blitzortung, NWS, Ambient WS-2000, Ecowitt PWS) and surfaces threat
information in the toolbar header plus optional desktop toasts.

This package is data-only — UI lives in ``lyra/ui/wx_indicator.py``
and ``lyra/ui/settings_dialog.py``.

Algorithmic content for the source adapters was pulled directly from
two of the author's prior projects (WX-Dashboard and SDRLogger+).
Both are GPL-licensed Lyra-author projects; see the per-file
attribution comments.

Architecture
============

  WxWorker (QThread)
      ├──> sources/blitzortung.py    fetch_strikes(lat, lon, range_km)
      ├──> sources/nws.py            fetch_storm_warnings, fetch_wind_alerts, fetch_metar
      ├──> sources/ambient.py        fetch_pws_data(api_key, app_key)
      └──> sources/ecowitt.py        fetch_pws_data(app_key, api_key, mac)
              │
              ▼
  Aggregator       — combines per-source readings, classifies tiers
              │
              ▼
  Radio signals    — wx_lightning_changed, wx_wind_changed, wx_severe_alert_changed
              │
              ▼
  WxIndicator      — header widget showing colored ⚡ / 💨 / ⚠ icons
  ToastDispatcher  — fires Windows toasts on tier-crossing events with
                      15-min hysteresis per condition
"""
