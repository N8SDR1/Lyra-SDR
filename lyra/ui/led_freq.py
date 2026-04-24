"""Large LED-style frequency display.

Amber digits on black background in MMM.kkk.hhh format (megahertz,
kilohertz, hertz). Click a digit to select its place-value, then use
the mouse wheel or arrow keys to tune.

Looks like a classic rig's front-panel VFO readout — the biggest
single visual shift toward the other reference SDR clients feel.

Interactions:
- Left-click a digit → select it (cyan underline)
- Mouse wheel over selected digit → ±1 on that place (e.g., selecting
  the hundreds-Hz digit and wheeling up increments 100 Hz at a time)
- Arrow up/down → increment/decrement selected digit
- Arrow left/right → move selection one place left/right
- Emits `freq_changed(int hz)` when the value changes
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme


class FrequencyDisplay(QWidget):

    freq_changed = Signal(int)

    # LED-style palette
    BG         = QColor(4, 4, 6)
    AMBER      = QColor(255, 171, 71)
    AMBER_DIM  = QColor(60, 40, 15)       # unlit ghost color
    SELECT     = QColor(0, 229, 255)      # cyan highlight for selected digit
    DOT        = QColor(255, 171, 71)

    # Digit index mapping: 0 = Hz ones, 8 = 100-MHz. Range 0..55 MHz
    # covered by HL2 so index 7-8 rarely above 5.
    N_DIGITS = 9
    MAX_HZ = 55_999_999

    def __init__(self, parent=None):
        super().__init__(parent)
        self._freq_hz = 7_074_000
        self._selected: int = 3            # default select 1-kHz digit
        self._digit_rects: list[tuple[int, QRectF]] = []
        # Enabled flag governs both interactivity (click/wheel/keys)
        # and painting (dim palette + an "OFF" banner for clear state).
        # Lets us use the same widget as a placeholder for future RX2
        # without a separate class.
        self._enabled = True
        # Optional banner shown over the digits when disabled. Useful
        # to distinguish "disabled RX2" from "RX2 at 0 Hz".
        self._disabled_banner: str = ""
        self.setMinimumSize(340, 66)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_vfo_enabled(self, enabled: bool, banner: str = ""):
        """Dim the digits + disable input when `enabled=False`. Used
        today only by RX2 as a placeholder; reactivated with a real
        freq when RX2 gateware is wired up."""
        self._enabled = bool(enabled)
        self._disabled_banner = banner
        self.setCursor(
            Qt.PointingHandCursor if self._enabled else Qt.ArrowCursor)
        self.update()

    # ── Public API ────────────────────────────────────────────────────
    def set_freq_hz(self, hz: int):
        hz = max(0, min(self.MAX_HZ, int(hz)))
        if hz == self._freq_hz:
            return
        self._freq_hz = hz
        self.update()

    @property
    def freq_hz(self) -> int:
        return self._freq_hz

    def set_selected_digit(self, idx: int):
        self._selected = max(0, min(self.N_DIGITS - 1, int(idx)))
        self.update()

    # ── Painting ──────────────────────────────────────────────────────
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        w = self.width()
        h = self.height()
        p.fillRect(self.rect(), self.BG)

        # Use a tall bold monospace for the classic LED readout feel.
        font = QFont()
        font.setFamilies(["Share Tech Mono", "Consolas", "Courier New"])
        font.setPixelSize(max(24, int(h * 0.62)))
        font.setWeight(QFont.Bold)
        p.setFont(font)
        fm = QFontMetrics(font)

        digit_w = fm.horizontalAdvance("0")
        dot_w = fm.horizontalAdvance(".")
        total_w = self.N_DIGITS * digit_w + 2 * dot_w
        x = (w - total_w) // 2
        baseline_y = (h + fm.ascent() - fm.descent()) // 2

        freq_str = f"{self._freq_hz:0{self.N_DIGITS}d}"
        self._digit_rects = []

        for i, ch in enumerate(freq_str):
            digit_index = self.N_DIGITS - 1 - i   # leftmost char → highest place
            rect = QRectF(x, baseline_y - fm.ascent(), digit_w, fm.height())
            self._digit_rects.append((digit_index, rect))

            # Ghost "8" behind the digit — faint unlit-segment effect
            p.setPen(QPen(self.AMBER_DIM, 1))
            p.drawText(QPointF(x, baseline_y), "8")

            # Actual digit in amber (or dim amber when disabled — a
            # greyed-out placeholder VFO). Leading-zero suppression
            # still applies so small frequencies don't look weird.
            if not self._enabled:
                pen_color = self.AMBER_DIM
            elif digit_index >= 6 and all(freq_str[j] == "0" for j in range(i + 1)):
                pen_color = self.AMBER_DIM
            else:
                pen_color = self.AMBER
            p.setPen(QPen(pen_color, 1))
            p.drawText(QPointF(x, baseline_y), ch)

            # Selection underline for the selected digit (disabled
            # VFOs don't get a selection cursor — nothing to tune).
            if self._enabled and digit_index == self._selected:
                p.setPen(QPen(self.SELECT, 3))
                p.drawLine(
                    QPointF(x + 1, baseline_y + 3),
                    QPointF(x + digit_w - 1, baseline_y + 3),
                )

            x += digit_w

            # Dot separator between MHz|kHz and kHz|Hz
            if i in (2, 5):
                p.setPen(QPen(self.DOT, 1))
                p.drawText(QPointF(x, baseline_y), ".")
                x += dot_w

        # Unit labels underneath each group
        unit_font = QFont()
        unit_font.setFamilies([theme.FONT_MONO_FAMILY, "Consolas"])
        unit_font.setPixelSize(max(9, int(h * 0.14)))
        unit_font.setBold(True)
        p.setFont(unit_font)
        ufm = QFontMetrics(unit_font)
        unit_y = h - 4

        # Compute group centers
        # MHz group: digits 8..6 (chars 0..2), kHz: 5..3 (chars 3..5), Hz: 2..0 (chars 6..8)
        x_start = (w - total_w) // 2
        group_starts = [x_start,
                        x_start + 3 * digit_w + dot_w,
                        x_start + 6 * digit_w + 2 * dot_w]
        for gx, label in zip(group_starts, ("MHz", "kHz", "Hz")):
            gw_center = gx + (3 * digit_w) // 2
            tw = ufm.horizontalAdvance(label)
            p.setPen(QPen(theme.TEXT_FAINT, 1))
            p.drawText(QPointF(gw_center - tw / 2, unit_y), label)

        # Disabled banner — painted on top of the dimmed digits so the
        # placeholder state is unambiguous. Small monospace italic,
        # cyan, horizontally centered.
        if not self._enabled and self._disabled_banner:
            banner_font = QFont()
            banner_font.setFamilies([theme.FONT_MONO_FAMILY, "Consolas"])
            banner_font.setPixelSize(max(10, int(h * 0.18)))
            banner_font.setItalic(True)
            banner_font.setBold(True)
            p.setFont(banner_font)
            bfm = QFontMetrics(banner_font)
            bw = bfm.horizontalAdvance(self._disabled_banner)
            p.setPen(QPen(QColor(0, 229, 255, 180), 1))
            p.drawText(QPointF((w - bw) // 2, h // 2 + 4),
                       self._disabled_banner)

    # ── Mouse / wheel / keyboard ──────────────────────────────────────
    def mousePressEvent(self, event):
        if not self._enabled:
            return
        if event.button() != Qt.LeftButton:
            return
        for idx, rect in self._digit_rects:
            if rect.contains(event.position()):
                self._selected = idx
                self.update()
                self.setFocus()
                return

    def wheelEvent(self, event):
        if not self._enabled:
            return super().wheelEvent(event)
        # If hovering a digit, wheel that digit's place (else the currently-
        # selected digit's place).
        digit = self._selected
        for idx, rect in self._digit_rects:
            if rect.contains(event.position()):
                digit = idx
                self._selected = idx
                break
        if digit < 0:
            return super().wheelEvent(event)
        step = 10 ** digit
        delta_units = event.angleDelta().y() // 120
        if delta_units == 0:
            return
        self._change_freq(delta_units * step)
        event.accept()

    def keyPressEvent(self, event):
        if not self._enabled:
            super().keyPressEvent(event)
            return
        if self._selected < 0:
            super().keyPressEvent(event)
            return
        step = 10 ** self._selected
        k = event.key()
        if k == Qt.Key_Up:
            self._change_freq(step)
        elif k == Qt.Key_Down:
            self._change_freq(-step)
        elif k == Qt.Key_Left:
            self._selected = min(self.N_DIGITS - 1, self._selected + 1)
            self.update()
        elif k == Qt.Key_Right:
            self._selected = max(0, self._selected - 1)
            self.update()
        else:
            super().keyPressEvent(event)

    def _change_freq(self, delta_hz: int):
        new_freq = max(0, min(self.MAX_HZ, self._freq_hz + delta_hz))
        if new_freq == self._freq_hz:
            return
        self._freq_hz = new_freq
        self.update()
        self.freq_changed.emit(new_freq)
