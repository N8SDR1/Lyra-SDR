"""Lyra panadapter watermark.

Renders a Lyra/lyre constellation image as a faint watermark behind
the spectrum trace, plus a slow pulsing Vega highlight on top of
the chosen star. Operator-toggleable in Settings → Visuals.

Asset: lyra/assets/watermarks/lyra-watermark.jpg — a stylized lyre
silhouette built from constellation stars + connecting lines on a
dark starfield. Loaded once and cached at the current widget size.

Visual treatment:
  - Centered horizontally on the panadapter
  - Vertically scaled to fit ~92% of widget height (preserves aspect)
  - Painted with low overall opacity so the trace dominates
  - Composed with CompositionMode_Plus (additive) so the dark
    background pixels of the source image disappear into the black
    panadapter background — only the bright stars / lines / lyre
    edges actually show through. This avoids the otherwise-visible
    "tinted rectangle" effect of a low-alpha dark-blue overlay.
  - Vega star (one of the bright distinct points in the source
    image) gets a slow sinusoidal pulse glow rendered on top as a
    multi-layer additive blob. Driven by time.monotonic() so it
    animates with the spectrum repaint cadence.

Drawn as the FIRST overlay (under the trace fill, passband, marker,
etc.) so the spectrum line dominates visually.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap


# Source image path, relative to this module's location.
# lyra/ui/constellation.py → ../assets/watermarks/...
_ASSET_PATH = (
    Path(__file__).parent.parent / "assets" / "watermarks" / "lyra-watermark.jpg"
)

# Watermark intensity (0.0 .. 1.0). Set deliberately low so the
# spectrum trace stays the primary visual element. Tunable from
# experience — bump up if too faint, down if too dominant.
WATERMARK_OPACITY = 0.25

# Vertical fraction of the panadapter the image occupies. 0.92
# leaves a small margin top/bottom.
WATERMARK_HEIGHT_FRAC = 0.92

# Horizontal stretch applied AFTER height scaling. 1.0 = preserve
# the source image's natural aspect ratio (square 720x720). >1.0
# stretches the image wider than tall, which suits a wide panadapter
# better — the lyre silhouette doesn't read as "tall thin lyre" but
# as a more spread-out constellation watermark. Set conservatively;
# heavy stretching distorts the image noticeably.
WATERMARK_WIDTH_STRETCH = 1.30

# Vega pulse — overlaid on top of the watermark image at the position
# of one of the brightest visible stars. Position is normalized within
# the source image (0..1, 0..1) so it tracks under widget resize.
# Tweakable: nudge x left/right or y up/down to land on a different
# star in the image if the operator prefers a different focal point.
VEGA_NX                = 0.45    # ~ above the lyre's left frame
VEGA_NY                = 0.20
VEGA_PULSE_PERIOD_S    = 3.5
VEGA_PULSE_MIN         = 0.40    # alpha multiplier at the dim end
VEGA_PULSE_MAX         = 1.00    # alpha multiplier at the bright end
VEGA_CORE_RADIUS_PX    = 4.0
VEGA_CORE_COLOR        = QColor(230, 240, 255)   # cool blue-white
# Multi-layer halo for a soft glow look. Each layer is (radius_mult,
# alpha_mult) — outer layers are bigger and dimmer.
VEGA_GLOW_LAYERS = (
    (1.0, 1.00),
    (2.2, 0.35),
    (4.0, 0.12),
)

# Module-level cache. The source pixmap loads once on first draw;
# the scaled cache rebuilds on widget-size change so we're not
# re-scaling a 720x720 JPEG every frame.
_source_pixmap: Optional[QPixmap] = None
_source_load_attempted = False
_cached_scaled: Optional[QPixmap] = None
_cached_size: tuple[int, int] = (0, 0)


def _vega_pulse_factor() -> float:
    """0..1 multiplier for Vega's alpha, animated over time.

    Sinusoid scaled to [VEGA_PULSE_MIN, VEGA_PULSE_MAX]. Driven by
    time.monotonic() so the phase is continuous across paint events
    without needing a separate QTimer — every panadapter repaint
    naturally advances the pulse."""
    phase = (time.monotonic() % VEGA_PULSE_PERIOD_S) / VEGA_PULSE_PERIOD_S
    s = 0.5 * (1.0 - math.cos(2.0 * math.pi * phase))   # 0..1
    return VEGA_PULSE_MIN + s * (VEGA_PULSE_MAX - VEGA_PULSE_MIN)


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
        # Scale to the target height first (preserves aspect from the
        # square source), then optionally stretch horizontally so the
        # watermark fills more of a wide panadapter. SmoothTransformation
        # = bilinear; cheap on a 720 px source and only runs on resize.
        intermediate = src.scaledToHeight(target_h, Qt.SmoothTransformation)
        if WATERMARK_WIDTH_STRETCH != 1.0:
            target_w = max(1, int(intermediate.width() * WATERMARK_WIDTH_STRETCH))
            _cached_scaled = intermediate.scaled(
                target_w, target_h,
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )
        else:
            _cached_scaled = intermediate
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

    # Vega pulse — multi-layer additive glow on top of the watermark.
    # Drawn in its own paint state so the pulse can use higher alpha
    # than the base watermark (drawing a bright star, not a dim
    # background image).
    pulse = _vega_pulse_factor()
    vega_cx = x + int(VEGA_NX * pix.width())
    vega_cy = y + int(VEGA_NY * pix.height())
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setCompositionMode(QPainter.CompositionMode_Plus)
    painter.setPen(Qt.NoPen)
    for radius_mult, alpha_mult in VEGA_GLOW_LAYERS:
        alpha = int(255 * pulse * alpha_mult)
        if alpha < 2:
            continue
        radius = VEGA_CORE_RADIUS_PX * radius_mult
        col = QColor(VEGA_CORE_COLOR)
        col.setAlpha(min(255, alpha))
        painter.setBrush(col)
        painter.drawEllipse(
            int(vega_cx - radius), int(vega_cy - radius),
            int(radius * 2), int(radius * 2),
        )
    painter.restore()
