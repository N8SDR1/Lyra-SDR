# Noise Reduction (NR)

Lyra's NR is a **streaming spectral-subtraction** noise reducer.
It sits between the demodulator and AGC in the audio chain, so it
cleans up hiss and background noise before AGC evaluates peaks —
which keeps AGC from pumping on broadband noise during quiet moments.

## The two independent controls

NR has **two** operator controls that work together:

1. **Profile** — how aggressively noise is subtracted
2. **Noise source** — what model of "noise" is being subtracted

Operator picks each independently. The profile and the source are
**orthogonal** — change one without losing the other.

## Toggling on/off

The **NR** button on the [DSP & AUDIO panel](panel:dsp). Lit = NR
engaged; dim = bypass.

## Profile (subtraction strength)

**Right-click** the NR button for the profile menu:

| Profile | DSP parameters | Use it for |
|---|---|---|
| **Light**      | α = 1.0, β = 0.20 | SSB ragchew, subtle hiss only |
| **Medium** *(default)* | α = 1.8, β = 0.12 | General speech — best all-rounder |
| **Aggressive** | α = 2.8, β = 0.06 | Noisy bands, weak DX, heavy QRN |
| **Neural**     | *RNNoise / DeepFilterNet* | (greyed out until installed) |

Where:

- **α** (over-subtraction factor) controls how aggressively noise
  magnitude is subtracted from the signal spectrum. Higher = more
  noise removed.
- **β** (spectral floor) limits how much any single FFT bin can be
  attenuated. Higher = less "musical noise" artifact, but less
  overall quieting. Lower = deeper noise suppression at the cost of
  more artifacts.

All profiles use the same STFT kernel — 256-point FFT, 50% Hanning
overlap, perfect reconstruction (COLA-exact) — so switching profiles
mid-transmission produces no clicks or dropouts.

## Noise source (Live ⇄ Captured)

The **source badge** sits on its own row directly below the DSP
buttons, alongside the **📷 Cap** capture button. It shows the
current noise source at a glance and one-click toggles between
the two states (when a captured profile is loaded).

### Live (VAD-tracked) — the default

```
🔵   Live (VAD)   ·   no captured profile
```

Lyra's adaptive estimator updates the noise model whenever a
frame is quieter than the current estimate (a VAD-style gate).
This is what NR1 has done since v0.0.5. Works fine on most bands
but is always *guessing* the noise model from current audio.

### Captured — Audacity-style locked profile

```
🟢   Powerline 80m  ·  3h old  ·  80m LSB  ⇄
```

Operator records ~2 seconds of pure band noise (no signal present)
on a quiet patch of the band; Lyra averages the FFT magnitudes
into a locked spectral profile. NR uses *that* as the noise
reference instead of the live estimate. **Generally cleaner output**
because the noise model is measured directly from real noise without
any signal contamination.

### How to capture a profile

1. Tune to a frequency on your current band where there's **no
   signal** — a quiet patch between active QSOs, or wait for a
   transmission gap.
2. Click the **📷 Cap** button. The button shows live progress
   "⏹ NN%" while capturing (default 2.0 sec; configurable in
   Settings → Noise → Capture duration).
3. When capture completes, a save-name dialog prompts. Default
   name is `<band> <date time>` like "80m 2026-04-30 14:22" —
   edit to whatever's meaningful: "Powerline 80m", "Storm noise",
   "FT8 hash 20m", etc.
4. After saving, the source badge auto-flips to the captured
   profile and the green dot lights up. NR is now using your
   captured profile as the noise model.

### Smart-guard

After every capture, Lyra checks the captured noise for
frame-to-frame power variance. **High variance suggests a signal
was riding through the capture window** — Lyra warns you in the
save dialog with a ⚠ banner ("smart-guard flagged signal during
capture") so you can re-capture before saving.

You can disable the guard in Settings → Noise → "Detect signal
during capture" if you know your captures are good and the
warning is firing on stable noise sources you've verified by ear.

### Right-click on 📷 Cap — full menu

```
Capture now (2.0 s)                ← uses your saved duration
Capture for 1.0 s
Capture for 3.0 s
Capture for 5.0 s
─────
Manage profiles…                   ← opens the profile manager
Open Noise settings…               ← jumps to Settings → Noise
─────
Clear loaded profile (Powerline 80m)
```

### Click the source badge to toggle

When a profile is loaded, click anywhere on the source badge
(the row below the DSP buttons) to flip Live ⇄ Captured. The
profile (Light/Medium/Aggressive) doesn't change — only the
noise source does. So you can:

- Switch Aggressive ⇄ Captured to chase a weak DX signal hiding
  in your captured-profile band noise
- Switch Light ⇄ Captured for SSB ragchew with a clean noise model
- Toggle source quickly to A/B test live vs captured against a
  real signal

### Age coloring on the badge

The captured-at age in the badge changes color based on
operator-tunable thresholds (Settings → Noise → "Profile age
warning"):

- **Grey** — fresh (default: less than 24 hours old)
- **Amber** — getting stale (24h – 7 days by default)
- **Red** — likely outdated (more than 7 days by default)

Recap when the band shifts noticeably — power-line patterns
change between morning/afternoon/night, atmospheric noise
shifts with propagation, etc.

### Mode mismatch warning

If you switch to a mode that doesn't match the profile's captured
mode, the badge shows ⚠ — the captured noise was measured through
a different audio chain (e.g. captured on USB, listening on LSB).
NR still works, but the model isn't a perfect fit. Re-capture or
switch back to the matching mode for best results.

## Manage Profiles dialog

Right-click on the 📷 Cap button → **Manage profiles…**, or visit
Settings → Noise → "Open profile manager…". Shows all your
captured profiles in a list with:

- **Name** — operator-typed display name
- **Band / Mode** — derived from frequency at capture time
- **Captured** — date/time, age-colored
- **Duration** — how long the capture was

Buttons:

- **Use Selected** — load this profile + flip source to Captured
- **Re-capture** — overwrite this profile with fresh band noise
- **Rename / Delete** — standard ops
- **Export… / Import…** — single-profile JSON files for sharing
  or backup

Profiles persist across Lyra restarts. The last-active profile
auto-restores at startup so you pick up where you left off.

### Profile storage location

Default: `%APPDATA%\Lyra\noise_profiles\` (Windows) or the
OS-equivalent user-data folder. Each profile is a single JSON
file you can copy/share/edit by hand.

Settings → Noise → "Storage location" lets you point Lyra at a
custom folder — Dropbox/OneDrive for sync between shacks, USB
drive for portable operation, NAS for shared club resources, etc.

## Internals (for the curious)

### Live source

The noise-floor estimate is tracked only during low-energy frames
(a simple VAD gate rejects speech/signal frames from the update).
This keeps the estimate from slowly rising as speech energy
dominates. Each profile has its own VAD gate (`vad_gate` ×
current-estimate-power threshold).

### Captured source

The captured profile is a 129-element float32 array (FFT_SIZE/2+1
bins for the 256-pt STFT). On capture, Lyra accumulates
per-bin magnitudes over the capture window, averages them, and
stores the result as the locked noise reference. The live VAD
estimator keeps running in parallel as a fallback in case the
captured profile is cleared or toggled off mid-stream.

### Reset behavior

On **mode change**, the NR internal state (live noise-floor
estimate + overlap-add tail) is reset so a stale estimate from a
previous mode doesn't leak in and cause a half-second of artifact.
The captured profile (operator-locked) is *preserved* across
resets — you don't lose your work just because you switched
band or mode.

## Classical vs. neural — the roadmap

Classical spectral subtraction has one well-known limitation:
**musical noise**. When a bin's gain jumps around rapidly between
frames (which happens near the noise threshold), the residual tail
sounds like "bubbling water" or "aliens talking." The Aggressive
profile has the most of this artifact by design. The captured
source generally has *less* musical noise than live-source because
the noise model is more accurate.

**Neural noise reduction** (RNNoise, DeepFilterNet) learns from
speech+noise datasets and suppresses noise without that musical-
noise signature. Lyra has a reserved **Neural** slot in the profile
menu. When RNNoise or DeepFilterNet is importable (e.g., via
`pip install rnnoise-wrapper` or similar), the slot enables
automatically and the Radio's NR backend can swap to the neural
engine. Until then it's greyed out with an "install RNNoise or
DeepFilterNet" hint in the menu.

When Neural is active, the source badge silently disables —
neural NR has its own internal trained noise model and doesn't
use the source toggle. Switching back to a classical profile
re-enables the badge and restores your last source choice.

This was a day-one project goal — see `docs/backlog.md`.

## What NR doesn't do

- **Impulse noise** (ignition noise, lightning crash, power-line
  buzz) — that's the **Noise Blanker (NB)** button's job, which
  operates in the I/Q domain pre-demod. NB is still a stub pending
  its own backend.
- **Specific carriers** (heterodynes, birdies) — use
  [notch filters](./notches.md) for that. NR treats the whole
  spectrum statistically; it will not surgically kill a single
  carrier without also thinning everything around it.

## Tips by mode

- **SSB** — Medium + Captured is the sweet spot once you have a
  band-specific profile. Switch profile to Light if NR is chewing
  on speech consonants. A captured profile from a quiet patch on
  your band is dramatically better than live tracking when the
  band noise is stationary.
- **CW** — counter-intuitively, NR can hurt weak CW by confusing
  the subtractor (a dit is short and can read as noise). Try
  **Light** + Live, or turn NR off and lean on a narrow filter
  plus the [APF](./apf.md).
- **FT8 / digital** — NR helps the decoder only modestly; the
  decoder does its own matched filtering. Try Medium + Captured
  for a known-noisy band, or leave NR off and let the decoder do
  its thing.
- **AM broadcast** — Medium + Captured with a wider RX BW (6 kHz)
  gives a noticeably cleaner fidelity on a ragged BC signal.

## Tips for captured profiles

- **Capture on a truly quiet patch** — between QSOs, in a
  transmission gap, or 5–10 kHz away from any active station.
  Smart-guard catches obvious mistakes but a clean capture is
  always the best starting point.
- **Re-capture when the band shifts** — power-line patterns
  change between morning/afternoon/night, atmospheric noise
  shifts with propagation. The age coloring on the source badge
  is your cue.
- **One profile per band/condition combo** — "Powerline 80m
  daytime", "80m night QRN", "20m FT8 hash" each get their own
  profile. They're 700 bytes each on disk; keep as many as you
  find useful.
- **Profile sharing** — export your "morning power-line on 80m"
  and send it to a friend on the same grid. They can import it
  and have your noise model. Useful for diagnosing common
  station-vs-station interference.
