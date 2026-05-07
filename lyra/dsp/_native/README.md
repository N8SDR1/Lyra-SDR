# `lyra/dsp/_native/` — bundled native DSP libraries

This directory holds the native Windows DLLs that `lyra.dsp.wdsp_engine`
loads at runtime. Lyra ships these so a fresh install works on any HL2
operator's PC without requiring a separate radio program to be installed.

## What's here

| File | Size (approx) | Role |
| --- | --- | --- |
| `wdsp.dll` | 5.5 MB | The DSP engine — RX / TX / AGC / NR / filters |
| `libfftw3-3.dll` | 2.6 MB | FFTW double-precision (used by WDSP) |
| `libfftw3f-3.dll` | 1.8 MB | FFTW single-precision (used by specbleach) |
| `rnnoise.dll` | 5.6 MB | RNNoise — neural-net noise reduction (NR3 / NR4 modes) |
| `specbleach.dll` | 41 KB | Spectral-bleach noise reduction |

Total: ~16 MB. These are 64-bit Windows DLLs (x64 only).

## Why bundled

The alternative — telling each operator "go install this other radio program
first to get the DLLs" — produces a worse install experience and a worse
support story. So Lyra ships everything it needs.

If you're running Lyra from a development checkout and these files aren't
present, `wdsp_native.py` falls back to looking in
`C:\Program Files\OpenHPSDR\Thetis-HL2\` and a couple of similar locations.
For a production install, the bundled copies in this folder are what gets
used.

## License

These DLLs are GPL-3.0-or-later. Lyra is also GPL-3.0-or-later, so the
combined work is license-compliant. The corresponding source code for these
libraries is publicly available; see the project README and `NOTICE.md` for
upstream pointers.

## Updating

When upgrading the bundled DSP runtime, drop replacement DLLs in here and
verify with:

```
python scratch/test_wdsp_poc.py
```

Expected: a clean WAV at `scratch/wdsp_poc_out.wav` containing a 1500 Hz
tone, with the script reporting `peak frequency in steady-state L: 1500.0
Hz`.
