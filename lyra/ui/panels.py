"""All Lyra control panels. Each subclasses GlassPanel and binds to
the central Radio controller via signals.

Panels are split by function (Connection, Tuning, Mode/Filter, Gain,
DSP/Notch, Audio Output, Spectrum, Waterfall, S-Meter). Adding or
relocating panels in the main layout is a one-liner in app.py.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QSizePolicy, QSlider, QStackedWidget, QVBoxLayout,
    QWidget,
)

from lyra.radio import Radio
from lyra.protocol.stream import SAMPLE_RATES
from lyra.ui.panel import GlassPanel
from lyra.ui.spectrum import SpectrumWidget, WaterfallWidget
from lyra.ui.smeter import SMeter, AnalogMeter, LedBarMeter, LitArcMeter
from lyra.control.tci import TciServer, TCI_DEFAULT_PORT
from lyra.bands import AMATEUR_BANDS, BROADCAST_BANDS, GEN_SLOTS, band_for_freq
from lyra.ui.led_freq import FrequencyDisplay


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

        outer = QVBoxLayout()
        outer.setSpacing(4)

        # ── Row 1: RX1 | LOGO | RX2 ──────────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        # Both frequency displays get a max height a bit under the
        # widget default so the row overall is a touch shorter —
        # leaves more visual room for the logo between them.
        FREQ_MAX_H = 46   # smaller digits, logo gets the spotlight

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
        self.freq_display.setMaximumHeight(FREQ_MAX_H)
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
        self.freq_display_rx2.setMaximumHeight(FREQ_MAX_H)
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

        h.addStretch(1)
        self.content_layout().addLayout(h)

        self._refresh_bw_combos()

        radio.mode_changed.connect(self._on_mode_changed)
        radio.rate_changed.connect(self._on_rate_changed)
        radio.rx_bw_changed.connect(self._on_radio_rx_bw_changed)
        radio.tx_bw_changed.connect(self._on_radio_tx_bw_changed)
        radio.bw_lock_changed.connect(self.lock_btn.setChecked)

    @staticmethod
    def _select_combo_data(combo: QComboBox, value):
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

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
            self._select_combo_data(combo, val)
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

    def _on_rate_changed(self, rate: int):
        self.rate_combo.blockSignals(True)
        self._select_combo_data(self.rate_combo, rate)
        self.rate_combo.blockSignals(False)

    def _on_radio_rx_bw_changed(self, mode: str, bw: int):
        if mode == self.radio.mode:
            self.rx_bw_combo.blockSignals(True)
            self._select_combo_data(self.rx_bw_combo, bw)
            self.rx_bw_combo.blockSignals(False)

    def _on_radio_tx_bw_changed(self, mode: str, bw: int):
        if mode == self.radio.mode:
            self.tx_bw_combo.blockSignals(True)
            self._select_combo_data(self.tx_bw_combo, bw)
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
        self.fps_slider = QSlider(Qt.Horizontal)
        self.fps_slider.setObjectName("fps_slider")
        self.fps_slider.setRange(5, 120)   # bumped from 60 → 120 for faster WF max
        self.fps_slider.setValue(radio.spectrum_fps)
        self.fps_slider.setFixedWidth(130)
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

        # Waterfall rate — unified slider covering multiplier (fast)
        # and divider (slow) in one control:
        #
        #   slider val   →  (divider, multiplier)  → rows/sec at 30 fps
        #   -----------  --------------------------  -------------------
        #   0            →  div=1, mult=3            90   (3x max)
        #   1            →  div=1, mult=2            60   (2x)
        #   2            →  div=1, mult=1            30   (normal)
        #   3            →  div=2                    15
        #   …
        #   22           →  div=21                    1.4 (slow crawl)
        #
        # Inverted appearance so RIGHT end = fast (user expectation).
        h.addSpacing(10)
        h.addWidget(QLabel("WF"))
        self.wf_slider = QSlider(Qt.Horizontal)
        self.wf_slider.setObjectName("wf_slider")
        self.wf_slider.setRange(0, 29)   # 0=10× fast, 9=normal, 29=21× slow
        self.wf_slider.setInvertedAppearance(True)   # right = faster
        self.wf_slider.setValue(self._wf_state_to_slider(
            radio.waterfall_divider, radio.waterfall_multiplier))
        self.wf_slider.setFixedWidth(140)
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

    # Unified WF slider encoding. Bumped 2026-04-24 to cover 1..10×
    # multipliers at the fast end (was 1..8×; earlier 1..3×).
    #
    #   slider  0..8  → multiplier 10..2 (fast mode, row duplication)
    #   slider  9     → normal (divider 1, multiplier 1)
    #   slider 10..29 → divider 2..21 (slow crawl, one row per N ticks)
    @staticmethod
    def _wf_slider_to_state(v: int) -> tuple[int, int]:
        """slider value → (divider, multiplier)."""
        if v <= 8:
            return (1, 10 - v)       # 0→10×, 1→9×, …, 8→2×
        if v == 9:
            return (1, 1)            # normal
        return (v - 8, 1)            # 10→div=2, 29→div=21

    @staticmethod
    def _wf_state_to_slider(divider: int, multiplier: int) -> int:
        """Inverse of _wf_slider_to_state — for restoring slider
        position from radio state."""
        if multiplier >= 2:
            return max(0, min(8, 10 - multiplier))
        if divider <= 1:
            return 9
        return min(29, divider + 8)

    def _rows_per_sec(self) -> float:
        fps = self.radio.spectrum_fps
        div = max(1, self.radio.waterfall_divider)
        mult = max(1, self.radio.waterfall_multiplier)
        return fps * mult / div

    def _refresh_fps_tooltip(self, fps: int):
        self.fps_slider.setToolTip(
            f"Spectrum refresh rate — {fps} fps. Lower = less CPU / GPU "
            "load, laggier trace. Higher = smoother but more work.")

    def _refresh_wf_tooltip(self):
        rps = self._rows_per_sec()
        mult = self.radio.waterfall_multiplier
        div = self.radio.waterfall_divider
        extra = ""
        if mult > 1:
            extra = f"  (fast mode: {mult}x row duplication)"
        elif div > 1:
            extra = f"  (1 row per {div} FFT ticks)"
        self.wf_slider.setToolTip(
            f"Waterfall scroll rate — {rps:.1f} rows/sec{extra}. "
            "Right = faster scroll (up to 3x spectrum FPS), left = "
            "slow crawl with more time-history visible.")

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

    def _on_fps_slider_drag(self, fps: int):
        """valueChanged fires constantly during drag AND for click-
        jumps / keyboard / programmatic setValue. While the mouse is
        actively held, only the tooltip updates — radio is left alone
        so the FFT timer's setInterval isn't hammered. Non-drag
        changes (no preceding sliderPressed) fall through to the
        75 ms debounce path."""
        self._refresh_fps_tooltip(fps)
        self._refresh_wf_tooltip()
        if self._fps_dragging:
            return
        self._fps_debounce.start()

    def _commit_fps_value(self):
        self.radio.set_spectrum_fps(self.fps_slider.value())

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
        if self.fps_slider.value() != fps:
            self.fps_slider.blockSignals(True)
            self.fps_slider.setValue(fps)
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

        # AF Gain slider — makeup gain in dB (0..+50), LINEAR (1 tick
        # = 1 dB). Sits BETWEEN AGC and Volume in the signal path:
        #     demod → AGC → AF Gain → Volume → tanh → sink
        # Designed for AGC-off operation (digital modes, contesters,
        # monitoring) where AGC isn't available to bring the signal
        # up to listenable level. Set once per station based on your
        # typical antenna/band level, then forget — Volume rides on
        # moment-to-moment listening comfort.
        #
        # Linear dB mapping (not perceptual curve) because makeup
        # gain is naturally thought of in dB by operators: "this band
        # needs another 15 dB" is a concrete adjustment.
        levels.addWidget(QLabel("AF"))
        self.af_gain_slider = QSlider(Qt.Horizontal)
        self.af_gain_slider.setObjectName("af_gain_slider")
        self.af_gain_slider.setRange(0, 50)
        self.af_gain_slider.setSingleStep(1)
        self.af_gain_slider.setPageStep(5)
        self.af_gain_slider.setValue(int(radio.af_gain_db))
        self.af_gain_slider.setFixedWidth(120)
        self.af_gain_slider.setToolTip(
            "AF Gain — post-demod makeup gain, 0 to +50 dB.\n\n"
            "Use this when AGC is off (digital modes) or the AGC "
            "target is too quiet for weak signals. Set once for "
            "your station's typical signal level, then ride Volume "
            "for moment-to-moment listening comfort.\n\n"
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
        self.out_combo.addItems(["AK4951", "PC Soundcard"])
        self.out_combo.setCurrentText(radio.audio_output)
        self.out_combo.setFixedWidth(120)
        self.out_combo.currentTextChanged.connect(self.radio.set_audio_output)
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
            ("APF", "Audio Peak Filter — narrow CW peaking"),
            ("NF",  "Notch Filter — manual notches (this panel)"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("dsp_btn")     # picks up the orange-when-on QSS
            btn.setCheckable(True)
            btn.setToolTip(tip)
            dsp_row.addWidget(btn)
            self.dsp_btns[label] = btn

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
        dsp_row.addWidget(self.notch_info)

        # ── NR (Noise Reduction) ─────────────────────────────────
        # Left-click  = toggle enable/disable
        # Right-click = profile menu (Light / Medium / Aggressive /
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
            "(Light / Medium / Aggressive / Neural)")
        radio.nr_enabled_changed.connect(self._on_nr_enabled_changed)
        radio.nr_profile_changed.connect(self._on_nr_profile_changed)

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
        for w in (agc_panel_label, self.agc_profile_lbl, thr_label,
                  self.agc_threshold_lbl, action_label, self.agc_action_lbl):
            w.setContextMenuPolicy(Qt.CustomContextMenu)
            w.customContextMenuRequested.connect(self._show_agc_menu)

        dsp_row.addStretch(1)

        # Shortcut to Settings → DSP tab. Wider so "DSP Settings…" fits
        # comfortably with the default button padding.
        self.dsp_settings_btn = QPushButton("DSP Settings…")
        self.dsp_settings_btn.setFixedWidth(140)
        self.dsp_settings_btn.setToolTip(
            "Open DSP settings (AGC profile + threshold, NB/NR/EQ)")
        self.dsp_settings_btn.clicked.connect(self._open_dsp_settings)
        dsp_row.addWidget(self.dsp_settings_btn)

        self.content_layout().addLayout(dsp_row)

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
    _NR_PROFILE_LABELS = {
        "light":      "Light",
        "medium":     "Medium",
        "aggressive": "Aggressive",
        "neural":     "Neural (RNNoise / DeepFilterNet)",
    }

    def _show_nr_menu(self, pos):
        """Right-click on the NR button pops a profile picker.
        Mirrors the AGC right-click idiom — radio buttons with the
        current profile checked. The Neural entry is greyed out if no
        neural-NR package is importable on this system."""
        btn = self.dsp_btns["NR"]
        menu = QMenu(self)
        current = self.radio.nr_profile
        neural_ok = Radio.neural_nr_available()
        for key, label in self._NR_PROFILE_LABELS.items():
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(key == current)
            if key == "neural" and not neural_ok:
                act.setEnabled(False)
                act.setText(f"{label}  (install RNNoise or DeepFilterNet)")
            act.triggered.connect(
                lambda _=False, k=key: self.radio.set_nr_profile(k))
            menu.addAction(act)
        menu.addSeparator()
        toggle_act = QAction(
            "Disable NR" if self.radio.nr_enabled else "Enable NR", menu)
        toggle_act.triggered.connect(
            lambda: self.radio.set_nr_enabled(not self.radio.nr_enabled))
        menu.addAction(toggle_act)
        menu.exec(btn.mapToGlobal(pos))

    def _on_nr_enabled_changed(self, on: bool):
        btn = self.dsp_btns["NR"]
        if btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)

    def _on_nr_profile_changed(self, name: str):
        """Update the NR button's tooltip so hover reflects the
        active profile. The button itself only shows 'NR' text (no
        room for the profile on the compact button row)."""
        label = self._NR_PROFILE_LABELS.get(name, name)
        self.dsp_btns["NR"].setToolTip(
            f"Noise Reduction — active profile: {label}.\n"
            "Left-click: toggle on/off.\n"
            "Right-click: change profile.")

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
            ("peak", "Peak (instant max in passband)",
             "Shows the strongest single FFT bin inside the RX "
             "passband. Responsive but jumpy on transients (CW dits, "
             "FT8 tones, lightning crashes)."),
            ("avg",  "Average (time-smoothed mean)",
             "Average of all bins in the passband, smoothed with a "
             "~5-frame EWMA. Steadier reading; better representation "
             "of the actual signal level the AGC sees."),
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
        if nearest.deep:
            flag_str.append("DEEP")
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
        # Deep-mode toggle (cascade)
        deep_label = ("Make this notch normal (1× iirnotch)"
                      if nearest.deep else
                      "Make this notch DEEP (cascade — ~2× attenuation)")
        deep_act = QAction(deep_label, menu)
        deep_act.triggered.connect(
            lambda _=False, f=nearest.abs_freq_hz:
                radio.toggle_notch_deep_at(f))
        menu.addAction(deep_act)

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

    # Turn-off action — convenient exit from notch mode back to
    # "right-click does nothing notch-related" state. Sits at the
    # bottom so it's out of the way of the common Add action.
    menu.addSeparator()
    off_act = QAction("Disable Notch Filter", menu)
    off_act.triggered.connect(
        lambda: radio.set_notch_enabled(False))
    menu.addAction(off_act)

    return menu


# ── Spectrum / Waterfall panels ─────────────────────────────────────────
class SpectrumPanel(GlassPanel):
    def __init__(self, radio: Radio, parent=None):
        super().__init__("PANADAPTER", parent, help_topic="spectrum")
        self.radio = radio
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

    def _on_click(self, freq_hz):
        self.radio.set_freq_hz(int(freq_hz))

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
        self._gen_memory: dict[str, tuple[int, str]] = {
            g.name: (g.default_hz, g.default_mode) for g in GEN_SLOTS
        }
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
            btn.setToolTip(
                f"{g.name} — general-coverage memory slot.\n"
                f"Click: tune to remembered freq/mode for this slot.\n"
                f"While active, tuning updates this slot's memory.")
            btn.clicked.connect(lambda _c, slot=g.name: self._on_gen_clicked(slot))
            self._gen_buttons[g.name] = btn
            row.addWidget(btn)
        row.addStretch(1)
        return row

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
        self.radio.set_freq_hz(int(freq_hz))

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
