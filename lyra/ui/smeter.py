"""Analog multi-scale S-meter — classic concentric-arc design.

Design features:
- Deep black background.
- Four concentric arc scales all sharing a single bottom-center pivot:
    outer  → S-units (1..9 in white, +10/+20/+30 in red)
    middle → PWR (watts, 0..200)
    inner  → SWR (1..∞ non-linear)
    core   → MIC (dB, -30..+5)
- Red shaded overload arc on the outer ring from S9 → S9+30.
- Single white needle shared across all scales (with slim glow halo);
  peak-hold needle in pale blue, decays slowly.
- Left-edge column of scale labels (S, PWR, SWR, MIC) in white.
- "RX1" indicator top-left.
- Large amber frequency readout at the bottom + cyan band + green mode.
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen,
    QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme


class AnalogMeter(QWidget):
    """Multi-scale analog meter (concentric S / PWR / SWR / MIC arcs)."""

    # Classic ham-radio-meter palette: black dial with white numerals,
    # vivid red for over-S9 zone, green peak needle, amber LCD freq
    # readout. Easy to read at a glance under bright shack lighting.
    BG          = QColor(6, 6, 8)
    FACE_RIM    = QColor(30, 30, 36)
    SCALE_WHITE = QColor(235, 235, 235)    # bright white numerals
    SCALE_DIM   = QColor(140, 140, 150)
    OVERLOAD    = QColor(255, 55, 55)
    OVERLOAD_BG = QColor(230, 40, 40, 200)
    NEEDLE      = QColor(245, 245, 245)
    NEEDLE_GLOW = QColor(120, 200, 255, 50)
    PEAK        = QColor(80, 230, 90)      # green peak needle
    READOUT_FG  = QColor(230, 168, 80)
    BAND_FG     = QColor(80, 200, 255)
    MODE_FG     = QColor(90, 230, 110)

    # Shallow-arc geometry — four concentric scales appear as gently-
    # curved bands across the top of the dial, with the pivot point
    # placed far below the visible dial area.
    SWEEP_HALF_DEG = 35.0        # total arc sweep = 70° (35° each side)
    SIDE_MARGIN_FRAC = 0.06      # outer arc leaves this much side margin

    # Radial spacing between concentric scales (in pixels)
    RING_SPACING_PX = 22

    def __init__(self, parent=None,
                 title: str = "S",
                 unit: str = "dBm",
                 db_min: float = -140.0,
                 db_s9: float = -73.0,
                 db_max: float = -43.0):
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._db_min = db_min
        self._db_s9 = db_s9
        self._db_max = db_max
        self._value = db_min
        self._peak = db_min
        self._peak_decay_dB_per_s = 5.0
        # dBFS → dBm conversion. With Lyra's true-dBFS spectrum
        # math (FFT normalized for coherent gain), a typical S9
        # signal reads near -54 dBFS at the bin peak, and S9 is
        # defined as -73 dBm — so the offset is -73 − (-54) = -19.
        # If you ever switch back to the old PSD-style "hot" math,
        # this offset needs to drop by ~34 to match.
        self._dbfs_to_dbm_offset = -19.0

        # Readout state (driven by the owning panel via setters)
        self._freq_hz = 0
        self._band_label = ""
        self._mode_label = ""

        # Min relaxed so the Meters panel can dock narrow.
        self.setMinimumSize(200, 140)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        # Transparent background — the scales/needles float on whatever
        # the parent GlassPanel paints. Only the LCD readout strip at
        # the bottom keeps its own opaque dark fill.
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")

        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick_decay)
        self._decay_timer.start(50)

    # ── Public setters ───────────────────────────────────────────────
    def set_level_dbfs(self, dbfs: float):
        dbm = dbfs + self._dbfs_to_dbm_offset
        self._value = dbm
        if dbm > self._peak:
            self._peak = dbm
        self.update()

    def set_freq_hz(self, hz: int):
        self._freq_hz = int(hz)
        self.update()

    def set_band(self, label: str):
        self._band_label = (label or "").upper()
        self.update()

    def set_mode(self, label: str):
        self._mode_label = (label or "").upper()
        self.update()

    # ── Geometry helpers ─────────────────────────────────────────────
    def _db_to_angle(self, db: float) -> float:
        """Map a dB value to Qt-convention angle (0° = 3 o'clock, CCW+).

        Sweep is centered on 90° (vertical up) with ±SWEEP_HALF_DEG to
        each side. Leftmost = 90 + half, rightmost = 90 - half.
        """
        db = max(self._db_min, min(self._db_max, db))
        frac = (db - self._db_min) / (self._db_max - self._db_min)
        left = 90.0 + self.SWEEP_HALF_DEG
        right = 90.0 - self.SWEEP_HALF_DEG
        return left - frac * (left - right)

    def _frac_to_angle(self, frac: float) -> float:
        """Generic 0..1 fraction → arc angle in the shallow sweep."""
        frac = max(0.0, min(1.0, frac))
        left = 90.0 + self.SWEEP_HALF_DEG
        right = 90.0 - self.SWEEP_HALF_DEG
        return left - frac * (left - right)

    def _compute_geometry(self, w: float, h: float):
        """Return (cx, pivot_y, r_s, r_pwr, r_swr, r_mic, readout_top)
        for the current widget size. Pivot sits well below the visible
        dial so the arcs appear as shallow crescents."""
        readout_h = 56
        top_h = 16
        # Available dial height after reserving top title area + readout
        dial_h = max(80, h - readout_h - top_h - 6)

        # Outer arc should span the full width minus a small side margin.
        side_margin = w * self.SIDE_MARGIN_FRAC
        arc_half_w = max(40, w / 2 - side_margin)
        # At angle ±SWEEP_HALF_DEG, horizontal offset = r * sin(angle).
        # Solve for radius so the arc just reaches the side margin.
        r_s = arc_half_w / math.sin(math.radians(self.SWEEP_HALF_DEG))

        # Arc visible depth = r * (1 - cos(sweep_half)). We cap r so the
        # visible arc depth for all four rings fits the dial area.
        rings_total_span_px = self.RING_SPACING_PX * 3  # 4 rings → 3 gaps
        max_arc_depth = dial_h - rings_total_span_px - 20  # tick/label room
        max_r_allowed = max_arc_depth / (1 - math.cos(
            math.radians(self.SWEEP_HALF_DEG)))
        if r_s > max_r_allowed:
            r_s = max_r_allowed

        r_pwr = r_s - self.RING_SPACING_PX
        r_swr = r_pwr - self.RING_SPACING_PX
        r_mic = r_swr - self.RING_SPACING_PX

        # Place pivot so the outer-arc top sits just below the top title.
        # top of outer arc (at angle 90°): y = pivot_y - r_s.
        arc_top_y = top_h + 8
        pivot_y = arc_top_y + r_s

        cx = w / 2
        readout_top = h - readout_h
        return cx, pivot_y, r_s, r_pwr, r_swr, r_mic, readout_top

    # ── Paint ────────────────────────────────────────────────────────
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        w, h = self.width(), self.height()
        # No background fill — floats on the parent panel's painted
        # surface. Makes the meter read as a "ghosted" overlay in
        # front of the panel instead of a solid black box.

        cx, pivot_y, r_s, r_pwr, r_swr, r_mic, readout_top = \
            self._compute_geometry(w, h)
        if r_s < 80:
            return

        self._draw_rx_indicator(p)
        self._draw_overload_band(p, cx, pivot_y, r_s)
        self._draw_s_scale(p, cx, pivot_y, r_s)
        self._draw_pwr_scale(p, cx, pivot_y, r_pwr)
        self._draw_swr_scale(p, cx, pivot_y, r_swr)
        self._draw_mic_scale(p, cx, pivot_y, r_mic)
        self._draw_side_labels(p, cx, pivot_y, r_s, r_pwr, r_swr, r_mic)

        # Hub (visible pivot cap) sits at a fixed spot on the vertical
        # axis just below the MIC scale. The needle emanates from this
        # hub to the tip that rides the outer arc — matches how the
        # operator sees a real analog meter.
        hub_x = cx
        hub_y = pivot_y - (r_mic - 8)
        tip_r = r_s + 2

        # Peak (behind), then glow halo, then main needle.
        self._draw_hub_needle(p, hub_x, hub_y, cx, pivot_y, tip_r,
                              self._db_to_angle(self._peak),
                              self.PEAK, thickness=1.5)
        self._draw_hub_needle(p, hub_x, hub_y, cx, pivot_y, tip_r,
                              self._db_to_angle(self._value),
                              self.NEEDLE_GLOW, thickness=6.0)
        self._draw_hub_needle(p, hub_x, hub_y, cx, pivot_y, tip_r,
                              self._db_to_angle(self._value),
                              self.NEEDLE, thickness=2.0)

        self._draw_pivot(p, hub_x, hub_y)

        self._draw_readout(p, 0, readout_top, w, h - readout_top)

    def _draw_hub_needle(self, p, hub_x, hub_y, cx, pivot_y, tip_r,
                         angle_deg, color, thickness=2.0):
        """Draw a straight needle from the visible hub to the tip on
        the arc at the given angle. The tip is at (pivot-based radius,
        angle) but the line origin is the visible hub, so the needle
        reads as "mounted on the hub" — visually correct even though
        our mathematical pivot is off-screen."""
        ang = math.radians(angle_deg)
        tx = cx + tip_r * math.cos(ang)
        ty = pivot_y - tip_r * math.sin(ang)
        p.setPen(QPen(color, thickness))
        p.drawLine(QPointF(hub_x, hub_y), QPointF(tx, ty))

    # ── Dial features ────────────────────────────────────────────────
    def _draw_rx_indicator(self, p):
        # Drawn in the dark surround area above the cream dial face,
        # so use a light color for contrast.
        f = QFont()
        f.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(QColor(235, 235, 235), 1))
        p.drawText(QPointF(12, 16), "RX1")

    def _draw_overload_band(self, p, cx, cy, r_s):
        """Solid red arc band from S9 → S9+30 on the outer S-ring."""
        band_w = 11
        rect = QRectF(cx - r_s, cy - r_s, 2 * r_s, 2 * r_s)
        a0 = self._db_to_angle(self._db_s9)
        a1 = self._db_to_angle(self._db_max)
        p.setPen(QPen(self.OVERLOAD_BG, band_w, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(rect, int(a0 * 16), int((a1 - a0) * 16))

    def _draw_s_scale(self, p, cx, cy, r):
        f = QFont()
        f.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        f.setPointSize(11)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        tick_out = r - 2
        tick_maj = r - 10
        tick_min = r - 6
        lbl_r = r - 22

        # Thin arc backdrop for S-scale
        p.setPen(QPen(self.SCALE_WHITE, 1))
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        a_left = self._frac_to_angle(0.0)
        a_right = self._frac_to_angle(1.0)
        p.drawArc(rect, int(a_right * 16),
                  int((a_left - a_right) * 16))

        # Minor ticks every 2 dB in S-range
        p.setPen(QPen(self.SCALE_DIM, 1))
        for db in range(int(self._db_min), int(self._db_s9) + 1, 2):
            ang = math.radians(self._db_to_angle(db))
            self._line_polar(p, cx, cy, tick_out, tick_min, ang)

        # Major S1/3/5/7/9 with numerals
        p.setPen(QPen(self.SCALE_WHITE, 2))
        for s in (1, 3, 5, 7, 9):
            db = self._db_s9 - (9 - s) * 6.0
            ang = math.radians(self._db_to_angle(db))
            self._line_polar(p, cx, cy, tick_out, tick_maj, ang)
        # Draw labels after lines so they don't get overdrawn
        p.setPen(QPen(self.SCALE_WHITE, 1))
        for s in (1, 3, 5, 7, 9):
            db = self._db_s9 - (9 - s) * 6.0
            ang = math.radians(self._db_to_angle(db))
            lx = cx + lbl_r * math.cos(ang)
            ly = cy - lbl_r * math.sin(ang)
            self._draw_centered(p, fm, str(s), lx, ly)

        # Red over-9 numerals: +10, +20, +30
        p.setPen(QPen(self.OVERLOAD, 2))
        for extra, lbl in ((10, "+10"), (20, "+20"), (30, "+30")):
            db = self._db_s9 + extra
            if db > self._db_max:
                continue
            ang = math.radians(self._db_to_angle(db))
            self._line_polar(p, cx, cy, tick_out, tick_maj, ang)
        p.setPen(QPen(self.OVERLOAD, 1))
        for extra, lbl in ((10, "+10"), (20, "+20"), (30, "+30")):
            db = self._db_s9 + extra
            if db > self._db_max:
                continue
            ang = math.radians(self._db_to_angle(db))
            lx = cx + lbl_r * math.cos(ang)
            ly = cy - lbl_r * math.sin(ang)
            self._draw_centered(p, fm, lbl, lx, ly)

    def _draw_pwr_scale(self, p, cx, cy, r):
        """Power output scale: 0..10W mapped across the arc (HL2 max)."""
        f = QFont()
        f.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        tick_out = r + 3
        tick_in = r - 5
        lbl_r = r - 13

        # Thin arc backdrop
        p.setPen(QPen(self.SCALE_DIM, 1))
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        a_left = self._frac_to_angle(0.0)
        a_right = self._frac_to_angle(1.0)
        p.drawArc(rect, int(a_right * 16),
                  int((a_left - a_right) * 16))

        # Labels at 0, 2, 5, 8, 10 watts (HL2 range); position as fraction of 0..10
        values = [(0, "0"), (2, "2"), (5, "5"), (8, "8"), (10, "10")]
        p.setPen(QPen(self.SCALE_WHITE, 1.5))
        for w_val, lbl in values:
            frac = w_val / 10.0
            ang = math.radians(self._frac_to_angle(frac))
            self._line_polar(p, cx, cy, tick_out, tick_in, ang)
            lx = cx + lbl_r * math.cos(ang)
            ly = cy - lbl_r * math.sin(ang)
            self._draw_centered(p, fm, lbl, lx, ly)

    def _draw_swr_scale(self, p, cx, cy, r):
        """SWR scale, non-linear: 1:1 at left → ∞ at right."""
        f = QFont()
        f.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        tick_out = r + 3
        tick_in = r - 5
        lbl_r = r - 13

        p.setPen(QPen(self.SCALE_DIM, 1))
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        a_left = self._frac_to_angle(0.0)
        a_right = self._frac_to_angle(1.0)
        p.drawArc(rect, int(a_right * 16),
                  int((a_left - a_right) * 16))

        # Non-linear mapping — 1:1 at 0, 1.5 at ~0.25, 2 at ~0.4, 3 at ~0.65,
        # 5 at ~0.85, ∞ at 1.0
        entries = [(1.0, 0.0, "1"), (1.5, 0.25, "1.5"),
                   (2.0, 0.4, "2"), (3.0, 0.65, "3"),
                   (5.0, 0.85, "5"), (None, 1.0, "∞")]
        p.setPen(QPen(self.SCALE_WHITE, 1.5))
        for _swr, frac, lbl in entries:
            ang = math.radians(self._frac_to_angle(frac))
            self._line_polar(p, cx, cy, tick_out, tick_in, ang)
            lx = cx + lbl_r * math.cos(ang)
            ly = cy - lbl_r * math.sin(ang)
            self._draw_centered(p, fm, lbl, lx, ly)

    def _draw_mic_scale(self, p, cx, cy, r):
        """MIC-level scale in dB: -30..+5."""
        f = QFont()
        f.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        tick_out = r + 3
        tick_in = r - 5
        lbl_r = r - 13

        p.setPen(QPen(self.SCALE_DIM, 1))
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        a_left = self._frac_to_angle(0.0)
        a_right = self._frac_to_angle(1.0)
        p.drawArc(rect, int(a_right * 16),
                  int((a_left - a_right) * 16))

        # Linear: -30 at 0, +5 at 1
        def frac(db):
            return (db - (-30)) / (5 - (-30))
        entries = [(-30, "-30"), (-20, "-20"), (-10, "-10"),
                   (0, "0"), (5, "+5")]
        p.setPen(QPen(self.SCALE_WHITE, 1.5))
        for db, lbl in entries:
            ang = math.radians(self._frac_to_angle(frac(db)))
            self._line_polar(p, cx, cy, tick_out, tick_in, ang)
            lx = cx + lbl_r * math.cos(ang)
            ly = cy - lbl_r * math.sin(ang)
            self._draw_centered(p, fm, lbl, lx, ly)

    def _draw_side_labels(self, p, cx, pivot_y, r_s, r_pwr, r_swr, r_mic):
        """Column of scale labels (S / PWR / SWR / MIC) just left of
        the leftmost end of each arc. Pivot is off-screen below; each
        arc's leftmost visible point is at angle = 90 + SWEEP_HALF."""
        f = QFont()
        f.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        f.setPointSize(10)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        ang_left = math.radians(90.0 + self.SWEEP_HALF_DEG)
        p.setPen(QPen(self.SCALE_WHITE, 1))
        labels = [
            (r_s,   "S"),
            (r_pwr, "PWR"),
            (r_swr, "SWR"),
            (r_mic, "MIC"),
        ]
        for r, txt in labels:
            lx = cx + r * math.cos(ang_left) - fm.horizontalAdvance(txt) - 4
            ly = pivot_y - r * math.sin(ang_left) + 4
            p.drawText(QPointF(lx, ly), txt)

    def _draw_needle(self, p, cx, cy, length, db, color, glow=False, thickness=2.0):
        ang = math.radians(self._db_to_angle(db))
        tx = cx + length * math.cos(ang)
        ty = cy - length * math.sin(ang)
        p.setPen(QPen(color, thickness))
        p.drawLine(QPointF(cx, cy), QPointF(tx, ty))

    def _draw_needle_glow(self, p, cx, cy, length, db):
        ang = math.radians(self._db_to_angle(db))
        tx = cx + length * math.cos(ang)
        ty = cy - length * math.sin(ang)
        p.setPen(QPen(self.NEEDLE_GLOW, 6))
        p.drawLine(QPointF(cx, cy), QPointF(tx, ty))

    def _draw_pivot(self, p, cx, cy):
        grad = QRadialGradient(cx - 2, cy - 2, 9)
        grad.setColorAt(0.0, QColor(200, 200, 200))
        grad.setColorAt(0.6, QColor(90, 90, 90))
        grad.setColorAt(1.0, QColor(10, 10, 10))
        p.setBrush(grad)
        p.setPen(QPen(QColor(0, 0, 0), 1))
        p.drawEllipse(QPointF(cx, cy), 7, 7)

    def _draw_readout(self, p, x, y, w, h):
        # Background strip
        rect = QRectF(x + 4, y + 2, w - 8, h - 6)
        bg_grad = QLinearGradient(0, y, 0, y + h)
        bg_grad.setColorAt(0.0, QColor(10, 10, 12))
        bg_grad.setColorAt(1.0, QColor(22, 22, 26))
        p.setBrush(bg_grad)
        p.setPen(QPen(QColor(50, 50, 60), 1))
        p.drawRoundedRect(rect, 3, 3)

        # Frequency — three-segment amber display: MHz.kHz.Hz
        mhz = self._freq_hz // 1_000_000
        khz = (self._freq_hz % 1_000_000) // 1000
        hz = self._freq_hz % 1000
        freq_text = f"{mhz:03d}.{khz:03d}.{hz:03d}"
        freq_font = QFont()
        freq_font.setFamilies([theme.FONT_MONO_FAMILY, "Consolas"])
        freq_font.setPointSize(16)
        freq_font.setBold(True)
        p.setFont(freq_font)
        fm = QFontMetrics(freq_font)
        tw = fm.horizontalAdvance(freq_text)
        cx = x + w / 2
        p.setPen(QPen(self.READOUT_FG, 1))
        p.drawText(QPointF(cx - tw / 2, y + h / 2 + 4), freq_text)

        # Band + mode on a second line
        tag_font = QFont()
        tag_font.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        tag_font.setPointSize(9)
        tag_font.setBold(True)
        p.setFont(tag_font)
        tfm = QFontMetrics(tag_font)
        band = self._band_label or "—"
        mode = self._mode_label or "—"
        band_w = tfm.horizontalAdvance(band)
        mode_w = tfm.horizontalAdvance(mode)
        gap = 40
        total = band_w + gap + mode_w
        bx = cx - total / 2
        p.setPen(QPen(self.BAND_FG, 1))
        p.drawText(QPointF(bx, y + h - 6), band)
        p.setPen(QPen(self.MODE_FG, 1))
        p.drawText(QPointF(bx + band_w + gap, y + h - 6), mode)

    # ── Utilities ────────────────────────────────────────────────────
    @staticmethod
    def _line_polar(p, cx, cy, r_out, r_in, ang_rad):
        p.drawLine(
            QPointF(cx + r_out * math.cos(ang_rad),
                    cy - r_out * math.sin(ang_rad)),
            QPointF(cx + r_in * math.cos(ang_rad),
                    cy - r_in * math.sin(ang_rad)),
        )

    @staticmethod
    def _draw_centered(p, fm, text, x_center, y_center):
        tw = fm.horizontalAdvance(text)
        p.drawText(QPointF(x_center - tw / 2, y_center + 4), text)

    def _tick_decay(self):
        dt_s = 0.05
        decay = self._peak_decay_dB_per_s * dt_s
        if self._peak > self._value + decay:
            self._peak -= decay
        else:
            self._peak = self._value
        self.update()


# ── Lit-arc segment meter (no needle — segments light along the arc) ──
class LitArcMeter(QWidget):
    """Curved analog-style meter face with NO needle — instead a row of
    radial LED-style segments traces the arc and lights up cumulatively
    from the left up to the current value.

    Why no needle:
      - Needle position is quantized by pixel resolution; segments give
        explicit count-of-divisions accuracy
      - Sub-tick FFT updates can flick the segment count smoothly without
        the visual "tremor" a fast-jittering needle has
      - Peak-hold becomes a single brighter segment that trails the bar
        — visually obvious, no second needle needed

    Mode switching via click-the-scale-label gesture: each mode label
    in the row above the arc is a hit target. Active mode is rendered
    in its own color; clicking another label switches modes. The arc's
    color gradient and scale numerals re-render to match.

    Modes implemented now:
      S       — signal strength in S-units (S0..S9+30)
      dBm     — same data, dBm scale label (-127..-43 dBm)
      AGC     — current AGC gain in dB (0..60 dB scale)

    Future modes (placeholders ready, wire up when TX ships):
      MIC     — microphone input level
      PWR     — RF output power, watts
      SWR     — VSWR during TX

    Per-mode color palette (operator-distinct so a glance tells you
    which mode the meter is in without reading the label):
      S/dBm  — deep green → light green → amber → red
      AGC    — deep blue → cyan → white-blue (cool palette)
      MIC    — slate → light amber → bright amber (TX warming)
      PWR    — deep red → orange → yellow (TX energy)
      SWR    — green at 1:1, climbing sharply to red past 2:1
    """

    # ── Mode definitions ─────────────────────────────────────────────
    MODE_S    = "S"
    MODE_DBM  = "dBm"
    MODE_AGC  = "AGC"
    # Future:
    MODE_MIC  = "MIC"
    MODE_PWR  = "PWR"
    MODE_SWR  = "SWR"

    # Modes available right now (RX-only). TX modes added when TX ships.
    AVAILABLE_MODES = (MODE_S, MODE_DBM, MODE_AGC)

    # Per-mode color stops — list of (fraction_along_arc, QColor).
    # paint() interpolates between adjacent stops for each segment.
    _S_GRADIENT = [
        (0.00, QColor(  0, 110,  40)),   # deep green   (S0)
        (0.40, QColor( 60, 220,  80)),   # bright green (S5)
        (0.65, QColor(180, 240,  60)),   # lime         (S9)
        (0.80, QColor(255, 200,  60)),   # amber        (+10)
        (0.92, QColor(255, 140,  40)),   # orange       (+20)
        (1.00, QColor(255,  60,  60)),   # red          (+30)
    ]
    _AGC_GRADIENT = [
        (0.00, QColor( 30,  60, 130)),   # deep blue
        (0.50, QColor(  0, 180, 255)),   # cyan
        (1.00, QColor(220, 240, 255)),   # near-white blue
    ]
    _MIC_GRADIENT = [
        (0.00, QColor( 90, 100, 110)),   # slate
        (0.60, QColor(220, 180,  60)),   # amber
        (1.00, QColor(255, 220,  80)),   # bright amber
    ]
    _PWR_GRADIENT = [
        (0.00, QColor(120,   0,   0)),   # deep red
        (0.50, QColor(255, 130,  40)),   # orange
        (1.00, QColor(255, 240,  80)),   # yellow
    ]
    _SWR_GRADIENT = [
        (0.00, QColor( 60, 220,  80)),   # green at 1.0:1
        (0.30, QColor(180, 240,  60)),   # lime up to 1.5:1
        (0.50, QColor(255, 200,  60)),   # amber at 2:1
        (1.00, QColor(255,  60,  60)),   # red beyond 2.5:1
    ]

    @classmethod
    def _gradient_for(cls, mode: str):
        return {
            cls.MODE_S:    cls._S_GRADIENT,
            cls.MODE_DBM:  cls._S_GRADIENT,    # same physical signal
            cls.MODE_AGC:  cls._AGC_GRADIENT,
            cls.MODE_MIC:  cls._MIC_GRADIENT,
            cls.MODE_PWR:  cls._PWR_GRADIENT,
            cls.MODE_SWR:  cls._SWR_GRADIENT,
        }.get(mode, cls._S_GRADIENT)

    # ── Geometry ─────────────────────────────────────────────────────
    NUM_SEGMENTS    = 80         # divisions along the arc
    SEG_GAP_DEG     = 0.10       # tiny gap between adjacent segments
    SWEEP_HALF_DEG  = 38.0       # arc sweep = 76° (38° each side)
    SIDE_MARGIN_PX  = 12

    # ── Visual style ─────────────────────────────────────────────────
    BG          = QColor(6, 6, 8)
    UNLIT_DIM   = QColor(38, 42, 50)        # unlit segment background
    PEAK_GLOW   = QColor(255, 255, 255)     # peak-hold marker overlay
    SCALE_LBL   = QColor(225, 230, 240)
    SCALE_DIM   = QColor(140, 145, 155)
    READOUT_FG  = QColor(255, 200, 80)      # numeric readout (amber LCD)
    MODE_ACTIVE = QColor(255, 200, 80)
    MODE_IDLE   = QColor(110, 120, 135)

    # Signal — emits when operator clicks a mode label.
    mode_changed = Signal(str)

    def __init__(self, parent=None, mode: str = MODE_S):
        super().__init__(parent)
        self._mode = mode if mode in self.AVAILABLE_MODES else self.MODE_S
        # Live values — held in their respective natural units.
        self._dbfs       = -160.0    # raw RX peak from radio.smeter_level
        self._peak_dbfs  = -160.0    # peak-hold trace
        self._agc_db     = 0.0       # current AGC applied gain in dB
        # dBFS → dBm offset (matches the S-meter cal post true-dBFS fix)
        self._dbfs_to_dbm_offset = -19.0
        # Peak-hold decay rate (dB / s). Tunable via setter / right-click.
        self._peak_decay_dB_per_s = 6.0

        # Mode-label hit boxes filled in paint(), used by mousePressEvent
        self._mode_hitboxes: dict[str, QRectF] = {}

        # Cached arc geometry — recomputed on resize, NOT per frame.
        self._geom_dirty = True
        self._segments: list[QPolygonF] = []
        self._scale_ticks: list[tuple[float, str, bool]] = []   # (frac, label, is_major)
        self._readout_rect = QRectF()

        # Decay timer — drives the peak-hold trace down over time.
        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick_decay)
        self._decay_timer.start(50)

        # Minimum size kept conservative so operators can dock the
        # Meters panel into a narrow column. The arc geometry is
        # bounded against label-edge fit (see _build_geometry), so
        # going below this min just shrinks the arc — segments and
        # labels still fit cleanly. Old min was 290×170, which made
        # the Meters panel hard to make compact.
        self.setMinimumSize(200, 140)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    # ── Public setters (panel wires Radio signals here) ──────────────
    def set_level_dbfs(self, dbfs: float):
        """Drive the S / dBm modes from the radio's smeter_level signal."""
        self._dbfs = float(dbfs)
        if self._dbfs > self._peak_dbfs:
            self._peak_dbfs = self._dbfs
        self.update()

    def set_agc_db(self, db: float):
        """Drive the AGC mode from the radio's current AGC gain action."""
        self._agc_db = float(db)
        self.update()

    def set_mode(self, mode: str):
        if mode not in self.AVAILABLE_MODES:
            return
        if mode == self._mode:
            return
        self._mode = mode
        # Reset peak when switching modes — old peak is meaningless
        # in the new scale.
        self._peak_dbfs = self._dbfs
        self.update()
        self.mode_changed.emit(mode)

    @property
    def mode(self) -> str:
        return self._mode

    def set_peak_decay_dbps(self, dbps: float):
        self._peak_decay_dB_per_s = max(0.0, float(dbps))

    # ── Internal: per-mode value-to-fraction mapping ─────────────────
    # Each mode reports a (current_fraction, peak_fraction, readout_string)
    # triple — the painter doesn't care which mode it is.
    def _value_state(self) -> tuple[float, float, str]:
        m = self._mode
        if m == self.MODE_S or m == self.MODE_DBM:
            # S0 = -127 dBm = -73 - 54; S9+30 = -43 dBm
            dbm_now  = self._dbfs + self._dbfs_to_dbm_offset
            dbm_peak = self._peak_dbfs + self._dbfs_to_dbm_offset
            f_now  = (dbm_now  + 127.0) / (-43.0 - -127.0)
            f_peak = (dbm_peak + 127.0) / (-43.0 - -127.0)
            f_now  = max(0.0, min(1.0, f_now))
            f_peak = max(0.0, min(1.0, f_peak))
            if m == self.MODE_S:
                readout = self._dbm_to_s_string(dbm_now)
            else:
                readout = f"{dbm_now:+.0f} dBm"
            return f_now, f_peak, readout
        if m == self.MODE_AGC:
            # 0..60 dB AGC range
            f_now = max(0.0, min(1.0, self._agc_db / 60.0))
            return f_now, f_now, f"{self._agc_db:+.1f} dB"
        # Future TX modes — no live data yet, return zero
        return 0.0, 0.0, "—"

    @staticmethod
    def _dbm_to_s_string(dbm: float) -> str:
        """Format a dBm value as an S-unit reading (S0..S9+30 dB)."""
        if dbm >= -73.0:
            over = dbm - -73.0
            if over < 0.5:
                return "S9"
            return f"S9+{int(round(over))}"
        # below S9: each S-unit is 6 dB
        s = max(0, int(round((dbm - -127.0) / 6.0)))
        return f"S{min(s, 9)}"

    # ── Geometry caching ─────────────────────────────────────────────
    def resizeEvent(self, _event):
        self._geom_dirty = True
        super().resizeEvent(_event)

    def _build_geometry(self):
        """Compute arc segment polygons + scale tick positions ONCE per
        size change. Called lazily from paint() when geom_dirty.

        Auto-centers the arc within the available drawing area:
        the radius is bounded by BOTH the horizontal and vertical
        room left after the mode chips + scale-label margin + readout
        strip are reserved. Whichever bound is tighter wins, then the
        resulting crescent is centered in the leftover space so the
        meter never gets clipped at small panel sizes.
        """
        from PySide6.QtCore import QPointF
        w, h = self.width(), self.height()
        # Reserve top strip for the mode labels, bottom for the readout.
        mode_strip_h    = 22
        readout_strip_h = 30
        # Vertical room reserved OUTSIDE the segments for scale labels
        # (which are drawn outside R_outer) — needs to come off the
        # top of the arc area before we compute the available depth.
        label_reserve   = 14
        arc_top    = mode_strip_h + label_reserve
        arc_bottom = h - readout_strip_h - 6

        usable_w = max(60, w - 2 * self.SIDE_MARGIN_PX)
        usable_h = max(40, arc_bottom - arc_top)

        sweep   = math.radians(self.SWEEP_HALF_DEG)
        sin_sw  = math.sin(sweep)
        cos_sw  = math.cos(sweep)

        # Two radius bounds — pick the tighter so the arc never
        # overflows in either dimension.
        #
        # Width bound: the rightmost scale LABEL center sits at
        #   center_x + R_label · sin(sweep)
        # and the label text adds LABEL_TEXT_HALF_WIDTH_PX past that.
        # We compute the bound against label position (not segment
        # position) because labels are what hangs off the widget
        # edge first — the segments themselves are well inside.
        #
        # Depth bound: arc visible depth = R · (1 − cos(sweep))
        LABEL_RADIAL_OFFSET    = 11.0   # how far labels sit OUTSIDE R_outer
        LABEL_TEXT_HALF_WIDTH  = 14.0   # widest label (e.g. '+30') ≈ 24 px
        # Solve label-edge ≤ usable_w/2 for R_outer:
        #   (R_outer + LABEL_RADIAL_OFFSET) · sin_sw + LABEL_TEXT_HALF_WIDTH
        #     ≤ usable_w / 2
        if sin_sw > 0:
            R_max_horiz = ((usable_w / 2.0 - LABEL_TEXT_HALF_WIDTH) / sin_sw
                           - LABEL_RADIAL_OFFSET)
        else:
            R_max_horiz = usable_w
        R_max_vert  = usable_h / max(1e-3, 1.0 - cos_sw)
        R_outer     = max(40.0, min(R_max_horiz, R_max_vert))

        # Center the visible-arc crescent in the available space.
        # The crescent's vertical extent is R*(1-cos(sweep)); place
        # its top edge so the crescent sits centered between
        # arc_top and arc_bottom.
        arc_depth   = R_outer * (1.0 - cos_sw)
        crescent_top_y = arc_top + (usable_h - arc_depth) / 2.0
        # The pivot lies R_outer below the topmost point of the arc
        # (which is at angle 0 = straight up); equivalently, R*cos(sweep)
        # below the bottom edge of the crescent at the side angles.
        center_x = w / 2.0
        center_y = crescent_top_y + R_outer

        # Segment thickness scales with arc radius so it looks right
        # at any panel size from compact (R~80) to large (R~250).
        seg_thick = max(8.0, min(20.0, R_outer * 0.10))
        R_inner = R_outer - seg_thick
        R_label = R_outer + LABEL_RADIAL_OFFSET

        # Build segment polygons. Each is a small wedge between two
        # angles, from R_inner to R_outer.
        n = self.NUM_SEGMENTS
        gap = math.radians(self.SEG_GAP_DEG)
        # Total angular span = 2 * sweep, divided into n segments
        seg_span = (2 * sweep - n * gap) / n
        # Angle 0 = straight up; positive = clockwise (right side)
        # We want segment 0 at the LEFT (angle = -sweep + gap/2)
        self._segments.clear()
        for i in range(n):
            a0 = -sweep + gap / 2 + i * (seg_span + gap)
            a1 = a0 + seg_span
            poly = QPolygonF()
            # Inner-left, outer-left, outer-right, inner-right
            poly.append(QPointF(center_x + R_inner * math.sin(a0),
                                center_y - R_inner * math.cos(a0)))
            poly.append(QPointF(center_x + R_outer * math.sin(a0),
                                center_y - R_outer * math.cos(a0)))
            poly.append(QPointF(center_x + R_outer * math.sin(a1),
                                center_y - R_outer * math.cos(a1)))
            poly.append(QPointF(center_x + R_inner * math.sin(a1),
                                center_y - R_inner * math.cos(a1)))
            self._segments.append(poly)

        # Scale ticks. The label set depends on mode; cache positions
        # per-mode would be over-engineering — paint() rebuilds the
        # label list on the fly using these (fraction, label, is_major)
        # tuples. We only need the ANGULAR positions cached here; the
        # actual labels come from the active mode.
        self._scale_label_geom = (center_x, center_y, R_label, sweep)

        # Readout text rect
        self._readout_rect = QRectF(
            0, h - readout_strip_h - 2, w, readout_strip_h)
        # Mode-label strip rect
        self._mode_strip_rect = QRectF(0, 0, w, mode_strip_h)

        self._geom_dirty = False

    # ── Mode-label tick definitions per mode ─────────────────────────
    def _scale_labels(self) -> list[tuple[float, str, bool]]:
        """Return list of (fraction_0_to_1, label_text, is_major)."""
        m = self._mode
        if m == self.MODE_S:
            # S0..S9 every 6 dB, then +10/+20/+30 above
            ticks = []
            for s_unit in range(0, 10):
                # Each S-unit = 6 dB. Scale spans -127..-43 (84 dB).
                # S0 = 0, S9 = -73 - -127 = 54 / 84 = 0.643
                dbm = -127.0 + s_unit * 6.0
                frac = (dbm + 127.0) / 84.0
                lbl = f"S{s_unit}" if s_unit % 3 == 0 else ""
                ticks.append((frac, lbl, s_unit % 3 == 0))
            for over in (10, 20, 30):
                dbm = -73.0 + over
                frac = (dbm + 127.0) / 84.0
                ticks.append((frac, f"+{over}", True))
            return ticks
        if m == self.MODE_DBM:
            return [
                (0.000, "-127", True),
                (0.250, "-106", False),
                (0.500, "-85", True),
                (0.643, "-73", True),    # S9 reference
                (0.750, "-64", False),
                (1.000, "-43", True),
            ]
        if m == self.MODE_AGC:
            # 0..60 dB
            ticks = []
            for db in range(0, 61, 10):
                ticks.append((db / 60.0, str(db), True))
            return ticks
        return []

    # ── Paint ────────────────────────────────────────────────────────
    def paintEvent(self, _event):
        if self._geom_dirty:
            self._build_geometry()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self.BG)

        f_now, f_peak, readout = self._value_state()
        gradient = self._gradient_for(self._mode)

        # ── Arc segments ────────────────────────────────────────────
        n = len(self._segments)
        # Index of the "currently lit" boundary — segments [0..lit_idx)
        # render in their gradient color, segments [lit_idx..) render dim.
        lit_idx = int(round(f_now * n))
        peak_idx = int(round(f_peak * n))
        for i, poly in enumerate(self._segments):
            frac = (i + 0.5) / n
            if i < lit_idx:
                col = self._sample_gradient(gradient, frac)
            else:
                col = self.UNLIT_DIM
            p.setBrush(col)
            p.setPen(Qt.NoPen)
            p.drawPolygon(poly)
        # Peak-hold marker — overlay a brighter version of the gradient
        # color on the peak segment (only if peak is ahead of current).
        if 0 <= peak_idx < n and peak_idx >= lit_idx:
            peak_col = self._sample_gradient(gradient, (peak_idx + 0.5) / n)
            # Brighten: blend toward white
            r, g, b = peak_col.red(), peak_col.green(), peak_col.blue()
            peak_col = QColor(min(255, r + 80),
                              min(255, g + 80),
                              min(255, b + 80))
            p.setBrush(peak_col)
            p.drawPolygon(self._segments[peak_idx])

        # ── Scale labels around the outside ─────────────────────────
        center_x, center_y, R_label, sweep = self._scale_label_geom
        scale_font = QFont()
        scale_font.setFamilies(["Consolas", "Courier New"])
        scale_font.setPointSize(8)
        scale_font.setBold(True)
        p.setFont(scale_font)
        fm = QFontMetrics(scale_font)
        for frac, lbl, is_major in self._scale_labels():
            angle = -sweep + frac * (2 * sweep)
            x = center_x + R_label * math.sin(angle)
            y = center_y - R_label * math.cos(angle)
            if is_major:
                p.setPen(QPen(self.SCALE_LBL, 1))
            else:
                p.setPen(QPen(self.SCALE_DIM, 1))
            if lbl:
                tw = fm.horizontalAdvance(lbl)
                p.drawText(QPointF(x - tw / 2, y + fm.ascent() / 2 - 2), lbl)
            else:
                # tick mark instead of label — small dot
                p.setBrush(self.SCALE_DIM)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(x, y), 1.4, 1.4)

        # ── Mode picker chips along the top ─────────────────────────
        self._mode_hitboxes.clear()
        chip_font = QFont()
        chip_font.setFamilies(["Segoe UI", "Arial"])
        chip_font.setPointSize(9)
        chip_font.setBold(True)
        p.setFont(chip_font)
        cfm = QFontMetrics(chip_font)
        chip_pad = 12
        x = 8
        y = 4
        for mode in self.AVAILABLE_MODES:
            tw = cfm.horizontalAdvance(mode) + chip_pad
            rect = QRectF(x, y, tw, self._mode_strip_rect.height() - 8)
            self._mode_hitboxes[mode] = rect
            is_active = (mode == self._mode)
            if is_active:
                # Filled chip in the mode's primary color
                grad = self._gradient_for(mode)
                fill_col = self._sample_gradient(grad, 0.7)
                fill_col = QColor(fill_col.red(), fill_col.green(),
                                  fill_col.blue(), 220)
                p.setBrush(fill_col)
                p.setPen(QPen(self.MODE_ACTIVE, 1))
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(QPen(QColor(0, 0, 0), 1))
            else:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(self.MODE_IDLE, 1))
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(QPen(self.MODE_IDLE, 1))
            p.drawText(rect, Qt.AlignCenter, mode)
            x += tw + 6

        # ── Numeric readout at the bottom ───────────────────────────
        readout_font = QFont()
        readout_font.setFamilies(["Consolas", "Courier New"])
        readout_font.setPointSize(16)
        readout_font.setBold(True)
        p.setFont(readout_font)
        p.setPen(QPen(self.READOUT_FG, 1))
        p.drawText(self._readout_rect, Qt.AlignCenter, readout)

    # ── Gradient sampling helper ─────────────────────────────────────
    @staticmethod
    def _sample_gradient(stops: list, frac: float) -> QColor:
        """Linear-interpolate the gradient stop list at `frac` (0..1)."""
        frac = max(0.0, min(1.0, frac))
        if frac <= stops[0][0]:
            return stops[0][1]
        if frac >= stops[-1][0]:
            return stops[-1][1]
        for i in range(len(stops) - 1):
            f0, c0 = stops[i]
            f1, c1 = stops[i + 1]
            if f0 <= frac <= f1:
                t = (frac - f0) / (f1 - f0) if f1 > f0 else 0.0
                return QColor(
                    int(round(c0.red()   + t * (c1.red()   - c0.red()))),
                    int(round(c0.green() + t * (c1.green() - c0.green()))),
                    int(round(c0.blue()  + t * (c1.blue()  - c0.blue()))),
                )
        return stops[-1][1]

    # ── Decay tick + mode click ──────────────────────────────────────
    def _tick_decay(self):
        if self._peak_dbfs > self._dbfs:
            self._peak_dbfs -= self._peak_decay_dB_per_s * 0.05
            if self._peak_dbfs < self._dbfs:
                self._peak_dbfs = self._dbfs
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        for mode, rect in self._mode_hitboxes.items():
            if rect.contains(pos):
                self.set_mode(mode)
                event.accept()
                return
        super().mousePressEvent(event)


# ── Multi-bar LED meter (S / PWR / SWR / MIC / AGC stacked) ───────────
class LedBarMeter(QWidget):
    """Compact stacked multi-meter — five thin LED bars, one per quantity.

    Rows (top → bottom):
      S    — signal strength (RX) — live now
      PWR  — TX RF output watts (placeholder until TX is wired)
      SWR  — TX SWR              (placeholder)
      MIC  — TX mic level        (placeholder)
      AGC  — RX AGC gain action  (placeholder; will be live with AGC profiles)

    Each bar is ~10 px tall, with a tiny scale legend above and the
    label on the left margin. Unlit bars stay dim so the layout reads
    clearly even when the radio is RX-only.
    """
    BG         = QColor(4, 4, 6)
    LED_GREEN  = QColor(40, 220, 100)
    LED_YELLOW = QColor(255, 210, 60)
    LED_RED    = QColor(255, 60, 60)
    LED_BLUE   = QColor(80, 200, 255)
    LED_ORANGE = QColor(255, 150, 50)
    AMBER      = QColor(255, 171, 71)
    LABEL_DIM  = QColor(110, 110, 130)
    PEAK       = QColor(245, 245, 245)

    BAR_H      = 9
    ROW_H      = 26    # bar + generous vertical gap to the next row
    LEGEND_H   = 9
    LABEL_W    = 32

    def __init__(self, parent=None,
                 db_min: float = -140.0,
                 db_s9: float = -73.0,
                 db_max: float = -43.0):
        super().__init__(parent)
        self._db_min = db_min
        self._db_s9 = db_s9
        self._db_max = db_max
        self._value = db_min
        self._peak = db_min
        self._peak_decay_dB_per_s = 6.0
        self._dbfs_to_dbm_offset = -53.0

        # Placeholder state for TX rows; will be wired up when TX comes.
        self._pwr_w = 0.0
        self._swr = 1.0
        self._mic_db = -60.0
        self._agc_db = 0.0
        self._tx_active = False
        self._agc_active = False

        # 5 rows × ROW_H + top legend + bottom margin
        ideal_h = self.LEGEND_H + 5 * self.ROW_H + 10
        # Min width relaxed to 200 so operators can dock the Meters
        # panel into a narrow column. Bar segments scale with width.
        self.setMinimumSize(200, ideal_h)
        self.setMaximumHeight(ideal_h + 10)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Transparent background so the meter floats on the panel color
        # instead of showing a darker rectangle around it.
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")

        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick_decay)
        self._decay_timer.start(50)

    def set_level_dbfs(self, dbfs: float):
        dbm = dbfs + self._dbfs_to_dbm_offset
        self._value = dbm
        if dbm > self._peak:
            self._peak = dbm
        self.update()

    def set_pwr_w(self, w: float): self._pwr_w = float(w); self.update()
    def set_swr(self, swr: float): self._swr = float(swr); self.update()
    def set_mic_db(self, db: float): self._mic_db = float(db); self.update()
    def set_agc_db(self, db: float): self._agc_db = float(db); self.update()
    def set_tx_active(self, on: bool): self._tx_active = bool(on); self.update()
    def set_agc_active(self, on: bool): self._agc_active = bool(on); self.update()

    def _frac_s(self, db: float) -> float:
        db = max(self._db_min, min(self._db_max, db))
        return (db - self._db_min) / (self._db_max - self._db_min)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        # No overall background fill — we want the parent panel color
        # showing through; each bar draws its own sunken track for
        # contrast with the lit segments.

        w, h = self.width(), self.height()
        pad_x = 4
        bar_x = pad_x + self.LABEL_W
        bar_w = w - bar_x - 30   # leave room for right-edge unit label

        font = QFont()
        font.setFamilies([theme.FONT_MONO_FAMILY, "Consolas"])
        font.setPointSize(7)
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)

        # ── Top scale legend (drawn once for the S-meter row) ─────────
        legend_y = 8
        # S-units 1/3/5/7/9 in amber
        for s in (1, 3, 5, 7, 9):
            db = self._db_s9 - (9 - s) * 6.0
            x = bar_x + self._frac_s(db) * bar_w
            lbl = str(s)
            tw = fm.horizontalAdvance(lbl)
            p.setPen(QPen(self.AMBER, 1))
            p.drawText(QPointF(x - tw / 2, legend_y), lbl)
        # over-9 in red
        for extra, lbl in ((10, "20"), (20, "40"), (30, "60")):
            db = self._db_s9 + extra
            if db > self._db_max:
                continue
            x = bar_x + self._frac_s(db) * bar_w
            tw = fm.horizontalAdvance(lbl)
            p.setPen(QPen(self.LED_RED, 1))
            p.drawText(QPointF(x - tw / 2, legend_y), lbl)
        # 'dB' marker
        p.setPen(QPen(self.LED_RED, 1))
        p.drawText(QPointF(bar_x + bar_w + 2, legend_y), "dB")

        row_y = legend_y + 4

        # ── S row (live RX) ──────────────────────────────────────────
        s_lit = self._frac_s(self._value)
        s_peak = self._frac_s(self._peak)
        s9_frac = (self._db_s9 - self._db_min) / (self._db_max - self._db_min)
        self._draw_row(p, row_y, bar_x, bar_w, "S", s_lit, s_peak,
                       s9_frac, dim=False)
        row_y += self.ROW_H

        # ── PWR row ──────────────────────────────────────────────────
        # 0..10 W (HL2 max). Color: green→yellow→red beyond 8 W
        pwr_frac = min(1.0, max(0.0, self._pwr_w / 10.0))
        self._draw_row(p, row_y, bar_x, bar_w, "PWR", pwr_frac, None,
                       0.8, dim=not self._tx_active)
        # Right-side unit label
        self._right_unit(p, row_y, w, "W")
        row_y += self.ROW_H

        # ── SWR row (non-linear) ────────────────────────────────────
        swr_frac = self._swr_to_frac(self._swr)
        self._draw_row(p, row_y, bar_x, bar_w, "SWR", swr_frac, None,
                       0.4, dim=not self._tx_active)
        row_y += self.ROW_H

        # ── MIC row (-60..+5 dB) ────────────────────────────────────
        mic_frac = (self._mic_db + 60) / 65.0
        mic_frac = min(1.0, max(0.0, mic_frac))
        self._draw_row(p, row_y, bar_x, bar_w, "MIC", mic_frac, None,
                       0.85, dim=not self._tx_active)
        self._right_unit(p, row_y, w, "dB")
        row_y += self.ROW_H

        # ── AGC row (action 0..30 dB) ───────────────────────────────
        agc_frac = min(1.0, max(0.0, self._agc_db / 30.0))
        self._draw_row(p, row_y, bar_x, bar_w, "AGC", agc_frac, None,
                       1.0, dim=not self._agc_active,
                       lit_color=self.LED_BLUE)
        self._right_unit(p, row_y, w, "dB")

    def _draw_row(self, p, row_y, bar_x, bar_w, label, lit_frac,
                  peak_frac, green_end, dim=False, lit_color=None):
        # Label on left
        lab_font = QFont()
        lab_font.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
        lab_font.setPointSize(8)
        lab_font.setBold(True)
        p.setFont(lab_font)
        p.setPen(QPen(self.LABEL_DIM if dim else self.AMBER, 1))
        p.drawText(QPointF(4, row_y + self.BAR_H), label)

        # Sunken track
        p.setBrush(QColor(10, 10, 14))
        p.setPen(QPen(QColor(26, 26, 32), 1))
        p.drawRect(QRectF(bar_x - 1, row_y - 1, bar_w + 2, self.BAR_H + 2))

        # LEDs — fewer segments (since rows are thinner)
        n = 36
        gap = 1
        seg_w = max(2.0, (bar_w - (n - 1) * gap) / n)
        for i in range(n):
            sx = bar_x + i * (seg_w + gap)
            seg_center = (i + 0.5) / n
            if lit_color is not None:
                base = lit_color
            elif seg_center < green_end - 0.04:
                base = self.LED_GREEN
            elif seg_center < green_end + 0.02:
                base = self.LED_YELLOW
            else:
                base = self.LED_RED
            on = (seg_center < lit_frac) and not dim
            if on:
                p.setBrush(base)
                p.setPen(Qt.NoPen)
                p.drawRect(QRectF(sx, row_y, seg_w, self.BAR_H))
            else:
                g = QColor(base)
                g.setAlpha(35 if not dim else 22)
                p.setBrush(g)
                p.setPen(Qt.NoPen)
                p.drawRect(QRectF(sx, row_y, seg_w, self.BAR_H))

        if peak_frac is not None and not dim and peak_frac > 0:
            px = bar_x + peak_frac * bar_w
            p.setPen(QPen(self.PEAK, 1.5))
            p.drawLine(QPointF(px, row_y - 1),
                       QPointF(px, row_y + self.BAR_H + 1))

    def _right_unit(self, p, row_y, w, text):
        font = QFont()
        font.setFamilies([theme.FONT_MONO_FAMILY, "Consolas"])
        font.setPointSize(7)
        p.setFont(font)
        p.setPen(QPen(self.LABEL_DIM, 1))
        p.drawText(QPointF(w - 22, row_y + self.BAR_H), text)

    @staticmethod
    def _swr_to_frac(swr: float) -> float:
        if swr <= 1.0:
            return 0.0
        if swr <= 3.0:
            return (swr - 1.0) / 2.0 * 0.7
        return 0.7 + min(1.0, (swr - 3.0) / 6.0) * 0.3

    def _tick_decay(self):
        dt_s = 0.05
        decay = self._peak_decay_dB_per_s * dt_s
        if self._peak > self._value + decay:
            self._peak -= decay
        else:
            self._peak = self._value
        self.update()


# ── Legacy bar SMeter kept for backward compatibility ─────────────────
class SMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(50)
        self.setMinimumWidth(280)
        self._dbfs = -120.0
        self._peak_dbfs = -120.0
        self._peak_hold_decay = 0.6
        self._s9_dbfs = -20.0
        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._tick_decay)
        self._decay_timer.start(50)

    def set_level_dbfs(self, dbfs: float):
        self._dbfs = dbfs
        if dbfs > self._peak_dbfs:
            self._peak_dbfs = dbfs
        self.update()

    def _tick_decay(self):
        self._peak_dbfs -= self._peak_hold_decay
        if self._peak_dbfs < self._dbfs:
            self._peak_dbfs = self._dbfs
        self.update()

    def _dbfs_to_fraction(self, dbfs: float) -> float:
        s0 = self._s9_dbfs - 54.0
        s9_30 = self._s9_dbfs + 30.0
        return float(np.clip((dbfs - s0) / (s9_30 - s0), 0.0, 1.0))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        p.fillRect(self.rect(), QColor(12, 20, 32))
        bar_x, bar_y, bar_w, bar_h = 10, h // 2 - 6, w - 20, 14
        p.setPen(QPen(QColor(40, 60, 80), 1))
        p.setBrush(QColor(18, 28, 42))
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 3, 3)
        s9_frac = self._dbfs_to_fraction(self._s9_dbfs)
        s9_x = bar_x + int(bar_w * s9_frac)
        p.setPen(QPen(QColor(255, 170, 80, 180), 1, Qt.DashLine))
        p.drawLine(s9_x, bar_y - 3, s9_x, bar_y + bar_h + 3)
        level_frac = self._dbfs_to_fraction(self._dbfs)
        fill_w = int(bar_w * level_frac)
        grad = QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
        grad.setColorAt(0.0, QColor(30, 180, 220))
        grad.setColorAt(s9_frac * 0.98, QColor(94, 200, 255))
        grad.setColorAt(min(s9_frac + 0.02, 1.0), QColor(230, 180, 60))
        grad.setColorAt(1.0, QColor(240, 80, 60))
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(bar_x + 1, bar_y + 1, max(0, fill_w - 1), bar_h - 2), 2, 2)
        peak_frac = self._dbfs_to_fraction(self._peak_dbfs)
        peak_x = bar_x + int(bar_w * peak_frac)
        p.setPen(QPen(QColor(255, 255, 255, 220), 2))
        p.drawLine(peak_x, bar_y - 2, peak_x, bar_y + bar_h + 2)
