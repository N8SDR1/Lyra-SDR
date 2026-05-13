# Lyra-SDR — Install Guide for Windows

A Qt6/PySide6 desktop SDR transceiver for the Hermes Lite 2 / 2+.

> **Prefer a printable version?** A formatted Word document is in
> [`docs/Lyra-SDR-Install-Guide.docx`](docs/Lyra-SDR-Install-Guide.docx)
> — same content, easier to print or share offline.

This guide is written for *"I have Windows and I sort of know what a
terminal is"* — not for Python developers. If you can copy and paste,
you can install Lyra.

---

## Prerequisites (one-time setup)

### 1. Install Python 3.11 or newer

- Download from <https://www.python.org/downloads/>
- Run the installer
- ⚠️ **CRITICAL:** Check the box **"Add python.exe to PATH"** before
  clicking Install. If you miss this, every command below will fail.
  You can re-run the installer if needed.
- Verify: open Command Prompt (press Windows key, type `cmd`, Enter)
  and run:

  ```
  python --version
  ```

  You should see something like `Python 3.11.x` or higher.

### 2. Install Git for Windows

- Download from <https://git-scm.com/download/win>
- Run the installer with all defaults
- Verify in Command Prompt:

  ```
  git --version
  ```

  Should print a version.

You can skip Git if you'd rather download a zip file — see Option B
in the next section.

---

## Get the code (one-time)

### Option A — with Git (recommended, easier to update later)

In Command Prompt:

```
cd %USERPROFILE%\Documents
git clone https://github.com/N8SDR1/Lyra-SDR.git
cd Lyra-SDR
```

This drops the project at `C:\Users\<you>\Documents\Lyra-SDR\`.

### Option B — without Git (zip download)

1. Visit <https://github.com/N8SDR1/Lyra-SDR>
2. Click the green **`<> Code`** button → **Download ZIP**
3. Unzip to `C:\Users\<you>\Documents\Lyra-SDR\`
4. Open Command Prompt and `cd` into that folder

---

## Install Python dependencies (one-time)

In the `Lyra-SDR` folder, the easy way:

```
pip install -r requirements.txt
```

Pip downloads about 150 MB of libraries. Takes a minute or two.

If you hit "permission denied" errors:

```
pip install --user -r requirements.txt
```

If `ftd2xx` specifically fails (no FTDI driver on your machine), it's
optional — only needed for USB-BCD external linear-amp control. To
install everything else and skip it:

```
pip install PySide6 numpy scipy sounddevice websockets
```

---

## Run Lyra

```
python -m lyra.ui.app
```

The Lyra window opens. From there:

1. Make sure your HL2 / HL2+ is powered on and on the same network
   as your PC.
2. Click the **▶ Start** button on the toolbar — discovery should
   find the radio automatically.
3. If discovery fails, use **File → Network/TCI…** to set the radio's
   IP manually.

Press **F1** inside Lyra for the in-app User Guide covering
operating, AGC, notch filters, the spectrum/waterfall display, TCI
integration, and more.

---

## Updating later (Git users only)

When new commits land in the repo:

```
cd %USERPROFILE%\Documents\Lyra-SDR
git pull
pip install -r requirements.txt
```

The second `pip install` only matters when dependencies change — it's
a no-op otherwise, so it's safe to always run after a pull.

---

## Common gotchas

| Symptom | Fix |
|---|---|
| `'python' is not recognized` | Python wasn't added to PATH. Reinstall and check that box. |
| `'pip' is not recognized` | Same — reinstall Python with PATH option. |
| `ModuleNotFoundError: No module named 'PySide6'` | You skipped the pip install step. Run it now. |
| `ftd2xx` fails to install | Skip it — install the other deps without `ftd2xx`. |
| Windows firewall popup on first launch | Allow it. Lyra needs UDP to talk to the HL2. |
| `No radio found` | Check HL2 power, network cable, and that no other client (Thetis, SparkSDR) is connected at the same time. |
| Audio works in other apps but not Lyra | Switch the **Out** combo on the DSP + Audio panel between **HL2 audio jack** and **PC Soundcard**. Most operators use PC Soundcard. |

---

## Tester feedback

When you run into something — a bug, a confusing UI, a missing
feature — please open an Issue on the repo:

<https://github.com/N8SDR1/Lyra-SDR/issues>

Include what you tried, what happened, what you expected, and the
contents of the Command Prompt window if there was a Python error.
Screenshots help.

73 from N8SDR.
