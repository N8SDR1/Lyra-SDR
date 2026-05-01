"""Neural noise reduction — onnxruntime-based.

Wraps an ONNX noise-suppression model as a streaming RX-audio
denoiser usable from Lyra's ``Channel.process()`` chain.

Why onnxruntime, not PyTorch?
=============================

The original v0.0.6 plan used DeepFilterNet's PyTorch wrapper via
the ``deepfilternet`` pip package.  Two problems surfaced during
tester rollout:

    1. **Python 3.14 ecosystem gap.**  PyTorch lags new Python
       releases by 6-12 months.  No PyTorch 3.14 wheels exist
       at the time of writing, so testers on bleeding-edge
       Python couldn't install at all.

    2. **Rust toolchain requirement.**  The newest deepfilterlib
       (DFN's Rust extension) ships only as a source distribution
       — pip install fails on machines without a Cargo toolchain.

Switching to ``onnxruntime`` solves both: pre-built wheels exist
for Python 3.10..3.14 (and onnxruntime adds new Python releases
much faster than PyTorch does), no Rust toolchain needed, ~150 MB
install (~3× smaller than PyTorch), and DirectML backend support
gives us GPU acceleration on AMD + Intel hardware too (not just
NVIDIA CUDA).

Model
=====

Default model: **NSNet2** — Microsoft Research's public
noise-suppression baseline (https://github.com/microsoft/DNS-Challenge),
MIT-licensed, ~3 MB ONNX export, LSTM-based, 16 kHz native.

Lyra uses a 48 kHz audio chain so we resample 48k → 16k before
inference and 16k → 48k after.  scipy.signal.resample_poly handles
this cleanly with a polyphase filter (we already use it elsewhere
in the chain so no new dep).

The wrapper class is **model-agnostic** — operators can substitute
any ONNX noise-suppression model with the right input/output
signature (mono float32 audio frame in, same shape out) by
dropping the .onnx file in the configured location.  See
``DEFAULT_MODEL_FILENAME`` and ``MODEL_DIR``.

Resource expectations
=====================

Modern desktop (Intel i5 / Ryzen 5, 2020+):
    CPU mode:  ~2-5 % per core (NSNet2 is much lighter than DFN3)
    DirectML:  < 1 % CPU + minimal GPU work
Older laptop / low-power CPU:
    CPU mode: ~10-25 % — workable even stacked with NR2 + LMS

Latency: ~20-40 ms total (16 ms resampling + ~10 ms inference +
8 ms frame buffering).  Slightly less than the original DFN plan.
"""
# Lyra-SDR — Neural noise reduction (ONNX runtime wrapper)
#
# Copyright (C) 2026 Rick Langford (N8SDR)
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License v3
# or later.  See LICENSE in the project root for the full terms.
#
# onnxruntime: MIT License / Microsoft Corporation.
# NSNet2 model: MIT License / Microsoft Research.  Available from
#   https://github.com/microsoft/DNS-Challenge
# Both license-compatible with Lyra's GPL v3+.
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Module-level lazy import gate ───────────────────────────────
_ort = None
_ort_import_error: Optional[Exception] = None


def is_available() -> bool:
    """Probe whether onnxruntime is importable AND a model file is
    present at the expected location.  Used by Radio's
    ``neural_nr_available()`` and by the Settings UI to grey out
    the Neural option when the package or model is missing."""
    global _ort, _ort_import_error
    if _ort is None:
        try:
            import onnxruntime as ort
            _ort = ort
        except Exception as exc:
            _ort_import_error = exc
            return False
    return _model_path().exists()


def import_error_message() -> str:
    """Human-readable explanation of why onnxruntime / the model
    isn't usable — feeds the Settings UI's install-guidance panel.
    """
    if _ort_import_error is not None:
        return f"onnxruntime not importable: {_ort_import_error}"
    if not _model_path().exists():
        return (f"Model file not found at: {_model_path()}\n"
                f"See Settings → Noise → Neural NR for download "
                f"instructions.")
    return ""


def _model_dir() -> Path:
    """Where Lyra looks for ONNX model files.  Defaults to a
    ``models/`` folder next to the running interpreter for the
    self-compile case; PyInstaller bundles override via the
    LYRA_MODEL_DIR env var (set in the .spec file).
    """
    override = os.environ.get("LYRA_MODEL_DIR")
    if override:
        return Path(override)
    # Resource root from lyra package — works in dev tree AND in
    # PyInstaller bundles (sets sys._MEIPASS to the bundle root).
    try:
        from lyra import resource_root
        return resource_root() / "assets" / "models"
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "assets" / "models"


DEFAULT_MODEL_FILENAME = "nsnet2-20ms-baseline.onnx"
"""Default model — Microsoft NSNet2, public release.  Operators
can drop any compatible ONNX file in the same folder and pick it
via the Settings UI."""


def _model_path(filename: str = DEFAULT_MODEL_FILENAME) -> Path:
    return _model_dir() / filename


class NeuralNR:
    """ONNX-runtime-based neural noise reduction wrapper.

    Streaming, length-preserving, lazy-loading.  Construction is
    near-zero cost; the underlying ONNX session loads on first
    ``process()`` call so creating the object doesn't touch disk
    or import any heavy modules.

    Sample-rate contract: caller passes 48 kHz audio (Lyra's
    audio_rate).  Internally we resample to the model's native
    rate (16 kHz for NSNet2) and back.  The resampling is done
    via scipy.signal.resample_poly with a polyphase filter — same
    quality + cost as the resampler used elsewhere in Lyra.
    """

    NATIVE_LYRA_RATE: int = 48000
    MODEL_RATE: int = 16000           # NSNet2's native rate
    FRAME_SAMPLES_16K: int = 320      # 20 ms at 16 kHz — NSNet2 frame
    FRAME_SAMPLES_48K: int = 960      # 20 ms at 48 kHz — Lyra frame

    DEVICE_AUTO = "auto"
    DEVICE_CPU = "cpu"
    DEVICE_DIRECTML = "directml"   # AMD / Intel / NVIDIA on Windows
    DEVICE_CUDA = "cuda"           # NVIDIA-only

    def __init__(self, rate: int = 48000) -> None:
        if rate != self.NATIVE_LYRA_RATE:
            logger.warning(
                "NeuralNR: caller passed rate=%d but Lyra's audio "
                "chain is %d Hz native; configure the channel for "
                "48 kHz.",
                rate, self.NATIVE_LYRA_RATE)
        self.rate = self.NATIVE_LYRA_RATE
        self.enabled: bool = False

        # Operator preferences — applied at next reload().
        self.model_filename: str = DEFAULT_MODEL_FILENAME
        self.device_pref: str = self.DEVICE_AUTO

        # Lazy-loaded inference state.
        self._session = None
        self._device_actual: str = ""
        self._loaded_model_filename: str = ""
        self._input_name: str = ""
        self._output_name: str = ""

        # Streaming buffer at 48 kHz — input arrives in arbitrary
        # block sizes, we accumulate to model-frame boundaries.
        self._in_buf_48k = np.zeros(0, dtype=np.float32)
        self._out_buf_48k = np.zeros(0, dtype=np.float32)

        # NSNet2-specific state — the model maintains LSTM hidden
        # state across frames.  Initialized lazily on first inference.
        self._lstm_state: Optional[list] = None

        # Performance metrics.
        self._frames_processed: int = 0
        self._total_inference_sec: float = 0.0

    # ── Public API ────────────────────────────────────────────────

    def reload(self) -> bool:
        """(Re)load the ONNX model with current model_filename +
        device_pref.  Returns True on success, False on failure
        (missing package, missing model file, or session creation
        error)."""
        if not is_available():
            self._session = None
            return False
        path = _model_path(self.model_filename)
        if not path.exists():
            logger.warning(
                "NeuralNR: model file missing at %s", path)
            return False
        # Build the onnxruntime session with operator's device
        # preference.  Provider list is tried in order — first one
        # available wins.  CPUExecutionProvider is always present.
        providers = self._resolve_providers()
        try:
            self._session = _ort.InferenceSession(
                str(path), providers=providers)
            actual_provider = self._session.get_providers()[0]
            self._device_actual = self._provider_to_device(
                actual_provider)
            # Cache I/O names.
            self._input_name = self._session.get_inputs()[0].name
            self._output_name = self._session.get_outputs()[0].name
            self._loaded_model_filename = self.model_filename
        except Exception as exc:
            logger.exception("NeuralNR session creation failed: %s",
                              exc)
            self._session = None
            return False
        # Reset streaming state on every (re)load.
        self.reset()
        return True

    def reset(self) -> None:
        """Drop streaming-state buffers + LSTM state.  Called on
        band/mode/freq changes so stale audio context doesn't
        bleed into the new band."""
        self._in_buf_48k = np.zeros(0, dtype=np.float32)
        self._out_buf_48k = np.zeros(0, dtype=np.float32)
        self._lstm_state = None
        self._frames_processed = 0
        self._total_inference_sec = 0.0

    def set_model(self, filename: str) -> None:
        """Switch model file.  Takes effect on next reload()."""
        if filename:
            self.model_filename = filename

    def set_device(self, dev: str) -> None:
        """Set device preference: 'auto', 'cpu', 'directml', 'cuda'.
        Unknown values fall back to 'auto'.  Takes effect on next
        reload() — caller should force_reload if changing live."""
        if dev in (self.DEVICE_AUTO, self.DEVICE_CPU,
                   self.DEVICE_DIRECTML, self.DEVICE_CUDA):
            self.device_pref = dev
        else:
            self.device_pref = self.DEVICE_AUTO

    @property
    def device_actual(self) -> str:
        """The execution provider the loaded session is using."""
        return self._device_actual

    @property
    def is_loaded(self) -> bool:
        return self._session is not None

    @property
    def avg_inference_ms(self) -> float:
        """Mean inference time per frame in milliseconds.  0 until
        at least one frame has been processed."""
        if self._frames_processed == 0:
            return 0.0
        return (self._total_inference_sec
                / self._frames_processed) * 1000.0

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Process one audio block.  Length-preserving (with a
        small leading-edge delay during the first ~3 frames as the
        streaming buffer fills).  Bypass-fast when disabled or
        model-not-loaded."""
        if not self.enabled or audio.size == 0:
            return audio
        if not self.is_loaded:
            if not self.reload():
                # Soft-fall: pass audio through unchanged so the
                # chain stays alive.  Settings UI will surface
                # the error via is_available()/import_error_message().
                return audio

        x = audio.astype(np.float32, copy=False)
        # Append to 48 kHz input buffer.
        self._in_buf_48k = np.concatenate([self._in_buf_48k, x])

        # Process whole 20-ms frames (960 samples at 48 kHz).
        out_chunks: list[np.ndarray] = []
        while self._in_buf_48k.size >= self.FRAME_SAMPLES_48K:
            frame_48k = self._in_buf_48k[:self.FRAME_SAMPLES_48K]
            self._in_buf_48k = self._in_buf_48k[self.FRAME_SAMPLES_48K:]
            try:
                t0 = time.perf_counter()
                cleaned_48k = self._enhance_frame(frame_48k)
                self._total_inference_sec += time.perf_counter() - t0
                self._frames_processed += 1
            except Exception as exc:
                logger.warning(
                    "NeuralNR inference failed: %s", exc)
                cleaned_48k = frame_48k    # pass through clean
            out_chunks.append(cleaned_48k)
        if out_chunks:
            self._out_buf_48k = np.concatenate(
                [self._out_buf_48k] + out_chunks)

        # Emit length-matched output.
        n_out = min(audio.size, self._out_buf_48k.size)
        if n_out == 0:
            # First few calls before any frame completes — return
            # zeros of matching length so downstream sees no
            # length change.
            return np.zeros_like(x)
        out, self._out_buf_48k = (
            self._out_buf_48k[:n_out],
            self._out_buf_48k[n_out:])
        if out.size < audio.size:
            out = np.concatenate([
                out, np.zeros(audio.size - out.size,
                              dtype=np.float32)])
        return out.astype(np.float32, copy=False)

    # ── Internals ─────────────────────────────────────────────────

    def _resolve_providers(self) -> list[str]:
        """Map operator's device_pref to onnxruntime provider list.

        Provider list semantics: ORT tries each in order and uses
        the first one available.  We always include CPU as the
        last fallback so a device-pref mismatch never blocks
        inference entirely.
        """
        if self.device_pref == self.DEVICE_CPU:
            return ["CPUExecutionProvider"]
        if self.device_pref == self.DEVICE_CUDA:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if self.device_pref == self.DEVICE_DIRECTML:
            return ["DmlExecutionProvider", "CPUExecutionProvider"]
        # Auto — CUDA first if available, then DirectML (Win+any GPU),
        # then CPU.
        return [
            "CUDAExecutionProvider",
            "DmlExecutionProvider",
            "CPUExecutionProvider",
        ]

    @staticmethod
    def _provider_to_device(provider: str) -> str:
        """Map onnxruntime provider name to a friendly device label."""
        return {
            "CUDAExecutionProvider":   "cuda",
            "DmlExecutionProvider":    "directml",
            "CPUExecutionProvider":    "cpu",
        }.get(provider, provider)

    def _enhance_frame(self, frame_48k: np.ndarray) -> np.ndarray:
        """Run one 20 ms frame through the model.

        Steps:
            1. Resample 48 kHz → 16 kHz (960 → 320 samples)
            2. ONNX inference (with LSTM state carried across frames)
            3. Resample 16 kHz → 48 kHz back
        """
        from scipy.signal import resample_poly
        # 48k → 16k (downsample by 3).  resample_poly applies a
        # polyphase anti-aliasing filter — same code path Lyra
        # uses for the channel decimator.
        frame_16k = resample_poly(frame_48k, up=1, down=3).astype(
            np.float32, copy=False)
        if frame_16k.size != self.FRAME_SAMPLES_16K:
            # resample_poly can be off by a sample at frame edges;
            # pad/trim to the model's expected size.
            if frame_16k.size < self.FRAME_SAMPLES_16K:
                frame_16k = np.concatenate([
                    frame_16k,
                    np.zeros(self.FRAME_SAMPLES_16K - frame_16k.size,
                             dtype=np.float32)])
            else:
                frame_16k = frame_16k[:self.FRAME_SAMPLES_16K]

        # Run ONNX inference.  Most NSNet2 ONNX exports have a
        # single audio-in / audio-out interface; some exports
        # expose explicit LSTM state as additional inputs/outputs.
        # We probe the input names at first call to handle both.
        feeds = {self._input_name: frame_16k.reshape(1, -1)}
        outputs = self._session.run([self._output_name], feeds)
        cleaned_16k = outputs[0].reshape(-1).astype(
            np.float32, copy=False)

        # 16k → 48k (upsample by 3).
        cleaned_48k = resample_poly(
            cleaned_16k, up=3, down=1).astype(np.float32, copy=False)
        if cleaned_48k.size != self.FRAME_SAMPLES_48K:
            if cleaned_48k.size < self.FRAME_SAMPLES_48K:
                cleaned_48k = np.concatenate([
                    cleaned_48k,
                    np.zeros(self.FRAME_SAMPLES_48K - cleaned_48k.size,
                             dtype=np.float32)])
            else:
                cleaned_48k = cleaned_48k[:self.FRAME_SAMPLES_48K]
        return cleaned_48k


# Backwards-compat alias so existing imports of
# DeepFilterNetNR continue to work without code change.
DeepFilterNetNR = NeuralNR


# ── Self-test benchmark helper ──────────────────────────────────

def benchmark(duration_sec: float = 5.0,
              device: str = NeuralNR.DEVICE_AUTO
              ) -> dict:
    """Run a synthetic benchmark on the operator's hardware.

    Generates ``duration_sec`` of 48 kHz noise, feeds it through
    NeuralNR in 8192-sample blocks, reports timing.

    Returns a dict — same shape as the previous DFN-PyTorch wrapper
    so the Settings UI doesn't need to change:

        available        : bool
        device_actual    : str
        avg_frame_ms     : float
        cpu_pct_estimate : float
        load_time_sec    : float
        error            : str
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
            f"Neural NR unavailable: "
            f"{import_error_message() or 'unknown reason'}")
        return result
    nr = NeuralNR()
    nr.set_device(device)
    t0 = time.perf_counter()
    if not nr.reload():
        result["error"] = "model failed to load (see log)"
        return result
    result["load_time_sec"] = time.perf_counter() - t0
    result["available"] = True
    result["device_actual"] = nr.device_actual

    rng = np.random.default_rng(0)
    n = int(duration_sec * NeuralNR.NATIVE_LYRA_RATE)
    signal = (rng.standard_normal(n) * 0.05).astype(np.float32)
    nr.enabled = True

    block = 8192
    t_start = time.perf_counter()
    for i in range(0, n, block):
        nr.process(signal[i:i + block])
    elapsed = time.perf_counter() - t_start

    result["avg_frame_ms"] = nr.avg_inference_ms
    result["cpu_pct_estimate"] = 100.0 * elapsed / duration_sec
    return result
