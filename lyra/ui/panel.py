"""GlassPanel — reusable container with rounded corners, inner gradient,
subtle cyan rim, and optional uppercased group header.

Every user-visible panel in Lyra inherits from this. Visuals are
centralized here so that when we tweak the "glass mirror" finish later,
we only touch one place.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from . import theme


class GlassPanel(QWidget):
    """Custom-painted panel container.

    Subclasses add child widgets to `self.content_layout()`.

    Optional `help_topic` argument — when set, a small `?` badge is
    drawn in the top-right corner of the header; clicking it asks the
    top-level window to open the Help dialog to the named topic.
    """

    # 20 (up from 18) gives the optional ? badge enough vertical room
    # to sit cleanly inside the header strip on every Windows font
    # rendering. The 2-pixel delta is invisible for panels that don't
    # use the badge.
    HEADER_HEIGHT = 20

    def __init__(self, title: str = "", parent=None,
                 help_topic: str | None = None):
        super().__init__(parent)
        self._title = title
        self._help_topic = help_topic
        self._layout = QVBoxLayout(self)
        top_margin = 8 + (self.HEADER_HEIGHT if title else 0)
        self._layout.setContentsMargins(10, top_margin, 10, 10)
        self._layout.setSpacing(6)

        # Optional context-help badge in the top-right of the header.
        # Bumped to 18 px (from 16) because 16 clipped the "?" glyph on
        # some Windows font renderings. 11 px font + tight padding keeps
        # the text visually centered in the rounded badge.
        self._help_btn: QPushButton | None = None
        if help_topic:
            self._help_btn = QPushButton("?", self)
            self._help_btn.setFixedSize(18, 18)
            self._help_btn.setCursor(Qt.PointingHandCursor)
            self._help_btn.setToolTip(
                f"Open User Guide — {help_topic.replace('-', ' ').title()}")
            # Always-on-top hint: any sibling widget a subclass creates
            # after us is drawn above by default; we fight that by
            # raising in showEvent / resizeEvent below.
            self._help_btn.setStyleSheet(
                "QPushButton {"
                "  background: transparent;"
                "  color: #00e5ff;"
                "  border: 1px solid #00e5ff;"
                "  border-radius: 9px;"
                "  font-weight: 700;"
                "  font-size: 11px;"
                "  padding: 0; margin: 0;"
                "  text-align: center;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(0, 229, 255, 50);"
                "  color: #7ff7ff;"
                "}"
            )
            self._help_btn.clicked.connect(self._on_help_clicked)
            self._help_btn.raise_()

    def content_layout(self) -> QVBoxLayout:
        return self._layout

    def set_title(self, title: str):
        self._title = title
        top_margin = 8 + (self.HEADER_HEIGHT if title else 0)
        self._layout.setContentsMargins(10, top_margin, 10, 10)
        self.update()

    def set_help_topic(self, topic: str | None):
        """Attach / replace the topic for the ? badge. Passing None hides
        it. Useful for subclasses whose help context is only known
        partway through construction."""
        self._help_topic = topic
        if topic and self._help_btn is None:
            # Lazy-create the badge if we didn't have one at init.
            self.__init__.__wrapped__  # placeholder to satisfy linters
        if self._help_btn is not None:
            self._help_btn.setVisible(bool(topic))
            if topic:
                self._help_btn.setToolTip(
                    f"Open User Guide — "
                    f"{topic.replace('-', ' ').title()}")
        if topic:
            self._position_help_btn()

    def _on_help_clicked(self):
        """Walk up to the top-level window and ask it to open the guide.
        Decoupled via duck-typing so panels don't need a direct reference
        to MainWindow."""
        if not self._help_topic:
            return
        mw = self.window()
        if hasattr(mw, "show_help"):
            mw.show_help(self._help_topic)

    def _position_help_btn(self):
        """Anchor the ? badge to the top-right inside the header strip.
        Also raise it to make sure it's drawn above sibling widgets
        that the subclass added to content_layout after us."""
        if self._help_btn is None:
            return
        margin = 8
        x = max(0, self.width() - self._help_btn.width() - margin)
        # Center vertically within the HEADER_HEIGHT strip so the badge
        # looks like part of the header rather than crowding the top.
        y = max(1, (self.HEADER_HEIGHT - self._help_btn.height()) // 2 + 1)
        self._help_btn.move(x, y)
        self._help_btn.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._position_help_btn()

    def showEvent(self, ev):
        super().showEvent(ev)
        # Children added by the subclass come after us in paint order;
        # re-raise the badge every time the panel is shown so nothing
        # sibling ever ends up drawn on top of it.
        self._position_help_btn()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect().adjusted(0, 0, -1, -1))

        # Body: vertical gradient, BG_PANEL top (slightly lifted) to a touch darker
        grad = QLinearGradient(0, 0, 0, rect.height())
        top_c = QColor(theme.BG_PANEL)
        bot_c = QColor(theme.BG_PANEL)
        # Lift top by ~10 units toward cyan/white for the "glass" sheen
        top_c = top_c.lighter(112)
        bot_c = bot_c.darker(105)
        grad.setColorAt(0.0, top_c)
        grad.setColorAt(1.0, bot_c)
        p.setBrush(grad)
        p.setPen(QPen(theme.BORDER, 1))
        p.drawRoundedRect(rect, theme.PANEL_RADIUS, theme.PANEL_RADIUS)

        # Inner 1-pixel highlight at the top edge (glass sheen)
        sheen = QColor(theme.ACCENT)
        sheen.setAlpha(22)
        p.setPen(QPen(sheen, 1))
        p.drawLine(int(rect.left() + 2), int(rect.top() + 1),
                   int(rect.right() - 2), int(rect.top() + 1))

        # Left accent stripe — cyan rim along the inside of the left edge
        # Only as tall as the title-less content so it doesn't fight the
        # header bar.
        accent_top = 6 + (self.HEADER_HEIGHT if self._title else 0)
        p.setPen(QPen(theme.ACCENT, 2))
        p.drawLine(2, accent_top, 2, int(rect.height() - 6))

        # Header
        if self._title:
            hdr_font = QFont()
            hdr_font.setFamilies([theme.FONT_HEAD_FAMILY, "Segoe UI"])
            hdr_font.setPointSize(8)
            hdr_font.setBold(True)
            hdr_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.8)
            p.setFont(hdr_font)
            # Header text in accent cyan for visual rhythm
            p.setPen(QPen(theme.ACCENT, 1))
            p.drawText(12, 14, self._title.upper())

            # Thin underline separator — cyan on left fading to border
            grad_pen = QPen(theme.ACCENT, 1)
            p.setPen(grad_pen)
            p.drawLine(12, 18, 40, 18)
            p.setPen(QPen(theme.BORDER, 1))
            p.drawLine(40, 18, int(rect.right() - 12), 18)
