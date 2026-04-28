"""Audio output sinks: where demodulated audio goes.

Two implementations:
- AK4951Sink: packs audio into EP2 TX slots on the HL2 stream; the
  updated gateware routes these samples to the AK4951 codec line-out.
- SoundDeviceSink: outputs to PC default playback device via sounddevice
  (soft dependency; only imported when this sink is selected).
"""
from __future__ import annotations

from typing import Optional, Protocol

import numpy as np


class AudioSink(Protocol):
    def write(self, audio: np.ndarray) -> None: ...
    def close(self) -> None: ...
    # Optional stereo balance support — sinks that can address
    # left/right channels independently (PC Soundcard) honor this;
    # sinks that physically can't (AK4951 — single mono pair) ignore.
    def set_lr_gains(self, left: float, right: float) -> None: ...


class AK4951Sink:
    """Route audio to the HL2's AK4951 line-level output via EP2 TX slots.

    The AK4951 is a true STEREO codec: the EP2 audio slot has separate
    16-bit Left + Right fields, and the gateware routes both to the
    AK4951 DAC's L/R channels. So Balance is honored end-to-end — we
    apply per-channel gains here and feed (N, 2) stereo into the EP2
    queue, which packs L and R independently into the frame.

    Sink-swap cleanup: the underlying HL2Stream owns a TX audio
    queue (deque) that's NOT per-sink — it's a long-lived buffer
    shared across sink swaps. We clear it on both init AND close,
    so swapping to/from this sink doesn't leak stale samples between
    sessions ("digitized robotic" symptom: old samples + new samples
    interleaved in the EP2 frames).
    """

    def __init__(self, stream):
        self._stream = stream
        # Drain any leftover TX audio from a previous session before
        # we start enqueuing fresh samples.
        if hasattr(stream, "clear_tx_audio"):
            stream.clear_tx_audio()
        self._stream.inject_audio_tx = True
        # Stereo balance gains. Default = equal-power center
        # (cos/sin at π/4 = √2/2 each). Updated by Radio whenever the
        # operator moves the Balance slider, exactly like SoundDeviceSink.
        self._left_gain = 0.7071067811865476
        self._right_gain = 0.7071067811865476

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        # Two input shapes are accepted:
        #   - mono (N,) — duplicated to L/R, then per-channel balance
        #     applied. Default audio chain produces this shape.
        #   - stereo (N, 2) — already L/R-distinct (e.g., BIN
        #     pseudo-binaural is on). Balance gains apply column-wise.
        if audio.ndim == 2 and audio.shape[1] == 2:
            stereo = audio.astype(np.float32, copy=False)
            stereo = stereo * np.array(
                [self._left_gain, self._right_gain], dtype=np.float32)
        else:
            mono = audio.astype(np.float32).reshape(-1)
            # When the operator hasn't touched Balance both gains are
            # √2/2 ≈ 0.707, so the AK4951 hears the same audio in both
            # ears as the legacy mono-duplicated path did.
            l = mono * self._left_gain
            r = mono * self._right_gain
            stereo = np.stack((l, r), axis=1)            # (N, 2)
        self._stream.queue_tx_audio(stereo)

    def set_lr_gains(self, left: float, right: float) -> None:
        """Update the L/R channel gains. Called by Radio whenever the
        operator changes the Balance slider; same contract as
        SoundDeviceSink. Equal-power pan law lives in
        Radio.balance_lr_gains which feeds this."""
        self._left_gain = float(left)
        self._right_gain = float(right)

    def close(self) -> None:
        self._stream.inject_audio_tx = False
        # Clear the queue on close so the NEXT sink (PC Soundcard
        # or another AK4951 instance) starts from a known empty
        # state. Without this, residual samples in the deque continue
        # being pulled by EP2 framing for up to ~1 second.
        if hasattr(self._stream, "clear_tx_audio"):
            self._stream.clear_tx_audio()


class SoundDeviceSink:
    """Route audio to the PC default playback device.

    Key design choices (documented because they matter for Windows
    audio interfaces, USB multichannel cards, and S/PDIF outputs):

    - **Prefers WASAPI over MME.** PortAudio's system default on
      Windows is MME (20+ years old, flaky with S/PDIF and USB audio
      interfaces, silently drops mono frames on some drivers). We
      explicitly pick the WASAPI host API's default output device
      when the caller didn't specify one. WASAPI is what every
      serious modern audio app on Windows uses (DAWs, SDR clients,
      browsers).

    - **Opens stereo, writes duplicated mono.** The demod chain is
      mono (SSB/CW/AM/FM/DIGU all produce a single audio channel).
      S/PDIF / TOSLINK outputs are rigidly 2-channel and some drivers
      silently drop mono frames instead of auto-duplicating — so we
      always open stereo and duplicate the mono sample into both L
      and R. Harmless on analog outputs (which would have duplicated
      anyway).
    """

    def __init__(self, rate: int = 48000, device: Optional[int] = None,
                 blocksize: int = 1024):
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RuntimeError(
                "sounddevice is not installed. `pip install sounddevice` "
                "or switch the audio output to AK4951."
            ) from e
        self._sd = sd
        self._rate = rate

        if device is None:
            device = self._pick_wasapi_default(sd)

        self._channels = 2
        self._stream = sd.OutputStream(
            samplerate=rate, channels=self._channels, dtype="float32",
            blocksize=blocksize, device=device,
        )
        self._stream.start()
        # Stereo balance gains. Default = equal-power center
        # (cos/sin at π/4 = √2/2 each). Updated by Radio whenever the
        # operator moves the Balance slider.
        self._left_gain = 0.7071067811865476
        self._right_gain = 0.7071067811865476

    @staticmethod
    def _pick_wasapi_default(sd):
        """Find the WASAPI host API's default output device. Returns a
        device index, or None if WASAPI isn't available (falls through
        to PortAudio's system default — probably MME on Windows, which
        is less reliable but not always broken).
        """
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            return None
        for i, ha in enumerate(hostapis):
            if ha["name"] == "Windows WASAPI":
                default_out = ha.get("default_output_device", -1)
                if default_out >= 0:
                    return default_out
                return None
        return None

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        # Two input shapes are accepted (see AK4951Sink.write for
        # rationale): mono (N,) or stereo (N, 2). BIN feeds the
        # stereo path; everything else hits the mono path.
        if audio.ndim == 2 and audio.shape[1] == 2:
            a = audio.astype(np.float32, copy=False) * np.array(
                [self._left_gain, self._right_gain], dtype=np.float32)
        else:
            mono = audio.astype(np.float32).reshape(-1)
            # Stereo build with per-channel balance gains applied.
            # When the operator hasn't moved the Balance slider both
            # gains are √2/2 (equal-power center) and the result is
            # the same audio in both ears as before the balance feature
            # existed.
            l = mono * self._left_gain
            r = mono * self._right_gain
            a = np.stack((l, r), axis=1)
        try:
            self._stream.write(a)
        except self._sd.PortAudioError:
            # Intentionally swallowed: a transient PortAudio error
            # (e.g., device exclusive-mode grabbed by another app)
            # should not crash the audio thread. If the user ever
            # reports "no audio" with a clean stream, re-enable the
            # diagnostic prints in the git history for this file.
            pass

    def set_lr_gains(self, left: float, right: float) -> None:
        """Update the L/R channel gains. Called by Radio whenever
        the operator changes the Balance slider. Values are floats
        in [0, 1]; equal-power pan law lives in Radio.balance_lr_gains
        which feeds this."""
        self._left_gain = float(left)
        self._right_gain = float(right)

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class NullSink:
    def write(self, audio): pass
    def close(self): pass
