"""Record N seconds of I/Q from the HL2 to a .npy file.

Example:
    python tools/record_iq.py --ip 10.10.30.100 --seconds 5 --rate 48000 \
        --freq 7074000 --out capture.npy
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lyra.protocol.stream import HL2Stream, FrameStats  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ip", required=True, help="HL2 IP address")
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--rate", type=int, default=48000, choices=[48000, 96000, 192000, 384000])
    p.add_argument("--freq", type=int, default=None, help="RX1 freq Hz (optional)")
    p.add_argument("--gain", type=int, default=19,
                   help="LNA gain dB, -12..+48 (default 19)")
    p.add_argument("--out", default="capture.npy")
    args = p.parse_args()

    buf: list[np.ndarray] = []

    def on_samples(samples, stats: FrameStats):
        buf.append(samples)

    stream = HL2Stream(args.ip, sample_rate=args.rate)
    print(f"Starting stream to {args.ip} at {args.rate} Hz...")
    stream.start(on_samples=on_samples, rx_freq_hz=args.freq, lna_gain_db=args.gain)

    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.seconds:
            time.sleep(0.1)
            s = stream.stats
            elapsed = time.monotonic() - t0
            print(
                f"\r  t={elapsed:5.2f}s  frames={s.frames:6d}  "
                f"samples={s.samples:8d}  seq_err={s.seq_errors}",
                end="",
                flush=True,
            )
    finally:
        stream.stop()
        print()

    if not buf:
        print("No samples received. Firewall blocking the radio's UDP replies?")
        sys.exit(1)

    iq = np.concatenate(buf)
    np.save(args.out, iq)

    # Quick sanity numbers
    power_db = 10.0 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-20)
    print(f"Saved {iq.shape[0]} samples to {args.out}")
    print(f"Mean power: {power_db:.1f} dBFS   Peak |I|: {np.max(np.abs(iq.real)):.4f}   "
          f"Peak |Q|: {np.max(np.abs(iq.imag)):.4f}")
    print(f"Duration (actual): {iq.shape[0] / args.rate:.3f} s "
          f"(expected {args.seconds:.3f} s)")


if __name__ == "__main__":
    main()
