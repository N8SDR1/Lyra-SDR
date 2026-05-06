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

## Algorithm picker (NR1 vs NR2)

**Right-click** the NR button for the algorithm menu:

| Algorithm | What it is | When to reach for it |
|---|---|---|
| **NR1** *(default)* | Classical spectral subtraction | General band noise; lower CPU; most speech-friendly default |
| **NR2** | Ephraim-Malah / MMSE-LSA, Wiener LUT, AEPF, SPP | Heavier QRN, weak DX, voice in dense noise; sharper formant preservation |
| **Neural** | *deferred* | *(disabled — pending RX2 + TX)* |

NR1 and NR2 are independent processors with their own STFT framing.
Switching between them is sample-accurate (no clicks).  The
operator-facing strength control (the slider next to the NR button)
re-binds to whichever algorithm is active.

## Strength slider

Both NR1 and NR2 expose a single continuous strength slider on the
DSP+Audio panel:

- **NR1**: range 0–100 %.  Interpolates internal α (over-subtraction)
  and β (spectral floor) between gentle and aggressive anchors.  At
  50 % the values land on the "Medium" preset that matches the
  classic Lyra default.  At 100 % the result is similar to what
  earlier Lyra versions called "Aggressive."
- **NR2**: range 0–200 %.  Drives the MMSE-LSA gain blend toward
  unity at low values (subtle) and pushes the corrected gain to a
  higher power at high values (deeper cleanup at the cost of
  thinning).  100 % is the WDSP-default tuning.

Internal DSP parameters per processor:

| Processor | FFT size | Hop | Bin spacing @ 48 kHz | Internal latency |
|---|---|---|---|---|
| NR1 | 256-pt | 128 | 187.5 Hz | ~2.7 ms |
| NR2 | 1024-pt | 512 | 46.9 Hz | ~10.7 ms |

Both use Hanning windowing with COLA-exact 50 % overlap-add — no
clicks on parameter changes mid-transmission.  NR2's larger FFT
gives 4× finer frequency resolution, which preserves voice formants
much more cleanly at high strength settings.

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

### Capture quality — your ear is the filter

Earlier versions of Lyra (v0.0.7.x through v0.0.9.4) ran a two-
layer "smart-guard" variance check on every capture and warned
you in the save dialog if a signal looked like it was riding
through the capture window.  **That check was removed in v0.0.9.5
after operator field testing showed it produced both false
positives** (firing on clean noise across multiple band positions)
**and false negatives** (passing FT8 captures cleanly).

The underlying reason: real ham band noise has legitimate
amplitude modulation (powerline arcing envelope at 120 Hz US /
100 Hz EU, BCB carrier modulation, atmospheric crashes, HF
propagation breathing).  The detector model couldn't separate
that from real signal contamination.

**The replacement is your ear.**  During the 2-second capture
window, listen.  Watch the waterfall.  If you heard a syllable
or saw a signal pass through, recapture.  If the band was clean,
save the profile.  Operators were already doing this naturally —
the algorithm was duplicating their judgment poorly.

The Settings → Noise → "Detect signal during capture" checkbox
is gone.  The save dialog is back to the simple "Capture
complete. Save as: [name]" flow it had before v0.0.7.

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

### Captured + NR2: closed-form Wiener filter

When captured-source mode is on AND NR2 is the active processor,
NR2 takes a different math path internally — a closed-form Wiener
filter computed directly from the captured profile, rather than
the decision-directed MMSE-LSA path it uses with live noise
tracking.  This is mathematically the optimal estimator when the
noise spectrum is known a priori (which is exactly the
captured-profile case).

In practical operator terms: **captured-source NR2 produces
cleaner output than captured-source NR1** for the same captured
profile.  If you've captured a good profile of your local noise
environment, NR2 with captured-source on is the strongest
spectral noise reduction Lyra has.

This was a v0.0.8 fix — earlier Lyra versions reused the live
MMSE-LSA pipeline when captured-source was on, which was
mathematically wrong (the decision-directed update assumes a
moving noise estimate; a frozen captured profile defeated the
musical-noise damping).  Operators upgrading from v0.0.7 should
notice their existing captured profiles work substantially
better in NR2 mode.

### Profile staleness notification

Every few seconds while a captured profile is loaded, Lyra
compares the live band noise spectrum against the loaded profile.
If the spectrum *shape* drifts beyond a threshold (e.g., 60 Hz
powerline comb appears or disappears, atmospheric noise replaces
local QRM, you change band-pass filters), a one-shot status-bar
toast appears:

```
⚠  Noise profile drifted 12.3 dB from current band conditions
   — consider recapturing.
```

The notification is **passive** — Lyra never auto-loads or
auto-switches profiles.  You decide whether the drift is meaningful
and whether to recapture.  Some drift is normal (band noise
changes with time of day); the toast just gives you a heads-up
when the change is large enough that your captured profile may no
longer be representative.

The check is scale-invariant — same noise getting louder or
quieter doesn't trigger it.  Only spectrum *shape* changes do.
That matches what AGC handles (level changes are AGC's job; shape
changes are NR's job).

Default ON; toggle in Settings → Noise → "Profile staleness
notifications."  Hysteresis prevents toast spam — at most one fire
per stale event, with re-arm after band conditions stabilize.

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

The captured profile is a per-bin float32 array of FFT magnitudes,
saved at NR1's FFT bin count (currently 129 bins for the 256-pt
STFT).  When NR2 loads a profile (NR2 uses a 1024-pt FFT → 513
bins), Lyra automatically resamples on load via linear
interpolation across the normalized bin axis — both arrays
represent the same DC-to-Nyquist frequency range, just at
different resolutions, so resampling produces a valid profile at
any target FFT size.  This means existing saved profiles work
with both NR1 and NR2 transparently.

On capture, Lyra accumulates per-bin magnitudes over the capture
window, averages them, and stores the result as the locked noise
reference.  The live noise tracker keeps running in parallel as a
fallback in case the captured profile is cleared or toggled off
mid-stream.

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

**Neural noise reduction** (RNNoise / DeepFilterNet / NSNet2)
learns from speech+noise datasets and suppresses noise without
that musical-noise signature.  Lyra has a reserved **Neural**
slot in the profile menu, currently **disabled** with the label
*(deferred — pending RX2 + TX)*.

The v0.0.6 development cycle briefly explored both PyTorch /
DeepFilterNet and onnxruntime / NSNet2 paths.  Both are viable
but introduce dependency-management friction (Python-version
lag, Rust toolchain requirements, model-file distribution)
that's better tackled when the broader transceiver functionality
(second receiver, TX path) is in place.  The integration code
was removed in v0.0.7 and will return as a clean implementation
once the radio side is feature-complete.

This is still a tracked project goal — see `docs/backlog.md`.

## Where NR sits in the audio chain

```
IQ → NB → decimate → manual notches → demod → LMS → ANF → SQ → NR → APF → audio out
```

Each stage has a specific role and they're complementary, not
overlapping:

- **NB** (Noise Blanker) — IQ-domain impulse blanker, before
  decimation.  Catches lightning crashes, ignition noise.  See
  [Noise Blanker help](./nb.md).
- **Manual notches** — kill known stable carriers (heterodyne
  whistles you've manually clicked).  See
  [Manual notches help](./notches.md).
- **LMS** (Line Enhancer / NR3) — predictive: lifts periodic
  content (CW carriers, voice formants) above broadband noise.
  Sits BEFORE ANF so LMS sees the periodic content it needs to
  predict (running ANF first would strip exactly what LMS is
  trying to lift).  See [LMS help](./lms.md).
- **ANF** (Auto Notch Filter) — cancels remaining periodic
  whistles ANF discovers automatically (heterodynes the operator
  hasn't manually notched).  See [ANF help](./anf.md).
- **SQ** (Squelch) — voice-presence-aware gate.  Sits AFTER the
  adaptive filters so they keep adapting during gate-closed
  periods, BEFORE NR so the voice-presence detector sees audio
  with full noise variance (NR-smoothed audio confuses the
  detector).
- **NR** (this doc) — broadband noise reduction.  Cleans up
  whatever residual hiss is left.
- **APF** (Audio Peaking Filter) — CW-only narrow boost.  See
  [APF help](./apf.md).

## What NR doesn't do

- **Impulse noise** (ignition noise, lightning crash, power-line
  buzz) — that's the **Noise Blanker (NB)** button's job, which
  operates in the I/Q domain pre-demod.
- **Specific carriers** (heterodynes, birdies) — use [manual
  notch filters](./notches.md) or let **ANF** find them
  automatically.  NR treats the whole spectrum statistically; it
  will not surgically kill a single carrier without also thinning
  everything around it.
- **Periodic content amplification** — that's **LMS** (line
  enhancer), the inverse of NR.  Where NR removes what doesn't
  look like signal, LMS amplifies what looks periodic.  Use them
  together for weak CW: LMS lifts the carrier, NR cleans the
  hiss around it.

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
  Listen during the 2-second capture and watch the waterfall —
  if anything passes through, re-capture.  Your ear is the
  best contamination detector.
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
