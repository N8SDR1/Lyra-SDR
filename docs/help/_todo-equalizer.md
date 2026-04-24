# Equalizer *(not yet implemented)*

Placeholder. The plan is a **true parametric EQ** with mandatory
bypass (EQ Off). Do **not** ship a fixed-band graphic EQ — users
expect parametric controls.

Document when it ships:

## Per-band controls
- Frequency (Hz)
- Gain (dB)
- Q (width)
- Filter type (peak / low-shelf / high-shelf / low-cut / high-cut)

## Chain architecture
- Separate RX and TX chains
- Per-mode defaults
- User-named presets
- **EQ Off** (bypass) mode — always default

## Reference
- EESDR3 RX/TX equalizer UI
- Thetis RX/TX equalizer preset system
