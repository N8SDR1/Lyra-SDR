"""
Lyra WDSP native bindings.

Loads the system-installed wdsp.dll and exposes the C entry points Lyra needs
through cffi. WDSP is a C library implementing the full RX / TX / AGC / NR /
filter chain for HPSDR-class radios. By calling into it directly we get a
GIL-free, sample-accurate DSP engine without trying to reimplement it in pure
Python.

This module is intentionally low-level: a thin, faithful wrapper over the C
ABI. Higher-level lifecycle and lyra-friendly types live in `wdsp_engine.py`.

DLL location
------------
The bindings look for `wdsp.dll` in (in order):

    1. The directory passed to ``load(dll_dir=...)``
    2. The ``LYRA_WDSP_DIR`` environment variable
    3. ``lyra/dsp/_native/`` — DLLs bundled with Lyra (preferred)
    4. ``C:\\Program Files\\OpenHPSDR\\Thetis-HL2`` (fallback for dev installs)
    5. ``C:\\Program Files\\OpenHPSDR\\Thetis``      (fallback for dev installs)

Production Lyra installs ship the DLL set inside ``lyra/dsp/_native/``,
so the bundled path is what end users hit. The fallback paths only matter
when developing against a local checkout without the DLLs vendored.

The DLL set includes:

    wdsp.dll          — the DSP engine itself
    libfftw3-3.dll    — FFTW (FFT library WDSP depends on)
    rnnoise.dll       — RNNoise neural noise-reduction (NR3 / NR4)
    specbleach.dll    — spectral-bleach noise reduction

License
-------
WDSP is GPL-3.0-or-later (Warren Pratt, NR0V). Lyra is GPL-3.0-or-later.
The two are link-compatible.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional

from cffi import FFI


# ---------------------------------------------------------------------------
# C declarations — only the entry points Lyra actually calls.
# ---------------------------------------------------------------------------

_CDEF = """
/* ----- channel lifecycle (channel.c) ------------------------------------ */

/*
 * OpenChannel allocates and configures an RX or TX channel.
 *
 *   channel       channel index (0 = RX1, 1 = RX2, 2 = TX, etc.)
 *   in_size       input buffer size in *frames* (NOT doubles)
 *   dsp_size      internal DSP buffer size in frames
 *   in_rate       input sample rate (Hz)
 *   dsp_rate      internal DSP sample rate (Hz)
 *   out_rate      output sample rate (Hz)
 *   type          0 = RX, 1 = TX
 *   state         initial run state (0 = stopped, 1 = running)
 *   tdelayup      slew-up delay (s)
 *   tslewup       slew-up duration (s)
 *   tdelaydown    slew-down delay (s)
 *   tslewdown     slew-down duration (s)
 *   block         "block until output available" flag — 1 makes fexchange0
 *                 block its caller until the DSP thread has produced the
 *                 next output buffer. This is what Thetis uses; it's what
 *                 keeps the audio cadence locked to the input cadence.
 *                 0 means fexchange0 returns immediately and the output
 *                 buffer may contain stale or zeroed data.
 */
void OpenChannel(int channel, int in_size, int dsp_size,
                 int input_samplerate, int dsp_rate, int output_samplerate,
                 int type, int state,
                 double tdelayup, double tslewup,
                 double tdelaydown, double tslewdown,
                 int block);

void CloseChannel(int channel);
int  SetChannelState(int channel, int state, int dmode);

/*
 * fexchange0: push one buffer of interleaved I/Q in, get interleaved L/R out.
 *
 *   in_buff       2 * in_size doubles, [I0,Q0, I1,Q1, ...]
 *   out_buff      2 * in_size doubles, [L0,R0, L1,R1, ...]
 *                 (output rate is `out_rate`, but buffer holds `in_size`
 *                  frames — WDSP handles the rate conversion internally.)
 *   error         out: 0 on success
 */
void fexchange0(int channel, double* in_buff, double* out_buff, int* error);

/* ----- RXA top-level (RXA.c) -------------------------------------------- */

void SetRXAMode(int channel, int mode);
void SetRXAPanelGain1(int channel, double gain);

/* RX bandpass post-demod */
void SetRXABandpassFreqs(int channel, double low, double high);
void RXASetPassband(int channel, double f_low, double f_high);

/* Per-WDSP-module run flags */
void SetRXAANRRun(int channel, int run);
void SetRXAANFRun(int channel, int run);
void SetRXAEMNRRun(int channel, int run);
void SetRXAFMSQRun(int channel, int run);
void SetRXAAMSQRun(int channel, int run);
void SetRXAAMSQThreshold(int channel, double threshold);

/* Noise blanker is exposed at the EXT (top-of-channel) layer in WDSP, not
   per-RXA, because it operates on raw I/Q before the RXA chain. */
void SetEXTNOBRun(int channel, int run);
void SetEXTANBRun(int channel, int run);

/* ----- AGC (wcpAGC.c) --------------------------------------------------- */

void SetRXAAGCMode(int channel, int mode);
void SetRXAAGCFixed(int channel, double fixed_gain_db);
void SetRXAAGCAttack(int channel, int attack_ms);
void SetRXAAGCDecay(int channel, int decay_ms);
void SetRXAAGCHang(int channel, int hang_ms);
void SetRXAAGCTop(int channel, double max_gain_db);
void SetRXAAGCThresh(int channel, double thresh_db, double size, double rate);
void SetRXAAGCSlope(int channel, double slope);
void SetRXAAGCHangThreshold(int channel, int hangthreshold);

/* ----- Meters (meter.c) ------------------------------------------------ */

/*
 * Read a meter value off an RX channel.  Meter types (matching
 * RXA.h enum rxaMeterType):
 *
 *     0  RXA_S_PK         S-meter peak (linear, dBFS via 10*log10(val))
 *     1  RXA_S_AV         S-meter average
 *     2  RXA_ADC_PK       ADC peak
 *     3  RXA_ADC_AV       ADC average
 *     4  RXA_AGC_GAIN     AGC linear gain (dB via 20*log10(val))
 *     5  RXA_AGC_PK       AGC envelope peak
 *     6  RXA_AGC_AV       AGC envelope average
 */
double GetRXAMeter(int channel, int mt);

/* ----- TX path (TXA.c) — declared but unused for v0.0.9.6 RX-only PoC ---- */

void SetTXAMode(int channel, int mode);
void SetTXABandpassFreqs(int channel, double low, double high);
void SetTXAPanelGain1(int channel, double gain);
"""


# ---------------------------------------------------------------------------
# rxaMode integer enum (matches WDSP RXA.h)
# ---------------------------------------------------------------------------

class RxaMode:
    LSB = 0
    USB = 1
    DSB = 2
    CWL = 3
    CWU = 4
    FM  = 5
    AM  = 6
    DIGU = 7
    SPEC = 8
    DIGL = 9
    SAM  = 10
    DRM  = 11


# ---------------------------------------------------------------------------
# AGC mode integer enum (matches Thetis Console AGCMode.cs)
# ---------------------------------------------------------------------------

class AgcMode:
    FIXED = 0
    LONG  = 1
    SLOW  = 2
    MED   = 3
    FAST  = 4
    CUSTOM = 5


class MeterType:
    """RXA meter type indices (matching wdsp/RXA.h enum rxaMeterType)."""
    S_PK     = 0
    S_AV     = 1
    ADC_PK   = 2
    ADC_AV   = 3
    AGC_GAIN = 4   # linear gain — convert to dB with 20*log10
    AGC_PK   = 5
    AGC_AV   = 6


# ---------------------------------------------------------------------------
# DLL location resolution
# ---------------------------------------------------------------------------

_REQUIRED_DLLS = (
    "wdsp.dll",
    "libfftw3-3.dll",
    "libfftw3f-3.dll",
    "rnnoise.dll",
    "specbleach.dll",
)

# Bundled location — DLLs ship inside the Lyra package next to this module.
_BUNDLED_DLL_DIR = Path(__file__).resolve().parent / "_native"

# Fallback locations — only used by developers running against a checkout
# where the DLLs haven't been vendored yet.
_FALLBACK_DLL_DIRS = [
    r"C:\Program Files\OpenHPSDR\Thetis-HL2",
    r"C:\Program Files\OpenHPSDR\Thetis",
]


def _resolve_dll_dir(explicit: Optional[str]) -> Path:
    """Find the first directory that contains the WDSP DLL set.

    Order: explicit arg → LYRA_WDSP_DIR env → bundled lyra/dsp/_native →
    fallback Thetis install paths.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("LYRA_WDSP_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(_BUNDLED_DLL_DIR)
    candidates.extend(Path(p) for p in _FALLBACK_DLL_DIRS)

    for d in candidates:
        if all((d / dll).exists() for dll in _REQUIRED_DLLS):
            return d

    raise FileNotFoundError(
        "Could not locate the WDSP DLL set "
        f"({', '.join(_REQUIRED_DLLS)}). Tried: "
        + ", ".join(repr(str(c)) for c in candidates)
        + "\n"
        "Set LYRA_WDSP_DIR or pass dll_dir=... to wdsp_native.load()."
    )


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_ffi: Optional[FFI] = None
_lib = None
_dll_dir: Optional[Path] = None
_lock = threading.Lock()


def load(dll_dir: Optional[str] = None):
    """Load wdsp.dll once per process. Idempotent.

    Returns the cffi library handle. Subsequent calls return the cached handle.
    """
    global _ffi, _lib, _dll_dir
    with _lock:
        if _lib is not None:
            return _lib

        directory = _resolve_dll_dir(dll_dir)

        # On Python 3.8+ Windows we must explicitly add the DLL directory so
        # wdsp.dll's dependent libfftw3-3.dll resolves.
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(directory))

        ffi = FFI()
        ffi.cdef(_CDEF)
        lib = ffi.dlopen(str(directory / "wdsp.dll"))

        _ffi = ffi
        _lib = lib
        _dll_dir = directory
        return lib


def ffi() -> FFI:
    """Return the cffi FFI instance (load() must have been called first)."""
    if _ffi is None:
        load()
    return _ffi  # type: ignore[return-value]


def lib():
    """Return the loaded wdsp.dll handle (load() must have been called first)."""
    if _lib is None:
        load()
    return _lib


def dll_dir() -> Optional[Path]:
    """Return the directory wdsp.dll was loaded from, or None if not loaded."""
    return _dll_dir


__all__ = [
    "load",
    "ffi",
    "lib",
    "dll_dir",
    "RxaMode",
    "AgcMode",
]
