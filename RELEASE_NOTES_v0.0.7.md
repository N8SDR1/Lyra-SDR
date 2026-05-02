# Lyra-SDR v0.0.7 — "Polish Pass"

Released: 2026-05-01

A focused tester-feedback release.  No new DSP or radio features —
every change is an operator-visible UI fix from feedback on the v0.0.6
install.

If you're already running v0.0.6, this is a drop-in upgrade: same
QSettings namespace, same dock layout (with one targeted fix — see
"Tuning panel resize" below), same captured noise profiles, same
weather-alert sources.  Nothing on the radio side is touched.

## Highlights

- **Three-column Noise Settings tab, rebalanced.**  Was two columns
  in v0.0.5, became three in early v0.0.6.x; testers flagged that the
  middle column (NB + ANF + LMS + Squelch) was driving the page
  height.  Now: `Cap + Squelch | NB + ANF | NR2 + Method + LMS` —
  even weight, no scrolling at 1080p.
- **Tuning panel vertical resize works again.**  Operator feedback:
  *"I can widen Tuning but not change its height."*  The
  `FrequencyDisplay` widget shipped with `QSizePolicy.Fixed` vertical,
  which made Qt's row-layout engine refuse extra height.  Fixed by
  overriding the freq-display vertical policy to Preferred and giving
  the panel an explicit `MinimumExpanding` policy with a 180 px floor.
- **Brighter checkboxes / radio buttons.**  Operator feedback: *"tick
  boxes blend into the background."*  Indicator borders now use the
  dusty-blue `TEXT_MUTED` color against the dark recess instead of
  the near-invisible `BORDER` tone.  Also bumped 14 → 16 px for
  visual weight.  Same treatment on radio buttons.
- **Update notifications: pre-release + full-release parity.**  The
  silent update checker was hitting GitHub's `/releases/latest`
  endpoint, which by design hides pre-releases.  Switched to
  `/releases` and pick the highest semver tag ourselves.  Testers on
  a pre-release now get notified of newer pre-releases AND any
  subsequent full release.
- **Toolbar update indicator.**  When an update is available, a
  small orange "🆕 vX.Y.Z available" pill appears centered between
  the clocks and the HL2 telemetry block on the header toolbar.
  Click to open Help → Check for Updates.

## UI polish details

- **Global font 10pt → 11pt** for readability on the dense Settings
  tabs.  All UI surfaces follow via the QApplication-level font.
- **MHz / Step labels under the freq display** were getting clipped
  against the digits.  Added 10 px of vertical breathing room
  between the freq-display row and the MHz/Step controls.
- **DSP+Audio panel: AGC + notch readouts now fixed-height.**
  Operator feedback: *"@notches and AGC dBFS labels stretch when I
  resize the panel; should behave like the buttons next to them."*
  Set `QSizePolicy(Preferred, Fixed)` on `notch_info`, the AGC
  profile / threshold / action labels.
- **Lock panels actually locks all panels.**  Operator feedback:
  *"some panels can be resized even when locked."*  The v0.0.6
  implementation disabled the `setFeatures` drag/float and the
  `QSplitter` handles, but missed the QMainWindow internal dock-area
  separator (which is built into Qt's dock-layout class, not a real
  splitter).  Third lock layer added: `setFixedSize(currentSize)`
  on every dock during lock; restored to unconstrained on unlock.
  Also gated so the unlock min/max calls only fire if a lock pin
  was actually placed — avoids subtle row-height interactions on
  fresh launches.

## Update checker / notification routing

- New module: `lyra/ui/update_check.py` rewrites the worker to
  iterate the `/releases` array, skip drafts, parse `tag_name`
  with `_parse_version`, and pick the highest version.  Drops the
  unparseable-tag and empty-payload branches with explicit error
  messages.
- New toolbar widget on the header: `update_indicator` — hidden by
  default, visible only when the silent checker finds a newer
  release.  Sits between two equal `Expanding` spacers so it
  centers between the clocks and the HL2 T/V telemetry block.
- Minor Qt quirk: `QToolBar.addWidget()` wraps the widget in a
  `QWidgetAction`, and the *action's* visibility is what the
  toolbar honors — not the widget's.  Captured the action and
  toggle that in the show/hide handlers.

## Self-compile install hardening

- The DSP+Audio device-list dropdown now distinguishes three
  failure modes with actionable error messages: *sounddevice not
  installed* (with the `pip install` line), *PortAudio failed to
  load* (with the troubleshooting hint), and *no devices reported
  by Windows* (with the "check Windows Sound settings" hint).
  Targets a v0.0.6 self-compile bug where one tester saw an empty
  device list with no explanation.

## Neural NR — formally deferred

The `onnxruntime` / DeepFilterNet exploration code from v0.0.6 dev
(~1,100 lines) is removed in v0.0.7.  The Neural slot in the
right-click NR backend menu remains in place as a `(deferred —
pending RX2 + TX)` placeholder so operators know it's planned.

WDSP-derived NR1, NR2, NR3 (LMS), ANF, NB, and Squelch all stay
exactly as shipped in v0.0.6 — this is a UI-only release.

## Compatibility

- **Settings**: drop-in over v0.0.6.  Same `HKEY_CURRENT_USER\
  Software\N8SDR\Lyra` namespace.  No migration needed.
- **Captured noise profiles**: drop-in.  Same JSON format, same
  storage location.
- **Dock layout**: drop-in.  The `_apply_panels_lock_from_settings`
  re-application is now a no-op on never-locked installs, so the
  Tuning row geometry is preserved across the upgrade.
- **License**: still GPL v3+ (unchanged from v0.0.6).
- **Minimum Windows**: still Windows 10 build 17763 (1809, October
  2018) or later.

## What's next

v0.0.8 work begins on the **second receiver (RX2)**.  The dual-VFO
slot is already in the Tuning panel — currently dimmed with a
"not yet wired" banner — and the HL2 has the headroom (DDC2 slot +
a second set of audio taps).  The Radio class just hasn't been
taught to drive it yet.

After RX2 lands, the TX path is next, followed by the deferred
neural NR exploration.

73 from N8SDR.
