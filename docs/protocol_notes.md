# HPSDR Protocol 1 — HL2 Notes

Distilled from the reference HPSDR client `clsRadioDiscovery.cs`, `NetworkIO.cs`, and the
Hermes Lite 2 wiki. Byte offsets are 0-based.

## Discovery

### Request (63 bytes, UDP to 255.255.255.255:1024)
| byte | value |
|------|-------|
| 0    | 0xEF  |
| 1    | 0xFE  |
| 2    | 0x02  |
| 3..62| 0x00  |

### Reply (parsed)
| bytes | meaning                                   |
|-------|-------------------------------------------|
| 0..1  | 0xEF 0xFE                                 |
| 2     | 0x02 = idle, 0x03 = busy                  |
| 3..8  | MAC address (6 bytes)                     |
| 9     | gateware code version                     |
| 10    | board ID (6 = HermesLite / HL2 / HL2+)    |
| 11    | HL2 EEPROM config                         |
| 12    | HL2 EEPROM config reserved                |
| 13..16| HL2 fixed-IP setting (big-endian)         |
| 19    | Metis version                             |
| 20    | number of RX                              |
| 21    | HL2 beta version                          |

HL2 and HL2+ both report board ID 6. Differentiation is via gateware
version and EEPROM content — TBD once we have an HL2+ on the bench.

## Streaming (to be documented)
- Start/stop commands
- I/Q frame format (EF FE 01, sequence number, USB-like 512-byte frames)
- C&C (Command & Control) fields for freq, PTT, gain, filters
- TX envelope framing

## Open questions for HL2+
- Does HL2+ expose any new C&C registers vs HL2? Check MI0BOT the reference HPSDR client
  diffs in `IoBoardHl2.cs` and `NetworkIO.cs`.
- Sample rate caps differ? HL2 supports 48k/96k/192k/384k.
