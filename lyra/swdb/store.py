"""In-memory frequency-indexed lookup of EibiEntry records.

The EiBi CSV holds ~30,000 entries.  Per-frame linear scan would
cost ~3 ms per panadapter frame, which is too much for a 60 fps
overlay.  Instead we sort by frequency once on load and use
binary search to slice the range a panadapter pass needs.

Typical visible-range query at 24 kHz panadapter span on a
single SW band:  K_visible = 30-100 entries, lookup ~3 us total.
"""
from __future__ import annotations

import bisect
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .eibi_parser import EibiEntry, parse_csv
from .time_filter import is_on_air

_log = logging.getLogger(__name__)


class EibiStore:
    """In-memory database of EiBi entries.

    Lifecycle:
      - Construct empty.
      - ``load(path)`` parses a CSV file.
      - ``lookup_in_range(lo_khz, hi_khz, ...)`` queries the
        sorted index per panadapter frame.
      - ``unload()`` releases all data; ``loaded`` flag flips
        to False until next load.
    """

    def __init__(self):
        # Entries sorted by frequency (kHz, ascending).
        self._entries: list[EibiEntry] = []
        # Parallel array of just the freq_khz values, for binary
        # search.  Kept in sync with _entries.
        self._freqs_khz: list[int] = []
        # Source-file metadata, surfaced in the Settings UI.
        self._source_path: Optional[Path] = None
        self._source_label: str = ""   # e.g. "EiBi A26"
        self._loaded_at: Optional[datetime] = None

    # ── Status accessors ─────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return bool(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def source_label(self) -> str:
        """Human-readable source-file tag for the Settings UI
        ('EiBi A26' etc.).  Empty string when nothing loaded."""
        return self._source_label

    @property
    def source_path(self) -> Optional[Path]:
        return self._source_path

    @property
    def loaded_at(self) -> Optional[datetime]:
        """When ``load()`` last completed.  None if never
        loaded.  Used by Settings to show 'X days old.'"""
        return self._loaded_at

    # ── Lifecycle ────────────────────────────────────────────

    def load(self, path: Path | str,
             label: str = "") -> tuple[int, list[str]]:
        """Parse ``path`` into the in-memory index.  Replaces
        any previously loaded data atomically -- on parse error
        the old data is preserved.

        Returns ``(entry_count, error_strings)``.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"EiBi file not found: {p}")
        entries, errors = parse_csv(p)
        if not entries:
            return 0, errors
        # Atomic replace.
        entries.sort(key=lambda e: e.freq_khz)
        self._entries = entries
        self._freqs_khz = [e.freq_khz for e in entries]
        self._source_path = p
        self._source_label = label or _label_from_filename(p)
        self._loaded_at = datetime.now(timezone.utc)
        _log.info(
            "EibiStore loaded %d entries from %s (%s)",
            len(entries), p, self._source_label)
        return len(entries), errors

    def unload(self) -> None:
        self._entries = []
        self._freqs_khz = []
        self._source_path = None
        self._source_label = ""
        self._loaded_at = None

    # ── Query: visible-range lookup ──────────────────────────

    def lookup_in_range(self,
                        freq_lo_khz: int,
                        freq_hi_khz: int,
                        utc: Optional[datetime] = None,
                        min_power: int = 1,
                        only_on_air: bool = True
                        ) -> list[EibiEntry]:
        """Return entries whose frequency is in ``[lo, hi]``,
        filtered by power class and (optionally) on-air state.

        Args:
            freq_lo_khz, freq_hi_khz: search range in kHz.
            utc: timestamp to use for on-air check; defaults to
                 now.  Tests pass a fixed value.
            min_power: minimum EiBi power class (0..3).  Default
                       1 = "50+ kW, likely receivable" per the
                       Settings UI's operator-friendly phrasing.
            only_on_air: True to filter out entries whose
                         schedule window doesn't include ``utc``.

        Implementation:
            * Binary search on _freqs_khz to find the slice
              boundary.
            * Linear filter pass over that slice (cheap; slice
              is typically small at panadapter zoom levels).

        Returns the matching entries in frequency-ascending order.
        """
        if not self._entries:
            return []
        if freq_hi_khz < freq_lo_khz:
            freq_lo_khz, freq_hi_khz = freq_hi_khz, freq_lo_khz
        lo_idx = bisect.bisect_left(self._freqs_khz, freq_lo_khz)
        hi_idx = bisect.bisect_right(self._freqs_khz, freq_hi_khz)
        if lo_idx >= hi_idx:
            return []
        result: list[EibiEntry] = []
        for entry in self._entries[lo_idx:hi_idx]:
            if entry.power_class < min_power:
                continue
            if only_on_air and not is_on_air(entry, utc):
                continue
            result.append(entry)
        return result

    def all_entries(self) -> list[EibiEntry]:
        """Return a shallow copy of the full sorted-by-frequency
        list.  Used by tests + diagnostics; NOT the panadapter hot
        path (which uses ``lookup_in_range``)."""
        return list(self._entries)


# ── Helpers ───────────────────────────────────────────────────


def _label_from_filename(p: Path) -> str:
    """Derive an operator-readable label from an EiBi file name.
    'sked-A26.csv' -> 'EiBi A26'.  Falls back to the basename
    when the conventional pattern isn't matched."""
    stem = p.stem.upper()
    if stem.startswith("SKED-"):
        return f"EiBi {stem[5:]}"
    return f"EiBi {p.name}"
