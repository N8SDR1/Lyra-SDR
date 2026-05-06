"""GitHub Releases update check — Help → Check for Updates…

Compares the running Lyra version against the most recent release
tag on the project's GitHub repo and shows a friendly dialog with
a "What's new" link if a newer version is out.

Design notes
------------
- Network call runs on a background QThread so the main thread
  doesn't freeze while waiting on GitHub. The dialog opens
  immediately with a "checking..." state.
- 5-second timeout — if GitHub is unreachable (firewall / offline /
  rate-limited), the dialog says so politely and offers a link to
  the releases page so the operator can check manually.
- Uses the standard library `urllib` so we don't add another
  dependency. GitHub's REST API doesn't require auth for the
  /releases/latest endpoint at this volume of use.
- Tag-name comparison is permissive: strips a leading "v" and
  compares as dotted version tuples. So tag "v0.0.3" matches
  __version__ "0.0.3" and (0,0,3) < (0,0,4) is the obvious
  "newer release available" trigger.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QVBoxLayout,
)

import lyra


REPO_OWNER = "N8SDR1"
REPO_NAME  = "Lyra-SDR"
# Note: NOT /releases/latest — that endpoint hides pre-releases by
# design.  We query /releases (all-releases list) and pick the
# highest version ourselves.  Lets testers on a pre-release still
# get notified of newer pre-releases AND any subsequent full
# release, plus full-release users still see new full releases as
# expected.  Same auth-free public endpoint, no rate-limit concern
# at our volume.
RELEASES_API_URL = (
    f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases")
RELEASES_PAGE_URL = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases")


# ── Version comparison helpers ─────────────────────────────────────────
# Match major.minor.patch with an OPTIONAL 4th micro-patch component.
# Lyra's version scheme is 4-component for hot-fix patch releases
# (e.g. 0.0.7.1, 0.0.8.1, 0.0.9.1) and 3-component for feature drops
# (0.0.7, 0.0.8, 0.0.9).  An earlier version of this regex captured
# only 3 components which silently ate the .micro suffix -- result:
# `is_newer("v0.0.9.1", "0.0.9")` returned False because both parsed
# to (0,0,9), so the startup silent-check never raised the toast /
# header indicator for any 4-component patch release.  Fixed
# 2026-05-03 (Brent Crier reported it during v0.0.9.1 testing).
_TAG_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?")


def _parse_version(s: str) -> Optional[tuple[int, int, int, int]]:
    """Parse '0.0.3' / 'v0.0.3' / '0.0.9.1' / 'v0.0.9.1' →
    (major, minor, patch, micro), padding the micro slot with 0 when
    a 3-component tag is given.  Returns None if the string doesn't
    match the major.minor.patch[.micro] pattern; the caller treats
    that as 'unknown, can't compare.'"""
    m = _TAG_RE.match(s.strip())
    if not m:
        return None
    micro = int(m.group(4)) if m.group(4) is not None else 0
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), micro)


def is_newer(remote_tag: str, local_version: str) -> bool:
    """True if the remote tag represents a NEWER release than the
    locally-running version. Both strings are run through _parse_version
    first; unparseable inputs return False (no nag on bad data).

    4-component compare matters: (0,0,9,0) < (0,0,9,1) so a v0.0.9.1
    pre-release correctly flags as newer than a running v0.0.9."""
    r = _parse_version(remote_tag)
    l = _parse_version(local_version)
    if r is None or l is None:
        return False
    return r > l


# ── Background fetch worker ────────────────────────────────────────────
class _ReleaseFetchWorker(QObject):
    """Runs the GitHub REST API call in a worker thread so the UI
    stays responsive while waiting on the network. Emits one of:

      finished_ok(tag, html_url, body)  — got a valid release
      finished_error(msg)               — anything went wrong
    """
    finished_ok = Signal(str, str, str)
    finished_error = Signal(str)

    def run(self):
        from urllib.request import Request, urlopen
        from urllib.error import URLError, HTTPError
        try:
            req = Request(
                RELEASES_API_URL,
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"Lyra/{lyra.__version__}"})
            with urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                self.finished_error.emit(
                    "No releases published yet on the GitHub repo.")
            else:
                self.finished_error.emit(
                    f"GitHub returned HTTP {e.code}: {e.reason}")
            return
        except URLError as e:
            self.finished_error.emit(
                f"Couldn't reach GitHub: {e.reason}")
            return
        except Exception as e:
            self.finished_error.emit(f"Update check failed: {e}")
            return

        # /releases returns a JSON array of release objects, newest
        # first by created_at.  We iterate all of them, skip drafts,
        # parse the tag_name, and pick the HIGHEST version (not the
        # most-recent — important if e.g. a pre-release was tagged
        # AFTER a full release was made for the same version).
        # Including pre-releases in the comparison lets testers on
        # one pre-release see newer pre-releases AND lets full-
        # release users still see full releases (since semver
        # comparison is symmetric).
        if not isinstance(payload, list):
            self.finished_error.emit(
                "GitHub returned an unexpected payload shape "
                "(expected an array of releases).")
            return
        if not payload:
            self.finished_error.emit(
                "No releases published yet on the GitHub repo.")
            return

        best_tag = ""
        best_url = RELEASES_PAGE_URL
        best_body = ""
        best_ver = None
        for rel in payload:
            if not isinstance(rel, dict):
                continue
            if rel.get("draft", False):
                continue
            tag = str(rel.get("tag_name", "") or "")
            if not tag:
                continue
            ver = _parse_version(tag)
            if ver is None:
                continue
            if best_ver is None or ver > best_ver:
                best_ver = ver
                best_tag = tag
                best_url = str(
                    rel.get("html_url", "") or RELEASES_PAGE_URL)
                best_body = str(rel.get("body", "") or "")

        if not best_tag:
            self.finished_error.emit(
                "GitHub returned releases but none had parseable "
                "version tags (expected vX.Y.Z form).")
            return
        self.finished_ok.emit(best_tag, best_url, best_body)


# ── User-facing dialog ─────────────────────────────────────────────────
class CheckForUpdatesDialog(QDialog):
    """Modal dialog that shows update-check status. Opens immediately
    with a 'Checking…' state and switches to the result when the
    background fetch finishes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lyra — Check for Updates")
        self.setMinimumSize(520, 320)

        v = QVBoxLayout(self)

        self._status = QLabel("<b>Checking GitHub for the latest release…</b>")
        v.addWidget(self._status)

        self._body = QTextBrowser()
        self._body.setOpenExternalLinks(True)
        self._body.setHtml(
            f"<p>Currently running: <b>Lyra v{lyra.__version__}</b> "
            f"({lyra.version_string()})</p>"
            "<p style='color:#8a9aac'>This dialog talks to GitHub's "
            "public releases API. No telemetry, no account, no data "
            "is sent — just a single GET to the project's releases "
            "endpoint.</p>")
        v.addWidget(self._body, 1)

        self._open_release_btn = QPushButton("Open release page in browser")
        self._open_release_btn.setVisible(False)
        self._open_release_btn.clicked.connect(self._on_open_release)

        self._open_repo_btn = QPushButton("Open repo on GitHub")
        self._open_repo_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RELEASES_PAGE_URL)))

        btns = QHBoxLayout()
        btns.addWidget(self._open_release_btn)
        btns.addStretch(1)
        btns.addWidget(self._open_repo_btn)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        btns.addWidget(close)
        v.addLayout(btns)

        # State for the open-release-page button
        self._release_url: str = RELEASES_PAGE_URL

        # Spin up the fetch on a worker thread so the UI doesn't block.
        self._thread = QThread(self)
        self._worker = _ReleaseFetchWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)
        # Auto-cleanup once either finished signal fires.
        self._worker.finished_ok.connect(self._thread.quit)
        self._worker.finished_error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    # ── Result handlers ───────────────────────────────────────────────
    def _on_finished_ok(self, tag: str, url: str, body: str):
        self._release_url = url or RELEASES_PAGE_URL
        local = lyra.__version__
        if is_newer(tag, local):
            self._status.setText(
                f"<b style='color:#39ff14'>"
                f"A newer Lyra is available — {tag}</b>")
            extra = (
                "<p>Click <b>Open release page</b> below to read the "
                "release notes and download the new build.</p>")
            self._open_release_btn.setVisible(True)
        elif tag.lstrip("v") == local:
            self._status.setText(
                "<b style='color:#80d8ff'>"
                "You're on the latest release.</b>")
            extra = ""
        else:
            # Local newer than remote (dev builds), or unparseable
            self._status.setText(
                f"<b>Latest GitHub release: {tag}</b><br>"
                f"<span style='color:#8a9aac'>"
                f"You're running v{local}.</span>")
            extra = ""
        # Render body if present (release notes from the GitHub release
        # description). Plain text → preserve linebreaks.
        body_html = ""
        if body.strip():
            body_html = (
                "<hr><p><b>Release notes:</b></p>"
                f"<pre style='white-space: pre-wrap'>{body}</pre>")
        self._body.setHtml(
            f"<p>Currently running: <b>Lyra v{local}</b></p>"
            f"<p>Latest published: <b>{tag}</b></p>"
            f"{extra}"
            f"{body_html}")

    def _on_finished_error(self, msg: str):
        self._status.setText(
            f"<b style='color:#ff8c3a'>Couldn't check for updates</b>")
        self._body.setHtml(
            f"<p>Currently running: <b>Lyra v{lyra.__version__}</b></p>"
            f"<p style='color:#ff8c3a'>{msg}</p>"
            "<p>You can still check manually — click "
            "<b>Open repo on GitHub</b> below.</p>"
            "<p style='color:#8a9aac'>Common reasons: no internet, "
            "Windows Firewall blocking python.exe outbound, GitHub "
            "rate-limit on this network.</p>")

    def _on_open_release(self):
        QDesktopServices.openUrl(QUrl(self._release_url))

    def closeEvent(self, event):
        # Make sure the worker thread is properly shut down if the
        # operator closes the dialog before the fetch completes.
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(500)
        super().closeEvent(event)


# ── Startup modal for unseen-version updates ──────────────────────────
#
# Headline polish item from §3.1 of the v0.1 RX2 consensus plan,
# pulled forward into v0.0.9.3.1 alongside the watermark fix.
#
# Today's auto-update path is non-modal — toast (12s) + Help-menu
# badge + toolbar indicator.  Operators who walk away from the desk
# during startup or who don't notice toolbar changes can miss it
# entirely.  Brent and Rick both reported missing prior releases.
#
# The modal is shown ONCE per new version detected, the first time
# the operator sees it.  Subsequent launches with the same tag fall
# back to the existing non-modal (toast + indicator) path — so this
# isn't nagware, just a "did you see this?" gate on first detection.
#
# State machine (driven by app.py _on_startup_update_available):
#   * tag in skipped_versions   → no modal, no toast, no indicator
#   * tag in modal_seen_versions → no modal; toast + indicator only
#   * else                       → MODAL, then add to modal_seen_versions
#                                  (skip-this-version button also adds
#                                  to skipped_versions for full silence)
class UpdateAvailableModal(QDialog):
    """First-time-per-version modal that announces a new Lyra release.

    Three operator choices, mapped to the QDialog result codes via
    custom signals so the caller can route them:

      open_release   → operator clicked "Open release page"
                       (also accepts the dialog so the indicator
                       stays visible until they actually upgrade)
      remind_later   → operator clicked "Remind me later"
                       (dialog closes; toast + indicator still shown
                       on this launch and future launches)
      skip_version   → operator clicked "Skip this version"
                       (dialog closes; tag added to skipped_versions
                       so toast + indicator suppressed for this tag
                       forever — until a NEWER tag appears)

    The dialog never blocks the silent-check thread or the main
    window; it's exec()'d after _on_startup_update_available has
    already wired the indicator + badge.
    """

    open_release = Signal()
    remind_later = Signal()
    skip_version = Signal()

    def __init__(self, tag: str, url: str, body: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Lyra — {tag} available")
        self.setMinimumSize(560, 360)
        # Modal but not application-modal — operator can drag the
        # main window underneath.
        self.setModal(True)

        self._url = url or RELEASES_PAGE_URL

        v = QVBoxLayout(self)

        title = QLabel(
            f"<h3 style='color:#39ff14; margin-bottom:4px'>"
            f"Lyra {tag} is available</h3>"
            f"<p style='color:#8a9aac; margin-top:0'>"
            f"You're running <b>v{lyra.__version__}</b>.</p>")
        title.setWordWrap(True)
        v.addWidget(title)

        body_view = QTextBrowser()
        body_view.setOpenExternalLinks(True)
        body_html = ""
        if body and body.strip():
            body_html = (
                "<p><b>Release notes</b></p>"
                f"<pre style='white-space: pre-wrap'>{body}</pre>")
        else:
            body_html = (
                "<p style='color:#8a9aac'>"
                "No release notes were attached to this build. "
                "Click <b>Open release page</b> for the full "
                "description.</p>")
        body_view.setHtml(body_html)
        v.addWidget(body_view, 1)

        hint = QLabel(
            "<p style='color:#8a9aac'>"
            "<b>Open release page</b> takes you to the GitHub "
            "download.<br>"
            "<b>Remind me later</b> dismisses this dialog but keeps "
            "the toolbar indicator.<br>"
            "<b>Skip this version</b> hides all notifications for "
            "{tag}; you'll be notified about newer ones."
            "</p>".replace("{tag}", tag))
        hint.setWordWrap(True)
        v.addWidget(hint)

        # ── Buttons ────────────────────────────────────────────────
        btns = QHBoxLayout()
        skip_btn = QPushButton("Skip this version")
        skip_btn.clicked.connect(self._on_skip)
        btns.addWidget(skip_btn)
        btns.addStretch(1)
        remind_btn = QPushButton("Remind me later")
        remind_btn.clicked.connect(self._on_remind)
        btns.addWidget(remind_btn)
        open_btn = QPushButton("Open release page")
        open_btn.setDefault(True)
        open_btn.setStyleSheet(
            "QPushButton { "
            "background: #2a7d2a; color: white; "
            "padding: 6px 16px; font-weight: 600; "
            "border: 1px solid #39ff14; border-radius: 4px; "
            "} "
            "QPushButton:hover { background: #339933; } "
            "QPushButton:pressed { background: #226622; }")
        open_btn.clicked.connect(self._on_open_release)
        btns.addWidget(open_btn)
        v.addLayout(btns)

    def _on_open_release(self):
        QDesktopServices.openUrl(QUrl(self._url))
        self.open_release.emit()
        self.accept()

    def _on_remind(self):
        self.remind_later.emit()
        self.reject()

    def _on_skip(self):
        self.skip_version.emit()
        self.reject()


# ── Silent background check (auto-update notification) ────────────────
class SilentUpdateChecker(QObject):
    """Run the same GitHub release check as the dialog, but headless —
    no UI, no modal. Emits one of:

      update_available(tag, html_url)  — newer version found
      no_update_available()            — caller is on the latest
      check_failed(reason)             — couldn't reach GitHub

    Used by MainWindow's startup hook to surface an "update available"
    notification in the status bar + Help menu badge without forcing
    the operator into a dialog. Operator can still open Help → Check
    for Updates… any time for the full dialog.

    Signal signature note: ``update_available`` carries ``(tag, url,
    body)`` as of v0.0.9.3.1 — the third arg is the GitHub release
    body (release notes) for the new first-time-per-version modal.
    Empty string when not available.
    """
    update_available = Signal(str, str, str)
    no_update_available = Signal()
    check_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_ReleaseFetchWorker] = None

    def start(self) -> None:
        """Kick off the background fetch. Safe to call multiple times;
        a fetch already in flight is left alone."""
        if self._thread is not None and self._thread.isRunning():
            return
        self._thread = QThread(self)
        self._worker = _ReleaseFetchWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_error.connect(self._on_finished_error)
        self._worker.finished_ok.connect(self._thread.quit)
        self._worker.finished_error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def _on_finished_ok(self, tag: str, url: str, body: str):
        if is_newer(tag, lyra.__version__):
            # v0.0.9.3.1: log to console so operators running with a
            # console window see the silent-check result.  Useful for
            # diagnosing "did the auto-update check actually find
            # something" without needing the toolbar UI.
            print(f"Lyra: silent update check found newer release "
                  f"{tag} (running v{lyra.__version__})")
            # Pass body through so the first-time-per-version modal
            # can render release notes inline.
            self.update_available.emit(
                tag, url or RELEASES_PAGE_URL, body or "")
        else:
            self.no_update_available.emit()

    def _on_finished_error(self, msg: str):
        self.check_failed.emit(msg)

    def _cleanup(self):
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = None
        # Keep the QThread reference until next start() — it gets
        # replaced on the next call.
