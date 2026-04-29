# Lyra v0.0.5 — "Listening Tools"

A meaningful audio + panadapter release. Two new CW DSP tools,
full GPU panadapter feature parity, an audio chain rebuild that
fixes several stability issues, and an auto-update check so
testers don't get stranded on old builds.

## New listening tools

- **APF — Audio Peaking Filter (CW)** — narrow peaking biquad
  centered on your CW pitch. Boosts weak CW above the noise floor
  without the ringing tail of a brick-wall filter. Right-click
  the APF button for BW / Gain quick presets.
- **BIN — Binaural pseudo-stereo (headphones)** — Hilbert phase
  split for spatial CW perception and SSB voice widening.
  Adjustable depth 0–100 %, equal-loudness normalized.

## GPU panadapter — full feature parity (BETA)

Everything the QPainter widget does, now on the GPU: band plan,
peak markers, spots, notches, click-to-tune, Y-axis drag, wheel
zoom, RX-BW edge drag, passband overlay, noise floor, VFO marker,
CW Zero, grid toggle. Opt-in via **Settings → Visuals → Graphics
backend → GPU panadapter (beta)**.

## Audio chain rebuild

- AGC profiles recalibrated — Fast/Med/Slow time constants were
  ~20× too slow. Now match standard SDR-client conventions.
- AGC OFF audibility fixed — was 14 dB quieter than AGC ON, now
  level.
- Mode = Tone hang fixed (was producing wrong-rate samples).
- Audio output rate-sticky bug fixed (AK4951 ↔ PC Soundcard
  switching could lock the audio path).
- WWV ↔ FT8 stuck audio fixed (round-robin C&C keepalive at the
  protocol layer).

## S-meter overhaul

- LNA-invariant readings — moving the LNA slider no longer changes
  the meter. dBm display now reflects actual antenna signal level.
- Auto-LNA pull-up (opt-in) — Auto button can now also raise gain
  on sustained-quiet bands, with a two-tier ceiling to stay out
  of the IMD zone.

## Quality of life

- Spot prefixes now show plain-text 2-letter ISO codes (e.g.
  `US N8SDR`, `JA JA1XYZ`) — replaces regional-indicator emoji
  flags that Windows can't render.
- Settings → DSP → CW group consolidates pitch, APF, and BIN
  controls.
- Auto-update check on startup — silent background check; shows
  status-bar message + Help menu badge when a newer release is
  available.

## What to test

- Headphones for BIN — try it on CW first, then SSB
- APF on weak CW signals — toggle on/off to compare
- GPU panadapter — switch backends, verify all overlays work
- Big freq/mode jumps (WWV ↔ FT8 etc.) — should no longer stick

## Known issues

- **GPU panadapter (BETA) — rendering glitch in this build.** The
  spectrum trace appears washed-out and smeared in GPU panadapter
  mode. This is a regression introduced during the v0.0.5 BETA
  feature-parity work and is being chased for a v0.0.5.1 patch.
  **Workaround:** stay on the default **OpenGL — accelerated
  QPainter** backend (Settings → Visuals → Graphics backend) — that
  path is fully functional and indistinguishable in feature set.
  Software (QPainter on CPU) is also fine if your GPU stack has
  trouble.

## Next up

- ANF (auto-notch) and NB (noise blanker) buttons remain stubs;
  the next release will tackle those plus a captured-noise-profile
  feature for targeted noise reduction.

## Install

Download `Lyra-Setup-0.0.5.exe` below, run it, click through the
installer. Folder install includes the User Guide markdown so
operators can press F1 in the app for the full topic tree.
