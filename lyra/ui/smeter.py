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
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
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
        self._dbfs_to_dbm_offset = -53.0  # -20 dBFS ≈ S9

        # Readout state (driven by the owning panel via setters)
        self._freq_hz = 0
        self._band_label = ""
        self._mode_label = ""

        self.setMinimumSize(290, 160)
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
        self.setMinimumSize(280, ideal_h)
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
