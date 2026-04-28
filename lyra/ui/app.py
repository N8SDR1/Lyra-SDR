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
        # Title bar carries the version so the operator can see at a
        # glance which build they're running — useful when triaging
        # bug reports (the tester might be on an older build than
        # they think).
        from lyra import __version__ as _ver
        self.setWindowTitle(
            f"Lyra v{_ver} — Hermes Lite 2+ SDR Transceiver")
        # App icon — shows in the window title bar, taskbar button,
        # Alt+Tab preview, and the rounded "pin to start" tile on
        # Windows 11. Loaded from the multi-res .ico so Windows picks
        # the right size for each context automatically.
        from PySide6.QtGui import QIcon
        from lyra import resource_root
        # resource_root() handles both dev-tree and PyInstaller-frozen
        # paths so the icon loads correctly from the .exe install dir.
        ico = resource_root() / "assets" / "logo" / "lyra.ico"
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

        # Permanent version readout on the right side of the status
        # bar so the operator can ALWAYS see what build they're on
        # (handy in screenshots accompanying bug reports). Lives in
        # the permanent-widgets area so transient status messages
        # don't overwrite it the way they would for showMessage().
        from lyra import __version__ as _ver, version_string as _vs
        from PySide6.QtWidgets import QLabel as _QLabel
        self._version_label = _QLabel(f"Lyra v{_ver}")
        self._version_label.setToolTip(_vs())
        self._version_label.setStyleSheet(
            "color: #6a7a8c; font-family: Consolas, monospace; "
            "padding: 0 8px;")
        self.statusBar().addPermanentWidget(self._version_label)

        self._load_settings()

        # Auto-snapshot of settings on every launch — gives the operator
        # a free "yesterday's working config" rollback target whenever
        # something in the current session goes sideways. Stored in
        # %LOCALAPPDATA%\\N8SDR\\Lyra\\snapshots\\auto-snapshot-*.json,
        # last 10 retained, surfaced via File → Snapshots.
        # Wrapped in try/except: a snapshot failure is annoying but
        # should never prevent Lyra from launching.
        try:
            from lyra.ui.settings_backup import auto_snapshot
            auto_snapshot(reason="launch")
        except Exception as _e:
            # Don't crash on permissions / disk-full / etc.
            print(f"Lyra: auto-snapshot on launch failed: {_e}")

        # OpenGL upsell — fire well after the main window is fully
        # painted, activated, and presented to the operator. Earlier
        # 600 ms wasn't enough on slower test machines (testers
        # reported the dialog never appeared on first launch); 2000 ms
        # gives Qt + the OS compositor + any antivirus first-launch
        # scan time to settle before the modal opens. The dialog
        # explicitly raise_()'s and activateWindow()'s itself in
        # _show_opengl_dialog so it can't get hidden behind the
        # main window even if the timing slips. Console log so we
        # can confirm in tester reports whether the nag actually
        # fired or was suppressed by the dismiss/already-on-OpenGL
        # rules.
        from PySide6.QtCore import QTimer as _QTimer
        _QTimer.singleShot(2000, self._maybe_show_opengl_nag)

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
        # Title is "Display" (was "View") — the old name collided with
        # the menu bar's View menu. Internal dict key stays "view" for
        # QSettings compat; only the user-facing title changed.
        self.docks["view"] = self._make_dock(
            "view", "Display", self.pnl_view)
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

        # ── Backup / import / export of all settings ────────────────
        # Operator-facing safety net: every preference Lyra stores
        # (layout, IP, audio device, AGC profile, color picks, balance,
        # cal trim, dock positions, band memory, etc.) lives under one
        # QSettings namespace. This menu lets the operator export the
        # whole thing to a JSON file, import a previously-saved file,
        # or roll back to one of the auto-snapshots taken on every
        # launch (kept in %LOCALAPPDATA%\\N8SDR\\Lyra\\snapshots).
        export_act = QAction("&Export settings…", self)
        export_act.setToolTip(
            "Save every Lyra preference to a JSON file you can keep, "
            "share, or use to migrate to another machine.")
        export_act.triggered.connect(self._on_export_settings)
        file_menu.addAction(export_act)

        import_act = QAction("&Import settings…", self)
        import_act.setToolTip(
            "Load a previously-exported JSON settings file. Auto-takes "
            "a safety snapshot first so the import is reversible.")
        import_act.triggered.connect(self._on_import_settings)
        file_menu.addAction(import_act)

        # Snapshots — populated dynamically each time the menu opens
        # so the list reflects whatever's currently in the snapshots
        # directory (auto + manual).
        self.snapshots_menu = file_menu.addMenu("S&napshots")
        self.snapshots_menu.aboutToShow.connect(self._populate_snapshots_menu)

        open_snapshots_folder_act = QAction("Open snapshots &folder…", self)
        open_snapshots_folder_act.setToolTip(
            "Open the folder where Lyra stores automatic snapshots and "
            "manual exports, in your file manager.")
        open_snapshots_folder_act.triggered.connect(
            self._on_open_snapshots_folder)
        file_menu.addAction(open_snapshots_folder_act)

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

        # Lock panels — when ON, every dock loses its movable / floatable /
        # closable bits so the operator can't accidentally drag a panel
        # off-position or close one in the middle of operating. Resize
        # between adjacent panels still works (those are QMainWindow
        # separators, not per-dock features). State persists in
        # QSettings under view/panels_locked.
        self.lock_panels_action = QAction("&Lock panels", self)
        self.lock_panels_action.setCheckable(True)
        self.lock_panels_action.setShortcut("Ctrl+L")
        self.lock_panels_action.setToolTip(
            "When checked, panel title bars are frozen — drag, float, "
            "and close are disabled so panels can't be moved by accident.\n"
            "Splitter resize between adjacent panels still works.\n"
            "Uncheck to rearrange panels.")
        self.lock_panels_action.toggled.connect(self._on_lock_panels_toggled)
        view_menu.addAction(self.lock_panels_action)

        view_menu.addSeparator()
        save_layout = QAction("Save current layout as my default", self)
        save_layout.setToolTip(
            "Capture the current arrangement (panel positions, sizes, "
            "splitter widths) and use it as the new default. "
            "'Restore my saved layout' below will return to THIS state.\n\n"
            "Refuses to save if any panel is too small to read — "
            "prevents capturing a momentarily-broken layout.")
        save_layout.triggered.connect(self._save_current_as_default_layout)
        view_menu.addAction(save_layout)

        restore_user = QAction("Restore my saved layout", self)
        restore_user.setToolTip(
            "Return to the panel arrangement you saved with "
            "'Save current layout as my default'. Separate from "
            "'Reset Panel Layout' — Reset always goes to factory, "
            "this goes to your saved version.")
        restore_user.triggered.connect(self._restore_user_default_layout)
        view_menu.addAction(restore_user)

        reset = QAction("Reset Panel Layout", self)
        reset.setToolTip(
            "ALWAYS restore the factory arrangement (Tuning + Mode + "
            "View on top, Band + Meters split, DSP+Audio at bottom). "
            "The universal panic button — works regardless of saved "
            "layouts or current state.")
        reset.triggered.connect(self._reset_layout)
        view_menu.addAction(reset)

        n8sdr_layout = QAction("Apply N8SDR layout", self)
        n8sdr_layout.setToolTip(
            "Apply the N8SDR operator's 3-row layout: Band+Tuning+Meters "
            "across the top, Mode+Filter and DSP+Audio stacked beneath, "
            "Display column on the right next to Meters. After applying, "
            "use 'Save current layout as my default' to lock it in.")
        n8sdr_layout.triggered.connect(self._apply_n8sdr_layout)
        view_menu.addAction(n8sdr_layout)
        clear_default = QAction("Forget saved layout", self)
        clear_default.setToolTip(
            "Discard the user-saved default layout. "
            "'Restore my saved layout' will report 'no saved layout' "
            "until you save a new one.")
        clear_default.triggered.connect(self._clear_user_default_layout)
        view_menu.addAction(clear_default)

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

        help_menu.addSeparator()
        # Diagnostics — HL2 telemetry probe. Captures live C&C bytes
        # for a few seconds so we can verify which C0 address carries
        # temperature / supply on the operator's specific HL2 firmware
        # without guessing through public docs.
        telem_probe_act = QAction("HL2 &Telemetry Probe…", self)
        telem_probe_act.setToolTip(
            "Capture a few seconds of HL2 telemetry bytes and show "
            "which C0 addresses carry which AIN values. Use this if "
            "the toolbar T/V readouts look wrong.")
        telem_probe_act.triggered.connect(self._open_telem_probe)
        help_menu.addAction(telem_probe_act)

        # Network Discovery Probe — diagnostic for "auto-discover
        # didn't find my HL2." Lists local IP interfaces, runs
        # broadcast / unicast discovery with full diagnostic logging,
        # offers copy-to-clipboard so testers can paste the log into
        # a bug report without hand-typing it.
        net_probe_act = QAction("&Network Discovery Probe…", self)
        net_probe_act.setToolTip(
            "Diagnose 'HL2 not found' issues. Shows your local "
            "network interfaces and runs discovery with full "
            "diagnostic logging — copy-to-clipboard for support.")
        net_probe_act.triggered.connect(self._open_discover_probe)
        help_menu.addAction(net_probe_act)

        # Test entry point for the OpenGL upsell dialog. Mostly useful
        # to operators who already enabled OpenGL but want to see what
        # the prompt looks like (otherwise the suppression rule hides
        # it from them forever). Also handy for QA / screenshots.
        opengl_dialog_act = QAction("Show OpenGL Suggestion Dialog", self)
        opengl_dialog_act.setToolTip(
            "Manually open the OpenGL graphics-backend suggestion dialog. "
            "Bypasses the 'don't show again' and 'OpenGL already active' "
            "silencing rules so you can see what new operators will see "
            "on first launch.")
        opengl_dialog_act.triggered.connect(
            lambda: self._show_opengl_dialog(force=True))
        help_menu.addAction(opengl_dialog_act)

        help_menu.addSeparator()
        # Check for updates — queries the GitHub releases API for the
        # latest tag, compares against the running lyra.__version__,
        # and shows a friendly dialog with the result. Background
        # thread for the network call so the UI doesn't freeze.
        update_act = QAction("Check for &Updates…", self)
        update_act.setToolTip(
            "Check the GitHub repo for a newer Lyra release. "
            "Single GET to the public releases API — no telemetry, "
            "no account, no data sent.")
        update_act.triggered.connect(self._on_check_for_updates)
        help_menu.addAction(update_act)

        # ☕ Support Lyra — opens the User Guide directly to the
        # support / donation topic. Single click for operators who
        # want to chip in via PayPal.
        support_act = QAction("☕ &Support Lyra…", self)
        support_act.setToolTip(
            "Open the User Guide → Support topic. Lyra is free and "
            "open-source; if it's been useful for your station, "
            "consider a small donation to keep development going.")
        support_act.triggered.connect(
            lambda: self.show_help("support"))
        help_menu.addAction(support_act)

        license_act = QAction("&License (MIT)…", self)
        license_act.setToolTip(
            "Open the User Guide → License topic. MIT — "
            "permissive, ham-radio-friendly, no warranty.")
        license_act.triggered.connect(
            lambda: self.show_help("license"))
        help_menu.addAction(license_act)

        about_act = QAction("&About Lyra…", self)
        about_act.setToolTip(
            "Version, build info, repo link, and license summary.")
        about_act.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_act)

    def _build_toolbar(self):
        """Main toolbar — explicit left-to-right order:

            Start  ●Streaming  ◌TCI Ready  ⚙Settings  Reset Panel Layout
            [dock toggles — Tuning / Mode+Filter / View / Band / Meters / DSP+Audio]
            ── spacer ── [ADC pk/rms]
            ── spacer ── [Local clock] [UTC clock]   (centered)
            ── spacer ── [HL2 T/V] [CPU%]

        Three Expanding spacers between ADC, Clocks, and HL2/CPU
        distribute them evenly so the clocks land in the visual
        center of the bar and the HL2/CPU cluster sits on the right.
        """
        from PySide6.QtWidgets import QLabel, QWidget, QSizePolicy
        from PySide6.QtCore import QTimer

        tb = QToolBar("Main", self)
        tb.setObjectName("main_toolbar")
        tb.setMovable(True)
        tb.setIconSize(tb.iconSize())  # use platform default

        # ── 1. Start / Stop ────────────────────────────────────────
        self.start_action = QAction("▶  Start", self)
        self.start_action.setCheckable(True)
        self.start_action.setToolTip("Start/stop the HL2 stream")
        self.start_action.toggled.connect(self._on_start_toggled)
        tb.addAction(self.start_action)

        # ── 2. Streaming status dot ────────────────────────────────
        self.status_dot = QLabel("  ●  not streaming  ")
        self.status_dot.setStyleSheet("color: #8a9aac; font-weight: 600;")
        tb.addWidget(self.status_dot)

        # ── 3. TCI Ready indicator ─────────────────────────────────
        # Replaces the former TCI dock panel — shows server state +
        # client count at a glance. Click to open Network settings.
        self.tci_indicator = QLabel("  ◌  TCI off  ")
        self.tci_indicator.setStyleSheet(
            "color: #6a7a8c; font-weight: 600; padding: 0 4px;")
        self.tci_indicator.setToolTip(
            "TCI server status. Click to open Network/TCI settings.")
        self.tci_indicator.setCursor(Qt.PointingHandCursor)
        self.tci_indicator.mousePressEvent = (
            lambda ev: self._open_settings(tab="Network"))
        tb.addWidget(self.tci_indicator)
        server = self.pnl_tci.server
        server.running_changed.connect(self._update_tci_indicator)
        server.client_count_changed.connect(self._update_tci_indicator)
        self._update_tci_indicator()

        tb.addSeparator()

        # ── 4. Settings ────────────────────────────────────────────
        settings_action = QAction("⚙  Settings…", self)
        settings_action.setToolTip("Radio, Network/TCI, Hardware, Audio, DSP…")
        settings_action.triggered.connect(self._open_settings)
        tb.addAction(settings_action)

        # ── 5. (Reset Panel Layout used to live here; moved off the
        #       toolbar after operator reported accidental clicks
        #       blowing away their custom layout. Still available
        #       via View menu → "Reset Panel Layout" so it's not
        #       lost — just not next to Settings where you can hit
        #       it with one stray click.) ──────────────────────────

        tb.addSeparator()

        # ── 6. Dock toggle buttons (Tuning / Mode+Filter / View /
        #     Band / Meters / DSP+Audio) ────────────────────────────
        for dock_name, dock in self.docks.items():
            tb.addAction(dock.toggleViewAction())

        # ── Small fixed gap — keeps ADC visually attached to the
        #     left cluster (just a breath of padding after DSP+Audio)
        #     instead of floating off into the middle of the bar.
        gap_pre_adc = QWidget()
        gap_pre_adc.setFixedWidth(14)
        tb.addWidget(gap_pre_adc)

        # ── 7. ADC peak + RMS indicator ────────────────────────────
        # Live RX-chain headroom: PEAK for clipping margin, RMS for
        # signal-energy / linearity diagnostics. Sits left-of-center
        # so it's visually paired with the radio-state cluster on the
        # left rather than the host-machine cluster on the right.
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

        # ── Spacer #2 — pushes clocks toward visual center ────────
        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer2)

        # ── 8. Clocks (local + UTC) ────────────────────────────────
        # Sized ~2.4× the surrounding toolbar text so the operator can
        # read the time across the room without leaning in.
        self.clock_local = QLabel("--:--:--")
        self.clock_local.setStyleSheet(
            "color: #ffd54f; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 22px; padding: 0 8px;")
        self.clock_local.setToolTip("PC local time (HH:MM:SS)")
        tb.addWidget(self.clock_local)
        self.clock_utc = QLabel("--:--:--Z")
        self.clock_utc.setStyleSheet(
            "color: #80d8ff; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 22px; padding: 0 8px;")
        self.clock_utc.setToolTip("UTC time (HH:MM:SSZ) — always Zulu")
        tb.addWidget(self.clock_utc)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clocks)
        self._clock_timer.start()
        self._tick_clocks()

        # ── Spacer #3 — equal stretch with spacer2 so the clocks
        #     land at the visual midpoint between ADC and HL2/CPU.
        spacer3 = QWidget()
        spacer3.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer3)

        # ── 9. HL2 hardware telemetry (temperature + supply V) ────
        self.hl2_telem_label = QLabel("HL2  T --°C   V --.- V")
        self.hl2_telem_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 6px;")
        self.hl2_telem_label.setToolTip(
            "HL2 hardware telemetry (read from EP6 stream).\n\n"
            "T — AD9866 on-die temperature sensor (°C). Idle ~45-55,\n"
            "    warm ~60-70, hot >75 — check airflow if you see\n"
            "    sustained readings above ~80 °C.\n\n"
            "V — 12 V supply rail measured through the on-board\n"
            "    divider. Should sit within 11.5-13.0 V on a healthy\n"
            "    PSU; sagging below 11 V points to a weak supply or\n"
            "    a long thin power lead.\n\n"
            "Reads '--' until the stream is running and a few EP6\n"
            "frames have arrived — the radio rotates which telemetry\n"
            "register it reports each frame.")
        tb.addWidget(self.hl2_telem_label)
        self.radio.hl2_telemetry_changed.connect(self._update_hl2_telemetry)

        # ── 10. CPU usage indicator (whole-system %, matches Task
        #     Manager's "CPU" column for this process) ──────────────
        # psutil.Process.cpu_percent() returns PER-CORE normalized
        # (so a single-thread 100% load on 1 of 8 cores reads as
        # 100%). Task Manager shows total-system % (12.5% in that
        # example), so we divide by core count to match what the
        # operator sees in Task Manager. Without this divisor the
        # Lyra reading was inflated by a factor of cpu_count.
        self.cpu_label = QLabel("CPU --%")
        self.cpu_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 6px;")
        self.cpu_label.setToolTip(
            "Lyra process CPU usage (% of total system CPU,\n"
            "matches Task Manager's per-process column).\n\n"
            "Sustained values above ~15-25% on a modern multi-core\n"
            "PC suggest an FFT size / sample-rate combination that's\n"
            "heavier than your CPU comfortably handles.")
        tb.addWidget(self.cpu_label)
        self._cpu_proc = None
        self._cpu_count = 1
        try:
            import psutil
            self._cpu_proc = psutil.Process()
            # Prime the CPU sampler — first call returns 0.0 because
            # percent() is delta-since-last.
            self._cpu_proc.cpu_percent(interval=None)
            # Logical core count, including SMT/hyperthreaded cores
            # (matches Windows Task Manager's denominator).
            self._cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        except Exception:
            self._cpu_proc = None
        self._cpu_timer = QTimer(self)
        self._cpu_timer.setInterval(1000)
        self._cpu_timer.timeout.connect(self._tick_cpu)
        self._cpu_timer.start()

        # ── 11. GPU usage indicator ────────────────────────────────
        # System-wide GPU utilisation %. Useful for diagnosing whether
        # window-compositor / Lyra paint workload is putting load on
        # the GPU. Lyra's spectrum/waterfall use QPainter (CPU paint),
        # so Lyra itself contributes little to GPU; this readout is
        # mainly for spotting external GPU load competing with the
        # PC's general responsiveness.
        #
        # Tries pynvml first (real NVIDIA per-GPU% with no extra
        # process spawn). Falls back to "n/a" if the lib isn't
        # installed or no NVIDIA GPU is present — graceful, the rest
        # of the toolbar keeps working unchanged.
        self.gpu_label = QLabel("GPU --%")
        self.gpu_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 6px;")
        self.gpu_label.setToolTip(
            "GPU utilization (system-wide %).\n\n"
            "Lyra's spectrum/waterfall paint with QPainter (CPU),\n"
            "so Lyra itself contributes little to GPU load — but\n"
            "this readout is useful for spotting external apps\n"
            "competing for GPU resources, or for confirming that\n"
            "the OS compositor isn't the bottleneck on slower PCs.\n\n"
            "Reads NVIDIA GPUs via NVML; AMD / Intel via Windows\n"
            "Performance Counters (PDH); no driver = 'n/a'.\n\n"
            "Right-click → Hide if you're investigating spectrum\n"
            "paint stutter — PDH calls are usually fast but can\n"
            "occasionally hit slow paths on Windows. Toggling this\n"
            "off is the fastest A/B test for that suspicion.")
        tb.addWidget(self.gpu_label)
        # GPU monitor — try two paths in order of preference:
        #
        # 1. NVML (NVIDIA only)  — precise per-card utilisation; works
        #    on any OS where the NVIDIA driver is installed. Lower
        #    overhead than the PDH path.
        # 2. Windows Performance Counters (any vendor)  — uses Win10+'s
        #    `\GPU Engine(*)\Utilization Percentage` counter set, which
        #    Microsoft populates from WDDM regardless of the GPU
        #    vendor. So AMD / Intel iGPU / NVIDIA (without NVML) all
        #    work via this path.
        # 3. None of the above — label stays "GPU n/a" and the rest
        #    of the toolbar is unaffected.
        self._gpu_mode = None        # "nvml" | "pdh" | None
        self._gpu_handle = None      # nvml device handle if mode == nvml
        self._gpu_nvml = None
        self._gpu_pdh_query = None   # PDH query handle if mode == pdh
        self._gpu_pdh_counter = None
        # Try NVML first.
        try:
            # The PyPI package is `nvidia-ml-py` (modern, non-deprecated).
            # It exposes its module as `pynvml` for legacy compatibility,
            # which triggers a FutureWarning from the older `pynvml`
            # deprecation shim if both are installed. Suppress that
            # warning scoped to this import only.
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=FutureWarning)
                import pynvml as _nvml
            _nvml.nvmlInit()
            if _nvml.nvmlDeviceGetCount() > 0:
                self._gpu_handle = _nvml.nvmlDeviceGetHandleByIndex(0)
                self._gpu_nvml = _nvml
                self._gpu_mode = "nvml"
        except Exception:
            pass
        # If NVML didn't bind, try Windows PDH (works for any vendor).
        if self._gpu_mode is None:
            try:
                import win32pdh
                self._gpu_pdh_module = win32pdh
                self._gpu_pdh_query = win32pdh.OpenQuery()
                self._gpu_pdh_counter = win32pdh.AddCounter(
                    self._gpu_pdh_query,
                    r"\GPU Engine(*)\Utilization Percentage")
                # PDH counters require two samples to compute a delta;
                # prime the query so the first _tick_gpu reads valid
                # data instead of zeroes.
                win32pdh.CollectQueryData(self._gpu_pdh_query)
                self._gpu_mode = "pdh"
            except Exception:
                # No pywin32, no Win10+ GPU counters, or PDH refused
                # the wildcard — silently fall back to "n/a".
                self._gpu_mode = None
        self._gpu_timer = QTimer(self)
        self._gpu_timer.setInterval(1000)
        self._gpu_timer.timeout.connect(self._tick_gpu)
        self._gpu_timer.start()
        # Right-click to hide — useful for A/B-testing whether the
        # GPU readout's PDH polling is contributing to spectrum
        # paint stutter on this hardware. PDH calls are usually fast
        # but can hit slow paths (counter reload, GPU-engine enum)
        # that block the calling thread for tens of ms.
        self.gpu_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.gpu_label.customContextMenuRequested.connect(
            lambda pos: self._show_readout_menu(self.gpu_label, "gpu", pos))
        # Same right-click affordance on CPU and HL2 for symmetry
        # and so the operator can isolate any of the three timers
        # if they want to.
        self.cpu_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cpu_label.customContextMenuRequested.connect(
            lambda pos: self._show_readout_menu(self.cpu_label, "cpu", pos))
        self.hl2_telem_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.hl2_telem_label.customContextMenuRequested.connect(
            lambda pos: self._show_readout_menu(self.hl2_telem_label, "hl2", pos))

        # ── Small fixed right margin — keeps HL2/CPU/GPU breathing
        #     room from the right edge of the toolbar instead of
        #     being jammed against the window border.
        gap_right = QWidget()
        gap_right.setFixedWidth(20)
        tb.addWidget(gap_right)

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

    # ── Banner clocks / telemetry / CPU handlers ─────────────────────
    def _tick_clocks(self):
        """1 Hz tick — repaint local + UTC clocks. We re-read the
        system clock each tick (rather than incrementing a counter) so
        the labels stay correct across DST boundaries, sleep/resume,
        and manual time changes."""
        from datetime import datetime, timezone
        now_local = datetime.now()
        now_utc = datetime.now(timezone.utc)
        self.clock_local.setText(now_local.strftime("%H:%M:%S"))
        self.clock_utc.setText(now_utc.strftime("%H:%M:%SZ"))

    def _update_hl2_telemetry(self, payload: dict):
        """Radio.hl2_telemetry_changed → update banner label.
        NaN fields render as 'n/a' so the operator can tell when a
        field isn't present in their HL2 firmware's telemetry stream
        (some firmware revisions don't populate the supply slot, for
        example) vs. an actual zero reading."""
        import math
        t = payload.get("temp_c", float("nan"))
        v = payload.get("supply_v", float("nan"))
        t_str = f"{t:4.1f}°C" if not math.isnan(t) else " n/a "
        v_str = f"{v:4.1f} V" if not math.isnan(v) else " n/a "
        self.hl2_telem_label.setText(f"HL2  T {t_str}   V {v_str}")

    def _tick_cpu(self):
        """1 Hz tick — refresh the CPU% label as a fraction of TOTAL
        system CPU (matches Task Manager's per-process column).

        psutil.Process.cpu_percent() returns PER-CORE normalized: a
        single-thread process pegged on one of N logical cores reads
        as 100% from that call. Task Manager normalizes against total
        system capacity (so the same process reads as 100/N %). Dividing
        by the logical core count makes Lyra's banner match what the
        operator sees in Task Manager — without the divisor, the Lyra
        reading was inflated by a factor equal to the core count
        (e.g. 8× too high on an 8-thread CPU).
        """
        if self._cpu_proc is None:
            return
        try:
            raw = self._cpu_proc.cpu_percent(interval=None)
        except Exception:
            return
        pct = raw / self._cpu_count
        self.cpu_label.setText(f"CPU {pct:4.1f}%")
        # Color thresholds tightened to match the new (smaller)
        # number range: <8% green, <16% yellow, <30% orange, ≥30% red.
        # On an 8-core box, 30% total ≈ 240% raw — that's well into
        # "DSP load is starting to bite" territory.
        if pct >= 30:
            color = "#ff4040"
        elif pct >= 16:
            color = "#ff8c3a"
        elif pct >= 8:
            color = "#ffd54f"
        else:
            color = "#39ff14"
        self.cpu_label.setStyleSheet(
            f"color: {color}; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 6px;")

    def _tick_gpu(self):
        """1 Hz tick — refresh the GPU% label.

        Two backend paths (see _build_toolbar):
        - "nvml" : NVIDIA per-card utilisation via NVML
        - "pdh"  : Windows Performance Counters wildcard query that
                   sums per-process per-engine utilisation across the
                   whole system. Vendor-agnostic.

        PDH values can briefly exceed 100% because they sum 3D + Copy
        + Compute + Video engines — those engines run in parallel on
        modern GPUs, so a hot frame can saturate two engines at once.
        We clamp the displayed value at 100% (the conceptual max for a
        "busy GPU" indicator) but the raw could be higher."""
        if self._gpu_mode is None:
            self.gpu_label.setText("GPU  n/a ")
            return
        try:
            if self._gpu_mode == "nvml":
                rates = self._gpu_nvml.nvmlDeviceGetUtilizationRates(
                    self._gpu_handle)
                pct = float(rates.gpu)
            else:   # pdh
                pdh = self._gpu_pdh_module
                pdh.CollectQueryData(self._gpu_pdh_query)
                values = pdh.GetFormattedCounterArray(
                    self._gpu_pdh_counter, pdh.PDH_FMT_DOUBLE)
                # values is a dict {instance_name: float}; sum gives
                # total system GPU activity across all engines + procs.
                pct = float(sum(values.values()))
                # Clamp display: the raw can exceed 100 when multiple
                # engines saturate simultaneously, but for a single
                # "busy GPU" readout 100% is the visual ceiling.
                if pct > 100.0:
                    pct = 100.0
        except Exception:
            self.gpu_label.setText("GPU  n/a ")
            return
        self.gpu_label.setText(f"GPU {pct:4.1f}%")
        # Color thresholds matched to CPU label so a glance at both
        # gives a consistent "load = green/yellow/orange/red" reading.
        if pct >= 75:
            color = "#ff4040"
        elif pct >= 50:
            color = "#ff8c3a"
        elif pct >= 25:
            color = "#ffd54f"
        else:
            color = "#39ff14"
        self.gpu_label.setStyleSheet(
            f"color: {color}; font-family: Consolas, monospace; "
            "font-weight: 700; padding: 0 6px;")

    def _maybe_show_opengl_nag(self):
        """One-time prompt suggesting the operator switch to the
        OpenGL backend when they're currently using software paint.

        Triggered once after the main window first becomes visible.
        Suppressed when the operator has either:
        - Selected OpenGL in Visuals settings (ACTIVE_BACKEND ==
          opengl on next launch), or
        - Checked "Don't show this again" in the dialog.

        Triggered manually via the test entry point
        Help → "Test OpenGL Suggestion Dialog" so operators can see
        what the dialog looks like without forcibly downgrading their
        backend setting.
        """
        from lyra.ui.gfx import ACTIVE_BACKEND, BACKEND_SOFTWARE
        # Diagnostic log so we can correlate tester reports against
        # what actually happened. ALL three branches print so a
        # tester saying "I never saw the dialog" lets us see WHY in
        # their console output.
        if ACTIVE_BACKEND != BACKEND_SOFTWARE:
            print(f"Lyra: OpenGL nag suppressed — already on backend "
                  f"{ACTIVE_BACKEND!r}")
            return
        if self._settings.value("ui/opengl_nag_dismissed", False, type=bool):
            print("Lyra: OpenGL nag suppressed — operator chose "
                  "'Don't show again' on a previous launch")
            return
        print("Lyra: showing OpenGL suggestion dialog (first launch / "
              "operator hasn't dismissed yet)")
        self._show_opengl_dialog(force=False)

    def _show_opengl_dialog(self, force: bool):
        """Render the OpenGL upsell dialog. Custom QDialog rather than
        QMessageBox so the 'Don't show again' checkbox is part of the
        normal layout flow (the QMessageBox checkBox API renders it in
        an awkward spot under the buttons that operators routinely miss).

        `force` = True bypasses the silencing rules — used by the
        Help-menu test entry point so operators can inspect the dialog
        even when they've already selected OpenGL or dismissed it.
        """
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QCheckBox,
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("Lyra — graphics backend")
        dlg.setMinimumWidth(520)

        v = QVBoxLayout(dlg)

        title = QLabel("<b>Lyra is using software (CPU) rendering.</b>")
        title.setStyleSheet("font-size: 14px; padding-bottom: 6px;")
        v.addWidget(title)

        body = QLabel(
            "Switching to the OpenGL backend offloads spectrum and "
            "waterfall painting to your GPU, which usually means:"
            "<br><br>"
            "&nbsp;&nbsp;• Smoother resize / fullscreen transitions<br>"
            "&nbsp;&nbsp;• Lower CPU% on the toolbar<br>"
            "&nbsp;&nbsp;• Less audio stutter under heavy DSP load"
            "<br><br>"
            "It works on essentially every GPU from the last 15 years "
            "and falls back to software automatically if it can't "
            "initialise."
            "<br><br>"
            "<b>Open Visuals settings now to switch?</b><br>"
            "<span style='color:#8a9aac'>(A restart is required after "
            "changing the backend.)</span>"
        )
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        v.addWidget(body)

        # Checkbox sits inside the dialog body, ABOVE the buttons,
        # where the operator will actually see it.
        dont_show = QCheckBox("Don't show this again")
        dont_show.setStyleSheet("padding-top: 10px;")
        v.addWidget(dont_show)

        # Buttons row
        h = QHBoxLayout()
        h.addStretch(1)
        later_btn = QPushButton("Maybe Later")
        later_btn.clicked.connect(dlg.reject)
        h.addWidget(later_btn)
        open_btn = QPushButton("Open Visuals Settings")
        open_btn.setDefault(True)
        open_btn.clicked.connect(dlg.accept)
        h.addWidget(open_btn)
        v.addLayout(h)

        # Force the dialog to the foreground. Without these calls
        # the dialog can land BEHIND the main window on Windows when
        # the main window has just been activated by show() — Qt's
        # modal grab is set on a window that's still settling its
        # Z-order. This bit testers on first launch.
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        result = dlg.exec()

        # Persist dismissal independent of which button was pressed —
        # both buttons close the dialog, the checkbox is the silencer.
        # The `force` path also persists, so an operator using the test
        # entry point can use it to flip the silencer on/off without
        # editing QSettings by hand.
        self._settings.setValue(
            "ui/opengl_nag_dismissed", bool(dont_show.isChecked()))

        if result == QDialog.Accepted:
            self._open_settings(tab="Visuals")

    def _show_about_dialog(self):
        """Help → About Lyra. Version, build date, repo link, license.

        Pulls all version-related strings from `lyra.__init__` so this
        dialog never goes stale relative to the package's actual
        version — bumping `__version__` in one place updates the
        title bar, the QApplication.applicationVersion(), and this
        dialog at once.
        """
        from lyra import __version__, version_string
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.about(
            self,
            "About Lyra",
            f"<h2>Lyra v{__version__}</h2>"
            f"<p style='color:#8a9aac'>{version_string()}</p>"
            "<p>Qt6 / PySide6 desktop SDR transceiver for the "
            "Hermes Lite 2 / 2+.</p>"
            "<p>Author: <b>Rick Langford (N8SDR)</b><br>"
            "Repository: <a href='https://github.com/N8SDR1/Lyra-SDR'>"
            "github.com/N8SDR1/Lyra-SDR</a><br>"
            "License: MIT</p>"
            "<p style='color:#8a9aac; font-size:10px'>"
            "TCI server protocol © EESDR Expert Electronics, "
            "implemented from the public TCI v1.9 / v2.0 spec."
            "</p>"
        )

    # ── Toolbar readout visibility (HL2 / CPU / GPU) ────────────────
    # Each of the three diagnostic readouts on the right side of the
    # toolbar can be hidden independently. Useful both for tidying up
    # the toolbar (operators who don't care about CPU%) and for
    # A/B-testing whether one of the timers is contributing to
    # paint stutter (mostly the GPU PDH path on Windows).
    _READOUT_LABELS = {
        "hl2": "HL2 telemetry",
        "cpu": "CPU usage",
        "gpu": "GPU usage",
    }

    def _show_readout_menu(self, label_widget, key: str, pos):
        """Right-click on a toolbar readout → hide / unhide menu."""
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        nice = self._READOUT_LABELS.get(key, key)
        hide_act = menu.addAction(f"Hide {nice} readout")
        hide_act.triggered.connect(lambda: self._set_readout_visible(key, False))
        # If any readouts are currently hidden, offer a "show all" entry.
        any_hidden = any(
            self._settings.value(f"toolbar/readout_hidden_{k}", False, type=bool)
            for k in self._READOUT_LABELS
        )
        if any_hidden:
            menu.addSeparator()
            show_all = menu.addAction("Show all hidden readouts")
            show_all.triggered.connect(self._show_all_readouts)
        menu.exec(label_widget.mapToGlobal(pos))

    def _set_readout_visible(self, key: str, visible: bool):
        """Show/hide a toolbar readout AND start/stop its driving
        timer so a hidden readout truly costs zero — no PDH polling,
        no telemetry emit, no clock tick wasted on something the
        operator can't see."""
        widget, timer_attr = {
            "hl2": (self.hl2_telem_label, None),  # no per-tick UI timer; data comes from radio
            "cpu": (self.cpu_label, "_cpu_timer"),
            "gpu": (self.gpu_label, "_gpu_timer"),
        }[key]
        widget.setVisible(visible)
        if timer_attr is not None:
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                if visible:
                    timer.start()
                else:
                    timer.stop()
        # The HL2 telemetry timer lives on Radio (2 Hz) — toggling it
        # would also affect anyone else listening, so we just hide the
        # label and leave Radio's timer running (cost: ~zero, just a
        # signal emit nobody reads).
        self._settings.setValue(f"toolbar/readout_hidden_{key}", not visible)
        self._settings.sync()
        self.statusBar().showMessage(
            f"{self._READOUT_LABELS[key]} {'hidden' if not visible else 'shown'}"
            "  —  right-click another readout to toggle, or use 'Show all'.",
            3000)

    def _show_all_readouts(self):
        """Restore visibility of all toolbar readouts (useful after
        an A/B test or just to recover from accidentally hiding one)."""
        for key in self._READOUT_LABELS:
            self._set_readout_visible(key, True)

    def _apply_readout_visibility_from_settings(self):
        """Read persisted hidden-state and apply on launch — called
        from _load_settings() so an operator's "I always want CPU
        hidden" preference carries over restarts."""
        for key in self._READOUT_LABELS:
            hidden = self._settings.value(
                f"toolbar/readout_hidden_{key}", False, type=bool)
            if hidden:
                self._set_readout_visible(key, False)

    # ── Lock / unlock panels ────────────────────────────────────────
    def _on_lock_panels_toggled(self, locked: bool):
        """View → Lock panels — toggle dock-bar drag/float/close
        features on every panel AND the QSplitter handles between
        panels. When locked, panels can't be moved or resized
        accidentally. State is persisted to QSettings so it
        survives across launches."""
        self._apply_panel_lock(locked)
        self._settings.setValue("view/panels_locked", bool(locked))
        self._settings.sync()
        # Brief status-bar toast so the operator sees the toggle
        # took effect (the title-bar visual change is subtle).
        self.statusBar().showMessage(
            "Panels locked — Ctrl+L to unlock"
            if locked else
            "Panels unlocked — drag freely",
            2500)

    def _apply_panel_lock(self, locked: bool) -> None:
        """Push the lock state out to all dock features and splitter
        handles. Split out from the toggle handler so we can re-apply
        after a layout reset (which creates new QSplitters that don't
        inherit the previous lock state)."""
        from PySide6.QtWidgets import QSplitter
        if locked:
            features = QDockWidget.NoDockWidgetFeatures
        else:
            features = (QDockWidget.DockWidgetMovable
                        | QDockWidget.DockWidgetFloatable
                        | QDockWidget.DockWidgetClosable)
        for dock in self.docks.values():
            dock.setFeatures(features)
        # QMainWindow internally builds QSplitters between docked
        # widgets. Disabling each handle blocks drag-to-resize while
        # keeping the visual separator visible (operator can still
        # see the boundaries — just can't grab them).
        for splitter in self.findChildren(QSplitter):
            for i in range(1, splitter.count()):
                handle = splitter.handle(i)
                if handle is not None:
                    handle.setEnabled(not locked)

    def _apply_panels_lock_from_settings(self):
        """Read the persisted lock state and apply on launch.
        Called from _load_settings()."""
        locked = self._settings.value(
            "view/panels_locked", False, type=bool)
        # Set the action's checked state without firing the toggled
        # signal (we'll apply via the helper directly to avoid the
        # double-trip through the setter).
        self.lock_panels_action.blockSignals(True)
        self.lock_panels_action.setChecked(bool(locked))
        self.lock_panels_action.blockSignals(False)
        self._on_lock_panels_toggled(bool(locked))

    # ── Settings backup / import / export ────────────────────────────
    def _on_export_settings(self):
        """File → Export settings… — save the QSettings namespace to a
        JSON file the operator picks. Uses the snapshots directory as
        the default starting location so manual exports live alongside
        the auto-snapshots in one easy-to-browse place."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from datetime import datetime
        from lyra.ui.settings_backup import snapshots_dir, export_settings
        # Auto-save first so the JSON has the latest UI state
        # (window geometry, dock positions, splitter widths) — the
        # operator's expectation is "I'm exporting WHAT I SEE NOW."
        try:
            self._save_settings()
        except Exception:
            pass
        default_name = (
            f"Lyra-config-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json")
        default_path = str(snapshots_dir() / default_name)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Lyra settings",
            default_path, "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            from pathlib import Path as _Path
            n = export_settings(_Path(path))
            QMessageBox.information(
                self, "Settings exported",
                f"Wrote {n} settings to:\n{path}")
        except Exception as e:
            QMessageBox.critical(
                self, "Export failed",
                f"Could not write settings to:\n{path}\n\n{e}")

    def _on_import_settings(self):
        """File → Import settings… — load a JSON file and replay it
        into QSettings. A safety snapshot of the current state is taken
        FIRST so the operator can roll back via Snapshots if the import
        was wrong. Layout-affecting changes need a restart to fully
        take effect — we surface that in the success dialog."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from lyra.ui.settings_backup import snapshots_dir, import_settings
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Lyra settings",
            str(snapshots_dir()), "JSON files (*.json);;All files (*)")
        if not path:
            return
        # Last-chance "really do this?" confirmation — import REPLACES
        # all current settings (clear-then-write) so the user shouldn't
        # be surprised after the click.
        reply = QMessageBox.question(
            self, "Import settings",
            "Importing will REPLACE all your current Lyra settings "
            "with the contents of:\n\n"
            f"{path}\n\n"
            "A safety snapshot of your current state will be taken "
            "first, so you can roll back via "
            "File → Snapshots if needed.\n\n"
            "Proceed?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel)
        if reply != QMessageBox.Yes:
            return
        try:
            from pathlib import Path as _Path
            n, safety_path = import_settings(_Path(path))
            QMessageBox.information(
                self, "Settings imported",
                f"Imported {n} settings from:\n{path}\n\n"
                f"Safety snapshot of your prior state was saved as:\n"
                f"{safety_path.name if safety_path else '(none)'}\n\n"
                "Some changes (panel layout, graphics backend, font "
                "sizes) need a Lyra restart to take effect.")
        except Exception as e:
            QMessageBox.critical(
                self, "Import failed",
                f"Could not import settings from:\n{path}\n\n{e}\n\n"
                "Your current settings have not been changed.")

    def _populate_snapshots_menu(self):
        """File → Snapshots — rebuild the submenu every time it opens
        so it always reflects the latest contents of the snapshots
        directory. Each entry is a clickable action that restores
        that snapshot (with the same safety-snapshot-first behavior
        as the regular Import action)."""
        from lyra.ui.settings_backup import list_snapshots, snapshot_summary
        self.snapshots_menu.clear()
        snaps = list_snapshots()
        if not snaps:
            empty = self.snapshots_menu.addAction(
                "(no snapshots yet — one is taken on each launch)")
            empty.setEnabled(False)
            return
        # Show up to ~15 most recent so the menu doesn't get
        # absurdly long if the operator's been running Lyra a lot.
        for path in snaps[:15]:
            summ = snapshot_summary(path)
            mtime = summ["mtime"]
            label = f"{mtime.strftime('%Y-%m-%d  %H:%M:%S')}  —  v{summ['lyra_version']}"
            reason = summ.get("snapshot_reason")
            if reason and reason != "launch":
                label += f"  ({reason})"
            act = self.snapshots_menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, p=path: self._restore_snapshot(p))

    def _restore_snapshot(self, path):
        """Restore from one of the snapshot files in the Snapshots
        submenu. Same flow as Import (with safety snapshot first)."""
        from PySide6.QtWidgets import QMessageBox
        from lyra.ui.settings_backup import import_settings
        reply = QMessageBox.question(
            self, "Restore snapshot",
            f"Restore Lyra settings from this snapshot?\n\n"
            f"{path.name}\n\n"
            "A safety snapshot of your CURRENT state will be taken "
            "first, so this is reversible.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel)
        if reply != QMessageBox.Yes:
            return
        try:
            n, safety_path = import_settings(path)
            QMessageBox.information(
                self, "Snapshot restored",
                f"Restored {n} settings.\n\n"
                f"Your previous state was saved as:\n"
                f"{safety_path.name if safety_path else '(none)'}\n\n"
                "Restart Lyra for layout / graphics-backend changes "
                "to take full effect.")
        except Exception as e:
            QMessageBox.critical(
                self, "Restore failed",
                f"Could not restore from:\n{path}\n\n{e}\n\n"
                "Your current settings have not been changed.")

    def _on_open_snapshots_folder(self):
        """File → Open snapshots folder — launch the OS file manager
        at the snapshots directory so the operator can copy / move /
        archive snapshot files manually."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from lyra.ui.settings_backup import snapshots_dir
        d = snapshots_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _on_check_for_updates(self):
        """Help → Check for Updates… — open the update-check dialog
        which queries GitHub for the latest release in a worker
        thread, compares versions, and shows the result."""
        from lyra.ui.update_check import CheckForUpdatesDialog
        CheckForUpdatesDialog(parent=self).exec()

    def _open_discover_probe(self):
        """Help → Network Discovery Probe… — opens the diagnostic
        dialog for 'auto-discover didn't find my HL2' issues."""
        from lyra.ui.discover_probe import NetworkDiscoveryProbeDialog
        NetworkDiscoveryProbeDialog(parent=self).exec()

    def _open_telem_probe(self):
        """Help → HL2 Telemetry Probe. Opens the diagnostic dialog
        that captures live C&C bytes and shows per-address summaries
        so we can verify the correct telemetry mapping for this HL2
        firmware without guessing through public docs."""
        from lyra.ui.telem_probe import TelemetryProbeDialog
        TelemetryProbeDialog(self.radio, parent=self).exec()

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

    # ── Layout sanity check ──────────────────────────────────────────
    # Any dock smaller than this in either dimension is "degenerate"
    # — the operator can't see useful UI in there. If the live layout
    # is degenerate, refuse to persist it.
    _MIN_VISIBLE_DOCK_W_PX     = 80
    _MIN_VISIBLE_DOCK_H_PX     = 50
    _MIN_CENTRAL_W_PX          = 200
    _MIN_CENTRAL_H_PX          = 120
    _MIN_MAIN_WINDOW_W_PX      = 600
    _MIN_MAIN_WINDOW_H_PX      = 400

    def _is_layout_state_sane(self) -> tuple[bool, str]:
        """Validate the LIVE layout before persisting it. Returns
        (is_sane, reason_if_not). The reason is a short human-readable
        string the caller can put in a status-bar toast."""
        if (self.width() < self._MIN_MAIN_WINDOW_W_PX
                or self.height() < self._MIN_MAIN_WINDOW_H_PX):
            return False, (f"main window {self.width()}×{self.height()} "
                           "below sane minimum")
        for name, dock in self.docks.items():
            if not dock.isVisible() or dock.isFloating():
                continue
            if (dock.width()  < self._MIN_VISIBLE_DOCK_W_PX
                    or dock.height() < self._MIN_VISIBLE_DOCK_H_PX):
                return False, (f"panel '{name}' is "
                               f"{dock.width()}×{dock.height()} "
                               "(below the sane minimum)")
        cw = self.centralWidget()
        if cw is not None:
            if (cw.width()  < self._MIN_CENTRAL_W_PX
                    or cw.height() < self._MIN_CENTRAL_H_PX):
                return False, ("central spectrum/waterfall area is "
                               f"{cw.width()}×{cw.height()} "
                               "(below sane minimum)")
        return True, ""

    # ── Reset Panel Layout — always goes to factory ──────────────────
    def _reset_layout(self):
        """ALWAYS restore the hardcoded factory arrangement.

        This is the universal "panic button" — it never goes to the
        user-saved default (use 'Restore my saved layout' for that).
        Earlier versions tried to be clever and prefer user-saved over
        factory, but that meant operators stuck with a corrupted saved
        layout had no escape hatch. Reset = factory, period.

        Factory arrangement: Tuning + Mode + View on top row,
        Band + Meters split, DSP+Audio at bottom.
        """
        for dock in self.docks.values():
            self.removeDockWidget(dock)
            dock.setFloating(False)
            dock.setVisible(True)
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
        # Re-apply the operator's panel-lock preference: layout reset
        # rebuilds the QSplitter tree, and the new handles default to
        # enabled. If the operator had panels locked before the reset,
        # they expect them locked after too.
        if self.lock_panels_action.isChecked():
            self._apply_panel_lock(True)
        self.statusBar().showMessage(
            "Panel layout reset to factory defaults", 2500)

    # Baked snapshot of the N8SDR operator's preferred layout, captured
    # by reading QSettings('N8SDR', 'Lyra')['user_default_dock_state']
    # and ['user_default_center_split'] after the operator hand-tuned
    # and saved it. restoreState reproduces it byte-for-byte regardless
    # of how Qt's internal splitter nesting would otherwise resolve a
    # splitDockWidget sequence — exact positions + exact proportions.
    #
    # Re-bake by re-running the extraction snippet documented in
    # docs/dev/layouts.md (or just on-the-fly via the python -c
    # snippet in commit b9182b3 of this file's history).
    _N8SDR_DOCK_STATE_B64 = (
        "AAAA/wAAAAD9AAAAAQAAAAIAAAoAAAABs/wBAAAAAvwAAAAAAAAHZwAABkQA////"
        "/AIAAAAC/AAAADcAAAEsAAAA7gD////8AQAAAAL8AAAAAAAAAyQAAALaAP////wC"
        "AAAAAvsAAAAIAGIAYQBuAGQBAAAANwAAAL0AAACKAP////sAAAAWAG0AbwBkAGUA"
        "XwBmAGkAbAB0AGUAcgEAAAD4AAAAawAAAE4A////+wAAAAwAdAB1AG4AaQBuAGcB"
        "AAADKAAABD8AAANmAP////sAAAASAGQAcwBwAF8AYQB1AGQAaQBvAQAAAWcAAACD"
        "AAAAbwD////8AAAHawAAApUAAAKGAP////wCAAAAAvsAAAAMAG0AZQB0AGUAcgBz"
        "AQAAADcAAAEpAAAA4gD////7AAAACAB2AGkAZQB3AQAAAWQAAACGAAAATAD///8A"
        "AAoAAAADVgAAAAQAAAAEAAAACAAAAAj8AAAAAQAAAAIAAAABAAAAGABtAGEAaQBu"
        "AF8AdABvAG8AbABiAGEAcgEAAAAA/////wAAAAAAAAAA"
    )
    _N8SDR_CENTER_SPLIT_B64 = (
        "AAAA/wAAAAEAAAACAAAA5wAAAmcAAAAABgEAAAACAA=="
    )

    def _apply_n8sdr_layout(self):
        """Apply the N8SDR operator's hand-tuned layout, byte-for-byte.

        Restores the exact dock arrangement and splitter proportions
        from the snapshot embedded above. Bypasses splitDockWidget's
        nesting quirks by going straight through restoreState().

        Layout (for reference, in case the snapshot ever needs to be
        re-derived):

            ┌────────┬─────────────────┬────────┐
            │  band  │     tuning      │ meters │
            ├────────┴─────────────────┤        │
            │      mode + filter       │  view  │
            ├──────────────────────────┤        │
            │       dsp + audio        │        │
            └──────────────────────────┴────────┘
            [ central panadapter / waterfall ]
        """
        import base64
        try:
            dock_bytes = base64.b64decode(self._N8SDR_DOCK_STATE_B64)
            ok = self.restoreState(dock_bytes)
            if not ok:
                # Restoration failed (Qt version mismatch, missing
                # dock object names, etc.) — fall through to the
                # programmatic build as a safety net.
                raise RuntimeError("restoreState returned False")
            split_bytes = base64.b64decode(self._N8SDR_CENTER_SPLIT_B64)
            self.center_splitter.restoreState(split_bytes)
        except Exception as e:
            # Programmatic fallback — build the layout via splitter
            # ops. Approximate proportions; operator drag-tunes from
            # there.
            print(f"[layout] N8SDR restoreState failed ({e}); "
                  "falling back to programmatic build")
            for dock in self.docks.values():
                self.removeDockWidget(dock)
                dock.setFloating(False)
                dock.setVisible(True)
            self.addDockWidget(Qt.TopDockWidgetArea, self.docks["band"])
            self.splitDockWidget(self.docks["band"],
                                 self.docks["meters"], Qt.Horizontal)
            self.splitDockWidget(self.docks["meters"],
                                 self.docks["view"], Qt.Vertical)
            self.splitDockWidget(self.docks["band"],
                                 self.docks["mode"], Qt.Vertical)
            self.splitDockWidget(self.docks["mode"],
                                 self.docks["dsp"], Qt.Vertical)
            self.splitDockWidget(self.docks["band"],
                                 self.docks["tuning"], Qt.Horizontal)
        for dock in self.docks.values():
            dock.setVisible(True)
        # Re-apply the operator's panel-lock preference (the rebuilt
        # QSplitter tree's new handles default to enabled).
        if self.lock_panels_action.isChecked():
            self._apply_panel_lock(True)
        self.statusBar().showMessage(
            "Applied N8SDR layout — use View → 'Save current layout "
            "as my default' to lock it in",
            5000)

    def _restore_user_default_layout(self):
        """View → Restore my saved layout — load whatever the operator
        stashed via 'Save current layout as my default'. Separate from
        _reset_layout so the operator picks intentionally between
        'factory' (Reset Panel Layout) and 'my saved one' (this)."""
        from PySide6.QtWidgets import QMessageBox
        user_state = self._settings.value("user_default_dock_state")
        user_split = self._settings.value("user_default_center_split")
        if not user_state:
            QMessageBox.information(
                self, "No saved layout",
                "You haven't saved a custom default layout yet.\n\n"
                "Arrange your panels how you want them, then "
                "View → Save current layout as my default.")
            return
        try:
            self.restoreState(user_state)
            if user_split:
                self.center_splitter.restoreState(user_split)
            for dock in self.docks.values():
                dock.setVisible(True)
            self.statusBar().showMessage(
                "Restored your saved default layout", 2500)
        except Exception as e:
            QMessageBox.critical(
                self, "Restore failed",
                f"Could not restore your saved layout: {e}\n\n"
                "Try 'Reset Panel Layout' to go back to the factory "
                "arrangement.")

    def _save_current_as_default_layout(self):
        """Capture the current dock state + center splitter widths
        into QSettings under user_default_* keys. Future
        'Restore my saved layout' calls will restore to THIS state.

        Refuses to save a degenerate layout — if any panel is too
        small to read, the layout was almost certainly captured
        during an in-progress drag and persisting it would just
        recreate the bug on the next launch."""
        from PySide6.QtWidgets import QMessageBox
        sane, reason = self._is_layout_state_sane()
        if not sane:
            QMessageBox.warning(
                self, "Won't save degenerate layout",
                f"The current layout looks broken:\n\n  {reason}\n\n"
                "Saving this as your default would just recreate "
                "the problem next launch. Drag the panels back to "
                "readable sizes and try again.")
            return
        self._settings.setValue("user_default_dock_state", self.saveState())
        self._settings.setValue(
            "user_default_center_split",
            self.center_splitter.saveState())
        self._settings.sync()
        QMessageBox.information(
            self, "Default layout saved",
            "Current panel arrangement saved as your default.\n\n"
            "View → 'Restore my saved layout' will return to THIS\n"
            "state any time. (Reset Panel Layout always goes to\n"
            "the factory arrangement, regardless of saved layouts.)")

    def _clear_user_default_layout(self):
        """Forget the user-saved default. 'Restore my saved layout'
        will report "no saved layout" until a new one is saved."""
        from PySide6.QtWidgets import QMessageBox
        self._settings.remove("user_default_dock_state")
        self._settings.remove("user_default_center_split")
        self._settings.sync()
        QMessageBox.information(
            self, "Saved layout discarded",
            "Your saved default layout has been removed.\n\n"
            "View → 'Restore my saved layout' will now report\n"
            "'no saved layout' until you save a new one.")

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
        if s.contains("waterfall_auto_scale"):
            r.set_waterfall_auto_scale(
                s.value("waterfall_auto_scale") in (True, "true", "True", 1, "1"))
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
        if s.contains("visuals/show_lyra_constellation"):
            v = s.value("visuals/show_lyra_constellation")
            # QSettings round-trips bools as the string "true"/"false"
            # on some platforms; normalize.
            if isinstance(v, str):
                v = v.lower() in ("1", "true", "yes")
            r.set_show_lyra_constellation(bool(v))
        if s.contains("visuals/show_lyra_meteors"):
            v = s.value("visuals/show_lyra_meteors")
            if isinstance(v, str):
                v = v.lower() in ("1", "true", "yes")
            r.set_show_lyra_meteors(bool(v))
        # ── Cal-fix migration ──────────────────────────────────────
        # The 0.0.2 release fixed the FFT normalization to be true
        # dBFS — every reading drops by ~34 dB compared to earlier
        # builds. Without migrating saved spectrum/waterfall ranges,
        # operators upgrading would see a flat-line trace at the top
        # of the display ("nothing's showing!") because their saved
        # range still spans -110..-20 (now far above where any real
        # signal lives).
        #
        # Discriminator: in true-dBFS, no real RX signal lives above
        # ~-40 dBFS (a clipping ADC peak is around -3, but persistent
        # signals are much lower). A saved max_db > -45 is therefore
        # the unmistakable signature of the pre-cal-fix scale where
        # operators kept the top of the range near -20 dBFS to show
        # strong signals.
        SPECTRUM_OLD_SCALE_DB_SHIFT = -34.0
        SPECTRUM_OLD_SCALE_HI_THRESHOLD = -45.0

        def _migrate_range(min_key: str, max_key: str):
            """Auto-shift saved range if it looks like pre-cal-fix.
            Idempotent — once migrated, the new max_db is well below
            the threshold so a second pass leaves it untouched."""
            if not (s.contains(min_key) and s.contains(max_key)):
                return None
            try:
                lo = float(s.value(min_key))
                hi = float(s.value(max_key))
            except (TypeError, ValueError):
                return None
            if hi > SPECTRUM_OLD_SCALE_HI_THRESHOLD:
                # Pre-cal-fix saved value — shift both edges down
                # so the visual scale stays aligned with the signal.
                lo += SPECTRUM_OLD_SCALE_DB_SHIFT
                hi += SPECTRUM_OLD_SCALE_DB_SHIFT
                # Persist immediately so we don't re-migrate next launch.
                s.setValue(min_key, lo)
                s.setValue(max_key, hi)
            return (lo, hi)

        try:
            spec_range = _migrate_range(
                "visuals/spectrum_min_db", "visuals/spectrum_max_db")
            if spec_range is not None:
                # Sanity check — a previous accidental Y-axis drag may
                # have persisted a too-narrow range (e.g. 12 dB). On
                # restart that pinches the trace at startup until auto-
                # scale catches up. If the saved span is < 30 dB, treat
                # as accidental and fall back to the wide default so
                # the first few frames show a usable trace.
                _lo, _hi = spec_range
                if _hi - _lo >= 30.0:
                    r.set_spectrum_db_range(_lo, _hi)
            wf_range = _migrate_range(
                "visuals/waterfall_min_db", "visuals/waterfall_max_db")
            if wf_range is not None:
                # Same too-narrow guard the spectrum range gets — a
                # previously saved pinched waterfall range would cause
                # the heatmap to render mostly one solid color until
                # auto-scale catches up. Fall back to defaults if the
                # saved span is < 30 dB.
                _wlo, _whi = wf_range
                if _whi - _wlo >= 30.0:
                    r.set_waterfall_db_range(_wlo, _whi)
            if s.contains("visuals/spectrum_cal_db"):
                r.set_spectrum_cal_db(float(s.value("visuals/spectrum_cal_db")))
            if s.contains("visuals/smeter_cal_db"):
                r.set_smeter_cal_db(float(s.value("visuals/smeter_cal_db")))
            if s.contains("smeter_mode"):
                r.set_smeter_mode(str(s.value("smeter_mode")))
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
        # Re-apply the panel-lock state so the operator's preference
        # carries over a restart. Done AFTER restoreState() so the
        # docks exist and have their default features set first.
        self._apply_panels_lock_from_settings()
        # Re-apply hidden-readout state for the toolbar diagnostic
        # readouts (HL2 / CPU / GPU). If the operator hid one of
        # them — e.g. as part of A/B-testing whether the GPU PDH
        # timer was contributing to paint stutter — that preference
        # carries over restarts.
        self._apply_readout_visibility_from_settings()
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
        s.setValue("waterfall_auto_scale", r.waterfall_auto_scale)
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
        s.setValue("visuals/spectrum_cal_db",   r.spectrum_cal_db)
        s.setValue("visuals/smeter_cal_db",     r.smeter_cal_db)
        s.setValue("smeter_mode",               r.smeter_mode)
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

        # Layout persistence with sanity check — refuse to overwrite
        # the saved dock_state with a degenerate one. This is the
        # important fix: the close-time auto-save was previously
        # unconditional, so if the operator's session ended with the
        # layout in a momentarily-bad state (e.g., mid-drag at the
        # moment of Alt+F4, or any of the "save my broken default"
        # bug-paths from earlier builds), the broken state became
        # PERMANENT — next launch reloaded it and there was no escape
        # short of registry editing.
        #
        # With this guard: if the live layout looks broken right now,
        # we leave the previous session's saved state intact. Result:
        # last-known-good is preserved, operator is never trapped by
        # a single bad close.
        sane, reason = self._is_layout_state_sane()
        if sane:
            s.setValue("dock_state", self.saveState())
            s.setValue("center_split", self.center_splitter.saveState())
        else:
            print(f"Lyra: skipped saving layout (reason: {reason}); "
                  "previous saved layout preserved")
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

    # Application metadata — Windows uses this for taskbar grouping,
    # AppUserModelID, "Open With" registration, and the title shown
    # in some dialog headers. Keep in sync with lyra/__init__.py.
    from lyra import __version__ as _lyra_version
    app.setApplicationName("Lyra")
    app.setApplicationDisplayName("Lyra")
    app.setApplicationVersion(_lyra_version)
    app.setOrganizationName("N8SDR")
    app.setOrganizationDomain("github.com/N8SDR1/Lyra-SDR")

    body = QFont()
    body.setFamilies([theme.FONT_FAMILY, "Segoe UI", "Arial"])
    body.setPointSize(theme.FONT_SIZE)
    app.setFont(body)

    app.setStyleSheet(theme.build_stylesheet())

    # Tooltip font set explicitly via QToolTip.setFont — QSS
    # `font-size` on QToolTip is honored unreliably across Qt
    # platform plugins (especially Windows native tooltips), so we
    # set the QFont directly. This is the authoritative source for
    # tooltip text size; the QSS rule is the fallback.
    from PySide6.QtWidgets import QToolTip
    tooltip_font = QFont()
    tooltip_font.setFamilies([theme.FONT_FAMILY, "Segoe UI", "Arial"])
    tooltip_font.setPointSize(11)
    QToolTip.setFont(tooltip_font)

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
