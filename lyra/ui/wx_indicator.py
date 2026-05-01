"""Header indicator for weather alerts.

Three icons stacked horizontally — lightning ⚡, wind 💨, severe ⚠.
Each is colored by its tier (yellow/orange/red) and hidden when its
tier is "none" so the row stays clean on quiet days.

Slots into the main toolbar between the ADC RMS readout and the
clocks.  Subscribes to ``radio.wx_snapshot_changed`` for updates.
"""
# Lyra-SDR — Weather header indicator
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from lyra.wx.aggregator import (
    LIGHTNING_NONE, LIGHTNING_FAR, LIGHTNING_MID, LIGHTNING_CLOSE,
    WIND_NONE, WIND_ELEVATED, WIND_HIGH, WIND_EXTREME,
    SEVERE_NONE, SEVERE_ACTIVE,
)


# Tier-color palette — matches SDRLogger+'s carryover scheme so
# operators familiar with the logger see the same color cues here.
COLOR_YELLOW = "#ffd700"
COLOR_ORANGE = "#ff8c00"
COLOR_RED = "#ff4444"


def _lightning_color(tier: str) -> str:
    return {
        LIGHTNING_FAR:   COLOR_YELLOW,
        LIGHTNING_MID:   COLOR_ORANGE,
        LIGHTNING_CLOSE: COLOR_RED,
    }.get(tier, COLOR_YELLOW)


def _wind_color(tier: str) -> str:
    return {
        WIND_ELEVATED: COLOR_YELLOW,
        WIND_HIGH:     COLOR_ORANGE,
        WIND_EXTREME:  COLOR_RED,
    }.get(tier, COLOR_YELLOW)


class WxIndicator(QWidget):
    """Compact weather-alert indicator for the main toolbar.

    Renders three little badges side by side:
        ⚡  18 mi · S       (lightning, with closest distance + bearing)
        💨  G 42            (wind, gust speed)
        ⚠                  (NWS severe alert active)

    Each badge auto-hides when its tier is "none" so the indicator
    occupies zero pixels on a quiet day.
    """

    def __init__(self, radio, distance_unit: str = "mi",
                 wind_unit: str = "mph", parent=None) -> None:
        super().__init__(parent)
        self.radio = radio
        self._distance_unit = distance_unit  # "mi" or "km"
        self._wind_unit = wind_unit          # "mph" / "kph" / "kt"

        layout = QHBoxLayout(self)
        # Margins: small left/right gap from neighboring toolbar
        # widgets, plus a touch of top/bottom so the badge mid-line
        # sits closer to the header's vertical center (the toolbar
        # is taller than this widget alone, so without this padding
        # the badges hug the top edge).
        layout.setContentsMargins(8, 4, 16, 4)
        # Inter-badge spacing — larger gap so each indicator reads
        # as a distinct item rather than running together.
        layout.setSpacing(14)

        # Lightning badge — bolt icon + closest-strike text.
        self._lightning_label = QLabel("")
        self._lightning_label.setAlignment(Qt.AlignVCenter)
        self._lightning_label.setVisible(False)
        self._lightning_label.setToolTip(
            "Lightning indicator.  Color reflects proximity to the\n"
            "closest detected strike: yellow > 25 mi, orange < 25 mi,\n"
            "red < 10 mi.  Hidden when no strikes detected.")
        layout.addWidget(self._lightning_label)

        # Wind badge — gust icon + sustained/gust speed.
        self._wind_label = QLabel("")
        self._wind_label.setAlignment(Qt.AlignVCenter)
        self._wind_label.setVisible(False)
        self._wind_label.setToolTip(
            "High-wind indicator.  Tier follows operator-set\n"
            "sustained / gust thresholds.  Hidden when wind is\n"
            "below the elevated tier.")
        layout.addWidget(self._wind_label)

        # Severe-storm badge — single warning glyph + headline tooltip.
        self._severe_label = QLabel("")
        self._severe_label.setAlignment(Qt.AlignVCenter)
        self._severe_label.setVisible(False)
        layout.addWidget(self._severe_label)

        # Subscribe to Radio's snapshot signal.
        self.radio.wx_snapshot_changed.connect(self._on_snapshot)
        self.radio.wx_enabled_changed.connect(self._on_enabled_changed)
        # Apply initial state if a snapshot already exists.
        if radio.wx_last_snapshot is not None:
            self._on_snapshot(radio.wx_last_snapshot)

    # ── Public API ────────────────────────────────────────────────

    def set_distance_unit(self, unit: str) -> None:
        """``mi`` or ``km`` for the lightning badge text."""
        self._distance_unit = "km" if unit.lower() == "km" else "mi"
        if self.radio.wx_last_snapshot is not None:
            self._on_snapshot(self.radio.wx_last_snapshot)

    def set_wind_unit(self, unit: str) -> None:
        """``mph``, ``kph``, or ``kt`` for the wind badge text."""
        u = unit.lower()
        if u not in ("mph", "kph", "kt", "knots"):
            u = "mph"
        self._wind_unit = u
        if self.radio.wx_last_snapshot is not None:
            self._on_snapshot(self.radio.wx_last_snapshot)

    # ── Slots ─────────────────────────────────────────────────────

    def _on_enabled_changed(self, on: bool) -> None:
        """When alerts are disabled at the master level, hide
        everything immediately rather than waiting for the next
        poll cycle to clear stale state."""
        if not on:
            self._lightning_label.setVisible(False)
            self._wind_label.setVisible(False)
            self._severe_label.setVisible(False)

    def _on_snapshot(self, snap) -> None:
        """Update each badge from the latest WxSnapshot."""
        # Lightning ────────────────────────────────────────────────
        ltier = snap.lightning.tier
        if ltier == LIGHTNING_NONE:
            self._lightning_label.setVisible(False)
        else:
            color = _lightning_color(ltier)
            dist_km = snap.lightning.closest_km
            if dist_km is None:
                txt = "⚡"
            else:
                if self._distance_unit == "km":
                    dtxt = f"{dist_km:.0f} km"
                else:
                    dtxt = f"{dist_km / 1.60934:.0f} mi"
                bearing = snap.lightning.closest_bearing_deg
                if bearing is not None:
                    dtxt += f" · {_compass_short(bearing)}"
                txt = f"⚡ {dtxt}"
            self._lightning_label.setText(txt)
            self._lightning_label.setStyleSheet(self._badge_css(color))
            tooltip = (
                f"⚡ Lightning detected\n"
                f"  closest strike: "
                f"{(dist_km or 0)/1.60934:.0f} mi "
                f"({dist_km or 0:.0f} km)\n"
                f"  recent strikes: {snap.lightning.strikes_recent}\n"
                f"  sources: "
                f"{', '.join(snap.lightning.sources_with_data) or 'none'}")
            self._lightning_label.setToolTip(tooltip)
            self._lightning_label.setVisible(True)

        # Wind ────────────────────────────────────────────────────
        wtier = snap.wind.tier
        if wtier == WIND_NONE:
            self._wind_label.setVisible(False)
        else:
            color = _wind_color(wtier)
            sustained = snap.wind.sustained_mph
            gust = snap.wind.gust_mph
            txt = f"💨 {self._format_wind(sustained, gust)}"
            self._wind_label.setText(txt)
            self._wind_label.setStyleSheet(self._badge_css(color))
            tooltip_lines = [
                "💨 High wind alert",
                f"  sustained: {self._fmt_speed(sustained)}",
                f"  gust:      {self._fmt_speed(gust)}",
            ]
            if snap.wind.nws_alert_headline:
                tooltip_lines.append(
                    f"  NWS: {snap.wind.nws_alert_headline}")
            if snap.wind.sources_with_data:
                tooltip_lines.append(
                    "  sources: "
                    + ", ".join(snap.wind.sources_with_data))
            self._wind_label.setToolTip("\n".join(tooltip_lines))
            self._wind_label.setVisible(True)

        # Severe (NWS thunderstorm/lightning warnings) ────────────
        if snap.severe.tier == SEVERE_NONE:
            self._severe_label.setVisible(False)
        else:
            self._severe_label.setText("⚠")
            self._severe_label.setStyleSheet(self._badge_css(COLOR_RED))
            self._severe_label.setToolTip(
                f"⚠ NWS Severe Weather Alert\n  {snap.severe.headline}")
            self._severe_label.setVisible(True)

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _badge_css(color: str) -> str:
        # Sized to read as a peer to the toolbar clocks (22px) — at
        # 17px the badges are clearly secondary but still
        # legible-from-across-the-room, which matters since lightning
        # / wind alerts are operationally urgent.  Padding scales
        # with font size to keep proportions clean.
        return (f"color: {color}; "
                "font-family: Consolas, monospace; font-weight: 700; "
                "font-size: 17px; padding: 5px 16px; "
                f"border: 1px solid {color}; border-radius: 11px; "
                f"background: rgba(255,255,255,0.05);")

    def _fmt_speed(self, mph) -> str:
        if mph is None:
            return "—"
        if self._wind_unit == "mph":
            return f"{mph:.0f} mph"
        if self._wind_unit == "kph":
            return f"{mph * 1.60934:.0f} kph"
        # knots
        return f"{mph * 0.868976:.0f} kt"

    def _format_wind(self, sustained, gust) -> str:
        """Compact wind display for the badge — prefers gust, falls
        back to sustained.  Format: 'G42' or 'S30'."""
        if gust is not None:
            return f"G{self._fmt_speed(gust)}"
        if sustained is not None:
            return f"S{self._fmt_speed(sustained)}"
        return "—"


def _compass_short(deg: float) -> str:
    """Short compass label (N, NE, E, ...) from a bearing in deg."""
    if deg is None:
        return ""
    # 8-point compass.
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int((deg + 22.5) // 45) % 8
    return points[idx]
