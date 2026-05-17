"""Settings dialog — tabbed, extensible.

Tabs (in order): Radio, Network/TCI, Hardware, DSP, Noise, Audio,
Visuals, Keyer, Bands, Weather.  Each tab is a QWidget that reads
from / writes to the Radio object or a related subsystem.
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
    QButtonGroup, QColorDialog, QComboBox, QFrame, QListWidget,
    QRadioButton, QSizePolicy, QSlider,
)


# ─────────────────────────────────────────────────────────────────────
# Wrapped-label vertical-squeeze guard
# ─────────────────────────────────────────────────────────────────────
#
# Qt bug seen on Settings → Noise / Visuals (2026-05-09 Brent +
# operator):  a QLabel with setWordWrap(True) sitting in a
# QVBoxLayout that is also packed with sliders, sub-groups, and
# action buttons gets vertically SHRUNK below its heightForWidth
# answer when the column runs out of preferred space.  Result:
# wrapped lines render on top of each other instead of forcing the
# parent to be taller (or scrolling).
#
# Calling this helper after setWordWrap(True) flips the vertical
# size policy to MinimumExpanding + sets hasHeightForWidth so the
# layout MUST honor the wrapped-text height.  Cheap; no visible
# downside on labels that already had room.

def _force_wrap_height(label: QLabel) -> None:
    """Force a wrapped QLabel to claim its full heightForWidth.

    Apply right after `label.setWordWrap(True)` — the label will no
    longer be shrunk below its wrapped height by a tight parent
    QVBoxLayout (which produces the visible "lines stacked on top of
    each other" rendering bug).
    """
    sp = label.sizePolicy()
    sp.setVerticalPolicy(QSizePolicy.MinimumExpanding)
    sp.setHeightForWidth(True)
    label.setSizePolicy(sp)

# Shared with the front-panel ViewPanel slider — both UIs map slider
# detents to the same FPS values + (divider, multiplier) tuples.
from lyra.ui.panels import (
    SPECTRUM_FPS_STEPS, WATERFALL_SPEED_STEPS, SteppedSlider,
    fps_to_slider_position, fps_from_slider_position,
    wf_to_slider_position, wf_from_slider_position,
)


# ─────────────────────────────────────────────────────────────────────
# Settings-dialog dead-widget guard
# ─────────────────────────────────────────────────────────────────────
#
# Background: many Settings tabs mirror Radio properties into UI
# widgets via slot lambdas like:
#
#     radio.foo_changed.connect(
#         lambda v: self.foo_widget.setValue(v)
#         if self.foo_widget.value() != v else None)
#
# The "if widget != value" check is feedback-loop avoidance — without
# it, setValue would re-emit and cause oscillation between the
# Settings UI and the Radio.  Functionally correct; lifetime-buggy.
#
# When the operator closes the Settings dialog, Qt destroys the
# dialog and ALL its child widgets at the C++ level (Qt parent-owns-
# child rule).  But the Python wrappers survive — the lambda's
# closure keeps `self` alive, which keeps `self.foo_widget` alive at
# the Python level.  And the `radio.foo_changed.connect(...)` wiring
# is on the long-lived Radio object — it's NEVER disconnected.
#
# Result: any signal fire after dialog close runs the lambda; the
# lambda calls `self.foo_widget.value()`; libshiboken raises
# "Internal C++ object already deleted"; PySide6 prints the
# traceback to stderr and continues.  Brent reported this in
# v0.0.9.6 with five identical wf_auto_scale_chk tracebacks per
# signal fire — proof that 5 stale lambdas had accumulated from
# repeated Settings open/close cycles.
#
# Two helpers below silence the noise + prevent further leak:
#
#   _is_widget_valid(widget)
#       True if the widget's underlying C++ object hasn't been
#       deleted.  Wraps shiboken6.isValid with an ImportError fallback
#       for the (unlikely) case where shiboken6 isn't on the path.
#
#   _safe_mirror(widget, getter, setter, value)
#       The sweep-and-replace target.  Reads getter() / setter()
#       BY NAME via getattr so callers don't have to bind methods
#       to a destroyed widget, validates the widget is alive before
#       touching it, and swallows the RuntimeError that fires if a
#       widget dies between validity check and call.
#
# Sweep history: ten lambda call sites + several mirror-method
# bodies were converted to use _safe_mirror across settings_dialog.py
# in the v0.0.9.6.1 hardening pass (2026-05-08, after Brent's
# operator-visible tracebacks).  When adding new mirror lambdas to
# this file, USE _safe_mirror — don't reintroduce the inline form.

def _is_widget_valid(widget) -> bool:
    """True if widget's underlying C++ object hasn't been deleted.

    Returns False once the QWidget has been destroyed by Qt's
    parent-owns-child cleanup (typically on Settings dialog close).
    Falls back to True if shiboken6 isn't importable — the call
    site's try/except RuntimeError still catches the dead-widget
    case in that scenario.
    """
    try:
        from shiboken6 import isValid
    except ImportError:
        return True
    try:
        return bool(isValid(widget))
    except Exception:
        return False


def _safe_mirror(widget, getter: str, setter: str, value) -> None:
    """Mirror ``value`` into ``widget`` if it differs.

    Args:
        widget: a QWidget (QSpinBox / QCheckBox / QSlider / etc.).
        getter: name of the read method on the widget — e.g.
            ``"value"``, ``"isChecked"``, ``"text"``,
            ``"currentIndex"``.
        setter: name of the write method on the widget — e.g.
            ``"setValue"``, ``"setChecked"``, ``"setText"``,
            ``"setCurrentIndex"``.
        value: the desired value.

    Behavior:
        * No-op if the widget's C++ object has been destroyed.
        * Reads ``widget.<getter>()`` and only calls
          ``widget.<setter>(value)`` if it differs (preserves the
          feedback-loop avoidance the inline pattern provided).
        * Swallows ``RuntimeError`` from libshiboken (race between
          isValid check and the actual method call).

    Replaces the inline pattern:
        ``self.X.<setter>(v) if self.X.<getter>() != v else None``
    """
    if not _is_widget_valid(widget):
        return
    try:
        getter_fn = getattr(widget, getter, None)
        setter_fn = getattr(widget, setter, None)
        if getter_fn is None or setter_fn is None:
            return
        if getter_fn() != value:
            setter_fn(value)
    except RuntimeError:
        # Race: widget became invalid between check and call.
        # Silent — operator's console stays clean.
        pass


def _swallow_dead_widget(fn):
    """Decorator for Settings-tab slot methods that touch widgets.

    Wraps the method body so a libshiboken "Internal C++ object
    already deleted" error (raised when the slot is invoked AFTER
    the Settings dialog has been closed and Qt has destroyed the
    widget) is swallowed silently rather than spamming the
    operator's console with multi-line tracebacks.

    Use on any ``_on_*`` slot method that's connected to a Radio
    signal or other long-lived emitter — those are the methods at
    risk of firing after the tab is gone.  Methods that only run
    while the dialog is alive (e.g. invoked from inside the same
    tab) don't need this.

    Re-raises any RuntimeError that doesn't look like the dead-
    widget marker, so genuine logic bugs still surface.
    """
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as exc:
            msg = str(exc)
            if ("already deleted" in msg
                    or "Internal C++ object" in msg
                    or "wrapped C/C++ object" in msg):
                return None
            raise
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", "")
    wrapper.__wrapped__ = fn
    return wrapper


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
from lyra.ptt import TrSequencing
from lyra.ui.toggle import ToggleSwitch
from lyra.ui.widgets.stepper_readout import StepperReadout


class TciSettingsTab(QWidget):
    """Network / TCI settings — parity with reference SDR clients Network tab."""

    def __init__(self, server: TciServer, radio=None, parent=None):
        super().__init__(parent)
        self.server = server
        # `radio` is optional so the tab still constructs standalone (tests,
        # preview). Spot / CW controls only appear when radio is present.
        self.radio = radio

        # v0.0.9.1 layout rewrite — three side-by-side columns to fit
        # all the TCI server / streaming / spot controls without a
        # multi-screen-tall stack.  Operator-requested layout based
        # on the canonical TCI Settings panel layout, minus items
        # that depend on RX2 (v0.1) or TX (v0.2) which stay parked
        # in the placeholder groups below.

        v = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        v.addLayout(top_row)

        # ╔══ Column 1: Server core ═════════════════════════════════╗
        srv_grp = QGroupBox("TCI Server")
        srv = QGridLayout(srv_grp)
        srv.setColumnStretch(2, 1)
        srv_row = 0

        srv.addWidget(QLabel("Bind IP:Port"), srv_row, 0)
        self.bind_edit = QLineEdit(f"{server.bind_host}:{server.port}")
        self.bind_edit.setFixedWidth(150)
        srv.addWidget(self.bind_edit, srv_row, 1)
        default_btn = QPushButton("Def")
        default_btn.setFixedWidth(40)
        default_btn.clicked.connect(self._reset_bind_default)
        srv.addWidget(default_btn, srv_row, 2, Qt.AlignLeft)
        srv_row += 1

        srv.addWidget(QLabel("Rate Limit (ms)"), srv_row, 0)
        self.rate_spin = QSpinBox()
        # Per TCI convention: minimum interval between same-key
        # broadcast messages, in milliseconds.  Internal storage is
        # rate_limit_hz (msg/sec), converted on read/write.
        self.rate_spin.setRange(1, 1000)
        self.rate_spin.setValue(int(1000 / max(server.rate_limit_hz, 1)))
        self.rate_spin.setFixedWidth(80)
        self.rate_spin.setSuffix(" ms")
        self.rate_spin.valueChanged.connect(
            lambda ms: setattr(self.server, "rate_limit_hz",
                               max(1, int(1000 / max(int(ms), 1)))))
        srv.addWidget(self.rate_spin, srv_row, 1)
        srv_row += 1

        self.init_state_chk = QCheckBox(
            "Send initial state on client connect")
        self.init_state_chk.setChecked(server.send_initial_state_on_connect)
        self.init_state_chk.toggled.connect(
            lambda v: setattr(self.server,
                              "send_initial_state_on_connect", v))
        srv.addWidget(self.init_state_chk, srv_row, 0, 1, 3)
        srv_row += 1

        # Mode-name mapping options — TCI's modulation enum is
        # historically CWU/CWL-blind (one "CW" mode), but newer
        # clients accept CWU/CWL verbatim.  These two flags let
        # operators tune for the client mix they have.
        self.mode_cwlcwu_out_chk = QCheckBox(
            "CWL/CWU becomes CW (outbound)")
        self.mode_cwlcwu_out_chk.setChecked(server.cwlcwu_becomes_cw_out)
        self.mode_cwlcwu_out_chk.toggled.connect(
            lambda v: setattr(self.server, "cwlcwu_becomes_cw_out", v))
        srv.addWidget(self.mode_cwlcwu_out_chk, srv_row, 0, 1, 3)
        srv_row += 1

        self.mode_cw_to_cwu_chk = QCheckBox(
            "CW becomes CWU above 10 MHz (inbound)")
        self.mode_cw_to_cwu_chk.setChecked(
            server.cw_becomes_cwu_above_10mhz_in)
        self.mode_cw_to_cwu_chk.toggled.connect(
            lambda v: setattr(self.server,
                              "cw_becomes_cwu_above_10mhz_in", v))
        srv.addWidget(self.mode_cw_to_cwu_chk, srv_row, 0, 1, 3)
        srv_row += 1

        self.emulate_expertsdr3_chk = QCheckBox(
            "Emulate ExpertSDR3 protocol")
        self.emulate_expertsdr3_chk.setToolTip(
            "Some legacy TCI clients only recognize the\n"
            "ExpertSDR3 protocol/device strings on connect.\n"
            "Enable this to spoof those strings if your client\n"
            "refuses to talk to Lyra.")
        self.emulate_expertsdr3_chk.setChecked(server.emulate_expertsdr3)
        self.emulate_expertsdr3_chk.toggled.connect(
            lambda v: setattr(self.server, "emulate_expertsdr3", v))
        srv.addWidget(self.emulate_expertsdr3_chk, srv_row, 0, 1, 3)
        srv_row += 1

        self.log_chk = QCheckBox("Log TCI traffic to console / viewer")
        self.log_chk.setChecked(server.log_traffic)
        self.log_chk.toggled.connect(
            lambda v: setattr(self.server, "log_traffic", v))
        srv.addWidget(self.log_chk, srv_row, 0, 1, 3)
        srv_row += 1

        self.enable_chk = QCheckBox("TCI Server Running")
        self.enable_chk.setChecked(server.is_running)
        self.enable_chk.toggled.connect(self._on_enable)
        srv.addWidget(self.enable_chk, srv_row, 0, 1, 2)
        self.log_btn = QPushButton("Show Log...")
        self.log_btn.setFixedWidth(95)
        self.log_btn.clicked.connect(self._show_log)
        srv.addWidget(self.log_btn, srv_row, 2, Qt.AlignLeft)
        srv_row += 1

        # Status
        self.status_label = QLabel()
        self.status_label.setStyleSheet(
            "color: #8a9aac; font-style: italic;")
        srv.addWidget(self.status_label, srv_row, 0, 1, 3)
        self._update_status()

        server.running_changed.connect(lambda _: self._update_status())
        server.client_count_changed.connect(lambda _: self._update_status())

        top_row.addWidget(srv_grp, 1)

        # ╔══ Column 2: Audio + IQ Streaming ════════════════════════╗
        stream_grp = QGroupBox("Audio + IQ Streaming")
        stm = QGridLayout(stream_grp)
        stm.setColumnStretch(0, 1)
        stm_row = 0

        self.allow_audio_chk = QCheckBox("Allow RX audio over TCI")
        self.allow_audio_chk.setToolTip(
            "Master enable for the TCI RX audio stream.\n"
            "When off, TCI clients receive no audio even if\n"
            "they send AUDIO_START.  Disable for CPU / safety\n"
            "reasons; default ON for normal use.")
        self.allow_audio_chk.setChecked(server.allow_audio_streaming)
        self.allow_audio_chk.toggled.connect(
            lambda v: setattr(self.server, "allow_audio_streaming", v))
        stm.addWidget(self.allow_audio_chk, stm_row, 0)
        stm_row += 1

        self.allow_iq_chk = QCheckBox("Allow IQ over TCI")
        self.allow_iq_chk.setToolTip(
            "Master enable for the TCI IQ stream.\n"
            "Used by panorama / spectrum-analyzer clients\n"
            "(SDRLogger+, etc.).  Default ON.")
        self.allow_iq_chk.setChecked(server.allow_iq_streaming)
        self.allow_iq_chk.toggled.connect(
            lambda v: setattr(self.server, "allow_iq_streaming", v))
        stm.addWidget(self.allow_iq_chk, stm_row, 0)
        stm_row += 1

        self.always_audio_chk = QCheckBox(
            "Always stream audio (don't wait for AUDIO_START)")
        self.always_audio_chk.setChecked(server.always_stream_audio)
        self.always_audio_chk.toggled.connect(
            lambda v: setattr(self.server, "always_stream_audio", v))
        stm.addWidget(self.always_audio_chk, stm_row, 0)
        stm_row += 1

        self.always_iq_chk = QCheckBox(
            "Always stream IQ (don't wait for IQ_START)")
        self.always_iq_chk.setChecked(server.always_stream_iq)
        self.always_iq_chk.toggled.connect(
            lambda v: setattr(self.server, "always_stream_iq", v))
        stm.addWidget(self.always_iq_chk, stm_row, 0)
        stm_row += 1

        self.swap_iq_chk = QCheckBox("Swap IQ on stream (Q,I instead of I,Q)")
        self.swap_iq_chk.setChecked(server.swap_iq_on_stream)
        self.swap_iq_chk.toggled.connect(
            lambda v: setattr(self.server, "swap_iq_on_stream", v))
        stm.addWidget(self.swap_iq_chk, stm_row, 0)
        stm_row += 1

        # Currently-streaming clients display: shows the operator
        # which TCI clients are subscribed to which streams, with
        # the per-client config (sample rate / format / channels).
        # Read-only; refreshed on a 1 Hz timer.
        stm.addWidget(QLabel("Currently streaming:"), stm_row, 0)
        stm_row += 1
        self.streaming_list = QListWidget()
        self.streaming_list.setMaximumHeight(140)
        self.streaming_list.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 10pt;")
        stm.addWidget(self.streaming_list, stm_row, 0)
        stm_row += 1
        self._streaming_timer = QTimer(self)
        self._streaming_timer.setInterval(1000)
        self._streaming_timer.timeout.connect(self._refresh_streaming_list)
        self._streaming_timer.start()
        self._refresh_streaming_list()

        top_row.addWidget(stream_grp, 1)

        # ╔══ Column 3: Spots ═══════════════════════════════════════╗
        spots = QGroupBox("TCI Spots")
        sl = QGridLayout(spots)
        sl.setColumnStretch(1, 1)
        sp_row = 0

        sl.addWidget(QLabel("Max spots"), sp_row, 0)
        self.max_spots_spin = QSpinBox()
        self.max_spots_spin.setRange(0, 100)
        self.max_spots_spin.setSingleStep(5)
        self.max_spots_spin.setFixedWidth(80)
        self.max_spots_spin.setToolTip(
            "Maximum spots kept in memory (0–100). "
            "20–30 is a sensible default for HF.")
        if self.radio is not None:
            self.max_spots_spin.setValue(self.radio.max_spots)
            self.max_spots_spin.valueChanged.connect(
                lambda v: self.radio.set_max_spots(v))
        sl.addWidget(self.max_spots_spin, sp_row, 1, Qt.AlignLeft)
        sp_row += 1

        sl.addWidget(QLabel("Lifetime"), sp_row, 0)
        self.lifetime_spin = QSpinBox()
        self.lifetime_spin.setRange(0, 86400)
        self.lifetime_spin.setSingleStep(60)
        self.lifetime_spin.setFixedWidth(90)
        self.lifetime_spin.setSuffix(" s")
        self.lifetime_spin.setToolTip(
            "Seconds after which a spot is considered stale.\n"
            "0 = never expire.")
        if self.radio is not None:
            self.lifetime_spin.setValue(self.radio.spot_lifetime_s)
            self.lifetime_spin.valueChanged.connect(
                lambda v: self.radio.set_spot_lifetime_s(v))
        sl.addWidget(self.lifetime_spin, sp_row, 1, Qt.AlignLeft)
        sp_row += 1

        # Lifetime quick presets
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        for label, seconds in (("5m", 300), ("10m", 600),
                               ("15m", 900), ("30m", 1800)):
            b = QPushButton(label)
            b.setFixedWidth(40)
            b.clicked.connect(
                lambda _=False, s=seconds: self.lifetime_spin.setValue(s))
            preset_row.addWidget(b)
        preset_row.addStretch(1)
        preset_wrap = QWidget()
        preset_wrap.setLayout(preset_row)
        sl.addWidget(preset_wrap, sp_row, 0, 1, 2)
        sp_row += 1

        sl.addWidget(QLabel("Mode filter"), sp_row, 0)
        self.mode_filter_edit = QLineEdit()
        self.mode_filter_edit.setPlaceholderText("FT8,CW,SSB")
        self.mode_filter_edit.setToolTip(
            "Comma-separated modes to render on the panadapter.\n"
            "Empty = show all.  'SSB' auto-includes USB+LSB.")
        if self.radio is not None:
            self.mode_filter_edit.setText(self.radio.spot_mode_filter_csv)
            self.mode_filter_edit.editingFinished.connect(
                lambda: self.radio.set_spot_mode_filter_csv(
                    self.mode_filter_edit.text()))
        sl.addWidget(self.mode_filter_edit, sp_row, 1)
        sp_row += 1

        self.flash_spots_chk = QCheckBox("Flash new spots")
        self.flash_spots_chk.setChecked(server.flash_new_spots)
        self.flash_spots_chk.toggled.connect(
            lambda v: setattr(self.server, "flash_new_spots", v))
        sl.addWidget(self.flash_spots_chk, sp_row, 0)
        # Color picker for flash color (small swatch button)
        self._flash_color_btn = QPushButton("    ")
        self._flash_color_btn.setFixedWidth(40)
        self._update_color_button(self._flash_color_btn,
                                  server.flash_spot_color)
        self._flash_color_btn.clicked.connect(self._pick_flash_color)
        sl.addWidget(self._flash_color_btn, sp_row, 1, Qt.AlignLeft)
        sp_row += 1

        self.flags_chk = QCheckBox("Show country flags on spots")
        self.flags_chk.setChecked(server.show_country_flags)
        self.flags_chk.toggled.connect(
            lambda v: setattr(self.server, "show_country_flags", v))
        sl.addWidget(self.flags_chk, sp_row, 0, 1, 2)
        sp_row += 1

        # Own callsign + own-spot color
        sl.addWidget(QLabel("Own callsign"), sp_row, 0)
        self.callsign_edit = QLineEdit(server.own_callsign)
        self.callsign_edit.setFixedWidth(100)
        self.callsign_edit.setPlaceholderText("(for spots)")
        self.callsign_edit.editingFinished.connect(
            lambda: setattr(self.server, "own_callsign",
                            self.callsign_edit.text().strip().upper()))
        sl.addWidget(self.callsign_edit, sp_row, 1, Qt.AlignLeft)
        sp_row += 1

        sl.addWidget(QLabel("Own-call color"), sp_row, 0)
        self._own_color_btn = QPushButton("    ")
        self._own_color_btn.setFixedWidth(40)
        self._update_color_button(self._own_color_btn,
                                  server.own_call_color)
        self._own_color_btn.clicked.connect(self._pick_own_color)
        sl.addWidget(self._own_color_btn, sp_row, 1, Qt.AlignLeft)
        sp_row += 1

        # CW Spot sideband forcing
        sl.addWidget(QLabel("CW spot sideband"), sp_row, 0)
        sp_row += 1
        self._cw_sb_group = QButtonGroup(self)
        cw_sb_row = QHBoxLayout()
        for label, key in (("Default", "default"),
                           ("Force CWU", "cwu"),
                           ("Force CWL", "cwl")):
            rb = QRadioButton(label)
            rb.setChecked(server.cw_spot_sideband_force == key)
            rb.toggled.connect(
                lambda checked, k=key:
                checked and setattr(self.server,
                                    "cw_spot_sideband_force", k))
            self._cw_sb_group.addButton(rb)
            cw_sb_row.addWidget(rb)
        cw_sb_row.addStretch(1)
        cw_sb_wrap = QWidget()
        cw_sb_wrap.setLayout(cw_sb_row)
        sl.addWidget(cw_sb_wrap, sp_row, 0, 1, 2)
        sp_row += 1

        # Master clear button + spot count
        clear_btn = QPushButton("Clear All Spots")
        clear_btn.setFixedWidth(140)
        if self.radio is not None:
            clear_btn.clicked.connect(self.radio.clear_spots)
        sl.addWidget(clear_btn, sp_row, 0)

        self.spot_count_lbl = QLabel()
        self.spot_count_lbl.setStyleSheet(
            "color: #8a9aac; font-style: italic;")
        sl.addWidget(self.spot_count_lbl, sp_row, 1, Qt.AlignLeft)
        if self.radio is not None:
            self._update_spot_count()
            self.radio.spots_changed.connect(
                lambda _: self._update_spot_count())
        sp_row += 1

        top_row.addWidget(spots, 1)

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

    def _update_color_button(self, btn: QPushButton, argb: int) -> None:
        """Paint the small color-picker swatch button with the given
        ARGB color (alpha discarded -- buttons are opaque squares)."""
        r = (argb >> 16) & 0xFF
        g = (argb >> 8) & 0xFF
        b = argb & 0xFF
        btn.setStyleSheet(
            f"background-color: rgb({r}, {g}, {b}); "
            "border: 1px solid #555;")

    def _pick_flash_color(self) -> None:
        """QColorDialog launcher for the flash-new-spots color."""
        argb = self.server.flash_spot_color
        col = QColor((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF)
        new = QColorDialog.getColor(col, self, "Flash spot color")
        if new.isValid():
            new_argb = (0xFF000000 | (new.red() << 16)
                        | (new.green() << 8) | new.blue())
            self.server.flash_spot_color = new_argb
            self._update_color_button(self._flash_color_btn, new_argb)

    def _pick_own_color(self) -> None:
        """QColorDialog launcher for the own-callsign spot color."""
        argb = self.server.own_call_color
        col = QColor((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF)
        new = QColorDialog.getColor(col, self, "Own callsign color")
        if new.isValid():
            new_argb = (0xFF000000 | (new.red() << 16)
                        | (new.green() << 8) | new.blue())
            self.server.own_call_color = new_argb
            self._update_color_button(self._own_color_btn, new_argb)

    def _refresh_streaming_list(self) -> None:
        """1 Hz refresh of the currently-streaming clients display.
        Reads TciServer.streaming_clients_summary() for the per-
        client config (audio + IQ subscription, format, channels)."""
        try:
            clients = self.server.streaming_clients_summary()
        except Exception:
            clients = []
        # Preserve current selection if any (rebuild-by-clear is the
        # easy approach for a list this small; selection rarely
        # matters for a read-only diagnostic).
        self.streaming_list.clear()
        if not clients:
            self.streaming_list.addItem(
                "(no TCI clients currently streaming)")
            return
        for c in clients:
            audio = (
                f"audio {c['audio_sample_rate']//1000}k "
                f"{c['audio_format']} "
                f"{'mono' if c['audio_channels'] == 1 else 'stereo'}"
                if c['audio_enabled'] else "")
            iq = (
                f"IQ {c['iq_sample_rate']//1000}k "
                f"{c['iq_format']}"
                if c['iq_enabled'] else "")
            streams = " · ".join(s for s in (audio, iq) if s)
            if not streams:
                streams = "(connected, no streams active)"
            self.streaming_list.addItem(f"{c['address']}    {streams}")

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
        """Update the TCI server status label.

        Wrapped in try/except RuntimeError to survive the same
        dialog-teardown race that bit ``_refresh_agc_action_label``
        in v0.0.9.4 — the TCI ``server`` object lives on Radio (long-
        lived), but the lambdas connected on lines ~189-190 reach
        into the dialog's ``self.status_label`` which gets destroyed
        when the operator closes Settings.  Operator-reported in
        v0.0.9.5 console log:

            RuntimeError: libshiboken: Internal C++ object
            (PySide6.QtWidgets.QLabel) already deleted.

        Defensive guard catches the rare race in either path; the
        connected lambdas don't need to change shape.
        """
        try:
            if self.server.is_running:
                self.status_label.setText(
                    f"● listening on {self.server.bind_host}:"
                    f"{self.server.port}  "
                    f"— {self.server.client_count} client"
                    f"{'s' if self.server.client_count != 1 else ''}")
            else:
                self.status_label.setText("○ stopped")
        except RuntimeError:
            # status_label widget destroyed (dialog teardown).
            # Server signals stay connected to long-lived Radio
            # state; let Qt clean up the connection on next gc.
            pass

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

        # ── Operator / Station ─────────────────────────────────────
        # Global station identification — callsign + grid square +
        # manual lat/lon backup.  Consumed by TCI spots, WX-Alerts,
        # and any future logging features.  Grid takes precedence
        # over manual lat/lon when valid.
        grp_op = QGroupBox("Operator / Station")
        gop = QGridLayout(grp_op)
        gop.setColumnStretch(1, 1)

        gop.addWidget(QLabel("Callsign"), 0, 0)
        self.callsign_edit = QLineEdit(radio.callsign)
        self.callsign_edit.setFixedWidth(140)
        self.callsign_edit.setPlaceholderText("e.g. N8SDR")
        self.callsign_edit.setToolTip(
            "Your amateur radio callsign.  Auto-uppercases.\n"
            "Used by TCI spots, WX-Alerts, and any feature that\n"
            "needs to identify your station.")
        self.callsign_edit.editingFinished.connect(
            lambda: self.radio.set_callsign(self.callsign_edit.text()))
        gop.addWidget(self.callsign_edit, 0, 1)

        gop.addWidget(QLabel("Grid Square"), 1, 0)
        self.grid_edit = QLineEdit(radio.grid_square)
        self.grid_edit.setFixedWidth(140)
        self.grid_edit.setPlaceholderText("e.g. EM89 or EM89ux")
        self.grid_edit.setToolTip(
            "Maidenhead Grid Locator (4, 6, or 8 chars).\n"
            "Auto-uppercases.  When valid, the lat/lon below are\n"
            "computed from the grid and the manual fields are\n"
            "ignored.  Leave blank to use the manual lat/lon\n"
            "override instead.")
        self.grid_edit.editingFinished.connect(
            self._on_grid_changed)
        gop.addWidget(self.grid_edit, 1, 1)

        # Live readout of computed lat/lon (read-only) —
        # immediately reflects whatever's effective right now.
        self.computed_loc_label = QLabel("")
        self.computed_loc_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-size: 11px;")
        gop.addWidget(QLabel("Computed Lat/Lon"), 2, 0)
        gop.addWidget(self.computed_loc_label, 2, 1)

        # Manual lat/lon backup — used when grid is blank/invalid.
        gop.addWidget(QLabel("Manual Lat (°)"), 3, 0)
        self.lat_edit = QLineEdit("")
        self.lat_edit.setFixedWidth(140)
        self.lat_edit.setPlaceholderText("e.g. 40.7128 (NYC)")
        self.lat_edit.setToolTip(
            "Backup latitude in decimal degrees, -90..+90.\n"
            "Used only when no valid grid square is set.")
        self.lat_edit.editingFinished.connect(self._on_manual_loc_changed)
        if radio.operator_lat_manual is not None:
            self.lat_edit.setText(f"{radio.operator_lat_manual:.4f}")
        gop.addWidget(self.lat_edit, 3, 1)

        gop.addWidget(QLabel("Manual Lon (°)"), 4, 0)
        self.lon_edit = QLineEdit("")
        self.lon_edit.setFixedWidth(140)
        self.lon_edit.setPlaceholderText("e.g. -74.0060 (NYC)")
        self.lon_edit.setToolTip(
            "Backup longitude in decimal degrees, -180..+180.\n"
            "Used only when no valid grid square is set.")
        self.lon_edit.editingFinished.connect(self._on_manual_loc_changed)
        if radio.operator_lon_manual is not None:
            self.lon_edit.setText(f"{radio.operator_lon_manual:.4f}")
        gop.addWidget(self.lon_edit, 4, 1)

        v.addWidget(grp_op)
        # Initial computed-readout refresh.
        self._refresh_computed_loc()
        # React to external changes (e.g., another tab edits these).
        radio.callsign_changed.connect(self._on_callsign_signal)
        radio.grid_square_changed.connect(self._on_grid_signal)
        radio.operator_location_changed.connect(
            lambda _lat, _lon: self._refresh_computed_loc())

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
            "Click-to-tune jumps to the freq + suggested mode.")
        self.bp_marks_chk.toggled.connect(
            self.radio.set_band_plan_show_landmarks)
        gbp.addWidget(self.bp_marks_chk, 2, 0, 1, 3)

        self.bp_ncdxf_chk = QCheckBox(
            "Show NCDXF beacon markers (cyan triangles, 5 fixed freqs)")
        self.bp_ncdxf_chk.setChecked(radio.band_plan_show_ncdxf)
        self.bp_ncdxf_chk.setToolTip(
            "Cyan triangles at the 5 NCDXF International Beacon Project\n"
            "frequencies (14.100 / 18.110 / 21.150 / 24.930 / 28.200 MHz).\n"
            "Hover one to see which of the 18 worldwide stations is\n"
            "transmitting on that band right now.  Independent from\n"
            "the digimode landmark toggle above.")
        self.bp_ncdxf_chk.toggled.connect(
            self.radio.set_band_plan_show_ncdxf)
        gbp.addWidget(self.bp_ncdxf_chk, 3, 0, 1, 3)

        self.bp_edge_chk = QCheckBox(
            "Show band-edge warnings + out-of-band toast")
        self.bp_edge_chk.setChecked(radio.band_plan_edge_warn)
        self.bp_edge_chk.setToolTip(
            "Vertical dashed-red line at band edges + a status-bar "
            "toast when you tune into or out of an allocated band.")
        self.bp_edge_chk.toggled.connect(
            self.radio.set_band_plan_edge_warn)
        gbp.addWidget(self.bp_edge_chk, 4, 0, 1, 3)

        v.addWidget(grp_bp)

        # ── Toolbar readouts (cosmetic) ──────────────────────────
        # CPU% toggle.  Defaults to hidden — most operators don't
        # want a load percentage on the toolbar all the time.
        # Re-enable here if you do want it.
        from PySide6.QtCore import QSettings as _QSettings
        grp_tb = QGroupBox("Toolbar & diagnostic readouts")
        gtb = QVBoxLayout(grp_tb)
        s = _QSettings("N8SDR", "Lyra")
        cpu_hidden = bool(s.value(
            "toolbar/readout_hidden_cpu", True, type=bool))
        self.show_cpu_chk = QCheckBox("Show CPU% on toolbar")
        self.show_cpu_chk.setChecked(not cpu_hidden)
        self.show_cpu_chk.setToolTip(
            "Show or hide the live CPU usage percentage on the\n"
            "toolbar.  Hidden by default — re-enable here if you\n"
            "want it visible (e.g. for diagnosing whether DSP load\n"
            "is bottlenecking the audio chain).")
        self.show_cpu_chk.toggled.connect(self._on_show_cpu_toggled)
        gtb.addWidget(self.show_cpu_chk)

        # HL2 hardware telemetry toggle.  Default visible — voltage
        # sag under key-down and AD9866 temperature rise on long
        # transmits are real TX diagnostic signals operators want to
        # be able to glance at without digging through menus.  Hide
        # here (or via right-click on the label) if you'd rather a
        # cleaner toolbar.
        hl2_hidden = bool(s.value(
            "toolbar/readout_hidden_hl2", False, type=bool))
        self.show_hl2_chk = QCheckBox("Show HL2 telemetry on toolbar")
        self.show_hl2_chk.setChecked(not hl2_hidden)
        self.show_hl2_chk.setToolTip(
            "Show or hide the HL2 hardware telemetry chip on the\n"
            "toolbar (AD9866 temperature + 12 V supply rail).\n\n"
            "Useful during TX to spot voltage sag (weak PSU / long\n"
            "thin power lead) and temperature rise on extended\n"
            "transmits.  Visible by default; hide here if you'd\n"
            "rather a cleaner toolbar.")
        self.show_hl2_chk.toggled.connect(self._on_show_hl2_toggled)
        gtb.addWidget(self.show_hl2_chk)

        # Phase 4 / v0.1.0 (2026-05-13): diagnostic overlay 3-state
        # toggle (CLAUDE.md §15.11).  Operator-driven UX polish so
        # the main window can be cleaned up for routine operating
        # without losing diagnostics during bench work.
        from PySide6.QtWidgets import QHBoxLayout as _QHBoxLayout
        diag_row = _QHBoxLayout()
        diag_label = QLabel("Diagnostic overlays:")
        diag_row.addWidget(diag_label)
        self.diag_overlay_combo = QComboBox()
        self.diag_overlay_combo.addItem("Full", userData="full")
        self.diag_overlay_combo.addItem("Minimal", userData="minimal")
        self.diag_overlay_combo.addItem("Off", userData="off")
        self.diag_overlay_combo.setToolTip(
            "Controls ADC pk/rms, stream status, and audio "
            "telemetry overlays on the main window.\n\n"
            "Full -- everything shown (current default).\n"
            "Minimal -- ADC pk/rms stays visible (useful for "
            "setting LNA without clipping); stream + audio "
            "telemetry hidden.\n"
            "Off -- all three hidden.  Cleanest main window for "
            "operating / screenshots / video.\n\n"
            "The underlying signals keep firing in every mode -- "
            "hiding is for the eyes, not for CPU savings (the "
            "cost is rounding-error)."
        )
        # Restore persisted value
        current_mode = str(s.value(
            "telemetry/overlay_mode", "full"))
        for i in range(self.diag_overlay_combo.count()):
            if self.diag_overlay_combo.itemData(i) == current_mode:
                self.diag_overlay_combo.setCurrentIndex(i)
                break
        self.diag_overlay_combo.setMinimumWidth(120)
        self.diag_overlay_combo.currentIndexChanged.connect(
            self._on_diag_overlay_changed)
        diag_row.addWidget(self.diag_overlay_combo)
        diag_row.addStretch(1)
        gtb.addLayout(diag_row)

        diag_help = QLabel(
            "Hides ADC pk/rms, stream status, and audio telemetry "
            "readouts on the main window."
        )
        diag_help.setWordWrap(True)
        diag_help.setStyleSheet("color: #8a9aac; font-size: 10px;")
        gtb.addWidget(diag_help)

        v.addWidget(grp_tb)

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

    @_swallow_dead_widget
    def _on_ip_changed(self, ip: str):
        if self.ip_edit.text() != ip:
            self.ip_edit.setText(ip)

    # ── Operator / Station handlers ──────────────────────────────────

    def _on_grid_changed(self) -> None:
        """Operator typed a new grid square.  Push to Radio (which
        validates, normalizes, and emits change signals); the
        readout below will refresh via the signal."""
        self.radio.set_grid_square(self.grid_edit.text())
        # If radio rejected the input, reflect the cleared state.
        if self.grid_edit.text().strip().upper() != self.radio.grid_square:
            self.grid_edit.setText(self.radio.grid_square)

    def _on_manual_loc_changed(self) -> None:
        """Operator edited the manual lat/lon override.  Push to
        Radio.  Empty fields clear the override."""
        lat_text = self.lat_edit.text().strip()
        lon_text = self.lon_edit.text().strip()
        try:
            lat = float(lat_text) if lat_text else None
        except ValueError:
            lat = None
            self.lat_edit.setText("")
        try:
            lon = float(lon_text) if lon_text else None
        except ValueError:
            lon = None
            self.lon_edit.setText("")
        # Clamp to valid Earth ranges.
        if lat is not None:
            lat = max(-90.0, min(90.0, lat))
        if lon is not None:
            lon = max(-180.0, min(180.0, lon))
        self.radio.set_operator_lat_lon(lat, lon)
        self._refresh_computed_loc()

    @_swallow_dead_widget
    def _on_callsign_signal(self, cs: str) -> None:
        """External callsign change (e.g. from REPL or another tab)
        — mirror into the line edit without re-firing."""
        if self.callsign_edit.text() != cs:
            self.callsign_edit.blockSignals(True)
            self.callsign_edit.setText(cs)
            self.callsign_edit.blockSignals(False)

    @_swallow_dead_widget
    def _on_grid_signal(self, grid: str) -> None:
        """External grid change — mirror into the edit + refresh
        the computed-lat/lon readout."""
        if self.grid_edit.text() != grid:
            self.grid_edit.blockSignals(True)
            self.grid_edit.setText(grid)
            self.grid_edit.blockSignals(False)
        self._refresh_computed_loc()

    def _refresh_computed_loc(self) -> None:
        """Update the read-only computed-lat/lon readout to reflect
        whatever's effective right now (grid-derived or manual
        override).  Also signals the operator visually whether
        their grid is being used vs the manual fallback."""
        lat = self.radio.operator_lat
        lon = self.radio.operator_lon
        if lat is None or lon is None:
            self.computed_loc_label.setText("(not set)")
            self.computed_loc_label.setStyleSheet(
                "color: #8a9aac; font-family: Consolas, monospace; "
                "font-size: 11px; font-style: italic;")
            return
        # Indicate whether the grid is the source.
        if self.radio.grid_square:
            src = f"from grid {self.radio.grid_square}"
        else:
            src = "manual override"
        self.computed_loc_label.setText(
            f"{lat:+.4f}°, {lon:+.4f}°   ({src})")
        self.computed_loc_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-size: 11px;")

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

    @_swallow_dead_widget
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

    def _find_main_window(self):
        """Locate Lyra's MainWindow for live-apply of toolbar /
        diagnostic-overlay toggles.

        The earlier ``self.window().parent()`` walk worked for the
        Settings dialog itself but the QTabWidget reparents each
        tab into its own QStackedWidget — and the parent of THAT
        is the QTabWidget, not the dialog.  The chain is reliable
        as long as we walk ``parentWidget()`` until we hit a
        ``QMainWindow``; failing that we sweep QApplication's
        top-level widgets for one.  Belt + suspenders so a single
        Qt internal-layout reshuffle in some future Qt release
        doesn't silently break the live-apply path again.
        """
        from PySide6.QtWidgets import QApplication, QMainWindow
        w = self.parentWidget()
        while w is not None:
            if isinstance(w, QMainWindow):
                return w
            w = w.parentWidget()
        app = QApplication.instance()
        if app is not None:
            for tlw in app.topLevelWidgets():
                if isinstance(tlw, QMainWindow):
                    return tlw
        return None

    def _on_show_cpu_toggled(self, checked: bool) -> None:
        """Toggle the toolbar CPU% readout's visibility.

        Writes to QSettings so the choice persists across restarts,
        and tells MainWindow to apply the change immediately so the
        operator doesn't have to relaunch.
        """
        from PySide6.QtCore import QSettings
        s = QSettings("N8SDR", "Lyra")
        s.setValue("toolbar/readout_hidden_cpu", not checked)
        try:
            mw = self._find_main_window()
            if mw is not None and hasattr(mw, "_set_readout_visible"):
                mw._set_readout_visible("cpu", checked)
        except Exception as exc:
            # Live-apply failed — the QSettings write will pick up
            # on next Lyra start.
            print(f"[settings] could not apply CPU visibility "
                  f"live: {exc}")

    def _on_show_hl2_toggled(self, checked: bool) -> None:
        """Toggle the toolbar HL2 hardware-telemetry readout.

        Sibling of ``_on_show_cpu_toggled``; same persistence +
        live-apply pattern, just targets the ``hl2`` key in the
        existing ``_READOUT_LABELS`` machinery on MainWindow.
        """
        from PySide6.QtCore import QSettings
        s = QSettings("N8SDR", "Lyra")
        s.setValue("toolbar/readout_hidden_hl2", not checked)
        try:
            mw = self._find_main_window()
            if mw is not None and hasattr(mw, "_set_readout_visible"):
                mw._set_readout_visible("hl2", checked)
        except Exception as exc:
            print(f"[settings] could not apply HL2 telemetry "
                  f"visibility live: {exc}")

    def _on_diag_overlay_changed(self, _idx: int) -> None:
        """Apply the diagnostic overlay 3-state mode live.

        Phase 4 / v0.1.0 (CLAUDE.md §15.11).  Same MainWindow-
        lookup pattern as ``_on_show_cpu_toggled``; QSettings
        write is performed inside MainWindow's
        ``_apply_telemetry_overlay_mode`` so a single source of
        truth handles persistence.
        """
        mode = str(self.diag_overlay_combo.currentData())
        try:
            mw = self._find_main_window()
            if mw is not None and hasattr(mw, "_apply_telemetry_overlay_mode"):
                mw._apply_telemetry_overlay_mode(mode)
        except Exception as exc:
            # Live-apply failed — write the QSettings key directly
            # so the choice still survives a relaunch.
            print(f"[settings] could not apply diagnostic "
                  f"overlay mode live: {exc}")
            try:
                from PySide6.QtCore import QSettings
                QSettings("N8SDR", "Lyra").setValue(
                    "telemetry/overlay_mode", mode)
            except Exception:
                pass


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
            lambda on: _safe_mirror(
                self.n2adr_toggle, "isChecked", "setChecked", bool(on)))
        radio.bcd_value_changed.connect(self._on_bcd_changed)
        radio.usb_bcd_changed.connect(
            lambda on: _safe_mirror(
                self.bcd_toggle, "isChecked", "setChecked", bool(on)))

    @staticmethod
    def _bits_text(bits: int) -> str:
        return f"0x{bits:02X}  pins {format_bits(bits)}"

    @staticmethod
    def _bcd_text(value: int, band: str) -> str:
        if not band:
            return f"0x{value:02X}  ({value})"
        return f"0x{value:02X}  ({value})  →  {band}"

    @_swallow_dead_widget
    def _on_bits_changed(self, bits: int, _human: str):
        self.bits_label.setText(self._bits_text(bits))

    @_swallow_dead_widget
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
    """DSP chain configuration — AGC profiles + advisory custom
    sliders, CW (Pitch / APF / BIN), Auto-LNA, Equalizer (TX
    placeholder), DSP threading.  NB / ANF / NR / Squelch /
    Captured Profile live on the separate Noise tab."""

    # Description + ordering for the AGC profile radio buttons.
    # Profiles drive WDSP's canonical AGC mode presets via
    # _wdsp_rx.set_agc(...).  Custom is currently advisory — see
    # the slider-section tooltips below.
    AGC_PROFILE_UI = [
        ("off",    "Off",     "No AGC — Volume + AF Gain scale the raw demod output"),
        ("fast",   "Fast",    "Quick attack/decay, no hang — CW, weak-signal work"),
        ("med",    "Medium",  "Moderate decay, no hang — general SSB / ragchew (default)"),
        ("slow",   "Slow",    "Longer decay with short hang — DX nets, AM broadcast"),
        ("long",   "Long",    "Long decay with long hang — beacons, steady-carrier listening"),
        ("auto",   "Auto",    "Same time-constants as Medium today (auto-threshold tracking is parked)"),
        ("custom", "Custom",  "Custom Release/Hang sliders below — currently advisory (same as Medium)"),
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

        # Advisory note — covers the next three rows of sliders
        # (Release / Hang / Threshold).  WDSP owns the live AGC
        # engine so these UI values are operator-state mirrors
        # only — see CLAUDE.md §14.9 Phase 9.5 for the wire-up TODO.
        advisory_lbl = QLabel(
            "Sliders below are persisted as operator preference; "
            "WDSP currently uses its canonical mode presets, so the "
            "Custom profile produces the same audio as Medium for now.")
        advisory_lbl.setWordWrap(True)
        advisory_lbl.setStyleSheet(
            "color: #999; font-style: italic; padding: 4px 0px;")
        ga.addWidget(advisory_lbl, 1, 0, 1, len(self.AGC_PROFILE_UI))

        # Custom sliders — always visible but disabled unless
        # Custom is picked.  Currently advisory (see note above).
        # Slider range covers all preset values (Fast = 0.300,
        # Med = 0.158, Slow = 0.083, Long = 0.040) so picking a
        # profile visibly slides the markers to the right spot.
        ga.addWidget(QLabel("Release"), 2, 0)
        self.release_slider = QSlider(Qt.Horizontal)
        self.release_slider.setRange(1, 300)   # 0.001 .. 0.300
        self.release_slider.setValue(int(radio.agc_release * 1000))
        self.release_slider.setFixedWidth(200)
        self.release_slider.setToolTip(
            "AGC release coefficient (advisory in WDSP mode).\n"
            "Higher = faster decay; lower = slower decay.\n"
            "Picking a profile slides this to the profile's preset.")
        self.release_slider.valueChanged.connect(self._on_custom_changed)
        ga.addWidget(self.release_slider, 2, 1, 1, 3)
        self.release_label = QLabel()
        ga.addWidget(self.release_label, 2, 4)

        ga.addWidget(QLabel("Hang"), 3, 0)
        self.hang_slider = QSlider(Qt.Horizontal)
        self.hang_slider.setRange(0, 100)  # blocks → roughly 0..4.5 s
        self.hang_slider.setValue(int(radio.agc_hang_blocks))
        self.hang_slider.setFixedWidth(200)
        self.hang_slider.setToolTip(
            "AGC hang duration in audio blocks (advisory in WDSP mode).\n"
            "Each block ≈ 43 ms at 48 kHz / 2048 samples.\n"
            "Picking a profile slides this to the profile's preset.")
        self.hang_slider.valueChanged.connect(self._on_custom_changed)
        ga.addWidget(self.hang_slider, 3, 1, 1, 3)
        self.hang_label = QLabel()
        ga.addWidget(self.hang_label, 3, 4)

        # AGC threshold — operator-tunable slider was removed in
        # v0.0.9.8.  The default (-100 dBFS, set in Radio
        # ``_open_wdsp_rx``) is comfortable for typical HF
        # operation, and the **Auto** button below recalibrates it
        # to ~5 dB above the current noise floor for band-condition
        # changes.  The DSP+Audio panel cluster's ``thr <-NN dBFS>``
        # readout still shows the current value live.  Power users
        # who want direct dBFS control can edit
        # ``HKCU\Software\N8SDR\Lyra\agc\threshold`` between
        # sessions, or hit Auto on a quiet patch of band.  Keeping
        # the row to display the readout + Auto button only.
        ga.addWidget(QLabel("Threshold"), 4, 0)
        self.threshold_label = QLabel()
        self.threshold_label.setToolTip(
            "Current AGC threshold in dBFS.  Read-only — use the\n"
            "Auto button to recalibrate.  Default -100 dBFS is a\n"
            "comfortable starting point for normal HF operation.")
        ga.addWidget(self.threshold_label, 4, 1, 1, 3)
        self.auto_thresh_btn = QPushButton("Auto")
        self.auto_thresh_btn.setToolTip(
            "Recalibrate the threshold to ~5 dB above the current\n"
            "rolling noise floor.  Best run on a quiet part of the\n"
            "band — places the threshold just above the noise so\n"
            "AGC engages on actual signals, not on the noise itself.")
        self.auto_thresh_btn.clicked.connect(self._on_auto_threshold)
        ga.addWidget(self.auto_thresh_btn, 4, 5)

        # Live action meter.  Shows the live AGC gain reduction in
        # dB when AGC is actively running.  Falls back to a sentinel
        # string when there's no live data:
        #   "—"            → stream is stopped (no demod blocks
        #                     flowing → no agc_action_db emissions)
        #   "(AGC off)"    → AGC profile is "off" (Radio short-
        #                     circuits before emitting agc_action_db,
        #                     so the live signal never fires)
        #   "+X.X dB"      → live readout from agc_action_db
        # The state-aware text is updated by _refresh_agc_action_label
        # which is wired to stream_state_changed + agc_profile_changed
        # below; the live numeric updates come from _on_action_db.
        # Span the label across 3 cols (was 2) so the colon doesn't
        # crowd the value field even with the widest "Custom" radio
        # column setting the row pitch.
        ga.addWidget(QLabel("Current AGC action:"), 5, 0, 1, 3)
        self.action_label = QLabel("—")
        self.action_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; font-weight: 700;")
        ga.addWidget(self.action_label, 5, 3, 1, 4)

        v.addWidget(grp_agc)

        # ── CW ───────────────────────────────────────────────────────
        # Home for all CW operator settings.  Today: pitch (drives
        # the WDSP CW demod offset, APF center, panadapter passband
        # overlay, and click-to-tune CW correction), APF (audio
        # peaking filter), BIN (binaural CW).  Persisted via
        # QSettings (radio handles save).
        #
        # FUTURE-MOVE NOTE: when TX lands, CW transmission adds
        # break-in mode (semi / full BK / off), keying speed (WPM),
        # weight (dot:dash ratio), sidetone level, paddle reverse,
        # and possibly memory/macro buffers. At that point this
        # group may graduate into a dedicated "CW" tab alongside
        # Radio / Network / DSP / Audio / Visuals / Keyer / Bands,
        # or merge into the Keyer tab. Pick the right home when TX
        # scope is clearer. For now, the DSP tab is the natural
        # home — CW RX is pure DSP and APF/BIN/pitch are all DSP
        # settings.
        grp_cw = QGroupBox("CW")
        gc = QGridLayout(grp_cw)
        gc.addWidget(QLabel("Pitch (Hz):"), 0, 0)
        from PySide6.QtWidgets import QSpinBox
        self.cw_pitch_spin = QSpinBox()
        self.cw_pitch_spin.setRange(200, 1500)
        self.cw_pitch_spin.setSingleStep(10)
        self.cw_pitch_spin.setSuffix(" Hz")
        self.cw_pitch_spin.setValue(int(radio.cw_pitch_hz))
        self.cw_pitch_spin.setFixedWidth(120)
        self.cw_pitch_spin.setToolTip(
            "CW tone frequency. Operator preference (typical 400-800 Hz; "
            "many ops settle on 600 or 700 Hz). Drives the WDSP CW "
            "demod offset, the APF center frequency, the panadapter "
            "passband overlay position, and the click-to-tune CW "
            "correction — all stay in sync. Live update on change.")
        self.cw_pitch_spin.valueChanged.connect(
            self.radio.set_cw_pitch_hz)
        gc.addWidget(self.cw_pitch_spin, 0, 1)
        gc.setColumnStretch(2, 1)
        # Reflect external changes (e.g., loaded from a snapshot or
        # set programmatically) back into the spinbox.
        radio.cw_pitch_changed.connect(
            lambda hz: _safe_mirror(
                self.cw_pitch_spin, "value", "setValue", int(hz)))

        # ── APF (Audio Peaking Filter) ───────────────────────────
        # Narrow peaking biquad centered on cw_pitch_hz. Only audible
        # in CWU/CWL — channel mode-gates internally. Three controls:
        # enable / -3 dB BW / peak gain. Center freq follows pitch
        # automatically (no separate spinbox).
        # Phase 4: APF range constants now live on Radio (APF_BW_*,
        # APF_GAIN_*); previously imported _APF here just to read
        # them.  Decouples settings_dialog from `lyra/dsp/apf.py`
        # ahead of that module's deletion in Phase 6.
        from PySide6.QtWidgets import QCheckBox
        gc.addWidget(QLabel("APF:"), 1, 0)
        self.apf_enable_chk = QCheckBox(
            "Boost CW signal at pitch (Audio Peaking Filter)")
        self.apf_enable_chk.setChecked(bool(radio.apf_enabled))
        self.apf_enable_chk.setToolTip(
            "When ON: a narrow peaking filter boosts audio at your CW\n"
            "pitch, lifting weak signals out of the noise without the\n"
            "ringing tail of a brick-wall narrow filter. Other audio\n"
            "in the passband stays audible (you keep band context).\n\n"
            "Only runs in CWU/CWL — preserved across mode switches\n"
            "but silent in SSB/AM/FM/digital. Default OFF.")
        self.apf_enable_chk.toggled.connect(self.radio.set_apf_enabled)
        # Safe slot wrapper — Brent + Rick hit "wrapped C++ object of
        # type QCheckBox has been deleted" RuntimeError when the
        # radio's apf_enabled_changed signal fired during/after this
        # dialog's destruction.  The race: dialog closed → C++ side
        # torn down → another thread emits the signal → lambda
        # tries to .isChecked() on a zombie wrapper.  Wrap in
        # try/except so the exception doesn't propagate up through
        # Radio's signal infrastructure.  Defensive — see v0.0.9.3.1.
        radio.apf_enabled_changed.connect(self._on_radio_apf_enabled_changed)
        gc.addWidget(self.apf_enable_chk, 1, 1, 1, 2)

        gc.addWidget(QLabel("APF BW (Hz):"), 2, 0)
        self.apf_bw_spin = QSpinBox()
        self.apf_bw_spin.setRange(radio.APF_BW_MIN_HZ,
                                   radio.APF_BW_MAX_HZ)
        self.apf_bw_spin.setSingleStep(10)
        self.apf_bw_spin.setSuffix(" Hz")
        self.apf_bw_spin.setValue(int(radio.apf_bw_hz))
        self.apf_bw_spin.setFixedWidth(120)
        self.apf_bw_spin.setToolTip(
            f"APF -3 dB bandwidth "
            f"({radio.APF_BW_MIN_HZ}-{radio.APF_BW_MAX_HZ} Hz).\n"
            "Lower = sharper peak, more boost concentration. Below ~30 Hz\n"
            "the filter starts to ring on dits — keep ≥40 Hz for\n"
            "comfortable CW. Default 80 Hz.")
        self.apf_bw_spin.valueChanged.connect(self.radio.set_apf_bw_hz)
        radio.apf_bw_changed.connect(
            lambda v_: _safe_mirror(
                self.apf_bw_spin, "value", "setValue", int(v_)))
        gc.addWidget(self.apf_bw_spin, 2, 1)

        gc.addWidget(QLabel("APF Gain (dB):"), 3, 0)
        self.apf_gain_spin = QSpinBox()
        self.apf_gain_spin.setRange(int(radio.APF_GAIN_MIN_DB),
                                    int(radio.APF_GAIN_MAX_DB))
        self.apf_gain_spin.setSingleStep(1)
        self.apf_gain_spin.setSuffix(" dB")
        self.apf_gain_spin.setValue(int(radio.apf_gain_db))
        self.apf_gain_spin.setFixedWidth(120)
        self.apf_gain_spin.setToolTip(
            f"APF peak gain ({int(radio.APF_GAIN_MIN_DB)}-"
            f"{int(radio.APF_GAIN_MAX_DB)} dB). "
            "Boost amount at the CW pitch.\n"
            "Above ~14 dB, AGC pumping becomes noticeable on signals\n"
            "that are already strong. Default +12 dB.")
        self.apf_gain_spin.valueChanged.connect(
            lambda v_: self.radio.set_apf_gain_db(float(v_)))
        radio.apf_gain_changed.connect(
            lambda v_: _safe_mirror(
                self.apf_gain_spin, "value", "setValue", int(v_)))
        gc.addWidget(self.apf_gain_spin, 3, 1)

        # ── BIN (Binaural pseudo-stereo) ─────────────────────────
        # Hilbert phase-split puts the audio "in the middle of the
        # head" for headphone listening. Helps both CW (spatial cue
        # for weak signals) and SSB (voice widening). Two controls:
        # enable + depth (0-100%).
        from lyra.dsp.binaural import BinauralFilter as _BIN
        gc.addWidget(QLabel("BIN:"), 4, 0)
        self.bin_enable_chk = QCheckBox(
            "Binaural pseudo-stereo (headphones)")
        self.bin_enable_chk.setChecked(bool(radio.bin_enabled))
        self.bin_enable_chk.setToolTip(
            "When ON: a Hilbert phase-split routes a 90°-shifted copy\n"
            "of the audio to one ear and the original to the other.\n"
            "The brain hears this as a wider soundstage — helpful for\n"
            "pulling weak CW out of noise (classic 'binaural CW' effect)\n"
            "and for SSB voice on headphones.\n\n"
            "Runs on all modes; no mode gate. Default OFF.")
        self.bin_enable_chk.toggled.connect(self.radio.set_bin_enabled)
        radio.bin_enabled_changed.connect(
            lambda on: _safe_mirror(
                self.bin_enable_chk, "isChecked", "setChecked", bool(on)))
        gc.addWidget(self.bin_enable_chk, 4, 1, 1, 2)

        gc.addWidget(QLabel("BIN Depth (%):"), 5, 0)
        self.bin_depth_spin = QSpinBox()
        self.bin_depth_spin.setRange(
            int(_BIN.DEPTH_MIN * 100), int(_BIN.DEPTH_MAX * 100))
        self.bin_depth_spin.setSingleStep(5)
        self.bin_depth_spin.setSuffix(" %")
        self.bin_depth_spin.setValue(int(round(radio.bin_depth * 100)))
        self.bin_depth_spin.setFixedWidth(120)
        self.bin_depth_spin.setToolTip(
            "BIN depth — 0% = mono (no separation, equivalent to off),\n"
            "100% = full Hilbert phase pair (maximum spatial cue).\n"
            "Equal-loudness normalized so depth doesn't change perceived\n"
            "volume. Default 70%.")
        self.bin_depth_spin.valueChanged.connect(
            lambda v_: self.radio.set_bin_depth(float(v_) / 100.0))
        radio.bin_depth_changed.connect(
            lambda v_: _safe_mirror(
                self.bin_depth_spin, "value", "setValue",
                int(round(v_ * 100))))
        gc.addWidget(self.bin_depth_spin, 5, 1)

        v.addWidget(grp_cw)

        # ── Auto-LNA (front-end gain automation) ─────────────────────
        # The Auto button on the DSP+Audio panel toggles overload-
        # protection back-off (always-on safety net). The pull-up
        # toggle here promotes it to bidirectional — also raises gain
        # when the band is sustained-quiet. Default OFF: the v1
        # upward-chasing implementation drove LNA to +44 dB on 40 m
        # and caused IMD, so this stays opt-in until field-tested.
        grp_lna = QGroupBox("Auto-LNA")
        gl = QVBoxLayout(grp_lna)
        self.lna_pullup_chk = QCheckBox(
            "Auto-LNA pull-up — also raise gain on sustained quiet "
            "bands (opt-in, default off)")
        self.lna_pullup_chk.setChecked(radio.lna_auto_pullup)
        self.lna_pullup_chk.setToolTip(
            "When OFF (default): the panel's Auto button is BACK-OFF\n"
            "ONLY — it lowers LNA when the ADC gets close to clipping\n"
            "and otherwise leaves your manual gain alone.\n\n"
            "When ON: Auto becomes bidirectional. After the band has\n"
            "been quiet (RMS < -50 dBFS, peak < -25 dBFS) for ~7.5\n"
            "seconds, Auto climbs LNA by 1 dB to dig out weak\n"
            "signals. Conservative ceiling at +24 dB to stay below\n"
            "the IMD zone. Self-limits naturally when the noise floor\n"
            "rises with gain. Defers 5 s after any manual slider\n"
            "change so your input always wins.\n\n"
            "Requires the Auto button to be enabled to take effect."
        )
        self.lna_pullup_chk.toggled.connect(
            self.radio.set_lna_auto_pullup)
        radio.lna_auto_pullup_changed.connect(
            lambda on: _safe_mirror(
                self.lna_pullup_chk, "isChecked", "setChecked", bool(on)))
        gl.addWidget(self.lna_pullup_chk)
        v.addWidget(grp_lna)

        # NB and NR moved to Settings → Noise tab when those
        # features shipped (Phase 3.D #1 NR / #2 NB).  EQ stays as a
        # placeholder — it's a future feature with no current
        # backend, and once it ships it'll get its own tab so the
        # parametric controls (per-band freq/gain/Q with bypass)
        # have room to breathe.
        grp_eq = QGroupBox("Equalizer (parametric)")
        geq = QVBoxLayout(grp_eq)
        geq.addWidget(QLabel("Parametric RX / TX equalizer — coming soon."))
        grp_eq.setEnabled(False)
        v.addWidget(grp_eq)

        # ── DSP Threading (v0.0.9.2 audio rebuild Commit 1) ──────────
        # Operator-selectable backend for where DSP runs:
        #   "worker" — dedicated DspWorker thread (DEFAULT as of
        #              v0.0.9.2; isolates DSP from UI activity to
        #              eliminate the click-pop class of bugs)
        #   "single" — Qt main thread (legacy fallback for any
        #              operator who hits a regression in worker
        #              mode and needs a known-good escape hatch)
        # Switching requires Lyra restart.  The value here is the
        # PREFERENCE (persisted, applied next restart); a hint shows
        # when it differs from what's currently running this session.
        # See docs/architecture/audio_rebuild_v0.1.md sec 3.1 + 9.3.
        grp_thr = QGroupBox("DSP Threading (advanced)")
        gthr = QVBoxLayout(grp_thr)

        thr_intro = QLabel(
            "Where Lyra's DSP audio chain runs.  Default as of "
            "v0.0.9.2 is the dedicated DSP worker thread — this "
            "isolates audio production from UI activity (paint "
            "events, mouse handling) and is part of the audio "
            "click-pop fix.\n\n"
            "Single-thread is the legacy mode (Qt main thread runs "
            "both UI and DSP).  Available as a fallback if you "
            "hit a regression in worker mode — flip back, restart, "
            "and please report what you saw.\n\n"
            "Changes take effect on the next Lyra restart.")
        thr_intro.setWordWrap(True)
        thr_intro.setStyleSheet("color: #8a9aac;")
        gthr.addWidget(thr_intro)

        from PySide6.QtWidgets import QComboBox
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Threading:"))
        self.threading_combo = QComboBox()
        # Worker first (default); single-thread second (legacy).
        # itemData stays the same string keys so QSettings round-
        # trips unchanged.
        self.threading_combo.addItem("Worker thread (default)", "worker")
        self.threading_combo.addItem("Single-thread (legacy)", "single")
        # Select the current persisted preference
        cur_mode = radio.dsp_threading_mode
        idx = next((i for i in range(self.threading_combo.count())
                    if self.threading_combo.itemData(i) == cur_mode), 0)
        self.threading_combo.setCurrentIndex(idx)
        self.threading_combo.setToolTip(
            "DSP threading mode preference.  Persisted via QSettings; "
            "takes effect on the next Lyra restart.\n\n"
            "Worker thread (default): DSP runs on a dedicated thread, "
            "isolated from UI rendering.  Eliminates the producer-side "
            "jitter that caused click-pop bugs in v0.0.9.1 and earlier.\n"
            "Single-thread (legacy): DSP runs on the Qt main thread.  "
            "Available as a fallback if worker mode regresses for you.")
        self.threading_combo.currentIndexChanged.connect(
            self._on_threading_combo)
        thr_row.addWidget(self.threading_combo, 1)
        gthr.addLayout(thr_row)

        # Restart-required hint — only visible when the operator's
        # selection differs from what's actually running this session.
        self.threading_restart_hint = QLabel("")
        self.threading_restart_hint.setWordWrap(True)
        self.threading_restart_hint.setStyleSheet(
            "color: #ffb84a; font-weight: 700; padding: 4px;")
        gthr.addWidget(self.threading_restart_hint)
        self._refresh_threading_restart_hint()
        radio.dsp_threading_mode_changed.connect(
            lambda _m: self._refresh_threading_restart_hint())

        v.addWidget(grp_thr)

        v.addStretch(1)

        self._update_labels()
        self._update_custom_enabled(radio.agc_profile)
        radio.agc_profile_changed.connect(self._on_profile_changed)
        radio.agc_action_db.connect(self._on_action_db)
        radio.agc_threshold_changed.connect(self._on_threshold_changed)
        # Initialize and keep the AGC-action label state-aware so it
        # never sits at the placeholder "—" when AGC is on and the
        # stream is running, and shows a sensible label ("AGC off",
        # "stream stopped") otherwise.  WDSP's AGC engine drives the
        # agc_action_db signal directly, but it doesn't fire when
        # the operator picks profile "off" — without this hook we'd
        # show stale data on toggle.
        radio.stream_state_changed.connect(
            lambda _on: self._refresh_agc_action_label())
        # agc_profile_changed already wired above for the radio
        # button selection; piggyback the action-label refresh on
        # the same signal via a separate connection.
        radio.agc_profile_changed.connect(
            lambda _name: self._refresh_agc_action_label())
        self._refresh_agc_action_label()

    def _on_custom_changed(self):
        release = self.release_slider.value() / 1000.0
        hang = int(self.hang_slider.value())
        self.radio.set_agc_custom(release, hang)
        self._update_labels()

    def _update_labels(self):
        import math
        release = self.release_slider.value() / 1000.0
        hang = self.hang_slider.value()
        # Hang time in ms (each block ≈ 43 ms at 48 kHz, 2048 samples)
        hang_ms = int(hang * 43)
        # Release coefficient → 1/e decay time in milliseconds.
        # For 1-pole exp filter: tau_blocks = -1 / ln(1 - alpha),
        # converted to ms via the same 43 ms/block factor.  Lets
        # the operator SEE the difference between Fast (≈120 ms),
        # Med (≈250 ms), Slow (≈495 ms), Long (≈1050 ms).
        if release > 0.0:
            tau_blocks = -1.0 / math.log(max(1.0 - release, 1e-6))
            tau_ms = int(tau_blocks * 43)
            self.release_label.setText(f"{release:.3f}  (≈{tau_ms} ms decay)")
        else:
            self.release_label.setText(f"{release:.3f}  (no decay)")
        self.hang_label.setText(f"{hang} blk  ({hang_ms} ms)")
        # Threshold readout — read directly from radio (no slider).
        t_dbfs = float(self.radio.agc_threshold)
        self.threshold_label.setText(f"{int(round(t_dbfs)):+d} dBFS")

    @_swallow_dead_widget
    def _on_threshold_changed(self, value: float):
        # Just refresh the label; no slider to sync.
        self._update_labels()

    def _on_auto_threshold(self):
        self.radio.auto_set_agc_threshold()

    def _update_custom_enabled(self, profile: str):
        is_custom = profile == "custom"
        self.release_slider.setEnabled(is_custom)
        self.hang_slider.setEnabled(is_custom)

    @_swallow_dead_widget
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

    @_swallow_dead_widget
    def _on_radio_apf_enabled_changed(self, on: bool) -> None:
        """Safe slot for ``radio.apf_enabled_changed``.

        Used to be a lambda inline at connect time; converted to a
        named method in v0.0.9.3.1 so we can wrap the body in a
        try/except RuntimeError guard.  Without the guard, both
        Brent and Rick hit "wrapped C++ object of type QCheckBox
        has been deleted" when the radio fired this signal during
        dialog teardown.

        The body is the original lambda's intent: only update the
        checkbox if its state would actually change, to avoid
        signal echo (toggled → set_apf_enabled → apf_enabled_changed
        → setChecked → toggled again).
        """
        try:
            if self.apf_enable_chk.isChecked() != bool(on):
                self.apf_enable_chk.setChecked(bool(on))
        except RuntimeError:
            # Dialog was destroyed between signal emit and slot
            # delivery — nothing to update, harmless to ignore.
            pass

    @_swallow_dead_widget
    def _on_action_db(self, action_db: float):
        """Live AGC gain reduction in dB, fired from
        ``Radio.agc_action_db`` once per demod block (~40 Hz).

        Skipped when AGC profile is "off": Radio doesn't emit in
        that case, but a stale subscriber from a previous active-
        AGC session could still fire one last value during the
        teardown.  Belt-and-suspenders — re-check the profile
        before writing live numbers over the "(AGC off)" sentinel.

        Wrapped in try/except RuntimeError to survive dialog
        teardown — see _on_radio_apf_enabled_changed comment for
        the same crash class.
        """
        if self.radio.agc_profile == "off" or not self.radio.is_streaming:
            return
        try:
            self.action_label.setText(f"{action_db:+.1f} dB")
        except RuntimeError:
            pass  # action_label destroyed — dialog being torn down

    def _refresh_agc_action_label(self) -> None:
        """Update the AGC-action label to reflect the current
        stream + profile state.

        Called at dialog construction and whenever stream state or
        AGC profile changes.  Resolves the label to one of three
        states:
        - "—"          → stream is stopped (no live AGC data)
        - "(AGC off)"  → AGC profile is "off" (Radio short-
                          circuits agc_action_db emission)
        - last live    → otherwise leave whatever the latest
                          _on_action_db update wrote there;
                          will be overwritten within ~25 ms

        Wrapped in try/except RuntimeError to survive dialog
        teardown — both stream_state_changed and agc_profile_changed
        connect lambdas that call this method, and either signal
        can fire after the dialog's C++ side is gone.
        """
        try:
            if not self.radio.is_streaming:
                self.action_label.setText("—")
                return
            if self.radio.agc_profile == "off":
                self.action_label.setText("(AGC off)")
                return
            # Stream live + AGC active — _on_action_db handles the
            # live numeric update.  Show a transient placeholder until
            # the next emission lands so the operator sees movement
            # rather than potentially-stale values from before a
            # profile change.
            self.action_label.setText("…")
        except RuntimeError:
            # action_label widget destroyed (dialog teardown race) —
            # harmless, signal will be auto-disconnected by Qt
            # parent-child cleanup soon.
            pass

    # ── DSP Threading (Phase 3.B+) ─────────────────────────────────
    def _on_threading_combo(self, idx: int):
        """Operator changed the threading-mode combo. Persist via
        Radio.set_dsp_threading_mode (which fires the *_changed
        signal — restart-hint label updates as a side effect)."""
        if idx < 0:
            return
        mode = self.threading_combo.itemData(idx)
        if mode:
            self.radio.set_dsp_threading_mode(str(mode))

    def _refresh_threading_restart_hint(self):
        """Show / hide the 'restart required' hint based on whether
        the operator's selected threading mode matches what's
        actually running this session."""
        running = self.radio.dsp_threading_mode_at_startup
        selected = self.radio.dsp_threading_mode
        if running == selected:
            self.threading_restart_hint.setText("")
            self.threading_restart_hint.setVisible(False)
        else:
            self.threading_restart_hint.setText(
                f"⚠  Restart Lyra to apply: currently running "
                f"'{running}', selected '{selected}'.")
            self.threading_restart_hint.setVisible(True)


class AudioSettingsTab(QWidget):
    """Audio output configuration.

    Currently hosts the PC Soundcard device picker. Grows over time
    as more audio knobs land (output channel routing, balance,
    per-output gain trim, etc.) — anything that's "where does my
    audio go and how is it shaped before the speakers" belongs here,
    distinct from the DSP tab which is about signal processing.
    """

    # CLAUDE.md §15.16 (2026-05-14): device dropdown is grouped by
    # host API with non-selectable section headers.  Header rows
    # carry this sentinel as their userData so _on_device_picked +
    # _sync_to_radio can distinguish them from real device entries.
    _SEP_SENTINEL = "__section_separator__"

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        v = QVBoxLayout(self)

        # ── Output sink selector (HL2 audio jack vs PC Soundcard) ──
        # Mirror of the "Out" combo on the DSP+Audio panel — exposed
        # here so operators looking under Settings → Audio find it.
        # v0.0.9.6: renamed the operator-facing "AK4951" label to
        # "HL2 audio jack" since not every HL2 revision uses the
        # AK4951 chip specifically (they all share the same EP2-
        # back-to-codec path though).  Internal QSettings value
        # stays "AK4951" for back-compat.
        grp_sink = QGroupBox("Output sink")
        gs = QHBoxLayout(grp_sink)
        gs.addWidget(QLabel("Send audio to:"))
        self._sink_combo = QComboBox()
        self._sink_combo.addItem("HL2 audio jack", userData="AK4951")
        self._sink_combo.addItem("PC Soundcard", userData="PC Soundcard")
        for i in range(self._sink_combo.count()):
            if self._sink_combo.itemData(i) == radio.audio_output:
                self._sink_combo.setCurrentIndex(i)
                break
        self._sink_combo.setMinimumWidth(180)
        self._sink_combo.currentIndexChanged.connect(
            lambda _idx: radio.set_audio_output(
                self._sink_combo.currentData()))
        gs.addWidget(self._sink_combo)
        gs.addStretch(1)
        sink_help = QLabel(
            "HL2 audio jack: audio routes back to the HL2's onboard "
            "codec via EP2 packets — single-crystal path, zero "
            "clock drift, recommended for HL2 hardware.\n"
            "PC Soundcard: routes to your computer's audio output. "
            "v0.0.9.6 includes adaptive rate matching to absorb "
            "the inevitable HL2-vs-soundcard crystal drift, plus "
            "operator-pickable PortAudio host API (below)."
        )
        sink_help.setWordWrap(True)
        sink_help.setStyleSheet("color: #8a9aac; font-size: 10px;")
        v.addWidget(grp_sink)
        v.addWidget(sink_help)

        # ── v0.0.9.6: PortAudio host API picker ────────────────────
        # Operator picks which Windows audio API to use for the PC
        # Soundcard sink.  Different APIs have different latency /
        # reliability tradeoffs; matches Thetis's Settings → Audio
        # → Driver flexibility.  Only meaningful when the sink
        # above is set to "PC Soundcard" — but always editable so
        # operators can pre-configure before switching.
        from lyra.dsp.audio_sink import enumerate_host_apis
        grp_api = QGroupBox("PortAudio host API (PC Soundcard only)")
        ga = QHBoxLayout(grp_api)
        ga.addWidget(QLabel("Audio API:"))
        self._api_combo = QComboBox()
        self._api_entries: list[dict] = enumerate_host_apis()
        for entry in self._api_entries:
            label = entry["label"]
            display = label
            if not entry["available"]:
                display = f"{label} (not available)"
            self._api_combo.addItem(display, userData=label)
            # Disable the row if not available (PySide6 requires
            # poking into the model item).
            if not entry["available"]:
                model = self._api_combo.model()
                idx = self._api_combo.count() - 1
                model.item(idx).setFlags(
                    model.item(idx).flags() & ~Qt.ItemIsEnabled)
        # Set selection from radio's stored value (or "Auto" default).
        current_api = radio.pc_audio_host_api or "Auto"
        for i in range(self._api_combo.count()):
            if self._api_combo.itemData(i) == current_api:
                self._api_combo.setCurrentIndex(i)
                break
        self._api_combo.setMinimumWidth(220)
        self._api_combo.setToolTip(
            "Auto: PortAudio picks the system default (currently "
            "WASAPI shared on Windows).  Generally OK but subject "
            "to occasional Windows audio-engine pauses.\n"
            "\n"
            "MME: oldest/most-compatible Windows audio API.  "
            "Higher latency (~50-100ms) but very reliable.  "
            "Default in Thetis VAC.\n"
            "\n"
            "WASAPI shared: low latency (~20ms), goes through "
            "Windows audio engine.  Subject to engine pauses on "
            "focus changes / app start-stop / etc.\n"
            "\n"
            "WASAPI exclusive: low latency, BYPASSES the Windows "
            "audio engine.  Cleanest audio path BUT locks the "
            "device — no other Windows apps can play through it "
            "while Lyra runs.  Use if you operate Lyra exclusively.\n"
            "\n"
            "WDM-KS: low latency, bypasses engine, allows sharing.  "
            "Some sound cards' WDM drivers are flaky — try if "
            "WASAPI exclusive isn't acceptable.\n"
            "\n"
            "DirectSound: legacy fallback.  Built-in resampling.\n"
            "\n"
            "ASIO: lowest latency.  Requires an ASIO driver "
            "installation (ASIO4ALL is a common free option, or "
            "use device-specific drivers from your audio interface "
            "manufacturer).  Not shown if no ASIO driver detected.\n"
            "\n"
            "Change takes effect immediately if PC Soundcard is "
            "the active output sink.  Persists across launches.")
        self._api_combo.currentIndexChanged.connect(
            lambda _idx: radio.set_pc_audio_host_api(
                self._api_combo.currentData()))
        ga.addWidget(self._api_combo)
        ga.addStretch(1)
        v.addWidget(grp_api)

        def _sync_sink_combo(stored: str) -> None:
            """Sync combo when Radio changes the output elsewhere
            (e.g., rate-driven auto-fallback when AK4951 hits a >48k
            stream).  Uses userData lookup so the operator-facing
            label and internal value stay synced.

            The try/except RuntimeError catches Qt's zombie-wrapper
            crash when this slot fires after the dialog widgets have
            been torn down but Radio's signal still holds the closure
            reference.  Same defensive pattern as
            `_on_radio_apf_enabled_changed` (CHANGELOG v0.0.9.5).
            """
            try:
                for idx in range(self._sink_combo.count()):
                    if self._sink_combo.itemData(idx) == stored:
                        if self._sink_combo.currentIndex() != idx:
                            self._sink_combo.setCurrentIndex(idx)
                        return
            except RuntimeError:
                pass
        radio.audio_output_changed.connect(_sync_sink_combo)

        # ── Output device (PC Soundcard sink) ──────────────────────
        grp_dev = QGroupBox("Output device — PC Soundcard sink")
        gd = QVBoxLayout(grp_dev)

        info = QLabel(
            "Lyra normally auto-picks the WASAPI default output device.\n"
            "Override here if your audio routes through a non-default\n"
            "card (USB audio interface, virtual cable, S/PDIF dongle, etc).\n"
            "Setting takes effect immediately when PC Soundcard is the\n"
            "active sink. Has no effect when HL2 audio jack is selected."
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

        # NOTE: Audio Leveler section removed in Phase 4 of the
        # legacy-DSP cleanup arc (CLAUDE.md §14.9).  WDSP's AGC
        # (FAST/MED/SLOW/LONG modes already on the DSP+Audio panel)
        # subsumes the dynamic-range work the leveler used to do.
        # See git history for the deleted profile + threshold +
        # ratio + makeup sliders if anyone needs to recover them.

        # ── Mic input (v0.2 Phase 2 commit 6) ──────────────────────
        # Lyra supports two mic-input paths because the HL2 hardware
        # family has two variants:
        # * HL2+ (AK4951 codec) -- mic input on the radio, samples
        #   arrive via EP6 byte slot 24-25.  "HL2 mic jack" path.
        # * Standard HL2 (no codec) -- no mic on the radio; operator
        #   plugs into PC sound card.  "PC sound card" path.
        # Set-and-forget configuration per operator's hardware --
        # lives on the Audio tab rather than its own TX tab so
        # operators find it where they look for audio routing.
        grp_mic = QGroupBox("Mic input")
        gm = QVBoxLayout(grp_mic)

        mic_help = QLabel(
            "Standard HL2 has no mic input on the radio; use PC "
            "sound card.\n"
            "HL2+ (AK4951 codec) has a mic jack -- use HL2 mic "
            "jack for that path.\n"
            "Set once per install -- Lyra remembers your choice."
        )
        mic_help.setStyleSheet("color: #8a9aac; font-size: 10px;")
        gm.addWidget(mic_help)

        # Source picker (radio buttons -- mutually exclusive, clearer
        # at a glance than a 2-item dropdown).
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source:"))
        self._mic_src_group = QButtonGroup(self)
        self._mic_src_hl2 = QRadioButton("HL2 mic jack")
        self._mic_src_hl2.setToolTip(
            "Mic samples come from the radio's AK4951 codec via EP6.\n"
            "Use this for HL2+ (with AK4951 chip).\n"
            "Standard HL2 has no radio-side mic; use PC sound card "
            "instead."
        )
        self._mic_src_pc = QRadioButton("PC sound card")
        self._mic_src_pc.setToolTip(
            "Mic samples come from a microphone plugged into your "
            "PC's sound card input.\n"
            "Use this for standard HL2 (no codec) or if you prefer "
            "a USB / Bluetooth mic regardless of HL2 variant."
        )
        self._mic_src_group.addButton(self._mic_src_hl2, 0)
        self._mic_src_group.addButton(self._mic_src_pc, 1)
        # Initial selection from Radio state
        if radio.mic_source == "hl2_jack":
            self._mic_src_hl2.setChecked(True)
        else:
            self._mic_src_pc.setChecked(True)
        src_row.addWidget(self._mic_src_hl2)
        src_row.addWidget(self._mic_src_pc)
        src_row.addStretch(1)
        gm.addLayout(src_row)

        # PC sound card sub-section -- only meaningful when PC source
        # is active.  Always editable so operators can pre-configure
        # before switching.
        self._pc_mic_subframe = QWidget()
        pc_layout = QVBoxLayout(self._pc_mic_subframe)
        pc_layout.setContentsMargins(20, 4, 0, 0)

        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Input device:"))
        self._mic_dev_combo = QComboBox()
        self._mic_dev_combo.setMinimumWidth(360)
        dev_row.addWidget(self._mic_dev_combo, 1)
        mic_refresh = QPushButton("Refresh")
        mic_refresh.setFixedWidth(80)
        mic_refresh.clicked.connect(self._populate_mic_devices)
        dev_row.addWidget(mic_refresh)
        pc_layout.addLayout(dev_row)

        ch_row = QHBoxLayout()
        ch_row.addWidget(QLabel("Channel:"))
        self._mic_ch_group = QButtonGroup(self)
        self._mic_ch_l = QRadioButton("Left")
        self._mic_ch_r = QRadioButton("Right")
        self._mic_ch_both = QRadioButton("Both (averaged)")
        self._mic_ch_group.addButton(self._mic_ch_l, 0)
        self._mic_ch_group.addButton(self._mic_ch_r, 1)
        self._mic_ch_group.addButton(self._mic_ch_both, 2)
        # Initial selection from Radio state
        if radio.pc_mic_channel == "L":
            self._mic_ch_l.setChecked(True)
        elif radio.pc_mic_channel == "R":
            self._mic_ch_r.setChecked(True)
        else:
            self._mic_ch_both.setChecked(True)
        ch_row.addWidget(self._mic_ch_l)
        ch_row.addWidget(self._mic_ch_r)
        ch_row.addWidget(self._mic_ch_both)
        ch_row.addStretch(1)
        pc_layout.addLayout(ch_row)

        gm.addWidget(self._pc_mic_subframe)
        v.addWidget(grp_mic)

        # Source picker wiring
        self._mic_src_hl2.toggled.connect(self._on_mic_src_changed)
        # Channel + device wired in _populate_mic_devices + handlers
        self._mic_ch_l.toggled.connect(self._on_mic_channel_changed)
        self._mic_ch_r.toggled.connect(self._on_mic_channel_changed)
        self._mic_ch_both.toggled.connect(self._on_mic_channel_changed)
        self._mic_dev_combo.currentIndexChanged.connect(
            self._on_mic_device_changed)

        # Initial PC mic sub-section enabled-state
        self._refresh_pc_mic_subframe_enabled()

        # Bidirectional sync from Radio side
        try:
            radio.mic_source_changed.connect(self._on_radio_mic_source_changed)
            radio.pc_mic_device_changed.connect(
                self._on_radio_pc_mic_device_changed)
            radio.pc_mic_channel_changed.connect(
                self._on_radio_pc_mic_channel_changed)
        except Exception:
            pass

        # Populate mic device list
        self._populate_mic_devices()

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
        Lyra's WASAPI-default preference.

        Error handling distinguishes between three failure modes so
        operators (especially testers running from source rather
        than the .exe) can self-diagnose:

        1. ``ModuleNotFoundError: sounddevice`` — package isn't
           installed.  Suggest the pip command.
        2. ``OSError`` / ``ImportError`` from the sounddevice
           module — usually means PortAudio DLLs are missing or
           the binary wheel got installed for the wrong Python /
           architecture.
        3. ``query_devices()`` raises something else — host audio
           system in a bad state (driver glitch, sound service
           stopped).  Show the raw exception for diagnostic purposes.
        """
        self._dev_combo.blockSignals(True)
        self._dev_combo.clear()
        # First entry is always "Auto (WASAPI default)" — the safe
        # default. userData=None signals "let SoundDeviceSink pick".
        self._dev_combo.addItem("Auto  (WASAPI default — recommended)", None)

        # Step 1 — try to import sounddevice itself.  Most common
        # failure mode for self-compiling testers.
        try:
            import sounddevice as sd
        except ModuleNotFoundError:
            self._dev_status.setText(
                "✗  sounddevice package not installed.\n"
                "    Open Command Prompt and run:\n"
                "        pip install sounddevice\n"
                "    Then restart Lyra.")
            self._dev_status.setStyleSheet(
                "color: #ff8c00; font-weight: 700; "
                "font-family: Consolas, monospace; "
                "white-space: pre-wrap;")
            self._sync_to_radio(self.radio.pc_audio_device_index)
            self._dev_combo.blockSignals(False)
            return
        except (ImportError, OSError) as e:
            self._dev_status.setText(
                f"✗  sounddevice module failed to load:\n"
                f"        {e}\n"
                f"    Likely the PortAudio binary that ships in\n"
                f"    the sounddevice wheel didn't unpack for your\n"
                f"    Python install.  Try:\n"
                f"        pip install --force-reinstall sounddevice")
            self._dev_status.setStyleSheet(
                "color: #ff4444; font-weight: 700; "
                "font-family: Consolas, monospace; "
                "white-space: pre-wrap;")
            self._sync_to_radio(self.radio.pc_audio_device_index)
            self._dev_combo.blockSignals(False)
            return

        # Step 2 — enumerate, GROUPED BY HOST API per CLAUDE.md §15.16
        # discovery 2026-05-14.  Walking devices in PortAudio-index
        # order interleaves duplicates of the same physical device
        # across host APIs (e.g. "Speakers (Realtek)" appears once
        # per WASAPI / WDM-KS / DirectSound / MME).  Grouping with
        # section-header dividers makes that explicable -- operator
        # sees the same speakers under WASAPI shared, WASAPI exclusive,
        # and WDM-KS, and picks the one matching their latency goal.
        #
        # Section dividers are added as items with userData=_SEP_SENTINEL
        # and disabled (Qt.ItemIsEnabled removed) so they appear in the
        # dropdown but can't be selected.  _on_device_picked guards
        # against programmatic selection of a divider just in case.
        try:
            devices = sd.query_devices()
            host_apis = list(sd.query_hostapis())
            # {host_api_index: [(dev_idx, dev_dict), ...]}
            groups: dict[int, list[tuple[int, dict]]] = {}
            for idx, dev in enumerate(devices):
                if dev.get("max_output_channels", 0) <= 0:
                    continue
                ha_idx = int(dev.get("hostapi", -1))
                if ha_idx < 0:
                    continue
                groups.setdefault(ha_idx, []).append((idx, dev))

            # Preferred display order -- matches the host-API picker
            # dropdown above for visual consistency.  Any host APIs
            # not in this list (rare; usually only on Linux/macOS)
            # land at the end in PortAudio-reported order.
            _PREFERRED_NAMES = (
                "Windows WASAPI",
                "Windows WDM-KS",
                "Windows DirectSound",
                "MME",
                "ASIO",
            )
            ordered_ha_idxs: list[int] = []
            for pref_name in _PREFERRED_NAMES:
                for idx, ha in enumerate(host_apis):
                    if (ha.get("name") == pref_name
                            and idx in groups
                            and idx not in ordered_ha_idxs):
                        ordered_ha_idxs.append(idx)
                        break
            for ha_idx in groups:
                if ha_idx not in ordered_ha_idxs:
                    ordered_ha_idxs.append(ha_idx)

            from PySide6.QtGui import QBrush, QColor as _QColor
            total = 0
            for ha_idx in ordered_ha_idxs:
                ha_name = host_apis[ha_idx].get(
                    "name", f"Host API {ha_idx}")
                # Section divider -- disabled, dim-colored.
                self._dev_combo.addItem(
                    f"───  {ha_name}  ───", self._SEP_SENTINEL)
                sep_idx = self._dev_combo.count() - 1
                model = self._dev_combo.model()
                item = model.item(sep_idx) if model is not None else None
                if item is not None:
                    item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                    item.setForeground(QBrush(_QColor("#7a90a8")))

                # Sort devices within a host-API group by name so
                # the same physical device appears in a stable
                # position across groups (eye scans down the column
                # and the matching "Speakers (Realtek)" row aligns).
                for dev_idx, dev in sorted(
                        groups[ha_idx],
                        key=lambda kv: kv[1]["name"].lower()):
                    rate = int(dev.get("default_samplerate", 0))
                    ch = dev.get("max_output_channels", 0)
                    # Drop the (host_api, …) suffix from each device
                    # row -- the section header already carries it.
                    # Leading two-space indent makes the visual
                    # hierarchy obvious in a flat QComboBox.
                    label = (f"  [{dev_idx:>3}] {dev['name']}   "
                             f"{ch}ch, {rate} Hz")
                    self._dev_combo.addItem(label, dev_idx)
                    total += 1

            if total == 0:
                self._dev_status.setText(
                    "⚠  sounddevice loaded but reports zero output "
                    "devices.  Check Windows Sound Control Panel — "
                    "ensure at least one Playback device is enabled.")
                self._dev_status.setStyleSheet(
                    "color: #ff8c00; font-weight: 700;")
            else:
                self._dev_status.setText(
                    f"✓  {total} device(s) across "
                    f"{len(ordered_ha_idxs)} host API(s)")
                self._dev_status.setStyleSheet(
                    "color: #6acb6a; font-weight: 700;")
        except Exception as e:
            self._dev_status.setText(
                f"✗  Device enumeration failed:\n        {e}")
            self._dev_status.setStyleSheet(
                "color: #ff4444; font-weight: 700; "
                "font-family: Consolas, monospace; "
                "white-space: pre-wrap;")

        self._sync_to_radio(self.radio.pc_audio_device_index)
        self._dev_combo.blockSignals(False)
        # Connect AFTER initial population so the sync above doesn't
        # trigger a spurious Radio.set call. Only connect once;
        # subsequent Refresh button clicks just rebuild the items.
        if not getattr(self, "_signal_connected", False):
            self._dev_combo.currentIndexChanged.connect(self._on_device_picked)
            self._signal_connected = True

    @_swallow_dead_widget
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
        # §15.16 device-list grouping: section-header rows carry
        # _SEP_SENTINEL as their userData and are flagged disabled,
        # so they don't normally fire this slot.  Belt + suspenders
        # in case a future Qt internal change starts forwarding
        # clicks on disabled items through the currentIndexChanged
        # signal -- just no-op and don't poke Radio.
        if device == self._SEP_SENTINEL:
            return
        # device is None for "Auto", or an int for a specific index.
        self.radio.set_pc_audio_device_index(device)

    # ── Mic input slots (v0.2 Phase 2 commit 6) ────────────────────

    def _refresh_pc_mic_subframe_enabled(self):
        """Dim the PC mic sub-section when HL2 jack is the active
        source -- the device + channel widgets stay editable so
        operators can pre-configure before switching, but visual
        affordance hints "these don't apply right now"."""
        enabled = self._mic_src_pc.isChecked()
        self._pc_mic_subframe.setEnabled(enabled)

    def _on_mic_src_changed(self, _checked: bool):
        """Operator clicked one of the source radio buttons."""
        new = "hl2_jack" if self._mic_src_hl2.isChecked() else "pc_soundcard"
        if new != self.radio.mic_source:
            self.radio.set_mic_source(new)
        self._refresh_pc_mic_subframe_enabled()

    def _on_mic_channel_changed(self, checked: bool):
        """One of the L / R / BOTH radio buttons toggled.  Fires
        twice per click (off-edge + on-edge); skip the off-edge."""
        if not checked:
            return
        if self._mic_ch_l.isChecked():
            new = "L"
        elif self._mic_ch_r.isChecked():
            new = "R"
        else:
            new = "BOTH"
        if new != self.radio.pc_mic_channel:
            self.radio.set_pc_mic_channel(new)

    def _on_mic_device_changed(self, combo_idx: int):
        """Operator picked a different input device."""
        device = self._mic_dev_combo.itemData(combo_idx)
        if device == self._SEP_SENTINEL:
            return
        # device is None for "Auto" (host-API default), or an int
        # for a specific device index.
        if device != self.radio.pc_mic_device:
            self.radio.set_pc_mic_device(device)

    def _populate_mic_devices(self):
        """Enumerate PortAudio INPUT devices via sounddevice and
        populate the mic-source device picker.  Same grouping logic
        as _populate_devices (the output side, §15.16) but filtered
        to devices that report ``max_input_channels > 0``.

        Falls back gracefully if sounddevice import fails -- the
        list shows just "Auto" and operator can still use the HL2
        mic jack path.
        """
        self._mic_dev_combo.blockSignals(True)
        self._mic_dev_combo.clear()
        self._mic_dev_combo.addItem(
            "Auto  (host-API default input device)", None)

        try:
            import sounddevice as sd
        except Exception as exc:
            self._mic_dev_combo.addItem(
                f"(sounddevice unavailable: {exc})", None)
            self._mic_dev_combo.blockSignals(False)
            return

        try:
            devices = list(sd.query_devices())
            host_apis = list(sd.query_hostapis())
        except Exception as exc:
            self._mic_dev_combo.addItem(
                f"(device query failed: {exc})", None)
            self._mic_dev_combo.blockSignals(False)
            return

        # Group by host API, same convention as §15.16 output device
        # grouping.  Only show devices that have input channels.
        input_devs_by_api: dict[int, list[tuple[int, dict]]] = {}
        for idx, d in enumerate(devices):
            if d.get("max_input_channels", 0) <= 0:
                continue
            hostapi_idx = d.get("hostapi", -1)
            input_devs_by_api.setdefault(hostapi_idx, []).append((idx, d))

        # Preferred order matches the output-side §15.16 list.
        _PREFERRED = (
            "Windows WASAPI", "Windows WDM-KS",
            "Windows DirectSound", "MME", "ASIO",
        )
        ordered_apis: list[int] = []
        seen = set()
        for name in _PREFERRED:
            for api_idx, api_info in enumerate(host_apis):
                if api_info.get("name") == name and api_idx in input_devs_by_api:
                    ordered_apis.append(api_idx)
                    seen.add(api_idx)
                    break
        # Catch any remaining APIs not in the preferred list
        for api_idx in input_devs_by_api:
            if api_idx not in seen:
                ordered_apis.append(api_idx)

        total = 0
        for api_idx in ordered_apis:
            api_name = (host_apis[api_idx]["name"]
                        if 0 <= api_idx < len(host_apis) else "Unknown")
            # Section divider (non-selectable header)
            hdr_idx = self._mic_dev_combo.count()
            self._mic_dev_combo.addItem(f"───  {api_name}  ───",
                                         self._SEP_SENTINEL)
            from PySide6.QtCore import Qt as _Qt
            # Make header non-selectable
            model = self._mic_dev_combo.model()
            item = model.item(hdr_idx)
            if item is not None:
                item.setFlags(item.flags() & ~_Qt.ItemIsEnabled)
                from PySide6.QtGui import QColor as _QC
                item.setForeground(_QC("#7a90a8"))
            # Actual device entries, sorted alphabetically within the
            # API group
            sorted_devs = sorted(input_devs_by_api[api_idx],
                                  key=lambda t: t[1].get("name", ""))
            for idx, d in sorted_devs:
                label = (f"[{idx}] {d.get('name', '?')}  "
                         f"({d.get('max_input_channels', 0)}ch)")
                self._mic_dev_combo.addItem(label, idx)
                total += 1
                # If this matches the operator's stored choice, select
                # it.
                if (self.radio.pc_mic_device is not None
                        and idx == self.radio.pc_mic_device):
                    self._mic_dev_combo.setCurrentIndex(
                        self._mic_dev_combo.count() - 1)

        self._mic_dev_combo.blockSignals(False)
        if total == 0:
            # No input devices found -- HL2-only setup, or PortAudio
            # mis-configured.  Combo still has "Auto" as the only
            # entry; operator's set_pc_mic_device defaults to None
            # which is the right answer.
            pass

    # Radio -> UI sync slots ────────────────────────────────────────

    def _on_radio_mic_source_changed(self, source: str):
        """Mic source flipped from elsewhere (autoload, future CAT/
        TCI) -- mirror UI without re-firing."""
        target_hl2 = (source == "hl2_jack")
        # Block both buttons' signals so toggling doesn't cascade
        for btn in (self._mic_src_hl2, self._mic_src_pc):
            btn.blockSignals(True)
        try:
            self._mic_src_hl2.setChecked(target_hl2)
            self._mic_src_pc.setChecked(not target_hl2)
        finally:
            for btn in (self._mic_src_hl2, self._mic_src_pc):
                btn.blockSignals(False)
        self._refresh_pc_mic_subframe_enabled()

    def _on_radio_pc_mic_device_changed(self, device):
        """Device changed elsewhere -- mirror combo selection."""
        target_idx = -1
        for i in range(self._mic_dev_combo.count()):
            if self._mic_dev_combo.itemData(i) == device:
                target_idx = i
                break
        if target_idx >= 0 and target_idx != self._mic_dev_combo.currentIndex():
            self._mic_dev_combo.blockSignals(True)
            try:
                self._mic_dev_combo.setCurrentIndex(target_idx)
            finally:
                self._mic_dev_combo.blockSignals(False)

    def _on_radio_pc_mic_channel_changed(self, channel: str):
        """Channel changed elsewhere -- mirror radio button."""
        target = {
            "L": self._mic_ch_l,
            "R": self._mic_ch_r,
            "BOTH": self._mic_ch_both,
        }.get(channel.upper())
        if target is None or target.isChecked():
            return
        for btn in (self._mic_ch_l, self._mic_ch_r, self._mic_ch_both):
            btn.blockSignals(True)
        try:
            target.setChecked(True)
        finally:
            for btn in (self._mic_ch_l, self._mic_ch_r, self._mic_ch_both):
                btn.blockSignals(False)

    # NOTE: Audio Leveler section slot implementations
    # (_on_lev_radio_toggled, _on_lev_thr_slider, _on_lev_ratio_slider,
    # _on_lev_makeup_slider, _on_lev_profile_signal, _on_lev_thr_signal,
    # _on_lev_ratio_signal, _on_lev_makeup_signal,
    # _update_lev_sliders_enabled) removed in Phase 4 of legacy-DSP
    # cleanup along with the Audio Leveler UI section.


class TxSettingsTab(QWidget):
    """Transmit configuration — the Settings home for everything
    that shapes or governs the transmit path.

    v0.2.0 Phase 3 commit 3.4 ships the TX Power & Drive section
    only.  The remaining sections land WITH their behavior in
    later sub-releases (no inert UI): they are documented insertion
    anchors below, not empty group boxes, so the tab grows in a
    fixed order without a reorganise.
    """

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        v = QVBoxLayout(self)

        # ── TX Power & Drive ────────────────────────────────────────
        pwr_grp = QGroupBox("TX Power & Drive")
        pwr_v = QVBoxLayout(pwr_grp)
        row = QHBoxLayout()
        self.tx_drive_stepper = StepperReadout(
            "TX Drive", 0, 100, step=7, shift_step=20, unit="%",
            initial=int(radio.tx_power_pct),
            caption_width=70, value_width=56)
        self.tx_drive_stepper.setToolTip(
            "Transmit output level, 0–100 %.  Maps onto the "
            "transmitter's stepped gain attenuator (quantised to "
            "the hardware's discrete steps).  Shares state with "
            "the TX panel's drive control — changing either moves "
            "both.  Set BEFORE keying into an antenna.")
        self.tx_drive_stepper.valueChanged.connect(
            self._on_tx_drive_changed)
        row.addWidget(self.tx_drive_stepper)
        row.addStretch(1)
        pwr_v.addLayout(row)
        v.addWidget(pwr_grp)

        # Bidirectional sync with the TX panel via the shared Radio
        # setter/signal; the blockSignals guard breaks the loop.
        self.radio.tx_power_pct_changed.connect(
            self._on_radio_tx_power_changed)

        # ── TX Safety (§15.20) — continuous-keydown timeout ─────────
        saf_grp = QGroupBox("TX Safety")
        saf_v = QVBoxLayout(saf_grp)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Stop transmit after"))
        self.tx_timeout_spin = QSpinBox()
        self.tx_timeout_spin.setRange(1, 20)
        self.tx_timeout_spin.setSuffix(" min")
        self.tx_timeout_spin.setValue(
            max(1, round(radio.tx_timeout_seconds / 60)))
        self.tx_timeout_spin.setToolTip(
            "Auto-return to receive after this many minutes of "
            "continuous keydown.  Catches a stuck PTT / falling "
            "asleep keyed / a software latch — it is not meant to "
            "interrupt a normal QSO (raise it, or tick Bypass, for "
            "long AM ragchews or slow CW).")
        self.tx_timeout_spin.valueChanged.connect(
            self._on_tx_timeout_spin)
        srow.addWidget(self.tx_timeout_spin)
        srow.addStretch(1)
        saf_v.addLayout(srow)
        self.tx_timeout_bypass_chk = QCheckBox(
            "Bypass timeout (long AM ragchews, slow CW, etc.)")
        self.tx_timeout_bypass_chk.setChecked(radio.tx_timeout_bypass)
        self.tx_timeout_bypass_chk.setToolTip(
            "When ticked, the safety timeout never arms.  Use only "
            "if you routinely hold a long continuous carrier and "
            "accept the responsibility for unkeying yourself.")
        self.tx_timeout_bypass_chk.toggled.connect(
            self._on_tx_timeout_bypass_chk)
        saf_v.addWidget(self.tx_timeout_bypass_chk)
        # Spin is meaningless while bypassed -> disable it.
        self.tx_timeout_spin.setEnabled(not radio.tx_timeout_bypass)
        v.addWidget(saf_grp)
        self.radio.tx_timeout_seconds_changed.connect(
            self._on_radio_tx_timeout_seconds)
        self.radio.tx_timeout_bypass_changed.connect(
            self._on_radio_tx_timeout_bypass)

        # ── Advanced — PA bias enable (§15.26 PART C) ───────────────
        adv_grp = QGroupBox("Advanced")
        adv_v = QVBoxLayout(adv_grp)
        self.pa_enable_chk = QCheckBox(
            "Enable PA  (transmit power amplifier bias)")
        self.pa_enable_chk.setChecked(radio.pa_enabled)
        _apollo = bool(getattr(
            radio.capabilities, "pa_enable_uses_apollo_i2c", False))
        _tip = (
            "Arms the transmit power amplifier.  DEFAULT OFF — with "
            "it off, keying MOX produces NO RF (safe for bench / "
            "dummy-load setup).  Tick it ONLY when you intend to "
            "transmit real power and have a dummy load or antenna "
            "connected.\n\n"
            "A safety stand-down (TX timeout / forced release) "
            "automatically disarms this — you re-tick it "
            "deliberately to transmit again.")
        if _apollo:
            _tip += (
                "\n\nNOTE (this hardware): PA enable is dual-path — "
                "some HL2 community-gateware variants also gate the "
                "PA through an Apollo-tuner control this switch does "
                "NOT drive, so on those gateware builds the PA may "
                "not fully key from this switch alone.  If you tick "
                "this and get no power, that gateware path is the "
                "reason (a separate change handles it).")
        self.pa_enable_chk.setToolTip(_tip)
        self.pa_enable_chk.toggled.connect(self._on_pa_enable_chk)
        adv_v.addWidget(self.pa_enable_chk)
        v.addWidget(adv_grp)
        self.radio.pa_enabled_changed.connect(
            self._on_radio_pa_enabled)

        # ── TR Sequencing (§15.26 Commit B) ─────────────────────────
        tr_grp = QGroupBox("TR Sequencing (ms)")
        tr_form = QFormLayout(tr_grp)
        d = radio.tr_delays
        self._tr_spins = {}
        # (key, label, min, tooltip)
        _tr_rows = [
            ("rf", "RF delay",
             TrSequencing.RF_DELAY_FLOOR_MS,
             "Gap between asserting transmit and applying RF.\n\n"
             "⚠ AMPLIFIER PROTECTION: this delay lets the T/R "
             "sequencing settle so an external linear is never "
             "hot-switched (RF into mid-transition relays = "
             "destroyed amp).  Hard floor enforced — you may "
             "RAISE it for slow relays / big amps, never lower "
             "it below the floor."),
            ("mox", "MOX delay", 0,
             "Gap after the transmit down-ramp before the MOX "
             "bit clears (lets in-flight transmit samples clear)."),
            ("ptt_out", "PTT-out / RX-settle", 0,
             "Hardware T/R settle after the MOX bit clears, "
             "before the receiver is restarted.  Lower values "
             "may reintroduce an un-key transient — raise if you "
             "hear one."),
            ("space_mox", "Space-MOX (CW)", 0,
             "CW inter-element hold (active when CW TX lands, "
             "v0.2.2)."),
            ("key_up", "Key-up (CW)", 0,
             "CW keyer hang (active when CW TX lands, v0.2.2)."),
        ]
        for key, label, vmin, tip in _tr_rows:
            sp = QSpinBox()
            sp.setRange(int(vmin), 2000)
            sp.setSuffix(" ms")
            sp.setValue(int(d.get(key, vmin)))
            sp.setToolTip(tip)
            sp.valueChanged.connect(
                lambda v, k=key: self.radio.set_tr_delay(k, int(v)))
            self._tr_spins[key] = sp
            tr_form.addRow(label, sp)
        v.addWidget(tr_grp)
        self.radio.tr_sequencing_changed.connect(
            self._on_radio_tr_sequencing)

        # ── Future sections (land WITH behavior — no inert UI) ──────
        # Ordered insertion anchors so the tab grows without reorg:
        #   • Advanced (above) still to gain: gateware
        #     reset-on-link-loss opt-in (default OFF) + the
        #     hardware-PTT-input opt-in (default OFF) — added to the
        #     same box when those land (foot-switch work / §15.20
        #     reset_on_disconnect exposure).
        #   • v0.2.1 — "Speech Processing": parametric EQ, multiband
        #     combinator, tube-plating, formant/sibilance, de-esser,
        #     compressor position, mic auto-AGC + mic gain.
        #   • v0.2.3 — "Voice Keyer & Profiles": message memory,
        #     operator-curated TX profile picker, VOX, monitor
        #     output device.
        # (Mic *gain* deferred: no Radio mic-gain setter exists yet
        #  — it arrives with the v0.2.1 speech-processing chain.)
        v.addStretch(1)

    # ── operator → Radio ────────────────────────────────────────────
    def _on_tx_drive_changed(self, pct: float) -> None:
        self.radio.set_tx_power_pct(int(round(pct)))

    # ── Radio → display (guarded against re-fire) ───────────────────
    def _on_radio_tx_power_changed(self, pct: int) -> None:
        if int(self.tx_drive_stepper.value()) == int(pct):
            return
        self.tx_drive_stepper.blockSignals(True)
        self.tx_drive_stepper.setValue(int(pct))
        self.tx_drive_stepper.blockSignals(False)

    # ── TX-safety-timeout sync (§15.20) ─────────────────────────────
    def _on_tx_timeout_spin(self, minutes: int) -> None:
        self.radio.set_tx_timeout_seconds(int(minutes) * 60)

    def _on_tx_timeout_bypass_chk(self, on: bool) -> None:
        self.radio.set_tx_timeout_bypass(bool(on))

    def _on_radio_tx_timeout_seconds(self, seconds: int) -> None:
        mins = max(1, round(int(seconds) / 60))
        if self.tx_timeout_spin.value() == mins:
            return
        self.tx_timeout_spin.blockSignals(True)
        self.tx_timeout_spin.setValue(mins)
        self.tx_timeout_spin.blockSignals(False)

    def _on_radio_tx_timeout_bypass(self, on: bool) -> None:
        on = bool(on)
        if self.tx_timeout_bypass_chk.isChecked() != on:
            self.tx_timeout_bypass_chk.blockSignals(True)
            self.tx_timeout_bypass_chk.setChecked(on)
            self.tx_timeout_bypass_chk.blockSignals(False)
        # Spin is meaningless while bypassed.
        self.tx_timeout_spin.setEnabled(not on)

    # ── PA-enable sync (§15.26 PART C) ──────────────────────────────
    def _on_pa_enable_chk(self, on: bool) -> None:
        self.radio.set_pa_enabled(bool(on))

    def _on_radio_pa_enabled(self, on: bool) -> None:
        on = bool(on)
        if self.pa_enable_chk.isChecked() == on:
            return
        self.pa_enable_chk.blockSignals(True)
        self.pa_enable_chk.setChecked(on)
        self.pa_enable_chk.blockSignals(False)

    def _on_radio_tr_sequencing(self, d: dict) -> None:
        # Radio → spinboxes (e.g. rf_delay was floor-clamped on a
        # too-low entry, or autoload).  Guarded against re-fire.
        for key, sp in self._tr_spins.items():
            v = int(d.get(key, sp.value()))
            if sp.value() == v:
                continue
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)


class VisualsSettingsTab(QWidget):
    """Spectrum + waterfall display options.

    Eight grouped sections distributed across two columns:

    1. **Graphics backend** — Software / OpenGL / Vulkan radio
       buttons.  Read at import time by gfx.py, so changes need
       a restart — we surface this clearly in a help label and
       persist to QSettings.
    2. **Waterfall palette + watermark + meteors + grid** —
       live palette combo (each palette is a 256-entry LUT in
       palettes.py), background watermark toggle, meteor-shower
       overlay toggles, spectrum grid spacing.
    3. **Signal range + auto-scale + peak markers + smoothing**
       — four sliders (spectrum / waterfall min + max), auto-
       scale toggles, peak-marker config, FFT smoothing factor.
    4. **Colors** — picker grid for trace / grid / fill / peak
       colors.
    5. **Spectrum cal + S-meter cal** — calibration offsets.
    6. **Update rates + zoom** — FFT FPS, panadapter / waterfall
       refresh, default zoom level.

    Radio clamps dB-range spans to ≥ 3 dB so the trace can't
    collapse to a flat line.
    """

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio

        # Two-column layout. The Visuals tab has eight grouped sections
        # (graphics backend, palette, signal range + auto-scale, peak
        # markers, colors, spectrum cal, S-meter cal, update rates) —
        # stacking them in a single column made the dialog unreadably
        # tall even on a 27" monitor. Side-by-side columns keep
        # everything visible without scrolling.
        outer = QHBoxLayout(self)
        outer.setSpacing(12)
        col_left = QVBoxLayout()
        col_right = QVBoxLayout()
        col_left.setSpacing(8)
        col_right.setSpacing(8)
        outer.addLayout(col_left, 1)
        outer.addLayout(col_right, 1)
        # Backwards-compat alias so all the existing
        # `v.addWidget(grp_xxx)` calls below land in the LEFT column
        # by default. Sections that should go in the right column
        # are reassigned later (see "right-column reassignments"
        # at the end of this method).
        v = col_left

        # ── Graphics backend ──────────────────────────────────────
        from lyra.ui.gfx import (
            ACTIVE_BACKEND, BACKEND_LABELS, BACKEND_SOFTWARE,
            BACKEND_OPENGL, BACKEND_GPU_OPENGL, BACKEND_VULKAN,
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
            (BACKEND_GPU_OPENGL,
             "GPU panadapter (BETA) — full custom OpenGL pipeline for "
             "the trace + waterfall. Vertex-buffer trace + texture-"
             "streaming waterfall via custom shaders. Fastest path. "
             "Currently missing some QPainter overlays (notches, spots, "
             "band plan, peak markers) — they'll be added in successive "
             "releases. Default stays on the QPainter renderers until "
             "this option has tester time across many GPU configurations."),
            (BACKEND_VULKAN,
             "Vulkan — placeholder for a future custom-pipeline "
             "renderer. Reserved if QRhi PySide6 bindings mature or a "
             "real performance need surfaces. Not implemented today."),
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

        # Lyra constellation watermark toggle. Lives in the same
        # group as the palette since both are panadapter visuals.
        self.lyra_const_check = QCheckBox(
            "Show Lyra constellation behind panadapter")
        self.lyra_const_check.setChecked(
            bool(self.radio.show_lyra_constellation))
        self.lyra_const_check.setToolTip(
            "Stylized Lyra / lyre constellation image rendered as a "
            "faint watermark behind the spectrum trace. Additive blend "
            "so only the bright stars and lyre lines show through; the "
            "dark background of the source image disappears into the "
            "black panadapter."
        )
        self.lyra_const_check.toggled.connect(
            self.radio.set_show_lyra_constellation)
        gp.addWidget(self.lyra_const_check, 1, 0, 1, 2, Qt.AlignLeft)

        self.lyra_meteors_check = QCheckBox(
            "Occasional meteors across panadapter")
        self.lyra_meteors_check.setChecked(
            bool(self.radio.show_lyra_meteors))
        self.lyra_meteors_check.setToolTip(
            "Subtle ambient meteor streaks that pass through the "
            "panadapter at random intervals (15–50 seconds apart, "
            "one at a time, ~0.5–0.8 s each). Independent of the "
            "constellation watermark — turn either on or off "
            "without affecting the other."
        )
        self.lyra_meteors_check.toggled.connect(
            self.radio.set_show_lyra_meteors)
        gp.addWidget(self.lyra_meteors_check, 2, 0, 1, 2, Qt.AlignLeft)

        # Grid line toggle — 9×9 divisions on the panadapter. Some
        # operators rely on them as a visual reference; others find
        # them noisy. Default ON.
        self.spectrum_grid_check = QCheckBox(
            "Show panadapter grid (9×9 divisions)")
        self.spectrum_grid_check.setChecked(
            bool(self.radio.show_spectrum_grid))
        self.spectrum_grid_check.setToolTip(
            "When on (default): a faint dark-blue grid overlays the\n"
            "panadapter, dividing it into 9 horizontal and 9 vertical\n"
            "sections. Useful for eyeballing dB and frequency.\n\n"
            "When off: clean trace-only view. The dB scale labels on\n"
            "the right edge and frequency labels on the bottom remain\n"
            "visible — only the dotted grid lines disappear."
        )
        self.spectrum_grid_check.toggled.connect(
            self.radio.set_show_spectrum_grid)
        gp.addWidget(self.spectrum_grid_check, 3, 0, 1, 2, Qt.AlignLeft)

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

        # Separate handlers — earlier the four sliders all fed
        # _on_db_changed which pushed BOTH spectrum AND waterfall
        # ranges to Radio every time. Side-effect: dragging a
        # waterfall slider tripped Radio.set_spectrum_db_range with
        # from_user=True, which DISABLES Spectrum auto-scale (the
        # auto-flag-off rule treats any user spectrum-range change
        # as a deliberate manual override). Now the waterfall
        # sliders only touch the waterfall range, leaving the
        # spectrum auto-flag alone.
        self._spec_min.valueChanged.connect(self._on_spec_db_changed)
        self._spec_max.valueChanged.connect(self._on_spec_db_changed)
        self._wf_min.valueChanged.connect(self._on_wf_db_changed)
        self._wf_max.valueChanged.connect(self._on_wf_db_changed)

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
            "Per-edge locks (drag the panadapter right-edge to set):\n"
            "  • Drag the FLOOR → auto stops moving it; your noise\n"
            "    space stays where you put it.\n"
            "  • Drag the CEILING → auto won't fall below it; the\n"
            "    ceiling can still RISE if a strong signal arrives\n"
            "    (so signals are never squeezed off-screen).\n\n"
            "Locks are saved per-band — switching bands restores\n"
            "whichever edges you'd locked there.\n\n"
            "To clear locks: right-click the dB scale on the\n"
            "panadapter → 'Reset display range'.")
        self.auto_scale_chk.toggled.connect(
            self.radio.set_spectrum_auto_scale)
        # Keep checkbox in sync if Radio turns it off (manual drag)
        radio.spectrum_auto_scale_changed.connect(
            lambda on: _safe_mirror(
                self.auto_scale_chk, "isChecked", "setChecked", bool(on)))
        gd.addWidget(self.auto_scale_chk, 10, 0, 1, 3, Qt.AlignLeft)

        # Independent waterfall auto-scale toggle. Default ON (waterfall
        # tracks the spectrum auto-scale). When OFF the waterfall stays
        # at whatever min/max sliders the operator set, regardless of
        # band activity — useful for the 'fixed darker waterfall so
        # signals pop' look. Sits right below the spectrum auto-scale
        # so both toggles are visually grouped.
        self.wf_auto_scale_chk = QCheckBox(
            "Waterfall auto-range follows spectrum")
        self.wf_auto_scale_chk.setChecked(radio.waterfall_auto_scale)
        self.wf_auto_scale_chk.setToolTip(
            "When ON (default): the waterfall's dB range tracks the\n"
            "spectrum auto-scale, so the heatmap fits each band's\n"
            "actual signal levels.\n\n"
            "When OFF: the waterfall stays at the Waterfall min/max\n"
            "sliders above. Useful if you prefer a fixed darker\n"
            "waterfall (set Waterfall max higher than your strongest\n"
            "regular signal) so transient peaks pop visually."
        )
        self.wf_auto_scale_chk.toggled.connect(
            self.radio.set_waterfall_auto_scale)
        radio.waterfall_auto_scale_changed.connect(
            lambda on: _safe_mirror(
                self.wf_auto_scale_chk, "isChecked", "setChecked", bool(on)))
        gd.addWidget(self.wf_auto_scale_chk, 11, 0, 1, 3, Qt.AlignLeft)

        # Spectrum fill toggle (operator request 2026-05-09) —
        # controls whether the spectrum trace gets a gradient-fill
        # area below the curve (the legacy always-on behavior).
        # Color picker for it lives in the Colors group below.
        self.spec_fill_chk = QCheckBox(
            "Fill area under spectrum trace (gradient)")
        self.spec_fill_chk.setChecked(radio.spectrum_fill_enabled)
        self.spec_fill_chk.setToolTip(
            "When ON: the spectrum trace gets a translucent gradient\n"
            "fill below the curve (alpha 100→10 top-to-bottom),\n"
            "matching the legacy Lyra look.\n\n"
            "When OFF: only the trace line is drawn — useful for a\n"
            "cleaner 'bare line' look or to see content behind the\n"
            "spectrum (e.g. landmark triangles, peak markers in\n"
            "Live mode).\n\n"
            "Color is configurable via the 'Spectrum fill' field in\n"
            "the Colors group below — empty = derive from the trace\n"
            "color.")
        self.spec_fill_chk.toggled.connect(
            self.radio.set_spectrum_fill_enabled)
        # Row 14 — sits below the smoothing slider (row 13) so the
        # appearance-toggle group reads top-to-bottom: NF line / peak
        # markers / smoothing / fill.  Row 4 was already used by the
        # "Reset to defaults" button.
        gd.addWidget(self.spec_fill_chk, 14, 0, 1, 3, Qt.AlignLeft)

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
            ("_fill_",  "Spectrum fill",
             radio.spectrum_fill_color,  "#5ec8ff",
             lambda hx: self.radio.set_spectrum_fill_color(hx)),
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
        # 140 px (was 120): the 120 px width was clipping the leading
        # "C" on Windows because Qt centers QPushButton text and trims
        # both edges when the rendered width exceeds the box.  Operator-
        # reported 2026-05-09.
        custom_btn.setFixedWidth(140)
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

        # → right column (Colors is one of the taller groups; pairing
        # it with Signal range on the right balances the two columns)
        col_right.addWidget(grp_col)

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

        # Spectrum smoothing — display-only EWMA filter on the trace.
        # Off by default (raw FFT). Strength 1..10 maps to alpha
        # ~0.91..0.09; higher = smoother / slower response. Useful
        # for reading weak signals through a noisy floor without
        # touching the audio DSP.
        self.smooth_chk = QCheckBox("Smooth spectrum trace")
        self.smooth_chk.setChecked(radio.spectrum_smoothing_enabled)
        self.smooth_chk.setToolTip(
            "Display-only EWMA averaging applied to the spectrum "
            "trace before drawing. Calms a jittery noise floor and "
            "makes weak signals easier to spot. Does NOT affect "
            "audio or DSP.")
        self.smooth_chk.toggled.connect(
            self.radio.set_spectrum_smoothing_enabled)
        # Rows 10/11 already host auto_scale_chk and wf_auto_scale_chk.
        # Place smoothing at rows 12/13 so the QGridLayout doesn't
        # stack two widgets at the same coordinates (which produced
        # visually-overlapping labels and made toggle state unreliable).
        gd.addWidget(self.smooth_chk, 12, 0, 1, 3, Qt.AlignLeft)

        gd.addWidget(QLabel("Strength"), 13, 0)
        self.smooth_slider = SteppedSlider(Qt.Horizontal)
        self.smooth_slider.setRange(1, 10)
        self.smooth_slider.setValue(int(radio.spectrum_smoothing_strength))
        self.smooth_slider.setFixedWidth(240)
        # Visible tick marks per detent — same UX pattern as the FPS /
        # WF sliders above.
        self.smooth_slider.setTickPosition(QSlider.TicksBelow)
        self.smooth_slider.setTickInterval(1)
        self.smooth_slider.setSingleStep(1)
        self.smooth_slider.setPageStep(1)
        self.smooth_slider.setToolTip(
            "Smoothing strength. 1 = barely averaged (fast). "
            "10 = heavily averaged (slow but very clean).")
        self.smooth_slider.valueChanged.connect(
            self._on_smooth_strength_changed)
        gd.addWidget(self.smooth_slider, 13, 1)
        self.smooth_lbl = QLabel(f"{int(radio.spectrum_smoothing_strength)}")
        self.smooth_lbl.setFixedWidth(80)
        self.smooth_lbl.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gd.addWidget(self.smooth_lbl, 13, 2, Qt.AlignLeft)

        # → right column (Signal range is one of the taller groups)
        col_right.addWidget(grp_db)

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
        _force_wrap_height(cal_help)
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
        _force_wrap_height(smeter_help)
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

        # Spectrum FPS — step-list slider. See SPECTRUM_FPS_STEPS in
        # panels.py for the full ladder. Hand-curated so each detent
        # is a useful value (5, 10, 15, 20, 25, 30, 40, 50, 60, 75,
        # 90, 120 fps) rather than every-integer-in-a-range.
        gr.addWidget(QLabel("Spectrum rate"), 1, 0)
        self.fps_slider = SteppedSlider(Qt.Horizontal)
        self.fps_slider.setRange(0, len(SPECTRUM_FPS_STEPS) - 1)
        self.fps_slider.setValue(fps_to_slider_position(radio.spectrum_fps))
        self.fps_slider.setFixedWidth(240)
        # Visible tick marks at each detent + 1-per-click stepping so the
        # operator both sees and feels the discrete steps.
        self.fps_slider.setTickPosition(QSlider.TicksBelow)
        self.fps_slider.setTickInterval(1)
        self.fps_slider.setSingleStep(1)
        self.fps_slider.setPageStep(1)
        self.fps_slider.setToolTip(
            "How fast the spectrum repaints. Lower = less CPU / GPU "
            "load, laggier trace. Higher = smoother but more work. "
            "40 fps is the default (a common SDR-client convention). At 60 "
            "fps and above, enable 'Smooth spectrum trace' below for "
            "the cleanest look.")
        gr.addWidget(self.fps_slider, 1, 1)
        self.fps_label = QLabel(f"{radio.spectrum_fps} fps")
        self.fps_label.setFixedWidth(80)
        self.fps_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gr.addWidget(self.fps_label, 1, 2, Qt.AlignLeft)
        # FPS slider — sliderPressed/Released pattern. See panels.py
        # ViewPanel for the rationale. Commits ONLY on release (or
        # on debounce expiration for click-jumps / keyboard).
        from PySide6.QtCore import QTimer as _QTimer
        self._fps_dragging = False
        self._fps_debounce = _QTimer(self)
        self._fps_debounce.setSingleShot(True)
        self._fps_debounce.setInterval(75)
        # Slider value is a step index; convert to FPS via the helper.
        self._fps_debounce.timeout.connect(
            lambda: self.radio.set_spectrum_fps(
                fps_from_slider_position(self.fps_slider.value())))
        self.fps_slider.sliderPressed.connect(
            lambda: (setattr(self, '_fps_dragging', True),
                     self._fps_debounce.stop()))
        self.fps_slider.sliderReleased.connect(self._on_fps_slider_release)
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
        self.wf_slider = SteppedSlider(Qt.Horizontal)
        self.wf_slider.setRange(0, len(WATERFALL_SPEED_STEPS) - 1)
        self.wf_slider.setInvertedAppearance(True)  # right = faster
        self.wf_slider.setValue(wf_to_slider_position(
            radio.waterfall_divider, radio.waterfall_multiplier))
        self.wf_slider.setFixedWidth(240)
        # Visible tick marks per detent + 1-per-click stepping.
        self.wf_slider.setTickPosition(QSlider.TicksBelow)
        self.wf_slider.setTickInterval(1)
        self.wf_slider.setSingleStep(1)
        self.wf_slider.setPageStep(1)
        self.wf_slider.setToolTip(
            "How fast the waterfall scrolls. Right end = up to 30× "
            "visual speed (rows linearly interpolated between FFTs — "
            "useful for digital-mode hunting at low spec rates). "
            "Middle = one row per FFT (1:1 with spectrum FPS). Left "
            "end = slow crawl (1 row per 20 FFTs; long time-history "
            "on screen at once for QSO chasing or multi-band scanning).")
        gr.addWidget(self.wf_slider, 2, 1)
        self.wf_label = QLabel(self._wf_label_text())
        self.wf_label.setFixedWidth(110)
        self.wf_label.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace;")
        gr.addWidget(self.wf_label, 2, 2, Qt.AlignLeft)
        # Plain debounce — operator confirmed waterfall slider works
        # fine with this; over-engineering with sliderPressed/Released
        # was unnecessary.
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
        # Right column gets a stretch too so its groups don't expand
        # vertically to fill the column height.
        col_right.addStretch(1)

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

    def _on_spec_db_changed(self):
        """Spectrum min/max slider drag — only push the SPECTRUM
        range to Radio. Touching this is a deliberate manual override,
        so set_spectrum_db_range(from_user=True) correctly disables
        auto-scale here.
        """
        sp_lo, sp_hi = self._spec_min.value(), self._spec_max.value()
        self._spec_min_lbl.setText(f"{sp_lo:+d} dBFS")
        self._spec_max_lbl.setText(f"{sp_hi:+d} dBFS")
        self.radio.set_spectrum_db_range(sp_lo, sp_hi)

    def _on_wf_db_changed(self):
        """Waterfall min/max slider drag — only push the WATERFALL
        range. Critically, does NOT touch set_spectrum_db_range, so
        the operator's spectrum auto-scale setting stays intact when
        they tweak the waterfall display range."""
        wf_lo, wf_hi = self._wf_min.value(), self._wf_max.value()
        self._wf_min_lbl.setText(f"{wf_lo:+d} dBFS")
        self._wf_max_lbl.setText(f"{wf_hi:+d} dBFS")
        self.radio.set_waterfall_db_range(wf_lo, wf_hi)

    # Backward-compat alias — anything that called the old combined
    # name still works (commits BOTH ranges as before).
    def _on_db_changed(self):
        self._on_spec_db_changed()
        self._on_wf_db_changed()

    def _on_cal_changed(self, val: int):
        """Cal slider drag — push to Radio + repaint label."""
        self._cal_lbl.setText(f"{val:+.1f} dB")
        self.radio.set_spectrum_cal_db(float(val))

    @_swallow_dead_widget
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

    @_swallow_dead_widget
    def _on_radio_smeter_cal_changed(self, db: float):
        """Radio.smeter_cal_db_changed — keep slider + label in sync."""
        target = int(round(db))
        if self._smeter_cal_slider.value() != target:
            self._smeter_cal_slider.blockSignals(True)
            self._smeter_cal_slider.setValue(target)
            self._smeter_cal_slider.blockSignals(False)
        self._smeter_cal_lbl.setText(f"{db:+.1f} dB")

    @_swallow_dead_widget
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

    @_swallow_dead_widget
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

    def _on_smooth_strength_changed(self, strength: int):
        self.smooth_lbl.setText(f"{int(strength)}")
        self.radio.set_spectrum_smoothing_strength(int(strength))

    def _reset_db_ranges(self):
        # Match Radio's pre-settings defaults.
        self._spec_min.setValue(-110)
        self._spec_max.setValue(-20)
        self._wf_min.setValue(-110)
        self._wf_max.setValue(-30)
        self._on_db_changed()

    # ── Update rates + zoom handlers ─────────────────────────────
    @_swallow_dead_widget
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

    def _on_fps_slider_release(self):
        """Mouse released — commit immediately (no debounce wait).
        Slider value is a step index; convert to FPS for the radio."""
        self._fps_dragging = False
        self._fps_debounce.stop()
        fps = fps_from_slider_position(self.fps_slider.value())
        self.radio.set_spectrum_fps(fps)

    def _on_fps_slider_drag(self, slider_pos: int):
        """valueChanged — refresh labels locally; commit only when
        operator releases the mouse (or via debounce on click-jump /
        keyboard, which don't fire press/release). Slider value is a
        step index; convert to FPS for display."""
        fps = fps_from_slider_position(slider_pos)
        self.fps_label.setText(f"{fps} fps")
        self.wf_label.setText(self._wf_label_text())
        if getattr(self, "_fps_dragging", False):
            return
        self._fps_debounce.start()

    # Backward-compat — anything that called _on_fps_changed by name
    # still works (commits immediately, no debounce).
    def _on_fps_changed(self, fps: int):
        self.fps_label.setText(f"{fps} fps")
        self.radio.set_spectrum_fps(fps)
        self.wf_label.setText(self._wf_label_text())

    @_swallow_dead_widget
    def _sync_fps_slider(self, fps: int):
        """Radio FPS changed elsewhere (front-panel slider, QSettings
        load, etc.) — mirror here without firing our own valueChanged.
        Snap arbitrary FPS values to the nearest step position."""
        target_pos = fps_to_slider_position(fps)
        if self.fps_slider.value() != target_pos:
            self.fps_slider.blockSignals(True)
            self.fps_slider.setValue(target_pos)
            self.fps_slider.blockSignals(False)
        self.fps_label.setText(f"{fps} fps")
        self.wf_label.setText(self._wf_label_text())

    # Slider encoding — delegates to the shared step-list helpers in
    # panels.py so the front-panel and Settings sliders can never
    # disagree about what each detent means.
    @staticmethod
    def _wf_slider_to_state(v: int) -> tuple[int, int]:
        return wf_from_slider_position(v)

    @staticmethod
    def _wf_state_to_slider(divider: int, multiplier: int) -> int:
        return wf_to_slider_position(divider, multiplier)

    def _wf_label_text(self) -> str:
        """rows/sec = fps × multiplier / divider. Accounts for the
        fast-mode multiplier so the readout agrees with what you
        actually see scrolling."""
        fps = self.radio.spectrum_fps
        div = max(1, self.radio.waterfall_divider)
        mult = max(1, self.radio.waterfall_multiplier)
        return f"{fps * mult / div:.1f} rows/s"

    def _on_wf_slider_drag_setting(self, _v: int):
        """Drag → refresh label + bump debounce timer."""
        self.wf_label.setText(self._wf_label_text())
        self._wf_debounce.start()

    def _commit_wf_value(self):
        """Commit the current waterfall slider value to Radio."""
        div, mult = self._wf_slider_to_state(self.wf_slider.value())
        self.radio.set_waterfall_divider(div)
        self.radio.set_waterfall_multiplier(mult)

    # Backward-compat — preserved for any code still calling by name.
    def _on_wf_slider_changed(self, v: int):
        div, mult = self._wf_slider_to_state(v)
        self.radio.set_waterfall_divider(div)
        self.radio.set_waterfall_multiplier(mult)
        self.wf_label.setText(self._wf_label_text())

    @_swallow_dead_widget
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


class NoiseSettingsTab(QWidget):
    """Noise toolkit settings.

    Operator-tunable knobs for the noise-toolkit features:
      * Captured Noise Profile — capture duration, gain
        smoothing (Phase 5b), FFT size (Phase 5c), staleness
        threshold, storage location, age-warning thresholds,
        profile manager + folder shortcuts.  v0.0.9.9 §14.6:
        full IQ-domain capture + apply pipeline is LIVE in
        WDSP mode.
      * Noise Blanker (NB) — profile picker (advisory) +
        threshold slider (advisory).  Live engine: WDSP NOB.
      * Auto Notch Filter (ANF) — profile picker (advisory) +
        μ slider (advisory).  Live engine: WDSP ANF.
      * All-Mode Squelch — master enable + threshold slider.
        Live engine: WDSP SSQL (SSB/CW/DIG) / FMSQ / AMSQ.

    Phase 7 (v0.0.9.6) removed the legacy NR2 + NR2 Gain Function
    + LMS-duplicate groups; their operator surface lives on the
    DSP+Audio panel (NR Mode 1-4 + AEPF + NPE for noise reduction;
    LMS button + strength slider for the line enhancer).
    """

    def __init__(self, radio):
        super().__init__()
        self.radio = radio

        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import (
            QFileDialog, QPushButton, QRadioButton, QButtonGroup,
            QSpinBox, QLineEdit, QGroupBox)
        self._QSettings = QSettings   # used by setter helpers below

        # Two-column layout (Phase 7 collapse from three columns):
        #   col_left   = Captured Noise Profile + All-Mode Squelch
        #   col_right  = NB + ANF
        # The middle column stays in the QHBoxLayout grid as a
        # placeholder for future expansion (e.g. when staleness
        # controls grow into their own group).  Reassignments
        # happen at the end of this method — see the column-
        # reassignment block.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        col_left = QVBoxLayout()
        col_middle = QVBoxLayout()
        col_right = QVBoxLayout()
        col_left.setSpacing(14)
        col_middle.setSpacing(14)
        col_right.setSpacing(14)
        outer.addLayout(col_left, 1)
        outer.addLayout(col_middle, 1)
        outer.addLayout(col_right, 1)
        # Backwards-compat alias — existing `v.addWidget(grp_xxx)`
        # calls below land in the LEFT column by default.  Groups
        # that belong in middle/right are reassigned at the end.
        v = col_left

        # ── Captured Noise Profile ──────────────────────────────
        grp_cap = QGroupBox("Captured Noise Profile")
        gv = QVBoxLayout(grp_cap)
        gv.setSpacing(8)

        # Multi-paragraph intro split across 3 separate QLabels.
        # `\n\n` paragraph breaks inside ONE wrapped QLabel can confuse
        # Qt's heightForWidth math when the column is space-tight
        # (visible artifact: wrapped lines render on top of each other
        # if the layout squeezes the label below its real height).
        # Three short labels each have a tractable single-paragraph
        # height that the QVBoxLayout always honors.
        intro_paras = [
            "Audacity-style noise capture.  Tune to a noise-only "
            "frequency or wait for a transmission gap, then click "
            "the 📷 Cap button on the DSP+Audio panel.  Lyra records "
            "the band's IQ-domain noise spectrum and saves it to a "
            "profile library you can name, organize, and reload "
            "across sessions.",
            "Profiles save to disk and persist across Lyra restarts.",
            "v0.0.9.9 (§14.6): the IQ-domain apply path is LIVE.  "
            "Toggling \"use captured\" on the DSP+Audio panel "
            "actually applies spectral subtraction now — operators "
            "hear the noise floor drop on bands where the profile "
            "matches.  Profiles captured pre-v0.0.9.9 use the "
            "legacy audio-domain format and are refused at load "
            "with a recapture hint.",
        ]
        for para in intro_paras:
            lbl = QLabel(para)
            lbl.setWordWrap(True)
            _force_wrap_height(lbl)
            lbl.setStyleSheet("color: #8a9aac;")
            gv.addWidget(lbl)

        # Capture duration slider — 1.0..5.0 sec, locked range.
        s = QSettings("N8SDR", "Lyra")
        cur_dur = float(s.value("noise/capture_duration_sec", 2.0,
                                type=float))
        cur_dur = max(1.0, min(5.0, cur_dur))   # clamp on load
        dur_row = QHBoxLayout()
        dur_row.addWidget(QLabel("Capture duration:"))
        self.dur_slider = QSlider(Qt.Horizontal)
        self.dur_slider.setRange(10, 50)   # tenths of a second
        self.dur_slider.setValue(int(round(cur_dur * 10)))
        self.dur_slider.setTickPosition(QSlider.TicksBelow)
        self.dur_slider.setTickInterval(10)
        self.dur_slider.setSingleStep(1)
        self.dur_slider.setPageStep(5)
        self.dur_label = QLabel(f"{cur_dur:.1f} s")
        self.dur_label.setMinimumWidth(60)
        self.dur_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700;")
        self.dur_slider.valueChanged.connect(self._on_duration_changed)
        dur_row.addWidget(self.dur_slider, 1)
        dur_row.addWidget(self.dur_label)
        gv.addLayout(dur_row)

        # ── §14.6 v0.0.9.9 Phase 5b: gain-smoothing slider ──
        # Temporal smoothing on the per-bin Wiener gain mask.
        # Reduces "watery" musical-noise character of pure
        # spectral subtraction.  Live-tunable: changes apply
        # instantly to the running engine.
        from lyra.dsp.captured_profile_iq import CapturedProfileIQ
        cur_smoothing = float(s.value(
            "noise/gain_smoothing",
            CapturedProfileIQ.DEFAULT_GAIN_SMOOTHING,
            type=float))
        cur_smoothing = max(0.0, min(0.95, cur_smoothing))
        smooth_row = QHBoxLayout()
        smooth_row.addWidget(QLabel("Gain smoothing:"))
        self.smooth_slider = QSlider(Qt.Horizontal)
        # Range 0..95 (integer steps, 0.01 resolution).  Slider
        # below 0.0 doesn't make sense; above 0.95 freezes the
        # mask audibly (signal onsets are missed).
        self.smooth_slider.setRange(0, 95)
        self.smooth_slider.setValue(int(round(cur_smoothing * 100)))
        self.smooth_slider.setTickPosition(QSlider.TicksBelow)
        self.smooth_slider.setTickInterval(20)
        self.smooth_slider.setSingleStep(5)
        self.smooth_slider.setPageStep(10)
        self.smooth_slider.setToolTip(
            "Temporal smoothing on the captured-profile gain mask "
            "(γ = 0.0..0.95).\n\n"
            "0.0 = no smoothing — instantaneous gain per frame "
            "(maximum responsiveness, maximum watery character)\n"
            "0.6 = ~10 ms time constant (default — good balance)\n"
            "0.8 = ~24 ms time constant (heavier smoothing, less "
            "watery, slightly slower onset response)\n"
            "0.95 = ~104 ms (very heavy — can blur fast signal "
            "onsets)\n\n"
            "Effective immediately — drag to A/B while listening.")
        self.smooth_label = QLabel(f"{cur_smoothing:.2f}")
        self.smooth_label.setMinimumWidth(60)
        self.smooth_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700;")
        self.smooth_slider.valueChanged.connect(
            self._on_gain_smoothing_changed)
        smooth_row.addWidget(self.smooth_slider, 1)
        smooth_row.addWidget(self.smooth_label)
        gv.addLayout(smooth_row)

        # ── §14.6 v0.0.9.9 Phase 5c: FFT-size dropdown ──
        # IQ analysis FFT size for new captures.  Bigger = finer
        # bin resolution (better noise discrimination) but more
        # CPU.  Operators rarely need to change this; the default
        # 2048 is the §14.6 sweet spot at 192 kHz IQ rate.
        #
        # Change takes effect on the NEXT engine recreation
        # (i.e., the next IQ rate change OR the next Lyra start).
        # Reason: changing FFT size invalidates any loaded
        # profile (different bin count); rather than dropping
        # the profile silently, we defer the change to the
        # natural recreation point so the operator's session
        # state stays stable.  Tooltip explains.
        from lyra.dsp.captured_profile_iq import CapturedProfileIQ as _CPI
        cur_fft = int(s.value(
            "noise/iq_capture_fft_size",
            _CPI.DEFAULT_FFT_SIZE,
            type=int))
        if cur_fft not in (1024, 2048, 4096):
            cur_fft = _CPI.DEFAULT_FFT_SIZE
        from PySide6.QtWidgets import QComboBox
        fft_row = QHBoxLayout()
        fft_row.addWidget(QLabel("FFT size:"))
        self.fft_size_combo = QComboBox()
        for size, label in [
            (1024, "1024  (~188 Hz/bin @ 192k IQ)"),
            (2048, "2048  (~94 Hz/bin @ 192k IQ — default)"),
            (4096, "4096  (~47 Hz/bin @ 192k IQ)"),
        ]:
            self.fft_size_combo.addItem(label, size)
        # Pick the active row.
        for i in range(self.fft_size_combo.count()):
            if self.fft_size_combo.itemData(i) == cur_fft:
                self.fft_size_combo.setCurrentIndex(i)
                break
        self.fft_size_combo.setToolTip(
            "FFT bin count for the IQ analysis window.\n\n"
            "1024 = ~188 Hz bin width at 192 kHz IQ.  Lower CPU.\n"
            "2048 = ~94 Hz (default; §14.6 sweet spot)\n"
            "4096 = ~47 Hz.  Finer noise discrimination, ~2× CPU.\n\n"
            "Change takes effect on the next IQ rate change OR "
            "the next Lyra restart — whichever comes first.  "
            "Profiles captured at a different FFT size won't "
            "load (different bin count); recapture if you "
            "change this.")
        self.fft_size_combo.setMinimumWidth(280)
        self.fft_size_combo.currentIndexChanged.connect(
            self._on_iq_fft_size_changed)
        fft_row.addWidget(self.fft_size_combo, 1)
        fft_row.addStretch(1)
        gv.addLayout(fft_row)

        # Profile staleness fire threshold — operator-tunable in
        # v0.0.9.5.  Was previously a hard-coded 10 dB constant.
        # When the loaded captured profile drifts beyond this from
        # current band noise, Lyra fires a status-bar toast
        # suggesting recapture.  Higher = more tolerant of drift.
        cur_thresh = float(s.value(
            "noise/staleness_threshold_db", 10.0, type=float))
        cur_thresh = max(3.0, min(25.0, cur_thresh))
        thresh_row = QHBoxLayout()
        thresh_row.addWidget(QLabel("Profile staleness:"))
        self.staleness_thresh_spin = QSpinBox()
        self.staleness_thresh_spin.setRange(3, 25)
        self.staleness_thresh_spin.setValue(int(round(cur_thresh)))
        self.staleness_thresh_spin.setSuffix(" dB")
        self.staleness_thresh_spin.setFixedWidth(110)
        self.staleness_thresh_spin.setToolTip(
            "How far the live noise floor must drift from the loaded "
            "captured profile (in dB) before Lyra warns you the "
            "profile may be stale.\n"
            "\n"
            "Default 10 dB — fires for genuine band-condition shifts "
            "(QRN onset, antenna swap, weather front) without "
            "spurious toasts on normal noise-floor breathing.\n"
            "\n"
            "Tighten to 5-7 dB on a very stable QTH; loosen to "
            "15-20 dB if you find toasts firing too readily on "
            "natural band drift.\n"
            "\n"
            "Rearm threshold (when the toast can fire again after "
            "drift drops back) tracks at 70% of this value — "
            "automatic, no separate knob.")
        self.staleness_thresh_spin.valueChanged.connect(
            self._on_staleness_threshold_changed)
        thresh_row.addWidget(self.staleness_thresh_spin)
        thresh_row.addStretch(1)
        gv.addLayout(thresh_row)

        # Storage location selector.
        loc_box = QGroupBox("Storage location")
        loc_v = QVBoxLayout(loc_box)
        loc_v.setSpacing(6)
        # Resolve the current default + custom paths.
        from lyra.dsp import noise_profile_store as nps
        default_path = str(nps.default_profile_folder())
        cur_custom = str(s.value("noise/profile_folder", "",
                                  type=str) or "")
        self._loc_default_radio = QRadioButton(
            f"Default — {default_path}")
        self._loc_custom_radio = QRadioButton("Custom folder:")
        if cur_custom:
            self._loc_custom_radio.setChecked(True)
        else:
            self._loc_default_radio.setChecked(True)
        loc_v.addWidget(self._loc_default_radio)
        loc_v.addWidget(self._loc_custom_radio)
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(20, 0, 0, 0)
        self._loc_custom_field = QLineEdit(cur_custom)
        self._loc_custom_field.setPlaceholderText(
            "Click Browse… to choose a folder")
        self._loc_custom_browse = QPushButton("Browse…")
        custom_row.addWidget(self._loc_custom_field, 1)
        custom_row.addWidget(self._loc_custom_browse)
        loc_v.addLayout(custom_row)
        # Wire — custom field is enabled only when Custom radio is on.
        self._loc_custom_field.setEnabled(self._loc_custom_radio.isChecked())
        self._loc_custom_browse.setEnabled(self._loc_custom_radio.isChecked())
        self._loc_default_radio.toggled.connect(self._on_location_radio_toggled)
        self._loc_custom_radio.toggled.connect(self._on_location_radio_toggled)
        self._loc_custom_browse.clicked.connect(self._on_browse_custom_folder)
        self._loc_custom_field.editingFinished.connect(
            self._on_custom_path_edited)
        gv.addWidget(loc_box)

        # Age warning thresholds.
        age_row = QHBoxLayout()
        age_row.addWidget(QLabel("Profile age warning:"))
        self.age_amber = QSpinBox()
        self.age_amber.setRange(1, 168)   # 1 hour to 1 week
        self.age_amber.setSuffix(" hours")
        self.age_amber.setValue(int(s.value(
            "noise/age_amber_hours", 24, type=int)))
        self.age_amber.valueChanged.connect(self._on_age_amber_changed)
        age_row.addWidget(QLabel("Amber after"))
        age_row.addWidget(self.age_amber)
        age_row.addSpacing(16)
        self.age_red = QSpinBox()
        self.age_red.setRange(1, 90)
        self.age_red.setSuffix(" days")
        self.age_red.setValue(int(s.value(
            "noise/age_red_days", 7, type=int)))
        self.age_red.valueChanged.connect(self._on_age_red_changed)
        age_row.addWidget(QLabel("Red after"))
        age_row.addWidget(self.age_red)
        age_row.addStretch(1)
        gv.addLayout(age_row)

        # Action buttons.
        action_row = QHBoxLayout()
        self.btn_open_manager = QPushButton("Open profile manager…")
        self.btn_open_manager.clicked.connect(
            self._on_open_profile_manager)
        action_row.addWidget(self.btn_open_manager)
        self.btn_open_folder = QPushButton("Open profiles folder…")
        self.btn_open_folder.setToolTip(
            "Open the active profile folder in your OS file explorer.")
        self.btn_open_folder.clicked.connect(self._on_open_profile_folder)
        action_row.addWidget(self.btn_open_folder)
        action_row.addStretch(1)
        gv.addLayout(action_row)

        v.addWidget(grp_cap)

        # ── Noise Blanker (NB, Phase 3.D #2) ──────────────────────
        grp_nb = QGroupBox("Noise Blanker (NB)")
        nbv = QVBoxLayout(grp_nb)
        nbv.setSpacing(8)

        nb_intro = QLabel(
            "IQ-domain impulse suppression.  Detects narrow impulse "
            "noise (ignition, lightning crashes, switching power "
            "supplies) before the bandpass filter spreads it across "
            "the audio passband.  WDSP runs the live blanker; the "
            "profile picker below is currently a binary on/off plus "
            "saved-preference (Light / Medium / Heavy / Custom all "
            "produce the same WDSP-default audio behavior — your "
            "selection is persisted for when the threshold mapping "
            "is wired up).")
        nb_intro.setWordWrap(True)
        _force_wrap_height(nb_intro)
        nb_intro.setStyleSheet("color: #8a9aac;")
        nbv.addWidget(nb_intro)

        # Profile combo — radio buttons in a row, matching the AGC
        # tab convention.
        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        nb_prof_row = QHBoxLayout()
        nb_prof_row.addWidget(QLabel("Profile:"))
        self._nb_radio_group = QButtonGroup(self)
        self._nb_radios: dict[str, QRadioButton] = {}
        for key, label in (
                ("off", "Off"),
                ("light", "Light"),
                ("medium", "Medium"),
                ("heavy", "Heavy"),
                ("custom", "Custom"),
        ):
            rb = QRadioButton(label)
            self._nb_radio_group.addButton(rb)
            self._nb_radios[key] = rb
            nb_prof_row.addWidget(rb)
            rb.toggled.connect(
                lambda checked, k=key: self._on_nb_radio_toggled(
                    checked, k))
        nb_prof_row.addStretch(1)
        nbv.addLayout(nb_prof_row)
        # Sync radio with current state.  Block signals during the
        # initial setChecked so the toggled handler doesn't fire
        # against widgets (the threshold slider) that are constructed
        # below this row — toggled handler calls
        # _update_nb_threshold_enabled which reaches for nb_thr_slider.
        target_rb = self._nb_radios.get(
            radio.nb_profile, self._nb_radios["off"])
        target_rb.blockSignals(True)
        target_rb.setChecked(True)
        target_rb.blockSignals(False)

        # Threshold slider (only directly meaningful when Custom is
        # active; greyed out on the presets but still readable).
        # Phase 6.C: range constants moved to the _NBState dataclass
        # alongside the deletion of lyra/dsp/nb.py.
        from lyra.dsp.channel import _NBState
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Threshold:"))
        self.nb_thr_slider = QSlider(Qt.Horizontal)
        # Range maps slider integer 15..500 to threshold 1.5..50.0
        # (×10 internal scaling for finer resolution).
        self.nb_thr_slider.setRange(
            int(_NBState.THRESHOLD_MIN * 10),
            int(_NBState.THRESHOLD_MAX * 10))
        self.nb_thr_slider.setValue(
            max(self.nb_thr_slider.minimum(),
                int(round(radio.nb_threshold * 10))))
        self.nb_thr_slider.setSingleStep(1)
        self.nb_thr_slider.setPageStep(10)
        self.nb_thr_label = QLabel(
            f"{radio.nb_threshold:.1f}×  background")
        self.nb_thr_label.setMinimumWidth(120)
        self.nb_thr_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700;")
        self.nb_thr_slider.valueChanged.connect(
            self._on_nb_threshold_slider)
        thr_row.addWidget(self.nb_thr_slider, 1)
        thr_row.addWidget(self.nb_thr_label)
        nbv.addLayout(thr_row)

        nb_hint = QLabel(
            "Threshold value is persisted but currently advisory — "
            "WDSP's blanker runs with its built-in default until a "
            "future build maps this slider to a WDSP parameter.\n\n"
            "Right-click the NB button on the DSP+Audio panel for "
            "quick on/off switching during operating.")
        nb_hint.setWordWrap(True)
        _force_wrap_height(nb_hint)
        # Drop the explicit font-size override — match the intro
        # paragraph above so the operator-facing text is at the
        # same readability tier as the rest of the tab.  Slightly
        # dimmer color than the intro keeps the visual hierarchy
        # ("intro" > "hint") without shrinking the type.
        nb_hint.setStyleSheet("color: #7a8a9c;")
        nbv.addWidget(nb_hint)

        # Refresh slider/radio when Radio fires its change signals
        # (e.g. operator toggles via DSP-row button).
        radio.nb_profile_changed.connect(self._on_nb_profile_signal)
        radio.nb_threshold_changed.connect(
            self._on_nb_threshold_signal)
        # Initial enabled-state for the threshold slider — only
        # active when Custom is selected.
        self._update_nb_threshold_enabled(radio.nb_profile)

        v.addWidget(grp_nb)

        # ── Auto Notch Filter (ANF, Phase 3.D #3) ─────────────────
        grp_anf = QGroupBox("Auto Notch Filter (ANF)")
        anfv = QVBoxLayout(grp_anf)
        anfv.setSpacing(8)

        anf_intro = QLabel(
            "Adaptive notch.  Hunts and surgically removes narrow "
            "tonal interference — heterodynes, BFO whistles, "
            "single-frequency carriers, RTTY spurs.  Operator turns "
            "it on; the filter learns whatever tones are present "
            "and nulls them without taking out genuine speech.  "
            "WDSP runs the live notch; the profile picker below is "
            "currently a binary on/off plus saved-preference "
            "(Light / Medium / Heavy / Custom all produce the same "
            "WDSP-default audio behavior — your selection is "
            "persisted for when the μ mapping is wired up).")
        anf_intro.setWordWrap(True)
        _force_wrap_height(anf_intro)
        anf_intro.setStyleSheet("color: #8a9aac;")
        anfv.addWidget(anf_intro)

        anf_prof_row = QHBoxLayout()
        anf_prof_row.addWidget(QLabel("Profile:"))
        self._anf_radio_group = QButtonGroup(self)
        self._anf_radios: dict[str, QRadioButton] = {}
        for key, label in (
                ("off", "Off"),
                ("light", "Light"),
                ("medium", "Medium"),
                ("heavy", "Heavy"),
                ("custom", "Custom"),
        ):
            rb = QRadioButton(label)
            self._anf_radio_group.addButton(rb)
            self._anf_radios[key] = rb
            anf_prof_row.addWidget(rb)
            rb.toggled.connect(
                lambda checked, k=key: self._on_anf_radio_toggled(
                    checked, k))
        anf_prof_row.addStretch(1)
        anfv.addLayout(anf_prof_row)
        # Block signals during initial setChecked so the toggled
        # handler doesn't fire against the μ slider before it's
        # built (same construction-order pattern as the NB section).
        anf_target = self._anf_radios.get(
            radio.anf_profile, self._anf_radios["off"])
        anf_target.blockSignals(True)
        anf_target.setChecked(True)
        anf_target.blockSignals(False)

        # μ slider — operator-tunable in Custom; presets show the
        # value but disabled.  Uses log scale for ergonomic feel
        # since μ ranges over 2 decades (1e-5 to 1e-3).
        anf_mu_row = QHBoxLayout()
        anf_mu_row.addWidget(QLabel("μ (adapt rate):"))
        self.anf_mu_slider = QSlider(Qt.Horizontal)
        # Map slider int 0..200 to log μ over [MU_MIN, MU_MAX].
        self.anf_mu_slider.setRange(0, 200)
        self.anf_mu_slider.setValue(
            self._anf_mu_to_slider(radio.anf_mu))
        self.anf_mu_slider.setSingleStep(1)
        self.anf_mu_slider.setPageStep(20)
        self.anf_mu_label = QLabel(f"μ = {radio.anf_mu:.2e}")
        self.anf_mu_label.setMinimumWidth(120)
        self.anf_mu_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700;")
        self.anf_mu_slider.valueChanged.connect(
            self._on_anf_mu_slider)
        anf_mu_row.addWidget(self.anf_mu_slider, 1)
        anf_mu_row.addWidget(self.anf_mu_label)
        anfv.addLayout(anf_mu_row)

        anf_hint = QLabel(
            "μ value is persisted but currently advisory — WDSP's "
            "ANF runs with its built-in adapt rate until a future "
            "build maps this slider to a WDSP parameter.\n\n"
            "Right-click the ANF button on the DSP+Audio panel "
            "for quick on/off switching during operating.")
        anf_hint.setWordWrap(True)
        _force_wrap_height(anf_hint)
        anf_hint.setStyleSheet("color: #7a8a9c;")
        anfv.addWidget(anf_hint)

        radio.anf_profile_changed.connect(self._on_anf_profile_signal)
        radio.anf_mu_changed.connect(self._on_anf_mu_signal)
        self._update_anf_mu_enabled(radio.anf_profile)

        v.addWidget(grp_anf)

        # ── NR / NR2 / NR2 Gain Function / LMS groups removed in
        #    Phase 7 (v0.0.9.6).  The live noise reduction engine is
        #    now WDSP's EMNR; operator controls (Mode 1-4 + AEPF +
        #    NPE + master enable) live on the DSP+Audio panel where
        #    they're one click away during operating.  The legacy
        #    NR2 aggression / smoothing / speech-aware controls and
        #    NR2 gain-method picker no longer mapped to anything
        #    audible (WDSP's gain method is picked by Mode 1-4).
        #    LMS strength duplicate dropped — the panel slider is
        #    sufficient.  Persisted state for these knobs still
        #    lives on the channel's _NR2State / _LMSState dataclasses
        #    so future builds can reactivate them without losing
        #    operator preferences.

        # ── Squelch section (all-mode SSQL) ───────────────────────
        grp_sq = QGroupBox("All-Mode Squelch")
        sqv = QVBoxLayout(grp_sq)
        sqv.setSpacing(8)
        sq_intro = QLabel(
            "Voice / signal presence gate that mutes audio between "
            "transmissions across all modes.  WDSP runs the live "
            "engine — SSQL window-detector for SSB / CW / DIG, and "
            "the WDSP FMSQ / AMSQ modules for FM and AM.  Mode "
            "routing happens automatically; you just set the "
            "threshold for the noise floor on your current band.")
        sq_intro.setWordWrap(True)
        _force_wrap_height(sq_intro)
        sq_intro.setStyleSheet("color: #8a9aac; font-size: 12px;")
        sqv.addWidget(sq_intro)

        # Master enable mirror.
        self.sq_enable_chk = QCheckBox("Enable all-mode squelch")
        self.sq_enable_chk.setChecked(radio.squelch_enabled)
        self.sq_enable_chk.toggled.connect(self.radio.set_squelch_enabled)
        radio.squelch_enabled_changed.connect(
            self.sq_enable_chk.setChecked)
        sqv.addWidget(self.sq_enable_chk)

        # Threshold slider mirror.
        sq_thr_row = QHBoxLayout()
        sq_thr_row.addWidget(QLabel("Threshold:"))
        self.sq_thr_slider = QSlider(Qt.Horizontal)
        self.sq_thr_slider.setRange(0, 100)
        self.sq_thr_slider.setValue(
            int(round(radio.squelch_threshold * 100)))
        self.sq_thr_slider.setTickPosition(QSlider.TicksBelow)
        self.sq_thr_slider.setTickInterval(25)
        self.sq_thr_slider.valueChanged.connect(
            lambda v: self.radio.set_squelch_threshold(v / 100.0))
        radio.squelch_threshold_changed.connect(
            self._on_sq_threshold_signal)
        self.sq_thr_label = QLabel(
            f"{int(round(radio.squelch_threshold * 100))}")
        self.sq_thr_label.setMinimumWidth(50)
        self.sq_thr_label.setStyleSheet(
            "color: #50d0ff; font-family: Consolas, monospace; "
            "font-weight: 700;")
        sq_thr_row.addWidget(self.sq_thr_slider, 1)
        sq_thr_row.addWidget(self.sq_thr_label)
        sqv.addLayout(sq_thr_row)

        sq_hint = QLabel(
            "0 = effectively off (gate always open).  10 = barely-"
            "on, opens on faintest signal.  20 = voice-friendly "
            "default.  40 = mutes on quiet bands.  60+ = strong "
            "signals only.\n"
            "WDSP SSQL time constants: ~700 ms mute (lets brief "
            "speech pauses ride through), ~100 ms unmute (snappy "
            "speech onset).  Direct sliders for those are on the "
            "post-RX2 backlog.")
        sq_hint.setWordWrap(True)
        _force_wrap_height(sq_hint)
        sq_hint.setStyleSheet("color: #7a8a9c; font-size: 11px;")
        sqv.addWidget(sq_hint)
        v.addWidget(grp_sq)

        # ── Column reassignments ─────────────────────────────────
        # Phase 7 (v0.0.9.6) collapsed the right column when NR2 /
        # NR2 Gain Function / LMS groups went away.  Two-column
        # layout now:
        #   left   = Captured Noise Profile (largest) + SQ
        #   right  = NB + ANF
        # The middle column stays in the layout grid for future
        # expansion (e.g. when staleness controls grow into their
        # own group) but receives no widgets at present.
        col_left.removeWidget(grp_nb)
        col_left.removeWidget(grp_anf)
        # grp_sq stays in col_left under grp_cap (no remove needed).
        col_right.addWidget(grp_nb)
        col_right.addWidget(grp_anf)
        # Stretch on all three columns so groups stack from the
        # top rather than spreading to fill the tab height.
        col_left.addStretch(1)
        col_middle.addStretch(1)
        col_right.addStretch(1)

    # ── Slot implementations ─────────────────────────────────────

    def _on_duration_changed(self, val_tenths: int) -> None:
        seconds = val_tenths / 10.0
        self.dur_label.setText(f"{seconds:.1f} s")
        s = self._QSettings("N8SDR", "Lyra")
        s.setValue("noise/capture_duration_sec", float(seconds))

    def _on_gain_smoothing_changed(self, val_centi: int) -> None:
        """Operator changed the gain-smoothing slider (§14.6 Phase
        5b).  Live-tunable: pushes the new γ to the running IQ
        engine immediately AND persists to QSettings.  Range guard
        on Radio side keeps γ ∈ [0.0, 0.99]."""
        gamma = max(0.0, min(0.95, val_centi / 100.0))
        self.smooth_label.setText(f"{gamma:.2f}")
        s = self._QSettings("N8SDR", "Lyra")
        s.setValue("noise/gain_smoothing", float(gamma))
        # Push to running engine (under Radio's lock).  Skip
        # silently if engine not initialized — first pickup will
        # happen via autoload at next launch.
        try:
            eng = getattr(self.radio, "_iq_capture", None)
            lock = getattr(self.radio, "_iq_capture_lock", None)
            if eng is not None and lock is not None:
                with lock:
                    eng.set_gain_smoothing(gamma)
        except Exception as exc:
            print(f"[Settings] gain_smoothing push failed: {exc}")

    def _on_iq_fft_size_changed(self, _idx: int) -> None:
        """Operator changed the IQ-capture FFT size dropdown
        (§14.6 Phase 5c).  Persists to QSettings AND updates
        ``Radio._iq_capture_fft_size`` so the next engine
        recreation (rate change or restart) picks the new value
        up.  Does NOT recreate the engine immediately — that
        would silently invalidate any loaded profile, surprising
        the operator.  Tooltip on the combo explains."""
        size = int(self.fft_size_combo.currentData() or 2048)
        if size not in (1024, 2048, 4096):
            return
        s = self._QSettings("N8SDR", "Lyra")
        s.setValue("noise/iq_capture_fft_size", int(size))
        try:
            self.radio._iq_capture_fft_size = int(size)
        except Exception as exc:
            print(f"[Settings] iq_capture_fft_size push failed: {exc}")

    def _on_staleness_threshold_changed(self, val_db: int) -> None:
        """Operator changed the staleness fire threshold via the
        spinbox.  Push to Radio (which forwards to NR1 + persists to
        QSettings).  Added v0.0.9.5."""
        try:
            self.radio.set_nr_staleness_threshold_db(float(val_db))
        except Exception as exc:
            print(f"[Settings] staleness threshold push failed: {exc}")

    def _on_location_radio_toggled(self, _checked: bool) -> None:
        custom = self._loc_custom_radio.isChecked()
        self._loc_custom_field.setEnabled(custom)
        self._loc_custom_browse.setEnabled(custom)
        if custom:
            path = self._loc_custom_field.text().strip()
            if path:
                self.radio.set_noise_profile_folder(path)
        else:
            # Default — clear the custom path setting.
            self.radio.set_noise_profile_folder("")

    def _on_browse_custom_folder(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        start = self._loc_custom_field.text().strip() or str(
            self.radio.noise_profile_folder)
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose noise-profile storage folder", start)
        if not chosen:
            return
        self._loc_custom_field.setText(chosen)
        if self._loc_custom_radio.isChecked():
            self.radio.set_noise_profile_folder(chosen)

    def _on_custom_path_edited(self) -> None:
        path = self._loc_custom_field.text().strip()
        if self._loc_custom_radio.isChecked() and path:
            self.radio.set_noise_profile_folder(path)

    def _on_age_amber_changed(self, val: int) -> None:
        s = self._QSettings("N8SDR", "Lyra")
        s.setValue("noise/age_amber_hours", int(val))

    def _on_age_red_changed(self, val: int) -> None:
        s = self._QSettings("N8SDR", "Lyra")
        s.setValue("noise/age_red_days", int(val))

    def _on_open_profile_manager(self) -> None:
        try:
            from lyra.ui.noise_profile_manager import NoiseProfileManager
        except ImportError:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Profile manager",
                "Profile manager dialog is not yet wired in this "
                "build.  Coming in Day 3 piece 3.")
            return
        dlg = NoiseProfileManager(self.radio, parent=self)
        dlg.exec()

    def _on_open_profile_folder(self) -> None:
        """Open the current profile folder in the OS file explorer."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from lyra.dsp import noise_profile_store as nps
        folder = self.radio.noise_profile_folder
        # Create the folder on demand so the explorer doesn't open
        # an "this folder doesn't exist" error.
        nps.ensure_folder(folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ── NB section slot implementations (Phase 3.D #2) ───────────

    def _on_nb_radio_toggled(self, checked: bool, key: str) -> None:
        """Operator picked an NB profile radio button."""
        if not checked:
            return
        # Custom doesn't change the threshold; presets do.  Apply
        # via Radio so the channel + signals + persistence all
        # update together.
        self.radio.set_nb_profile(key)
        self._update_nb_threshold_enabled(key)

    def _on_nb_threshold_slider(self, val_x10: int) -> None:
        threshold = val_x10 / 10.0
        self.nb_thr_label.setText(f"{threshold:.1f}×  background")
        # Setting the threshold switches profile to Custom (Radio
        # handles that side-effect), which the radio button group
        # then mirrors via the nb_profile_changed signal.
        self.radio.set_nb_threshold(threshold)

    @_swallow_dead_widget
    def _on_nb_profile_signal(self, name: str) -> None:
        """Mirror an external profile change (e.g., from the
        DSP-row NB button right-click menu) into the radio group."""
        rb = self._nb_radios.get(name)
        if rb and not rb.isChecked():
            rb.blockSignals(True)
            rb.setChecked(True)
            rb.blockSignals(False)
        self._update_nb_threshold_enabled(name)

    @_swallow_dead_widget
    def _on_nb_threshold_signal(self, threshold: float) -> None:
        """Mirror an external threshold change into the slider."""
        target = int(round(threshold * 10))
        if self.nb_thr_slider.value() != target:
            self.nb_thr_slider.blockSignals(True)
            self.nb_thr_slider.setValue(target)
            self.nb_thr_slider.blockSignals(False)
        self.nb_thr_label.setText(f"{threshold:.1f}×  background")

    def _update_nb_threshold_enabled(self, profile: str) -> None:
        """The threshold slider is only directly tunable in Custom;
        on presets it shows the preset value but is greyed."""
        self.nb_thr_slider.setEnabled(profile == "custom")

    # ── ANF section slot implementations (Phase 3.D #3) ──────────

    def _on_anf_radio_toggled(self, checked: bool, key: str) -> None:
        if not checked:
            return
        self.radio.set_anf_profile(key)
        self._update_anf_mu_enabled(key)

    def _on_anf_mu_slider(self, slider_int: int) -> None:
        mu = self._anf_slider_to_mu(slider_int)
        self.anf_mu_label.setText(f"μ = {mu:.2e}")
        self.radio.set_anf_mu(mu)

    @_swallow_dead_widget
    def _on_anf_profile_signal(self, name: str) -> None:
        rb = self._anf_radios.get(name)
        if rb and not rb.isChecked():
            rb.blockSignals(True)
            rb.setChecked(True)
            rb.blockSignals(False)
        self._update_anf_mu_enabled(name)

    @_swallow_dead_widget
    def _on_anf_mu_signal(self, mu: float) -> None:
        target = self._anf_mu_to_slider(mu)
        if self.anf_mu_slider.value() != target:
            self.anf_mu_slider.blockSignals(True)
            self.anf_mu_slider.setValue(target)
            self.anf_mu_slider.blockSignals(False)
        self.anf_mu_label.setText(f"μ = {mu:.2e}")

    def _update_anf_mu_enabled(self, profile: str) -> None:
        self.anf_mu_slider.setEnabled(profile == "custom")

    # NOTE: NR2 aggression / smoothing / speech-aware slot
    # implementations removed in Phase 7 (v0.0.9.6) along with
    # the corresponding controls in the Noise tab.  WDSP's EMNR
    # exposes its operator surface via the DSP+Audio panel
    # (Mode 1-4 + AEPF + NPE).  Persisted state still lives on
    # _NR2State for forward compatibility.

    @staticmethod
    def _anf_mu_to_slider(mu: float) -> int:
        """Map μ ∈ [1e-5, 1e-3] to slider int 0..200 logarithmically.

        Log scale ergonomics: equal slider movement is equal
        multiplicative change in μ.
        """
        import math
        from lyra.dsp.channel import _ANFState as ANF
        mu = max(ANF.MU_MIN, min(ANF.MU_MAX, mu))
        # log10(MU_MIN) = -5, log10(MU_MAX) = -3 → 2 decades.
        log_min = math.log10(ANF.MU_MIN)
        log_max = math.log10(ANF.MU_MAX)
        frac = (math.log10(mu) - log_min) / (log_max - log_min)
        return int(round(frac * 200))

    @staticmethod
    def _anf_slider_to_mu(slider_int: int) -> float:
        import math
        from lyra.dsp.channel import _ANFState as ANF
        log_min = math.log10(ANF.MU_MIN)
        log_max = math.log10(ANF.MU_MAX)
        frac = max(0, min(200, slider_int)) / 200.0
        return 10.0 ** (log_min + frac * (log_max - log_min))

    # NOTE: NR2 method-picker and LMS strength-mirror slots
    # removed Phase 7 along with the corresponding Settings groups.
    # The DSP+Audio panel's NR Mode 1-4 + LMS strength slider
    # are the live operator surface for those parameters now.

    @_swallow_dead_widget
    def _on_sq_threshold_signal(self, value: float) -> None:
        """Mirror an external squelch threshold change into slider."""
        target = int(round(value * 100))
        if self.sq_thr_slider.value() != target:
            self.sq_thr_slider.blockSignals(True)
            self.sq_thr_slider.setValue(target)
            self.sq_thr_slider.blockSignals(False)
        self.sq_thr_label.setText(f"{target}")


class PropagationSettingsTab(QWidget):
    """Settings home for the Propagation panel + NCDXF beacons.

    The Propagation dock itself shows live solar / band / Follow
    controls; this tab is for the persistent toggles that don't
    belong on the slim status panel — namely the NCDXF spectrum-
    marker toggle and a quick reference to clock accuracy (which
    matters for beacon Follow timing).
    """

    def __init__(self, radio):
        super().__init__()
        self.radio = radio

        v = QVBoxLayout(self)

        # ── Spectrum overlay ─────────────────────────────────────
        grp_marker = QGroupBox("Spectrum overlay")
        gm = QVBoxLayout(grp_marker)

        self.ncdxf_chk = QCheckBox(
            "Show NCDXF beacon markers on the panadapter")
        self.ncdxf_chk.setChecked(radio.band_plan_show_ncdxf)
        self.ncdxf_chk.setToolTip(
            "Cyan triangles at the 5 NCDXF International Beacon\n"
            "Project frequencies (14.100 / 18.110 / 21.150 /\n"
            "24.930 / 28.200 MHz).  Hover one to see which of the\n"
            "18 worldwide stations is transmitting on that band\n"
            "right now.  Click to QSY.\n\n"
            "Independent from the digimode landmarks toggle — you\n"
            "can have NCDXF markers on while FT8/WSPR triangles\n"
            "are off, or vice versa.")
        self.ncdxf_chk.toggled.connect(
            self.radio.set_band_plan_show_ncdxf)
        gm.addWidget(self.ncdxf_chk)

        marker_help = QLabel(
            "<i>Tip: the panadapter has to be tuned to a band that\n"
            "includes one of the NCDXF frequencies for the markers\n"
            "to be visible.  20m / 17m / 15m / 12m / 10m only.</i>")
        marker_help.setStyleSheet("color: #8a9eb6; padding: 4px 4px 0 4px;")
        marker_help.setWordWrap(True)
        gm.addWidget(marker_help)

        v.addWidget(grp_marker)

        # ── Clock accuracy ───────────────────────────────────────
        grp_clock = QGroupBox("Clock accuracy (NCDXF Follow)")
        gc = QVBoxLayout(grp_clock)

        clock_text = QLabel(
            "NCDXF beacons rotate on 10-second slots.  Lyra computes\n"
            "which station is on the air purely from your PC clock\n"
            "(no callsign decoding) — so a clock that drifts by more\n"
            "than ~3 seconds will mis-identify beacons.\n\n"
            "Right-click either toolbar clock (Local time or UTC) to:\n"
            "  • Check drift against a public NTP server\n"
            "  • Sync time now (Windows w32time)\n"
            "  • Read the explanation\n\n"
            "If your check comes back significantly off, the UTC\n"
            "clock will show a ⚠ prefix until you re-check.")
        clock_text.setWordWrap(True)
        clock_text.setStyleSheet("padding: 4px;")
        gc.addWidget(clock_text)

        v.addWidget(grp_clock)

        # ── Where to find the live controls ──────────────────────
        grp_panel = QGroupBox("Live controls")
        gp = QVBoxLayout(grp_panel)
        panel_text = QLabel(
            "The Propagation dock (View menu → Propagation) shows\n"
            "live solar numbers (SFI / A / K), a per-band conditions\n"
            "heatmap (Day/Night-aware via your QTH grid square), and\n"
            "the NCDXF Follow dropdown which auto-tunes one chosen\n"
            "station around its 5-band rotation.\n\n"
            "Set your grid square in <b>Radio → Operator</b> so the\n"
            "Day/Night band-conditions pick uses your local sunrise\n"
            "and sunset.")
        panel_text.setWordWrap(True)
        panel_text.setStyleSheet("padding: 4px;")
        gp.addWidget(panel_text)

        v.addWidget(grp_panel)

        v.addStretch(1)

        # Track radio-side changes so external toggles (band-plan
        # tab, dock right-click, etc.) keep this checkbox in sync.
        radio.band_plan_show_ncdxf_changed.connect(self._on_ncdxf_changed)

    @_swallow_dead_widget
    def _on_ncdxf_changed(self, on: bool):
        if self.ncdxf_chk.isChecked() != on:
            self.ncdxf_chk.blockSignals(True)
            self.ncdxf_chk.setChecked(bool(on))
            self.ncdxf_chk.blockSignals(False)


class WxAlertsSettingsTab(QWidget):
    """Phase 4 — Weather Alerts settings.

    Disclaimer-gated configuration for Lyra's all-source weather
    monitor (lightning detection + high-wind alerts + NWS severe
    storm warnings).  Reads operator location from the global
    Radio settings (callsign + grid square + manual lat/lon) so
    those fields aren't duplicated here.
    """

    DISCLAIMER_TEXT = (
        "Weather alerts (lightning and high wind) are provided as "
        "<b>informational awareness only</b>. They are approximations "
        "based on public data sources (NWS/NOAA, Blitzortung, "
        "Ambient Weather, Ecowitt) and may be delayed, incomplete, or "
        "inaccurate. Do <b>NOT</b> rely on Lyra-SDR as your primary "
        "safety system. Always use official weather services and your "
        "own judgment. By enabling weather alerts you acknowledge "
        "this limitation and accept responsibility for your own "
        "station and antenna safety."
    )

    def __init__(self, radio):
        super().__init__()
        self.radio = radio

        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import (
            QPushButton, QGroupBox, QLineEdit, QSpinBox, QDoubleSpinBox,
            QComboBox, QFrame)

        # Two-column layout below the top header (disclaimer + master
        # enable).  Same pattern as VisualsSettingsTab and the Noise
        # tab — disclaimer at top spans the full width because it's a
        # safety-critical acknowledgment, then the per-feature
        # configuration splits across columns:
        #   left  = alert-CONFIG (Lightning + Wind)
        #   right = ops + creds  (Notifications + API Credentials)
        # Operators rarely need to switch between left and right
        # mid-task so they stay visually separated; the layout
        # cuts vertical scrolling roughly in half.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        # Top header — disclaimer + master enable, full-width.
        v = outer    # full-width section so the existing addWidget
                     # calls below land here for the disclaimer +
                     # master-enable groups.

        # ── Disclaimer panel ────────────────────────────────────────
        disc_box = QGroupBox("⚠  Disclaimer — read before enabling")
        disc_box.setStyleSheet(
            "QGroupBox { border: 1px solid #ff8c00; "
            "background-color: rgba(255,140,0,0.06); "
            "border-radius: 6px; padding: 8px; margin-top: 6px; } "
            "QGroupBox::title { color: #ff8c00; font-weight: 700; "
            "padding: 0 6px; }")
        dv = QVBoxLayout(disc_box)
        disc_text = QLabel(self.DISCLAIMER_TEXT)
        disc_text.setWordWrap(True)
        disc_text.setStyleSheet("color: #eaf4ff; line-height: 1.6;")
        disc_text.setTextFormat(Qt.RichText)
        dv.addWidget(disc_text)

        self.disc_chk = QCheckBox(
            "I understand — this is a convenience feature, not a "
            "safety system")
        self.disc_chk.setChecked(self.radio.wx_disclaimer_accepted)
        self.disc_chk.setStyleSheet(
            "color: #eaf4ff; font-weight: 600;")
        self.disc_chk.toggled.connect(self._on_disclaimer_toggled)
        dv.addWidget(self.disc_chk)
        v.addWidget(disc_box)

        # ── Master enable ──────────────────────────────────────────
        ena_box = QGroupBox("Weather Alerts")
        ev = QGridLayout(ena_box)
        ev.setColumnStretch(1, 1)

        self.master_chk = QCheckBox("Enable weather alerts")
        self.master_chk.setEnabled(self.radio.wx_disclaimer_accepted)
        self.master_chk.setChecked(self.radio.wx_enabled)
        self.master_chk.toggled.connect(self.radio.set_wx_enabled)
        self.radio.wx_enabled_changed.connect(self.master_chk.setChecked)
        ev.addWidget(self.master_chk, 0, 0, 1, 2)

        # Operator-location summary — points to Radio settings tab.
        loc_label = QLabel("")
        loc_label.setWordWrap(True)
        loc_label.setStyleSheet(
            "color: #8fafc0; font-size: 11px; font-style: italic;")
        self._loc_summary = loc_label
        ev.addWidget(QLabel("Operator location:"), 1, 0)
        ev.addWidget(loc_label, 1, 1)
        self._refresh_location_summary()
        self.radio.callsign_changed.connect(
            lambda _: self._refresh_location_summary())
        self.radio.grid_square_changed.connect(
            lambda _: self._refresh_location_summary())
        self.radio.operator_location_changed.connect(
            lambda _lat, _lon: self._refresh_location_summary())
        v.addWidget(ena_box)

        # ── Switch to two-column layout for the per-feature sections.
        # Disclaimer + master enable above stay full-width; everything
        # below gets split into Lightning/Wind on the left and
        # Notifications/Credentials on the right.
        col_row = QHBoxLayout()
        col_row.setSpacing(12)
        col_left = QVBoxLayout()
        col_right = QVBoxLayout()
        col_left.setSpacing(10)
        col_right.setSpacing(10)
        col_row.addLayout(col_left, 1)
        col_row.addLayout(col_right, 1)
        outer.addLayout(col_row)
        # Backwards-compat alias so the existing v.addWidget(...)
        # calls below for grp_lt + grp_wd land in the LEFT column.
        # Notification / credential groups are explicitly added to
        # col_right at their own block.
        v = col_left

        # ── Lightning section ──────────────────────────────────────
        s = QSettings("N8SDR", "Lyra")
        lt_box = QGroupBox("Lightning Detection")
        ltv = QVBoxLayout(lt_box)
        ltv.setSpacing(8)
        lt_intro = QLabel(
            "Toolbar shows closest detected strike with proximity "
            "color (yellow > 25 mi, orange < 25 mi, red < 10 mi). "
            "Hidden when no strikes detected within range.")
        lt_intro.setWordWrap(True)
        lt_intro.setStyleSheet("color: #8fafc0; font-size: 12px;")
        ltv.addWidget(lt_intro)

        # Range + units
        rng_row = QHBoxLayout()
        rng_row.addWidget(QLabel("Alert range:"))
        self.range_spin = QSpinBox()
        self.range_spin.setRange(5, 500)
        self.range_spin.setSuffix("")
        self.range_spin.setFixedWidth(80)
        cur_range_km = float(s.value(
            "wx/lightning_range_km", 80.0, type=float))
        self._dist_unit = str(s.value("wx/distance_unit", "mi", type=str))
        # Display in operator's preferred units.
        if self._dist_unit == "km":
            self.range_spin.setValue(int(round(cur_range_km)))
        else:
            self.range_spin.setValue(int(round(cur_range_km / 1.60934)))
        self.range_spin.valueChanged.connect(self._on_range_changed)
        rng_row.addWidget(self.range_spin)
        self.unit_combo = QComboBox()
        self.unit_combo.addItem("Miles", "mi")
        self.unit_combo.addItem("Kilometres", "km")
        self.unit_combo.setCurrentIndex(0 if self._dist_unit == "mi" else 1)
        self.unit_combo.currentIndexChanged.connect(
            self._on_dist_unit_changed)
        rng_row.addWidget(self.unit_combo)
        rng_row.addStretch(1)
        ltv.addLayout(rng_row)

        # Source checkboxes
        src_label = QLabel("DATA SOURCES")
        src_label.setStyleSheet(
            "color: #50d0ff; font-size: 11px; font-weight: 700; "
            "letter-spacing: 1px; padding-top: 4px;")
        ltv.addWidget(src_label)
        self.src_blitz_chk = QCheckBox(
            "Blitzortung.org  — global lightning network (free)")
        self.src_blitz_chk.setChecked(bool(
            s.value("wx/src_blitzortung", False, type=bool)))
        self.src_blitz_chk.toggled.connect(
            lambda on: self.radio.set_wx_config(src_blitzortung=on))
        ltv.addWidget(self.src_blitz_chk)

        self.src_nws_chk = QCheckBox(
            "NOAA / NWS  — severe thunderstorm warnings (US, free)")
        self.src_nws_chk.setChecked(bool(
            s.value("wx/src_nws", False, type=bool)))
        self.src_nws_chk.toggled.connect(
            lambda on: self.radio.set_wx_config(src_nws=on))
        ltv.addWidget(self.src_nws_chk)

        self.src_amb_chk = QCheckBox(
            "Ambient Weather  — PWS with WH31L lightning add-on "
            "(requires API keys below)")
        self.src_amb_chk.setChecked(bool(
            s.value("wx/src_ambient", False, type=bool)))
        self.src_amb_chk.toggled.connect(
            lambda on: self.radio.set_wx_config(src_ambient=on))
        ltv.addWidget(self.src_amb_chk)

        self.src_eco_chk = QCheckBox(
            "Ecowitt  — PWS with WH57 lightning sensor "
            "(requires app+api+MAC below)")
        self.src_eco_chk.setChecked(bool(
            s.value("wx/src_ecowitt", False, type=bool)))
        self.src_eco_chk.toggled.connect(
            lambda on: self.radio.set_wx_config(src_ecowitt=on))
        ltv.addWidget(self.src_eco_chk)
        v.addWidget(lt_box)

        # ── Wind section ───────────────────────────────────────────
        wd_box = QGroupBox("High Wind Alerts")
        wdv = QVBoxLayout(wd_box)
        wdv.setSpacing(8)
        wd_intro = QLabel(
            "Three tiers: yellow at 10 mph below threshold, orange "
            "at threshold, red on NWS High / Extreme Wind Warning or "
            "15 mph above threshold.")
        wd_intro.setWordWrap(True)
        wd_intro.setStyleSheet("color: #8fafc0; font-size: 12px;")
        wdv.addWidget(wd_intro)

        # Thresholds
        thr_grid = QGridLayout()
        thr_grid.addWidget(QLabel("Sustained threshold (mph):"), 0, 0)
        self.wind_sust_spin = QSpinBox()
        self.wind_sust_spin.setRange(5, 100)
        self.wind_sust_spin.setValue(int(round(float(
            s.value("wx/wind_sustained_mph", 30.0, type=float)))))
        self.wind_sust_spin.setFixedWidth(80)
        self.wind_sust_spin.valueChanged.connect(
            lambda v: self.radio.set_wx_config(
                wind_sustained_mph=float(v)))
        thr_grid.addWidget(self.wind_sust_spin, 0, 1)
        thr_grid.setColumnStretch(2, 1)

        thr_grid.addWidget(QLabel("Gust threshold (mph):"), 1, 0)
        self.wind_gust_spin = QSpinBox()
        self.wind_gust_spin.setRange(10, 150)
        self.wind_gust_spin.setValue(int(round(float(
            s.value("wx/wind_gust_mph", 40.0, type=float)))))
        self.wind_gust_spin.setFixedWidth(80)
        self.wind_gust_spin.valueChanged.connect(
            lambda v: self.radio.set_wx_config(wind_gust_mph=float(v)))
        thr_grid.addWidget(self.wind_gust_spin, 1, 1)
        wdv.addLayout(thr_grid)

        wd_src_label = QLabel("DATA SOURCES")
        wd_src_label.setStyleSheet(
            "color: #50d0ff; font-size: 11px; font-weight: 700; "
            "letter-spacing: 1px; padding-top: 4px;")
        wdv.addWidget(wd_src_label)
        self.wind_nws_chk = QCheckBox(
            "NWS Wind Alerts  — High Wind / Wind Advisory / Extreme "
            "Wind (US, free)")
        self.wind_nws_chk.setChecked(self.src_nws_chk.isChecked())
        self.wind_nws_chk.setEnabled(False)
        self.wind_nws_chk.setToolTip(
            "Driven by the same NOAA/NWS toggle in the Lightning "
            "section above.")
        wdv.addWidget(self.wind_nws_chk)
        # Keep the disabled checkbox in sync with the lightning NWS one.
        self.src_nws_chk.toggled.connect(self.wind_nws_chk.setChecked)

        self.wind_metar_chk = QCheckBox(
            "NWS METAR  — live wind from your nearest ICAO station")
        self.wind_metar_chk.setChecked(bool(
            s.value("wx/src_nws_metar", False, type=bool)))
        self.wind_metar_chk.toggled.connect(
            lambda on: self.radio.set_wx_config(src_nws_metar=on))
        wdv.addWidget(self.wind_metar_chk)

        metar_row = QHBoxLayout()
        metar_row.addWidget(QLabel("    METAR station (ICAO):"))
        self.metar_edit = QLineEdit(str(
            s.value("wx/nws_metar_station", "", type=str)))
        self.metar_edit.setFixedWidth(80)
        self.metar_edit.setPlaceholderText("e.g. KLUK")
        self.metar_edit.editingFinished.connect(
            lambda: self.radio.set_wx_config(
                nws_metar_station=self.metar_edit.text().strip().upper()))
        metar_row.addWidget(self.metar_edit)
        metar_row.addStretch(1)
        wdv.addLayout(metar_row)

        # Ambient + Ecowitt are dual-purpose — the same PWS feed
        # serves both lightning detection AND wind / gust data.
        # Surface them in the Wind section as locked mirrors of the
        # Lightning-section toggles so operators see the full picture
        # of what's contributing to wind readings.
        self.wind_amb_chk = QCheckBox(
            "Ambient Weather  — sustained / gust from your PWS "
            "anemometer")
        self.wind_amb_chk.setChecked(self.src_amb_chk.isChecked())
        self.wind_amb_chk.setEnabled(False)
        self.wind_amb_chk.setToolTip(
            "Driven by the Ambient Weather toggle in the Lightning "
            "section above — same PWS feed serves both lightning and "
            "wind readings.")
        wdv.addWidget(self.wind_amb_chk)
        self.src_amb_chk.toggled.connect(self.wind_amb_chk.setChecked)

        self.wind_eco_chk = QCheckBox(
            "Ecowitt  — sustained / gust from your PWS anemometer")
        self.wind_eco_chk.setChecked(self.src_eco_chk.isChecked())
        self.wind_eco_chk.setEnabled(False)
        self.wind_eco_chk.setToolTip(
            "Driven by the Ecowitt toggle in the Lightning section "
            "above — same PWS feed serves both lightning and wind "
            "readings.")
        wdv.addWidget(self.wind_eco_chk)
        self.src_eco_chk.toggled.connect(self.wind_eco_chk.setChecked)

        v.addWidget(wd_box)
        # Stretch at the bottom of the left column so groups stack
        # from the top rather than spreading vertically.
        v.addStretch(1)
        # Switch to the right column for Notifications + Credentials.
        v = col_right

        # ── Notifications ──────────────────────────────────────────
        nt_box = QGroupBox("Notifications")
        ntv = QVBoxLayout(nt_box)
        ntv.setSpacing(6)
        self.toast_chk = QCheckBox("Show desktop toast notifications")
        self.toast_chk.setChecked(bool(
            s.value("wx/desktop_enabled", True, type=bool)))
        self.toast_chk.toggled.connect(self._on_desktop_toggled)
        ntv.addWidget(self.toast_chk)

        self.audio_chk = QCheckBox("Play audio cue with toast")
        self.audio_chk.setChecked(bool(
            s.value("wx/audio_enabled", True, type=bool)))
        self.audio_chk.toggled.connect(self._on_audio_toggled)
        ntv.addWidget(self.audio_chk)

        self.test_btn = QPushButton("Send test toast")
        self.test_btn.setFixedWidth(140)
        self.test_btn.clicked.connect(self.radio.fire_wx_test_toast)
        ntv.addWidget(self.test_btn)
        v.addWidget(nt_box)

        # ── API credentials ────────────────────────────────────────
        cred_box = QGroupBox("API Credentials")
        cv = QGridLayout(cred_box)
        cv.setColumnStretch(1, 1)

        amb_label = QLabel(
            "<b>Ambient Weather</b>  "
            "<a href='https://ambientweather.net/account/keys'>"
            "ambientweather.net/account/keys</a>")
        amb_label.setOpenExternalLinks(True)
        cv.addWidget(amb_label, 0, 0, 1, 2)

        cv.addWidget(QLabel("API Key:"), 1, 0)
        self.amb_api_edit = QLineEdit(str(
            s.value("wx/ambient_api_key", "", type=str)))
        self.amb_api_edit.setEchoMode(QLineEdit.Password)
        self.amb_api_edit.editingFinished.connect(
            lambda: self.radio.set_wx_config(
                ambient_api_key=self.amb_api_edit.text().strip()))
        cv.addWidget(self.amb_api_edit, 1, 1)

        cv.addWidget(QLabel("Application Key:"), 2, 0)
        self.amb_app_edit = QLineEdit(str(
            s.value("wx/ambient_app_key", "", type=str)))
        self.amb_app_edit.setEchoMode(QLineEdit.Password)
        self.amb_app_edit.editingFinished.connect(
            lambda: self.radio.set_wx_config(
                ambient_app_key=self.amb_app_edit.text().strip()))
        cv.addWidget(self.amb_app_edit, 2, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2a3848;")
        cv.addWidget(sep, 3, 0, 1, 2)

        eco_label = QLabel(
            "<b>Ecowitt</b>  "
            "<a href='https://www.ecowitt.net/'>ecowitt.net</a> → "
            "API Setting")
        eco_label.setOpenExternalLinks(True)
        cv.addWidget(eco_label, 4, 0, 1, 2)

        cv.addWidget(QLabel("Application Key:"), 5, 0)
        self.eco_app_edit = QLineEdit(str(
            s.value("wx/ecowitt_app_key", "", type=str)))
        self.eco_app_edit.setEchoMode(QLineEdit.Password)
        self.eco_app_edit.editingFinished.connect(
            lambda: self.radio.set_wx_config(
                ecowitt_app_key=self.eco_app_edit.text().strip()))
        cv.addWidget(self.eco_app_edit, 5, 1)

        cv.addWidget(QLabel("API Key:"), 6, 0)
        self.eco_api_edit = QLineEdit(str(
            s.value("wx/ecowitt_api_key", "", type=str)))
        self.eco_api_edit.setEchoMode(QLineEdit.Password)
        self.eco_api_edit.editingFinished.connect(
            lambda: self.radio.set_wx_config(
                ecowitt_api_key=self.eco_api_edit.text().strip()))
        cv.addWidget(self.eco_api_edit, 6, 1)

        cv.addWidget(QLabel("Gateway MAC:"), 7, 0)
        self.eco_mac_edit = QLineEdit(str(
            s.value("wx/ecowitt_mac", "", type=str)))
        self.eco_mac_edit.setPlaceholderText("e.g. 34:94:54:AB:CD:EF")
        self.eco_mac_edit.editingFinished.connect(
            lambda: self.radio.set_wx_config(
                ecowitt_mac=self.eco_mac_edit.text().strip().upper()))
        cv.addWidget(self.eco_mac_edit, 7, 1)
        v.addWidget(cred_box)

        v.addStretch(1)

    # ── Slot implementations ─────────────────────────────────────────

    def _on_disclaimer_toggled(self, on: bool) -> None:
        self.radio.set_wx_disclaimer_accepted(on)
        self.master_chk.setEnabled(on)
        if not on:
            self.master_chk.setChecked(False)

    def _on_range_changed(self, value: int) -> None:
        # Convert from operator's display unit to internal km.
        if self._dist_unit == "km":
            km = float(value)
        else:
            km = float(value) * 1.60934
        self.radio.set_wx_config(lightning_range_km=km)

    def _on_dist_unit_changed(self, idx: int) -> None:
        unit = self.unit_combo.itemData(idx) or "mi"
        self._dist_unit = unit
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").setValue("wx/distance_unit", unit)
        except Exception:
            pass
        # Refresh the indicator's display unit too.
        try:
            from PySide6.QtWidgets import QApplication
            for w in QApplication.instance().topLevelWidgets():
                wxi = w.findChild(type(w))  # noqa — placeholder
        except Exception:
            pass

    def _on_audio_toggled(self, on: bool) -> None:
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").setValue("wx/audio_enabled", on)
        except Exception:
            pass
        if self.radio._wx_worker is not None:
            self.radio._wx_worker.set_audio_enabled(on)

    def _on_desktop_toggled(self, on: bool) -> None:
        try:
            from PySide6.QtCore import QSettings
            QSettings("N8SDR", "Lyra").setValue("wx/desktop_enabled", on)
        except Exception:
            pass
        if self.radio._wx_worker is not None:
            self.radio._wx_worker.set_desktop_enabled(on)

    def _refresh_location_summary(self) -> None:
        cs = self.radio.callsign or "(no callsign)"
        gs = self.radio.grid_square or "(no grid)"
        lat = self.radio.operator_lat
        lon = self.radio.operator_lon
        if lat is None or lon is None:
            self._loc_summary.setText(
                f"{cs}  ·  {gs}  ·  <b>location not set</b> — set in "
                "Radio settings tab")
            self._loc_summary.setStyleSheet(
                "color: #ff8c00; font-size: 11px; font-weight: 600;")
        else:
            self._loc_summary.setText(
                f"{cs}  ·  {gs}  ·  {lat:+.3f}°, {lon:+.3f}°  "
                "(set in Radio settings tab)")
            self._loc_summary.setStyleSheet(
                "color: #8fafc0; font-size: 11px; font-style: italic;")
        self._loc_summary.setTextFormat(Qt.RichText)


class BandsSettingsTab(QWidget):
    """Bands tab — a lightweight tab-of-tabs that hosts band-related
    Settings sub-tabs.

    Sub-tabs:
      * Memory       — full table view + edit / delete / reorder /
                       CSV import / export of operator memory
                       presets.
      * Time Stations — placeholder for now; quick selection is via
                       right-click on the TIME button.  A full
                       Settings sub-tab arrives in a future
                       iteration.
      * SW Database  — EiBi master enable + download + power
                       filter + overlay behavior + per-band
                       suppression.

    Each sub-tab is a self-contained QWidget; this class just wires
    them into the inner QTabWidget.
    """

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self._inner_tabs = QTabWidget()
        # Memory sub-tab: full management UI for the 20-slot bank.
        self._memory_subtab = _MemorySubTab(radio, self)
        self._inner_tabs.addTab(self._memory_subtab, "Memory")
        # Time Stations placeholder.  Right-click menu on the TIME
        # button covers daily operating; Settings management
        # (per-station hide / reorder / mode override) lands later.
        time_placeholder = QLabel(
            "Time Stations management — coming in a follow-up "
            "release.\n\nUse the right-click menu on the TIME "
            "button on the Bands panel for direct station picks."
        )
        time_placeholder.setAlignment(Qt.AlignCenter)
        time_placeholder.setStyleSheet(
            "color: #5a7080; padding: 40px;")
        time_placeholder.setWordWrap(True)
        self._inner_tabs.addTab(time_placeholder, "Time Stations")
        # SW Database sub-tab (v0.0.9 Step 4b).  Covers EiBi
        # download, master enable, power filter, and overlay
        # behavior.  Step 4c adds the panadapter rendering.
        self._sw_subtab = _SwDatabaseSubTab(radio, self)
        self._inner_tabs.addTab(self._sw_subtab, "SW Database")
        v.addWidget(self._inner_tabs)

    def show_memory_subtab(self) -> None:
        """Switch the inner tab widget to Memory.  Called when the
        operator picks 'Manage presets…' from the Mem button's
        right-click menu."""
        for i in range(self._inner_tabs.count()):
            if self._inner_tabs.tabText(i).lower() == "memory":
                self._inner_tabs.setCurrentIndex(i)
                return


class _MemorySubTab(QWidget):
    """Full-management UI for the 20-slot operator memory bank.

    Loaded from the same ``MemoryStore`` instance used by the Bands
    panel's Mem button -- mutations here are reflected there
    immediately because both views read the same QSettings-backed
    JSON list.

    Operator-facing actions:
      * Edit selected row (also via double-click on a row)
      * Delete selected row (with confirm dialog)
      * Move Up / Move Down to reorder
      * Import CSV (merge or replace)
      * Export CSV
      * Clear All (with explicit confirm typing dialog)
    """

    HEADERS = ("#", "Name", "Frequency", "Mode", "Bandwidth", "Notes")

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        # Reuse the singleton MemoryStore the Bands panel uses.
        # Both load from the same QSettings key, so mutations
        # round-trip cleanly even though we technically have two
        # in-memory copies during the dialog's lifetime.  On
        # close, the panel's MemoryStore reloads from QSettings
        # if needed.
        from lyra.memory import MemoryStore
        self._store = MemoryStore()

        v = QVBoxLayout(self)
        # Status line at top: "X of 20 saved".
        self._status = QLabel("")
        self._status.setStyleSheet(
            "color: #80a0b0; font-size: 11px;")
        v.addWidget(self._status)

        # ── Table ────────────────────────────────────────────────
        from PySide6.QtWidgets import (
            QAbstractItemView, QHeaderView, QTableWidget,
            QTableWidgetItem,
        )
        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        # Single-row selection so up/down/edit/delete are
        # unambiguous.
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        # Column sizing: # narrow, Freq + Mode + BW snug, Name +
        # Notes stretch.
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # #
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)            # Name
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # Freq
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # Mode
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # BW
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)            # Notes
        self._table.itemDoubleClicked.connect(
            lambda _: self._on_edit_clicked())
        v.addWidget(self._table, 1)

        # ── Action button row ────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        btn_row.addWidget(self._edit_btn)
        self._delete_btn = QPushButton("Delete…")
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        btn_row.addWidget(self._delete_btn)
        self._up_btn = QPushButton("Move Up")
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        btn_row.addWidget(self._up_btn)
        self._down_btn = QPushButton("Move Down")
        self._down_btn.clicked.connect(lambda: self._move_selected(+1))
        btn_row.addWidget(self._down_btn)
        btn_row.addStretch(1)
        # CSV import/export + clear all on the right.
        self._import_btn = QPushButton("Import CSV…")
        self._import_btn.clicked.connect(self._on_import_csv)
        btn_row.addWidget(self._import_btn)
        self._export_btn = QPushButton("Export CSV…")
        self._export_btn.clicked.connect(self._on_export_csv)
        btn_row.addWidget(self._export_btn)
        self._clear_btn = QPushButton("Clear All…")
        self._clear_btn.clicked.connect(self._on_clear_all)
        btn_row.addWidget(self._clear_btn)
        v.addLayout(btn_row)

        # CSV format hint below the buttons.
        hint = QLabel(
            "<i>CSV columns: Name, Freq_Hz, Mode, RX_BW_Hz, Notes."
            "  Example row:</i><br>"
            "<code>\"My 40m FT8\",7074000,DIGU,3000,"
            "\"weekend digital ops\"</code>")
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color: #80a0b0; font-size: 11px; padding-top: 8px;")
        v.addWidget(hint)

        # Initial population.
        self._refresh()

    # ── Refresh + status ─────────────────────────────────────────

    def _refresh(self) -> None:
        """Rebuild the table from the current store state."""
        from PySide6.QtWidgets import QTableWidgetItem
        # Reload from disk in case panel-side mutations happened
        # outside this dialog's lifetime.
        from lyra.memory import MemoryStore
        self._store = MemoryStore()
        presets = self._store.list()
        self._table.setRowCount(len(presets))
        for row, p in enumerate(presets):
            cells = [
                str(row + 1),
                p.name,
                f"{p.freq_hz/1e6:.4f} MHz",
                p.mode,
                (f"{p.rx_bw_hz} Hz"
                 if p.rx_bw_hz is not None else "(default)"),
                p.notes,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col in (0, 2, 3, 4):
                    item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(row, col, item)
        # Status line.
        cap = self._store.MAX_PRESETS
        self._status.setText(
            f"<b>{len(presets)}</b> of <b>{cap}</b> presets saved")
        # Button enable state.
        has_rows = len(presets) > 0
        self._edit_btn.setEnabled(has_rows)
        self._delete_btn.setEnabled(has_rows)
        self._up_btn.setEnabled(has_rows)
        self._down_btn.setEnabled(has_rows)
        self._export_btn.setEnabled(has_rows)
        self._clear_btn.setEnabled(has_rows)

    def _selected_row(self) -> int:
        """Return the currently-selected row index, or -1 if none."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    # ── Actions ──────────────────────────────────────────────────

    def _on_edit_clicked(self) -> None:
        """Edit the selected preset (or first row if none selected)
        via a small dialog with all fields populated."""
        row = self._selected_row()
        if row < 0:
            return
        preset = self._store.get(row)
        if preset is None:
            return
        new = self._edit_dialog(preset)
        if new is None:
            return
        # Name-collision check (skip the current row).
        existing_idx = self._store.find_by_name(new.name)
        if existing_idx is not None and existing_idx != row:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Name collision",
                f"Another preset is already named '{new.name}'.\n"
                "Pick a different name or rename the other one first.")
            return
        self._store.update(row, new)
        self._refresh()

    def _on_delete_clicked(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        row = self._selected_row()
        if row < 0:
            return
        preset = self._store.get(row)
        if preset is None:
            return
        confirm = QMessageBox.question(
            self, "Delete preset?",
            f"Delete '{preset.name}' "
            f"({preset.freq_hz/1e6:.4f} MHz {preset.mode})?\n\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        self._store.delete(row)
        self._refresh()

    def _move_selected(self, direction: int) -> None:
        row = self._selected_row()
        if row < 0:
            return
        target = row + direction
        if target < 0 or target >= self._store.count:
            return
        self._store.move(row, target)
        self._refresh()
        # Re-select the moved row at its new position.
        self._table.selectRow(target)

    def _on_clear_all(self) -> None:
        from PySide6.QtWidgets import (
            QInputDialog, QMessageBox,
        )
        # Two-step confirm: first Yes/No, then "type CLEAR" so the
        # operator can't accidentally wipe their bank.
        confirm = QMessageBox.question(
            self, "Clear ALL presets?",
            f"Permanently delete ALL {self._store.count} memory "
            "presets?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        text, ok = QInputDialog.getText(
            self, "Type CLEAR to confirm",
            "Type CLEAR (uppercase) to confirm the wipe, or "
            "cancel to keep your presets:")
        if not ok or text.strip() != "CLEAR":
            return
        self._store.clear()
        self._refresh()

    def _on_export_csv(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export memory presets",
            "lyra_memory_presets.csv",
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            self._write_csv(path)
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Export failed", f"Could not write '{path}': {e}")
            return
        try:
            self.radio.status_message.emit(
                f"Exported {self._store.count} presets to "
                f"{path}", 2500)
        except Exception:
            pass

    def _write_csv(self, path: str) -> None:
        import csv
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                ["Name", "Freq_Hz", "Mode", "RX_BW_Hz", "Notes"])
            for p in self._store.list():
                w.writerow([
                    p.name,
                    p.freq_hz,
                    p.mode,
                    p.rx_bw_hz if p.rx_bw_hz is not None else "",
                    p.notes,
                ])

    def _on_import_csv(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(
            self, "Import memory presets",
            "",
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            new_presets, errors = self._parse_csv(path)
        except OSError as e:
            QMessageBox.warning(
                self, "Import failed",
                f"Could not read '{path}': {e}")
            return
        if errors:
            details = "\n".join(errors[:10])
            extra = (f"\n... and {len(errors) - 10} more"
                     if len(errors) > 10 else "")
            QMessageBox.warning(
                self, "Import warnings",
                f"{len(errors)} row(s) skipped due to errors:\n\n"
                f"{details}{extra}")
        if not new_presets:
            QMessageBox.information(
                self, "Nothing imported",
                "No valid presets found in the file.")
            return
        # Merge or replace prompt.
        action = self._ask_merge_or_replace(len(new_presets))
        if action == "cancel":
            return
        from lyra.memory import MemoryStore
        cap = MemoryStore.MAX_PRESETS
        if action == "replace":
            self._store.clear()
        # Cap-aware merge.
        added = 0
        for p in new_presets:
            if self._store.at_max:
                break
            existing_idx = self._store.find_by_name(p.name)
            if existing_idx is not None:
                # Overwrite same-name on import.  Operator opted
                # in by selecting the file.
                self._store.update(existing_idx, p)
            else:
                self._store.add(p)
            added += 1
        self._refresh()
        if self._store.at_max and len(new_presets) > added:
            QMessageBox.information(
                self, "Imported up to bank limit",
                f"Imported {added} of {len(new_presets)} presets "
                f"(bank capped at {cap}).")
        try:
            self.radio.status_message.emit(
                f"Imported {added} presets from {path}", 2500)
        except Exception:
            pass

    def _parse_csv(self, path: str):
        """Parse a CSV file into a list of MemoryPreset.  Returns
        (presets, errors) where errors is a list of human-readable
        messages for malformed rows."""
        import csv
        from lyra.memory import MemoryPreset
        presets = []
        errors = []
        with open(path, "r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for line_no, row in enumerate(reader, start=2):
                try:
                    name = (row.get("Name") or "").strip()
                    if not name:
                        errors.append(
                            f"line {line_no}: missing Name")
                        continue
                    freq_str = (row.get("Freq_Hz") or "").strip()
                    if not freq_str:
                        errors.append(
                            f"line {line_no}: missing Freq_Hz")
                        continue
                    freq = int(float(freq_str))
                    mode = (row.get("Mode") or "").strip().upper()
                    if not mode:
                        errors.append(
                            f"line {line_no}: missing Mode")
                        continue
                    rx_bw_raw = (row.get("RX_BW_Hz") or "").strip()
                    rx_bw = int(rx_bw_raw) if rx_bw_raw else None
                    notes = (row.get("Notes") or "").strip()
                    presets.append(MemoryPreset(
                        name=name, freq_hz=freq, mode=mode,
                        rx_bw_hz=rx_bw, notes=notes))
                except (ValueError, TypeError) as e:
                    errors.append(
                        f"line {line_no}: malformed -- {e}")
                    continue
        return presets, errors

    def _ask_merge_or_replace(self, n_new: int) -> str:
        """Three-button dialog asking whether to MERGE the imported
        presets into the existing bank or REPLACE the bank entirely.
        Returns 'merge', 'replace', or 'cancel'."""
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("Import memory presets")
        box.setIcon(QMessageBox.Question)
        existing = self._store.count
        box.setText(
            f"Found {n_new} preset(s) in the file.\n"
            f"Current bank has {existing} preset(s).\n\n"
            "How do you want to import?")
        merge_btn = box.addButton(
            "Merge (keep existing)", QMessageBox.AcceptRole)
        replace_btn = box.addButton(
            "Replace (clear existing first)",
            QMessageBox.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(merge_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is replace_btn:
            return "replace"
        if clicked is cancel_btn:
            return "cancel"
        return "merge"

    # ── Edit dialog ──────────────────────────────────────────────

    def _edit_dialog(self, preset):
        """Show edit dialog pre-populated from ``preset``.  Returns
        the new MemoryPreset on Save, or None on Cancel."""
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
            QSpinBox,
        )
        from lyra.memory import MemoryPreset, MemoryStore
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit '{preset.name}'")
        form = QFormLayout(dlg)
        name_edit = QLineEdit(preset.name, dlg)
        name_edit.setMaxLength(MemoryStore.MAX_NAME_LEN)
        form.addRow("Name:", name_edit)
        # Frequency in Hz, displayed in MHz for readability.
        freq_edit = QLineEdit(f"{preset.freq_hz / 1e6:.6f}", dlg)
        form.addRow("Frequency (MHz):", freq_edit)
        mode_edit = QLineEdit(preset.mode, dlg)
        form.addRow("Mode:", mode_edit)
        # Bandwidth: 0 / blank means "use mode default".
        bw_edit = QLineEdit(
            str(preset.rx_bw_hz) if preset.rx_bw_hz is not None else "",
            dlg)
        form.addRow("Bandwidth Hz (blank = default):", bw_edit)
        notes_edit = QLineEdit(preset.notes, dlg)
        notes_edit.setMaxLength(MemoryStore.MAX_NOTES_LEN)
        form.addRow("Notes:", notes_edit)
        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=dlg)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.Accepted:
            return None
        # Validate.
        try:
            new_name = name_edit.text().strip()
            if not new_name:
                raise ValueError("name is required")
            new_freq = int(float(freq_edit.text().strip()) * 1e6)
            new_mode = mode_edit.text().strip().upper()
            if not new_mode:
                raise ValueError("mode is required")
            bw_text = bw_edit.text().strip()
            new_bw = int(bw_text) if bw_text else None
            new_notes = notes_edit.text().strip()
        except (ValueError, TypeError) as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Invalid input", f"Edit rejected: {e}")
            return None
        return MemoryPreset(
            name=new_name, freq_hz=new_freq, mode=new_mode,
            rx_bw_hz=new_bw, notes=new_notes)


class _SwDatabaseSubTab(QWidget):
    """Settings UI for the EiBi SW broadcaster overlay (v0.0.9
    Step 4b).

    Sections (top to bottom):
      1. Master enable -- single checkbox that gates the overlay.
      2. Database status -- file path, age, source label,
         "Update database now" button driving EibiDownloader.
      3. Display filters -- "show stations" power-level radios
         (operator-friendly labels per design doc §3), the
         "hide off-air" checkbox, the "show on amateur bands too"
         force-on override.
      4. Attribution + folder-open helper.
    """

    SETTING_MASTER_ENABLED = "swdb/overlay_master_enabled"
    SETTING_HIDE_OFF_AIR = "swdb/hide_off_air"
    SETTING_FORCE_ALL_BANDS = "swdb/overlay_force_all_bands"
    SETTING_MIN_POWER = "swdb/min_power"

    POWER_OPTIONS = [
        (0, "All stations (most cluttered)"),
        (1, "Likely receivable (recommended)"),
        (2, "Strong stations only"),
        (3, "Mega-stations only"),
    ]

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        self._downloader = None  # lazy on first download
        from PySide6.QtCore import QSettings as _QS
        self._qs = _QS("N8SDR", "Lyra")

        v = QVBoxLayout(self)
        v.setSpacing(12)

        # ── 1. Master enable ─────────────────────────────────
        self._enable_cb = QCheckBox("Enable EiBi overlay")
        self._enable_cb.setChecked(self._read_bool(
            self.SETTING_MASTER_ENABLED, default=False))
        self._enable_cb.toggled.connect(self._on_enable_toggled)
        v.addWidget(self._enable_cb)
        info = QLabel(
            "<i>When enabled, broadcaster station labels appear on "
            "the panadapter automatically whenever the VFO is "
            "outside the amateur bands of your selected region "
            "(Settings &rarr; Operator &rarr; Region).  Inside "
            "amateur bands no labels are drawn -- EiBi covers "
            "shortwave broadcasters, not amateur activity.</i>")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        info.setStyleSheet(
            "color: #80a0b0; font-size: 11px;")
        v.addWidget(info)

        # ── 2. Database status + Update button ───────────────
        db_box = QGroupBox("Database")
        db_layout = QVBoxLayout(db_box)
        self._status_label = QLabel("")
        self._status_label.setTextFormat(Qt.RichText)
        self._status_label.setWordWrap(True)
        db_layout.addWidget(self._status_label)
        # Auto-download row.
        row = QHBoxLayout()
        self._update_btn = QPushButton("Update database now")
        self._update_btn.clicked.connect(self._on_update_clicked)
        row.addWidget(self._update_btn)
        self._open_folder_btn = QPushButton("Open database folder")
        self._open_folder_btn.clicked.connect(
            self._on_open_folder_clicked)
        row.addWidget(self._open_folder_btn)
        row.addStretch(1)
        db_layout.addLayout(row)

        # v0.0.9 4c hotfix: prominent manual-install row for the
        # cases where automatic downloads fail (TLS issues,
        # firewall blocking outbound HTTPS to the eibispace.de
        # mirrors, network policy, etc.).  Two buttons + a
        # hyperlink hint -- operator opens the EiBi site in their
        # browser, downloads the CSV manually, then points Lyra at
        # the file.  This row is ALWAYS visible (not just on
        # error) so operators can find the manual path even if
        # they prefer it for any reason.
        manual_box = QGroupBox("Manual install (when downloads fail)")
        manual_layout = QVBoxLayout(manual_box)
        manual_hint = QLabel(
            "<p>If <b>Update database now</b> can't reach EiBi "
            "(SSL errors, firewall, etc.), download the file "
            "manually:</p>"
            "<ol>"
            "<li>Click <b>Open EiBi downloads page</b> below.  "
            "<b>Note:</b> EiBi's site uses HTTP, not HTTPS -- "
            "your browser may warn that the connection is "
            "<i>not secure</i>.  That's safe to accept for this "
            "site -- it's a free public broadcast schedule, no "
            "passwords or personal data are exchanged.  In "
            "Chrome / Edge: click <b>Advanced</b> &rarr; "
            "<b>Continue to site</b>.  If you have HTTPS-only "
            "mode on, temporarily disable it for this domain.</li>"
            "<li>Find and right-click the current season's "
            "<code>sked-A##.csv</code> or "
            "<code>sked-B##.csv</code> file -- choose "
            "<b>Save link as…</b> -- save anywhere (~3 MB).</li>"
            "<li>Click <b>Load local CSV…</b> below and pick "
            "the file -- Lyra copies it into the right folder "
            "and loads it.</li>"
            "</ol>"
            "<p style='color:#a0b0c0;'><i>If the URL is also "
            "blocked by your network (not just TLS), use the "
            "<b>Copy URL</b> button to grab the address and try "
            "a different download tool.</i></p>")
        manual_hint.setWordWrap(True)
        manual_hint.setTextFormat(Qt.RichText)
        manual_layout.addWidget(manual_hint)
        manual_row = QHBoxLayout()
        self._open_eibi_btn = QPushButton(
            "🌐  Open EiBi downloads page")
        self._open_eibi_btn.setToolTip(
            "Opens http://www.eibispace.de/dx/ in your browser.\n"
            "(EiBi's site is HTTP only -- browsers will say\n"
            "'Not secure' but it's safe to accept; it's a free\n"
            "public broadcast schedule, no auth.)")
        self._open_eibi_btn.clicked.connect(
            self._on_open_eibi_site_clicked)
        manual_row.addWidget(self._open_eibi_btn)
        # Copy-URL button as a workaround when the browser
        # outright refuses to load the page (HTTPS-only mode,
        # corporate proxy, etc.) -- operator pastes into wget /
        # curl / a different download client.
        self._copy_url_btn = QPushButton("📋  Copy URL")
        self._copy_url_btn.setToolTip(
            "Copy http://www.eibispace.de/dx/ to your clipboard\n"
            "so you can paste into wget / curl / another browser.")
        self._copy_url_btn.clicked.connect(self._on_copy_url_clicked)
        manual_row.addWidget(self._copy_url_btn)
        self._load_local_btn = QPushButton("📁  Load local CSV…")
        self._load_local_btn.setToolTip(
            "Pick a sked-XX.csv you've already downloaded.\n"
            "Lyra validates it parses, copies it into the\n"
            "standard swdb folder, and loads it.")
        self._load_local_btn.clicked.connect(
            self._on_load_local_clicked)
        manual_row.addWidget(self._load_local_btn)
        manual_row.addStretch(1)
        manual_layout.addLayout(manual_row)
        db_layout.addWidget(manual_box)
        # Progress label / bar (visible only during download).
        from PySide6.QtWidgets import QProgressBar
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setRange(0, 0)  # indeterminate by default
        db_layout.addWidget(self._progress)
        self._progress_text = QLabel("")
        self._progress_text.setVisible(False)
        self._progress_text.setStyleSheet(
            "color: #80a0b0; font-size: 11px;")
        db_layout.addWidget(self._progress_text)
        v.addWidget(db_box)

        # ── 3. Display filters ───────────────────────────────
        flt_box = QGroupBox("Show stations")
        flt_layout = QVBoxLayout(flt_box)
        self._power_group = QButtonGroup(self)
        current_min_power = int(self._qs.value(
            self.SETTING_MIN_POWER, 1) or 1)
        for level, label in self.POWER_OPTIONS:
            rb = QRadioButton(label)
            self._power_group.addButton(rb, level)
            if level == current_min_power:
                rb.setChecked(True)
            flt_layout.addWidget(rb)
        self._power_group.idClicked.connect(self._on_power_changed)
        v.addWidget(flt_box)

        # Hide off-air + force-all checkboxes.
        self._hide_off_air_cb = QCheckBox(
            "Hide stations not currently on-air")
        self._hide_off_air_cb.setChecked(self._read_bool(
            self.SETTING_HIDE_OFF_AIR, default=True))
        self._hide_off_air_cb.toggled.connect(
            lambda c: self._qs.setValue(
                self.SETTING_HIDE_OFF_AIR, bool(c)))
        v.addWidget(self._hide_off_air_cb)

        self._force_all_cb = QCheckBox(
            "Show overlay on amateur bands too (advanced)")
        self._force_all_cb.setChecked(self._read_bool(
            self.SETTING_FORCE_ALL_BANDS, default=False))
        self._force_all_cb.toggled.connect(
            lambda c: self._qs.setValue(
                self.SETTING_FORCE_ALL_BANDS, bool(c)))
        force_hint = QLabel(
            "<i>Bypass the band-plan auto-detect.  Useful for "
            "identifying broadcast QRM bleeding into amateur "
            "bands; off otherwise.</i>")
        force_hint.setWordWrap(True)
        force_hint.setTextFormat(Qt.RichText)
        force_hint.setStyleSheet(
            "color: #80a0b0; font-size: 11px; padding-left: 20px;")
        v.addWidget(self._force_all_cb)
        v.addWidget(force_hint)

        # ── 4. Attribution ───────────────────────────────────
        attribution = QLabel(
            "<small>Data: <a href='https://www.eibispace.de/'>"
            "EiBi (Eike Bierwirth)</a> &mdash; free for "
            "non-commercial use, attribution required.  Lyra "
            "does not redistribute the data; you download it "
            "yourself via the button above.</small>")
        attribution.setOpenExternalLinks(True)
        attribution.setTextFormat(Qt.RichText)
        attribution.setWordWrap(True)
        attribution.setStyleSheet(
            "color: #80a0b0; font-size: 11px; padding-top: 10px;")
        v.addWidget(attribution)
        v.addStretch(1)

        self._refresh_status()

    # ── Helpers ──────────────────────────────────────────────

    def _read_bool(self, key: str, default: bool) -> bool:
        val = self._qs.value(key, default)
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "yes")

    def _refresh_status(self) -> None:
        store = self.radio.eibi_store
        if store.loaded:
            from datetime import datetime, timezone
            age_days = ""
            if store.loaded_at is not None:
                delta = (
                    datetime.now(timezone.utc) - store.loaded_at)
                if delta.days > 0:
                    age_days = f", loaded {delta.days} day(s) ago"
                else:
                    hours = delta.seconds // 3600
                    age_days = (
                        f", loaded {hours} hour(s) ago"
                        if hours else ", loaded just now")
            label = store.source_label or "(unknown)"
            entry_count = store.count
            self._status_label.setText(
                f"<p>Currently loaded: <b>{label}</b> "
                f"({entry_count:,} entries{age_days})</p>"
                f"<p style='color:#80a0b0; font-size:11px;'>"
                f"File: <code>{store.source_path}</code></p>")
            self._open_folder_btn.setEnabled(True)
        else:
            path = self.radio._eibi_default_path()
            self._status_label.setText(
                "<p><b>No database loaded yet.</b></p>"
                "<p>Click <i>Update database now</i> to download "
                f"the current season's CSV from "
                "<code>https://www.eibispace.de/</code> (~3 MB).</p>"
                f"<p style='color:#80a0b0; font-size:11px;'>"
                f"Will save to: <code>{path}</code></p>")
            # The folder might not exist yet, but we'll create
            # it when the operator clicks Open.
            self._open_folder_btn.setEnabled(True)

    def _on_enable_toggled(self, checked: bool) -> None:
        self._qs.setValue(self.SETTING_MASTER_ENABLED, bool(checked))
        # Ping the radio so subscribers (the panadapter, when
        # Step 4c lands) refresh their cached gate state.
        try:
            self.radio.eibi_store_changed.emit()
        except Exception:
            pass

    def _on_power_changed(self, button_id: int) -> None:
        self._qs.setValue(self.SETTING_MIN_POWER, int(button_id))
        try:
            self.radio.eibi_store_changed.emit()
        except Exception:
            pass

    def _on_update_clicked(self) -> None:
        """Kick off a background download of the current season's
        EiBi CSV.  Disables the button while in flight; shows
        progress."""
        from pathlib import Path
        from PySide6.QtCore import QStandardPaths
        from PySide6.QtWidgets import QMessageBox
        if (self._downloader is not None
                and self._downloader.is_running()):
            QMessageBox.information(
                self, "Download in progress",
                "A download is already in progress.")
            return
        appdata = QStandardPaths.writableLocation(
            QStandardPaths.AppLocalDataLocation)
        if not appdata:
            QMessageBox.warning(
                self, "No writable data location",
                "Lyra couldn't determine a writable app-data "
                "folder.  Set swdb/file_path in QSettings to a "
                "location you can write to.")
            return
        dest_dir = Path(appdata) / "swdb"

        from lyra.swdb.downloader import EibiDownloader
        import lyra
        if self._downloader is None:
            self._downloader = EibiDownloader(
                self,
                user_agent_version=getattr(lyra, "__version__", ""))
            self._downloader.progress.connect(self._on_download_progress)
            self._downloader.status.connect(
                lambda s: self._progress_text.setText(s))
            self._downloader.finished_ok.connect(
                self._on_download_ok)
            self._downloader.finished_error.connect(
                self._on_download_error)
        # Show progress UI.
        self._update_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._progress_text.setVisible(True)
        self._progress_text.setText("Connecting to eibispace.de…")
        self._downloader.fetch(dest_dir=dest_dir, season="auto")

    def _on_download_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(done)
            pct = int(done * 100 / total) if total else 0
            self._progress_text.setText(
                f"Downloading: {done:,} / {total:,} bytes "
                f"({pct}%)")
        else:
            self._progress_text.setText(
                f"Downloading: {done:,} bytes (size unknown)")

    def _on_download_ok(self, path: str, byte_count: int) -> None:
        from pathlib import Path
        # Save the path to QSettings so future startups know
        # which file to load.
        self._qs.setValue("swdb/file_path", path)
        # Reload the store from the new file.
        try:
            self.radio.reload_eibi_store(Path(path))
            self._progress_text.setText(
                f"Done -- {byte_count:,} bytes saved.  Loaded "
                f"{self.radio.eibi_store.count:,} entries.")
        except Exception as e:
            self._progress_text.setText(
                f"Downloaded but parse failed: {e}")
        self._progress.setVisible(False)
        self._update_btn.setEnabled(True)
        self._refresh_status()
        # Hide progress text after a short delay so the operator
        # can read the success message.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(
            5000, lambda: self._progress_text.setVisible(False))

    def _on_download_error(self, msg: str) -> None:
        self._progress.setVisible(False)
        self._progress_text.setText(f"Update failed: {msg}")
        self._update_btn.setEnabled(True)
        # Leave the error text visible; clears on next interaction.

    EIBI_MANUAL_URL = "http://www.eibispace.de/dx/"

    def _on_open_eibi_site_clicked(self) -> None:
        """Open the EiBi downloads directory in the operator's
        default browser so they can pick the season CSV they want.

        Uses HTTP (not HTTPS) because EiBi's TLS cert chain is
        broken (cert issued for the apex but the site canonical
        is www); HTTP works around that.  Browsers flag the
        connection as "Not secure" but the operator can click
        through.  See the GroupBox hint text for the
        operator-facing explanation.
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(self.EIBI_MANUAL_URL))

    def _on_copy_url_clicked(self) -> None:
        """Copy the EiBi downloads URL to the clipboard so the
        operator can paste into wget / curl / another browser
        if Lyra's open-in-default-browser is blocked."""
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        cb.setText(self.EIBI_MANUAL_URL)
        try:
            self.radio.status_message.emit(
                f"Copied to clipboard: {self.EIBI_MANUAL_URL}",
                3000)
        except Exception:
            pass

    def _on_load_local_clicked(self) -> None:
        """Operator-side fallback when network downloads fail.
        Pops a file dialog, lets the operator pick a CSV they've
        already downloaded manually, validates it parses, and
        copies it into the standard swdb folder so future
        startups auto-load.
        """
        from pathlib import Path
        import shutil
        from PySide6.QtCore import QStandardPaths
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Load EiBi CSV",
            "",
            "EiBi CSV (sked-*.csv);;CSV files (*.csv);;All files (*)")
        if not path_str:
            return
        src = Path(path_str)
        # Validate parses.
        from lyra.swdb.eibi_parser import parse_csv
        try:
            entries, errors = parse_csv(src)
        except Exception as e:
            QMessageBox.warning(
                self, "Couldn't parse file",
                f"'{src.name}' didn't parse as an EiBi CSV:\n\n{e}")
            return
        if not entries:
            QMessageBox.warning(
                self, "No entries found",
                f"'{src.name}' parsed but had 0 valid entries.\n\n"
                "Are you sure this is an EiBi CSV?  If so, the "
                "file may be corrupt -- try re-downloading.")
            return
        # Copy into the standard swdb folder so future startups
        # auto-load.
        appdata = QStandardPaths.writableLocation(
            QStandardPaths.AppLocalDataLocation)
        if not appdata:
            QMessageBox.warning(
                self, "No writable data location",
                "Lyra couldn't determine a writable app-data folder.")
            return
        dest_dir = Path(appdata) / "swdb"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        try:
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
        except OSError as e:
            QMessageBox.warning(
                self, "Couldn't copy file", f"{e}")
            return
        # Update QSettings + reload.
        self._qs.setValue("swdb/file_path", str(dest))
        try:
            self.radio.reload_eibi_store(dest)
        except Exception as e:
            QMessageBox.warning(
                self, "Loaded but couldn't index",
                f"File copied to {dest} but the in-memory load "
                f"failed: {e}")
            return
        self._progress_text.setVisible(True)
        self._progress_text.setText(
            f"Loaded {len(entries):,} entries from "
            f"{src.name} ({len(errors)} skipped malformed rows)")
        self._refresh_status()
        try:
            self.radio.status_message.emit(
                f"EiBi loaded: {len(entries):,} entries", 3000)
        except Exception:
            pass

    def _on_open_folder_clicked(self) -> None:
        """Open the operating-system file manager at the swdb
        folder.  Creates the folder first if it doesn't exist
        yet (so first-time use isn't a dead-end click)."""
        from pathlib import Path
        from PySide6.QtCore import (
            QStandardPaths, QUrl,
        )
        from PySide6.QtGui import QDesktopServices
        path = self.radio._eibi_default_path()
        folder = path.parent if path is not None else None
        if folder is None:
            appdata = QStandardPaths.writableLocation(
                QStandardPaths.AppLocalDataLocation)
            folder = Path(appdata) / "swdb" if appdata else None
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class SettingsDialog(QDialog):
    """App-wide tabbed settings — accessed from the main toolbar (⚙).

    Tab order (matches reference SDR clients layout):
      Radio → Network/TCI → Hardware → DSP → Noise → Audio →
      Visuals → Keyer → Bands → Weather
    """

    def __init__(self, radio, tci_server: TciServer, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lyra — Settings")
        # Open wide by default — at 640×560 the dense Visuals tab in
        # particular was cramped even on a 27" display. The dialog
        # remains resizable so operators on smaller monitors can pull
        # it tighter; minimum keeps it from collapsing into nothing.
        # Initial size bumped 1100x760 -> 1280x880 (operator-reported
        # 2026-05-09): on tabs with denser content (Visuals, Bands,
        # Audio) the default 1100x760 was clipping label text or
        # cutting off rows at the bottom, forcing the operator to
        # drag-resize on every open.  Going taller used to push the
        # bottom off-screen, requiring close-and-reopen to recover.
        # 1280x880 fits all tabs without scroll/clip on a typical
        # 1080p+ display while still leaving 200 px of margin below
        # on a 1080p screen.  Operators on smaller displays can
        # still pull it tighter — minimumSize unchanged.
        self.resize(1280, 880)
        self.setMinimumSize(640, 480)

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

        # Noise tab — Captured Profile + NB + ANF + Squelch.
        # Lives on its own tab to keep DSP from getting overcrowded.
        self.tab_noise = NoiseSettingsTab(radio)
        self.tabs.addTab(self.tab_noise, "Noise")

        self.tab_audio = AudioSettingsTab(radio)
        self.tabs.addTab(self.tab_audio, "Audio")

        self.tab_tx = TxSettingsTab(radio)
        self.tabs.addTab(self.tab_tx, "TX")

        self.tab_visuals = VisualsSettingsTab(radio)
        self.tabs.addTab(self.tab_visuals, "Visuals")

        # Keyer placeholder still pending.
        keyer_placeholder = QLabel("Keyer settings — coming soon.")
        keyer_placeholder.setAlignment(Qt.AlignCenter)
        keyer_placeholder.setStyleSheet("color: #5a7080; padding: 40px;")
        self.tabs.addTab(keyer_placeholder, "Keyer")
        # v0.0.9: Bands tab is now real -- hosts Memory + Time
        # Stations + SW Database sub-tabs.
        self.tab_bands = BandsSettingsTab(radio)
        self.tabs.addTab(self.tab_bands, "Bands")

        # Propagation — NCDXF marker toggle + clock-accuracy reference.
        # The live solar/band/Follow controls live on the dock
        # (View → Propagation); this tab is the home for the
        # persistent toggles that don't belong on the slim panel.
        self.tab_prop = PropagationSettingsTab(radio)
        self.tabs.addTab(self.tab_prop, "Propagation")

        # Phase 4 — Weather Alerts.  Lives last in the tab order
        # because it's an opt-in convenience feature (gated by an
        # explicit safety disclaimer) rather than a core radio
        # function.
        self.tab_wx = WxAlertsSettingsTab(radio)
        self.tabs.addTab(self.tab_wx, "Weather")

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
