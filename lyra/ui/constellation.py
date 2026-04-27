"""Lyra panadapter watermark.

Renders a Lyra/lyre constellation image as a faint watermark behind
the spectrum trace. Operator-toggleable in Settings → Visuals.

Asset: lyra/assets/watermarks/lyra-watermark.jpg — a stylized lyre
silhouette built from constellation stars + connecting lines on a
dark starfield. Loaded once and cached at the current widget size.

Visual treatment:
  - Centered horizontally on the panadapter
  - Vertically scaled to fit ~85% of widget height (preserves aspect)
  - Painted with low overall opacity so the trace dominates
  - Composed with CompositionMode_Plus (additive) so the dark
    background pixels of the source image disappear into the black
    panadapter background — only the bright stars / lines / lyre
    edges actually show through. This avoids the otherwise-visible
    "tinted rectangle" effect of a low-alpha dark-blue overlay.

Drawn as the FIRST overlay (under the trace fill, passband, marker,
etc.) so the spectrum line dominates visually.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap


# Source image path, relative to this module's location.
# lyra/ui/constellation.py → ../assets/watermarks/...
_ASSET_PATH = (
    Path(__file__).parent.parent / "assets" / "watermarks" / "lyra-watermark.jpg"
)

# Watermark intensity (0.0 .. 1.0). Set deliberately low so the
# spectrum trace stays the primary visual element. Tunable from
# experience — bump up if too faint, down if too dominant.
WATERMARK_OPACITY = 0.35

# Vertical fraction of the panadapter the image occupies (preserved
# aspect, so width follows). 0.85 leaves a small margin top/bottom.
WATERMARK_HEIGHT_FRAC = 0.92

# Module-level cache. The source pixmap loads once on first draw;
# the scaled cache rebuilds on widget-size change so we're not
# re-scaling a 720x720 JPEG every frame.
_source_pixmap: Optional[QPixmap] = None
_source_load_attempted = False
_cached_scaled: Optional[QPixmap] = None
_cached_size: tuple[int, int] = (0, 0)


def _load_source() -> Optional[QPixmap]:
    """Load the source image once. Returns None if the asset is
    missing or fails to decode (e.g. installed without assets) so
    the caller can no-op gracefully instead of crashing the paint
    thread."""
    global _source_pixmap, _source_load_attempted
    if _source_load_attempted:
        return _source_pixmap
    _source_load_attempted = True
    if not _ASSET_PATH.exists():
        return None
    pix = QPixmap(str(_ASSET_PATH))
    if pix.isNull():
        return None
    _source_pixmap = pix
    return _source_pixmap


def draw(painter: QPainter, w: int, h: int) -> None:
    """Render the Lyra watermark scaled to fit the panadapter area.

    Both spectrum_gpu.SpectrumGpuWidget and spectrum.SpectrumWidget
    call this from their _draw_overlays entry point as the first
    overlay so the trace and other markers sit on top."""
    if w <= 0 or h <= 0:
        return
    src = _load_source()
    if src is None:
        return

    global _cached_scaled, _cached_size
    if _cached_scaled is None or _cached_size != (w, h):
        target_h = max(1, int(h * WATERMARK_HEIGHT_FRAC))
        # SmoothTransformation = bilinear; cheap on a 720px source
        # and only runs on widget resize.
        _cached_scaled = src.scaledToHeight(target_h, Qt.SmoothTransformation)
        _cached_size = (w, h)

    pix = _cached_scaled
    x = (w - pix.width()) // 2
    y = (h - pix.height()) // 2

    painter.save()
    # Additive blending: dark source pixels (the image's navy
    # background) contribute nothing on top of the black panadapter,
    # so we only see the bright lyre/star content. Low opacity keeps
    # the highlights from blowing out the trace.
    painter.setCompositionMode(QPainter.CompositionMode_Plus)
    painter.setOpacity(WATERMARK_OPACITY)
    painter.drawPixmap(x, y, pix)
    painter.restore()
