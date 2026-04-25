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

        # OpenGL upsell — fire shortly after the main window is fully
        # painted so the modal dialog reliably appears on top instead
        # of being parented to a half-shown window (which on Windows
        # can occasionally hide the dialog behind the main one). 600ms
        # is "human-perceptible delay-free" but lets Qt finish window
        # construction + first paint pass.
        from PySide6.QtCore import QTimer as _QTimer
        _QTimer.singleShot(600, self._maybe_show_opengl_nag)

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
        save_layout = QAction("Save current layout as my default", self)
        save_layout.setToolTip(
            "Capture the current arrangement (panel positions, sizes, "
            "splitter widths) and use it as the new default. 'Reset Panel "
            "Layout' below will then restore to THIS state instead of the "
            "factory layout.")
        save_layout.triggered.connect(self._save_current_as_default_layout)
        view_menu.addAction(save_layout)
        reset = QAction("Reset Panel Layout", self)
        reset.triggered.connect(self._reset_layout)
        view_menu.addAction(reset)
        clear_default = QAction("Forget saved layout (revert to factory)", self)
        clear_default.setToolTip(
            "Discard the user-saved default layout. Reset Panel Layout "
            "will once again restore to the factory arrangement.")
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
        # Check for updates — placeholder for the installer-build
        # release pipeline. When the PyInstaller / Inno Setup work
        # lands we'll wire this to query the GitHub Releases API for
        # the latest tag, compare to lyra.__version__, and show a
        # "0.0.3 available — Open release page" dialog if newer.
        # The action is greyed out for now so operators see it exists
        # without it firing on a not-yet-implemented endpoint.
        update_act = QAction("Check for &Updates…  (coming soon)", self)
        update_act.setEnabled(False)
        update_act.setToolTip(
            "Will query GitHub Releases for a newer Lyra version. "
            "Wired up in the installer-build release.")
        help_menu.addAction(update_act)

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

        # ── 5. Reset Panel Layout ──────────────────────────────────
        reset = QAction("Reset Panel Layout", self)
        reset.setToolTip("Restore the default panel arrangement")
        reset.triggered.connect(self._reset_layout)
        tb.addAction(reset)

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
            "Reads NVIDIA GPUs via the NVML library; AMD / Intel /\n"
            "no NVIDIA = 'n/a'. Install nvidia-ml-py for the readout.")
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
        if ACTIVE_BACKEND != BACKEND_SOFTWARE:
            return
        if self._settings.value("ui/opengl_nag_dismissed", False, type=bool):
            return
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

    def _open_telem_probe(self):
        """Help → HL2 Telemetry Probe. Opens the diagnostic dialog
        that captures live C&C bytes and shows per-address summaries
        so we can verify the correct telemetry mapping for this HL2
        firmware without guessing through public docs."""
        from .telem_probe import TelemetryProbeDialog
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

    def _reset_layout(self):
        """Restore the default arrangement.

        Two-tier default:
        - If the operator has saved a personal default via
          'Save current layout as my default', restore THAT.
        - Otherwise restore the factory layout (Tuning + Mode + View
          on top row, Band + Meters split, DSP at bottom).

        This way 'Reset Panel Layout' is "go back to known good"
        regardless of whether the operator's "known good" matches
        the factory or their own customization.
        """
        user_state = self._settings.value("user_default_dock_state")
        user_split = self._settings.value("user_default_center_split")
        if user_state:
            try:
                self.restoreState(user_state)
                if user_split:
                    self.center_splitter.restoreState(user_split)
                for dock in self.docks.values():
                    dock.setVisible(True)
                return
            except Exception:
                # Fall through to factory layout if the saved state
                # is somehow corrupt (binary format change between
                # Qt versions, etc.)
                pass
        # Factory layout — Remove every dock from its current
        # position, then rebuild.
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

    def _save_current_as_default_layout(self):
        """Capture the current dock state + center splitter widths
        into QSettings under user_default_* keys. Future
        'Reset Panel Layout' calls will restore to THIS state."""
        self._settings.setValue("user_default_dock_state", self.saveState())
        self._settings.setValue(
            "user_default_center_split",
            self.center_splitter.saveState())
        self._settings.sync()
        # Toast in the status bar so the operator sees it took effect
        if hasattr(self, "status_dot"):
            # Reuse the streaming dot's transient-message channel via
            # the radio if available, else just a print fallback.
            pass
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "Default layout saved",
            "Current panel arrangement saved as your default.\n\n"
            "'Reset Panel Layout' (View menu / toolbar) will now\n"
            "restore to THIS state instead of the factory layout.")

    def _clear_user_default_layout(self):
        """Forget the user-saved default. Reset Panel Layout falls
        back to the hardcoded factory arrangement."""
        self._settings.remove("user_default_dock_state")
        self._settings.remove("user_default_center_split")
        self._settings.sync()
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "Saved layout discarded",
            "Your saved default layout has been removed.\n\n"
            "'Reset Panel Layout' will now restore to the factory\n"
            "arrangement (Tuning + Mode + View on top, etc).")

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
                r.set_spectrum_db_range(*spec_range)
            wf_range = _migrate_range(
                "visuals/waterfall_min_db", "visuals/waterfall_max_db")
            if wf_range is not None:
                r.set_waterfall_db_range(*wf_range)
            if s.contains("visuals/spectrum_cal_db"):
                r.set_spectrum_cal_db(float(s.value("visuals/spectrum_cal_db")))
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
        s.setValue("visuals/spectrum_cal_db",   r.spectrum_cal_db)
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
