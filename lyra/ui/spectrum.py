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

BG = QColor(0, 0, 0)         # panadapter background — pure black to match the waterfall
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
    # a notch).  Positive = wheel up = freq up.  SpectrumPanel routes
    # this to Radio.panadapter_scroll_tune which uses the operator's
    # Display-panel-set scroll step (default 1 kHz).
    wheel_tune    = Signal(int)
    # Ctrl+wheel over empty spectrum keeps the legacy zoom behavior
    # so power users with muscle memory for the old gesture still
    # have a path.  Positive = zoom in, negative = zoom out.
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
    DRAG_TUNE_THRESHOLD_PX = 5  # cursor must move > this to treat
                                # left-press as pan instead of click

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spec_db: np.ndarray | None = None
        self._min_db = -110.0
        self._max_db = -20.0
        self._center_hz = 0.0
        self._span_hz = 48000.0
        # Display-only EWMA smoothing (off by default). When enabled,
        # set_spectrum() blends each frame into _smoothed_db and the
        # widget renders the smoothed buffer instead of the raw FFT.
        # Pure visual filter — does not touch DSP or audio.
        self._smoothing_enabled: bool = False
        self._smoothing_strength: int = 4    # 1..10 (higher = smoother)
        self._smoothed_db: np.ndarray | None = None
        # Pre-allocated scratch buffer for the EWMA in-place computation.
        # Without this, each frame allocated a fresh ndarray for the
        # `alpha * spec_db` term — at 60 fps with 4096 bins that was
        # ~1 MB/sec of allocation churn, which Python's GC had to mop
        # up and showed up as a noticeable CPU bump when smoothing
        # was enabled. With this, EWMA is fully in-place after frame 1.
        self._smoothing_scratch: np.ndarray | None = None

        # ── Cached QFonts (paint-loop optimization) ──────────────────
        # Building a QFont() inside paintEvent is expensive on Windows:
        # each construction can allocate a GDI HFONT handle, force the
        # font engine to re-resolve metrics, and churn the QPainter
        # text cache. With overlays redrawing at 30-60 fps and several
        # font allocations per frame (segment labels, edge warnings,
        # peak labels, noise-floor label, spot boxes), per-frame
        # construction is one of the bigger contributors to UI lag
        # under long sessions. Cache them once here, reuse forever.
        # _font_spot is built lazily on first paint because it derives
        # from QPainter's current font (which we don't have at __init__).
        from PySide6.QtGui import QFont as _QFont
        self._font_7pt_bold = _QFont()
        self._font_7pt_bold.setPointSize(7)
        self._font_7pt_bold.setBold(True)
        self._font_8pt_bold = _QFont()
        self._font_8pt_bold.setPointSize(8)
        self._font_8pt_bold.setBold(True)
        self._font_consolas_8 = _QFont("Consolas")
        self._font_consolas_8.setPointSize(8)
        self._font_spot: _QFont | None = None   # built lazily, see paintEvent

        # ── Paint instrumentation (opt-in via LYRA_PAINT_DEBUG=1) ────
        # When enabled, we accumulate per-paint timings and print a
        # one-line summary every 5 seconds: frame count, average, p95,
        # peak, and the slowest single paint that period. Used to
        # diagnose the long-running visual-drag bug. Default off so
        # production sessions don't spam the console.
        import os as _os
        self._paint_debug = (_os.environ.get("LYRA_PAINT_DEBUG", "")
                             .strip() in ("1", "true", "True"))
        self._paint_t0_window = 0.0
        self._paint_durations: list[float] = []
        self._paint_setspec_count = 0   # set_spectrum() calls this window
        # Notches: list of (abs_freq_hz, width_hz, active, deep) tuples.
        # Visualization:
        #   active=True            → saturated red filled rectangle
        #   active=False           → desaturated grey rectangle
        #   deep=True (cascade)    → thicker outline + "^" suffix on
        #                            the width label, signaling 2×
        #                            attenuation
        self._notches: list[tuple[float, float, bool, bool]] = []
        self._spots: list[dict] = []
        # ── EiBi SW broadcaster overlay (v0.0.9 Step 4c) ────────
        # Visible-range entries are pushed in by SpectrumPanel
        # (which owns the freq + settings + EibiStore plumbing).
        # The widget just renders.  Each item is a tuple:
        #   (freq_hz, station, language, target, on_air_now)
        # The "on_air_now" hint lets the renderer pick a slightly
        # different color for entries that ARE on the air vs ones
        # the operator chose to show even when off-air.
        self._eibi_entries: list[tuple[int, str, str, str, bool]] = []
        # Master gate -- when False, the widget skips the overlay
        # entirely regardless of how many entries are stashed.
        # Set by SpectrumPanel based on the overlay_gate result.
        self._eibi_overlay_visible: bool = False
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
        # CW Zero (white) reference line offset from the VFO marker,
        # in Hz. +pitch in CWU, -pitch in CWL, 0 elsewhere (line hidden).
        self._cw_zero_offset_hz: int = 0
        # Lyra constellation watermark visibility — operator toggle.
        # Default ON; switched via Settings → Visuals.
        self._show_constellation: bool = True
        # Occasional meteor streaks across the panadapter — independent
        # toggle, default OFF (opt-in flair).
        self._show_meteors: bool = False
        # Grid lines (9×9 horiz/vert dotted divisions) — operator
        # toggle. Default ON; switched via Settings → Visuals.
        self._show_grid: bool = True
        self._passband_hi_hz: int = 0
        # ── Click-to-tune snap (v0.0.7.1 click-to-tune v1) ──────
        # See docs/architecture/click_to_tune_plan.md.  Plain click =
        # literal tune to cursor freq (legacy behaviour); Shift+click
        # = snap to nearest spectrum peak within +/- range_hz, if the
        # peak is at least min_snr_db above the rolling noise floor.
        # Operator-tunable defaults can be wired through Radio later.
        self._snap_tune_range_hz: float = 200.0
        self._snap_tune_min_snr_db: float = 6.0
        # When True, hover with the modifier held shows a reticle at
        # the predicted snap target so the operator sees where the
        # click will land before committing.
        self._snap_tune_show_reticle: bool = True
        # Cached hover snap target -- None when not hovering with
        # modifier held, otherwise the absolute freq the next click
        # would snap to.  Updated in mouseMoveEvent, drawn in
        # paintEvent.
        self._snap_hover_freq: float | None = None
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
        # Vertical pixels at the top of the widget reserved by the
        # band-plan overlay (segment strip + landmark triangles). Set
        # by paintEvent each frame so the spot packer below knows where
        # it can start placing rows without colliding into the colored
        # mode bar or the FT8 / FT4 / WSPR triangles. Default 0 means
        # "no band-plan overlay active, spots may use full vertical".
        self._band_plan_reserved_px: int = 0
        self._drag_notch: tuple[float, float] | None = None
        self._drag_start_y: int = 0
        # Active passband-edge drag: None, "lo", or "hi"
        self._drag_pb_edge: str | None = None
        # Active Y-axis dB-scale drag: stores (start_mouse_y,
        # start_min_db, start_max_db, mode) where mode is
        # "min" / "max" / "pan". Set when user press-and-holds in
        # the rightmost ~50 px strip (the dB labels area).
        self._drag_db_scale: tuple[int, float, float, str] | None = None
        # Click-vs-drag-tune state. Set on left-press over empty
        # spectrum (no notch, landmark, passband edge, or dB-scale
        # hit). Tracks (start_x, start_center_hz, in_drag). The
        # `in_drag` flag flips True the first time the cursor moves
        # past DRAG_TUNE_THRESHOLD_PX, so we can tell a click from a
        # pan gesture on release: still False = single-click tune to
        # cursor (legacy behavior); True = pan-tune already updated
        # the freq during the drag, just clear state.
        self._drag_tune: tuple[int, float, bool] | None = None
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

    def set_eibi_entries(self,
                         entries: list[tuple[int, str, str, str, bool]],
                         visible: bool) -> None:
        """Push the visible-range EiBi entries into the widget for
        the next paint.  Called by SpectrumPanel after a freq /
        zoom change or when the EibiStore reloads.

        Each entry tuple is
        ``(freq_hz, station, language, target, on_air_now)``.
        ``visible`` is the master gate -- when False, the widget
        skips drawing entirely.

        Internal storage is the same shape as the input; no
        further filtering happens at paint time."""
        self._eibi_entries = list(entries)
        self._eibi_overlay_visible = bool(visible)
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

    def set_cw_zero_offset(self, offset_hz: int) -> None:
        """CW Zero (white) reference line offset from the VFO marker,
        in Hz. +pitch in CWU, -pitch in CWL, 0 outside CW (hidden)."""
        self._cw_zero_offset_hz = int(offset_hz)
        self.update()

    def set_show_constellation(self, visible: bool) -> None:
        """Toggle the Lyra constellation watermark behind the trace."""
        self._show_constellation = bool(visible)
        self.update()

    def set_show_meteors(self, visible: bool) -> None:
        """Toggle occasional meteor streaks across the panadapter."""
        self._show_meteors = bool(visible)
        self.update()

    def set_show_grid(self, visible: bool) -> None:
        """Toggle the 9×9 grid divisions on the panadapter."""
        self._show_grid = bool(visible)
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

    def set_spectrum_smoothing_enabled(self, on: bool):
        """Display-only EWMA smoothing of the spectrum trace. Off by
        default. Disabling clears the EWMA buffer so a later re-enable
        starts clean rather than picking up a stale memory."""
        self._smoothing_enabled = bool(on)
        if not self._smoothing_enabled:
            self._smoothed_db = None
        self.update()

    def set_spectrum_smoothing_strength(self, strength: int):
        """Smoothing strength 1..10 (higher = smoother / slower response)."""
        self._smoothing_strength = max(1, min(10, int(strength)))
        # No buffer reset — let the new alpha take effect on next frame.
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

    def set_notches(self, items):
        """Receive the notch list from Radio. Items are
        (abs_freq_hz, width_hz, active, deep[, depth_db, cascade])
        tuples — same shape that Radio.notch_details emits.

        Tolerates 3-tuples (legacy: no deep flag), 4-tuples (legacy:
        no depth/cascade), and 6-tuples (notch v2).  The internal
        ``self._notches`` list keeps the 4-tuple shape used by
        existing rendering code; the extra fields are stored
        separately if rendering wants them later."""
        norm = []
        for it in items:
            if len(it) >= 4:
                f, w, a, d = it[0], it[1], it[2], it[3]
            elif len(it) == 3:
                f, w, a = it
                d = False
            else:
                continue
            norm.append((float(f), float(w), bool(a), bool(d)))
        self._notches = norm
        self.update()

    def _freq_at_x(self, x: float) -> float:
        if self.width() <= 0 or self._span_hz <= 0:
            return self._center_hz
        frac = x / self.width()
        return self._center_hz + (frac - 0.5) * self._span_hz

    def _bin_to_freq(self, bin_idx: float) -> float:
        """Inverse of the implicit bin-to-freq mapping in _spec_db.
        Bin 0 sits at center - span/2, bin N-1 at center + span/2 -
        bin_width.  Accepts a float bin index for parabolic-peak
        interpolation."""
        if self._spec_db is None or len(self._spec_db) < 2:
            return self._center_hz
        n = len(self._spec_db)
        return self._center_hz + (
            (float(bin_idx) / max(1, n - 1)) - 0.5) * self._span_hz

    # Pixel-based search radius for click-to-tune snap (v0.0.7.2).
    # At wide zoom (192 kHz span / 1500 px = 128 Hz/px) a 200 Hz
    # Hz-based range is only ~3 px wide, smaller than typical
    # operator click precision.  Effective range is
    # max(snap_tune_range_hz, SNAP_PIXEL_RADIUS * hz_per_px) so the
    # snap-near-cursor behaviour stays sensible at every zoom level.
    SNAP_PIXEL_RADIUS = 80

    def _find_snap_target(self, x_pixel: float) -> float | None:
        """Find the strongest spectrum bin within +/- snap_range_hz of
        the cursor frequency.  Return the parabolically-interpolated
        center frequency of that peak, or None if no peak is at
        least ``snap_min_snr_db`` above the rolling noise floor.

        Used by mouseReleaseEvent (commit-on-click) and mouseMoveEvent
        (hover reticle)."""
        if self._spec_db is None or len(self._spec_db) < 3:
            return None
        if self._span_hz <= 0:
            return None
        f_cursor = self._freq_at_x(x_pixel)
        n = len(self._spec_db)
        # Convert freq range to bin range.  Effective search range
        # = max of operator-set Hz range and pixel radius in Hz.
        bins_per_hz = (n - 1) / max(1.0, self._span_hz)
        widget_w = max(1, self.width())
        hz_per_px = self._span_hz / widget_w
        pixel_range_hz = self.SNAP_PIXEL_RADIUS * hz_per_px
        eff_range_hz = max(self._snap_tune_range_hz, pixel_range_hz)
        half_bins = max(1, int(round(
            eff_range_hz * bins_per_hz)))
        # Cursor bin index (clamped) — the search window centers here.
        cursor_bin = int(round(
            ((f_cursor - self._center_hz) / self._span_hz + 0.5)
            * (n - 1)))
        cursor_bin = max(0, min(n - 1, cursor_bin))
        bin_lo = max(0, cursor_bin - half_bins)
        bin_hi = min(n, cursor_bin + half_bins + 1)
        if bin_hi - bin_lo < 2:
            return None
        window = self._spec_db[bin_lo:bin_hi]
        peak_local = int(np.argmax(window))
        peak_idx = peak_local + bin_lo
        peak_db = float(self._spec_db[peak_idx])
        # SNR check vs the rolling noise floor.  When floor isn't
        # known yet (first second of streaming), gate on peak vs
        # min-of-window (still a useful local-prominence test).
        if self._noise_floor_db is not None:
            ref_db = float(self._noise_floor_db)
        else:
            ref_db = float(np.min(window))
        if (peak_db - ref_db) < self._snap_tune_min_snr_db:
            return None
        # Parabolic peak interpolation for sub-bin precision.  Only
        # works when the peak isn't on the window edge; fall back to
        # the integer bin otherwise.
        if 0 < peak_idx < n - 1:
            y0 = float(self._spec_db[peak_idx - 1])
            y1 = float(self._spec_db[peak_idx])
            y2 = float(self._spec_db[peak_idx + 1])
            denom = (y0 - 2.0 * y1 + y2)
            if abs(denom) > 1e-9:
                delta = 0.5 * (y0 - y2) / denom
                # Parabolic interpolation can suggest |delta|>1 when
                # the spectrum is non-parabolic; clamp for safety.
                delta = max(-1.0, min(1.0, delta))
                peak_idx_refined = float(peak_idx) + delta
            else:
                peak_idx_refined = float(peak_idx)
        else:
            peak_idx_refined = float(peak_idx)
        return self._bin_to_freq(peak_idx_refined)

    def _should_snap(self, event) -> bool:
        """Decide whether the click should snap based on modifiers.
        v0.0.7.1: Shift = snap, plain = literal.  In a future
        Settings tab the operator can flip this default."""
        return bool(event.modifiers() & Qt.ShiftModifier)

    def _notch_half_px(self, width_hz: float) -> int:
        """Half-pixel-width of the notch's visible rectangle. Used
        for both rendering and hit-testing — clicking anywhere inside
        the shaded region selects the notch, not just on the 2 px
        center line. Always at least NOTCH_HIT_PX so very narrow
        notches stay grabbable."""
        if self._span_hz <= 0 or self.width() <= 0:
            return self.NOTCH_HIT_PX
        hz_per_px = self._span_hz / self.width()
        visual_half = int(width_hz * 0.5 / hz_per_px)
        return max(self.NOTCH_HIT_PX, visual_half)

    def _nearest_notch_at_x(self, x: float):
        """Return (freq, width, active, deep) of the nearest notch
        whose visible rectangle contains x, or None."""
        if not self._notches or self._span_hz <= 0:
            return None
        best = None
        best_px = None
        for freq, width_hz, active, deep in self._notches:
            nf = (freq - self._center_hz) / self._span_hz + 0.5
            if not (0.0 <= nf <= 1.0):
                continue
            nx = nf * self.width()
            px = abs(nx - x)
            hit_radius = self._notch_half_px(width_hz)
            if px <= hit_radius and (best_px is None or px < best_px):
                best_px = px
                best = (freq, width_hz, active, deep)
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
            # Empty spectrum left-press: don't emit clicked_freq yet.
            # Stash drag-tune candidate state — mouseMoveEvent decides
            # whether this becomes a pan-tune; mouseReleaseEvent fires
            # the legacy single-click tune if no pan happened.
            #
            # v0.0.7.3+: latch the snap target AT PRESS TIME if Shift
            # is held.  Capturing the snap intent here (vs recomputing
            # on release) makes the commit atomic w.r.t. Shift state
            # -- the operator can release Shift any time after press
            # and the click still snaps to what the reticle was
            # showing.  Without this, releasing Shift slightly before
            # the mouse caused the release-time modifier check to
            # fail and the click to fall through to literal-click,
            # "missing" the reticle.
            press_snap = None
            if shift:
                press_snap = self._find_snap_target(x)
            self._drag_tune = (
                int(x), float(self._center_hz), False, press_snap)
            self.setCursor(Qt.OpenHandCursor)
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
        # Notch-width drag (vertical motion). Drag UP = narrower
        # (smaller width), drag DOWN = wider — matches the wheel
        # convention. Multiplicative so the response feels uniform
        # across width ranges. 1.5% per pixel after a 4 px deadzone.
        if self._drag_notch is not None:
            freq, start_width, _active, _deep = self._drag_notch
            dy = self._drag_start_y - int(event.position().y())
            if abs(dy) < 4:
                return
            dy_eff = dy - (4 if dy > 0 else -4)
            new_width = max(5.0, min(2000.0,
                                     start_width * (1.015 ** -dy_eff)))
            self.notch_q_drag.emit(freq, new_width)
            return
        # Click-and-drag tune (pan). Sign convention: cursor moves
        # right → spectrum should slide right "following the finger" →
        # so center freq DECREASES (lower freqs come into view from
        # the left). The "drag the spectrum like a Google Maps view"
        # interaction model is the common SDR-client convention.
        #
        # v0.0.7.2: rate-limit drag emits so the freq doesn't fire
        # at mouseMoveEvent's native ~120 Hz.  Each freq change
        # cascades into HL2 C&C frame writes + notch rebuilds +
        # spectrum pipeline updates; firing those at 120 Hz makes
        # the panadapter lag while the freq readout scrolls freely.
        # 33 ms / 1 Hz gating keeps the operator-perceived pan
        # smooth at ~30 fps with no backend overload.
        if self._drag_tune is not None:
            start_x, start_center, in_drag, press_snap = self._drag_tune
            dx = int(event.position().x()) - start_x
            if not in_drag:
                if abs(dx) < self.DRAG_TUNE_THRESHOLD_PX:
                    return  # still inside the click dead-zone
                in_drag = True
                self._drag_tune = (
                    start_x, start_center, True, press_snap)
                self.setCursor(Qt.ClosedHandCursor)
                self._drag_last_emit_ms = 0.0
                self._drag_last_emit_hz = float(start_center)
            if self._span_hz <= 0 or self.width() <= 0:
                return
            hz_per_px = self._span_hz / self.width()
            new_center = start_center - dx * hz_per_px
            import time as _t
            now_ms = _t.monotonic() * 1000.0
            if (now_ms - getattr(self, "_drag_last_emit_ms", 0.0)
                    < 33.0):
                return
            if abs(new_center - getattr(self, "_drag_last_emit_hz",
                                        new_center)) < 1.0:
                return
            self._drag_last_emit_ms = now_ms
            self._drag_last_emit_hz = float(new_center)
            self.clicked_freq.emit(float(new_center))
            return
        # Hover cursor hint — only update when not already dragging so
        # we don't fight Qt's drag-cursor state. Landmarks get a
        # pointing-hand cursor to telegraph "this is clickable."
        y = event.position().y()
        # Notch hover: callout tooltip + cursor hint.
        # Tooltip shows the absolute freq + width + state so the
        # operator can identify which notch is which without
        # right-clicking.
        notch_hit = self._nearest_notch_at_x(x)
        if notch_hit is not None:
            freq, width_hz, active, deep = notch_hit
            flags = []
            if not active:
                flags.append("INACTIVE")
            if deep:
                flags.append("DEEP")
            flag_str = (" — " + " / ".join(flags)) if flags else ""
            tip = (f"Notch  {freq/1e6:.4f} MHz\n"
                   f"Width  {int(round(width_hz))} Hz{flag_str}")
            self.setToolTip(tip)
            self.setCursor(Qt.SizeVerCursor)
            return
        # Clear tooltip when not over a notch so it doesn't linger.
        if self.toolTip():
            self.setToolTip("")
        if self._landmark_at(x, y) is not None:
            self.setCursor(Qt.PointingHandCursor)
        elif self._db_scale_mode_at(x, y) is not None:
            self.setCursor(Qt.SizeVerCursor)
        elif self._passband_edge_at_x(x) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.CrossCursor)

        # ── EiBi label hover tooltip (v0.0.9 Step 4c) ────────────
        # Hover an EiBi station label -> tooltip with full details
        # (freq, language, target area, on-air status).
        eibi_hit = self._eibi_hit_at(x, y)
        if eibi_hit is not None:
            freq_hz, station, language, target, on_air = eibi_hit
            on_str = "ON AIR" if on_air else "off air"
            tip_lines = [
                f"<b>{station}</b>",
                f"{freq_hz/1e6:.4f} MHz  ({on_str})",
            ]
            if language:
                tip_lines.append(f"Language: {language}")
            if target:
                tip_lines.append(f"Target: {target}")
            self.setToolTip("\n".join(tip_lines))
        elif self.toolTip().startswith("<b>") and "MHz" in self.toolTip():
            # Clear stale EiBi tooltip when we move off a label.
            self.setToolTip("")

        # ── Snap-target hover reticle (click-to-tune v1) ─────────
        # When the modifier is held over empty spectrum, find the
        # snap target and stash it for paintEvent to draw a reticle.
        # Updates as the cursor moves so the operator sees where the
        # next click will land.  Clear when modifier releases.
        prev = self._snap_hover_freq
        if (self._snap_tune_show_reticle
                and bool(event.modifiers() & Qt.ShiftModifier)):
            self._snap_hover_freq = self._find_snap_target(x)
        else:
            self._snap_hover_freq = None
        if prev != self._snap_hover_freq:
            self.update()

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
            return
        # Drag-tune release. If we never crossed the threshold this
        # was a plain click — fire the legacy tune-to-cursor so a
        # sharp single click still re-tunes to exactly where the user
        # clicked (the test the operator instinctively reaches for).
        if self._drag_tune is not None:
            start_x, _start_center, in_drag, press_snap = self._drag_tune
            self._drag_tune = None
            self.setCursor(Qt.CrossCursor)
            if not in_drag:
                # v0.0.7.3+: snap target was latched at press time
                # so the commit is atomic w.r.t. Shift state.
                # Operator can release Shift any time before
                # releasing the mouse; the click still snaps to
                # what the reticle was showing.
                if press_snap is not None:
                    self.clicked_freq.emit(float(press_snap))
                else:
                    self.clicked_freq.emit(
                        self._freq_at_x(float(start_x)))
            # Clear any lingering hover-reticle state so it doesn't
            # ghost on subsequent hovers.
            self._snap_hover_freq = None
            self.update()

    def wheelEvent(self, event):
        if self.width() <= 0:
            return
        x = event.position().x()
        freq = self._freq_at_x(x)
        delta_units = event.angleDelta().y() // 120
        if delta_units == 0:
            super().wheelEvent(event)
            return
        # Wheel routing:
        #   * Over a notch tick → adjust that notch's width (classic
        #     "hover a notch and wheel" gesture, unchanged).
        #   * Ctrl+wheel anywhere else → zoom (escape hatch for
        #     operators with muscle memory from the pre-2026-05-08
        #     wheel-zoom default).
        #   * Plain wheel anywhere else → tune VFO by the operator's
        #     panadapter scroll step (Display panel combo).
        if self._nearest_notch_at_x(x) is not None:
            self.wheel_at_freq.emit(freq, int(delta_units))
        elif event.modifiers() & Qt.ControlModifier:
            self.wheel_zoom.emit(int(delta_units))
        else:
            self.wheel_tune.emit(int(delta_units))
        event.accept()

    def set_spectrum(self, spec_db: np.ndarray, center_hz: float, span_hz: float):
        # Paint-debug instrumentation: count how often spectrum data
        # arrives, so the 5-second summary can flag rate anomalies
        # (e.g. signal-multiplication causing N× emit per FFT).
        if self._paint_debug:
            self._paint_setspec_count += 1
        # Optional EWMA smoothing — display-only, off by default. We
        # do this BEFORE storing _spec_db so peak-hold and the trace
        # both work off the smoothed signal (consistent operator view).
        # Strength 1..10 maps to alpha ≈ 1 - strength/11, so higher
        # strength = smaller alpha = slower / smoother response.
        if (self._smoothing_enabled
                and spec_db is not None and spec_db.size > 0):
            alpha = 1.0 - (self._smoothing_strength / 11.0)
            if (self._smoothed_db is None
                    or self._smoothed_db.shape != spec_db.shape):
                # Initialize from the first frame so we don't show a
                # long fade-in from -inf. Also (re)allocate the scratch
                # buffer to match.
                self._smoothed_db = spec_db.astype(np.float32).copy()
                self._smoothing_scratch = np.empty_like(self._smoothed_db)
            else:
                # In-place EWMA via the lerp form  s += α·(x − s).
                # Three numpy ops, all with `out=` so no per-frame
                # allocation. Equivalent math to s = α·x + (1−α)·s.
                np.subtract(spec_db, self._smoothed_db,
                            out=self._smoothing_scratch)
                self._smoothing_scratch *= alpha
                self._smoothed_db += self._smoothing_scratch
            spec_db = self._smoothed_db   # downstream (peak-hold + draw) sees smoothed
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
        # Optional paint timing — enabled by LYRA_PAINT_DEBUG=1 env var.
        # See __init__ for the rationale. When off, the only added cost
        # is one attribute lookup + branch.
        if self._paint_debug:
            import time as _ptime
            _paint_t0 = _ptime.perf_counter()
        else:
            _paint_t0 = None

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)

        w = self.width()
        h = self.height()

        # Grid — operator-toggleable via Settings → Visuals. Default
        # on; some operators prefer a clean trace-only view.
        if self._show_grid:
            p.setPen(QPen(GRID, 1))
            for i in range(1, 10):
                y = int(h * i / 10)
                p.drawLine(0, y, w, y)
            for i in range(1, 10):
                x = int(w * i / 10)
                p.drawLine(x, 0, x, h)

        # Lyra constellation watermark — drawn after the grid but
        # before the trace, so the spectrum line dominates visually.
        # Edge-faded toward the widget center so it stays out of the
        # trace's way. Toggleable per `_show_constellation`.
        if getattr(self, "_show_constellation", True):
            from lyra.ui.constellation import draw as _draw_constellation
            _draw_constellation(p, w, h)
        # Occasional meteors — opt-in flair, drawn just after the
        # watermark so streaks composite over any visible stars.
        if getattr(self, "_show_meteors", False):
            from lyra.ui.constellation import draw_meteors as _draw_meteors
            _draw_meteors(p, w, h)

        # ── Band-plan overlay ─────────────────────────────────────
        # Top strip: colored sub-band segments (CW / DIG / SSB / FM)
        # per the active region. Drawn BEFORE the trace so the yellow
        # trace remains legible through the thin strip. Second strip
        # just below hosts landmark triangles (FT8 / FT4 / WSPR).
        # Both are skipped when region == "NONE" or individually off.
        BAND_STRIP_H = 10         # colored segment band
        LANDMARK_STRIP_H = 12     # landmark triangles + labels
        top_reserve = 0
        # Reset per-frame; the band-plan branch below adds to it. The
        # spot packer reads this after band-plan paint to know where it
        # can start its first row.
        self._band_plan_reserved_px = 0
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
                        # Cached font — see __init__.
                        p.setFont(self._font_7pt_bold)
                        p.drawText(x0 + 3, BAND_STRIP_H - 2, seg["label"])
            if self._show_band_landmarks:
                top_reserve += LANDMARK_STRIP_H
                marks = _bp.visible_landmarks(
                    self._band_plan_region,
                    self._center_hz, self._span_hz)
                hz_per_px = self._span_hz / w
                center_x = w / 2
                tri_y = BAND_STRIP_H if self._show_band_segments else 0
                from PySide6.QtGui import QPolygonF
                from PySide6.QtCore import QPointF
                # Cached font — see __init__.
                p.setFont(self._font_7pt_bold)
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
                        # Cached font — see __init__.
                        p.setFont(self._font_8pt_bold)
                        p.drawText(ex + 3,
                                   h - 22,
                                   f"{b['name']} EDGE")
            # Publish the reserved height so the spot packer below can
            # avoid colliding with the segment strip + landmark triangles.
            self._band_plan_reserved_px = top_reserve

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
            # Placeholder so an empty spectrum widget doesn't look hung
            p.setPen(QPen(QColor(120, 140, 160, 200), 1))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Waiting for stream…\n\n"
                       "Click ▶ Start on the toolbar")
            if _paint_t0 is not None:
                self._record_paint_time(_paint_t0)
            return

        span_db = self._max_db - self._min_db
        if span_db <= 0:
            # Defensive: collapsed dB range. Force a reasonable
            # working span so the trace renders instead of returning
            # silently (which used to look like a hang).
            span_db = 90.0

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
            # Cached font — see __init__.
            p.setFont(self._font_consolas_8)
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
                    # Cached font — see __init__.
                    p.setFont(self._font_7pt_bold)
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

        # CW Zero (white) reference line — visible only in CW modes.
        # Sits at +/-pitch from the VFO marker, marking the filter
        # center where a clicked CW signal lands and is heard.
        if self._cw_zero_offset_hz and self._span_hz > 0:
            hz_per_px = self._span_hz / max(1, w)
            xz = int(round(cx + self._cw_zero_offset_hz / hz_per_px))
            if 0 <= xz < w:
                p.setPen(QPen(QColor(255, 255, 255, 220), 1, Qt.SolidLine))
                p.drawLine(xz, 0, xz, h)

        # Notch markers — filled rectangle spanning the
        # notch's -3 dB bandwidth (the actual region the filter
        # attenuates). Operators see immediately what's getting
        # killed in Hz, with no guessing about Q-vs-bandwidth.
        #
        # Visual states:
        #   active   = saturated red fill + bright red center line
        #   inactive = desaturated grey fill + grey center line
        #              (notch saved but bypassed in DSP — operator
        #              can A/B without losing placement)
        # Minimum visible width: rectangle is always at least
        # NOTCH_HIT_PX wide so the smallest notches stay grabbable.
        if self._notches and self._span_hz > 0:
            hz_per_px = self._span_hz / max(1, w)
            for freq, width_hz, active, deep in self._notches:
                nf = (freq - self._center_hz) / self._span_hz + 0.5
                if not (0.0 <= nf <= 1.0):
                    continue
                nx = int(nf * w)
                # Width in pixels, with a minimum so the notch is
                # always visible/grabbable even at narrow widths.
                half_px = max(self.NOTCH_HIT_PX,
                              int(width_hz * 0.5 / hz_per_px))
                x_start = max(0, nx - half_px)
                x_end   = min(w - 1, nx + half_px)
                if x_end <= x_start:
                    continue
                if active:
                    fill = QColor(220, 60, 60, 110)        # active red
                    line = QColor(240, 80, 80, 230)
                    label_color = QColor(255, 200, 200)
                else:
                    fill = QColor(140, 140, 150, 80)       # inactive grey
                    line = QColor(170, 170, 180, 180)
                    label_color = QColor(170, 170, 180)
                # Filled rectangle spanning the full notch bandwidth
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRect(x_start, 0, x_end - x_start, h)
                # Edge outlines: thicker for deep (cascade) notches so
                # the operator can see at a glance which notches are
                # running cascaded for ~2× attenuation.
                edge_width = 3 if deep else 1
                p.setPen(QPen(line, edge_width, Qt.SolidLine))
                p.drawLine(x_start, 0, x_start, h)
                p.drawLine(x_end,   0, x_end,   h)
                # Center hairline for precise targeting
                p.setPen(QPen(line, 1, Qt.SolidLine))
                p.drawLine(nx, 0, nx, h)
                # Width label, drawn just to the right of the notch
                # if there's room. "^" suffix marks deep notches.
                if half_px >= 8 and nx + half_px + 60 < w:
                    suffix = "^" if deep else ""
                    p.setPen(label_color)
                    p.drawText(nx + half_px + 4, 14,
                               f"{int(round(width_hz))}{suffix} Hz")

        # Frequency scale on bottom
        p.setPen(QPen(AXIS, 1))
        for i in range(1, 10):
            x = int(w * i / 10)
            offset_hz = (i / 10 - 0.5) * self._span_hz
            freq_khz = (self._center_hz + offset_hz) / 1000.0
            label = f"{freq_khz:,.1f}"
            p.drawText(x - 30, h - 4, label)

        # ── EiBi SW broadcaster overlay (v0.0.9 Step 4c) ──────────
        # Drawn BELOW the TCI spots so live operator activity (TCI
        # spots) wins on z-order when both are visible.  EiBi
        # entries are static-table broadcaster IDs; spots are the
        # live "operators are talking RIGHT NOW" overlay.
        if (self._eibi_overlay_visible
                and self._eibi_entries
                and self._span_hz > 0):
            self._draw_eibi_overlay(p, w, h)
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

            # Spot font — built ONCE on first paint by inheriting the
            # widget's current theme font (so we don't nominate a
            # family Qt can't actually load), then cached. Subsequent
            # paints just reuse self._font_spot.
            if self._font_spot is None:
                self._font_spot = QFont(p.font())
                self._font_spot.setPointSize(8)
                self._font_spot.setBold(True)
            p.setFont(self._font_spot)
            fm = QFontMetrics(self._font_spot)
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
                # Offset spot rows below the band-plan overlay (segment
                # strip + landmark triangles) so callsign boxes don't
                # paint over the colored mode bar or the FT8 / FT4 /
                # WSPR triangle markers. Adds a 3 px gap so the spot
                # boxes sit just under the triangle labels rather than
                # touching them. When band-plan overlay is off
                # (_band_plan_reserved_px == 0) this collapses back to
                # the original "by = 2 + ..." behavior.
                row_y0 = (self._band_plan_reserved_px + 3
                          if self._band_plan_reserved_px > 0 else 2)
                by = row_y0 + chosen_row * (box_h + 2)
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
                fill_alpha   = int(round(200 * alpha_mul))
                text_alpha   = int(round(255 * alpha_mul))

                spot_color = QColor(rc, gc, bc, border_alpha)
                # Dark semi-opaque fill — gives white text inside max
                # contrast while the colored border still identifies
                # the spot type by hue. Standard cluster-display
                # treatment (DXLab, N1MM+, SpotCollector). Replaces
                # the prior spot-color tint that blended visually
                # with the same-colored text.
                dark_fill  = QColor(20, 22, 28, fill_alpha)
                text       = s.get("display") or s.get("call", "")

                rect = QRectF(bx, by, tw, box_h)
                p.setBrush(dark_fill)
                p.setPen(QPen(spot_color, 1))
                p.drawRoundedRect(rect, 3, 3)
                # 1 px black drop shadow for that final bit of pop on
                # bright backgrounds (e.g. spot box overlapping a
                # bright trace peak).
                shadow_rect = QRectF(bx + 1, by + 1, tw, box_h)
                p.setPen(QPen(QColor(0, 0, 0,
                                     max(180, text_alpha)), 1))
                p.drawText(shadow_rect, Qt.AlignCenter, text)
                # White text — pure (255,255,255) reads cleanly
                # against the dark fill at any age-fade level and
                # across all spot border colors.
                p.setPen(QPen(QColor(255, 255, 255, text_alpha), 1))
                p.drawText(rect, Qt.AlignCenter, text)
                # NOTE: no vertical drop line — box position already
                # encodes the spot frequency, and the line was
                # visually noisy on busy bands. Matches the GPU
                # backend's rendering and standard SDR-client
                # convention.

        # ── Snap-target reticle (click-to-tune v1) ───────────────
        # Drawn AFTER spots / notches / passband so it's always on
        # top — operator's pre-click visual confirmation of where
        # the snap will land.  Only when modifier-hover stashed a
        # target in mouseMoveEvent.
        if (self._snap_hover_freq is not None
                and self._snap_tune_show_reticle
                and self._span_hz > 0):
            sx = ((self._snap_hover_freq - self._center_hz)
                  / self._span_hz + 0.5) * w
            if 0 <= sx <= w:
                # Cyan reticle: vertical tick + small crosshair.
                # Subtle but unmistakable; matches the
                # spectrum-trace accent palette.
                p.setPen(QPen(QColor(80, 220, 255, 220), 1,
                              Qt.SolidLine))
                sxi = int(round(sx))
                # Vertical tick spanning the bottom 1/3 of the
                # widget so it doesn't overlap the dB scale labels
                # at the right edge.
                p.drawLine(sxi, h - h // 3, sxi, h - 4)
                # Crosshair at the bottom edge.
                p.drawLine(sxi - 5, h - 4, sxi + 5, h - 4)
                p.drawLine(sxi, h - 9, sxi, h - 4)
                # Small label showing the snap-target freq in kHz
                # offset from center -- helps operator confirm at
                # a glance which signal they're about to hit.
                p.setFont(self._font_7pt_bold)
                offset_hz = int(round(
                    self._snap_hover_freq - self._center_hz))
                if abs(offset_hz) >= 1000:
                    label = f"{offset_hz/1000:+.1f}k"
                else:
                    label = f"{offset_hz:+d}"
                # Place label above the tick, biased to whichever
                # side has room.
                lx = sxi + 4 if sxi < w - 40 else sxi - 38
                p.drawText(lx, h - h // 3 - 4, label)

        # End of paintEvent — record duration for diagnostics if the
        # operator launched with LYRA_PAINT_DEBUG=1. Cheap when off.
        if _paint_t0 is not None:
            self._record_paint_time(_paint_t0)

    def _draw_eibi_overlay(self, p, w: int, h: int) -> None:
        """Draw EiBi station-name labels for entries in
        ``self._eibi_entries`` at their freq positions.

        Layout: multi-row greedy stacking (same approach as TCI
        spots).  Each visible station gets a vertical tick at its
        freq position + a label box.  Labels are placed in the
        first row that doesn't collide horizontally with already-
        placed labels (+ small horizontal padding).  Up to
        ``MAX_EIBI_ROWS`` rows below the band-plan strip; entries
        that don't fit are dropped this frame (still hit-testable
        via the ``_eibi_hit_rects`` cache, but only the placed
        ones are added there).

        Color choices:
          * On-air entries get a muted teal label (visible against
            the dark theme without overwhelming the trace).
          * Off-air entries (when operator opted to show them via
            Settings) get desaturated grey so they don't distract
            from currently-active stations.

        v0.0.9 hotfix: bumped font 8pt -> 10pt for readability;
        added multi-row stacking so labels no longer overlap on
        busy bands.
        """
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QFont, QFontMetrics
        MAX_EIBI_ROWS = 4
        ROW_GAP_PX = 4         # horizontal padding between same-row boxes
        ROW_HEIGHT_GAP = 2     # vertical gap between stacked rows
        self._eibi_hit_rects = []
        center = self._center_hz
        span = self._span_hz
        if span <= 0:
            return
        font = QFont(p.font())
        font.setPointSize(10)
        font.setBold(False)
        p.setFont(font)
        fm = QFontMetrics(font)
        text_h = fm.height()
        row_h = text_h + ROW_HEIGHT_GAP
        # Top y for label band -- below the band-plan strip so we
        # don't overlap the colored allocation rectangles at the
        # top of the widget.
        band_strip_h = (
            self._band_plan_reserved_px + 3
            if self._band_plan_reserved_px > 0 else 18)
        first_row_top = band_strip_h
        # Filter to on-screen entries first.
        visible = []
        for entry in self._eibi_entries:
            freq_hz, station, language, target, on_air = entry
            offset_hz = freq_hz - center
            if abs(offset_hz) > span / 2:
                continue
            x = int(w * (offset_hz / span + 0.5))
            visible.append((x, entry))
        # Sort left-to-right so left-edge entries get first crack
        # at the top row.
        visible.sort(key=lambda t: t[0])
        # Greedy row assignment.
        row_ranges: list[list[tuple[int, int]]] = [
            [] for _ in range(MAX_EIBI_ROWS)]
        for x, entry in visible:
            freq_hz, station, language, target, on_air = entry
            label_text = station[:24]
            tw = fm.horizontalAdvance(label_text) + 4
            # Prefer label to the right of the tick; flip to left
            # if running out of widget width.
            label_x = x + 3
            if label_x + tw > w - 4:
                label_x = max(2, x - tw - 3)
            x_start = label_x - ROW_GAP_PX
            x_end = label_x + tw + ROW_GAP_PX
            chosen_row = -1
            for r in range(MAX_EIBI_ROWS):
                fits = True
                for rs, re in row_ranges[r]:
                    if not (x_end <= rs or x_start >= re):
                        fits = False
                        break
                if fits:
                    chosen_row = r
                    break
            if chosen_row < 0:
                # Couldn't find a free row; drop this label this
                # frame.  Operator can zoom in to see it.
                continue
            row_ranges[chosen_row].append((x_start, x_end))
            label_y = first_row_top + chosen_row * row_h
            # Tick from band-plan strip down to this row's top.
            tick_color = (
                QColor(80, 200, 220, 220) if on_air
                else QColor(120, 130, 140, 140))
            p.setPen(tick_color)
            p.drawLine(x, first_row_top - 2, x, label_y + 1)
            # Label text.
            p.setPen(
                QColor(180, 220, 230, 230) if on_air
                else QColor(160, 170, 180, 170))
            p.drawText(
                label_x, label_y + fm.ascent(), label_text)
            self._eibi_hit_rects.append((
                QRectF(label_x, label_y, tw, text_h),
                entry,
            ))

    def _eibi_hit_at(self, x: float, y: float):
        """Hit-test the EiBi label rects from the latest paint.
        Returns the matching entry tuple or None.  Called from
        mouseMoveEvent to drive the hover tooltip."""
        rects = getattr(self, "_eibi_hit_rects", None)
        if not rects:
            return None
        for rect, entry in rects:
            if rect.contains(x, y):
                return entry
        return None

    def _record_paint_time(self, t0: float) -> None:
        """Append one paint duration to the rolling window and, every
        5 seconds, print a single summary line: frame count, set_spectrum
        call count, mean / p95 / max paint times in ms, plus a basic
        load classification. Used to diagnose the visual-drag bug."""
        import time as _ptime
        now = _ptime.perf_counter()
        dt_ms = (now - t0) * 1000.0
        self._paint_durations.append(dt_ms)
        if self._paint_t0_window == 0.0:
            self._paint_t0_window = now
            return
        if (now - self._paint_t0_window) < 5.0:
            return
        # Window full — emit summary and reset.
        ds = self._paint_durations
        n = len(ds)
        if n == 0:
            self._paint_t0_window = now
            return
        ds_sorted = sorted(ds)
        mean_ms = sum(ds) / n
        p95_ms  = ds_sorted[int(0.95 * (n - 1))]
        max_ms  = ds_sorted[-1]
        elapsed = now - self._paint_t0_window
        fps = n / elapsed if elapsed > 0 else 0.0
        setspec = self._paint_setspec_count
        verdict = "ok"
        if max_ms > 100.0 or mean_ms > 20.0:
            verdict = "SLOW"
        elif max_ms > 50.0 or mean_ms > 10.0:
            verdict = "warm"
        print(f"[Lyra paint] Spectrum {verdict}: "
              f"{n} frames in {elapsed:.1f}s ({fps:.1f} fps), "
              f"set_spectrum={setspec}, "
              f"mean={mean_ms:.1f}ms p95={p95_ms:.1f}ms max={max_ms:.1f}ms")
        self._paint_durations = []
        self._paint_setspec_count = 0
        self._paint_t0_window = now


class WaterfallWidget(_PaintedWidget):
    """Scrolling heatmap. Newest row at top, older rows scroll down."""

    clicked_freq = Signal(float)
    # Payload: (abs_freq_hz, shift_held, global_position). See the
    # SpectrumWidget notes — plain right-click is now a menu trigger,
    # not a silent add-notch, to stop the "I just clicked and a notch
    # appeared" surprise.
    right_clicked_freq = Signal(float, bool, QPoint)
    wheel_at_freq = Signal(float, int)
    # Plain wheel over empty waterfall = tune VFO (panadapter scroll
    # step).  Mirrors SpectrumWidget so the gesture works on either
    # view.
    wheel_tune = Signal(int)
    notch_q_drag = Signal(float, float)

    NOTCH_HIT_PX = 14
    DRAG_TUNE_THRESHOLD_PX = 5  # mirror SpectrumWidget so the same
                                # gesture works on either view

    def __init__(self, parent=None, rows: int = 500):
        super().__init__(parent)
        self._rows = rows
        self._width = 1
        self._data: np.ndarray | None = None
        self._min_db = -110.0
        self._max_db = -30.0
        self._center_hz = 0.0
        self._span_hz = 48000.0
        # See SpectrumWidget for the (freq, width_hz, active, deep) shape.
        self._notches: list[tuple[float, float, bool, bool]] = []
        self._drag_notch: tuple[float, float, bool, bool] | None = None
        self._drag_start_y: int = 0
        # Click-vs-drag-tune state — see SpectrumWidget for design notes.
        self._drag_tune: tuple[int, float, bool] | None = None
        # Palette is looked up by name so the Visuals tab can hot-swap
        # it without reconstructing the widget. Name is persisted via
        # QSettings and restored on startup.
        from . import palettes
        self._palette_name = palettes.DEFAULT_PALETTE
        self._palette = palettes.get(self._palette_name)
        self.setMinimumHeight(200)
        self.setCursor(Qt.CrossCursor)
        # Enable hover events without a mouse button held — needed
        # for the notch-callout tooltip to fire on plain hover.
        self.setMouseTracking(True)

    def set_tuning(self, center_hz: float, span_hz: float):
        self._center_hz = center_hz
        self._span_hz = span_hz

    def set_notches(self, items):
        """Receive notches from Radio.  Items are
        ``(abs_freq_hz, width_hz, active, deep[, depth_db, cascade])``
        tuples.  Tolerates legacy 3-, 4-, and 6-tuple shapes for
        backwards compat."""
        norm = []
        for it in items:
            if len(it) >= 4:
                f, w, a, d = it[0], it[1], it[2], it[3]
            elif len(it) == 3:
                f, w, a = it
                d = False
            else:
                continue
            norm.append((float(f), float(w), bool(a), bool(d)))
        self._notches = norm
        self.update()

    def _freq_at_x(self, x: float) -> float:
        if self.width() <= 0 or self._span_hz <= 0:
            return self._center_hz
        return self._center_hz + (x / self.width() - 0.5) * self._span_hz

    def _notch_half_px(self, width_hz: float) -> int:
        if self._span_hz <= 0 or self.width() <= 0:
            return self.NOTCH_HIT_PX
        hz_per_px = self._span_hz / self.width()
        return max(self.NOTCH_HIT_PX, int(width_hz * 0.5 / hz_per_px))

    def _nearest_notch_at_x(self, x: float):
        if not self._notches or self._span_hz <= 0:
            return None
        best, best_px = None, None
        for freq, width_hz, active, deep in self._notches:
            nf = (freq - self._center_hz) / self._span_hz + 0.5
            if not (0.0 <= nf <= 1.0):
                continue
            px = abs(nf * self.width() - x)
            hit_radius = self._notch_half_px(width_hz)
            if px <= hit_radius and (best_px is None or px < best_px):
                best, best_px = (freq, width_hz, active, deep), px
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
            # Defer tune emit — could be a click OR start of a pan.
            # See SpectrumWidget mousePressEvent for full design notes.
            self._drag_tune = (int(x), float(self._center_hz), False)
            self.setCursor(Qt.OpenHandCursor)
        elif event.button() == Qt.RightButton:
            gpos = event.globalPosition().toPoint()
            self.right_clicked_freq.emit(freq, shift, gpos)

    def mouseMoveEvent(self, event):
        if self._drag_notch is not None:
            freq, start_width, _active, _deep = self._drag_notch
            dy = self._drag_start_y - int(event.position().y())
            if abs(dy) < 4:  # dead zone
                return
            dy_eff = dy - (4 if dy > 0 else -4)
            # Drag UP = narrower (matches wheel up-narrow convention)
            new_width = max(5.0, min(2000.0,
                                     start_width * (1.015 ** -dy_eff)))
            self.notch_q_drag.emit(freq, new_width)
            return
        # Drag-tune (pan). See SpectrumWidget mouseMoveEvent for the
        # sign convention and threshold rationale — same gesture here.
        if self._drag_tune is not None:
            start_x, start_center, in_drag = self._drag_tune
            dx = int(event.position().x()) - start_x
            if not in_drag:
                if abs(dx) < self.DRAG_TUNE_THRESHOLD_PX:
                    return
                in_drag = True
                self._drag_tune = (start_x, start_center, True)
                self.setCursor(Qt.ClosedHandCursor)
            if self._span_hz <= 0 or self.width() <= 0:
                return
            hz_per_px = self._span_hz / self.width()
            new_center = start_center - dx * hz_per_px
            self.clicked_freq.emit(float(new_center))
            return
        # Hover callout — same payload as the
        # spectrum widget so the operator gets identical info no
        # matter which view they hover.
        x = event.position().x()
        notch_hit = self._nearest_notch_at_x(x)
        if notch_hit is not None:
            freq, width_hz, active, deep = notch_hit
            flags = []
            if not active:
                flags.append("INACTIVE")
            if deep:
                flags.append("DEEP")
            flag_str = (" — " + " / ".join(flags)) if flags else ""
            self.setToolTip(
                f"Notch  {freq/1e6:.4f} MHz\n"
                f"Width  {int(round(width_hz))} Hz{flag_str}"
            )
            self.setCursor(Qt.SizeVerCursor)
        else:
            if self.toolTip():
                self.setToolTip("")
            self.setCursor(Qt.CrossCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._drag_notch is not None:
            self._drag_notch = None
            self.setCursor(Qt.CrossCursor)
            return
        if self._drag_tune is not None:
            start_x, _start_center, in_drag = self._drag_tune
            self._drag_tune = None
            self.setCursor(Qt.CrossCursor)
            if not in_drag:
                # Plain click — fire the legacy tune-to-cursor.
                self.clicked_freq.emit(self._freq_at_x(float(start_x)))

    def wheelEvent(self, event):
        if self.width() <= 0:
            return
        x = event.position().x()
        freq = self._freq_at_x(x)
        delta_units = event.angleDelta().y() // 120
        if delta_units == 0:
            super().wheelEvent(event)
            return
        # Wheel routing (mirrors SpectrumWidget):
        #   * Over a notch → wheel_at_freq (panel maps to width adjust)
        #   * Plain wheel elsewhere → tune VFO by panadapter scroll step
        # (No Ctrl+wheel zoom branch here — the waterfall doesn't have
        # an independent zoom; it shares the spectrum's zoom.)
        if self._nearest_notch_at_x(x) is not None:
            self.wheel_at_freq.emit(freq, int(delta_units))
        else:
            self.wheel_tune.emit(int(delta_units))
        event.accept()

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
            # Paint at least the BG so the operator sees the widget
            # is alive even if the very first row hasn't been
            # successfully scaled yet (span check below).
            self.update()

        span = self._max_db - self._min_db
        # Defensive: if dB range collapsed (shouldn't happen since
        # set_waterfall_db_range clamps to >=3 dB span, but QSettings
        # could load stale corrupt values), force a workable span so
        # the waterfall doesn't silently freeze with rows queued.
        if span <= 0:
            span = 80.0
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
        # Smooth pixmap transform — without this, drawImage scales the
        # waterfall bitmap with nearest-neighbor sampling (pixelated /
        # chunky look when the widget is wider than the FFT bin count,
        # which is essentially always at zoom > 1×). Bilinear gives a
        # clean continuous appearance. Antialiasing helps the notch
        # rectangles + VFO marker line blend cleanly against the
        # waterfall colors.
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), BG)
        if self._data is None:
            # Show a placeholder so the operator knows the widget is
            # alive — without this, an empty widget looked indistinguishable
            # from a hung one. Painted center-screen, dim grey.
            p.setPen(QPen(QColor(120, 140, 160, 200), 1))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Waiting for stream…\n\n"
                       "Click ▶ Start on the toolbar")
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

        # Notch markers — match the spectrum widget's filled-rectangle
        # style (the SDR-client convention). Inactive notches render in grey
        # so the operator can A/B without losing placement. Deep
        # notches get thicker edge outlines.
        if self._notches and self._span_hz > 0:
            w = self.width()
            h = self.height()
            hz_per_px = self._span_hz / max(1, w)
            for freq, width_hz, active, deep in self._notches:
                nf = (freq - self._center_hz) / self._span_hz + 0.5
                if not (0.0 <= nf <= 1.0):
                    continue
                nx = int(nf * w)
                half_px = max(self.NOTCH_HIT_PX,
                              int(width_hz * 0.5 / hz_per_px))
                if active:
                    fill = QColor(220, 60, 60, 95)
                    line = QColor(240, 80, 80, 220)
                else:
                    fill = QColor(140, 140, 150, 70)
                    line = QColor(170, 170, 180, 170)
                p.setPen(Qt.NoPen)
                p.setBrush(fill)
                p.drawRect(nx - half_px, 0, 2 * half_px, h)
                # Thicker edge outlines on deep notches so they're
                # visually distinct from normal ones.
                edge_width = 3 if deep else 1
                p.setPen(QPen(line, edge_width, Qt.SolidLine))
                p.drawLine(nx - half_px, 0, nx - half_px, h)
                p.drawLine(nx + half_px, 0, nx + half_px, h)
                p.setPen(QPen(line, 1, Qt.SolidLine))
                p.drawLine(nx, 0, nx, h)
