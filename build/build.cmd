@echo off
rem One-button rebuild of the Lyra-SDR Windows installer.
rem
rem Usage:
rem   cd Y:\Claude local\SDRProject
rem   build\build.cmd
rem
rem Output:
rem   dist\Lyra\Lyra.exe                 (the executable + bundled libs)
rem   dist\installer\Lyra-Setup-X.Y.Z.exe (the operator-facing installer)
rem
rem Prerequisites (one-time setup):
rem   - Python 3.11+ on PATH (via `py launcher` or python.exe)
rem   - pip install pyinstaller (>= 6.0)
rem   - All Lyra runtime requirements installed (pip install -r
rem     requirements.txt)
rem   - Inno Setup 6 installed at the default location
rem     (C:\Program Files (x86)\Inno Setup 6\ISCC.exe)
rem
rem Before bumping the version on a release:
rem   1. Edit lyra\__init__.py — bump __version__, __version_name__,
rem      and __build_date__ (from "dev" to today's YYYY-MM-DD).
rem   2. Edit build\installer.iss — bump LyraVersion to match.
rem   3. git commit + git tag the release.
rem   4. THEN run this script to produce the installer.

setlocal
cd /d "%~dp0\.."

echo === Step 1/2: PyInstaller ============================
pyinstaller --noconfirm --clean build\lyra.spec
if errorlevel 1 (
    echo PyInstaller failed; aborting.
    exit /b 1
)

echo.
echo === Step 2/2: Inno Setup =============================
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
if errorlevel 1 (
    echo Inno Setup failed; aborting.
    exit /b 1
)

echo.
echo === Build complete ===================================
echo   Executable: dist\Lyra\Lyra.exe
echo   Installer:  dist\installer\Lyra-Setup-*.exe
endlocal
