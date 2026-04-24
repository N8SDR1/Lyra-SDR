"""Graphics backend selector for the painted widgets.

Controls whether `SpectrumWidget` and `WaterfallWidget` inherit from
`QWidget` (software `QPainter` on the CPU) or `QOpenGLWidget` (Qt's
OpenGL-accelerated painter — same `QPainter` calls, but rasterization
and compositing happen on the GPU).

**Why it matters**: CPU painting during a window resize / fullscreen
toggle blocks the Python main thread, and because the demod runs on
the same thread it can cause audio to stutter. Moving paint to the
GPU decouples them — the resize stays smooth and audio keeps running.

**Read-at-import**: the base class is resolved *once* at module-load
time by reading the user's Visuals → Graphics backend preference from
QSettings. Changing the setting therefore requires restarting Lyra,
which is clearly stated in the Visuals tab UI. We take this tradeoff
so the widget classes stay simple — no per-instance conditional
painting logic.

**Fallback**: if OpenGL is selected but the import fails (some Windows
installs of PySide6 ship without `QtOpenGLWidgets`, or the OS lacks a
working GL driver), we silently fall back to software and expose the
resolved backend via `ACTIVE_BACKEND` so Settings can show the truth.

**Vulkan**: not implemented — `QVulkanWindow` is a different paradigm
(no QPainter, all shaders). Listed in the UI as disabled for future
work; always falls back to software here.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QWidget

# Canonical backend identifiers used in QSettings and the UI combo.
BACKEND_SOFTWARE = "software"
BACKEND_OPENGL   = "opengl"
BACKEND_VULKAN   = "vulkan"

BACKEND_LABELS = {
    BACKEND_SOFTWARE: "Software (QPainter)",
    BACKEND_OPENGL:   "OpenGL (GPU-accelerated QPainter)",
    BACKEND_VULKAN:   "Vulkan (not implemented)",
}


def _pick_base():
    """Return (base_class, active_backend_id). Reads QSettings, tries
    to import the requested backend, falls back to QWidget on any
    failure."""
    choice = str(QSettings("N8SDR", "Lyra").value(
        "visuals/graphics_backend", BACKEND_SOFTWARE)).lower()

    if choice == BACKEND_OPENGL:
        try:
            from PySide6.QtOpenGLWidgets import QOpenGLWidget
            # Verify we can actually construct one — some headless/
            # test environments have the class but no GL context.
            return QOpenGLWidget, BACKEND_OPENGL
        except ImportError:
            # QtOpenGLWidgets missing — fall through to software.
            pass

    # Vulkan path intentionally not wired. Always software fallback.
    return QWidget, BACKEND_SOFTWARE


ACCELERATED_BASE, ACTIVE_BACKEND = _pick_base()
