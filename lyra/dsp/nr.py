"""Classical noise reduction — streaming spectral subtraction.

Single-channel mono float32 audio at 48 kHz. Uses an STFT with 50 %
Hanning-window overlap (COLA-exact reconstruction) and magnitude-
domain subtraction. A VAD-like rule updates the noise floor estimate
only on frames quieter than the current estimate, so speech doesn't
pollute the noise model.

Three profiles tune the aggression / artifact trade-off:

    Light       — SSB ragchew, subtle hiss reduction, minimal artifacts
    Medium      — standard speech NR (default)
    Aggressive  — weak-signal DX, noisy bands; accept more "musical
                  noise" artifacts for deeper noise suppression

The "musical noise" artifact of classical subtraction is a known
limitation; the aggressive profile spreads this by using a higher
spectral floor. Neural NR (RNNoise / DeepFilterNet — planned)
eliminates it almost entirely. See docs/backlog.md.

Integration: Radio calls `.process(audio_block)` once per demod
block. The module is length-preserving: input N samples → output N
samples (with ~2.7 ms internal latency, one FFT frame of 256 at 48k).
"""
from __future__ import annotations

import numpy as np


class SpectralSubtractionNR:
    FFT_SIZE: int = 256
    HOP: int = 128       # 50% overlap → COLA-exact with Hanning

    # Per-profile DSP parameters:
    #   alpha        — over-subtraction factor (higher = more noise removed)
    #   beta         — spectral floor (higher = less musical-noise artifact)
    #   noise_track  — exp-smoothing rate for noise-floor estimate
    #   vad_gate     — frame is "noise" if power < noise_est × this factor
    PROFILES: dict[str, dict[str, float]] = {
        "light":      {"alpha": 1.0, "beta": 0.20, "noise_track": 0.03,  "vad_gate": 3.0},
        "medium":     {"alpha": 1.8, "beta": 0.12, "noise_track": 0.015, "vad_gate": 3.0},
        "aggressive": {"alpha": 2.8, "beta": 0.06, "noise_track": 0.008, "vad_gate": 4.0},
    }
    DEFAULT_PROFILE = "medium"

    def __init__(self, rate: int = 48000):
        self.rate = rate
        self._fft = self.FFT_SIZE
        self._hop = self.HOP
        self._window = np.hanning(self._fft).astype(np.float32)

        n_bins = self._fft // 2 + 1
        # Initial noise-floor guess — very small so the first speech
        # frame won't be obliterated while the estimator catches up.
        self._noise_mag = np.full(n_bins, 1e-3, dtype=np.float32)

        # Streaming state
        self._in_buf = np.zeros(0, dtype=np.float32)
        # overlap-add carry for the tail of the last frame
        self._out_carry = np.zeros(self._hop, dtype=np.float32)

        self.enabled = False
        self.profile = self.DEFAULT_PROFILE
        self._apply_profile()

    # ── public API ────────────────────────────────────────────────
    def set_profile(self, name: str):
        if name in self.PROFILES:
            self.profile = name
            self._apply_profile()

    def reset(self):
        """Drop all streaming state — call on mode / rate / stream
        transitions so a stale overlap tail doesn't leak into new audio."""
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_carry = np.zeros(self._hop, dtype=np.float32)
        n_bins = self._fft // 2 + 1
        self._noise_mag = np.full(n_bins, 1e-3, dtype=np.float32)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one demod block. Returns the same length of audio
        (possibly delayed by one hop on the very first call) or the
        input unchanged when `enabled == False`."""
        if not self.enabled or audio.size == 0:
            return audio

        # Append new samples to the pending-input buffer
        x = audio.astype(np.float32, copy=False)
        self._in_buf = np.concatenate([self._in_buf, x])

        out_chunks: list[np.ndarray] = []
        while self._in_buf.size >= self._fft:
            frame = self._in_buf[:self._fft] * self._window
            spec = np.fft.rfft(frame)
            mag = np.abs(spec)

            # Noise-floor tracking — simple VAD: update only when the
            # frame is quieter than vad_gate × the current estimate.
            frame_pow = float(np.mean(mag * mag))
            noise_pow = float(np.mean(self._noise_mag * self._noise_mag))
            if frame_pow <= noise_pow * self._vad_gate:
                a = self._noise_track
                self._noise_mag = (1.0 - a) * self._noise_mag + a * mag

            # Magnitude-domain subtraction with spectral floor
            denom = np.maximum(mag, 1e-10)
            gain = np.maximum(1.0 - self._alpha * self._noise_mag / denom,
                              self._beta).astype(np.float32)
            time_frame = np.fft.irfft(spec * gain, self._fft).astype(np.float32)

            # Overlap-add: first hop samples get combined with the
            # carried tail from the previous frame; back half becomes
            # new carry. Hanning @ 50% overlap = COLA-exact, so the
            # sum cleanly reconstructs unity-gain signal regions.
            head = self._out_carry + time_frame[:self._hop]
            out_chunks.append(head)
            self._out_carry = time_frame[self._hop:].copy()

            # Advance input by one hop
            self._in_buf = self._in_buf[self._hop:]

        if not out_chunks:
            # First call didn't produce a full hop yet — return silence
            # of matching length so downstream doesn't see a length change.
            # This happens only when the very first block is smaller than
            # FFT_SIZE, which doesn't occur with Radio's 2048-sample blocks.
            return np.zeros_like(audio)

        output = np.concatenate(out_chunks)
        # Match length to the input block by padding or trimming. This
        # preserves per-block timing even with the ½-frame internal lag.
        if output.size < audio.size:
            output = np.concatenate(
                [output, np.zeros(audio.size - output.size, dtype=np.float32)])
        elif output.size > audio.size:
            output = output[:audio.size]
        return output

    # ── internals ─────────────────────────────────────────────────
    def _apply_profile(self):
        p = self.PROFILES[self.profile]
        self._alpha = p["alpha"]
        self._beta = p["beta"]
        self._noise_track = p["noise_track"]
        self._vad_gate = p["vad_gate"]
