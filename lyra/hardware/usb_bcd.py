"""USB-BCD band-data output for external linear amplifiers.

The HL2 has no native BCD band-data output. To drive a linear amp's
auto-bandswitch input (Yaesu BCD standard, also used by SPE, ACOM,
Elecraft KPA1500, Burst, etc.) reference clients use an external FTDI cable in
synchronous bit-bang mode that exposes 8 output pins. A single byte
write sets the parallel pin state; the cable's wiring carries that to
the amp's BAND DATA input.

Mirrors the reference HPSDR client's `UsbBCDCable.cs`:
    FTDI driver       D2XX (Windows) via the `ftd2xx` Python wrapper
    Bit mode          0x40 SYNC_BITBANG, 8-bit mask
    Baud rate         921 600
    Per-write payload one byte = BCD band number

Yaesu BCD band numbers (matches the reference HPSDR client `SetBCDbyBand`):
    GEN / 60m / 2m / WWV / VHF /BC : 0   (no signal — amp bypasses)
    160 m : 1     80 m : 2     40 m : 3
    30  m : 4     20 m : 5     17 m : 6
    15  m : 7     12 m : 8     10 m : 9
    6   m : 10

⚠ SAFETY
The wrong BCD code at high power can route TX through the wrong filter
in the amplifier and destroy LDMOS devices and/or filter circuits.
Operators MUST verify wiring + low-power test on every band before
keying at full output. This module's `enabled` flag should be off until
the operator has confirmed the chain end-to-end.

This module lazily imports `ftd2xx`. If the package or driver isn't
installed, `Ftd2xxNotInstalled` is raised on first device-touching call;
the rest of Lyra keeps running normally.
"""
from __future__ import annotations

from typing import List, Optional


# ── Yaesu BCD band-code map ────────────────────────────────────────────
# Keys match `Band.name` from lyra/bands.py.
BAND_TO_BCD: dict[str, int] = {
    "160m": 1, "80m": 2, "60m": 0, "40m": 3, "30m": 4, "20m": 5,
    "17m":  6, "15m": 7, "12m": 8, "10m": 9, "6m":  10,
    # Broadcast & GEN bands → no amp band → 0 (amp bypasses)
}


def bcd_for_band(band_name: str, sixty_as_forty: bool = False) -> int:
    """Return the Yaesu BCD code for a given band name.

    `sixty_as_forty`: the original Yaesu BCD standard predates 60 m
    allocations, so there's no assigned code. Most linear amps use the
    40 m filter for 60 m operation. When this flag is True, 60 m maps
    to BCD 3 (the 40 m code) so the amp switches to its 40 m filter.
    When False (default), 60 m maps to 0 (amp bypasses) — matching
    the reference HPSDR client's unmapped behavior for band 60 m.
    """
    if sixty_as_forty and band_name == "60m":
        return 3
    return BAND_TO_BCD.get(band_name, 0)


# ── Lazy FTDI import + error type ──────────────────────────────────────
class Ftd2xxNotInstalled(RuntimeError):
    """Raised if the operator enables USB-BCD but `ftd2xx` is missing.

    Install with `pip install ftd2xx` and ensure the FTDI D2XX driver is
    on the system. On Windows the driver is bundled with most FTDI
    devices' default installer.
    """


def _import_ftd2xx():
    try:
        import ftd2xx as _f
        return _f
    except ImportError as e:
        raise Ftd2xxNotInstalled(
            "ftd2xx not installed — run: pip install ftd2xx") from e


# ── Device discovery ───────────────────────────────────────────────────
def list_devices() -> List[dict]:
    """Return a list of FTDI devices currently attached. Each entry is
    `{serial, description, location_id, type}`. Empty list if no devices
    or if ftd2xx isn't installed (silently — caller can show 'install' UI)."""
    try:
        ft = _import_ftd2xx()
    except Ftd2xxNotInstalled:
        return []
    out = []
    try:
        n = ft.createDeviceInfoList()
        for i in range(n):
            info = ft.getDeviceInfoDetail(i)
            out.append({
                "serial":      info.get("serial", b"").decode(errors="replace"),
                "description": info.get("description", b"").decode(errors="replace"),
                "location_id": info.get("location", 0),
                "type":        info.get("type", 0),
            })
    except Exception as e:
        print(f"[usb-bcd] device enumeration error: {e}")
    return out


# ── Cable controller ───────────────────────────────────────────────────
class UsbBcdCable:
    """Single FTDI cable in 8-pin sync-bitbang mode. Writes one byte
    that maps directly to the cable's 8 output pins."""

    BAUD = 921_600
    BIT_MASK = 0xFF
    BIT_MODE_SYNC_BITBANG = 0x04   # FT_BITMODE_SYNC_BITBANG

    def __init__(self, serial: str):
        self.serial = serial
        self._dev = None
        self._last_value = 0xFF        # force a real write on first set
        ft = _import_ftd2xx()
        # Open by serial. ftd2xx serial param is bytes.
        self._dev = ft.openEx(serial.encode("ascii"))
        self._dev.setBitMode(self.BIT_MASK, self.BIT_MODE_SYNC_BITBANG)
        self._dev.setBaudRate(self.BAUD)
        # Start with all outputs off.
        self.write_byte(0x00)

    def write_byte(self, value: int):
        value &= 0xFF
        if value == self._last_value:
            return
        self._last_value = value
        if self._dev is None:
            return
        self._dev.write(bytes([value]))

    def close(self):
        if self._dev is not None:
            try:
                self.write_byte(0x00)         # leave amp's BCD lines low
                self._dev.close()
            except Exception:
                pass
            self._dev = None
