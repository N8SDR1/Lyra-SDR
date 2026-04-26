# Lyra-SDR Build Pipeline

PyInstaller + Inno Setup → standalone Windows installer.

## What's in this directory

| File | Role |
|---|---|
| `lyra.spec` | PyInstaller spec — defines what gets bundled into the .exe |
| `installer.iss` | Inno Setup script — wraps the PyInstaller bundle into an installer |
| `build.cmd` | One-button rebuild script (calls the above two in order) |

## Prerequisites (one-time setup)

1. **Python 3.11+** on PATH
2. **PyInstaller ≥ 6.0**: `pip install pyinstaller`
3. **All Lyra runtime requirements**: `pip install -r requirements.txt`
   from the project root
4. **Inno Setup 6** installed at `C:\Program Files (x86)\Inno Setup 6\`
   (download from [jrsoftware.org/isdl.php](https://jrsoftware.org/isdl.php))

## How to build

```cmd
cd Y:\Claude local\SDRProject
build\build.cmd
```

Output:
- `dist\Lyra\Lyra.exe` — the executable + bundled Python runtime + libs
- `dist\Lyra\` — folder-mode bundle (browseable, includes docs/help/*.md
  so operators can edit help pages and hit Reload in the User Guide)
- `dist\installer\Lyra-Setup-X.Y.Z.exe` — the installer for testers

## Cutting a new release

1. **Edit `lyra/__init__.py`** — bump:
   - `__version__` (e.g. `"0.0.3"` → `"0.0.4"`)
   - `__version_name__` (rename to fit the release theme, e.g. `"TX Phase 1"`)
   - `__build_date__` from `"dev"` (during development) to today's
     `"YYYY-MM-DD"` for the release commit
2. **Edit `build/installer.iss`** — bump `LyraVersion` and
   `LyraVersionName` to match the same values above
3. **`git commit -am "Bump to vX.Y.Z"` + `git tag vX.Y.Z`**
4. **Run `build\build.cmd`** to produce the installer
5. **Test the installer** on a clean VM (or at least a different
   Windows account) to make sure it installs / runs / uninstalls
   cleanly
6. **Push the tag**: `git push origin vX.Y.Z`
7. **Create a GitHub Release** for the tag, attach the installer
   .exe to the release, write release notes
8. **Reset `__build_date__` back to `"dev"`** in `lyra/__init__.py`
   for ongoing development (avoids accidentally shipping a stale
   date-stamped build)

## Why folder-mode and not one-file?

PyInstaller's `--onefile` packs everything into a single .exe that
self-extracts to a temp directory at every launch. Pros: single file
to distribute. Cons:

- Slow startup (extraction takes seconds)
- AV scanners frequently flag the bootloader as suspicious
- Operators can't see / edit the bundled help markdown files
- Crash diagnosis is harder (no real disk paths to look at)

Folder mode trades "single .exe" for these wins. The Inno Setup
installer makes "single distributable file" a non-issue anyway —
operators get one `Lyra-Setup-X.Y.Z.exe` to download.

## Why no UPX / signed executables?

- **UPX**: compresses the .exe but consistently triggers false
  positives on Windows Defender / Norton / McAfee. Not worth the
  support burden for marginal size savings.
- **Code signing**: requires a paid certificate from a recognized
  CA ($200-500/year). Until Lyra has revenue or a community-funded
  cert, we ship unsigned. Operators may see Windows SmartScreen
  warnings on first launch — they click "More info → Run anyway."
  The README + install guide should mention this so testers aren't
  surprised.

## Troubleshooting build failures

**"ModuleNotFoundError" at runtime** — PyInstaller's static analyzer
missed a hidden import. Add the module to the `hiddenimports` list
in `lyra.spec` and rebuild.

**"FileNotFoundError" at runtime for docs/help/*.md or assets/** —
Check that the missing path is in the `datas` list in `lyra.spec`.
The path is the source location → bundle subdirectory tuple.

**Installer can't find `dist\Lyra`** — PyInstaller failed silently;
re-run `pyinstaller --noconfirm --clean build\lyra.spec` directly
and read the full output.

**"file in use" during install** — Lyra is already running. Close
it first.
