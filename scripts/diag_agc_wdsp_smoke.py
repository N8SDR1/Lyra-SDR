"""Smoke test for the WDSP AGC port.

Not a full validation harness -- those need synthetic signals
across many regimes.  This is a "does it run, does it produce
plausible output" sanity check.

Run:  python scripts/diag_agc_wdsp_smoke.py
"""
from __future__ import annotations

import time
import numpy as np

from lyra.dsp.agc_wdsp import (
    WdspAgc,
    MODE_OFF,
    MODE_FAST,
    MODE_MEDIUM,
    MODE_SLOW,
    MODE_LONG,
)


def time_block(agc: WdspAgc, audio: np.ndarray, label: str) -> None:
    t0 = time.perf_counter()
    out = agc.process(audio)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    rms_in = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    rms_out = float(np.sqrt(np.mean(out.astype(np.float64) ** 2)))
    peak_in = float(np.max(np.abs(audio)))
    peak_out = float(np.max(np.abs(out)))
    print(
        f"  {label:32s} | t={elapsed_ms:6.2f} ms | "
        f"in_rms={rms_in:.4f} out_rms={rms_out:.4f} | "
        f"in_peak={peak_in:.4f} out_peak={peak_out:.4f} | "
        f"gain={agc.gain:.3f}"
    )


def main() -> None:
    rate = 48000
    block_n = 1024  # match Lyra's typical audio block size
    rng = np.random.default_rng(0xA6C)

    # ── Test 1: silence ───────────────────────────────────────────
    print("\n=== Silence (MODE_MEDIUM) ===")
    agc = WdspAgc(sample_rate=rate, mode=MODE_MEDIUM)
    silence = np.zeros(block_n, dtype=np.float32)
    for i in range(5):
        time_block(agc, silence, f"silence block {i}")

    # ── Test 2: gaussian noise (-40 dBFS) ─────────────────────────
    # Brent's "scratchy noise floor" reproducer.  Steady noise at
    # ~-40 dBFS for a few seconds.  Output RMS should be steady,
    # gain should NOT pump audibly, no overflow.
    print("\n=== Gaussian noise -40 dBFS (MODE_MEDIUM) ===")
    agc = WdspAgc(sample_rate=rate, mode=MODE_MEDIUM)
    noise_amp = 0.01  # -40 dBFS RMS roughly
    for i in range(20):
        noise = (rng.standard_normal(block_n) * noise_amp).astype(np.float32)
        time_block(agc, noise, f"noise block {i:2d}")

    # ── Test 3: sine burst over noise ─────────────────────────────
    # Models a signal arriving on a quiet band.  AGC should attack
    # cleanly without overshoot (this is what look-ahead buys us).
    print("\n=== Quiet noise + sine burst (MODE_MEDIUM) ===")
    agc = WdspAgc(sample_rate=rate, mode=MODE_MEDIUM)
    t = np.arange(block_n) / rate
    sine = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    for i in range(3):
        noise = (rng.standard_normal(block_n) * 0.005).astype(np.float32)
        time_block(agc, noise, f"pre-burst noise {i}")
    time_block(agc, sine, "SINE BURST 1 (50% peak)")
    for i in range(3):
        noise = (rng.standard_normal(block_n) * 0.005).astype(np.float32)
        time_block(agc, noise, f"post-burst noise {i}")

    # ── Test 4: impulse / pop ─────────────────────────────────────
    # Single full-scale impulse simulates a static crash.  The
    # legacy Lyra AGC stays clamped for hundreds of ms after this;
    # WDSP should fast-decay back to normal in ~25 ms (mode 4 →
    # state 1 transition with tau_fast_decay = 5 ms × 5 = 25 ms).
    print("\n=== Pop transient (MODE_MEDIUM) ===")
    agc = WdspAgc(sample_rate=rate, mode=MODE_MEDIUM)
    for i in range(2):
        noise = (rng.standard_normal(block_n) * 0.01).astype(np.float32)
        time_block(agc, noise, f"pre-pop noise {i}")
    impulse = np.zeros(block_n, dtype=np.float32)
    impulse[0] = 1.0  # full-scale impulse at sample 0
    time_block(agc, impulse, "IMPULSE (pop)")
    for i in range(8):
        noise = (rng.standard_normal(block_n) * 0.01).astype(np.float32)
        time_block(agc, noise, f"post-pop noise {i}")

    # ── Test 5: timing across all modes ───────────────────────────
    print("\n=== Per-block timing across modes ===")
    test_audio = (rng.standard_normal(block_n) * 0.1).astype(np.float32)
    for mode_id, mode_name in [
        (MODE_FAST, "FAST"),
        (MODE_MEDIUM, "MEDIUM"),
        (MODE_SLOW, "SLOW"),
        (MODE_LONG, "LONG"),
        (MODE_OFF, "OFF"),
    ]:
        agc = WdspAgc(sample_rate=rate, mode=mode_id)
        # Warm up (first call allocates caches inside numpy)
        agc.process(test_audio)
        # Median of 50 calls
        times_ms = []
        for _ in range(50):
            t0 = time.perf_counter()
            agc.process(test_audio)
            times_ms.append((time.perf_counter() - t0) * 1000.0)
        median = sorted(times_ms)[len(times_ms) // 2]
        p95 = sorted(times_ms)[int(len(times_ms) * 0.95)]
        print(
            f"  {mode_name:7s} | "
            f"median={median:5.2f} ms  p95={p95:5.2f} ms  "
            f"(budget: 21 ms)"
        )

    # ── Test 6: DC / constant signal ──────────────────────────────
    # Should NOT cause volts to grow unboundedly or NaN.
    print("\n=== DC constant 0.1 (MODE_MEDIUM) ===")
    agc = WdspAgc(sample_rate=rate, mode=MODE_MEDIUM)
    dc = (np.ones(block_n) * 0.1).astype(np.float32)
    for i in range(5):
        out = agc.process(dc)
        if not np.all(np.isfinite(out)):
            print(f"  BLOCK {i}: NON-FINITE OUTPUT (FAIL)")
        else:
            time_block(agc, dc, f"DC block {i}")

    print("\nSmoke test complete.")


if __name__ == "__main__":
    main()
