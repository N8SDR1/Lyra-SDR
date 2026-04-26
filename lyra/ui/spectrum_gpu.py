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
A.2 (THIS COMMIT): widget skeleton. Subclasses QOpenGLWidget,
implements the three required virtual methods (initializeGL,
resizeGL, paintGL) with the minimum needed to:
  - Compile the trace shader program
  - Clear the framebuffer to Lyra's background color each frame
  - Not crash on widget destruction

No data flow yet, no draw calls beyond the clear — just proves the
widget can be created, shown, painted, and closed without errors.

A.3 will add: vertex buffer + Vertex Array Object (VAO) + working
            trace draw with synthetic data
A.4 will add: streaming-texture waterfall + working draw call
A.5 will add: standalone demo runner (separate file)
A.6 will add: external profile pass

Phase B will integrate into Lyra (Settings UI, real Radio data).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLFunctions_4_3_Core, QOpenGLShader, QOpenGLShaderProgram,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


# OpenGL 4.3 buffer / clear flag constants. Imported here once at
# module load instead of repeating the magic numbers in every
# paintGL/initializeGL body. Matches the GL spec values exactly.
GL_COLOR_BUFFER_BIT = 0x4000


# Background color for the panadapter (RGB normalized 0..1) — matches
# the QPainter widget's `BG = QColor(12, 20, 32)` so visuals stay
# continuous when the operator switches renderers in Settings.
_BG_R, _BG_G, _BG_B = 12 / 255.0, 20 / 255.0, 32 / 255.0

# Where the GLSL source files live, relative to this module. We
# resolve once at module load so per-frame paths don't do filesystem
# work. Sources are loaded by initializeGL() because QOpenGLShader
# needs an active GL context to compile.
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
    # Double-buffered swap, no multisample (the trace and waterfall
    # both render at native pixel resolution; MSAA would help line
    # edges but cost ~4x fragment work for marginal visual gain).
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    fmt.setSamples(0)
    # Vsync ON — the panadapter is throttled to FFT rate (typically
    # 13-30 Hz) anyway; sync-to-display avoids tearing for free.
    fmt.setSwapInterval(1)
    return fmt


class SpectrumGpuWidget(QOpenGLWidget):
    """GPU-rendered spectrum + (eventually) waterfall panadapter.

    Phase A.2 state: clears the framebuffer to Lyra's background
    color each frame and nothing else. Compiles the trace shader
    program in initializeGL() to prove the GLSL → GPU path works
    without crashing.

    Public API for later phases (stubbed now, fleshed out in A.3+):
        set_spectrum(spec_db, min_db, max_db)
            Upload one frame of spectrum data.
        set_trace_color(QColor)
            Operator's chosen trace color.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # CRUCIAL: set the format on THIS widget's instance BEFORE
        # any show / paint happens. Setting the global default via
        # QSurfaceFormat.setDefaultFormat is also valid but global
        # state is harder to reason about. Per-widget setFormat
        # keeps the choice local to this widget tree.
        self.setFormat(lyra_gl_format())

        # Shader programs and GPU resource handles. Populated in
        # initializeGL(), released automatically when the widget is
        # destroyed (QOpenGLShaderProgram has parent ownership).
        self._prog_trace: Optional[QOpenGLShaderProgram] = None

        # GL function accessor — a versioned function table that gives
        # us core 4.3 calls without depending on PyOpenGL. Bound to
        # the widget's context in initializeGL() once the context is
        # current. We type as Optional so the IDE warns if anything
        # tries to use it before init.
        self._gl: Optional[QOpenGLFunctions_4_3_Core] = None

    # ── QOpenGLWidget virtual method overrides ─────────────────────

    def initializeGL(self) -> None:
        """Called by Qt once after the OpenGL context becomes current.

        This is where we build all GPU-side resources: shader programs,
        vertex buffers, vertex array objects, textures. Fires once at
        first show; if the widget is reparented to a different
        top-level window with a different GL context, Qt MAY call this
        again — be idempotent (drop and rebuild).

        Phase A.2 work: compile the trace shader program. Proves the
        GLSL files load + compile + link without errors in the actual
        runtime context (the validate_gpu_shaders.py script does this
        too, but only against an offscreen context — this verifies the
        on-screen widget context behaves the same way).
        """
        # Bind the GL function table to the current context so we
        # have native access to glClear / glDrawArrays / etc. without
        # PyOpenGL. Re-initialize on every initializeGL call (the
        # context may have changed if the widget was reparented).
        self._gl = QOpenGLFunctions_4_3_Core()
        self._gl.initializeOpenGLFunctions()

        # If initializeGL fires again (e.g., reparenting), drop any
        # prior program so we don't leak.
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
            # Surface compile errors immediately rather than silently
            # falling back to a broken-but-running widget.
            raise RuntimeError(
                "Trace shader compile failed:\n" + prog.log())
        if not prog.link():
            raise RuntimeError(
                "Trace shader link failed:\n" + prog.log())
        self._prog_trace = prog

    def resizeGL(self, w: int, h: int) -> None:
        """Called by Qt when the widget is resized.

        QOpenGLWidget already updates the GL viewport for us before
        this is invoked — we only need to override if we maintain
        view/projection matrices that depend on aspect ratio. The
        Phase A trace path uses NDC coordinates exclusively (CPU does
        the bin→NDC mapping in set_spectrum), so we don't need to do
        anything here. Override exists as a hook for Phase B
        (overlays / axis labels that depend on widget size).
        """
        pass

    def paintGL(self) -> None:
        """Called by Qt per frame to draw.

        QOpenGLWidget binds the framebuffer for us before this fires
        and swaps the buffer after we return — we only need to issue
        actual GL draw calls.

        Phase A.2 work: clear to background color. Nothing else. If
        this displays a solid dark-blue rectangle when shown, the
        whole GL pipeline is healthy and we can move on to A.3.
        """
        if self._gl is None:
            return  # initializeGL hasn't run yet — defensive
        self._gl.glClearColor(_BG_R, _BG_G, _BG_B, 1.0)
        self._gl.glClear(GL_COLOR_BUFFER_BIT)

    # ── Public data API (stubbed for Phase A.2; filled in A.3+) ────

    def set_spectrum(self, spec_db, min_db: float = -130.0,
                     max_db: float = -30.0) -> None:
        """Upload a frame of spectrum data for the next render.

        Phase A.2 stub — accepts the call but does nothing. A.3 will
        flesh this out to map bins → NDC.x and dB → NDC.y, write the
        result into the dynamic vertex buffer, and request a repaint.
        """
        pass

    def set_trace_color(self, color: QColor) -> None:
        """Set the trace line color.

        Phase A.2 stub — A.3 will write this into the trace.frag
        `traceColor` uniform.
        """
        pass
