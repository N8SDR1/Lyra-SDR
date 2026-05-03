"""EiBi CSV format parser.

EiBi (Eike Bierwirth) publishes a semicolon-delimited CSV of
worldwide shortwave broadcaster schedules at
<https://www.eibispace.de/>.  Files are named by ITU broadcast
season:

    sked-A26.csv  =  April-October 2026  (DST / summer)
    sked-B26.csv  =  October 2026 - March 2027  (winter)

Header row::

    KHZ;TIM;DAY;ITU;STN;LANG;TGT;REM;P;START;STOP

All fields are strings; some may be empty.  We parse each row
into an ``EibiEntry`` dataclass with light coercion: KHZ -> int,
TIM -> (start_minute, stop_minute) UTC tuples, DAY -> frozenset
of weekday numbers, P -> int 0-3 with defaults.

Bad rows are SKIPPED with a logged warning rather than aborting
parse -- operator-supplied files may have manual edits.

EiBi license: free for non-commercial use, attribution required.
Lyra does not redistribute the file; operators download it
themselves.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EibiEntry:
    """One scheduled shortwave broadcast.

    Attributes:
        freq_khz:    broadcast frequency in kHz (integer).
        time_start:  start of transmission window, UTC minutes
                     since midnight (0..1440).
        time_stop:   end of transmission window, UTC minutes
                     since midnight (0..1440).  Wraps -- if
                     stop < start, the entry crosses 0000 UTC.
        days:        frozenset of ISO-week day numbers (1=Mon,
                     7=Sun).  Empty set means "every day"
                     (per EiBi convention: blank DAY field).
        itu_code:    ITU country code (3-letter, e.g. "USA",
                     "GBR", "RUS").
        station:     station name (e.g. "Voice of America").
        language:    EiBi language code (single-char or short,
                     e.g. "E"=English, "S"=Spanish).
        target:      target area code (e.g. "EaAs", "Eu", "NAm").
        remarks:     site / transmitter remarks (free text).
        power_class: 0=<50kW, 1=50-100kW, 2=100-250kW, 3=250+kW.
                     Defaults to 0 if the EiBi field is empty
                     or unparseable.
        valid_from:  schedule period start (YYMMDD or None).
        valid_to:    schedule period end (YYMMDD or None).
    """
    freq_khz: int
    time_start: int
    time_stop: int
    days: frozenset[int]
    itu_code: str
    station: str
    language: str
    target: str
    remarks: str
    power_class: int
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None

    @property
    def is_daily(self) -> bool:
        """True if this entry transmits every day (DAY field
        was blank in the source)."""
        return not self.days


# ── Row coercion helpers ──────────────────────────────────────


def _parse_tim(s: str) -> Optional[tuple[int, int]]:
    """Parse a TIM field of the form 'HHMM-HHMM' (UTC).  Returns
    (start_minute, stop_minute) tuple or None on malformed input.

    Examples:
        '0000-2400' -> (0, 1440)
        '1500-1600' -> (900, 960)
        '2300-0100' -> (1380, 60)   (crosses midnight)
    """
    s = (s or "").strip()
    if not s:
        return None
    if "-" not in s:
        return None
    a, b = s.split("-", 1)
    a = a.strip()
    b = b.strip()
    if len(a) != 4 or len(b) != 4 or not (a.isdigit() and b.isdigit()):
        return None
    try:
        sh, sm = int(a[:2]), int(a[2:])
        eh, em = int(b[:2]), int(b[2:])
    except ValueError:
        return None
    # EiBi uses 2400 to mean end-of-day (= midnight next day).
    # Allow it through; the time_filter logic handles the
    # special case via modulo math.
    if not (0 <= sh <= 24 and 0 <= eh <= 24):
        return None
    if not (0 <= sm < 60 and 0 <= em < 60):
        return None
    return (sh * 60 + sm, eh * 60 + em)


def _parse_days(s: str) -> frozenset[int]:
    """Parse a DAY field like '1234567' (each digit = a weekday)
    into a frozenset of day numbers.  Empty / blank string ->
    empty set (= every day, per EiBi convention).

    Convention: EiBi uses 1=Mon ... 7=Sun, matching Python's
    isoweekday().
    """
    s = (s or "").strip()
    if not s:
        return frozenset()
    days = set()
    for ch in s:
        if ch.isdigit():
            d = int(ch)
            if 1 <= d <= 7:
                days.add(d)
    return frozenset(days)


def _parse_power(s: str) -> int:
    """Parse the P field (0-3 power class).  Empty / non-numeric
    -> 0 (treated as 'low power, <50 kW').  See EibiEntry docstring
    for the class meanings."""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        v = int(s)
    except ValueError:
        return 0
    return max(0, min(3, v))


def _parse_yymmdd(s: str) -> Optional[date]:
    """Parse a YYMMDD string into a date.  Returns None on
    malformed input.  EiBi years are 2-digit; we assume
    2000-2099 (the data starts in 2000)."""
    s = (s or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    try:
        yy = int(s[0:2])
        mm = int(s[2:4])
        dd = int(s[4:6])
        return date(2000 + yy, mm, dd)
    except (ValueError, OverflowError):
        return None


# ── Public parser ─────────────────────────────────────────────


def parse_csv(path: Path | str) -> tuple[list[EibiEntry], list[str]]:
    """Parse an EiBi CSV file into ``(entries, errors)``.

    ``errors`` is a list of human-readable warning strings for
    rows that couldn't be parsed.  Truncated to ~100 entries to
    avoid runaway memory if the file is corrupt.

    Stable across malformed input -- a single bad row never aborts
    the parse.
    """
    return _parse_text(_read_text(path))


def parse_string(text: str) -> tuple[list[EibiEntry], list[str]]:
    """Convenience wrapper for testing -- parse a string of CSV
    content directly without going through a file.  Same
    contract as ``parse_csv``."""
    return _parse_text(text)


def _read_text(path: Path | str) -> str:
    p = Path(path)
    # EiBi files are typically UTF-8, but historically some have
    # been Latin-1 with non-ASCII station names.  Try UTF-8 first,
    # fall back to Latin-1 with replace on errors so a mis-encoded
    # row doesn't fail the whole parse.
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return p.read_text(encoding="latin-1", errors="replace")


def _parse_text(text: str) -> tuple[list[EibiEntry], list[str]]:
    entries: list[EibiEntry] = []
    errors: list[str] = []
    MAX_ERRORS = 100

    # Use csv.reader with semicolon delimiter; EiBi sometimes has
    # quoted fields containing semicolons in REM, so quote-aware
    # parsing matters.
    reader = csv.reader(text.splitlines(), delimiter=";",
                        quotechar='"')
    header_seen = False
    for line_no, row in enumerate(reader, start=1):
        if not row:
            continue
        # Skip the header row (case-insensitive contains 'KHZ').
        if not header_seen:
            joined = ";".join(row).upper()
            if "KHZ" in joined and "TIM" in joined:
                header_seen = True
                continue
            # EiBi files may begin with a few comment lines that
            # don't have a header -- count any line with too few
            # columns as a comment and skip.
            if len(row) < 5:
                continue
        if len(row) < 5:
            # Comment / blank-ish line in the middle of the file.
            continue
        try:
            entry = _parse_row(row)
            if entry is not None:
                entries.append(entry)
        except (ValueError, TypeError) as e:
            if len(errors) < MAX_ERRORS:
                errors.append(f"line {line_no}: {e}")
            continue
    return entries, errors


def _parse_row(row: list[str]) -> Optional[EibiEntry]:
    """Parse a single CSV row into an ``EibiEntry``.  Returns None
    if the row is unusable (e.g. unparseable freq); raises
    ValueError for cases where we want to log a warning."""
    # Pad row to expected 11 columns (KHZ;TIM;DAY;ITU;STN;LANG;TGT;
    # REM;P;START;STOP) so missing tail fields don't crash.
    while len(row) < 11:
        row = row + [""]
    khz_s = row[0].strip()
    if not khz_s:
        return None
    # KHZ may have a trailing fractional kHz on a few rows
    # (e.g. "1611.5") -- truncate to integer kHz for the index.
    try:
        khz = int(round(float(khz_s)))
    except ValueError:
        return None
    # Reject obviously-out-of-range frequencies
    # (EiBi covers ~150 kHz to 30 MHz).
    if not (100 <= khz <= 35000):
        return None

    tim = _parse_tim(row[1])
    if tim is None:
        # Unparseable time window -- skip rather than guess.
        # No error logged because partial-entry CSV rows are common.
        return None
    time_start, time_stop = tim

    days = _parse_days(row[2])
    itu = row[3].strip()
    stn = row[4].strip()
    lang = row[5].strip()
    tgt = row[6].strip()
    rem = row[7].strip()
    pwr = _parse_power(row[8])
    valid_from = _parse_yymmdd(row[9]) if len(row) > 9 else None
    valid_to = _parse_yymmdd(row[10]) if len(row) > 10 else None

    if not stn:
        # Station name required; without it the overlay can't
        # render anything useful.
        return None
    return EibiEntry(
        freq_khz=khz,
        time_start=time_start,
        time_stop=time_stop,
        days=days,
        itu_code=itu,
        station=stn,
        language=lang,
        target=tgt,
        remarks=rem,
        power_class=pwr,
        valid_from=valid_from,
        valid_to=valid_to,
    )
