"""Phase 0 regression null test -- no audible behavior change.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 12:

  > **Phase 0 test:** ``tests/integration/test_phase0_null.py`` runs
  > the same synthetic 1 kHz input through Phase 0's modified RX1
  > path, captures the same shape, and asserts
  > ``numpy.allclose(actual, expected, atol=1e-6, rtol=0)``.

  > **Acceptance criterion:** ε ≤ 1e-6 per sample (~120 dB dynamic
  > range; well above any post-WDSP quantization noise since WDSP
  > cffi processing is deterministic).

  > "No behavior change" upgraded from operator-listening-vibe to
  > numeric assertion.

Run from repo root::

    python -m unittest tests.integration.test_phase0_null -v

To regenerate the baseline (e.g. after a deliberate Phase 1+ change
that does perturb the audio path)::

    python -m tests.integration.gen_phase0_baseline

then commit the updated ``phase0_baseline.npy`` alongside the change
that justifies it.  Without the regeneration step, Phase 1+
implementers can't accidentally introduce a silent regression.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np


BASELINE_PATH = (
    Path(__file__).resolve().parent / "data" / "phase0_baseline.npy"
)

# Capture configuration -- MUST match gen_phase0_baseline.py exactly.
# Keeping these in sync is enforced manually (one source of truth would
# require a third module; for two files that change rarely it's not
# worth the indirection).  Differences here would cause spurious
# failures -- if you change the baseline generator, mirror the changes
# here AND regenerate the .npy.
IN_RATE = 96_000
OUT_RATE = 48_000
IN_SIZE = 1024
TONE_HZ = 1000.0
DURATION_SEC = 30.0
MODE = "USB"
FILTER_LOW_HZ = 200.0
FILTER_HIGH_HZ = 3100.0

ATOL = 1e-6        # ~120 dB dynamic range below full-scale
RTOL = 0.0


class Phase0NullTest(unittest.TestCase):
    """Numeric "no behavior change" gate per consensus-plan §3.1.x.12."""

    @classmethod
    def setUpClass(cls) -> None:
        if not BASELINE_PATH.exists():
            raise unittest.SkipTest(
                f"Baseline {BASELINE_PATH} not found.  Run "
                f"`python -m tests.integration.gen_phase0_baseline` "
                f"to generate it."
            )
        cls.expected = np.load(BASELINE_PATH)

    def test_rx1_path_byte_identical_to_baseline(self) -> None:
        """The exact assertion the plan specifies:
        ``numpy.allclose(actual, expected, atol=1e-6, rtol=0)``.

        WDSP cffi processing is deterministic given identical inputs
        + configuration, so the realistic outcome on Phase 0
        (which is supposed to be audio-path-byte-identical to
        v0.0.9.9.1) is exact match.  The 1e-6 tolerance gives ~120
        dB headroom for anything that's NOT byte-identical to still
        pass -- e.g., a future refactor that legitimately changes
        float-summation order.
        """
        from lyra.dsp.wdsp_engine import RxChannel, RxConfig

        cfg = RxConfig(in_rate=IN_RATE, in_size=IN_SIZE, out_rate=OUT_RATE)
        rx = RxChannel(channel=0, cfg=cfg)
        try:
            rx.set_mode(MODE)
            rx.set_filter(FILTER_LOW_HZ, FILTER_HIGH_HZ)
            rx.set_agc("FIXED")
            rx.start()

            total_in = int(DURATION_SEC * IN_RATE)
            nblocks = total_in // IN_SIZE
            out_per_block = rx.out_size
            actual = np.empty(
                (nblocks * out_per_block, 2), dtype=np.float32
            )

            for b in range(nblocks):
                sample_idx0 = b * IN_SIZE
                t = ((sample_idx0 + np.arange(IN_SIZE, dtype=np.float64))
                     / IN_RATE)
                phase = 2.0 * np.pi * TONE_HZ * t
                iq = (np.cos(phase) + 1j * np.sin(phase)).astype(
                    np.complex64
                )
                actual[b * out_per_block:(b + 1) * out_per_block] = \
                    rx.process(iq)
        finally:
            rx.stop()

        # Shape must match (catches a sample-rate-config drift before
        # the slower allclose comparison even runs).
        self.assertEqual(
            actual.shape, self.expected.shape,
            f"shape drift: baseline={self.expected.shape} "
            f"actual={actual.shape}"
        )
        # The plan's exact assertion.
        if not np.allclose(actual, self.expected, atol=ATOL, rtol=RTOL):
            # Compute and report the worst per-sample delta so a
            # regression is diagnosable from the test output, not
            # just "assertion failed".
            diff = np.abs(actual.astype(np.float64)
                          - self.expected.astype(np.float64))
            self.fail(
                f"Phase 0 RX1 audio path diverged from baseline.\n"
                f"  max |delta|  = {diff.max():.3e}  (limit {ATOL:.0e})\n"
                f"  mean |delta| = {diff.mean():.3e}\n"
                f"  samples above tol: "
                f"{int((diff > ATOL).sum())} / {diff.size} "
                f"({100.0 * (diff > ATOL).sum() / diff.size:.4f}%)\n"
                f"  If this change is INTENTIONAL (e.g., a Phase 1+ "
                f"audio refactor), regenerate the baseline via "
                f"`python -m tests.integration.gen_phase0_baseline` "
                f"and commit the updated .npy alongside this change."
            )


if __name__ == "__main__":
    unittest.main()
