"""Audio mixer for RX1+RX2 stereo routing (v0.0.9.6).

Provides:

* :class:`StereoMixer` — Lyra-native two-channel mixer with WDSP-style
  per-channel pan curve.  Used by Radio's audio routing to combine RX1
  and RX2 into a stereo pair (L, R) for the audio sink.

* :func:`pan_to_gains` — direct port of WDSP's ``SetRXAPanelPan`` curve
  (`patchpanel.c::SetRXAPanelPan` lines 158-176).  Translates a 0.0..1.0
  pan position into (gainI, gainQ) — equivalent to (gainL, gainR) for a
  stereo audio channel.

WDSP attribution
----------------
The pan curve is a direct port of:

    D:\\sdrprojects\\OpenHPSDR-Thetis-2.10.3.13\\Project Files\\Source\\
    wdsp\\patchpanel.c::SetRXAPanelPan(int channel, double pan)

Original copyright (C) 2013 Warren Pratt, NR0V.  Distributed under
GNU GPL v2+, made available to Lyra under the GPL v3+ relicense per
``docs/architecture/wdsp_integration.md``.

The mixer itself (:class:`StereoMixer`) is Lyra-native.  WDSP's mixer
(``ChannelMaster/aamix.c``) is Thetis-specific glue — long routing
state machine for ANAN's 7-channel topology — not portable to Lyra's
simpler per-RX architecture.  We follow the same operator-visible
semantics (pan, mute, gain) but write the implementation in
NumPy + Python.  See ``CLAUDE.md`` §13.3 for the WDSP-port-not-
Thetis-copy principle.

Usage
-----
::

    mixer = StereoMixer(n_channels=2)
    mixer.set_pan(0, 0.0)   # RX1 hard-left
    mixer.set_pan(1, 1.0)   # RX2 hard-right
    mixer.set_mute(1, False)

    # Each block of audio:
    rx1_audio = ...   # float32, shape (block_size,)
    rx2_audio = ...   # float32, shape (block_size,)
    stereo = mixer.mix([rx1_audio, rx2_audio])
    # stereo.shape == (block_size, 2)  — interleavable to L/R sink
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np


# ── Pan curve (direct WDSP port) ─────────────────────────────────────


def pan_to_gains(pan: float) -> tuple[float, float]:
    """Convert a pan position in [0.0, 1.0] to (left_gain, right_gain).

    Direct port of ``wdsp/patchpanel.c::SetRXAPanelPan`` (Pratt, 2013).

    Curve semantics:

    * ``pan == 0.0``  → hard left  (gainL=1.0, gainR=0.0)
    * ``pan == 0.5``  → both unity (gainL=1.0, gainR=1.0).  This is
      6 dB louder than the endpoints — a deliberate WDSP design
      decision, NOT an equal-power curve.  The intent is "pan=0.5
      means BOTH channels at full" (binaural reception of a single
      RX), while the endpoints attenuate the OTHER channel toward
      zero.  Don't substitute a constant-power curve here — it
      changes operator-perceived behavior at center.
    * ``pan == 1.0``  → hard right (gainL=0.0, gainR=1.0)

    Math (from patchpanel.c lines 163-175):

    .. code-block:: text

        if pan <= 0.5:                    if pan > 0.5:
            gainL = 1.0                       gainL = sin(pan * pi)
            gainR = sin(pan * pi)             gainR = 1.0

    At pan=0.5: sin(pi/2) = 1.0 for both — explains the 6 dB peak
    at center.

    pan is clamped to [0.0, 1.0] defensively before evaluation.

    Args:
        pan: 0.0 (hard left) to 1.0 (hard right).

    Returns:
        (gain_left, gain_right) — each in [0.0, 1.0].
    """
    p = max(0.0, min(1.0, float(pan)))
    if p <= 0.5:
        gain_l = 1.0
        gain_r = math.sin(p * math.pi)
    else:
        gain_l = math.sin(p * math.pi)
        gain_r = 1.0
    return (gain_l, gain_r)


# ── Lyra-native stereo mixer (modeled on WDSP semantics, NumPy impl) ─


class StereoMixer:
    """Combine N mono audio channels into a stereo (L, R) output.

    Lyra-native mixer modeled on WDSP's pan-mute-gain semantics but
    implemented in NumPy.  Designed for the v0.0.9.6 audio path:
    RX1 + RX2 (both mono) -> stereo bytes for the audio sink.

    Per-channel state:

    * ``pan[k]``: 0.0 (hard left) .. 1.0 (hard right), default 0.5
      (equal in both)
    * ``gain[k]``: linear scalar applied to the channel before
      pan mapping (default 1.0)
    * ``muted[k]``: when True, channel contributes 0.0 (default
      False)

    Thread-safety: state writes are single-attribute under the GIL;
    safe across threads as long as the operator UI thread doesn't
    corrupt mid-write.  Audio thread reads atomic-ish snapshots.
    Per ``CLAUDE.md`` §5, no explicit locking needed.

    Output is float32 with shape ``(block_size, 2)`` — channel-
    interleaved-friendly when caller flattens or transposes.
    """

    def __init__(self, n_channels: int = 2) -> None:
        if n_channels < 1:
            raise ValueError(
                f"StereoMixer needs at least 1 channel; got "
                f"{n_channels}")
        self.n_channels = int(n_channels)
        self._pan = np.full(self.n_channels, 0.5, dtype=np.float64)
        self._gain = np.ones(self.n_channels, dtype=np.float64)
        self._muted = np.zeros(self.n_channels, dtype=bool)

    # ── operator-tunable params ──────────────────────────────────

    def set_pan(self, channel: int, pan: float) -> None:
        """Set channel pan in [0.0, 1.0].  See :func:`pan_to_gains`."""
        if not (0 <= channel < self.n_channels):
            raise IndexError(
                f"channel {channel} out of range "
                f"[0, {self.n_channels})")
        self._pan[channel] = max(0.0, min(1.0, float(pan)))

    def get_pan(self, channel: int) -> float:
        return float(self._pan[channel])

    def set_gain(self, channel: int, gain: float) -> None:
        """Set channel linear gain.  Clamped to [0.0, 4.0] to prevent
        runaway scaling — operators who want >12 dB boost should
        adjust their AGC instead."""
        if not (0 <= channel < self.n_channels):
            raise IndexError(
                f"channel {channel} out of range "
                f"[0, {self.n_channels})")
        self._gain[channel] = max(0.0, min(4.0, float(gain)))

    def get_gain(self, channel: int) -> float:
        return float(self._gain[channel])

    def set_mute(self, channel: int, muted: bool) -> None:
        if not (0 <= channel < self.n_channels):
            raise IndexError(
                f"channel {channel} out of range "
                f"[0, {self.n_channels})")
        self._muted[channel] = bool(muted)

    def is_muted(self, channel: int) -> bool:
        return bool(self._muted[channel])

    # ── audio path ───────────────────────────────────────────────

    def mix(
        self, channels: Sequence[Optional[np.ndarray]]
    ) -> np.ndarray:
        """Mix N mono channels into stereo.

        Args:
            channels: list of length ``n_channels``; each element is
                either a 1-D float32 array of audio samples or None
                (channel unused this block — equivalent to silent).
                All non-None arrays must have the same length.

        Returns:
            ``(block_size, 2)`` float32 array, [:, 0] is left,
            [:, 1] is right.
        """
        if len(channels) != self.n_channels:
            raise ValueError(
                f"expected {self.n_channels} channels; got "
                f"{len(channels)}")

        # Find the block size from any non-None channel.  All non-
        # None channels must match.
        block_size = 0
        for chan in channels:
            if chan is None:
                continue
            if block_size == 0:
                block_size = chan.size
            elif chan.size != block_size:
                raise ValueError(
                    f"channel size mismatch: expected {block_size}, "
                    f"got {chan.size}")
        if block_size == 0:
            # All channels None → return empty (defensive; operator
            # shouldn't see this in practice).
            return np.zeros((0, 2), dtype=np.float32)

        out = np.zeros((block_size, 2), dtype=np.float32)
        for k, chan in enumerate(channels):
            if chan is None or self._muted[k]:
                continue
            gain_l, gain_r = pan_to_gains(self._pan[k])
            scale = self._gain[k]
            # Accumulate (sum mix — typical SDR audio summation).
            # NumPy broadcasts the scalar per-channel.
            out[:, 0] += np.asarray(chan, dtype=np.float32) * np.float32(scale * gain_l)
            out[:, 1] += np.asarray(chan, dtype=np.float32) * np.float32(scale * gain_r)
        return out

    # ── convenience presets ──────────────────────────────────────

    def preset_rx1_left_rx2_right(self) -> None:
        """Standard RX2-on configuration: RX1 hard-left, RX2 hard-
        right.  Convenience helper called by Radio when RX2 enables.
        Both channels unmuted, gains at unity."""
        if self.n_channels < 2:
            raise RuntimeError(
                "preset_rx1_left_rx2_right needs n_channels >= 2")
        self.set_pan(0, 0.0)
        self.set_pan(1, 1.0)
        self.set_mute(0, False)
        self.set_mute(1, False)
        self.set_gain(0, 1.0)
        self.set_gain(1, 1.0)

    def preset_rx1_only_centered(self) -> None:
        """RX2-off configuration: RX1 centered (both channels at
        unity), other channels muted.  Default state at startup."""
        self.set_pan(0, 0.5)
        self.set_mute(0, False)
        self.set_gain(0, 1.0)
        for k in range(1, self.n_channels):
            self.set_mute(k, True)
