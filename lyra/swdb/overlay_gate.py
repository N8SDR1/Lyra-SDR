"""Auto-detect whether the EiBi overlay should render right now.

Per design doc §5.6.1: the overlay auto-activates based on what
the system already knows -- the operator's region and the current
VFO frequency vs that region's amateur band-plan.

Inside ham-band frequency ranges      -> NO overlay (correct;
                                          EiBi has no amateur
                                          activity).
Outside (SW broadcast / utility / GEN) -> overlay renders
                                          (if master enabled
                                          and database loaded).

Operator never has to think about per-band toggles -- the system
reads what's already configured in Settings -> Operator -> Region.
"""
from __future__ import annotations

from typing import Optional


def overlay_should_render(
    freq_hz: int,
    region: str,
    master_enabled: bool,
    force_all_bands: bool = False,
) -> bool:
    """Return True when the EiBi overlay should be drawn for the
    current tuning state.

    Args:
        freq_hz: current VFO frequency in Hz.
        region: operator's region setting -- 'US', 'R1', 'R3', or
                'NONE'.  Read from Settings -> Operator.  The
                'NONE' case means the operator opted out of
                band-plan classification; in that case the
                overlay shows everywhere when master is enabled.
        master_enabled: top-level Settings checkbox.  When False,
                this returns False unconditionally.
        force_all_bands: optional override.  When True, bypass the
                band-plan check and render even on amateur
                frequencies.  Rare (used by operators trying to
                identify broadcast QRM bleeding into ham bands).

    Logic:
        master False           -> False (overlay disabled)
        force_all_bands True   -> True (operator explicitly opted in)
        region == 'NONE'       -> True (no band classification)
        in amateur band        -> False (overlay would clutter)
        otherwise              -> True
    """
    if not master_enabled:
        return False
    if force_all_bands:
        return True
    region_norm = (region or "").upper()
    if region_norm == "NONE":
        return True
    return not _is_in_amateur_band(int(freq_hz), region_norm)


def _is_in_amateur_band(freq_hz: int, region: str) -> bool:
    """Check whether ``freq_hz`` falls within any amateur band
    segment for the given region.

    Reuses Lyra's existing ``lyra/band_plan.find_band`` so the
    overlay stays in sync with the band-plan tables that drive
    the spectrum strip + landmark triangles.  ``find_band``
    returns a Band dict whose ``low``/``high`` fields define the
    full amateur allocation; if the freq is inside any of them,
    we treat it as "ham band" and suppress the overlay.

    Returns False on any error (band-plan module unavailable,
    region unknown) so the overlay errs on the side of
    rendering rather than hiding -- safer default for an SWL
    feature.
    """
    try:
        from lyra.band_plan import find_band
    except ImportError:
        # Lyra's band-plan module not present; treat as "no
        # amateur bands defined" so the overlay renders
        # everywhere when master is enabled.
        return False
    try:
        band = find_band(region, int(freq_hz))
    except (ValueError, KeyError, AttributeError, TypeError):
        return False
    return band is not None
