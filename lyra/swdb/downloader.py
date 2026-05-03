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
#
# Hostname history note (2026-05): the canonical EiBi URL has
# moved between www.eibispace.de and eibispace.de over the years.
# As of this writing the apex domain (no www) has a valid TLS
# cert; the www subdomain returns a cert issued for the apex,
# producing SSL hostname-mismatch errors on download.  Default to
# the apex URL; the worker iterates through fallback URLs below
# if the primary fails.
EIBI_BASE_URL_DEFAULT = "https://eibispace.de/dx/"

# Fallback URLs tried in order when the primary fails.  Each is
# logged so operators can see WHICH endpoint succeeded.  HTTP at
# the end of the chain because EiBi historically also published
# over plain HTTP (this is a freely-published broadcast schedule,
# no auth or sensitive data; the tradeoff for getting the file
# at all when TLS misconfig blocks HTTPS is acceptable).
EIBI_FALLBACK_URLS = (
    "https://eibispace.de/dx/",
    "https://www.eibispace.de/dx/",
    "http://eibispace.de/dx/",
    "http://www.eibispace.de/dx/",
)


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
    'A' season + 2026 -> 'sked-a26.csv'.

    EiBi serves the files with **lowercase** season letter
    (sked-a26.csv, sked-b26.csv).  This is case-sensitive on the
    server -- using uppercase 'A' returns 404.  Operator-confirmed
    URL form (2026-05-02): https://eibispace.de/dx/sked-a26.csv
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if season is None or season.upper() == "AUTO":
        season = current_season(now)
    season = season.upper()
    if season not in ("A", "B"):
        raise ValueError(f"unknown season {season!r}")
    yy = now.year % 100
    # Lowercase the season letter for the filename (server
    # case-sensitive; uppercase returns 404).  current_season()
    # and the rest of the code treat 'A' / 'B' as canonical
    # uppercase identifiers; only the URL filename portion needs
    # to be lowercase.
    return f"sked-{season.lower()}{yy:02d}.csv"


class _DownloadWorker(QObject):
    """Worker that runs the actual HTTP(S) GET.  Lives on a
    QThread so the UI stays responsive during the network call.
    Iterates through the supplied URL list in order and stops on
    the first success.  Emits progress + completion signals back
    to the main thread.
    """

    progress = Signal(int, int)        # bytes_done, bytes_total (-1 if unknown)
    status   = Signal(str)             # human-readable progress string
    finished_ok = Signal(str, int)     # path, byte_count
    finished_error = Signal(str)       # human-readable error

    TIMEOUT_S = 30.0
    USER_AGENT_PREFIX = "Lyra-SDR"

    def __init__(self, urls: list, dest: Path,
                 user_agent_version: str = ""):
        super().__init__()
        # Accept either a single URL string (legacy) or a list of
        # fallback URLs to try in order.
        if isinstance(urls, str):
            urls = [urls]
        self._urls = list(urls)
        self._dest = dest
        self._ua = (
            f"{self.USER_AGENT_PREFIX}/{user_agent_version}"
            if user_agent_version else self.USER_AGENT_PREFIX)

    def run(self) -> None:
        """Background entry point.  Wired to QThread.started in
        ``EibiDownloader.start``.

        Tries each URL in ``self._urls`` in order.  Stops on the
        first success.  If ALL fail, emits ``finished_error`` with
        a summary of what was tried.
        """
        if not self._urls:
            self.finished_error.emit("No URLs to try.")
            return
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.finished_error.emit(
                f"Disk error preparing {self._dest.parent}: {e}")
            return
        attempts = []
        for url in self._urls:
            self.status.emit(f"Trying {url} …")
            ok, msg = self._try_one(url)
            if ok:
                # msg is the byte count on success.
                self.finished_ok.emit(str(self._dest), int(msg))
                return
            attempts.append(f"  {url}\n    -> {msg}")
        # All URLs failed.  Show the operator everything we tried.
        summary = ("All EiBi mirrors failed.  Attempts:\n"
                   + "\n".join(attempts)
                   + "\n\n"
                   "Workaround: download the CSV in your browser "
                   "from https://eibispace.de/ and place it in "
                   f"{self._dest.parent} -- then restart Lyra.")
        self.finished_error.emit(summary)

    def _try_one(self, url: str) -> tuple[bool, str]:
        """Attempt a single URL.  Returns (True, byte_count_str)
        on success, (False, error_string) on failure."""
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen
        try:
            req = Request(url, headers={"User-Agent": self._ua})
            with urlopen(req, timeout=self.TIMEOUT_S) as resp:
                cl_header = resp.headers.get("Content-Length")
                total = int(cl_header) if cl_header else -1
                done = 0
                tmp = self._dest.with_suffix(
                    self._dest.suffix + ".tmp")
                with tmp.open("wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        self.progress.emit(done, total)
                if self._dest.exists():
                    self._dest.unlink()
                tmp.replace(self._dest)
            return True, str(done)
        except HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except URLError as e:
            return False, f"Network/SSL: {e.reason}"
        except OSError as e:
            return False, f"Disk error: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


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
    status   = Signal(str)            # which URL we're trying
    finished_ok = Signal(str, int)    # path, byte_count
    finished_error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None,
                 base_url: Optional[str] = None,
                 user_agent_version: str = ""):
        super().__init__(parent)
        # If operator overrode the base URL via QSettings, that
        # wins -- otherwise iterate through the EIBI_FALLBACK_URLS
        # chain.  base_url=None preserves the chain.
        self._override_base_url = (
            base_url.rstrip("/") + "/"
            if base_url else None)
        self._user_agent_version = user_agent_version
        self._thread: Optional[QThread] = None
        self._worker: Optional[_DownloadWorker] = None

    def _build_url_list(self, filename: str) -> list:
        """Return the list of URLs to try, in priority order.
        Uses operator override when set, otherwise the
        EIBI_FALLBACK_URLS chain.
        """
        if self._override_base_url:
            return [self._override_base_url + filename]
        return [base + filename for base in EIBI_FALLBACK_URLS]

    def fetch(self, dest_dir: Path | str,
              season: str = "auto",
              filename: Optional[str] = None) -> None:
        """Kick off a fetch.  Returns immediately; result arrives
        via ``finished_ok`` / ``finished_error`` signals.

        ``season`` accepts ``'auto'`` (default), ``'A'``, or ``'B'``.
        ``filename`` overrides the auto-named season file -- useful
        for operators who want to load a specific file by URL
        (e.g. an archived season for historical analysis).

        Iterates through the fallback URL chain on TLS / network
        failures so a misconfigured cert on one mirror doesn't
        block the operator from getting the data.
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
        urls = self._build_url_list(fname)
        self._thread = QThread(self)
        self._worker = _DownloadWorker(
            urls=urls, dest=dest,
            user_agent_version=self._user_agent_version)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self.progress)
        self._worker.status.connect(self.status)
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
