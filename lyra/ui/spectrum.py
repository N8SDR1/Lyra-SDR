"""Spectrum + waterfall widgets. Custom-painted, no matplotlib."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, Signal
from PySide6.QtGui import (
    QColor, QImage, QLinearGradient, QPainter, QPen, QPolygonF,
)

# Graphics backend base class (QWidget or QOpenGLWidget) — selected at
# import time by gfx.py from the user's Visuals preference. Uses the
# same QPainter code on either backend; OpenGL just moves rasterization
# to the GPU so resize/fullscreen doesn't stall the demod thread.
from .gfx import ACCELERATED_BASE as _PaintedWidget

BG = QColor(12, 20, 32)
GRID = QColor(40, 60, 80)
AXIS = QColor(170, 204, 238)
TRACE = QColor(94, 200, 255)
TRACE_FILL = QColor(94, 200, 255, 60)


class SpectrumWidget(_PaintedWidget):
    """FFT magnitude line (dBFS/Hz) with filled gradient beneath."""

    clicked_freq = Signal(float)
    # Payload: (abs_freq_hz, shift_held, global_position). The globalPos
    # lets the panel anchor a context menu at the click site — the panel
    # owns Radio and decides "add notch here / remove nearest / clear
    # all" so the widget stays pure. Shift-held flag preserves the old
    # quick-gesture (shift+right = remove nearest) for folks who
    # learned it; plain right-click now opens the menu instead of
    # silently adding a notch (which surprised operators who were just
    # trying to poke around).
    right_clicked_freq = Signal(float, bool, QPoint)
    wheel_at_freq = Signal(float, int)
    # Fires when the mouse wheel scrolls over empty spectrum (not over
    # a notch). Positive = zoom in, negative = zoom out. SpectrumPanel
    # routes this to Radio.zoom_step.
    wheel_zoom    = Signal(int)
    notch_q_drag = Signal(float, float)
    spot_clicked = Signal(float)                      # freq near a spot
    # Live-fired while the user drags a passband edge. Payload is the
    # proposed RX BW in Hz (50 Hz granularity, 50..15000 clamp). The
    # panel routes this to radio.set_rx_bw(current_mode, bw).
    passband_edge_drag = Signal(int)
    # Band-plan landmark click → panel tunes freq + switches mode.
    landmark_clicked = Signal(int, str)               # (freq_hz, mode)
    # Y-axis drag to adjust spectrum dB scale. Emits proposed
    # (min_db, max_db); panel forwards to Radio.set_spectrum_db_range.
    db_scale_drag = Signal(float, float)

    NOTCH_HIT_PX = 14
    SPOT_HIT_PX = 8
    PASSBAND_HIT_PX = 6         # clickable halo around each dashed edge

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spec_db: np.ndarray | None = None
        self._min_db = -110.0
        self._max_db = -20.0
        self._center_hz = 0.0
        self._span_hz = 48000.0
        self._notches: list[tuple[float, float]] = []
        self._spots: list[dict] = []
        # Used for the age-fade on spot boxes (newer spots at full alpha,
        # older ones fading toward 30%). Kept in sync with Radio via
        # set_spot_lifetime_s; defaults to a sensible 10 min.
        self._spot_lifetime_s: int = 600
        # Mode filter: if non-empty, only render spots whose uppercase
        # mode is in this set. Updated via set_spot_mode_filter() which
        # accepts SDRLogger+-style CSV ("FT8,CW,SSB").
        self._spot_mode_filter: set[str] = set()
        # RX filter passband overlay — (low_offset_hz, high_offset_hz)
        # relative to the tuned center. Drawn as a translucent cyan
        # rect so the operator can see which signals are IN vs OUT of
        # the demod filter. Set by Radio.passband_changed.
        self._passband_lo_hz: int = 0
        self._passband_hi_hz: int = 0
        # Noise-floor reference line. None = hidden; otherwise draw a
        # muted dashed horizontal line at the corresponding y-pixel.
        # Radio pushes updates via Radio.noise_floor_changed; the
        # magic value -999 toggles it off (covers the "disabled"
        # broadcast from set_noise_floor_enabled(False)).
        self._noise_floor_db: float | None = None
        # Peak-markers overlay: per-bin peak-hold buffer. Only the
        # bins inside the RX passband are rendered, so the feature
        # doesn't clutter the whole spectrum with irrelevant peaks.
        # Decays linearly at `_peak_markers_decay_dbps` dB/second.
        self._peak_markers_enabled: bool = False
        self._peak_markers_decay_dbps: float = 10.0
        self._peak_markers_style: str = "dots"   # "line" / "dots" / "triangles"
        self._peak_markers_show_db: bool = False
        self._peak_hold_db: np.ndarray | None = None
        self._peak_last_ts: float | None = None
        # User-picked colors. Empty trace color falls back to the
        # default TRACE constant. Segment overrides are layered on top
        # of band_plan.SEGMENT_COLORS at paint time.
        self._user_trace_color: str = ""
        self._user_segment_colors: dict[str, str] = {}
        self._user_nf_color: str = ""   # NF-line override, "" = default sage
        self._user_peak_color: str = "" # peak marker override, "" = default amber
        # Band-plan overlay state — which region, which toggles.
        # Drawn at the TOP of the widget in a ~22 px band above the
        # spectrum trace (segments in top ~10 px, landmarks ~12 px).
        self._band_plan_region: str = "NONE"
        self._show_band_segments: bool = True
        self._show_band_landmarks: bool = True
        self._show_band_edge_warn: bool = True
        self._drag_notch: tuple[float, float] | None = None
        self._drag_start_y: int = 0
        # Active passband-edge drag: None, "lo", or "hi"
        self._drag_pb_edge: str | None = None
        # Active Y-axis dB-scale drag: stores (start_mouse_y,
        # start_min_db, start_max_db, mode) where mode is
        # "min" / "max" / "pan". Set when user press-and-holds in
        # the rightmost ~50 px strip (the dB labels area).
        self._drag_db_scale: tuple[int, float, float, str] | None = None
        # Proposed-range emits live during drag — panel forwards to Radio.
        # (Not a Qt Signal here because SpectrumWidget doesn't have the
        # decoration import at that spot; we emit via an existing signal.)
        self.setMinimumHeight(140)
        self.setCursor(Qt.CrossCursor)
        # Enable hover tracking so the cursor can hint "resize here"
        # when hovering an edge. Notch cursor logic already kicks in
        # on press, so this only affects the passband edges.
        self.setMouseTracking(True)

    def set_spots(self, spots: list[dict]):
        self._spots = list(spots)
        self.update()

    def set_spot_lifetime_s(self, seconds: int):
        """Drives the age-fade on spot boxes. Older spots fade toward
        30% alpha as they approach the lifetime limit. 0 = no fade."""
        self._spot_lifetime_s = max(0, int(seconds))
        self.update()

    def set_passband(self, low_hz: int, high_hz: int):
        """Set the RX filter passband overlay in Hz offsets from the
        tuned carrier. Low < High. (0, 0) hides the overlay."""
        self._passband_lo_hz = int(low_hz)
        self._passband_hi_hz = int(high_hz)
        self.update()

    def set_spectrum_trace_color(self, hex_str: str):
        """Override the cyan/yellow trace line color. Empty string
        reverts to the default from theme.TRACE."""
        self._user_trace_color = str(hex_str or "")
        self.update()

    def set_segment_color_overrides(self, overrides: dict):
        """Merge a {kind: hex} dict of per-segment color overrides
        (CW/DIG/SSB/FM). Absent keys use band_plan defaults."""
        self._user_segment_colors = {
            str(k).upper(): str(v) for k, v in dict(overrides or {}).items() if v}
        self.update()

    def set_noise_floor_color(self, hex_str: str):
        """Noise-floor reference line color override. Empty string
        reverts to the default sage green."""
        self._user_nf_color = str(hex_str or "")
        self.update()

    def set_peak_markers_color(self, hex_str: str):
        """Peak-marker color override. Empty string reverts to default
        amber (255, 190, 90)."""
        self._user_peak_color = str(hex_str or "")
        self.update()

    def set_peak_markers_enabled(self, on: bool):
        """Toggle the in-passband peak-hold overlay. Disabling clears
        the peak buffer so a later re-enable starts clean."""
        self._peak_markers_enabled = bool(on)
        if not self._peak_markers_enabled:
            self._peak_hold_db = None
            self._peak_last_ts = None
        self.update()

    def set_peak_markers_decay_dbps(self, dbps: float):
        self._peak_markers_decay_dbps = float(dbps)

    def set_peak_markers_style(self, name: str):
        name = (name or "dots").strip().lower()
        if name not in ("line", "dots", "triangles"):
            name = "dots"
        self._peak_markers_style = name
        self.update()

    def set_peak_markers_show_db(self, on: bool):
        self._peak_markers_show_db = bool(on)
        self.update()

    def set_band_plan_region(self, region_id: str):
        self._band_plan_region = str(region_id) or "NONE"
        self.update()

    def set_band_plan_show_segments(self, on: bool):
        self._show_band_segments = bool(on)
        self.update()

    def set_band_plan_show_landmarks(self, on: bool):
        self._show_band_landmarks = bool(on)
        self.update()

    def set_band_plan_show_edge_warn(self, on: bool):
        self._show_band_edge_warn = bool(on)
        self.update()

    def set_noise_floor_db(self, dbfs: float):
        """Radio emits this at ~6 Hz. -999 means 'marker disabled' —
        we interpret it as 'hide' without a separate signal."""
        if dbfs <= -500.0:
            self._noise_floor_db = None
        else:
            self._noise_floor_db = float(dbfs)
        self.update()

    def _passband_edge_px(self) -> tuple[int | None, int | None]:
        """Return (x_lo, x_hi) pixel positions of the drawn passband
        edges, or (None, None) if the passband is hidden / off-screen."""
        if (self._passband_hi_hz <= self._passband_lo_hz
                or self._span_hz <= 0 or self.width() <= 0):
            return (None, None)
        hz_per_px = self._span_hz / self.width()
        center_x = self.width() / 2
        x_lo = int(center_x + self._passband_lo_hz / hz_per_px)
        x_hi = int(center_x + self._passband_hi_hz / hz_per_px)
        return (x_lo, x_hi)

    DB_SCALE_ZONE_PX = 50   # rightmost strip that grabs dB-scale drag

    def _db_scale_mode_at(self, x: float, y: float) -> str | None:
        """If cursor is in the right-edge dB-labels zone, return the
        drag mode:
          - "max" (top third)   → drag adjusts only max_db
          - "min" (bottom third) → drag adjusts only min_db
          - "pan" (middle third) → drag pans the whole range
        Otherwise None."""
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return None
        if x < w - self.DB_SCALE_ZONE_PX or x > w:
            return None
        # Avoid the very top strip where band-plan overlays live
        if y < 24:
            return None
        third = h / 3
        if y < third:
            return "max"
        if y > 2 * third:
            return "min"
        return "pan"

    def _landmark_at(self, x: float, y: float):
        """Return the landmark dict clicked at (x, y), or None.
        Matches the paint geometry: landmark triangles live in the
        strip just under the sub-band-segment band (y=10..22 when
        both strips are on, y=0..12 when only landmarks are shown).
        Click tolerance ±6 px horizontally."""
        if self._band_plan_region == "NONE" or not self._show_band_landmarks:
            return None
        if self._span_hz <= 0 or self.width() <= 0:
            return None
        BAND_STRIP_H = 10
        LANDMARK_STRIP_H = 12
        tri_y_top = BAND_STRIP_H if self._show_band_segments else 0
        tri_y_bot = tri_y_top + LANDMARK_STRIP_H
        if not (tri_y_top <= y <= tri_y_bot):
            return None
        from lyra import band_plan as _bp
        marks = _bp.visible_landmarks(
            self._band_plan_region, self._center_hz, self._span_hz)
        hz_per_px = self._span_hz / self.width()
        center_x = self.width() / 2
        best = None
        best_dx = None
        for lm in marks:
            mx = center_x + (lm["freq"] - self._center_hz) / hz_per_px
            dx = abs(x - mx)
            if dx <= 6.0 and (best_dx is None or dx < best_dx):
                best, best_dx = lm, dx
        return best

    def _passband_edge_at_x(self, x: float) -> str | None:
        """Return "lo", "hi", or None depending on whether `x` is
        within PASSBAND_HIT_PX of an edge. Used both for hover cursor
        and press-to-drag."""
        x_lo, x_hi = self._passband_edge_px()
        if x_lo is None:
            return None
        if abs(x - x_lo) <= self.PASSBAND_HIT_PX:
            return "lo"
        if abs(x - x_hi) <= self.PASSBAND_HIT_PX:
            return "hi"
        return None

    def _proposed_bw_from_drag(self, x: float) -> int | None:
        """Translate a drag cursor x-position into a new proposed RX
        BW in Hz, clamped + quantized. Returns None if the result
        would be nonsensical (e.g. inverted edges for SSB modes)."""
        if self._span_hz <= 0 or self.width() <= 0:
            return None
        hz_per_px = self._span_hz / self.width()
        center_x = self.width() / 2
        offset_hz = (x - center_x) * hz_per_px

        lo, hi = self._passband_lo_hz, self._passband_hi_hz
        edge = self._drag_pb_edge
        if edge is None:
            return None

        # Determine mode-geometry from the passband offsets:
        #   USB-style: lo == 0, hi > 0  → drag hi only; new_bw = new_hi
        #   LSB-style: hi == 0, lo < 0  → drag lo only; new_bw = -new_lo
        #   Symmetric: lo == -hi        → either edge drags both sides;
        #                                   new_bw = 2 * |offset|
        #   CW: lo, hi both offset around pitch; treat as symmetric
        #       around the pitch center so drag widens the box evenly.
        if lo == 0 and hi > 0:
            # USB / DIGU — only "hi" edge meaningful
            if edge != "hi":
                return None
            bw = int(round(offset_hz))
        elif hi == 0 and lo < 0:
            # LSB / DIGL — only "lo" edge meaningful
            if edge != "lo":
                return None
            bw = int(round(-offset_hz))
        elif lo < 0 and hi > 0 and abs(lo + hi) <= max(5, abs(lo) // 20):
            # Symmetric around the carrier — either edge grows BW
            bw = int(round(2 * abs(offset_hz)))
        else:
            # CW-style asymmetric: pitch center = (lo + hi) / 2
            pitch_center = (lo + hi) / 2
            bw = int(round(2 * abs(offset_hz - pitch_center)))

        # Clamp + quantize to 50 Hz steps
        bw = max(50, min(15000, bw))
        bw = 50 * ((bw + 25) // 50)
        return bw

    def set_spot_mode_filter(self, csv_or_set):
        """Accept either the raw CSV string (parsed here) or a pre-built
        set of uppercase mode codes. Empty = no filter, render all spots.
        SSB in the CSV auto-expands to match USB/LSB/SSB."""
        if isinstance(csv_or_set, (set, frozenset)):
            self._spot_mode_filter = {m.upper() for m in csv_or_set}
        else:
            csv = str(csv_or_set or "").strip()
            if not csv:
                self._spot_mode_filter = set()
            else:
                raw = [m.strip().upper() for m in csv.split(",") if m.strip()]
                out: set[str] = set()
                for m in raw:
                    if m == "SSB":
                        out.update(("SSB", "USB", "LSB"))
                    else:
                        out.add(m)
                self._spot_mode_filter = out
        self.update()

    def set_notches(self, pairs: list[tuple[float, float]]):
        self._notches = list(pairs)
        self.update()

    # Legacy single-freq API retained for backward compatibility
    def set_notch_freqs(self, freqs: list[float]):
        self._notches = [(f, 30.0) for f in freqs]
        self.update()

    def _freq_at_x(self, x: float) -> float:
        if self.width() <= 0 or self._span_hz <= 0:
            return self._center_hz
        frac = x / self.width()
        return self._center_hz + (frac - 0.5) * self._span_hz

    def _notch_half_px(self, freq: float, q: float) -> int:
        """Match the visual band width so hit-testing picks up clicks
        anywhere inside the shaded region, not just the 2-pixel center line.
        """
        if self._span_hz <= 0 or self.width() <= 0:
            return self.NOTCH_HIT_PX
        hz_per_px = self._span_hz / self.width()
        bb_freq = max(abs(freq - self._center_hz), self._span_hz * 0.02)
        notch_bw_hz = (bb_freq / q) * 3.0
        visual_half = int(notch_bw_hz / hz_per_px / 2)
        return max(self.NOTCH_HIT_PX, visual_half)

    def _nearest_notch_at_x(self, x: float) -> tuple[float, float] | None:
        if not self._notches or self._span_hz <= 0:
            return None
        best = None
        best_px = None
        for freq, q in self._notches:
            nf = (freq - self._center_hz) / self._span_hz + 0.5
            if not (0.0 <= nf <= 1.0):
                continue
            nx = nf * self.width()
            px = abs(nx - x)
            hit_radius = self._notch_half_px(freq, q)
            if px <= hit_radius and (best_px is None or px < best_px):
                best_px = px
                best = (freq, q)
        return best

    def mousePressEvent(self, event):
        if self.width() <= 0:
            return
        x = event.position().x()
        y = event.position().y()
        freq = self._freq_at_x(x)
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if event.button() == Qt.LeftButton:
            # Priority: landmark → dB-scale drag zone → passband-edge
            # drag → notch-Q drag → tune. dB zone is the rightmost
            # 50 px strip (below the top overlays).
            lm = self._landmark_at(x, y)
            if lm is not None:
                self.landmark_clicked.emit(
                    int(lm["freq"]), str(lm["mode"]))
                return
            db_mode = self._db_scale_mode_at(x, y)
            if db_mode is not None:
                self._drag_db_scale = (
                    int(y), float(self._min_db), float(self._max_db),
                    db_mode)
                self.setCursor(Qt.SizeVerCursor)
                return
            edge = self._passband_edge_at_x(x)
            if edge is not None:
                self._drag_pb_edge = edge
                self.setCursor(Qt.SizeHorCursor)
                return
            hit = self._nearest_notch_at_x(x)
            if hit is not None:
                self._drag_notch = hit
                self._drag_start_y = int(event.position().y())
                self.setCursor(Qt.SizeVerCursor)
                return
            self.clicked_freq.emit(freq)
        elif event.button() == Qt.RightButton:
            gpos = event.globalPosition().toPoint()
            self.right_clicked_freq.emit(freq, shift, gpos)

    def mouseMoveEvent(self, event):
        x = event.position().x()
        # dB-scale drag — update spectrum range from vertical mouse delta
        if self._drag_db_scale is not None:
            start_y, start_min, start_max, mode = self._drag_db_scale
            dy = int(event.position().y()) - start_y
            # Pixel delta → dB delta. Full widget height ≈ full span
            # so map dy proportionally. Invert so dragging UP raises
            # the corresponding edge (intuitive "lift the scale").
            h = max(1, self.height())
            span = start_max - start_min
            db_delta = -dy * (span / h)
            if mode == "max":
                new_max = start_max + db_delta
                new_min = start_min
            elif mode == "min":
                new_max = start_max
                new_min = start_min + db_delta
            else:   # "pan" — shift both edges together
                new_max = start_max + db_delta
                new_min = start_min + db_delta
            # Clamp: keep both edges within [-150, 0] dBFS and
            # preserve at least 3 dB of span
            new_min = max(-150.0, min(-3.0, new_min))
            new_max = max(new_min + 3.0, min(0.0, new_max))
            self.db_scale_drag.emit(float(new_min), float(new_max))
            return
        # Passband-edge drag — live-update RX BW as the user pulls
        if self._drag_pb_edge is not None:
            proposed = self._proposed_bw_from_drag(x)
            if proposed is not None:
                self.passband_edge_drag.emit(proposed)
            return
        # Notch-Q drag (vertical motion)
        if self._drag_notch is not None:
            freq, start_q = self._drag_notch
            dy = self._drag_start_y - int(event.position().y())
            if abs(dy) < 4:
                return
            dy_eff = dy - (4 if dy > 0 else -4)
            new_q = max(2.0, min(500.0, start_q * (1.015 ** dy_eff)))
            self.notch_q_drag.emit(freq, new_q)
            return
        # Hover cursor hint — only update when not already dragging so
        # we don't fight Qt's drag-cursor state. Landmarks get a
        # pointing-hand cursor to telegraph "this is clickable."
        y = event.position().y()
        if self._landmark_at(x, y) is not None:
            self.setCursor(Qt.PointingHandCursor)
        elif self._db_scale_mode_at(x, y) is not None:
            self.setCursor(Qt.SizeVerCursor)
        elif self._passband_edge_at_x(x) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._drag_db_scale is not None:
            self._drag_db_scale = None
            self.setCursor(Qt.CrossCursor)
            return
        if self._drag_pb_edge is not None:
            self._drag_pb_edge = None
            self.setCursor(Qt.CrossCursor)
            return
        if self._drag_notch is not None:
            self._drag_notch = None
            self.setCursor(Qt.CrossCursor)

    def wheelEvent(self, event):
        if self.width() <= 0:
            return
        x = event.position().x()
        freq = self._freq_at_x(x)
        delta_units = event.angleDelta().y() // 120
        if delta_units == 0:
            super().wheelEvent(event)
            return
        # If the wheel is over a notch tick, adjust that notch's Q
        # (preserves the classic "hover a notch and wheel" gesture).
        # Otherwise, wheel = zoom the panadapter.
        if self._nearest_notch_at_x(x) is not None:
            self.wheel_at_freq.emit(freq, int(delta_units))
        else:
            self.wheel_zoom.emit(int(delta_units))
        event.accept()

    def set_spectrum(self, spec_db: np.ndarray, center_hz: float, span_hz: float):
        self._spec_db = spec_db
        self._center_hz = center_hz
        self._span_hz = span_hz
        # Maintain peak-hold buffer — linear dB/sec decay, clamped by
        # the live spectrum (so a peak can never be below the current
        # signal level at that bin).
        if self._peak_markers_enabled and spec_db is not None and spec_db.size > 0:
            import time as _time
            now = _time.monotonic()
            if (self._peak_hold_db is None
                    or self._peak_hold_db.shape != spec_db.shape):
                self._peak_hold_db = spec_db.astype(np.float32).copy()
                self._peak_last_ts = now
            else:
                dt = max(0.005, now - (self._peak_last_ts or now))
                self._peak_last_ts = now
                # Decay then clamp-up to current level
                self._peak_hold_db -= self._peak_markers_decay_dbps * dt
                np.maximum(self._peak_hold_db, spec_db,
                           out=self._peak_hold_db)
        self.update()

    def set_db_range(self, min_db: float, max_db: float):
        self._min_db = min_db
        self._max_db = max_db
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)

        w = self.width()
        h = self.height()

        # Grid
        p.setPen(QPen(GRID, 1))
        for i in range(1, 10):
            y = int(h * i / 10)
            p.drawLine(0, y, w, y)
        for i in range(1, 10):
            x = int(w * i / 10)
            p.drawLine(x, 0, x, h)

        # ── Band-plan overlay ─────────────────────────────────────
        # Top strip: colored sub-band segments (CW / DIG / SSB / FM)
        # per the active region. Drawn BEFORE the trace so the yellow
        # trace remains legible through the thin strip. Second strip
        # just below hosts landmark triangles (FT8 / FT4 / WSPR).
        # Both are skipped when region == "NONE" or individually off.
        BAND_STRIP_H = 10         # colored segment band
        LANDMARK_STRIP_H = 12     # landmark triangles + labels
        top_reserve = 0
        if (self._band_plan_region != "NONE"
                and self._span_hz > 0 and w > 0):
            from lyra import band_plan as _bp
            if self._show_band_segments:
                top_reserve += BAND_STRIP_H
                # Draw segments
                segs = _bp.visible_segments(
                    self._band_plan_region,
                    self._center_hz, self._span_hz)
                hz_per_px = self._span_hz / w
                center_x = w / 2
                for seg, seg_lo, seg_hi in segs:
                    x0 = int(center_x + (seg_lo - self._center_hz) / hz_per_px)
                    x1 = int(center_x + (seg_hi - self._center_hz) / hz_per_px)
                    x0 = max(0, x0); x1 = min(w, x1)
                    if x1 <= x0:
                        continue
                    # User override takes precedence over the
                    # band-plan default; fall back to default, then to
                    # a neutral teal if a segment kind is unknown.
                    color_hex = (self._user_segment_colors.get(seg["kind"])
                                 or _bp.SEGMENT_COLORS.get(seg["kind"],
                                                           "#5c8caa"))
                    col = QColor(color_hex)
                    col.setAlpha(210)
                    p.fillRect(x0, 0, x1 - x0, BAND_STRIP_H, col)
                    # Label if there's room (≥ 24 px wide)
                    if x1 - x0 >= 24:
                        p.setPen(QPen(QColor(240, 240, 240, 220), 1))
                        from PySide6.QtGui import QFont as _QFont
                        lbl_font = _QFont()
                        lbl_font.setPointSize(7)
                        lbl_font.setBold(True)
                        p.setFont(lbl_font)
                        p.drawText(x0 + 3, BAND_STRIP_H - 2, seg["label"])
            if self._show_band_landmarks:
                top_reserve += LANDMARK_STRIP_H
                marks = _bp.visible_landmarks(
                    self._band_plan_region,
                    self._center_hz, self._span_hz)
                hz_per_px = self._span_hz / w
                center_x = w / 2
                tri_y = BAND_STRIP_H if self._show_band_segments else 0
                from PySide6.QtGui import QPolygonF, QFont as _QFont2
                from PySide6.QtCore import QPointF
                lbl_font = _QFont2()
                lbl_font.setPointSize(7)
                lbl_font.setBold(True)
                p.setFont(lbl_font)
                for lm in marks:
                    mx = int(center_x + (lm["freq"] - self._center_hz) / hz_per_px)
                    if not (0 <= mx <= w):
                        continue
                    # Downward-pointing triangle
                    tri = QPolygonF([
                        QPointF(mx - 4, tri_y + 1),
                        QPointF(mx + 4, tri_y + 1),
                        QPointF(mx,     tri_y + 6),
                    ])
                    p.setPen(QPen(QColor(255, 215, 0, 200), 1))
                    p.setBrush(QColor(255, 215, 0, 140))
                    p.drawPolygon(tri)
                    # Small label to the right of the triangle
                    p.setPen(QPen(QColor(255, 215, 0, 220), 1))
                    p.drawText(mx + 6, tri_y + LANDMARK_STRIP_H - 1,
                               lm["label"])
            # Band-edge warnings — red vertical line + label at any
            # band edge that's inside the current visible span. Same
            # for region edges (segment boundaries not shown as edges
            # — only full-band edges count as "don't TX past here").
            if self._show_band_edge_warn:
                lo_vis = self._center_hz - self._span_hz / 2
                hi_vis = self._center_hz + self._span_hz / 2
                hz_per_px = self._span_hz / w
                center_x = w / 2
                for b in _bp.get_region(self._band_plan_region)["bands"]:
                    for edge_hz in (b["low"], b["high"]):
                        if not (lo_vis <= edge_hz <= hi_vis):
                            continue
                        ex = int(center_x +
                                 (edge_hz - self._center_hz) / hz_per_px)
                        p.setPen(QPen(QColor(255, 80, 80, 220), 2,
                                      Qt.DashLine))
                        p.drawLine(ex, 0, ex, h)
                        p.setPen(QPen(QColor(255, 120, 120, 220), 1))
                        from PySide6.QtGui import QFont as _QFont3
                        ef = _QFont3()
                        ef.setPointSize(8)
                        ef.setBold(True)
                        p.setFont(ef)
                        p.drawText(ex + 3,
                                   h - 22,
                                   f"{b['name']} EDGE")

        # RX passband overlay — drawn UNDER the trace so the spectrum
        # line remains fully legible. Translucent cyan fill + dashed
        # border lines at the passband edges.
        if (self._passband_hi_hz > self._passband_lo_hz
                and self._span_hz > 0 and w > 0):
            hz_per_px = self._span_hz / w
            center_x = w / 2
            x_lo = int(center_x + self._passband_lo_hz / hz_per_px)
            x_hi = int(center_x + self._passband_hi_hz / hz_per_px)
            # Clip to widget bounds
            x_lo = max(0, min(w, x_lo))
            x_hi = max(0, min(w, x_hi))
            if x_hi > x_lo:
                fill = QColor(0, 229, 255, 28)     # faint cyan tint
                edge = QColor(0, 229, 255, 140)    # slightly brighter edges
                p.fillRect(x_lo, 0, x_hi - x_lo, h, fill)
                p.setPen(QPen(edge, 1, Qt.DashLine))
                p.drawLine(x_lo, 0, x_lo, h)
                p.drawLine(x_hi - 1, 0, x_hi - 1, h)

        if self._spec_db is None or len(self._spec_db) == 0:
            return

        span_db = self._max_db - self._min_db
        if span_db <= 0:
            return

        n = len(self._spec_db)
        # Decimate or interpolate to pixel width
        if n >= w:
            idx = (np.linspace(0, n - 1, w)).astype(np.int32)
            line = self._spec_db[idx]
        else:
            line = np.interp(np.linspace(0, n - 1, w),
                             np.arange(n), self._spec_db)

        ys = h - ((line - self._min_db) / span_db) * h
        ys = np.clip(ys, 0, h - 1)

        # Noise-floor reference line — drawn under the trace so the
        # signal envelope remains the dominant visual. Muted sage-
        # green dashes + small "NF -NN dBFS" label at the right edge.
        if (self._noise_floor_db is not None
                and self._min_db <= self._noise_floor_db <= self._max_db):
            nf_y = int(h - ((self._noise_floor_db - self._min_db)
                            / span_db) * h)
            nf_y = max(0, min(h - 1, nf_y))
            # User-picked NF color if set, else default sage green.
            # Alpha is always ~160 so the line stays unobtrusive
            # regardless of what the user picks.
            if self._user_nf_color:
                nf_color = QColor(self._user_nf_color)
                nf_color.setAlpha(180)
            else:
                nf_color = QColor(120, 200, 140, 160)
            p.setPen(QPen(nf_color, 1, Qt.DashLine))
            p.drawLine(0, nf_y, w, nf_y)
            # Label in a tiny monospace so it doesn't fight the grid
            from PySide6.QtGui import QFont
            label_font = QFont("Consolas")
            label_font.setPointSize(8)
            p.setFont(label_font)
            p.setPen(QPen(nf_color, 1))
            # Position just above the line, right-justified near the edge
            p.drawText(w - 90, nf_y - 3,
                       f"NF {self._noise_floor_db:+.0f} dBFS")

        # Filled area under trace — uses the user's trace color if
        # picked, else the default TRACE. Gradient uses two alphas of
        # the same color so the user's pick drives the whole scheme.
        trace_color = QColor(self._user_trace_color) \
            if self._user_trace_color else QColor(TRACE)
        grad_top = QColor(trace_color)
        grad_top.setAlpha(100)
        grad_bot = QColor(trace_color)
        grad_bot.setAlpha(10)
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, grad_top)
        grad.setColorAt(1.0, grad_bot)

        from PySide6.QtGui import QPolygonF
        from PySide6.QtCore import QPointF
        poly = QPolygonF()
        poly.append(QPointF(0, h))
        for i in range(w):
            poly.append(QPointF(i, ys[i]))
        poly.append(QPointF(w - 1, h))
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawPolygon(poly)

        # Trace line (same color, full opacity)
        p.setPen(QPen(trace_color, 1.2))
        for i in range(w - 1):
            p.drawLine(i, int(ys[i]), i + 1, int(ys[i + 1]))

        # Peak markers — in-passband peak-hold trace (amber, brighter
        # than the fill gradient), clipped to pixel columns that fall
        # inside the current RX passband window. Feature only renders
        # if enabled + passband is valid + peak buffer is populated.
        if (self._peak_markers_enabled
                and self._peak_hold_db is not None
                and self._passband_hi_hz > self._passband_lo_hz
                and self._span_hz > 0):
            hz_per_px = self._span_hz / w
            center_x = w / 2
            pb_x_lo = int(center_x + self._passband_lo_hz / hz_per_px)
            pb_x_hi = int(center_x + self._passband_hi_hz / hz_per_px)
            pb_x_lo = max(0, pb_x_lo)
            pb_x_hi = min(w, pb_x_hi)
            if pb_x_hi > pb_x_lo + 2:
                # Decimate / interpolate the peak buffer to pixel width
                # the same way the live trace was.
                n_peak = len(self._peak_hold_db)
                if n_peak >= w:
                    idx = (np.linspace(0, n_peak - 1, w)).astype(np.int32)
                    peak_line = self._peak_hold_db[idx]
                else:
                    peak_line = np.interp(np.linspace(0, n_peak - 1, w),
                                          np.arange(n_peak),
                                          self._peak_hold_db)
                py = h - ((peak_line - self._min_db) / span_db) * h
                py = np.clip(py, 0, h - 1)
                # User-picked color if set, else default warm amber.
                # Alpha is fixed at 230 so the marker stays distinctly
                # visible regardless of hue.
                if self._user_peak_color:
                    amber = QColor(self._user_peak_color)
                    amber.setAlpha(230)
                else:
                    amber = QColor(255, 190, 90, 230)
                style = self._peak_markers_style
                if style == "line":
                    p.setPen(QPen(amber, 1.3))
                    for i in range(pb_x_lo, pb_x_hi - 1):
                        p.drawLine(i, int(py[i]),
                                   i + 1, int(py[i + 1]))
                elif style == "triangles":
                    # Small downward-pointing triangles at sampled bins
                    # (every 4 px so they don't smudge into a solid band)
                    p.setPen(QPen(amber, 1))
                    p.setBrush(QColor(255, 190, 90, 200))
                    from PySide6.QtGui import QPolygonF
                    from PySide6.QtCore import QPointF
                    for i in range(pb_x_lo, pb_x_hi, 4):
                        yi = int(py[i])
                        tri = QPolygonF([
                            QPointF(i - 3, yi - 4),
                            QPointF(i + 3, yi - 4),
                            QPointF(i,     yi),
                        ])
                        p.drawPolygon(tri)
                else:  # "dots"
                    p.setPen(QPen(amber, 1))
                    p.setBrush(QColor(255, 190, 90, 220))
                    # One dot every 2 px — tight enough to read as a
                    # curve at normal zoom, loose enough to count as
                    # discrete marks.
                    for i in range(pb_x_lo, pb_x_hi, 2):
                        p.drawEllipse(QPointF(i, py[i]), 1.8, 1.8)

                # Optional peak-dB readout — shown only at the top
                # N=3 strongest peaks in the passband so the spectrum
                # doesn't get flooded with numbers. Tick-box controlled.
                if self._peak_markers_show_db and pb_x_hi > pb_x_lo + 10:
                    pb_slice = peak_line[pb_x_lo:pb_x_hi]
                    # Find up to 3 local maxima ≥ 6 dB above the min in
                    # the passband (avoid labeling baseline).
                    min_in_pb = float(np.min(pb_slice))
                    threshold = min_in_pb + 6.0
                    from PySide6.QtGui import QFont as _QFont
                    lbl_font = _QFont()
                    lbl_font.setPointSize(7)
                    lbl_font.setBold(True)
                    p.setFont(lbl_font)
                    p.setPen(QPen(amber, 1))
                    # Greedy: find the top 3 peaks separated by ≥ 16 px
                    candidates = []
                    for i in range(1, len(pb_slice) - 1):
                        v = pb_slice[i]
                        if (v > pb_slice[i - 1] and v >= pb_slice[i + 1]
                                and v >= threshold):
                            candidates.append((v, i + pb_x_lo))
                    candidates.sort(reverse=True)
                    chosen: list[tuple[float, int]] = []
                    for val, x_peak in candidates:
                        if all(abs(x_peak - xc) >= 16 for _, xc in chosen):
                            chosen.append((val, x_peak))
                        if len(chosen) >= 3:
                            break
                    for val, x_peak in chosen:
                        yi = int(h - ((val - self._min_db) / span_db) * h)
                        yi = max(10, yi)
                        p.drawText(x_peak + 4, yi - 2,
                                   f"{val:+.0f}")

        # dB scale labels on right
        p.setPen(QPen(AXIS, 1))
        for i in range(0, 11, 2):
            db = self._max_db - (i / 10) * span_db
            y = int(h * i / 10)
            p.drawText(w - 45, y + 10, f"{db:+.0f}")

        # VFO marker — vertical line at center (radio is tuned here)
        cx = w // 2
        p.setPen(QPen(QColor(255, 170, 80, 220), 1, Qt.DashLine))
        p.drawLine(cx, 0, cx, h)

        # Notch markers — draw each notch as a red "well" descending
        # from the top of the spectrum area, with the well contour
        # following the notch filter's actual biquad magnitude
        # response. That way the operator can eyeball both how wide
        # the notch is AND how deeply it attenuates at center. Depth
        # is capped at CUT_DEPTH_DB_MAX for display (a real biquad
        # notch has a theoretical null at exact center frequency —
        # infinite dB — but showing an infinite well isn't useful).
        #
        # Math: approx biquad band-reject magnitude²
        #   |H(Δf)|² ≈ Δf² / (Δf² + (bw/2)²)
        # where bw = f_baseband / Q (biquad bandwidth at the current
        # baseband offset). That gives −3 dB at Δf = bw/2, consistent
        # with the hit-testing geometry elsewhere in this widget.
        if self._notches and self._span_hz > 0 and span_db > 0:
            CUT_DEPTH_DB_MAX = 40.0
            hz_per_px = self._span_hz / max(1, w)
            for freq, q in self._notches:
                nf = (freq - self._center_hz) / self._span_hz + 0.5
                if not (0.0 <= nf <= 1.0):
                    continue
                nx = int(nf * w)
                bb_freq = max(abs(freq - self._center_hz),
                              self._span_hz * 0.02)
                bw_hz = bb_freq / max(q, 1e-3)
                # Well extends out to ±6×bw (well past where the cut
                # is meaningful) so the V-shape tails cleanly back to
                # zero rather than clipping mid-slope.
                visual_half_hz = bw_hz * 6.0
                half_px = max(6, int(visual_half_hz / hz_per_px))
                x_start = max(0, nx - half_px)
                x_end   = min(w - 1, nx + half_px)
                if x_end <= x_start:
                    continue
                xs = np.arange(x_start, x_end + 1, dtype=np.float64)
                dfs = (xs - nx) * hz_per_px
                bw_half = bw_hz * 0.5
                ratio_sq = (dfs * dfs) / (
                    dfs * dfs + bw_half * bw_half + 1e-12)
                atten_db = -10.0 * np.log10(ratio_sq + 1e-12)
                atten_db = np.minimum(atten_db, CUT_DEPTH_DB_MAX)
                ys = (atten_db / span_db) * h
                poly = QPolygonF()
                poly.append(QPointF(float(x_start), 0.0))
                for xi, yi in zip(xs, ys):
                    poly.append(QPointF(float(xi), float(yi)))
                poly.append(QPointF(float(x_end), 0.0))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 80, 80, 110))
                p.drawPolygon(poly)
                # Center hairline so very narrow wells stay grabbable
                # even when the fill is only a pixel or two wide.
                p.setPen(QPen(QColor(255, 80, 80, 220), 1, Qt.SolidLine))
                p.drawLine(nx, 0, nx, h)
                # Depth label just below the well's bottom. Only
                # draw if there's room — don't clutter tiny spans or
                # overlap the freq scale at the bottom.
                depth_y = int((CUT_DEPTH_DB_MAX / span_db) * h)
                if 14 < depth_y < h - 18:
                    p.setPen(QColor(255, 210, 210))
                    p.drawText(nx + 4, depth_y + 12,
                               f"−{int(CUT_DEPTH_DB_MAX)} dB")

        # Frequency scale on bottom
        p.setPen(QPen(AXIS, 1))
        for i in range(1, 10):
            x = int(w * i / 10)
            offset_hz = (i / 10 - 0.5) * self._span_hz
            freq_khz = (self._center_hz + offset_hz) / 1000.0
            label = f"{freq_khz:,.1f}"
            p.drawText(x - 30, h - 4, label)

        # TCI Spots — conventional colored box with callsign text inside.
        # Callsign may contain the country-flag emoji (SDRLogger+ does this).
        #
        # Anti-clutter strategy (FT8 can pile 20+ spots into a few kHz):
        #   A. Multi-row collision stacking — up to MAX_SPOT_ROWS (4)
        #      rows. Newest spots (highest ts) get first crack at the top
        #      row; older spots cascade down. Any spot that can't find a
        #      non-overlapping row is dropped this frame (not drawn, but
        #      still held in _spots for hit-testing).
        #   D. Age-fade — linear from 100% alpha at ts=now to 30% alpha
        #      at ts=now-lifetime. Oldest spots visually recede so fresh
        #      ones pop. 30% floor so near-expiry spots remain legible.
        if self._spots and self._span_hz > 0:
            import time
            from PySide6.QtCore import QRectF
            from PySide6.QtGui import QFont, QFontMetrics
            MAX_SPOT_ROWS = 4
            ROW_GAP_PX    = 3      # horizontal padding between same-row boxes
            AGE_FADE_FLOOR = 0.30  # min alpha multiplier for very old spots

            # Dedicated font with emoji fallback so flag glyphs render.
            spot_font = QFont()
            spot_font.setFamilies(["Segoe UI Emoji", "Segoe UI", "Arial"])
            spot_font.setPointSize(8)
            spot_font.setBold(True)
            p.setFont(spot_font)
            fm = QFontMetrics(spot_font)
            padding_h = 5
            padding_v = 2
            box_h = fm.height() + 2 * padding_v

            # Filter to on-screen spots. Apply mode-filter here too so
            # it doesn't pollute the collision packer with spots that
            # will never render. Empty filter = accept all.
            mode_filter = self._spot_mode_filter
            visible = []
            for s in self._spots:
                if mode_filter:
                    mode = str(s.get("mode", "")).upper()
                    if mode not in mode_filter:
                        continue
                nf = (s["freq_hz"] - self._center_hz) / self._span_hz + 0.5
                if 0.0 <= nf <= 1.0:
                    visible.append((nf, s))

            # Newest-first so fresh spots land in the top row; older ones
            # cascade down. Spots without a ts sort last (treated as age=∞).
            visible.sort(key=lambda t: -t[1].get("ts", 0.0))

            # Greedy row assignment — each row keeps a list of occupied
            # horizontal intervals; walk rows 0..3, take the first that
            # doesn't overlap the proposed box (+ ROW_GAP_PX margin).
            row_ranges: list[list[tuple[int, int]]] = [
                [] for _ in range(MAX_SPOT_ROWS)]
            placed: list[tuple[dict, int, float, float, float]] = []
            # placed items: (spot_dict, nx, bx, by, tw)

            for nf, s in visible:
                nx = int(nf * w)
                text = s.get("display") or s.get("call", "")
                tw = fm.horizontalAdvance(text) + 2 * padding_h
                bx = nx - tw // 2
                bx = max(2, min(w - tw - 2, bx))
                x_start = bx - ROW_GAP_PX
                x_end = bx + tw + ROW_GAP_PX

                chosen_row = -1
                for r in range(MAX_SPOT_ROWS):
                    fits = True
                    for rs, re in row_ranges[r]:
                        if not (x_end <= rs or x_start >= re):
                            fits = False
                            break
                    if fits:
                        chosen_row = r
                        break
                if chosen_row < 0:
                    continue   # no free row — drop this frame

                row_ranges[chosen_row].append((x_start, x_end))
                by = 2 + chosen_row * (box_h + 2)
                placed.append((s, nx, bx, by, tw))

            # Render placed spots with age-based alpha.
            now = time.monotonic()
            lifetime = self._spot_lifetime_s
            for s, nx, bx, by, tw in placed:
                # Age fade — older spots fade toward the floor alpha.
                if lifetime > 0:
                    age = now - s.get("ts", now)
                    frac = max(0.0, min(1.0, age / lifetime))
                    alpha_mul = 1.0 - (1.0 - AGE_FADE_FLOOR) * frac
                else:
                    alpha_mul = 1.0

                argb = s.get("color", 0xFFFFD700)
                rc = (argb >> 16) & 0xFF
                gc = (argb >> 8) & 0xFF
                bc = argb & 0xFF
                border_alpha = int(round(255 * alpha_mul))
                tint_alpha   = int(round( 45 * alpha_mul))
                text_alpha   = int(round(255 * alpha_mul))

                spot_color = QColor(rc, gc, bc, border_alpha)
                tint       = QColor(rc, gc, bc, tint_alpha)
                text       = s.get("display") or s.get("call", "")

                rect = QRectF(bx, by, tw, box_h)
                # Outlined box: faint tint + 1 px border in the spot color.
                p.setBrush(tint)
                p.setPen(QPen(spot_color, 1))
                p.drawRoundedRect(rect, 3, 3)
                # Text in the spot color (slightly more opaque pen so text
                # stays legible even when age-fade is kicking in hard).
                p.setPen(QPen(QColor(rc, gc, bc, text_alpha), 1))
                p.drawText(rect, Qt.AlignCenter, text)
                # Vertical tick from box down to the spectrum trace.
                tick_pen = QPen(QColor(rc, gc, bc,
                                       max(80, border_alpha)), 1)
                p.setPen(tick_pen)
                p.drawLine(nx, int(by + box_h), nx, h - 18)


class WaterfallWidget(_PaintedWidget):
    """Scrolling heatmap. Newest row at top, older rows scroll down."""

    clicked_freq = Signal(float)
    # Payload: (abs_freq_hz, shift_held, global_position). See the
    # SpectrumWidget notes — plain right-click is now a menu trigger,
    # not a silent add-notch, to stop the "I just clicked and a notch
    # appeared" surprise.
    right_clicked_freq = Signal(float, bool, QPoint)
    wheel_at_freq = Signal(float, int)
    notch_q_drag = Signal(float, float)

    NOTCH_HIT_PX = 14

    def __init__(self, parent=None, rows: int = 500):
        super().__init__(parent)
        self._rows = rows
        self._width = 1
        self._data: np.ndarray | None = None
        self._min_db = -110.0
        self._max_db = -30.0
        self._center_hz = 0.0
        self._span_hz = 48000.0
        self._notches: list[tuple[float, float]] = []
        self._drag_notch: tuple[float, float] | None = None
        self._drag_start_y: int = 0
        # Palette is looked up by name so the Visuals tab can hot-swap
        # it without reconstructing the widget. Name is persisted via
        # QSettings and restored on startup.
        from . import palettes
        self._palette_name = palettes.DEFAULT_PALETTE
        self._palette = palettes.get(self._palette_name)
        self.setMinimumHeight(200)
        self.setCursor(Qt.CrossCursor)

    def set_tuning(self, center_hz: float, span_hz: float):
        self._center_hz = center_hz
        self._span_hz = span_hz

    def set_notches(self, pairs: list[tuple[float, float]]):
        self._notches = list(pairs)
        self.update()

    def _freq_at_x(self, x: float) -> float:
        if self.width() <= 0 or self._span_hz <= 0:
            return self._center_hz
        return self._center_hz + (x / self.width() - 0.5) * self._span_hz

    def _notch_half_px(self, freq: float, q: float) -> int:
        if self._span_hz <= 0 or self.width() <= 0:
            return self.NOTCH_HIT_PX
        hz_per_px = self._span_hz / self.width()
        bb_freq = max(abs(freq - self._center_hz), self._span_hz * 0.02)
        notch_bw_hz = (bb_freq / q) * 3.0
        return max(self.NOTCH_HIT_PX, int(notch_bw_hz / hz_per_px / 2))

    def _nearest_notch_at_x(self, x: float) -> tuple[float, float] | None:
        if not self._notches or self._span_hz <= 0:
            return None
        best, best_px = None, None
        for freq, q in self._notches:
            nf = (freq - self._center_hz) / self._span_hz + 0.5
            if not (0.0 <= nf <= 1.0):
                continue
            px = abs(nf * self.width() - x)
            hit_radius = self._notch_half_px(freq, q)
            if px <= hit_radius and (best_px is None or px < best_px):
                best, best_px = (freq, q), px
        return best

    def mousePressEvent(self, event):
        if self.width() <= 0:
            return
        x = event.position().x()
        freq = self._freq_at_x(x)
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if event.button() == Qt.LeftButton:
            hit = self._nearest_notch_at_x(x)
            if hit is not None:
                self._drag_notch = hit
                self._drag_start_y = int(event.position().y())
                self.setCursor(Qt.SizeVerCursor)
                return
            self.clicked_freq.emit(freq)
        elif event.button() == Qt.RightButton:
            gpos = event.globalPosition().toPoint()
            self.right_clicked_freq.emit(freq, shift, gpos)

    def mouseMoveEvent(self, event):
        if self._drag_notch is None:
            return
        freq, start_q = self._drag_notch
        dy = self._drag_start_y - int(event.position().y())
        if abs(dy) < 4:  # dead zone — tiny movement doesn't change Q
            return
        dy_eff = dy - (4 if dy > 0 else -4)
        new_q = max(2.0, min(500.0, start_q * (1.015 ** dy_eff)))
        self.notch_q_drag.emit(freq, new_q)

    def _waterfall_notch_half_px(self, freq, q):  # pragma: no cover
        return self._notch_half_px(freq, q)

    def mouseReleaseEvent(self, event):
        if self._drag_notch is not None and event.button() == Qt.LeftButton:
            self._drag_notch = None
            self.setCursor(Qt.CrossCursor)

    def wheelEvent(self, event):
        if self.width() <= 0:
            return
        freq = self._freq_at_x(event.position().x())
        delta_units = event.angleDelta().y() // 120
        if delta_units != 0:
            self.wheel_at_freq.emit(freq, int(delta_units))
            event.accept()
            return
        super().wheelEvent(event)

    def set_palette(self, name: str):
        """Switch waterfall palette live. Unknown names fall back to
        the default (see palettes.get()). Redraws everything already
        in the scroll buffer by re-mapping existing magnitudes — but
        we only have the post-colormap pixel buffer here, not the raw
        dB matrix, so the switch applies going forward; old rows keep
        their colors until they scroll off. Good-enough tradeoff that
        avoids doubling the memory footprint."""
        from . import palettes
        self._palette_name = name
        self._palette = palettes.get(name)
        self.update()

    @property
    def palette_name(self) -> str:
        return self._palette_name

    def set_db_range(self, min_db: float, max_db: float):
        self._min_db = float(min_db)
        self._max_db = float(max_db)
        self.update()

    def push_row(self, spec_db: np.ndarray):
        n = len(spec_db)
        if self._data is None or self._width != n:
            self._width = n
            self._data = np.zeros((self._rows, n), dtype=np.uint32)
            self._data[:] = 0xFF000000 | (BG.red() << 16) | (BG.green() << 8) | BG.blue()

        span = self._max_db - self._min_db
        if span <= 0:
            return
        norm = np.clip((spec_db - self._min_db) / span, 0.0, 1.0)
        idx = (norm * 255).astype(np.uint8)
        rgb = self._palette[idx]
        argb = (np.uint32(0xFF000000)
                | (rgb[:, 0].astype(np.uint32) << 16)
                | (rgb[:, 1].astype(np.uint32) << 8)
                | rgb[:, 2].astype(np.uint32))

        # Scroll down by 1, insert new row at top.
        self._data[1:] = self._data[:-1]
        self._data[0] = argb
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        if self._data is None:
            return
        # Build a QImage view over the numpy buffer (no copy). The QImage
        # must reference bytes that outlive the paint call — self._data does.
        buf = self._data
        img = QImage(
            buf.tobytes(), buf.shape[1], buf.shape[0],
            buf.shape[1] * 4, QImage.Format_ARGB32,
        )
        p.drawImage(self.rect(), img)

        # VFO marker
        cx = self.width() // 2
        p.setPen(QPen(QColor(255, 170, 80, 180), 1, Qt.DashLine))
        p.drawLine(cx, 0, cx, self.height())

        # Notch markers — horizontal-gradient ribbon + center + edges
        if self._notches and self._span_hz > 0:
            w = self.width()
            h = self.height()
            hz_per_px = self._span_hz / max(1, w)
            for freq, q in self._notches:
                nf = (freq - self._center_hz) / self._span_hz + 0.5
                if not (0.0 <= nf <= 1.0):
                    continue
                nx = int(nf * w)
                bb_freq = max(abs(freq - self._center_hz), self._span_hz * 0.02)
                notch_bw_hz = (bb_freq / q) * 3.0
                half_px = max(4, int(notch_bw_hz / hz_per_px / 2))
                grad = QLinearGradient(nx - half_px, 0, nx + half_px, 0)
                grad.setColorAt(0.0, QColor(255, 80, 80, 0))
                grad.setColorAt(0.5, QColor(255, 80, 80, 110))
                grad.setColorAt(1.0, QColor(255, 80, 80, 0))
                p.fillRect(nx - half_px, 0, 2 * half_px, h, grad)
                p.setPen(QPen(QColor(255, 80, 80, 240), 2, Qt.SolidLine))
                p.drawLine(nx, 0, nx, h)
                p.setPen(QPen(QColor(255, 140, 140, 140), 1, Qt.DashLine))
                p.drawLine(nx - half_px, 0, nx - half_px, h)
                p.drawLine(nx + half_px, 0, nx + half_px, h)
