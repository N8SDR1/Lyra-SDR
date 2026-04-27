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
import random
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
#
# NX = 0.50 puts Vega exactly on the panadapter's horizontal center,
# which is also where the VFO frequency marker line lives. The
# pulsing star then visually coincides with the tuned-frequency
# marker — a deliberate brand moment ("the radio is tuned to Vega"
# motif). Keep NX at 0.50 to preserve this alignment; nudge NY to
# move Vega up or down on the marker line.
VEGA_NX                = 0.50
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


# ── Meteors (separate toggle from the watermark) ───────────────────
#
# Occasional shooting-star streaks across the panadapter. Designed
# to be RARE so they feel like ambient sky weather — closer to "did
# I just see something?" than constant motion. Operator-tunable
# spawn gap of 15..50 seconds; max 1 visible at a time.

METEOR_MIN_GAP_S         = 15.0    # min seconds between meteors
METEOR_MAX_GAP_S         = 50.0    # max seconds between meteors
METEOR_MAX_CONCURRENT    = 1       # never more than this on screen
METEOR_DURATION_MIN_S    = 0.50    # full-traversal time
METEOR_DURATION_MAX_S    = 0.85
METEOR_HEAD_RADIUS_PX    = 2.5
METEOR_TRAIL_SAMPLES     = 12      # how many trail dots to render
METEOR_TRAIL_LENGTH_S    = 0.18    # seconds of "tail" visible behind the head
METEOR_FADE_IN_S         = 0.05
METEOR_FADE_OUT_S        = 0.15
METEOR_TRAIL_MAX_ALPHA   = 0.55    # trail max alpha relative to head
METEOR_FIREBALL_PROB     = 0.15    # chance of warm-gold meteor (vs cool blue)
METEOR_HEAD_BLUE_COLOR   = QColor(225, 235, 255)
METEOR_HEAD_GOLD_COLOR   = QColor(255, 215, 130)

# Pseudo-gravity in px/s². Applied as a downward acceleration during
# the meteor's flight, producing a visible parabolic arc instead of
# a straight diagonal line. 0 = straight line. Higher = more bow.
# Real-physics gravity (~9.8 m/s²) is invisible at this scale and
# duration; we exaggerate so the curve actually reads on screen.
METEOR_ARC_GRAVITY_PX_S2 = 600.0

# Module-level meteor state. Each meteor is a dict with spawn coords,
# velocity, color, duration, and spawn time. Position is recomputed
# from age each frame so we don't accumulate per-frame integration drift.
_meteors: list[dict] = []
_next_meteor_t: float = 0.0
_meteor_state_init: bool = False


def _spawn_meteor(w: int, h: int) -> dict:
    """Build a new meteor with random spawn position, trajectory, and color."""
    # Spawn from one of three upper edges so meteors enter visibly
    # rather than appearing mid-screen. Bias toward straight-from-top
    # (most common entry angle for visual meteors).
    side = random.choices(
        ["top", "top_left", "top_right"],
        weights=[0.6, 0.2, 0.2],
        k=1,
    )[0]
    if side == "top":
        sx = random.uniform(0.15 * w, 0.85 * w)
        sy = -10.0
    elif side == "top_left":
        sx = -10.0
        sy = random.uniform(0.0, 0.30 * h)
    else:  # top_right
        sx = float(w) + 10.0
        sy = random.uniform(0.0, 0.30 * h)

    # Aim across the widget toward the opposite-bottom side, with a
    # bit of jitter so two meteors don't all converge on the same spot.
    tx = (w - sx) + random.uniform(-0.15 * w, 0.15 * w)
    ty = float(h) + 20.0
    duration = random.uniform(METEOR_DURATION_MIN_S, METEOR_DURATION_MAX_S)

    # Solve for initial velocity that produces a parabolic arc landing
    # at (tx, ty) at age=duration under constant downward acceleration.
    # Position model: x(t) = sx + vx·t          (no lateral gravity)
    #                 y(t) = sy + vy·t + ½·g·t²
    # so vy = (ty − sy − ½·g·dur²) ÷ dur — the gravity term is
    # subtracted so the meteor still hits the target despite curving.
    g = METEOR_ARC_GRAVITY_PX_S2
    vx = (tx - sx) / duration
    vy = (ty - sy - 0.5 * g * duration * duration) / duration

    color = (METEOR_HEAD_GOLD_COLOR
             if random.random() < METEOR_FIREBALL_PROB
             else METEOR_HEAD_BLUE_COLOR)

    return {
        "spawn_t": time.monotonic(),
        "sx": sx, "sy": sy,
        "vx": vx, "vy": vy,
        "ay": g,                # constant downward accel for the arc
        "duration": duration,
        "color": color,
    }


def _draw_one_meteor(painter: QPainter, m: dict, now: float) -> bool:
    """Render one meteor at its current age. Returns False if it's expired."""
    age = now - m["spawn_t"]
    if age >= m["duration"]:
        return False

    # Lifecycle alpha — fade in at start, fade out at end so meteors
    # don't pop on/off abruptly.
    if age < METEOR_FADE_IN_S:
        life_a = age / METEOR_FADE_IN_S
    elif age > m["duration"] - METEOR_FADE_OUT_S:
        life_a = max(0.0, (m["duration"] - age) / METEOR_FADE_OUT_S)
    else:
        life_a = 1.0
    if life_a <= 0.0:
        return True

    # Trail — drawn back-to-front (dimmest first) so the brightest
    # near-head sample composites on top of the dimmer far-tail
    # samples. Position uses the same parabolic kinematic as the
    # head so the trail naturally curves along the meteor's arc.
    ay = m.get("ay", 0.0)
    painter.setPen(Qt.NoPen)
    for i in range(METEOR_TRAIL_SAMPLES, 0, -1):
        t_back = (i / METEOR_TRAIL_SAMPLES) * METEOR_TRAIL_LENGTH_S
        past_age = age - t_back
        if past_age < 0.0:
            continue
        tx = m["sx"] + m["vx"] * past_age
        ty = m["sy"] + m["vy"] * past_age + 0.5 * ay * past_age * past_age
        # frac is 1.0 at the head, 0.0 at the tail end.
        frac = 1.0 - (i / METEOR_TRAIL_SAMPLES)
        a = int(255 * life_a * frac * METEOR_TRAIL_MAX_ALPHA)
        if a < 2:
            continue
        col = QColor(m["color"])
        col.setAlpha(min(255, a))
        painter.setBrush(col)
        radius = METEOR_HEAD_RADIUS_PX * (0.35 + 0.65 * frac)
        painter.drawEllipse(
            int(tx - radius), int(ty - radius),
            int(radius * 2), int(radius * 2),
        )

    # Bright head on top.
    hx = m["sx"] + m["vx"] * age
    hy = m["sy"] + m["vy"] * age + 0.5 * ay * age * age
    a = int(255 * life_a)
    col = QColor(m["color"])
    col.setAlpha(min(255, a))
    painter.setBrush(col)
    r = METEOR_HEAD_RADIUS_PX
    painter.drawEllipse(int(hx - r), int(hy - r), int(r * 2), int(r * 2))
    return True


def draw_meteors(painter: QPainter, w: int, h: int) -> None:
    """Update + render the meteor system. Called from the spectrum
    widgets when their `_show_meteors` flag is set.

    Renders independent of the watermark image — meteors look fine
    against a black panadapter on their own. Uses additive blending
    so the streaks look luminous instead of pasted-on.
    """
    if w <= 0 or h <= 0:
        return

    global _next_meteor_t, _meteor_state_init
    now = time.monotonic()

    # Initialize the spawn schedule on first call so the very first
    # meteor doesn't appear immediately after the panadapter opens.
    if not _meteor_state_init:
        _next_meteor_t = now + random.uniform(METEOR_MIN_GAP_S, METEOR_MAX_GAP_S)
        _meteor_state_init = True

    # Spawn check.
    if (now >= _next_meteor_t and len(_meteors) < METEOR_MAX_CONCURRENT):
        _meteors.append(_spawn_meteor(w, h))
        _next_meteor_t = now + random.uniform(METEOR_MIN_GAP_S, METEOR_MAX_GAP_S)

    if not _meteors:
        return

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setCompositionMode(QPainter.CompositionMode_Plus)
    survivors: list[dict] = []
    for m in _meteors:
        if _draw_one_meteor(painter, m, now):
            survivors.append(m)
    painter.restore()
    # Replace in place so the module state stays the same list object.
    _meteors[:] = survivors


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
