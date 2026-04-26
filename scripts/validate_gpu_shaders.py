"""Compile-validate every GLSL source in lyra/ui/spectrum_gpu_shaders/.

Usage:
    python scripts/validate_gpu_shaders.py

Exits 0 if all shaders compile cleanly. Exits non-zero with the
GLSL compiler log for any that fail. Useful as a pre-commit check
or as the first line of debugging when the GPU panadapter starts
showing a black screen after a shader edit.

The runtime widget code (lyra/ui/spectrum_gpu.py — coming in
Phase A.2) ALSO compiles these shaders on initialization, so this
script is purely for early/CI feedback. Same compile logic, same
QOpenGLShader call — what passes here will pass at runtime.

Implementation notes
--------------------
QOpenGLShader.compileSourceFile() requires an active QOpenGLContext
to call. We build a hidden QOffscreenSurface + QOpenGLContext to
satisfy that without popping a window. The context targets GLSL
3.30 core profile (matches the #version 330 core directive in our
shader sources), which is the minimum hardware we'll ever support
on Win10/11.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import (
    QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat,
)
from PySide6.QtOpenGL import QOpenGLShader


SHADER_DIR = (Path(__file__).resolve().parent.parent
              / "lyra" / "ui" / "spectrum_gpu_shaders")


def _build_context() -> tuple[QOffscreenSurface, QOpenGLContext]:
    """Build a hidden OpenGL context suitable for shader compilation.

    Returns (surface, context). Both must stay alive for the duration
    of any shader compilation calls — Python GC them and the context
    becomes invalid mid-compile.
    """
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)

    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    if not surface.isValid():
        raise RuntimeError(
            "Could not create offscreen GL surface — your driver may "
            "not expose OpenGL 3.3 core profile in headless mode.")

    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        raise RuntimeError(
            "Could not create OpenGL context. Check that your GPU "
            "driver supports OpenGL 3.3+.")
    if not ctx.makeCurrent(surface):
        raise RuntimeError("Could not make OpenGL context current.")
    return surface, ctx


def main() -> int:
    if not SHADER_DIR.is_dir():
        print(f"FAIL: shader directory not found: {SHADER_DIR}")
        return 1

    sources = (sorted(SHADER_DIR.glob("*.vert"))
               + sorted(SHADER_DIR.glob("*.frag")))
    if not sources:
        print(f"FAIL: no .vert / .frag files in {SHADER_DIR}")
        return 1

    # QGuiApplication is sufficient (no QWidget / event loop needed
    # for offscreen GL context creation).
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)

    surface, ctx = _build_context()

    print(f"Validating {len(sources)} shader(s) in {SHADER_DIR.name}/")
    # glGetString returns a Python str directly under PySide6.
    fns = ctx.functions()
    print(f"  GL vendor:   {fns.glGetString(0x1F00)}")  # GL_VENDOR
    print(f"  GL renderer: {fns.glGetString(0x1F01)}")  # GL_RENDERER
    print(f"  GL version:  {fns.glGetString(0x1F02)}")  # GL_VERSION
    print()

    failed = 0
    for path in sources:
        if path.suffix == ".vert":
            kind = QOpenGLShader.ShaderTypeBit.Vertex
        else:
            kind = QOpenGLShader.ShaderTypeBit.Fragment
        sh = QOpenGLShader(kind)
        ok = sh.compileSourceFile(str(path))
        if ok:
            print(f"  OK    {path.name}")
        else:
            print(f"  FAIL  {path.name}")
            log = sh.log() or "(no log returned)"
            for line in log.splitlines():
                print(f"        {line}")
            failed += 1

    ctx.doneCurrent()
    print()
    if failed:
        print(f"{failed} shader(s) failed to compile.")
        return 1
    print(f"All {len(sources)} shader(s) compiled cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
