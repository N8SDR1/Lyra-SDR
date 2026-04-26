# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Lyra-SDR.

Build with:
    pyinstaller --noconfirm build/lyra.spec

Output:
    dist/Lyra/Lyra.exe          ← the executable
    dist/Lyra/...               ← bundled libs + assets
    (folder-mode bundle, NOT one-file — operators can browse the
     install dir, see the help markdown files, copy/edit them; one-
     file mode hides everything inside a temp self-extract which
     makes troubleshooting harder for testers)

Build requirements:
    Windows + Python 3.11+ + pyinstaller>=6.0
    All Lyra runtime requirements satisfied (PySide6, NumPy, SciPy,
    sounddevice, websockets, psutil, nvidia-ml-py, pywin32)
"""
from pathlib import Path

# Project root = parent of the build/ directory holding this spec.
# PyInstaller runs the spec with __file__ pointing to a temp copy,
# so we use SPECPATH (set by PyInstaller) instead.
PROJECT_ROOT = Path(SPECPATH).resolve().parent

# ── Datas: extra files that need to ship alongside the .exe ───────────
datas = [
    # Help-guide markdown files. Read live by HelpDialog at runtime.
    (str(PROJECT_ROOT / "docs" / "help"), "docs/help"),
    # App icons + logo PNG. Used by the title bar / About / startup.
    (str(PROJECT_ROOT / "assets" / "logo"), "assets/logo"),
    # DXCC country prefix database used by TCI spot enrichment.
    # Lazily loaded; if missing, country flags just don't render —
    # but we ship it because operators expect spot flags to work.
]
# Optional: bundle data/cty.dat for DXCC if it exists.
_cty = PROJECT_ROOT / "data" / "cty.dat"
if _cty.exists():
    datas.append((str(_cty), "data"))

# ── Hidden imports — modules PyInstaller's static analysis misses ─────
# Most are picked up automatically from the import graph; these are
# the ones it tends to lose because they're loaded via getattr / late
# import / are platform-conditional.
hiddenimports = [
    # Optional GPU / system instrumentation — guarded with try/except
    # at the call site, but PyInstaller may not see them since the
    # import is inside the try block.
    "psutil",
    "pynvml",
    "win32pdh",
    # PySide6 bits that some hosts miss when scanning Qt plugins
    "PySide6.QtNetwork",
    "PySide6.QtWebSockets",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
]

# ── Excludes: modules we DON'T need bundled (shrinks the install) ─────
excludes = [
    # Test infra — operators don't need pytest in the bundle.
    "pytest", "pytest_cov", "_pytest",
    # Heavy alternative GUI toolkits we don't use.
    "tkinter", "PyQt5", "PyQt6", "PySide2",
    # ML/data libs that some upstream deps pull transitively but we
    # never reference (saves hundreds of MB if they were installed).
    "matplotlib", "pandas", "IPython", "jupyter", "notebook",
    "torch", "tensorflow",
]

block_cipher = None


a = Analysis(
    [str(PROJECT_ROOT / "lyra" / "ui" / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Lyra",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                           # UPX often confuses AV scanners
    console=False,                       # ← no console window (--windowed)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "logo" / "lyra.ico"),
    version=None,                        # could add a Windows version
                                          # info block; not required
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Lyra",
)
