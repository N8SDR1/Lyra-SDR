# Getting Started

## 1. Know your hardware

- **HL2** (plain) — stock Hermes Lite 2 board. RX audio is decoded on
  the PC and played through the PC sound system (default audio device
  or whatever you pick in **DSP & AUDIO → Output**).
- **HL2+** — HL2 base **plus** the AK4951 audio add-in board. RX audio
  can be routed via EP2 → AK4951 → phones/line jack → PC line-in for
  lower-latency hardware monitoring. TX uses the AK4951 microphone
  input. Requires the updated HL2+ gateware.

## 2. Network

The HL2 is a Layer-2 Ethernet device using the HPSDR Protocol-1 on UDP
port 1024. It must be reachable on your local subnet. Typical setups:

- **Direct Ethernet** to the PC — simplest, no switch needed.
- **Same LAN as PC** — any gigabit switch is fine.
- **Across routers** — not supported. P1 discovery is broadcast-only.

Make sure Windows Firewall allows inbound UDP 1024 for `python.exe`
(or whatever you've packaged Lyra as).

## 3. First launch

1. Toolbar → **⚙ Settings…** → **Radio** tab.
2. Click **Discover** — any HL2 on the subnet will appear. Pick yours
   or paste the IP manually.
3. **Network/TCI** tab — default TCI port is 40001. Leave TCI disabled
   for now unless you have logging software to connect.
4. **Hardware** tab — enable the N2ADR filter board if you have one
   (and only if you do; otherwise the OC outputs drive nothing and
   it's harmless but unnecessary).
5. **Audio** tab — pick your output device, or leave as **Default**.
6. Close Settings.

## 4. Fire it up

- Toolbar → **▶ Start**. Status dot goes green.
- You should see a spectrum trace and hear a noise floor.
- If you don't: see **Troubleshooting**.

## 5. The top toolbar at a glance

Reading left to right, the always-visible toolbar shows:

| Section | What it is |
|---|---|
| **▶ Start** | One-click stream start/stop |
| **● Streaming** | Connection status dot (gray / yellow / green) |
| **● TCI ready** | TCI server status — click to open Network settings |
| **⚙ Settings…** | Tabbed settings dialog |
| **Reset Panel Layout** | Restore the factory panel arrangement |
| **Tuning / Mode+Filter / View / Band / Meters / DSP+Audio** | Show / hide each docked panel |
| **ADC pk / rms** | Live RX-chain headroom (color-coded for clip risk) |
| **HH:MM:SS  HH:MM:SSZ** | Local + UTC clocks (large, always visible) |
| **HL2  T xx.x°C   V xx.x V** | Live HL2 hardware telemetry |
| **CPU x.x%** | Lyra process CPU load (matches Task Manager) |
| **GPU x.x%** | System-wide GPU load |

The HL2 telemetry pair takes a few EP6 frames to populate after
**Start** because the radio rotates which register it reports each
frame. If voltage stays at `n/a`, your HL2 firmware variant doesn't
populate that telemetry slot — open **Help → HL2 Telemetry Probe…**
to see what your specific firmware sends.

The CPU/GPU readouts are color-coded green / yellow / orange / red
so you can glance at the toolbar and see whether something is
hammering your machine.

## 6. Save your workspace

The panel layout (which panels are visible, where they dock, floating
window positions) is saved automatically on close and restored on next
launch.

**View → Reset Panel Layout** restores the factory arrangement if you
end up with panels somewhere weird.

**View → Save current layout as my default** captures wherever you've
arranged things now and uses it as the new factory default — so
"Reset Panel Layout" goes back to *your* preferred layout instead of
the original one.

## 7. About this build

The version you're running is shown in three places:

- The window title bar (`Lyra v{{ version }} — Hermes Lite 2+ SDR Transceiver`)
- A permanent label on the right side of the status bar
- **Help → About Lyra…**

When filing a bug report, please include the version string from any
of those — it lets the maintainer match your report to the exact
code that produced it.

## 8. Staying current — update notifications

Lyra checks the GitHub repo at startup to see if there's a newer
release. The check is silent (no telemetry, no account, no data
sent — just a single anonymous GET to the public releases API; see
**License & privacy**) and the result drives a three-tier
notification flow so you can't miss an important update but you're
also not nagged.

### What you'll see when an update is available

**The first time** Lyra detects a new version on startup:

- A modal dialog opens: **"Lyra v0.X.Y is available"** with the
  release notes and three buttons.
- The toolbar grows a glowing **🆕 v0.X.Y available** indicator
  (between the clocks and the HL2 telemetry block) that pulses
  for ~5 seconds to draw the eye.
- A status-bar toast also appears for 12 seconds.

**On subsequent launches** with the same unseen version:

- No modal (you've already been told once).
- Toolbar indicator stays visible until you upgrade or skip.
- Status-bar toast still appears briefly.

### The three modal buttons

| Button | Effect |
|---|---|
| **Open release page** | Launches your browser at the GitHub release page. The toolbar indicator stays visible until you actually install the new build (so you don't forget). |
| **Remind me later** | Closes the modal. Toolbar indicator + toast still appear on this launch and future launches; the modal won't re-pop for this same version. |
| **Skip this version** | Hides ALL notifications for that exact version forever. You'll still be notified about *newer* versions when they appear. Use this for pre-releases you don't want, or if you've decided to stay on your current build. |

### Manual checks

Any time you want to look manually, **Help → Check for Updates…**
opens the same dialog with the full release notes. This works
even if you've skipped a version — manual checks always show the
current state.

### If you operate offline

The startup check fails silently if Lyra can't reach GitHub (no
internet, firewall block, GitHub rate-limit). You won't see an
error — Lyra just doesn't pop the modal and doesn't update its
cache. There's nothing for you to configure. Manual
**Help → Check for Updates** then surfaces the failure reason
inside the dialog if you want to know why.

## 9. Backups & snapshots

Lyra stores every operator preference (layout, IP address, audio
device, AGC profile, color picks, balance, cal trim, dock positions,
band memory, and more) under a single namespace. The **File** menu
exposes four actions for managing this:

| Action | Does |
|---|---|
| **Export settings…** | Save your entire preference set to a JSON file. Use this to back up before risky changes, share a config with another operator, or migrate to a new machine. |
| **Import settings…** | Load a previously-exported JSON. Replaces your current settings. **A safety snapshot of your current state is taken first** so you can roll back. |
| **Snapshots ▸** | Submenu of automatic snapshots. Lyra takes one snapshot every launch and keeps the last 10. Click any entry to restore. |
| **Open snapshots folder** | Launch File Explorer at the folder where snapshots live (`%LOCALAPPDATA%\N8SDR\Lyra\snapshots\` on Windows). |

**The auto-snapshot is your free safety net** — if anything in
your current session goes sideways (wrong panel layout saved as
default, accidental Reset to a blank screen, color picks gone
weird), File → Snapshots → "yesterday at 14:23" puts you back in
one click.

A few notes:

- Snapshots are plain JSON. You can open one in any text editor
  to inspect or hand-edit individual settings if you really want to.
- Importing a snapshot from a NEWER Lyra version is refused (the
  refusal is friendly, not destructive). Update Lyra first.
- Layout / graphics-backend changes need a Lyra restart to fully
  take effect after import — the success dialog reminds you.
- Manual exports (created via "Export settings…") are NOT counted
  toward the 10-snapshot retention limit; they live alongside the
  auto-snapshots in the same folder but are never auto-deleted.
