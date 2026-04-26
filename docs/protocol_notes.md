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

---

# HPSDR Protocol 2 — Apache ANAN family + Brick II

Reference spec: `openHPSDR Ethernet Protocol v4.4` (TAPR/OpenHPSDR-Firmware,
Mar 2019). The Lyra implementation lives under `lyra/protocol/p2/` and is
isolated from the P1/HL2 path documented above.

## Discovery

Both P1 and P2 listen on UDP 1024 for discovery. P2's host-to-radio packet
is 60 bytes:

| byte    | value                                   |
|---------|-----------------------------------------|
| 0..3    | seq # (0x00000000)                      |
| 4       | 0x02 (command = discovery)              |
| 5..59   | zero                                    |

The reply is also 60 bytes; the first two bytes are seq=0 (not `EF FE`),
which is how the parser disambiguates from a P1 reply. Board ID is at
byte 11 (10 = SATURN/ANAN-G2). Full reply layout in
`lyra/protocol/p2/discovery.py`.

## Streaming endpoints (post-General-Packet defaults)

| port  | direction      | purpose                              |
|-------|----------------|--------------------------------------|
| 1024  | bidirectional  | Discovery + General Packet           |
| 1025  | host → radio   | DDC Specific (per-DDC config)        |
| 1025  | radio → host   | High Priority Status                 |
| 1026  | host → radio   | DUC Specific (TX, **not in v1**)     |
| 1026  | radio → host   | Mic / line samples (**not in v1**)   |
| 1027  | host → radio   | High Priority From PC (run/freq/PTT) |
| 1028  | host → radio   | DDC Audio (radio's own DAC out)      |
| 1029+ | host → radio   | DUC0+ I&Q (TX baseband, **not v1**)  |
| 1035+ | radio → host   | DDC0..DDC79 I&Q (RX baseband)        |

The host's actual receive port for DDC0 IQ is whatever ephemeral port
P2Stream binds — the radio is told via the General Packet's
"DDC0 IQ port" field (bytes 17-18).

## DDC IQ frame (1444 bytes for 1 DDC × 24-bit × 238 samples)

| byte    | content                          |
|---------|----------------------------------|
| 0..3    | seq # (BE uint32, per-port)      |
| 4..11   | sample-clock timestamp (BE u64)  |
| 12..13  | bits per sample (BE u16, 24)     |
| 14..15  | I&Q samples per frame (BE u16, 238) |
| 16..1443| 238 × (I3 + Q3) BE 24-bit signed  |

## Differences from P1 worth remembering

- **Multi-socket vs multiplexed.** P1 multiplexes RX/TX/control over one
  UDP flow with EP markers (0x06, 0x02). P2 splits each role onto its
  own UDP port and each port has its own sequence counter.
- **No keepalive requirement.** P1 demands an EP2 keepalive on every EP6
  frame or the radio halts within seconds. P2 has no such rule; we re-send
  the High Priority packet once per second per spec recommendation.
- **Wider sample-rate range.** P1 (HL2): 48/96/192/384 kHz. P2 (G2):
  48/96/192/384/768/1536 kHz.
- **Audio out.** P1 jams stereo audio into EP2's per-frame slot. P2 uses
  a dedicated UDP port (1028) — out of scope for v1.
- **Per-DDC freq, not via C&C.** P1 sets RX1 freq via C0/C1..C4 register
  writes piggybacked on EP2. P2 has a dedicated 32-bit-per-DDC slot in
  the High Priority packet.

## Brick II

Apache Brick II is not enumerated in v4.4 (board IDs 7-9 are reserved).
Until we see one on the wire, the boards table treats unknown IDs as
"Unknown(id=N)" without crashing. Once observed, add the row to
`lyra/protocol/p2/boards.py`.
