"""Bench instrument for the v0.0.9.6 audio resampler ports.

Drives synthetic signals through :class:`lyra.dsp.varsamp.VarSamp`
and :class:`lyra.dsp.rmatch.RMatch` and reports on:

  * varsamp output sample count vs. expected (verifies ratio
    accuracy at fixed ``var``)
  * varsamp output spectral content (verifies anti-alias filter)
  * rmatch ring-fill convergence under a constant rate mismatch
    (the "two-crystal drift" case the production system has to
    handle)
  * rmatch glitch counts (underflow / overflow) under various
    conditions

Run::

    python -m scripts.diag_audio_resampler

This is the v0.0.9.6 equivalent of ``diag_agc_wdsp_smoke.py`` —
self-contained verification before integration into Lyra's audio
sink.
"""
from __future__ import annotations

import time

import numpy as np

from lyra.dsp.varsamp import VarSamp
from lyra.dsp.rmatch import RMatch


# ── VarSamp: ratio + spectral fidelity ───────────────────────────────


def bench_varsamp_ratio_accuracy() -> None:
    """For a range of var values, send a long-enough run to wash out
    the per-block ±1-sample variance, and verify the AVERAGE output/
    input ratio matches the requested ratio."""
    print()
    print("=== VarSamp ratio accuracy (steady-state) ===")
    print(f"{'var':>8s}  {'expected ratio':>16s}  "
          f"{'actual ratio':>14s}  {'error (ppm)':>12s}")
    rate = 48000
    insize = 2048
    n_blocks = 200
    in_block = (0.1 * np.sin(2 * np.pi * 1000 *
                              np.arange(insize) / rate)).astype(np.float32)
    for var in [0.95, 0.99, 1.0, 1.01, 1.05]:
        vs = VarSamp(in_rate=rate, out_rate=rate, density=64)
        total_in = 0
        total_out = 0
        for _ in range(n_blocks):
            out = vs.process(in_block, var=var)
            total_in += insize
            total_out += out.size
        actual_ratio = total_out / total_in
        expected = var * 1.0  # nom_ratio = 1.0
        error_ppm = (actual_ratio / expected - 1.0) * 1e6
        print(f"{var:>8.4f}  {expected:>16.6f}  "
              f"{actual_ratio:>14.6f}  {error_ppm:>+12.1f}")


def bench_varsamp_spectral_passband() -> None:
    """Sweep a tone across the audio band, push it through varsamp at
    var=1.0 (passthrough), and measure passband flatness + stopband
    rejection."""
    print()
    print("=== VarSamp spectral response (var=1.0, 48k -> 48k) ===")
    rate = 48000
    insize = 4096
    vs = VarSamp(in_rate=rate, out_rate=rate, density=64)
    print(f"{'freq (Hz)':>10s}  {'in RMS':>8s}  "
          f"{'out RMS':>8s}  {'gain (dB)':>10s}")
    for freq in [100, 500, 1000, 3000, 6000, 10000, 15000, 20000, 22000]:
        vs.reset()
        # Drive in-block of pure tone, then read out.
        in_block = (0.5 * np.sin(2 * np.pi * freq *
                                  np.arange(insize) / rate)).astype(np.float32)
        # Push a few blocks to flush startup transients, then measure.
        for _ in range(4):
            out = vs.process(in_block, var=1.0)
        # Take RMS of the last block, skip first 256 samples for FIR
        # group-delay margin.
        if out.size > 512:
            out_rms = float(np.sqrt(np.mean(out[256:].astype(np.float64) ** 2)))
        else:
            out_rms = 0.0
        in_rms = float(np.sqrt(np.mean(in_block.astype(np.float64) ** 2)))
        gain_db = 20.0 * np.log10(max(out_rms / max(in_rms, 1e-12), 1e-12))
        print(f"{freq:>10d}  {in_rms:>8.4f}  {out_rms:>8.4f}  "
              f"{gain_db:>+10.2f}")


# ── RMatch: ring fill + control loop convergence ────────────────────


def bench_rmatch_balanced() -> None:
    """Producer + consumer running at exactly nominal rate.  var
    should converge to ~1.0; ring should hold near rsize/2."""
    print()
    print("=== RMatch balanced (no drift) ===")
    rm = RMatch(insize=2048, outsize=512,
                nom_inrate=48000, nom_outrate=48000, density=64)
    print(f"  ringsize={rm.ringsize}, target n_ring={rm.rsize // 2}")
    in_block = (0.1 * np.sin(2 * np.pi * 1000 *
                              np.arange(2048) / 48000)).astype(np.float32)
    print(f"{'cycle':>6s}  {'var':>8s}  {'n_ring':>8s}  "
          f"{'underflows':>10s}  {'overflows':>10s}  {'ctl':>4s}")
    for cycle in range(0, 200, 20):
        for _ in range(20):
            rm.write(in_block)
            for _ in range(4):
                rm.read(512)
        d = rm.diagnostics()
        print(f"{cycle:>6d}  {d['var']:>8.4f}  {d['n_ring']:>8d}  "
              f"{d['underflows']:>10d}  {d['overflows']:>10d}  "
              f"{'ON' if d['control_active'] else 'off':>4s}")


def bench_rmatch_constant_drift(drift_ppm: float) -> None:
    """Simulate a constant rate mismatch — producer runs at
    `48000 * (1 + drift_ppm * 1e-6)` while consumer runs at exactly
    48000.  Ring drift should drive var to compensate."""
    print()
    print(f"=== RMatch constant +{drift_ppm:.0f} ppm producer drift ===")
    rm = RMatch(insize=2048, outsize=512,
                nom_inrate=48000, nom_outrate=48000, density=64,
                ff_alpha=0.05, prop_gain=0.01)
    # Simulate drift by sending an extra sample every N cycles.
    # +drift_ppm ppm = 1 extra input per 1e6/drift_ppm samples.
    # At 2048 samples/cycle, that's 1 extra cycle every
    # 1e6 / (drift_ppm * 2048) cycles.
    in_block = (0.1 * np.sin(2 * np.pi * 1000 *
                              np.arange(2048) / 48000)).astype(np.float32)
    extra_cycle = max(1, int(1e6 / (drift_ppm * 2048)))
    print(f"  extra input every {extra_cycle} cycles")
    print(f"{'cycle':>6s}  {'var':>8s}  {'n_ring':>8s}  "
          f"{'underflows':>10s}  {'overflows':>10s}")
    n_cycles = max(500, extra_cycle * 4)
    for cycle in range(n_cycles):
        rm.write(in_block)
        if cycle % extra_cycle == 0:
            rm.write(in_block)  # bonus write = drift
        for _ in range(4):
            rm.read(512)
        if cycle % (n_cycles // 10) == 0:
            d = rm.diagnostics()
            print(f"{cycle:>6d}  {d['var']:>8.4f}  {d['n_ring']:>8d}  "
                  f"{d['underflows']:>10d}  {d['overflows']:>10d}")


def main() -> None:
    print("=" * 60)
    print("v0.0.9.6 audio resampler bench")
    print("=" * 60)
    bench_varsamp_ratio_accuracy()
    bench_varsamp_spectral_passband()
    bench_rmatch_balanced()
    bench_rmatch_constant_drift(50.0)
    print()
    print("=" * 60)
    print("INTERPRETATION")
    print("=" * 60)
    print("VarSamp ratio: error_ppm should be < 100 for var ~= 1.0")
    print("(steady-state ratio accuracy of the polyphase resampler).")
    print()
    print("VarSamp spectrum: passband (100 Hz - ~21 kHz at fs=48k)")
    print("should be flat within ±1 dB; stopband (>22 kHz) should")
    print("be -40 dB or better.  Hamming-windowed firwin gets ~-50 dB.")
    print()
    print("RMatch balanced: var should drift toward 1.0 over time;")
    print("n_ring should stay close to rsize/2 = ringsize/2.  ")
    print("Underflow/overflow counts should stop growing once the")
    print("control loop locks in.  If var pegs at the clamp, the")
    print("gain parameters need re-tuning for this insize/outsize/")
    print("rate combination.")
    print()
    print("RMatch constant drift: var should converge toward")
    print("(1 + drift_ppm*1e-6).  Underflow count should stay low.")
    print("Overflow count may spike during transient but should")
    print("plateau once var locks in.")


if __name__ == "__main__":
    main()
