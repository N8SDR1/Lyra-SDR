"""Waterfall color palettes.

Each palette is a 256 × 3 uint8 numpy array mapping normalized signal
strength (0.0 = min_db, 1.0 = max_db) to RGB. `WaterfallWidget` looks
them up by name.

Adding a new palette is 5 lines: define the stops (normalized position
+ RGB triplet), wrap in `_build()`, add to `PALETTES` dict, and it
appears in the Visuals-tab combo box automatically.
"""
from __future__ import annotations

import numpy as np


def _build(stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    """Linear-interpolate a 256-entry RGB lookup table from the stops.
    Stops are (normalized_position_0_to_1, (r, g, b))."""
    xs = np.array([s[0] for s in stops])
    rs = np.array([s[1][0] for s in stops])
    gs = np.array([s[1][1] for s in stops])
    bs = np.array([s[1][2] for s in stops])
    t = np.linspace(0, 1, 256)
    return np.stack([np.interp(t, xs, rs),
                     np.interp(t, xs, gs),
                     np.interp(t, xs, bs)], axis=1).astype(np.uint8)


# ── Palettes ────────────────────────────────────────────────────────
#
# Order matters — keys are iterated in definition order for the UI
# combo box. "Classic" is the factory default (the previous "Default"
# key was a near-duplicate; legacy saved prefs migrate to "Classic"
# via the alias table below.)

PALETTES: dict[str, np.ndarray] = {}

# Classic — icy-blue → cyan → yellow → red, the SDR-classic heatmap
# look that operators expect out of the box. Factory default.
PALETTES["Classic"] = _build([
    (0.00, (  4,   8,  16)),
    (0.15, ( 10,  24,  60)),
    (0.35, ( 20,  80, 180)),
    (0.55, ( 30, 180, 220)),
    (0.75, (230, 220, 120)),
    (0.90, (240, 100,  40)),
    (1.00, (255,  80,  80)),
])

# Inferno — dark purple → orange → yellow. Very high perceptual
# contrast, matplotlib's go-to for scientific heatmaps.
PALETTES["Inferno"] = _build([
    (0.00, (  0,   0,   4)),
    (0.20, ( 40,  11,  84)),
    (0.40, (101,  21, 110)),
    (0.60, (159,  42,  99)),
    (0.75, (212,  72,  66)),
    (0.87, (245, 125,  21)),
    (0.95, (250, 193,  39)),
    (1.00, (252, 255, 164)),
])

# Viridis — deep-purple → teal → yellow-green. Color-blind friendly,
# perceptually uniform; increasingly popular in modern SDR apps.
PALETTES["Viridis"] = _build([
    (0.00, ( 68,   1,  84)),
    (0.25, ( 59,  82, 139)),
    (0.50, ( 33, 145, 140)),
    (0.75, ( 94, 201,  98)),
    (1.00, (253, 231,  37)),
])

# Plasma — deep blue → magenta → orange → yellow. High contrast
# without the dark purple of Inferno — weak signals pop earlier
# because the 0..0.3 normalized range is already pinks and blues
# rather than near-black. matplotlib-origin perceptually uniform.
PALETTES["Plasma"] = _build([
    (0.00, ( 13,   8, 135)),
    (0.20, ( 84,   2, 163)),
    (0.40, (156,  23, 158)),
    (0.60, (218,  57, 105)),
    (0.80, (249, 149,  63)),
    (1.00, (240, 249,  33)),
])

# Rainbow — full saturated spectrum sweep. Old-school SDR look
# (BladeRF, early HDSDR). Easy to read carrier peaks but
# perceptually non-uniform; useful when you want the dynamic range
# to feel "loud."
PALETTES["Rainbow"] = _build([
    (0.00, (  0,   0,   0)),
    (0.15, (  0,   0, 128)),
    (0.30, (  0, 128, 255)),
    (0.45, (  0, 255, 128)),
    (0.60, (255, 255,   0)),
    (0.80, (255, 128,   0)),
    (1.00, (255,   0,   0)),
])

# Ocean — blacks → deep navy → teal → cyan → white. Calm cool palette,
# easy on the eyes during long listening sessions.
PALETTES["Ocean"] = _build([
    (0.00, (  0,   0,   0)),
    (0.25, (  8,  25,  64)),
    (0.50, ( 18,  95, 130)),
    (0.75, ( 80, 200, 220)),
    (1.00, (230, 245, 255)),
])

# Night — dim red-tinted palette for low-light / night-vision
# friendly operation. Preserves dark-adapted eyes on late-night DX.
PALETTES["Night"] = _build([
    (0.00, (  0,   0,   0)),
    (0.30, ( 40,   0,   0)),
    (0.60, (130,  10,  10)),
    (0.85, (220,  80,  30)),
    (1.00, (255, 180,  60)),
])

# Grayscale — monochrome. Useful for screenshots / printing and for
# comparing signals purely by magnitude without color bias.
PALETTES["Grayscale"] = _build([
    (0.00, (  0,   0,   0)),
    (1.00, (255, 255, 255)),
])


DEFAULT_PALETTE = "Classic"

# Legacy palette-key aliases. Any saved user preference that predates
# a rename gets silently mapped to the new name here so we don't blow
# up existing QSettings. Keep this as a simple migration table rather
# than handle it in app.py.
_ALIASES: dict[str, str] = {
    "thetis":   "Classic",      # historical default
    "default":  "Classic",      # renamed 2026-04-24 (was near-duplicate)
    "classic":  "Classic",      # lowercase convenience
    # Former "Classic" was the rainbow — reroute any saved "classic"
    # that actually MEANT the rainbow to the new name:
    # (We can't tell from the string alone, so the above maps to the
    # new default. If a user had the old rainbow saved, they'll need
    # to re-pick "Rainbow". Alternative was making "Rainbow" the
    # migration target, but defaulting to the softer palette is the
    # safer choice for 95% of operators.)
}


def get(name: str) -> np.ndarray:
    """Look up a palette by name (case-insensitive). Falls back to the
    default palette if the name isn't known — lets us rename palettes
    in future without breaking old persisted settings."""
    if not name:
        return PALETTES[DEFAULT_PALETTE]
    aliased = _ALIASES.get(name.lower())
    if aliased:
        return PALETTES[aliased]
    for key, pal in PALETTES.items():
        if key.lower() == name.lower():
            return pal
    return PALETTES[DEFAULT_PALETTE]


def canonical_name(name: str) -> str:
    """Like `get()` but returns the canonical palette name string,
    applying rename-aliases. Used when persisting so future launches
    store the new name not the old one."""
    if not name:
        return DEFAULT_PALETTE
    aliased = _ALIASES.get(name.lower())
    if aliased:
        return aliased
    for key in PALETTES.keys():
        if key.lower() == name.lower():
            return key
    return DEFAULT_PALETTE


def names() -> list[str]:
    """List available palette names in UI-display order."""
    return list(PALETTES.keys())
