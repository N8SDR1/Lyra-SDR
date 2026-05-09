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
    # GPU panadapter shaders (GLSL .vert / .frag). Loaded at runtime
    # by spectrum_gpu.py via Path(__file__) / "spectrum_gpu_shaders".
    # Without this, the frozen .exe initializes the GL programs with
    # empty shader source — rendering becomes garbage (washed-out
    # trace, blank waterfall) while the QPainter overlays continue
    # to work. Bundled into lyra/ui/spectrum_gpu_shaders/ so the
    # runtime path matches the dev-tree path.
    (str(PROJECT_ROOT / "lyra" / "ui" / "spectrum_gpu_shaders"),
     "lyra/ui/spectrum_gpu_shaders"),
    # Lyra constellation watermark (JPG). Loaded at runtime by
    # lyra/ui/constellation.py via Path(__file__).parent.parent /
    # "assets" / "watermarks" / "lyra-watermark.jpg".  Without this,
    # the constellation/lyre overlay silently no-ops in the
    # installer because _load_source() returns None on missing
    # asset (meteors keep working — they're pure code).  Operator-
    # reported missing in v0.0.9.3 installer; fixed in v0.0.9.4.
    (str(PROJECT_ROOT / "lyra" / "assets" / "watermarks"),
     "lyra/assets/watermarks"),
    # DXCC country prefix database used by TCI spot enrichment.
    # Lazily loaded; if missing, country flags just don't render —
    # but we ship it because operators expect spot flags to work.
    #
    # WDSP DSP engine native DLLs — wdsp.dll + libfftw3 (single +
    # double precision) + rnnoise + specbleach.  ~16 MB total.
    # CRITICAL for v0.0.9.6+ since the entire RX1 audio chain runs
    # through these via cffi.  Without them, the loader's fallback
    # path expects either an LYRA_WDSP_DIR env var or a Thetis-HL2
    # install — neither of which a typical end-user has — and Lyra
    # refuses to open the audio engine.  Source layout:
    # ``lyra/dsp/_native/{wdsp,libfftw3-3,libfftw3f-3,rnnoise,
    # specbleach}.dll``.  Runtime layout (after PyInstaller bundles):
    # ``_internal/lyra/dsp/_native/...`` so wdsp_native.py's
    # ``Path(__file__).resolve().parent / "_native"`` resolution
    # finds them.
    (str(PROJECT_ROOT / "lyra" / "dsp" / "_native"),
     "lyra/dsp/_native"),
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
