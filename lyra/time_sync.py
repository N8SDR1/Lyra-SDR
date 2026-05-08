"""NTP drift check + Windows time-sync helpers.

NCDXF beacons rotate on 10-second slots — if the operator's PC clock
is off by more than a couple of seconds the spectrum-marker tooltips
and Follow-mode VFO retunes drift to the wrong station.  This module
gives the operator a one-shot way to verify their clock against a
public NTP server and (on Windows) to nudge w32time into a resync.

Design notes:

* Pure stdlib — uses ``socket`` to send a 48-byte NTP v4 client
  packet at UDP port 123.  No ntplib / no extra wheel dependency,
  same posture as ``lyra/propagation.py`` (urllib for HamQSL).
* Tries a couple of well-known pool servers in sequence so a single
  flaky DNS lookup or transient block doesn't kill the check.
* Threaded on the caller's side — the UI launches this in a
  ``QThread`` or short-lived ``threading.Thread`` so we never
  block the Qt main loop.  The functions here are blocking by
  design (network I/O); the UI layer wraps them.
* Windows resync uses ``w32tm /resync`` — works without elevation
  on a stock Windows install if Windows Time service is running,
  fails fast otherwise.  We surface stdout/stderr so the operator
  can see why if it didn't take.

Algorithm (NTP "offset"):

    T1 = local time when we send (our originate timestamp)
    T2 = server time when it received (in reply)
    T3 = server time when it transmitted (in reply)
    T4 = local time when reply arrives
    offset = ((T2 - T1) + (T3 - T4)) / 2

    +offset  →  local clock is BEHIND server (system time too low)
    -offset  →  local clock is AHEAD of server (system time too high)

A drift of ±1 sec is fine for NCDXF (slot is 10 sec, station ID
re-evaluates each tick).  ±3 sec means the operator may
mis-identify a beacon at slot boundaries.  ±5+ sec is broken.
"""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional


# Default NTP servers — public, anycast, no key required.  Tried in
# order; first one that answers wins.  Pool addresses do round-robin
# resolution so even one entry covers many physical servers.
DEFAULT_NTP_SERVERS = (
    "time.cloudflare.com",
    "pool.ntp.org",
    "time.google.com",
    "time.windows.com",
)

# Number of seconds between the NTP epoch (1900-01-01) and the Unix
# epoch (1970-01-01).  The NTP timestamp's seconds field is counted
# from 1900; Python's time.time() returns seconds from 1970.
NTP_EPOCH_OFFSET = 2208988800

# How long to wait per server before giving up and trying the next.
_SOCKET_TIMEOUT_SEC = 2.5


@dataclass(frozen=True)
class NtpResult:
    """Result of a single NTP query."""
    server: str            # which server answered
    offset_sec: float      # local-clock minus server-clock; +offset = local is BEHIND server
    round_trip_sec: float  # round-trip delay
    server_unix: float     # server time at moment of response, as Unix seconds
    local_unix: float      # local time at moment of response, as Unix seconds

    @property
    def drift_seconds(self) -> float:
        """Absolute drift magnitude — convenience accessor."""
        return abs(self.offset_sec)

    @property
    def severity(self) -> str:
        """``ok`` / ``warn`` / ``bad`` for color coding.

        Thresholds picked for NCDXF: 10-sec slots, so anything below
        1 sec is invisible, 1-3 sec is "yellow flag", 3+ sec is
        "you'll mis-identify beacons."
        """
        d = self.drift_seconds
        if d < 1.0:
            return "ok"
        if d < 3.0:
            return "warn"
        return "bad"


def _build_client_packet() -> bytes:
    """Return a 48-byte NTP v4 client request packet.

    LI=0 (no warning), VN=4 (NTPv4), Mode=3 (client) → first byte 0x23.
    All other fields are zero — the server fills them in on response.
    """
    return b"\x23" + b"\x00" * 47


def _ntp_ts_to_unix(seconds_be: int, fraction_be: int) -> float:
    """Convert a 64-bit NTP timestamp (seconds + fraction) to Unix seconds."""
    return (seconds_be - NTP_EPOCH_OFFSET) + (fraction_be / 2**32)


def query_ntp(server: str, timeout_sec: float = _SOCKET_TIMEOUT_SEC) -> Optional[NtpResult]:
    """Send a single NTP query to ``server`` and return the result.

    Returns ``None`` if the query fails (DNS, timeout, malformed
    response).  Caller decides whether to retry against another
    server.
    """
    try:
        addr = (server, 123)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout_sec)
            t1 = time.time()
            s.sendto(_build_client_packet(), addr)
            data, _ = s.recvfrom(1024)
            t4 = time.time()

        if len(data) < 48:
            return None

        # Parse the 48-byte response.  We need T2 (Receive Timestamp,
        # bytes 32-39) and T3 (Transmit Timestamp, bytes 40-47).
        # Big-endian 32-bit seconds + 32-bit fraction.
        recv_sec, recv_frac, tx_sec, tx_frac = struct.unpack("!IIII", data[32:48])
        t2 = _ntp_ts_to_unix(recv_sec, recv_frac)
        t3 = _ntp_ts_to_unix(tx_sec, tx_frac)

        # Some servers (or transient errors) return zeroed timestamps.
        # Reject obviously-garbage replies so the caller can move on.
        if t2 < 1_500_000_000 or t3 < 1_500_000_000:
            return None

        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        round_trip = (t4 - t1) - (t3 - t2)

        return NtpResult(
            server=server,
            offset_sec=offset,
            round_trip_sec=round_trip,
            server_unix=t3,
            local_unix=t4,
        )
    except (OSError, socket.timeout, struct.error):
        return None


def check_drift(servers: tuple[str, ...] = DEFAULT_NTP_SERVERS) -> Optional[NtpResult]:
    """Try each server in turn; return the first successful result.

    Returns ``None`` if every server fails (offline, firewalled, etc.).
    """
    for srv in servers:
        result = query_ntp(srv)
        if result is not None:
            return result
    return None


@dataclass(frozen=True)
class ResyncResult:
    """Result of attempting a Windows w32tm /resync."""
    ok: bool
    output: str       # combined stdout + stderr (truncated)
    returncode: int


def attempt_windows_resync(timeout_sec: float = 8.0) -> ResyncResult:
    """Shell out to ``w32tm /resync`` (Windows only).

    Works without elevation on a default Windows install IF the
    Windows Time service is running.  If it isn't (or the host is
    domain-joined and the time peer is unreachable), the command
    returns a clear error which we surface verbatim to the operator.

    On non-Windows hosts this is a no-op and returns ``ok=False``
    with a polite message.
    """
    if sys.platform != "win32":
        return ResyncResult(
            ok=False,
            output="Automatic resync is only supported on Windows.\n"
                   "On macOS/Linux, configure NTP via your OS settings.",
            returncode=-1,
        )

    try:
        # CREATE_NO_WINDOW = 0x08000000 — keeps a console window from
        # flashing.  Available on Win Python 3.7+.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            ["w32tm", "/resync"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            creationflags=creationflags,
        )
        out = (completed.stdout or "") + (completed.stderr or "")
        out = out.strip()[:1500]  # cap so tooltip / dialog stays readable
        return ResyncResult(
            ok=(completed.returncode == 0),
            output=out or "(no output from w32tm)",
            returncode=completed.returncode,
        )
    except FileNotFoundError:
        return ResyncResult(
            ok=False,
            output="w32tm.exe not found in PATH.\n"
                   "Windows Time service may not be installed.",
            returncode=-1,
        )
    except subprocess.TimeoutExpired:
        return ResyncResult(
            ok=False,
            output=f"w32tm /resync timed out after {timeout_sec:.0f} seconds.\n"
                   "The Windows Time service may be stopped, or the\n"
                   "configured time peer is unreachable.",
            returncode=-1,
        )
    except OSError as exc:
        return ResyncResult(
            ok=False,
            output=f"Could not run w32tm: {exc}",
            returncode=-1,
        )


def format_drift(result: NtpResult) -> str:
    """Operator-friendly one-line summary of an NtpResult.

    Examples:
        "+0.12 sec (clock OK)"
        "-2.40 sec (clock 2.4 sec ahead of NTP)"
        "+5.83 sec (clock 5.8 sec behind NTP)"
    """
    sign = "+" if result.offset_sec >= 0 else "-"
    drift = abs(result.offset_sec)

    if drift < 1.0:
        return f"{sign}{drift:.2f} sec (clock OK)"

    direction = "behind" if result.offset_sec > 0 else "ahead of"
    return f"{sign}{drift:.2f} sec (clock {drift:.1f} sec {direction} NTP)"
