"""Propagation panel — slim status strip showing solar + band conditions
plus an NCDXF beacon Follow dropdown.

UX intent (~30 px tall × ~580 px wide):

    ┌─ PROP ──────────────────────────────────────────── ▣ ✕ ─┐
    │  SFI 130   A 5   K 2  │  160 80 40 30 20 17 15 12 10 6  │  ▾ Follow │
    └────────────────────────────────────────────────────────┘

Three glance-readable groups, separated by thin dividers:

* Solar numbers (SFI / A / K) — color-coded against operator-tested
  HamQSL thresholds.  Hover the panel for an extended tooltip with
  SSN / X-Ray / solar wind / last-update timestamp.

* Band heatmap (160 / 80 / 40 / 30 / 20 / 17 / 15 / 12 / 10 / 6) —
  each band label is colored Good / Fair / Poor / unknown based on
  HamQSL's Day or Night prediction for the operator's local time
  (sunrise/sunset computed from QTH lat/lon).  Bands HamQSL doesn't
  cover (160m and 6m) render in muted gray.

* Follow dropdown — picks one of the 18 NCDXF stations to auto-tune
  through the rotation, or "Off" to disable.  Live spectrum-marker
  tooltips at 14.100 / 18.110 / 21.150 / 24.930 / 28.200 MHz show
  the current callsign on each band, so the panel doesn't need to
  duplicate that info.

Data is fetched via ``lyra.propagation.HamQslSolarCache`` (15 min
cache).  Refresh runs in the panel's QTimer (60 s tick to catch
sunrise/sunset transitions; HamQSL itself is only re-polled every
15 min).  No background thread — Qt timer runs on the main loop and
the urlopen is short.

The panel is operator-toggleable like every other Lyra dock.  See
``MainWindow.add_propagation_dock`` for wiring.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QToolButton,
)

from lyra.propagation import (
    HamQslSolarCache,
    NCDXF_STATIONS,
    hamqsl_rating_for_band,
    is_daylight,
    rating_color_hex,
)
from lyra.ui.panel import GlassPanel


class PropagationPanel(GlassPanel):
    """Slim solar + band-conditions + NCDXF-follow panel."""

    BAND_LABELS = ["160", "80", "40", "30", "20", "17", "15", "12", "10", "6"]
    REFRESH_INTERVAL_MS = 60 * 1000   # 60 sec — catches sunrise/sunset
    INITIAL_DELAY_MS = 1500            # avoid blocking startup

    # Solar number color thresholds — same operator-tested values
    # used in SDRLogger+ (Rick / N8SDR field-validated against
    # actual band conditions).  See lyra.propagation rating_color_hex
    # for the band-rating colors; these are a separate scale for
    # numeric solar values.
    GREEN  = "#4caf50"
    YELLOW = "#f0c040"
    RED    = "#e05c5c"
    DEFAULT = "#cdd9e5"

    @staticmethod
    def _sfi_color(sfi: float) -> str:
        if sfi >= 100: return PropagationPanel.GREEN
        if sfi >= 80:  return PropagationPanel.YELLOW
        return PropagationPanel.RED

    @staticmethod
    def _a_color(a: float) -> str:
        # Lower A-index = quieter geomag = better.
        if a <= 7:   return PropagationPanel.GREEN
        if a <= 19:  return PropagationPanel.YELLOW
        return PropagationPanel.RED

    @staticmethod
    def _k_color(k: float) -> str:
        # Lower K-index = quieter geomag = better.
        if k <= 2:   return PropagationPanel.GREEN
        if k == 3:   return PropagationPanel.YELLOW
        return PropagationPanel.RED

    def __init__(self, radio, parent=None):
        super().__init__("PROP", parent, help_topic="propagation")
        self.radio = radio
        self._solar_cache = HamQslSolarCache()
        self._last_data: Optional[dict] = None

        # ── Layout — single horizontal row, three groups ──────────────
        h = QHBoxLayout()
        h.setSpacing(10)
        h.setContentsMargins(0, 0, 0, 0)

        # Solar numbers
        self._sfi_label = self._make_value_label("SFI", "—")
        self._a_label   = self._make_value_label("A", "—")
        self._k_label   = self._make_value_label("K", "—")
        h.addWidget(self._sfi_label["box"])
        h.addWidget(self._a_label["box"])
        h.addWidget(self._k_label["box"])

        h.addWidget(self._make_divider())

        # Band heatmap — 10 colored band labels
        self._band_labels: dict[str, QLabel] = {}
        for b in self.BAND_LABELS:
            lbl = QLabel(b)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFixedWidth(32)
            lbl.setStyleSheet(self._band_style(rating_color_hex(None)))
            lbl.setToolTip(f"{b}m — no data yet")
            self._band_labels[b] = lbl
            h.addWidget(lbl)

        h.addWidget(self._make_divider())

        # NCDXF Follow dropdown — QToolButton with menu
        self._follow_btn = QToolButton()
        self._follow_btn.setText("▾  Follow: Off")
        self._follow_btn.setPopupMode(QToolButton.InstantPopup)
        self._follow_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._follow_btn.setStyleSheet(
            "QToolButton {"
            "  color: #cdd9e5; background: transparent;"
            "  border: 1px solid #2a3a4a; border-radius: 3px;"
            "  padding: 3px 10px; font-family: 'Segoe UI', sans-serif;"
            "  font-size: 13px; font-weight: 600;"
            "}"
            "QToolButton:hover { border-color: #4a90c2; }"
            "QToolButton::menu-indicator { width: 0; }"
        )
        self._follow_btn.setToolTip(
            "NCDXF Beacon Auto-Follow.\n"
            "Pick a station and Lyra auto-tunes through the rotation\n"
            "(20m → 17m → 15m → 12m → 10m every 10 sec).\n"
            "An SDR-only superpower — a knob radio operator would\n"
            "have to mash band-change manually every 10 sec.")
        self._build_follow_menu()
        h.addWidget(self._follow_btn)

        h.addStretch(1)
        self.content_layout().addLayout(h)

        # Two-way sync with Radio (CAT command, autoload, etc. land
        # via the signal so the dropdown label refreshes automatically).
        radio._ncdxf_follow_changed.connect(self._on_follow_changed)

        # Refresh timer — kicks off shortly after construction so we
        # don't slow down Lyra startup, then ticks every 60 sec.  The
        # underlying HamQslSolarCache only hits the network when its
        # 15-min TTL expires; the panel-side refresh is cheap (math +
        # color updates).
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self._refresh)
        QTimer.singleShot(self.INITIAL_DELAY_MS, self._refresh)
        self._refresh_timer.start()

    # ── Helpers ───────────────────────────────────────────────────────

    def _make_value_label(self, key: str, val: str) -> dict:
        """Build a "KEY  VAL" pair where KEY is muted and VAL is
        accent-colored (color set live by _refresh)."""
        from PySide6.QtWidgets import QHBoxLayout, QWidget
        box = QWidget()
        bl = QHBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(3)
        kl = QLabel(key)
        kl.setStyleSheet(
            "color: #7a8a9c; font-family: 'Segoe UI', sans-serif; "
            "font-size: 13px; font-weight: 600;")
        vl = QLabel(val)
        vl.setStyleSheet(
            "color: #cdd9e5; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 14px;")
        vl.setMinimumWidth(32)
        bl.addWidget(kl)
        bl.addWidget(vl)
        return {"box": box, "key": kl, "val": vl}

    def _make_divider(self) -> QLabel:
        d = QLabel("│")
        d.setStyleSheet("color: #2a3a4a; font-size: 16px;")
        d.setFixedWidth(7)
        return d

    @staticmethod
    def _band_style(color_hex: str) -> str:
        return (
            f"color: {color_hex};"
            "font-family: Consolas, monospace;"
            "font-size: 13px; font-weight: 700;"
            "padding: 3px 4px; border-radius: 3px;"
            "background: rgba(20, 30, 45, 120);"
        )

    # ── Follow menu ───────────────────────────────────────────────────

    def _build_follow_menu(self) -> None:
        """Construct the dropdown menu listing Off + 18 NCDXF stations.

        Uses a QActionGroup so exactly one entry shows as checked at a
        time, mirroring Radio's _ncdxf_follow_station state.
        """
        menu = QMenu(self._follow_btn)
        group = QActionGroup(self)
        group.setExclusive(True)

        off_act = QAction("Off  (no follow)", self)
        off_act.setCheckable(True)
        off_act.setChecked(self.radio.ncdxf_follow_station is None)
        off_act.triggered.connect(
            lambda: self.radio.set_ncdxf_follow_station(None))
        group.addAction(off_act)
        menu.addAction(off_act)
        menu.addSeparator()

        for callsign, desc, _, _ in NCDXF_STATIONS:
            act = QAction(f"{callsign}  ({desc})", self)
            act.setCheckable(True)
            act.setChecked(self.radio.ncdxf_follow_station == callsign)
            act.triggered.connect(
                lambda checked=False, c=callsign:
                    self.radio.set_ncdxf_follow_station(c))
            group.addAction(act)
            menu.addAction(act)

        self._follow_btn.setMenu(menu)
        self._follow_actions = group   # keep alive

    def _on_follow_changed(self, callsign: str) -> None:
        """Update the dropdown button label when Radio reports a
        follow-state change (operator picked from menu, autoload
        ran at startup, CAT command, etc.)."""
        if callsign:
            self._follow_btn.setText(f"▾  Follow: {callsign}")
        else:
            self._follow_btn.setText("▾  Follow: Off")
        # Keep the menu's checkmarks in sync.  The action group is
        # exclusive so toggling one un-checks the others; we just
        # find the matching action and check it.
        for act in self._follow_actions.actions():
            txt = act.text()
            if not callsign:
                act.setChecked(txt.startswith("Off"))
            else:
                act.setChecked(txt.startswith(callsign + "  ("))

    # ── Refresh — pulls solar data + recomputes band heatmap ──────────

    def _refresh(self) -> None:
        """Recompute everything from cached solar data + current time.

        Cheap: no network unless the 15-min HamQSL cache TTL expired.
        Sunrise/sunset re-evaluation is the main reason this fires
        every 60 sec — bands flip Day↔Night exactly when the sun
        crosses the horizon at the operator's QTH.
        """
        try:
            data = self._solar_cache.get()
        except Exception as exc:
            print(f"[PropagationPanel] solar fetch error: {exc}")
            data = self._last_data
        if data is None:
            # No cache yet and the fetch errored — leave placeholders.
            return
        self._last_data = data

        # Solar numbers — convert defensively, color via thresholds.
        self._update_value(self._sfi_label, "SFI", data.get("sfi"),
                           self._sfi_color)
        self._update_value(self._a_label, "A", data.get("aindex"),
                           self._a_color)
        self._update_value(self._k_label, "K", data.get("kindex"),
                           self._k_color)

        # Header tooltip — extended numbers + last-updated timestamp.
        sw = data.get("solarwind") or "—"
        ssn = data.get("sunspots") or "—"
        xray = data.get("xray") or "—"
        upd = data.get("updated") or "—"
        self.setToolTip(
            "<b>Propagation snapshot</b><br>"
            f"Solar Flux Index (SFI): {data.get('sfi') or '—'}<br>"
            f"Sunspot Number (SSN): {ssn}<br>"
            f"A-index: {data.get('aindex') or '—'} "
            f"&nbsp;&nbsp; K-index: {data.get('kindex') or '—'}<br>"
            f"X-Ray flux: {xray} &nbsp;&nbsp; "
            f"Solar wind: {sw} km/s<br>"
            f"<i>Updated: {upd}</i><br>"
            "<i>Source: hamqsl.com (15-min cache)</i>"
        )

        # Band heatmap — pick Day or Night per band based on
        # operator's QTH + current UTC.
        bands_dict = data.get("bands", {})
        op_lat = self.radio.operator_lat
        op_lon = self.radio.operator_lon
        if op_lat is None or op_lon is None:
            # No QTH set — fall back to Day rating.  Operator can
            # set their grid in Settings → Radio.
            day = True
        else:
            day = is_daylight(op_lat, op_lon,
                              datetime.now(timezone.utc))

        for b, lbl in self._band_labels.items():
            rating = hamqsl_rating_for_band(b, bands_dict, day)
            color = rating_color_hex(rating)
            lbl.setStyleSheet(self._band_style(color))
            tip_rating = rating or "no prediction"
            tip_period = "day" if day else "night"
            lbl.setToolTip(
                f"<b>{b}m</b> — {tip_rating} ({tip_period})<br>"
                "<i>HamQSL prediction based on current solar/geomag</i>"
            )

    def _update_value(self, label_dict: dict, key: str,
                      raw_value: Optional[str],
                      color_fn) -> None:
        """Update one of the three solar value boxes.

        Raw HamQSL strings can be "?" or "N/A" or empty when the feed
        is unsure; we render those as a dash and use the default
        color.  Otherwise parse-and-color via the supplied function.
        """
        val = (raw_value or "").strip()
        if not val or val in ("?", "N/A", "—"):
            label_dict["val"].setText("—")
            label_dict["val"].setStyleSheet(
                "color: #cdd9e5; font-family: Consolas, monospace; "
                "font-weight: 700; font-size: 12px;")
            return
        try:
            num = float(val)
        except ValueError:
            label_dict["val"].setText(val)
            return
        color = color_fn(num)
        label_dict["val"].setText(val)
        label_dict["val"].setStyleSheet(
            f"color: {color}; font-family: Consolas, monospace; "
            "font-weight: 700; font-size: 12px;")
