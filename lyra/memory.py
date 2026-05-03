"""Operator-named frequency memory presets.

A simple flat memory bank — up to 20 operator-saved (name, freq,
mode, notes) entries that the operator can recall by name from
the Bands panel's "Mem" dropdown button or manage in
Settings → Bands → Memory.

Persists in QSettings under ``bands/user_presets`` as a single
JSON-serialized list of dicts.  Single-key persistence keeps the
QSettings registry clean and lets us atomically replace the whole
list (add / delete / reorder all become "rewrite the JSON").

Why 20?
-------
Operator-set cap.  More than ~20 in a flat list becomes hard to
navigate by name; if operators routinely want hundreds of
entries we'd build a bank-of-banks system (v0.1+).  20 covers
typical use cases: a few favorite frequencies per band you visit
regularly + a few utility / time stations + a few "DX I'm trying
to work" entries.

Design doc: ``docs/architecture/v0.0.9_memory_stations_design.md``
section 4.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class MemoryPreset:
    """One memory entry.

    Operator-meaningful fields:
        name:      30-char display name shown in the dropdown menu
                   and management table.  Required, must be unique
                   (case-insensitive) within the bank.
        freq_hz:   absolute sky frequency.  Recalled directly via
                   ``Radio.set_freq_hz``.
        mode:      demod mode string ("USB", "LSB", "CWU", "AM",
                   ...).  Recalled via ``Radio.set_mode`` BEFORE
                   the freq, so the demod is set up correctly when
                   the freq lands.
        notes:     80-char free-text remarks shown as tooltip in
                   the dropdown.  Optional, can be blank.
        rx_bw_hz:  optional bandwidth override.  None means
                   "leave RX BW at the mode's current setting" --
                   most operators want this.  Override is for the
                   rare case where a preset is for a specific
                   bandwidth (e.g., a 250 Hz CW filter on a
                   weak-signal CW preset).
    """
    name: str
    freq_hz: int
    mode: str
    notes: str = ""
    rx_bw_hz: Optional[int] = None

    def __post_init__(self):
        # Sanity / clamp on construction so bad operator input
        # can't poison QSettings.
        self.name = str(self.name).strip()[:MemoryStore.MAX_NAME_LEN]
        self.freq_hz = int(self.freq_hz)
        self.mode = str(self.mode).strip()
        self.notes = str(self.notes).strip()[
            :MemoryStore.MAX_NOTES_LEN]
        if self.rx_bw_hz is not None:
            try:
                self.rx_bw_hz = int(self.rx_bw_hz)
            except (TypeError, ValueError):
                self.rx_bw_hz = None


class MemoryStore:
    """Manages the operator's memory presets list.

    Single-instance per Lyra session — instantiated by Radio (or
    the panel that uses it) at startup, hydrated from QSettings.
    Mutations (add / update / delete) write back to QSettings
    immediately so a crash doesn't lose the operator's work.

    The store is intentionally NOT a ``QObject`` -- the panel is
    the natural Qt-signal owner for "memory changed" events; this
    class just owns the data.
    """

    MAX_PRESETS = 20
    MAX_NAME_LEN = 30
    MAX_NOTES_LEN = 80

    QSETTINGS_KEY = "bands/user_presets"

    def __init__(self):
        self._presets: list[MemoryPreset] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        """Hydrate from QSettings.  Malformed entries are skipped
        with a logged warning rather than crashing -- a single bad
        row shouldn't take out the whole bank."""
        from PySide6.QtCore import QSettings as _QS
        qs = _QS("N8SDR", "Lyra")
        raw = qs.value(self.QSETTINGS_KEY, "")
        if not raw:
            return
        try:
            data = json.loads(str(raw))
        except json.JSONDecodeError as e:
            print(f"[MemoryStore] QSettings JSON corrupt: {e}; "
                  "starting with empty bank")
            return
        if not isinstance(data, list):
            print(f"[MemoryStore] QSettings entry isn't a list "
                  f"(got {type(data).__name__}); starting empty")
            return
        for entry in data:
            try:
                self._presets.append(MemoryPreset(
                    name=str(entry.get("name", "")),
                    freq_hz=int(entry.get("freq_hz", 0)),
                    mode=str(entry.get("mode", "USB")),
                    notes=str(entry.get("notes", "")),
                    rx_bw_hz=entry.get("rx_bw_hz"),
                ))
            except (TypeError, ValueError, KeyError) as e:
                print(f"[MemoryStore] skipping malformed entry "
                      f"{entry!r}: {e}")
                continue
        # Trim to MAX_PRESETS in case the user-edited registry
        # exceeds the cap.  Newest entries (later in the list) win.
        if len(self._presets) > self.MAX_PRESETS:
            self._presets = self._presets[: self.MAX_PRESETS]

    def _save(self) -> None:
        """Atomically write the whole bank to QSettings.  Called
        after every mutation."""
        from PySide6.QtCore import QSettings as _QS
        qs = _QS("N8SDR", "Lyra")
        data = [asdict(p) for p in self._presets]
        qs.setValue(self.QSETTINGS_KEY, json.dumps(data))

    # ── Queries ──────────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self._presets)

    @property
    def at_max(self) -> bool:
        """True if the bank is at MAX_PRESETS -- caller (e.g. the
        panel's "Save current as new preset" menu item) should
        grey itself out and prompt the operator to delete an
        existing entry first."""
        return len(self._presets) >= self.MAX_PRESETS

    def list(self) -> list[MemoryPreset]:
        """Return a SHALLOW COPY of the presets list.  Mutating
        the returned list won't affect the store; mutate via
        add/update/delete instead."""
        return list(self._presets)

    def get(self, idx: int) -> Optional[MemoryPreset]:
        """Return the preset at index ``idx``, or None if out
        of range."""
        if 0 <= idx < len(self._presets):
            return self._presets[idx]
        return None

    def find_by_name(self, name: str) -> Optional[int]:
        """Case-insensitive name lookup.  Returns the first index
        where ``preset.name`` matches, or None.  Used by the save
        dialog to detect overwrites."""
        target = (name or "").strip().lower()
        if not target:
            return None
        for i, p in enumerate(self._presets):
            if p.name.lower() == target:
                return i
        return None

    # ── Mutations ────────────────────────────────────────────────

    def add(self, preset: MemoryPreset) -> bool:
        """Append a new preset.  Returns False if the bank is
        already at MAX_PRESETS (caller is expected to confirm
        with operator before deleting an existing entry).

        Name collision policy: caller is responsible for checking
        ``find_by_name`` before calling ``add``.  This method does
        NOT auto-replace -- it'll happily add duplicates if you
        let it (which is probably not what you want).
        """
        if self.at_max:
            return False
        if not preset.name:
            return False
        self._presets.append(preset)
        self._save()
        return True

    def update(self, idx: int, preset: MemoryPreset) -> bool:
        """Replace the preset at ``idx`` with a new one.  Returns
        False if ``idx`` out of range."""
        if not (0 <= idx < len(self._presets)):
            return False
        self._presets[idx] = preset
        self._save()
        return True

    def delete(self, idx: int) -> bool:
        """Remove the preset at ``idx``.  Returns False if out
        of range."""
        if not (0 <= idx < len(self._presets)):
            return False
        del self._presets[idx]
        self._save()
        return True

    def move(self, src: int, dst: int) -> bool:
        """Move the preset at ``src`` to position ``dst`` (drag-
        reorder support).  Both indices clamp to valid range;
        returns False on out-of-range src."""
        if not (0 <= src < len(self._presets)):
            return False
        dst = max(0, min(len(self._presets) - 1, dst))
        if src == dst:
            return True
        item = self._presets.pop(src)
        self._presets.insert(dst, item)
        self._save()
        return True

    def clear(self) -> None:
        """Remove every preset.  Caller (Settings → Memory →
        Clear All button) should confirm with operator first --
        no undo at this layer."""
        self._presets.clear()
        self._save()
