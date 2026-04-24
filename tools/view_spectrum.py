"""Plot spectrum + waterfall from a recorded I/Q .npy file.

Example:
    python tools/view_spectrum.py ft8_gain19.npy --rate 48000 --center 7074000
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt


def main():
    p = argparse.ArgumentParser()
    p.add_argument("capture", help="Path to .npy I/Q file (complex64)")
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--center", type=float, default=0.0,
                   help="Center freq Hz (for X-axis labels)")
    p.add_argument("--fft", type=int, default=4096, help="FFT size")
    p.add_argument("--overlap", type=float, default=0.5)
    args = p.parse_args()

    iq = np.load(args.capture)
    if iq.dtype not in (np.complex64, np.complex128):
        raise SystemExit(f"Expected complex I/Q, got dtype {iq.dtype}")

    n_fft = args.fft
    step = max(1, int(n_fft * (1 - args.overlap)))
    n_frames = max(1, (len(iq) - n_fft) // step + 1)

    window = np.hanning(n_fft).astype(np.float32)
    win_norm = np.sum(window ** 2)

    spec = np.empty((n_frames, n_fft), dtype=np.float32)
    for i in range(n_frames):
        seg = iq[i * step : i * step + n_fft] * window
        f = np.fft.fftshift(np.fft.fft(seg))
        spec[i] = 10.0 * np.log10((np.abs(f) ** 2) / win_norm + 1e-20)

    avg_spec = spec.mean(axis=0)

    freqs_hz = np.fft.fftshift(np.fft.fftfreq(n_fft, 1.0 / args.rate))
    x_axis = (args.center + freqs_hz) / 1e6 if args.center else freqs_hz / 1e3
    x_label = "MHz" if args.center else "kHz from DC"

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [1, 2]})

    ax1.plot(x_axis, avg_spec, color="#5ec8ff", lw=0.8)
    ax1.set_ylabel("dBFS/Hz")
    ax1.set_title(f"{Path(args.capture).name}   "
                  f"{len(iq)} samples @ {args.rate} Hz   "
                  f"mean={10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-20):.1f} dBFS")
    ax1.grid(True, alpha=0.3)
    ax1.set_facecolor("#0c1420")

    vmin = np.percentile(spec, 5)
    vmax = np.percentile(spec, 99)
    ax2.imshow(
        spec,
        aspect="auto",
        origin="upper",
        extent=[x_axis[0], x_axis[-1], len(iq) / args.rate, 0],
        cmap="inferno",
        vmin=vmin,
        vmax=vmax,
    )
    ax2.set_ylabel("time (s)")
    ax2.set_xlabel(x_label)

    fig.patch.set_facecolor("#0c1420")
    for ax in (ax1, ax2):
        for spine in ax.spines.values():
            spine.set_color("#5ec8ff")
        ax.tick_params(colors="#aaccee")
        ax.yaxis.label.set_color("#aaccee")
        ax.xaxis.label.set_color("#aaccee")
        ax.title.set_color("#e0f0ff")

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
