"""APF — Audio Peaking Filter for CW.

A narrow peaking biquad centered on the operator's CW pitch. Boosts a
narrow region of the audio band (where the CW tone sits) without the
ringing tail of a brick-wall narrow filter — operators get a louder,
clearer signal and the rest of the passband stays audible for context.

DSP form
--------
Single-section RBJ peaking IIR biquad ("audio EQ cookbook" formula,
Robert Bristow-Johnson, public domain). Coefficients:

    A     = 10^(gain_db / 40)
    w0    = 2π f0 / fs
    cosw  = cos(w0)
    sinw  = sin(w0)
    Q     = f0 / bw_hz
    alpha = sinw / (2 Q)

    b0 =  1 + alpha * A
    b1 = -2 * cosw
    b2 =  1 - alpha * A
    a0 =  1 + alpha / A
    a1 = -2 * cosw
    a2 =  1 - alpha / A

Difference equation, normalized by a0:

    y[n] = (b0/a0) x[n] + (b1/a0) x[n-1] + (b2/a0) x[n-2]
                        - (a1/a0) y[n-1] - (a2/a0) y[n-2]

We use scipy.signal.lfilter with `zi` state so the filter persists
across blocks (no per-block click) and a single-call coefficient
update doesn't stomp the in-flight history (the y[n-1], y[n-2] tail
keeps decaying naturally). Float32 throughout — biquad precision is
fine at this Q on audio.

Place in chain
--------------
After NR (audio domain), before AGC. AGC then chases the boosted
tone, which is the whole point — the operator hears the signal at
target level, not at the original (boosted - boost_gain) level.

Mode-gate
---------
APF only runs in CWU/CWL. The Channel passes audio straight through
in other modes — the operator's setting is preserved (button stays
"on") so re-entering CW restores the prior state, but no DSP is
applied where it wouldn't help.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np


class AudioPeakFilter:
    """RBJ peaking biquad with persistent state.

    Construct once per channel; mutate via setters. `process(audio)`
    returns the filtered block at the same sample rate. `enabled`
    short-circuits the filter (passes input through, does NOT reset
    state) so toggling on/off doesn't introduce transients.
    """

    # Operator-facing limits. Picked to keep the filter usable
    # without drifting into ringing-resonator territory:
    #   - bw_hz < ~30 Hz starts to ring on dits
    #   - gain_db > 18 dB pumps too hard against AGC
    BW_MIN_HZ: int = 30
    BW_MAX_HZ: int = 200
    GAIN_MIN_DB: float = 0.0
    GAIN_MAX_DB: float = 18.0

    # Defaults — modest BW, modest boost. Field-test starting point.
    BW_DEFAULT_HZ: int = 80
    GAIN_DEFAULT_DB: float = 12.0

    def __init__(
        self,
        sample_rate: int,
        center_hz: float = 600.0,
        bw_hz: int = BW_DEFAULT_HZ,
        gain_db: float = GAIN_DEFAULT_DB,
    ) -> None:
        self._fs: int = int(sample_rate)
        self._f0: float = float(center_hz)
        self._bw: int = self._clamp_bw(int(bw_hz))
        self._gain_db: float = self._clamp_gain(float(gain_db))
        self.enabled: bool = False

        # Biquad state — last two input + output samples per channel.
        # scipy.signal.lfilter expects a flat zi vector of length
        # max(len(a), len(b)) - 1 = 2 for a biquad.
        self._zi: np.ndarray = np.zeros(2, dtype=np.float32)

        # Coefficients — recomputed lazily on first process() / setter.
        self._b: np.ndarray = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        self._a: np.ndarray = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        self._recompute()

    # ── Setters ────────────────────────────────────────────────────
    def set_sample_rate(self, fs: int) -> None:
        fs = int(fs)
        if fs == self._fs:
            return
        self._fs = fs
        self._recompute()

    def set_center_hz(self, f0: float) -> None:
        f0 = float(f0)
        if f0 == self._f0:
            return
        self._f0 = f0
        self._recompute()

    def set_bw_hz(self, bw_hz: int) -> None:
        bw = self._clamp_bw(int(bw_hz))
        if bw == self._bw:
            return
        self._bw = bw
        self._recompute()

    def set_gain_db(self, gain_db: float) -> None:
        g = self._clamp_gain(float(gain_db))
        if g == self._gain_db:
            return
        self._gain_db = g
        self._recompute()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    # ── Read-only views ────────────────────────────────────────────
    @property
    def center_hz(self) -> float:
        return self._f0

    @property
    def bw_hz(self) -> int:
        return self._bw

    @property
    def gain_db(self) -> float:
        return self._gain_db

    @property
    def sample_rate(self) -> int:
        return self._fs

    # ── Audio entry point ──────────────────────────────────────────
    def process(self, audio: np.ndarray) -> np.ndarray:
        """Apply the peaking filter to a mono float32 audio block.

        If disabled, returns the input untouched. If enabled but the
        coefficients are degenerate (e.g. f0 outside Nyquist after a
        rate change), also passes through.

        State persists across calls so block boundaries don't click.
        On a settings change, coefficients update but the filter's
        in-flight zi is preserved — that's intentional. With a low-Q
        peaking biquad, swapping coefficients on a populated zi is
        smooth; resetting zi on every change would CLICK on each
        slider step, which is much worse.
        """
        # Diagnostic — v0.0.9.1 APF investigation.  Operator-
        # reported: APF on/off in CW + AGC-off produces no audible
        # difference.  Print rate-limited stats so we can confirm
        # the filter is actually running, see input/output magnitudes
        # to verify the boost is happening, and catch any silent
        # bypass case (degenerate coefficients, zero input, etc.).
        # REMOVE after diagnosis is complete.
        import os as _os
        if _os.environ.get("LYRA_APF_DEBUG"):
            self._dbg_apf_call_count = getattr(
                self, "_dbg_apf_call_count", 0) + 1
            # Print every 50th call ≈ once per second at typical
            # 48 kHz audio block cadence.  Not too spammy, frequent
            # enough to see real-time changes.
            if self._dbg_apf_call_count % 50 == 0:
                in_max = float(np.max(np.abs(audio))) if audio.size else 0.0
                in_rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if audio.size else 0.0
                print(
                    f"[APF] enabled={self.enabled} "
                    f"f0={self._f0:.0f}Hz bw={self._bw}Hz "
                    f"gain={self._gain_db:.1f}dB "
                    f"fs={self._fs} "
                    f"audio.size={audio.size} "
                    f"in_max={in_max:.4f} in_rms={in_rms:.4f} "
                    f"b0={self._b[0]:.4f} a0=1.0000")
        if not self.enabled or audio.size == 0:
            return audio
        # Defensive: avoid filtering with degenerate coefficients
        # (e.g. f0 == 0, or fs missing). Cheap check.
        if self._f0 <= 0.0 or self._fs <= 0:
            return audio
        # Use scipy.signal.lfilter for the biquad. Imported lazily so
        # the module can be loaded in environments that don't yet have
        # scipy ready (avoids a hard-fail at import time).
        from scipy.signal import lfilter

        # lfilter expects float64 for stable accumulation; cast back
        # to float32 on output to match the rest of the audio chain.
        out, self._zi = lfilter(
            self._b.astype(np.float64),
            self._a.astype(np.float64),
            audio.astype(np.float64, copy=False),
            zi=self._zi.astype(np.float64),
        )
        # Persist the float32 view of the new state so the next call
        # consumes/produces float32 throughout.
        self._zi = self._zi.astype(np.float32, copy=False)
        out_f32 = out.astype(np.float32, copy=False)

        # Continuation of the diagnostic — print output stats so we
        # can compare in vs out and confirm the filter is producing
        # the expected boost at the configured center frequency.
        if _os.environ.get("LYRA_APF_DEBUG") and \
                self._dbg_apf_call_count % 50 == 0:
            out_max = float(np.max(np.abs(out_f32))) if out_f32.size else 0.0
            out_rms = float(np.sqrt(np.mean(out_f32.astype(np.float64) ** 2))) if out_f32.size else 0.0
            ratio_db = (
                20.0 * np.log10(out_rms / max(in_rms, 1e-12))
                if in_rms > 0 else 0.0)
            print(
                f"[APF]    -> out_max={out_max:.4f} out_rms={out_rms:.4f} "
                f"out/in_ratio={ratio_db:+.2f}dB")

        return out_f32

    def reset(self) -> None:
        """Drop the filter's in-flight state. Called on freq/mode
        change paths where a discontinuity in the audio is already
        expected — at that point it's safe to clear zi without an
        audible click."""
        self._zi[:] = 0.0

    # ── Internals ──────────────────────────────────────────────────
    def _clamp_bw(self, bw: int) -> int:
        if bw < self.BW_MIN_HZ:
            return self.BW_MIN_HZ
        if bw > self.BW_MAX_HZ:
            return self.BW_MAX_HZ
        return bw

    def _clamp_gain(self, gain_db: float) -> float:
        if gain_db < self.GAIN_MIN_DB:
            return self.GAIN_MIN_DB
        if gain_db > self.GAIN_MAX_DB:
            return self.GAIN_MAX_DB
        return gain_db

    def _recompute(self) -> None:
        """Recompute biquad coefficients from current parameters.
        Defensive against degenerate inputs — sets a unity-gain
        passthrough if the math would blow up."""
        fs = self._fs
        f0 = self._f0
        bw = self._bw
        # f0 must sit strictly inside the Nyquist window; fall back
        # to passthrough otherwise (operator may be mid-rate-change).
        if fs <= 0 or f0 <= 0.0 or f0 >= fs / 2.0 or bw <= 0:
            self._b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            self._a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            return
        A = 10.0 ** (self._gain_db / 40.0)
        w0 = 2.0 * math.pi * f0 / fs
        cosw = math.cos(w0)
        sinw = math.sin(w0)
        # Q derived from BW. Higher pitch + same BW = higher Q (sharper),
        # which is fine because higher pitches naturally tolerate it.
        Q = max(0.1, f0 / float(bw))
        alpha = sinw / (2.0 * Q)
        b0 = 1.0 + alpha * A
        b1 = -2.0 * cosw
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cosw
        a2 = 1.0 - alpha / A
        # Normalize by a0 — lfilter will too, but normalizing here lets
        # us inspect "transparent" coefficients in unit tests.
        self._b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float32)
        self._a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float32)
