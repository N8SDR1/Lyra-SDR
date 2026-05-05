"""Demodulation primitives — stateful FIR-based, artifact-free.

Using scipy.signal.lfilter with maintained state across blocks to avoid
the FFT block-edge artifacts (motorboating/ticking) that a naive
block-by-block FFT filter produces.

Sideband convention: on this HL2, positive baseband frequencies
correspond to what users hear as LSB (the spectrum is effectively
mirrored relative to the tuned frequency — likely a gateware or I/Q
decode artifact). The demod classes apply the empirically-correct sign
so the "USB"/"LSB" mode labels match operator expectations.
"""
from __future__ import annotations

import numpy as np

try:
    from scipy.signal import firwin, lfilter
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False


class SSBDemod:
    """Single-sideband demodulation (USB or LSB) with complex FIR bandpass.

    `low_hz` and `high_hz` control the audio passband.  The filter is
    sharper when taps is larger, but taps should stay odd for symmetry.
    """

    def __init__(self, rate: int, mode: str = "USB",
                 low_hz: float = 300.0, high_hz: float = 2700.0,
                 taps: int = 255):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy is required; run: pip install scipy")
        self.rate = rate
        self.mode = mode
        f_center = (low_hz + high_hz) / 2.0
        half_bw = (high_hz - low_hz) / 2.0
        lpf = firwin(taps, half_bw, fs=rate, window="hann")
        n = np.arange(taps) - (taps - 1) / 2.0
        # HL2 baseband spectrum is mirrored relative to the standard
        # convention: USB RF signals land in NEGATIVE baseband freqs on
        # this gateware. Confirmed empirically on 40m FT8 (N8SDR 2026-04-21):
        # user had to select "LSB" in prior code to hear USB-transmitted FT8.
        # A bandpass centered at -f_center is built via lpf * exp(-j*ω*n).
        sign = -1.0 if mode == "USB" else +1.0
        phasor = np.exp(sign * 1j * 2 * np.pi * f_center * n / rate)
        self.coeffs = (lpf * phasor).astype(np.complex64)
        self.state = np.zeros(taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        out, self.state = lfilter(self.coeffs, 1.0, iq, zi=self.state)
        # Factor 2 compensates for keeping only one sideband
        return (np.real(out) * 2.0).astype(np.float32)


class CWDemod:
    """CW — narrow complex bandpass CENTERED on ±cw_pitch from carrier.

    Standard amateur-radio CW architecture, matching common HPSDR-class
    clients and rigs (SmartSDR / FT-991 / IC-7300):

    - The filter sits at +pitch (CWU) or -pitch (CWL) baseband, with
      width = BW.
    - The operator zero-beats by tuning so the signal lands INSIDE that
      offset window. Click-to-tune drops the marker pitch Hz away from
      the clicked signal (handled in panels._on_click).
    - real() of the complex bandpass output yields a real audio tone
      at the pitch frequency directly — no separate BFO needed.
    - The visible gap between marker and passband on the panadapter
      IS the zero-beat indicator. Tune until the signal sits inside
      the offset rectangle and you're on-frequency at the chosen pitch.

    Independent of BW: works correctly for narrow contest filters
    (250 / 300 / 500 Hz) regardless of pitch setting. Operator can
    have BW=300 with pitch=650 and the filter sits at 500..800 Hz —
    the signal is heard at 650 Hz with 300 Hz of selectivity around it.

    Defaults: 650 Hz pitch, 250 Hz bandwidth.
    """

    def __init__(self, rate: int, pitch_hz: float = 650.0,
                 bw_hz: float = 250.0, taps: int = 513,
                 sideband: str = "U"):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.pitch_hz = float(pitch_hz)
        self.bw_hz = float(bw_hz)
        self.taps = int(taps)
        self.sideband = sideband
        self._build_filter()
        self.state = np.zeros(self.taps - 1, dtype=np.complex64)

    def _build_filter(self) -> None:
        """Complex bandpass = real LPF at bw/2, frequency-shifted by
        ±pitch via multiplication with a complex exponential.
        CWU → +pitch (signal at +pitch baseband on HL2 gateware
        for an upper-sideband CW signal). CWL → -pitch."""
        cutoff = max(50.0, self.bw_hz / 2.0)
        proto = firwin(self.taps, cutoff, fs=self.rate,
                       window="hann").astype(np.float64)
        # HL2 baseband mirror: USB RF signals land at NEGATIVE baseband
        # on this gateware (same convention SSBDemod handles). CWU
        # therefore wants the filter at -pitch baseband, CWL at +pitch.
        sign = -1.0 if self.sideband.upper().startswith("U") else +1.0
        n = np.arange(self.taps)
        shift = np.exp(1j * 2.0 * np.pi * sign * self.pitch_hz * n / self.rate)
        self.lpf = (proto * shift).astype(np.complex64)

    def set_pitch_hz(self, pitch_hz: float) -> None:
        """Update pitch live and rebuild the bandpass filter at the
        new center frequency. Brief click on this rare operator
        action is acceptable."""
        self.pitch_hz = float(pitch_hz)
        self._build_filter()
        self.state = np.zeros(self.taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        # Complex bandpass at ±pitch; real() of output is audio at pitch.
        filt, self.state = lfilter(self.lpf, 1.0, iq, zi=self.state)
        return np.real(filt).astype(np.float32)


class DSBDemod:
    """Double-sideband suppressed-carrier AM.

    Real part of a bandpass-filtered I/Q gives both sidebands summed.
    Requires a carrier to be present at DC (baseband); if carrier is
    absent, use SAM or carrier-restore AM modes instead.
    """

    def __init__(self, rate: int, bw_hz: float = 5000.0, taps: int = 255):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.lpf = firwin(taps, bw_hz / 2.0, fs=rate,
                          window="hann").astype(np.float64)
        self.state = np.zeros(taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        filt, self.state = lfilter(self.lpf, 1.0, iq, zi=self.state)
        return np.real(filt).astype(np.float32) * 2.0


class FMDemod:
    """Narrow-band FM via phase discriminator.

    audio(t) ∝ arg( iq(t) * conj(iq(t-1)) )
    Followed by de-emphasis LPF. Default deviation 5 kHz (typical NBFM
    on 10 m / 2 m repeaters in HF ranges where HL2 operates).
    """

    def __init__(self, rate: int, deviation_hz: float = 5000.0,
                 audio_bw_hz: float = 3000.0, taps: int = 129):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.deviation = deviation_hz
        self.lpf = firwin(taps, audio_bw_hz, fs=rate,
                          window="hann").astype(np.float64)
        self.state = np.zeros(taps - 1, dtype=np.float32)
        self._prev = np.complex64(1 + 0j)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        # Shift by one sample across block boundary
        shifted = np.empty_like(iq)
        shifted[0] = self._prev
        shifted[1:] = iq[:-1]
        self._prev = iq[-1]
        # Phase difference → instantaneous frequency
        disc = np.angle(iq * np.conj(shifted))
        # Scale so ±deviation maps to ±1.0
        audio_raw = (disc * self.rate / (2 * np.pi * self.deviation)).astype(np.float32)
        # De-emphasis LPF
        filtered, self.state = lfilter(self.lpf, 1.0, audio_raw, zi=self.state)
        return filtered.astype(np.float32)


class NotchFilter:
    """Stateful IIR notch — removes a narrow band of frequencies from
    complex I/Q before demod. Applied real-valued to I and Q
    separately so the notch is symmetric around DC (perfect for
    killing a carrier or CW interference near baseband).

    v0.0.7.1 notch v2 rewrite.  Two big changes from the original:

    1. **Parametric peaking-EQ biquad with operator-set depth.**
       Replaces ``scipy.signal.iirnotch`` (which produced -infinity
       only at the exact center but only -3 dB at the visible width
       edges) with the RBJ Audio EQ Cookbook peaking-EQ biquad
       running with negative gain.  Operator picks the depth they
       want and gets it everywhere inside the kill region.  See
       ``docs/architecture/notch_v2_design.md`` §2.4 for the math.

    2. **Cascade integer instead of "deep" boolean.**  Multiple
       biquad stages with independent state, all sharing the same
       coefficients, distribute the total depth across N stages.
       More stages = sharper transition shoulders for the same
       total center depth.  Default cascade=2 (matches the
       operator's expectation from the old deep=True path).

    3. **True two-filter crossfade on coefficient swap.**  When
       operator drags the frequency / width / depth / cascade of an
       existing notch, the old filter and new filter both run for
       the next 5 ms (240 samples at 48 kHz) and their outputs are
       linearly mixed.  After the crossfade window, the old filter
       is dropped.  This removes the "tick on every drag step"
       artifact the original code had.

    Parameters:
      rate         : sample rate in Hz (caller is expected to use
                     48000 — Lyra runs notches at audio rate after
                     IQ decimation).
      freq_hz      : notch center frequency in Hz (always positive;
                     mapped to the absolute baseband offset by the
                     caller).
      width_hz     : -3 dB-from-peak bandwidth in Hz.  Q is derived
                     internally as ``freq_hz / width_hz``.
      depth_db     : notch attenuation at ``freq_hz``, in dB
                     (negative).  Default -50.  Slider range
                     -20 to -80 in the operator UI.
      cascade      : number of biquad stages (1-4).  Each stage gets
                     ``depth_db / cascade`` so the total at
                     ``freq_hz`` is exactly ``depth_db``.  Default 2.
      dc_blocker   : if True, use a 4th-order Butterworth high-pass
                     instead of the peaking-EQ design.  For notches
                     placed at / near DC where the peaking-EQ math
                     degenerates (sin(w0) → 0).  In that mode
                     ``depth_db`` and ``cascade`` are ignored.

    Either way, the rendered "notch region" on the spectrum spans
    ``freq_hz ± width_hz/2``.
    """

    # Coefficient-swap crossfade window — 5 ms at 48 kHz.  Long
    # enough to mask the IIR transient that comes from feeding
    # post-old-coeffs state into new coefficients; short enough to
    # feel responsive during a slider drag.
    CROSSFADE_SAMPLES: int = 240

    # Stability guard: clamp Q to keep IIR poles safely inside the
    # unit circle in float32.  At Q=100 the poles sit at radius
    # ~1 - π/(rate*Q), well clear of the unit circle.  See the design
    # doc §3.1 for the analysis.  width_hz is clamped from below to
    # max(rate/(2*Q_MAX), 1.0 Hz) inside _design_peaking_biquad.
    Q_MAX: float = 100.0

    def __init__(self, rate: int, freq_hz: float, width_hz: float,
                 dc_blocker: bool = False,
                 depth_db: float = -50.0,
                 cascade: int = 2,
                 deep: bool | None = None):
        """Build a stateful notch.

        ``deep`` is accepted for backward compat with old call sites:
        if explicitly passed, it overrides ``cascade`` (deep=True →
        cascade=2, deep=False → cascade=1).  New code should pass
        ``cascade`` directly.
        """
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = int(rate)
        self.freq_hz = float(freq_hz)
        self.width_hz = float(width_hz)
        self.dc_blocker = bool(dc_blocker)
        if deep is not None:
            cascade = 2 if deep else 1
        self.cascade = max(1, min(4, int(cascade)))
        self.depth_db = float(depth_db)

        # Coefficients + state come from a private helper so we can
        # rebuild on update_coeffs() without duplicating the design
        # logic.
        self.b, self.a, self.states = self._design_filter()

        # Crossfade state for coefficient swaps.  When non-None,
        # ``self._old_filter`` is a snapshot of the previous filter
        # (its own b / a / states tuple) running in parallel for
        # the next ``CROSSFADE_SAMPLES`` output samples.
        self._old_b: np.ndarray | None = None
        self._old_a: np.ndarray | None = None
        self._old_states: list | None = None
        self._crossfade_remaining: int = 0

    # ── Public read-only views (compat for old callers) ──────────

    @property
    def deep(self) -> bool:
        """Legacy compat: True if cascade > 1.  Reads only; writes
        go through ``update_coeffs`` or full reconstruction."""
        return self.cascade > 1

    # ── Filter design (private) ──────────────────────────────────

    def _design_filter(self):
        """Compute (b, a, states) for the current parameters.
        states is a list of one zi-init per cascade stage; for the
        DC-blocker path the list has length 1 (no cascade applied).
        """
        if self.dc_blocker:
            from scipy.signal import butter
            # 4th-order Butterworth high-pass, corner at width/2 so
            # the visible kill region (freq ± width/2 → 0..width)
            # matches what the operator sees on the spectrum overlay.
            corner = max(self.width_hz * 0.5, 5.0)
            b, a = butter(4, corner, btype='high', fs=self.rate)
            order = max(len(a), len(b)) - 1
            states = [
                {
                    "i": np.zeros(order, dtype=np.float64),
                    "q": np.zeros(order, dtype=np.float64),
                }
            ]
            return (np.asarray(b, dtype=np.float64),
                    np.asarray(a, dtype=np.float64),
                    states)
        # Off-DC: parametric peaking-EQ biquad with negative gain.
        # Total target depth distributed across cascade stages.
        per_stage_db = self.depth_db / self.cascade
        b, a = self._design_peaking_biquad(
            self.rate, self.freq_hz, self.width_hz, per_stage_db)
        order = max(len(a), len(b)) - 1
        states = [
            {
                "i": np.zeros(order, dtype=np.float64),
                "q": np.zeros(order, dtype=np.float64),
            }
            for _ in range(self.cascade)
        ]
        return (np.asarray(b, dtype=np.float64),
                np.asarray(a, dtype=np.float64),
                states)

    @classmethod
    def _design_peaking_biquad(cls, rate: float, freq_hz: float,
                               width_hz: float, gain_db: float):
        """RBJ Audio EQ Cookbook peaking-EQ biquad.  With negative
        gain_db this gives a notch with ``depth = gain_db`` at the
        center and a smooth, predictable shoulder shape.

        Stability: clamp Q to ``Q_MAX`` so the poles stay safely
        inside the unit circle in float32.  See design doc §3.1.
        """
        # Safe minimum width given the Q_MAX stability cap.
        min_width = max(freq_hz / cls.Q_MAX, 1.0)
        w_eff = max(float(width_hz), min_width)
        # Q from the operator-facing -3 dB-from-peak BW.
        q = max(freq_hz / w_eff, 0.5)
        w0 = 2.0 * np.pi * freq_hz / rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / (2.0 * q)
        # Peaking EQ amplitude factor.  gain_db is negative for a
        # notch; A < 1 produces attenuation at f0.
        A = 10.0 ** (gain_db / 40.0)
        # Coefficients per the RBJ Cookbook.
        b0 = 1.0 + alpha * A
        b1 = -2.0 * cos_w0
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha / A
        # Normalize so a[0] = 1 (lfilter convention).
        b = np.array([b0 / a0, b1 / a0, b2 / a0], dtype=np.float64)
        a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
        return b, a

    # ── Live coefficient swap ────────────────────────────────────

    def update_coeffs(self, freq_hz: float | None = None,
                      width_hz: float | None = None,
                      depth_db: float | None = None,
                      cascade: int | None = None) -> None:
        """Recompute coefficients in place; preserve continuity via a
        two-filter crossfade.

        Any params left as None keep their current values.

        Behaviour at the swap:
          1. Snapshot current b/a/states as ``self._old_*``.  These
             keep running during the crossfade window so the operator
             never hears a click from the IIR state mismatch.
          2. Update ``self.freq_hz`` / ``self.width_hz`` /
             ``self.depth_db`` / ``self.cascade`` from the args (or
             keep existing).
          3. Build fresh b/a/states for the new parameters.
          4. Arm the crossfade counter at ``CROSSFADE_SAMPLES``.

        process() then runs both old and new on every block until
        the counter expires, linearly crossfading their outputs.
        After expiry, the old filter is discarded.

        Back-to-back swaps during a fast drag handle correctly:
        each new swap snapshots the **current crossfade** as the
        new "old" reference, so the operator hears a continuous
        smooth blend without stacked artifacts.  The brief residual
        bypass during overlapping swaps is sub-perceptible at the
        slider's natural drag cadence.
        """
        # Step 1: snapshot the current filter as the "old" side of
        # the crossfade.  If a crossfade is already running, we
        # snapshot whatever the CURRENT live filter produces, not
        # the older fade target — so no state stacking.
        self._old_b = self.b
        self._old_a = self.a
        self._old_states = self.states
        self._crossfade_remaining = self.CROSSFADE_SAMPLES

        # Step 2-3: update params and rebuild.
        if freq_hz is not None:
            self.freq_hz = float(freq_hz)
        if width_hz is not None:
            self.width_hz = float(width_hz)
        if depth_db is not None:
            self.depth_db = float(depth_db)
        if cascade is not None:
            self.cascade = max(1, min(4, int(cascade)))
        self.b, self.a, self.states = self._design_filter()

    # ── Process (hot path) ───────────────────────────────────────

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return iq
        # Cast to float64 internally — IIR feedback accuracy matters
        # at high Q.  Output cast back to complex64 at the end.
        i_in = iq.real.astype(np.float64, copy=False)
        q_in = iq.imag.astype(np.float64, copy=False)
        # Run the new (live) filter through all cascade stages.
        i_new, q_new = self._run_cascade(
            self.b, self.a, self.states, i_in, q_in)
        # If a crossfade is in progress, run the old filter in
        # parallel and mix.
        if self._crossfade_remaining > 0 and self._old_b is not None:
            i_old, q_old = self._run_cascade(
                self._old_b, self._old_a, self._old_states,
                i_in, q_in)
            n = len(i_in)
            consumed = self.CROSSFADE_SAMPLES - self._crossfade_remaining
            # ramp = fraction of "new" mixed in.  Goes from 'consumed'
            # at sample 0 of this block to 'consumed + n' across the
            # block.  Clipped to [0, 1] in case the crossfade ends
            # mid-block.
            ramp = np.clip(
                (consumed + np.arange(n, dtype=np.float64))
                / self.CROSSFADE_SAMPLES,
                0.0, 1.0,
            )
            i_out = ramp * i_new + (1.0 - ramp) * i_old
            q_out = ramp * q_new + (1.0 - ramp) * q_old
            self._crossfade_remaining = max(
                0, self._crossfade_remaining - n)
            if self._crossfade_remaining == 0:
                # Done — drop the old filter.
                self._old_b = None
                self._old_a = None
                self._old_states = None
        else:
            i_out = i_new
            q_out = q_new
        return (i_out + 1j * q_out).astype(np.complex64)

    @staticmethod
    def _run_cascade(b, a, states, i_in, q_in):
        """Run a cascade of identical biquad stages, each with its own
        zi state, on the I and Q halves of a complex signal.

        states is a list of dicts {"i": zi_i, "q": zi_q}.  Each
        stage's state is updated in place.  Returns (i_out, q_out)
        as float64 arrays.
        """
        i_cur = i_in
        q_cur = q_in
        for st in states:
            i_cur, st["i"] = lfilter(b, a, i_cur, zi=st["i"])
            q_cur, st["q"] = lfilter(b, a, q_cur, zi=st["q"])
        return i_cur, q_cur


class AMDemod:
    """AM envelope detection with LPF and DC removal."""

    def __init__(self, rate: int, bw_hz: float = 5000.0, taps: int = 129):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy is required; run: pip install scipy")
        self.rate = rate
        self.lpf = firwin(taps, bw_hz, fs=rate, window="hann").astype(np.float64)
        self.state = np.zeros(taps - 1, dtype=np.complex64)
        self._dc = 0.0

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        filtered, self.state = lfilter(self.lpf, 1.0, iq, zi=self.state)
        env = np.abs(filtered).astype(np.float32)
        # Simple one-pole DC removal (slow enough to track AM carrier only)
        block_mean = float(np.mean(env))
        self._dc = 0.95 * self._dc + 0.05 * block_mean
        return (env - self._dc).astype(np.float32)


# Legacy one-shot functions kept for backward compatibility with existing
# tools/tests. Not used by the live app — they have block-edge artifacts.
def usb_demod(iq: np.ndarray, rate: int,
              low_hz: float = 300.0, high_hz: float = 2700.0) -> np.ndarray:
    d = SSBDemod(rate, "USB", low_hz, high_hz)
    return d.process(iq.astype(np.complex64))


def lsb_demod(iq: np.ndarray, rate: int,
              low_hz: float = 300.0, high_hz: float = 2700.0) -> np.ndarray:
    d = SSBDemod(rate, "LSB", low_hz, high_hz)
    return d.process(iq.astype(np.complex64))


def am_demod(iq: np.ndarray, rate: int, bw_hz: float = 5000.0) -> np.ndarray:
    d = AMDemod(rate, bw_hz)
    return d.process(iq.astype(np.complex64))
