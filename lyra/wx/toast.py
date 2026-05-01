"""Windows toast notifications for weather alerts.

Uses `winrt`-based toasts where available (modern Win10/11), falls
back to a simple Qt popup when not.  Keeps a per-condition hysteresis
so toasts don't spam: once a toast fires for a condition, that
condition can't toast again for ``HYSTERESIS_SEC`` until the tier
DROPS out of the alert range.

Audio cue is operator-toggleable.  When enabled, plays a brief
system-sound ("Asterisk" on Windows, equivalent on other OSes)
synchronously with the toast.
"""
# Lyra-SDR — Weather toast dispatcher
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# Released under GPL v3 or later (see LICENSE).
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Per-condition cooldown — once a condition fires, suppress further
# notifications for the same condition for this long, even if the
# tier returns to alert state mid-cooldown.
HYSTERESIS_SEC: float = 15.0 * 60.0   # 15 minutes


@dataclass
class ToastDispatcher:
    """Stateful toast emitter with hysteresis.  One instance per
    Lyra app session — owned by WxWorker."""

    audio_enabled: bool = True
    desktop_enabled: bool = True

    # last-fired timestamps (monotonic) per logical condition.
    _last_fire: dict[str, float] = field(default_factory=dict)
    # Tier-dropout tracking — once the tier goes back to non-alert,
    # we clear the hysteresis for that condition so a new event can
    # fire immediately.
    _tier_was_alert: dict[str, bool] = field(default_factory=dict)

    def maybe_fire(self, condition_key: str, tier_is_alert: bool,
                    title: str, body: str) -> None:
        """Possibly fire a toast for ``condition_key``.

        - If tier just CROSSED INTO alert (was non-alert, now is) and
          hysteresis isn't active, fire and start cooldown.
        - If tier dropped out of alert, clear the cooldown so the
          next entry into alert can fire immediately.
        - Otherwise, do nothing.

        ``condition_key`` is a stable string like 'lightning_close'
        or 'wind_extreme' so each condition has its own cooldown.
        """
        was_alert = self._tier_was_alert.get(condition_key, False)
        now = time.monotonic()
        if not tier_is_alert:
            # Tier left alert state — reset cooldown so re-entry will
            # toast immediately.
            self._tier_was_alert[condition_key] = False
            return
        # Currently in alert.  Toast only on the entering edge or
        # after cooldown.
        if was_alert:
            # Already firing this condition; suppress.
            return
        last = self._last_fire.get(condition_key, 0.0)
        if now - last < HYSTERESIS_SEC:
            # Still within cooldown of a previous fire.
            self._tier_was_alert[condition_key] = True
            return
        # Fire it.
        self._tier_was_alert[condition_key] = True
        self._last_fire[condition_key] = now
        self._emit(title, body)

    def force_fire(self, title: str, body: str) -> None:
        """Fire a toast unconditionally (bypasses hysteresis).
        Used by the 'Send test toast' button in Settings."""
        self._emit(title, body)

    def _emit(self, title: str, body: str) -> None:
        """Actually display the toast + play the audio cue."""
        if self.audio_enabled:
            self._play_audio()
        if not self.desktop_enabled:
            return
        # Try WinRT first (Windows 10/11 modern toast).
        if sys.platform == "win32":
            if self._emit_winrt(title, body):
                return
            if self._emit_powershell(title, body):
                return
        # Fall back to a Qt popup that auto-closes.
        self._emit_qt_fallback(title, body)

    @staticmethod
    def _play_audio() -> None:
        """Play a brief system sound — non-blocking on all platforms."""
        try:
            if sys.platform == "win32":
                import winsound
                # Asterisk = the standard Windows alert ding.  Async
                # so we don't block the audio thread or the worker.
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return
        except Exception:
            pass
        # Cross-platform fallback — Qt's QApplication.beep().
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.beep()
        except Exception:
            pass

    @staticmethod
    def _emit_winrt(title: str, body: str) -> bool:
        """Attempt a modern Win10/11 toast via the winrt bindings.
        Returns True on success, False to fall through to other paths.
        """
        try:
            from winrt.windows.ui.notifications import (
                ToastNotificationManager,
                ToastNotification)
            from winrt.windows.data.xml.dom import XmlDocument
            template = (
                '<toast><visual><binding template="ToastGeneric">'
                f'<text>{_xml_escape(title)}</text>'
                f'<text>{_xml_escape(body)}</text>'
                '</binding></visual></toast>')
            xml = XmlDocument()
            xml.load_xml(template)
            notifier = ToastNotificationManager.create_toast_notifier(
                "Lyra-SDR")
            notifier.show(ToastNotification(xml))
            return True
        except Exception as exc:
            logger.debug("winrt toast unavailable: %s", exc)
            return False

    @staticmethod
    def _emit_powershell(title: str, body: str) -> bool:
        """Fallback: shell out to PowerShell's Windows.Forms balloon
        notification.  Works on every Windows install with a
        graphical session, no extra packages needed."""
        try:
            import subprocess
            esc_title = title.replace("'", "''")
            esc_body = body.replace("'", "''")
            ps = (
                "[void] [System.Reflection.Assembly]::LoadWithPartialName"
                "('System.Windows.Forms');"
                "$o = New-Object System.Windows.Forms.NotifyIcon;"
                "$o.Icon = [System.Drawing.SystemIcons]::Information;"
                "$o.Visible = $True;"
                f"$o.ShowBalloonTip(8000, '{esc_title}', '{esc_body}', "
                "[System.Windows.Forms.ToolTipIcon]::Warning);"
                "Start-Sleep -Seconds 9; $o.Dispose();"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                 "-Command", ps],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        except Exception as exc:
            logger.debug("powershell toast failed: %s", exc)
            return False

    @staticmethod
    def _emit_qt_fallback(title: str, body: str) -> None:
        """Last-resort Qt popup (auto-dismiss after 8s).  Used when
        platform-native toast isn't available.  Must be called on
        the Qt main thread."""
        try:
            from PySide6.QtWidgets import (
                QApplication, QSystemTrayIcon)
            app = QApplication.instance()
            if app is None:
                return
            # Walk the top-level windows for a tray icon Lyra may have
            # registered.  If none exists, skip — we don't want to
            # create a tray icon just for this.
            for w in app.topLevelWidgets():
                tray = w.findChild(QSystemTrayIcon)
                if tray is not None:
                    tray.showMessage(
                        title, body,
                        QSystemTrayIcon.MessageIcon.Warning, 8000)
                    return
        except Exception as exc:
            logger.debug("Qt fallback toast failed: %s", exc)


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))
