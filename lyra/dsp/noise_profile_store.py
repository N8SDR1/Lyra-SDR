"""Captured-noise-profile persistence layer (Phase 3.D #1, Day 2).

Stores the operator's captured noise profiles as one JSON file per
profile in a configurable folder.  Default folder is the OS-standard
user-data location (``%APPDATA%\\Lyra`` on Windows,
``~/Library/Application Support/Lyra`` on macOS,
``~/.local/share/Lyra`` on Linux); operators can override via the
Settings → Noise tab.

Schema version 1 (this commit):

.. code-block:: json

    {
        "schema_version": 1,
        "name": "Powerline 80m",
        "captured_at_iso": "2026-04-30T14:22:13Z",
        "freq_hz": 3825000,
        "mode": "LSB",
        "duration_sec": 2.0,
        "fft_size": 256,
        "lyra_version": "0.0.6",
        "magnitudes": [/* 129 floats */]
    }

Each profile's filename on disk is ``<sanitized_name>.json``.  The
in-file ``name`` is the operator-typed display name; the filename
is a sanitized version (illegal chars stripped) so on-disk paths
work cross-platform.  Listing scans the folder for ``*.json`` and
reads the in-file ``name`` for display.

Atomic writes: every save goes to ``<final>.tmp`` first, then
``os.replace`` atomically swaps it to ``<final>``.  If Lyra crashes
mid-write, either the previous version or the new version exists
fully — never a half-written profile.

Compatibility: profiles store ``fft_size`` so a future change to
``SpectralSubtractionNR.FFT_SIZE`` doesn't silently produce
wrong-sized output.  ``is_compatible(meta)`` checks the stored
``fft_size`` against the current NR config; the manager dialog
uses this to grey-out incompatible profiles.

This module is pure persistence — no Qt, no Lyra-state coupling
beyond the small ``lyra_version`` string we stamp into each
profile.  Callable from any thread; atomic-write protects against
concurrent writers (last-writer-wins, no torn files).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

# ── Module-level constants ──────────────────────────────────────────────

SCHEMA_VERSION: int = 1
"""On-disk JSON schema version.  Bump if the format changes in a
backward-incompatible way; loaders should reject unknown versions."""

PROFILE_EXT: str = ".json"
"""File extension for profile files in the storage folder."""

# Filenames: forbid \ / : * ? " < > | (Windows) and / NUL (POSIX).
# Strip those and collapse whitespace runs to single underscores.
# Anything else (letters, digits, dashes, dots, parens, etc.) is
# kept; profile names like "Powerline 80m (daytime)" stay readable
# on disk as "Powerline_80m_(daytime).json".
_FILENAME_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\x00]')
_FILENAME_WHITESPACE_RE = re.compile(r'\s+')

DEFAULT_APP_NAME: str = "Lyra"
"""User-data subfolder name; same casing across all OS conventions."""

DEFAULT_PROFILES_SUBDIR: str = "noise_profiles"
"""Subfolder under the user-data folder where profile JSON lives."""


# ── Data classes ────────────────────────────────────────────────────────

@dataclass
class ProfileMeta:
    """Lightweight profile metadata for list-view display.

    Loaded from the JSON header without reading the full magnitudes
    array — fast scan of the profile folder.
    """
    name: str
    captured_at_iso: str
    freq_hz: int
    mode: str
    duration_sec: float
    fft_size: int
    lyra_version: str
    schema_version: int = SCHEMA_VERSION
    file_path: Optional[Path] = None
    """Absolute path to the JSON file on disk (filled in by
    :func:`list_profiles`).  Useful for the management UI."""

    def is_compatible(self, current_fft_size: int) -> bool:
        """True if this profile's fft_size matches the runtime NR
        config.  The manager dialog should grey out incompatible
        profiles rather than silently failing to load them."""
        return (self.schema_version == SCHEMA_VERSION
                and self.fft_size == current_fft_size)

    def captured_at_datetime(self) -> Optional[datetime]:
        """Parse the ISO-8601 timestamp into a datetime, or None
        if it's malformed (defensive — shouldn't happen with files
        we wrote, but JSON files are operator-editable)."""
        try:
            # Python 3.11+ accepts the trailing 'Z' directly;
            # earlier versions need substitution.
            iso = self.captured_at_iso.replace("Z", "+00:00")
            return datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None


@dataclass
class NoiseProfile:
    """Full profile record — metadata + the magnitudes array."""
    name: str
    captured_at_iso: str
    freq_hz: int
    mode: str
    duration_sec: float
    fft_size: int
    lyra_version: str
    magnitudes: np.ndarray  # float32, length fft_size//2 + 1
    schema_version: int = SCHEMA_VERSION

    def to_meta(self, file_path: Optional[Path] = None) -> ProfileMeta:
        return ProfileMeta(
            name=self.name,
            captured_at_iso=self.captured_at_iso,
            freq_hz=self.freq_hz,
            mode=self.mode,
            duration_sec=self.duration_sec,
            fft_size=self.fft_size,
            lyra_version=self.lyra_version,
            schema_version=self.schema_version,
            file_path=file_path,
        )


# ── Filename / path helpers ─────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """Convert an operator-typed profile name into a filesystem-safe
    base filename (no extension).

    - Strips characters forbidden on Windows / POSIX
    - Collapses whitespace runs to single underscores
    - Trims leading/trailing whitespace and dots
    - Falls back to "profile" if the result is empty

    Example: ``"Powerline 80m / daytime"`` →  ``"Powerline_80m__daytime"``
    """
    s = _FILENAME_FORBIDDEN_RE.sub("", str(name))
    s = _FILENAME_WHITESPACE_RE.sub("_", s).strip(" .")
    if not s:
        s = "profile"
    return s


def default_user_data_dir() -> Path:
    """OS-standard user-data folder for Lyra.

    Resolves to:
    - Windows: ``%APPDATA%\\Lyra``
    - macOS: ``~/Library/Application Support/Lyra``
    - Linux: ``~/.local/share/Lyra`` (or ``$XDG_DATA_HOME/Lyra``)

    Path is returned even if it doesn't exist yet — caller is
    responsible for ``mkdir(parents=True, exist_ok=True)`` before
    writing.  All file-writing helpers in this module create the
    folder as needed.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", "")
        if base:
            return Path(base) / DEFAULT_APP_NAME
        # Fallback if APPDATA is unset (very unusual on Windows).
        return Path.home() / "AppData" / "Roaming" / DEFAULT_APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / DEFAULT_APP_NAME
    # Linux / other POSIX.  Honor XDG_DATA_HOME if set, per the
    # XDG Base Directory Specification.
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return Path(xdg) / DEFAULT_APP_NAME
    return Path.home() / ".local" / "share" / DEFAULT_APP_NAME


def default_profile_folder() -> Path:
    """Resolve the default folder Lyra will store noise profiles in.

    Equivalent to ``default_user_data_dir() / DEFAULT_PROFILES_SUBDIR``.
    Used as the fallback when no custom path is set in QSettings.
    """
    return default_user_data_dir() / DEFAULT_PROFILES_SUBDIR


def resolve_profile_folder(custom_path: str = "") -> Path:
    """Resolve the active profile folder path.

    If ``custom_path`` is non-empty AND points to a usable directory
    (or a path whose parent exists so we could create it), use it.
    Otherwise fall back to :func:`default_profile_folder`.

    Returns the folder Path; does NOT create the folder yet —
    caller calls :func:`ensure_folder` when actually writing.

    Caller is typically Settings logic that pulls
    ``noise/profile_folder`` from QSettings and passes it here.
    Empty string is the "use default" sentinel.
    """
    if custom_path:
        p = Path(custom_path).expanduser()
        # Accept the path if it exists as a directory, OR if its
        # parent exists (so we can create it on first write).
        if p.is_dir():
            return p
        try:
            if p.parent.is_dir():
                return p
        except (OSError, ValueError):
            pass
        # Fall through to default if the custom path is unusable.
    return default_profile_folder()


def ensure_folder(folder: Path) -> Path:
    """Create the folder (and parents) if it doesn't exist.  Returns
    the folder path for chaining.  Idempotent."""
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _profile_file_path(folder: Path, name: str) -> Path:
    """Internal: derive the on-disk file path for a profile name."""
    return folder / (sanitize_filename(name) + PROFILE_EXT)


# ── List / load / save / delete / rename ────────────────────────────────

def list_profiles(folder: Path) -> list[ProfileMeta]:
    """Scan ``folder`` for profile JSON files and return a list of
    metadata records (no magnitudes loaded — fast scan).

    Profiles that fail to parse are silently skipped (they show up
    in logs via stderr but don't block the list).  An empty or
    nonexistent folder returns an empty list.

    Result is sorted by ``captured_at_iso`` descending (newest
    first), which is the most useful default for the manager
    dialog list view.
    """
    if not folder.is_dir():
        return []
    metas: list[ProfileMeta] = []
    for path in sorted(folder.glob("*" + PROFILE_EXT)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            metas.append(ProfileMeta(
                name=str(data.get("name", path.stem)),
                captured_at_iso=str(data.get("captured_at_iso", "")),
                freq_hz=int(data.get("freq_hz", 0)),
                mode=str(data.get("mode", "")),
                duration_sec=float(data.get("duration_sec", 0.0)),
                fft_size=int(data.get("fft_size", 0)),
                lyra_version=str(data.get("lyra_version", "")),
                schema_version=int(data.get("schema_version", 0)),
                file_path=path,
            ))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            # Corrupt or hand-edited profile — log and skip rather
            # than blow up the whole list.
            print(f"[noise_profile_store] skipping malformed "
                  f"profile {path.name}: {exc}",
                  file=sys.stderr)
    metas.sort(key=lambda m: m.captured_at_iso, reverse=True)
    return metas


def load_profile(folder: Path, name: str) -> NoiseProfile:
    """Load a profile by name.  Raises FileNotFoundError if the
    profile doesn't exist, ValueError on parse / schema problems."""
    path = _profile_file_path(folder, name)
    if not path.is_file():
        raise FileNotFoundError(
            f"noise profile {name!r} not found in {folder}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    schema = int(data.get("schema_version", 0))
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"profile {name!r} has unsupported schema_version "
            f"{schema} (expected {SCHEMA_VERSION})")
    mags_list = data.get("magnitudes", None)
    if not isinstance(mags_list, list):
        raise ValueError(
            f"profile {name!r} is missing or has invalid "
            f"'magnitudes' array")
    mags = np.asarray(mags_list, dtype=np.float32)
    return NoiseProfile(
        name=str(data.get("name", name)),
        captured_at_iso=str(data.get("captured_at_iso", "")),
        freq_hz=int(data.get("freq_hz", 0)),
        mode=str(data.get("mode", "")),
        duration_sec=float(data.get("duration_sec", 0.0)),
        fft_size=int(data.get("fft_size", 0)),
        lyra_version=str(data.get("lyra_version", "")),
        magnitudes=mags,
        schema_version=schema,
    )


def save_profile(folder: Path, profile: NoiseProfile,
                 overwrite: bool = False) -> Path:
    """Write ``profile`` to disk under ``folder``.  Creates the
    folder if needed.  Atomic — writes to ``<name>.json.tmp`` then
    renames to ``<name>.json``.

    Args:
        folder: target folder (created on demand)
        profile: the NoiseProfile to write
        overwrite: if False (default), raises FileExistsError when
                   a profile with the same sanitized filename
                   already exists.  UI surfaces this as
                   "name already exists, choose another or
                   delete the existing one".

    Returns the final path the profile was written to.
    """
    ensure_folder(folder)
    final_path = _profile_file_path(folder, profile.name)
    if final_path.exists() and not overwrite:
        raise FileExistsError(
            f"a profile already exists at {final_path}")

    # Build the JSON payload.  numpy float32 -> Python float for
    # json.dumps (json doesn't know about numpy scalars).
    payload = {
        "schema_version": SCHEMA_VERSION,
        "name": str(profile.name),
        "captured_at_iso": str(profile.captured_at_iso),
        "freq_hz": int(profile.freq_hz),
        "mode": str(profile.mode),
        "duration_sec": float(profile.duration_sec),
        "fft_size": int(profile.fft_size),
        "lyra_version": str(profile.lyra_version),
        "magnitudes": [float(x) for x in profile.magnitudes.tolist()],
    }

    # Atomic write: write to a sibling .tmp file in the same folder
    # (must be same filesystem for os.replace to be atomic), then
    # replace the final path.  tempfile.NamedTemporaryFile is fine
    # but we close-and-rename ourselves for cross-platform behavior.
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=final_path.stem + ".",
        suffix=".tmp",
        dir=str(folder),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't always available (some FUSE/ramdisk).
                # The os.replace will still be atomic; we just lose
                # the durability guarantee on a hard reboot.
                pass
        # os.replace is atomic on POSIX and on Windows since 3.3 —
        # it overwrites the destination if it exists, so this
        # works for both new-create and overwrite cases.
        os.replace(tmp_path, final_path)
    except Exception:
        # Clean up the tmp file if anything went wrong before the
        # replace landed.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return final_path


def delete_profile(folder: Path, name: str) -> bool:
    """Remove a profile from disk.  Returns True if a file was
    deleted, False if no profile with that name existed."""
    path = _profile_file_path(folder, name)
    if not path.is_file():
        return False
    path.unlink()
    return True


def rename_profile(folder: Path, old_name: str, new_name: str,
                   overwrite: bool = False) -> Path:
    """Rename a profile.  Updates both the on-disk filename AND
    the in-file ``name`` field.

    Args:
        folder: storage folder
        old_name: existing profile name
        new_name: new profile name
        overwrite: if False (default), raises FileExistsError if
                   a profile with the new name already exists

    Returns the new file path on success.  Raises FileNotFoundError
    if the source profile doesn't exist.
    """
    old_path = _profile_file_path(folder, old_name)
    new_path = _profile_file_path(folder, new_name)
    if not old_path.is_file():
        raise FileNotFoundError(
            f"profile {old_name!r} not found in {folder}")
    if old_path == new_path:
        # Sanitized filenames collide (e.g. renaming "Foo" to
        # "Foo "); just update the in-file display name.
        prof = load_profile(folder, old_name)
        prof.name = new_name
        return save_profile(folder, prof, overwrite=True)
    if new_path.exists() and not overwrite:
        raise FileExistsError(
            f"a profile already exists at {new_path}")
    # Load → mutate name → save under new path → delete old.
    prof = load_profile(folder, old_name)
    prof.name = new_name
    save_profile(folder, prof, overwrite=overwrite)
    old_path.unlink()
    return new_path


# ── Export / import (Phase 3.D #1, polish) ──────────────────────────────

def export_profile(folder: Path, name: str, dst_path: Path) -> Path:
    """Copy a profile to an arbitrary path on disk.  Used by the
    manager dialog's Export button — operator picks where to put
    a single-profile JSON for sharing or backup."""
    src_path = _profile_file_path(folder, name)
    if not src_path.is_file():
        raise FileNotFoundError(
            f"profile {name!r} not found in {folder}")
    dst_path = Path(dst_path)
    if dst_path.is_dir():
        dst_path = dst_path / src_path.name
    shutil.copy2(src_path, dst_path)
    return dst_path


def import_profile(src_path: Path, dst_folder: Path,
                   rename_to: Optional[str] = None,
                   overwrite: bool = False) -> str:
    """Bring a profile JSON into the storage folder.

    Validates the file against the current schema before copying.
    Returns the in-file ``name`` of the imported profile (which
    may differ from the source filename's stem).

    If a profile with the same name already exists and
    ``overwrite`` is False, raises FileExistsError — UI can
    re-prompt with a different name.
    """
    src_path = Path(src_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"source file not found: {src_path}")
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    schema = int(data.get("schema_version", 0))
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"profile at {src_path} has unsupported schema_version "
            f"{schema} (expected {SCHEMA_VERSION})")
    name = str(rename_to) if rename_to else str(data.get("name", src_path.stem))
    mags = np.asarray(data.get("magnitudes", []), dtype=np.float32)
    profile = NoiseProfile(
        name=name,
        captured_at_iso=str(data.get("captured_at_iso", "")),
        freq_hz=int(data.get("freq_hz", 0)),
        mode=str(data.get("mode", "")),
        duration_sec=float(data.get("duration_sec", 0.0)),
        fft_size=int(data.get("fft_size", 0)),
        lyra_version=str(data.get("lyra_version", "")),
        magnitudes=mags,
        schema_version=schema,
    )
    save_profile(dst_folder, profile, overwrite=overwrite)
    return name


# ── Convenience: make a NoiseProfile from a live NR + metadata ──────────

def make_profile_from_nr(name: str,
                         magnitudes: np.ndarray,
                         freq_hz: int,
                         mode: str,
                         duration_sec: float,
                         fft_size: int,
                         lyra_version: str,
                         captured_at: Optional[datetime] = None,
                         ) -> NoiseProfile:
    """Build a NoiseProfile from the values held by Radio + NR at
    capture time.  Caller stamps the ``captured_at`` timestamp; if
    omitted, ``datetime.now(tz=UTC)`` is used.

    Used by the Radio integration (Day 2.5) to package up a fresh
    capture for save_profile().
    """
    if captured_at is None:
        captured_at = datetime.now(tz=timezone.utc)
    iso = captured_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return NoiseProfile(
        name=str(name),
        captured_at_iso=iso,
        freq_hz=int(freq_hz),
        mode=str(mode),
        duration_sec=float(duration_sec),
        fft_size=int(fft_size),
        lyra_version=str(lyra_version),
        magnitudes=np.asarray(magnitudes, dtype=np.float32).copy(),
    )
