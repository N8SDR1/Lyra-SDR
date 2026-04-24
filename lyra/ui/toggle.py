"""ToggleSwitch — proper iOS-style on/off toggle widget.

Replaces QCheckBox where a clearly-readable on/off state matters
(N2ADR enable, USB-BCD enable, etc.). Animated thumb slide; cyan
when on, dim when off. Larger hit target than a checkbox.
"""
from __future__ import annotations

from PySide6.QtCore import (
    Property, QEasingCurve, QPropertyAnimation, QRectF, Qt, Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme


class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    TRACK_W = 46
    TRACK_H = 22
    THUMB_R = 9
    PAD = 2

    def __init__(self, parent=None, on: bool = False):
        super().__init__(parent)
        self._on = on
        self._enabled_flag = True
        self._thumb_pos = float(self.TRACK_W - self.THUMB_R - self.PAD
                                if on else self.THUMB_R + self.PAD)
        self.setFixedSize(self.TRACK_W + 4, self.TRACK_H + 4)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"thumb_pos")
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def setEnabled(self, enabled: bool):
        # Override so our cursor + paint states reflect the disabled look.
        self._enabled_flag = bool(enabled)
        self.setCursor(Qt.PointingHandCursor if enabled
                       else Qt.ForbiddenCursor)
        super().setEnabled(enabled)
        self.update()

    def isChecked(self) -> bool:
        return self._on

    def setChecked(self, on: bool):
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        target = (self.TRACK_W - self.THUMB_R - self.PAD
                  if on else self.THUMB_R + self.PAD)
        self._anim.stop()
        self._anim.setStartValue(self._thumb_pos)
        self._anim.setEndValue(float(target))
        self._anim.start()
        self.toggled.emit(self._on)

    def mousePressEvent(self, event):
        if not self._enabled_flag:
            return
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._on)

    def get_thumb_pos(self) -> float:
        return self._thumb_pos

    def set_thumb_pos(self, v: float):
        self._thumb_pos = v
        self.update()

    thumb_pos = Property(float, get_thumb_pos, set_thumb_pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        disabled = not self._enabled_flag

        # Track
        track_rect = QRectF(2, 2, self.TRACK_W, self.TRACK_H)
        if disabled:
            track_color = QColor(theme.BG_RECESS).darker(130)
            border_color = QColor(theme.BORDER).darker(140)
        elif self._on:
            track_color = QColor(theme.ACCENT)
            track_color.setAlpha(220)
            border_color = theme.ACCENT
        else:
            track_color = QColor(theme.BG_RECESS)
            border_color = theme.BORDER
        p.setBrush(track_color)
        p.setPen(QPen(border_color, 1))
        p.drawRoundedRect(track_rect, self.TRACK_H / 2, self.TRACK_H / 2)

        # Thumb
        thumb_y = (self.TRACK_H + 4) / 2
        if disabled:
            thumb_fill = QColor(theme.TEXT_FAINT).darker(140)
            thumb_border = QColor(theme.BORDER).darker(140)
        elif self._on:
            thumb_fill = QColor(255, 255, 255)
            thumb_border = theme.ACCENT.darker(140)
        else:
            thumb_fill = QColor(theme.TEXT_FAINT)
            thumb_border = theme.BORDER
        p.setBrush(thumb_fill)
        p.setPen(QPen(thumb_border, 1))
        p.drawEllipse(
            QRectF(self._thumb_pos - self.THUMB_R,
                   thumb_y - self.THUMB_R,
                   self.THUMB_R * 2, self.THUMB_R * 2))
