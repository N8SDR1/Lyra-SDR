"""GPU-accelerated panadapter — Phase A skeleton.

This module is **opt-in** and parallel to `spectrum.py`. The existing
QPainter-based `SpectrumWidget` / `WaterfallWidget` remain the default
production renderers. `SpectrumGpuWidget` is a from-scratch
implementation built directly on Qt's QOpenGLWidget, which gives us
GPU-accelerated rendering through the platform's OpenGL driver
(NVIDIA, AMD, Intel HD/Iris/Arc — all supported).

Design rationale
----------------
We chose QOpenGLWidget over QRhi/Vulkan because:

  - QRhi PySide6 bindings (Qt 6.7+) are still very new — initial
    Phase A.3 attempts hit deep crashes inside Qt's D3D11 backend
    that aren't easily debuggable from Python. See the parked
    `feature/qrhi-panadapter` branch (tag: experiment-qrhi-attempt)
    for the journey.
  - QOpenGLWidget has been in PySide6 for years, has hundreds of
    working examples, and is the path most Qt+Python apps take
    when they need GPU rendering.
  - On Win10/11 with modern GPU drivers, OpenGL Just Works™. If a
    machine's native OpenGL is broken, Qt automatically falls back
    to ANGLE (which translates to D3D11), so we get D3D coverage
    indirectly without writing D3D code.
  - macOS uses Apple's OpenGL implementation (deprecated but still
    functional). Long-term Mac support would migrate to Metal — that's
    a separate project, not a v0.0.5/0.0.6 concern.
  - Vulkan can be revisited later via QRhi if/when PySide6 bindings
    mature, OR if a real performance need arises that OpenGL can't
    handle. Today neither is true.

The Settings → Visuals → Graphics backend combo will gain a third
choice ("OpenGL — GPU-accelerated panadapter"), with the existing
"Software (QPainter)" remaining as the unconditional fallback.
Vulkan stays in the combo as "(future)" — greyed out but visible —
so the operator-facing UI hook is preserved if we ever revisit.

Phase A scope (this file's progress)
------------------------------------
A.2: widget skeleton with shader compile + clear. Proven on the dev
machine.

A.3 (THIS COMMIT): vertex-buffer trace draw with synthetic data.
  - Dynamic VBO sized for MAX_BINS vec2 points
  - VAO that bundles the vertex attribute state (required in 3.3+
    core profile — no default VAO like the legacy compatibility
    profile had)
  - paintGL: bind program + VAO + VBO, upload current trace data
    via QOpenGLBuffer.write(), draw with GL_LINE_STRIP — ONE call
    per frame instead of the per-pixel drawLine loop the QPainter
    widget does
  - Built-in animated sine-wave generator runs until set_spectrum()
    is called, so the widget is self-testing without a Radio
    attached. The standalone demo (Phase A.5) will use the public
    set_spectrum() API; same path Lyra's panel system will use in
    Phase B.
  - 30 Hz repaint timer drives the animation while in synthetic
    mode. Disabled when set_spectrum() takes over.

A.4 will add: streaming-texture waterfall + working draw call
A.5 will add: standalone demo runner (separate file)
A.6 will add: external profile pass

Phase B will integrate into Lyra (Settings UI, real Radio data).
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFunctions_4_3_Core, QOpenGLShader,
    QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


# Background color for the panadapter (RGB normalized 0..1) — matches
# the QPainter widget's `BG = QColor(12, 20, 32)` so visuals stay
# continuous when the operator switches renderers in Settings.
_BG_R, _BG_G, _BG_B = 0.0, 0.0, 0.0    # black, matches the QPainter widget's BG and the waterfall

# Default trace color — Lyra's TRACE QColor(94, 200, 255) normalized.
_DEFAULT_TRACE = (94 / 255.0, 200 / 255.0, 255 / 255.0, 1.0)

# Vertex buffer capacity in number of POINTS (not bytes). Each point
# is a vec2 (8 bytes). 8192 covers any FFT size Lyra uses today and
# leaves headroom for future increases. Allocated once at
# initializeGL() time — reallocating GL buffers is expensive.
MAX_BINS = 8192
BYTES_PER_POINT = 8  # vec2 = 2 × float32

# OpenGL constants we use directly (matches the GL spec values exactly
# so they're stable across drivers / Qt versions). Imported here once
# rather than scattered throughout paintGL bodies.
GL_COLOR_BUFFER_BIT      = 0x4000
GL_LINE_STRIP            = 0x0003
GL_TRIANGLE_STRIP        = 0x0005
GL_FLOAT                 = 0x1406
GL_RED                   = 0x1903
GL_R8                    = 0x8229
GL_UNSIGNED_BYTE         = 0x1401
GL_TEXTURE_2D            = 0x0DE1
GL_TEXTURE0              = 0x84C0
GL_TEXTURE_MIN_FILTER    = 0x2801
GL_TEXTURE_MAG_FILTER    = 0x2800
GL_TEXTURE_WRAP_S        = 0x2802
GL_TEXTURE_WRAP_T        = 0x2803
GL_LINEAR                = 0x2601
GL_NEAREST               = 0x2600
GL_CLAMP_TO_EDGE         = 0x812F
GL_UNPACK_ALIGNMENT      = 0x0CF5
GL_VIEWPORT              = 0x0BA2

# Where the GLSL source files live. Resolved via lyra.resource_root()
# so the path works in BOTH the dev tree (Path(__file__).parent.parent
# of the lyra package == project root) and in PyInstaller frozen
# builds (sys._MEIPASS == bundle root). The build/lyra.spec bundles
# these at lyra/ui/spectrum_gpu_shaders/ so the relative path matches.
#
# Earlier this used `Path(__file__).resolve().parent / "spectrum_gpu_shaders"`,
# which broke in PyInstaller folder-mode bundles: when modules live
# inside the lyra.pyz archive, __file__ reports a path inside that
# archive that isn't a real on-disk directory. The shader programs
# silently linked with empty source → washed-out trace + blank
# waterfall in the .exe even though dev tree was clean.
import lyra as _lyra
_SHADER_DIR = (_lyra.resource_root() / "lyra" / "ui" /
               "spectrum_gpu_shaders")


def lyra_gl_format() -> QSurfaceFormat:
    """Return the QSurfaceFormat all Lyra OpenGL widgets should use.

    Centralized so the widget itself, the demo runner (Phase A.5),
    and the validation script all request the same context profile
    and version. Caller is responsible for setting this on the
    widget BEFORE first show — once a context is created with one
    format, changing the format requires recreating the widget.

    OpenGL 4.3 core profile — covers every Win10/11 GPU since 2013.
    Adds compute shaders + debug output + SSBOs as future-feature
    options. Individual shader sources can stay at #version 330
    core unless they need newer GLSL features.
    """
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    fmt.setSamples(0)
    fmt.setSwapInterval(1)  # vsync on
    return fmt


class SpectrumGpuWidget(QOpenGLWidget):
    """GPU-rendered spectrum + (eventually) waterfall panadapter.

    Phase A.3 state: draws a self-generated synthetic sine-wave
    trace using one draw call per frame against a dynamic vertex
    buffer. The trace animates so successful operation is obvious
    visually. Switches to operator-supplied data on first
    set_spectrum() call.

    Public API:
        set_spectrum(spec_db, min_db=-130, max_db=-30)
            Upload one frame of spectrum data. dB → NDC.y mapping
            uses min_db/max_db as the scale window.
        set_trace_color(QColor)
            Set the trace line color.
        set_tuning(center_hz, span_hz)
            Tell the widget what frequency window it represents, so
            interactions like click-to-tune know what frequency the
            cursor is pointing at.

    Signals:
        clicked_freq(float)
            Emitted when the operator left-clicks anywhere on the
            trace. Payload is the absolute frequency in Hz at the
            click position. Panel wires this to radio.set_freq_hz.
    """

    # Click-to-tune signal — payload is absolute Hz at click x.
    clicked_freq = Signal(float)
    # Right-click signal — payload is (abs_freq_hz, shift_held,
    # global_position). globalPos lets the panel anchor a context
    # menu at the click site; shift_held preserves the legacy
    # shift+right = remove-nearest-notch quick gesture.
    right_clicked_freq = Signal(float, bool, QPoint)
    # Mouse-wheel zoom — payload is direction (+1 = zoom in,
    # -1 = zoom out), one emit per wheel notch.
    wheel_zoom = Signal(int)
    # Wheel-over-notch — payload is (notch_freq_hz, delta_units).
    # Panel routes to set_notch_width_at to adjust the notch's width
    # multiplicatively. Same name as the QPainter widget's signal.
    wheel_at_freq = Signal(float, int)
    # Notch-width drag — payload is (notch_freq_hz, new_width_hz).
    # Emitted while operator drags a notch horizontally to resize it.
    # Signal name kept for compatibility with the existing panel
    # _on_notch_q_drag handler (legacy name from when it carried Q).
    notch_q_drag = Signal(float, float)
    # Y-axis drag in the right-edge dB-label zone — emits proposed
    # (min_db, max_db) range live as the operator drags. Panel
    # forwards to radio.set_spectrum_db_range. Same shape as the
    # QPainter widget's signal.
    db_scale_drag = Signal(float, float)

    # Passband-edge drag — emits proposed RX BW in Hz (already
    # clamped + quantized). Panel forwards to radio.set_rx_bw.
    passband_edge_drag = Signal(int)

    # Band-plan landmark click — payload is (freq_hz, mode). Panel
    # routes to set_freq_hz + set_mode so a click on FT8/FT4/WSPR
    # triangles tunes the radio and switches mode in one shot.
    landmark_clicked = Signal(int, str)

    # Width of the right-edge strip that grabs dB-scale drag instead
    # of click-to-tune. Matches the QPainter widget's value.
    DB_SCALE_ZONE_PX = 50
    # Click halo around each passband edge for grab detection.
    PASSBAND_HIT_PX = 6
    # Click halo around each notch (also the minimum visual width).
    NOTCH_HIT_PX = 14

    # Synthetic-data point count — mimics Lyra's typical FFT size
    # (4096) so the test exercises the same draw cost as real usage.
    _SYNTHETIC_N = 4096

    # How often to repaint while in synthetic-data mode. 30 Hz is a
    # comfortable visual rate that exercises the upload+draw cycle
    # without burning CPU on a passive demo.
    _SYNTHETIC_HZ = 30

    def __init__(self, parent=None, synthetic: bool = False):
        """Construct the GPU spectrum widget.

        synthetic: if True, the widget runs an internal sine-wave
            generator at ~30 Hz until set_spectrum() is first called.
            Useful for the standalone demo runner and for ad-hoc
            "is this widget working" tests. **Defaults to FALSE** —
            production integration (Lyra's SpectrumPanel) creates
            the widget without synthetic mode, so the trace stays
            blank until Radio.spectrum_ready starts feeding data.
            Without this default, synthetic frames would be visible
            briefly at startup before the first real frame arrives.
        """
        super().__init__(parent)
        # Per-widget format (vs setting the global default) keeps the
        # GL context choice local to this widget tree.
        self.setFormat(lyra_gl_format())

        # GL function table — bound to the active context in
        # initializeGL once the context is current. Native 4.3 core
        # access without depending on PyOpenGL.
        self._gl: Optional[QOpenGLFunctions_4_3_Core] = None

        # GPU-side handles. All None until initializeGL runs; cleaned
        # up automatically by QObject parent ownership when the widget
        # is destroyed.
        self._prog_trace: Optional[QOpenGLShaderProgram] = None
        self._vbo_trace: Optional[QOpenGLBuffer] = None
        self._vao_trace: Optional[QOpenGLVertexArrayObject] = None
        # Cached attribute / uniform locations resolved once at link
        # time. -1 means "not found" (defensive — shouldn't happen
        # if the shader compiled correctly).
        self._loc_position: int = -1
        self._loc_trace_color: int = -1

        # CPU-side trace data. Pre-allocated to MAX_BINS so paintGL
        # never allocates. Shape (N, 2) float32; column 0 = NDC.x,
        # column 1 = NDC.y. _trace_n is the number of valid points
        # currently in the buffer prefix.
        self._trace_xy = np.zeros((MAX_BINS, 2), dtype=np.float32)
        self._trace_n = 0

        # Trace color — operator-overridable via set_trace_color.
        self._trace_color: tuple[float, float, float, float] = _DEFAULT_TRACE

        # Tuning state — what frequency window the widget currently
        # represents. Updated via set_tuning(); used by mouse
        # interactions (click-to-tune) and overlays that need to
        # know widget-x → frequency mapping. Default values are
        # placeholders that produce sensible behavior before the
        # first set_tuning call.
        self._center_hz: float = 0.0
        self._span_hz: float = 48000.0

        # Latest (min_db, max_db) range — stashed by set_spectrum
        # each frame so the Y-axis drag handler knows where to start
        # from. Default placeholders match the QPainter widget.
        self._min_db: float = -130.0
        self._max_db: float = -30.0

        # dB-scale drag state. None when not dragging; tuple of
        # (start_y, start_min, start_max) while a drag is in flight.
        self._db_drag: Optional[tuple[int, float, float]] = None

        # Noise-floor reference line (Phase B.10). dB value updated
        # from radio.noise_floor_changed; color may be overridden by
        # the operator via Visuals → Colors.
        self._noise_floor_db: Optional[float] = None
        self._nf_color_hex: str = ""  # empty = use sage-green default

        # Passband overlay (Phase B.11). lo/hi are Hz offsets from
        # the carrier (negative = below, positive = above). For USB
        # we have lo=0, hi=+bw; for LSB lo=-bw, hi=0; for SSB-equiv
        # symmetric modes lo=-bw/2, hi=+bw/2; CW has both off-center
        # by the pitch.
        self._passband_lo_hz: float = 0.0
        self._passband_hi_hz: float = 0.0
        # Active passband-edge drag — None or "lo" / "hi".
        self._drag_pb_edge: Optional[str] = None

        # CW Zero (white) reference line offset from the VFO marker,
        # in Hz. +pitch in CWU, -pitch in CWL, 0 elsewhere (line
        # hidden). Set via radio.cw_zero_offset_changed → set_cw_zero_offset.
        self._cw_zero_offset_hz: int = 0
        # Lyra constellation watermark visibility — operator toggle.
        # Default ON; switched via Settings → Visuals.
        self._show_constellation: bool = True
        # Occasional meteor streaks across the panadapter — independent
        # toggle, default OFF (opt-in flair).
        self._show_meteors: bool = False
        # Grid lines (9×9 horiz/vert dotted divisions). Default ON;
        # operator toggle via Settings → Visuals.
        self._show_grid: bool = True
        # DX/contest spots — list of dicts as published by Radio.
        # Each spot dict: freq_hz, mode, call/display, ts (monotonic),
        # color (0xAARRGGBB). Same payload shape as the CPU widget's
        # _spots so the rendering code can be a 1:1 port.
        self._spots: list[dict] = []
        # Age-fade lifetime in seconds. 0 = no fade. Mirrors radio
        # default of 600 s (10 min) — set authoritatively from
        # radio.spot_lifetime_s when the panel wires the signal.
        self._spot_lifetime_s: int = 600
        # Optional mode filter — empty set = render all spots.
        # Populated via set_spot_mode_filter(csv_or_set).
        self._spot_mode_filter: set[str] = set()
        # Vertical pixels at the top of the panadapter reserved by
        # the band-plan strip (segment colors + landmark triangles).
        # Spot row 0 sits BELOW this offset so callsign boxes don't
        # paint over the band-plan visuals. 0 when band plan is
        # off. Recomputed in _draw_band_plan each frame.
        self._band_plan_reserved_px: int = 0

        # ── Band-plan overlay state ───────────────────────────────
        # Same toggles as the CPU widget. NONE = no overlay drawn.
        self._band_plan_region: str = "NONE"
        self._show_band_segments: bool = True
        self._show_band_landmarks: bool = True
        self._show_band_edge_warn: bool = True
        # Per-segment color overrides (CW/DIG/SSB/FM) layered on top
        # of band_plan.SEGMENT_COLORS at paint time.
        self._user_segment_colors: dict[str, str] = {}
        # Cache of the landmarks rendered this frame, used by the
        # mouse-press handler to dispatch click-to-tune on triangles.
        # Tuples of (freq_hz, mode, x_px, y_top, y_bot).
        self._landmark_hit_cache: list[tuple[int, str, int, int, int]] = []

        # ── Peak-markers overlay state ────────────────────────────
        # In-passband peak-hold trace — same buffer shape as the live
        # spec_db, decayed at peak_markers_decay_dbps and clamped up
        # to the live values each frame. Disabled by default.
        self._peak_markers_enabled: bool = False
        self._peak_markers_decay_dbps: float = 10.0
        self._peak_markers_style: str = "dots"   # "line" / "dots" / "triangles"
        self._peak_markers_show_db: bool = False
        self._user_peak_color: str = ""
        self._peak_hold_db: Optional[np.ndarray] = None
        self._peak_last_ts: Optional[float] = None

        # Display-only EWMA smoothing — see spectrum.py for the same
        # pattern. Off by default; toggled from Settings → Display.
        self._smoothing_enabled: bool = False
        self._smoothing_strength: int = 4    # 1..10
        self._smoothed_db: Optional[np.ndarray] = None
        # Pre-allocated scratch for in-place EWMA — see spectrum.py.
        self._smoothing_scratch: Optional[np.ndarray] = None

        # ── Cached QFonts (paint-loop optimization) ──────────────────
        # See spectrum.py.__init__ for rationale. Same fonts as the
        # CPU widget plus a 9pt-bold (status overlays) and a spot
        # font with explicit family fallbacks to render emoji flags.
        from PySide6.QtGui import QFont as _QFont
        self._font_7pt_bold = _QFont()
        self._font_7pt_bold.setPointSize(7)
        self._font_7pt_bold.setBold(True)
        self._font_8pt = _QFont()
        self._font_8pt.setPointSize(8)
        self._font_8pt_bold = _QFont()
        self._font_8pt_bold.setPointSize(8)
        self._font_8pt_bold.setBold(True)
        self._font_9pt_bold = _QFont()
        self._font_9pt_bold.setPointSize(9)
        self._font_9pt_bold.setBold(True)
        self._font_consolas_8 = _QFont("Consolas")
        self._font_consolas_8.setPointSize(8)
        # Spot font with explicit family fallback chain — same chain
        # the per-frame allocator was using; just built once. Qt picks
        # per-glyph: callsign chars render in Exo 2 (theme primary),
        # flag emoji falls through to Segoe UI Emoji.
        self._font_spot = _QFont()
        self._font_spot.setFamilies([
            "Exo 2",            # theme primary — covers Latin / ASCII
            "Segoe UI",         # ASCII backup
            "Arial",            # universal ASCII backup
            "Segoe UI Emoji",   # flag emoji fallback (regional indicators)
        ])
        self._font_spot.setPointSize(9)
        self._font_spot.setBold(True)

        # ── Paint instrumentation (opt-in via LYRA_PAINT_DEBUG=1) ────
        # Mirrors the CPU widget's instrumentation. Enabling this env
        # var prints a one-line summary every 5 seconds with frame
        # count, set_spectrum call count, mean / p95 / max paint
        # times, and a verdict tag. Used to diagnose the visual-drag
        # bug. Default off; near-zero cost when disabled.
        import os as _os
        self._paint_debug = (_os.environ.get("LYRA_PAINT_DEBUG", "")
                             .strip() in ("1", "true", "True"))
        self._paint_t0_window: float = 0.0
        self._paint_durations: list[float] = []
        self._paint_setspec_count: int = 0

        # Notch markers (Phase B.13). Each entry is
        # (abs_freq_hz, width_hz, active, deep). Updated from
        # radio.notches_changed via the panel.
        self._notches: list[tuple[float, float, bool, bool]] = []
        # Active notch-width drag — None or the abs_freq_hz of the
        # notch being resized. While set, mouseMoveEvent emits
        # notch_q_drag with the proposed new width based on the
        # cursor's horizontal distance from the notch center.
        self._drag_notch_freq: Optional[float] = None

        # Synthetic-data animation state. _synthetic_active toggles
        # OFF the moment set_spectrum() is called (real data takes
        # over). Default False — see constructor docstring.
        self._synthetic_active = bool(synthetic)
        self._t0 = time.monotonic()

        # Drives synthetic-mode animation. Real data path doesn't
        # need this — set_spectrum's caller (Radio in Phase B) will
        # request repaints via update() at FFT rate. Only started
        # when synthetic mode is on.
        self._synth_timer = QTimer(self)
        self._synth_timer.setInterval(int(1000 / self._SYNTHETIC_HZ))
        self._synth_timer.timeout.connect(self.update)
        if self._synthetic_active:
            self._synth_timer.start()

    # ── Public data API ────────────────────────────────────────────

    def set_spectrum(self, spec_db: np.ndarray,
                     min_db: float = -130.0,
                     max_db: float = -30.0) -> None:
        """Upload a frame of spectrum data for the next render.

        spec_db: 1-D numpy array of dB values (one per FFT bin).
        min_db/max_db: scale window — bins below min_db render at
            the bottom of the widget, bins above max_db at the top.

        Stops the synthetic animation timer on first call (operator
        data takes over). The caller is responsible for triggering
        subsequent repaints via the widget's update() — typically
        Radio.spectrum_ready will already be doing that at FFT rate.
        """
        n = int(min(spec_db.shape[0], MAX_BINS))
        if n < 2:
            return
        # Paint-debug counter: how often spectrum data is arriving.
        # Compare against frame count in the 5-second summary to
        # detect rate anomalies.
        if self._paint_debug:
            self._paint_setspec_count += 1
        # Cache the range so the Y-axis drag handler knows the
        # current (min, max) to compute proposed deltas from.
        self._min_db = float(min_db)
        self._max_db = float(max_db)
        # Optional EWMA smoothing (display only, off by default). We
        # operate on the same `n`-prefix the trace + peak-hold use so
        # everything stays in sync. Strength 1..10 → alpha ≈ 1 -
        # strength/11; higher strength = slower / smoother response.
        if self._smoothing_enabled:
            spec_pref = spec_db[:n].astype(np.float32, copy=False)
            alpha = 1.0 - (self._smoothing_strength / 11.0)
            if (self._smoothed_db is None
                    or self._smoothed_db.shape != spec_pref.shape):
                self._smoothed_db = spec_pref.copy()
                self._smoothing_scratch = np.empty_like(self._smoothed_db)
            else:
                # In-place EWMA via lerp:  s += α·(x − s). Reuses the
                # pre-allocated scratch so each frame is allocation-free.
                # See spectrum.py for the rationale (~1 MB/s GC pressure
                # under the old `alpha * x` allocation pattern at 60 fps).
                np.subtract(spec_pref, self._smoothed_db,
                            out=self._smoothing_scratch)
                self._smoothing_scratch *= alpha
                self._smoothed_db += self._smoothing_scratch
            # Substitute the smoothed buffer for downstream consumers
            # (peak-hold + GL trace upload) — both already use spec_db[:n].
            spec_db = self._smoothed_db
            n = self._smoothed_db.shape[0]
        # Peak-hold buffer maintenance — same algo as the CPU
        # widget: linear dB/sec decay, then clamp-up to the live
        # spectrum so peaks can never sit below current signal.
        # Use the truncated `n`-prefix so the buffer always matches
        # what the GL trace will draw.
        if self._peak_markers_enabled:
            spec_view = spec_db[:n].astype(np.float32, copy=False)
            now = time.monotonic()
            if (self._peak_hold_db is None
                    or self._peak_hold_db.shape != spec_view.shape):
                self._peak_hold_db = spec_view.copy()
                self._peak_last_ts = now
            else:
                dt = max(0.005, now - (self._peak_last_ts or now))
                self._peak_last_ts = now
                self._peak_hold_db -= self._peak_markers_decay_dbps * dt
                np.maximum(self._peak_hold_db, spec_view,
                           out=self._peak_hold_db)
        # Map bins → NDC.x: linear from -1 (left) to +1 (right).
        xs = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        # Map dB → NDC.y: linear from -1 (bottom = min_db) to +1
        # (top = max_db). Clipped so out-of-range values stick to
        # the edges instead of going off-screen.
        span = max(1e-6, max_db - min_db)
        ys = ((spec_db.astype(np.float32) - min_db) / span) * 2.0 - 1.0
        np.clip(ys, -1.0, 1.0, out=ys)
        self._trace_xy[:n, 0] = xs
        self._trace_xy[:n, 1] = ys
        self._trace_n = n
        # Real data takes over — disable synthetic generator.
        if self._synthetic_active:
            self._synthetic_active = False
            self._synth_timer.stop()
        self.update()

    def set_trace_color(self, color: QColor) -> None:
        """Set the trace line color.

        Applied via the trace.frag `traceColor` uniform on the next
        paint. Stored as float tuple for cheap upload.
        """
        self._trace_color = (
            color.redF(), color.greenF(), color.blueF(), color.alphaF(),
        )
        self.update()

    def set_tuning(self, center_hz: float, span_hz: float) -> None:
        """Tell the widget what frequency window it currently shows.

        center_hz: absolute Hz at widget horizontal-center.
        span_hz:   total Hz span across widget width.

        Doesn't trigger a repaint — VFO marker is at center pixel
        either way. Just updates state for click-to-tune math and
        for any future overlays (passband, peak markers, etc.) that
        need bin↔frequency mapping.
        """
        self._center_hz = float(center_hz)
        self._span_hz = float(max(1.0, span_hz))

    def set_noise_floor_db(self, db: float) -> None:
        """Update the noise-floor reference value (Phase B.10).

        Connected to radio.noise_floor_changed in the panel. -999 is
        the convention for "noise floor disabled" (Visuals checkbox
        off) — we suppress drawing in that case.
        """
        self._noise_floor_db = (
            None if db < -150.0 else float(db))
        self.update()

    def set_noise_floor_color(self, hex_str: str) -> None:
        """Override the noise-floor line color. Empty string =
        revert to the sage-green default."""
        self._nf_color_hex = str(hex_str or "")
        self.update()

    def set_passband(self, lo_hz: float, hi_hz: float) -> None:
        """RX filter passband, as Hz offsets from the carrier.
        Connected to radio.passband_changed. Updates the cyan
        overlay rectangle on the next paint."""
        self._passband_lo_hz = float(lo_hz)
        self._passband_hi_hz = float(hi_hz)
        self.update()

    def set_cw_zero_offset(self, offset_hz: int) -> None:
        """CW Zero (white) reference line offset from the VFO marker.
        Connected to radio.cw_zero_offset_changed. +pitch in CWU,
        -pitch in CWL, 0 outside CW (line hidden)."""
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

    # ── Band-plan overlay API (mirrors the CPU widget) ─────────────
    def set_band_plan_region(self, region_id: str) -> None:
        """Active region id for the band-plan overlay. 'NONE' hides
        the overlay entirely."""
        self._band_plan_region = str(region_id) or "NONE"
        self.update()

    def set_band_plan_show_segments(self, on: bool) -> None:
        """Toggle the colored sub-band segment strip (top of widget)."""
        self._show_band_segments = bool(on)
        self.update()

    def set_band_plan_show_landmarks(self, on: bool) -> None:
        """Toggle the landmark triangles strip (FT8/FT4/WSPR/etc.)."""
        self._show_band_landmarks = bool(on)
        self.update()

    def set_band_plan_show_edge_warn(self, on: bool) -> None:
        """Toggle the red dashed band-edge warning line."""
        self._show_band_edge_warn = bool(on)
        self.update()

    def set_segment_color_overrides(self, overrides: dict) -> None:
        """Merge a {kind: hex} dict of per-segment color overrides
        (CW / DIG / SSB / FM). Absent keys use band_plan defaults."""
        self._user_segment_colors = {
            str(k).upper(): str(v)
            for k, v in dict(overrides or {}).items() if v}
        self.update()

    # ── Spectrum smoothing API (display-only EWMA) ─────────────────
    def set_spectrum_smoothing_enabled(self, on: bool) -> None:
        """Display-only EWMA smoothing of the spectrum trace. Off by
        default. Disabling clears the EWMA buffer so a later re-enable
        starts clean rather than picking up a stale memory."""
        self._smoothing_enabled = bool(on)
        if not self._smoothing_enabled:
            self._smoothed_db = None
        self.update()

    def set_spectrum_smoothing_strength(self, strength: int) -> None:
        """Smoothing strength 1..10 (higher = smoother / slower response)."""
        self._smoothing_strength = max(1, min(10, int(strength)))
        self.update()

    # ── Peak-markers API (mirrors the CPU widget) ──────────────────
    def set_peak_markers_enabled(self, on: bool) -> None:
        """Toggle the in-passband peak-hold overlay. Disabling clears
        the buffer so a later re-enable starts clean."""
        self._peak_markers_enabled = bool(on)
        if not self._peak_markers_enabled:
            self._peak_hold_db = None
            self._peak_last_ts = None
        self.update()

    def set_peak_markers_decay_dbps(self, dbps: float) -> None:
        self._peak_markers_decay_dbps = float(dbps)

    def set_peak_markers_style(self, name: str) -> None:
        name = (name or "dots").strip().lower()
        if name not in ("line", "dots", "triangles"):
            name = "dots"
        self._peak_markers_style = name
        self.update()

    def set_peak_markers_show_db(self, on: bool) -> None:
        self._peak_markers_show_db = bool(on)
        self.update()

    def set_peak_markers_color(self, hex_str: str) -> None:
        """Override the peak-marker color. Empty string reverts to
        the default warm amber."""
        self._user_peak_color = str(hex_str or "")
        self.update()

    def set_spots(self, spots: list) -> None:
        """Set the spot list. Each entry is a dict with keys:
        freq_hz, mode, call/display, ts (monotonic), color (0xAARRGGBB).
        Connected to radio.spots_changed in panels.py."""
        self._spots = list(spots)
        self.update()

    def set_spot_lifetime_s(self, seconds: int) -> None:
        """Drives the age-fade on spot boxes. Older spots fade toward
        30% alpha as they approach the lifetime limit. 0 = no fade."""
        self._spot_lifetime_s = max(0, int(seconds))
        self.update()

    def set_spot_mode_filter(self, csv_or_set) -> None:
        """Accept a CSV string ('FT8,CW,SSB') or a pre-built set.
        Empty = no filter, render all spots. SSB in CSV expands to
        match USB/LSB/SSB so a single 'SSB' selector covers all
        three sideband modes."""
        if isinstance(csv_or_set, (set, frozenset)):
            self._spot_mode_filter = {m.upper() for m in csv_or_set}
        else:
            csv = str(csv_or_set or "").strip()
            if not csv:
                self._spot_mode_filter = set()
            else:
                raw = [m.strip().upper()
                       for m in csv.split(",") if m.strip()]
                out: set[str] = set()
                for m in raw:
                    if m == "SSB":
                        out.update(("SSB", "USB", "LSB"))
                    else:
                        out.add(m)
                self._spot_mode_filter = out
        self.update()

    def set_notches(self, notches: list) -> None:
        """Set the notch list for overlay drawing (Phase B.13).
        notches: list of (abs_freq_hz, width_hz, active, deep)
        tuples — same shape as Radio.notch_details. Connected to
        radio.notches_changed."""
        self._notches = list(notches) if notches else []
        self.update()

    # ── Landmark hit-test (band-plan click-to-tune) ────────────────
    def _landmark_at(self, x: float, y: float):
        """Return (freq_hz, mode) if (x,y) hits a landmark triangle,
        else None. Cache is rebuilt every paint by `_draw_band_plan`,
        so the geometry is always the most recent frame's. ±5 px of
        slop on x so triangles stay grabbable at small sizes."""
        if (self._band_plan_region == "NONE"
                or not self._show_band_landmarks
                or not self._landmark_hit_cache):
            return None
        for freq, mode, mx, y_top, y_bot in self._landmark_hit_cache:
            # Allow a tiny bit of slack below the triangle so the
            # operator doesn't have to thread the eye of a needle.
            if abs(x - mx) <= 5 and y_top - 1 <= y <= y_bot + 2:
                return (freq, mode)
        return None

    # ── Notch hit-test (Phase B.14) ────────────────────────────────
    def _notch_at_x(self, x: int) -> Optional[float]:
        """Return abs_freq_hz of the notch whose hit-zone contains
        x, or None. The hit zone is the visible rectangle expanded
        to NOTCH_HIT_PX min half-width (matches the QPainter widget)."""
        if not self._notches or self._span_hz <= 0:
            return None
        w = self.width()
        if w <= 0:
            return None
        hz_per_px = self._span_hz / max(1, w)
        for freq, width_hz, active, deep in self._notches:
            nf = (freq - self._center_hz) / self._span_hz + 0.5
            if not (0.0 <= nf <= 1.0):
                continue
            nx = int(nf * w)
            half_px = max(self.NOTCH_HIT_PX,
                          int(width_hz * 0.5 / hz_per_px))
            if abs(x - nx) <= half_px:
                return float(freq)
        return None

    # ── Passband-edge geometry helpers (Phase B.11) ────────────────
    def _passband_edge_px(self) -> tuple[Optional[int], Optional[int]]:
        """Return (x_lo, x_hi) pixels for the passband edges, or
        (None, None) if the passband is invalid / not visible."""
        if self._passband_hi_hz <= self._passband_lo_hz:
            return (None, None)
        if self._span_hz <= 0:
            return (None, None)
        w = self.width()
        if w <= 0:
            return (None, None)
        hz_per_px = self._span_hz / w
        center_x = w / 2
        x_lo = int(center_x + self._passband_lo_hz / hz_per_px)
        x_hi = int(center_x + self._passband_hi_hz / hz_per_px)
        return (x_lo, x_hi)

    def _passband_edge_at_x(self, x: float) -> Optional[str]:
        """Return 'lo', 'hi', or None depending on whether x is
        within PASSBAND_HIT_PX of a passband edge."""
        x_lo, x_hi = self._passband_edge_px()
        if x_lo is None or x_hi is None:
            return None
        if abs(x - x_lo) <= self.PASSBAND_HIT_PX:
            return "lo"
        if abs(x - x_hi) <= self.PASSBAND_HIT_PX:
            return "hi"
        return None

    def _proposed_bw_from_drag(self, x: float) -> Optional[int]:
        """Translate a drag cursor x-position into a new proposed RX
        BW in Hz, clamped + quantized. None if the result would be
        nonsensical for the current passband geometry. Logic ported
        verbatim from the QPainter widget."""
        if self._span_hz <= 0 or self.width() <= 0:
            return None
        hz_per_px = self._span_hz / self.width()
        center_x = self.width() / 2
        offset_hz = (x - center_x) * hz_per_px
        lo, hi = self._passband_lo_hz, self._passband_hi_hz
        edge = self._drag_pb_edge
        if edge is None:
            return None
        if lo == 0 and hi > 0:
            # USB / DIGU
            if edge != "hi":
                return None
            bw = int(round(offset_hz))
        elif hi == 0 and lo < 0:
            # LSB / DIGL
            if edge != "lo":
                return None
            bw = int(round(-offset_hz))
        elif lo < 0 and hi > 0 and abs(lo + hi) <= max(5, abs(lo) // 20):
            # Symmetric around carrier
            bw = int(round(2 * abs(offset_hz)))
        else:
            # CW asymmetric — pitch center = (lo + hi) / 2
            pitch_center = (lo + hi) / 2
            bw = int(round(2 * abs(offset_hz - pitch_center)))
        bw = max(50, min(15000, bw))
        bw = 50 * ((bw + 25) // 50)
        return bw

    # ── Mouse interactions ─────────────────────────────────────────
    def _freq_at_pixel(self, x: int) -> float:
        """Convert widget-x pixel to absolute frequency in Hz."""
        w = max(1, self.width())
        hz_per_px = self._span_hz / w
        return self._center_hz + (x - w / 2.0) * hz_per_px

    def _is_in_db_zone(self, x: int) -> bool:
        """True if x falls in the right-edge dB-scale grab strip."""
        w = self.width()
        return (w - self.DB_SCALE_ZONE_PX) <= x <= w

    def mousePressEvent(self, event) -> None:
        """Mouse button handler. Priority order (left button):
          1. Notch hit (Phase B.14) — drag-to-resize the notch width
          2. Passband-edge drag (Phase B.11) — if near passband edge
          3. dB-scale drag (Phase B.8) — if in right-edge zone
          4. Click-to-tune (Phase B.5) — anywhere else
        Right-click → context menu (Phase B.6)
        """
        x = int(event.position().x())
        y = int(event.position().y())
        if event.button() == Qt.LeftButton:
            # Highest priority: landmark triangle click. The hit cache
            # was rebuilt on the most recent paint, so the geometry is
            # current. A click on FT8/FT4/WSPR/etc. tunes + switches
            # mode in one shot.
            lm = self._landmark_at(x, y)
            if lm is not None:
                self.landmark_clicked.emit(int(lm[0]), str(lm[1]))
                return
            notch_freq = self._notch_at_x(x)
            if notch_freq is not None:
                # Press on a notch → enter width-drag mode. Suppress
                # click-to-tune so clicking a notch doesn't retune
                # the radio. Width updates fire on mouseMoveEvent.
                self._drag_notch_freq = notch_freq
                return
            edge = self._passband_edge_at_x(x)
            if edge is not None:
                self._drag_pb_edge = edge
            elif self._is_in_db_zone(x):
                self._db_drag = (y, self._min_db, self._max_db)
            else:
                self.clicked_freq.emit(float(self._freq_at_pixel(x)))
        elif event.button() == Qt.RightButton:
            shift_held = bool(event.modifiers() & Qt.ShiftModifier)
            self.right_clicked_freq.emit(
                float(self._freq_at_pixel(x)),
                shift_held,
                event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """Live drag dispatch — notch width / passband edge / dB
        scale, whichever drag is currently active."""
        x = event.position().x()
        # Notch width drag — emit notch_q_drag with proposed new width
        if self._drag_notch_freq is not None and self._span_hz > 0 \
                and self.width() > 0:
            hz_per_px = self._span_hz / self.width()
            center_x = self.width() / 2
            click_freq = self._center_hz + (x - center_x) * hz_per_px
            # Width = 2× the absolute distance from notch center to
            # cursor (in Hz). Clamped to a sensible range; quantized
            # to 10 Hz.
            half_width = abs(click_freq - self._drag_notch_freq)
            new_width = max(10.0, min(8000.0, 2.0 * half_width))
            new_width = 10.0 * round(new_width / 10.0)
            self.notch_q_drag.emit(
                float(self._drag_notch_freq), float(new_width))
            return
        # Passband-edge drag — emit proposed BW
        if self._drag_pb_edge is not None:
            proposed = self._proposed_bw_from_drag(x)
            if proposed is not None:
                self.passband_edge_drag.emit(proposed)
            return
        # dB-scale drag — emit proposed (min, max)
        if self._db_drag is not None:
            start_y, start_min, start_max = self._db_drag
            dy = int(event.position().y()) - start_y
            h = max(1, self.height())
            span = start_max - start_min
            db_delta = -dy * (span / h)
            new_min = start_min + db_delta
            new_max = start_max + db_delta
            new_min = max(-150.0, min(-3.0, new_min))
            new_max = max(new_min + 3.0, min(0.0, new_max))
            self.db_scale_drag.emit(float(new_min), float(new_max))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._db_drag = None
            self._drag_pb_edge = None
            self._drag_notch_freq = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        """Mouse wheel = zoom OR notch-width adjust.

        Phase B.14: wheel-over-notch adjusts that notch's width
        (via the wheel_at_freq signal which the panel routes to
        radio's notch-width API). Wheel over empty spectrum = zoom
        (wheel_zoom signal).
        """
        dy = event.angleDelta().y()
        if dy == 0:
            event.accept()
            return
        delta = 1 if dy > 0 else -1
        x = int(event.position().x())
        notch_freq = self._notch_at_x(x)
        if notch_freq is not None:
            # Wheel over notch → adjust width. Panel handles the
            # actual width math against radio.set_notch_width_at.
            self.wheel_at_freq.emit(float(notch_freq), delta)
        else:
            self.wheel_zoom.emit(delta)
        event.accept()

    # ── QOpenGLWidget virtual method overrides ─────────────────────

    def initializeGL(self) -> None:
        """Called by Qt once after the OpenGL context becomes current.

        This is where we build all GPU-side resources: shader
        programs, vertex buffers, vertex array objects. Fires once
        at first show; if the widget is reparented to a different
        top-level window with a different GL context, Qt MAY call
        this again — be idempotent (drop and rebuild).
        """
        # GL function table for the current context.
        self._gl = QOpenGLFunctions_4_3_Core()
        self._gl.initializeOpenGLFunctions()

        # Hook context-destruction so we can release GPU resources
        # while the GL context is still valid. Without this, Python's
        # GC may run after Qt has torn down the context, producing
        # "destroy called without current context" warnings (and in
        # extreme cases leaking GPU memory until the process exits).
        ctx = self.context()
        if ctx is not None:
            ctx.aboutToBeDestroyed.connect(self._cleanup_gl_resources)

        # ── Shader program ────────────────────────────────────────
        # If initializeGL fires again, drop any prior program first.
        if self._prog_trace is not None:
            self._prog_trace.removeAllShaders()
            self._prog_trace.deleteLater()
            self._prog_trace = None
        prog = QOpenGLShaderProgram(self)
        ok = (prog.addShaderFromSourceFile(
                  QOpenGLShader.ShaderTypeBit.Vertex,
                  str(_SHADER_DIR / "trace.vert"))
              and prog.addShaderFromSourceFile(
                  QOpenGLShader.ShaderTypeBit.Fragment,
                  str(_SHADER_DIR / "trace.frag")))
        if not ok:
            raise RuntimeError(
                "Trace shader compile failed:\n" + prog.log())
        if not prog.link():
            raise RuntimeError(
                "Trace shader link failed:\n" + prog.log())
        self._prog_trace = prog
        # Cache locations so paintGL doesn't have to resolve them
        # by string lookup every frame.
        self._loc_position    = prog.attributeLocation("position")
        self._loc_trace_color = prog.uniformLocation("traceColor")

        # ── Vertex Buffer Object (VBO) ─────────────────────────────
        # Dynamic = will be re-uploaded every frame. Allocated to
        # MAX_BINS × 8 bytes; we'll only write/draw the prefix in use.
        if self._vbo_trace is not None:
            self._vbo_trace.destroy()
        self._vbo_trace = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._vbo_trace.setUsagePattern(QOpenGLBuffer.UsagePattern.DynamicDraw)
        self._vbo_trace.create()
        self._vbo_trace.bind()
        self._vbo_trace.allocate(MAX_BINS * BYTES_PER_POINT)

        # ── Vertex Array Object (VAO) ──────────────────────────────
        # OpenGL 3.3+ core profile requires an explicit VAO — there's
        # no default one like in the compatibility profile. The VAO
        # bundles together: which buffer is bound to which attribute,
        # what the attribute layout is. Bind it once here; bind it
        # again in paintGL before drawing.
        if self._vao_trace is not None:
            self._vao_trace.destroy()
        self._vao_trace = QOpenGLVertexArrayObject(self)
        self._vao_trace.create()
        self._vao_trace.bind()

        # Hook the position attribute up to the bound VBO. Format:
        #   location 0 (matches `layout(location = 0)` in trace.vert)
        #   2 floats per vertex (vec2)
        #   stride = 8 bytes (one vec2)
        #   offset = 0 (position starts at byte 0)
        prog.bind()
        prog.enableAttributeArray(self._loc_position)
        prog.setAttributeBuffer(
            self._loc_position,
            0x1406,            # GL_FLOAT
            0,                 # offset
            2,                 # tupleSize (vec2)
            BYTES_PER_POINT,   # stride
        )
        prog.release()

        # Release VAO so other code doesn't accidentally modify it.
        self._vao_trace.release()
        self._vbo_trace.release()

    def resizeGL(self, w: int, h: int) -> None:
        """Hook for resize-time setup. Phase A trace path uses NDC
        throughout, so no per-resize state needs updating here.
        Viewport is set in paintGL each frame (see _set_viewport)
        because Qt 6 / PySide6 6.11 sometimes resets the viewport
        between resizeGL and paintGL, making a viewport set here
        unreliable."""
        pass

    def _set_viewport(self) -> None:
        """Set glViewport to match the current widget size in
        framebuffer pixels. Called from paintGL on every frame so it
        always matches the current widget size, even after Qt has
        re-set the viewport to something else under the hood. The
        per-frame cost is one int multiply + one GL call — negligible.
        """
        if self._gl is None:
            return
        dpr = self.devicePixelRatioF()
        fb_w = max(1, int(round(self.width() * dpr)))
        fb_h = max(1, int(round(self.height() * dpr)))
        self._gl.glViewport(0, 0, fb_w, fb_h)

    def paintGL(self) -> None:
        """Called by Qt per frame to draw.

        QOpenGLWidget binds the framebuffer for us before this fires
        and swaps the buffer after we return — we only need to issue
        actual GL draw calls.

        Phase A.3 work:
          1. Optionally regenerate synthetic test data (if no real
             frames have arrived via set_spectrum yet)
          2. Upload current trace vertices to the dynamic VBO via
             QOpenGLBuffer.write
          3. Bind shader program + VAO, set the traceColor uniform,
             issue ONE glDrawArrays call with GL_LINE_STRIP topology
             — the GPU rasterizes the whole connected line in one
             shot, no per-segment Python overhead

        On a 1500-pixel-wide trace this replaces ~1500 individual
        QPainter drawLine calls with one GL draw call. That's the
        core architectural win.
        """
        if self._gl is None or self._prog_trace is None:
            return

        # Set viewport to match current widget size every frame —
        # Qt 6 / PySide6 6.11 sometimes resets viewport between
        # resizeGL and paintGL, making a viewport set in resizeGL
        # unreliable. Per-frame cost is negligible.
        self._set_viewport()

        # ── Phase A test data ─────────────────────────────────────
        # Synthetic moving sine wave runs until set_spectrum is
        # called. _synth_timer drives the repaints.
        if self._synthetic_active:
            self._generate_synthetic()

        # ── Clear ─────────────────────────────────────────────────
        self._gl.glClearColor(_BG_R, _BG_G, _BG_B, 1.0)
        self._gl.glClear(GL_COLOR_BUFFER_BIT)

        n = self._trace_n
        if n < 2:
            return  # nothing to draw yet

        # ── Upload current vertex data ────────────────────────────
        # QOpenGLBuffer.write takes (offset, data, count_in_bytes).
        # Slice the prefix actually in use; .tobytes() copies into a
        # contiguous bytes object for the upload.
        self._vbo_trace.bind()
        self._vbo_trace.write(0, self._trace_xy[:n].tobytes(),
                              n * BYTES_PER_POINT)

        # ── Draw ──────────────────────────────────────────────────
        self._prog_trace.bind()
        # Set trace color uniform (cheap — 4 floats per frame).
        if self._loc_trace_color >= 0:
            r, g, b, a = self._trace_color
            self._prog_trace.setUniformValue(
                self._loc_trace_color, r, g, b, a)
        self._vao_trace.bind()
        self._gl.glDrawArrays(GL_LINE_STRIP, 0, n)
        self._vao_trace.release()
        self._prog_trace.release()
        self._vbo_trace.release()

    # ── Internal: synthetic data generator (Phase A test only) ─────
    def _generate_synthetic(self) -> None:
        """Fill the trace buffer with a time-varying sine wave so we
        can visually confirm the GPU upload+draw cycle is firing on
        every frame. Removed in Phase B once set_spectrum is being
        driven by a real source.
        """
        n = self._SYNTHETIC_N
        xs = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        # 3 cycles + a slow time-based phase offset so the wave
        # visibly scrolls left over time. Amplitude 0.7 keeps the
        # trace inside the widget without touching the edges.
        t = time.monotonic() - self._t0
        ys = (np.sin(xs * math.pi * 6.0 - t * 2.0) * 0.7).astype(np.float32)
        self._trace_xy[:n, 0] = xs
        self._trace_xy[:n, 1] = ys
        self._trace_n = n

    # ── QPainter overlay pass (Phase B.4+ feature parity) ──────────
    #
    # QOpenGLWidget supports a paintEvent override that runs AFTER
    # the GL drawing (paintGL) finishes. We use it to layer QPainter-
    # drawn overlays — VFO marker, passband, notches, peak markers,
    # band-plan strip, spots, etc. — on top of the GPU-rendered
    # trace. This is the "hybrid" approach: GL handles the heavy
    # per-frame data drawing (the trace itself, ~1500 line segments
    # collapsed to one draw call), QPainter handles the lighter
    # overlay work (a few rectangles + lines + text per frame). Best
    # of both worlds: GPU acceleration where it matters, code reuse
    # from the existing QPainter widget where it doesn't.

    def paintEvent(self, event) -> None:
        # Optional paint timing — see __init__. Includes BOTH the GL
        # framebuffer + the QPainter overlay pass, since either can
        # be the bottleneck on different machines.
        if self._paint_debug:
            import time as _ptime
            _paint_t0 = _ptime.perf_counter()
        else:
            _paint_t0 = None

        # Standard QOpenGLWidget machinery — runs initializeGL on
        # first call, resizeGL on resize, paintGL every frame. After
        # super returns, the GL framebuffer has been swapped to
        # screen and the widget is in QPainter-able state.
        super().paintEvent(event)
        # Now draw QPainter overlays on top of the GL output.
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._draw_overlays(painter)
        finally:
            painter.end()

        if _paint_t0 is not None:
            self._record_paint_time(_paint_t0)

    def _record_paint_time(self, t0: float) -> None:
        """5-second rolling summary of GPU widget paint timings. See
        SpectrumWidget._record_paint_time in spectrum.py for the same
        pattern + verdict-tag rationale."""
        import time as _ptime
        now = _ptime.perf_counter()
        dt_ms = (now - t0) * 1000.0
        self._paint_durations.append(dt_ms)
        if self._paint_t0_window == 0.0:
            self._paint_t0_window = now
            return
        if (now - self._paint_t0_window) < 5.0:
            return
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
        print(f"[Lyra paint] SpectrumGPU {verdict}: "
              f"{n} frames in {elapsed:.1f}s ({fps:.1f} fps), "
              f"set_spectrum={setspec}, "
              f"mean={mean_ms:.1f}ms p95={p95_ms:.1f}ms max={max_ms:.1f}ms")
        self._paint_durations = []
        self._paint_setspec_count = 0
        self._paint_t0_window = now

    def _draw_overlays(self, painter: QPainter) -> None:
        """Draw all QPainter overlays in the right order.

        Order matters — earlier draws sit underneath later ones.
        Mirrors the original QPainter SpectrumWidget's paint order
        so the visual feel is identical between backends.
        """
        # Grid — drawn first so the trace and labels sit on top.
        # Mirrors the CPU widget's order. Toggleable per `_show_grid`.
        if self._show_grid:
            self._draw_grid(painter)
        # Lyra constellation watermark — drawn FIRST so passband,
        # marker, notches, and labels all sit on top. Edge-faded so
        # the trace area in the middle of the widget stays clean.
        # Toggleable per `_show_constellation`.
        if self._show_constellation:
            from lyra.ui.constellation import draw as draw_constellation
            draw_constellation(painter, self.width(), self.height())
        # Occasional meteors — opt-in flair, drawn after the watermark
        # so the streaks composite cleanly on top of any visible
        # constellation pixels. Independent toggle.
        if self._show_meteors:
            from lyra.ui.constellation import draw_meteors
            draw_meteors(painter, self.width(), self.height())
        # Band-plan overlay — top-strip segments + landmark triangles
        # + edge warnings. Drawn BEFORE the trace overlays so the strip
        # sits at the very top edge but doesn't obscure the trace or
        # the passband. Sets _band_plan_reserved_px each frame for the
        # spot packer below to honor.
        self._draw_band_plan(painter)
        self._draw_passband(painter)
        self._draw_noise_floor(painter)
        # Peak-hold markers — translucent in-passband overlay drawn
        # ABOVE the trace (which lives in the GL framebuffer behind
        # this QPainter pass) so peaks read clearly against the live
        # spectrum.
        self._draw_peak_markers(painter)
        self._draw_db_scale_labels(painter)
        self._draw_vfo_marker(painter)
        # CW Zero line drawn AFTER the VFO marker so the white line
        # sits on top — operators read the marker→white-line gap as
        # the audible pitch position.
        self._draw_cw_zero_line(painter)
        self._draw_notches(painter)
        self._draw_spots(painter)
        self._draw_freq_scale_labels(painter)

    # ── Axis labels ─────────────────────────────────────────────────
    AXIS_COLOR = QColor(170, 204, 238)  # matches spectrum.py AXIS

    # Grid line color — matches spectrum.py GRID = QColor(40, 60, 80)
    # so the two backends look identical when grid is enabled.
    GRID_COLOR = QColor(40, 60, 80)

    def _draw_grid(self, painter: QPainter) -> None:
        """9×9 dotted grid divisions. Mirrors the CPU widget exactly:
        9 horizontal lines at y = h*i/10, 9 verticals at x = w*i/10,
        in muted dark-blue so they recede behind the spectrum trace.
        Drawn at the very start of the overlay chain so everything
        else (constellation watermark, passband, trace, markers, labels)
        sits on top."""
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        painter.setPen(QPen(self.GRID_COLOR, 1))
        for i in range(1, 10):
            y = int(h * i / 10)
            painter.drawLine(0, y, w, y)
        for i in range(1, 10):
            x = int(w * i / 10)
            painter.drawLine(x, 0, x, h)

    # ── Band-plan overlay drawing ──────────────────────────────────
    # Strip heights — match the CPU widget exactly so the two
    # backends render the band-plan area at the same vertical scale.
    BAND_STRIP_H = 10        # colored sub-band segment band
    LANDMARK_STRIP_H = 12    # landmark triangles + labels

    def _draw_band_plan(self, painter: QPainter) -> None:
        """Top-of-widget band-plan overlay. Mirrors spectrum.py's
        QPainter implementation: a colored segment strip, a landmark
        triangle strip just below, and full-band edge warnings as
        red dashed verticals that span the full widget height.
        Updates `_band_plan_reserved_px` each frame so the spot
        packer below can avoid colliding into the colored strips.
        Also rebuilds `_landmark_hit_cache` for click-to-tune
        dispatch in mousePressEvent."""
        # Reset every frame; populated below if the overlay is active.
        self._band_plan_reserved_px = 0
        self._landmark_hit_cache = []
        if self._band_plan_region == "NONE":
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0 or self._span_hz <= 0:
            return
        from lyra import band_plan as _bp
        from PySide6.QtGui import QPolygonF
        from PySide6.QtCore import QPointF

        hz_per_px = self._span_hz / w
        center_x = w / 2
        top_reserve = 0

        # ── Colored sub-band segment strip ────────────────────────
        if self._show_band_segments:
            top_reserve += self.BAND_STRIP_H
            segs = _bp.visible_segments(
                self._band_plan_region,
                self._center_hz, self._span_hz)
            # Cached font — see __init__.
            seg_lbl_font = self._font_7pt_bold
            for seg, seg_lo, seg_hi in segs:
                x0 = int(center_x + (seg_lo - self._center_hz) / hz_per_px)
                x1 = int(center_x + (seg_hi - self._center_hz) / hz_per_px)
                x0 = max(0, x0)
                x1 = min(w, x1)
                if x1 <= x0:
                    continue
                color_hex = (
                    self._user_segment_colors.get(seg["kind"])
                    or _bp.SEGMENT_COLORS.get(seg["kind"], "#5c8caa"))
                col = QColor(color_hex)
                col.setAlpha(210)
                painter.fillRect(x0, 0, x1 - x0, self.BAND_STRIP_H, col)
                if x1 - x0 >= 24:
                    painter.setPen(QPen(QColor(240, 240, 240, 220), 1))
                    painter.setFont(seg_lbl_font)
                    painter.drawText(x0 + 3, self.BAND_STRIP_H - 2,
                                     seg["label"])

        # ── Landmark triangle strip ───────────────────────────────
        if self._show_band_landmarks:
            top_reserve += self.LANDMARK_STRIP_H
            marks = _bp.visible_landmarks(
                self._band_plan_region,
                self._center_hz, self._span_hz)
            tri_y = (self.BAND_STRIP_H
                     if self._show_band_segments else 0)
            # Cached font — see __init__.
            painter.setFont(self._font_7pt_bold)
            for lm in marks:
                mx = int(center_x +
                         (lm["freq"] - self._center_hz) / hz_per_px)
                if not (0 <= mx <= w):
                    continue
                # Downward-pointing triangle, identical geometry to
                # the CPU widget so the landmark hit zone matches.
                tri = QPolygonF([
                    QPointF(mx - 4, tri_y + 1),
                    QPointF(mx + 4, tri_y + 1),
                    QPointF(mx,     tri_y + 6),
                ])
                painter.setPen(QPen(QColor(255, 215, 0, 200), 1))
                painter.setBrush(QColor(255, 215, 0, 140))
                painter.drawPolygon(tri)
                painter.setPen(QPen(QColor(255, 215, 0, 220), 1))
                painter.drawText(mx + 6,
                                 tri_y + self.LANDMARK_STRIP_H - 1,
                                 lm["label"])
                # Stash for hit-test — the triangle's vertical extent
                # is roughly tri_y .. tri_y + 6 px.
                self._landmark_hit_cache.append((
                    int(lm["freq"]),
                    str(lm.get("mode", "")),
                    mx, tri_y, tri_y + 6,
                ))

        # ── Band-edge warnings ────────────────────────────────────
        if self._show_band_edge_warn:
            lo_vis = self._center_hz - self._span_hz / 2
            hi_vis = self._center_hz + self._span_hz / 2
            # Cached font — see __init__.
            edge_lbl_font = self._font_8pt_bold
            for b in _bp.get_region(self._band_plan_region)["bands"]:
                for edge_hz in (b["low"], b["high"]):
                    if not (lo_vis <= edge_hz <= hi_vis):
                        continue
                    ex = int(center_x +
                             (edge_hz - self._center_hz) / hz_per_px)
                    painter.setPen(QPen(QColor(255, 80, 80, 220), 2,
                                        Qt.DashLine))
                    painter.drawLine(ex, 0, ex, h)
                    painter.setPen(QPen(QColor(255, 120, 120, 220), 1))
                    painter.setFont(edge_lbl_font)
                    painter.drawText(ex + 3, h - 22,
                                     f"{b['name']} EDGE")

        # Publish the reserved height so the spot packer below can
        # avoid stomping on the segment strip + landmark triangles.
        self._band_plan_reserved_px = top_reserve

    # ── Peak-markers drawing ───────────────────────────────────────
    def _draw_peak_markers(self, painter: QPainter) -> None:
        """In-passband peak-hold overlay. Same algo as the CPU
        widget: clip x-range to the passband, decimate/interpolate
        the peak buffer to widget-pixel width, render with the
        operator-selected style (line/dots/triangles), and
        optionally label the top-3 peaks. Buffer maintenance lives
        in set_spectrum so decay tracks frame cadence."""
        if (not self._peak_markers_enabled
                or self._peak_hold_db is None
                or self._passband_hi_hz <= self._passband_lo_hz
                or self._span_hz <= 0):
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        span_db = self._max_db - self._min_db
        if span_db <= 0:
            return
        hz_per_px = self._span_hz / w
        center_x = w / 2
        pb_x_lo = int(center_x + self._passband_lo_hz / hz_per_px)
        pb_x_hi = int(center_x + self._passband_hi_hz / hz_per_px)
        pb_x_lo = max(0, pb_x_lo)
        pb_x_hi = min(w, pb_x_hi)
        if pb_x_hi <= pb_x_lo + 2:
            return
        # Decimate / interpolate the peak buffer to pixel width — same
        # mapping as the live trace path so peaks line up bin-for-bin.
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

        # Default warm amber, or the operator's pick.
        if self._user_peak_color:
            amber = QColor(self._user_peak_color)
            amber.setAlpha(230)
        else:
            amber = QColor(255, 190, 90, 230)
        style = self._peak_markers_style
        if style == "line":
            painter.setPen(QPen(amber, 1.3))
            for i in range(pb_x_lo, pb_x_hi - 1):
                painter.drawLine(i, int(py[i]),
                                 i + 1, int(py[i + 1]))
        elif style == "triangles":
            from PySide6.QtGui import QPolygonF
            from PySide6.QtCore import QPointF
            painter.setPen(QPen(amber, 1))
            painter.setBrush(QColor(255, 190, 90, 200))
            for i in range(pb_x_lo, pb_x_hi, 4):
                yi = int(py[i])
                tri = QPolygonF([
                    QPointF(i - 3, yi - 4),
                    QPointF(i + 3, yi - 4),
                    QPointF(i,     yi),
                ])
                painter.drawPolygon(tri)
        else:  # "dots"
            from PySide6.QtCore import QPointF
            painter.setPen(QPen(amber, 1))
            painter.setBrush(QColor(255, 190, 90, 220))
            # One dot every 2 px — tight enough to read as a curve at
            # normal zoom, loose enough to count as discrete marks.
            for i in range(pb_x_lo, pb_x_hi, 2):
                painter.drawEllipse(QPointF(float(i), float(py[i])),
                                    1.8, 1.8)

        # Optional peak-dB readout — top 3 peaks ≥ 6 dB above the
        # min in the passband, separated by ≥ 16 px so labels
        # don't crowd each other.
        if self._peak_markers_show_db and pb_x_hi > pb_x_lo + 10:
            pb_slice = peak_line[pb_x_lo:pb_x_hi]
            min_in_pb = float(np.min(pb_slice))
            threshold = min_in_pb + 6.0
            # Cached font — see __init__.
            painter.setFont(self._font_7pt_bold)
            painter.setPen(QPen(amber, 1))
            candidates = []
            for i in range(1, len(pb_slice) - 1):
                v = pb_slice[i]
                if (v > pb_slice[i - 1]
                        and v >= pb_slice[i + 1]
                        and v >= threshold):
                    candidates.append((float(v), i + pb_x_lo))
            candidates.sort(reverse=True)
            chosen: list[tuple[float, int]] = []
            for val, x_peak in candidates:
                if all(abs(x_peak - xc) >= 16
                       for _, xc in chosen):
                    chosen.append((val, x_peak))
                if len(chosen) >= 3:
                    break
            for val, x_peak in chosen:
                yi = int(h - ((val - self._min_db) / span_db) * h)
                yi = max(10, yi)
                painter.drawText(x_peak + 4, yi - 2,
                                 f"{val:+.0f}")

    def _draw_db_scale_labels(self, painter: QPainter) -> None:
        """dB scale tick labels on the RIGHT edge — '+0', '-10',
        '-20', etc. every other tenth of widget height. Operator can
        read signal levels off the trace at a glance.

        Uses an explicit font + larger size than the QPainter widget
        because Qt's default font over QOpenGLWidget renders too thin
        to read against the spectrum trace. Bumped to 9 pt bold.
        """
        h = self.height()
        w = self.width()
        if h <= 0 or w <= 0:
            return
        span = self._max_db - self._min_db
        if span <= 0:
            return
        # Cached font — see __init__.
        painter.setFont(self._font_9pt_bold)
        painter.setPen(QPen(self.AXIS_COLOR, 1))
        for i in range(0, 11, 2):
            db = self._max_db - (i / 10) * span
            y = int(h * i / 10)
            # Clamp y so text stays inside the widget rect even at
            # the i=0 (top) and i=10 (bottom) ticks.
            y_text = max(12, min(h - 4, y + 10))
            painter.drawText(w - 50, y_text, f"{db:+.0f}")

    def _draw_notches(self, painter: QPainter) -> None:
        """Filled rectangle spanning each notch's −3 dB bandwidth,
        with edge outlines (thicker for 'deep'/cascaded notches)
        and a center hairline. Inactive notches render in muted
        grey. Width labels appear when there's room. Color +
        styling match the QPainter widget verbatim so notch UX
        feels identical between backends."""
        if not self._notches or self._span_hz <= 0:
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        hz_per_px = self._span_hz / max(1, w)
        # Cached font — see __init__.
        label_font = self._font_8pt
        for freq, width_hz, active, deep in self._notches:
            nf = (freq - self._center_hz) / self._span_hz + 0.5
            if not (0.0 <= nf <= 1.0):
                continue
            nx = int(nf * w)
            half_px = max(self.NOTCH_HIT_PX,
                          int(width_hz * 0.5 / hz_per_px))
            x_start = max(0, nx - half_px)
            x_end = min(w - 1, nx + half_px)
            if x_end <= x_start:
                continue
            if active:
                fill = QColor(220, 60, 60, 110)
                line = QColor(240, 80, 80, 230)
                label_color = QColor(255, 200, 200)
            else:
                fill = QColor(140, 140, 150, 80)
                line = QColor(170, 170, 180, 180)
                label_color = QColor(170, 170, 180)
            # Filled rectangle covering the full notch bandwidth.
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRect(x_start, 0, x_end - x_start, h)
            # Edge outlines — thicker for cascaded ("deep") notches.
            edge_width = 3 if deep else 1
            painter.setPen(QPen(line, edge_width, Qt.SolidLine))
            painter.drawLine(x_start, 0, x_start, h)
            painter.drawLine(x_end, 0, x_end, h)
            # Center hairline for precise targeting.
            painter.setPen(QPen(line, 1, Qt.SolidLine))
            painter.drawLine(nx, 0, nx, h)
            # Width label, only if there's room.
            if half_px >= 8 and nx + half_px + 60 < w:
                suffix = "^" if deep else ""
                painter.setPen(label_color)
                painter.setFont(label_font)
                painter.drawText(nx + half_px + 4, 14,
                                 f"{int(round(width_hz))}{suffix} Hz")

    def _draw_spots(self, painter: QPainter) -> None:
        """DX/contest spot boxes with multi-row collision packing
        and age-fade. Mirrors the CPU widget's spot rendering so
        the same TCI/cluster spot stream looks identical regardless
        of which backend is active.

        Layout rules (1:1 with the CPU implementation):
          - Up to 4 stacked rows. Newest spots (highest ts) get the
            top row; older spots cascade down. Spots that can't find
            a non-overlapping row this frame are skipped (still in
            _spots for hit-testing once that lands).
          - Linear age-fade: 100% alpha at ts=now down to 30% floor
            at ts = now - lifetime. Lifetime 0 disables fade.
          - Mode filter (set_spot_mode_filter) skips spots whose
            mode is not in the active filter set. Empty = render all.
          - Each box is a rounded rectangle with the spot color as
            border + tint, callsign/display centered, and a vertical
            tick from the box down to the trace area.
          - Top of row 0 sits BELOW _band_plan_reserved_px so spot
            boxes don't paint over the band-plan strip when that
            overlay lands. Currently 0 (band plan not yet wired).
        """
        if not self._spots or self._span_hz <= 0:
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
        import time
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QFontMetrics
        MAX_SPOT_ROWS = 4
        ROW_GAP_PX = 3
        AGE_FADE_FLOOR = 0.30
        # Dedicated font for spot callsigns. The display string is
        # typically "<flag emoji> <callsign>" (e.g. "🇺🇸 N8SDR")
        # because the TCI spot handler enriches the call with a
        # DXCC flag from cty.dat. So the font needs both:
        #   1. A text family that has ASCII glyphs for the callsign
        #   2. An emoji family that has the regional-indicator
        #      glyphs (U+1F1E6..U+1F1FF) for the flag
        # Theme font (Exo 2) covers ASCII but not emoji, so we
        # ALSO need an emoji family in the fallback chain.
        # setFamilies takes an ordered list: Qt picks the family
        # for each glyph by walking the list and using the first
        # one that supports it. Putting the text family first means
        # callsign chars render in Exo 2 (matches the rest of the
        # UI); flag emoji falls through to Segoe UI Emoji.
        # Cached font — see __init__.
        spot_font = self._font_spot
        painter.setFont(spot_font)
        fm = QFontMetrics(spot_font)
        padding_h = 5
        padding_v = 2
        box_h = fm.height() + 2 * padding_v
        # Filter to on-screen + mode-passing spots
        mode_filter = self._spot_mode_filter
        visible: list[tuple[float, dict]] = []
        for s in self._spots:
            if mode_filter:
                m = str(s.get("mode", "")).upper()
                if m not in mode_filter:
                    continue
            nf = (s["freq_hz"] - self._center_hz) / self._span_hz + 0.5
            if 0.0 <= nf <= 1.0:
                visible.append((nf, s))
        # Newest-first so fresh spots claim the top row
        visible.sort(key=lambda t: -t[1].get("ts", 0.0))
        # Greedy multi-row collision packing
        row_ranges: list[list[tuple[int, int]]] = [
            [] for _ in range(MAX_SPOT_ROWS)]
        placed: list[tuple[dict, int, float, float, float]] = []
        # placed = (spot, nx, bx, by, tw)
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
                continue
            row_ranges[chosen_row].append((x_start, x_end))
            row_y0 = (self._band_plan_reserved_px + 3
                      if self._band_plan_reserved_px > 0 else 2)
            by = row_y0 + chosen_row * (box_h + 2)
            placed.append((s, nx, bx, by, tw))
        # Render with age-based alpha
        now = time.monotonic()
        lifetime = self._spot_lifetime_s
        for s, nx, bx, by, tw in placed:
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
            text_alpha = int(round(255 * alpha_mul))
            spot_color = QColor(rc, gc, bc, border_alpha)
            # Dark semi-opaque fill — gives max contrast to white text
            # inside while letting the colored border still identify
            # the spot type by hue. Field test on 20 m showed that
            # spot-color text + spot-color tint fill blended into a
            # gauzy outline that operators couldn't read on a busy
            # band. White-text-on-dark-fill is the standard cluster-
            # display treatment (DXLab, N1MM+, SpotCollector all do
            # something similar) and stays legible at any age-fade
            # level.
            fill_alpha = int(round(200 * alpha_mul))
            dark_fill = QColor(20, 22, 28, fill_alpha)
            text = s.get("display") or s.get("call", "")
            rect = QRectF(bx, by, tw, box_h)
            painter.setBrush(dark_fill)
            painter.setPen(QPen(spot_color, 1))
            painter.drawRoundedRect(rect, 3, 3)
            # White text with a 1 px black drop shadow for that final
            # bit of pop on bright backgrounds (e.g. when the spot
            # box overlaps a bright trace peak).
            shadow_rect = QRectF(bx + 1, by + 1, tw, box_h)
            painter.setPen(QPen(QColor(0, 0, 0, max(180, text_alpha)),
                                1))
            painter.drawText(shadow_rect, Qt.AlignCenter, text)
            painter.setPen(QPen(QColor(255, 255, 255, text_alpha), 1))
            painter.drawText(rect, Qt.AlignCenter, text)
            # NOTE: no vertical drop line from the box to the trace —
            # the box's horizontal position already encodes the spot
            # frequency, and the line was visually noisy on busy
            # bands. Standard SDR-client convention is positional-
            # only labels (TCI's spot: protocol carries no tick info
            # either; rendering choices are entirely on the client).

    def _draw_freq_scale_labels(self, painter: QPainter) -> None:
        """Frequency tick labels at the BOTTOM — kHz with one
        decimal, every tenth of widget width. Lets the operator
        verify what frequency they're clicking on (and what's in
        view) without checking the tuning panel.
        """
        h = self.height()
        w = self.width()
        if h <= 0 or w <= 0 or self._span_hz <= 0:
            return
        # Cached font — see __init__.
        painter.setFont(self._font_9pt_bold)
        painter.setPen(QPen(self.AXIS_COLOR, 1))
        for i in range(1, 10):
            x = int(w * i / 10)
            offset_hz = (i / 10 - 0.5) * self._span_hz
            freq_khz = (self._center_hz + offset_hz) / 1000.0
            label = f"{freq_khz:,.1f}"
            painter.drawText(x - 30, h - 4, label)

    def _draw_passband(self, painter: QPainter) -> None:
        """Translucent cyan rectangle covering the RX filter
        passband, with dashed cyan edges. Operator can grab the
        edges to drag-resize RX BW (handled in mousePressEvent /
        mouseMoveEvent). Visuals match the QPainter widget."""
        x_lo, x_hi = self._passband_edge_px()
        if x_lo is None or x_hi is None:
            return
        w = self.width()
        h = self.height()
        x_lo = max(0, min(w, x_lo))
        x_hi = max(0, min(w, x_hi))
        if x_hi <= x_lo:
            return
        fill = QColor(0, 229, 255, 28)    # faint cyan
        edge = QColor(0, 229, 255, 140)   # brighter cyan for edges
        painter.fillRect(x_lo, 0, x_hi - x_lo, h, fill)
        painter.setPen(QPen(edge, 1, Qt.DashLine))
        painter.drawLine(x_lo, 0, x_lo, h)
        painter.drawLine(x_hi - 1, 0, x_hi - 1, h)

    def _draw_noise_floor(self, painter: QPainter) -> None:
        """Horizontal dashed line at the noise-floor dB level, with
        a small "NF -NN dBFS" label at the right edge. Color +
        styling match the QPainter widget.
        """
        if self._noise_floor_db is None:
            return
        # Skip if NF is outside the current visible dB range.
        if not (self._min_db <= self._noise_floor_db <= self._max_db):
            return
        h = self.height()
        w = self.width()
        if h <= 0 or w <= 0:
            return
        span = max(1e-6, self._max_db - self._min_db)
        nf_y = int(h - ((self._noise_floor_db - self._min_db) / span) * h)
        nf_y = max(0, min(h - 1, nf_y))
        # Color — operator override or sage-green default. Alpha is
        # always 180-ish so the line stays subtle regardless of hue.
        if self._nf_color_hex:
            color = QColor(self._nf_color_hex)
            color.setAlpha(180)
        else:
            color = QColor(120, 200, 140, 160)
        painter.setPen(QPen(color, 1, Qt.DashLine))
        painter.drawLine(0, nf_y, w, nf_y)
        # Label, right-justified near the right edge so it doesn't
        # collide with the trace in the busy middle of the spectrum.
        # Cached font — see __init__.
        painter.setFont(self._font_consolas_8)
        painter.setPen(QPen(color, 1))
        painter.drawText(w - 90, nf_y - 3,
                         f"NF {self._noise_floor_db:+.0f} dBFS")

    def _draw_vfo_marker(self, painter: QPainter) -> None:
        """Vertical dashed orange line at the widget's horizontal
        center — that's where the radio is tuned. Color + alpha +
        line style match the QPainter SpectrumWidget exactly so the
        feel is identical on backend swap."""
        cx = self.width() // 2
        painter.setPen(QPen(QColor(255, 170, 80, 220), 1, Qt.DashLine))
        painter.drawLine(cx, 0, cx, self.height())

    def _draw_cw_zero_line(self, painter: QPainter) -> None:
        """White vertical line at +/-pitch from the VFO marker —
        marks the CW filter center and the position where a clicked
        CW signal lands and is heard. Hidden in non-CW modes."""
        if not self._cw_zero_offset_hz or self._span_hz <= 0:
            return
        hz_per_px = self._span_hz / max(1, self.width())
        cx = self.width() // 2
        x = int(round(cx + self._cw_zero_offset_hz / hz_per_px))
        if x < 0 or x >= self.width():
            return
        # Solid white line, slightly translucent so it doesn't fully
        # obscure the signal under it.
        painter.setPen(QPen(QColor(255, 255, 255, 220), 1, Qt.SolidLine))
        painter.drawLine(x, 0, x, self.height())

    # ── GL teardown ────────────────────────────────────────────────
    def _cleanup_gl_resources(self) -> None:
        """Release GPU resources while the GL context is still
        current. Wired up in initializeGL via
        QOpenGLContext.aboutToBeDestroyed. Without this hook,
        Python's GC may call resource destructors AFTER Qt has torn
        down the context, producing 'destroy called without current
        context' warnings.

        Idempotent — safe to call more than once. Re-runs are no-ops
        because the destroy() calls null out the references.
        """
        if self._vbo_trace is not None:
            self._vbo_trace.destroy()
            self._vbo_trace = None
        if self._vao_trace is not None:
            self._vao_trace.destroy()
            self._vao_trace = None
        if self._prog_trace is not None:
            self._prog_trace.removeAllShaders()
            self._prog_trace.deleteLater()
            self._prog_trace = None


# ── Waterfall ─────────────────────────────────────────────────────────


class WaterfallGpuWidget(QOpenGLWidget):
    """GPU-rendered scrolling waterfall using texture streaming.

    The "scroll" is done entirely in the fragment shader by varying
    the texture sample row based on a `uRowOffset` uniform. CPU-side
    we maintain a circular write pointer into a fixed-size 2D R8
    texture; each new row is one glTexSubImage2D call covering one
    row's pixels. Zero buffer scrolling, zero memmove cost.

    Compare to the existing QPainter WaterfallWidget which does
        self._data[1:] = self._data[:-1]
    on every new row — a full ~10 MB memcpy per push on a typical
    waterfall buffer. This widget's per-push cost is bounded by the
    width of one row (~16 KB on a 4096-bin buffer) and is GPU-side.

    Public API:
        push_row(spec_db, min_db=-130, max_db=-30)
            Append one new row to the top of the waterfall.
        set_palette(palette_rgb)
            Swap the 256-entry color LUT.
        set_tuning(center_hz, span_hz)
            Tell the widget what frequency window it represents.

    Signals:
        clicked_freq(float)
            Emitted on left-click; payload is absolute Hz at the
            click x. Panel wires to radio.set_freq_hz.
    """

    clicked_freq = Signal(float)
    right_clicked_freq = Signal(float, bool, QPoint)

    # Number of rows in the texture. 600 matches the typical Lyra
    # waterfall height. Allocated once at initializeGL — operator
    # restart needed to change. Texture memory: ROW_COUNT * MAX_BINS
    # bytes (~5 MB at 600×8192).
    ROW_COUNT = 600

    # How often to repaint while in synthetic-data mode.
    _SYNTHETIC_HZ = 30

    def __init__(self, parent=None, synthetic: bool = False):
        """Construct the GPU waterfall widget.

        synthetic: if True, the widget runs an internal data
            generator (moving gaussian bump on a noise floor) at
            ~30 Hz until push_row() is first called. Useful for
            standalone demos and ad-hoc widget tests. **Defaults
            to FALSE** — production integration creates the widget
            without synthetic mode so the texture stays empty until
            Radio.waterfall_ready starts feeding real rows. Without
            this default, synthetic rows would be visible briefly
            at startup before the first real frame, leaving stale
            test patterns in the circular buffer.
        """
        super().__init__(parent)
        self.setFormat(lyra_gl_format())

        # GL function table — bound in initializeGL.
        self._gl: Optional[QOpenGLFunctions_4_3_Core] = None

        # GPU resource handles.
        self._prog: Optional[QOpenGLShaderProgram] = None
        self._vbo: Optional[QOpenGLBuffer] = None
        self._vao: Optional[QOpenGLVertexArrayObject] = None
        # QOpenGLTexture wrapper used for creation + binding. We need
        # this because PySide6's QOpenGLFunctions_4_3_Core does NOT
        # expose glGenTextures (every other texture function is there).
        # Per-row uploads still go through raw glTexSubImage2D — it's
        # more efficient than QOpenGLTexture.setData for sub-region
        # updates. We grab textureId() once after creation for those
        # raw calls.
        self._tex: Optional[QOpenGLTexture] = None
        self._tex_id: int = 0
        # Palette LUT texture — 256x1 RGB. Holds the currently
        # active color palette from lyra.ui.palettes. Bound to
        # texture unit 1 in paintGL; sampled in waterfall.frag via
        # the paletteTex sampler uniform.
        self._palette_tex: Optional[QOpenGLTexture] = None
        # Latest palette data the operator picked, as a 256x3 uint8
        # numpy array. None means "use a built-in fallback gradient
        # at first paint." set_palette() updates this; the upload
        # happens in paintGL when _palette_dirty is True.
        self._palette_data: Optional[np.ndarray] = None
        self._palette_dirty: bool = False
        # Cached locations.
        self._loc_position: int = -1
        self._loc_texcoord: int = -1
        self._loc_row_offset: int = -1
        self._loc_row_count: int = -1
        self._loc_sampler: int = -1
        self._loc_palette: int = -1
        self._loc_tex_u_max: int = -1

        # Circular buffer write state. _write_row points at the row
        # we MOST RECENTLY wrote (the visual top of the waterfall).
        # On each push it moves one row "up" with wrap-around. The
        # fragment shader reads this via uRowOffset.
        self._write_row: int = 0
        # Number of valid rows pushed so far. Used to suppress the
        # "show stale random GPU memory" effect during the first few
        # frames — once we've pushed ROW_COUNT rows, the texture is
        # fully populated.
        self._rows_pushed: int = 0

        # Row buffer used by push_row to convert dB → byte. Pre-
        # allocated to MAX_BINS so push_row never allocates.
        self._row_bytes = np.zeros(MAX_BINS, dtype=np.uint8)
        # Number of bins in the most-recent push.
        self._last_row_n: int = 0
        # Pending-rows queue. push_row APPENDS each new row + the
        # texture-row position it should land at; paintGL processes
        # the entire queue so no rows get dropped between paints.
        # Without this, when waterfall_ready fires faster than the
        # paint cadence (e.g., 110 rows/sec data + 60 Hz paint), only
        # the LATEST row would be uploaded — earlier rows would just
        # get their write-pointer position bumped without ever
        # writing data, leaving black gaps in the rendered waterfall.
        # Each entry: (write_row_index, bytes-of-length-n, n).
        self._pending_uploads: list[tuple[int, bytes, int]] = []

        # Tuning state — what frequency window the widget currently
        # represents. Updated via set_tuning(); used by click-to-tune.
        self._center_hz: float = 0.0
        self._span_hz: float = 48000.0

        # Notch markers (Phase B.13). Each entry is
        # (abs_freq_hz, width_hz, active, deep) — same shape as
        # the spectrum widget's notch list. Updated from
        # radio.notches_changed via the panel.
        self._notches: list[tuple[float, float, bool, bool]] = []

        # Synthetic mode for self-test without a spectrum source.
        # Default False — see constructor docstring.
        self._synthetic_active = bool(synthetic)
        self._t0 = time.monotonic()
        self._synth_timer = QTimer(self)
        self._synth_timer.setInterval(int(1000 / self._SYNTHETIC_HZ))
        self._synth_timer.timeout.connect(self._synthetic_tick)
        if self._synthetic_active:
            self._synth_timer.start()

        # ── Paint instrumentation (LYRA_PAINT_DEBUG=1) ──────────────
        # Same pattern as SpectrumGpuWidget. Tagged "WaterfallGPU" in
        # the printed line so we can compare the two widgets side by
        # side during a drag event.
        import os as _os
        self._paint_debug = (_os.environ.get("LYRA_PAINT_DEBUG", "")
                             .strip() in ("1", "true", "True"))
        self._paint_t0_window: float = 0.0
        self._paint_durations: list[float] = []
        self._paint_pushrow_count: int = 0   # push_row() calls this window

    # ── Public data API ────────────────────────────────────────────

    def set_tuning(self, center_hz: float, span_hz: float) -> None:
        """Tell the widget what frequency window it represents.
        See SpectrumGpuWidget.set_tuning for the full rationale."""
        self._center_hz = float(center_hz)
        self._span_hz = float(max(1.0, span_hz))

    def _freq_at_pixel(self, x: int) -> float:
        w = max(1, self.width())
        hz_per_px = self._span_hz / w
        return self._center_hz + (x - w / 2.0) * hz_per_px

    def mousePressEvent(self, event) -> None:
        x = int(event.position().x())
        f = self._freq_at_pixel(x)
        if event.button() == Qt.LeftButton:
            self.clicked_freq.emit(float(f))
        elif event.button() == Qt.RightButton:
            shift_held = bool(event.modifiers() & Qt.ShiftModifier)
            self.right_clicked_freq.emit(
                float(f), shift_held, event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def set_notches(self, notches: list) -> None:
        """Set the notch list for waterfall overlay drawing.
        Identical contract to SpectrumGpuWidget.set_notches —
        connected to radio.notches_changed."""
        self._notches = list(notches) if notches else []
        self.update()

    # Notch click-halo / minimum-visible-width — matches the
    # spectrum widget so a notch grabbable on one widget is also
    # grabbable on the other.
    NOTCH_HIT_PX = 14

    def set_palette(self, palette_rgb: np.ndarray) -> None:
        """Set the waterfall color palette.

        palette_rgb: (256, 3) uint8 numpy array of RGB values, as
            produced by lyra.ui.palettes._build(). The shape is
            checked defensively; non-conforming inputs are ignored
            with a console warning rather than crashing the widget
            mid-stream.

        The actual GPU upload happens lazily in paintGL — we just
        stash the data + mark dirty here. That way set_palette is
        safe to call from any thread context (the GL upload always
        runs on the Qt main thread inside paintGL).
        """
        try:
            arr = np.asarray(palette_rgb, dtype=np.uint8)
        except (ValueError, TypeError) as e:
            print(f"WaterfallGpuWidget.set_palette: bad input - {e}")
            return
        if arr.shape != (256, 3):
            print(f"WaterfallGpuWidget.set_palette: expected (256,3), "
                  f"got {arr.shape}")
            return
        self._palette_data = arr.copy()  # defensive copy
        self._palette_dirty = True
        self.update()

    def push_row(self, spec_db: np.ndarray,
                 min_db: float = -130.0,
                 max_db: float = -30.0) -> None:
        """Add one new row to the top of the waterfall.

        spec_db: 1-D numpy array of dB values (one per FFT bin). May
            be shorter than MAX_BINS — only the first n columns of
            the texture row will be updated.
        min_db/max_db: dynamic-range window. Values <= min_db map to
            0 (darkest), values >= max_db map to 255 (brightest).
        """
        # Paint-debug counter (rows pushed per 5-second window).
        if self._paint_debug:
            self._paint_pushrow_count += 1
        # Real data takes over — disable synthetic generator before
        # we forward to the internal pusher (so the synthetic timer
        # doesn't fight with caller-driven pushes).
        if self._synthetic_active:
            self._synthetic_active = False
            self._synth_timer.stop()
        self._push_row_internal(spec_db, min_db, max_db)

    def _push_row_internal(self, spec_db: np.ndarray,
                           min_db: float, max_db: float) -> None:
        """The actual data pipeline shared between the public
        push_row and the synthetic-mode timer. Doesn't touch the
        synthetic-active flag or the synthetic timer.

        APPENDS a new row + its destination position to the pending
        queue. paintGL drains the queue in one batch — all rows get
        uploaded, none get dropped, regardless of paint cadence.
        """
        n = int(min(spec_db.shape[0], MAX_BINS))
        if n < 2:
            return
        span = max(1e-6, max_db - min_db)
        # Clip + scale to 0..255 byte range. Compute into the
        # pre-allocated _row_bytes scratch, then COPY the prefix
        # into a fresh bytes object for the queue (we need a
        # snapshot — _row_bytes will be overwritten on the next
        # push, but the queue might still hold the previous row).
        norm = ((spec_db[:n].astype(np.float32) - min_db) / span)
        np.clip(norm, 0.0, 1.0, out=norm)
        self._row_bytes[:n] = (norm * 255.0).astype(np.uint8)
        self._last_row_n = n

        # Move write pointer up one row with wrap-around. After this
        # _write_row IS the position of the new (newest) row.
        self._write_row = (self._write_row - 1) % self.ROW_COUNT
        self._rows_pushed = min(self._rows_pushed + 1, self.ROW_COUNT)

        # Snapshot the row data + its target position into the queue.
        # paintGL will upload the entire queue. tobytes() copies, so
        # subsequent pushes overwriting _row_bytes won't disturb the
        # snapshot.
        self._pending_uploads.append(
            (self._write_row, self._row_bytes[:n].tobytes(), n))
        # Cap queue depth — if paintGL falls way behind (e.g., during
        # a long Settings dialog open), don't let the queue grow
        # unbounded. ROW_COUNT is the natural cap (more pending rows
        # than total texture rows would just overwrite each other).
        if len(self._pending_uploads) > self.ROW_COUNT:
            # Drop oldest rows — they'd be invisible anyway since
            # they'll be overwritten by newer ones at the same
            # texture positions on wraparound.
            del self._pending_uploads[
                : len(self._pending_uploads) - self.ROW_COUNT]
        self.update()

    # ── QOpenGLWidget overrides ────────────────────────────────────

    def initializeGL(self) -> None:
        """Build the GPU resources: shader program, fullscreen quad
        VBO+VAO, and the rolling-row 2D texture."""
        self._gl = QOpenGLFunctions_4_3_Core()
        self._gl.initializeOpenGLFunctions()

        # Hook context destruction so we release GPU resources while
        # the GL context is still valid (see SpectrumGpuWidget's
        # _cleanup_gl_resources docstring for the full rationale).
        ctx = self.context()
        if ctx is not None:
            ctx.aboutToBeDestroyed.connect(self._cleanup_gl_resources)

        # ── Shader program ────────────────────────────────────────
        if self._prog is not None:
            self._prog.removeAllShaders()
            self._prog.deleteLater()
        prog = QOpenGLShaderProgram(self)
        ok = (prog.addShaderFromSourceFile(
                  QOpenGLShader.ShaderTypeBit.Vertex,
                  str(_SHADER_DIR / "waterfall.vert"))
              and prog.addShaderFromSourceFile(
                  QOpenGLShader.ShaderTypeBit.Fragment,
                  str(_SHADER_DIR / "waterfall.frag")))
        if not ok:
            raise RuntimeError(
                "Waterfall shader compile failed:\n" + prog.log())
        if not prog.link():
            raise RuntimeError(
                "Waterfall shader link failed:\n" + prog.log())
        self._prog = prog
        self._loc_position    = prog.attributeLocation("position")
        self._loc_texcoord    = prog.attributeLocation("texcoord")
        self._loc_row_offset  = prog.uniformLocation("uRowOffset")
        self._loc_row_count   = prog.uniformLocation("uRowCount")
        self._loc_sampler     = prog.uniformLocation("waterfallTex")
        self._loc_tex_u_max   = prog.uniformLocation("uTexUMax")
        self._loc_palette     = prog.uniformLocation("paletteTex")

        # ── Fullscreen quad VBO ───────────────────────────────────
        # Interleaved: vec2 position + vec2 texcoord = 4 floats per
        # vertex, stride 16 bytes. Four vertices for a triangle strip
        # covering the whole NDC space. Texcoord.y goes 0 (top) to 1
        # (bottom) so the shader's "newest at top" convention is
        # correct without flipping anywhere.
        if self._vbo is not None:
            self._vbo.destroy()
        self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._vbo.setUsagePattern(QOpenGLBuffer.UsagePattern.StaticDraw)
        self._vbo.create()
        self._vbo.bind()
        quad = np.array([
            # x,    y,    u,    v
            -1.0, -1.0,  0.0, 1.0,   # bottom-left
             1.0, -1.0,  1.0, 1.0,   # bottom-right
            -1.0,  1.0,  0.0, 0.0,   # top-left
             1.0,  1.0,  1.0, 0.0,   # top-right
        ], dtype=np.float32)
        self._vbo.allocate(quad.tobytes(), quad.nbytes)

        # ── VAO ───────────────────────────────────────────────────
        if self._vao is not None:
            self._vao.destroy()
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()
        self._vao.bind()

        prog.bind()
        prog.enableAttributeArray(self._loc_position)
        prog.setAttributeBuffer(self._loc_position, GL_FLOAT,
                                0, 2, 16)   # offset 0, vec2, stride 16
        prog.enableAttributeArray(self._loc_texcoord)
        prog.setAttributeBuffer(self._loc_texcoord, GL_FLOAT,
                                8, 2, 16)   # offset 8, vec2, stride 16
        prog.release()

        self._vao.release()
        self._vbo.release()

        # ── Texture (R8 single-channel, MAX_BINS × ROW_COUNT) ─────
        # Created via QOpenGLTexture because PySide6 doesn't expose
        # glGenTextures. Allocated empty; per-row uploads happen via
        # raw glTexSubImage2D calls in paintGL (more efficient than
        # QOpenGLTexture.setData for sub-region updates).
        gl = self._gl
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)   # tight rows

        if self._tex is not None:
            self._tex.destroy()
            self._tex = None
        self._tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        self._tex.setFormat(QOpenGLTexture.TextureFormat.R8_UNorm)
        self._tex.setSize(MAX_BINS, self.ROW_COUNT)
        self._tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
        self._tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self._tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        # allocateStorage with explicit src format/type ensures the
        # storage is in the format we'll be uploading. Initial content
        # is undefined — first frames are blank-by-design (we suppress
        # draws until at least one row has been pushed).
        self._tex.allocateStorage(
            QOpenGLTexture.PixelFormat.Red,
            QOpenGLTexture.PixelType.UInt8)
        self._tex_id = int(self._tex.textureId())

        # ── Palette LUT texture (256 × 1, RGB) ─────────────────────
        # Holds the operator's chosen color palette. Sampled by the
        # fragment shader via the paletteTex sampler. Uses GL_LINEAR
        # so the 256 discrete stops render as smooth gradients.
        if self._palette_tex is not None:
            self._palette_tex.destroy()
            self._palette_tex = None
        self._palette_tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
        self._palette_tex.setFormat(QOpenGLTexture.TextureFormat.RGB8_UNorm)
        self._palette_tex.setSize(256, 1)
        self._palette_tex.setMinificationFilter(QOpenGLTexture.Filter.Linear)
        self._palette_tex.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
        self._palette_tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
        self._palette_tex.allocateStorage(
            QOpenGLTexture.PixelFormat.RGB,
            QOpenGLTexture.PixelType.UInt8)
        # If set_palette was called BEFORE initializeGL (e.g., panels.py
        # seeds the palette from radio.waterfall_palette right after
        # widget construction, before the widget has been shown), the
        # palette data is already stashed and just needs an upload.
        # Mark dirty so the next paintGL pushes it.
        if self._palette_data is not None:
            self._palette_dirty = True
        else:
            # No palette set yet — install a sane fallback gradient
            # (Classic) so the waterfall isn't black-on-black during
            # the very first frames after initializeGL but before the
            # operator's palette has been applied.
            try:
                from lyra.ui.palettes import PALETTES
                self._palette_data = PALETTES["Classic"].copy()
                self._palette_dirty = True
            except (ImportError, KeyError):
                pass  # widget will still work, just black until set

        # Suppress drawing the textured quad until a row is pushed.
        self._row_pending = False

    def resizeGL(self, w: int, h: int) -> None:
        # Viewport is set in paintGL (see _set_viewport) — Qt 6 /
        # PySide6 6.11 resets the viewport between resizeGL and
        # paintGL, so a viewport set here would be discarded.
        pass

    def _set_viewport(self) -> None:
        """Set glViewport to current widget framebuffer size — must
        be called from paintGL each frame because Qt resets viewport
        between resizeGL and paintGL."""
        if self._gl is None:
            return
        dpr = self.devicePixelRatioF()
        fb_w = max(1, int(round(self.width() * dpr)))
        fb_h = max(1, int(round(self.height() * dpr)))
        self._gl.glViewport(0, 0, fb_w, fb_h)

    def paintGL(self) -> None:
        if self._gl is None or self._prog is None or self._tex is None:
            return
        gl = self._gl
        # Set viewport every frame — see _set_viewport docstring.
        self._set_viewport()

        # ── Upload pending palette LUT (rare — only on switch) ────
        # If set_palette() was called since the last paint, push the
        # 256x3 RGB array to the palette texture. Cheap (768 bytes),
        # but doing it here keeps GL access on the right thread.
        if self._palette_dirty and self._palette_tex is not None \
                and self._palette_data is not None:
            self._palette_tex.bind(1)
            gl.glTexSubImage2D(
                GL_TEXTURE_2D, 0,
                0, 0,                            # x, y in texture
                256, 1,                          # width, height
                0x1907,                          # GL_RGB
                GL_UNSIGNED_BYTE,
                self._palette_data.tobytes(),
            )
            self._palette_tex.release(1)
            self._palette_dirty = False

        # ── Drain the pending-row queue ───────────────────────────
        # All rows accumulated since the last paintGL get uploaded
        # in order. Each row goes to its OWN target texture position
        # (_pending_uploads carries (row, bytes, n) tuples). Without
        # this loop, fast push_row callers (waterfall_ready firing
        # at >60 Hz with paint capped to 60 Hz vsync) would lose
        # every other row — write pointer would advance but only
        # the latest data would actually land in the texture.
        if self._pending_uploads:
            self._tex.bind(0)
            for write_row, data_bytes, n in self._pending_uploads:
                gl.glTexSubImage2D(
                    GL_TEXTURE_2D, 0,
                    0, write_row,             # x, y in texture
                    n, 1,                      # width, height
                    GL_RED, GL_UNSIGNED_BYTE,
                    data_bytes,
                )
            self._tex.release(0)
            self._pending_uploads.clear()

        # ── Clear ─────────────────────────────────────────────────
        gl.glClearColor(_BG_R, _BG_G, _BG_B, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        # Don't draw until at least one row has been pushed —
        # otherwise we'd display undefined GPU memory contents.
        if self._rows_pushed < 1:
            return

        # ── Draw the fullscreen quad ─────────────────────────────
        self._prog.bind()
        # Bind both textures: waterfall data on unit 0, palette LUT
        # on unit 1. QOpenGLTexture.bind(N) handles
        # glActiveTexture(GL_TEXTUREN) + glBindTexture for us.
        self._tex.bind(0)
        if self._palette_tex is not None:
            self._palette_tex.bind(1)
        # Sampler uniforms MUST be set with the int-specific overload
        # — the generic setUniformValue routes Python int through the
        # float overload, which leaves the sampler in an undefined
        # state and triggers GL_INVALID_OPERATION on draw.
        if self._loc_sampler >= 0:
            self._prog.setUniformValue1i(self._loc_sampler, 0)
        if self._loc_palette >= 0:
            self._prog.setUniformValue1i(self._loc_palette, 1)
        if self._loc_row_offset >= 0:
            self._prog.setUniformValue1f(
                self._loc_row_offset, float(self._write_row))
        if self._loc_row_count >= 0:
            self._prog.setUniformValue1f(
                self._loc_row_count, float(self.ROW_COUNT))
        # Constrain texture sampling to the populated portion of the
        # texture. The texture is allocated MAX_BINS columns wide for
        # headroom, but only the first _last_row_n columns get
        # uploaded. Without this scale the right portion of the
        # screen would sample uninitialized texture territory and
        # render black — was the cause of the "rendering only fills
        # part of the window" bug we hit during Phase A.4.
        if self._loc_tex_u_max >= 0 and self._last_row_n > 0:
            self._prog.setUniformValue1f(
                self._loc_tex_u_max,
                float(self._last_row_n) / float(MAX_BINS),
            )
        self._vao.bind()
        gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        self._vao.release()
        self._tex.release(0)
        if self._palette_tex is not None:
            self._palette_tex.release(1)
        self._prog.release()

    # ── Internal: synthetic data generator (Phase A test only) ─────

    def _synthetic_tick(self) -> None:
        """Generate one row of synthetic waterfall data: a moving
        gaussian bump on a noise floor. Visually obvious whether the
        push_row + circular-buffer + GPU-sample path is working
        end-to-end without needing a real spectrum source. Calls
        _push_row_internal so synthetic mode stays active across
        ticks (the public push_row would auto-disable us).
        """
        if not self._synthetic_active:
            return
        n = 4096
        t = time.monotonic() - self._t0
        # Noise floor
        spec = np.random.normal(-110, 3, n).astype(np.float32)
        # Moving gaussian bump scrolls left-right over time
        center = int(n / 2 + math.sin(t * 0.5) * (n / 3))
        width = 80
        x = np.arange(n)
        bump = 70 * np.exp(-((x - center) ** 2) / (2 * width * width))
        spec += bump.astype(np.float32)
        self._push_row_internal(spec, min_db=-130.0, max_db=-30.0)

    # ── QPainter overlay pass ──────────────────────────────────────
    # See SpectrumGpuWidget.paintEvent for the full rationale on the
    # GL-then-QPainter hybrid approach.

    def paintEvent(self, event) -> None:
        # Optional paint timing — see __init__.
        if self._paint_debug:
            import time as _ptime
            _paint_t0 = _ptime.perf_counter()
        else:
            _paint_t0 = None

        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            self._draw_overlays(painter)
        finally:
            painter.end()

        if _paint_t0 is not None:
            self._record_paint_time(_paint_t0)

    def _record_paint_time(self, t0: float) -> None:
        """5-second rolling summary of waterfall widget paint timings.
        Mirrors SpectrumGpuWidget._record_paint_time. Counts push_row()
        calls separately as "rows=N" — that's how often new spectrum
        data was pushed in by the radio."""
        import time as _ptime
        now = _ptime.perf_counter()
        dt_ms = (now - t0) * 1000.0
        self._paint_durations.append(dt_ms)
        if self._paint_t0_window == 0.0:
            self._paint_t0_window = now
            return
        if (now - self._paint_t0_window) < 5.0:
            return
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
        rows = self._paint_pushrow_count
        verdict = "ok"
        if max_ms > 100.0 or mean_ms > 20.0:
            verdict = "SLOW"
        elif max_ms > 50.0 or mean_ms > 10.0:
            verdict = "warm"
        print(f"[Lyra paint] WaterfallGPU {verdict}: "
              f"{n} frames in {elapsed:.1f}s ({fps:.1f} fps), "
              f"rows={rows}, "
              f"mean={mean_ms:.1f}ms p95={p95_ms:.1f}ms max={max_ms:.1f}ms")
        self._paint_durations = []
        self._paint_pushrow_count = 0
        self._paint_t0_window = now

    def _draw_overlays(self, painter: QPainter) -> None:
        self._draw_notches(painter)
        self._draw_vfo_marker(painter)

    def _draw_vfo_marker(self, painter: QPainter) -> None:
        """Vertical dashed orange line at the widget center. Slightly
        more transparent than the spectrum widget's version (alpha
        180 vs 220) so it doesn't fight the bright signal columns
        in the waterfall — matches the QPainter WaterfallWidget."""
        cx = self.width() // 2
        painter.setPen(QPen(QColor(255, 170, 80, 180), 1, Qt.DashLine))
        painter.drawLine(cx, 0, cx, self.height())

    def _draw_notches(self, painter: QPainter) -> None:
        """Notch markers on the waterfall — same red/grey filled
        rectangles as the spectrum widget but slightly more
        transparent so they don't drown out the underlying signal
        traces. Matches the QPainter WaterfallWidget."""
        if not self._notches or self._span_hz <= 0:
            return
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return
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
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRect(nx - half_px, 0, 2 * half_px, h)
            edge_width = 3 if deep else 1
            painter.setPen(QPen(line, edge_width, Qt.SolidLine))
            painter.drawLine(nx - half_px, 0, nx - half_px, h)
            painter.drawLine(nx + half_px, 0, nx + half_px, h)
            painter.setPen(QPen(line, 1, Qt.SolidLine))
            painter.drawLine(nx, 0, nx, h)

    # ── GL teardown ────────────────────────────────────────────────
    def _cleanup_gl_resources(self) -> None:
        """Release GPU resources while the GL context is still
        current. Wired up in initializeGL via
        QOpenGLContext.aboutToBeDestroyed. Without this hook the
        QOpenGLTexture destructor logs 'destroy called without
        current context' at process exit.

        Idempotent — safe to call more than once.
        """
        if self._tex is not None:
            self._tex.destroy()
            self._tex = None
            self._tex_id = 0
        if self._palette_tex is not None:
            self._palette_tex.destroy()
            self._palette_tex = None
        if self._vbo is not None:
            self._vbo.destroy()
            self._vbo = None
        if self._vao is not None:
            self._vao.destroy()
            self._vao = None
        if self._prog is not None:
            self._prog.removeAllShaders()
            self._prog.deleteLater()
            self._prog = None

