"""Diagnostic via grabFramebuffer() — tells us the actual framebuffer size.
Run from project root:  python scripts/diag_waterfall.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from lyra.ui.spectrum_gpu import WaterfallGpuWidget

app = QApplication(sys.argv)

w = WaterfallGpuWidget()
w.resize(1200, 400)
w.setWindowTitle("diag")
w.show()


def grab_and_print():
    img = w.grabFramebuffer()
    print(f"widget.size = {w.width()}x{w.height()}")
    print(f"widget.geometry = {w.geometry().width()}x{w.geometry().height()}")
    print(f"widget.devicePixelRatioF = {w.devicePixelRatioF()}")
    print(f"defaultFramebufferObject = {w.defaultFramebufferObject()}")
    print(f"grabFramebuffer().size = {img.width()}x{img.height()}")
    print(f"grabFramebuffer().devicePixelRatio = {img.devicePixelRatio()}")
    # Save the image so we can see what the framebuffer actually
    # contains — diagnoses whether the issue is "framebuffer is half
    # width" (image will be small) or "framebuffer is correct size
    # but only half is drawn into" (image will be full but with the
    # right half black).
    out_path = Path(__file__).resolve().parent / "diag_framebuffer.png"
    img.save(str(out_path))
    print(f"Saved framebuffer dump to: {out_path}")


# Wait long enough for several paints to complete + something to be drawn
QTimer.singleShot(1000, grab_and_print)
QTimer.singleShot(1500, app.quit)
app.exec()
print("done")
