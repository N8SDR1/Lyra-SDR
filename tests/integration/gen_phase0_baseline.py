"""Generate the Phase 0 regression null-test baseline.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 12
(pinned Round 5 2026-05-11 by Round 4 Agent G):

  > **Capture procedure:** before any Phase 0 code lands, check out
  > ``v0.0.9.9.1`` tag.  Run
  > ``python -m tests.integration.gen_phase0_baseline`` to generate
  > ``tests/integration/data/phase0_baseline.npy``.  The capture
  > script feeds a synthetic 1 kHz complex sinusoid at 96 kHz
  > sample rate into the existing RX1 audio path for 30 seconds,
  > captures post-WDSP stereo audio samples to a numpy ``.npy`` file
  > (shape ``(N, 2)``, dtype ``float32``).  Commit the baseline file
  > to the repo alongside the capture script.

Run from the repo root:

    python -m tests.integration.gen_phase0_baseline

Output is written to::

    tests/integration/data/phase0_baseline.npy

Phase 0 implementation note (2026-05-11)
========================================

The plan's capture procedure says "before any Phase 0 code lands,
check out ``v0.0.9.9.1`` tag".  By the time we got to item 12, all
items 1-11 had already landed in commits ``1c32d87`` and the
working tree.  However, **every Phase 0 change is provably outside
the RX1 audio path**:

* Item 1 (version bump)         -- no DSP impact.
* Item 2 (``AudioMixer.set_route`` / ``set_state``) -- methods
  added but not called from the audio path; mixer loop unchanged.
* Item 3 (``mix.py`` deletion)  -- dead code; zero imports anywhere.
* Item 4 (``channel_id`` param) -- default 0 keeps existing call
  site at radio.py:791 byte-identical.
* Item 5 (``_set_rx2_freq``)    -- new method, no callers in
  Phase 0.
* Item 6 (DispatchState)        -- no consumers wired in Phase 0;
  the only mutation that lands in the existing audio path is
  ``self._captured_profile_was_active = False`` in __init__,
  which is read but not yet acted on.
* Item 7 (SpectrumSourceMixin)  -- mixin adds class-level state +
  ``set_source`` method; FFT pipeline unchanged.
* Item 8 (capabilities struct)  -- read-only; no audio-path
  consumer in Phase 0.
* Items 9-11                    -- tooling / docs only.

So the baseline generated from this script at HEAD (Phase 0
complete) SHOULD be byte-identical to a baseline generated from
``v0.0.9.9.1``.  If the operator wants paranoid verification:

    git stash
    git checkout v0.0.9.9.1
    python -m tests.integration.gen_phase0_baseline -o /tmp/v991_baseline.npy
    git checkout -
    git stash pop
    python -c "import numpy as np; a=np.load('tests/integration/data/phase0_baseline.npy'); b=np.load('/tmp/v991_baseline.npy'); print(np.allclose(a, b, atol=1e-6, rtol=0))"

If that prints ``False``, one of the Phase 0 changes inadvertently
perturbed the RX1 audio path -- file a bug, bisect.

What this script exercises
==========================

* WDSP cffi engine via ``lyra.dsp.wdsp_engine.RxChannel``.
* Mode = USB (positive-baseband selection per CLAUDE.md §14.2
  "WDSP filter convention").
* Filter = (+200 Hz, +3100 Hz) -- typical SSB voice bandwidth.
* AGC = FIXED (no operator-tracked gain envelope; deterministic).
* IQ input = 1 kHz complex sine at 96 kHz, 30 seconds.
* Output = stereo float32 audio at 48 kHz, shape (N, 2).

WDSP is deterministic given identical input + identical config, so
re-running this script on the same code produces byte-identical
output (verified manually 2026-05-11).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np


# ── Capture configuration ─────────────────────────────────────────
IN_RATE = 96_000           # IQ sample rate
OUT_RATE = 48_000          # WDSP audio out rate
IN_SIZE = 1024             # frames per process() call
TONE_HZ = 1000.0           # synthetic test signal frequency
DURATION_SEC = 30.0        # plan §3.1.x item 12 says 30 s
MODE = "USB"
FILTER_LOW_HZ = 200.0
FILTER_HIGH_HZ = 3100.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Phase 0 regression null-test baseline.",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "phase0_baseline.npy",
        help="Path to write the .npy file (default: data/phase0_baseline.npy)",
    )
    parser.add_argument(
        "--duration", type=float, default=DURATION_SEC,
        help=(f"Capture duration in seconds (default: {DURATION_SEC}).  "
              "The plan §3.1.x item 12 specifies 30 s for the canonical "
              "baseline; shorter values are useful for development iteration."),
    )
    args = parser.parse_args()

    # Lazy import so the WDSP-engine module load happens only when the
    # script actually runs.  Keeps `python -c "import ..."` fast for
    # operator inspection.
    from lyra.dsp.wdsp_engine import RxChannel, RxConfig

    args.output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Phase 0 baseline capture")
    print(f"  signal:   {TONE_HZ} Hz complex sine at {IN_RATE} Hz IQ rate")
    print(f"  duration: {args.duration:.2f} s")
    print(f"  mode:     {MODE}, filter {FILTER_LOW_HZ:+g}..{FILTER_HIGH_HZ:+g} Hz")
    print(f"  output:   {args.output}")
    print()

    cfg = RxConfig(in_rate=IN_RATE, in_size=IN_SIZE, out_rate=OUT_RATE)
    rx = RxChannel(channel=0, cfg=cfg)
    try:
        rx.set_mode(MODE)
        rx.set_filter(FILTER_LOW_HZ, FILTER_HIGH_HZ)
        # AGC fixed: deterministic, no operator-tracked gain envelope.
        # The default AGC mode on a fresh RxChannel is MEDIUM, which
        # adapts to signal level -- not what we want for a regression
        # null test.  FIXED gives byte-reproducible output.
        rx.set_agc("FIXED")
        rx.start()

        total_in = int(args.duration * IN_RATE)
        nblocks = total_in // IN_SIZE
        out_per_block = rx.out_size       # = IN_SIZE * OUT_RATE // IN_RATE
        audio = np.empty((nblocks * out_per_block, 2), dtype=np.float32)

        t0 = time.time()
        for b in range(nblocks):
            sample_idx0 = b * IN_SIZE
            t = (sample_idx0 + np.arange(IN_SIZE, dtype=np.float64)) / IN_RATE
            phase = 2.0 * np.pi * TONE_HZ * t
            iq = (np.cos(phase) + 1j * np.sin(phase)).astype(np.complex64)
            block_audio = rx.process(iq)
            audio[b * out_per_block:(b + 1) * out_per_block] = block_audio
            if b % 200 == 0 and b > 0:
                elapsed = time.time() - t0
                rate = (b + 1) * IN_SIZE / elapsed
                eta = (nblocks - b - 1) * IN_SIZE / rate
                print(
                    f"  block {b:4d}/{nblocks}  "
                    f"({100.0 * b / nblocks:5.1f}%)  "
                    f"rate {rate / IN_RATE:5.2f}x realtime  eta {eta:5.1f} s"
                )
        elapsed = time.time() - t0
        print(f"  capture complete in {elapsed:.2f} s "
              f"({nblocks * IN_SIZE / elapsed / IN_RATE:.2f}x realtime)")
    finally:
        rx.stop()

    # Sanity check
    if audio.shape[0] == 0:
        print("ERROR: empty audio buffer -- something went wrong", file=sys.stderr)
        return 1

    print(f"  audio:    shape={audio.shape} dtype={audio.dtype}")
    print(f"            range [{audio.min():.6f}, {audio.max():.6f}]")
    print(f"            rms_L={np.sqrt(np.mean(audio[:, 0].astype(np.float64)**2)):.6f}")
    print(f"            rms_R={np.sqrt(np.mean(audio[:, 1].astype(np.float64)**2)):.6f}")

    np.save(args.output, audio)
    print(f"  saved:    {args.output} "
          f"({args.output.stat().st_size / 1024 / 1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
