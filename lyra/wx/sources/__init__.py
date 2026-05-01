"""Data-source adapters for Lyra's weather-alerts feature.

Each source module exposes plain-Python fetch functions — no Qt,
no global state.  The aggregator (``lyra.wx.aggregator``) calls
these from the WxWorker thread and combines the results.

Sources implemented:
    - blitzortung — public global lightning-strike network
    - nws         — National Weather Service active alerts + METAR
    - ambient     — Ambient Weather PWS (with optional WH31L lightning)
    - ecowitt     — Ecowitt PWS via v3 cloud API
"""
