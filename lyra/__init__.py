"""Lyra — Qt6 SDR transceiver for Hermes Lite 2 / 2+.

Version policy
--------------
Pre-1.0 releases use 0.<minor>.<patch> where:
  - <minor> bumps for user-facing feature batches
  - <patch> bumps for bug-fix-only releases between feature batches

The single source of truth is `__version__` below. UI surfaces (title
bar, About dialog, status bar, settings dialog footer) and the install
guide all read from this string — bump it once here per release and
everything else follows automatically.
"""
__version__      = "0.0.3"
__version_name__ = "First Tester Build"
# Stamped at release time to today's date when packaging the
# Inno Setup installer. During raw-source-tree development this stays
# "dev"; the packaging script rewrites this constant before the
# PyInstaller bundle is frozen, so the About dialog + status bar in
# the released .exe show the date the operator received it.
__build_date__   = "2026-04-25"


def version_string() -> str:
    """Human-readable version string for UI / log display.

    Format:
        '0.0.3 — First Tester Build (dev build)'    (development tree)
        '0.0.3 — First Tester Build  (2026-04-25)'  (released installer)
    """
    base = f"{__version__} — {__version_name__}"
    if __build_date__ == "dev":
        return f"{base} (dev build)"
    return f"{base}  ({__build_date__})"
