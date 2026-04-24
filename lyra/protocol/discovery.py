"""HPSDR Protocol 1 discovery for Hermes Lite 2 / 2+.

Reference: the reference HPSDR client clsRadioDiscovery.cs (MW0LGE / MI0BOT).
Board ID 6 = HermesLite family. HL2+ reports the same board ID; we
distinguish it later via gateware version / EEPROM config.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional

DISCOVERY_PORT = 1024
DISCOVERY_PACKET_LEN = 63
BOARD_HERMES_LITE = 6

# Board IDs per the reference HPSDR client mapP1DeviceType
BOARD_NAMES = {
    0: "Atlas",
    1: "Hermes",
    2: "HermesII",
    4: "Angelia",
    5: "Orion",
    6: "HermesLite",
    10: "OrionMKII",
}


@dataclass
class RadioInfo:
    ip: str
    mac: str
    board_id: int
    board_name: str
    code_version: int
    is_busy: bool
    # HL2-specific extras (from MI0BOT fork)
    ee_config: int = 0
    ee_config_reserved: int = 0
    fixed_ip_hl2: str = ""
    num_rxs: int = 0
    beta_version: int = 0
    metis_version: int = 0


def _build_discovery_packet_p1() -> bytes:
    pkt = bytearray(DISCOVERY_PACKET_LEN)
    pkt[0] = 0xEF
    pkt[1] = 0xFE
    pkt[2] = 0x02
    return bytes(pkt)


def _parse_reply(data: bytes, sender_ip: str) -> Optional[RadioInfo]:
    if len(data) < 24:
        return None
    if data[0] != 0xEF or data[1] != 0xFE:
        return None
    status = data[2]
    if status not in (0x02, 0x03):
        return None

    mac = ":".join(f"{b:02X}" for b in data[3:9])
    code_version = data[9]
    board_id = data[10]
    is_busy = status == 0x03

    info = RadioInfo(
        ip=sender_ip,
        mac=mac,
        board_id=board_id,
        board_name=BOARD_NAMES.get(board_id, f"Unknown({board_id})"),
        code_version=code_version,
        is_busy=is_busy,
    )

    # HL2 extras from MI0BOT fork (bytes 11..16)
    if board_id == BOARD_HERMES_LITE:
        info.ee_config = data[11]
        info.ee_config_reserved = data[12]
        fixed_ip = bytes(reversed(data[13:17]))
        info.fixed_ip_hl2 = ".".join(str(b) for b in fixed_ip)

    if len(data) > 20:
        info.metis_version = data[19]
        info.num_rxs = data[20]

    # HL2-specific layout (MI0BOT): num_rxs lives at [19], beta at [21].
    # Override the generic Metis assignment above.
    if board_id == BOARD_HERMES_LITE:
        if len(data) > 19:
            info.num_rxs = data[19]
        if len(data) > 21:
            info.beta_version = data[21]

    return info


def discover(
    timeout_s: float = 1.5,
    attempts: int = 2,
    local_bind: str = "0.0.0.0",
    target_ip: Optional[str] = None,
) -> List[RadioInfo]:
    """Broadcast a Protocol 1 discovery and collect replies.

    Args:
        timeout_s: total wall time to wait for replies per attempt.
        attempts: how many times to resend the discovery packet.
        local_bind: local IP to bind the socket to (0.0.0.0 = all NICs).
        target_ip: if set, unicast to this IP instead of broadcasting.
    """
    found: dict[str, RadioInfo] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((local_bind, 0))
    sock.settimeout(0.1)

    packet = _build_discovery_packet_p1()
    destination = target_ip if target_ip else "255.255.255.255"

    try:
        for _ in range(attempts):
            sock.sendto(packet, (destination, DISCOVERY_PORT))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                info = _parse_reply(data, addr[0])
                if info and info.mac not in found:
                    found[info.mac] = info
    finally:
        sock.close()

    return list(found.values())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HL2 Protocol 1 discovery")
    parser.add_argument("--target", help="Unicast target IP (skip broadcast)")
    parser.add_argument("--bind", default="0.0.0.0", help="Local bind IP")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--attempts", type=int, default=2)
    args = parser.parse_args()

    radios = discover(
        timeout_s=args.timeout,
        attempts=args.attempts,
        local_bind=args.bind,
        target_ip=args.target,
    )
    if not radios:
        print("No radios found.")
    for r in radios:
        print(f"{r.ip:15s}  {r.mac}  {r.board_name}  "
              f"gateware=v{r.code_version}.{r.beta_version}  "
              f"busy={r.is_busy}  rxs={r.num_rxs}")
        if r.board_id == BOARD_HERMES_LITE:
            print(f"    HL2 fixed-IP setting: {r.fixed_ip_hl2}  "
                  f"ee_config=0x{r.ee_config:02X}")
