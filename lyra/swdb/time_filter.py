"""'Currently on-air?' predicate for EibiEntry instances.

Given the current UTC datetime and an entry's TIM + DAY fields,
decide whether the broadcaster is transmitting right now.  Used
by the panadapter overlay to filter out stations whose schedule
window doesn't include the moment of rendering.

This is per-frame work for ~30k entries when zoomed out, so the
math is kept tight: integer-only minute arithmetic, set membership
on the day-of-week filter, no datetime objects in the hot path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .eibi_parser import EibiEntry


def is_on_air(entry: EibiEntry,
              utc: Optional[datetime] = None) -> bool:
    """Return True iff ``entry`` is currently transmitting per
    its schedule.

    UTC time defaults to ``datetime.now(timezone.utc)`` (the
    common path for live overlay rendering).  Tests can pass a
    fixed value to drive specific schedule windows.

    Schedule logic:
      * Day-of-week: matches if entry's days set is empty (=
        every day) or contains the current ISO weekday.
      * Time window: minute math from 0000 UTC.  Wrap-around
        handled (e.g. 2300-0100 means 23:00 today through 01:00
        tomorrow).
    """
    if utc is None:
        utc = datetime.now(timezone.utc)

    # Day-of-week filter.
    today_dow = utc.isoweekday()  # 1=Mon ... 7=Sun
    if entry.days and today_dow not in entry.days:
        return False

    # Minute-of-day filter.  EiBi uses 24:00 as a synonym for
    # 00:00 next day; minutes in the parser come out as
    # start_min in [0, 1440] and stop_min in [0, 1440].
    now_min = utc.hour * 60 + utc.minute
    s = entry.time_start
    e = entry.time_stop
    if s == e:
        # Zero-length window — treat as 'always off' rather than
        # 'always on' to avoid pathological all-day stations
        # with bad data getting through.
        return False
    if s < e:
        # Same-day window.
        return s <= now_min < e
    # Wrap-around (entry runs past midnight into the next day).
    # In: now_min >= s OR now_min < e.
    return now_min >= s or now_min < e


def minutes_until_change(entry: EibiEntry,
                         utc: Optional[datetime] = None) -> int:
    """Return the number of minutes until ``entry``'s on-air
    state flips (off->on or on->off).  Useful for the panadapter
    overlay's refresh cadence: redraw at most once per minute,
    only when at least one entry is about to change state.

    Always returns >= 0.  Wraps to next day if needed.
    """
    if utc is None:
        utc = datetime.now(timezone.utc)
    now_min = utc.hour * 60 + utc.minute
    on = is_on_air(entry, utc)
    target = entry.time_stop if on else entry.time_start
    delta = (target - now_min) % 1440
    return delta if delta > 0 else 1440
