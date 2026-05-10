"""Captured-noise-profile persistence layer.

Stores the operator's captured noise profiles as one JSON file per
profile in a configurable folder.  Default folder is the OS-standard
user-data location (``%APPDATA%\\Lyra`` on Windows,
``~/Library/Application Support/Lyra`` on macOS,
``~/.local/share/Lyra`` on Linux); operators can override via the
Settings → Noise tab.

**Schema history**

* **v1 (deprecated, refused on load)** — audio-domain captured
  profiles.  Stored 129 floats (``fft_size//2 + 1``) representing
  half-spectrum magnitudes from 48 kHz mono audio post-WDSP.  This
  format only worked on the pure-Python DSP path that was retired
  in v0.0.9.6's "Audio Foundation" cleanup arc (see CLAUDE.md
  §14.9 for the deletion record).  v1 files on disk load far
  enough to surface metadata in the manager dialog, but
  :func:`load_profile` raises ``ValueError`` with a clear
  recapture message — there is no consumer for the audio-domain
  magnitudes in v0.0.9.9+.

* **v2 (current, this commit, §14.6)** — IQ-domain captured
  profiles.  Stored ``fft_size`` floats (full complex-FFT
  magnitude spectrum, NOT the half-spectrum) at the operator's
  IQ rate.  Captured pre-WDSP so it sidesteps the AGC-mismatch
  that broke three rounds of post-WDSP audio-domain attempts in
  v0.0.9.6.  Mode-independent (same profile applies to
  USB/LSB/CW/AM/FM since baseband noise pattern is the same
  regardless of demod choice).  Rate-specific (192k vs 96k vs
  48k all need their own profile, since baseband bin structure
  differs).

Schema version 2 (this commit):

.. code-block:: json

    {
        "schema_version": 2,
        "domain": "iq",
        "name": "Powerline 80m",
        "captured_at_iso": "2026-05-10T14:22:13Z",
        "freq_hz": 3825000,
        "mode": "LSB",
        "duration_sec": 2.0,
        "rate_hz": 192000,
        "fft_size": 2048,
        "lyra_version": "0.0.9.9",
        "magnitudes": [/* 2048 floats — full complex FFT magnitude */]
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

Compatibility: profiles store ``fft_size`` AND ``rate_hz`` so a
mismatch with runtime IQ rate is caught at load time.
``is_compatible(fft_size, rate_hz)`` checks BOTH against the
current capture config; the manager dialog uses this to grey-out
incompatible profiles.  Cross-rate interpolation is deliberately
NOT supported — bin structure differs across IQ rates and lossy
conversion would give plausible-but-wrong subtraction.

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

SCHEMA_VERSION: int = 2
"""On-disk JSON schema version.  Bump if the format changes in a
backward-incompatible way; loaders should reject unknown versions.

* v1 = audio-domain (deprecated, refused on load — see module
  docstring for the migration story).
* v2 = IQ-domain (current).
"""

DOMAIN_IQ: str = "iq"
"""Discriminator value for the ``domain`` field on v2 profiles.

Only one valid value today.  Forward-defensive so a future v3
schema with, say, a hybrid IQ+audio domain can be distinguished
without another schema bump just for the discriminator."""

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
    rate_hz: int
    """IQ rate (Hz) at capture time.  The profile's bin structure
    is rate-specific; :meth:`is_compatible` rejects it at runtime
    if the current radio IQ rate doesn't match.

    Surfaces as 0 for list rows that came from a v1 file (legacy
    audio-domain had no IQ rate to record); :func:`list_profiles`
    fills 0 in via ``int(data.get("rate_hz", 0))`` when scanning a
    v1 JSON.  v1 rows are also flagged unloadable by
    :meth:`is_loadable`, so the 0 sentinel never reaches the
    apply path."""
    domain: str
    """Profile domain discriminator.  Always ``"iq"`` for v2
    profiles.

    Surfaces as ``""`` for list rows that came from a v1 file
    (legacy audio-domain — no domain field was stored); same
    list-scan fallback path as ``rate_hz`` above."""
    lyra_version: str
    schema_version: int = SCHEMA_VERSION
    file_path: Optional[Path] = None
    """Absolute path to the JSON file on disk (filled in by
    :func:`list_profiles`).  Useful for the management UI."""

    def is_loadable(self) -> bool:
        """True if this profile can possibly be loaded into the
        current Lyra build — schema is current AND domain is
        IQ-domain.

        Used by the manager dialog to grey-out v1 profiles (which
        :func:`load_profile` refuses with a recapture hint).  Does
        NOT validate FFT size or IQ rate — that check belongs at
        apply time, where mismatch is caught by
        :meth:`is_compatible`.

        Splitting "loadable at all?" from "compatible with current
        capture config?" matters because operators may have v2
        profiles captured at, say, 96 kHz that they want to keep
        on disk for use later when they switch the radio to that
        rate; those should NOT be greyed out at 192 kHz, just
        flagged as needing-rate-switch.
        """
        return (self.schema_version == SCHEMA_VERSION
                and self.domain == DOMAIN_IQ)

    def is_compatible(self, current_fft_size: int,
                      current_rate_hz: int) -> bool:
        """True if this profile is usable with the current runtime
        capture config — :meth:`is_loadable` AND same FFT size AND
        same IQ rate.

        Apply-time check — used by the captured-profile applier
        (Phase 4 of §14.6) to refuse mismatched profiles rather
        than producing plausible-but-wrong subtraction.  Cross-
        rate interpolation is deliberately not supported; bin
        structure differs across IQ rates.

        The manager dialog uses :meth:`is_loadable` for grey-out
        instead of this stricter check.
        """
        return (self.is_loadable()
                and self.fft_size == current_fft_size
                and self.rate_hz == current_rate_hz)

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
    """Full profile record — metadata + the magnitudes array.

    For v2 IQ-domain profiles, ``magnitudes`` is the full complex
    FFT magnitude spectrum (length ``fft_size``, NOT
    ``fft_size//2 + 1`` — IQ has independent positive AND
    negative baseband content so both halves matter).  Values are
    float32 averages of ``|FFT(iq_block)|`` accumulated over the
    capture window.
    """
    name: str
    captured_at_iso: str
    freq_hz: int
    mode: str
    duration_sec: float
    fft_size: int
    rate_hz: int
    """IQ rate (Hz) at capture time."""
    domain: str
    """Always ``DOMAIN_IQ`` for v2 profiles."""
    lyra_version: str
    magnitudes: np.ndarray  # float32, length fft_size (full complex spectrum)
    schema_version: int = SCHEMA_VERSION

    def to_meta(self, file_path: Optional[Path] = None) -> ProfileMeta:
        return ProfileMeta(
            name=self.name,
            captured_at_iso=self.captured_at_iso,
            freq_hz=self.freq_hz,
            mode=self.mode,
            duration_sec=self.duration_sec,
            fft_size=self.fft_size,
            rate_hz=self.rate_hz,
            domain=self.domain,
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
            # rate_hz + domain are v2 fields — v1 files won't have
            # them.  Default to 0 / "" so v1 metas are visible in
            # the manager dialog (where is_loadable() will mark
            # them strikethrough + tooltip "recapture in 0.0.9.9+")
            # without crashing the scan.
            metas.append(ProfileMeta(
                name=str(data.get("name", path.stem)),
                captured_at_iso=str(data.get("captured_at_iso", "")),
                freq_hz=int(data.get("freq_hz", 0)),
                mode=str(data.get("mode", "")),
                duration_sec=float(data.get("duration_sec", 0.0)),
                fft_size=int(data.get("fft_size", 0)),
                rate_hz=int(data.get("rate_hz", 0)),
                domain=str(data.get("domain", "")),
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
    profile doesn't exist, ValueError on parse / schema problems.

    v1 profiles (audio-domain, pre-WDSP era) are deliberately
    refused with a clear recapture message — the audio-domain
    apply path was retired in the v0.0.9.6 cleanup arc and there
    is no consumer for v1 magnitudes in v0.0.9.9+.  See module
    docstring for migration history.
    """
    path = _profile_file_path(folder, name)
    if not path.is_file():
        raise FileNotFoundError(
            f"noise profile {name!r} not found in {folder}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    schema = int(data.get("schema_version", 0))
    if schema == 1:
        # Legacy audio-domain profile — no consumer in WDSP-only
        # builds.  Surface a recapture hint rather than a generic
        # "unsupported schema" error so the operator knows what
        # to do next.
        raise ValueError(
            f"profile {name!r} uses the legacy v1 audio-domain "
            f"format (pre-v0.0.9.6).  The audio-domain apply path "
            f"was retired with the WDSP audio engine; please "
            f"recapture this profile in v0.0.9.9 or later to use "
            f"the new IQ-domain engine.")
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"profile {name!r} has unsupported schema_version "
            f"{schema} (expected {SCHEMA_VERSION})")
    domain = str(data.get("domain", ""))
    if domain != DOMAIN_IQ:
        raise ValueError(
            f"profile {name!r} has unsupported domain "
            f"{domain!r} (expected {DOMAIN_IQ!r})")
    mags_list = data.get("magnitudes", None)
    if not isinstance(mags_list, list):
        raise ValueError(
            f"profile {name!r} is missing or has invalid "
            f"'magnitudes' array")
    mags = np.asarray(mags_list, dtype=np.float32)
    fft_size = int(data.get("fft_size", 0))
    if fft_size > 0 and mags.size != fft_size:
        # Bin-count sanity check — full complex IQ spectrum is
        # ``fft_size`` long, NOT the half-spectrum that v1 used.
        # If a hand-edited file has 129 floats but claims
        # fft_size=2048, refuse rather than silently mis-aligning.
        raise ValueError(
            f"profile {name!r} has {mags.size} magnitude bins "
            f"but fft_size={fft_size} (expected exactly "
            f"{fft_size} bins for a v2 IQ-domain profile)")
    return NoiseProfile(
        name=str(data.get("name", name)),
        captured_at_iso=str(data.get("captured_at_iso", "")),
        freq_hz=int(data.get("freq_hz", 0)),
        mode=str(data.get("mode", "")),
        duration_sec=float(data.get("duration_sec", 0.0)),
        fft_size=fft_size,
        rate_hz=int(data.get("rate_hz", 0)),
        domain=domain,
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
        "domain": str(profile.domain or DOMAIN_IQ),
        "name": str(profile.name),
        "captured_at_iso": str(profile.captured_at_iso),
        "freq_hz": int(profile.freq_hz),
        "mode": str(profile.mode),
        "duration_sec": float(profile.duration_sec),
        "rate_hz": int(profile.rate_hz),
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
    if schema == 1:
        # Same hint as load_profile — operator may have an old
        # backup JSON they're trying to import.  Refuse rather
        # than silently ingest a profile that has no consumer.
        raise ValueError(
            f"profile at {src_path} uses the legacy v1 audio-"
            f"domain format (pre-v0.0.9.6).  Recapture in "
            f"v0.0.9.9+ to use the new IQ-domain engine.")
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"profile at {src_path} has unsupported schema_version "
            f"{schema} (expected {SCHEMA_VERSION})")
    domain = str(data.get("domain", ""))
    if domain != DOMAIN_IQ:
        raise ValueError(
            f"profile at {src_path} has unsupported domain "
            f"{domain!r} (expected {DOMAIN_IQ!r})")
    name = str(rename_to) if rename_to else str(data.get("name", src_path.stem))
    mags = np.asarray(data.get("magnitudes", []), dtype=np.float32)
    profile = NoiseProfile(
        name=name,
        captured_at_iso=str(data.get("captured_at_iso", "")),
        freq_hz=int(data.get("freq_hz", 0)),
        mode=str(data.get("mode", "")),
        duration_sec=float(data.get("duration_sec", 0.0)),
        fft_size=int(data.get("fft_size", 0)),
        rate_hz=int(data.get("rate_hz", 0)),
        domain=domain,
        lyra_version=str(data.get("lyra_version", "")),
        magnitudes=mags,
        schema_version=schema,
    )
    save_profile(dst_folder, profile, overwrite=overwrite)
    return name


# ── Convenience: make a NoiseProfile from a live capture + metadata ─────

def make_profile_from_capture(name: str,
                              magnitudes: np.ndarray,
                              freq_hz: int,
                              mode: str,
                              duration_sec: float,
                              fft_size: int,
                              rate_hz: int,
                              lyra_version: str,
                              captured_at: Optional[datetime] = None,
                              domain: str = DOMAIN_IQ,
                              ) -> NoiseProfile:
    """Build a v2 IQ-domain :class:`NoiseProfile` from the values
    held by ``Radio`` + the IQ capture accumulator at capture time.

    Caller stamps the ``captured_at`` timestamp; if omitted,
    ``datetime.now(tz=UTC)`` is used.

    Args:
        name: operator-typed display name
        magnitudes: float32 array, length ``fft_size`` (full
                    complex FFT magnitude spectrum from the IQ
                    capture accumulator)
        freq_hz: tuned frequency at capture time (informational —
                 IQ profiles are mode-independent so the freq
                 metadata is for operator reference only)
        mode: tuned demod mode at capture time (informational,
              same reasoning as ``freq_hz``)
        duration_sec: actual capture window length in seconds
        fft_size: FFT size used for the capture (must match
                  ``len(magnitudes)``)
        rate_hz: IQ rate at capture time — required, since
                 v2 profiles are rate-specific
        lyra_version: Lyra ``__version__`` string at capture time
        captured_at: UTC timestamp; default ``datetime.now(UTC)``
        domain: discriminator — defaults to ``DOMAIN_IQ`` and that
                is the only currently-valid value.  Argument exists
                so a future hybrid-domain v3 schema can pass a
                different value without breaking the signature.

    Used by the Radio integration to package up a fresh capture
    for ``save_profile()``.
    """
    if captured_at is None:
        captured_at = datetime.now(tz=timezone.utc)
    iso = captured_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    mags = np.asarray(magnitudes, dtype=np.float32).copy()
    if int(fft_size) > 0 and mags.size != int(fft_size):
        raise ValueError(
            f"magnitudes array length {mags.size} does not match "
            f"fft_size {fft_size} (expected exactly {fft_size} "
            f"bins for a v2 IQ-domain profile)")
    return NoiseProfile(
        name=str(name),
        captured_at_iso=iso,
        freq_hz=int(freq_hz),
        mode=str(mode),
        duration_sec=float(duration_sec),
        fft_size=int(fft_size),
        rate_hz=int(rate_hz),
        domain=str(domain),
        lyra_version=str(lyra_version),
        magnitudes=mags,
    )
