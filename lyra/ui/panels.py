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
    QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from lyra.radio import Radio
from lyra.protocol.stream import SAMPLE_RATES
from lyra.ui.panel import GlassPanel
from lyra.ui.spectrum import SpectrumWidget, WaterfallWidget
from lyra.ui.smeter import SMeter, AnalogMeter, LedBarMeter
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
        from pathlib import Path as _Path
        from PySide6.QtGui import QPixmap as _QPixmap
        logo_path = (_Path(__file__).resolve().parents[2] /
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
        super().__init__("VIEW", parent, help_topic="spectrum")
        self.radio = radio

        h = QHBoxLayout()
        h.setSpacing(6)

        # Zoom combo — same preset levels as Settings + mouse wheel.
        h.addWidget(QLabel("Zoom"))
        self.zoom_combo = QComboBox()
        for lvl in Radio.ZOOM_LEVELS:
            self.zoom_combo.addItem(f"{lvl:g}x", float(lvl))
        self._sync_zoom_combo(radio.zoom)
        self.zoom_combo.setFixedWidth(64)
        self.zoom_combo.setToolTip(
            "Panadapter zoom. Mouse-wheel on empty spectrum steps "
            "through the same levels.")
        self.zoom_combo.currentIndexChanged.connect(self._on_zoom_pick)
        h.addWidget(self.zoom_combo)

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
        self.fps_slider.valueChanged.connect(self._on_fps_changed)
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
        self.wf_slider.valueChanged.connect(self._on_wf_changed)
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
    # feels wildly touchy at low volumes. See slider init for table.
    VOL_MAX = 2.0
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
        self.lna_slider.valueChanged.connect(self.radio.set_gain_db)
        levels.addWidget(self.lna_slider)
        self.lna_label = QLabel(f"{radio.gain_db:+d} dB")
        self.lna_label.setFixedWidth(60)
        levels.addWidget(self.lna_label)

        # Auto-LNA toggle. Lit = control loop is actively nudging LNA
        # gain every 1.5 s to keep the ADC peak near −15 dBFS (±3 dB
        # deadband). Operator can still drag the slider to override —
        # Auto will walk back toward target next tick.
        self.auto_lna_btn = QPushButton("Auto")
        self.auto_lna_btn.setObjectName("dsp_btn")    # orange when on
        self.auto_lna_btn.setCheckable(True)
        self.auto_lna_btn.setFixedWidth(50)
        self.auto_lna_btn.setChecked(radio.lna_auto)
        self.auto_lna_btn.setToolTip(
            "Auto-LNA: continuously adjusts LNA gain to keep ADC peak "
            "near −15 dBFS. Override anytime by dragging the slider.")
        self.auto_lna_btn.toggled.connect(self.radio.set_lna_auto)
        levels.addWidget(self.auto_lna_btn)

        levels.addSpacing(20)

        # Volume slider uses a PERCEPTUAL (quadratic) curve so each
        # 1% tick produces a roughly uniform loudness change. With a
        # linear slider → linear multiplier mapping the bottom end of
        # the slider was unusably sensitive (1% tick = 2x perceptual
        # loudness at low volumes), which is why we route through a
        # curve here rather than calling set_volume(slider/100) directly.
        #
        #   slider 0..100 → multiplier = (slider/100) ** 2 * VOL_MAX
        #   VOL_MAX = 2.0 keeps an extra 6 dB of headroom above unity.
        #   At slider=100 → ×2.0  (loudest — hard limit above unity)
        #   At slider= 71 → ×1.0  (unity gain — "natural" listen level)
        #   At slider= 50 → ×0.5
        #   At slider= 25 → ×0.125
        #   At slider= 10 → ×0.02
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

        # ── Row 2 — Audio output + Notch configuration ──────────────
        h = QHBoxLayout()

        # Audio output selector
        h.addWidget(QLabel("Out"))
        self.out_combo = QComboBox()
        self.out_combo.addItems(["AK4951", "PC Soundcard"])
        self.out_combo.setCurrentText(radio.audio_output)
        self.out_combo.setFixedWidth(120)
        self.out_combo.currentTextChanged.connect(self.radio.set_audio_output)
        h.addWidget(self.out_combo)

        h.addSpacing(14)

        # Notch bank — front-panel footprint kept small. Gestures and
        # "default Q" live in tooltips + the spectrum/waterfall
        # right-click context menu so the row doesn't need a second
        # slider or a four-hundred-pixel legend.
        self.notch_btn = QPushButton("Notch")
        self.notch_btn.setCheckable(True)
        self.notch_btn.setFixedWidth(70)
        self.notch_btn.setToolTip(
            "Notch Filter — manual per-frequency notches.\n\n"
            "On the spectrum or waterfall:\n"
            "  • Right-click          — menu (Add / Remove nearest / Clear all /\n"
            "                            Default Q for new notches)\n"
            "  • Shift + right-click  — quick-remove nearest notch\n"
            "  • Left-drag a notch    — adjust that notch's Q (width)\n"
            "  • Wheel over a notch   — adjust that notch's Q (width)")
        self.notch_btn.toggled.connect(self.radio.set_notch_enabled)
        h.addWidget(self.notch_btn)

        # Compact live counter replaces the old legend-heavy info
        # label. Shows "N notches" + per-notch Qs when present.
        # Full gesture hints are on the Notch button's tooltip above.
        # NO stretch factor here — the old layout used stretch=1 which
        # made the label expand to fill the entire row, leaving a
        # long empty band that looked like broken UI. addStretch below
        # pushes empty space to the end instead so the counter stays
        # tight against the Notch button.
        self.notch_info = QLabel("0 notches")
        self.notch_info.setMinimumWidth(120)
        self.notch_info.setToolTip(self.notch_btn.toolTip())
        h.addWidget(self.notch_info)
        h.addStretch(1)

        self.content_layout().addLayout(h)

        # NOTE: `q_slider` was removed from this row (it was a tiny
        # orange slider labeled "default Q" whose purpose wasn't
        # obvious — per-notch Q is already adjustable via left-drag /
        # wheel-over after creation, so pre-setting a default Q is a
        # power-user knob that belongs in a menu, not the front row).
        # The radio.set_notch_q_default API is still there; it's now
        # reachable via the spectrum/waterfall right-click menu's
        # "Default Q for new notches ▸" submenu.
        self.q_slider = None

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
        self.dsp_btns["NF"].setChecked(radio.notch_enabled)
        self.dsp_btns["NF"].toggled.connect(self.radio.set_notch_enabled)

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
        radio.notch_enabled_changed.connect(self.notch_btn.setChecked)
        radio.notch_enabled_changed.connect(self.dsp_btns["NF"].setChecked)
        # notch_q_changed formerly drove a front-panel slider; the
        # slider was removed (see DSP row notes). Keep no-op so future
        # UI re-exposure (e.g. Settings DSP tab, or a menu readout) has
        # a wiring point.
        radio.notch_q_changed.connect(lambda _q: None)
        radio.audio_output_changed.connect(
            lambda o: self.out_combo.setCurrentText(o) if self.out_combo.currentText() != o else None)
        # LNA gain + Volume feedback (previously lived in GainPanel)
        radio.gain_changed.connect(self._on_gain_changed)
        radio.volume_changed.connect(self._on_volume_changed)
        # Mute + Auto-LNA state sync (signals driven by Radio — covers
        # QSettings load + any future TCI / CAT mute command).
        radio.muted_changed.connect(self._on_muted_changed)
        radio.lna_auto_changed.connect(self._on_lna_auto_changed)

    # Perceptual volume curve — 0..100 slider → 0..VOL_MAX multiplier
    # via a power curve, so each slider tick yields a roughly equal
    # loudness step. Human hearing is logarithmic — a linear slider
    # feels wildly touchy at low volumes. See slider init for table.
    VOL_MAX = 2.0
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

    def _on_muted_changed(self, muted: bool):
        """Radio mute state changed (e.g., via TCI, QSettings load).
        Keep the UI button in sync without firing our own clicked."""
        if self.mute_btn.isChecked() != muted:
            self.mute_btn.blockSignals(True)
            self.mute_btn.setChecked(muted)
            self.mute_btn.blockSignals(False)

    def _on_lna_auto_changed(self, on: bool):
        """Radio Auto-LNA state changed — keep the button in sync."""
        if self.auto_lna_btn.isChecked() != on:
            self.auto_lna_btn.blockSignals(True)
            self.auto_lna_btn.setChecked(on)
            self.auto_lna_btn.blockSignals(False)

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

    def _on_notches_changed(self, pairs):
        # pairs is list[(freq_hz, q)]. Compact counter only — gesture
        # hints live on the Notch button's tooltip so the row stays
        # tight. When there are active notches we append their Qs so
        # the operator can verify at a glance what shapes are in play
        # without having to hover each marker.
        n = len(pairs)
        q_text = ""
        if pairs:
            qs = [q for _, q in pairs]
            q_text = f"  (Q={', '.join(f'{q:.0f}' for q in qs)})"
        self.notch_info.setText(
            f"{n} notch{'es' if n != 1 else ''}{q_text}")

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

    Two implementations share the same signal level input:
      - `AnalogMeter`  (classic radio-dial arc, 4 concentric scales)
      - `LedBarMeter`  (compact LED segmented bar)
    User toggles with the A / LED button in the panel header. Choice
    persists across launches via QSettings.
    """
    def __init__(self, radio: Radio, parent=None):
        super().__init__("METERS", parent, help_topic="smeter")
        self.radio = radio

        # Both widgets instantiated; we just swap which one is visible.
        self.analog_meter = AnalogMeter(title="S")
        self.led_meter = LedBarMeter()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.analog_meter)   # index 0
        self.stack.addWidget(self.led_meter)      # index 1

        # Header row with style toggle
        header = QHBoxLayout()
        self.style_btn = QPushButton("LED")
        self.style_btn.setCheckable(True)
        self.style_btn.setFixedWidth(50)
        self.style_btn.setToolTip(
            "Toggle meter style (off = classic analog, on = LED bar)")
        self.style_btn.toggled.connect(self._on_style_toggled)
        header.addWidget(self.style_btn)
        header.addStretch(1)

        self.content_layout().addLayout(header)
        self.content_layout().addWidget(self.stack)

        # Shared wiring — both meters receive the same signal updates
        radio.smeter_level.connect(self.analog_meter.set_level_dbfs)
        radio.smeter_level.connect(self.led_meter.set_level_dbfs)
        radio.freq_changed.connect(self._on_freq_changed)
        radio.mode_changed.connect(self.analog_meter.set_mode)

        self.analog_meter.set_freq_hz(radio.freq_hz)
        self.analog_meter.set_mode(radio.mode)
        self._on_freq_changed(radio.freq_hz)

    @property
    def style(self) -> str:
        return "led" if self.style_btn.isChecked() else "analog"

    def set_style(self, s: str):
        self.style_btn.setChecked(s == "led")

    def _on_style_toggled(self, on: bool):
        self.stack.setCurrentIndex(1 if on else 0)
        self.style_btn.setText("Analog" if on else "LED")

    def _on_freq_changed(self, hz: int):
        self.analog_meter.set_freq_hz(hz)
        b = band_for_freq(hz)
        self.analog_meter.set_band(b.name if b else "GEN")


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

    # Default-Q submenu. Formerly a front-panel slider (the mystery
    # orange one); now a menu entry so the front row stays clean.
    # The current default is shown with a trailing checkmark so the
    # operator can see what "wider" or "narrower" is relative to.
    menu.addSeparator()
    q_menu = menu.addMenu("Default Q for new notches")
    current_q = float(getattr(radio, "notch_q", 30.0))
    for q_preset, descr in (
        (10,  "very wide — rattly broadcast splatter"),
        (30,  "default — general CW/carrier kill"),
        (60,  "narrow — surgical on one carrier"),
        (100, "very narrow — isolates a single tone"),
        (200, "pinpoint — surgical beacon notch"),
    ):
        label = f"Q = {q_preset}   {descr}"
        if abs(current_q - q_preset) < 0.5:
            label = "✓  " + label
        else:
            label = "    " + label
        act = QAction(label, q_menu)
        act.triggered.connect(
            lambda _checked=False, q=q_preset:
                radio.set_notch_q_default(float(q)))
        q_menu.addAction(act)

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
        # Multiplicative so one tick = noticeable change. Up = higher Q.
        factor = 1.2 ** delta_units
        # Fetch current Q of nearest and multiply
        for f, q in self.radio.notch_details:
            if abs(f - freq_hz) <= self.radio.rate / 8:
                self.radio.set_notch_q_at(f, q * factor)
                return

    def _on_notch_q_drag(self, freq_hz, new_q):
        self.radio.set_notch_q_at(freq_hz, new_q)


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
