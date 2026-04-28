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
RELEASES_API_URL = (
    f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest")
RELEASES_PAGE_URL = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases")


# ── Version comparison helpers ─────────────────────────────────────────
_TAG_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def _parse_version(s: str) -> Optional[tuple[int, int, int]]:
    """Parse '0.0.3' or 'v0.0.3' → (0, 0, 3). Returns None if the
    string doesn't match the major.minor.patch pattern; the caller
    treats that as 'unknown, can't compare.'"""
    m = _TAG_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(remote_tag: str, local_version: str) -> bool:
    """True if the remote tag represents a NEWER release than the
    locally-running version. Both strings are run through _parse_version
    first; unparseable inputs return False (no nag on bad data)."""
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
        tag = str(payload.get("tag_name", "") or "")
        url = str(payload.get("html_url", "") or RELEASES_PAGE_URL)
        body = str(payload.get("body", "") or "")
        if not tag:
            self.finished_error.emit(
                "GitHub response had no tag_name; can't compare versions.")
            return
        self.finished_ok.emit(tag, url, body)


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
    """
    update_available = Signal(str, str)
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
            self.update_available.emit(tag, url or RELEASES_PAGE_URL)
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
