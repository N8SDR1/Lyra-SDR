"""Neural noise reduction — DeepFilterNet integration.

Wraps the DeepFilterNet (DFN) library as a streaming RX-audio
denoiser usable from Lyra's Channel.process() chain.

DFN at a glance
===============

DeepFilterNet is a two-stage neural denoiser by Hendrik Schröter
et al. (FAU Erlangen-Nürnberg, 2022-2024).  Stage 1 estimates
ERB-band gains; stage 2 applies a learned complex deep-filter
post-correction.  Trained on the DNS-Challenge corpus — much wider
noise diversity than the telephony-data RNNoise was trained on.

Native sample rate: 48 kHz (matches Lyra's audio rate exactly —
no resampling).  Frame size: 480 samples (10 ms).  Chunked
streaming inference is supported via the library's df_features
helpers.

Resource expectations (rough — operators should run the
benchmark in Settings to measure their actual system):

    Modern desktop (Intel i5 / Ryzen 5, 2020+):
        CPU mode: ~10-15 % per core
        GPU mode (any CUDA / DirectML GPU): < 1 % CPU + ~100 MB VRAM
    Older laptop / low-power CPU:
        CPU mode: 30-80 % — likely audio dropouts when stacked
                            with NR2 + LMS

Latency: ~30-50 ms (one DFN frame = 10 ms + lookahead inside the
two-stage architecture).

Operator warnings
-----------------

This module deliberately does NOT pip-install deepfilternet at
runtime.  Operators must opt in:

    pip install deepfilternet

A graceful soft-fail returns an "unavailable" state when the
import fails so Lyra still launches if the operator never wanted
neural NR in the first place.

The Settings → Noise → Neural NR group surfaces install
instructions, the latency / CPU warnings above, and a
"Test on your system" benchmark button that reports actual
measured cost on the operator's hardware.
"""
# Lyra-SDR — Neural noise reduction (DeepFilterNet wrapper)
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License v3
# or later.  See LICENSE in the project root for the full terms.
#
# DeepFilterNet itself: Apache 2.0 / Hendrik Schröter et al.
# License-compatible with Lyra's GPL v3+.  We import the package
# at runtime — no DFN code is bundled.
from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# Lazy-imported here so absence of the package doesn't break import-
# time of any code that needs the class definitions (e.g., for
# isinstance checks, default-arg references, etc.).
_dfn = None
_dfn_import_error: Optional[Exception] = None


def is_available() -> bool:
    """Probe whether deepfilternet is importable.  Used by Radio's
    neural_nr_available() and by the Settings UI to grey out the
    Neural option when the package isn't installed."""
    global _dfn, _dfn_import_error
    if _dfn is not None:
        return True
    if _dfn_import_error is not None:
        return False
    try:
        import df  # noqa  (deepfilternet's import root is `df`)
        _dfn = df
        return True
    except Exception as exc:
        _dfn_import_error = exc
        return False


def import_error_message() -> str:
    """Human-readable explanation of why deepfilternet isn't
    importable — used by the Settings UI to show install
    guidance."""
    if _dfn_import_error is None:
        return ""
    return str(_dfn_import_error)


class DeepFilterNetNR:
    """Streaming DeepFilterNet wrapper for Lyra's Channel.

    Construct lazily — the underlying torch model loads at first
    process() call, not at __init__, so creating the object doesn't
    have side-effects.  This lets Channel.process() create one
    unconditionally while keeping the cost zero until the operator
    actually enables Neural NR.

    Streaming protocol: caller passes audio chunks of any length;
    we buffer to DFN's native 480-sample (10 ms) frame size,
    process via the library's chunked enhancement API, and return
    output of the same length as input (with a small leading-edge
    delay during the first ~5 frames as the lookahead window fills).

    Sample-rate contract: 48 kHz native.  We don't resample
    internally — caller must feed 48 kHz audio.  Lyra's
    Channel.audio_rate is 48 kHz, so no work needed.
    """

    NATIVE_SAMPLE_RATE: int = 48000
    FRAME_SAMPLES: int = 480   # 10 ms at 48 kHz — DFN's native frame

    # Two model variants — DFN2 is faster, DFN3 is the latest +
    # higher-quality.  Operators pick via Settings.
    MODEL_DFN2 = "DeepFilterNet2"
    MODEL_DFN3 = "DeepFilterNet3"
    DEFAULT_MODEL = MODEL_DFN3

    # Inference device — auto-detected at init time but operator can
    # force CPU for stability or GPU for speed.
    DEVICE_AUTO = "auto"
    DEVICE_CPU = "cpu"
    DEVICE_CUDA = "cuda"

    def __init__(self, rate: int = 48000) -> None:
        if rate != self.NATIVE_SAMPLE_RATE:
            logger.warning(
                "DeepFilterNetNR: caller passed rate=%d but DFN is "
                "48 kHz native; resampling not implemented in this "
                "wrapper.  Configure your channel for 48 kHz audio.",
                rate)
        self.rate = self.NATIVE_SAMPLE_RATE
        self.enabled: bool = False

        # Operator-tunable — applied at next reload().
        self.model_name: str = self.DEFAULT_MODEL
        self.device_pref: str = self.DEVICE_AUTO

        # Lazy-loaded model state.
        self._model = None
        self._df_state = None
        self._device_actual: str = ""    # populated after load
        self._loaded_model_name: str = ""

        # Streaming buffer — audio comes in arbitrarily sized blocks
        # but DFN wants 480-sample frames.  We accumulate input here
        # and emit output as full frames complete.
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)

        # Performance metrics — populated by process() and surfaced
        # via the public API for the Settings benchmark + status
        # readout.
        self._frames_processed: int = 0
        self._total_inference_sec: float = 0.0

    # ── Public API ────────────────────────────────────────────────

    def reload(self) -> bool:
        """(Re)load the model with current model_name + device_pref.

        Returns True on success, False on failure (typically import
        error or no torch / no model file).  Operators see failures
        as a soft-disable + diagnostic message rather than a crash.
        """
        if not is_available():
            self._model = None
            return False
        try:
            from df.enhance import init_df
        except Exception as exc:
            logger.warning("DFN init_df import failed: %s", exc)
            return False
        # Resolve device preference to a torch device string.
        try:
            import torch
            if self.device_pref == self.DEVICE_AUTO:
                self._device_actual = (
                    "cuda" if torch.cuda.is_available() else "cpu")
            elif self.device_pref == self.DEVICE_CUDA:
                self._device_actual = (
                    "cuda" if torch.cuda.is_available() else "cpu")
                if not torch.cuda.is_available():
                    logger.warning(
                        "DFN: CUDA requested but unavailable; "
                        "falling back to CPU")
            else:
                self._device_actual = "cpu"
        except Exception as exc:
            logger.warning("DFN: torch unavailable: %s", exc)
            return False
        try:
            # init_df returns (model, df_state, suffix) per their API.
            # post_filter=True enables stage-2 deep-filter — slightly
            # better quality at minor extra CPU cost.
            self._model, self._df_state, _ = init_df(
                post_filter=True)
            try:
                self._model = self._model.to(self._device_actual)
            except Exception:
                # If `.to()` isn't supported (older API), fall back
                # to CPU and keep going.
                self._device_actual = "cpu"
            self._loaded_model_name = self.model_name
        except Exception as exc:
            logger.exception("DFN model load failed: %s", exc)
            self._model = None
            return False
        return True

    def reset(self) -> None:
        """Drop streaming-state buffers + reset the inference
        counter.  Called on band/mode/freq changes to prevent
        stale tail audio from bleeding into the new context."""
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)
        self._frames_processed = 0
        self._total_inference_sec = 0.0

    def set_model(self, name: str) -> None:
        """Switch model variant.  Takes effect on next reload()."""
        if name in (self.MODEL_DFN2, self.MODEL_DFN3):
            self.model_name = name
        # Note: DFN's init_df currently auto-picks the bundled model;
        # explicit model selection isn't exposed in the simplest
        # public API.  We track the operator's preference so a
        # future refactor can wire it through.

    def set_device(self, dev: str) -> None:
        """Set device preference: 'auto', 'cpu', 'cuda'."""
        if dev in (self.DEVICE_AUTO, self.DEVICE_CPU, self.DEVICE_CUDA):
            self.device_pref = dev

    @property
    def device_actual(self) -> str:
        """The device the loaded model is actually running on
        (resolved from device_pref + availability at load time)."""
        return self._device_actual

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def avg_inference_ms(self) -> float:
        """Mean inference time per frame in milliseconds.  Returns
        0 until at least one frame has been processed."""
        if self._frames_processed == 0:
            return 0.0
        avg_sec = self._total_inference_sec / self._frames_processed
        return avg_sec * 1000.0

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one audio block.  Length-preserving (with a
        small leading-edge delay during the first few frames as
        the streaming buffer fills).  Bypass-fast when disabled
        or model not loaded.
        """
        if not self.enabled or audio.size == 0:
            return audio
        if not self.is_loaded:
            # Lazy load on first use.
            if not self.reload():
                # Still failed — return input unchanged so the chain
                # keeps flowing; the Settings UI will surface the
                # error via is_available() / import_error_message().
                return audio

        x = audio.astype(np.float32, copy=False)
        # Append to input buffer.
        self._in_buf = np.concatenate([self._in_buf, x])

        # Process whole 480-sample frames.
        out_chunks: list[np.ndarray] = []
        while self._in_buf.size >= self.FRAME_SAMPLES:
            frame = self._in_buf[:self.FRAME_SAMPLES]
            self._in_buf = self._in_buf[self.FRAME_SAMPLES:]
            try:
                t0 = time.perf_counter()
                cleaned = self._enhance_frame(frame)
                self._total_inference_sec += time.perf_counter() - t0
                self._frames_processed += 1
            except Exception as exc:
                logger.warning("DFN inference failed: %s", exc)
                # Don't drop audio — pass the frame through clean.
                cleaned = frame
            out_chunks.append(cleaned)
        if out_chunks:
            self._out_buf = np.concatenate(
                [self._out_buf] + out_chunks)

        # Emit as much output as input we just consumed.  Hold
        # back the rest until enough frames are buffered to keep
        # length-preserving.  In steady state, in == out length.
        n_out = min(audio.size, self._out_buf.size)
        if n_out == 0:
            # First call before any frame completes — return zeros
            # of matching length so downstream sees no length change.
            return np.zeros_like(x)
        out, self._out_buf = (
            self._out_buf[:n_out], self._out_buf[n_out:])
        if out.size < audio.size:
            # Pad with silence — only happens during initial frames.
            out = np.concatenate([
                out, np.zeros(audio.size - out.size, dtype=np.float32)])
        return out.astype(np.float32, copy=False)

    # ── Internals ─────────────────────────────────────────────────

    def _enhance_frame(self, frame: np.ndarray) -> np.ndarray:
        """Run DFN's enhance() on a single 480-sample frame.

        DFN's public enhance() expects a torch tensor at sample
        rate self.NATIVE_SAMPLE_RATE; we convert in/out per call.
        For the streaming case we accept the per-frame conversion
        cost since DFN doesn't expose a true streaming-tensor API
        in its 0.5.x public surface.
        """
        from df.enhance import enhance
        import torch
        # DFN expects float32 [-1, 1] tensors.  Add a leading dim
        # for the channel axis (DFN was trained on 1-channel audio).
        t = torch.from_numpy(frame).unsqueeze(0)
        if self._device_actual != "cpu":
            t = t.to(self._device_actual)
        with torch.no_grad():
            cleaned = enhance(self._model, self._df_state, t)
        if self._device_actual != "cpu":
            cleaned = cleaned.cpu()
        out = cleaned.squeeze(0).numpy()
        # DFN should return same-length audio, but defensive cast
        # to the expected 480-sample frame just in case.
        if out.size != self.FRAME_SAMPLES:
            if out.size > self.FRAME_SAMPLES:
                out = out[:self.FRAME_SAMPLES]
            else:
                out = np.concatenate([
                    out,
                    np.zeros(self.FRAME_SAMPLES - out.size,
                             dtype=np.float32)])
        return out.astype(np.float32, copy=False)


# ── Self-test benchmark helper ──────────────────────────────────

def benchmark(duration_sec: float = 5.0,
              device: str = DeepFilterNetNR.DEVICE_AUTO
              ) -> dict:
    """Run a synthetic benchmark on the operator's hardware.

    Generates ``duration_sec`` worth of 48 kHz noise, feeds it
    through DeepFilterNetNR in 8192-sample blocks (matching Lyra's
    typical channel block size), and reports timing.

    Returns a dict with keys:
        available        : bool  — package importable
        device_actual    : str   — 'cuda' or 'cpu' actually used
        avg_frame_ms     : float — mean per-frame inference time
        cpu_pct_estimate : float — estimated % of one core consumed
        load_time_sec    : float — model load duration
        error            : str   — non-empty on failure

    Used by the Settings UI's "Test on your system" button so
    operators can see actual hardware cost before committing to
    the feature.
    """
    result = {
        "available": False,
        "device_actual": "",
        "avg_frame_ms": 0.0,
        "cpu_pct_estimate": 0.0,
        "load_time_sec": 0.0,
        "error": "",
    }
    if not is_available():
        result["error"] = (
            f"deepfilternet not installed: "
            f"{import_error_message() or 'unknown import error'}")
        return result
    nr = DeepFilterNetNR()
    nr.set_device(device)
    t0 = time.perf_counter()
    if not nr.reload():
        result["error"] = "model failed to load (see log)"
        return result
    result["load_time_sec"] = time.perf_counter() - t0
    result["available"] = True
    result["device_actual"] = nr.device_actual

    # Build the test signal — mid-amplitude white noise.
    rng = np.random.default_rng(0)
    n = int(duration_sec * DeepFilterNetNR.NATIVE_SAMPLE_RATE)
    signal = (rng.standard_normal(n) * 0.05).astype(np.float32)
    nr.enabled = True

    block = 8192
    t_start = time.perf_counter()
    for i in range(0, n, block):
        nr.process(signal[i:i + block])
    elapsed = time.perf_counter() - t_start

    result["avg_frame_ms"] = nr.avg_inference_ms
    # CPU% estimate = wall-clock time / audio duration × 100.  At
    # 1.0 we're using one full core to stay realtime; below 1.0 we
    # have headroom; above 1.0 we'd produce dropouts.
    result["cpu_pct_estimate"] = (
        100.0 * elapsed / duration_sec)
    return result
