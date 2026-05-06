"""Variable-rate polyphase resampler — port of WDSP varsamp.c (v0.0.9.6).

Direct port of Warren Pratt's variable-ratio polyphase resampler from
WDSP, used as the DSP primitive underneath :mod:`lyra.dsp.rmatch`'s
adaptive PI control loop.  Together they bridge the clock-drift gap
between Lyra's IQ output rate and the PC sound card's actual rate
(the "two-crystal drift" that's been producing operator-reported
audio glitches on PC Soundcard mode).

WDSP attribution
----------------
Direct port of:

    D:\\sdrprojects\\OpenHPSDR-Thetis-2.10.3.13\\Project Files\\Source\\
    wdsp\\varsamp.c

Original copyright (C) 2017 Warren Pratt, NR0V.  GPL v2+, used under
GPL v3+ relicense per ``docs/architecture/wdsp_integration.md``.

Algorithm overview
------------------
A polyphase low-pass anti-alias filter run at a continuously-tunable
fractional rate.

* The prototype FIR filter ``h`` is designed once at construction with
  ``R`` interpolated taps per nominal-input-sample (``R`` ≈ 256 — a
  density factor; higher ``R`` = finer ratio resolution at the cost
  of init memory).
* On each output sample, the working filter ``hs`` is computed by
  linearly interpolating between adjacent prototype taps at the
  current fractional position ``h_offset``.
* ``h_offset`` accumulates ``delta = 1.0 - inv_cvar`` per output
  sample, where ``cvar = var * nom_ratio`` is the *current* output/
  input rate ratio.  Wraps modulo 1.0.
* Output samples are emitted whenever the per-input-sample fractional
  counter ``isamps`` reaches 1.0.  This produces a near-constant
  output rate that tracks the operator's ``var`` multiplier.

Per-call ``var`` parameter (typically supplied by the rmatch PI
control loop) lets the caller continuously retune the ratio.  When
``varmode=1``, varsamp linearly interpolates from the previous
``inv_cvar`` to the new one across the whole input buffer — smooth
ratio transition rather than a step change.

Differences from the C original
-------------------------------
* C used ``uint64_t`` bit-truncation tricks (``& 0xffff_ffff_ffff_0000``)
  to avoid floating-point drift accumulating in ``inv_cvar``.  Python
  doesn't have that exact pattern; we use ``np.float64`` throughout
  and accept the ~1e-15 round-off — well below the audio-band
  resolution we care about.
* C treats input/output as interleaved I/Q ``double[2*size]``.  We
  accept either real-only ``np.float32`` or complex ``np.complex64``
  — both common in the Lyra audio chain.  The internal ring is
  always complex internally (matches the C structure) but we project
  to real on output if the input was real.
* FIR design uses ``scipy.signal.firwin`` rather than porting WDSP's
  ``fir_bandpass``.  ``firwin`` is a standard utility (Hamming-windowed
  ideal low-pass), well-trodden, gives equivalent passband + stopband
  characteristics for our purposes.

Tests
-----
See ``scripts/diag_varsamp_resample.py`` for the bench instrument
that drives synthetic sine waves through known ratio changes and
measures output frequency / spectral cleanliness against expectation.
"""
from __future__ import annotations

import math
import struct
from typing import Optional

import numpy as np


# ── inv_cvar bit-truncation helper ──────────────────────────────────
#
# WDSP varsamp.c lines 151-153 do this every input-sample iteration:
#   picvar = (uint64_t*)(&inv_cvar);
#   N = *picvar & 0xffffffffffff0000;
#   inv_cvar = *((double *)&N);
#
# Zeros the LOW 16 BITS of the IEEE-754 double's mantissa.  This is
# NOT a noise-reduction trick — it's deterministic quantization to
# ~48-bit precision.  Without it the inv_cvar accumulator drifts by
# ULP-scale amounts each iteration, modulating the FIR-tap
# interpolation phase by ~2^-52 random jitter.  That jitter is
# broadband and modulates the output filter shape — produces
# continuous low-level "color" on broadband content (the operator-
# reported "thin/brittle" symptom on PC Soundcard mode).
#
# Single-tone bench tests don't catch this because steady tones
# don't exercise the broadband filter shape.

_INV_CVAR_MANTISSA_MASK = 0xFFFF_FFFF_FFFF_0000


def _truncate_inv_cvar(value: float) -> float:
    """Apply WDSP's bit-mask quantization to a float64."""
    packed = struct.pack("<d", float(value))
    bits = struct.unpack("<Q", packed)[0]
    bits &= _INV_CVAR_MANTISSA_MASK
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


class VarSamp:
    """Variable-ratio polyphase resampler.

    Holds the prototype FIR coefficients, ring buffer, and per-output-
    sample fractional-offset state.  Caller drives by feeding input
    blocks via :meth:`process`, each call returning a (possibly
    different-length) output block.

    Construction is moderately expensive (designs the FIR filter)
    so prefer creating once and reusing across stream lifetime.
    Use :meth:`set_rates` to retune nominal rates without throwing
    away streaming state.
    """

    def __init__(
        self,
        in_rate: int,
        out_rate: int,
        *,
        density: int = 256,
        fc: float = 0.0,
        fc_low: float = -1.0,
        gain: float = 1.0,
        initial_var: float = 1.0,
        varmode: int = 1,
    ) -> None:
        """Construct a VarSamp.

        Args:
            in_rate: nominal input sample rate (Hz).
            out_rate: nominal output sample rate (Hz).  Together with
                in_rate sets ``nom_ratio = out_rate / in_rate``.
            density: FIR density factor (the WDSP ``R``).  256 is the
                Thetis default and gives ~12-bit fractional ratio
                resolution.  Lower values cost less memory; higher
                values cost more.  Range [16, 1024] reasonable.
            fc: anti-alias high cutoff (Hz).  Default 0.0 means
                "auto" — picks 0.95 × 0.45 × min(in_rate, out_rate).
            fc_low: low cutoff (Hz).  Negative means "mirror of fc"
                (standard real-valued low-pass).  Positive values
                produce a band-pass for the I/Q complex case.
                Default -1.0 (low-pass).
            gain: DC gain applied via FIR coefficients.  Default 1.0.
            initial_var: starting value of the rate multiplier.  1.0
                means "exactly nominal ratio."  rmatch will adjust
                this per-block via the PI loop, typical operating
                range [0.96, 1.04].
            varmode: 0 = use ``var`` constant across each input
                block; 1 = interpolate ``inv_cvar`` linearly from the
                previous ``var`` to the new ``var`` across the
                input block (smooth ratio transitions).  Default 1.
        """
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.R = max(16, min(1024, int(density)))
        self.fcin = float(fc)
        self.fc_low = float(fc_low)
        self.gain = float(gain)
        self.var = float(initial_var)
        self.varmode = int(varmode) & 1

        # Computed in _calc_filter:
        self.nom_ratio: float = 0.0
        self.cvar: float = 0.0
        self.inv_cvar: float = 0.0
        self.old_inv_cvar: float = 0.0
        self.dicvar: float = 0.0
        self.delta: float = 0.0
        self.fc: float = 0.0
        self.rsize: int = 0
        self.ncoef: int = 0
        self.h: Optional[np.ndarray] = None      # prototype FIR
        self.hs: Optional[np.ndarray] = None     # per-frame shifted FIR
        self.ring: Optional[np.ndarray] = None   # complex ring buffer
        self.idx_in: int = 0
        self.h_offset: float = 0.0
        self.isamps: float = 0.0

        self._calc_filter()

    # ── Filter design + state setup ──────────────────────────────

    def _calc_filter(self) -> None:
        """Compute nominal ratio, ring size, FIR prototype.

        Lifted from ``varsamp.c::calc_varsamp`` lines 29-69.  The key
        choices are:

        * ``rsize = 140 * norm_rate / min_rate`` rounded.  This is
          WDSP's empirical ring length tuned for ham audio bandwidths
          — long enough that the FIR spans the audible band cleanly.
        * ``ncoef = R * rsize + 1`` — the prototype filter is a
          dense low-pass with R polyphase branches.
        * Cutoff defaults: high = 0.95 × 0.45 × min_rate; low =
          -high (mirror, real low-pass).  WDSP's "0.95 × 0.45"
          empirically chosen to leave a small guard band against
          aliasing while preserving the upper voice formants.
        """
        self.nom_ratio = float(self.out_rate) / float(self.in_rate)
        self.cvar = self.var * self.nom_ratio
        self.inv_cvar = 1.0 / self.cvar
        self.old_inv_cvar = self.inv_cvar
        self.dicvar = 0.0
        self.delta = abs(1.0 / self.cvar - 1.0)
        self.fc = self.fcin

        if self.out_rate >= self.in_rate:
            min_rate = float(self.in_rate)
            norm_rate = min_rate
        else:
            min_rate = float(self.out_rate)
            norm_rate = float(self.in_rate)

        if self.fc == 0.0:
            self.fc = 0.95 * 0.45 * min_rate

        fc_norm_high = self.fc / norm_rate
        if self.fc_low < 0.0:
            fc_norm_low = -fc_norm_high
        else:
            fc_norm_low = self.fc_low / norm_rate

        # WDSP's rsize formula.  Round (not floor) for stability —
        # the C uses int truncation which is fine since norm_rate /
        # min_rate is always >= 1.0.
        self.rsize = int(140.0 * norm_rate / min_rate)
        self.ncoef = self.rsize + 1
        self.ncoef += (self.R - 1) * (self.ncoef - 1)

        # FIR prototype.  WDSP uses its own fir_bandpass; we use
        # scipy.signal.firwin (Hamming-windowed sinc, equivalent
        # for a low-pass).  fc_norm_low symmetric → real low-pass.
        try:
            from scipy.signal import firwin
        except ImportError as exc:
            raise ImportError(
                "lyra.dsp.varsamp requires scipy for FIR design "
                "(scipy.signal.firwin).  Install scipy: pip install "
                "scipy") from exc

        # cutoff = high cutoff in normalized [0, 1] = (cycles/sample)/
        # (R*norm_rate/2).  After R-fold expansion the prototype runs
        # at R*norm_rate; cutoff there is fc_norm_high / R.
        cutoff_norm = fc_norm_high / float(self.R)
        # Clamp cutoff to a safe range — at very high or very low
        # ratios the cutoff might wander out of [0, 0.5).
        cutoff_norm = max(0.001, min(0.499, cutoff_norm))
        # FIR window: Blackman-Harris matches WDSP's fir_bandpass
        # default (~-92 dB stopband) and gives substantially less
        # aliasing-induced coloration than Hamming (~-43 dB stopband).
        # The audit identified Hamming as a contributor to the
        # operator-reported "colored / brittle / thin" sound.  No
        # transition-band penalty at our ncoef (R*rsize+1, typically
        # 8000-18000 taps) — Blackman-Harris's wider main-lobe is
        # absorbed by the long FIR.
        # firwin returns coefficients normalized for unity gain at DC;
        # multiply by R*gain to match WDSP's convention.
        h = firwin(self.ncoef, cutoff_norm * 2.0, window="blackmanharris")
        h = (h * float(self.R) * self.gain).astype(np.float64)
        self.h = h
        self.hs = np.zeros(self.rsize, dtype=np.float64)

        # Ring buffer is complex (I/Q) per WDSP convention.  Even when
        # the audio is real (mono RX) we keep the structure consistent
        # so the same code path serves both.
        self.ring = np.zeros(self.rsize, dtype=np.complex128)
        self.idx_in = self.rsize - 1
        self.h_offset = 0.0
        self.isamps = 0.0

    def _hshift(self) -> None:
        """Compute the per-output-sample shifted FIR ``hs`` from the
        prototype ``h``.

        Direct port of ``varsamp.c::hshift`` lines 114-124.

        Maps the current ``h_offset`` (fractional, [0, 1)) to a tap
        position in the R-fold prototype ``h``, then linearly
        interpolates between the two nearest taps to fill ``hs``.
        Linear interpolation between R-fold dense taps gives
        approximately ``log2(R)`` bits of fractional-rate accuracy —
        at R=256 that's 8 bits, plenty for sub-ppm rate matching.

        BUG FIX v0.0.9.6 dev cycle: original Python port assigned
        hs[0] = h[hidx], hs[rsize-1] = h[hidx + (rsize-1)*R].
        WDSP's C does the OPPOSITE: hs[rsize-1] = h[hidx],
        hs[0] = h[hidx + (rsize-1)*R].  C's loop is

            for i in [rsize-1..0], j = hidx + (rsize-1-i)*R, k = j+1
                hs[i] = h[j] + frac * (h[k] - h[j])

        which makes hs[i] decrease in j as i decreases — i.e., hs
        is FILLED IN REVERSE.  My original port produced an array
        TIME-REVERSED relative to C.  For a symmetric prototype
        (Hamming firwin output), magnitudes are similar but the
        convolution effectively runs filter time-reversed —
        creates phase distortion, pre-ringing on transients, and
        the "colored / brittle / thin" coloration operator
        reported on PC Soundcard mode in v0.0.9.6 testing.

        Single-tone bench tests didn't catch this because steady
        tones don't exercise transient/phase response.
        """
        pos = float(self.R) * self.h_offset
        hidx = int(pos)
        frac = pos - float(hidx)
        rsize = self.rsize
        # Match C's hs filling: hs[i] = h[hidx + (rsize-1-i)*R]
        # for i in [0, rsize-1].  For i=0: hs[0] = h[hidx + (rsize-1)*R].
        # For i=rsize-1: hs[rsize-1] = h[hidx].
        i_seq = np.arange(rsize, dtype=np.int64)
        j_idx = hidx + (rsize - 1 - i_seq) * self.R
        k_idx = j_idx + 1
        # h is length ncoef = R * rsize + 1; max k for i=0 is
        # hidx + (rsize-1)*R + 1 ≤ R + (rsize-1)*R = R*rsize = ncoef - 1.
        self.hs[:] = self.h[j_idx] + frac * (self.h[k_idx] - self.h[j_idx])

    # ── Public API ───────────────────────────────────────────────

    def reset(self) -> None:
        """Clear the streaming state so the next ``process`` call
        starts fresh.  Filter coefficients (which are init-time-
        bound) are preserved.  Use this on audio discontinuities
        (rate change, mode change, stream restart)."""
        if self.ring is not None:
            self.ring[:] = 0.0
        self.idx_in = self.rsize - 1
        self.h_offset = 0.0
        self.isamps = 0.0

    def set_rates(self, in_rate: int, out_rate: int) -> None:
        """Re-design the filter for new nominal rates.  Drops streaming
        state (rsize / FIR / ring all change)."""
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._calc_filter()

    def process(
        self, audio_in: np.ndarray, var: Optional[float] = None
    ) -> np.ndarray:
        """Resample one input block.  Returns the output block.

        Direct port of ``varsamp.c::xvarsamp`` lines 126-181.

        Args:
            audio_in: input block.  Either ``np.float32`` (real,
                shape ``(N,)``) or ``np.complex64``/``complex128``
                (shape ``(N,)``).  Real input is treated as I-only
                with Q=0 internally and projected back to real on
                output.
            var: rate multiplier for this block.  If None, uses the
                current ``self.var`` (last value set).  Typical
                operating range [0.96, 1.04] from rmatch's PI loop.

        Returns:
            Output block, same dtype as ``audio_in``.  Length is
            approximately ``len(audio_in) * var * nom_ratio`` but
            varies block-to-block by at most ±1 sample due to the
            fractional accumulator.
        """
        if audio_in.size == 0:
            return audio_in[:0].copy()

        if var is not None:
            self.old_inv_cvar = self.inv_cvar
            self.var = float(var)
            self.cvar = self.var * self.nom_ratio
            self.inv_cvar = 1.0 / self.cvar

        # When varmode=1, interpolate inv_cvar from old to new across
        # the input block — smooth ratio transitions, no zipper noise.
        if self.varmode and var is not None:
            self.dicvar = ((self.inv_cvar - self.old_inv_cvar)
                           / float(audio_in.size))
            self.inv_cvar = self.old_inv_cvar
        else:
            self.dicvar = 0.0

        # Detect input form.  We work internally in complex128 ring
        # but project back to the input dtype on output.
        is_real = not np.iscomplexobj(audio_in)
        if is_real:
            in_complex = audio_in.astype(np.float64) + 0.0j
        else:
            in_complex = audio_in.astype(np.complex128, copy=False)

        # Output buffer — allocate generously.  Worst case at var=1.04
        # and nom_ratio=1.0 gives output ≈ 1.04 * input + 1; we use
        # 1.2x as a safe upper bound.
        max_out = int(audio_in.size * self.cvar * 1.2 + 16)
        out = np.zeros(max_out, dtype=np.complex128)
        outsamps = 0

        ring = self.ring
        hs = self.hs
        rsize = self.rsize
        idx_in = self.idx_in
        inv_cvar = self.inv_cvar
        dicvar = self.dicvar
        h_offset = self.h_offset
        isamps = self.isamps

        for i in range(audio_in.size):
            # Write input sample to ring at idx_in.
            ring[idx_in] = in_complex[i]

            # Update inv_cvar (per-input-sample drift if varmode=1).
            inv_cvar += dicvar
            # BUG FIX (v0.0.9.6 2026-05-06 audit): apply WDSP's
            # bit-mask quantization to inv_cvar.  varsamp.c:151-153
            # zero the low 16 mantissa bits each iteration —
            # deterministic ~48-bit precision, NOT noise reduction.
            # Without it, ULP jitter on delta modulates FIR-tap
            # phase and produces audible "thin/brittle" coloration
            # on broadband content.  Single-tone bench tests
            # didn't catch this because steady tones don't
            # exercise broadband filter shape.
            inv_cvar = _truncate_inv_cvar(inv_cvar)
            delta = 1.0 - inv_cvar

            # Emit zero or more output samples while the fractional
            # counter is below 1.0.
            while isamps < 1.0:
                # BUG FIX (v0.0.9.6 2026-05-06 audit): WDSP's
                # ordering is hshift FIRST (using current h_offset),
                # THEN advance h_offset.  My original Python
                # advanced h_offset first, producing a half-sample
                # phase shift + buffer-boundary discontinuity at
                # ~23 Hz (insize=2048 / 48k = 23 Hz block rate)
                # that manifested as low-rate flutter coloration.
                # See varsamp.c:159-162 — the order matters.

                # Compute working FIR hs from prototype h at the
                # CURRENT h_offset (vectorized hshift).  This
                # filter design will be applied to the ring on the
                # next line.
                self.h_offset = h_offset
                self._hshift()

                # Convolve hs with ring (rotated to start at idx_in).
                # WDSP loops over j with (idx_in + j) mod rsize.
                # NumPy vectorizes via two-piece indexing.
                if idx_in == 0:
                    out_val = np.dot(hs, ring)
                else:
                    out_val = (np.dot(hs[:rsize - idx_in], ring[idx_in:])
                               + np.dot(hs[rsize - idx_in:], ring[:idx_in]))

                # AFTER computing this output sample, advance
                # h_offset for the NEXT output sample's hshift.
                h_offset += delta
                while h_offset >= 1.0:
                    h_offset -= 1.0
                while h_offset < 0.0:
                    h_offset += 1.0

                if outsamps < max_out:
                    out[outsamps] = out_val
                    outsamps += 1
                else:
                    # Should never hit this with the 1.2x sizing —
                    # defensive only.
                    break
                isamps += inv_cvar

            isamps -= 1.0
            idx_in -= 1
            if idx_in < 0:
                idx_in = rsize - 1

        # Persist state for next call.
        self.idx_in = idx_in
        self.inv_cvar = inv_cvar
        self.dicvar = dicvar
        self.h_offset = h_offset
        self.isamps = isamps

        # Trim and project to input dtype.
        out = out[:outsamps]
        if is_real:
            out_real = out.real.astype(audio_in.dtype, copy=False)
            return out_real
        else:
            return out.astype(audio_in.dtype, copy=False)
