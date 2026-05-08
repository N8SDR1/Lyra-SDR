# Noise Reduction (NR)

Lyra's noise reduction is the **WDSP EMNR** (Enhanced Multi-band
Noise Reduction) engine — a Wiener / MMSE-LSA family of spectral
estimators developed by Warren Pratt NR0V and used by Thetis,
PowerSDR, and other openHPSDR-class clients for years.  Lyra
exposes WDSP's knobs in a streamlined four-control UI on the
DSP & AUDIO panel.

## Operator controls

The NR UX has four orthogonal controls:

1. **NR enable** — the **NR** button on the DSP & AUDIO panel.
   Lit = engaged; dim = bypass.
2. **Mode (1..4)** — picks WDSP's gain function.
3. **AEPF** — Adaptive Equalization Post-Filter (anti-musical-
   noise).
4. **NPE** — Noise Power Estimator selector.

Plus a fifth, indirect control:

5. **📷 Cap** — captured noise profile.  Capture works; the
   apply step is currently disabled (see "Captured profile
   status" below).

### NR enable

The **NR** button on the [DSP & AUDIO panel](panel:dsp).  Drives
WDSP's EMNR run flag — when on, every audio block runs through
the noise reducer.  When off, EMNR is bypassed at no CPU cost.

### Mode (1..4)

A small slider next to the NR button picks WDSP's EMNR gain
function.  Each mode is a different mathematical model for "what
gain should each frequency bin get given the live signal +
noise estimate?":

| Mode | WDSP gain function | Character                                               |
|------|--------------------|---------------------------------------------------------|
| **1** | Wiener + SPP       | Smooth, mid-aggressive.  Good general-purpose.          |
| **2** | Wiener simple      | Edgier — more raw subtraction, less smoothing.          |
| **3** | **MMSE-LSA** *(default)* | WDSP default.  Smoothest output, best for speech. |
| **4** | Trained adaptive   | Most aggressive.  Strong cleanup, can thin signal.      |

There's no single "best" mode — they sound different on
different bands and signal types.  The slider is right at your
fingertips; try them all on a real signal and pick the one
that sounds best to you.

### AEPF — Adaptive Equalization Post-Filter

The **AEPF** checkbox toggles WDSP's anti-musical-noise post-
filter.  EMNR by itself can leave a "watery" / "bubbling"
residual on weak signals; AEPF smooths the output spectrum to
suppress that artifact.  Default **on**.

Toggle off if you're chasing the absolute lowest noise floor on
a weak signal and want to see if the un-AEPF'd output reveals
something the post-filter is hiding.  For day-to-day operating,
leave AEPF on.

### NPE — Noise Power Estimator

WDSP needs an estimate of the "current noise" to know what to
subtract.  Two estimators are available:

- **OSMS** *(default)* — Optimal Smoothing of Minimum Statistics.
  Tracks the per-bin minimum power over a rolling window.  Fast
  to track band-noise changes.
- **MCRA** — Minima-Controlled Recursive Averaging.  More
  conservative; slower to update but gives a steadier noise
  reference.

Switch via the **NPE** dropdown next to the AEPF checkbox.  Most
operators won't need to change this — OSMS is the default for
good reason.

## Captured noise profile

The **📷 Cap** button captures ~2 seconds of pure band noise
into a saved spectral profile.  The intent is "lock in a
measured noise model so NR has a perfect a-priori reference"
instead of inferring noise from live audio.

### Current status (v0.0.9.6)

> **Capture works.  Apply is currently disabled.**

The capture button still records, names, and saves profiles —
all the operator-facing flow is preserved.  However, applying a
captured profile to live audio is disabled in WDSP mode.

The reason: the captured-profile-apply path was originally
implemented in Lyra's pure-Python NR.  After the WDSP audio
rebuild (v0.0.9.6) that Python path was bypassed, and several
attempts at a WDSP-side equivalent (initial direct-spectrum
subtraction, gentle Wiener, adaptive scaling, gain-floor) all
produced audible artifacts that the operator field-tested as
worse than the un-captured WDSP NR output.  The apply step is
disabled until a clean IQ-domain rebuild lands (tracked in the
project backlog as the "captured-profile IQ-domain rebuild"
item).

In the meantime:

- **Capture** still works — you can build a library of profiles
  for your bands, locations, and noise environments.
- **Save / load / rename / export / import** all work normally.
- The profiles are tagged with band, mode, freq, and timestamp
  so they're ready to use the moment the apply path comes back.
- The source badge below the DSP buttons may show ⚠ if you
  toggle "use captured" — that's the engine telling you the
  apply step is inert.

### How to capture (for future-proofing your library)

1. Tune to a frequency on your current band where there's
   **no signal** — a quiet patch between active QSOs, or wait
   for a transmission gap.
2. Click the **📷 Cap** button.  Capture progress shows on the
   button as "⏹ NN%" (default 2.0 sec; configurable in
   Settings → Noise → Capture duration).
3. When capture completes, a save-name dialog prompts.  Default
   name is `<band> <date time>` like "80m 2026-04-30 14:22" —
   edit to whatever's meaningful: "Powerline 80m", "Storm
   noise", "FT8 hash 20m", etc.
4. Profile saves to your noise-profile folder.  When the apply
   path returns in a future build, your library is already
   ready.

### Capture quality — your ear is the filter

During the 2-second capture window, **listen and watch the
waterfall**.  If you hear a syllable or see a signal pass
through, recapture.  If the band was clean, save the profile.

Earlier Lyra versions had a "smart-guard" detector that tried
to flag contaminated captures automatically.  It produced both
false positives and false negatives because real ham-band noise
has legitimate amplitude modulation (powerline arcing,
atmospheric crashes, propagation breathing) the detector
couldn't separate from real signal contamination.  It was
removed in v0.0.9.5 — your ear and waterfall are better filters
than the algorithm was.

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

### Manage Profiles dialog

Right-click on the 📷 Cap button → **Manage profiles…**, or
visit Settings → Noise → "Open profile manager…".  Shows all
your captured profiles in a list with:

- **Name** — operator-typed display name
- **Band / Mode** — derived from frequency at capture time
- **Captured** — date/time
- **Duration** — how long the capture was

Buttons:

- **Use Selected** — load this profile (applies once the apply
  path returns)
- **Re-capture** — overwrite this profile with fresh band noise
- **Rename / Delete** — standard ops
- **Export… / Import…** — single-profile JSON files for sharing
  or backup

Profiles persist across Lyra restarts.  The last-active profile
auto-restores at startup so you pick up where you left off.

### Profile storage location

Default: `%APPDATA%\Lyra\noise_profiles\` (Windows) or the
OS-equivalent user-data folder.  Each profile is a single JSON
file you can copy/share/edit by hand.

Settings → Noise → "Storage location" lets you point Lyra at a
custom folder — Dropbox/OneDrive for sync between shacks, USB
drive for portable operation, NAS for shared club resources, etc.

## Where NR sits in the audio chain

WDSP's RXA chain runs entirely inside the C-side cffi engine.
For operator mental-model purposes:

```
IQ → NB → notches → demod → LMS → NR (EMNR) → ANF → AGC → APF (CW) → audio out
```

NR sees the post-demod audio; AGC then evaluates the
NR-cleaned output for level decisions, so AGC doesn't pump on
broadband noise during quiet moments.

## What NR doesn't do

- **Impulse noise** (ignition noise, lightning crashes, power-
  line buzz spikes) — that's the **Noise Blanker (NB)** button's
  job, which operates pre-demod where impulses are still narrow.
- **Specific carriers** (heterodynes, birdies) — use [manual
  notch filters](./notches.md) (you click them) or **ANF** (it
  finds them automatically).  NR treats the whole spectrum
  statistically; it will not surgically kill a single carrier
  without thinning everything around it.
- **Periodic content amplification** — that's **LMS** (line
  enhancer), the inverse of NR.  Where NR removes what doesn't
  look like signal, LMS amplifies what looks periodic.  Use them
  together for weak CW: LMS lifts the carrier, NR cleans the
  hiss around it.

## Tips by mode

- **SSB ragchew** — Mode **3** (MMSE-LSA) + AEPF on is the
  comfortable default.  Try Mode 1 if you want a touch more
  aggressive cleanup; Mode 4 if a noisy band needs the heaviest
  hand.
- **Weak DX SSB** — Mode **3** + AEPF on, NPE = OSMS.  Try
  toggling AEPF off briefly to make sure the post-filter isn't
  hiding the signal you're listening for.
- **CW** — Mode **2** (Wiener simple) is sometimes the best CW
  mode because the smoother Mode 3 can blur a clean tone.  Try
  with NR off too — a narrow filter + the [APF](./apf.md) often
  beats NR for weak CW.
- **FT8 / digital** — most operators run NR off.  The decoder
  does its own matched filtering; NR can confuse it.
- **AM broadcast** — Mode **3** + AEPF on with a wider RX BW
  (6 kHz) gives noticeably cleaner fidelity on a ragged BC signal.

## Future work

The captured-profile apply path will return as a clean
IQ-domain rebuild (tracked in the backlog).  When it lands, the
"use captured" toggle on the source badge will start affecting
the audio again, the apply step will produce visibly cleaner
output on noise-floor-dominated signals, and the inert-warning
indicator will go away.

Until then, treat captured profiles as a library you're
building for the moment they become live again — every clean
capture you save now is one less you'll need to record later.
