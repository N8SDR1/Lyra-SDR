# Noise Reduction & Noise Blanker *(not yet implemented)*

Placeholder. Document when these ship:

## Noise Blanker (NB)
- Threshold, pulse-width parameters
- Pre-demod (I/Q-domain) operation
- When to use vs NR

## Noise Reduction (NR)
- Classical spectral subtraction (ship first — always available)
- Quality / adaptation-speed parameter
- Post-demod (audio-domain) operation

## Neural / ANC
- RNNoise (small RNN, real-time, speech-optimized) — first neural
  target
- DeepFilterNet — alternative, heavier but higher quality
- Model file handling (optional vs required)
- Toggle-off fallback to classical NR

## Auto-Notch Filter (ANF)
- LMS adaptive notch for CW interference
- Difference from the manual Notch filter (which is user-placed IIR)
