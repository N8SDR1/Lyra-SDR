"""A/B compare WDSP AGC modes on identical input.

If different modes produce indistinguishable output for the same
input, my port has a bug -- modes have decay constants ranging
from 50 ms (Fast) to 2000 ms (Long), they CANNOT produce identical
audio.

Run:  python -m scripts.diag_agc_wdsp_mode_compare
"""
from __future__ import annotations

import numpy as np

from lyra.dsp.agc_wdsp import (
    WdspAgc,
    MODE_FAST,
    MODE_MEDIUM,
    MODE_SLOW,
    MODE_LONG,
)


def _run_agc_through_signal(
    mode: int, signal: np.ndarray, rate: int
) -> np.ndarray:
    """Process the entire signal through a fresh WdspAgc at `mode`,
    in 512-sample blocks, returning the concatenated output.
    Fresh instance per call so state doesn't leak between modes."""
    agc = WdspAgc(sample_rate=rate, mode=mode)
    block = 512
    out_chunks = []
    for start in range(0, signal.size, block):
        chunk = signal[start:start + block].astype(np.float32)
        if chunk.size == 0:
            break
        out_chunks.append(agc.process(chunk))
    return np.concatenate(out_chunks)


def main() -> None:
    rate = 48000
    rng = np.random.default_rng(42)

    # ── Build a representative test signal ────────────────────────
    # 4 seconds total: 1 sec noise, 0.5 sec sine burst, 2 sec noise,
    # 0.5 sec sine burst.  This exercises the modes' transient
    # recovery and decay behavior — exactly what should differ
    # across Fast / Medium / Slow / Long.
    n_total = rate * 4
    t = np.arange(n_total) / rate
    # Background gaussian noise at -40 dBFS RMS
    noise = (rng.standard_normal(n_total) * 0.01).astype(np.float64)
    # Two sine bursts (1 kHz, 50% peak) for AGC to attack on
    sine_amp = 0.5
    sig = noise.copy()
    burst1_start = rate              # t=1s
    burst1_end = int(rate * 1.5)     # t=1.5s
    burst2_start = int(rate * 3.5)   # t=3.5s
    burst2_end = rate * 4            # t=4s
    sig[burst1_start:burst1_end] += (
        sine_amp * np.sin(2 * np.pi * 1000.0 * t[burst1_start:burst1_end])
    )
    sig[burst2_start:burst2_end] += (
        sine_amp * np.sin(2 * np.pi * 1000.0 * t[burst2_start:burst2_end])
    )
    sig = sig.astype(np.float32)

    # ── Run all four modes on the same signal ─────────────────────
    print(f"\nProcessing {n_total} samples ({n_total/rate:.1f}s) "
          f"through 4 modes...\n")

    outputs = {
        "FAST":   _run_agc_through_signal(MODE_FAST,   sig, rate),
        "MEDIUM": _run_agc_through_signal(MODE_MEDIUM, sig, rate),
        "SLOW":   _run_agc_through_signal(MODE_SLOW,   sig, rate),
        "LONG":   _run_agc_through_signal(MODE_LONG,   sig, rate),
    }

    # ── Sanity-check: all outputs same length ─────────────────────
    lengths = {name: out.size for name, out in outputs.items()}
    print(f"Output lengths: {lengths}")
    if len(set(lengths.values())) != 1:
        print("FAIL: output lengths differ across modes")
        return

    # ── Compare RMS over post-burst windows ──────────────────────
    # The KEY metric: how fast does each mode recover after the
    # first sine burst ends?  Fast should recover in ~50 ms,
    # Medium ~250 ms, Slow ~500 ms (+ 1000 ms hang), Long ~2000 ms
    # (+ 2000 ms hang).  Sample RMS at staged windows after
    # burst1_end (t=1.5s).
    print("\nPost-burst recovery RMS at staged times after first")
    print("burst ends (t=1.5 s).  This is the 'how quickly does")
    print("the noise return' metric -- different modes MUST differ")
    print("here unless the port has a bug.\n")

    times_after_burst_ms = [10, 50, 100, 250, 500, 1000, 1500]
    burst_end_sample = burst1_end

    print(f"{'Time after burst':>20s} | "
          f"{'FAST':>10s} | {'MEDIUM':>10s} | "
          f"{'SLOW':>10s} | {'LONG':>10s}")
    print("-" * 75)
    for delta_ms in times_after_burst_ms:
        sample_at = burst_end_sample + int(rate * delta_ms / 1000)
        # Sample RMS in a 10 ms window centered at sample_at
        win_n = int(rate * 0.010)
        s_start = max(0, sample_at - win_n // 2)
        s_end = min(n_total, sample_at + win_n // 2)
        rmss = {}
        for name, out in outputs.items():
            window = out[s_start:s_end].astype(np.float64)
            rmss[name] = float(np.sqrt(np.mean(window ** 2)))
        print(
            f"{delta_ms:>15d} ms     | "
            f"{rmss['FAST']:>10.5f} | {rmss['MEDIUM']:>10.5f} | "
            f"{rmss['SLOW']:>10.5f} | {rmss['LONG']:>10.5f}"
        )

    # ── Sample-by-sample correlation between modes ────────────────
    # If two modes produce IDENTICAL output, the correlation
    # between them is 1.0 and the abs-difference is 0.  If they
    # differ at all, both metrics will diverge.  This is the
    # "are the modes really different" smoking gun.
    print("\nMode-vs-mode metrics (should NOT be 1.0 / 0.0):")
    print(f"{'Mode A':>10s} {'Mode B':>10s} | "
          f"{'corr':>8s} {'mean|diff|':>12s} {'max|diff|':>12s}")
    print("-" * 60)
    pairs = [
        ("FAST", "MEDIUM"),
        ("FAST", "SLOW"),
        ("FAST", "LONG"),
        ("MEDIUM", "SLOW"),
        ("MEDIUM", "LONG"),
        ("SLOW", "LONG"),
    ]
    for a, b in pairs:
        out_a = outputs[a].astype(np.float64)
        out_b = outputs[b].astype(np.float64)
        # Pearson correlation
        try:
            corr = float(np.corrcoef(out_a, out_b)[0, 1])
        except Exception:
            corr = float("nan")
        mean_abs_diff = float(np.mean(np.abs(out_a - out_b)))
        max_abs_diff = float(np.max(np.abs(out_a - out_b)))
        print(
            f"{a:>10s} {b:>10s} | "
            f"{corr:>8.5f} {mean_abs_diff:>12.6f} {max_abs_diff:>12.6f}"
        )

    # ── Verdict ──────────────────────────────────────────────────
    print("\nINTERPRETATION")
    print("-" * 60)
    print("If correlation between any two modes is > 0.999 AND")
    print("max|diff| is < 0.01, the port is BROKEN -- those modes")
    print("are producing essentially identical output and the")
    print("operator-perceived 'all modes sound the same' is real.")
    print("")
    print("Healthy state: correlations should be in 0.7-0.95 range")
    print("(modes track the same signal but with different time")
    print("constants), and post-burst RMS should diverge clearly --")
    print("FAST recovering to ~normal in 50-100 ms, LONG still")
    print("clamped at 1000-1500 ms.")


if __name__ == "__main__":
    main()
