"""Background QThread that polls weather sources at configured intervals.

Owns:
    - One ``WxConfig`` describing all source toggles + thresholds
    - One ``ToastDispatcher`` for desktop notifications
    - A periodic timer that calls ``aggregator.aggregate()`` and emits
      Qt signals back to the main thread for UI updates

Signals (all routed via Qt.QueuedConnection so handlers run on the
main/UI thread):
    snapshot_ready(WxSnapshot)  — emitted every poll cycle
    error_occurred(str)         — non-fatal source-fetch errors

The worker is owned by the Radio instance.  It only runs when the
master enable flag is set (via Radio.set_wx_enabled).  When disabled
it sleeps in 5-second slices so the operator's enable toggle takes
effect within a few seconds without burning CPU.
"""
# Lyra-SDR — Weather worker thread
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

import logging
import time
from dataclasses import replace

from PySide6.QtCore import QThread, Signal

from lyra.wx.aggregator import (
    LIGHTNING_CLOSE, WIND_EXTREME, SEVERE_ACTIVE,
    WxConfig, WxSnapshot, aggregate)
from lyra.wx.toast import ToastDispatcher

logger = logging.getLogger(__name__)


class WxWorker(QThread):
    """Periodic poller for weather sources.

    Construct with the Radio instance so we can read operator
    location from there as it changes.  ``set_config()`` updates the
    poll behavior (thresholds, source enables, credentials).
    """

    # Emitted every successful poll cycle, on the worker thread but
    # AutoConnection delivers to the main thread when the receiver
    # lives there.
    snapshot_ready = Signal(object)   # WxSnapshot
    error_occurred = Signal(str)

    # Poll interval — ~30 seconds is plenty for lightning detection
    # and avoids hitting Blitzortung / Ambient rate limits.  NWS is
    # polled at the same cadence; the Ecowitt module has its own
    # 30-second internal cache so back-to-back calls are cheap.
    POLL_INTERVAL_SEC: float = 30.0

    # When the master enable is OFF, sleep in slices of this length
    # so toggling on takes effect within a few seconds.
    IDLE_SLICE_SEC: float = 5.0

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        self._config = WxConfig()
        self._enabled = False
        self._stop_requested = False
        self._toast = ToastDispatcher()
        # Pulled from operator location at poll time so we don't have
        # to re-bind signals when the grid changes.

    # ── Public API ────────────────────────────────────────────────

    def set_config(self, cfg: WxConfig) -> None:
        """Update the poll behavior atomically.  Safe from any thread
        — Python attribute assignment is atomic, and we copy the
        dataclass so the worker's per-cycle reading is consistent.
        """
        self._config = replace(cfg)

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)

    def set_audio_enabled(self, on: bool) -> None:
        self._toast.audio_enabled = bool(on)

    def set_desktop_enabled(self, on: bool) -> None:
        self._toast.desktop_enabled = bool(on)

    def fire_test_toast(self) -> None:
        """Operator-triggered test — bypasses hysteresis."""
        self._toast.force_fire(
            "Lyra Weather Alerts — Test",
            "If you see this, the toast pipeline is working.")

    def request_stop(self) -> None:
        """Signal the worker to exit cleanly on the next slice."""
        self._stop_requested = True

    # ── QThread main loop ─────────────────────────────────────────

    def run(self) -> None:
        # Initial small delay so the rest of the app has a chance to
        # finish booting (settings autoloads, Radio construction
        # finishes, etc.) before we start hitting the network.
        for _ in range(3):
            if self._stop_requested:
                return
            time.sleep(1.0)

        while not self._stop_requested:
            if not self._enabled:
                time.sleep(self.IDLE_SLICE_SEC)
                continue

            # Pull operator location from Radio at poll time so
            # changes to grid square or manual override take effect
            # without restarting the worker.
            cfg = replace(
                self._config,
                my_lat=self.radio.operator_lat,
                my_lon=self.radio.operator_lon)

            try:
                snap = aggregate(cfg)
            except Exception as exc:
                logger.exception("Weather aggregator crashed")
                self.error_occurred.emit(f"aggregator: {exc}")
                snap = WxSnapshot(error=str(exc))

            # Toast checks — fire on alert-tier transitions.
            try:
                self._maybe_toast(snap)
            except Exception as exc:
                logger.warning("Toast dispatcher error: %s", exc)

            self.snapshot_ready.emit(snap)
            if snap.error:
                self.error_occurred.emit(snap.error)

            # Sleep in slices so we react to enable/stop toggles
            # without waiting a full poll interval.
            elapsed = 0.0
            while (elapsed < self.POLL_INTERVAL_SEC
                   and not self._stop_requested
                   and self._enabled):
                time.sleep(min(1.0, self.POLL_INTERVAL_SEC - elapsed))
                elapsed += 1.0

    # ── Internals ─────────────────────────────────────────────────

    def _maybe_toast(self, snap: WxSnapshot) -> None:
        """Fire toasts on tier-crossing events.  Handles per-condition
        hysteresis so we don't spam.
        """
        # Lightning close-tier (≤10 mi by default).
        is_close = (snap.lightning.tier == LIGHTNING_CLOSE)
        if is_close:
            dist_km = snap.lightning.closest_km or 0.0
            dist_mi = dist_km / 1.60934
            self._toast.maybe_fire(
                "lightning_close",
                tier_is_alert=True,
                title="⚡ Lightning detected nearby",
                body=(f"Closest strike {dist_mi:.0f} mi "
                      f"({dist_km:.0f} km) — consider disconnecting "
                      "antennas."))
        else:
            self._toast.maybe_fire("lightning_close", False, "", "")

        # Wind extreme tier.
        is_extreme = (snap.wind.tier == WIND_EXTREME)
        if is_extreme:
            sustained = snap.wind.sustained_mph or 0.0
            gust = snap.wind.gust_mph or 0.0
            headline = (snap.wind.nws_alert_headline
                         or "extreme wind detected")
            self._toast.maybe_fire(
                "wind_extreme",
                tier_is_alert=True,
                title="💨 Extreme wind",
                body=(f"{headline} — sustained "
                      f"{sustained:.0f} mph, gust {gust:.0f} mph."))
        else:
            self._toast.maybe_fire("wind_extreme", False, "", "")

        # NWS severe storm warning (lightning-flavored).
        is_severe = (snap.severe.tier == SEVERE_ACTIVE)
        if is_severe:
            self._toast.maybe_fire(
                "severe_storm",
                tier_is_alert=True,
                title="⚠ NWS Storm Warning",
                body=snap.severe.headline)
        else:
            self._toast.maybe_fire("severe_storm", False, "", "")
