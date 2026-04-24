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


class AK4951Sink:
    """Route audio to the HL2's AK4951 line-level output via EP2 TX slots."""

    def __init__(self, stream):
        self._stream = stream
        self._stream.inject_audio_tx = True

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        self._stream.queue_tx_audio(audio.astype(np.float32))

    def close(self) -> None:
        self._stream.inject_audio_tx = False


class SoundDeviceSink:
    """Route audio to the PC default playback device."""

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
        self._stream = sd.OutputStream(
            samplerate=rate, channels=1, dtype="float32",
            blocksize=blocksize, device=device,
        )
        self._stream.start()

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        # Ensure shape (N, 1) for mono
        a = audio.astype(np.float32).reshape(-1, 1)
        try:
            self._stream.write(a)
        except self._sd.PortAudioError:
            pass

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class NullSink:
    def write(self, audio): pass
    def close(self): pass
