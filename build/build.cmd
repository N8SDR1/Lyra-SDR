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
rem Full release sequence (do steps 1-5 BEFORE running this script,
rem and steps 7-9 AFTER):
rem   1. Edit lyra\__init__.py — bump __version__, __version_name__,
rem      and __build_date__ (from "dev" to today's YYYY-MM-DD).
rem   2. Edit build\installer.iss — bump LyraVersion to match.
rem   3. Update CHANGELOG.md with the dated release entry.
rem   4. git commit the version bump.
rem   5. git tag -a v0.0.X -m "..."
rem   6. Run this script (PyInstaller + Inno Setup).
rem   7. git push origin <feature-branch>
rem   8. git push origin v0.0.X
rem   9. git push origin <feature-branch>:main   <-- DON'T SKIP
rem  10. Create GitHub Release with the installer attached.
rem
rem See CLAUDE.md section 11 "Releases" for the full reasoning,
rem including the v0.0.9.6 - v0.0.9.9 drift incident that
rem prompted explicit step 9.

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
echo.
echo === Post-build release checklist =====================
echo   If this is a release build, complete these steps:
echo.
echo     git push origin ^<feature-branch^>
echo     git push origin v0.0.X
echo     git push origin ^<feature-branch^>:main   ^<-- DON'T SKIP
echo.
echo   Then on GitHub:
echo     1. Create a new release from tag v0.0.X
echo     2. Attach dist\installer\Lyra-Setup-*.exe
echo     3. Paste release notes and publish
echo.
echo   The "push to main" step is the one that has been missed
echo   in past releases; without it, anyone tracking main pulls
echo   stale code while feature branches ship.  See CLAUDE.md
echo   section 11 "Releases" for the full reasoning.
echo ======================================================
endlocal
