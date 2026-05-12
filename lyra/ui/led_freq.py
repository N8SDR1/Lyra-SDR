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

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QLineEdit, QSizePolicy, QWidget

from . import theme


def parse_freq_input(text: str) -> int | None:
    """Parse a free-form frequency entry into Hz, or None if invalid.

    Accepts all the formats an operator might reasonably type:

      "7.074"       → 7,074,000 Hz   (single dot → MHz decimal)
      "7,074"       → 7,074,000 Hz   (Euro decimal)
      "7.125.000"   → 7,125,000 Hz   (multi-DOT → Hz with separators)
      "7,074,000"   → 7,074,000 Hz   (multi-COMMA → Hz with separators)
      "7074000"     → 7,074,000 Hz   (bare large → Hz)
      "7074"        → 7,074,000 Hz   (bare mid-range → kHz)
      "7"           → 7,000,000 Hz   (bare small → MHz)
      "14.230"      → 14,230,000 Hz
      ""            → None
      "garbage"     → None

    The multi-DOT case (matches the LED display's native MMM.kkk.hhh
    format) is the operating-friendly path — operators see
    "7.125.000" on the display and naturally type the same back in.
    """
    s = text.strip().replace(' ', '')
    if not s:
        return None
    # Multiple separators (any combination of commas + dots) =
    # thousands separators, NOT decimals. This is the LED-display-
    # native format ("7.125.000"), the comma-separator standard
    # ("7,074,000"), and any mixed entry an operator might type
    # ("7,074.000"). All decode to a raw Hz integer.
    if (s.count(',') + s.count('.')) > 1:
        try:
            return int(s.replace(',', '').replace('.', ''))
        except ValueError:
            return None
    # Single separator: it's a DECIMAL point (MHz). Comma is the
    # Euro-style decimal — normalize to dot before parsing.
    s = s.replace(',', '.')
    try:
        if '.' in s:
            return int(round(float(s) * 1_000_000))   # MHz with decimal
        n = int(s)
        if n < 100:
            return n * 1_000_000      # < 100 → MHz
        if n < 100_000:
            return n * 1_000          # 100 .. 99,999 → kHz
        return n                      # >= 100,000 → Hz
    except ValueError:
        return None


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
        # Optional EXTERNAL step in Hz — when set (>0) the mouse wheel
        # tunes by exactly this amount per click, regardless of which
        # digit the wheel is over. Lets the parent panel's "Step"
        # combo (1 / 10 / 50 / 100 / 500 / 1k / 5k / 10k Hz) drive the
        # wheel behavior, which matches how rigs and other SDR clients
        # work. When 0/None, falls back to per-digit 10^N stepping.
        self._external_step_hz: int = 0
        # Phase 3.B/3.D v0.1: focus-border state.  Painted in our
        # custom paintEvent (a setStyleSheet border is overdrawn by
        # the LED fillRect and never appears).  TuningPanel calls
        # ``set_focus_active(bool)`` on every focused_rx_changed
        # edge.
        #
        # Color scheme (operator UX 2026-05-12):
        #   * Focused RX (operator is listening here): GREEN
        #     (#00E676 -- bright "alive / listening" cue).
        #   * TX-active VFO (operator will transmit here): RED
        #     (#FF1744 -- universal radio TX convention; takes
        #     precedence over focus color when both apply, so the
        #     operator never confuses "I'm focused here" with
        #     "I'm about to transmit here").
        #
        # ``set_tx_active(bool)`` wires the RED override; today it
        # has no live caller (Phase 3.E TX integration will hook
        # this up to the MOX state machine when TX ships).
        self._focus_active: bool = False
        self._tx_active: bool = False
        # Inline text editor for direct frequency entry. Created lazy
        # on first double-click. Hidden when not editing — the LED
        # painting still happens normally underneath.
        self._edit_field: QLineEdit | None = None
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

    def set_focus_active(self, active: bool) -> None:
        """Phase 3.B/3.D v0.1 -- toggle the GREEN focus border that
        marks this LED as the currently-focused VFO.  Painted in
        our own paintEvent (a QSS border would be overdrawn by the
        widget's background fillRect).  Color changed orange ->
        green per operator UX call 2026-05-12; red is reserved for
        the TX-active state (see ``set_tx_active``)."""
        new = bool(active)
        if new == self._focus_active:
            return
        self._focus_active = new
        self.update()

    def set_tx_active(self, active: bool) -> None:
        """Phase 3.E hook (Lyra TX integration) -- toggle the RED
        border that marks this VFO as the active TX path.  RED
        takes precedence over GREEN focus color in paintEvent, so
        when both ``_tx_active`` and ``_focus_active`` are True the
        border renders red ("this is where I'm transmitting from"
        outranks "this is where I'm focused for tuning").

        No live caller today; wired by the MOX state machine when
        Phase 3.E / v0.2 TX ships.  Method added now so the
        FrequencyDisplay API surface is stable through TX work."""
        new = bool(active)
        if new == self._tx_active:
            return
        self._tx_active = new
        self.update()

    def set_external_step_hz(self, hz: int):
        """Set an external step value in Hz that the mouse wheel will
        use instead of per-digit stepping. 0 / None disables and
        restores per-digit behavior. Called by TuningPanel when the
        Step combo changes so the wheel honors the operator's
        chosen tuning resolution."""
        try:
            v = int(hz) if hz is not None else 0
        except (TypeError, ValueError):
            v = 0
        self._external_step_hz = max(0, v)
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

        # Phase 3.B/3.D/3.E v0.1 focus + TX border (painted LAST so
        # it sits on top of the LED background and digit overdraw).
        # Drawn just inside the widget's rect so the 2 px stroke
        # doesn't get clipped at the edges.
        #
        # Color precedence: TX active (red) > focus active (green) >
        # no border.  RED outranks GREEN so the operator never
        # mistakes "focused" for "transmitting" -- when SPLIT auto-
        # moves TX to VFO B during Phase 3.E, the operator sees
        # GREEN on the still-listening VFO and RED on the
        # transmitting one at a glance.
        if self._tx_active:
            p.setPen(QPen(QColor(0xFF, 0x17, 0x44), 2))   # red
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(
                QRectF(1.0, 1.0, w - 2.0, h - 2.0), 4.0, 4.0)
        elif self._focus_active:
            p.setPen(QPen(QColor(0x00, 0xE6, 0x76), 2))   # green
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(
                QRectF(1.0, 1.0, w - 2.0, h - 2.0), 4.0, 4.0)

    # ── Direct frequency entry ────────────────────────────────────────
    def _enter_edit_mode(self):
        """Show the inline QLineEdit overlaid on the digit area for
        direct typing. Pre-fills with the current frequency in MHz
        format with 6 decimals (matches what the operator sees on
        screen) and selects all so a fresh number replaces it."""
        if not self._enabled:
            return
        if self._edit_field is None:
            self._edit_field = QLineEdit(self)
            # Match the LED look closely so the transition feels
            # seamless: black bg, amber text, mono font, cyan
            # selection border, big digits.
            self._edit_field.setStyleSheet(
                "QLineEdit {"
                " background: #000000;"
                " color: #ffb000;"
                " border: 2px solid #00d8ff;"
                " border-radius: 3px;"
                " font-family: Consolas, monospace;"
                " font-weight: 700;"
                " padding: 2px 6px;"
                "}"
            )
            # Two commit triggers — belt + suspenders:
            #   returnPressed: explicit Enter/Return key
            #   editingFinished: also fires on Enter, AND on focus loss
            #     (so clicking outside the field commits rather than
            #     silently dropping the text)
            # Both call into _commit_edit which is guarded by a
            # _edit_committed flag — the flag prevents the same
            # entry from being applied twice (Enter fires both
            # signals back-to-back). Esc cancels via eventFilter
            # which sets _edit_cancelled to suppress the commit on
            # the editingFinished fired by the resulting focus loss.
            self._edit_cancelled = False
            self._edit_committed = False
            self._edit_field.returnPressed.connect(self._commit_edit)
            self._edit_field.editingFinished.connect(self._on_editing_finished)
            self._edit_field.installEventFilter(self)
        # Position over the full widget rect; keep it slightly inset
        # so the cyan border is visible.
        margin = 4
        self._edit_field.setGeometry(
            margin, margin,
            max(60, self.width() - 2 * margin),
            max(20, self.height() - 2 * margin),
        )
        # Match font size to the available height
        font = self._edit_field.font()
        font.setPixelSize(max(14, int((self.height() - 2 * margin) * 0.55)))
        self._edit_field.setFont(font)
        self._edit_field.setText(f"{self._freq_hz / 1_000_000:.6f}")
        self._edit_field.selectAll()
        # Reset state for this new editing session
        self._edit_cancelled = False
        self._edit_committed = False
        self._edit_field.show()
        self._edit_field.setFocus()

    def _commit_edit(self):
        if self._edit_field is None or self._edit_committed:
            return
        # Mark committed FIRST so the editingFinished signal that
        # follows our hide() can't re-enter this function and try
        # to commit the same text twice.
        self._edit_committed = True
        text = self._edit_field.text()
        self._edit_field.hide()
        hz = parse_freq_input(text)
        if hz is None:
            print(f"[freq] could not parse {text!r} — entry ignored")
            return
        new_hz = max(0, min(self.MAX_HZ, int(hz)))
        delta = new_hz - self._freq_hz
        if delta != 0:
            self._change_freq(delta)

    def _on_editing_finished(self):
        """editingFinished fires on Enter AND on focus loss. Treat
        focus-loss-with-content as "user clicked away meaning to
        commit" rather than silently discarding the text. The Esc
        path sets _edit_cancelled so we know to skip commit there."""
        if self._edit_cancelled:
            self._edit_cancelled = False
            return
        # Defer to commit. _commit_edit is idempotent (early-exits
        # if the field's already hidden) so a double-fire from
        # returnPressed + editingFinished is harmless.
        self._commit_edit()

    def _cancel_edit(self):
        if self._edit_field is not None:
            self._edit_cancelled = True
            self._edit_field.hide()

    def eventFilter(self, obj, event):
        # Intercept Esc on the inline editor to cancel without commit
        if obj is self._edit_field and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self._cancel_edit()
                return True
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event):
        """Double-click anywhere on the LED display → enter direct-
        edit mode. Right-clicking digits is reserved for future
        memory features so we use double-click here, which is also
        how most desktop apps signal "edit this thing"."""
        if self._enabled and event.button() == Qt.LeftButton:
            self._enter_edit_mode()

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
        delta_units = event.angleDelta().y() // 120
        if delta_units == 0:
            return
        # Two-tier wheel behavior (the typical SDR-client convention):
        #
        # 1. If the cursor is HOVERING a specific digit, that digit's
        #    place value (10^digit_index) wins. Lets the operator
        #    aim precisely — hover the kHz digit to tune in 1 kHz
        #    steps regardless of what the Step combo says.
        #
        # 2. Otherwise (cursor not on a digit, or aiming at the
        #    overall display body), use the external step from the
        #    parent panel's Step combo. That's the operator-set
        #    "default tuning resolution".
        #
        # 3. As a last resort (no external step set, no digit hover,
        #    but a digit IS selected from a previous click), use that
        #    selected digit's place value.
        hover_digit = -1
        for idx, rect in self._digit_rects:
            if rect.contains(event.position()):
                hover_digit = idx
                self._selected = idx
                break
        if hover_digit >= 0:
            step = 10 ** hover_digit
        elif self._external_step_hz > 0:
            step = self._external_step_hz
        elif self._selected >= 0:
            step = 10 ** self._selected
        else:
            return super().wheelEvent(event)
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
