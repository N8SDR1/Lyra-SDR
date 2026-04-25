"""Settings dialog — tabbed, extensible.

First tab: Network / TCI. Subsequent tabs: Audio, DSP, Visuals,
Keyer, etc. — additive, each a QWidget that reads from / writes to
the Radio or a subsystem.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QButtonGroup, QColorDialog, QComboBox, QFrame, QRadioButton, QSlider,
)


class _ColorPickLabel(QLabel):
    """Clickable label that represents a color-pick target.

    The label itself is painted in the field's current color and
    bolded — so the operator can see at a glance which color each
    option currently uses, without needing a separate swatch box.

    Left-click aims this field (makes it the target for the next
    preset-palette or custom-picker pick). Right-click resets it to
    factory default — same gesture the old swatch buttons used.
    """

    clicked = Signal(str)           # emits key
    reset_requested = Signal(str)   # emits key

    def __init__(self, key: str, text: str, parent=None):
        super().__init__(text, parent)
        self._key = key
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(
            "Left-click = aim this field for color picking.\n"
            "Right-click = reset to factory default.")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._key)
        elif event.button() == Qt.RightButton:
            self.reset_requested.emit(self._key)
        super().mousePressEvent(event)

from lyra.control.tci import TciServer, TCI_DEFAULT_PORT
from lyra.hardware.oc import format_bits
from lyra.hardware.usb_bcd import list_devices as list_ftdi_devices
from lyra.ui.toggle import ToggleSwitch


class TciSettingsTab(QWidget):
    """Network / TCI settings — parity with reference SDR clients Network tab."""

    def __init__(self, server: TciServer, radio=None, parent=None):
        super().__init__(parent)
        self.server = server
        # `radio` is optional so the tab still constructs standalone (tests,
        # preview). Spot / CW controls only appear when radio is present.
        self.radio = radio

        v = QVBoxLayout(self)

        # ── TCI Server group ────────────────────────────────────────
        grp = QGroupBox("TCI Server")
        g = QGridLayout(grp)
        g.setColumnStretch(2, 1)
        row = 0

        self.enable_chk = QCheckBox("TCI Server Running")
        self.enable_chk.setChecked(server.is_running)
        self.enable_chk.toggled.connect(self._on_enable)
        g.addWidget(self.enable_chk, row, 0, 1, 3)
        row += 1

        g.addWidget(QLabel("Bind IP:Port"), row, 0)
        self.bind_edit = QLineEdit(f"{server.bind_host}:{server.port}")
        self.bind_edit.setFixedWidth(160)
        g.addWidget(self.bind_edit, row, 1)
        default_btn = QPushButton("Default")
        default_btn.setFixedWidth(80)
        default_btn.clicked.connect(self._reset_bind_default)
        g.addWidget(default_btn, row, 2, Qt.AlignLeft)
        row += 1

        g.addWidget(QLabel("Rate Limit (msg/s)"), row, 0)
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(1, 1000)
        self.rate_spin.setValue(int(server.rate_limit_hz))
        self.rate_spin.setFixedWidth(80)
        self.rate_spin.valueChanged.connect(
            lambda v: setattr(self.server, "rate_limit_hz", v))
        g.addWidget(self.rate_spin, row, 1)
        row += 1

        self.init_state_chk = QCheckBox("Send initial VFO state on connect")
        self.init_state_chk.setChecked(server.send_initial_state_on_connect)
        self.init_state_chk.toggled.connect(
            lambda v: setattr(self.server, "send_initial_state_on_connect", v))
        g.addWidget(self.init_state_chk, row, 0, 1, 3)
        row += 1

        g.addWidget(QLabel("Own Callsign"), row, 0)
        self.callsign_edit = QLineEdit(server.own_callsign)
        self.callsign_edit.setFixedWidth(120)
        self.callsign_edit.setPlaceholderText("(for spots)")
        self.callsign_edit.editingFinished.connect(
            lambda: setattr(self.server, "own_callsign",
                            self.callsign_edit.text().strip().upper()))
        g.addWidget(self.callsign_edit, row, 1)
        row += 1

        self.log_chk = QCheckBox("Log TCI traffic to console / viewer")
        self.log_chk.setChecked(server.log_traffic)
        self.log_chk.toggled.connect(
            lambda v: setattr(self.server, "log_traffic", v))
        g.addWidget(self.log_chk, row, 0, 1, 3)
        row += 1

        self.log_btn = QPushButton("Show TCI Log...")
        self.log_btn.clicked.connect(self._show_log)
        g.addWidget(self.log_btn, row, 0)
        row += 1

        # Status
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #8a9aac; font-style: italic;")
        g.addWidget(self.status_label, row, 0, 1, 3)
        self._update_status()

        server.running_changed.connect(lambda _: self._update_status())
        server.client_count_changed.connect(lambda _: self._update_status())

        v.addWidget(grp)

        # ── TCI Spots ────────────────────────────────────────────────
        # DX-cluster / skimmer spots pushed from TCI clients (N1MM+,
        # log4OM, CW Skimmer via TCI). Displayed as markers on the
        # panadapter; click to tune. These controls live here rather
        # than on the front panel because they're "set once and forget".
        spots = QGroupBox("TCI Spots")
        sl = QGridLayout(spots)
        sl.setColumnStretch(2, 1)
        spots_row = 0

        sl.addWidget(QLabel("Max spots"), spots_row, 0)
        self.max_spots_spin = QSpinBox()
        # Capped at 100 — anything more and the panadapter turns into a
        # wall of overlapping call-sign boxes. With FT8 especially, even
        # 30 visible spots is already crowded at a 4 kHz span.
        self.max_spots_spin.setRange(0, 100)
        self.max_spots_spin.setSingleStep(5)
        self.max_spots_spin.setFixedWidth(90)
        self.max_spots_spin.setToolTip(
            "Maximum spots kept in memory (0–100). Oldest are evicted "
            "(LRU) once this is exceeded. Lower values = less clutter "
            "on the panadapter — 20–30 is a sensible default for HF.")
        if self.radio is not None:
            self.max_spots_spin.setValue(self.radio.max_spots)
            self.max_spots_spin.valueChanged.connect(
                lambda v: self.radio.set_max_spots(v))
        sl.addWidget(self.max_spots_spin, spots_row, 1)
        spots_row += 1

        sl.addWidget(QLabel("Lifetime"), spots_row, 0)
        self.lifetime_spin = QSpinBox()
        self.lifetime_spin.setRange(0, 86400)   # up to 24 h
        self.lifetime_spin.setSingleStep(60)
        self.lifetime_spin.setFixedWidth(90)
        self.lifetime_spin.setSuffix(" s")
        self.lifetime_spin.setToolTip(
            "Seconds after which a spot is considered stale and removed "
            "from the panadapter. 0 = never expire. Use the preset "
            "buttons for common values, or type a custom number.")
        if self.radio is not None:
            self.lifetime_spin.setValue(self.radio.spot_lifetime_s)
            self.lifetime_spin.valueChanged.connect(
                lambda v: self.radio.set_spot_lifetime_s(v))
        sl.addWidget(self.lifetime_spin, spots_row, 1)
        # Preset shortcuts — minute-scale for typical ham use. Manual
        # box stays editable for anything else the user wants.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        for label, seconds in (("5 min",  300),
                               ("10 min", 600),
                               ("15 min", 900),
                               ("30 min", 1800)):
            b = QPushButton(label)
            b.setFixedWidth(54)
            b.clicked.connect(
                lambda _=False, s=seconds: self.lifetime_spin.setValue(s))
            preset_row.addWidget(b)
        preset_row.addStretch(1)
        preset_wrap = QWidget()
        preset_wrap.setLayout(preset_row)
        sl.addWidget(preset_wrap, spots_row, 2)
        spots_row += 1

        # Mode filter — mirrors SDRLogger+ idiom: single CSV field,
        # empty = all, case-insensitive, "SSB" auto-expands to match
        # USB/LSB/SSB-tagged cluster spots. Reduces clutter when you
        # only care about a specific mode (e.g. set to "FT8" to hide
        # every CW/SSB spot on a congested band).
        sl.addWidget(QLabel("Mode filter"), spots_row, 0)
        self.mode_filter_edit = QLineEdit()
        self.mode_filter_edit.setPlaceholderText(
            "e.g. FT8,CW,SSB   (empty = show all)")
        self.mode_filter_edit.setToolTip(
            "Comma-separated modes to render on the panadapter. "
            "Case-insensitive. Empty = no filter.\n"
            "'SSB' automatically includes USB and LSB — cluster "
            "spots are almost always tagged USB/LSB, not SSB.\n"
            "Examples:  FT8   |   FT8,FT4   |   CW,SSB   |   "
            "RTTY,PSK31")
        if self.radio is not None:
            self.mode_filter_edit.setText(self.radio.spot_mode_filter_csv)
            self.mode_filter_edit.editingFinished.connect(
                lambda: self.radio.set_spot_mode_filter_csv(
                    self.mode_filter_edit.text()))
        sl.addWidget(self.mode_filter_edit, spots_row, 1, 1, 2)
        spots_row += 1

        # Master clear button
        clear_btn = QPushButton("Clear All Spots")
        clear_btn.setFixedWidth(140)
        clear_btn.setToolTip(
            "Remove every spot from the panadapter. Next spot push from a "
            "TCI client will start the list fresh.")
        if self.radio is not None:
            clear_btn.clicked.connect(self.radio.clear_spots)
        sl.addWidget(clear_btn, spots_row, 0)

        self.spot_count_lbl = QLabel()
        self.spot_count_lbl.setStyleSheet(
            "color: #8a9aac; font-style: italic;")
        sl.addWidget(self.spot_count_lbl, spots_row, 1, 1, 2)
        if self.radio is not None:
            self._update_spot_count()
            self.radio.spots_changed.connect(
                lambda _: self._update_spot_count())
        spots_row += 1

        v.addWidget(spots)

        # ── CW / keying over TCI (placeholder — needs TX path) ──────
        # Planned controls: "CW Skimmer send via TCI", CW keyer keying
        # enable, PTT authorization per-client. Parked as disabled
        # placeholders so the dialog structure is visible and filling
        # them in when TX ships is a mechanical job, not a redesign.
        cwg = QGroupBox("CW / Keying over TCI  (TX path not yet implemented)")
        cwg.setEnabled(False)
        cwl = QGridLayout(cwg)
        cwl.setColumnStretch(1, 1)
        cw_row = 0

        cwl.addWidget(QLabel("Allow CW keying from TCI client"), cw_row, 0)
        self.cw_allow_chk = QCheckBox()
        cwl.addWidget(self.cw_allow_chk, cw_row, 1, Qt.AlignLeft)
        cw_row += 1

        cwl.addWidget(QLabel("Keyer speed limit (WPM)"), cw_row, 0)
        self.cw_speed_spin = QSpinBox()
        self.cw_speed_spin.setRange(5, 60)
        self.cw_speed_spin.setValue(30)
        self.cw_speed_spin.setFixedWidth(80)
        cwl.addWidget(self.cw_speed_spin, cw_row, 1, Qt.AlignLeft)
        cw_row += 1

        cwl.addWidget(QLabel("Forward CW-Skimmer spots (via TCI)"), cw_row, 0)
        cwl.addWidget(QCheckBox(), cw_row, 1, Qt.AlignLeft)
        cw_row += 1

        v.addWidget(cwg)

        # ── PTT policy (placeholder — needs TX path) ────────────────
        pttg = QGroupBox("PTT over TCI  (TX path not yet implemented)")
        pttg.setEnabled(False)
        pttl = QGridLayout(pttg)
        pttl.setColumnStretch(1, 1)
        ptt_row = 0

        pttl.addWidget(QLabel("Allow PTT from TCI clients"), ptt_row, 0)
        pttl.addWidget(QCheckBox(), ptt_row, 1, Qt.AlignLeft)
        ptt_row += 1

        pttl.addWidget(QLabel("Require password"), ptt_row, 0)
        pttl.addWidget(QCheckBox(), ptt_row, 1, Qt.AlignLeft)
        ptt_row += 1

        pttl.addWidget(QLabel("Password"), ptt_row, 0)
        pttl.addWidget(QLineEdit(), ptt_row, 1)
        ptt_row += 1

        v.addWidget(pttg)

        v.addStretch(1)

    def _update_spot_count(self):
        n = len(self.radio.spots) if self.radio is not None else 0
        self.spot_count_lbl.setText(
            f"{n} spot{'s' if n != 1 else ''} currently held")

    def _on_enable(self, checked: bool):
        self._apply_bind_edit()
        if checked:
            ok = self.server.start()
            if not ok:
                self.enable_chk.blockSignals(True)
                self.enable_chk.setChecked(False)
                self.enable_chk.blockSignals(False)
        else:
            self.server.stop()

    def _apply_bind_edit(self):
        text = self.bind_edit.text().strip()
        if ":" in text:
            host, port_str = text.rsplit(":", 1)
        else:
            host, port_str = "127.0.0.1", text
        try:
            self.server.port = int(port_str)
            self.server.bind_host = host or "127.0.0.1"
        except ValueError:
            pass

    def _reset_bind_default(self):
        self.bind_edit.setText(f"127.0.0.1:{TCI_DEFAULT_PORT}")
        self._apply_bind_edit()

    def _update_status(self):
        if self.server.is_running:
            self.status_label.setText(
                f"● listening on {self.server.bind_host}:{self.server.port}  "
                f"— {self.server.client_count} client"
                f"{'s' if self.server.client_count != 1 else ''}")
        else:
            self.status_label.setText("○ stopped")

    def _show_log(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("TCI Log")
        dlg.resize(700, 400)
        layout = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setFont(QFont("Consolas", 9))
        text.setPlainText("\n".join(self.server.traffic_log) or
                          "(no traffic — enable the log checkbox first)")
        layout.addWidget(text)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        layout.addWidget(close, alignment=Qt.AlignRight)
        dlg.exec()


class RadioSettingsTab(QWidget):
    """Radio connection + discovery + autostart options."""

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        v = QVBoxLayout(self)

        grp = QGroupBox("Radio Connection")
        g = QGridLayout(grp)
        g.setColumnStretch(1, 1)

        g.addWidget(QLabel("IP Address"), 0, 0)
        self.ip_edit = QLineEdit(radio.ip)
        self.ip_edit.setFixedWidth(160)
        self.ip_edit.editingFinished.connect(self._commit_ip)
        g.addWidget(self.ip_edit, 0, 1)

        self.discover_btn = QPushButton("Discover")
        self.discover_btn.setToolTip("Broadcast on the LAN to find HL2 radios")
        self.discover_btn.clicked.connect(self._on_discover)
        g.addWidget(self.discover_btn, 0, 2)

        # Status line
        g.addWidget(QLabel("Status"), 1, 0)
        self.status_label = QLabel("●  not connected")
        self.status_label.setStyleSheet("color: #8a9aac;")
        g.addWidget(self.status_label, 1, 1, 1, 2)

        # Connect button (Start/Stop)
        self.connect_btn = QPushButton("Start Streaming")
        self.connect_btn.setCheckable(True)
        self.connect_btn.toggled.connect(self._on_connect_toggled)
        g.addWidget(self.connect_btn, 2, 1)

        v.addWidget(grp)

        grp2 = QGroupBox("Startup")
        g2 = QGridLayout(grp2)
        self.autostart_chk = QCheckBox("Auto-start stream on app launch")
        self.autostart_chk.setToolTip(
            "Begin streaming automatically after discovery on next launch")
        g2.addWidget(self.autostart_chk, 0, 0)
        v.addWidget(grp2)

        # ── Band plan / Region ──────────────────────────────────
        # Drives the colored sub-band strip + landmark triangles at
        # the top of the panadapter, plus an advisory out-of-band
        # toast. HL2 hardware remains unlocked regardless.
        from lyra.band_plan import REGIONS
        grp_bp = QGroupBox("Band plan (panadapter overlay)")
        gbp = QGridLayout(grp_bp)
        gbp.setColumnStretch(1, 1)

        gbp.addWidget(QLabel("Region"), 0, 0)
        self.region_combo = QComboBox()
        for rid, reg in REGIONS.items():
            self.region_combo.addItem(reg["name"], rid)
        # Select current
        for i in range(self.region_combo.count()):
            if self.region_combo.itemData(i) == radio.band_plan_region:
                self.region_combo.setCurrentIndex(i)
                break
        self.region_combo.setToolTip(
            "Region drives sub-band segment colors, landmark "
            "frequencies, and edge-of-band warnings. 'None' disables "
            "all three (HL2 remains unlocked regardless).")
        self.region_combo.currentIndexChanged.connect(
            lambda _i: self.radio.set_band_plan_region(
                str(self.region_combo.currentData())))
        gbp.addWidget(self.region_combo, 0, 1, 1, 2)

        self.bp_seg_chk = QCheckBox("Show sub-band segment strip")
        self.bp_seg_chk.setChecked(radio.band_plan_show_segments)
        self.bp_seg_chk.setToolTip(
            "Thin colored bar at the top of the panadapter showing "
            "CW / DIG / SSB / FM sub-bands per region allocation.")
        self.bp_seg_chk.toggled.connect(
            self.radio.set_band_plan_show_segments)
        gbp.addWidget(self.bp_seg_chk, 1, 0, 1, 3)

        self.bp_marks_chk = QCheckBox(
            "Show landmarks (FT8 / FT4 / WSPR / PSK triangles)")
        self.bp_marks_chk.setChecked(radio.band_plan_show_landmarks)
        self.bp_marks_chk.setToolTip(
            "Small amber triangles marking digimode watering holes. "
            "Future: click-to-tune.")
        self.bp_marks_chk.toggled.connect(
            self.radio.set_band_plan_show_landmarks)
        gbp.addWidget(self.bp_marks_chk, 2, 0, 1, 3)

        self.bp_edge_chk = QCheckBox(
            "Show band-edge warnings + out-of-band toast")
        self.bp_edge_chk.setChecked(radio.band_plan_edge_warn)
        self.bp_edge_chk.setToolTip(
            "Vertical dashed-red line at band edges + a status-bar "
            "toast when you tune into or out of an allocated band.")
        self.bp_edge_chk.toggled.connect(
            self.radio.set_band_plan_edge_warn)
        gbp.addWidget(self.bp_edge_chk, 3, 0, 1, 3)

        v.addWidget(grp_bp)

        v.addStretch(1)

        # Track state from radio
        radio.ip_changed.connect(self._on_ip_changed)
        radio.stream_state_changed.connect(self._on_stream_state_changed)
        self._on_stream_state_changed(radio.is_streaming)

    @property
    def autostart(self) -> bool:
        return self.autostart_chk.isChecked()

    def set_autostart(self, on: bool):
        self.autostart_chk.setChecked(bool(on))

    def _commit_ip(self):
        self.radio.set_ip(self.ip_edit.text().strip())

    def _on_ip_changed(self, ip: str):
        if self.ip_edit.text() != ip:
            self.ip_edit.setText(ip)

    def _on_discover(self):
        self.discover_btn.setEnabled(False)
        try:
            self.radio.discover()
        finally:
            QTimer.singleShot(300, lambda: self.discover_btn.setEnabled(True))

    def _on_connect_toggled(self, on: bool):
        if on and not self.radio.is_streaming:
            self.radio.start()
        elif not on and self.radio.is_streaming:
            self.radio.stop()

    def _on_stream_state_changed(self, running: bool):
        self.connect_btn.blockSignals(True)
        self.connect_btn.setChecked(running)
        self.connect_btn.setText("Stop Streaming" if running else "Start Streaming")
        self.connect_btn.blockSignals(False)
        self.ip_edit.setEnabled(not running)
        self.discover_btn.setEnabled(not running)
        self.status_label.setStyleSheet(
            "color: #39ff14;" if running else "color: #8a9aac;")
        self.status_label.setText(
            "●  streaming" if running else "●  not connected")


class HardwareSettingsTab(QWidget):
    """External hardware — N2ADR filter board, USB-BCD amp control."""

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        v = QVBoxLayout(self)

        # ── N2ADR Filter Board ────────────────────────────────────────
        grp_n2adr = QGroupBox("External Filter Board (N2ADR / compatible)")
        gn = QGridLayout(grp_n2adr)
        gn.setColumnStretch(2, 1)

        gn.addWidget(QLabel("Installed"), 0, 0)
        self.n2adr_toggle = ToggleSwitch(on=radio.filter_board_enabled)
        self.n2adr_toggle.toggled.connect(self.radio.set_filter_board_enabled)
        gn.addWidget(self.n2adr_toggle, 0, 1)

        hint = QLabel(
            "Drives the 7 OC outputs on HL2's J16 to switch the filter "
            "board's relays per band. Implements the standard N2ADR "
            "filter-board preset.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a9aac;")
        gn.addWidget(hint, 1, 0, 1, 3)

        gn.addWidget(QLabel("Current OC pattern:"), 2, 0)
        self.bits_label = QLabel(self._bits_text(radio.oc_bits))
        self.bits_label.setStyleSheet(
            "color: #39ff14; font-family: Consolas, monospace; font-weight: 700;")
        gn.addWidget(self.bits_label, 2, 1, 1, 2)

        v.addWidget(grp_n2adr)

        # ── USB-BCD Amplifier Control ─────────────────────────────────
        grp_bcd = QGroupBox("USB-BCD Cable (External Linear Amp)")
        gb = QGridLayout(grp_bcd)
        gb.setColumnStretch(2, 1)

        # Safety warning — RED, prominent.
        warn = QLabel(
            "⚠  SAFETY: Wrong BCD code at high power can route TX into the "
            "wrong filter and destroy LDMOS devices and the amp's filter "
            "board. Verify wiring AND do a low-power test on every band "
            "before keying full output. Disable here for any cable change.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "color: #ff4444; font-weight: 700; padding: 6px; "
            "border: 1px solid #ff4444; border-radius: 3px; "
            "background: rgba(255,68,68,30);")
        gb.addWidget(warn, 0, 0, 1, 3)

        gb.addWidget(QLabel("FTDI Device"), 1, 0)
        self.ftdi_combo = QComboBox()
        self._refresh_ftdi_devices()
        self.ftdi_combo.currentIndexChanged.connect(self._on_ftdi_changed)
        gb.addWidget(self.ftdi_combo, 1, 1)

        rescan = QPushButton("Rescan")
        rescan.setToolTip("Re-enumerate FTDI USB devices")
        rescan.clicked.connect(self._refresh_ftdi_devices)
        gb.addWidget(rescan, 1, 2)

        gb.addWidget(QLabel("Enable Auto-Bandswitch"), 2, 0)
        self.bcd_toggle = ToggleSwitch(on=radio.usb_bcd_enabled)
        self.bcd_toggle.toggled.connect(self._on_bcd_toggled)
        gb.addWidget(self.bcd_toggle, 2, 1)

        # 60 m was never in the original Yaesu BCD spec. Most amps use
        # their 40 m filter for 60 m operation, so default True.
        gb.addWidget(QLabel("60 m uses 40 m BCD"), 3, 0)
        self.bcd_60as40_toggle = ToggleSwitch(on=radio.bcd_60m_as_40m)
        self.bcd_60as40_toggle.toggled.connect(self.radio.set_bcd_60m_as_40m)
        gb.addWidget(self.bcd_60as40_toggle, 3, 1)
        bcd60_hint = QLabel(
            "Most linear amps share the 40 m filter for 60 m "
            "(there's no 60 m code in the Yaesu standard). Turn this "
            "off only if your amp has a dedicated 60 m filter or you "
            "prefer the amp to bypass on 60 m.")
        bcd60_hint.setWordWrap(True)
        bcd60_hint.setStyleSheet("color: #8a9aac; font-size: 10px;")
        gb.addWidget(bcd60_hint, 4, 0, 1, 3)

        gb.addWidget(QLabel("Current BCD value:"), 5, 0)
        self.bcd_label = QLabel(self._bcd_text(radio.usb_bcd_value, ""))
        self.bcd_label.setStyleSheet(
            "color: #39ff14; font-family: Consolas, monospace; font-weight: 700;")
        gb.addWidget(self.bcd_label, 5, 1, 1, 2)

        gb_hint = QLabel(
            "Yaesu BCD standard: 160m=1, 80m=2, 40m=3, 30m=4, 20m=5, "
            "17m=6, 15m=7, 12m=8, 10m=9, 6m=10. WARC and BC bands send "
            "0 (amp bypasses)."
        )
        gb_hint.setWordWrap(True)
        gb_hint.setStyleSheet("color: #8a9aac; font-size: 10px;")
        gb.addWidget(gb_hint, 6, 0, 1, 3)

        v.addWidget(grp_bcd)
        v.addStretch(1)

        # Bind to radio signals
        radio.oc_bits_changed.connect(self._on_bits_changed)
        radio.filter_board_changed.connect(
            lambda on: self.n2adr_toggle.setChecked(on)
                       if self.n2adr_toggle.isChecked() != on else None)
        radio.bcd_value_changed.connect(self._on_bcd_changed)
        radio.usb_bcd_changed.connect(
            lambda on: self.bcd_toggle.setChecked(on)
                       if self.bcd_toggle.isChecked() != on else None)

    @staticmethod
    def _bits_text(bits: int) -> str:
        return f"0x{bits:02X}  pins {format_bits(bits)}"

    @staticmethod
    def _bcd_text(value: int, band: str) -> str:
        if not band:
            return f"0x{value:02X}  ({value})"
        return f"0x{value:02X}  ({value})  →  {band}"

    def _on_bits_changed(self, bits: int, _human: str):
        self.bits_label.setText(self._bits_text(bits))

    def _on_bcd_changed(self, value: int, band: str):
        self.bcd_label.setText(self._bcd_text(value, band))

    def _refresh_ftdi_devices(self):
        self.ftdi_combo.blockSignals(True)
        self.ftdi_combo.clear()
        devices = list_ftdi_devices()
        has_device = bool(devices)

        if not has_device:
            self.ftdi_combo.addItem("(no FTDI devices detected)", "")
            self.ftdi_combo.setEnabled(False)
        else:
            self.ftdi_combo.setEnabled(True)
            for dev in devices:
                serial = dev.get("serial", "") or "(no serial)"
                desc = dev.get("description", "") or "FTDI"
                self.ftdi_combo.addItem(f"{serial} — {desc}", serial)
            # Select the radio's currently-stored serial if in the list
            current = self.radio.usb_bcd_serial
            for i in range(self.ftdi_combo.count()):
                if self.ftdi_combo.itemData(i) == current:
                    self.ftdi_combo.setCurrentIndex(i)
                    break
        self.ftdi_combo.blockSignals(False)

        # Safety: operator cannot enable BCD output unless the cable is
        # physically present and enumerated. Prevents accidental "amp
        # in TX with wrong filter selected" scenarios.
        if hasattr(self, "bcd_toggle"):
            self.bcd_toggle.setEnabled(has_device)
            self.bcd_toggle.setToolTip(
                "" if has_device
                else "Plug in the FTDI USB-BCD cable and click Rescan.")
            if not has_device and self.bcd_toggle.isChecked():
                # Auto-disable if the device was pulled while enabled
                self.bcd_toggle.setChecked(False)

    def _on_ftdi_changed(self, _idx):
        serial = self.ftdi_combo.currentData() or ""
        self.radio.set_usb_bcd_serial(serial)

    def _on_bcd_toggled(self, on: bool):
        # Apply current device selection before opening
        self.radio.set_usb_bcd_serial(self.ftdi_combo.currentData() or "")
        self.radio.set_usb_bcd_enabled(on)


class DspSettingsTab(QWidget):
    """DSP chain configuration — AGC, NB, NR, ANC, ANF, EQ."""

    # Description + ordering for the AGC profile radio buttons.
    AGC_PROFILE_UI = [
        ("off",    "Off",     "No AGC — volume scales raw demod output"),
        ("fast",   "Fast",    "~150 ms hang, quick release — CW and weak signals"),
        ("med",    "Medium",  "~500 ms hang — general SSB / ragchew (default)"),
        ("slow",   "Slow",    "~2 s hang — DX nets, steady AM broadcast"),
        ("auto",   "Auto",    "Medium release + threshold auto-tracks noise floor every 3 s"),
        ("custom", "Custom",  "User-defined release and hang values"),
    ]

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        v = QVBoxLayout(self)

        # ── AGC profile selector ─────────────────────────────────────
        grp_agc = QGroupBox("AGC (Automatic Gain Control)")
        ga = QGridLayout(grp_agc)

        self._agc_group = QButtonGroup(self)
        self._agc_radios: dict[str, QRadioButton] = {}
        for i, (key, label, tooltip) in enumerate(self.AGC_PROFILE_UI):
            rb = QRadioButton(label)
            rb.setToolTip(tooltip)
            rb.setChecked(radio.agc_profile == key)
            rb.toggled.connect(
                lambda on, k=key: on and self.radio.set_agc_profile(k))
            ga.addWidget(rb, 0, i)
            self._agc_group.addButton(rb, i)
            self._agc_radios[key] = rb

        # Custom sliders — always visible but disabled unless Custom is picked
        ga.addWidget(QLabel("Release"), 1, 0)
        self.release_slider = QSlider(Qt.Horizontal)
        self.release_slider.setRange(1, 100)   # 0.001 .. 0.100
        self.release_slider.setValue(int(radio.agc_release * 1000))
        self.release_slider.setFixedWidth(200)
        self.release_slider.valueChanged.connect(self._on_custom_changed)
        ga.addWidget(self.release_slider, 1, 1, 1, 3)
        self.release_label = QLabel()
        ga.addWidget(self.release_label, 1, 4)

        ga.addWidget(QLabel("Hang"), 2, 0)
        self.hang_slider = QSlider(Qt.Horizontal)
        self.hang_slider.setRange(0, 100)  # blocks → roughly 0..4.5 s
        self.hang_slider.setValue(int(radio.agc_hang_blocks))
        self.hang_slider.setFixedWidth(200)
        self.hang_slider.valueChanged.connect(self._on_custom_changed)
        ga.addWidget(self.hang_slider, 2, 1, 1, 3)
        self.hang_label = QLabel()
        ga.addWidget(self.hang_label, 2, 4)

        # AGC threshold slider + auto button (the right-click-AGC
        # "automatic AGC threshold" equivalent)
        ga.addWidget(QLabel("Threshold"), 3, 0)
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(5, 90)   # 0.05 .. 0.90
        self.threshold_slider.setValue(int(radio.agc_threshold * 100))
        self.threshold_slider.setFixedWidth(200)
        self.threshold_slider.valueChanged.connect(
            lambda v: self.radio.set_agc_threshold(v / 100.0))
        ga.addWidget(self.threshold_slider, 3, 1, 1, 2)
        self.threshold_label = QLabel()
        ga.addWidget(self.threshold_label, 3, 3)
        self.auto_thresh_btn = QPushButton("Auto")
        self.auto_thresh_btn.setToolTip(
            "Set the AGC threshold ~18 dB above the current noise floor.\n"
            "Equivalent to the right-click → 'automatic AGC "
            "threshold'. Best run on a quiet part of the band.")
        self.auto_thresh_btn.clicked.connect(self._on_auto_threshold)
        ga.addWidget(self.auto_thresh_btn, 3, 4)

        # Live action meter
        ga.addWidget(QLabel("Current AGC action:"), 4, 0, 1, 2)
        self.action_label = QLabel("—")
        self.action_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; font-weight: 700;")
        ga.addWidget(self.action_label, 4, 2, 1, 3)

        v.addWidget(grp_agc)

        # Placeholders for the DSP features still being built
        grp_nb = QGroupBox("Noise Blanker (impulse suppression)")
        gb = QVBoxLayout(grp_nb)
        gb.addWidget(QLabel("Threshold / pulse-width controls — coming soon."))
        grp_nb.setEnabled(False)
        v.addWidget(grp_nb)

        grp_nr = QGroupBox("Noise Reduction (adaptive / spectral subtraction)")
        gnr = QVBoxLayout(grp_nr)
        gnr.addWidget(QLabel(
            "Classical spectral subtraction + optional neural "
            "(RNNoise / DeepFilterNet) — coming soon."))
        grp_nr.setEnabled(False)
        v.addWidget(grp_nr)

        grp_eq = QGroupBox("Equalizer (parametric)")
        geq = QVBoxLayout(grp_eq)
        geq.addWidget(QLabel("Parametric RX / TX equalizer — coming soon."))
        grp_eq.setEnabled(False)
        v.addWidget(grp_eq)

        v.addStretch(1)

        self._update_labels()
        self._update_custom_enabled(radio.agc_profile)
        radio.agc_profile_changed.connect(self._on_profile_changed)
        radio.agc_action_db.connect(self._on_action_db)
        radio.agc_threshold_changed.connect(self._on_threshold_changed)

    def _on_custom_changed(self):
        release = self.release_slider.value() / 1000.0
        hang = int(self.hang_slider.value())
        self.radio.set_agc_custom(release, hang)
        self._update_labels()

    def _update_labels(self):
        release = self.release_slider.value() / 1000.0
        hang = self.hang_slider.value()
        # Hang time in ms (each block ≈ 43 ms at 48 kHz, 2048 samples)
        hang_ms = int(hang * 43)
        self.release_label.setText(f"{release:.3f}")
        self.hang_label.setText(f"{hang} blk  ({hang_ms} ms)")
        t = self.threshold_slider.value() / 100.0
        import math
        self.threshold_label.setText(
            f"{t:.2f}  ({20 * math.log10(max(t, 1e-6)):+.0f} dBFS)")

    def _on_threshold_changed(self, value: float):
        v = int(round(value * 100))
        if self.threshold_slider.value() != v:
            self.threshold_slider.blockSignals(True)
            self.threshold_slider.setValue(v)
            self.threshold_slider.blockSignals(False)
        self._update_labels()

    def _on_auto_threshold(self):
        self.radio.auto_set_agc_threshold()

    def _update_custom_enabled(self, profile: str):
        is_custom = profile == "custom"
        self.release_slider.setEnabled(is_custom)
        self.hang_slider.setEnabled(is_custom)

    def _on_profile_changed(self, name: str):
        rb = self._agc_radios.get(name)
        if rb and not rb.isChecked():
            rb.blockSignals(True)
            rb.setChecked(True)
            rb.blockSignals(False)
        # Preset changes live-update the sliders so the operator can see
        # what values each preset uses.
        if name != "custom":
            self.release_slider.blockSignals(True)
            self.hang_slider.blockSignals(True)
            self.release_slider.setValue(int(self.radio.agc_release * 1000))
            self.hang_slider.setValue(int(self.radio.agc_hang_blocks))
            self.release_slider.blockSignals(False)
            self.hang_slider.blockSignals(False)
        self._update_labels()
        self._update_custom_enabled(name)

    def _on_action_db(self, action_db: float):
        self.action_label.setText(f"{action_db:+.1f} dB")


class AudioSettingsTab(QWidget):
    """Audio output configuration.

    Currently hosts the PC Soundcard device picker. Grows over time
    as more audio knobs land (output channel routing, balance,
    per-output gain trim, etc.) — anything that's "where does my
    audio go and how is it shaped before the speakers" belongs here,
    distinct from the DSP tab which is about signal processing.
    """

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        v = QVBoxLayout(self)

        # ── Output sink selector (AK4951 vs PC Soundcard) ──────────
        # Mirror of the "Out" combo on the DSP+Audio panel — exposed
        # here so operators looking under Settings → Audio find it.
        grp_sink = QGroupBox("Output sink")
        gs = QHBoxLayout(grp_sink)
        gs.addWidget(QLabel("Send audio to:"))
        self._sink_combo = QComboBox()
        self._sink_combo.addItems(["AK4951", "PC Soundcard"])
        self._sink_combo.setCurrentText(radio.audio_output)
        self._sink_combo.setMinimumWidth(180)
        self._sink_combo.currentTextChanged.connect(radio.set_audio_output)
        gs.addWidget(self._sink_combo)
        gs.addStretch(1)
        sink_help = QLabel(
            "AK4951 = HL2's onboard codec (line-out jack on the board).\n"
            "PC Soundcard = your computer's audio output."
        )
        sink_help.setStyleSheet("color: #8a9aac; font-size: 10px;")
        v.addWidget(grp_sink)
        v.addWidget(sink_help)
        # Keep the combo in sync if Radio changes the output elsewhere
        # (rate-driven auto-fallback when AK4951 hits a >48k stream).
        radio.audio_output_changed.connect(
            lambda o: self._sink_combo.setCurrentText(o)
            if self._sink_combo.currentText() != o else None
        )

        # ── Output device (PC Soundcard sink) ──────────────────────
        grp_dev = QGroupBox("Output device — PC Soundcard sink")
        gd = QVBoxLayout(grp_dev)

        info = QLabel(
            "Lyra normally auto-picks the WASAPI default output device.\n"
            "Override here if your audio routes through a non-default\n"
            "card (USB audio interface, virtual cable, S/PDIF dongle, etc).\n"
            "Setting takes effect immediately when PC Soundcard is the\n"
            "active sink. Has no effect when AK4951 is selected."
        )
        info.setStyleSheet("color: #8a9aac; font-size: 10px;")
        gd.addWidget(info)

        self._dev_combo = QComboBox()
        self._dev_combo.setMinimumWidth(420)
        gd.addWidget(self._dev_combo)

        # Refresh + status row
        row = QHBoxLayout()
        self._dev_status = QLabel("")
        self._dev_status.setStyleSheet("color: #6a7a8c; font-size: 10px;")
        row.addWidget(self._dev_status, 1)
        refresh_btn = QPushButton("Refresh device list")
        refresh_btn.setFixedWidth(150)
        refresh_btn.clicked.connect(self._populate_devices)
        row.addWidget(refresh_btn)
        gd.addLayout(row)

        v.addWidget(grp_dev)
        v.addStretch(1)

        # Initial population. Done after layout so the combo is sized
        # before items are added (prevents combo width jump).
        self._populate_devices()

        # When Radio changes the device elsewhere (QSettings load,
        # future TCI control), reflect it here.
        radio.pc_audio_device_changed.connect(self._sync_to_radio)

    def _populate_devices(self):
        """Enumerate PortAudio output devices via sounddevice. Lists
        all hostapis (MME, DirectSound, WASAPI, WDM-KS) so the
        operator can pick a specific backend if they want to override
        Lyra's WASAPI-default preference."""
        self._dev_combo.blockSignals(True)
        self._dev_combo.clear()
        # First entry is always "Auto (WASAPI default)" — the safe
        # default. userData=None signals "let SoundDeviceSink pick".
        self._dev_combo.addItem("Auto  (WASAPI default — recommended)", None)
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for idx, dev in enumerate(devices):
                if dev.get("max_output_channels", 0) <= 0:
                    continue
                ha_name = sd.query_hostapis(dev["hostapi"])["name"]
                rate = int(dev.get("default_samplerate", 0))
                ch = dev.get("max_output_channels", 0)
                label = (f"[{idx:>3}] {dev['name']}   "
                         f"({ha_name}, {ch}ch, {rate} Hz)")
                self._dev_combo.addItem(label, idx)
            self._dev_status.setText(
                f"{self._dev_combo.count() - 1} output device(s) detected"
            )
        except Exception as e:
            self._dev_status.setText(f"Device enumeration failed: {e}")
        self._sync_to_radio(self.radio.pc_audio_device_index)
        self._dev_combo.blockSignals(False)
        # Connect AFTER initial population so the sync above doesn't
        # trigger a spurious Radio.set call. Only connect once;
        # subsequent Refresh button clicks just rebuild the items.
        if not getattr(self, "_signal_connected", False):
            self._dev_combo.currentIndexChanged.connect(self._on_device_picked)
            self._signal_connected = True

    def _sync_to_radio(self, current_idx):
        """Set the combo selection to match Radio's current device.
        Called on initial populate and whenever Radio emits
        pc_audio_device_changed."""
        target = -1
        for i in range(self._dev_combo.count()):
            if self._dev_combo.itemData(i) == current_idx:
                target = i
                break
        if target < 0:
            target = 0          # fall back to "Auto"
        if self._dev_combo.currentIndex() != target:
            self._dev_combo.setCurrentIndex(target)

    def _on_device_picked(self, combo_idx: int):
        device = self._dev_combo.itemData(combo_idx)
        # device is None for "Auto", or an int for a specific index.
        self.radio.set_pc_audio_device_index(device)


class VisualsSettingsTab(QWidget):
    """Spectrum + waterfall display options.

    Three groups:
    1. **Graphics backend** — Software / OpenGL / Vulkan radio buttons.
       Read at import time by gfx.py, so changes need a restart — we
       surface this clearly in a help label and persist to QSettings.
    2. **Waterfall palette** — live combo. Each palette is a 256-entry
       LUT defined in palettes.py; switching redraws the waterfall
       from the next row onward (old rows keep their color until they
       scroll off).
    3. **dB range** — four sliders (spectrum min/max, waterfall
       min/max) with live labels. Radio clamps span to ≥ 3 dB so the
       trace can't collapse to a flat line.
    """

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        v = QVBoxLayout(self)

        # ── Graphics backend ──────────────────────────────────────
        from lyra.ui.gfx import (
            ACTIVE_BACKEND, BACKEND_LABELS, BACKEND_SOFTWARE,
            BACKEND_OPENGL, BACKEND_VULKAN,
        )
        grp_gfx = QGroupBox("Graphics backend")
        g = QGridLayout(grp_gfx)
        g.setColumnStretch(1, 1)

        settings = self._settings()
        chosen = str(settings.value(
            "visuals/graphics_backend", BACKEND_SOFTWARE)).lower()

        self._gfx_group = QButtonGroup(self)
        self._gfx_radios: dict[str, QRadioButton] = {}
        row = 0
        for key, label in (
            (BACKEND_SOFTWARE,
             "Software — QPainter on the CPU. Always available; safe "
             "fallback on every GPU."),
            (BACKEND_OPENGL,
             "OpenGL — GPU-accelerated QPainter. Smoother resize / "
             "fullscreen, reduces audio stutter. Recommended."),
            (BACKEND_VULKAN,
             "Vulkan — experimental, not yet implemented."),
        ):
            rb = QRadioButton(BACKEND_LABELS[key])
            rb.setToolTip(label)
            if key == BACKEND_VULKAN:
                rb.setEnabled(False)
            if key == chosen:
                rb.setChecked(True)
            rb.toggled.connect(
                lambda on, k=key: on and self._on_backend_picked(k))
            g.addWidget(rb, row, 0, 1, 2)
            self._gfx_group.addButton(rb)
            self._gfx_radios[key] = rb
            row += 1

        # Status line — tells the operator what backend is actually in
        # use (may differ from the saved preference if OpenGL failed).
        active_label = QLabel(
            f"Currently active: <b>{BACKEND_LABELS[ACTIVE_BACKEND]}</b>"
            + ("" if ACTIVE_BACKEND == chosen else
               "  <span style='color:#ffab47'>(restart required to "
               "apply your selection)</span>"))
        active_label.setTextFormat(Qt.RichText)
        active_label.setStyleSheet(
            "color: #8a9aac; font-size: 10px; padding: 4px 0;")
        g.addWidget(active_label, row, 0, 1, 2)
        v.addWidget(grp_gfx)

        # ── Waterfall palette ─────────────────────────────────────
        from lyra.ui import palettes
        grp_pal = QGroupBox("Waterfall palette")
        gp = QGridLayout(grp_pal)
        gp.setColumnStretch(1, 1)

        gp.addWidget(QLabel("Palette"), 0, 0)
        self.palette_combo = QComboBox()
        for name in palettes.names():
            self.palette_combo.addItem(name)
        current = radio.waterfall_palette
        idx = self.palette_combo.findText(current)
        if idx >= 0:
            self.palette_combo.setCurrentIndex(idx)
        self.palette_combo.setFixedWidth(180)
        self.palette_combo.setToolTip(
            "Colors applied to the waterfall heatmap. Changes are live "
            "from the next FFT row onward; rows already on-screen keep "
            "their existing colors until they scroll off the bottom.")
        self.palette_combo.currentTextChanged.connect(
            self.radio.set_waterfall_palette)
        gp.addWidget(self.palette_combo, 0, 1, Qt.AlignLeft)
        v.addWidget(grp_pal)

        # ── dB ranges (spectrum + waterfall) ──────────────────────
        grp_db = QGroupBox("Signal range (dB)")
        gd = QGridLayout(grp_db)
        gd.setColumnStretch(2, 1)

        # Four sliders: spec min, spec max, wf min, wf max. Range
        # [-150, 0] dBFS covers the useful envelope for HF.
        self._spec_min, self._spec_min_lbl = self._db_slider(
            gd, 0, "Spectrum min",  radio.spectrum_db_range[0])
        self._spec_max, self._spec_max_lbl = self._db_slider(
            gd, 1, "Spectrum max",  radio.spectrum_db_range[1])
        self._wf_min,   self._wf_min_lbl   = self._db_slider(
            gd, 2, "Waterfall min", radio.waterfall_db_range[0])
        self._wf_max,   self._wf_max_lbl   = self._db_slider(
            gd, 3, "Waterfall max", radio.waterfall_db_range[1])

        self._spec_min.valueChanged.connect(self._on_db_changed)
        self._spec_max.valueChanged.connect(self._on_db_changed)
        self._wf_min.valueChanged.connect(self._on_db_changed)
        self._wf_max.valueChanged.connect(self._on_db_changed)

        # Listen for spectrum range changes from the Radio side too
        # — auto-scale's periodic re-fit fires through this path, and
        # we want the sliders here to track so the dialog stays in
        # sync if it happens to be open during an auto-fit.
        radio.spectrum_db_range_changed.connect(self._sync_spec_sliders)
        radio.waterfall_db_range_changed.connect(self._sync_wf_sliders)

        # "Reset" button restores the pre-settings defaults
        reset_btn = QPushButton("Reset to defaults")
        reset_btn.setFixedWidth(150)
        reset_btn.clicked.connect(self._reset_db_ranges)
        gd.addWidget(reset_btn, 4, 0, 1, 3, Qt.AlignLeft)

        # Spectrum auto-scale toggle. Periodic auto-fit of the
        # spectrum dB range to (noise floor − 10) .. (peak + 5).
        # Useful when band conditions change drastically — switching
        # from a quiet 30m to a noisy 40m without manual rescaling.
        # Manual slider drag (above) or Y-axis right-edge drag on
        # the panadapter turns auto-scale OFF so a deliberate
        # adjustment isn't immediately overwritten.
        # Placed at row 10 (after the existing peak-markers controls
        # which occupy rows 6-9) — keep it visually grouped with
        # the dB range section it affects.
        self.auto_scale_chk = QCheckBox(
            "Auto range scaling (spectrum dB scale fits to band)")
        self.auto_scale_chk.setChecked(radio.spectrum_auto_scale)
        self.auto_scale_chk.setToolTip(
            "Continuously fits the spectrum dB range to current\n"
            "band conditions:\n"
            "   low edge  = noise floor − 15 dB\n"
            "   high edge = strongest peak (rolling 10 sec) + 15 dB\n"
            "Updates every ~2 sec.\n\n"
            "Rolling-max ceiling: a strong intermittent signal\n"
            "keeps the top edge raised until ~10 s after it last\n"
            "appeared, so transient peaks don't overshoot the\n"
            "display.\n\n"
            "Manual scale drag (the sliders above, or the panadapter\n"
            "right-edge Y-axis zone) turns this OFF — your deliberate\n"
            "scale wins until you re-enable auto here.")
        self.auto_scale_chk.toggled.connect(
            self.radio.set_spectrum_auto_scale)
        # Keep checkbox in sync if Radio turns it off (manual drag)
        radio.spectrum_auto_scale_changed.connect(
            lambda on: self.auto_scale_chk.setChecked(on)
            if self.auto_scale_chk.isChecked() != on else None)
        gd.addWidget(self.auto_scale_chk, 10, 0, 1, 3, Qt.AlignLeft)

        # Noise-floor marker toggle sits with the other spectrum
        # appearance controls. Default on — it's a quiet, informative
        # reference without adding visual clutter.
        self.nf_chk = QCheckBox(
            "Show noise-floor reference line on the spectrum")
        self.nf_chk.setChecked(radio.noise_floor_enabled)
        self.nf_chk.setToolTip(
            "Dashed sage-green line + dBFS label showing the current "
            "noise floor (20th-percentile FFT, rolling-averaged over "
            "~1 s). Lets you see S/N at a glance without measuring.")
        self.nf_chk.toggled.connect(self.radio.set_noise_floor_enabled)
        gd.addWidget(self.nf_chk, 5, 0, 1, 3, Qt.AlignLeft)

        # ── Colors (user pickers) ────────────────────────────────
        # UI pattern (2026-04-24, revised): no swatch boxes. Each
        # option is represented by its own field-name label, with the
        # label text ITSELF painted in that field's current color and
        # bolded. That way the operator can read the current
        # configuration at a glance — the words "Spectrum trace" are
        # drawn in the spectrum trace color, "Peak markers" in the
        # peak marker color, and so on.
        #
        # Interaction:
        #   1. Click a field label → it becomes the "aim" (underline
        #      + subtle dark background).
        #   2. Click any preset chip in the palette below → that
        #      color applies to the aimed field.
        #   3. "Custom color…" button opens a non-native QColorDialog
        #      as a fallback for colors not in the 18 presets.
        #   4. Right-click any field label → reset that one to its
        #      factory default.
        #   5. "Reset all" button → every field back to defaults.
        grp_col = QGroupBox("Colors")
        gc_outer = QVBoxLayout(grp_col)

        # Field-name labels arranged in a 3-column grid. The text of
        # each label is painted in that field's current color AND
        # bolded, so the operator sees at a glance what every option
        # is currently set to — no separate swatch box needed. The
        # whole label is clickable (left = aim, right = reset).
        sw_grid = QGridLayout()
        sw_grid.setHorizontalSpacing(16)
        sw_grid.setVerticalSpacing(6)

        # Dict of key → _ColorPickLabel (the clickable colored label),
        # plus the matching on-pick callbacks and display text for
        # dialog titles. Name kept as `_color_swatches` for minimal
        # diff from the old swatch-button implementation.
        self._color_swatches: dict[str, _ColorPickLabel] = {}
        self._color_callbacks: dict[str, callable] = {}
        self._color_displays: dict[str, str] = {}
        self._active_swatch_key: str | None = None

        from lyra import band_plan as _bp

        SWATCH_SPECS = [
            # (key, label, current, default, on_pick)
            ("_trace_", "Spectrum trace",
             radio.spectrum_trace_color, "#5ec8ff",
             lambda hx: self.radio.set_spectrum_trace_color(hx)),
            ("_nf_",    "Noise-floor",
             radio.noise_floor_color,   "#78c88c",
             lambda hx: self.radio.set_noise_floor_color(hx)),
            ("_peak_",  "Peak markers",
             radio.peak_markers_color,  "#ffbe5a",
             lambda hx: self.radio.set_peak_markers_color(hx)),
            ("CW",  "CW segments",
             radio.segment_colors.get("CW", ""),
             _bp.SEGMENT_COLORS.get("CW",  "#3c5a9c"),
             lambda hx: self.radio.set_segment_color("CW", hx)),
            ("DIG", "DIG segments",
             radio.segment_colors.get("DIG", ""),
             _bp.SEGMENT_COLORS.get("DIG", "#9c3c9c"),
             lambda hx: self.radio.set_segment_color("DIG", hx)),
            ("SSB", "SSB segments",
             radio.segment_colors.get("SSB", ""),
             _bp.SEGMENT_COLORS.get("SSB", "#3c9c6a"),
             lambda hx: self.radio.set_segment_color("SSB", hx)),
            ("FM",  "FM segments",
             radio.segment_colors.get("FM",  ""),
             _bp.SEGMENT_COLORS.get("FM",  "#c47a2a"),
             lambda hx: self.radio.set_segment_color("FM", hx)),
        ]
        # 3 columns, laid out row by row
        for i, (key, label, cur, dflt, cb) in enumerate(SWATCH_SPECS):
            r, c = divmod(i, 3)
            lbl = self._make_color_swatch(key, label, cur, dflt, cb)
            sw_grid.addWidget(lbl, r, c)
            self._color_swatches[key] = lbl
            self._color_callbacks[key] = cb
            self._color_displays[key] = label
        gc_outer.addLayout(sw_grid)

        # ── Inline preset palette ────────────────────────────────
        # 18 commonly-useful colors in 3 rows of 6. Click any one to
        # apply it to the currently-aimed field. Always visible so
        # picking is two-click: field label → preset chip.
        hint_lbl = QLabel(
            "Click a field name above to aim it, then click a color below:")
        hint_lbl.setStyleSheet(
            "color: #8a9aac; font-size: 10px; font-style: italic; "
            "padding-top: 4px;")
        gc_outer.addWidget(hint_lbl)

        preset_grid = QGridLayout()
        preset_grid.setHorizontalSpacing(2)
        preset_grid.setVerticalSpacing(2)
        PRESETS = [
            # Row 1 — warm
            "#e53935", "#fb8c00", "#ffb300", "#fdd835", "#c0ca33", "#7cb342",
            # Row 2 — cool
            "#26a69a", "#00acc1", "#039be5", "#1e88e5", "#3949ab", "#8e24aa",
            # Row 3 — accents + neutrals
            "#d81b60", "#ff7043", "#6d4c41", "#78909c", "#eceff1", "#ffffff",
        ]
        for i, hx in enumerate(PRESETS):
            r, c = divmod(i, 6)
            chip = QPushButton()
            chip.setFixedSize(28, 22)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(hx)
            chip.setStyleSheet(
                f"QPushButton {{ background: {hx}; "
                f"border: 1px solid #2a3a4a; border-radius: 2px; }}"
                f"QPushButton:hover {{ border: 1px solid #00e5ff; }}")
            chip.clicked.connect(
                lambda _=False, h=hx: self._apply_preset_color(h))
            preset_grid.addWidget(chip, r, c)
        gc_outer.addLayout(preset_grid)

        # Action row: Custom…, Reset-aimed, Reset-all
        btn_row = QHBoxLayout()
        custom_btn = QPushButton("Custom color…")
        custom_btn.setFixedWidth(120)
        custom_btn.setToolTip(
            "Open a full color picker for the aimed field. "
            "Falls back here if the preset palette doesn't have "
            "the exact tone you want.")
        custom_btn.clicked.connect(self._open_custom_picker)
        btn_row.addWidget(custom_btn)

        reset_one_btn = QPushButton("Reset aimed")
        reset_one_btn.setFixedWidth(110)
        reset_one_btn.setToolTip(
            "Reset just the currently-aimed field to its factory "
            "default. Same as right-clicking the label.")
        reset_one_btn.clicked.connect(self._reset_aimed_color)
        btn_row.addWidget(reset_one_btn)

        btn_row.addStretch(1)
        reset_colors_btn = QPushButton("Reset all")
        reset_colors_btn.setFixedWidth(110)
        reset_colors_btn.setToolTip("Reset every color field to defaults.")
        reset_colors_btn.clicked.connect(self._reset_all_colors)
        btn_row.addWidget(reset_colors_btn)
        gc_outer.addLayout(btn_row)

        v.addWidget(grp_col)

        # Aim the first swatch by default so clicking a preset "just
        # works" even before the user taps a swatch. Visible cyan
        # border tells them which one is active.
        self._set_active_swatch("_trace_")

        # Peak markers — in-passband peak-hold overlay. Toggle + a
        # decay-rate slider in dB/second. Slower decay = peaks linger
        # longer; faster decay = peaks fade quickly. Default 10 dB/s
        # means a peak 30 dB above the floor fades in ~3 s.
        self.peak_chk = QCheckBox(
            "Show peak markers (in-passband peak-hold overlay)")
        self.peak_chk.setChecked(radio.peak_markers_enabled)
        self.peak_chk.setToolTip(
            "Amber peak-hold trace drawn only inside the RX filter "
            "passband so you can see the strongest recent peak of "
            "signals within the audible window. Decays linearly.")
        self.peak_chk.toggled.connect(self.radio.set_peak_markers_enabled)
        gd.addWidget(self.peak_chk, 6, 0, 1, 3, Qt.AlignLeft)

        gd.addWidget(QLabel("Decay"), 7, 0)
        self.peak_decay_slider = QSlider(Qt.Horizontal)
        self.peak_decay_slider.setRange(1, 120)   # dB/sec
        self.peak_decay_slider.setValue(int(round(radio.peak_markers_decay_dbps)))
        self.peak_decay_slider.setFixedWidth(240)
        self.peak_decay_slider.setToolTip(
            "Peak decay rate in dB / second. Lower = peaks linger "
            "longer (spot rare weak signals). Higher = peaks follow "
            "the signal closely.")
        self.peak_decay_slider.valueChanged.connect(self._on_peak_decay_changed)
        gd.addWidget(self.peak_decay_slider, 7, 1)
        self.peak_decay_lbl = QLabel(
            f"{int(round(radio.peak_markers_decay_dbps))} dB/s")
        self.peak_decay_lbl.setFixedWidth(80)
        self.peak_decay_lbl.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gd.addWidget(self.peak_decay_lbl, 7, 2, Qt.AlignLeft)

        # Peak-marker render style (Line / Dots / Triangles)
        gd.addWidget(QLabel("Peak style"), 8, 0)
        self.peak_style_combo = QComboBox()
        for label, key in (("Line", "line"),
                           ("Dots", "dots"),
                           ("Triangles", "triangles")):
            self.peak_style_combo.addItem(label, key)
        # Preselect current
        for i in range(self.peak_style_combo.count()):
            if self.peak_style_combo.itemData(i) == radio.peak_markers_style:
                self.peak_style_combo.setCurrentIndex(i)
                break
        self.peak_style_combo.setFixedWidth(110)
        self.peak_style_combo.setToolTip(
            "How peak markers render inside the passband. Dots + "
            "Triangles are discrete marks; Line is a continuous trace.")
        self.peak_style_combo.currentIndexChanged.connect(
            lambda _i: self.radio.set_peak_markers_style(
                str(self.peak_style_combo.currentData())))
        gd.addWidget(self.peak_style_combo, 8, 1, Qt.AlignLeft)

        # Numeric dB readout at peaks (up to 3 strongest in passband)
        self.peak_show_db_chk = QCheckBox(
            "Show peak dB value at strongest peaks")
        self.peak_show_db_chk.setChecked(radio.peak_markers_show_db)
        self.peak_show_db_chk.setToolTip(
            "Label the 3 strongest peaks inside the passband with "
            "their dBFS value. Off by default to keep the spectrum "
            "uncluttered.")
        self.peak_show_db_chk.toggled.connect(
            self.radio.set_peak_markers_show_db)
        gd.addWidget(self.peak_show_db_chk, 9, 0, 1, 3, Qt.AlignLeft)

        v.addWidget(grp_db)

        # ── Spectrum cal trim ─────────────────────────────────────
        # Per-rig calibration offset added to every FFT bin before
        # display. Lyra's FFT math is normalized for true dBFS by
        # default — a unit-amplitude full-scale tone reads exactly
        # 0 dBFS — but the path from the antenna to the ADC has
        # losses that vary by station: preselector insertion loss,
        # antenna efficiency, internal cable loss, LNA cal drift.
        # The cal slider lets the operator dial in a per-rig offset
        # so on-air signal levels match a known reference.
        grp_cal = QGroupBox("Spectrum calibration")
        gc = QGridLayout(grp_cal)
        gc.setColumnStretch(1, 1)

        cal_help = QLabel(
            "Per-rig dB offset added to every spectrum bin before "
            "display. Use to compensate for known pre-LNA losses or "
            "to match a reference signal generator. Default = 0 dB "
            "(true dBFS — a full-scale tone reads as 0).")
        cal_help.setWordWrap(True)
        # Inherit the dialog's default font size (matching "Show peak"
        # / "Show noise floor" chk text); the muted color is the only
        # visual differentiator vs the chk labels.
        cal_help.setStyleSheet("color: #b6c0cc;")
        gc.addWidget(cal_help, 0, 0, 1, 3)

        gc.addWidget(QLabel("Cal"), 1, 0)
        self._cal_slider = QSlider(Qt.Horizontal)
        self._cal_slider.setRange(
            int(radio.SPECTRUM_CAL_MIN_DB),
            int(radio.SPECTRUM_CAL_MAX_DB))
        self._cal_slider.setValue(int(round(radio.spectrum_cal_db)))
        self._cal_slider.setTickPosition(QSlider.TicksBelow)
        self._cal_slider.setTickInterval(10)
        self._cal_slider.setFixedWidth(280)
        self._cal_slider.setToolTip(
            "Spectrum cal — per-rig dB offset.\n\n"
            "Lyra's FFT is normalized so a unit-amplitude tone reads\n"
            "as 0 dBFS by default. The path from antenna to ADC adds\n"
            "losses (preselector, cable, antenna efficiency, LNA cal\n"
            "drift) that can shift readings by tens of dB depending\n"
            "on your station. Dial in an offset here so on-air signal\n"
            "levels match a known reference (e.g. signal generator at\n"
            "a known dBm + path loss).\n\n"
            "Range: -40 to +40 dB. Default 0 = pure theoretical dBFS.\n"
            "Double-click to snap back to zero.")
        self._cal_slider.valueChanged.connect(self._on_cal_changed)
        # Double-click on the slider track resets to zero
        self._cal_slider.mouseDoubleClickEvent = (
            lambda _e: self._cal_slider.setValue(0))
        gc.addWidget(self._cal_slider, 1, 1)
        self._cal_lbl = QLabel(f"{radio.spectrum_cal_db:+.1f} dB")
        self._cal_lbl.setFixedWidth(80)
        self._cal_lbl.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gc.addWidget(self._cal_lbl, 1, 2, Qt.AlignLeft)
        # Two-way sync — Radio can also change cal (e.g. QSettings load)
        radio.spectrum_cal_db_changed.connect(self._on_radio_cal_changed)

        # ── S-meter cal (independent of spectrum cal) ──────────────
        # Adds an offset to the smeter_level signal ONLY (so the
        # meter dBm reading shifts), without touching the spectrum
        # display itself. Lets the operator calibrate the S-meter
        # against a known reference signal — e.g. inject -73 dBm
        # from a signal generator, see what the meter reads, dial in
        # the difference here.
        smeter_help = QLabel(
            "Independent dB offset added to the S-meter reading "
            "ONLY — does not shift the spectrum scale. Use this to "
            "calibrate the meter against a known reference signal. "
            "Tip: right-click the meter face for a one-click "
            "'calibrate to S9 / S5 / -73 dBm' menu.")
        smeter_help.setWordWrap(True)
        smeter_help.setStyleSheet("color: #b6c0cc;")
        gc.addWidget(smeter_help, 2, 0, 1, 3)

        gc.addWidget(QLabel("S-meter"), 3, 0)
        self._smeter_cal_slider = QSlider(Qt.Horizontal)
        self._smeter_cal_slider.setRange(
            int(radio.SMETER_CAL_MIN_DB),
            int(radio.SMETER_CAL_MAX_DB))
        self._smeter_cal_slider.setValue(int(round(radio.smeter_cal_db)))
        self._smeter_cal_slider.setTickPosition(QSlider.TicksBelow)
        self._smeter_cal_slider.setTickInterval(10)
        self._smeter_cal_slider.setFixedWidth(280)
        self._smeter_cal_slider.setToolTip(
            "S-meter cal — per-rig dB offset on the meter reading.\n\n"
            "Independent of the spectrum cal above. Adjust this to "
            "make S9 read -73 dBm (or whatever your reference is)\n"
            "without re-shifting the panadapter scale.\n\n"
            "Range: -40 to +40 dB. Default 0.\n"
            "Double-click to snap back to zero.")
        self._smeter_cal_slider.valueChanged.connect(self._on_smeter_cal_changed)
        self._smeter_cal_slider.mouseDoubleClickEvent = (
            lambda _e: self._smeter_cal_slider.setValue(0))
        gc.addWidget(self._smeter_cal_slider, 3, 1)
        self._smeter_cal_lbl = QLabel(f"{radio.smeter_cal_db:+.1f} dB")
        self._smeter_cal_lbl.setFixedWidth(80)
        self._smeter_cal_lbl.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gc.addWidget(self._smeter_cal_lbl, 3, 2, Qt.AlignLeft)
        radio.smeter_cal_db_changed.connect(self._on_radio_smeter_cal_changed)

        v.addWidget(grp_cal)

        # ── Update rates + panadapter zoom ────────────────────────
        # Zoom crops to centered FFT bins; spectrum FPS drives the
        # refresh timer; waterfall divider decouples the scrolling
        # heatmap from the spectrum rate (e.g., 30 fps spectrum +
        # 3 rows/sec waterfall for long time-history).
        grp_rate = QGroupBox("Update rates and zoom")
        gr = QGridLayout(grp_rate)
        gr.setColumnStretch(2, 1)

        # Panadapter zoom — preset combo (mouse wheel on the spectrum
        # also cycles through these).
        gr.addWidget(QLabel("Panadapter zoom"), 0, 0)
        self.zoom_combo = QComboBox()
        for level in radio.ZOOM_LEVELS:
            self.zoom_combo.addItem(f"{level:g}x", float(level))
        # Select current
        for i in range(self.zoom_combo.count()):
            if abs(self.zoom_combo.itemData(i) - radio.zoom) < 1e-6:
                self.zoom_combo.setCurrentIndex(i)
                break
        self.zoom_combo.setFixedWidth(100)
        self.zoom_combo.setToolTip(
            "Crop the FFT to a centered subset of bins, so the "
            "panadapter magnifies around your RX frequency. "
            "Also: scroll the mouse wheel on empty spectrum to step "
            "through these levels.")
        self.zoom_combo.currentIndexChanged.connect(
            lambda i: self.radio.set_zoom(self.zoom_combo.itemData(i)))
        gr.addWidget(self.zoom_combo, 0, 1, Qt.AlignLeft)
        radio.zoom_changed.connect(self._on_zoom_changed)

        # Spectrum FPS — how often the spectrum repaints (5..60 Hz).
        gr.addWidget(QLabel("Spectrum rate"), 1, 0)
        self.fps_slider = QSlider(Qt.Horizontal)
        self.fps_slider.setRange(5, 120)   # bumped from 60 for faster WF
        self.fps_slider.setValue(radio.spectrum_fps)
        self.fps_slider.setFixedWidth(240)
        self.fps_slider.setToolTip(
            "How fast the spectrum repaints. Lower = less CPU / GPU "
            "load, laggier trace. Higher = smoother but more work. "
            "30 fps is a good balance.")
        gr.addWidget(self.fps_slider, 1, 1)
        self.fps_label = QLabel(f"{radio.spectrum_fps} fps")
        self.fps_label.setFixedWidth(80)
        self.fps_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gr.addWidget(self.fps_label, 1, 2, Qt.AlignLeft)
        # Debounced commit — see same pattern in panels.py.ViewPanel
        # for why this matters. Drag value just updates the label
        # locally; the radio doesn't see the new fps until the slider
        # has been quiet for 75 ms.
        from PySide6.QtCore import QTimer as _QTimer
        self._fps_debounce = _QTimer(self)
        self._fps_debounce.setSingleShot(True)
        self._fps_debounce.setInterval(75)
        self._fps_debounce.timeout.connect(
            lambda: self.radio.set_spectrum_fps(self.fps_slider.value()))
        self.fps_slider.valueChanged.connect(self._on_fps_slider_drag)
        # Two-way sync: if the front-panel FPS slider moves (or QSettings
        # load, or any future TCI hook), reflect it here. Without this
        # the Settings tab can drift out of sync with the live Radio
        # state — user reported the front-panel slider going faster
        # than the Settings one.
        radio.spectrum_fps_changed.connect(self._sync_fps_slider)

        # Waterfall rate — unified slider covering BOTH the multiplier
        # (fast mode, row duplication) and the divider (slow mode).
        # Must match the encoding used by ViewPanel on the front
        # panel or the two sliders will disagree — both are wired to
        # the same Radio state, and the round-trip has to be clean.
        #
        #   0..8  → multiplier 10..2  (fast mode, up to 10× visual speed)
        #   9     → normal (divider 1, multiplier 1)
        #   10..29 → divider 2..21 (slow crawl)
        gr.addWidget(QLabel("Waterfall rate"), 2, 0)
        self.wf_slider = QSlider(Qt.Horizontal)
        self.wf_slider.setRange(0, 29)
        self.wf_slider.setInvertedAppearance(True)  # right = faster
        self.wf_slider.setValue(self._wf_state_to_slider(
            radio.waterfall_divider, radio.waterfall_multiplier))
        self.wf_slider.setFixedWidth(240)
        self.wf_slider.setToolTip(
            "How fast the waterfall scrolls. Right end = up to 10× "
            "visual speed (row duplication), middle = one row per "
            "FFT, left end = slow crawl (1 row per 21 FFTs, long "
            "time-history on screen at once).")
        gr.addWidget(self.wf_slider, 2, 1)
        self.wf_label = QLabel(self._wf_label_text())
        self.wf_label.setFixedWidth(110)
        self.wf_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gr.addWidget(self.wf_label, 2, 2, Qt.AlignLeft)
        # Same debounce as the FPS slider above — committing the new
        # multiplier per pixel during a fast drag makes the FFT loop
        # emit wildly varying numbers of waterfall_ready signals,
        # blowing up the main-thread queue.
        self._wf_debounce = _QTimer(self)
        self._wf_debounce.setSingleShot(True)
        self._wf_debounce.setInterval(75)
        self._wf_debounce.timeout.connect(self._commit_wf_value)
        self.wf_slider.valueChanged.connect(self._on_wf_slider_drag_setting)
        # Sync back: if the front-panel slider moves, reflect it here.
        radio.waterfall_divider_changed.connect(self._sync_wf_slider)
        radio.waterfall_multiplier_changed.connect(self._sync_wf_slider)

        v.addWidget(grp_rate)

        v.addStretch(1)

    # ── Helpers ──────────────────────────────────────────────────
    @staticmethod
    def _settings():
        from PySide6.QtCore import QSettings
        return QSettings("N8SDR", "Lyra")

    def _on_backend_picked(self, key: str):
        """Persist the chosen backend. Restart required — the UI
        label already warns about this."""
        self._settings().setValue("visuals/graphics_backend", key)

    def _db_slider(self, grid, row: int, label: str, initial: float):
        """Factory for a labeled dB slider row. Returns (slider, val_label)."""
        grid.addWidget(QLabel(label), row, 0)
        s = QSlider(Qt.Horizontal)
        s.setRange(-150, 0)
        s.setValue(int(round(initial)))
        s.setFixedWidth(280)
        grid.addWidget(s, row, 1)
        lbl = QLabel(f"{int(round(initial)):+d} dBFS")
        lbl.setFixedWidth(80)
        lbl.setStyleSheet("color: #cdd9e5; font-family: Consolas, monospace;")
        grid.addWidget(lbl, row, 2, Qt.AlignLeft)
        return s, lbl

    def _on_db_changed(self):
        """Push slider values to Radio (which will emit the change
        signals the painted widgets are subscribed to)."""
        sp_lo, sp_hi = self._spec_min.value(), self._spec_max.value()
        wf_lo, wf_hi = self._wf_min.value(),   self._wf_max.value()
        self._spec_min_lbl.setText(f"{sp_lo:+d} dBFS")
        self._spec_max_lbl.setText(f"{sp_hi:+d} dBFS")
        self._wf_min_lbl.setText(f"{wf_lo:+d} dBFS")
        self._wf_max_lbl.setText(f"{wf_hi:+d} dBFS")
        self.radio.set_spectrum_db_range(sp_lo, sp_hi)
        self.radio.set_waterfall_db_range(wf_lo, wf_hi)

    def _on_cal_changed(self, val: int):
        """Cal slider drag — push to Radio + repaint label."""
        self._cal_lbl.setText(f"{val:+.1f} dB")
        self.radio.set_spectrum_cal_db(float(val))

    def _on_radio_cal_changed(self, db: float):
        """Radio.spectrum_cal_db_changed — keep slider + label in sync
        without re-firing our own valueChanged into Radio."""
        target = int(round(db))
        if self._cal_slider.value() != target:
            self._cal_slider.blockSignals(True)
            self._cal_slider.setValue(target)
            self._cal_slider.blockSignals(False)
        self._cal_lbl.setText(f"{db:+.1f} dB")

    def _on_smeter_cal_changed(self, val: int):
        """S-meter cal slider drag — push to Radio + repaint label."""
        self._smeter_cal_lbl.setText(f"{val:+.1f} dB")
        self.radio.set_smeter_cal_db(float(val))

    def _on_radio_smeter_cal_changed(self, db: float):
        """Radio.smeter_cal_db_changed — keep slider + label in sync."""
        target = int(round(db))
        if self._smeter_cal_slider.value() != target:
            self._smeter_cal_slider.blockSignals(True)
            self._smeter_cal_slider.setValue(target)
            self._smeter_cal_slider.blockSignals(False)
        self._smeter_cal_lbl.setText(f"{db:+.1f} dB")

    def _sync_spec_sliders(self, lo: float, hi: float):
        """Spectrum dB range changed at the Radio side (auto-scale,
        Y-axis drag on the panadapter, etc.) — keep our sliders +
        labels in sync. Block signals during setValue so we don't
        bounce back into Radio.set_spectrum_db_range."""
        for slider, val in ((self._spec_min, int(lo)),
                            (self._spec_max, int(hi))):
            if slider.value() != val:
                slider.blockSignals(True)
                slider.setValue(val)
                slider.blockSignals(False)
        self._spec_min_lbl.setText(f"{int(lo):+d} dBFS")
        self._spec_max_lbl.setText(f"{int(hi):+d} dBFS")

    def _sync_wf_sliders(self, lo: float, hi: float):
        """Same as _sync_spec_sliders but for the waterfall pair."""
        for slider, val in ((self._wf_min, int(lo)),
                            (self._wf_max, int(hi))):
            if slider.value() != val:
                slider.blockSignals(True)
                slider.setValue(val)
                slider.blockSignals(False)
        self._wf_min_lbl.setText(f"{int(lo):+d} dBFS")
        self._wf_max_lbl.setText(f"{int(hi):+d} dBFS")

    def _make_color_swatch(self, key: str, label_text: str,
                           current_hex: str, default_hex: str, on_pick):
        """Factory: colored+bold clickable label. Its text IS the
        field name ("Spectrum trace", "Peak markers", etc.), painted
        in the field's current color so the operator sees at a glance
        what everything's set to. Left-click aims this field for the
        next preset/custom pick; right-click resets it to factory
        default. The active-aimed field is highlighted with a subtle
        background + underline.

        Name kept as `_make_color_swatch` for minimal diff from the
        old swatch-button factory; the return type is now a
        _ColorPickLabel instead of a QPushButton.
        """
        lbl = _ColorPickLabel(key, label_text)
        lbl.setProperty("default_hex", default_hex)
        lbl.setProperty("current_hex", current_hex or "")
        lbl.setProperty("swatch_key", key)
        self._paint_swatch(lbl, current_hex or default_hex)

        lbl.clicked.connect(lambda k=key: self._set_active_swatch(k))
        lbl.reset_requested.connect(
            lambda k=key: self._reset_swatch(k))
        return lbl

    @staticmethod
    def _paint_swatch(lbl, hex_color: str, active: bool = False):
        """Paint a color-label's text in `hex_color`, bold. When
        `active=True` the label is the currently-aimed target — we
        underline it and add a subtle dark background so the operator
        can see which field the next preset/custom pick will affect.
        """
        # Readable text over either dark or light backgrounds:
        # QLabel inherits the dialog's dark theme, so a light-bg
        # pale hint lives on the underline rather than a hard box.
        if active:
            style = (
                f"QLabel {{ color: {hex_color}; font-weight: 800; "
                f"background: #12202c; border: 1px solid #00e5ff; "
                f"border-radius: 3px; padding: 2px 6px; "
                f"text-decoration: underline; }}")
        else:
            style = (
                f"QLabel {{ color: {hex_color}; font-weight: 800; "
                f"background: transparent; border: 1px solid transparent; "
                f"border-radius: 3px; padding: 2px 6px; }}"
                f"QLabel:hover {{ border: 1px solid #7ff7ff; }}")
        lbl.setStyleSheet(style)

    def _set_active_swatch(self, key: str):
        """Highlight the selected field so the operator can see
        which one the next preset/custom click will affect."""
        if key not in self._color_swatches:
            return
        # De-highlight the previous active one
        if (self._active_swatch_key
                and self._active_swatch_key in self._color_swatches):
            prev = self._color_swatches[self._active_swatch_key]
            prev_hex = (prev.property("current_hex") or
                        prev.property("default_hex"))
            self._paint_swatch(prev, prev_hex, active=False)
        self._active_swatch_key = key
        lbl = self._color_swatches[key]
        cur = lbl.property("current_hex") or lbl.property("default_hex")
        self._paint_swatch(lbl, cur, active=True)

    def _apply_preset_color(self, hex_str: str):
        """User clicked a color chip in the inline palette — apply
        it to whichever field is currently aimed."""
        key = self._active_swatch_key
        if not key or key not in self._color_swatches:
            return
        lbl = self._color_swatches[key]
        lbl.setProperty("current_hex", hex_str)
        self._paint_swatch(lbl, hex_str, active=True)
        cb = self._color_callbacks.get(key)
        if cb:
            cb(hex_str)

    def _open_custom_picker(self):
        """Fallback custom picker for colors not in the 18-preset
        grid. Uses the static QColorDialog.getColor() helper — it
        builds, shows, and exec()s the dialog in one call, with the
        non-native (Qt-rendered) variant so it always stacks above
        the Settings dialog on Windows.

        Previous implementation tried to build the dialog manually
        then call show()+raise_()+exec(), which hit a NameError on
        QColor (never imported). The error killed the slot silently
        — nothing flashed, no taskbar entry. Switching to the static
        helper + a proper top-of-file QColor import fixes both.
        """
        key = self._active_swatch_key
        if not key or key not in self._color_swatches:
            return
        lbl = self._color_swatches[key]
        cur = (lbl.property("current_hex")
               or lbl.property("default_hex")
               or "#5ec8ff")
        parent = self.window() or self
        title = f"Pick custom color — {self._color_displays.get(key, key)}"
        color = QColorDialog.getColor(
            QColor(cur), parent, title,
            QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():
            hx = color.name()
            lbl.setProperty("current_hex", hx)
            self._paint_swatch(lbl, hx, active=True)
            cb = self._color_callbacks.get(key)
            if cb:
                cb(hx)

    def _reset_swatch(self, key: str):
        """Reset one field to its factory-default color (clears override)."""
        if key not in self._color_swatches:
            return
        lbl = self._color_swatches[key]
        dflt = lbl.property("default_hex") or "#888888"
        lbl.setProperty("current_hex", "")
        is_active = (key == self._active_swatch_key)
        self._paint_swatch(lbl, dflt, active=is_active)
        cb = self._color_callbacks.get(key)
        if cb:
            cb("")

    def _reset_aimed_color(self):
        if self._active_swatch_key:
            self._reset_swatch(self._active_swatch_key)

    def _reset_all_colors(self):
        """Clear every user color override back to factory defaults."""
        self.radio.set_spectrum_trace_color("")
        self.radio.set_noise_floor_color("")
        self.radio.set_peak_markers_color("")
        self.radio.reset_segment_colors()
        # Repaint every label to its factory-default hex, preserving
        # the active-highlight on the currently-aimed one.
        for key, lbl in self._color_swatches.items():
            lbl.setProperty("current_hex", "")
            dflt = lbl.property("default_hex") or "#888888"
            is_active = (key == self._active_swatch_key)
            self._paint_swatch(lbl, dflt, active=is_active)

    def _on_peak_decay_changed(self, dbps: int):
        self.peak_decay_lbl.setText(f"{dbps} dB/s")
        self.radio.set_peak_markers_decay_dbps(float(dbps))

    def _reset_db_ranges(self):
        # Match Radio's pre-settings defaults.
        self._spec_min.setValue(-110)
        self._spec_max.setValue(-20)
        self._wf_min.setValue(-110)
        self._wf_max.setValue(-30)
        self._on_db_changed()

    # ── Update rates + zoom handlers ─────────────────────────────
    def _on_zoom_changed(self, zoom: float):
        """Radio zoom changed (e.g., from wheel) — keep the combo in
        sync. Block signals so we don't bounce back to Radio."""
        for i in range(self.zoom_combo.count()):
            if abs(self.zoom_combo.itemData(i) - zoom) < 1e-6:
                if self.zoom_combo.currentIndex() != i:
                    self.zoom_combo.blockSignals(True)
                    self.zoom_combo.setCurrentIndex(i)
                    self.zoom_combo.blockSignals(False)
                return

    def _on_fps_slider_drag(self, fps: int):
        """Slider value changed (every pixel of drag) — refresh
        labels locally + bump the debounce timer. Radio is updated
        only when motion has been quiet for 75 ms."""
        self.fps_label.setText(f"{fps} fps")
        self.wf_label.setText(self._wf_label_text())
        self._fps_debounce.start()

    # Backward-compat — anything that called _on_fps_changed by name
    # still works (commits immediately, no debounce).
    def _on_fps_changed(self, fps: int):
        self.fps_label.setText(f"{fps} fps")
        self.radio.set_spectrum_fps(fps)
        self.wf_label.setText(self._wf_label_text())

    def _sync_fps_slider(self, fps: int):
        """Radio FPS changed elsewhere (front-panel slider, QSettings
        load, etc.) — mirror here without firing our own valueChanged."""
        if self.fps_slider.value() != fps:
            self.fps_slider.blockSignals(True)
            self.fps_slider.setValue(fps)
            self.fps_slider.blockSignals(False)
        self.fps_label.setText(f"{fps} fps")
        self.wf_label.setText(self._wf_label_text())

    # Slider encoding helpers — must match ViewPanel._wf_slider_to_state.
    @staticmethod
    def _wf_slider_to_state(v: int) -> tuple[int, int]:
        """slider value → (divider, multiplier). See ViewPanel comment."""
        if v <= 8:
            return (1, 10 - v)       # 0→10×, 1→9×, …, 8→2×
        if v == 9:
            return (1, 1)
        return (v - 8, 1)            # 10→div=2, 29→div=21

    @staticmethod
    def _wf_state_to_slider(divider: int, multiplier: int) -> int:
        if multiplier >= 2:
            return max(0, min(8, 10 - multiplier))
        if divider <= 1:
            return 9
        return min(29, divider + 8)

    def _wf_label_text(self) -> str:
        """rows/sec = fps × multiplier / divider. Accounts for the
        fast-mode multiplier so the readout agrees with what you
        actually see scrolling."""
        fps = self.radio.spectrum_fps
        div = max(1, self.radio.waterfall_divider)
        mult = max(1, self.radio.waterfall_multiplier)
        return f"{fps * mult / div:.1f} rows/s"

    def _on_wf_slider_drag_setting(self, _v: int):
        """Slider dragged — refresh label locally + bump debounce."""
        self.wf_label.setText(self._wf_label_text())
        self._wf_debounce.start()

    def _commit_wf_value(self):
        """Debounced commit of the waterfall slider value to Radio."""
        div, mult = self._wf_slider_to_state(self.wf_slider.value())
        self.radio.set_waterfall_divider(div)
        self.radio.set_waterfall_multiplier(mult)

    # Backward-compat — preserved for any code still calling by name.
    def _on_wf_slider_changed(self, v: int):
        div, mult = self._wf_slider_to_state(v)
        self.radio.set_waterfall_divider(div)
        self.radio.set_waterfall_multiplier(mult)
        self.wf_label.setText(self._wf_label_text())

    def _sync_wf_slider(self, *_):
        """Radio state changed elsewhere (front-panel slider moved,
        QSettings load, etc.) — mirror here without firing our own
        valueChanged."""
        target = self._wf_state_to_slider(
            self.radio.waterfall_divider, self.radio.waterfall_multiplier)
        if self.wf_slider.value() != target:
            self.wf_slider.blockSignals(True)
            self.wf_slider.setValue(target)
            self.wf_slider.blockSignals(False)
        self.wf_label.setText(self._wf_label_text())


class SettingsDialog(QDialog):
    """App-wide tabbed settings — accessed from the main toolbar (⚙).

    Tab order (matches reference SDR clients layout):
      Radio → Network/TCI → Hardware → Audio → DSP → Visuals → Keyer → Bands
    """

    def __init__(self, radio, tci_server: TciServer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lyra — Settings")
        self.resize(640, 560)

        v = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tab_radio = RadioSettingsTab(radio)
        self.tabs.addTab(self.tab_radio, "Radio")

        self.tab_tci = TciSettingsTab(tci_server, radio=radio)
        self.tabs.addTab(self.tab_tci, "Network / TCI")

        self.tab_hw = HardwareSettingsTab(radio)
        self.tabs.addTab(self.tab_hw, "Hardware")

        self.tab_dsp = DspSettingsTab(radio)
        self.tabs.addTab(self.tab_dsp, "DSP")

        self.tab_audio = AudioSettingsTab(radio)
        self.tabs.addTab(self.tab_audio, "Audio")

        self.tab_visuals = VisualsSettingsTab(radio)
        self.tabs.addTab(self.tab_visuals, "Visuals")

        for name in ("Keyer", "Bands"):
            placeholder = QLabel(f"{name} settings — coming soon.")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setStyleSheet("color: #5a7080; padding: 40px;")
            self.tabs.addTab(placeholder, name)

        v.addWidget(self.tabs)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        v.addWidget(btns)

    def show_tab(self, name: str):
        """Jump directly to a named tab. Matches by substring (case-
        insensitive) so callers can pass 'Network', 'TCI', 'DSP',
        'Hardware', etc. without having to know the exact tab label."""
        needle = name.lower()
        for i in range(self.tabs.count()):
            if needle in self.tabs.tabText(i).lower():
                self.tabs.setCurrentIndex(i)
                return
