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
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFunctions_4_3_Core, QOpenGLShader,
    QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


# Background color for the panadapter (RGB normalized 0..1) — matches
# the QPainter widget's `BG = QColor(12, 20, 32)` so visuals stay
# continuous when the operator switches renderers in Settings.
_BG_R, _BG_G, _BG_B = 12 / 255.0, 20 / 255.0, 32 / 255.0

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

# Where the GLSL source files live, relative to this module.
_SHADER_DIR = Path(__file__).resolve().parent / "spectrum_gpu_shaders"


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
            uses min_db/max_db as the scale window. Bins below
            min_db render at the bottom of the widget; bins above
            max_db at the top.
        set_trace_color(QColor)
            Set the trace line color. Applied via the trace.frag
            `traceColor` uniform on the next paint.
    """

    # Synthetic-data point count — mimics Lyra's typical FFT size
    # (4096) so the test exercises the same draw cost as real usage.
    _SYNTHETIC_N = 4096

    # How often to repaint while in synthetic-data mode. 30 Hz is a
    # comfortable visual rate that exercises the upload+draw cycle
    # without burning CPU on a passive demo.
    _SYNTHETIC_HZ = 30

    def __init__(self, parent=None):
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

        # Synthetic-data animation state. _synthetic_active toggles
        # OFF the moment set_spectrum() is called (real data takes
        # over). Until then, paintGL regenerates the test sine wave
        # each frame so motion is visible.
        self._synthetic_active = True
        self._t0 = time.monotonic()

        # Drives synthetic-mode animation. Real data path doesn't
        # need this — set_spectrum's caller (Radio in Phase B) will
        # request repaints via update() at FFT rate.
        self._synth_timer = QTimer(self)
        self._synth_timer.setInterval(int(1000 / self._SYNTHETIC_HZ))
        self._synth_timer.timeout.connect(self.update)
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
            Append one new row to the top of the waterfall. dB →
            byte mapping uses min_db/max_db as the dynamic range;
            values outside the range are clipped.
    """

    # Number of rows in the texture. 600 matches the typical Lyra
    # waterfall height. Allocated once at initializeGL — operator
    # restart needed to change. Texture memory: ROW_COUNT * MAX_BINS
    # bytes (~5 MB at 600×8192).
    ROW_COUNT = 600

    # How often to repaint while in synthetic-data mode.
    _SYNTHETIC_HZ = 30

    def __init__(self, parent=None):
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
        # Cached locations.
        self._loc_position: int = -1
        self._loc_texcoord: int = -1
        self._loc_row_offset: int = -1
        self._loc_row_count: int = -1
        self._loc_sampler: int = -1
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
        # Number of bins in the most-recent push (used for partial
        # texture uploads when the spectrum has fewer bins than the
        # texture is wide).
        self._last_row_n: int = 0

        # Synthetic mode for self-test without a spectrum source.
        self._synthetic_active = True
        self._t0 = time.monotonic()
        self._synth_timer = QTimer(self)
        self._synth_timer.setInterval(int(1000 / self._SYNTHETIC_HZ))
        self._synth_timer.timeout.connect(self._synthetic_tick)
        self._synth_timer.start()

    # ── Public data API ────────────────────────────────────────────

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
        synthetic-active flag or the synthetic timer."""
        n = int(min(spec_db.shape[0], MAX_BINS))
        if n < 2:
            return
        span = max(1e-6, max_db - min_db)
        # Clip + scale to 0..255 byte range.
        norm = ((spec_db[:n].astype(np.float32) - min_db) / span)
        np.clip(norm, 0.0, 1.0, out=norm)
        self._row_bytes[:n] = (norm * 255.0).astype(np.uint8)
        self._last_row_n = n

        # Move write pointer up one row with wrap-around. After this
        # _write_row IS the position of the new (newest) row.
        self._write_row = (self._write_row - 1) % self.ROW_COUNT
        self._rows_pushed = min(self._rows_pushed + 1, self.ROW_COUNT)

        # Mark a pending upload — actual glTexSubImage2D happens in
        # paintGL where the GL context is current.
        self._row_pending = True
        self.update()

    # ── QOpenGLWidget overrides ────────────────────────────────────

    def initializeGL(self) -> None:
        """Build the GPU resources: shader program, fullscreen quad
        VBO+VAO, and the rolling-row 2D texture."""
        self._gl = QOpenGLFunctions_4_3_Core()
        self._gl.initializeOpenGLFunctions()

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

        # ── Upload a pending row ──────────────────────────────────
        # We bind via QOpenGLTexture (which both binds AND activates
        # the texture unit) then issue a raw glTexSubImage2D for the
        # one-row sub-region update. The sub-image upload is more
        # efficient than QOpenGLTexture.setData for partial updates.
        if getattr(self, "_row_pending", False) and self._last_row_n > 0:
            self._tex.bind(0)
            data = self._row_bytes[:self._last_row_n].tobytes()
            gl.glTexSubImage2D(
                GL_TEXTURE_2D, 0,
                0, self._write_row,           # x, y in texture
                self._last_row_n, 1,           # width, height (one row)
                GL_RED, GL_UNSIGNED_BYTE,
                data,
            )
            self._tex.release(0)
            self._row_pending = False

        # ── Clear ─────────────────────────────────────────────────
        gl.glClearColor(_BG_R, _BG_G, _BG_B, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)

        # Don't draw until at least one row has been pushed —
        # otherwise we'd display undefined GPU memory contents.
        if self._rows_pushed < 1:
            return

        # ── Draw the fullscreen quad ─────────────────────────────
        self._prog.bind()
        # Bind the texture to unit 0. QOpenGLTexture.bind(0) handles
        # glActiveTexture(GL_TEXTURE0) + glBindTexture for us.
        self._tex.bind(0)
        # Sampler uniform MUST be set with the int-specific overload —
        # the generic setUniformValue routes Python int through the
        # float overload, which leaves the sampler in an undefined
        # state and triggers GL_INVALID_OPERATION on draw.
        if self._loc_sampler >= 0:
            self._prog.setUniformValue1i(self._loc_sampler, 0)
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

