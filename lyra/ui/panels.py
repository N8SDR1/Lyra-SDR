"""All Lyra control panels. Each subclasses GlassPanel and binds to
the central Radio controller via signals.

Panels are split by function (Connection, Tuning, Mode/Filter, Gain,
DSP/Notch, Audio Output, Spectrum, Waterfall, S-Meter). Adding or
relocating panels in the main layout is a one-liner in app.py.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMenu, QPushButton, QSizePolicy, QSlider,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)


# ── Captured-noise-profile name length cap ──────────────────────────
# Why this exists: the inline source-badge on the DSP+Audio panel has
# a fixed maximum width.  Without a length cap, an operator who types
# a long profile name ("Ridgewood AM-broadcast carrier 1490 kHz")
# gets it elided to "Ridgewood AM-bro…" on the badge — confusing,
# because the UI silently changes what they entered.  Better: cap
# at the prompt so what they type is what they see.  24 chars fits
# the badge cleanly along with age + band/mode + ⇄ glyph.
MAX_PROFILE_NAME_CHARS = 24


def _prompt_profile_name(
    parent, title: str, prompt: str, default_text: str = "") -> tuple[str, bool]:
    """Show a single-line text prompt with a hard length cap.

    Wraps QInputDialog so the underlying QLineEdit gets setMaxLength
    applied — QInputDialog.getText() doesn't expose that knob, so
    we build a QInputDialog manually and reach into its line edit.
    Returns (text, ok) — same shape as QInputDialog.getText.
    """
    dlg = QInputDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setLabelText(prompt)
    dlg.setTextValue(default_text)
    dlg.setInputMode(QInputDialog.TextInput)
    # Reach the internal QLineEdit and clamp it.  Qt's docs aren't
    # explicit about which child object hosts it, but findChild on
    # QLineEdit reliably returns the prompt's editor.
    #
    # Legacy-name preservation: if the dialog is pre-populated with
    # a name that already exceeds the cap (operator created it before
    # the cap was added), don't silently truncate — let them round-
    # trip the existing name through rename without losing chars.
    # New names are still capped at MAX_PROFILE_NAME_CHARS.
    le = dlg.findChild(QLineEdit)
    if le is not None:
        cap = max(MAX_PROFILE_NAME_CHARS, len(default_text))
        le.setMaxLength(cap)
    ok = bool(dlg.exec())
    return dlg.textValue(), ok


class SteppedSlider(QSlider):
    """QSlider that paints visible tick marks ON TOP of the styled
    groove + handle.

    Why this exists: plain QSS-styled QSliders lose their native tick
    marks. Once `QSlider::groove` or `QSlider::handle` is stylesheeted,
    Qt switches the whole widget to fully-custom rendering and skips
    tick painting entirely (a long-standing Qt quirk; see Qt forum
    threads going back to Qt 5). Setting tickPosition + tickInterval
    becomes a no-op for visible feedback.

    Fix: subclass and overdraw ticks ourselves after the styled paint.
    Used for the FPS + Waterfall step-list sliders so the operator can
    actually see the discrete detent positions.
    """

    TICK_COLOR = QColor(140, 165, 195, 200)
    TICK_WIDTH_PX = 1
    TICK_HEIGHT_PX = 4
    TICK_PAD_PX = 1   # gap between groove bottom and tick top

    def paintEvent(self, event):
        super().paintEvent(event)
        if (self.tickPosition() == QSlider.NoTicks
                or self.orientation() != Qt.Horizontal):
            return
        rng = self.maximum() - self.minimum()
        if rng <= 0:
            return
        interval = max(1, self.tickInterval())
        # The styled handle is 12 px wide (theme.py). Slider's drawable
        # range starts handle_w/2 from each end so ticks line up with
        # the handle's center at min and max.
        HANDLE_W = 12
        track_left = HANDLE_W / 2
        track_right = self.width() - HANDLE_W / 2
        track_w = max(1.0, track_right - track_left)
        # Y center of the groove is roughly widget mid-height. Theme
        # gives groove height=4. Ticks below that, with TICK_PAD gap.
        groove_y_mid = self.height() / 2
        tick_y_top = int(groove_y_mid + 2 + self.TICK_PAD_PX)
        tick_y_bot = tick_y_top + self.TICK_HEIGHT_PX

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(self.TICK_COLOR, self.TICK_WIDTH_PX))
        i = self.minimum()
        while i <= self.maximum():
            frac = (i - self.minimum()) / rng
            x = int(round(track_left + frac * track_w))
            painter.drawLine(x, tick_y_top, x, tick_y_bot)
            i += interval
        painter.end()

from lyra.radio import Radio
from lyra.protocol.stream import SAMPLE_RATES
from lyra.ui.panel import GlassPanel
from lyra.ui.spectrum import SpectrumWidget, WaterfallWidget
from lyra.ui.smeter import SMeter, AnalogMeter, LedBarMeter, LitArcMeter
from lyra.control.tci import TciServer, TCI_DEFAULT_PORT
from lyra.bands import AMATEUR_BANDS, BROADCAST_BANDS, GEN_SLOTS, band_for_freq
from lyra.ui.led_freq import FrequencyDisplay


# ── Slider step lists ────────────────────────────────────────────────
# Both the front-panel ViewPanel and the Settings → Display tab share
# these so the two sliders can never disagree about what each detent
# means. Hand-curated to give fine grain at the low end (where each
# step changes the visual feel substantially) and coarser jumps at the
# high end (where 50 vs 55 fps is indistinguishable).
#
# Reference: 40 Hz is a common SDR-client default; 60 Hz with
# spectrum averaging enabled is the smoothness/cost sweet spot for
# most operator hardware.  Lyra's default is 40 fps.
SPECTRUM_FPS_STEPS: tuple[int, ...] = (
    5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90, 120,
)
SPECTRUM_FPS_DEFAULT = 40   # index 6 in SPECTRUM_FPS_STEPS

# Waterfall step list — (divider, multiplier) tuples ordered FAST → SLOW.
# Index 8 is the "neutral" 1-row-per-FFT setting. Operator-facing slider
# is INVERTED (right = faster) so movement direction matches expectation.
#
# Bumped 2026-04-29: max multiplier extended from 10× → 30× per operator
# request. At low spec rates (5-20 fps), the previous 10× cap meant
# rows-per-second was too slow for digital-mode hunting (FT8 etc).
# Multiplier-mode rows are linearly interpolated from the previous FFT
# (no CPU cost beyond the single new FFT we already computed).
WATERFALL_SPEED_STEPS: tuple[tuple[int, int], ...] = (
    # Fast end (multiplier > 1, divider = 1)
    (1, 30), (1, 20), (1, 15), (1, 10), (1, 6), (1, 4), (1, 3), (1, 2),
    # Neutral (index 8) — one row per FFT
    (1, 1),
    # Slow end (divider > 1, multiplier = 1)
    (2, 1), (3, 1), (5, 1), (8, 1), (12, 1), (20, 1),
)
WATERFALL_NEUTRAL_INDEX = 8


def fps_to_slider_position(fps: int) -> int:
    """Find the closest step index for an arbitrary FPS value. Used
    when restoring slider position from Radio state (which may hold
    a value not in the step list — e.g. a legacy QSettings value)."""
    fps = int(fps)
    return min(range(len(SPECTRUM_FPS_STEPS)),
               key=lambda i: abs(SPECTRUM_FPS_STEPS[i] - fps))


def fps_from_slider_position(pos: int) -> int:
    """Slider position → FPS. Clamps to valid range."""
    pos = max(0, min(len(SPECTRUM_FPS_STEPS) - 1, int(pos)))
    return SPECTRUM_FPS_STEPS[pos]


def wf_to_slider_position(divider: int, multiplier: int) -> int:
    """Find the closest step index for an arbitrary (divider, multiplier)
    pair. Compares the effective scroll factor (multiplier / divider)
    against each step's factor."""
    divider = max(1, int(divider))
    multiplier = max(1, int(multiplier))
    target = multiplier / divider
    best_idx = WATERFALL_NEUTRAL_INDEX
    best_diff = float("inf")
    for i, (d, m) in enumerate(WATERFALL_SPEED_STEPS):
        diff = abs((m / d) - target)
        if diff < best_diff:
            best_diff = diff
            best_idx = i
    return best_idx


def wf_from_slider_position(pos: int) -> tuple[int, int]:
    """Slider position → (divider, multiplier). Clamps to valid range."""
    pos = max(0, min(len(WATERFALL_SPEED_STEPS) - 1, int(pos)))
    return WATERFALL_SPEED_STEPS[pos]


# ── Connection ──────────────────────────────────────────────────────────
class ConnectionPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("CONNECTION", parent, help_topic="getting-started")
        self.radio = radio

        h = QHBoxLayout()
        h.addWidget(QLabel("IP"))
        self.ip_edit = QLineEdit(radio.ip)
        self.ip_edit.setFixedWidth(130)
        self.ip_edit.editingFinished.connect(self._on_ip_commit)
        h.addWidget(self.ip_edit)

        self.disc_btn = QPushButton("Discover")
        self.disc_btn.clicked.connect(self._on_discover)
        h.addWidget(self.disc_btn)

        self.start_btn = QPushButton("Start")
        self.start_btn.setFixedWidth(90)
        self.start_btn.setCheckable(True)
        self.start_btn.clicked.connect(self._on_start_stop)
        h.addWidget(self.start_btn)

        self.content_layout().addLayout(h)

        radio.ip_changed.connect(lambda ip: self.ip_edit.setText(ip))
        radio.stream_state_changed.connect(self._on_stream_changed)

    def _on_ip_commit(self):
        self.radio.set_ip(self.ip_edit.text().strip())

    def _on_discover(self):
        self.disc_btn.setEnabled(False)
        try:
            self.radio.discover()
        finally:
            self.disc_btn.setEnabled(not self.radio.is_streaming)

    def _on_start_stop(self):
        if self.radio.is_streaming:
            self.radio.stop()
        else:
            self.radio.start()

    def _on_stream_changed(self, running: bool):
        self.start_btn.setText("Stop" if running else "Start")
        self.start_btn.setChecked(running)
        self.ip_edit.setEnabled(not running)
        self.disc_btn.setEnabled(not running)


# ── Tuning ──────────────────────────────────────────────────────────────
class TuningPanel(GlassPanel):
    """VFO panel. Three-column layout:

        [ RX1 freq display ]  [ LOGO ]  [ RX2 freq display ]

    RX2 is a disabled placeholder until the second receiver is wired
    (HL2 has the headroom — DDC2 slot + a second set of audio taps —
    the Radio just hasn't been taught about it yet). Keeping the UI
    slot here so the layout doesn't shift when RX2 lands; we just
    flip `set_vfo_enabled(True)` on that widget.

    Below the three-column VFO row sits a TX-split strip (hidden
    until TX path ships), then the MHz type-in + Step selector.
    """

    def __init__(self, radio: Radio, parent=None):
        super().__init__("TUNING", parent, help_topic="tuning")
        self.radio = radio

        # Operator feedback v0.0.6.x: "I cannot adjust the height of
        # the Tuning panel."  Root cause: the FrequencyDisplay widget
        # ships with QSizePolicy.Fixed vertically, which made the
        # RX1/RX2 columns refuse extra height; even with the logo
        # column's internal stretches, Qt's layout engine reported
        # row1 as effectively Fixed vertical to the parent dock, so
        # the QMainWindow row separator wouldn't drag.  We declare an
        # explicit MinimumExpanding vertical policy on the panel and
        # a friendly minimum height (operator can still shrink it
        # well below the default), and override the freq_display
        # vertical policy further down to Preferred so the column
        # cooperates.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        self.setMinimumHeight(180)

        outer = QVBoxLayout()
        outer.setSpacing(4)

        # ── Row 1: RX1 | LOGO | RX2 ──────────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        # RX1 — the live VFO. Small "RX1" label above it so its
        # identity is explicit once RX2 and TX split come online.
        rx1_col = QVBoxLayout()
        rx1_col.setSpacing(2)
        rx1_label = QLabel("RX1")
        rx1_label.setStyleSheet(
            "color: #00e5ff; font-weight: 800; "
            "letter-spacing: 1.5px; font-size: 9px;")
        rx1_col.addWidget(rx1_label)
        self.freq_display = FrequencyDisplay()
        # Override the class-level QSizePolicy.Fixed → Preferred so
        # the freq column cooperates when the row is asked to grow
        # (see panel-level note above on Tuning vertical resize).
        # Also drop the maximum-height cap: it was 46 but the class
        # minimum is 66, so it was a no-op constraint anyway, and
        # the LED renderer scales gracefully when given more room.
        self.freq_display.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.freq_display.set_freq_hz(radio.freq_hz)
        self.freq_display.freq_changed.connect(self.radio.set_freq_hz)
        rx1_col.addWidget(self.freq_display)
        row1.addLayout(rx1_col, 5)      # stretch weight

        # Logo — center column. 130 px scaled from the 256 source
        # for crisp rendering at larger sizes. Stretch weight 3 gives
        # it a properly wide middle column. Top padding pushes the
        # logo down a few pixels for breathing room between the
        # panel header and the logo crown.
        logo_container = QVBoxLayout()
        logo_container.setSpacing(0)
        logo_container.setContentsMargins(0, 0, 0, 0)
        logo_container.addSpacing(6)          # fixed top padding
        logo_container.addStretch(1)          # flex above
        self.logo_label = QLabel()
        from PySide6.QtGui import QPixmap as _QPixmap
        from lyra import resource_root
        # resource_root() handles both dev-tree and PyInstaller-frozen
        # paths so the logo loads correctly when running from the .exe.
        logo_path = (resource_root() /
                     "assets" / "logo" / "lyra-icon-256.png")
        if logo_path.is_file():
            pix = _QPixmap(str(logo_path))
            self.logo_label.setPixmap(pix.scaled(
                150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.logo_label.setAlignment(Qt.AlignCenter)
            self.logo_label.setToolTip(
                "Lyra SDR — click to open the User Guide (F1)")
            self.logo_label.setCursor(Qt.PointingHandCursor)
            self.logo_label.mousePressEvent = (
                lambda _ev: self.window().show_help()
                if hasattr(self.window(), "show_help") else None)
        logo_container.addWidget(self.logo_label, alignment=Qt.AlignCenter)
        logo_container.addStretch(1)
        row1.addLayout(logo_container, 3)

        # RX2 — placeholder. Shown as a dimmed disabled FrequencyDisplay
        # with an "RX2 — DISABLED" banner. Ready for activation when
        # the second receiver comes online.
        rx2_col = QVBoxLayout()
        rx2_col.setSpacing(2)
        rx2_label = QLabel("RX2")
        rx2_label.setStyleSheet(
            "color: #6a7a8c; font-weight: 800; "
            "letter-spacing: 1.5px; font-size: 9px;")
        rx2_col.addWidget(rx2_label)
        self.freq_display_rx2 = FrequencyDisplay()
        # Same vertical policy override as the RX1 freq display —
        # without it, this column also reports Fixed vertical and
        # blocks row resizing.
        self.freq_display_rx2.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.freq_display_rx2.set_freq_hz(0)
        self.freq_display_rx2.set_vfo_enabled(False, "RX2 — not yet wired")
        rx2_col.addWidget(self.freq_display_rx2)
        row1.addLayout(rx2_col, 5)
        outer.addLayout(row1)

        # ── Row 2: TX split strip (hidden until TX lands) ────────
        # The strip is built now so layout is stable; it just stays
        # hidden. When TX ships we setVisible(True) and wire the freq.
        self.tx_split_row = QWidget()
        tx_h = QHBoxLayout(self.tx_split_row)
        tx_h.setContentsMargins(0, 0, 0, 0)
        tx_h.setSpacing(6)
        tx_label = QLabel("TX1 SPLIT")
        tx_label.setStyleSheet(
            "color: #ff6bcb; font-weight: 800; "
            "letter-spacing: 1.5px; font-size: 9px;")
        tx_h.addWidget(tx_label)
        self.tx_split_info = QLabel("— off —")
        self.tx_split_info.setStyleSheet(
            "color: #8a9aac; font-style: italic; font-size: 10px;")
        tx_h.addWidget(self.tx_split_info)
        tx_h.addStretch(1)
        self.tx_split_row.setVisible(False)      # flip on when TX ships
        outer.addWidget(self.tx_split_row)

        # Breathing room between the freq-display row and the MHz +
        # Step controls below — operator feedback v0.0.6.x reported
        # the MHz/Step labels were getting visually clipped against
        # the bottom edge of the freq-display digits.  10 px gap is
        # noticeable without pushing the rest of the panel down too far.
        outer.addSpacing(10)

        # ── Row 3: MHz type-in + Step ────────────────────────────
        h = QHBoxLayout()
        h.addWidget(QLabel("MHz"))
        self.freq_spin = QDoubleSpinBox()
        self.freq_spin.setDecimals(6)
        self.freq_spin.setRange(0.0, 55.0)
        self.freq_spin.setValue(radio.freq_hz / 1e6)
        self.freq_spin.setFixedWidth(130)
        self.freq_spin.setKeyboardTracking(False)
        self.freq_spin.valueChanged.connect(self._on_freq_changed)
        h.addWidget(self.freq_spin)

        h.addWidget(QLabel("Step"))
        self.step_combo = QComboBox()
        for label, hz in [("1 Hz", 1), ("10 Hz", 10), ("50 Hz", 50),
                          ("100 Hz", 100), ("500 Hz", 500), ("1 kHz", 1000),
                          ("5 kHz", 5000), ("10 kHz", 10000)]:
            self.step_combo.addItem(label, hz)
        self.step_combo.setCurrentText("1 kHz")
        self.step_combo.setFixedWidth(80)
        self.step_combo.currentIndexChanged.connect(self._on_step_changed)
        h.addWidget(self.step_combo)
        self._on_step_changed(self.step_combo.currentIndex())
        h.addStretch(1)

        outer.addLayout(h)
        # Final vertical stretch — without this, the panel's outer
        # layout has a fixed sizeHint (logo + freq row + MHz row +
        # spacing) and Qt's QMainWindow dock-area layout treats the
        # row as effectively pinned at that height.  Operator
        # feedback v0.0.6.x: "I can widen Tuning but can't change
        # its height while the others resize fine."  The stretch
        # gives the dock somewhere to put extra vertical space so
        # the top-area / center separator can be dragged up or down.
        outer.addStretch(1)
        self.content_layout().addLayout(outer)

        radio.freq_changed.connect(self._on_radio_freq_changed)

    def _on_freq_changed(self, mhz: float):
        self.radio.set_freq_hz(int(round(mhz * 1e6)))

    def _on_step_changed(self, _idx):
        step = int(self.step_combo.currentData())
        self.freq_spin.setSingleStep(step / 1e6)
        # Push the step to the LED display so its mouse wheel uses
        # this Hz value instead of per-digit 10^N stepping. Operators
        # expect "I picked 100 Hz step → wheeling tunes 100 Hz per
        # click no matter where my cursor is on the digits".
        if hasattr(self, "freq_display"):
            self.freq_display.set_external_step_hz(step)

    def _on_radio_freq_changed(self, hz: int):
        # Sync both the LED display and the backup spinbox
        self.freq_display.set_freq_hz(hz)
        mhz = hz / 1e6
        if abs(self.freq_spin.value() - mhz) > 0.0000005:
            self.freq_spin.blockSignals(True)
            self.freq_spin.setValue(mhz)
            self.freq_spin.blockSignals(False)


# ── Mode / Filter / Rate ────────────────────────────────────────────────
class ModeFilterPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("MODE + FILTER", parent, help_topic="modes-filters")
        self.radio = radio

        # Layout strategy: each label+combo is packed tight in a
        # sub-layout (3 px gap), and sub-layouts are separated by
        # larger gaps (12 px) so the visual grouping is clear without
        # wasting horizontal space between a label and its widget.
        h = QHBoxLayout()
        h.setSpacing(12)

        def _pair(label: str, widget) -> QHBoxLayout:
            lyt = QHBoxLayout()
            lyt.setSpacing(3)
            lyt.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label)
            lyt.addWidget(lbl)
            lyt.addWidget(widget)
            return lyt

        self.rate_combo = QComboBox()
        for r in SAMPLE_RATES:
            self.rate_combo.addItem(f"{r // 1000} k", r)
        self.rate_combo.setFixedWidth(70)
        self._select_combo_data(self.rate_combo, radio.rate)
        self.rate_combo.currentIndexChanged.connect(
            lambda _i: self.radio.set_rate(int(self.rate_combo.currentData())))
        h.addLayout(_pair("Rate", self.rate_combo))

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(Radio.ALL_MODES)
        self.mode_combo.setCurrentText(radio.mode)
        self.mode_combo.setFixedWidth(80)
        self.mode_combo.currentTextChanged.connect(self.radio.set_mode)
        h.addLayout(_pair("Mode", self.mode_combo))

        self.rx_bw_combo = QComboBox()
        self.rx_bw_combo.setFixedWidth(80)
        self.rx_bw_combo.currentIndexChanged.connect(self._on_rx_bw_changed)
        h.addLayout(_pair("RX BW", self.rx_bw_combo))

        # Lock button sits between RX and TX BW pairs — no label of its
        # own; the link-icon glyph + tooltip carries the meaning.
        self.lock_btn = QPushButton("🔗")
        self.lock_btn.setCheckable(True)
        self.lock_btn.setFixedWidth(32)
        self.lock_btn.setToolTip("Lock TX BW to RX BW")
        self.lock_btn.toggled.connect(self.radio.set_bw_lock)
        h.addWidget(self.lock_btn)

        self.tx_bw_combo = QComboBox()
        self.tx_bw_combo.setFixedWidth(80)
        self.tx_bw_combo.currentIndexChanged.connect(self._on_tx_bw_changed)
        h.addLayout(_pair("TX BW", self.tx_bw_combo))

        # CW pitch — operator-adjustable audio tone for CW modes.
        # Hidden outside CWU/CWL since it has no meaning there. Range
        # 200..1500 Hz covers operator preference (low-pitch fans tune
        # ~400, contesters often run 600-700, some prefer 800+).
        self.cw_pitch_label = QLabel("CW Pitch")
        self.cw_pitch_spin = QSpinBox()
        self.cw_pitch_spin.setRange(200, 1500)
        self.cw_pitch_spin.setSingleStep(10)
        self.cw_pitch_spin.setSuffix(" Hz")
        self.cw_pitch_spin.setFixedWidth(95)
        self.cw_pitch_spin.setValue(int(radio.cw_pitch_hz))
        self.cw_pitch_spin.setToolTip(
            "Audio tone heard for tuned CW signals. Click-to-tune places "
            "the marker this many Hz away from the signal so it lands "
            "inside the filter at the chosen pitch."
        )
        self.cw_pitch_spin.valueChanged.connect(self.radio.set_cw_pitch_hz)
        h.addWidget(self.cw_pitch_label)
        h.addWidget(self.cw_pitch_spin)

        h.addStretch(1)
        self.content_layout().addLayout(h)

        self._refresh_bw_combos()
        self._update_cw_pitch_visibility()

        radio.mode_changed.connect(self._on_mode_changed)
        radio.rate_changed.connect(self._on_rate_changed)
        radio.rx_bw_changed.connect(self._on_radio_rx_bw_changed)
        radio.tx_bw_changed.connect(self._on_radio_tx_bw_changed)
        radio.bw_lock_changed.connect(self.lock_btn.setChecked)
        # Keep the spinner in sync if pitch changes elsewhere (e.g.
        # the Settings → DSP duplicate of the same control).
        radio.cw_pitch_changed.connect(self._on_radio_cw_pitch_changed)

    @staticmethod
    def _select_combo_data(combo: QComboBox, value):
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    @staticmethod
    def _ensure_bw_value(combo: QComboBox, value: int):
        """Select `value` in a BW combo. If it matches a preset, just
        select that preset. If not — for example after the operator
        dragged the spectrum-filter edge to a non-preset value like
        5.2 kHz — insert a '(custom)' entry at the top of the dropdown
        and select it, so the combo accurately reflects the actual
        bandwidth instead of silently lying about it.

        Any prior '(custom)' entry is removed first so repeated drags
        don't accumulate stale entries. When the operator subsequently
        picks a real preset, the next call here strips the custom
        entry and selects the preset normally."""
        # Strip prior custom entries to avoid accumulation.
        for i in range(combo.count() - 1, -1, -1):
            if "(custom)" in combo.itemText(i):
                combo.removeItem(i)
        # Try to match a preset.
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return
        # Not a preset — show as a custom entry at the top.
        label = (f"{value/1000:.1f} k (custom)" if value >= 1000
                 else f"{value} Hz (custom)")
        combo.insertItem(0, label, value)
        combo.setCurrentIndex(0)

    def _refresh_bw_combos(self):
        mode = self.radio.mode
        presets = Radio.BW_PRESETS.get(mode, [2400])
        rx_bw = self.radio.rx_bw_for(mode)
        tx_bw = self.radio.tx_bw_for(mode)
        for combo, val in ((self.rx_bw_combo, rx_bw), (self.tx_bw_combo, tx_bw)):
            combo.blockSignals(True)
            combo.clear()
            for hz in presets:
                label = f"{hz/1000:.1f} k" if hz >= 1000 else f"{hz} Hz"
                combo.addItem(label, hz)
            # Use _ensure_bw_value so a non-preset BW (e.g. dragged
            # from the spectrum filter edge) shows up as "(custom)"
            # rather than the combo silently mismatching the radio.
            self._ensure_bw_value(combo, val)
            combo.blockSignals(False)

    def _on_rx_bw_changed(self, _idx):
        data = self.rx_bw_combo.currentData()
        if data is not None:
            self.radio.set_rx_bw(self.radio.mode, int(data))

    def _on_tx_bw_changed(self, _idx):
        data = self.tx_bw_combo.currentData()
        if data is not None:
            self.radio.set_tx_bw(self.radio.mode, int(data))

    def _on_mode_changed(self, mode: str):
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText(mode)
        self.mode_combo.blockSignals(False)
        self._refresh_bw_combos()
        self._update_cw_pitch_visibility()

    def _update_cw_pitch_visibility(self):
        is_cw = self.radio.mode in ("CWU", "CWL")
        self.cw_pitch_label.setVisible(is_cw)
        self.cw_pitch_spin.setVisible(is_cw)

    def _on_radio_cw_pitch_changed(self, pitch_hz: int):
        # Keep our spinner in sync when the pitch is changed from
        # another UI surface (Settings → DSP). Block signals to avoid
        # a feedback loop back into radio.set_cw_pitch_hz.
        self.cw_pitch_spin.blockSignals(True)
        self.cw_pitch_spin.setValue(int(pitch_hz))
        self.cw_pitch_spin.blockSignals(False)

    def _on_rate_changed(self, rate: int):
        self.rate_combo.blockSignals(True)
        self._select_combo_data(self.rate_combo, rate)
        self.rate_combo.blockSignals(False)

    def _on_radio_rx_bw_changed(self, mode: str, bw: int):
        if mode == self.radio.mode:
            self.rx_bw_combo.blockSignals(True)
            self._ensure_bw_value(self.rx_bw_combo, bw)
            self.rx_bw_combo.blockSignals(False)

    def _on_radio_tx_bw_changed(self, mode: str, bw: int):
        if mode == self.radio.mode:
            self.tx_bw_combo.blockSignals(True)
            self._ensure_bw_value(self.tx_bw_combo, bw)
            self.tx_bw_combo.blockSignals(False)


# ── View / Zoom / Rates ────────────────────────────────────────────────
class ViewPanel(GlassPanel):
    """Live panadapter controls — zoom, spectrum FPS, waterfall rate.

    Thin single-row panel meant to sit next to MODE + FILTER. All three
    controls also live in Settings → Visuals (so power users can
    fine-tune via sliders) but the operator wants them one click away
    during a QSO / DX chase without having to open and close Settings.

    Two-way wired to Radio: changes from here propagate to Radio (and
    therefore to the painted widgets), and Radio-side changes (e.g.
    mouse-wheel zoom on the spectrum) flow back here to keep the combo /
    sliders in sync.
    """

    def __init__(self, radio: Radio, parent=None):
        # Panel header reads "DISPLAY" rather than "VIEW" — the latter
        # was confusing operators because it collides with the menu
        # bar's "View" menu (panel toggles, layout reset, etc.). The
        # internal class name stays ViewPanel and the QSettings dock
        # key stays "view" so existing saved layouts keep working.
        super().__init__("DISPLAY", parent, help_topic="spectrum")
        self.radio = radio

        h = QHBoxLayout()
        h.setSpacing(6)

        # Zoom combo — same preset levels as Settings + mouse wheel.
        # Pairs with a fine-zoom slider to its right: combo for fast
        # preset jumps (1× / 2× / 4× / 8× / 16×), slider for in-between
        # values (e.g. 1.5×, 2.5×, 3.7×) when the operator wants to
        # fine-tune the panadapter span without snapping to a preset.
        h.addWidget(QLabel("Zoom"))
        self.zoom_combo = QComboBox()
        for lvl in Radio.ZOOM_LEVELS:
            self.zoom_combo.addItem(f"{lvl:g}x", float(lvl))
        self._sync_zoom_combo(radio.zoom)
        self.zoom_combo.setFixedWidth(64)
        self.zoom_combo.setToolTip(
            "Panadapter zoom presets (1× / 2× / 4× / 8× / 16×).\n"
            "Mouse-wheel on empty spectrum steps through these.\n"
            "For in-between values, use the slider to the right.")
        self.zoom_combo.currentIndexChanged.connect(self._on_zoom_pick)
        h.addWidget(self.zoom_combo)

        # Fine-zoom slider — linear 1.0× .. 16.0× in 0.1× ticks.
        # Internal slider int = zoom × 10 so we don't need a custom
        # double-slider widget. Same ZOOM_MIN..MAX bounds as the
        # combo's first/last preset, so anything reachable here is
        # also a valid Radio.set_zoom() value.
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setObjectName("zoom_slider")
        self.zoom_slider.setRange(10, 160)         # 1.0x .. 16.0x
        self.zoom_slider.setSingleStep(1)          # 0.1x per arrow tick
        self.zoom_slider.setPageStep(5)            # 0.5x per PgUp/PgDn
        self.zoom_slider.setValue(int(round(radio.zoom * 10)))
        self.zoom_slider.setFixedWidth(110)
        self.zoom_slider.setToolTip(
            "Fine zoom — drag for any value between 1.0× and 16.0×\n"
            "in 0.1× steps. Useful when a preset is too coarse\n"
            "(e.g. 1.5× to fit a SSB QSO without overshooting to 2×,\n"
            "or 3× to span a CW pile-up).\n\n"
            "The combo on the left snaps to the standard presets;\n"
            "this slider freely rides between them. Either control\n"
            "drives the same Radio.zoom — mouse-wheel on the\n"
            "spectrum still uses preset steps.")
        # Same press/release pattern as the FPS slider — committing
        # zoom on every pixel of drag was DESTROYING the waterfall
        # display. WaterfallWidget reallocates its scroll buffer to
        # all-zero whenever the bin count changes, and zoom changes
        # the bin count (keep = fft_size/zoom). Per-pixel commits =
        # hundreds of full waterfall buffer wipes during a drag.
        # Now: commit only on release (or via debounce for click /
        # arrow-key changes).
        self._zoom_dragging = False
        # _QTimer is used by both the zoom and FPS sliders below.
        # Imported here (the earlier of the two construction sites)
        # so both can reference it.
        from PySide6.QtCore import QTimer as _QTimer
        self._zoom_debounce = _QTimer(self)
        self._zoom_debounce.setSingleShot(True)
        self._zoom_debounce.setInterval(75)
        self._zoom_debounce.timeout.connect(self._commit_zoom_value)
        self.zoom_slider.sliderPressed.connect(self._on_zoom_slider_press)
        self.zoom_slider.sliderReleased.connect(self._on_zoom_slider_release)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        h.addWidget(self.zoom_slider)

        # Live readout next to the slider — "1.7x" — so the operator
        # always sees the current value without having to read pixel
        # positions. Same monospace styling as other live readouts on
        # this row.
        self.zoom_label = QLabel(f"{radio.zoom:.1f}x")
        self.zoom_label.setFixedWidth(40)
        self.zoom_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-weight: 700;")
        h.addWidget(self.zoom_label)

        # Spectrum rate — compact slider only; live value is in the
        # tooltip on hover. Operator wanted a thin panel with no
        # redundant numeric readouts.
        h.addSpacing(10)
        h.addWidget(QLabel("Spec"))
        self.fps_slider = SteppedSlider(Qt.Horizontal)
        self.fps_slider.setObjectName("fps_slider")
        # Step-list slider — each detent is a useful FPS value the
        # operator might actually pick (5, 10, 15, 20, 25, 30, 40, 50,
        # 60, 75, 90, 120). Linear-from-5-to-120 used to feel "wild
        # at top, hard to land low" because human perception of fps
        # is logarithmic-ish. See SPECTRUM_FPS_STEPS at module top.
        self.fps_slider.setRange(0, len(SPECTRUM_FPS_STEPS) - 1)
        self.fps_slider.setValue(fps_to_slider_position(radio.spectrum_fps))
        self.fps_slider.setFixedWidth(130)
        # Visible ticks + 1-per-step page/single moves so the operator
        # both SEES and FEELS the discrete detents. Without these, the
        # slider snaps to integer positions but visually looks smooth.
        self.fps_slider.setTickPosition(QSlider.TicksBelow)
        self.fps_slider.setTickInterval(1)
        self.fps_slider.setSingleStep(1)
        self.fps_slider.setPageStep(1)
        self._refresh_fps_tooltip(radio.spectrum_fps)
        # FPS slider commit policy:
        #   - while mouse is held (drag): just refresh tooltip, NO radio update
        #   - on mouse release: commit immediately
        #   - click-jump / keyboard / programmatic setValue: commit through
        #     a 75 ms debounce (since no press/release events fire for those)
        # The earlier debounce-only pattern was less responsive than expected
        # — operator dragged the slider and didn't see the spectrum change
        # until 75 ms after the last move. The press/release pattern commits
        # the moment the operator lets go, which feels instant.
        from PySide6.QtCore import QTimer as _QTimer
        self._fps_dragging = False
        self._fps_debounce = _QTimer(self)
        self._fps_debounce.setSingleShot(True)
        self._fps_debounce.setInterval(75)
        self._fps_debounce.timeout.connect(self._commit_fps_value)
        self.fps_slider.sliderPressed.connect(self._on_fps_slider_press)
        self.fps_slider.sliderReleased.connect(self._on_fps_slider_release)
        self.fps_slider.valueChanged.connect(self._on_fps_slider_drag)
        h.addWidget(self.fps_slider)

        # Waterfall rate — step-list slider covering multiplier (fast)
        # and divider (slow) in one control. See WATERFALL_SPEED_STEPS
        # at module top for the full list. Inverted so RIGHT = faster.
        # Index 8 = neutral (1 row per FFT). Fast end goes up to 30×
        # multiplier (linearly interpolated rows from the previous FFT
        # — no extra CPU cost, just visual scroll speed).
        h.addSpacing(10)
        h.addWidget(QLabel("WF"))
        self.wf_slider = SteppedSlider(Qt.Horizontal)
        self.wf_slider.setObjectName("wf_slider")
        self.wf_slider.setRange(0, len(WATERFALL_SPEED_STEPS) - 1)
        self.wf_slider.setInvertedAppearance(True)   # right = faster
        self.wf_slider.setValue(wf_to_slider_position(
            radio.waterfall_divider, radio.waterfall_multiplier))
        self.wf_slider.setFixedWidth(140)
        # Visible ticks per detent — see fps_slider above for rationale.
        self.wf_slider.setTickPosition(QSlider.TicksBelow)
        self.wf_slider.setTickInterval(1)
        self.wf_slider.setSingleStep(1)
        self.wf_slider.setPageStep(1)
        self._refresh_wf_tooltip()
        # Debounce — works fine for the WF slider (operator confirmed)
        self._wf_debounce = _QTimer(self)
        self._wf_debounce.setSingleShot(True)
        self._wf_debounce.setInterval(75)
        self._wf_debounce.timeout.connect(self._commit_wf_value)
        self.wf_slider.valueChanged.connect(self._on_wf_slider_drag)
        h.addWidget(self.wf_slider)

        h.addStretch(1)
        self.content_layout().addLayout(h)

        # Two-way sync — Radio emits on zoom wheel / TCI / QSettings
        radio.zoom_changed.connect(self._on_radio_zoom_changed)
        radio.spectrum_fps_changed.connect(self._on_radio_fps_changed)
        radio.waterfall_divider_changed.connect(self._on_radio_wf_state_changed)
        radio.waterfall_multiplier_changed.connect(self._on_radio_wf_state_changed)

    # ── helpers ──────────────────────────────────────────────────
    def _sync_zoom_combo(self, zoom: float):
        for i in range(self.zoom_combo.count()):
            if abs(self.zoom_combo.itemData(i) - zoom) < 1e-6:
                if self.zoom_combo.currentIndex() != i:
                    self.zoom_combo.blockSignals(True)
                    self.zoom_combo.setCurrentIndex(i)
                    self.zoom_combo.blockSignals(False)
                return

    # Backward-compat shims. The waterfall slider encoding now lives
    # at module scope (WATERFALL_SPEED_STEPS + wf_*_slider_position
    # helpers) so the Settings dialog's slider can share it. These
    # static methods stay so any existing call sites keep working.
    @staticmethod
    def _wf_slider_to_state(v: int) -> tuple[int, int]:
        return wf_from_slider_position(v)

    @staticmethod
    def _wf_state_to_slider(divider: int, multiplier: int) -> int:
        return wf_to_slider_position(divider, multiplier)

    def _rows_per_sec(self) -> float:
        fps = self.radio.spectrum_fps
        div = max(1, self.radio.waterfall_divider)
        mult = max(1, self.radio.waterfall_multiplier)
        return fps * mult / div

    def _refresh_fps_tooltip(self, fps: int):
        # Smoothing tip when fps is high — Apache Labs guidance is that
        # 60 Hz looks best with averaging on, otherwise frame-to-frame
        # jitter is more visible at higher rates.
        tip = (f"Spectrum refresh rate — {fps} fps. Lower = less CPU / "
               "GPU load. Higher = smoother but more work.")
        if fps >= 50:
            tip += ("\n\nTip: at high FPS, enable Settings → Display → "
                    "'Smooth spectrum trace' for cleanest look.")
        self.fps_slider.setToolTip(tip)

    def _refresh_wf_tooltip(self):
        rps = self._rows_per_sec()
        mult = self.radio.waterfall_multiplier
        div = self.radio.waterfall_divider
        extra = ""
        if mult > 1:
            extra = f"  (fast mode: {mult}× row interpolation)"
        elif div > 1:
            extra = f"  (1 row per {div} FFT ticks)"
        self.wf_slider.setToolTip(
            f"Waterfall scroll rate — {rps:.1f} rows/sec{extra}. "
            "Right = faster scroll (up to 30× at the fast end, "
            "useful for digital-mode hunting at low spec rates), "
            "left = slow crawl with more time-history visible.")

    # ── user-driven ──────────────────────────────────────────────
    def _on_zoom_pick(self, _idx: int):
        self.radio.set_zoom(float(self.zoom_combo.currentData()))

    def _on_zoom_slider_press(self):
        """Mouse-down on zoom slider — drag begins."""
        self._zoom_dragging = True
        self._zoom_debounce.stop()

    def _on_zoom_slider_release(self):
        """Mouse-up — drag complete. Commit immediately."""
        self._zoom_dragging = False
        self._zoom_debounce.stop()
        self._commit_zoom_value()

    def _on_zoom_slider(self, v: int):
        """valueChanged — drag-aware. While the operator is actively
        dragging, only the live label updates; radio is left alone so
        the waterfall buffer doesn't get wiped on every pixel of
        motion. Click-jumps and keyboard changes go through the 75 ms
        debounce path."""
        zoom = max(1.0, min(16.0, v / 10.0))
        self.zoom_label.setText(f"{zoom:.1f}x")
        if self._zoom_dragging:
            return
        self._zoom_debounce.start()

    def _commit_zoom_value(self):
        v = self.zoom_slider.value()
        zoom = max(1.0, min(16.0, v / 10.0))
        # Snap to a preset when the slider lands within ±0.05× of one
        # so the combo + slider feel coupled (otherwise the combo
        # caption stays "1x" while the slider sits at 1.7x and the
        # operator wonders which value is authoritative).
        for preset in Radio.ZOOM_LEVELS:
            if abs(zoom - preset) <= 0.05:
                zoom = preset
                break
        self.radio.set_zoom(zoom)

    # ── Debounced slider commit ─────────────────────────────────────
    # valueChanged → just refresh the tooltip (cheap) and (re)start
    # the 75 ms one-shot debounce. The radio doesn't see the new value
    # until the slider has been quiet for 75 ms, so a drag that fires
    # 200 valueChanged events results in ONE radio update, not 200.
    # Mouse release naturally triggers the final commit because no
    # more valueChanged events arrive after release.
    def _on_fps_slider_press(self):
        """Mouse-down on the FPS slider — drag begins."""
        self._fps_dragging = True
        self._fps_debounce.stop()

    def _on_fps_slider_release(self):
        """Mouse-up — drag complete. Commit the final value RIGHT NOW
        (no debounce wait) so the operator sees the spectrum trace
        update the instant they let go."""
        self._fps_dragging = False
        self._fps_debounce.stop()
        self._commit_fps_value()

    def _on_fps_slider_drag(self, slider_pos: int):
        """valueChanged fires constantly during drag AND for click-
        jumps / keyboard / programmatic setValue. The slider position
        is an index into SPECTRUM_FPS_STEPS — convert to fps for the
        tooltip. While the mouse is actively held, only the tooltip
        updates — radio is left alone so the FFT timer's setInterval
        isn't hammered. Non-drag changes (no preceding sliderPressed)
        fall through to the 75 ms debounce path."""
        fps = fps_from_slider_position(slider_pos)
        self._refresh_fps_tooltip(fps)
        self._refresh_wf_tooltip()
        if self._fps_dragging:
            return
        self._fps_debounce.start()

    def _commit_fps_value(self):
        fps = fps_from_slider_position(self.fps_slider.value())
        self.radio.set_spectrum_fps(fps)

    def _on_wf_slider_drag(self, _v: int):
        """Drag → refresh tooltip + bump debounce timer. Radio commits
        only after 75 ms of quiet."""
        self._refresh_wf_tooltip()
        self._wf_debounce.start()

    def _commit_wf_value(self):
        div, mult = self._wf_slider_to_state(self.wf_slider.value())
        self.radio.set_waterfall_divider(div)
        self.radio.set_waterfall_multiplier(mult)

    # Backward-compat aliases (in case anything else calls these by
    # the old names).
    def _on_fps_changed(self, fps: int):
        self.radio.set_spectrum_fps(fps)
        self._refresh_fps_tooltip(fps)
        self._refresh_wf_tooltip()

    def _on_wf_changed(self, v: int):
        div, mult = self._wf_slider_to_state(v)
        self.radio.set_waterfall_divider(div)
        self.radio.set_waterfall_multiplier(mult)
        self._refresh_wf_tooltip()

    # ── radio-driven (e.g. wheel-zoom, Visuals tab slider) ───────
    def _on_radio_zoom_changed(self, zoom: float):
        self._sync_zoom_combo(zoom)
        # Keep the fine-zoom slider + label in sync without firing
        # our own valueChanged handler (would loop back into Radio).
        target = int(round(zoom * 10))
        if self.zoom_slider.value() != target:
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.setValue(target)
            self.zoom_slider.blockSignals(False)
        self.zoom_label.setText(f"{zoom:.1f}x")

    def _on_radio_fps_changed(self, fps: int):
        # Radio holds an arbitrary FPS int; slider operates on step
        # indices. Snap to nearest step for the slider position.
        target_pos = fps_to_slider_position(fps)
        if self.fps_slider.value() != target_pos:
            self.fps_slider.blockSignals(True)
            self.fps_slider.setValue(target_pos)
            self.fps_slider.blockSignals(False)
        self._refresh_fps_tooltip(fps)
        self._refresh_wf_tooltip()

    def _on_radio_wf_state_changed(self, _=None):
        target = self._wf_state_to_slider(
            self.radio.waterfall_divider, self.radio.waterfall_multiplier)
        if self.wf_slider.value() != target:
            self.wf_slider.blockSignals(True)
            self.wf_slider.setValue(target)
            self.wf_slider.blockSignals(False)
        self._refresh_wf_tooltip()


# ── Gain (LNA + Volume) ─────────────────────────────────────────────────
class GainPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("GAIN", parent, help_topic="getting-started")
        self.radio = radio

        h = QHBoxLayout()

        h.addWidget(QLabel("LNA"))
        self.lna_slider = QSlider(Qt.Horizontal)
        self.lna_slider.setObjectName("gain_slider")   # amber handle
        # Range matches Radio.LNA_MIN_DB/MAX_DB — HL2 AD9866 PGA is
        # effective only up to +31 dB; values 32-48 add no gain and
        # can cause IMD into the ADC.
        self.lna_slider.setRange(Radio.LNA_MIN_DB, Radio.LNA_MAX_DB)
        self.lna_slider.setValue(radio.gain_db)
        self.lna_slider.setFixedWidth(160)
        self.lna_slider.valueChanged.connect(self.radio.set_gain_db)
        h.addWidget(self.lna_slider)

        self.lna_label = QLabel(f"{radio.gain_db:+d} dB")
        self.lna_label.setFixedWidth(60)
        h.addWidget(self.lna_label)

        h.addSpacing(14)

        h.addWidget(QLabel("Vol"))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setObjectName("vol_slider")    # green handle
        self.vol_slider.setRange(0, 300)
        self.vol_slider.setValue(int(radio.volume * 100))
        self.vol_slider.setFixedWidth(120)
        self.vol_slider.valueChanged.connect(
            lambda v: self.radio.set_volume(v / 100.0))
        h.addWidget(self.vol_slider)

        self.vol_label = QLabel(f"{int(radio.volume*100)}%")
        self.vol_label.setFixedWidth(50)
        h.addWidget(self.vol_label)

        self.content_layout().addLayout(h)

        radio.gain_changed.connect(self._on_gain_changed)
        radio.volume_changed.connect(self._on_volume_changed)

    # Perceptual volume curve — 0..100 slider → 0..VOL_MAX multiplier
    # via a power curve, so each slider tick yields a roughly equal
    # loudness step. Human hearing is logarithmic — a linear slider
    # feels wildly touchy at low volumes.
    #
    # Since the AF Gain split (2026-04-24, Option B), Volume is
    # purely the FINAL OUTPUT TRIM stage. The makeup gain that was
    # previously squeezed into Volume's 50× headroom now lives in a
    # separate AF Gain dB slider, leaving Volume as a clean 0..1.0
    # (unity-at-max) trim — the role it always should have had.
    VOL_MAX = 1.0
    VOL_GAMMA = 2.0

    @classmethod
    def _slider_to_volume(cls, s: int) -> float:
        frac = max(0, min(100, int(s))) / 100.0
        return (frac ** cls.VOL_GAMMA) * cls.VOL_MAX

    @classmethod
    def _volume_to_slider(cls, v: float) -> int:
        v = max(0.0, min(cls.VOL_MAX, float(v)))
        frac = (v / cls.VOL_MAX) ** (1.0 / cls.VOL_GAMMA)
        return int(round(frac * 100))

    def _on_vol_slider(self, slider_val: int):
        """User dragged the slider → apply perceptual curve → Radio."""
        self.vol_label.setText(f"{slider_val}%")
        self.radio.set_volume(self._slider_to_volume(slider_val))

    def _on_gain_changed(self, db: int):
        self.lna_label.setText(f"{db:+d} dB")
        if self.lna_slider.value() != db:
            self.lna_slider.blockSignals(True)
            self.lna_slider.setValue(db)
            self.lna_slider.blockSignals(False)

    def _on_volume_changed(self, v: float):
        """Radio volume changed elsewhere — convert multiplier back
        to slider position via inverse curve and update UI."""
        target = self._volume_to_slider(v)
        self.vol_label.setText(f"{target}%")
        if self.vol_slider.value() != target:
            self.vol_slider.blockSignals(True)
            self.vol_slider.setValue(target)
            self.vol_slider.blockSignals(False)


# ── DSP / Notch / Audio output ──────────────────────────────────────────
class DspPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("DSP + AUDIO", parent, help_topic="agc")
        self.radio = radio

        # ── Row 1 — LEVELS (LNA + Volume) ───────────────────────────
        # Consolidated from the former separate GainPanel. LNA gain
        # and post-demod volume are the two "amount of signal" knobs
        # an operator touches constantly, and they belong next to the
        # AGC readout that drives them.
        levels = QHBoxLayout()
        levels.addWidget(QLabel("LNA"))
        self.lna_slider = QSlider(Qt.Horizontal)
        self.lna_slider.setObjectName("gain_slider")
        # Range matches Radio.LNA_MIN_DB/MAX_DB — HL2 AD9866 PGA is
        # effective only up to +31 dB; values 32-48 add no gain and
        # can cause IMD into the ADC.
        self.lna_slider.setRange(Radio.LNA_MIN_DB, Radio.LNA_MAX_DB)
        self.lna_slider.setValue(radio.gain_db)
        self.lna_slider.setFixedWidth(180)
        # Tick marks at the zone boundaries so the operator can see
        # at a glance where "sweet spot" ends and "high-gain / IMD
        # risk" begins. Combined with the per-zone color on the LNA
        # value label below, the slider becomes self-documenting.
        self.lna_slider.setTickPosition(QSlider.TicksBelow)
        self.lna_slider.setTickInterval(10)
        self.lna_slider.setToolTip(
            "LNA — RF input gain on the HL2's AD9866 PGA.\n\n"
            "Linearity zones (the LNA dB readout is colored):\n"
            "  GREEN   −12 .. +20 dB   sweet spot — clean, low IMD\n"
            "  YELLOW  +20 .. +28 dB   high gain — fine on quiet bands\n"
            "  ORANGE  +28 .. +31 dB   IMD risk — only for very weak\n"
            "                          signals on otherwise quiet bands\n"
            "                          where you really need every dB\n\n"
            "Above +31 dB the AD9866 PGA stops giving real gain and\n"
            "drives the ADC into compression — Lyra hard-caps the\n"
            "slider at +31 to prevent that.")
        self.lna_slider.valueChanged.connect(self.radio.set_gain_db)
        levels.addWidget(self.lna_slider)
        self.lna_label = QLabel(f"{radio.gain_db:+d} dB")
        self.lna_label.setFixedWidth(60)
        # Initial color zone (green/yellow/orange depending on the
        # restored gain). Refreshed in _on_gain_changed on every
        # gain change — manual or Auto-LNA.
        self._refresh_lna_label_color(radio.gain_db)
        levels.addWidget(self.lna_label)

        # Auto-LNA toggle. Behavior is BACK-OFF-ONLY: when an
        # incoming signal pushes the ADC peak above ~-10 dBFS, the
        # loop drops gain by 2-3 dB to leave headroom. It does NOT
        # raise gain on its own — the operator sets a baseline and
        # Auto only protects against transient overload.
        self.auto_lna_btn = QPushButton("Auto")
        self.auto_lna_btn.setObjectName("dsp_btn")    # orange when on
        self.auto_lna_btn.setCheckable(True)
        self.auto_lna_btn.setFixedWidth(50)
        self.auto_lna_btn.setChecked(radio.lna_auto)
        self.auto_lna_btn.setToolTip(
            "Auto-LNA — overload protection (back-off only).\n\n"
            "When ON, Lyra drops LNA gain when the ADC peak exceeds\n"
            "  > -3 dBFS  → drop 3 dB (urgent, near clipping)\n"
            "  > -10 dBFS → drop 2 dB (hot, leave margin)\n\n"
            "It does NOT raise gain — that's deliberate. Set your\n"
            "baseline LNA manually for the band you're on; Auto only\n"
            "kicks in when a strong signal threatens to overload the\n"
            "ADC. If you've never seen Auto fire, your antenna isn't\n"
            "delivering signals strong enough to need it (which is\n"
            "the common-case in normal HF conditions).")
        self.auto_lna_btn.toggled.connect(self.radio.set_lna_auto)
        levels.addWidget(self.auto_lna_btn)

        # "Last Auto-LNA event" badge — shows the most recent
        # back-off Auto applied (e.g. "↓2 dB 14:23:01") so the
        # operator can see Auto IS working, even if the event is
        # transient. Empty until Auto first fires; cleared when Auto
        # is toggled off.
        self.lna_auto_event_lbl = QLabel("")
        self.lna_auto_event_lbl.setFixedWidth(120)
        self.lna_auto_event_lbl.setStyleSheet(
            "color: #ffab47; font-family: Consolas, monospace; "
            "font-size: 10px;")
        self.lna_auto_event_lbl.setToolTip(
            "Most recent Auto-LNA back-off event. Updates whenever "
            "Auto drops gain; persists between events so you can see "
            "what Auto last did.")
        levels.addWidget(self.lna_auto_event_lbl)
        # Subscribe to Radio's new lna_auto_event signal (added below).
        radio.lna_auto_event.connect(self._on_lna_auto_event)
        # Brief slider flash after an Auto event — handled by a
        # one-shot QTimer that resets the slider's stylesheet.
        self._lna_flash_timer = QTimer(self)
        self._lna_flash_timer.setSingleShot(True)
        self._lna_flash_timer.timeout.connect(self._clear_lna_flash)

        levels.addSpacing(20)

        # AF Gain slider — makeup gain in dB (0..+80), LINEAR (1 tick
        # = 1 dB). Sits BETWEEN AGC and Volume in the signal path:
        #     demod → AGC → AF Gain → Volume → tanh → sink
        # Designed for AGC-off operation (digital modes, contesters,
        # monitoring) where AGC isn't available to bring the signal
        # up to listenable level. Set once per station based on your
        # typical antenna/band level, then forget — Volume rides on
        # moment-to-moment listening comfort.
        #
        # Range goes to +80 dB so AGC-off operation has roughly the
        # same makeup-gain headroom that AGC-on gets via the AGC
        # stage's internal +60 dB max gain. With the previous +50
        # dB cap, AGC OFF on weak signals could be ~30 dB quieter
        # than AGC ON even with everything maxed.
        #
        # Linear dB mapping (not perceptual curve) because makeup
        # gain is naturally thought of in dB by operators: "this band
        # needs another 15 dB" is a concrete adjustment.
        levels.addWidget(QLabel("AF"))
        self.af_gain_slider = QSlider(Qt.Horizontal)
        self.af_gain_slider.setObjectName("af_gain_slider")
        self.af_gain_slider.setRange(0, 80)
        self.af_gain_slider.setSingleStep(1)
        self.af_gain_slider.setPageStep(5)
        self.af_gain_slider.setValue(int(radio.af_gain_db))
        self.af_gain_slider.setFixedWidth(120)
        self.af_gain_slider.setToolTip(
            "AF Gain — post-demod makeup gain, 0 to +80 dB.\n\n"
            "Use this when AGC is off (digital modes) or the AGC "
            "target is too quiet for weak signals. Set once for "
            "your station's typical signal level, then ride Volume "
            "for moment-to-moment listening comfort.\n\n"
            "The +80 dB ceiling matches the headroom AGC ON gets "
            "via its internal automatic gain stage. Most operators "
            "land in the +20..+50 dB zone; the upper range is for "
            "running AGC off on weak signals.\n\n"
            "The tanh limiter after this stage prevents clipping "
            "at the sink, so you can't damage speakers with high "
            "AF Gain settings — the worst case is soft saturation.")
        self.af_gain_slider.valueChanged.connect(self.radio.set_af_gain_db)
        levels.addWidget(self.af_gain_slider)
        self.af_gain_label = QLabel(f"+{int(radio.af_gain_db)} dB")
        self.af_gain_label.setFixedWidth(50)
        levels.addWidget(self.af_gain_label)

        levels.addSpacing(12)

        # Volume slider uses a PERCEPTUAL (quadratic) curve so each
        # 1% tick produces a roughly uniform loudness change. With a
        # linear slider → linear multiplier mapping the bottom end of
        # the slider was unusably sensitive (1% tick = 2x perceptual
        # loudness at low volumes), which is why we route through a
        # curve here rather than calling set_volume(slider/100) directly.
        #
        #   slider 0..100 → multiplier = (slider/100) ** 2 * VOL_MAX
        #   VOL_MAX = 1.0  (Volume is now a pure output trim — makeup
        #   gain lives in the AF Gain slider to the left.)
        #   At slider=100 → ×1.0   (unity — full AF-gained signal)
        #   At slider= 71 → ×0.5   (−6 dB)
        #   At slider= 50 → ×0.25  (−12 dB — traditional "half")
        #   At slider= 25 → ×0.0625(−24 dB — quiet listening)
        #   At slider= 10 → ×0.01  (−40 dB — background)
        levels.addWidget(QLabel("Vol"))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setObjectName("vol_slider")
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setSingleStep(1)
        self.vol_slider.setPageStep(5)
        self.vol_slider.setToolTip(
            "Output volume. Slider uses a perceptual curve — each tick "
            "yields a roughly equal loudness step. ~71% = unity gain.")
        self.vol_slider.setValue(self._volume_to_slider(radio.volume))
        self.vol_slider.setFixedWidth(160)
        self.vol_slider.valueChanged.connect(self._on_vol_slider)
        levels.addWidget(self.vol_slider)
        self.vol_label = QLabel(f"{self._volume_to_slider(radio.volume)}%")
        self.vol_label.setFixedWidth(50)
        levels.addWidget(self.vol_label)

        levels.addSpacing(12)

        # Balance slider — stereo pan from full-left to full-right.
        # Slider range is -100..+100 (center 0) so 1 tick = 1% pan
        # offset, with a reset-to-center via double-click.
        # Equal-power pan law lives in Radio.balance_lr_gains so the
        # perceived loudness stays constant as the operator sweeps
        # the pan across center.
        # FUTURE: when RX2 / Split arrive, this becomes the RX1
        # balance and a second slider (and a routing-mode picker)
        # joins it for RX2.
        levels.addWidget(QLabel("Bal"))
        self.bal_slider = QSlider(Qt.Horizontal)
        self.bal_slider.setObjectName("bal_slider")
        self.bal_slider.setRange(-100, 100)
        self.bal_slider.setSingleStep(1)
        self.bal_slider.setPageStep(10)
        self.bal_slider.setValue(int(round(radio.balance * 100)))
        self.bal_slider.setFixedWidth(120)
        # Visible tick marks under the slider so the operator can see
        # where center is at a glance — interval 50 gives ticks at
        # L100, L50, C, R50, R100. Combined with the snap-deadzone in
        # _on_bal_slider, sweeping through center "clicks" into true
        # zero and the label shows "C" so there's tactile + visual +
        # textual confirmation the audio is mono-balanced.
        self.bal_slider.setTickPosition(QSlider.TicksBelow)
        self.bal_slider.setTickInterval(50)
        self.bal_slider.setToolTip(
            "Stereo balance — pan the audio between left and right.\n"
            "Center = both ears equal (label reads 'C').\n\n"
            "Tick marks: L100 / L50 / Center / R50 / R100.\n"
            "Sweeping near center auto-snaps to true zero (±3% deadzone)\n"
            "so the slider 'clicks into' mono without you having to aim.\n\n"
            "Double-click anywhere on the slider to instantly recenter.\n\n"
            "Useful for DX-split listening (when RX2 ships) and for A/B\n"
            "against a noise source in one channel.")
        self.bal_slider.valueChanged.connect(self._on_bal_slider)
        # Double-click recenters — kept as the precise gesture even
        # though the snap deadzone makes it usually unnecessary.
        self.bal_slider.mouseDoubleClickEvent = (
            lambda _e: self.bal_slider.setValue(0))
        levels.addWidget(self.bal_slider)
        self.bal_label = QLabel(self._format_bal(radio.balance))
        self.bal_label.setFixedWidth(40)
        # Click the "C / L37 / R12" label to recenter — third
        # discoverable gesture for getting back to mono.
        self.bal_label.setCursor(Qt.PointingHandCursor)
        self.bal_label.setToolTip("Click to recenter balance to mono.")
        self.bal_label.mousePressEvent = (
            lambda _e: self.bal_slider.setValue(0))
        levels.addWidget(self.bal_label)

        # Sync from Radio side too (e.g. QSettings load, future TCI)
        radio.balance_changed.connect(self._on_radio_balance_changed)

        levels.addSpacing(12)

        # Audio output destination — moved to the levels row as part of
        # the Option A consolidation so the entire audio chain
        # (LNA → AF → Vol → Bal → Out) reads left-to-right on a single
        # row. Frees the former Row 2 for future EQ / Profile / Notch
        # default-width controls without forcing the panel taller.
        levels.addWidget(QLabel("Out"))
        self.out_combo = QComboBox()
        # v0.0.9.6: operator-facing labels.  Internal QSettings value
        # for HL2 codec stays "AK4951" for back-compat (no operator
        # data migration needed); the combo translates display ↔
        # stored on selection.  Renamed because not all HL2 revisions
        # use the AK4951 chip specifically — they all share the same
        # EP2-back-to-codec path though, so "HL2 audio jack" is the
        # accurate name.
        self.out_combo.addItem("HL2 audio jack", userData="AK4951")
        self.out_combo.addItem("PC Soundcard", userData="PC Soundcard")
        # Set selection from the stored value.
        for i in range(self.out_combo.count()):
            if self.out_combo.itemData(i) == radio.audio_output:
                self.out_combo.setCurrentIndex(i)
                break
        self.out_combo.setFixedWidth(140)
        self.out_combo.setToolTip(
            "RX audio output destination.\n"
            "\n"
            "HL2 audio jack: route audio back to the HL2 over the "
            "network (EP2 frames) and play through the HL2's "
            "onboard codec headphone jack.  Single-crystal path, "
            "zero clock drift, recommended for HL2 hardware.\n"
            "\n"
            "PC Soundcard: route audio to the host PC's default "
            "WASAPI output device.  v0.0.9.6 enables drift "
            "compensation via WDSP-derived adaptive resampler — "
            "should be glitch-free for standard ±50 ppm crystal "
            "tolerance.")
        self.out_combo.currentIndexChanged.connect(
            lambda _idx: self.radio.set_audio_output(
                self.out_combo.currentData()))
        levels.addWidget(self.out_combo)

        levels.addSpacing(12)

        # Mute button — Radio-side state so it survives volume slider
        # drags while muted (slider can be positioned for post-unmute
        # without breaking silence). Muting multiplies final output by
        # 0 but leaves AGC / metering untouched.
        self.mute_btn = QPushButton("MUTE")
        self.mute_btn.setObjectName("dsp_btn")        # orange when checked
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFixedWidth(54)
        self.mute_btn.setChecked(radio.muted)
        self.mute_btn.setToolTip(
            "Silence output without changing the Volume slider. "
            "Click again to resume at the current volume setting.")
        self.mute_btn.toggled.connect(self.radio.set_muted)
        levels.addWidget(self.mute_btn)

        # DSP Settings shortcut — moved here from the DSP buttons
        # row below per operator UX request (more reachable spot
        # next to the levels controls; the DSP row stays focused
        # on actual DSP toggles).
        levels.addSpacing(8)
        self.dsp_settings_btn = QPushButton("DSP Settings…")
        self.dsp_settings_btn.setFixedWidth(140)
        self.dsp_settings_btn.setToolTip(
            "Open DSP settings (AGC profile + threshold, NB/NR/EQ)")
        self.dsp_settings_btn.clicked.connect(self._open_dsp_settings)
        levels.addWidget(self.dsp_settings_btn)

        levels.addStretch(1)
        self.content_layout().addLayout(levels)

        # Notch tooltip text — shared by the NF button on the DSP row
        # below AND the notch_info counter that sits next to it. Defined
        # here once so both references stay in sync. Counter + button
        # lived on a dedicated Row 2 originally; Option A consolidation
        # collapsed that row into the levels row above + the DSP row
        # below to recover vertical space.
        self._notch_tooltip = (
            "Notch Filter — manual per-frequency notches.\n"
            "Toggle on/off via the NF button on this DSP row.\n\n"
            "On the spectrum or waterfall (NF must be ON):\n"
            "  • Right-click          — menu (Add / Disable this /\n"
            "                            Make DEEP / Remove nearest /\n"
            "                            Clear all / Default width)\n"
            "  • Shift + right-click  — quick-remove nearest notch\n"
            "  • Left-drag a notch    — adjust that notch's width\n"
            "  • Wheel over a notch   — adjust that notch's width\n"
            "                            (down = wider, up = narrower)\n\n"
            "Counter format:\n"
            "  '3 notches  [50, 80*, 200^ Hz]  (1 off, 1 deep)'\n"
            "  Widths in Hz; markers:  *=inactive  ^=deep (cascade).\n\n"
            "Deep notches cascade the filter twice for ~2× dB\n"
            "attenuation — useful for stubborn carriers, costs 2×\n"
            "CPU and 2× settle time on placement.\n\n"
            "When NF is OFF, right-click shows a single 'Enable Notch\n"
            "Filter' item — right-click stays reserved for other\n"
            "spectrum features until you turn NF on.")

        # NOTE: there is no per-notch slider on the front panel.
        # Per-notch width is adjusted via wheel/drag over the notch
        # rectangle on the spectrum, and the default width for new
        # notches is in the right-click menu's "Default width" submenu.

        # ── DSP button row (NB / BIN / NR / ANF / APF / NF) ─────────
        # Backends will land per-feature; for now these toggle stubs
        # so the UI is in place. State signals route via Radio so TCI
        # and CAT can also drive them later.
        dsp_row = QHBoxLayout()
        dsp_row.setSpacing(4)
        dsp_row.addWidget(QLabel("DSP"))
        self.dsp_btns: dict[str, QPushButton] = {}
        for label, tip in (
            ("NB",  "Noise Blanker — impulse-noise suppression"),
            ("BIN", "Binaural — pseudo-stereo SSB spread"),
            ("NR",  "Noise Reduction — adaptive denoiser"),
            ("ANF", "Auto Notch — hunts and removes carriers"),
            ("LMS", "LMS Line Enhancer — lifts CW / tones above broadband noise"),
            ("SQ",  "All-Mode Squelch — voice-presence detector, mutes between transmissions"),
            ("APF", "Audio Peak Filter — narrow CW peaking"),
            ("NF",  "Notch Filter — manual notches (this panel)"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("dsp_btn")     # picks up the orange-when-on QSS
            btn.setCheckable(True)
            btn.setToolTip(tip)
            dsp_row.addWidget(btn)
            self.dsp_btns[label] = btn

        # ── ANF (Auto Notch Filter, Phase 3.D #3) ─────────────────
        # Left-click  = toggle on/off (cycles between Off and the
        #               last non-Off profile, default Medium)
        # Right-click = profile picker (Off / Light / Medium /
        #               Heavy / Custom) + Open Noise settings
        anf_btn = self.dsp_btns["ANF"]
        anf_btn.setChecked(radio.anf_enabled)
        anf_btn.toggled.connect(self._on_anf_btn_toggled)
        anf_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        anf_btn.customContextMenuRequested.connect(self._show_anf_menu)
        anf_btn.setToolTip(
            "Auto Notch Filter — LMS adaptive predictor.\n"
            "Surgically nulls hetorodynes / carriers / RTTY spurs.\n"
            "Left-click: toggle on/off.\n"
            "Right-click: pick profile (Off / Light / Medium / Heavy).")
        radio.anf_profile_changed.connect(self._on_anf_profile_changed)
        cur_anf = radio.anf_profile
        self._anf_last_active_profile = (
            cur_anf if cur_anf != "off" else "medium")

        # ── NB (Noise Blanker, Phase 3.D #2) ──────────────────────
        # Left-click  = toggle on/off (cycles between Off and the
        #               last non-Off profile, default Medium)
        # Right-click = profile picker (Off / Light / Medium /
        #               Heavy / Custom) + Open Noise settings
        nb_btn = self.dsp_btns["NB"]
        nb_btn.setChecked(radio.nb_enabled)
        nb_btn.toggled.connect(self._on_nb_btn_toggled)
        nb_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        nb_btn.customContextMenuRequested.connect(self._show_nb_menu)
        nb_btn.setToolTip(
            "Noise Blanker — IQ-domain impulse suppression.\n"
            "Targets ignition / power-line / lightning impulses.\n"
            "Left-click: toggle on/off.\n"
            "Right-click: pick profile (Off / Light / Medium / Heavy).")
        radio.nb_profile_changed.connect(self._on_nb_profile_changed)
        # Remember the operator's last non-Off profile so a
        # left-click toggle returns there instead of always picking
        # Medium.  Initialized from the loaded profile if it's not Off.
        cur_profile = radio.nb_profile
        self._nb_last_active_profile = (
            cur_profile if cur_profile != "off" else "medium")

        # Wire the ones we already implement; rest are visual-only stubs.
        # NF is now the single enable/disable button for notches — the
        # earlier standalone "Notch" button on the row above was
        # removed (it duplicated this one; both lit together, which
        # read as broken UI feedback).
        self.dsp_btns["NF"].setChecked(radio.notch_enabled)
        self.dsp_btns["NF"].toggled.connect(self.radio.set_notch_enabled)
        self.dsp_btns["NF"].setToolTip(self._notch_tooltip)

        # Live notch counter — sits immediately right of the NF
        # button so the operator's eye finds it without scanning the
        # whole panel. Tooltip mirrors the NF button so the same
        # gesture cheat-sheet pops on either hover target.
        self.notch_info = QLabel("0 notches")
        self.notch_info.setMinimumWidth(120)
        self.notch_info.setToolTip(self._notch_tooltip)
        self.notch_info.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-size: 10px;")
        # Lock vertical sizing so this label behaves like the DSP
        # buttons — fixed height regardless of panel height.
        # Operator feedback v0.0.6.x: the notch + AGC readouts were
        # stretching to fill the row when the DSP+Audio panel grew
        # taller, while the buttons to the left stayed put.
        self.notch_info.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Fixed)
        dsp_row.addWidget(self.notch_info)

        # ── NR (Noise Reduction) ─────────────────────────────────
        # Left-click  = toggle enable/disable
        # Right-click = profile menu (Light / Medium / Heavy /
        #               Neural[disabled until a neural package ships])
        nr_btn = self.dsp_btns["NR"]
        nr_btn.setChecked(radio.nr_enabled)
        nr_btn.toggled.connect(self.radio.set_nr_enabled)
        nr_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        nr_btn.customContextMenuRequested.connect(self._show_nr_menu)
        nr_btn.setToolTip(
            "Noise Reduction — spectral subtraction.\n"
            "Left-click: toggle on/off.\n"
            "Right-click: pick profile "
            "(Light / Medium / Heavy / Neural)")
        radio.nr_enabled_changed.connect(self._on_nr_enabled_changed)
        radio.nr_profile_changed.connect(self._on_nr_profile_changed)
        # Initialize NR button text + tooltip from current state.
        # The signal-driven update only fires on FUTURE changes;
        # without this push we'd have stale "NR" button text when
        # operator restarts Lyra with NR profile already set to NR2.
        self._on_nr_profile_changed(radio.nr_profile)

        # ── Capture Noise Profile button (Phase 3.D #1) ──────────
        # Compact action button paired with NR — left-click starts
        # a 2-second capture with the operator's saved duration
        # preference; right-click pops the full menu (capture /
        # manage / settings / clear / open profiles folder).  The
        # button text and color flip during capture to give live
        # progress feedback.
        self.nr_cap_btn = QPushButton("📷 Cap")
        self.nr_cap_btn.setObjectName("dsp_btn")
        self.nr_cap_btn.setToolTip(
            "Capture noise profile\n"
            "Left-click: start a capture (default 2.0 s)\n"
            "Right-click: capture options + manager + settings\n\n"
            "Tune to a noise-only frequency or wait for a\n"
            "transmission gap before clicking; the captured\n"
            "profile becomes a locked NR reference more\n"
            "accurate than the live VAD-tracked estimate.")
        self.nr_cap_btn.clicked.connect(self._on_nr_capture_clicked)
        self.nr_cap_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.nr_cap_btn.customContextMenuRequested.connect(
            self._show_nr_capture_menu)
        # Slight visual distinction: action-button (not a toggle),
        # so we leave it un-checkable.
        self.nr_cap_btn.setCheckable(False)
        # NOTE: nr_cap_btn isn't added to dsp_row — it gets parented
        # below in the nr_status_row alongside the source badge so
        # all noise-profile-related controls cluster on one line
        # (operator UX feedback: keeps the DSP buttons row uncluttered
        # and groups capture/source visually).
        # Live capture-progress poll — drives the button label
        # while a capture is in progress.  Stopped when state goes
        # back to idle/ready.
        self._nr_cap_poll = QTimer(self)
        self._nr_cap_poll.setInterval(100)  # 10 Hz UI refresh
        self._nr_cap_poll.timeout.connect(self._refresh_nr_capture_button)
        # Capture-done signal: prompts for save name + warns on
        # smart-guard "suspect" verdict.
        radio.noise_capture_done.connect(self._on_noise_capture_done)
        radio.noise_active_profile_changed.connect(
            self._on_noise_active_profile_changed)
        # P1.2 — staleness signal: toast when loaded captured profile
        # drifts beyond threshold.  Single-fire per stale event with
        # rearm; see Radio.noise_profile_stale docstring.
        radio.noise_profile_stale.connect(
            self._on_noise_profile_stale)
        # Source-toggle changes also update tooltips/labels via the
        # active-profile-changed slot (it re-paints the Cap button +
        # NR button tooltip with current state).
        radio.nr_use_captured_profile_changed.connect(
            lambda _on: self._on_noise_active_profile_changed(
                self.radio.active_captured_profile_name))

        # ── APF (Audio Peaking Filter) ────────────────────────────
        # Left-click  = toggle enable/disable
        # Right-click = quick BW/Gain sliders + open Settings shortcut
        # Mode-gated to CW (button greys when not CWU/CWL but stays
        # toggleable so the operator's setting is preserved across
        # mode switches).
        apf_btn = self.dsp_btns["APF"]
        apf_btn.setChecked(radio.apf_enabled)
        apf_btn.toggled.connect(self.radio.set_apf_enabled)
        apf_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        apf_btn.customContextMenuRequested.connect(self._show_apf_menu)
        radio.apf_enabled_changed.connect(self._on_apf_enabled_changed)
        radio.apf_bw_changed.connect(lambda _bw: self._refresh_apf_tooltip())
        radio.apf_gain_changed.connect(lambda _g: self._refresh_apf_tooltip())
        radio.mode_changed.connect(lambda _m: self._refresh_apf_tooltip())
        # Initial tooltip reflects current params + active mode.
        self._refresh_apf_tooltip()

        # ── LMS (NR3 line enhancer) ───────────────────────────────
        # Left-click  = toggle enable/disable
        # Right-click = quick strength preset menu
        # Independent of NR — slots ANF → LMS → NR in the chain so
        # both can run simultaneously (LMS lifts the periodic part,
        # NR cleans up broadband residual).
        lms_btn = self.dsp_btns["LMS"]
        lms_btn.setChecked(radio.lms_enabled)
        lms_btn.toggled.connect(self.radio.set_lms_enabled)
        lms_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        lms_btn.customContextMenuRequested.connect(self._show_lms_menu)
        lms_btn.setToolTip(
            "LMS Line Enhancer (NR3-style adaptive predictor)\n"
            "  Lifts periodic content (CW tones, voice formants)\n"
            "  above broadband noise.  Different from NR1/NR2:\n"
            "  this is predictive, they're subtractive — both can\n"
            "  run together for best weak-signal results.\n"
            "\n"
            "  Most useful in CW for weak DX in band hiss.\n"
            "  Right-click for strength presets.")
        radio.lms_enabled_changed.connect(lms_btn.setChecked)

        # ── SQ (All-Mode Squelch — SSQL) ──────────────────────────
        # Left-click  = toggle enable/disable
        # Right-click = quick threshold presets + open Settings
        # Threshold slider sits on the same row as the NR strength
        # sliders; only visible when SQ is enabled.
        sq_btn = self.dsp_btns["SQ"]
        sq_btn.setChecked(radio.squelch_enabled)
        sq_btn.toggled.connect(self.radio.set_squelch_enabled)
        sq_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        sq_btn.customContextMenuRequested.connect(self._show_sq_menu)
        sq_btn.setToolTip(
            "All-Mode Voice-Presence Squelch (SSQL)\n"
            "  Mutes audio output between transmissions on every\n"
            "  modulation type (SSB, AM, FM, CW).  Detects voice\n"
            "  via zero-crossing-rate analysis — works regardless\n"
            "  of how the signal was modulated.\n"
            "\n"
            "  Right-click for threshold presets.\n"
            "  Adjust threshold via the slider that appears when\n"
            "  this is enabled.")
        radio.squelch_enabled_changed.connect(sq_btn.setChecked)

        # ── BIN (Binaural pseudo-stereo) ──────────────────────────
        # Left-click  = toggle enable/disable
        # Right-click = depth presets (25 / 50 / 70 / 100 %)
        # No mode gate — runs on all modes (helpful for both CW
        # spatial cue and SSB voice widening on headphones).
        bin_btn = self.dsp_btns["BIN"]
        bin_btn.setChecked(radio.bin_enabled)
        bin_btn.toggled.connect(self.radio.set_bin_enabled)
        bin_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        bin_btn.customContextMenuRequested.connect(self._show_bin_menu)
        radio.bin_enabled_changed.connect(self._on_bin_enabled_changed)
        radio.bin_depth_changed.connect(lambda _d: self._refresh_bin_tooltip())
        self._refresh_bin_tooltip()

        dsp_row.addSpacing(12)

        # Live AGC readout — profile | threshold | current gain action.
        # The whole cluster (including the three labels) hosts a right-click
        # context menu to cycle profile without opening Settings.
        agc_panel_label = QLabel("AGC")
        agc_panel_label.setStyleSheet(
            "color: #00e5ff; font-weight: 800; letter-spacing: 1px;")
        agc_panel_label.setToolTip(
            "Right-click to change AGC profile (Off / Fast / Med / Slow)")
        dsp_row.addWidget(agc_panel_label)

        self.agc_profile_lbl = QLabel("—")
        self.agc_profile_lbl.setToolTip(
            "Current AGC profile — right-click to pick Off / Fast / Med /"
            " Slow / Auto / Custom. AUTO continuously tracks the noise"
            " floor; CUST uses your custom release/hang from Settings.")
        self.agc_profile_lbl.setCursor(Qt.PointingHandCursor)
        dsp_row.addWidget(self.agc_profile_lbl)

        thr_label = QLabel("thr")
        thr_label.setStyleSheet("color: #8a9aac; font-size: 9px;")
        dsp_row.addWidget(thr_label)
        self.agc_threshold_lbl = QLabel("—")
        self.agc_threshold_lbl.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-weight: 700; min-width: 70px;")
        dsp_row.addWidget(self.agc_threshold_lbl)

        action_label = QLabel("gain")
        action_label.setStyleSheet("color: #8a9aac; font-size: 9px;")
        dsp_row.addWidget(action_label)
        self.agc_action_lbl = QLabel("—")
        self.agc_action_lbl.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700; min-width: 58px;")
        dsp_row.addWidget(self.agc_action_lbl)

        # Right-click menu on the AGC widgets to pick profile without
        # opening Settings. "Auto" profile replaces the old dedicated button.
        # Also lock vertical sizing — same operator-feedback fix as
        # notch_info above: these labels were stretching to fill the
        # row when the panel grew taller, while DSP buttons stayed put.
        for w in (agc_panel_label, self.agc_profile_lbl, thr_label,
                  self.agc_threshold_lbl, action_label, self.agc_action_lbl):
            w.setContextMenuPolicy(Qt.CustomContextMenu)
            w.customContextMenuRequested.connect(self._show_agc_menu)
            w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        dsp_row.addStretch(1)

        # NOTE: the DSP Settings shortcut button used to live here at
        # the right end of the DSP buttons row — it moved up to the
        # levels row (next to MUTE) per operator UX feedback.  This
        # row now ends with a stretch so the buttons + counters +
        # AGC indicator stay left-aligned.

        self.content_layout().addLayout(dsp_row)

        # ── NR noise-source status badge (Phase 3.D #1) ─────────
        # Sits on its own thin sub-row directly below the DSP
        # buttons.  Always visible; click toggles Live ⇄ Captured
        # (when a profile is loaded; greyed otherwise).  Shows the
        # active profile name + age + mode/band — the operator
        # never has to open a menu to check what NR is using.
        self.nr_source_badge = QPushButton()
        self.nr_source_badge.setObjectName("nr_source_badge")
        self.nr_source_badge.setFlat(True)
        self.nr_source_badge.setCursor(Qt.PointingHandCursor)
        # Left-align text inside the button.  Bump left padding so
        # the colored emoji dot doesn't visually clip against the
        # button's rounded edge — Qt's emoji rendering sometimes
        # has a tight bounding box and 8 px wasn't enough.
        self.nr_source_badge.setStyleSheet(
            "QPushButton#nr_source_badge {"
            "  text-align: left;"
            "  padding: 4px 10px 4px 14px;"
            "  border: 1px solid transparent;"
            "  border-radius: 4px;"
            "  font-family: 'Segoe UI', sans-serif;"
            "  font-size: 11px;"
            "}"
            "QPushButton#nr_source_badge:hover:!disabled {"
            "  background-color: rgba(80, 208, 255, 0.10);"
            "  border-color: rgba(80, 208, 255, 0.35);"
            "}"
            "QPushButton#nr_source_badge:disabled {"
            "  color: #6a7a8c;"
            "}")
        self.nr_source_badge.clicked.connect(
            self._on_nr_source_badge_clicked)

        # ── Noise-controls sub-row (Phase 3.D #4 layout pass) ────
        # Single horizontal row that hosts all noise-toolkit panel
        # controls.  Per operator UX feedback the row is now:
        #
        #   [NR2 strength: ───── 7%]   [📷 Cap]   [Source: ...   ⇄]
        #
        # NR2 strength widgets are constructed first but only added
        # to the row layout when nr_profile == "nr2"; otherwise the
        # cap button is left-aligned and the source badge takes the
        # available width.  Width-constrained: NR2 slider gets a
        # fixed sensible width (similar to AF/Vol slider width)
        # rather than stretching to fill, and the source badge gets
        # a maximum width so it doesn't span the entire panel.

        # Build NR1 strength widgets — added to the row dynamically
        # below.  Visible when active NR backend is NR1 (the
        # classical spectral-subtraction path).  Maps slider 0..100
        # to NR1 strength 0.0..1.0 (parallel to NR2's 0..150 → 0..1.5).
        # NR Mode selector (post-2026-05-07 NR-UX overhaul).
        # The legacy "NR strength" slider is repurposed as a 1..4
        # mode selector that drives WDSP's EMNR gain_method.  The
        # variable is still named nr1_strength_slider for stylesheet
        # / Qt-name compatibility but it now selects MODE not strength.
        # See Radio._NR_MODE_TO_GAIN_METHOD for the mapping:
        #   Mode 1 → Wiener+SPP (smooth, mid-aggressive)
        #   Mode 2 → Wiener simple (edgier, more raw subtraction)
        #   Mode 3 → MMSE-LSA (WDSP default, smoothest) ← default
        #   Mode 4 → Trained adaptive (newest, most aggressive)
        self._nr1_label_widget = QLabel("Mode:")
        self._nr1_label_widget.setStyleSheet(
            "color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "font-size: 11px;")
        self.nr1_strength_slider = QSlider(Qt.Horizontal)
        self.nr1_strength_slider.setRange(1, 4)
        self.nr1_strength_slider.setValue(
            int(getattr(radio, "nr_mode", 3)))
        self.nr1_strength_slider.setSingleStep(1)
        self.nr1_strength_slider.setPageStep(1)
        self.nr1_strength_slider.setTickPosition(QSlider.TicksBelow)
        self.nr1_strength_slider.setTickInterval(1)
        self.nr1_strength_slider.setFixedWidth(120)
        self.nr1_strength_slider.setToolTip(
            "NR mode (1..4) — picks WDSP's EMNR gain function.\n"
            "  Mode 1 = Wiener + SPP soft mask (smooth, mid)\n"
            "  Mode 2 = Wiener simple (edgier subtraction)\n"
            "  Mode 3 = MMSE-LSA (WDSP default, smoothest) [default]\n"
            "  Mode 4 = Trained adaptive (most aggressive)\n"
            "\n"
            "AEPF (anti-musical-noise) checkbox is separate.\n"
            "Try AEPF off + different modes to find your best sound.")
        self.nr1_strength_slider.valueChanged.connect(
            self._on_nr_mode_slider)
        self.nr1_strength_label = QLabel(
            f"{int(getattr(radio, 'nr_mode', 3))}")
        self.nr1_strength_label.setFixedWidth(20)
        self.nr1_strength_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 11px;")
        # AEPF (Adaptive Equalization Post-Filter) toggle — anti-
        # musical-noise smoother on EMNR's gain mask.  Default ON
        # because the un-AEPF residual is noticeably more "watery."
        # Operator can disable to A/B raw EMNR character on clean
        # bands where the smoothing isn't needed.  (QCheckBox +
        # QComboBox come from the module-level import at top —
        # earlier I imported them locally which shadowed QComboBox
        # for the whole method and crashed line 1350's
        # `self.out_combo = QComboBox()` with UnboundLocalError.)
        self.aepf_checkbox = QCheckBox("AEPF")
        self.aepf_checkbox.setChecked(
            bool(getattr(radio, "aepf_enabled", True)))
        self.aepf_checkbox.setToolTip(
            "Adaptive Equalization Post-Filter — reduces musical-noise\n"
            "artifacts in NR output.  ON (default) gives smoother,\n"
            "less 'watery' character.  OFF gives raw EMNR — cleaner\n"
            "noise floor on quiet bands but more pronounced subtraction\n"
            "residue on noisier ones.")
        self.aepf_checkbox.setStyleSheet(
            "color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "font-size: 11px;")
        self.aepf_checkbox.toggled.connect(self._on_aepf_checkbox)

        # NPE — Noise Power Estimator — operator picks how WDSP's
        # EMNR tracks the noise floor.  Surfacing this knob is one
        # of Lyra's WDSP-UX differentiators (other clients hide it).
        # Two-method dropdown:
        #   OSMS — recursive averaging (WDSP default, smoother)
        #   MCRA — Minimum-Controlled Recursive Avg (faster-tracking)
        self._npe_label_widget = QLabel("NPE:")
        self._npe_label_widget.setStyleSheet(
            "color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "font-size: 11px;")
        self.npe_combo = QComboBox()
        self.npe_combo.addItem("OSMS", 0)
        self.npe_combo.addItem("MCRA", 1)
        self.npe_combo.setCurrentIndex(
            int(getattr(radio, "npe_method", 0)))
        self.npe_combo.setFixedWidth(80)
        self.npe_combo.setToolTip(
            "NPE — Noise Power Estimator\n"
            "\n"
            "  OSMS  Recursive averaging (default).\n"
            "        Smoother tracking, best for stationary noise\n"
            "        (atmospheric, broadband ambient hiss).\n"
            "\n"
            "  MCRA  Minimum-Controlled Recursive Averaging.\n"
            "        Faster-tracking, better for non-stationary\n"
            "        noise (changing band conditions, intermittent\n"
            "        QRM).")
        self.npe_combo.setStyleSheet(
            "QComboBox {"
            "  color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "  font-size: 11px;"
            "}")
        self.npe_combo.currentIndexChanged.connect(self._on_npe_combo)

        # Build NR2 strength widgets — added to the row dynamically
        # below depending on whether NR2 is active.
        self._nr2_label_widget = QLabel("NR2 strength:")
        self._nr2_label_widget.setStyleSheet(
            "color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "font-size: 11px;")
        # Slider maps integer 0..200 → aggression 0.0..2.0 (×100
        # internal scaling for 0.01-step precision).  Range was
        # bumped from 0..150 to 0..200 after the WDSP-port machinery
        # (Martin + SPP + AEPF) made the higher aggression range
        # listenable — SPP guards speech bins from over-attenuation,
        # AEPF smooths deep-suppression masks.  Operators on very
        # noisy bands can usefully push past 100% now.
        self.nr2_agg_slider = QSlider(Qt.Horizontal)
        self.nr2_agg_slider.setRange(0, 200)
        self.nr2_agg_slider.setValue(
            int(round(radio.nr2_aggression * 100)))
        self.nr2_agg_slider.setSingleStep(5)
        self.nr2_agg_slider.setPageStep(25)
        self.nr2_agg_slider.setTickPosition(QSlider.TicksBelow)
        self.nr2_agg_slider.setTickInterval(50)
        # Slightly wider than NR1's slider (160) to accommodate the
        # 0..250 range with comparable per-tick resolution.
        self.nr2_agg_slider.setFixedWidth(200)
        self.nr2_agg_slider.setToolTip(
            "NR2 suppression strength.\n"
            "  0   = unity gain (effectively NR off)\n"
            "  100 = full Ephraim-Malah / Wiener (default)\n"
            "  150 = harder cleanup\n"
            "  200 = ceiling — extreme noise / stacking with LMS\n"
            "\n"
            "Right-click the NR button to switch between NR1, NR2,\n"
            "and Neural backends.  Right-click this slider to pick\n"
            "MMSE-LSA vs Wiener gain function.")
        self.nr2_agg_slider.valueChanged.connect(
            self._on_nr2_agg_slider)
        # Right-click on the NR2 slider opens the gain-method picker
        # (MMSE-LSA vs Wiener).  Both are MMSE-derived; MMSE-LSA is
        # the WDSP case-1 path with sharper noise attack and
        # classical sound, Wiener is the WDSP case-0 path with
        # smoother per-bin transitions and "fuller" residual.
        self.nr2_agg_slider.setContextMenuPolicy(Qt.CustomContextMenu)
        self.nr2_agg_slider.customContextMenuRequested.connect(
            self._show_nr2_method_menu)
        self.nr2_agg_label = QLabel(
            f"{int(round(radio.nr2_aggression * 100))} %")
        self.nr2_agg_label.setFixedWidth(40)
        self.nr2_agg_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 11px;")

        # Source badge gets a max width so it doesn't span the
        # whole panel — looks visually balanced with the cap
        # button + nr2 strider.  ~360 px fits the typical
        # "🟢  <name>  ·  3h old  ·  80m LSB  ⇄" string at the 24-char
        # name cap (enforced at the save/rename prompts) with margin
        # to spare; longer profile names truncate gracefully via Qt's
        # text-eliding.  Bumped from 360 to 460 after operator
        # feedback that the right edge was clipping the "⇄" arrow on
        # mid-length names.
        self.nr_source_badge.setMaximumWidth(460)

        # LMS strength slider — slot in the NR-status row;
        # visibility tied to LMS enable so it doesn't crowd the
        # row when LMS is off.  Same UX pattern as the SQ slider.
        self._lms_label_widget = QLabel("LMS:")
        self._lms_label_widget.setStyleSheet(
            "color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "font-size: 11px;")
        # Slider 0..100 → strength 0.0..1.0.  At 50 the algorithm
        # parameters land on Pratt's WDSP defaults (the operator-
        # validated 'classic ANR' tuning).
        self.lms_strength_slider = QSlider(Qt.Horizontal)
        self.lms_strength_slider.setRange(0, 100)
        self.lms_strength_slider.setValue(
            int(round(radio.lms_strength * 100)))
        self.lms_strength_slider.setSingleStep(5)
        self.lms_strength_slider.setPageStep(25)
        self.lms_strength_slider.setTickPosition(QSlider.TicksBelow)
        self.lms_strength_slider.setTickInterval(25)
        self.lms_strength_slider.setFixedWidth(160)
        self.lms_strength_slider.setToolTip(
            "LMS line-enhancer strength (multi-parameter).\n"
            "  0   = subtle:  32 taps, 50% wet/dry mix\n"
            "  50  = default: 80 taps, 75% wet (WDSP-class)\n"
            "  100 = full:   128 taps, 100% wet (pure prediction)\n"
            "\n"
            "Higher = more selective predictor + less of the\n"
            "original signal blended in.  Bigger perceptual\n"
            "swing than just adapt-rate alone — operator should\n"
            "hear ~10 dB difference between min and max on\n"
            "stable signals like CW carriers.\n"
            "\n"
            "Right-click the LMS button for preset shortcuts.")
        self.lms_strength_slider.valueChanged.connect(
            self._on_lms_strength_slider)
        self.lms_strength_label = QLabel(
            f"{int(round(radio.lms_strength * 100))} %")
        self.lms_strength_label.setFixedWidth(40)
        self.lms_strength_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 11px;")

        # Squelch threshold slider — slot in the same row as the
        # NR strength sliders; visibility tied to squelch enable.
        self._sq_label_widget = QLabel("Squelch:")
        self._sq_label_widget.setStyleSheet(
            "color: #cdd9e5; font-family: 'Segoe UI', sans-serif; "
            "font-size: 11px;")
        self.sq_threshold_slider = QSlider(Qt.Horizontal)
        # Slider 0..100 → threshold 0.0..1.0.  Most useful range
        # is 0..50 (default 16); above 50 the squelch becomes hard
        # to keep open.  Operator can drag the full 0..100 range
        # but the typical sweet spot is 10..30.
        self.sq_threshold_slider.setRange(0, 100)
        self.sq_threshold_slider.setValue(
            int(round(radio.squelch_threshold * 100)))
        self.sq_threshold_slider.setSingleStep(2)
        self.sq_threshold_slider.setPageStep(10)
        self.sq_threshold_slider.setTickPosition(QSlider.TicksBelow)
        self.sq_threshold_slider.setTickInterval(20)
        self.sq_threshold_slider.setFixedWidth(160)
        self.sq_threshold_slider.setToolTip(
            "Squelch threshold.\n"
            "  0   = effectively off (everything passes)\n"
            "  10  = barely-on, opens on faintest signal\n"
            "  20  = default — voice-friendly\n"
            "  40  = medium — mutes on quiet bands\n"
            "  60  = tight — strong signals only\n"
            "  80+ = very tight (only loud stations unmute)\n"
            "\n"
            "Right-click the SQ button for preset shortcuts.")
        self.sq_threshold_slider.valueChanged.connect(
            self._on_sq_threshold_slider)
        self.sq_threshold_label = QLabel(
            f"{int(round(radio.squelch_threshold * 100))}")
        self.sq_threshold_label.setFixedWidth(28)
        self.sq_threshold_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 11px;")
        # Activity indicator — small dot, green when audio is
        # passing through the squelch, dark grey when muted.
        # Hidden when squelch is disabled.
        self.sq_activity_dot = QLabel("●")
        self.sq_activity_dot.setFixedWidth(14)
        self.sq_activity_dot.setStyleSheet(
            "color: #303030; font-size: 14px;")
        self.sq_activity_dot.setToolTip(
            "Squelch activity:\n"
            "  green = passing audio\n"
            "  grey  = muted")
        # Polling timer — refresh the activity dot at 10 Hz so it
        # tracks the squelch state without flooding the event loop.
        self._sq_activity_timer = QTimer(self)
        self._sq_activity_timer.setInterval(100)
        self._sq_activity_timer.timeout.connect(
            self._refresh_sq_activity_dot)

        nr_status_row = QHBoxLayout()
        nr_status_row.setContentsMargins(0, 0, 0, 0)
        nr_status_row.setSpacing(6)
        # NR-UX overhaul (2026-05-07): the legacy NR1 strength
        # slider is repurposed as Mode selector (1..4); NR2
        # aggression slider is HIDDEN entirely (it was only ever
        # active when nr_profile == "nr2", which the new model
        # collapses into Mode 1).  AEPF checkbox + NPE dropdown
        # sit alongside the mode slider for one-click NR character
        # tuning.  All three (Mode + AEPF + NPE) are NR character
        # knobs — Cap button + source badge are noise-reference
        # controls (orthogonal concept).
        nr_status_row.addWidget(self._nr1_label_widget)
        nr_status_row.addWidget(self.nr1_strength_slider)
        nr_status_row.addWidget(self.nr1_strength_label)
        nr_status_row.addSpacing(8)
        nr_status_row.addWidget(self.aepf_checkbox)
        nr_status_row.addSpacing(4)
        nr_status_row.addWidget(self._npe_label_widget)
        nr_status_row.addWidget(self.npe_combo)
        # Legacy NR2 widgets stay constructed for code compatibility
        # but are not added to the layout — they don't appear in the
        # UI under the new model.  In legacy mode they're unreachable
        # via UI but operator can still drive nr2_aggression via
        # CAT/TCI for backward compat.
        nr_status_row.addSpacing(8)
        nr_status_row.addWidget(self.nr_cap_btn)
        nr_status_row.addWidget(self.nr_source_badge)
        nr_status_row.addSpacing(12)
        # LMS widgets — always added, visibility controlled by
        # the LMS enable state.  LMS is independent of NR1/NR2
        # (runs as its own stage in the chain) so it has its own
        # always-visible-when-enabled slider.
        nr_status_row.addWidget(self._lms_label_widget)
        nr_status_row.addWidget(self.lms_strength_slider)
        nr_status_row.addWidget(self.lms_strength_label)
        nr_status_row.addSpacing(12)
        # Squelch widgets — always added, visibility controlled
        # by the SQ enable state.  Activity dot first (small),
        # then label, slider, threshold readout.
        nr_status_row.addWidget(self.sq_activity_dot)
        nr_status_row.addWidget(self._sq_label_widget)
        nr_status_row.addWidget(self.sq_threshold_slider)
        nr_status_row.addWidget(self.sq_threshold_label)
        # Stretch at the end so all widgets stay left-aligned and
        # don't try to fill horizontal space.
        nr_status_row.addStretch(1)
        self.content_layout().addLayout(nr_status_row)
        # NR-UX overhaul: Mode + AEPF always visible (the new model
        # has a single mode selector, not branched by backend).
        # Legacy NR2 widgets stay hidden (they're not in the layout
        # but ensure setVisible(False) for any code path that might
        # query them).
        self._nr1_label_widget.setVisible(True)
        self.nr1_strength_slider.setVisible(True)
        self.nr1_strength_label.setVisible(True)
        self._nr2_label_widget.setVisible(False)
        self.nr2_agg_slider.setVisible(False)
        self.nr2_agg_label.setVisible(False)
        # LMS slider visibility tied to LMS enable state.
        lms_visible = bool(radio.lms_enabled)
        self._lms_label_widget.setVisible(lms_visible)
        self.lms_strength_slider.setVisible(lms_visible)
        self.lms_strength_label.setVisible(lms_visible)
        # Squelch slider visibility tied to enable state.
        sq_visible = bool(radio.squelch_enabled)
        self._sq_label_widget.setVisible(sq_visible)
        self.sq_threshold_slider.setVisible(sq_visible)
        self.sq_threshold_label.setVisible(sq_visible)
        self.sq_activity_dot.setVisible(sq_visible)
        if sq_visible:
            self._sq_activity_timer.start()
        # Two-way sync so each slider mirrors Radio's state.
        radio.nr1_strength_changed.connect(
            self._on_nr1_strength_signal)
        radio.nr2_aggression_changed.connect(
            self._on_nr2_agg_signal)
        # NR-UX overhaul: mirror Radio's mode + AEPF + NPE state
        # into the new widgets.
        radio.nr_mode_changed.connect(self._on_nr_mode_signal)
        radio.aepf_enabled_changed.connect(self._on_aepf_enabled_signal)
        radio.npe_method_changed.connect(self._on_npe_method_signal)
        radio.squelch_threshold_changed.connect(
            self._on_sq_threshold_signal)
        radio.squelch_enabled_changed.connect(
            self._on_sq_enabled_changed)
        radio.lms_strength_changed.connect(
            self._on_lms_strength_signal)
        radio.lms_enabled_changed.connect(
            self._on_lms_enabled_changed)
        # Show/hide NR1/NR2 widgets when active NR backend changes.
        radio.nr_profile_changed.connect(
            self._refresh_nr2_panel_visibility)
        # Refresh on any of the events that affect what the badge
        # should display.
        radio.nr_use_captured_profile_changed.connect(
            lambda _on: self._refresh_nr_source_badge())
        radio.noise_active_profile_changed.connect(
            lambda _name: self._refresh_nr_source_badge())
        radio.mode_changed.connect(
            lambda _m: self._refresh_nr_source_badge())
        radio.freq_changed.connect(
            lambda _f: self._refresh_nr_source_badge())
        # Periodic age-color refresh — the badge color depends on
        # captured-at age, which advances even when nothing else
        # changes.  60s timer is plenty (age thresholds are in
        # hours/days).
        self._nr_badge_age_timer = QTimer(self)
        self._nr_badge_age_timer.setInterval(60_000)
        self._nr_badge_age_timer.timeout.connect(
            self._refresh_nr_source_badge)
        self._nr_badge_age_timer.start()
        # Initial paint.
        self._refresh_nr_source_badge()

        # Wire the live readouts to radio signals
        self._update_agc_profile(radio.agc_profile)
        self._update_agc_threshold(radio.agc_threshold)
        radio.agc_profile_changed.connect(self._update_agc_profile)
        radio.agc_threshold_changed.connect(self._update_agc_threshold)
        # agc_action_db fires every demod block (~40+ Hz) which would flicker
        # the label unreadably. Track peak-since-last-paint and repaint on a
        # timer at ~6 Hz so the value is both legible and shows short bursts.
        self._agc_action_peak = 0.0
        self._agc_action_last = 0.0
        self._agc_color_bucket = -1   # so first paint forces stylesheet set
        radio.agc_action_db.connect(self._on_agc_action)
        self._agc_paint_timer = QTimer(self)
        self._agc_paint_timer.setInterval(160)   # ~6 Hz
        self._agc_paint_timer.timeout.connect(self._paint_agc_action)
        self._agc_paint_timer.start()

        radio.notches_changed.connect(self._on_notches_changed)
        # NF button is the sole notch enable/disable UI now; the
        # standalone "Notch" button that used to mirror this signal
        # was removed to eliminate the confusing "two buttons light
        # together" feedback.
        radio.notch_enabled_changed.connect(self.dsp_btns["NF"].setChecked)
        # Default-width changes don't drive a front-panel slider
        # (they used to in the old Q-slider era; that was removed).
        # Kept no-op so future UI re-exposure has a wiring point.
        radio.notch_default_width_changed.connect(lambda _w: None)
        radio.audio_output_changed.connect(
            lambda o: self.out_combo.setCurrentText(o) if self.out_combo.currentText() != o else None)
        # LNA gain + Volume feedback (previously lived in GainPanel)
        radio.gain_changed.connect(self._on_gain_changed)
        radio.volume_changed.connect(self._on_volume_changed)
        # AF Gain state sync — covers QSettings load and future TCI
        # / CAT remote-control adjustments.
        radio.af_gain_db_changed.connect(self._on_af_gain_db_changed)
        # Mute + Auto-LNA state sync (signals driven by Radio — covers
        # QSettings load + any future TCI / CAT mute command).
        radio.muted_changed.connect(self._on_muted_changed)
        radio.lna_auto_changed.connect(self._on_lna_auto_changed)

    # Perceptual volume curve — 0..100 slider → 0..VOL_MAX multiplier
    # via a power curve, so each slider tick yields a roughly equal
    # loudness step. Human hearing is logarithmic — a linear slider
    # feels wildly touchy at low volumes.
    #
    # Since the AF Gain split (2026-04-24, Option B), Volume is
    # purely the FINAL OUTPUT TRIM stage. The makeup gain that was
    # previously squeezed into Volume's 50× headroom now lives in a
    # separate AF Gain dB slider, leaving Volume as a clean 0..1.0
    # (unity-at-max) trim — the role it always should have had.
    VOL_MAX = 1.0
    VOL_GAMMA = 2.0

    @classmethod
    def _slider_to_volume(cls, s: int) -> float:
        frac = max(0, min(100, int(s))) / 100.0
        return (frac ** cls.VOL_GAMMA) * cls.VOL_MAX

    @classmethod
    def _volume_to_slider(cls, v: float) -> int:
        v = max(0.0, min(cls.VOL_MAX, float(v)))
        frac = (v / cls.VOL_MAX) ** (1.0 / cls.VOL_GAMMA)
        return int(round(frac * 100))

    def _on_vol_slider(self, slider_val: int):
        """User dragged the slider → apply perceptual curve → Radio."""
        self.vol_label.setText(f"{slider_val}%")
        self.radio.set_volume(self._slider_to_volume(slider_val))

    # ── LNA linearity zones ─────────────────────────────────────
    # AD9866 PGA linearity behaviour (HL2 community consensus):
    #   -12 .. +20 dB   sweet spot — clean conversion, low IMD
    #   +20 .. +28 dB   high gain  — fine on quiet bands, watch IMD
    #   +28 .. +31 dB   IMD risk   — only if you really need every dB
    # Above +31 dB the PGA stops contributing real gain and starts
    # compressing the ADC; Lyra hard-caps the slider at +31 in
    # Radio.set_gain_db so the operator cannot enter that region.
    _LNA_ZONE_GREEN_MAX  = 20    # green if db <= this
    _LNA_ZONE_YELLOW_MAX = 28    # yellow if db <= this; orange above
    _LNA_COLOR_GREEN  = "#39ff14"
    _LNA_COLOR_YELLOW = "#ffd54f"
    _LNA_COLOR_ORANGE = "#ff8c3a"

    @classmethod
    def _lna_zone_color(cls, db: int) -> str:
        if db <= cls._LNA_ZONE_GREEN_MAX:
            return cls._LNA_COLOR_GREEN
        if db <= cls._LNA_ZONE_YELLOW_MAX:
            return cls._LNA_COLOR_YELLOW
        return cls._LNA_COLOR_ORANGE

    def _refresh_lna_label_color(self, db: int):
        color = self._lna_zone_color(db)
        self.lna_label.setStyleSheet(
            f"color: {color}; font-family: Consolas, monospace; "
            "font-weight: 700;")

    def _on_gain_changed(self, db: int):
        self.lna_label.setText(f"{db:+d} dB")
        self._refresh_lna_label_color(db)
        if self.lna_slider.value() != db:
            self.lna_slider.blockSignals(True)
            self.lna_slider.setValue(db)
            self.lna_slider.blockSignals(False)

    def _on_lna_auto_event(self, payload: dict):
        """Radio.lna_auto_event — Auto-LNA just adjusted gain.
        Show a 'last event' badge and briefly flash the slider so
        the operator can SEE Auto working in real time (the slider
        movement alone can be missed if you're not looking at it)."""
        delta = payload.get("delta_db", 0)
        when = payload.get("when_local", "")
        peak = payload.get("peak_dbfs", 0.0)
        arrow = "↓" if delta < 0 else "↑"
        self.lna_auto_event_lbl.setText(
            f"{arrow}{abs(delta)} dB  {when}")
        self.lna_auto_event_lbl.setToolTip(
            f"Auto-LNA fired at {when}\n"
            f"ADC peak was {peak:+.1f} dBFS\n"
            f"Gain change: {arrow}{abs(delta)} dB")
        # Brief amber flash on the slider so the eye catches it.
        self.lna_slider.setStyleSheet(
            "QSlider::groove:horizontal { "
            "background: #ffab47; border-radius: 3px; }"
        )
        self._lna_flash_timer.start(800)

    def _clear_lna_flash(self):
        """Reset the LNA slider stylesheet after the post-Auto flash."""
        self.lna_slider.setStyleSheet("")

    def _on_volume_changed(self, v: float):
        """Radio volume changed elsewhere — convert multiplier back
        to slider position via inverse curve and update UI."""
        target = self._volume_to_slider(v)
        self.vol_label.setText(f"{target}%")
        if self.vol_slider.value() != target:
            self.vol_slider.blockSignals(True)
            self.vol_slider.setValue(target)
            self.vol_slider.blockSignals(False)

    # ── Balance slider (Phase 1: pan a single mono RX across L/R) ───
    # Future RX2 / Split expansion: when a second receiver lands, the
    # balance model becomes "RX1 → L gain, RX2 → R gain" with a routing
    # mode enum on Radio. The slider widget itself stays the same
    # control surface — only the meaning of the gains shifts upstream.
    @staticmethod
    def _format_bal(b: float) -> str:
        # b ∈ [-1, +1] → "L100", "C", "R37" etc.
        if abs(b) < 0.01:
            return "C"
        if b < 0:
            return f"L{int(round(-b * 100))}"
        return f"R{int(round(b * 100))}"

    # Deadzone (in slider ticks, ±) that snaps the slider back to true
    # zero when the operator sweeps through center. Small enough that a
    # deliberate L3% pan is still reachable; large enough that aiming for
    # mono doesn't require pixel-perfect placement.
    _BAL_CENTER_SNAP_TICKS = 3

    def _on_bal_slider(self, slider_val: int):
        """User dragged the Balance slider → push to Radio.
        If we're inside the center-snap deadzone, force the slider
        widget back to 0 so the operator gets a "clicks into mono"
        feel and the label cleanly reads "C"."""
        v = int(slider_val)
        if -self._BAL_CENTER_SNAP_TICKS <= v <= self._BAL_CENTER_SNAP_TICKS \
                and v != 0:
            # Re-enter this handler with v=0 — block signals on the
            # second pass to prevent infinite recursion.
            self.bal_slider.blockSignals(True)
            self.bal_slider.setValue(0)
            self.bal_slider.blockSignals(False)
            v = 0
        b = max(-100, min(100, v)) / 100.0
        self.bal_label.setText(self._format_bal(b))
        self.radio.set_balance(b)

    def _on_radio_balance_changed(self, b: float):
        """Radio balance changed elsewhere (QSettings load, future
        TCI/CAT) — keep slider + label in sync without re-firing."""
        target = int(round(max(-1.0, min(1.0, float(b))) * 100))
        self.bal_label.setText(self._format_bal(b))
        if self.bal_slider.value() != target:
            self.bal_slider.blockSignals(True)
            self.bal_slider.setValue(target)
            self.bal_slider.blockSignals(False)

    def _on_af_gain_db_changed(self, db: int):
        """Radio AF Gain changed elsewhere — keep slider + label in
        sync (e.g. QSettings load, future TCI/CAT control)."""
        self.af_gain_label.setText(f"+{db} dB")
        if self.af_gain_slider.value() != db:
            self.af_gain_slider.blockSignals(True)
            self.af_gain_slider.setValue(db)
            self.af_gain_slider.blockSignals(False)

    def _on_muted_changed(self, muted: bool):
        """Radio mute state changed (e.g., via TCI, QSettings load).
        Keep the UI button in sync without firing our own clicked."""
        if self.mute_btn.isChecked() != muted:
            self.mute_btn.blockSignals(True)
            self.mute_btn.setChecked(muted)
            self.mute_btn.blockSignals(False)

    def _on_lna_auto_changed(self, on: bool):
        """Radio Auto-LNA state changed — keep the button in sync.
        Clear the 'last event' badge when Auto turns off (otherwise
        the stale event text sits there indefinitely)."""
        if self.auto_lna_btn.isChecked() != on:
            self.auto_lna_btn.blockSignals(True)
            self.auto_lna_btn.setChecked(on)
            self.auto_lna_btn.blockSignals(False)
        if not on:
            self.lna_auto_event_lbl.setText("")
            self.lna_auto_event_lbl.setToolTip("")

    # ── NR (Noise Reduction) ────────────────────────────────────
    # Right-click menu now picks the BACKEND (NR1 / NR2 / Neural),
    # not a strength tier — strength is set via the inline panel
    # slider for whichever backend is active.  Mirrors NR2's
    # already-existing slider-only UX.
    _NR_PROFILE_LABELS = {
        "nr1":        "Classical NR",
        "nr2":        "High Quality (NR2)",
        "neural":     "Neural (RNNoise / DeepFilterNet)",
    }

    def _show_nr_menu(self, pos):
        """Right-click on the NR button — Mode 1..4 + AEPF + enable/disable.

        Post-2026-05-07 NR-UX overhaul: the legacy NR1/NR2/Neural
        backend picker is gone; the menu now mirrors the inline
        Mode slider + AEPF checkbox.  Operator can pick mode here
        as a quick-access alternative to the slider, plus toggle
        AEPF and master enable.
        """
        btn = self.dsp_btns["NR"]
        menu = QMenu(self)
        current_mode = int(getattr(self.radio, "nr_mode", 3))
        mode_labels = {
            1: "Mode 1  —  Wiener + SPP (smooth, mid)",
            2: "Mode 2  —  Wiener simple (edgier)",
            3: "Mode 3  —  MMSE-LSA  (default, smoothest)",
            4: "Mode 4  —  Trained adaptive (most aggressive)",
        }
        for m in (1, 2, 3, 4):
            act = QAction(mode_labels[m], menu)
            act.setCheckable(True)
            act.setChecked(current_mode == m)
            act.triggered.connect(
                lambda _=False, mm=m: self.radio.set_nr_mode(mm))
            menu.addAction(act)
        menu.addSeparator()
        # AEPF toggle — anti-musical-noise post-filter.
        aepf_on = bool(getattr(self.radio, "aepf_enabled", True))
        aepf_act = QAction(
            "✓ AEPF (anti-musical-noise)" if aepf_on
            else "  AEPF (anti-musical-noise)", menu)
        aepf_act.setCheckable(True)
        aepf_act.setChecked(aepf_on)
        aepf_act.triggered.connect(
            lambda _=False: self.radio.set_aepf_enabled(
                not bool(getattr(self.radio, "aepf_enabled", True))))
        menu.addAction(aepf_act)
        # NPE method submenu — operator picks the noise tracker.
        npe_menu = menu.addMenu("Noise Power Estimator")
        current_npe = int(getattr(self.radio, "npe_method", 0))
        for npe_val, npe_label in (
                (0, "OSMS  (recursive — smoother, stationary noise)"),
                (1, "MCRA  (faster — non-stationary noise)")):
            npe_act = QAction(npe_label, npe_menu)
            npe_act.setCheckable(True)
            npe_act.setChecked(current_npe == npe_val)
            npe_act.triggered.connect(
                lambda _=False, v=npe_val: self.radio.set_npe_method(v))
            npe_menu.addAction(npe_act)
        menu.addSeparator()
        toggle_act = QAction(
            "Disable NR" if self.radio.nr_enabled else "Enable NR", menu)
        toggle_act.triggered.connect(
            lambda: self.radio.set_nr_enabled(not self.radio.nr_enabled))
        menu.addAction(toggle_act)
        menu.exec(btn.mapToGlobal(pos))

    # ── Capture-button helpers (Phase 3.D #1) ────────────────────────

    def _on_nr_capture_clicked(self) -> None:
        """Left-click on the Capture button.

        - If no capture is in progress: start a 2-second capture
          (or whatever duration is saved in QSettings).
        - If a capture IS in progress: cancel it.

        Both paths are no-cost if Radio isn't ready; the actual
        capture only progresses when audio is flowing through
        the channel.
        """
        from PySide6.QtCore import QSettings
        state, _ = self.radio.nr_capture_progress()
        if state == "capturing":
            self.radio.cancel_noise_capture()
            self._nr_cap_poll.stop()
            self._refresh_nr_capture_button()
            return
        # Pull the saved duration preference (set in Settings -> Noise
        # tab); default 2.0 sec per locked operator decision.
        s = QSettings("N8SDR", "Lyra")
        duration = float(s.value("noise/capture_duration_sec", 2.0,
                                 type=float))
        # Need NR enabled OR at least the audio chain to feed the
        # accumulator.  NR's process() handles capture-while-disabled
        # via the lightweight FFT-only path, so we don't auto-enable
        # NR here — operator decides.
        self.radio.begin_noise_capture(duration)
        self._nr_cap_poll.start()
        self._refresh_nr_capture_button()

    def _refresh_nr_capture_button(self) -> None:
        """Update the Capture button label to reflect current state.

        Called from the 100 ms QTimer while a capture is in progress
        plus once on each state transition.  When the capture
        finishes, the timer stops and the button returns to its
        idle label.
        """
        state, frac = self.radio.nr_capture_progress()
        btn = self.nr_cap_btn
        if state == "capturing":
            pct = int(round(frac * 100))
            btn.setText(f"⏹ {pct}%")
            btn.setToolTip(
                f"Capturing noise profile — {pct}% complete.\n"
                "Click to cancel.")
        else:
            self._nr_cap_poll.stop()
            btn.setText("📷 Cap")
            btn.setToolTip(
                "Capture noise profile\n"
                "Left-click: start a capture (default 2.0 s)\n"
                "Right-click: capture options + manager + settings")

    def _show_nr_capture_menu(self, pos):
        """Right-click on the Capture button — full menu of
        capture-related actions."""
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QInputDialog
        btn = self.nr_cap_btn
        menu = QMenu(self)

        # Capture-now entries with a few common durations.
        s = QSettings("N8SDR", "Lyra")
        default_dur = float(s.value("noise/capture_duration_sec", 2.0,
                                    type=float))
        cap_now = QAction(f"Capture now ({default_dur:.1f} s)", menu)
        cap_now.triggered.connect(self._on_nr_capture_clicked)
        menu.addAction(cap_now)
        for dur in (1.0, 2.0, 3.0, 5.0):
            if abs(dur - default_dur) < 0.01:
                continue   # already shown as the "default" entry
            act = QAction(f"Capture for {dur:.1f} s", menu)
            act.triggered.connect(
                lambda _=False, d=dur: self.radio.begin_noise_capture(d))
            act.triggered.connect(self._nr_cap_poll.start)
            act.triggered.connect(self._refresh_nr_capture_button)
            menu.addAction(act)

        menu.addSeparator()

        # Manage / Settings shortcuts.
        manage_act = QAction("Manage profiles…", menu)
        manage_act.triggered.connect(self._open_noise_profile_manager)
        menu.addAction(manage_act)
        settings_act = QAction("Open Noise settings…", menu)
        settings_act.triggered.connect(self._open_noise_settings)
        menu.addAction(settings_act)

        # Clear (only if a profile is loaded).
        if self.radio.has_captured_profile():
            menu.addSeparator()
            clear_act = QAction(
                f"Clear loaded profile "
                f"({self.radio.active_captured_profile_name})", menu)
            clear_act.triggered.connect(self._on_clear_captured_profile)
            menu.addAction(clear_act)

        menu.exec(btn.mapToGlobal(pos))

    def _on_noise_capture_done(self, verdict: str) -> None:
        """Slot for ``Radio.noise_capture_done`` — fires when a
        capture finalizes inside NR.

        Stops the progress poll (the timer would have stopped on
        next tick anyway), refreshes the button, then opens a
        Save-As dialog so the operator can name and persist the
        profile.  If smart-guard verdict is "suspect", the dialog
        starts with a warning banner.
        """
        self._nr_cap_poll.stop()
        self._refresh_nr_capture_button()
        self._prompt_save_captured_profile(verdict)

    def _on_noise_active_profile_changed(self, name: str) -> None:
        """Slot for ``Radio.noise_active_profile_changed`` — fires
        when a profile is loaded or cleared.  We refresh the
        Capture button tooltip + the NR button tooltip so hover
        text reflects the new state."""
        self._refresh_nr_capture_button()

    def _on_noise_profile_stale(self, drift_db: float) -> None:
        """Slot for ``Radio.noise_profile_stale`` — fires once per
        stale event when the loaded captured profile no longer
        matches the live noise floor.

        Shows a status-bar toast with the drift value plus a hint
        that the operator may want to recapture.  At-most-one fire
        per stale event with hysteresis-based rearm (see Radio
        signal docstring).
        """
        try:
            from PySide6.QtWidgets import QMainWindow
            mw = self.window()
            if mw is None:
                return
            # 12-second toast — long enough to read but not annoying.
            mw.statusBar().showMessage(
                f"⚠  Noise profile drifted {drift_db:.1f} dB from "
                f"current band conditions — consider recapturing.",
                12000)
        except Exception as exc:
            print(f"[panels] could not show staleness toast: {exc}")
        # Also refresh the NR button tooltip via the existing path.
        self._on_nr_profile_changed(self.radio.nr_profile)

    def _prompt_save_captured_profile(self, verdict: str = "") -> None:
        """Open a save-as dialog after a capture finalizes."""
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        # Default name = "<band> <YYYY-MM-DD HH:MM>" — operator can
        # accept or edit.
        from datetime import datetime
        try:
            from lyra.bands import band_for_freq_hz
            band = band_for_freq_hz(int(self.radio._freq_hz)) or ""
        except Exception:
            band = ""
        mode = ""
        try:
            mode = str(getattr(self.radio, "mode", "") or "")
        except Exception:
            pass
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        default_name = f"{band} {ts}".strip()

        # Dialog title carries the band/mode metadata so the
        # operator sees what's about to be stamped into the JSON
        # without having to look at the radio panel.
        title_bits = ["Save captured noise profile"]
        if band or mode:
            ctx_bits = [b for b in (band, mode) if b]
            title_bits.append(f"— {' '.join(ctx_bits)}")
        title = "  ".join(title_bits)

        # Plain capture-complete prompt — pre-smart-guard flow
        # restored in v0.0.9.5 after the guard was decommissioned.
        # Operator's ear + waterfall during capture are the actual
        # filter.  No verdict, no warnings, no branching — just
        # name and save.  ``verdict`` arg retained for slot-signal
        # compatibility but ignored.
        prompt = "Capture complete.  Save as:"
        name, ok = _prompt_profile_name(
            self, title, prompt, default_name)
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        try:
            self.radio.save_current_capture_as(name, overwrite=False)
            self.radio.status_message.emit(
                f"Saved noise profile: {name}", 4000)
            # Auto-flip the NR source toggle to "captured" — the
            # operator just captured and saved a profile, almost
            # certainly wants the next audio block to use it.  NR
            # aggression profile (Light/Medium/Heavy) stays as
            # operator had it; only the source flips.
            self.radio.set_nr_use_captured_profile(True)
        except FileExistsError:
            # Re-prompt with overwrite confirmation.
            ans = QMessageBox.question(
                self, "Overwrite existing profile?",
                f"A profile named {name!r} already exists.  "
                f"Overwrite it?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No)
            if ans == QMessageBox.StandardButton.Yes:
                try:
                    self.radio.save_current_capture_as(
                        name, overwrite=True)
                    self.radio.status_message.emit(
                        f"Saved noise profile: {name}", 4000)
                    self.radio.set_nr_use_captured_profile(True)
                except Exception as exc:
                    QMessageBox.warning(
                        self, "Save failed", str(exc))
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    def _on_clear_captured_profile(self) -> None:
        """Drop the loaded profile.  Radio.clear_captured_profile()
        also flips the noise-source toggle back to Live so the
        operator's NR aggression profile (Light/Medium/Heavy)
        keeps working with the live VAD estimate."""
        self.radio.clear_captured_profile()

    # ── NR2 panel-slider handlers — Phase 3.D #4 ─────────────────────

    def _on_nr2_agg_slider(self, slider_int: int) -> None:
        """Operator dragged the NR2 strength slider."""
        agg = slider_int / 100.0
        self.nr2_agg_label.setText(f"{slider_int} %")
        self.radio.set_nr2_aggression(agg)

    def _on_nr2_agg_signal(self, agg: float) -> None:
        """Mirror an external aggression change (e.g. from Settings
        → Noise tab) into the panel slider."""
        target = int(round(agg * 100))
        if self.nr2_agg_slider.value() != target:
            self.nr2_agg_slider.blockSignals(True)
            self.nr2_agg_slider.setValue(target)
            self.nr2_agg_slider.blockSignals(False)
        self.nr2_agg_label.setText(f"{target} %")

    # ── LMS handlers ──────────────────────────────────────────────

    def _on_lms_strength_slider(self, slider_int: int) -> None:
        """Operator dragged the LMS strength slider."""
        v = slider_int / 100.0
        self.lms_strength_label.setText(f"{slider_int} %")
        self.radio.set_lms_strength(v)

    def _on_lms_strength_signal(self, value: float) -> None:
        """Mirror an external LMS strength change into the slider."""
        target = int(round(value * 100))
        if self.lms_strength_slider.value() != target:
            self.lms_strength_slider.blockSignals(True)
            self.lms_strength_slider.setValue(target)
            self.lms_strength_slider.blockSignals(False)
        self.lms_strength_label.setText(f"{target} %")

    def _on_lms_enabled_changed(self, on: bool) -> None:
        """Show/hide the LMS strength slider when LMS toggles."""
        self._lms_label_widget.setVisible(on)
        self.lms_strength_slider.setVisible(on)
        self.lms_strength_label.setVisible(on)

    # ── Squelch handlers ──────────────────────────────────────────

    def _on_sq_threshold_slider(self, slider_int: int) -> None:
        """Operator dragged the squelch threshold slider."""
        v = slider_int / 100.0
        self.sq_threshold_label.setText(f"{slider_int}")
        self.radio.set_squelch_threshold(v)

    def _on_sq_threshold_signal(self, value: float) -> None:
        """Mirror an external threshold change into the slider."""
        target = int(round(value * 100))
        if self.sq_threshold_slider.value() != target:
            self.sq_threshold_slider.blockSignals(True)
            self.sq_threshold_slider.setValue(target)
            self.sq_threshold_slider.blockSignals(False)
        self.sq_threshold_label.setText(f"{target}")

    def _on_sq_enabled_changed(self, on: bool) -> None:
        """Show/hide the squelch slider + activity dot when the
        operator toggles SQ on/off, and start/stop the activity
        polling timer."""
        self._sq_label_widget.setVisible(on)
        self.sq_threshold_slider.setVisible(on)
        self.sq_threshold_label.setVisible(on)
        self.sq_activity_dot.setVisible(on)
        if on:
            self._sq_activity_timer.start()
        else:
            self._sq_activity_timer.stop()
            # Reset dot to neutral grey when squelch disabled.
            self.sq_activity_dot.setStyleSheet(
                "color: #303030; font-size: 14px;")

    def _refresh_sq_activity_dot(self) -> None:
        """Update the activity dot color based on whether the
        squelch is currently passing audio.  Called by the
        polling timer at 10 Hz."""
        passing = bool(self.radio.squelch_passing)
        # Green when passing, dark grey when muted.
        color = "#3aa64a" if passing else "#404040"
        self.sq_activity_dot.setStyleSheet(
            f"color: {color}; font-size: 14px;")

    def _show_sq_menu(self, pos):
        """Right-click on the SQ button — threshold preset shortcuts.

        Direct operator UX — no need to drag the slider when one of
        the standard zones is what you want.  Threshold values
        match the AllModeSquelch.set_threshold docstring.
        """
        btn = self.dsp_btns["SQ"]
        menu = QMenu(self)
        current = self.radio.squelch_threshold
        for label, value in (
                ("Off    (0)   — squelch always open", 0.0),
                ("Loose  (10)  — barely-on, opens on faintest signal", 0.10),
                ("Default (20) — voice-friendly default", 0.20),
                ("Medium (40)  — mutes on quiet bands", 0.40),
                ("Tight  (60)  — strong signals only", 0.60),
        ):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(abs(current - value) < 1e-3)
            act.triggered.connect(
                lambda _=False, v=value:
                    self.radio.set_squelch_threshold(v))
            menu.addAction(act)
        menu.addSeparator()
        toggle_label = ("Disable Squelch" if self.radio.squelch_enabled
                        else "Enable Squelch")
        toggle_act = QAction(toggle_label, menu)
        toggle_act.triggered.connect(
            lambda: self.radio.set_squelch_enabled(
                not self.radio.squelch_enabled))
        menu.addAction(toggle_act)
        menu.exec(btn.mapToGlobal(pos))

    def _show_nr2_method_menu(self, pos):
        """Right-click on the NR2 strength slider — gain function
        picker.  Both gain methods come from WDSP emnr.c with
        Pratt attribution.  See nr2.py docstring for the math.
        """
        menu = QMenu(self)
        current = self.radio.nr2_gain_method
        for key, label in (
                ("mmse_lsa",
                 "MMSE-LSA  (default — classical, sharper attack)"),
                ("wiener",
                 "Wiener     (smoother transitions, fuller residue)"),
        ):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(key == current)
            act.triggered.connect(
                lambda _=False, k=key: self.radio.set_nr2_gain_method(k))
            menu.addAction(act)
        menu.exec(self.nr2_agg_slider.mapToGlobal(pos))

    # ── NR1 strength-slider handlers ─────────────────────────────────

    def _on_nr1_strength_slider(self, slider_int: int) -> None:
        """Operator dragged the NR1 strength slider."""
        s = slider_int / 100.0
        self.nr1_strength_label.setText(f"{slider_int} %")
        self.radio.set_nr1_strength(s)

    def _on_nr1_strength_signal(self, strength: float) -> None:
        """LEGACY — was used to mirror NR1 strength changes into the
        slider.  Now the slider drives Mode (1..4) instead of strength,
        so this handler is a no-op kept for connect() compatibility.
        Operator strength changes via CAT/TCI still update Radio
        state for legacy mode but don't reflect in the new mode UI."""
        return

    # ── NR Mode + AEPF handlers (NR-UX overhaul 2026-05-07) ─────
    def _on_nr_mode_slider(self, value: int) -> None:
        """Operator dragged the Mode slider — push to Radio."""
        try:
            mode = int(max(1, min(4, value)))
            self.nr1_strength_label.setText(f"{mode}")
            self.radio.set_nr_mode(mode)
        except Exception as exc:
            print(f"[panels] mode slider error: {exc}")

    def _on_nr_mode_signal(self, mode: int) -> None:
        """Mirror an external NR mode change (autoload, CAT) into the
        slider widget."""
        try:
            mode = int(max(1, min(4, mode)))
            if self.nr1_strength_slider.value() != mode:
                self.nr1_strength_slider.blockSignals(True)
                self.nr1_strength_slider.setValue(mode)
                self.nr1_strength_slider.blockSignals(False)
            self.nr1_strength_label.setText(f"{mode}")
        except Exception:
            pass

    def _on_aepf_checkbox(self, checked: bool) -> None:
        """Operator toggled the AEPF checkbox."""
        try:
            self.radio.set_aepf_enabled(bool(checked))
        except Exception as exc:
            print(f"[panels] AEPF checkbox error: {exc}")

    def _on_aepf_enabled_signal(self, enabled: bool) -> None:
        """Mirror an external AEPF state change into the checkbox."""
        try:
            current = self.aepf_checkbox.isChecked()
            if current != bool(enabled):
                self.aepf_checkbox.blockSignals(True)
                self.aepf_checkbox.setChecked(bool(enabled))
                self.aepf_checkbox.blockSignals(False)
        except Exception:
            pass

    def _on_npe_combo(self, index: int) -> None:
        """Operator picked an NPE method from the dropdown."""
        try:
            method = int(self.npe_combo.itemData(index))
            self.radio.set_npe_method(method)
        except Exception as exc:
            print(f"[panels] NPE combo error: {exc}")

    def _on_npe_method_signal(self, method: int) -> None:
        """Mirror an external NPE method change into the dropdown
        (autoload, CAT, etc.)."""
        try:
            method = int(method)
            # Find the index for this method value.
            for i in range(self.npe_combo.count()):
                if int(self.npe_combo.itemData(i)) == method:
                    if self.npe_combo.currentIndex() != i:
                        self.npe_combo.blockSignals(True)
                        self.npe_combo.setCurrentIndex(i)
                        self.npe_combo.blockSignals(False)
                    break
        except Exception:
            pass

    def _refresh_nr2_panel_visibility(self, profile: str | None = None) -> None:
        """LEGACY method — now a no-op-ish guard.

        Pre-2026-05-07 this toggled NR1 vs NR2 strength slider
        visibility based on backend.  After the NR-mode UX overhaul,
        the Mode slider is always visible and the legacy NR2 widgets
        are not added to any layout — calling setVisible(True) on
        them would promote them to top-level windows (Qt default
        behavior for parentless widgets), causing the "floating
        widget mess" bug operators reported.
        """
        # Mode slider always visible.
        self._nr1_label_widget.setVisible(True)
        self.nr1_strength_slider.setVisible(True)
        self.nr1_strength_label.setVisible(True)
        # Legacy NR2 widgets stay hidden permanently — never call
        # setVisible(True) on them, they'd float as windows.
        self._nr2_label_widget.setVisible(False)
        self.nr2_agg_slider.setVisible(False)
        self.nr2_agg_label.setVisible(False)

    # ── NB (Noise Blanker) handlers — Phase 3.D #2 ───────────────────

    _NB_PROFILE_LABELS = {
        "off":        "Off",
        "light":      "Light",
        "medium":     "Medium",
        "heavy":      "Heavy",
        # "custom" handled inline (label includes the threshold value)
    }

    def _on_nb_btn_toggled(self, checked: bool) -> None:
        """Left-click on the NB button toggles between Off and the
        operator's last non-Off profile.  If they've never picked
        a non-Off profile yet, defaults to Medium.

        Right-click is the full profile picker — see _show_nb_menu.
        """
        if checked:
            target = self._nb_last_active_profile
            if target == "off":
                target = "medium"
            self.radio.set_nb_profile(target)
        else:
            self.radio.set_nb_profile("off")

    def _show_nb_menu(self, pos):
        """Right-click on the NB button — profile picker."""
        btn = self.dsp_btns["NB"]
        menu = QMenu(self)
        current = self.radio.nb_profile
        for key in ("off", "light", "medium", "heavy"):
            label = self._NB_PROFILE_LABELS[key]
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(key == current)
            act.triggered.connect(
                lambda _=False, k=key: self.radio.set_nb_profile(k))
            menu.addAction(act)
        # Custom shows the current threshold for context.
        cust_label = "Custom"
        if current == "custom":
            cust_label = (f"Custom  (threshold = "
                          f"{self.radio.nb_threshold:.1f}×)")
        cust_act = QAction(cust_label, menu)
        cust_act.setCheckable(True)
        cust_act.setChecked(current == "custom")
        # Custom is opened via the Settings → Noise tab (where the
        # operator gets a slider for the threshold value), not
        # directly settable from this menu.
        cust_act.triggered.connect(self._open_noise_settings)
        menu.addAction(cust_act)
        menu.addSeparator()
        settings_act = QAction("Open Noise settings…", menu)
        settings_act.triggered.connect(self._open_noise_settings)
        menu.addAction(settings_act)
        menu.exec(btn.mapToGlobal(pos))

    def _on_nb_profile_changed(self, name: str) -> None:
        """Slot for ``Radio.nb_profile_changed``.  Sync the NB
        button's checked state with whether the profile is
        non-Off."""
        if name != "off":
            # Operator picked a real profile — remember it for the
            # next left-click toggle.
            self._nb_last_active_profile = name
        nb_btn = self.dsp_btns["NB"]
        target_checked = (name != "off")
        if nb_btn.isChecked() != target_checked:
            nb_btn.blockSignals(True)
            nb_btn.setChecked(target_checked)
            nb_btn.blockSignals(False)
        # Refresh tooltip so hover reflects active profile.
        label = self._NB_PROFILE_LABELS.get(
            name, f"Custom ({self.radio.nb_threshold:.1f}×)")
        nb_btn.setToolTip(
            f"Noise Blanker — IQ-domain impulse suppression.\n"
            f"  Profile: {label}\n"
            f"\n"
            f"Left-click: toggle on/off.\n"
            f"Right-click: pick profile or open Noise settings.")

    # ── ANF (Auto Notch Filter) handlers — Phase 3.D #3 ─────────────

    _ANF_PROFILE_LABELS = {
        "off":        "Off",
        "light":      "Light",
        "medium":     "Medium",
        "heavy":      "Heavy",
        # "custom" handled inline (label includes mu)
    }

    def _on_anf_btn_toggled(self, checked: bool) -> None:
        """Left-click on the ANF button toggles between Off and
        the operator's last non-Off profile (default Medium)."""
        if checked:
            target = self._anf_last_active_profile
            if target == "off":
                target = "medium"
            self.radio.set_anf_profile(target)
        else:
            self.radio.set_anf_profile("off")

    def _show_lms_menu(self, pos):
        """Right-click on the LMS button — strength presets.

        Mirrors the NR1 strength UX: Light / Medium / Heavy as
        slider-position shortcuts, plus a settings link.  No
        per-profile DSP-parameter table here — the strength slider
        smoothly interpolates 2μ and γ between Pratt's empirically-
        tuned bounds.
        """
        btn = self.dsp_btns["LMS"]
        menu = QMenu(self)
        current = self.radio.lms_strength
        for label, value in (
                ("Light  (slider 0.0)", 0.0),
                ("Medium (slider 0.5)  — Pratt default", 0.5),
                ("Heavy  (slider 1.0)", 1.0),
        ):
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(abs(current - value) < 1e-3)
            act.triggered.connect(
                lambda _=False, v=value: self.radio.set_lms_strength(v))
            menu.addAction(act)
        menu.addSeparator()
        toggle_label = ("Disable LMS" if self.radio.lms_enabled
                        else "Enable LMS")
        toggle_act = QAction(toggle_label, menu)
        toggle_act.triggered.connect(
            lambda: self.radio.set_lms_enabled(
                not self.radio.lms_enabled))
        menu.addAction(toggle_act)
        menu.exec(btn.mapToGlobal(pos))

    def _show_anf_menu(self, pos):
        """Right-click on the ANF button — profile picker."""
        btn = self.dsp_btns["ANF"]
        menu = QMenu(self)
        current = self.radio.anf_profile
        for key in ("off", "light", "medium", "heavy"):
            label = self._ANF_PROFILE_LABELS[key]
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(key == current)
            act.triggered.connect(
                lambda _=False, k=key: self.radio.set_anf_profile(k))
            menu.addAction(act)
        cust_label = "Custom"
        if current == "custom":
            cust_label = (f"Custom  (μ = {self.radio.anf_mu:.1e})")
        cust_act = QAction(cust_label, menu)
        cust_act.setCheckable(True)
        cust_act.setChecked(current == "custom")
        # Custom is set via the Settings → Noise tab slider.
        cust_act.triggered.connect(self._open_noise_settings)
        menu.addAction(cust_act)
        menu.addSeparator()
        settings_act = QAction("Open Noise settings…", menu)
        settings_act.triggered.connect(self._open_noise_settings)
        menu.addAction(settings_act)
        menu.exec(btn.mapToGlobal(pos))

    def _on_anf_profile_changed(self, name: str) -> None:
        """Slot for ``Radio.anf_profile_changed``.  Sync the ANF
        button's checked state."""
        if name != "off":
            self._anf_last_active_profile = name
        anf_btn = self.dsp_btns["ANF"]
        target_checked = (name != "off")
        if anf_btn.isChecked() != target_checked:
            anf_btn.blockSignals(True)
            anf_btn.setChecked(target_checked)
            anf_btn.blockSignals(False)
        label = self._ANF_PROFILE_LABELS.get(
            name, f"Custom (μ={self.radio.anf_mu:.1e})")
        anf_btn.setToolTip(
            f"Auto Notch Filter — LMS adaptive notch.\n"
            f"  Profile: {label}\n"
            f"\n"
            f"Left-click: toggle on/off.\n"
            f"Right-click: pick profile or open Noise settings.")

    # ── NR noise-source badge (Phase 3.D #1) ─────────────────────────

    def _on_nr_source_badge_clicked(self) -> None:
        """Click on the inline badge — toggle Live ⇄ Captured.

        Disabled in stylesheet+setEnabled when no profile is loaded,
        so this slot only fires when a profile exists.  Belt-and-
        suspenders: re-check has_captured_profile() and bail if
        somehow the click landed without one (e.g., race during
        clear)."""
        if not self.radio.has_captured_profile():
            return
        self.radio.set_nr_use_captured_profile(
            not self.radio.nr_use_captured_profile)

    def _refresh_nr_source_badge(self) -> None:
        """Repaint the inline badge to match current state.

        States the badge can show:
        - No profile loaded:
            "Source: Live (VAD)  ·  no captured profile loaded"
            (greyed, not clickable)
        - Profile loaded, source = Live:
            "Source: Live (VAD)  ⇄  click to use captured: Powerline 80m"
        - Profile loaded, source = Captured:
            "Source: Captured: Powerline 80m  ·  3d · 80m LSB  ⇄"
            (text colored per age threshold; ⚠ if mode mismatch)
        """
        from PySide6.QtCore import QSettings
        badge = self.nr_source_badge
        has_cap = self.radio.has_captured_profile()
        use_cap = self.radio.nr_use_captured_profile
        meta = self.radio.active_captured_profile_meta or {}
        cap_name = self.radio.active_captured_profile_name

        if not has_cap:
            # No profile — Live source is forced; badge is
            # informational only.  Two extra spaces after the
            # emoji avoid visual clipping in Qt's emoji metrics.
            badge.setEnabled(False)
            badge.setText("🔵   Live (VAD)   ·   no captured profile")
            badge.setToolTip(
                "Noise Reduction source: Live (VAD-tracked estimate).\n\n"
                "Capture a noise profile (📷 Cap button) to unlock "
                "the Captured source option.\n\n"
                "Right-click the NR button to change subtraction "
                "strength (Light / Medium / Heavy).")
            return

        # A profile is loaded — badge is clickable to flip source.
        badge.setEnabled(True)
        if not use_cap:
            # Live source, but a profile is loaded and ready.
            badge.setText(f"🔵   Live (VAD)   ⇄   use: {cap_name}")
            badge.setToolTip(
                "Noise Reduction source: Live (VAD-tracked estimate).\n\n"
                f"Click to switch to the loaded captured profile "
                f"{cap_name!r}.\n\n"
                "Right-click the NR button to change subtraction "
                "strength.")
            return

        # Captured source active.  Show name + age + band/mode +
        # mismatch warning.  Resolve age coloring from QSettings
        # thresholds.
        s = QSettings("N8SDR", "Lyra")
        amber_h = int(s.value("noise/age_amber_hours", 24, type=int))
        red_d = int(s.value("noise/age_red_days", 7, type=int))

        age_text, age_color = self._format_profile_age(
            meta.get("captured_at_iso", ""), amber_h, red_d)
        band_mode = self._format_profile_band_mode(meta)
        # Mode-mismatch warning glyph.
        cap_mode = str(meta.get("mode", "")).strip()
        cur_mode = str(self.radio.mode).strip() if hasattr(
            self.radio, "mode") else ""
        mismatch = (cap_mode and cur_mode
                    and cap_mode.lower() != cur_mode.lower())

        # Lead with three spaces after the emoji to avoid clipping.
        # Drop the "Source:" / "Captured:" prefixes — the green dot
        # plus profile name already conveys the source state, and
        # those prefixes were inflating the badge length unnecessarily.
        bits = [f"🟢   {cap_name}"]
        if age_text:
            bits.append(age_text)
        if band_mode:
            bits.append(band_mode)
        if mismatch:
            bits.append(f"⚠ captured on {cap_mode}")
        bits.append("⇄")
        badge.setText("  ·  ".join(bits))

        # Apply age coloring via inline stylesheet override.  Same
        # bumped left padding as the default stylesheet so the dot
        # doesn't clip against the rounded edge.
        badge.setStyleSheet(
            f"QPushButton#nr_source_badge {{"
            f"  text-align: left;"
            f"  padding: 4px 10px 4px 14px;"
            f"  border: 1px solid transparent;"
            f"  border-radius: 4px;"
            f"  font-family: 'Segoe UI', sans-serif;"
            f"  font-size: 11px;"
            f"  color: {age_color};"
            f"}}"
            f"QPushButton#nr_source_badge:hover:!disabled {{"
            f"  background-color: rgba(80, 208, 255, 0.10);"
            f"  border-color: rgba(80, 208, 255, 0.35);"
            f"}}")

        tooltip_lines = [
            f"Noise Reduction source: Captured profile {cap_name!r}.",
            "",
            "Click to switch back to the live VAD-tracked estimate.",
            "",
            "Right-click the NR button to change subtraction "
            "strength (Light / Medium / Heavy).",
        ]
        if mismatch:
            tooltip_lines.append(
                f"\n⚠  This profile was captured on {cap_mode} "
                f"but you're currently on {cur_mode}.\n"
                f"NR will still subtract the captured noise, but "
                f"the model may not perfectly match your current "
                f"audio chain.")
        badge.setToolTip("\n".join(tooltip_lines))

    def _format_profile_age(
            self, captured_at_iso: str,
            amber_hours: int, red_days: int
            ) -> tuple[str, str]:
        """Returns ('3 days old', '#ffb84a') style tuple.

        Color rule matches the manager dialog's:
        - <amber_hours: grey
        - amber_hours .. red_days*24h: amber
        - >red_days: red
        """
        from datetime import datetime, timezone
        if not captured_at_iso:
            return ("", "#cdd9e5")
        try:
            iso = captured_at_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return ("", "#cdd9e5")
        delta = datetime.now(timezone.utc) - dt
        hours = delta.total_seconds() / 3600.0
        if hours < 1.0:
            text = "just now"
        elif hours < 24:
            text = f"{int(hours)}h old"
        else:
            days = hours / 24.0
            if days < 7:
                text = f"{int(days)}d old"
            else:
                text = f"{int(days)}d old"
        if hours > red_days * 24:
            return (text, "#ff6060")
        if hours > amber_hours:
            return (text, "#ffb84a")
        return (text, "#cdd9e5")

    def _format_profile_band_mode(self, meta: dict) -> str:
        """Resolve '80m LSB' / '40m USB' / etc. for badge display."""
        mode = str(meta.get("mode", "")).strip()
        freq_hz = int(meta.get("freq_hz", 0))
        try:
            from lyra.bands import band_for_freq_hz
            band = band_for_freq_hz(freq_hz) or ""
        except Exception:
            band = ""
        if band and mode:
            return f"{band} {mode}"
        if mode:
            return mode
        if freq_hz > 0:
            return f"{freq_hz/1e6:.3f} MHz"
        return ""

    def _open_noise_profile_manager(self) -> None:
        """Open the Manage Profiles dialog (created in Day 3 piece 3)."""
        try:
            from lyra.ui.noise_profile_manager import NoiseProfileManager
        except ImportError:
            # Manager dialog not yet wired — fall back to the
            # noise-settings shortcut so the operator at least
            # reaches a profile-related place.
            self._open_noise_settings()
            return
        dlg = NoiseProfileManager(self.radio, parent=self.window())
        dlg.exec()

    def _open_noise_settings(self) -> None:
        """Open Settings on the Noise tab."""
        mw = self.window()
        if hasattr(mw, "_open_settings"):
            try:
                mw._open_settings(tab="Noise")
            except Exception:
                # Tab may not exist yet during a partial migration —
                # fall back to DSP, which still has NR placeholders.
                mw._open_settings(tab="DSP")

    def _on_nr_enabled_changed(self, on: bool):
        btn = self.dsp_btns["NR"]
        if btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)

    def _on_nr_profile_changed(self, name: str):
        """Update the NR button's text + tooltip to reflect the
        active backend + noise source.

        Button text:
        - "NR"   when NR1 (classical spectral subtraction) or the
                 neural placeholder is active
        - "NR2"  when the Ephraim-Malah MMSE-LSA processor is
                 active — operators see at a glance which
                 algorithm is running.

        Strength is shown via the inline slider next to the button,
        so the tooltip just names the backend now.
        """
        # NR-UX overhaul (2026-05-07): button text is always "NR".
        # Mode (1..4) and AEPF state shown in tooltip; right-click
        # offers mode picker + AEPF toggle.  No more NR1/NR2 backend
        # switching from the button — that concept is gone.
        mode = int(getattr(self.radio, "nr_mode", 3))
        aepf_on = bool(getattr(self.radio, "aepf_enabled", True))
        if (self.radio.nr_use_captured_profile
                and self.radio.has_captured_profile()):
            source = (f"Captured: "
                      f"{self.radio.active_captured_profile_name}")
        else:
            source = "Live (VAD)"
        nr_btn = self.dsp_btns["NR"]
        nr_btn.setText("NR")
        nr_btn.setToolTip(
            f"Noise Reduction\n"
            f"  Mode:    {mode}  (1=Wiener+SPP, 2=Wiener simple, "
            f"3=MMSE-LSA, 4=Trained adaptive)\n"
            f"  AEPF:    {'on' if aepf_on else 'off'}  "
            f"(anti-musical-noise post-filter)\n"
            f"  Source:  {source}\n"
            f"\n"
            f"Left-click: toggle NR on/off.\n"
            f"Right-click: pick mode / toggle AEPF.\n"
            f"Drag the Mode slider to switch modes.")

    # ── APF button handlers ────────────────────────────────────────
    def _show_apf_menu(self, pos):
        """Right-click on the APF button pops a quick-access menu
        for BW and Gain. The full slider UI lives in Settings → DSP
        → CW; this is just for fast on-the-air tweaks without
        opening the dialog. BW/gain entries are radio buttons
        showing the current value at the top, then a few common
        presets the operator can pick directly."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        from lyra.dsp.apf import AudioPeakFilter as _APF
        btn = self.dsp_btns["APF"]
        menu = QMenu(self)

        # Toggle at the top.
        toggle_act = QAction(
            "Disable APF" if self.radio.apf_enabled else "Enable APF",
            menu)
        toggle_act.triggered.connect(
            lambda: self.radio.set_apf_enabled(not self.radio.apf_enabled))
        menu.addAction(toggle_act)
        menu.addSeparator()

        # Bandwidth presets — narrow / medium / wide. Current value
        # gets the radio-button check.
        bw_menu = menu.addMenu(f"Bandwidth ({self.radio.apf_bw_hz} Hz)")
        cur_bw = int(self.radio.apf_bw_hz)
        for bw in (40, 60, 80, 100, 150):
            act = QAction(f"{bw} Hz", bw_menu)
            act.setCheckable(True)
            act.setChecked(bw == cur_bw)
            act.triggered.connect(
                lambda _=False, v=bw: self.radio.set_apf_bw_hz(v))
            bw_menu.addAction(act)

        # Gain presets — gentle / standard / strong.
        gain_menu = menu.addMenu(
            f"Gain (+{int(self.radio.apf_gain_db)} dB)")
        cur_g = int(self.radio.apf_gain_db)
        for g in (6, 9, 12, 15, 18):
            act = QAction(f"+{g} dB", gain_menu)
            act.setCheckable(True)
            act.setChecked(g == cur_g)
            act.triggered.connect(
                lambda _=False, v=g: self.radio.set_apf_gain_db(float(v)))
            gain_menu.addAction(act)

        menu.addSeparator()
        more_act = QAction("More settings…", menu)
        more_act.triggered.connect(self._open_dsp_settings_at_cw)
        menu.addAction(more_act)
        menu.exec(btn.mapToGlobal(pos))

    def _open_dsp_settings_at_cw(self):
        """Pop Settings → DSP. The CW group sits inside the DSP tab
        and includes APF, so the operator lands close to the full
        APF controls (BW slider + gain slider). Same call path used
        by the SDR-cal panel's right-click for Visuals."""
        mw = self.window()
        if hasattr(mw, "_open_settings"):
            mw._open_settings(tab="DSP")
        else:
            try:
                self.radio.status_message.emit(
                    "APF: open File → DSP… for full BW / Gain controls",
                    3000)
            except Exception:
                pass

    def _on_apf_enabled_changed(self, on: bool):
        btn = self.dsp_btns["APF"]
        if btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)
        self._refresh_apf_tooltip()

    def _refresh_apf_tooltip(self):
        """Compose the APF button tooltip from current Radio state.
        Shows BW + Gain numerics, plus a CW-mode hint if the radio is
        currently on a non-CW mode (so the operator knows why
        toggling the button doesn't audibly do anything right now)."""
        btn = self.dsp_btns.get("APF")
        if btn is None:
            return
        bw = int(self.radio.apf_bw_hz)
        gain = int(self.radio.apf_gain_db)
        is_cw = self.radio.mode in ("CWU", "CWL")
        mode_hint = "" if is_cw else (
            "\n\nCurrent mode is not CW — APF stays armed but only\n"
            "audibly affects audio in CWU / CWL.")
        btn.setToolTip(
            f"Audio Peaking Filter — narrow CW boost.\n"
            f"BW {bw} Hz, +{gain} dB at the CW pitch.\n"
            "Left-click: toggle on/off.\n"
            "Right-click: quick BW / Gain presets, or open Settings."
            f"{mode_hint}")

    # ── BIN button handlers ────────────────────────────────────────
    def _show_bin_menu(self, pos):
        """Right-click on BIN pops a depth-preset menu. Useful for
        on-the-air tuning without opening Settings. Current depth
        is checked so the operator always sees where they are."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        btn = self.dsp_btns["BIN"]
        menu = QMenu(self)

        toggle_act = QAction(
            "Disable BIN" if self.radio.bin_enabled else "Enable BIN",
            menu)
        toggle_act.triggered.connect(
            lambda: self.radio.set_bin_enabled(not self.radio.bin_enabled))
        menu.addAction(toggle_act)
        menu.addSeparator()

        depth_menu = menu.addMenu(
            f"Depth ({int(round(self.radio.bin_depth * 100))} %)")
        cur_pct = int(round(self.radio.bin_depth * 100))
        for pct in (25, 50, 70, 85, 100):
            act = QAction(f"{pct} %", depth_menu)
            act.setCheckable(True)
            act.setChecked(pct == cur_pct)
            act.triggered.connect(
                lambda _=False, v=pct:
                self.radio.set_bin_depth(float(v) / 100.0))
            depth_menu.addAction(act)

        menu.addSeparator()
        more_act = QAction("More settings…", menu)
        more_act.triggered.connect(self._open_dsp_settings_at_cw)
        menu.addAction(more_act)
        menu.exec(btn.mapToGlobal(pos))

    def _on_bin_enabled_changed(self, on: bool):
        btn = self.dsp_btns["BIN"]
        if btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)
        self._refresh_bin_tooltip()

    def _refresh_bin_tooltip(self):
        """Tooltip live-updates with current depth so hover always
        reflects the active setting."""
        btn = self.dsp_btns.get("BIN")
        if btn is None:
            return
        pct = int(round(self.radio.bin_depth * 100))
        btn.setToolTip(
            f"Binaural pseudo-stereo — Hilbert phase-split for headphones.\n"
            f"Depth {pct} % (0 % = mono, 100 % = full spatial pair).\n"
            "Left-click: toggle on/off.\n"
            "Right-click: pick depth, or open Settings.")

    def _on_notches_changed(self, items):
        # items is list[(freq_hz, width_hz, active, deep)]. Compact
        # counter only — gesture hints live on the NF button's
        # tooltip. Shows widths in Hz so shape is readable at a
        # glance. Markers:
        #   *  inactive (bypassed, kept for A/B)
        #   ^  deep (cascaded for ~2× attenuation)
        n = len(items)
        if not items:
            self.notch_info.setText("0 notches")
            return
        widths = []
        for _, w, active, deep in items:
            mark = ""
            if not active:
                mark += "*"
            if deep:
                mark += "^"
            widths.append(f"{int(round(w))}{mark}")
        n_off = sum(1 for _, _, a, _ in items if not a)
        n_deep = sum(1 for _, _, _, d in items if d)
        suffix_parts = []
        if n_off:
            suffix_parts.append(f"{n_off} off")
        if n_deep:
            suffix_parts.append(f"{n_deep} deep")
        suffix = f"  ({', '.join(suffix_parts)})" if suffix_parts else ""
        self.notch_info.setText(
            f"{n} notch{'es' if n != 1 else ''}  "
            f"[{', '.join(widths)} Hz]{suffix}")

    def _open_dsp_settings(self):
        """Delegate to the MainWindow's Settings opener, jumping to the
        DSP tab directly."""
        mw = self.window()
        if hasattr(mw, "_open_settings"):
            mw._open_settings(tab="DSP")

    # ── Right-click AGC profile menu ─────────────────────────────────
    # Menu order. "Auto" is a full profile that owns continuous
    # threshold tracking (radio-side timer). "Custom" is settable from
    # the DSP settings tab only (need release + hang values from user).
    _AGC_PROFILES = ("off", "fast", "med", "slow", "auto", "custom")
    _AGC_PROFILE_LABELS = {
        "off":    "Off",
        "fast":   "Fast",
        "med":    "Med",
        "slow":   "Slow",
        "auto":   "Auto",
        "custom": "Custom…",
    }
    # Color the profile label differently so the operator sees at a
    # glance which mode is active. Auto + Custom are "special" (cyan +
    # magenta), static Fast/Med/Slow stay amber, Off is muted gray.
    _AGC_PROFILE_COLORS = {
        "off":    "#8a9aac",   # muted gray — disabled
        "fast":   "#ffab47",   # amber — static fast release
        "med":    "#ffab47",   # amber — static medium release
        "slow":   "#ffab47",   # amber — static slow release
        "auto":   "#00e5ff",   # cyan — actively tracking noise floor
        "custom": "#ff6bcb",   # magenta — user parameters in effect
    }
    _AGC_PROFILE_TEXT = {
        "off":    "OFF",
        "fast":   "FAST",
        "med":    "MED",
        "slow":   "SLOW",
        "auto":   "AUTO",
        "custom": "CUST",
    }

    def _show_agc_menu(self, pos):
        """Pop a context menu listing AGC profiles (checked = current)."""
        sender = self.sender()
        menu = QMenu(self)
        current = self.radio.agc_profile
        for name in self._AGC_PROFILES:
            label = self._AGC_PROFILE_LABELS[name]
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(name == current)
            if name == "custom":
                # "Custom" needs release + hang values, so route through
                # the DSP settings tab instead of firing directly.
                act.triggered.connect(self._open_dsp_settings)
            else:
                act.triggered.connect(
                    lambda _=False, n=name: self.radio.set_agc_profile(n))
            menu.addAction(act)
        menu.addSeparator()
        settings_act = QAction("DSP settings…", menu)
        settings_act.triggered.connect(self._open_dsp_settings)
        menu.addAction(settings_act)
        menu.exec(sender.mapToGlobal(pos))

    # ── Live AGC readouts ────────────────────────────────────────────
    def _update_agc_profile(self, profile: str):
        key = profile if profile in self._AGC_PROFILE_COLORS else "med"
        color = self._AGC_PROFILE_COLORS[key]
        text = self._AGC_PROFILE_TEXT[key]
        self.agc_profile_lbl.setStyleSheet(
            f"color: {color}; font-weight: 700; min-width: 48px;"
            " letter-spacing: 1px;")
        self.agc_profile_lbl.setText(text)

    def _update_agc_threshold(self, threshold: float):
        import math
        dbfs = 20 * math.log10(max(threshold, 1e-6))
        self.agc_threshold_lbl.setText(f"{dbfs:+.0f} dBFS")

    # Pre-built stylesheets for the three AGC action color buckets — cached
    # so we don't force Qt to reparse CSS on every repaint.
    _AGC_ACTION_STYLES = (
        # bucket 0: green  (|action| < 3 dB — AGC barely doing anything)
        "color: #39ff14; font-family: Consolas, monospace; "
        "font-weight: 700; min-width: 58px;",
        # bucket 1: amber  (3..10 dB — working)
        "color: #ffab47; font-family: Consolas, monospace; "
        "font-weight: 700; min-width: 58px;",
        # bucket 2: red-orange  (>10 dB — hitting hard / strong signal)
        "color: #ff6b35; font-family: Consolas, monospace; "
        "font-weight: 700; min-width: 58px;",
    )

    def _on_agc_action(self, action_db: float):
        """Slot for radio.agc_action_db. Fires every demod block — we
        just track the peak magnitude since last paint here; the timer
        does the actual label update."""
        self._agc_action_last = action_db
        mag = abs(action_db)
        if mag > abs(self._agc_action_peak):
            self._agc_action_peak = action_db

    def _paint_agc_action(self):
        """Paint the accumulated AGC action at timer rate (~6 Hz)."""
        # Show the signed peak magnitude since last paint; decay it toward
        # the latest value so a transient burst shows briefly then settles.
        action_db = self._agc_action_peak
        # Decay peak toward current so the display doesn't get stuck high.
        self._agc_action_peak = 0.6 * self._agc_action_peak + 0.4 * self._agc_action_last
        mag = abs(action_db)
        if mag < 3:
            bucket = 0
        elif mag < 10:
            bucket = 1
        else:
            bucket = 2
        if bucket != self._agc_color_bucket:
            self._agc_color_bucket = bucket
            self.agc_action_lbl.setStyleSheet(self._AGC_ACTION_STYLES[bucket])
        self.agc_action_lbl.setText(f"{action_db:+.1f} dB")


# ── S-Meter panel (wraps the SMeter widget) ─────────────────────────────
class SMeterPanel(GlassPanel):
    """Meter panel with switchable visual style.

    Three meter implementations share the same signal-level input:
      - `LitArcMeter`  (NEW default — analog-curve face with NO needle;
                        a row of LED-style segments lights cumulatively
                        along the arc; click-the-mode-chip switches
                        between S / dBm / AGC scales with per-mode color)
      - `LedBarMeter`  (compact stacked LED bars)
      - `AnalogMeter`  (legacy classic dial with needle — kept as
                        fallback during the LitArcMeter rollout, will be
                        removed once the new meter is settled)

    Operator picks via the small style chip-row in the panel header.
    Choice persists via QSettings (key: meters/style).
    """

    # Stack indices for the three meter styles.
    STYLE_LITARC = "litarc"
    STYLE_LED    = "led"
    STYLE_ANALOG = "analog"
    _STYLE_ORDER = (STYLE_LITARC, STYLE_LED, STYLE_ANALOG)
    _STYLE_LABELS = {
        STYLE_LITARC: "Lit-Arc",
        STYLE_LED:    "LED",
        STYLE_ANALOG: "Analog",
    }

    def __init__(self, radio: Radio, parent=None):
        super().__init__("METERS", parent, help_topic="smeter")
        self.radio = radio

        # Allow this whole panel to shrink horizontally to whatever
        # the meter widgets allow (200 px). Without this explicit min,
        # the parent dock honors the LAYOUT's computed minimum which
        # is dominated by the header chip-row's preferred width — and
        # the operator can't drag the splitter narrower than that.
        self.setMinimumWidth(200)

        # All three meter widgets live in the stack; we just swap visibility.
        self.litarc_meter = LitArcMeter()
        self.led_meter    = LedBarMeter()
        self.analog_meter = AnalogMeter(title="S")

        self.stack = QStackedWidget()
        self.stack.addWidget(self.litarc_meter)   # index 0
        self.stack.addWidget(self.led_meter)      # index 1
        self.stack.addWidget(self.analog_meter)   # index 2
        self.stack.setMinimumWidth(200)

        # Header — style picker as a row of small toggle chips.
        # Compact + the active style is visually obvious without
        # opening a combo, click any chip to switch instantly.
        header = QHBoxLayout()
        header.setSpacing(4)
        self._style_btns: dict[str, QPushButton] = {}
        for key in self._STYLE_ORDER:
            btn = QPushButton(self._STYLE_LABELS[key])
            btn.setCheckable(True)
            # 24 px is the minimum that lets descenders ("g", "y" etc.)
            # plus the QPushButton's internal padding render without
            # clipping the bottom of letters. Old 20 px clipped chrs
            # like "Lit-Arc" / "Analog" — too short.
            btn.setFixedHeight(24)
            btn.setObjectName("dsp_btn")
            # Shrink-friendly: chips report a small minimum so the
            # panel can be docked narrow. Qt elides chip text only as
            # a last resort; with normal panel widths all three labels
            # render in full, but at the absolute narrowest the chip
            # row clips/elides rather than blocking the panel from
            # shrinking.
            btn.setMinimumWidth(0)
            btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            btn.setToolTip(
                f"Switch to the '{self._STYLE_LABELS[key]}' meter style")
            btn.clicked.connect(
                lambda _checked=False, k=key: self.set_style(k))
            header.addWidget(btn)
            self._style_btns[key] = btn
        header.addStretch(1)

        self.content_layout().addLayout(header)
        self.content_layout().addWidget(self.stack)

        # Shared signal wiring — every meter sees every update so the
        # operator can swap styles mid-session without losing any data
        # streams. Also track the latest dBm reading locally so the
        # right-click "Calibrate to current = X" menu can compute the
        # correct offset relative to right-now's reading.
        self._latest_smeter_dbm = -120.0
        radio.smeter_level.connect(self.litarc_meter.set_level_dbfs)
        radio.smeter_level.connect(self.led_meter.set_level_dbfs)
        radio.smeter_level.connect(self.analog_meter.set_level_dbfs)
        radio.smeter_level.connect(self._track_smeter_dbm)
        radio.agc_action_db.connect(self.litarc_meter.set_agc_db)
        radio.freq_changed.connect(self._on_freq_changed)
        radio.mode_changed.connect(self.analog_meter.set_mode)

        # Right-click on the meter stack → calibration menu. Wired on
        # the QStackedWidget so it works regardless of which child
        # meter style is currently active.
        from PySide6.QtCore import Qt as _Qt
        self.stack.setContextMenuPolicy(_Qt.CustomContextMenu)
        self.stack.customContextMenuRequested.connect(
            self._show_smeter_cal_menu)

        self.analog_meter.set_freq_hz(radio.freq_hz)
        self.analog_meter.set_mode(radio.mode)
        self._on_freq_changed(radio.freq_hz)

        # Default to the new lit-arc meter; load_settings() will
        # restore the operator's saved preference before they see it.
        self.set_style(self.STYLE_LITARC)

    @property
    def style(self) -> str:
        for key, btn in self._style_btns.items():
            if btn.isChecked():
                return key
        return self.STYLE_LITARC

    def set_style(self, s: str):
        if s not in self._STYLE_ORDER:
            s = self.STYLE_LITARC
        idx = self._STYLE_ORDER.index(s)
        self.stack.setCurrentIndex(idx)
        for key, btn in self._style_btns.items():
            btn.blockSignals(True)
            btn.setChecked(key == s)
            btn.blockSignals(False)

    def _on_freq_changed(self, hz: int):
        self.analog_meter.set_freq_hz(hz)
        b = band_for_freq(hz)
        self.analog_meter.set_band(b.name if b else "GEN")

    def _track_smeter_dbm(self, dbfs: float):
        """Track the latest meter reading in dBm so the right-click
        cal menu can compute the correct offset. dBfs→dBm uses the
        same conversion the meter widgets do (-19 offset post true-
        dBFS math fix)."""
        self._latest_smeter_dbm = float(dbfs) + (-19.0)

    def _show_smeter_cal_menu(self, pos):
        """Right-click on the meter face → S-meter calibration +
        response-mode menu.

        Sections:
          - Response mode (Peak / Average)
          - Calibrate to a known reference (S9, S5, S3, S1, custom)
          - Reset cal to zero
          - Open Settings → Visuals for sliders

        The "calibrate to" entries call radio.calibrate_smeter_to_dbm
        with the current reading, so the operator just clicks while
        a known-amplitude signal is being received. Common workflow:
          1. Pipe a signal generator at a known dBm into the antenna
          2. Right-click the meter → "Calibrate to current = -73 dBm"
          3. Meter cal trim auto-adjusts so the next reading matches
        """
        from PySide6.QtWidgets import QMenu, QInputDialog
        menu = QMenu(self)
        cur_dbm = self._latest_smeter_dbm
        cur_label = f"current: {cur_dbm:+.1f} dBm  ({self.radio.smeter_mode})"

        info = menu.addAction(cur_label)
        info.setEnabled(False)
        menu.addSeparator()

        # ── Response mode (Peak / Average) ──────────────────────
        # Radio buttons inside a submenu so the active mode is
        # visually obvious.
        mode_menu = menu.addMenu("Response mode")
        cur_mode = self.radio.smeter_mode
        for key, label, tip in (
            ("peak", "Peak (instant integrated power)",
             "Total power summed across all FFT bins inside the RX "
             "passband, no time smoothing. Responsive but jumpy on "
             "transients (CW dits, FT8 tones, lightning crashes)."),
            ("avg",  "Average (smoothed integrated power)",
             "Total power summed across all FFT bins inside the RX "
             "passband, EWMA-smoothed (~1 s at 5 fps). Steadier "
             "reading; better representation of the actual signal "
             "level the AGC sees."),
        ):
            act = mode_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == cur_mode)
            act.setToolTip(tip)
            act.triggered.connect(
                lambda _checked=False, k=key: self.radio.set_smeter_mode(k))

        menu.addSeparator()
        # Quick presets — common references on the IARU S-meter
        # convention (S1 = -121 dBm, 6 dB / S-unit, S9 = -73, +20 = -53).
        for label, target_dbm in (
            ("Calibrate so current reads S9  (-73 dBm)",   -73.0),
            ("Calibrate so current reads S5  (-97 dBm)",   -97.0),
            ("Calibrate so current reads S3  (-109 dBm)", -109.0),
            ("Calibrate so current reads S1  (-121 dBm)", -121.0),
        ):
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, td=target_dbm:
                    self.radio.calibrate_smeter_to_dbm(td, self._latest_smeter_dbm))

        menu.addSeparator()
        custom_act = menu.addAction("Calibrate to specific dBm…")
        def _do_custom():
            value, ok = QInputDialog.getDouble(
                self, "S-meter calibration",
                f"Set the meter to read this many dBm for the "
                f"current signal\n(currently reading "
                f"{self._latest_smeter_dbm:+.1f} dBm):",
                self._latest_smeter_dbm, -150.0, 0.0, 1)
            if ok:
                self.radio.calibrate_smeter_to_dbm(
                    value, self._latest_smeter_dbm)
        custom_act.triggered.connect(_do_custom)

        menu.addSeparator()
        cur_cal = self.radio.smeter_cal_db
        reset = menu.addAction(f"Reset cal to 0 dB  (currently {cur_cal:+.1f})")
        reset.triggered.connect(lambda: self.radio.set_smeter_cal_db(0.0))

        menu.addSeparator()
        open_settings = menu.addAction("Open Visuals settings → cal sliders…")
        # The MainWindow holds the open-settings hook; walk up the
        # parent chain to find it. Falls back to a no-op if for some
        # reason this panel isn't parented to a MainWindow.
        def _open_visuals():
            mw = self.window()
            if hasattr(mw, "_open_settings"):
                mw._open_settings(tab="Visuals")
        open_settings.triggered.connect(_open_visuals)

        menu.exec(self.stack.mapToGlobal(pos))


# ── Notch context-menu builder (shared by spectrum + waterfall) ────────
# Factored so both SpectrumPanel and WaterfallPanel produce an identical
# menu — otherwise the two views would drift every time we tweaked the
# options, which has bitten us before. Kept as a free function rather
# than a method so there's no temptation to subclass one view from the
# other just to share it.
#
# Gating: when the Notch button is OFF, the menu degrades to a single
# "Enable Notch Filter" item. Reasons:
#   1. Right-click is a scarce gesture and we want to reserve it for
#      non-notch features (drag-to-tune hotspot menus, band-plan
#      overlay controls, etc.) when notches aren't the active concern.
#   2. If we let the full menu run while NF is off, add_notch would
#      auto-enable it — surprising behaviour for an operator who
#      intentionally turned it off.
#   3. Existing notches persist while NF is off (DSP just bypasses
#      them — see radio.set_notch_enabled), so re-enabling brings
#      back whatever they had before.
def _notch_preset_name_for(radio, n) -> str:
    """Return the preset key (Normal / Deep / Surgical) that matches
    notch ``n``'s (depth_db, cascade), or 'Custom' if no preset
    matches exactly.

    Threshold: depth_db match within 1 dB (sub-perceptible drift),
    cascade exact.  Used by the right-click menu to show "currently:
    X" for the operator and to mark which preset the notch is on.
    """
    presets = getattr(radio, "NOTCH_PRESETS", {})
    for key, params in presets.items():
        if (int(params["cascade"]) == int(n.cascade)
                and abs(float(params["depth_db"])
                         - float(n.depth_db)) <= 1.0):
            return key.capitalize()
    return "Custom"


def _build_notch_menu(parent_widget, radio, freq_hz: float) -> QMenu:
    menu = QMenu(parent_widget)

    if not radio.notch_enabled:
        # NF off → offer only the enable action so right-click still
        # does something discoverable rather than silently doing
        # nothing. No add/remove/clear here because mutating the
        # notch bank while the feature is supposedly "off" is
        # confusing (even though add_notch auto-enables, the operator
        # explicitly just turned it off).
        hint = QAction(
            "Notch Filter is OFF — turn it on to use notches", menu)
        hint.setEnabled(False)
        menu.addAction(hint)
        menu.addSeparator()
        on_act = QAction("Enable Notch Filter", menu)
        on_act.triggered.connect(
            lambda: radio.set_notch_enabled(True))
        menu.addAction(on_act)
        return menu

    add_act = QAction(f"Add notch at {freq_hz/1e6:.4f} MHz", menu)
    add_act.triggered.connect(lambda: radio.add_notch(float(freq_hz)))
    menu.addAction(add_act)

    have_any = bool(radio.notch_details)

    # If there's a notch near the click, expose per-notch toggles +
    # remove. Lookup tolerance is generous so the operator doesn't
    # need pixel-precise aim.
    nearest_idx = radio._find_nearest_notch_idx(
        float(freq_hz), tolerance_hz=2000.0)
    if nearest_idx is not None:
        nearest = radio._notches[nearest_idx]
        flag_str = []
        if not nearest.active:
            flag_str.append("OFF")
        # v0.0.7.1 notch v2: per-notch flag readout shows the
        # preset that matches its current depth/cascade params if
        # any, otherwise a custom indicator.
        preset_match = _notch_preset_name_for(radio, nearest)
        flag_str.append(preset_match.upper())
        flags = f" — {' / '.join(flag_str)}" if flag_str else ""
        # Active-state toggle
        toggle_label = ("Disable this notch" if nearest.active
                        else "Enable this notch")
        toggle_act = QAction(
            f"{toggle_label}  ({nearest.abs_freq_hz/1e6:.4f} MHz, "
            f"{int(round(nearest.width_hz))} Hz{flags})", menu)
        toggle_act.triggered.connect(
            lambda _=False, f=nearest.abs_freq_hz:
                radio.toggle_notch_active_at(f))
        menu.addAction(toggle_act)

        # v0.0.7.1 notch v2: 3-preset profile submenu.  Replaces the
        # legacy "Make this notch DEEP" toggle with explicit
        # operator-controlled depth + cascade choices.  See
        # notch_v2_design.md sec 7.1 for the operator-facing UX.
        prof_menu = menu.addMenu(
            f"Notch profile  (currently: {preset_match})")
        for preset_key, preset_label, descr in (
            ("normal",
             "Normal",
             "balanced — 2× cascade, -50 dB.  Default for new notches."),
            ("deep",
             "Deep",
             "stronger — 2× cascade, -70 dB.  Stubborn carriers."),
            ("surgical",
             "Surgical",
             "sharp — 4× cascade, -50 dB.  Narrow kill, fast shoulders."),
        ):
            full = f"{preset_label}  —  {descr}"
            mark = "✓  " if preset_key == preset_match else "    "
            act = QAction(mark + full, prof_menu)
            act.triggered.connect(
                lambda _=False, f=nearest.abs_freq_hz, p=preset_key:
                    radio.set_notch_preset_at(f, p))
            prof_menu.addAction(act)

    rm_act = QAction("Remove nearest notch", menu)
    rm_act.setEnabled(have_any)
    rm_act.triggered.connect(
        lambda: radio.remove_nearest_notch(float(freq_hz)))
    menu.addAction(rm_act)

    menu.addSeparator()
    clr_act = QAction("Clear ALL notches", menu)
    clr_act.setEnabled(have_any)
    clr_act.triggered.connect(radio.clear_notches)
    menu.addAction(clr_act)

    # Default-width submenu (replaces the old default-Q one). Width
    # is in Hz so operators don't need to mentally translate Q values
    # — the typical SDR-client parameter choice. Presets
    # cover common use cases from "narrow CW notch" up to "broadcast
    # splatter blanket".
    menu.addSeparator()
    w_menu = menu.addMenu("Default width for new notches")
    current_w = float(getattr(radio, "notch_default_width_hz", 80.0))
    for w_preset, descr in (
        (20,   "very narrow — pinpoint single tone"),
        (50,   "narrow — surgical CW carrier kill"),
        (80,   "default — covers FT8 / FT4 (47 Hz spread)"),
        (150,  "wide — RTTY pair, drifty CW"),
        (300,  "very wide — broadband het, splatter"),
        (600,  "blanket — segments of QRM"),
    ):
        label = f"{w_preset:>3d} Hz   {descr}"
        if abs(current_w - w_preset) < 0.5:
            label = "✓  " + label
        else:
            label = "    " + label
        act = QAction(label, w_menu)
        act.triggered.connect(
            lambda _checked=False, w=w_preset:
                radio.set_notch_default_width_hz(float(w)))
        w_menu.addAction(act)

    # Default profile for new notches (notch v2).  Same 3 presets
    # exposed in the per-notch profile submenu above; this picks
    # which preset newly-placed notches start with.
    cur_default_depth = float(
        getattr(radio, "_notch_default_depth_db", -50.0))
    cur_default_cascade = int(
        getattr(radio, "_notch_default_cascade", 2))
    cur_default_key = "custom"
    for key, params in getattr(radio, "NOTCH_PRESETS", {}).items():
        if (int(params["cascade"]) == cur_default_cascade
                and abs(float(params["depth_db"])
                         - cur_default_depth) <= 1.0):
            cur_default_key = key
            break
    p_menu = menu.addMenu(
        f"Default profile for new notches  "
        f"(currently: {cur_default_key.capitalize()})")
    for preset_key, preset_label, descr in (
        ("normal",   "Normal",   "balanced — 2× cascade, -50 dB"),
        ("deep",     "Deep",     "stronger — 2× cascade, -70 dB"),
        ("surgical", "Surgical", "sharp — 4× cascade, -50 dB"),
    ):
        mark = "✓  " if preset_key == cur_default_key else "    "
        act = QAction(mark + f"{preset_label}  —  {descr}", p_menu)
        act.triggered.connect(
            lambda _=False, p=preset_key:
                radio.set_notch_default_preset(p))
        p_menu.addAction(act)

    # ── Saved notch banks (operator-named presets) ──────────────
    # v0.0.7.1 notch v2 -- operator can save the current bank under
    # a name ('My 40m setup') and reload it later.  Banks persist
    # to QSettings under notches/banks/<name>.
    menu.addSeparator()
    saved_banks = []
    try:
        saved_banks = list(radio.list_notch_banks())
    except Exception:
        saved_banks = []
    banks_menu = menu.addMenu("Notch banks  (saved presets)")

    save_act = QAction("Save current bank as...", banks_menu)
    save_act.setEnabled(have_any)
    save_act.triggered.connect(
        lambda: _prompt_save_notch_bank(parent_widget, radio))
    banks_menu.addAction(save_act)

    if saved_banks:
        load_menu = banks_menu.addMenu("Load saved bank")
        for nm in saved_banks:
            act = QAction(nm, load_menu)
            act.triggered.connect(
                lambda _=False, n=nm: radio.load_notch_bank(n))
            load_menu.addAction(act)

        del_menu = banks_menu.addMenu("Delete saved bank")
        for nm in saved_banks:
            act = QAction(nm, del_menu)
            act.triggered.connect(
                lambda _=False, n=nm:
                    _confirm_delete_notch_bank(parent_widget, radio, n))
            del_menu.addAction(act)
    else:
        empty = QAction("(no saved banks yet)", banks_menu)
        empty.setEnabled(False)
        banks_menu.addAction(empty)

    # Turn-off action — convenient exit from notch mode back to
    # "right-click does nothing notch-related" state. Sits at the
    # bottom so it's out of the way of the common Add action.
    menu.addSeparator()
    off_act = QAction("Disable Notch Filter", menu)
    off_act.triggered.connect(
        lambda: radio.set_notch_enabled(False))
    menu.addAction(off_act)

    return menu


def _prompt_save_notch_bank(parent_widget, radio) -> None:
    """Pop a small text-input dialog asking for the bank name, then
    save.  If the name already exists, ask for confirmation before
    overwriting.  Called from the right-click menu's Save action."""
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    name, ok = QInputDialog.getText(
        parent_widget, "Save notch bank",
        "Save current notches as:",
    )
    if not ok:
        return
    name = (name or "").strip()
    if not name:
        return
    existing = []
    try:
        existing = list(radio.list_notch_banks())
    except Exception:
        existing = []
    if name in existing:
        confirm = QMessageBox.question(
            parent_widget, "Overwrite saved bank?",
            f"A bank named '{name}' already exists.\n\nOverwrite?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
    radio.save_notch_bank(name)


def _confirm_delete_notch_bank(parent_widget, radio, name: str) -> None:
    """Confirm-and-delete dialog for a saved notch bank.  Called
    from the right-click menu's Delete submenu."""
    from PySide6.QtWidgets import QMessageBox
    confirm = QMessageBox.question(
        parent_widget, "Delete notch bank?",
        f"Delete saved notch bank '{name}'?\n\n"
        "This cannot be undone.",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
    )
    if confirm == QMessageBox.Yes:
        radio.delete_notch_bank(name)


# ── Spectrum / Waterfall panels ─────────────────────────────────────────
def _read_qs_bool(qs, key, default):
    """Tiny QSettings boolean coercion helper used by the
    EiBi-overlay refresh path -- QSettings stores everything as
    str on Windows so bool-cast doesn't work directly."""
    val = qs.value(key, default)
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


class SpectrumPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("PANADAPTER", parent, help_topic="spectrum")
        self.radio = radio

        # Branch on graphics backend. The default ("software" /
        # "opengl") creates the existing QPainter SpectrumWidget with
        # all its overlays and interactions wired up. The new
        # "gpu_opengl" path builds the from-scratch SpectrumGpuWidget
        # — fast trace render, but currently no overlays / no
        # interactions (notches, spots, band plan, peak markers,
        # click-to-tune, etc.). Successive commits will add those
        # back. Default stays QPainter until the GPU widget reaches
        # feature parity AND has tester time across many GPU configs.
        from lyra.ui.gfx import is_gpu_panadapter_active
        if is_gpu_panadapter_active():
            self._setup_gpu_panadapter()
        else:
            self._setup_qpainter_panadapter()

    # ── GPU panadapter (BACKEND_GPU_OPENGL) ────────────────────────
    def _setup_gpu_panadapter(self) -> None:
        """Wire SpectrumGpuWidget for production use. The GPU widget
        is now at feature parity with the QPainter widget for the
        overlays and interactions that ship today: notches, spots,
        passband, noise-floor, click-to-tune, right-click menu, wheel
        zoom, Y-axis drag, RX BW drag, band-plan strip + landmark
        click-to-tune, and in-passband peak markers. Trace + waterfall
        are GPU-accelerated; QPainter overlays are layered on top in
        paintEvent.
        """
        from lyra.ui.spectrum_gpu import SpectrumGpuWidget
        self.widget = SpectrumGpuWidget()
        self.content_layout().addWidget(self.widget)
        # Wrap the spectrum_ready signal — Radio emits
        # (spec_db, center_hz, rate) but our widget wants
        # (spec_db, min_db, max_db). We read the dB range fresh
        # from Radio each tick so live Settings changes take effect
        # immediately without an extra signal subscription.
        self.radio.spectrum_ready.connect(self._gpu_on_spectrum_ready)
        # Click-to-tune (Phase B.5). Routed through _on_click so CW
        # modes get the pitch offset compensation (signal lands inside
        # the SSB-style filter at +/- pitch from the marker).
        self.widget.clicked_freq.connect(self._on_click)
        # Right-click context menu (Phase B.6). Reuses the same
        # handlers as the QPainter path — _on_right_click handles
        # the shift+right quick-remove + plain-right menu logic.
        self.widget.right_clicked_freq.connect(self._on_right_click)
        # Mouse-wheel zoom (Phase B.7) — direct passthrough to Radio.
        self.widget.wheel_zoom.connect(self.radio.zoom_step)
        # Wheel-over-notch (Phase B.14) — adjust that notch's width.
        self.widget.wheel_at_freq.connect(self._on_wheel)
        # Drag-on-notch (Phase B.14) — resize notch width via drag.
        self.widget.notch_q_drag.connect(self._on_notch_q_drag)
        # Y-axis drag for spectrum dB range (Phase B.8) — drag in
        # the right-edge zone shifts both min/max together. Forwards
        # to Radio.set_spectrum_db_range; the new range comes back
        # to the widget on the next spectrum_ready tick (we read
        # radio.spectrum_db_range fresh in _gpu_on_spectrum_ready).
        self.widget.db_scale_drag.connect(
            lambda lo, hi: self.radio.set_spectrum_db_range(lo, hi))
        # Noise-floor reference line (Phase B.10).
        self.radio.noise_floor_changed.connect(
            self.widget.set_noise_floor_db)
        # Operator's noise-floor color override (live updates from
        # Visuals → Colors).
        self.widget.set_noise_floor_color(self.radio.noise_floor_color)
        self.radio.noise_floor_color_changed.connect(
            self.widget.set_noise_floor_color)
        # Passband overlay (Phase B.11) — seed + track changes.
        pb_lo, pb_hi = self.radio._compute_passband()
        self.widget.set_passband(pb_lo, pb_hi)
        self.radio.passband_changed.connect(self.widget.set_passband)
        # CW Zero (white) reference line — visible only in CWU/CWL.
        self.widget.set_cw_zero_offset(int(self.radio.cw_zero_offset_hz))
        self.radio.cw_zero_offset_changed.connect(
            self.widget.set_cw_zero_offset)
        # Lyra constellation watermark — operator-toggleable.
        self.widget.set_show_constellation(
            bool(self.radio.show_lyra_constellation))
        self.radio.lyra_constellation_changed.connect(
            self.widget.set_show_constellation)
        # Occasional meteors — separate toggle, opt-in flair.
        self.widget.set_show_meteors(bool(self.radio.show_lyra_meteors))
        self.radio.lyra_meteors_changed.connect(
            self.widget.set_show_meteors)
        # Grid lines (9×9 horiz/vert divisions) — operator toggle.
        self.widget.set_show_grid(bool(self.radio.show_spectrum_grid))
        self.radio.spectrum_grid_changed.connect(
            self.widget.set_show_grid)
        # Notch markers (Phase B.13) — seed + track changes.
        self.widget.set_notches(self.radio.notch_details)
        self.radio.notches_changed.connect(self.widget.set_notches)
        # DX/contest spots — seed lifetime + filter, then track signals.
        self.widget.set_spot_lifetime_s(self.radio.spot_lifetime_s)
        self.radio.spot_lifetime_changed.connect(
            self.widget.set_spot_lifetime_s)
        self.widget.set_spot_mode_filter(self.radio.spot_mode_filter_csv)
        self.radio.spot_mode_filter_changed.connect(
            self.widget.set_spot_mode_filter)
        self.radio.spots_changed.connect(self.widget.set_spots)
        # ── EiBi SW broadcaster overlay (v0.0.9 Step 4c) ──────
        # The panel watches freq + zoom + store-changes and pushes
        # the visible-range entry list into the widget.  Auto-
        # detection (no overlay inside ham bands) lives in the
        # _refresh_eibi method.
        self.radio.freq_changed.connect(
            lambda *_: self._refresh_eibi_overlay())
        self.radio.rate_changed.connect(
            lambda *_: self._refresh_eibi_overlay())
        self.radio.zoom_changed.connect(
            lambda *_: self._refresh_eibi_overlay())
        self.radio.eibi_store_changed.connect(
            self._refresh_eibi_overlay)
        # Initial pass.
        self._refresh_eibi_overlay()
        # Drag-edge-to-resize-RX-BW (Phase B.11). Operator pulls a
        # cyan edge → widget emits proposed BW (Hz, already
        # quantized + clamped) → push straight into Radio for the
        # current mode.
        self.widget.passband_edge_drag.connect(
            lambda bw: self.radio.set_rx_bw(self.radio.mode, int(bw)))
        # Band-plan overlay (region + segment / landmark / edge-warn
        # toggles + per-segment color overrides). 1:1 with the QPainter
        # widget wiring below — same Radio attributes / signals drive
        # the GPU widget's setters.
        self.widget.set_band_plan_region(self.radio.band_plan_region)
        self.widget.set_band_plan_show_segments(
            self.radio.band_plan_show_segments)
        self.widget.set_band_plan_show_landmarks(
            self.radio.band_plan_show_landmarks)
        self.widget.set_band_plan_show_edge_warn(
            self.radio.band_plan_edge_warn)
        self.widget.set_segment_color_overrides(self.radio.segment_colors)
        self.radio.band_plan_region_changed.connect(
            self.widget.set_band_plan_region)
        self.radio.band_plan_show_segments_changed.connect(
            self.widget.set_band_plan_show_segments)
        self.radio.band_plan_show_landmarks_changed.connect(
            self.widget.set_band_plan_show_landmarks)
        self.radio.band_plan_edge_warn_changed.connect(
            self.widget.set_band_plan_show_edge_warn)
        self.radio.segment_colors_changed.connect(
            self.widget.set_segment_color_overrides)
        # Landmark click-to-tune — tune freq + switch mode in one shot.
        # Reuses the same handler as the QPainter path so behavior is
        # identical (status_message, etc.).
        self.widget.landmark_clicked.connect(self._on_landmark_clicked)

        # Peak markers — in-passband peak-hold overlay. Seed every
        # tunable + subscribe to live changes from Settings → Visuals.
        self.widget.set_peak_markers_enabled(
            self.radio.peak_markers_enabled)
        self.widget.set_peak_markers_decay_dbps(
            self.radio.peak_markers_decay_dbps)
        self.widget.set_peak_markers_style(self.radio.peak_markers_style)
        self.widget.set_peak_markers_show_db(
            self.radio.peak_markers_show_db)
        self.widget.set_peak_markers_color(self.radio.peak_markers_color)
        self.radio.peak_markers_enabled_changed.connect(
            self.widget.set_peak_markers_enabled)
        self.radio.peak_markers_decay_changed.connect(
            self.widget.set_peak_markers_decay_dbps)
        self.radio.peak_markers_style_changed.connect(
            self.widget.set_peak_markers_style)
        self.radio.peak_markers_show_db_changed.connect(
            self.widget.set_peak_markers_show_db)
        self.radio.peak_markers_color_changed.connect(
            self.widget.set_peak_markers_color)

        # Spectrum smoothing — display-only EWMA. Seed + subscribe.
        self.widget.set_spectrum_smoothing_enabled(
            self.radio.spectrum_smoothing_enabled)
        self.widget.set_spectrum_smoothing_strength(
            self.radio.spectrum_smoothing_strength)
        self.radio.spectrum_smoothing_enabled_changed.connect(
            self.widget.set_spectrum_smoothing_enabled)
        self.radio.spectrum_smoothing_strength_changed.connect(
            self.widget.set_spectrum_smoothing_strength)

        # Trace color — Radio holds the operator's pick; sync it now
        # and on changes.
        self._gpu_apply_trace_color()
        self.radio.spectrum_trace_color_changed.connect(
            lambda _hex: self._gpu_apply_trace_color())

    def _gpu_on_spectrum_ready(self, spec_db, center_hz, rate):
        # Push tuning info first so any subsequent overlay /
        # interaction code knows the freq window the widget
        # represents. The rate IS the span here (samples/sec ↔ Hz).
        self.widget.set_tuning(center_hz, rate)
        lo, hi = self.radio.spectrum_db_range
        self.widget.set_spectrum(spec_db, min_db=lo, max_db=hi)

    def _gpu_apply_trace_color(self) -> None:
        from PySide6.QtGui import QColor
        col = QColor(self.radio.spectrum_trace_color)
        if col.isValid():
            self.widget.set_trace_color(col)

    # ── QPainter panadapter (BACKEND_SOFTWARE / BACKEND_OPENGL) ────
    def _setup_qpainter_panadapter(self) -> None:
        """Original SpectrumPanel wiring, unchanged. Built when the
        backend is BACKEND_SOFTWARE or BACKEND_OPENGL — both run the
        QPainter SpectrumWidget; the only difference is its base
        class (QWidget vs QOpenGLWidget) which is resolved at gfx.py
        import time."""
        radio = self.radio
        self.widget = SpectrumWidget()
        self.content_layout().addWidget(self.widget)
        self.widget.clicked_freq.connect(self._on_click)
        self.widget.right_clicked_freq.connect(self._on_right_click)
        self.widget.wheel_at_freq.connect(self._on_wheel)
        # Mouse wheel on empty spectrum = zoom in/out via Radio's
        # preset zoom levels. wheel_at_freq still handles notch-Q when
        # the wheel is over a notch tick (widget-side dispatch).
        self.widget.wheel_zoom.connect(self.radio.zoom_step)
        self.widget.notch_q_drag.connect(self._on_notch_q_drag)
        self.widget.spot_clicked.connect(self._on_spot_clicked)
        radio.spectrum_ready.connect(self._on_spectrum_ready)
        radio.notches_changed.connect(self.widget.set_notches)
        radio.spots_changed.connect(self.widget.set_spots)
        # Seed + track the spot lifetime so the widget can age-fade
        # oldest boxes toward the 30% alpha floor as they approach expiry.
        self.widget.set_spot_lifetime_s(radio.spot_lifetime_s)
        radio.spot_lifetime_changed.connect(self.widget.set_spot_lifetime_s)
        # Mode filter — SDRLogger+-style CSV. Widget parses the string
        # (with SSB → USB/LSB/SSB auto-expansion) and applies during render.
        self.widget.set_spot_mode_filter(radio.spot_mode_filter_csv)
        radio.spot_mode_filter_changed.connect(self.widget.set_spot_mode_filter)
        # Spectrum dB-range — live control from Visuals settings.
        lo, hi = radio.spectrum_db_range
        self.widget.set_db_range(lo, hi)
        radio.spectrum_db_range_changed.connect(self.widget.set_db_range)
        # RX filter passband overlay — translucent cyan rect showing
        # which bins are in vs out of the current demod filter.
        pb_lo, pb_hi = radio._compute_passband()
        self.widget.set_passband(pb_lo, pb_hi)
        radio.passband_changed.connect(self.widget.set_passband)
        # CW Zero (white) reference line — visible only in CWU/CWL.
        self.widget.set_cw_zero_offset(int(radio.cw_zero_offset_hz))
        radio.cw_zero_offset_changed.connect(self.widget.set_cw_zero_offset)
        # Lyra constellation watermark — operator-toggleable.
        self.widget.set_show_constellation(bool(radio.show_lyra_constellation))
        radio.lyra_constellation_changed.connect(
            self.widget.set_show_constellation)
        # Occasional meteors — separate toggle, opt-in flair.
        self.widget.set_show_meteors(bool(radio.show_lyra_meteors))
        radio.lyra_meteors_changed.connect(self.widget.set_show_meteors)
        # Grid lines (9×9 horiz/vert divisions) — operator toggle.
        self.widget.set_show_grid(bool(radio.show_spectrum_grid))
        radio.spectrum_grid_changed.connect(self.widget.set_show_grid)
        # Drag-to-resize: user grabs a cyan edge and drags → widget
        # emits the proposed BW (already clamped + quantized) → we
        # push it straight into Radio.set_rx_bw for the current mode.
        self.widget.passband_edge_drag.connect(
            lambda bw: self.radio.set_rx_bw(self.radio.mode, int(bw)))
        # Noise-floor reference line — Radio emits at ~6 Hz while
        # streaming, or -999 when toggled off.
        radio.noise_floor_changed.connect(self.widget.set_noise_floor_db)
        # Band-plan overlay (region + segment/landmark/edge toggles).
        self.widget.set_band_plan_region(radio.band_plan_region)
        self.widget.set_band_plan_show_segments(radio.band_plan_show_segments)
        self.widget.set_band_plan_show_landmarks(radio.band_plan_show_landmarks)
        self.widget.set_band_plan_show_edge_warn(radio.band_plan_edge_warn)
        radio.band_plan_region_changed.connect(
            self.widget.set_band_plan_region)
        radio.band_plan_show_segments_changed.connect(
            self.widget.set_band_plan_show_segments)
        radio.band_plan_show_landmarks_changed.connect(
            self.widget.set_band_plan_show_landmarks)
        radio.band_plan_edge_warn_changed.connect(
            self.widget.set_band_plan_show_edge_warn)
        # Peak markers — in-passband peak-hold overlay.
        self.widget.set_peak_markers_enabled(radio.peak_markers_enabled)
        self.widget.set_peak_markers_decay_dbps(radio.peak_markers_decay_dbps)
        radio.peak_markers_enabled_changed.connect(
            self.widget.set_peak_markers_enabled)
        radio.peak_markers_decay_changed.connect(
            self.widget.set_peak_markers_decay_dbps)
        # Spectrum smoothing — display-only EWMA. Seed + subscribe.
        self.widget.set_spectrum_smoothing_enabled(
            radio.spectrum_smoothing_enabled)
        self.widget.set_spectrum_smoothing_strength(
            radio.spectrum_smoothing_strength)
        radio.spectrum_smoothing_enabled_changed.connect(
            self.widget.set_spectrum_smoothing_enabled)
        radio.spectrum_smoothing_strength_changed.connect(
            self.widget.set_spectrum_smoothing_strength)
        # Landmark click-to-tune: tune freq + switch mode in one shot.
        self.widget.landmark_clicked.connect(self._on_landmark_clicked)
        # User color picks — seed widget from Radio, subscribe to updates.
        self.widget.set_spectrum_trace_color(radio.spectrum_trace_color)
        self.widget.set_segment_color_overrides(radio.segment_colors)
        self.widget.set_noise_floor_color(radio.noise_floor_color)
        radio.spectrum_trace_color_changed.connect(
            self.widget.set_spectrum_trace_color)
        radio.segment_colors_changed.connect(
            self.widget.set_segment_color_overrides)
        radio.noise_floor_color_changed.connect(
            self.widget.set_noise_floor_color)
        self.widget.set_peak_markers_color(radio.peak_markers_color)
        radio.peak_markers_color_changed.connect(
            self.widget.set_peak_markers_color)
        # Peak-marker style + readout
        self.widget.set_peak_markers_style(radio.peak_markers_style)
        self.widget.set_peak_markers_show_db(radio.peak_markers_show_db)
        radio.peak_markers_style_changed.connect(
            self.widget.set_peak_markers_style)
        radio.peak_markers_show_db_changed.connect(
            self.widget.set_peak_markers_show_db)
        # Y-axis drag-to-scale → push back to Radio spectrum_db_range
        self.widget.db_scale_drag.connect(
            lambda lo, hi: self.radio.set_spectrum_db_range(lo, hi))

    def _on_spectrum_ready(self, spec_db, center_hz, rate):
        self.widget.set_spectrum(spec_db, center_hz, rate)

    def _refresh_eibi_overlay(self) -> None:
        """Recompute the EiBi visible-entry list and push it into
        the panadapter widget.  Driven by freq / zoom / store /
        settings change events; not called per paint.

        Logic:
          1. Read settings (master, force_all, min_power, hide_off_air)
          2. Run overlay_gate to decide if we should render at all
          3. If yes, query EibiStore.lookup_in_range across the
             current visible span
          4. Push (entries, visible_flag) to the widget

        Skips the widget-push entirely when overlay is gated off
        (visible_flag=False clears any stale labels)."""
        try:
            from PySide6.QtCore import QSettings as _QS
            from lyra.swdb.overlay_gate import overlay_should_render
            from lyra.swdb.time_filter import is_on_air
            qs = _QS("N8SDR", "Lyra")
            master = _read_qs_bool(
                qs, "swdb/overlay_master_enabled", False)
            force_all = _read_qs_bool(
                qs, "swdb/overlay_force_all_bands", False)
            try:
                min_power = int(qs.value("swdb/min_power", 1) or 1)
            except (TypeError, ValueError):
                min_power = 1
            hide_off_air = _read_qs_bool(
                qs, "swdb/hide_off_air", True)
            # Operator's region for the band-plan check.
            region = ""
            try:
                region = str(qs.value(
                    "operator/band_plan_region", "US") or "US")
            except Exception:
                region = "US"
            freq_hz = int(getattr(self.radio, "freq_hz", 0))
            should = overlay_should_render(
                freq_hz, region, master, force_all)
            if not should or not getattr(
                    self.radio, "eibi_store", None):
                # Clear any stale entries.
                if hasattr(self.widget, "set_eibi_entries"):
                    self.widget.set_eibi_entries([], False)
                return
            store = self.radio.eibi_store
            if not store.loaded:
                if hasattr(self.widget, "set_eibi_entries"):
                    self.widget.set_eibi_entries([], False)
                return
            # Visible-range query.
            span_hz = int(getattr(self.widget, "_span_hz", 0))
            if span_hz <= 0:
                return
            lo_khz = (freq_hz - span_hz // 2) // 1000
            hi_khz = (freq_hz + span_hz // 2) // 1000
            entries = store.lookup_in_range(
                lo_khz, hi_khz,
                min_power=min_power,
                only_on_air=hide_off_air)
            # Tuple-pack for the widget; recompute on-air status
            # so the renderer can color-code (only meaningful when
            # hide_off_air is False, but cheap to always include).
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            packed = [
                (e.freq_khz * 1000, e.station, e.language,
                 e.target, is_on_air(e, now))
                for e in entries
            ]
            if hasattr(self.widget, "set_eibi_entries"):
                self.widget.set_eibi_entries(packed, True)
        except Exception as ex:
            print(f"[SpectrumPanel] EiBi overlay refresh failed: {ex}")

    def _on_click(self, freq_hz):
        # Click-to-tune. CW filters sit OFFSET from the marker by
        # ±cw_pitch (CWU passband above the marker, CWL below). For
        # the clicked signal to land INSIDE the filter we tune the
        # marker to (signal - pitch) for CWU, or (signal + pitch)
        # for CWL. Other modes tune directly.
        target = int(freq_hz)
        mode = self.radio.mode
        if mode in ("CWU", "CWL"):
            pitch = int(self.radio.cw_pitch_hz)
            # Panadapter is sky-freq convention (display-mirror flip).
            # CWU signal sits ABOVE the carrier visually, so to put a
            # clicked signal at +pitch from the new VFO we subtract
            # pitch from the click freq. CWL mirrored.
            target += -pitch if mode == "CWU" else +pitch
        self.radio.set_freq_hz(target)

    def _on_spot_clicked(self, freq_hz):
        # User clicked on a spot marker — tune + emit TCI spot_activated.
        self.radio.activate_spot_near(float(freq_hz))

    def _on_landmark_clicked(self, freq_hz: int, mode: str):
        """User clicked a band-plan landmark triangle — tune there
        and switch to the landmark's suggested mode (FT8 → DIGU, etc.)"""
        self.radio.set_mode(mode)
        self.radio.set_freq_hz(int(freq_hz))
        self.radio.status_message.emit(
            f"Tuned to {freq_hz/1e6:.3f} MHz {mode}", 2000)

    def _on_right_click(self, freq_hz, shift, global_pos):
        # Both gestures (shift+right = quick-remove, plain right =
        # menu) are gated on notch_enabled. When NF is off we only
        # show the menu (which degrades to a single "Enable Notch
        # Filter" item). Rationale: right-click is a scarce gesture
        # we want free for future spectrum features (drag-to-tune,
        # spot menus, etc.) when the operator isn't working notches.
        if shift and self.radio.notch_enabled:
            self.radio.remove_nearest_notch(freq_hz)
            return
        self._show_notch_menu(freq_hz, global_pos)

    def _show_notch_menu(self, freq_hz, global_pos):
        """Context menu anchored at the right-click site. When the
        Notch button is ON, shows Add / Remove-nearest / Clear-all /
        Default-Q submenu / Disable. When OFF, degrades to a single
        "Enable Notch Filter" item so the gesture stays discoverable
        but doesn't mutate the notch bank."""
        menu = _build_notch_menu(self, self.radio, freq_hz)
        menu.exec(global_pos)

    def _on_wheel(self, freq_hz, delta_units):
        # Wheel over a notch adjusts its WIDTH multiplicatively.
        # Down = wider, up = narrower (matches "scroll up to zoom in /
        # narrow the focus"). 1.15x per tick so each click is visible
        # but not jumpy. Looks up the nearest notch via Radio so we
        # don't depend on the panel knowing the data shape.
        factor = (1 / 1.15) ** delta_units
        nearest_idx = self.radio._find_nearest_notch_idx(
            float(freq_hz), tolerance_hz=self.radio.rate / 8)
        if nearest_idx is None:
            return
        n = self.radio._notches[nearest_idx]
        self.radio.set_notch_width_at(n.abs_freq_hz, n.width_hz * factor)

    def _on_notch_q_drag(self, freq_hz, new_value):
        # Signal name is historical ("q_drag"); payload is now WIDTH
        # in Hz. Spectrum widget computes the proposed width from
        # vertical drag distance and emits it directly.
        self.radio.set_notch_width_at(freq_hz, new_value)


# ── Band selector ──────────────────────────────────────────────────────
class BandPanel(GlassPanel):
    """Horizontal band-button strip à la other reference SDR clients.

    Click a band → tune to the band's default freq + set the conventional
    mode for that band. The button matching the current tune frequency
    is highlighted automatically.

    Per-band memory (last-used freq/mode/gain per band) is on the roadmap
    — this first pass restores default freqs only.
    """

    BUTTON_WIDTH = 42

    def __init__(self, radio: Radio, parent=None):
        super().__init__("BAND", parent, help_topic="tuning")
        self.radio = radio
        self._buttons: dict[str, QPushButton] = {}
        self._gen_buttons: dict[str, QPushButton] = {}
        self._all_bands = list(AMATEUR_BANDS) + list(BROADCAST_BANDS)
        # Per-GEN-slot memory: last freq/mode used while active.
        # v0.0.9 Step 2: customizable via right-click "Save current
        # freq+mode here".  Loaded from QSettings on init; falls
        # back to the bands.py-defined defaults for slots that
        # haven't been customized yet.  See _load_gen_memory and
        # _save_gen_memory for the persistence layer.
        self._gen_memory: dict[str, tuple[int, str]] = {
            g.name: (g.default_hz, g.default_mode) for g in GEN_SLOTS
        }
        # Optional per-slot operator-supplied label (e.g. "40m SSB",
        # "AM Broadcast 1530").  Not the button TEXT (which stays
        # "GEN1" etc. for visual consistency); shown in tooltip.
        self._gen_labels: dict[str, str] = {
            g.name: "" for g in GEN_SLOTS
        }
        self._load_gen_memory()
        # v0.0.9 Step 3a: operator memory presets (up to 20).
        # Hydrated from QSettings; persists across sessions.
        # See lyra/memory.py for the storage layer.
        from lyra.memory import MemoryStore
        self._memory = MemoryStore()
        self._active_gen: str | None = None   # when freq is outside all bands

        v = QVBoxLayout()
        v.setSpacing(4)

        v.addLayout(self._make_row(AMATEUR_BANDS, "AMATEUR"))
        v.addLayout(self._make_row(BROADCAST_BANDS, "BC"))
        v.addLayout(self._make_gen_row())
        self.content_layout().addLayout(v)

        radio.freq_changed.connect(self._on_freq_changed)
        radio.mode_changed.connect(self._on_mode_changed)
        self._on_freq_changed(radio.freq_hz)

    def _make_row(self, bands, label_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(2)
        lbl = QLabel(label_text)
        lbl.setFixedWidth(60)
        lbl.setStyleSheet(
            "color: #00e5ff; font-size: 9px; font-weight: 700; "
            "letter-spacing: 2px;")
        row.addWidget(lbl)
        for b in bands:
            btn = self._make_band_button(b.label)
            btn.setToolTip(
                f"{b.name}  —  {b.lo_hz/1e6:.3f} to {b.hi_hz/1e6:.3f} MHz\n"
                f"Click: tune to {b.default_hz/1e6:.3f} MHz, {b.default_mode}")
            btn.clicked.connect(lambda _checked, band=b: self._on_band_clicked(band))
            self._buttons[b.name] = btn
            row.addWidget(btn)
        row.addStretch(1)
        return row

    def _make_band_button(self, text: str) -> QPushButton:
        """Band buttons override default QSS padding so 3-4 char labels
        fit the compact width, and the CHECKED state uses a red-glowing
        outline so the active band pops dramatically against the cyan
        theme."""
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedWidth(self.BUTTON_WIDTH)
        btn.setStyleSheet("""
            QPushButton {
                padding: 4px 2px;
            }
            QPushButton:checked {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 #3a0e0e, stop:0.6 #260808, stop:1 #1a0505);
                border: 2px solid #ff3344;
                color: #ffcc88;
                font-weight: 800;
            }
            QPushButton:checked:hover {
                border-color: #ff6677;
                color: #ffddaa;
            }
        """)
        return btn

    def _make_gen_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(2)
        lbl = QLabel("OTHER")
        lbl.setFixedWidth(60)
        lbl.setStyleSheet(
            "color: #00e5ff; font-size: 9px; font-weight: 700; "
            "letter-spacing: 2px;")
        row.addWidget(lbl)
        for g in GEN_SLOTS:
            btn = self._make_band_button(g.label)
            btn.setFixedWidth(self.BUTTON_WIDTH + 12)  # 4-char labels need more
            btn.clicked.connect(
                lambda _c, slot=g.name: self._on_gen_clicked(slot))
            # v0.0.9 Step 2: right-click context menu lets the
            # operator customize each GEN slot.  See
            # _show_gen_menu for the menu items.
            from PySide6.QtCore import Qt as _Qt
            btn.setContextMenuPolicy(_Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, slot=g.name, b=btn:
                    self._show_gen_menu(slot, b, pos))
            self._gen_buttons[g.name] = btn
            self._update_gen_tooltip(g.name)
            row.addWidget(btn)
        # ── TIME button (v0.0.9 Step 1) ──────────────────────────────
        # HF time-station cycle.  Slotted right after GEN3 in the
        # OTHER row.  Plain click cycles through (station, freq)
        # entries in operator-country-priority order.  Right-click
        # opens a full station list grouped by country.  Each click
        # also sets the right mode (most are AM, CHU is USB).
        # Cycle index persists across launches via QSettings under
        # bands/time_cycle_idx.  See lyra/data/time_stations.py for
        # the static data.
        time_btn = self._make_band_button("TIME")
        time_btn.setCheckable(False)  # cycle button -- never "active"
        time_btn.setFixedWidth(self.BUTTON_WIDTH + 12)
        time_btn.setToolTip(
            "TIME — HF time-signal station cycle.\n"
            "Click: tune to next station/frequency (WWV, CHU, BPM,\n"
            "RWM, etc.).  Cycle order prioritizes stations near your\n"
            "configured callsign's country.\n"
            "Right-click: open full station list and pick directly.")
        time_btn.clicked.connect(self._on_time_clicked)
        from PySide6.QtCore import Qt as _Qt
        time_btn.setContextMenuPolicy(_Qt.CustomContextMenu)
        time_btn.customContextMenuRequested.connect(
            lambda pos: self._show_time_menu(time_btn, pos))
        self._time_button = time_btn
        row.addWidget(time_btn)
        # ── Memory button (v0.0.9 Step 3a) ──────────────────────────
        # Operator-named frequency memory bank.  Up to 20 entries.
        # Plain left-click: dropdown menu listing all saved presets;
        # click any to recall (tune to its freq + mode).
        # Right-click: management menu (save current, delete, etc.).
        # Sits right after TIME -- the design-doc-locked button order
        # is GEN1 / GEN2 / GEN3 / TIME / Memory.
        mem_btn = self._make_band_button("Mem")
        mem_btn.setCheckable(False)
        mem_btn.setFixedWidth(self.BUTTON_WIDTH + 12)
        mem_btn.clicked.connect(
            lambda: self._show_memory_recall_menu(mem_btn))
        from PySide6.QtCore import Qt as _Qt
        mem_btn.setContextMenuPolicy(_Qt.CustomContextMenu)
        mem_btn.customContextMenuRequested.connect(
            lambda pos: self._show_memory_manage_menu(mem_btn, pos))
        self._memory_button = mem_btn
        self._update_memory_tooltip()
        row.addWidget(mem_btn)
        row.addStretch(1)
        return row

    def _on_time_clicked(self) -> None:
        """Cycle to the next time-station / frequency.

        Cycle index persists via QSettings.  Country-aware ordering
        derives from Radio.operator_country_iso (callsign-based
        DXCC prefix lookup).  Tuning emits the right mode (AM for
        most stations, USB for CHU).  Status-bar message confirms
        what was tuned to.
        """
        from PySide6.QtCore import QSettings as _QS
        from lyra.data.time_stations import (
            order_stations, cycle_entry, total_cycle_length)
        country = ""
        try:
            country = self.radio.operator_country_iso
        except AttributeError:
            country = ""
        stations = order_stations(country)
        total = total_cycle_length(stations)
        if total == 0:
            return  # no stations defined; should never happen
        qs = _QS("N8SDR", "Lyra")
        idx = int(qs.value("bands/time_cycle_idx", 0) or 0)
        # Resolve current entry, then advance cycle for next press.
        # Resolving with the OLD idx and advancing means the very
        # first click after install lands on entry 0 (operator's
        # most-likely-relevant station's lowest freq).
        station, freq_khz = cycle_entry(stations, idx)
        next_idx = (idx + 1) % total
        qs.setValue("bands/time_cycle_idx", next_idx)
        # Tune.  Mode set first so the demod is right when the
        # freq lands.  Country/country-distance not used here for
        # display reasons -- the simple status-bar message is
        # plenty.
        self.radio.set_mode(station.mode)
        self.radio.set_freq_hz(freq_khz * 1000)
        # Status-bar confirmation -- which station + freq.
        try:
            self.radio.status_message.emit(
                f"TIME: {station.id} on {freq_khz/1000:.3f} MHz "
                f"({station.mode}) -- {station.name}",
                3000)
        except Exception:
            pass
        # Mark active GEN clear -- we're not on a GEN slot anymore.
        self._active_gen = None

    def _show_time_menu(self, anchor_btn, pos) -> None:
        """Right-click popup: full station list, grouped by country,
        with each frequency selectable directly.  Lets the operator
        jump to a specific station+freq without cycling through
        intermediates.
        """
        from PySide6.QtCore import QSettings as _QS
        from PySide6.QtWidgets import QMenu
        from lyra.data.time_stations import (
            order_stations, total_cycle_length)
        country = ""
        try:
            country = self.radio.operator_country_iso
        except AttributeError:
            country = ""
        stations = order_stations(country)
        menu = QMenu(self)
        # Header: indicate ordering basis.
        if country:
            hdr = QAction(
                f"Station list  (priority: {country})", menu)
        else:
            hdr = QAction("Station list", menu)
        hdr.setEnabled(False)
        menu.addAction(hdr)
        menu.addSeparator()
        for s in stations:
            sub = menu.addMenu(f"{s.id}  —  {s.name}")
            if s.notes:
                hint = QAction(s.notes, sub)
                hint.setEnabled(False)
                sub.addAction(hint)
                sub.addSeparator()
            for f_khz in s.freqs_khz:
                act = QAction(
                    f"{f_khz/1000:.3f} MHz  ({s.mode})", sub)
                act.triggered.connect(
                    lambda _c=False, st=s, fk=f_khz:
                        self._tune_time_station(st, fk))
                sub.addAction(act)
        menu.addSeparator()
        # Reset cycle index for those who like to start fresh.
        reset_act = QAction("Reset cycle to first entry", menu)
        reset_act.triggered.connect(self._reset_time_cycle)
        menu.addAction(reset_act)
        menu.exec(anchor_btn.mapToGlobal(pos))

    def _tune_time_station(self, station, freq_khz: int) -> None:
        """Tune directly to a specific time station + frequency from
        the right-click menu.  Updates the cycle index so the next
        plain click follows on from this position."""
        from PySide6.QtCore import QSettings as _QS
        from lyra.data.time_stations import order_stations
        country = ""
        try:
            country = self.radio.operator_country_iso
        except AttributeError:
            country = ""
        stations = order_stations(country)
        # Find the absolute cycle index of the chosen (station, freq)
        # so a subsequent plain click cycles correctly.
        running = 0
        new_idx = 0
        for s in stations:
            if s.id == station.id:
                try:
                    new_idx = running + s.freqs_khz.index(freq_khz)
                except ValueError:
                    new_idx = running
                break
            running += len(s.freqs_khz)
        qs = _QS("N8SDR", "Lyra")
        # Store CURRENT entry's index + 1 so next click advances past.
        qs.setValue("bands/time_cycle_idx", new_idx + 1)
        self.radio.set_mode(station.mode)
        self.radio.set_freq_hz(freq_khz * 1000)
        try:
            self.radio.status_message.emit(
                f"TIME: {station.id} on {freq_khz/1000:.3f} MHz "
                f"({station.mode}) -- {station.name}",
                3000)
        except Exception:
            pass
        self._active_gen = None

    def _reset_time_cycle(self) -> None:
        """Reset the time-station cycle index to 0.  Operator's first
        click after this lands on the highest-priority station's
        lowest frequency."""
        from PySide6.QtCore import QSettings as _QS
        qs = _QS("N8SDR", "Lyra")
        qs.setValue("bands/time_cycle_idx", 0)
        try:
            self.radio.status_message.emit(
                "TIME cycle reset", 1500)
        except Exception:
            pass

    # ── GEN1/2/3 customization (v0.0.9 Step 2) ────────────────────

    def _load_gen_memory(self) -> None:
        """Hydrate ``self._gen_memory`` and ``self._gen_labels`` from
        QSettings.  Slots without a stored value keep their
        bands.py-coded defaults.  Called once at __init__ time."""
        from PySide6.QtCore import QSettings as _QS
        qs = _QS("N8SDR", "Lyra")
        for g in GEN_SLOTS:
            freq_key = f"bands/{g.name.lower()}_freq_hz"
            mode_key = f"bands/{g.name.lower()}_mode"
            label_key = f"bands/{g.name.lower()}_label"
            try:
                freq = int(qs.value(freq_key, g.default_hz))
            except (ValueError, TypeError):
                freq = g.default_hz
            mode = str(qs.value(mode_key, g.default_mode) or g.default_mode)
            label = str(qs.value(label_key, "") or "")
            self._gen_memory[g.name] = (freq, mode)
            self._gen_labels[g.name] = label[:30]   # 30-char clamp

    def _save_gen_memory(self, slot: str) -> None:
        """Persist a single GEN slot's freq / mode / label to
        QSettings.  Called after Save Current, Set Custom Label, or
        Reset to Default."""
        from PySide6.QtCore import QSettings as _QS
        qs = _QS("N8SDR", "Lyra")
        freq, mode = self._gen_memory[slot]
        label = self._gen_labels.get(slot, "")
        qs.setValue(f"bands/{slot.lower()}_freq_hz", int(freq))
        qs.setValue(f"bands/{slot.lower()}_mode", str(mode))
        qs.setValue(f"bands/{slot.lower()}_label", str(label))

    def _update_gen_tooltip(self, slot: str) -> None:
        """Refresh the tooltip on a GEN button to reflect the current
        saved freq / mode / label."""
        btn = self._gen_buttons.get(slot)
        if btn is None:
            return
        freq, mode = self._gen_memory[slot]
        label = self._gen_labels.get(slot, "")
        tip_lines = [
            f"{slot} — general-coverage memory slot.",
        ]
        if label:
            tip_lines.append(f'  "{label}"')
        tip_lines.append(
            f"Saved: {freq/1e6:.4f} MHz, {mode}")
        tip_lines.append("")
        tip_lines.append("Click: tune to saved freq + mode.")
        tip_lines.append("Right-click: save current / set label / reset.")
        btn.setToolTip("\n".join(tip_lines))

    def _show_gen_menu(self, slot: str, anchor_btn, pos) -> None:
        """Right-click menu for a GEN slot.  Operator-facing
        actions: save current, set custom label, reset to default.
        See v0.0.9_memory_stations_design.md §3 for the UX spec."""
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        # Header showing what's currently saved.
        freq, mode = self._gen_memory[slot]
        label = self._gen_labels.get(slot, "")
        hdr_text = f"{slot}: {freq/1e6:.4f} MHz, {mode}"
        if label:
            hdr_text += f'  ("{label}")'
        hdr = QAction(hdr_text, menu)
        hdr.setEnabled(False)
        menu.addAction(hdr)
        menu.addSeparator()
        # Primary action: save current freq + mode here.
        save_act = QAction("Save current freq + mode here…", menu)
        save_act.triggered.connect(
            lambda _c=False: self._gen_save_current(slot))
        menu.addAction(save_act)
        # Custom label.
        label_act = QAction("Set custom label…", menu)
        label_act.triggered.connect(
            lambda _c=False: self._gen_set_label(slot))
        menu.addAction(label_act)
        menu.addSeparator()
        # Reset to bands.py default.
        reset_act = QAction("Reset to default", menu)
        reset_act.triggered.connect(
            lambda _c=False: self._gen_reset_default(slot))
        menu.addAction(reset_act)
        menu.exec(anchor_btn.mapToGlobal(pos))

    def _gen_save_current(self, slot: str) -> None:
        """Show a confirm dialog asking the operator to commit the
        current radio freq + mode + optional label to ``slot``.

        Per design doc §3.3 -- the dialog shows BOTH what's about
        to be saved (current radio state) AND what's being
        replaced (existing slot contents) so the operator can see
        what they're losing.  Cancel = no-op.  Save = overwrite
        QSettings, refresh tooltip, status-bar toast."""
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QLabel, QLineEdit,
            QVBoxLayout,
        )
        cur_freq = int(self.radio.freq_hz)
        cur_mode = str(self.radio.mode)
        old_freq, old_mode = self._gen_memory[slot]
        old_label = self._gen_labels.get(slot, "")
        # Build a small confirm dialog.  Inline rather than a
        # separate class because this is operator-friction-light
        # and we don't reuse the layout elsewhere.
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Save {slot} preset?")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"<b>Save current frequency + mode to {slot}?</b>"))
        v.addWidget(QLabel(
            f"<p>Current: <b>{cur_freq/1e6:.4f} MHz</b>, "
            f"<b>{cur_mode}</b></p>"
            f"<p>Existing: <b>{old_freq/1e6:.4f} MHz</b>, "
            f"<b>{old_mode}</b>"
            + (f' ("{old_label}")' if old_label else "")
            + "</p>"))
        v.addWidget(QLabel("Optional label (30 chars max):"))
        edit = QLineEdit(old_label, dlg)
        edit.setMaxLength(30)
        v.addWidget(edit)
        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        # Commit.
        new_label = edit.text().strip()[:30]
        self._gen_memory[slot] = (cur_freq, cur_mode)
        self._gen_labels[slot] = new_label
        self._save_gen_memory(slot)
        self._update_gen_tooltip(slot)
        # Status-bar toast.
        try:
            label_part = f' ("{new_label}")' if new_label else ""
            self.radio.status_message.emit(
                f"{slot} saved: {cur_freq/1e6:.4f} MHz, "
                f"{cur_mode}{label_part}", 2500)
        except Exception:
            pass

    def _gen_set_label(self, slot: str) -> None:
        """Open a small text-input dialog letting the operator name
        an existing GEN preset without changing its freq/mode."""
        from PySide6.QtWidgets import QInputDialog
        cur_label = self._gen_labels.get(slot, "")
        text, ok = QInputDialog.getText(
            self, f"Set {slot} label",
            f"Custom label for {slot} (30 chars max, blank to clear):",
            text=cur_label,
        )
        if not ok:
            return
        self._gen_labels[slot] = text.strip()[:30]
        self._save_gen_memory(slot)
        self._update_gen_tooltip(slot)
        try:
            self.radio.status_message.emit(
                f"{slot} label updated", 1500)
        except Exception:
            pass

    def _gen_reset_default(self, slot: str) -> None:
        """Restore the bands.py-coded default for a GEN slot.  Quick
        action with status-bar toast (no confirm dialog -- design
        doc §3.2 specifies confirm-on-save only, not on reset)."""
        # Find the slot's default in GEN_SLOTS.
        defaults = next(
            (g for g in GEN_SLOTS if g.name == slot), None)
        if defaults is None:
            return
        self._gen_memory[slot] = (
            defaults.default_hz, defaults.default_mode)
        self._gen_labels[slot] = ""
        self._save_gen_memory(slot)
        self._update_gen_tooltip(slot)
        try:
            self.radio.status_message.emit(
                f"{slot} reset to default "
                f"({defaults.default_hz/1e6:.4f} MHz, "
                f"{defaults.default_mode})", 2000)
        except Exception:
            pass

    # ── Memory presets (v0.0.9 Step 3a) ───────────────────────────

    def _update_memory_tooltip(self) -> None:
        """Refresh the Mem button tooltip to reflect current bank
        size."""
        if not hasattr(self, "_memory_button"):
            return
        n = self._memory.count
        cap = self._memory.MAX_PRESETS
        lines = [f"Memory presets — {n} of {cap} saved"]
        if n > 0:
            lines.append("")
            lines.append("Click: dropdown to recall a saved preset.")
            lines.append(
                "Right-click: save current, delete, manage.")
        else:
            lines.append("")
            lines.append("Right-click to save the current freq+mode.")
        self._memory_button.setToolTip("\n".join(lines))

    def _show_memory_recall_menu(self, anchor) -> None:
        """Plain-left-click handler: drop a popup menu listing the
        operator's saved presets.  Click any to recall its freq +
        mode.  An empty bank gets a hint pointing the operator at
        the right-click save action."""
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        presets = self._memory.list()
        if not presets:
            empty = QAction(
                "(no presets saved -- right-click to save current)",
                menu)
            empty.setEnabled(False)
            menu.addAction(empty)
        else:
            for i, p in enumerate(presets):
                txt = (f"{p.name}  —  {p.freq_hz/1e6:.4f} MHz  "
                       f"{p.mode}")
                act = QAction(txt, menu)
                if p.notes:
                    act.setToolTip(p.notes)
                act.triggered.connect(
                    lambda _c=False, idx=i:
                        self._recall_memory(idx))
                menu.addAction(act)
        # Pop the menu just below the button so it feels like
        # a real dropdown rather than a context menu.
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _show_memory_manage_menu(self, anchor, pos) -> None:
        """Right-click management menu.  Save current, recall
        submenu, delete submenu, "manage..." entry pointing at
        Settings (3b will enable that)."""
        from PySide6.QtWidgets import QMenu
        from lyra.memory import MemoryStore
        menu = QMenu(self)
        # Save current as new preset.
        if self._memory.at_max:
            save_text = (f"Save current as new preset  "
                         f"(bank full — max {MemoryStore.MAX_PRESETS})")
        else:
            n = self._memory.count
            save_text = (
                f"Save current as new preset…  "
                f"({n} / {MemoryStore.MAX_PRESETS} used)")
        save_act = QAction(save_text, menu)
        save_act.setEnabled(not self._memory.at_max)
        save_act.triggered.connect(self._save_current_to_memory)
        menu.addAction(save_act)
        # Per-preset actions when bank is non-empty.
        if self._memory.count > 0:
            menu.addSeparator()
            recall_sub = menu.addMenu("Recall preset")
            del_sub = menu.addMenu("Delete preset")
            for i, p in enumerate(self._memory.list()):
                short = (f"{p.name}  ({p.freq_hz/1e6:.4f} MHz "
                         f"{p.mode})")
                act = QAction(short, recall_sub)
                act.triggered.connect(
                    lambda _c=False, idx=i:
                        self._recall_memory(idx))
                recall_sub.addAction(act)
                # Delete entry uses just the name for compactness;
                # hover shows the freq+mode.
                del_act = QAction(p.name, del_sub)
                del_act.setToolTip(short)
                del_act.triggered.connect(
                    lambda _c=False, idx=i:
                        self._delete_memory_with_confirm(idx))
                del_sub.addAction(del_act)
        menu.addSeparator()
        # v0.0.9 Step 3b: opens Settings → Bands → Memory for the
        # full table-view management UI (edit / delete / reorder /
        # import / export to CSV).
        manage_act = QAction(
            "Manage presets…  (Settings → Bands → Memory)", menu)
        manage_act.triggered.connect(self._open_memory_settings)
        menu.addAction(manage_act)
        menu.exec(anchor.mapToGlobal(pos))

    def _open_memory_settings(self) -> None:
        """Open the Settings dialog already navigated to Bands →
        Memory.  Construction-time navigation (vs. post-exec)
        because ``SettingsDialog.exec()`` is modal-blocking and
        any post-call hook would never fire until the dialog
        already closed.
        """
        try:
            from PySide6.QtWidgets import QApplication
            from lyra.ui.settings_dialog import SettingsDialog
            # Locate the running MainWindow so we get the right
            # parent + the TciServer reference SettingsDialog needs.
            mw = None
            for w in QApplication.topLevelWidgets():
                if hasattr(w, "pnl_tci") and hasattr(w, "radio"):
                    mw = w
                    break
            if mw is None:
                return
            tci_server = mw.pnl_tci.server
            dlg = SettingsDialog(self.radio, tci_server, parent=mw)
            # Navigate to Bands BEFORE exec() so the dialog opens
            # already on the right tab.
            dlg.show_tab("Bands")
            if (hasattr(dlg, "tab_bands")
                    and hasattr(dlg.tab_bands, "show_memory_subtab")):
                dlg.tab_bands.show_memory_subtab()
            dlg.exec()
        except Exception as e:
            print(f"[Mem] open settings failed: {e}")

    def _recall_memory(self, idx: int) -> None:
        """Recall a memory preset by index: tune to its freq + mode.
        If an rx_bw_hz override is set, also pin the bandwidth."""
        p = self._memory.get(idx)
        if p is None:
            return
        self.radio.set_mode(p.mode)
        self.radio.set_freq_hz(p.freq_hz)
        if p.rx_bw_hz is not None:
            try:
                self.radio.set_rx_bw(p.mode, p.rx_bw_hz)
            except Exception:
                # Rare: mode might not have a settable BW path.
                # Best-effort -- the freq + mode tune is the
                # important part.
                pass
        self._active_gen = None
        try:
            notes = f' — {p.notes}' if p.notes else ""
            self.radio.status_message.emit(
                f"Recalled '{p.name}': {p.freq_hz/1e6:.4f} MHz "
                f"{p.mode}{notes}", 2500)
        except Exception:
            pass

    def _save_current_to_memory(self) -> None:
        """Open a small dialog asking for a name + optional notes,
        then save the current radio freq + mode under that name.
        Name collision warns and offers overwrite."""
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QLabel, QLineEdit,
            QMessageBox, QVBoxLayout,
        )
        from lyra.memory import MemoryPreset, MemoryStore
        if self._memory.at_max:
            # Should be unreachable (menu greyed out), but defensive.
            QMessageBox.information(
                self, "Memory bank full",
                f"Bank holds the maximum of {MemoryStore.MAX_PRESETS} "
                "presets.  Delete an existing entry first.")
            return
        cur_freq = int(self.radio.freq_hz)
        cur_mode = str(self.radio.mode)
        # Build dialog inline.
        dlg = QDialog(self)
        dlg.setWindowTitle("Save preset")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"<p>Saving:  <b>{cur_freq/1e6:.4f} MHz</b>, "
            f"<b>{cur_mode}</b></p>"))
        v.addWidget(QLabel(
            f"Name (required, {MemoryStore.MAX_NAME_LEN} chars max):"))
        name_edit = QLineEdit(dlg)
        name_edit.setMaxLength(MemoryStore.MAX_NAME_LEN)
        v.addWidget(name_edit)
        v.addWidget(QLabel(
            f"Notes (optional, "
            f"{MemoryStore.MAX_NOTES_LEN} chars max):"))
        notes_edit = QLineEdit(dlg)
        notes_edit.setMaxLength(MemoryStore.MAX_NOTES_LEN)
        v.addWidget(notes_edit)
        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=dlg)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        # Empty-name guard: keep Save greyed until non-empty.
        save_btn = btns.button(QDialogButtonBox.Save)
        save_btn.setEnabled(False)
        name_edit.textChanged.connect(
            lambda t: save_btn.setEnabled(bool(t.strip())))
        if dlg.exec() != QDialog.Accepted:
            return
        name = name_edit.text().strip()
        notes = notes_edit.text().strip()
        if not name:
            return
        # Name-collision check.
        existing_idx = self._memory.find_by_name(name)
        if existing_idx is not None:
            old = self._memory.get(existing_idx)
            confirm = QMessageBox.question(
                self, "Overwrite existing preset?",
                f"A preset named '{old.name}' already exists "
                f"({old.freq_hz/1e6:.4f} MHz, {old.mode}).\n\n"
                "Overwrite with the current freq+mode?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if confirm != QMessageBox.Yes:
                return
            # Update in place rather than add+delete.
            self._memory.update(
                existing_idx,
                MemoryPreset(name=name, freq_hz=cur_freq,
                             mode=cur_mode, notes=notes))
        else:
            ok = self._memory.add(MemoryPreset(
                name=name, freq_hz=cur_freq, mode=cur_mode,
                notes=notes))
            if not ok:
                QMessageBox.warning(
                    self, "Save failed",
                    "Couldn't save (bank full or invalid input).")
                return
        self._update_memory_tooltip()
        try:
            self.radio.status_message.emit(
                f"Preset '{name}' saved: "
                f"{cur_freq/1e6:.4f} MHz {cur_mode}", 2500)
        except Exception:
            pass

    def _delete_memory_with_confirm(self, idx: int) -> None:
        """Delete a preset after operator confirmation."""
        from PySide6.QtWidgets import QMessageBox
        p = self._memory.get(idx)
        if p is None:
            return
        confirm = QMessageBox.question(
            self, "Delete preset?",
            f"Delete '{p.name}' "
            f"({p.freq_hz/1e6:.4f} MHz {p.mode})?\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self._memory.delete(idx)
        self._update_memory_tooltip()
        try:
            self.radio.status_message.emit(
                f"Preset '{p.name}' deleted", 1500)
        except Exception:
            pass

    def _on_band_clicked(self, band):
        # Per-band memory: restore if previously visited, else use the
        # band's coded default. recall_band handles both cases.
        self._active_gen = None
        self.radio.recall_band(band.name, band.default_hz, band.default_mode)

    def _on_gen_clicked(self, slot_name: str):
        freq, mode = self._gen_memory[slot_name]
        self._active_gen = slot_name
        self.radio.set_freq_hz(freq)
        self.radio.set_mode(mode)

    def _on_freq_changed(self, hz: int):
        current = band_for_freq(hz)
        # Structured band match takes priority over GEN slot highlight.
        for name, btn in self._buttons.items():
            btn.blockSignals(True)
            btn.setChecked(current is not None and current.name == name)
            btn.blockSignals(False)
        # GEN button highlight + auto-save to active slot's memory.
        if current is None and self._active_gen is not None:
            self._gen_memory[self._active_gen] = (hz, self.radio.mode)
        elif current is not None:
            self._active_gen = None
        for name, btn in self._gen_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(self._active_gen == name)
            btn.blockSignals(False)

    def _on_mode_changed(self, mode: str):
        if self._active_gen is not None:
            freq, _ = self._gen_memory[self._active_gen]
            self._gen_memory[self._active_gen] = (self.radio.freq_hz, mode)


# ── TCI server status + control ────────────────────────────────────────
class TciPanel(GlassPanel):
    """Compact TCI control in the main window. Deeper settings live in
    the Settings dialog (Network / TCI tab)."""

    def __init__(self, radio: Radio, parent=None):
        super().__init__("TCI SERVER", parent, help_topic="tci")
        self.radio = radio
        self.server = TciServer(radio)

        # Wire Radio's audio + IQ taps to TciServer's binary
        # broadcast methods (v0.0.9.1+ TCI audio / IQ streaming).
        # Cross-thread-safe: when the worker thread emits these
        # signals, Qt automatically uses QueuedConnection to deliver
        # on the main thread (where TciServer + QWebSocket live).
        # Cost when no TCI clients are subscribed: one early-return
        # in broadcast_audio / broadcast_iq.
        radio.audio_for_tci_emit.connect(self.server.broadcast_audio)
        radio.iq_for_tci_emit.connect(self.server.broadcast_iq)

        h = QHBoxLayout()

        self.enable_btn = QPushButton("Start")
        self.enable_btn.setCheckable(True)
        self.enable_btn.setFixedWidth(70)
        self.enable_btn.toggled.connect(self._on_toggled)
        h.addWidget(self.enable_btn)

        self.status_label = QLabel("stopped")
        self.status_label.setMinimumWidth(220)
        h.addWidget(self.status_label)

        self.settings_btn = QPushButton("Settings…")
        self.settings_btn.setFixedWidth(90)
        self.settings_btn.clicked.connect(self._open_settings)
        h.addWidget(self.settings_btn)

        h.addStretch(1)
        self.content_layout().addLayout(h)

        self.server.running_changed.connect(self._on_running_changed)
        self.server.client_count_changed.connect(self._update_status)
        self.server.status_message.connect(
            lambda t, ms: self.radio.status_message.emit(t, ms))

    def _on_toggled(self, on: bool):
        if on:
            ok = self.server.start()
            if not ok:
                self.enable_btn.blockSignals(True)
                self.enable_btn.setChecked(False)
                self.enable_btn.blockSignals(False)
        else:
            self.server.stop()

    def _on_running_changed(self, running: bool):
        self.enable_btn.setText("Stop" if running else "Start")
        self.enable_btn.setChecked(running)
        self._update_status()

    def _update_status(self, _=None):
        if self.server.is_running:
            n = self.server.client_count
            self.status_label.setText(
                f"{self.server.bind_host}:{self.server.port} — "
                f"{n} client{'s' if n != 1 else ''}")
        else:
            self.status_label.setText("stopped")

    def _open_settings(self):
        # Lazy import so the dialog isn't constructed until needed
        from lyra.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self.radio, self.server, parent=self.window())
        dlg.exec()
        # After settings dialog closes, update our compact readout.
        self._update_status()

    def shutdown(self):
        self.server.stop()


class WaterfallPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("WATERFALL", parent, help_topic="spectrum")
        self.radio = radio

        # Branch on graphics backend (mirror of SpectrumPanel — see
        # that class's __init__ for the full rationale).
        from lyra.ui.gfx import is_gpu_panadapter_active
        if is_gpu_panadapter_active():
            self._setup_gpu_waterfall()
        else:
            self._setup_qpainter_waterfall()

    # ── GPU waterfall (BACKEND_GPU_OPENGL) ─────────────────────────
    def _setup_gpu_waterfall(self) -> None:
        """Phase B.2/B.3 wiring for WaterfallGpuWidget. Connects:

          - waterfall_ready          → push_row (with dB range)
          - palette (seed + change)  → set_palette (256x3 LUT upload)

        DELIBERATELY NOT WIRED:
          - notches overlay (no shader pass yet)
          - click-to-tune, right-click menu, wheel notch-Q
          - tuning-aware redraws (no center/rate display)
        """
        from lyra.ui.spectrum_gpu import WaterfallGpuWidget
        self.widget = WaterfallGpuWidget()
        self.content_layout().addWidget(self.widget)
        self.radio.waterfall_ready.connect(self._gpu_on_waterfall_ready)
        # Click-to-tune (Phase B.5) — route through _on_click so the
        # CW pitch correction applies in GPU mode too.
        self.widget.clicked_freq.connect(self._on_click)
        # Right-click context menu (Phase B.6) — reuses _on_right_click.
        self.widget.right_clicked_freq.connect(self._on_right_click)
        # Notch markers on the waterfall (Phase B.13).
        self.widget.set_notches(self.radio.notch_details)
        self.radio.notches_changed.connect(self.widget.set_notches)
        # Seed the palette from Radio's current selection, and track
        # changes so the operator's Settings → Visuals → Palette
        # combo flips the waterfall colors live (one 768-byte texture
        # update — visible on the very next frame).
        self._gpu_apply_palette(self.radio.waterfall_palette)
        self.radio.waterfall_palette_changed.connect(
            self._gpu_apply_palette)

    def _gpu_on_waterfall_ready(self, spec_db, center_hz, rate):
        self.widget.set_tuning(center_hz, rate)
        lo, hi = self.radio.waterfall_db_range
        self.widget.push_row(spec_db, min_db=lo, max_db=hi)

    def _gpu_apply_palette(self, name: str) -> None:
        """Look up the palette by name in lyra.ui.palettes and push
        the 256x3 RGB array into the widget. No-op if the name is
        unknown — Radio falls back to 'Classic' anyway."""
        try:
            from lyra.ui.palettes import PALETTES
        except ImportError:
            return
        arr = PALETTES.get(name)
        if arr is None:
            arr = PALETTES.get("Classic")
        if arr is not None:
            self.widget.set_palette(arr)

    # ── QPainter waterfall (BACKEND_SOFTWARE / BACKEND_OPENGL) ─────
    def _setup_qpainter_waterfall(self) -> None:
        """Original WaterfallPanel wiring, unchanged."""
        radio = self.radio
        self.widget = WaterfallWidget()
        self.content_layout().addWidget(self.widget)
        self.widget.clicked_freq.connect(self._on_click)
        self.widget.right_clicked_freq.connect(self._on_right_click)
        self.widget.wheel_at_freq.connect(self._on_wheel)
        self.widget.notch_q_drag.connect(self._on_notch_q_drag)
        # Subscribe to waterfall_ready (fires on its own cadence — the
        # Radio gates it by waterfall_divider). This decouples the
        # scrolling heatmap rate from the spectrum FPS so you can, e.g.,
        # run a smooth 30 fps spectrum above a slow-crawl waterfall.
        radio.waterfall_ready.connect(self._on_waterfall_ready)
        radio.notches_changed.connect(self.widget.set_notches)
        # Live palette + dB-range from Visuals settings tab
        self.widget.set_palette(radio.waterfall_palette)
        radio.waterfall_palette_changed.connect(self.widget.set_palette)
        lo, hi = radio.waterfall_db_range
        self.widget.set_db_range(lo, hi)
        radio.waterfall_db_range_changed.connect(self.widget.set_db_range)

    def _on_waterfall_ready(self, spec_db, center_hz, rate):
        self.widget.set_tuning(center_hz, rate)
        self.widget.push_row(spec_db)

    def _on_click(self, freq_hz):
        # Click-to-tune. CW filters sit OFFSET from the marker by
        # ±cw_pitch (CWU passband above the marker, CWL below). For
        # the clicked signal to land INSIDE the filter we tune the
        # marker to (signal - pitch) for CWU, or (signal + pitch)
        # for CWL. Other modes tune directly.
        target = int(freq_hz)
        mode = self.radio.mode
        if mode in ("CWU", "CWL"):
            pitch = int(self.radio.cw_pitch_hz)
            # Panadapter is sky-freq convention (display-mirror flip).
            # CWU signal sits ABOVE the carrier visually, so to put a
            # clicked signal at +pitch from the new VFO we subtract
            # pitch from the click freq. CWL mirrored.
            target += -pitch if mode == "CWU" else +pitch
        self.radio.set_freq_hz(target)

    def _on_right_click(self, freq_hz, shift, global_pos):
        # Mirrors SpectrumPanel — both gestures gated on notch_enabled
        # so right-click stays free for future waterfall-specific
        # features when notches aren't the active concern.
        if shift and self.radio.notch_enabled:
            self.radio.remove_nearest_notch(freq_hz)
            return
        self._show_notch_menu(freq_hz, global_pos)

    def _show_notch_menu(self, freq_hz, global_pos):
        menu = _build_notch_menu(self, self.radio, freq_hz)
        menu.exec(global_pos)

    def _on_wheel(self, freq_hz, delta_units):
        factor = 1.2 ** delta_units
        for f, q in self.radio.notch_details:
            if abs(f - freq_hz) <= self.radio.rate / 8:
                self.radio.set_notch_q_at(f, q * factor)
                return

    def _on_notch_q_drag(self, freq_hz, new_q):
        self.radio.set_notch_q_at(freq_hz, new_q)
