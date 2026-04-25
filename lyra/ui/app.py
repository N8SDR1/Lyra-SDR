"""Lyra — main window.

Dockable-panel composer: every control group is wrapped in a
`QDockWidget` so the operator can drag, detach, float, resize, hide,
and rearrange panels freely. The spectrum + waterfall live in the
central widget; every other panel is a dock.

Panel visibility can be toggled via the View menu; layout persists
across app restarts via `saveState()`/`restoreState()`.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QSettings, QByteArray
from PySide6.QtGui import QFont, QPalette, QAction
from PySide6.QtWidgets import (
    QApplication, QDockWidget, QHBoxLayout, QMainWindow, QMenuBar,
    QSplitter, QToolBar, QVBoxLayout, QWidget,
)

from lyra.radio import Radio
from lyra.ui import theme
from lyra.ui.panels import (
    TuningPanel, ModeFilterPanel, DspPanel,
    SMeterPanel, SpectrumPanel, WaterfallPanel, TciPanel, BandPanel,
    ViewPanel,
)
from lyra.ui.settings_dialog import SettingsDialog
from lyra.ui.help_dialog import HelpDialog


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lyra — Hermes Lite 2+ SDR Transceiver")
        # App icon — shows in the window title bar, taskbar button,
        # Alt+Tab preview, and the rounded "pin to start" tile on
        # Windows 11. Loaded from the multi-res .ico so Windows picks
        # the right size for each context automatically.
        from pathlib import Path
        from PySide6.QtGui import QIcon
        ico = Path(__file__).resolve().parents[2] / "assets" / "logo" / "lyra.ico"
        if ico.is_file():
            self._app_icon = QIcon(str(ico))
            self.setWindowIcon(self._app_icon)
            # Also apply it to the application-global icon so any
            # dialog (Settings, Help, About) inherits it.
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.setWindowIcon(self._app_icon)
        self.resize(1400, 880)
        # Enable full dock flexibility. GroupedDragging lets tabbed
        # docks be dragged together; the corner assignments give the
        # top/bottom areas the full width so drop-zones are bigger and
        # easier to hit when rearranging panels.
        self.setDockOptions(
            QMainWindow.AnimatedDocks
            | QMainWindow.AllowNestedDocks
            | QMainWindow.AllowTabbedDocks
            | QMainWindow.GroupedDragging
        )
        self.setDockNestingEnabled(True)
        self.setCorner(Qt.TopLeftCorner,     Qt.TopDockWidgetArea)
        self.setCorner(Qt.TopRightCorner,    Qt.TopDockWidgetArea)
        self.setCorner(Qt.BottomLeftCorner,  Qt.BottomDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.BottomDockWidgetArea)

        # New canonical QSettings location: Org="N8SDR", App="Lyra".
        # (Project was previously "HL2SDR / Panadapter" during development —
        # the migration block below copies that state across once so the
        # first launch after rename doesn't lose the user's saved layout,
        # IP, band memory, AGC config, etc.)
        self._settings = QSettings("N8SDR", "Lyra")
        self._migrate_legacy_settings()
        self.radio = Radio()

        # ── Compose panels ───────────────────────────────────────────
        # Connection controls (IP, Discover) moved into Settings → Radio.
        # Start/Stop lives on the toolbar below for one-click access.
        self.pnl_tuning     = TuningPanel(self.radio)
        self.pnl_mode       = ModeFilterPanel(self.radio)
        self.pnl_view       = ViewPanel(self.radio)
        self.pnl_band       = BandPanel(self.radio)
        # GainPanel is DELETED: it had an old 0-300 range slider with a
        # linear v/100 mapping, while DspPanel uses a 0-100 slider with
        # a perceptual power curve. Both panels connected to
        # radio.volume_changed and to radio.set_volume via different
        # formulas, so every value-change cascade made the two panels
        # ricochet each other through QSettings until the volume
        # slammed to bizarre values (0.24 → 2.00 → 0.02 → 0.10) in
        # the first few seconds of runtime. Hiding GainPanel wasn't
        # enough — its widgets + signal connections stay live when a
        # panel is .hide()'d, only invisible. We must not construct
        # it at all.
        self.pnl_dsp        = DspPanel(self.radio)
        self.pnl_smeter     = SMeterPanel(self.radio)
        self.pnl_spectrum   = SpectrumPanel(self.radio)
        self.pnl_waterfall  = WaterfallPanel(self.radio)
        self.pnl_tci        = TciPanel(self.radio)

        self.docks: dict[str, QDockWidget] = {}
        self._build_layout()
        self._build_menus()
        self._build_toolbar()

        # Status bar driven by Radio signals
        self.radio.status_message.connect(
            lambda text, timeout: self.statusBar().showMessage(text, timeout))

        self._load_settings()

    # ── Layout ───────────────────────────────────────────────────────
    def _build_layout(self):
        # Central widget: spectrum + waterfall in a vertical splitter so
        # the operator can drag the divider to change the ratio between
        # them. Default 1 : 2 (spectrum gets a third, waterfall two
        # thirds). Splitter state is persisted separately from the dock
        # layout because QMainWindow.saveState() doesn't cover splitters
        # inside the central widget.
        self.center_splitter = QSplitter(Qt.Vertical)
        self.center_splitter.setObjectName("center_splitter")
        self.center_splitter.setHandleWidth(6)
        self.center_splitter.setChildrenCollapsible(False)
        self.center_splitter.addWidget(self.pnl_spectrum)
        self.center_splitter.addWidget(self.pnl_waterfall)
        self.center_splitter.setStretchFactor(0, 1)
        self.center_splitter.setStretchFactor(1, 2)
        # Initial size ratio (overridden by restored state if present)
        self.center_splitter.setSizes([300, 600])
        self.setCentralWidget(self.center_splitter)

        # Create all control docks (no more Connection dock — it's in
        # the toolbar + Settings dialog now).
        self.docks["tuning"] = self._make_dock(
            "tuning", "Tuning", self.pnl_tuning)
        self.docks["mode"] = self._make_dock(
            "mode_filter", "Mode + Filter", self.pnl_mode)
        self.docks["view"] = self._make_dock(
            "view", "View", self.pnl_view)
        self.docks["band"] = self._make_dock(
            "band", "Band", self.pnl_band)
        self.docks["meters"] = self._make_dock(
            "meters", "Meters", self.pnl_smeter)
        self.docks["dsp"] = self._make_dock(
            "dsp_audio", "DSP + Audio", self.pnl_dsp)
        # GainPanel removed entirely — see comment at construction site.
        # Its LNA + Vol sliders duplicated DspPanel's and fought for
        # radio.volume via incompatible mappings.
        # TciPanel is intentionally NOT docked — it's a space waste when
        # TCI is working fine (which is most of the time). The panel is
        # still instantiated so the TciServer it owns stays alive; state
        # is shown via a clickable indicator dot on the main toolbar and
        # configured via File → Network / TCI… (Settings dialog).
        self.pnl_tci.setParent(self)
        self.pnl_tci.hide()

        # Default arrangement:
        #   Top:    Tuning | Mode+Filter
        #           Band   (full-width row below tuning column)
        #           Meters (full-width row below band)
        #   Second row below that: Mode+Filter | View   (side-by-side,
        #     so the operator can snap live zoom / spectrum rate /
        #     waterfall rate right next to the rate/mode/BW combos)
        #   Center: Spectrum + Waterfall
        #   Bottom: DSP+Audio (full width, includes LNA + Vol)
        self.addDockWidget(Qt.TopDockWidgetArea, self.docks["tuning"])
        self.splitDockWidget(self.docks["tuning"],
                             self.docks["mode"], Qt.Horizontal)
        # View panel butts against Mode+Filter on the same row (per
        # user layout preference). Qt will balance the widths on first
        # launch; user-driven resize is preserved across restarts via
        # saveState()/restoreState().
        self.splitDockWidget(self.docks["mode"],
                             self.docks["view"], Qt.Horizontal)
        self.splitDockWidget(self.docks["tuning"],
                             self.docks["band"], Qt.Vertical)
        self.splitDockWidget(self.docks["band"],
                             self.docks["meters"], Qt.Vertical)

        self.addDockWidget(Qt.BottomDockWidgetArea, self.docks["dsp"])

    def _make_dock(self, object_name: str, title: str, panel: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)   # required for saveState
        dock.setWidget(panel)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        # The dock's title bar now provides the panel title — hide the
        # GlassPanel's own internal header to avoid redundancy.
        if hasattr(panel, "set_title"):
            panel.set_title("")
        return dock

    # ── Menus (View toggles + reset layout + Help) ──────────────────
    def _build_menus(self):
        mb = self.menuBar()

        # File — Settings shortcut + quit. Quit is implicit in the
        # window close box, but giving it a menu entry + hotkey is
        # conventional on Windows.
        file_menu = mb.addMenu("&File")
        settings_act = QAction("&Settings…", self)
        settings_act.setShortcut("Ctrl+,")
        settings_act.triggered.connect(self._open_settings)
        file_menu.addAction(settings_act)
        # Direct jumps into specific Settings tabs — handy now that
        # some panels (e.g. TCI) are being consolidated into Settings
        # instead of cluttering the dock area.
        net_act = QAction("&Network / TCI…", self)
        net_act.setToolTip("Open Settings → Network/TCI tab")
        net_act.triggered.connect(lambda: self._open_settings(tab="Network"))
        file_menu.addAction(net_act)
        hw_act = QAction("&Hardware…", self)
        hw_act.setToolTip("Open Settings → Hardware tab (N2ADR, USB-BCD)")
        hw_act.triggered.connect(lambda: self._open_settings(tab="Hardware"))
        file_menu.addAction(hw_act)
        dsp_act = QAction("&DSP…", self)
        dsp_act.setToolTip("Open Settings → DSP tab (AGC, NR, NB, EQ)")
        dsp_act.triggered.connect(lambda: self._open_settings(tab="DSP"))
        file_menu.addAction(dsp_act)
        file_menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        view_menu = mb.addMenu("&View")
        for name, dock in self.docks.items():
            # Each dock provides a pre-wired QAction to toggle visibility
            view_menu.addAction(dock.toggleViewAction())
        view_menu.addSeparator()
        reset = QAction("Reset Panel Layout", self)
        reset.triggered.connect(self._reset_layout)
        view_menu.addAction(reset)

        # Help menu — user guide + keyboard-shortcuts shortcut topic
        # + about. F1 opens the guide from anywhere.
        help_menu = mb.addMenu("&Help")
        guide_act = QAction("&User Guide", self)
        guide_act.setShortcut("F1")
        guide_act.triggered.connect(self.show_help)
        help_menu.addAction(guide_act)
        shortcuts_act = QAction("&Keyboard Shortcuts", self)
        shortcuts_act.triggered.connect(
            lambda: self.show_help("shortcuts"))
        help_menu.addAction(shortcuts_act)
        troubleshoot_act = QAction("&Troubleshooting", self)
        troubleshoot_act.triggered.connect(
            lambda: self.show_help("troubleshooting"))
        help_menu.addAction(troubleshoot_act)

    def _build_toolbar(self):
        """Main toolbar: Start/Stop + Settings + Layout commands.

        These are the always-visible "rig power" controls that need to
        be one click away. IP selection, TCI setup, hardware options,
        etc. all live in Settings now.
        """
        tb = QToolBar("Main", self)
        tb.setObjectName("main_toolbar")
        tb.setMovable(True)
        tb.setIconSize(tb.iconSize())  # use platform default

        # Logo now lives on the Tuning panel between RX1 and RX2 —
        # much more visible there + room for the future RX2 + TX split
        # readouts. Window icon still applies (title bar / taskbar).

        # Start / Stop — checkable so the button visibly reflects state
        self.start_action = QAction("▶  Start", self)
        self.start_action.setCheckable(True)
        self.start_action.setToolTip("Start/stop the HL2 stream")
        self.start_action.toggled.connect(self._on_start_toggled)
        tb.addAction(self.start_action)

        # Connection status dot (color-coded label)
        from PySide6.QtWidgets import QLabel
        self.status_dot = QLabel("  ●  not streaming  ")
        self.status_dot.setStyleSheet("color: #8a9aac; font-weight: 600;")
        tb.addWidget(self.status_dot)

        # TCI status indicator — replaces the former TCI dock panel.
        # Shows at a glance whether the TCI server is running and how
        # many clients are connected. Click to open Settings → Network/TCI.
        # (Plain QLabel with a mouse-click hook, so it can live inline
        # with the toolbar widgets instead of adding a full QAction.)
        self.tci_indicator = QLabel("  ◌  TCI off  ")
        self.tci_indicator.setStyleSheet(
            "color: #6a7a8c; font-weight: 600; padding: 0 4px;")
        self.tci_indicator.setToolTip(
            "TCI server status. Click to open Network/TCI settings.")
        self.tci_indicator.setCursor(Qt.PointingHandCursor)
        # Lambda wrapper around mousePressEvent to route the click
        self.tci_indicator.mousePressEvent = (
            lambda ev: self._open_settings(tab="Network"))
        tb.addWidget(self.tci_indicator)

        # Wire to the TCI server's state signals
        server = self.pnl_tci.server
        server.running_changed.connect(self._update_tci_indicator)
        server.client_count_changed.connect(self._update_tci_indicator)
        self._update_tci_indicator()

        # ── ADC peak + RMS indicator ───────────────────────────────
        # Dual readout: peak shows clipping margin (headroom), RMS
        # shows signal-energy level (predictable under LNA gain
        # changes, useful for linearity diagnostics). On a noise
        # floor, typical peak/RMS crest factor is ~10-12 dB.
        # Fed by radio.lna_peak_dbfs + lna_rms_dbfs at ~4 Hz.
        self.adc_peak_indicator = QLabel("  ADC pk --  rms --  dBFS  ")
        self.adc_peak_indicator.setStyleSheet(
            "color: #6a7a8c; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 4px;")
        self.adc_peak_indicator.setToolTip(
            "Live ADC levels in dBFS (peak / RMS).\n\n"
            "PEAK — instantaneous maximum magnitude, used for\n"
            "clipping / headroom diagnostics:\n"
            "  > -3 dBFS  CLIPPING — drop LNA immediately\n"
            "  -3 to -10  Hot / IMD risk\n"
            "  -10 to -30 Sweet spot\n"
            "  -30 to -50 Acceptable / weak-signal friendly\n"
            "  < -50     Low — raise LNA or check antenna\n\n"
            "RMS — steady-state signal energy. Tracks LNA\n"
            "changes linearly (1 dB LNA = 1 dB RMS), so it's\n"
            "the reliable measurement for chain-linearity tests.\n"
            "Typical peak-RMS difference on noise: ~10-12 dB.")
        self.adc_peak_indicator.setMinimumWidth(220)
        tb.addWidget(self.adc_peak_indicator)
        self._adc_peak_db = -160.0
        self._adc_rms_db = -160.0
        self.radio.lna_peak_dbfs.connect(self._update_adc_peak)
        self.radio.lna_rms_dbfs.connect(self._update_adc_rms)

        tb.addSeparator()

        settings_action = QAction("⚙  Settings…", self)
        settings_action.setToolTip("Radio, Network/TCI, Hardware, Audio, DSP…")
        settings_action.triggered.connect(self._open_settings)
        tb.addAction(settings_action)

        tb.addSeparator()

        reset = QAction("Reset Panel Layout", self)
        reset.setToolTip("Restore the default panel arrangement")
        reset.triggered.connect(self._reset_layout)
        tb.addAction(reset)

        tb.addSeparator()
        for dock_name, dock in self.docks.items():
            tb.addAction(dock.toggleViewAction())

        self.addToolBar(Qt.TopToolBarArea, tb)

        # React to Radio state changes so the start button + status label
        # always reflect reality (e.g., TCI toggling the stream).
        self.radio.stream_state_changed.connect(self._on_stream_state_changed)

    def _on_start_toggled(self, on: bool):
        if on and not self.radio.is_streaming:
            self.radio.start()
        elif not on and self.radio.is_streaming:
            self.radio.stop()

    def _update_tci_indicator(self, *_):
        """Refresh the toolbar TCI dot. Green when running (bright if
        clients connected, dim if idle). Gray when server is stopped."""
        server = self.pnl_tci.server
        if not server.is_running:
            self.tci_indicator.setText("  ◌  TCI off  ")
            self.tci_indicator.setStyleSheet(
                "color: #6a7a8c; font-weight: 600; padding: 0 4px;")
            self.tci_indicator.setToolTip(
                "TCI server is OFF. Click to open Network/TCI settings.")
            return
        n = server.client_count
        if n == 0:
            # Running but no clients yet — softer green
            self.tci_indicator.setText("  ●  TCI ready  ")
            self.tci_indicator.setStyleSheet(
                "color: #7acb8a; font-weight: 700; padding: 0 4px;")
            self.tci_indicator.setToolTip(
                f"TCI listening on {server.bind_host}:{server.port} "
                f"(no clients connected yet). Click to open settings.")
        else:
            # Running with at least one client — bright green
            self.tci_indicator.setText(
                f"  ●  TCI {n} client{'s' if n != 1 else ''}  ")
            self.tci_indicator.setStyleSheet(
                "color: #39ff14; font-weight: 700; padding: 0 4px;")
            self.tci_indicator.setToolTip(
                f"TCI listening on {server.bind_host}:{server.port} "
                f"with {n} connected client{'s' if n != 1 else ''}. "
                f"Click to open settings.")

    def _on_stream_state_changed(self, running: bool):
        self.start_action.blockSignals(True)
        self.start_action.setChecked(running)
        self.start_action.setText("⏹  Stop" if running else "▶  Start")
        self.start_action.blockSignals(False)
        if running:
            self.status_dot.setText("  ●  streaming  ")
            self.status_dot.setStyleSheet("color: #39ff14; font-weight: 700;")
        else:
            self.status_dot.setText("  ●  not streaming  ")
            self.status_dot.setStyleSheet("color: #8a9aac; font-weight: 600;")
            # Reset the ADC peak indicator to a dim placeholder — no
            # stream, no meaningful reading.
            self._adc_peak_db = -160.0
            self._adc_rms_db = -160.0
            self.adc_peak_indicator.setText("  ADC pk --  rms --  dBFS  ")
            self.adc_peak_indicator.setStyleSheet(
                "color: #6a7a8c; font-family: Consolas, monospace; "
                "font-weight: 700; padding: 0 4px;")

    def _update_adc_peak(self, dbfs: float):
        """Store latest peak and repaint the combined indicator."""
        self._adc_peak_db = dbfs
        self._repaint_adc_indicator()

    def _update_adc_rms(self, dbfs: float):
        """Store latest RMS and repaint the combined indicator."""
        self._adc_rms_db = dbfs
        self._repaint_adc_indicator()

    def _repaint_adc_indicator(self):
        """Render the combined peak + RMS readout with color coding
        driven by the PEAK value (that's what you care about for
        clipping / headroom). RMS is shown alongside as a diagnostic
        for chain linearity — a 1 dB LNA change should move RMS by
        ~1 dB; peak can jitter much more due to transients."""
        pk = self._adc_peak_db
        rms = self._adc_rms_db
        if pk > -3.0:
            color = "#ff4040"        # clipping — red
        elif pk > -10.0:
            color = "#ff8c3a"        # hot — orange
        elif pk > -30.0:
            color = "#39ff14"        # sweet spot — green
        elif pk > -50.0:
            color = "#7fd9ff"        # acceptable — light cyan
        else:
            color = "#8a9aac"        # low — muted gray
        self.adc_peak_indicator.setText(
            f"  ADC pk {pk:+5.1f}  rms {rms:+5.1f}  dBFS  ")
        self.adc_peak_indicator.setStyleSheet(
            f"color: {color}; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 4px;")

    def _open_settings(self, tab: str | None = None):
        dlg = SettingsDialog(self.radio, self.pnl_tci.server, parent=self)
        if tab:
            dlg.show_tab(tab)
        dlg.exec()

    # ── Help ────────────────────────────────────────────────────────
    def show_help(self, topic: str | None = None):
        """Open the in-app User Guide. Re-uses one persistent dialog so
        opening/closing doesn't lose scroll position or selection."""
        if getattr(self, "_help_dialog", None) is None:
            self._help_dialog = HelpDialog(self)
            # Wayfinding: when the guide contains a `panel:xxx` link and
            # the user clicks it, flash the matching dock so they can
            # locate it at a glance in the main window.
            self._help_dialog.panel_highlight_requested.connect(
                self.highlight_panel)
        if topic:
            self._help_dialog.show_topic(topic)
        else:
            self._help_dialog.show()
            self._help_dialog.raise_()
            self._help_dialog.activateWindow()

    # Dock-name aliases so help-file `panel:xxx` links can use friendly
    # short names (panel:dsp, panel:meter, etc.) even if we internally
    # renamed a dock key.
    _PANEL_ALIASES = {
        "dsp":         "dsp",
        "dsp-audio":   "dsp",
        "audio":       "dsp",
        "agc":         "dsp",
        "meter":       "meters",
        "meters":      "meters",
        "smeter":      "meters",
        "band":        "band",
        "bands":       "band",
        "tune":        "tuning",
        "tuning":      "tuning",
        "mode":        "mode",
        "mode-filter": "mode",
        "filter":      "mode",
        # LNA + Volume merged into DSP+AUDIO — legacy link redirects.
        "gain":        "dsp",
        "lna":         "dsp",
        "volume":      "dsp",
        "vol":         "dsp",
        "spectrum":    None,     # central widget, not a dock
        "waterfall":   None,
    }

    def highlight_panel(self, name: str):
        """Flash a dock panel to draw the operator's attention — used
        by `panel:xxx` links in the User Guide. If the dock is hidden
        or floating out of sight, bring it back on-screen first."""
        key = self._PANEL_ALIASES.get(name.strip().lower(), name)
        if key is None:
            # Central-widget panels (panadapter / waterfall) — flash the
            # whole center splitter instead of looking up a dock.
            self._flash_widget(self.center_splitter)
            return
        dock = self.docks.get(key)
        if dock is None:
            return
        if not dock.isVisible():
            dock.show()
        dock.raise_()
        self._flash_widget(dock)

    def _flash_widget(self, widget):
        """Apply a transient bright border to `widget` for ~1.5 s so
        the operator can spot it. Uses QSS on the outer widget so it
        doesn't fight with custom paintEvents of child GlassPanels."""
        from PySide6.QtCore import QTimer
        prior = widget.styleSheet()
        widget.setStyleSheet(prior +
            " QDockWidget, QWidget#__flash { border: 2px solid #ffd700; }"
            " QWidget#__flash { background: rgba(255, 215, 0, 20); }")
        # Force a repaint on the dock chrome
        widget.update()
        QTimer.singleShot(1500, lambda: widget.setStyleSheet(prior))

    def _reset_layout(self):
        """Restore the default arrangement (handy if panels end up in
        weird places after a docking experiment)."""
        # Remove every dock from its current position, then rebuild.
        for dock in self.docks.values():
            self.removeDockWidget(dock)
            dock.setFloating(False)
            dock.setVisible(True)
        # Re-apply default layout (matches _build_layout)
        self.addDockWidget(Qt.TopDockWidgetArea, self.docks["tuning"])
        self.splitDockWidget(self.docks["tuning"],
                             self.docks["mode"], Qt.Horizontal)
        self.splitDockWidget(self.docks["mode"],
                             self.docks["view"], Qt.Horizontal)
        self.splitDockWidget(self.docks["tuning"],
                             self.docks["band"], Qt.Vertical)
        self.splitDockWidget(self.docks["band"],
                             self.docks["meters"], Qt.Vertical)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.docks["dsp"])
        for dock in self.docks.values():
            dock.setVisible(True)

    # ── Persistence ──────────────────────────────────────────────────
    def _migrate_legacy_settings(self):
        """One-time copy of pre-rename QSettings from the old HL2SDR /
        Panadapter location into the new N8SDR / Lyra one. Idempotent —
        runs on every startup but only copies keys if the new location
        is empty (i.e., the first launch after the rename). Legacy keys
        are left in place so a rollback doesn't lose them."""
        if self._settings.allKeys():
            return   # new location already populated
        legacy = QSettings("HL2SDR", "Panadapter")
        keys = legacy.allKeys()
        if not keys:
            return   # nothing to migrate (fresh install)
        for k in keys:
            self._settings.setValue(k, legacy.value(k))
        self._settings.sync()

    def _load_settings(self):
        s = self._settings
        r = self.radio
        if s.contains("ip"):            r.set_ip(str(s.value("ip")))
        if s.contains("freq_hz"):
            try: r.set_freq_hz(int(s.value("freq_hz")))
            except (TypeError, ValueError): pass
        if s.contains("rate"):
            try: r.set_rate(int(s.value("rate")))
            except (TypeError, ValueError): pass
        if s.contains("mode"):          r.set_mode(str(s.value("mode")))
        if s.contains("gain"):
            try: r.set_gain_db(int(s.value("gain")))
            except (TypeError, ValueError): pass
        if s.contains("volume"):
            try:
                # Migration: pre-AF-Gain-split QSettings had volume in
                # 0..3.0 range (the old VOL_MAX era). set_volume now
                # clamps to 0..1.0, so legacy values silently snap to
                # full output — the operator can re-dial to taste.
                r.set_volume(float(s.value("volume")))
            except (TypeError, ValueError):
                pass
        if s.contains("af_gain_db"):
            try: r.set_af_gain_db(int(s.value("af_gain_db")))
            except (TypeError, ValueError): pass
        if s.contains("audio_output"):  r.set_audio_output(str(s.value("audio_output")))
        if s.contains("spectrum_auto_scale"):
            r.set_spectrum_auto_scale(
                s.value("spectrum_auto_scale") in (True, "true", "True", 1, "1"))
        if s.contains("pc_audio_device"):
            v = s.value("pc_audio_device")
            try:
                # Allow empty / "auto" / "" to mean None (auto-pick).
                # Otherwise parse as int device index.
                if v in (None, "", "auto", "None"):
                    r.set_pc_audio_device_index(None)
                else:
                    r.set_pc_audio_device_index(int(v))
            except (TypeError, ValueError):
                pass
        if s.contains("bw_locked"):
            r.set_bw_lock(s.value("bw_locked") in (True, "true", "True", 1, "1"))
        if s.contains("filter_board"):
            r.set_filter_board_enabled(
                s.value("filter_board") in (True, "true", "True", 1, "1"))
        if s.contains("usb_bcd/serial"):
            r.set_usb_bcd_serial(str(s.value("usb_bcd/serial")))
        if s.contains("usb_bcd/60m_as_40m"):
            r.set_bcd_60m_as_40m(
                s.value("usb_bcd/60m_as_40m") in (True, "true", "True", 1, "1"))
        # Per-band memory (last freq/mode/gain per band)
        if s.contains("band_memory"):
            import json
            try:
                snap = json.loads(str(s.value("band_memory")))
                r.restore_band_memory(snap)
            except (ValueError, TypeError):
                pass
        # Note: usb_bcd/enabled is NOT auto-restored. The operator must
        # explicitly turn the cable on each session — safety measure.
        if s.contains("meter_style"):
            self.pnl_smeter.set_style(str(s.value("meter_style")))
        # AGC profile + custom values
        if s.contains("agc/profile"):
            r.set_agc_profile(str(s.value("agc/profile")))
        if s.contains("agc/release") and s.contains("agc/hang_blocks"):
            try:
                rel = float(s.value("agc/release"))
                hang = int(s.value("agc/hang_blocks"))
                if r.agc_profile == "custom":
                    r.set_agc_custom(rel, hang)
            except (TypeError, ValueError):
                pass
        if s.contains("agc/threshold"):
            try:
                r.set_agc_threshold(float(s.value("agc/threshold")))
            except (TypeError, ValueError):
                pass
        # TCI Spots persistence
        if s.contains("spots/max"):
            try: r.set_max_spots(int(s.value("spots/max")))
            except (TypeError, ValueError): pass
        if s.contains("spots/lifetime_s"):
            try: r.set_spot_lifetime_s(int(s.value("spots/lifetime_s")))
            except (TypeError, ValueError): pass
        if s.contains("spots/mode_filter"):
            r.set_spot_mode_filter_csv(str(s.value("spots/mode_filter")))
        # Visuals persistence (palette + dB ranges; graphics backend is
        # read by gfx.py at import time from the same QSettings key, so
        # no code here — changing it requires a restart which the UI
        # already explains).
        if s.contains("visuals/waterfall_palette"):
            r.set_waterfall_palette(str(s.value("visuals/waterfall_palette")))
        try:
            if s.contains("visuals/spectrum_min_db") and s.contains("visuals/spectrum_max_db"):
                r.set_spectrum_db_range(
                    float(s.value("visuals/spectrum_min_db")),
                    float(s.value("visuals/spectrum_max_db")))
            if s.contains("visuals/waterfall_min_db") and s.contains("visuals/waterfall_max_db"):
                r.set_waterfall_db_range(
                    float(s.value("visuals/waterfall_min_db")),
                    float(s.value("visuals/waterfall_max_db")))
            if s.contains("visuals/zoom"):
                r.set_zoom(float(s.value("visuals/zoom")))
            if s.contains("visuals/spectrum_fps"):
                r.set_spectrum_fps(int(s.value("visuals/spectrum_fps")))
            if s.contains("visuals/waterfall_divider"):
                r.set_waterfall_divider(int(s.value("visuals/waterfall_divider")))
            if s.contains("visuals/waterfall_multiplier"):
                r.set_waterfall_multiplier(int(s.value("visuals/waterfall_multiplier")))
        except (TypeError, ValueError):
            pass
        # Levels automation
        if s.contains("levels/muted"):
            r.set_muted(s.value("levels/muted") in (True, "true", "True", 1, "1"))
        if s.contains("levels/lna_auto"):
            r.set_lna_auto(s.value("levels/lna_auto") in (True, "true", "True", 1, "1"))
        # Noise Reduction
        if s.contains("nr/profile"):
            r.set_nr_profile(str(s.value("nr/profile")))
        if s.contains("nr/enabled"):
            r.set_nr_enabled(s.value("nr/enabled") in (True, "true", "True", 1, "1"))
        # Noise-floor marker on the spectrum (default on)
        if s.contains("visuals/noise_floor_marker"):
            r.set_noise_floor_enabled(
                s.value("visuals/noise_floor_marker")
                in (True, "true", "True", 1, "1"))
        # Band plan
        if s.contains("band_plan/region"):
            r.set_band_plan_region(str(s.value("band_plan/region")))
        if s.contains("band_plan/show_segments"):
            r.set_band_plan_show_segments(
                s.value("band_plan/show_segments")
                in (True, "true", "True", 1, "1"))
        if s.contains("band_plan/show_landmarks"):
            r.set_band_plan_show_landmarks(
                s.value("band_plan/show_landmarks")
                in (True, "true", "True", 1, "1"))
        if s.contains("band_plan/edge_warn"):
            r.set_band_plan_edge_warn(
                s.value("band_plan/edge_warn")
                in (True, "true", "True", 1, "1"))
        # Peak markers
        if s.contains("visuals/peak_markers"):
            r.set_peak_markers_enabled(
                s.value("visuals/peak_markers")
                in (True, "true", "True", 1, "1"))
        if s.contains("visuals/peak_decay_dbps"):
            try:
                r.set_peak_markers_decay_dbps(
                    float(s.value("visuals/peak_decay_dbps")))
            except (TypeError, ValueError):
                pass
        # User-picked colors
        if s.contains("visuals/trace_color"):
            r.set_spectrum_trace_color(str(s.value("visuals/trace_color")))
        if s.contains("visuals/nf_color"):
            r.set_noise_floor_color(str(s.value("visuals/nf_color")))
        if s.contains("visuals/peak_color"):
            r.set_peak_markers_color(str(s.value("visuals/peak_color")))
        for kind in ("CW", "DIG", "SSB", "FM"):
            key = f"visuals/segment_color/{kind}"
            if s.contains(key):
                r.set_segment_color(kind, str(s.value(key)))
        # Peak marker style + readout toggle
        if s.contains("visuals/peak_style"):
            r.set_peak_markers_style(str(s.value("visuals/peak_style")))
        if s.contains("visuals/peak_show_db"):
            r.set_peak_markers_show_db(
                s.value("visuals/peak_show_db")
                in (True, "true", "True", 1, "1"))
        geom = s.value("geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        # Dock layout (positions, sizes, floating state, visibility)
        state = s.value("dock_state")
        if isinstance(state, QByteArray) and not state.isEmpty():
            self.restoreState(state)
        # Central splitter (panadapter / waterfall ratio)
        split_state = s.value("center_split")
        if isinstance(split_state, QByteArray) and not split_state.isEmpty():
            self.center_splitter.restoreState(split_state)
        # ── TCI settings ─────────────────────────────────────────────
        tci = self.pnl_tci.server
        if s.contains("tci/port"):
            try: tci.port = int(s.value("tci/port"))
            except (TypeError, ValueError): pass
        if s.contains("tci/host"):        tci.bind_host = str(s.value("tci/host"))
        if s.contains("tci/rate_hz"):
            try: tci.rate_limit_hz = int(s.value("tci/rate_hz"))
            except (TypeError, ValueError): pass
        if s.contains("tci/send_initial"):
            tci.send_initial_state_on_connect = s.value("tci/send_initial") in (True, "true", "True", 1, "1")
        if s.contains("tci/callsign"):    tci.own_callsign = str(s.value("tci/callsign"))
        if s.contains("tci/log"):
            tci.log_traffic = s.value("tci/log") in (True, "true", "True", 1, "1")
        if s.contains("tci/running") and s.value("tci/running") in (True, "true", "True", 1, "1"):
            self.pnl_tci.enable_btn.setChecked(True)
        self.pnl_tci._update_status()

    def _save_settings(self):
        s = self._settings
        r = self.radio
        s.setValue("ip", r.ip)
        s.setValue("freq_hz", r.freq_hz)
        s.setValue("rate", r.rate)
        s.setValue("mode", r.mode)
        s.setValue("gain", r.gain_db)
        s.setValue("volume", r.volume)
        s.setValue("af_gain_db", r.af_gain_db)
        s.setValue("audio_output", r.audio_output)
        s.setValue("spectrum_auto_scale", r.spectrum_auto_scale)
        # PC Soundcard device index — None = auto, int = specific
        # PortAudio device. Stored as string "auto" or the int as
        # str so QSettings round-trips cleanly across platforms.
        s.setValue("pc_audio_device",
                   "auto" if r.pc_audio_device_index is None
                   else str(int(r.pc_audio_device_index)))
        s.setValue("bw_locked", r.bw_locked)
        s.setValue("filter_board", r.filter_board_enabled)
        s.setValue("usb_bcd/serial", r.usb_bcd_serial)
        s.setValue("usb_bcd/60m_as_40m", r.bcd_60m_as_40m)
        s.setValue("meter_style", self.pnl_smeter.style)
        s.setValue("agc/profile", r.agc_profile)
        s.setValue("agc/release", r.agc_release)
        s.setValue("agc/hang_blocks", r.agc_hang_blocks)
        s.setValue("agc/threshold", r.agc_threshold)
        # TCI spots
        s.setValue("spots/max", r.max_spots)
        s.setValue("spots/lifetime_s", r.spot_lifetime_s)
        s.setValue("spots/mode_filter", r.spot_mode_filter_csv)
        # Visuals
        s.setValue("visuals/waterfall_palette", r.waterfall_palette)
        sp_lo, sp_hi = r.spectrum_db_range
        wf_lo, wf_hi = r.waterfall_db_range
        s.setValue("visuals/spectrum_min_db",  sp_lo)
        s.setValue("visuals/spectrum_max_db",  sp_hi)
        s.setValue("visuals/waterfall_min_db", wf_lo)
        s.setValue("visuals/waterfall_max_db", wf_hi)
        s.setValue("visuals/zoom",              r.zoom)
        s.setValue("visuals/spectrum_fps",      r.spectrum_fps)
        s.setValue("visuals/waterfall_divider",   r.waterfall_divider)
        s.setValue("visuals/waterfall_multiplier", r.waterfall_multiplier)
        # Levels automation
        s.setValue("levels/muted",    r.muted)
        s.setValue("levels/lna_auto", r.lna_auto)
        # Noise Reduction
        s.setValue("nr/enabled",      r.nr_enabled)
        s.setValue("nr/profile",      r.nr_profile)
        # Noise-floor marker
        s.setValue("visuals/noise_floor_marker", r.noise_floor_enabled)
        # Band plan
        s.setValue("band_plan/region",         r.band_plan_region)
        s.setValue("band_plan/show_segments",  r.band_plan_show_segments)
        s.setValue("band_plan/show_landmarks", r.band_plan_show_landmarks)
        s.setValue("band_plan/edge_warn",      r.band_plan_edge_warn)
        # Peak markers
        s.setValue("visuals/peak_markers",     r.peak_markers_enabled)
        s.setValue("visuals/peak_decay_dbps",  r.peak_markers_decay_dbps)
        # User-picked colors
        s.setValue("visuals/trace_color",      r.spectrum_trace_color)
        s.setValue("visuals/nf_color",         r.noise_floor_color)
        s.setValue("visuals/peak_color",       r.peak_markers_color)
        for kind, hex_str in r.segment_colors.items():
            s.setValue(f"visuals/segment_color/{kind}", hex_str)
        # Peak marker style + readout toggle
        s.setValue("visuals/peak_style",       r.peak_markers_style)
        s.setValue("visuals/peak_show_db",     r.peak_markers_show_db)
        # graphics_backend is already written by VisualsSettingsTab
        # directly when the operator picks a backend — no code here.
        import json
        s.setValue("band_memory", json.dumps(r.band_memory_snapshot))
        s.setValue("geometry", self.saveGeometry())
        s.setValue("dock_state", self.saveState())
        s.setValue("center_split", self.center_splitter.saveState())
        tci = self.pnl_tci.server
        s.setValue("tci/port", tci.port)
        s.setValue("tci/host", tci.bind_host)
        s.setValue("tci/rate_hz", tci.rate_limit_hz)
        s.setValue("tci/send_initial", tci.send_initial_state_on_connect)
        s.setValue("tci/callsign", tci.own_callsign)
        s.setValue("tci/log", tci.log_traffic)
        s.setValue("tci/running", tci.is_running)

    def closeEvent(self, event):
        self._save_settings()
        self.pnl_tci.shutdown()
        self.radio.stop()
        super().closeEvent(event)


def main():
    # If the user has selected OpenGL in Visuals settings, request a
    # default GL surface format *before* QApplication is constructed —
    # Qt requires this ordering. The format is permissive (OpenGL 2.0
    # Compatibility) so it works on effectively every GPU from the
    # last 15 years; if it fails, gfx.py already fell back to software.
    from lyra.ui.gfx import ACTIVE_BACKEND, BACKEND_OPENGL
    if ACTIVE_BACKEND == BACKEND_OPENGL:
        from PySide6.QtGui import QSurfaceFormat
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        fmt.setVersion(2, 0)
        fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
        fmt.setSwapBehavior(QSurfaceFormat.DoubleBuffer)
        fmt.setSwapInterval(1)      # vsync
        QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)

    body = QFont()
    body.setFamilies([theme.FONT_FAMILY, "Segoe UI", "Arial"])
    body.setPointSize(theme.FONT_SIZE)
    app.setFont(body)

    app.setStyleSheet(theme.build_stylesheet())

    pal = QPalette()
    pal.setColor(QPalette.Window, theme.BG_APP)
    pal.setColor(QPalette.Base, theme.BG_CTRL)
    pal.setColor(QPalette.Text, theme.TEXT_PRIMARY)
    pal.setColor(QPalette.WindowText, theme.TEXT_PRIMARY)
    app.setPalette(pal)

    print(f"[theme] applied stylesheet, {len(theme.build_stylesheet())} bytes")
    print(f"[theme] body font family: {body.family()}")

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
