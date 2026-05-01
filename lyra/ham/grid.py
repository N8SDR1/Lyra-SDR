"""Maidenhead Grid Locator <-> latitude/longitude conversions.

The Maidenhead system encodes a position on Earth in 4, 6, or 8
character pairs:

  - First pair:  field   — 18×18 cells of 20°×10° (A-R / A-R)
  - Second pair: square  — 10×10 cells of 2°×1°  (0-9 / 0-9)
  - Third pair:  subsq.  — 24×24 cells of 5'×2.5' (a-x / a-x)
  - Fourth pair: ext.    — 10×10 cells of 30"×15" (0-9 / 0-9)

Common ham use is 4-character (e.g. ``EM89``) or 6-character
(e.g. ``EM89ux``) precision.  Lyra accepts both and returns the
center point of whichever cell-size was specified.

This is a self-contained pure-Python module — no SciPy, no NumPy.
Used by WX-Alerts (operator location for source queries) and any
future feature that needs the operator's latitude / longitude.
"""
from __future__ import annotations

import re
from typing import Optional

# Strict format check — 4, 6, or 8 chars exactly.
# Field pair is letters A-R; square pair is digits 0-9; subsquare
# pair is letters A-X (case-insensitive); extended subsquare is
# digits 0-9.
_GRID_RE = re.compile(
    r"^[A-R]{2}[0-9]{2}([A-X]{2}([0-9]{2})?)?$",
    re.IGNORECASE,
)


def is_valid_grid(grid: str) -> bool:
    """True if ``grid`` is a syntactically valid Maidenhead locator
    of length 4, 6, or 8."""
    if not grid:
        return False
    return bool(_GRID_RE.match(grid.strip()))


def grid_to_latlon(grid: str) -> Optional[tuple[float, float]]:
    """Convert a Maidenhead locator to (lat, lon) of the cell's
    center point.  Returns None for invalid input.

    Cell-size convention: the returned point is the geometric
    center of the smallest specified cell.  4-char gives field+
    square center (~half-cell of 1°×0.5° resolution), 6-char gives
    subsquare center (~2.5'×1.25' = ~3 km accuracy at mid-
    latitudes), 8-char is field-survey accuracy.
    """
    if not is_valid_grid(grid):
        return None
    g = grid.strip().upper()

    # Field pair — 18×18 cells of 20° lon × 10° lat (lon goes from
    # 180°W to 180°E, lat from 90°S to 90°N).
    lon = (ord(g[0]) - ord("A")) * 20.0 - 180.0
    lat = (ord(g[1]) - ord("A")) * 10.0 - 90.0

    # Square pair — 10×10 of 2° lon × 1° lat.
    lon += int(g[2]) * 2.0
    lat += int(g[3]) * 1.0

    # If 4-char only, the cell center is at +1° lon, +0.5° lat from
    # the SW corner.
    if len(g) == 4:
        lon += 1.0
        lat += 0.5
        return (lat, lon)

    # Subsquare pair — 24×24 of 5' lon × 2.5' lat = 5/60° lon,
    # 2.5/60° lat = 1/12° lon, 1/24° lat.
    lon += (ord(g[4]) - ord("A")) * (5.0 / 60.0)
    lat += (ord(g[5]) - ord("A")) * (2.5 / 60.0)

    if len(g) == 6:
        # 6-char cell center: SW corner + half a subsquare cell.
        lon += (5.0 / 60.0) / 2.0
        lat += (2.5 / 60.0) / 2.0
        return (lat, lon)

    # Extended subsquare pair (8-char) — 10×10 of 30" lon × 15" lat.
    lon += int(g[6]) * (30.0 / 3600.0)
    lat += int(g[7]) * (15.0 / 3600.0)
    # 8-char cell center: SW corner + half an extended cell.
    lon += (30.0 / 3600.0) / 2.0
    lat += (15.0 / 3600.0) / 2.0
    return (lat, lon)


def latlon_to_grid(lat: float, lon: float, precision: int = 6) -> str:
    """Convert (lat, lon) to a Maidenhead grid of the requested
    character length.

    Args:
        lat: latitude in degrees (-90..+90)
        lon: longitude in degrees (-180..+180)
        precision: 4, 6, or 8.  Defaults to 6 (typical ham radio).

    Returns the grid string (uppercase letters in pairs 1 and 3,
    digits in pairs 2 and 4).  Out-of-range inputs are clamped to
    valid Earth coordinates.
    """
    if precision not in (4, 6, 8):
        precision = 6
    # Clamp to valid Earth ranges.
    lat = max(-90.0, min(90.0, float(lat)))
    lon = max(-180.0, min(180.0, float(lon)))

    # Shift to [0, 360°) lon and [0, 180°) lat for cell math.
    lon_a = lon + 180.0
    lat_a = lat + 90.0

    # Field pair — A..R for 20° lon / 10° lat cells.
    field_lon = int(lon_a // 20.0)
    field_lat = int(lat_a // 10.0)
    field_lon = min(field_lon, 17)  # clamp at R for lon=180.0 edge
    field_lat = min(field_lat, 17)
    out = chr(ord("A") + field_lon) + chr(ord("A") + field_lat)

    # Reduce to within-field offset.
    lon_a -= field_lon * 20.0
    lat_a -= field_lat * 10.0

    # Square pair — 2° lon / 1° lat cells.
    sq_lon = int(lon_a // 2.0)
    sq_lat = int(lat_a // 1.0)
    sq_lon = min(sq_lon, 9)
    sq_lat = min(sq_lat, 9)
    out += str(sq_lon) + str(sq_lat)

    if precision == 4:
        return out

    lon_a -= sq_lon * 2.0
    lat_a -= sq_lat * 1.0

    # Subsquare pair — 5' lon / 2.5' lat cells.
    sub_lon = int(lon_a // (5.0 / 60.0))
    sub_lat = int(lat_a // (2.5 / 60.0))
    sub_lon = min(sub_lon, 23)
    sub_lat = min(sub_lat, 23)
    out += chr(ord("A") + sub_lon) + chr(ord("A") + sub_lat)

    if precision == 6:
        return out

    lon_a -= sub_lon * (5.0 / 60.0)
    lat_a -= sub_lat * (2.5 / 60.0)

    # Extended subsquare pair — 30" lon / 15" lat cells.
    ext_lon = int(lon_a // (30.0 / 3600.0))
    ext_lat = int(lat_a // (15.0 / 3600.0))
    ext_lon = min(ext_lon, 9)
    ext_lat = min(ext_lat, 9)
    out += str(ext_lon) + str(ext_lat)

    return out


def normalize_grid(grid: str) -> str:
    """Return the grid in canonical case (letters uppercase for
    pairs 1+3, lowercase for none — Maidenhead is conventionally
    written with the field uppercase and subsquare lowercase, but
    Lyra stores everything uppercase for simplicity).

    Returns empty string for invalid input.
    """
    if not is_valid_grid(grid):
        return ""
    return grid.strip().upper()
