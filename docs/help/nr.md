# Noise Reduction (NR)

Lyra's NR is a **streaming spectral-subtraction** noise reducer.
It sits between the demodulator and AGC in the audio chain, so it
cleans up hiss and background noise before AGC evaluates peaks —
which keeps AGC from pumping on broadband noise during quiet moments.

## Toggling on/off

The **NR** button on the [DSP & AUDIO panel](panel:dsp). Lit = NR
engaged; dim = bypass.

## Profile selector

**Right-click** the NR button for a profile menu:

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

## Internals (for the curious)

The noise-floor estimate is tracked only during low-energy frames
(a simple VAD gate rejects speech/signal frames from the update).
This keeps the estimate from slowly rising as speech energy
dominates. Each profile has its own VAD gate (`vad_gate` ×
current-estimate-power threshold).

On **mode change**, the NR internal state (noise-floor estimate +
overlap-add tail) is reset so a stale estimate from a previous mode
doesn't leak in and cause a half-second of artifact.

## Classical vs. neural — the roadmap

Classical spectral subtraction has one well-known limitation:
**musical noise**. When a bin's gain jumps around rapidly between
frames (which happens near the noise threshold), the residual tail
sounds like "bubbling water" or "aliens talking." The Aggressive
profile has the most of this artifact by design.

**Neural noise reduction** (RNNoise, DeepFilterNet) learns from
speech+noise datasets and suppresses noise without that musical-
noise signature. Lyra has a reserved **Neural** slot in the profile
menu. When RNNoise or DeepFilterNet is importable (e.g., via
`pip install rnnoise-wrapper` or similar), the slot enables
automatically and the Radio's NR backend can swap to the neural
engine. Until then it's greyed out with an "install RNNoise or
DeepFilterNet" hint in the menu.

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

- **SSB** — Medium profile is the sweet spot. Switch to Light if
  NR is chewing on speech consonants.
- **CW** — counter-intuitively, NR can hurt weak CW by confusing
  the subtractor (a dit is short and can read as noise). Try
  **Light** or turn NR off and lean on a narrow filter.
- **FT8 / digital** — NR helps the decoder only modestly; the
  decoder does its own matched filtering. Try Medium, or leave NR
  off and let the decoder do its thing.
- **AM broadcast** — Medium with a wider RX BW (6 kHz) gives a
  noticeably cleaner fidelity on a ragged BC signal.
