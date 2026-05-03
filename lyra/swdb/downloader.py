"""Background HTTPS downloader for the EiBi seasonal CSV.

EiBi publishes the schedule data at <https://www.eibispace.de/>.
File naming convention:

    sked-A26.csv   April-October 2026  (DST / summer season)
    sked-B26.csv   October 2026 - March 2027  (winter season)

The Settings UI's "Update database now" button drives this.  All
network work happens on a worker thread so the Qt UI doesn't
freeze waiting on the download.

License: EiBi data is free for non-commercial use, attribution
required.  Lyra surfaces the attribution string in the Settings
tab; we do NOT bundle the file in our distribution.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

_log = logging.getLogger(__name__)


# Default base URL.  Operator can override via QSettings to
# accommodate mirrors or future URL changes without a Lyra release.
EIBI_BASE_URL_DEFAULT = "https://www.eibispace.de/dx/"


def current_season(now: Optional[datetime] = None) -> str:
    """Return the current ITU broadcast-season code: 'A' (summer
    DST, late March to late October) or 'B' (winter, late
    October to late March).

    Approximation: A from April through end of October, B
    otherwise.  The actual transition is the last Sunday of
    March / October each year, but for the purpose of picking
    the right CSV file the approximation is fine -- both seasons'
    files remain valid for ~a week of overlap around the
    transition.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    m = now.month
    if 4 <= m <= 10:
        return "A"
    if m == 3 and now.day >= 25:
        return "A"
    if m == 11 and now.day < 1:
        # Cushion for late October; in practice 'A' until
        # last Sunday of October.
        return "A"
    return "B"


def season_filename(season: Optional[str] = None,
                    now: Optional[datetime] = None) -> str:
    """Compute the EiBi season-file basename for a given moment.
    'A' season + 2026 -> 'sked-A26.csv'."""
    if now is None:
        now = datetime.now(timezone.utc)
    if season is None or season.upper() == "AUTO":
        season = current_season(now)
    season = season.upper()
    if season not in ("A", "B"):
        raise ValueError(f"unknown season {season!r}")
    yy = now.year % 100
    return f"sked-{season}{yy:02d}.csv"


class _DownloadWorker(QObject):
    """Worker that runs the actual HTTPS GET.  Lives on a
    QThread so the UI stays responsive during the network call.
    Emits progress + completion signals back to the main thread.
    """

    progress = Signal(int, int)        # bytes_done, bytes_total (-1 if unknown)
    finished_ok = Signal(str, int)     # path, byte_count
    finished_error = Signal(str)       # human-readable error

    TIMEOUT_S = 30.0
    USER_AGENT_PREFIX = "Lyra-SDR"

    def __init__(self, url: str, dest: Path,
                 user_agent_version: str = ""):
        super().__init__()
        self._url = url
        self._dest = dest
        self._ua = (
            f"{self.USER_AGENT_PREFIX}/{user_agent_version}"
            if user_agent_version else self.USER_AGENT_PREFIX)

    def run(self) -> None:
        """Background entry point.  Wired to QThread.started in
        ``EibiDownloader.start``."""
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            req = Request(self._url, headers={"User-Agent": self._ua})
            with urlopen(req, timeout=self.TIMEOUT_S) as resp:
                # Some servers don't send Content-Length; surface
                # -1 to the UI so it shows 'downloading...' instead
                # of a phony percentage.
                cl_header = resp.headers.get("Content-Length")
                total = int(cl_header) if cl_header else -1
                done = 0
                # Stream into a tmp file then rename, so a
                # mid-download crash can't leave the operator
                # with a half-written CSV that fails to parse.
                tmp = self._dest.with_suffix(self._dest.suffix + ".tmp")
                with tmp.open("wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self.progress.emit(done, total)
                # Atomic rename.  Replace existing.
                if self._dest.exists():
                    self._dest.unlink()
                tmp.replace(self._dest)
            self.finished_ok.emit(str(self._dest), done)
        except HTTPError as e:
            self.finished_error.emit(
                f"HTTP {e.code}: {e.reason} -- {self._url}")
        except URLError as e:
            self.finished_error.emit(
                f"Network error: {e.reason}")
        except OSError as e:
            self.finished_error.emit(
                f"Disk error writing {self._dest}: {e}")
        except Exception as e:
            self.finished_error.emit(
                f"Download failed: {e}")


class EibiDownloader(QObject):
    """Public API for downloading an EiBi season CSV in the
    background.  Manages the worker-thread lifecycle so callers
    can fire-and-forget; signals carry the result.

    Typical use::

        dl = EibiDownloader(self)
        dl.progress.connect(...)
        dl.finished_ok.connect(...)
        dl.finished_error.connect(...)
        dl.fetch(dest_dir=Path("..."), season="auto")
    """

    progress = Signal(int, int)       # bytes_done, bytes_total
    finished_ok = Signal(str, int)    # path, byte_count
    finished_error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None,
                 base_url: str = EIBI_BASE_URL_DEFAULT,
                 user_agent_version: str = ""):
        super().__init__(parent)
        self._base_url = base_url.rstrip("/") + "/"
        self._user_agent_version = user_agent_version
        self._thread: Optional[QThread] = None
        self._worker: Optional[_DownloadWorker] = None

    def fetch(self, dest_dir: Path | str,
              season: str = "auto",
              filename: Optional[str] = None) -> None:
        """Kick off a fetch.  Returns immediately; result arrives
        via ``finished_ok`` / ``finished_error`` signals.

        ``season`` accepts ``'auto'`` (default), ``'A'``, or ``'B'``.
        ``filename`` overrides the auto-named season file -- useful
        for operators who want to load a specific file by URL
        (e.g. an archived season for historical analysis).
        """
        if self._thread is not None and self._thread.isRunning():
            self.finished_error.emit(
                "A download is already in progress.")
            return
        try:
            fname = filename or season_filename(season=season)
        except ValueError as e:
            self.finished_error.emit(str(e))
            return
        dest = Path(dest_dir) / fname
        url = self._base_url + fname
        self._thread = QThread(self)
        self._worker = _DownloadWorker(
            url=url, dest=dest,
            user_agent_version=self._user_agent_version)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self.progress)
        self._worker.finished_ok.connect(self.finished_ok)
        self._worker.finished_error.connect(self.finished_error)
        # Auto-cleanup on completion.
        self._worker.finished_ok.connect(self._thread.quit)
        self._worker.finished_error.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _cleanup(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = None
        # Keep thread reference until next fetch -- replaced on
        # the next call.

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()
