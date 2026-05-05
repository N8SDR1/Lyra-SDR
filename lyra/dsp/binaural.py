"""BIN — Binaural pseudo-stereo for headphone listening.

Takes mono audio and generates a stereo pair where one channel is
the in-phase signal and the other is its 90°-shifted (Hilbert)
counterpart, mixed by a depth parameter. The brain perceives this
as a wider soundstage — weak CW lifts out of the noise more easily,
and SSB voice gains a sense of space ("inside-the-head" effect that
ham operators recognize from other HPSDR-class clients).

DSP form
--------
1. Hilbert FIR — odd-length, antisymmetric impulse response (Type IV
   linear-phase). Built from the textbook truncated `2/(πk)` series,
   Hamming-windowed for sidelobe control. Group delay = (N-1)/2
   samples.
2. Persistent `zi` state across blocks via `scipy.signal.lfilter`
   so streaming is click-free at block boundaries.
3. Original ("real") signal is delayed by the FIR group delay so
   the two channels stay sample-aligned — without that, L and R
   would skew apart and the spatial cue would smear.

Output
------
For depth d ∈ [0, 1]:
    L = delayed_real - d * hilbert_shifted
    R = delayed_real + d * hilbert_shifted
    norm = 1 / sqrt(1 + d²)   # equal-loudness normalization
    L *= norm; R *= norm

At d=0 → L = R = delayed mono (identical channels, no spatial cue).
At d=1 → full Hilbert pair, classic "binaural CW" feel.

The normalization factor keeps perceived loudness constant as the
operator sweeps the depth slider. Without it, depth=1 would sound
~3 dB louder than depth=0 (the orthogonal Hilbert pair adds in
quadrature when summed back to mono in the listener's brain).

Place in chain
--------------
After AGC + AF Gain + Volume + tanh limiter, last stage before the
audio sink. Mono goes in, stereo (N, 2) comes out. The sinks both
accept the stereo shape and apply the operator's L/R balance
gains on the way out.

CPU cost
--------
~63-tap FIR × one sample = 63 multiplies per audio sample. At 48 kHz
that's ~3 MFLOPS — immeasurable.
"""
from __future__ import annotations

import numpy as np


class BinauralFilter:
    """Mono → stereo pseudo-binaural.

    Construct once per Radio. Mutate via setters. `process(audio)`
    returns either:
      - the input untouched (mono, shape (N,)) when disabled
      - a (N, 2) float32 stereo array when enabled

    The caller (Radio) routes the result to a stereo-aware sink.
    """

    # Filter design constants. 63 taps gives ~31-sample group delay
    # (~645 µs at 48 kHz) — imperceptible delay, plenty of stopband
    # rejection for the Hilbert response, cheap to run.
    _N_TAPS: int = 63

    # Operator-facing limits.
    DEPTH_MIN: float = 0.0
    DEPTH_MAX: float = 1.0
    DEPTH_DEFAULT: float = 0.7

    def __init__(
        self,
        sample_rate: int = 48000,
        depth: float = DEPTH_DEFAULT,
    ) -> None:
        self._fs: int = int(sample_rate)
        self._depth: float = self._clamp_depth(float(depth))
        self.enabled: bool = False

        # Hilbert FIR + persistent state. Built once; no parameter
        # currently affects its shape (sample rate is implicit in
        # how the result interacts with downstream listening, but
        # the kernel itself is rate-agnostic).
        self._h: np.ndarray = self._build_hilbert_fir(self._N_TAPS)
        self._zi_h: np.ndarray = np.zeros(
            self._N_TAPS - 1, dtype=np.float64)

        # Delay line for the in-phase ("real") path. Length =
        # group delay so L (delayed real) lines up with R (Hilbert-
        # filtered) sample-for-sample.
        self._delay_n: int = (self._N_TAPS - 1) // 2
        self._delay_buf: np.ndarray = np.zeros(
            self._delay_n, dtype=np.float32)

    # ── Setters ────────────────────────────────────────────────────
    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def set_depth(self, depth: float) -> None:
        d = self._clamp_depth(float(depth))
        if d == self._depth:
            return
        self._depth = d

    def set_sample_rate(self, fs: int) -> None:
        # Hilbert FIR is rate-agnostic; we don't actually need to
        # rebuild on rate change. Left as a setter for symmetry with
        # other DSP classes.
        self._fs = int(fs)

    # ── Read-only views ────────────────────────────────────────────
    @property
    def depth(self) -> float:
        return self._depth

    # ── Audio entry point ──────────────────────────────────────────
    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process a mono float32 block.

        Returns:
          - the input array untouched if disabled or empty (caller
            should handle the (N,) → (N, 2) duplication itself)
          - a (N, 2) float32 stereo array if enabled
        """
        if not self.enabled or audio.size == 0:
            return audio
        # Lazy scipy import — keeps the module loadable in test
        # environments that don't yet have scipy ready.
        from scipy.signal import lfilter

        x = audio.astype(np.float64, copy=False)
        # Hilbert-filtered (90°-shifted) version with persistent state
        shifted, self._zi_h = lfilter(
            self._h, [1.0], x, zi=self._zi_h)
        shifted_f32 = shifted.astype(np.float32, copy=False)

        # Delay the original by the FIR group delay so L and R align.
        # Concatenate the saved tail with the new block, then split.
        n_in = audio.size
        if self._delay_n > 0:
            extended = np.concatenate(
                [self._delay_buf, audio.astype(np.float32, copy=False)])
            delayed = extended[:n_in]
            # Save the last `_delay_n` samples for next call's prefix.
            self._delay_buf = extended[n_in:].astype(
                np.float32, copy=False)
        else:
            delayed = audio.astype(np.float32, copy=False)

        d = self._depth
        # Sum/diff mix — orthogonal Hilbert pair gives the classic
        # binaural feel. d=0 collapses to mono, d=1 is full pair.
        ds = d * shifted_f32
        L = delayed - ds
        R = delayed + ds
        # Equal-loudness normalization so depth doesn't change
        # perceived volume. RMS of L,R = sqrt(1 + d²) * RMS(mono)
        # for orthogonal real/Hilbert components, so we scale by
        # 1/sqrt(1+d²).
        norm = float(1.0 / np.sqrt(1.0 + d * d))
        L = L * norm
        R = R * norm
        return np.stack([L, R], axis=1).astype(np.float32, copy=False)

    def reset(self) -> None:
        """Drop in-flight FIR state + delay line. Called on freq /
        mode change paths where an audio discontinuity is already
        expected so clearing state can't add an audible click."""
        self._zi_h[:] = 0.0
        self._delay_buf[:] = 0.0

    # ── Internals ──────────────────────────────────────────────────
    def _clamp_depth(self, depth: float) -> float:
        if depth < self.DEPTH_MIN:
            return self.DEPTH_MIN
        if depth > self.DEPTH_MAX:
            return self.DEPTH_MAX
        return depth

    @staticmethod
    def _build_hilbert_fir(n_taps: int) -> np.ndarray:
        """Build a textbook Hilbert FIR.

        Truncated 2/(πk) impulse response, antisymmetric, odd-length
        (Type III/IV), Hamming-windowed for sidelobe control. The
        DC and Nyquist bins are exactly zero and the magnitude
        response is approximately flat in between — i.e., a 90°
        phase shifter.
        """
        if n_taps % 2 == 0:
            n_taps += 1   # force odd
        n = (n_taps - 1) // 2
        h = np.zeros(n_taps, dtype=np.float64)
        for k in range(-n, n + 1):
            if k != 0 and k % 2 != 0:
                h[k + n] = 2.0 / (np.pi * k)
        h *= np.hamming(n_taps)
        return h
