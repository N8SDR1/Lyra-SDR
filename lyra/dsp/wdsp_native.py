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

/* FM squelch threshold (fmsq.c::SetRXAFMSQThreshold).  Sets both
   tail_thresh and unmute_thresh (latter at 0.9× threshold).  Lyra
   was previously calling SetRXAFMSQRun but never the threshold
   setter — operator's FM-mode squelch slider had no effect.  Wired
   in Phase 6.A4. */
void SetRXAFMSQThreshold(int channel, double threshold);

/* AM squelch hold-tail (amsq.c::SetRXAAMSQMaxTail).  Maximum
   tail-decay length after speech ends; clamped internally to
   `min_tail`.  Wired in Phase 6.A4. */
void SetRXAAMSQMaxTail(int channel, double tail);

/* Auto-Notch Filter parameters (anf.c::SetRXAANFVals).  Sets
   the four LMS-predictor knobs in one call:
     n_taps   — filter length (default 64)
     delay    — decorrelation delay samples (default 16)
     two_mu   — adaptation step size (≈ 1e-4; see anf.c hint)
     gamma    — leakage factor (≈ 0.10; see anf.c hint)
   Calls flush_anf to clear adaptive state.  Wired in Phase 6.A4
   so operator's μ slider in Settings → Noise actually drives
   ANF behavior. */
void SetRXAANFVals(int channel, int taps, int delay, double gain, double leakage);

/* Patch panel — output stage of WDSP's RXA chain.  The panel's
   `copy` field controls how the (I, Q) at the panel's input gets
   written to (L, R) at the output.  WDSP's create_panel default is
   copy=0 (no copy: L=I, R=Q).  CRITICAL: EMNR (and possibly other
   spectral stages) ZERO the Q channel on output (`a->out[2i+1]=0`).
   For SSB modes this is fine because the post-EMNR BP1 has an
   asymmetric passband that acts as a Hilbert restorer and brings
   Q back analytically.  For AM/FM/DSB with symmetric passband
   around DC, BP1 doesn't restore Q — so Q stays zero through the
   panel and the operator hears only the LEFT channel.
   SetRXAPanelBinaural(0) sets panel.copy=1 (copy I to Q at panel
   output), giving mono on both channels regardless of upstream
   stage behaviour.  This is what Thetis does at channel init. */
void SetRXAPanelBinaural(int channel, int bin);

/* SSQL — WDSP's all-mode "Single-mode Squelch Level" voice-activity
   detector.  Used by Thetis for SSB / CW / DIG modes (FM has its
   own FMSQ, AM has its own AMSQ).  SSQL is a frequency-to-voltage
   converter feeding a window detector + trigger — works on the
   pre-AGC IQ envelope, so AGC compression doesn't blind it the way
   audio-domain RMS gates get blinded.
   threshold: 0.0..1.0, WU2O-tested default 0.16
   tau_mute / tau_unmute: seconds, 0.1 default both directions */
void SetRXASSQLRun(int channel, int run);
void SetRXASSQLThreshold(int channel, double threshold);
void SetRXASSQLTauMute(int channel, double tau_mute);
void SetRXASSQLTauUnMute(int channel, double tau_unmute);

/* EMNR live-tuning — exposes WDSP's noise-reducer knobs that
   determine sonic character.  WDSP defaults: gain_method=2 (MMSE-LSA),
   npe_method=0, ae_run=1 (AEPF on; reduces musical noise).
   Operator visibility: gain_method gives genuinely different
   character per setting; AEPF on/off changes how aggressive the
   musical-noise smoothing is. */
void SetRXAEMNRgainMethod(int channel, int method);
void SetRXAEMNRnpeMethod(int channel, int method);
void SetRXAEMNRaeRun(int channel, int run);
void SetRXAEMNRaeZetaThresh(int channel, double zetathresh);
void SetRXAEMNRaePsi(int channel, double psi);
void SetRXAEMNRtrainZetaThresh(int channel, double thresh);
void SetRXAEMNRtrainT2(int channel, double t2);

/* ANR (LMS adaptive line enhancer) live-tuning — gain ≡ LMS step
   size (the "mu" / aggression knob); leakage is the leak factor
   (controls stability vs adaptation speed); taps = filter length;
   delay = decorrelation delay.  WDSP defaults: taps=64, delay=16,
   gain=0.0001, leakage=0.1. */
void SetRXAANRVals(int channel, int taps, int delay, double gain, double leakage);
void SetRXAANRTaps(int channel, int taps);
void SetRXAANRDelay(int channel, int delay);
void SetRXAANRGain(int channel, double gain);
void SetRXAANRLeakage(int channel, double leakage);

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

/* ----- rmatch (rmatch.c) ------------------------------------------------ */

/*
 * Adaptive variable-rate resampler with PI control loop, used to bridge
 * the dual-clock drift between Lyra's nominal 48 kHz audio rate and the
 * PC sound card's actual rate.  Lyra previously had a pure-Python port
 * of this module (lyra/dsp/rmatch.py + varsamp.py); v0.0.9.6 wires the
 * native DLL implementation through cffi to recover ~50% of the CPU
 * the PC Soundcard audio path was burning in numpy per-sample loops.
 *
 * Buffer convention: in/out are interleaved I/Q complex doubles, i.e.
 *   in:  2 * insize  doubles, [I0,Q0, I1,Q1, ...]  per xrmatchIN call
 *   out: 2 * outsize doubles, [I0,Q0, I1,Q1, ...]  per xrmatchOUT call
 * For mono audio (Lyra's PC Sound case), feed Q=0 on input and read
 * the I component on output.
 *
 * `create_rmatchV` is the convenience constructor that fills in WDSP's
 * default tuning for ff_alpha / prop gains / ringmins+maxes / R / etc.
 * After creation, fine-tune with the setters.
 */
void* create_rmatchV(int in_size, int out_size, int nom_inrate,
                     int nom_outrate, int ringsize, double var);
void destroy_rmatchV(void* ptr);

void xrmatchIN(void* b, double* in_buf);
void xrmatchOUT(void* b, double* out_buf);

void setRMatchInsize(void* ptr, int insize);
void setRMatchOutsize(void* ptr, int outsize);
void setRMatchNomInrate(void* ptr, int nom_inrate);
void setRMatchNomOutrate(void* ptr, int nom_outrate);
void setRMatchRingsize(void* ptr, int ringsize);

void getRMatchDiags(void* b, int* underflows, int* overflows,
                    double* var, int* ringsize, int* nring);
void resetRMatchDiags(void* b);
void forceRMatchVar(void* b, int force, double fvar);

void setRMatchFeedbackGain(void* b, double feedback_gain);
void setRMatchSlewTime(void* b, double slew_time);
void setRMatchSlewTime1(void* b, double slew_time);
void setRMatchPropRingMin(void* ptr, int prop_min);
void setRMatchPropRingMax(void* ptr, int prop_max);
void setRMatchFFRingMin(void* ptr, int ff_ringmin);
void setRMatchFFRingMax(void* ptr, int ff_ringmax);
void setRMatchFFAlpha(void* ptr, double ff_alpha);
void getControlFlag(void* ptr, int* control_flag);

/* ----- EXT noise blankers (nob.c / nobII.c) ----------------------------- */

/*
 * The EXT noise blankers operate on raw I/Q before the RXA chain.
 * They are NOT created automatically by OpenChannel — calling
 * SetEXTNOBRun / SetEXTANBRun on an uninitialized blanker segfaults
 * the DLL.  Lyra calls create_anbEXT / create_nobEXT once per
 * channel during _open_wdsp_rx.
 *
 *   id              channel index, same space as OpenChannel
 *   run             initial run state (0 = bypass, 1 = active)
 *   mode            (NOB only) 0..3 — see Thetis NB2Mode for meanings
 *   buffsize        same as in_size used for OpenChannel
 *   samplerate      same as in_rate used for OpenChannel
 *   tau / advtime / hangtime / backtau / threshold:
 *     standard ANB / NOB tuning. Defaults that work for HL2-class
 *     impulse noise are listed in wdsp_engine.RxChannel below.
 */
void create_anbEXT(int id, int run, int buffsize, double samplerate,
                   double tau, double hangtime, double advtime,
                   double backtau, double threshold);
void destroy_anbEXT(int id);
void flush_anbEXT(int id);

void create_nobEXT(int id, int run, int mode, int buffsize, double samplerate,
                   double slewtime, double hangtime, double advtime,
                   double backtau, double threshold);
void destroy_nobEXT(int id);
void flush_nobEXT(int id);

/*
 * EXT blankers process the raw IQ in-place outside the RXA chain.
 * SetEXTNOBRun / SetEXTANBRun ONLY set the run flag inside the
 * blanker struct — they do NOT splice the blanker into fexchange0.
 * Lyra has to call xnobEXT / xanbEXT explicitly on the IQ buffer
 * BEFORE handing it to fexchange0; that's where the actual
 * impulse-noise blanking happens.  When run=0, xnobEXT is still
 * safe to call — but the output buffer is left untouched (NOT
 * copied from input), so callers must check the run flag in
 * Python and skip the call if neither blanker is active.
 */
void xnobEXT(int id, double* in_buf, double* out_buf);
void xanbEXT(int id, double* in_buf, double* out_buf);

/* Runtime EXT-blanker setters — used by the operator's NB profile
   slider to nudge threshold without recreating the blanker.  The
   create-side defaults handle most operating conditions; operator
   typically only adjusts threshold from "light" to "heavy" presets. */
void SetEXTNOBTau(int id, double tau);
void SetEXTNOBHangtime(int id, double time);
void SetEXTNOBAdvtime(int id, double time);
void SetEXTNOBBacktau(int id, double tau);
void SetEXTNOBThreshold(int id, double thresh);
void SetEXTNOBMode(int id, int mode);

void SetEXTANBTau(int id, double tau);
void SetEXTANBHangtime(int id, double time);
void SetEXTANBAdvtime(int id, double time);
void SetEXTANBBacktau(int id, double tau);
void SetEXTANBThreshold(int id, double thresh);

/* ----- APF (Audio Peaking Filter, RXA SPEAK stage in iir.c) ------------- */

/*
 * Single-peak resonant boost centered on the operator's CW pitch.
 * Wired into the RXA chain as the "biquad" stage (WDSP terminology
 * — it's actually a higher-order resonator with multiple stages
 * configured via internal nstages).  Used by ham operators who like
 * a louder, narrower boost on the CW tone for easier copy.
 *
 *   freq        center frequency (Hz)
 *   bw          -3 dB bandwidth (Hz)
 *   gain        LINEAR gain multiplier — operator dB converted to
 *               linear via 10^(dB/20) before passing in
 */
void SetRXABiQuadRun(int channel, int run);
void SetRXABiQuadFreq(int channel, double freq);
void SetRXABiQuadBandwidth(int channel, double bw);
void SetRXABiQuadGain(int channel, double gain);

/* ----- Notch database (nbp.c) ------------------------------------------ */

/*
 * RXA notch database: each RX channel owns a notchdb that the front-of-
 * chain bandpass (NBP0) consults.  Manual operator-driven notches map
 * directly onto this database.
 *
 *   channel       RX channel index
 *   notch         notch slot index (0..maxnotches-1)
 *   fcenter       center frequency (Hz, signed — negative for LSB)
 *   fwidth        notch width (Hz, positive)
 *   active        0 = ignore this slot, 1 = apply it
 *
 * Add/Edit/Delete return 0 on success, -1 on error (e.g. bad index).
 * RXANBPSetNotchesRun is the master notch-engine on/off switch — must
 * be 1 for any individual notches to actually attenuate.
 * RXANBPSetTuneFrequency tells the notch engine the current VFO so
 * that absolute notch frequencies map correctly onto baseband.
 */
int  RXANBPAddNotch(int channel, int notch, double fcenter, double fwidth, int active);
int  RXANBPGetNotch(int channel, int notch, double* fcenter, double* fwidth, int* active);
int  RXANBPDeleteNotch(int channel, int notch);
int  RXANBPEditNotch(int channel, int notch, double fcenter, double fwidth, int active);
void RXANBPGetNumNotches(int channel, int* nnotches);
void RXANBPSetNotchesRun(int channel, int run);
void RXANBPSetTuneFrequency(int channel, double tunefreq);
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
