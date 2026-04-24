"""Low-level probe: send start, print raw UDP bytes regardless of format.

If firewall blocks us:  total_bytes = 0
If frames arrive but parser rejects them:  total_bytes > 0, bad_frames > 0
If frames arrive and parse fine:  total_bytes > 0, good_frames > 0
"""
from __future__ import annotations

import argparse
import socket
import struct
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ip", required=True)
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--bind", default="0.0.0.0",
                   help="Local bind IP (use your NIC IP if 0.0.0.0 fails)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, 0))
    sock.settimeout(0.25)

    local = sock.getsockname()
    print(f"Local socket: {local[0]}:{local[1]}  ->  radio {args.ip}:1024")

    # start IQ
    start_pkt = bytearray(64)
    start_pkt[0] = 0xEF
    start_pkt[1] = 0xFE
    start_pkt[2] = 0x04
    start_pkt[3] = 0x01
    n = sock.sendto(bytes(start_pkt), (args.ip, 1024))
    print(f"Sent start ({n} bytes): {bytes(start_pkt[:4]).hex()} ...")

    total_bytes = 0
    packets = 0
    good_frames = 0
    bad_frames = 0
    unique_sizes: dict[int, int] = {}
    first_bytes_sample = None

    t0 = time.monotonic()
    while time.monotonic() - t0 < args.seconds:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        packets += 1
        total_bytes += len(data)
        unique_sizes[len(data)] = unique_sizes.get(len(data), 0) + 1
        if first_bytes_sample is None:
            first_bytes_sample = (addr, data[:16].hex())
        if len(data) == 1032 and data[0] == 0xEF and data[1] == 0xFE and data[2] == 0x01:
            good_frames += 1
        else:
            bad_frames += 1
            if args.verbose and bad_frames < 5:
                print(f"  non-IQ from {addr}: len={len(data)} head={data[:8].hex()}")

    # stop
    stop_pkt = bytearray(64)
    stop_pkt[0] = 0xEF
    stop_pkt[1] = 0xFE
    stop_pkt[2] = 0x04
    stop_pkt[3] = 0x00
    sock.sendto(bytes(stop_pkt), (args.ip, 1024))
    sock.close()

    print(f"\npackets={packets}  total_bytes={total_bytes}")
    print(f"good IQ frames (1032B, EF FE 01 ...)  : {good_frames}")
    print(f"other packets (parser would drop)      : {bad_frames}")
    if unique_sizes:
        print(f"sizes seen: {unique_sizes}")
    if first_bytes_sample:
        print(f"first packet from {first_bytes_sample[0]}: {first_bytes_sample[1]}...")

    if packets == 0:
        print("\n-> ZERO packets. Windows Firewall is almost certainly blocking inbound UDP.")
        print("   Quick fix (run in ADMIN PowerShell):")
        print("     New-NetFirewallRule -DisplayName 'Lyra Python inbound' "
              "-Direction Inbound -Program (Get-Command python).Source -Action Allow")


if __name__ == "__main__":
    main()
