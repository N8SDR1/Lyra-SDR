"""Stepper readout — compact "[−]  value  [+]" widget.

Replaces a QSlider + value-label pair in cases where:

* the value range is small (~tens of integer steps)
* precise readout matters more than fast drag-tuning
* horizontal real estate is at a premium
* accidental drag from a wheel-bumped slider would be expensive

Used on the DSP+Audio panel top row (CLAUDE.md §15.17) for the
three level controls (Vol RX1, Vol RX2, AF Gain).  Designed to be
reusable: panel code instantiates with (label, vmin, vmax, step,
shift_step, unit, decimals) and connects a single ``valueChanged``
signal.

Operator gestures:

* Click ``[−]`` / ``[+]``  → step by ``step`` (default 1).
* Shift+click               → step by ``shift_step`` (default 5).
* Click-and-hold            → step once, pause 400 ms, then repeat
                              at 12 Hz; after 2 sec of holding the
                              repeat granularity widens to
                              ``shift_step`` so big swings finish in
                              a reasonable time.
* Mouse-wheel over widget   → step (one tick = one ``step``).
* Right-click value label   → ``QInputDialog`` for exact-value
                              typed entry.  Matches the AGC-threshold
                              right-click gesture pattern.

What this widget DELIBERATELY does NOT do (per §15.17 design lock):

* No double-click-to-reset gesture (operator: "not needed").
* No Shift-wheel for larger step (operator: "not needed").
* No tick marks / detents / "0" snap line (numeric readout is the
  detent — operator sees the value directly).

The widget owns its current value and clamps on ``setValue()`` calls.
The driving signal (``valueChanged``) fires after every change —
panel code is responsible for forwarding into Radio's setter
(possibly with a unit conversion shim, see §15.17 Vol RX1 / RX2
linear↔dB note).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QPushButton, QSizePolicy, QWidget,
)


class StepperReadout(QWidget):
    """Compact [−] value [+] widget — see module docstring."""

    valueChanged = Signal(float)

    # Click-and-hold ramp constants — chosen to feel like the
    # Windows-volume-mixer scroll, not a CAD-tool nudge widget.
    _HOLD_INITIAL_PAUSE_MS = 400        # delay before first repeat
    _HOLD_REPEAT_INTERVAL_MS = 84       # ~12 Hz
    _HOLD_ACCELERATE_AFTER_MS = 2000    # widen step granularity after this
    # (Accelerated phase: same 12 Hz repeat rate, but each tick spans
    # ``shift_step`` units instead of ``step`` units — keeps the ramp
    # smooth-feeling but covers large ranges in <5 sec.)

    def __init__(self,
                 label: str,
                 vmin: float,
                 vmax: float,
                 *,
                 step: float = 1.0,
                 shift_step: float = 5.0,
                 unit: str = "",
                 decimals: int = 0,
                 initial: float | None = None,
                 caption_width: int | None = None,
                 value_width: int = 60,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._step = float(step)
        self._shift_step = float(shift_step)
        self._unit = str(unit)
        self._decimals = int(decimals)
        self._value = self._clamp(
            float(initial) if initial is not None else self._vmin)

        # ── Layout: [caption] [−] [value] [+] ──────────────────────
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._caption_label = QLabel(label)
        if caption_width is not None:
            self._caption_label.setFixedWidth(int(caption_width))
        lay.addWidget(self._caption_label)

        self._minus_btn = QPushButton("−")
        self._minus_btn.setObjectName("stepper_btn")
        self._minus_btn.setFixedWidth(26)
        self._minus_btn.setAutoRepeat(False)        # we own the ramp
        self._minus_btn.setCursor(Qt.PointingHandCursor)
        self._minus_btn.setFocusPolicy(Qt.NoFocus)  # don't steal Tab focus
        lay.addWidget(self._minus_btn)

        self._value_label = QLabel(self._format_value(self._value))
        self._value_label.setObjectName("stepper_value")
        self._value_label.setAlignment(Qt.AlignCenter)
        self._value_label.setFixedWidth(int(value_width))
        self._value_label.setCursor(Qt.PointingHandCursor)
        self._value_label.setToolTip(
            f"Right-click to type an exact value "
            f"(range {self._format_value(self._vmin)} … "
            f"{self._format_value(self._vmax)}).")
        # Right-click on the value label → exact-entry dialog.
        self._value_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self._value_label.customContextMenuRequested.connect(
            self._open_exact_entry_dialog)
        lay.addWidget(self._value_label)

        self._plus_btn = QPushButton("+")
        self._plus_btn.setObjectName("stepper_btn")
        self._plus_btn.setFixedWidth(26)
        self._plus_btn.setAutoRepeat(False)
        self._plus_btn.setCursor(Qt.PointingHandCursor)
        self._plus_btn.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self._plus_btn)

        # Widget itself can size to its contents (caller may further
        # set a fixed width if they want columnar alignment across
        # multiple instances).
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        # ── Hold-ramp state ────────────────────────────────────────
        self._hold_direction: int = 0    # +1, -1, or 0 (not held)
        self._hold_start_ms: int = 0
        self._hold_shift_active: bool = False  # was Shift held when press began

        self._initial_pause_timer = QTimer(self)
        self._initial_pause_timer.setSingleShot(True)
        self._initial_pause_timer.timeout.connect(self._on_initial_pause_done)

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(self._HOLD_REPEAT_INTERVAL_MS)
        self._repeat_timer.timeout.connect(self._on_hold_repeat)

        # ── Button wiring — pressed/released drive the ramp;
        # plain clicked is INTENTIONALLY not used because it fires
        # too late (on release) for the immediate single-step feel.
        # Instead, we step on press, then start the hold ramp; if the
        # button is released before the initial pause expires, that
        # press just acts as a single click.
        self._minus_btn.pressed.connect(lambda: self._on_btn_pressed(-1))
        self._minus_btn.released.connect(self._on_btn_released)
        self._plus_btn.pressed.connect(lambda: self._on_btn_pressed(+1))
        self._plus_btn.released.connect(self._on_btn_released)

    # ── Public API ──────────────────────────────────────────────────
    def value(self) -> float:
        """Return the current value."""
        return self._value

    def setValue(self, v: float) -> None:
        """Set the value, clamping to [vmin, vmax] and emitting
        ``valueChanged`` if it actually changed."""
        new = self._clamp(float(v))
        if abs(new - self._value) < 1e-9:
            return
        self._value = new
        self._value_label.setText(self._format_value(self._value))
        self.valueChanged.emit(self._value)

    def setRange(self, vmin: float, vmax: float) -> None:
        """Update the value range; re-clamps the current value."""
        self._vmin = float(vmin)
        self._vmax = float(vmax)
        self._value_label.setToolTip(
            f"Right-click to type an exact value "
            f"(range {self._format_value(self._vmin)} … "
            f"{self._format_value(self._vmax)}).")
        self.setValue(self._value)

    def setUnit(self, unit: str) -> None:
        """Update the displayed unit suffix."""
        self._unit = str(unit)
        self._value_label.setText(self._format_value(self._value))

    def caption_label(self) -> QLabel:
        """Expose the caption label so callers can restyle it
        (e.g. dim when the RX is muted)."""
        return self._caption_label

    def value_label(self) -> QLabel:
        """Expose the value label for the same reason."""
        return self._value_label

    # ── Mouse-wheel handling ────────────────────────────────────────
    def wheelEvent(self, ev: QWheelEvent) -> None:
        """Wheel up = step up, wheel down = step down.  Single step
        per notch — no Shift modifier per §15.17 lock."""
        delta = ev.angleDelta().y()
        if delta == 0:
            ev.ignore()
            return
        direction = +1 if delta > 0 else -1
        self._step_by(direction * self._step)
        ev.accept()

    # ── Internal helpers ────────────────────────────────────────────
    def _clamp(self, v: float) -> float:
        return max(self._vmin, min(self._vmax, v))

    def _format_value(self, v: float) -> str:
        if self._decimals <= 0:
            text = f"{int(round(v))}"
        else:
            text = f"{v:.{self._decimals}f}"
        return f"{text} {self._unit}".rstrip()

    def _step_by(self, delta: float) -> None:
        self.setValue(self._value + delta)

    # ── Button press / release / hold ramp ──────────────────────────
    def _on_btn_pressed(self, direction: int) -> None:
        """Step once immediately on press, then start the hold-ramp
        pause.  Shift held when the press begins = the entire press
        operates at ``shift_step`` granularity (both the single click
        and any subsequent ramp ticks).
        """
        from PySide6.QtWidgets import QApplication
        mods = QApplication.keyboardModifiers()
        self._hold_shift_active = bool(mods & Qt.ShiftModifier)
        per_tick = (self._shift_step
                    if self._hold_shift_active else self._step)
        self._hold_direction = direction
        # Immediate step.
        self._step_by(direction * per_tick)
        # Start the pause-before-repeat timer.
        from PySide6.QtCore import QDateTime
        self._hold_start_ms = QDateTime.currentMSecsSinceEpoch()
        self._initial_pause_timer.start(self._HOLD_INITIAL_PAUSE_MS)

    def _on_btn_released(self) -> None:
        """Cancel any pending ramp."""
        self._hold_direction = 0
        self._hold_shift_active = False
        self._initial_pause_timer.stop()
        self._repeat_timer.stop()

    def _on_initial_pause_done(self) -> None:
        """Initial pause done — start the 12 Hz repeat phase."""
        if self._hold_direction == 0:
            return
        self._repeat_timer.start()

    def _on_hold_repeat(self) -> None:
        """One repeat tick.  After _HOLD_ACCELERATE_AFTER_MS we
        widen each tick to ``shift_step`` granularity so the operator
        isn't watching a slow crawl across a 60-unit range."""
        if self._hold_direction == 0:
            return
        from PySide6.QtCore import QDateTime
        elapsed = (QDateTime.currentMSecsSinceEpoch()
                   - self._hold_start_ms)
        if self._hold_shift_active or elapsed >= self._HOLD_ACCELERATE_AFTER_MS:
            per_tick = self._shift_step
        else:
            per_tick = self._step
        self._step_by(self._hold_direction * per_tick)

    # ── Right-click exact-entry dialog ─────────────────────────────
    def _open_exact_entry_dialog(self, _pos) -> None:
        """Pop a QInputDialog for typed value entry.  Honors decimals."""
        title = f"Set {self._caption_label.text()}"
        prompt = (f"Enter value "
                  f"({self._format_value(self._vmin)} … "
                  f"{self._format_value(self._vmax)}):")
        if self._decimals <= 0:
            new, ok = QInputDialog.getInt(
                self, title, prompt,
                value=int(round(self._value)),
                minValue=int(round(self._vmin)),
                maxValue=int(round(self._vmax)),
                step=int(round(self._step)) or 1)
        else:
            new, ok = QInputDialog.getDouble(
                self, title, prompt,
                value=float(self._value),
                minValue=float(self._vmin),
                maxValue=float(self._vmax),
                decimals=self._decimals)
        if ok:
            self.setValue(float(new))
