"""Backup / import / export for Lyra's QSettings.

Lyra stores every operator-tunable preference (layout, IP, audio device,
AGC profile, color picks, balance trim, cal offset, dock positions,
band memory, etc.) under a single QSettings namespace
("N8SDR" / "Lyra"). On Windows that's the registry path
`HKEY_CURRENT_USER\\Software\\N8SDR\\Lyra`; on other platforms, the
QSettings default file path for the org/app pair.

This module exposes three operations:

  export_settings(path)     — write all keys to a JSON file
  import_settings(path)     — read a JSON file and replay it into QSettings
                              (creates an auto-snapshot first as a safety net)
  auto_snapshot()           — write a timestamped snapshot to the snapshots
                              directory; sweeps oldest snapshots beyond the
                              MAX_AUTO_SNAPSHOTS retention limit

Plus helpers:

  list_snapshots()          — return list of snapshot file Paths, newest first
  snapshots_dir()           — return Path to the snapshots directory

JSON format
-----------
The on-disk JSON is human-readable so an operator can inspect / hand-edit
a snapshot if they need to:

  {
    "lyra_version":   "0.0.3",
    "schema_version": 1,
    "exported_at":    "2026-04-25T14:23:01.123456",
    "settings": {
        "<flat/key>": <json-encodable value>,
        ...
    }
  }

QByteArray values (the dock_state / center_split blobs holding window
geometry + dock positions) are base64-encoded and wrapped in a small
type-tag dict so they round-trip cleanly through JSON:

  "dock_state": {"_lyra_type": "qbytearray", "data": "AAA..."}

Schema versioning
-----------------
`schema_version` is bumped any time the on-disk JSON contract changes
in a way that requires migration. Today's schema is version 1 — a flat
dict of QSettings keys. Future versions might introduce categories,
profile slots, etc.; the import path will dispatch on the version tag
and migrate older snapshots forward.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QSettings, QStandardPaths

import lyra


SCHEMA_VERSION = 1
ORG_NAME = "N8SDR"
APP_NAME = "Lyra"

# Auto-snapshot retention: keep the N most recent automatic snapshots.
# Older ones get pruned on the next snapshot creation. Manually-named
# exports created via "Export current settings…" are NOT counted toward
# this limit (different filename pattern, see _is_auto_snapshot).
MAX_AUTO_SNAPSHOTS = 10

# Filename patterns. Auto-snapshots use the timestamp pattern; manual
# exports get whatever filename the operator picks.
_AUTO_SNAPSHOT_PREFIX = "auto-snapshot-"
_AUTO_SNAPSHOT_SUFFIX = ".json"
# Strict pattern so "auto-snapshot-anything-else.json" doesn't get
# pruned accidentally.
_AUTO_SNAPSHOT_RE = re.compile(
    r"^auto-snapshot-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.json$"
)


# ── Filesystem layout ─────────────────────────────────────────────────
def snapshots_dir() -> Path:
    """Return Path to the snapshots directory, creating it if needed.

    Uses Qt's AppLocalDataLocation which on Windows resolves to
    `%LOCALAPPDATA%\\N8SDR\\Lyra\\snapshots`. On Linux it's
    `~/.local/share/Lyra/snapshots` (or similar). All snapshots are
    plain JSON files in this single flat directory — no subdirs.
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.AppLocalDataLocation)
    if not base:
        # Fallback for any platform Qt can't resolve a path on.
        base = str(Path.home() / ".lyra")
    p = Path(base) / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_snapshots() -> list[Path]:
    """All JSON files in the snapshots directory, newest mtime first.
    Includes both auto-snapshots and any manual exports the operator
    happens to have saved into the same directory."""
    d = snapshots_dir()
    files = [p for p in d.glob("*.json") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _is_auto_snapshot(p: Path) -> bool:
    return bool(_AUTO_SNAPSHOT_RE.match(p.name))


# ── QSettings ⇄ JSON serialization ────────────────────────────────────
_QBYTEARRAY_TYPE_TAG = "qbytearray"


def _encode_value(v: Any) -> Any:
    """Convert a single QSettings value into a JSON-encodable form.

    The bulk of QSettings values pass through unchanged (str / int /
    float / bool / list of those). The two awkward cases are:

      - QByteArray (dock_state / center_split / geometry) — wrapped
        in a type-tag dict with base64 payload.
      - bytes (some platforms hand back raw bytes for the same fields)
        — same wrapping, since base64-encoded bytes are how we choose
        to round-trip them.
    """
    if isinstance(v, QByteArray):
        return {
            "_lyra_type": _QBYTEARRAY_TYPE_TAG,
            "data": base64.b64encode(bytes(v)).decode("ascii"),
        }
    if isinstance(v, (bytes, bytearray)):
        return {
            "_lyra_type": _QBYTEARRAY_TYPE_TAG,
            "data": base64.b64encode(bytes(v)).decode("ascii"),
        }
    if isinstance(v, (list, tuple)):
        return [_encode_value(x) for x in v]
    if isinstance(v, dict):
        # Defensive — Qt sometimes returns dicts for QStringMap-typed
        # values. Recurse so nested QByteArrays still survive.
        return {k: _encode_value(val) for k, val in v.items()}
    # Fallback: anything not natively JSON-encodable becomes a string.
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _decode_value(v: Any) -> Any:
    """Inverse of _encode_value — turn a JSON value back into the
    QSettings type it should be (QByteArray for the type-tagged case,
    everything else passes through)."""
    if isinstance(v, dict) and v.get("_lyra_type") == _QBYTEARRAY_TYPE_TAG:
        try:
            raw = base64.b64decode(v["data"])
            return QByteArray(raw)
        except Exception:
            # Malformed base64 — drop the value rather than corrupt
            # QSettings with a bad type.
            return None
    if isinstance(v, list):
        return [_decode_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _decode_value(val) for k, val in v.items()}
    return v


def _open_settings() -> QSettings:
    return QSettings(ORG_NAME, APP_NAME)


# ── Public API ────────────────────────────────────────────────────────
def export_settings(path: Path) -> int:
    """Write the entire QSettings namespace to `path` as JSON.

    Returns the number of keys exported. The Lyra version that produced
    the snapshot is recorded in the JSON header so import can warn on a
    cross-version restore.
    """
    s = _open_settings()
    data: dict[str, Any] = {}
    for key in s.allKeys():
        data[key] = _encode_value(s.value(key))
    payload = {
        "lyra_version":   getattr(lyra, "__version__", "unknown"),
        "schema_version": SCHEMA_VERSION,
        "exported_at":    datetime.now().isoformat(),
        "settings":       data,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return len(data)


def import_settings(path: Path,
                    snapshot_first: bool = True) -> tuple[int, Optional[Path]]:
    """Replay every key from a JSON snapshot back into QSettings.

    By default, takes a safety snapshot of the current state BEFORE
    applying the import — so the operator can always roll back via
    File → Settings → Snapshots if the import was a mistake. The
    safety-snapshot path is returned so the caller can show a
    user-visible "rolled to <name>" hint.

    Returns (num_keys_imported, safety_snapshot_path_or_None).

    Schema mismatch handling: if the JSON's schema_version is higher
    than this build understands, raises ValueError — better to refuse
    than to half-apply settings the operator's snapshot author meant
    to be paired with new keys we don't know about yet.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: not a Lyra settings snapshot")
    schema = int(payload.get("schema_version", 0))
    if schema > SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} was written by a newer Lyra (schema "
            f"v{schema}); this build understands up to v{SCHEMA_VERSION}. "
            "Update Lyra or use a snapshot from a matching version.")
    settings_dict = payload.get("settings", {})
    if not isinstance(settings_dict, dict):
        raise ValueError(f"{path.name}: 'settings' is not an object")

    safety_path: Optional[Path] = None
    if snapshot_first:
        safety_path = auto_snapshot(reason="pre-import-safety")

    s = _open_settings()
    # Clear-then-write rather than merge, so removed keys actually go
    # away. Otherwise the operator can never "shrink" their config by
    # importing a leaner snapshot.
    s.clear()
    count = 0
    for key, raw in settings_dict.items():
        s.setValue(key, _decode_value(raw))
        count += 1
    s.sync()
    return count, safety_path


def auto_snapshot(reason: str = "launch") -> Path:
    """Take an automatic snapshot of the current QSettings state.

    The `reason` is encoded into the filename to help the operator
    distinguish "this snapshot was taken because Lyra just launched"
    from "this one was taken because we were about to import another
    snapshot." Both are still pruned by the same MAX_AUTO_SNAPSHOTS
    retention sweep below.

    Returns the Path to the new snapshot.
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"{_AUTO_SNAPSHOT_PREFIX}{ts}{_AUTO_SNAPSHOT_SUFFIX}"
    path = snapshots_dir() / name
    # If two snapshots land in the same second (rare but possible —
    # snapshot at launch can collide with snapshot at first import),
    # disambiguate with a numeric suffix so we never overwrite.
    if path.exists():
        for n in range(1, 100):
            alt = snapshots_dir() / (
                f"{_AUTO_SNAPSHOT_PREFIX}{ts}-{n}{_AUTO_SNAPSHOT_SUFFIX}")
            if not alt.exists():
                path = alt
                break
    export_settings(path)
    # Optionally include the reason as a sidecar — for now we encode
    # it in the JSON header instead so we don't pollute the directory
    # with twice as many files.
    try:
        with path.open("r+", encoding="utf-8") as f:
            payload = json.load(f)
            payload["snapshot_reason"] = reason
            f.seek(0); f.truncate()
            json.dump(payload, f, indent=2, sort_keys=True)
    except Exception:
        # Header annotation is nice-to-have, not critical.
        pass
    _prune_auto_snapshots()
    return path


def _prune_auto_snapshots():
    """Keep only the MAX_AUTO_SNAPSHOTS most recent auto-snapshot files;
    delete older ones. Manual exports (any filename not matching the
    auto-snapshot pattern) are NEVER touched by this sweep."""
    autos = [p for p in list_snapshots() if _is_auto_snapshot(p)]
    # list_snapshots returns newest-first; everything past the limit
    # is older than we want to keep.
    for old in autos[MAX_AUTO_SNAPSHOTS:]:
        try:
            old.unlink()
        except OSError:
            # Permission denied / file in use — best-effort, skip.
            pass


def snapshot_summary(path: Path) -> dict:
    """Read just the metadata header from a snapshot file (without
    loading the full settings dict). Returns a dict with keys:

      lyra_version, schema_version, exported_at, snapshot_reason,
      settings_count, file_size, mtime, name

    Used by the Snapshots submenu to label each entry meaningfully.
    """
    info = {
        "name":          path.name,
        "file_size":     path.stat().st_size,
        "mtime":         datetime.fromtimestamp(path.stat().st_mtime),
        "lyra_version":  "?",
        "schema_version": 0,
        "exported_at":   "",
        "snapshot_reason": "",
        "settings_count": 0,
    }
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        info["lyra_version"]    = str(payload.get("lyra_version", "?"))
        info["schema_version"]  = int(payload.get("schema_version", 0))
        info["exported_at"]     = str(payload.get("exported_at", ""))
        info["snapshot_reason"] = str(payload.get("snapshot_reason", ""))
        info["settings_count"]  = len(payload.get("settings", {}))
    except Exception:
        pass
    return info
