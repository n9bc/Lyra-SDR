# Apache ANAN-G2 (ORION2) on Protocol 2 — wire-level findings

Written 2026-04-26 against a real ANAN-G2 reporting `proto=v4.3 fw=26
[beta]`, MAC `D8:3A:DD:07:E9:8C`, IP `192.168.10.206`. The Lyra v1
P2 implementation in `lyra/protocol/p2/` does discovery successfully
but gets **zero IQ frames** when streaming. This document traces the
gap end-to-end so the next session can fix it without re-deriving
context.

Sources used:
- Wire capture from a working Thetis session (`thetis_live.pcapng`,
  21,848 packets, 8 s window).
- `pihpsdr/src/new_protocol.c` (DL1YCF) — the canonical P2 client.
- `deskhpsdr/src/new_protocol.c` (DL1BZ fork) — same byte layouts.
- v4.4 spec PDF — already referenced by the existing
  `docs/superpowers/specs/2026-04-26-protocol-2-apache-design.md`.

When the spec and the wire capture disagree, **the wire capture wins**
— that is what real Apache firmware actually expects. The "v4.3"
discovery byte the radio reports is a firmware revision, not the
protocol-spec version; the radio still speaks the protocol pi-hpsdr
implements.

---

## TL;DR — what was broken (all FIXED 2026-04-26)

Seven issues, ranked by impact at discovery:

1. **DDC frequencies are phase increments, not Hz.** `phase = freq_Hz
   × 2³² / 122_880_000`. **Fixed** — `freq_hz_to_phase()` in
   `packets.py`, applied in `build_high_priority_packet()` when
   `phase_mode=True` (default).
2. **ANAN-G2 uses DDC2/DDC3 for RX1/RX2**, not DDC0/DDC1. **Fixed**
   — `BoardSpec.ddc_offset_for_rx1` in `boards.py`; `P2Stream` reads
   the offset and routes the enable mask, DDC config block, freq
   slot, and IQ port subscription accordingly.
3. **General Packet byte 37 = 0x08 (phase-mode)** and bytes 38, 58,
   59 are mandatory. **Fixed** — `GeneralPacketConfig` defaults
   `phase_mode=True`, `enable_hardware_timer=True`, `pa_enable=True`,
   `alex_enable=0x03`.
4. **DDC Specific layout** (kHz vs Hz, slot offset). **Fixed** — our
   encoder was already correct on layout; only the slot offset needed
   to come from `BoardSpec`. ADC dither_mask=0x07 added for Apache.
5. **Apache uses the source port of the host's commands as the IQ
   destination**, ignoring the General Packet's declared body port
   (`ddc_iq_destination_port`). Verified against captured Thetis:
   declared body = 1035, IQ arrived on Thetis's source port 51538.
   **Fixed** — `P2Stream` now uses a single shared socket for sending
   commands and receiving IQ.
6. **Windows NIC coalescing concatenates IQ frames.** A single
   `recvfrom()` returned up to 5776 bytes (= 4 × 1444) on the live
   radio. **Fixed** — new `parse_ddc_iq_frames()` walks the buffer in
   1444-byte chunks; `_rx_loop` uses it and reads up to 8 frames per
   recv with a 256 KB socket buffer.
7. **DUC Specific not sent at startup** — pi-hpsdr always emits one,
   even RX-only. **Fixed** — `build_duc_specific_packet()` added and
   `P2Stream.start()` sends it after DDC Specific.

Plus a **bonus discovered while bringing this up**:

8. **`ConnectionResetError` (Windows ICMP "port unreachable") killed
   the rx_loop** instantly when any control packet bounced (e.g.
   sending DUC Specific to a port the radio's mic listener hadn't
   bound yet during startup). **Fixed** — `_rx_loop` now treats it
   like `socket.timeout` and continues. Same fix applied earlier in
   `discovery.py`.

**Verified**: 6514 frames in 8 s against 192.168.10.206 at 192 kHz,
zero `seq_errors`. End-to-end RX path now works on real Apache
hardware.

---

## The Thetis handshake — packet by packet

Captured order and cadence on the wire (port refers to dst port for
host→radio):

```
t=0      Discovery (port 1024, 60 B)         — already done by Lyra
t=~0     General Packet (port 1024, 60 B)    — wrong byte layout (issues #3,4)
t=~0     DDC Specific (port 1025, 1444 B)    — wrong DDC slot, wrong rate units (issues #2,5)
t=~0     DUC Specific (port 1026, 60 B)      — never sent by Lyra (issue #7)
t=~0     High Priority (port 1027, 1444 B)   — wrong freq encoding, wrong DDC slot (issues #1,2)
…then continuously while running:
every 500 ms:  General Packet refresh (port 1024)
every 500 ms:  High Priority refresh (port 1027)   ← Lyra already does this at 1 Hz
every ≈122 µs: DUC IQ TX baseband (port 1029, 1444 B) ← TX path, ignore for RX-only
every ≈680 µs: Mic Echo / DDC Audio (port 1028, 260 B) ← ditto
```

Radio → host while running:

```
src 1025 (60 B):   High Priority Status (≈4 Hz). Telemetry: PTT, alex
                   feedback, ADC overflow flags. Optional to consume
                   for v1 RX, useful later.
src 1026 (variable): Mic samples. Apache is sending the radio's mic
                   input back to the host. Ignore for RX-only.
src 1037 (variable): DDC2 IQ stream (RX1). This is what we want.
                   Frames are 1444 B native but Windows may deliver
                   them coalesced (1444, 2888, 4332, or 5784 B per
                   recvfrom).
```

---

## Packet 1 — General Packet (60 bytes, port 1024)

Refreshed by Thetis every 500 ms. Most bytes are zero; the
load-bearing ones:

| Offset | Field | Required value (ORION2) | Source |
|--------|-------|--------------------------|--------|
| 0–3    | sequence (BE) | host-incremented | spec |
| 4      | command byte  | `0x00` (already correct) | spec |
| 5–6    | DDC command port (BE) | `0x0401` = 1025 | already correct |
| 7–8    | DUC command port (BE) | `0x0402` = 1026 | already correct |
| 9–10   | High-Priority host→radio port (BE) | `0x0403` = 1027 | already correct |
| 11–12  | High-Priority radio→host port (BE) | `0x0401` = 1025 | already correct |
| 13–14  | DDC audio port (BE) | `0x0404` = 1028 | already correct |
| 15–16  | DUC0 IQ port (BE) | `0x0405` = 1029 | already correct |
| 17–18  | DDC IQ base port (BE) | `0x040b` = 1035 | already correct |
| 19–20  | mic samples port (BE) | `0x0402` = 1026 | already correct |
| 21–22  | wideband base port (BE) | `0x0403` = 1027 | already correct |
| 23     | wideband enable mask | `0x00` (no WB) | already correct |
| 24–25  | wideband samples-per-packet (BE) | `0x0200` = 512 | already correct |
| 26     | wideband bits-per-sample | `0x10` = 16 | already correct |
| 27     | wideband update rate ms | `0x46` = 70 (we use 0x14 = 20; differs but harmless when WB off) | minor |
| 28     | wideband packets-per-frame | `0x20` = 32 | already correct |
| **37** | **phase-mode flag** | **`0x08`** | pi-hpsdr `new_protocol_general`; required |
| **38** | **enable hardware timer** | **`0x01`** | required |
| **58** | **PA enable** | **`0x01`** | ORION2 needs PA=1 even in RX |
| **59** | **Alex enable** | **`0x03`** | ORION2 needs both Alex0 + Alex1 |

The captured Thetis byte 37 is `0x08`. Our builder writes `0x00`.
That single byte makes the radio interpret all DDC frequencies as raw
Hz and silently mis-tune.

Bytes 33 also showed `0x03` and byte 34 `0x20` in the capture; both
are zero in pi-hpsdr's general packet. They are likely set by
Thetis for a feature pi-hpsdr doesn't use (envelope PWM dot-clock,
maybe). Not load-bearing — pi-hpsdr works without them.

---

## Packet 2 — DDC Specific (1444 bytes, port 1025)

Refreshed on configuration change. Layout (pi-hpsdr
`new_protocol_receive_specific`):

| Offset | Field | Notes |
|--------|-------|-------|
| 0–3    | sequence (BE) | per-port counter |
| 4      | n_adcs | **2 for ANAN-G2** (we send 1) |
| 5      | dither mask | bit per ADC: `0x07` = ADC0+1+2 dither, what Thetis sends |
| 6      | random-whitening mask | usually 0 |
| 7–16   | DDC enable mask, 80 bits little-end-byte | bit 2 set → byte 7 = `0x04` for RX1 on ORION2 |
| 17 + ddc·6 + 0 | ADC source for this DDC | 0 = ADC0, 1 = ADC1 |
| 17 + ddc·6 + 1 | sample rate **kHz** MSB (BE) | for 192 kHz: `0x00` |
| 17 + ddc·6 + 2 | sample rate **kHz** LSB | for 192 kHz: `0xC0` |
| 17 + ddc·6 + 3 | reserved (CIC1) | 0 |
| 17 + ddc·6 + 4 | reserved (CIC2) | 0 |
| 17 + ddc·6 + 5 | bits per sample | always `24` = `0x18` |
| 1363–1442 | sync matrix | zero unless ganging DDCs |

So for ANAN-G2 RX1 at 192 kHz on ADC0 you write a config block at
**byte offset 29** (= 17 + 2·6), not byte 17.

**Our current bug**: we set the rate via `struct.pack_into(">H", pkt,
block_off + 1, rate_khz)` which puts the BE 16-bit value at offsets
+1 and +2. That's actually correct! The kHz units are right too. The
wire layout matches pi-hpsdr. The remaining bug is just that we use
the wrong `block_off` (DDC0 → 17, should be DDC2 → 29) and the wrong
enable-mask bit (bit 0 → bit 2).

Thetis additionally pre-configures DDC1, DDC3, DDC4, DDC5, DDC6 with
sane defaults even though only DDC2 is enabled. The radio only
streams the enabled DDC; the extra config blocks are forward-looking
for diversity / dual-RX. Not required, but cheap.

---

## Packet 3 — DUC Specific (60 bytes, port 1026)

We don't send this at all. pi-hpsdr sends it unconditionally at
startup. Verbatim values from
`new_protocol_transmit_specific()` for an idle radio:

| Offset | Field | Value (RX-only safe defaults) |
|--------|-------|------------------------------|
| 0–3    | sequence (BE) | per-port counter |
| 4      | n_DACs | `0x01` |
| 5      | CW mode flags | `0x00` |
| 6      | sidetone volume | `0x00` (no sidetone) |
| 7–8    | sidetone freq (BE) | `0x02BC` = 700 Hz (default) |
| 9      | keyer speed | `0x12` = 18 wpm |
| 10     | keyer weight | `0x32` = 50% |
| 11–12  | keyer hang time (BE) | `0x013C` = 316 ms |
| 13     | RF delay | `0x09` |
| 17     | keyer ramp width | `0xC0` |
| 58     | ADC1 attenuation | `0x00` |
| 59     | ADC0 attenuation | `0x00` |

The Thetis capture shows the same first 16 bytes. Sending this once
at session start should be enough — pi-hpsdr resends it only on
keyer-config change.

---

## Packet 4 — High Priority (1444 bytes, port 1027)

Sent on every state change AND refreshed on a timer (Thetis: 500 ms,
pi-hpsdr: configurable, our v1: 1 s). Critical fields:

| Offset | Field | Notes |
|--------|-------|-------|
| 0–3    | sequence (BE) | per-port counter |
| **4**  | **run / PTT bits** | bit 0 = run (must be 1 to stream), bits 1–4 = PTT0..3 |
| 5      | CW key flags | 0 for RX-only |
| 6–8    | reserved (CWX, etc.) | 0 |
| **9 + ddc·4 .. 12 + ddc·4** | **DDC[ddc] phase increment (BE)** | `phase = freq_Hz × 34.952533…` |
| 329–332 | DUC phase increment (BE) | optional for RX-only; can be 0 |
| 345    | TX drive level | 0 for RX |
| 1400   | XVTR relay + audio mute bits | 0 for RX |
| 1401   | open-collector outputs | encodes auto-band switch — see below |
| 1428–1431 | Alex1 control word (BE) | encodes BPF/LPF/ANT for second Alex |
| 1432–1435 | Alex0 control word (BE) | same for first Alex |
| 1442   | ADC1 step attenuator | 0 for RX (or 31 in TX with PA) |
| 1443   | ADC0 step attenuator | same |

**For ANAN-G2 RX1 at 14.250 MHz**: phase = `round(14_250_000 ×
4_294_967_296 / 122_880_000)` = `498_073_600` = `0x1DB00000`. This
goes at bytes **17–20** (DDC2), not 9–12.

The captured Thetis values confirm: `0x150E06F6` at bytes 17–20 →
`/ 34.9525` → 10.106 MHz (radio was tuned to 30 m), and `0x1D630A83`
at bytes 21–24 → 14.097 MHz (RX2 on 20 m).

**Alex0/Alex1 control words**: Thetis writes `0x01100002` at
1428–1431 and `0x01100010` at 1432–1435. Bit assignments encode
preselector relays per Apache hardware doc. For a generic
"don't-mute-the-input" RX configuration, mirror what Thetis sends
exactly. Future work: derive these from current RX frequency.

`open_collector_outputs` byte 1401 = `0x90` in the capture. This is
the OC band-switch byte that drives the rear-panel auto-tuner /
amplifier. Sane default: 0. It only matters if external accessories
are wired up.

---

## Packet 5 — DDC IQ frame (radio → host, port 1035 + ddc_idx)

Layout (verified against a real captured frame, seq 17452):

| Offset | Size | Field |
|--------|------|-------|
| 0–3    | 4    | sequence (BE) |
| 4–11   | 8    | timestamp (BE, sample-clock count, may be 0) |
| 12–13  | 2    | bits per sample (BE) — always 24 |
| 14–15  | 2    | samples per frame (BE) — 238 for 24-bit single-DDC |
| 16…end | 6 each | I/Q samples, **24-bit big-endian signed**, I then Q |

For 1 DDC × 24-bit: payload = 1444 − 16 = 1428 bytes / 6 = **238
samples per frame**.

Sample decode in pseudocode:
```python
i = int.from_bytes(b[off:off+3], 'big', signed=True)
q = int.from_bytes(b[off+3:off+6], 'big', signed=True)
i_norm, q_norm = i / 2**23, q / 2**23   # → [-1, 1)
```

Our `parse_ddc_iq_frame()` already does exactly this and matches the
real radio. **No change needed in the parser logic itself**, only in
how it's called when the OS coalesces frames.

### Receive Segment Coalescing (RSC) on Windows

Captured frame sizes on UDP src port 1037: **1452, 2896, 4340,
5784**. After subtracting the 8-byte UDP header these are 1444, 2888
(=2×1444), 4332 (=3×1444), 5776 (=4×1444). Windows' NIC offload
combined up to 4 native frames into a single delivery.

The fix is to slice the recv buffer into chunks of 1444 (or whatever
the per-frame size for the current bits/SPP combo is) and parse each
chunk independently:

```python
data = sock.recvfrom(8192)   # bigger recv buffer
while data:
    frame_len = 16 + 238 * 6  # 1444 for 1×24-bit
    chunk, data = data[:frame_len], data[frame_len:]
    parse_ddc_iq_frame(chunk)
```

Sequence numbers in coalesced frames are consecutive (17452, 17453,
17454, 17455 — verified in the capture). Our seq-error counter will
correctly attribute drops to the radio rather than the OS.

Disabling RSC at the NIC level (`netsh int tcp set global rsc=disabled`
or per-NIC via `Disable-NetAdapterRsc`) is an alternative but should
NOT be the chosen fix — coalescing also happens via UDP Generic
Receive Offload on Linux and would resurface there.

---

## Mapped fixes — what to change in `lyra/protocol/p2/`

Concrete edits, in roughly the order they should be made and tested:

### `packets.py`

1. **General Packet builder** (`build_general_packet`):
   - Set `pkt[37] = 0x08` (phase-increment mode).
   - Default `enable_hardware_timer=True` so byte 38 = 0x01.
   - Add `pa_enable: bool = True` field, write `pkt[58] = 0x01`.
   - Add `alex_enable: int = 0x03` field for ORION2, write `pkt[59]`.

2. **DDC Specific builder** (`build_ddc_specific_packet`):
   - Existing layout is byte-correct; no change to the function.
   - Caller (P2Stream) must pass the right `ddc_enable_mask` and
     `ddcs` indices for the radio's board ID.

3. **High Priority builder** (`build_high_priority_packet`):
   - Existing field encoding (BE u32) is correct — but the value
     written must be a **phase increment**, not Hz. Two options:
     (a) caller passes phase increments; (b) builder takes Hz and
     converts internally (cleaner). Recommend (b): convert via
     `phase = round(freq_hz * (1 << 32) / 122_880_000)`.
   - Add support for the Alex0/Alex1 control words at bytes 1428–
     1435. For v1, hardcode the Thetis values
     (`0x01100002` / `0x01100010`); for v2, derive from frequency.

4. **Add `build_duc_specific_packet`** — emit the 60-byte packet
   using the safe defaults documented above.

5. **`parse_ddc_iq_frame`** — no change to the parser. Add a sibling
   `parse_ddc_iq_frames(data, frame_size)` that walks the buffer in
   `frame_size` chunks and yields one `IqFrame` per native frame.

### `stream.py`

1. After board ID is known (passed in from discovery), set
   `self._ddc_index = 2 if board_id in (ORION2, SATURN, ANGELIA, ORION) else 0`.
   Use this for the enable mask, the per-DDC config block, and the
   IQ destination port subscription.

2. Bind the IQ socket to **the local interface that reaches the
   radio** — same NIC selection logic as the multi-NIC discovery
   refactor we just landed. Otherwise the General Packet's
   `ddc_iq_destination_port` field needs to also be set, but the
   port alone isn't enough if the radio doesn't know which IP to
   send to. (Open question — pi-hpsdr always specifies a destination
   IP somewhere; verify on next pass.)

3. Send the new DUC Specific packet once at start, before High
   Priority.

4. Increase the IQ recv buffer to 8192 and switch from
   `parse_ddc_iq_frame(data)` to the new
   `parse_ddc_iq_frames(data, frame_size)`.

5. Send the General Packet refresh on a 500 ms cadence (matches
   Thetis; current code only sends it once at start).

### `boards.py`

1. Add a `uses_ddc2_offset: bool` field on `BoardSpec` (or compute
   it from a list `{ANGELIA, ORION, ORION2, SATURN}`). Used by
   `P2Stream` to pick the DDC slot.

2. Document the actual board ID for ANAN-G2 v4.3 firmware. The
   captured discovery reply shows `board_id = 10` (ORION2) — this
   is already handled by the boards table.

### Tests

1. New unit tests in `tests/test_anan_g2.py`:
   - `build_high_priority_packet` with `freq_hz=14_250_000` and
     `phase_mode=True` produces `0x1DB18C45` at bytes 17–20.
   - `build_general_packet` with default args produces `0x08` at
     byte 37.
   - `build_ddc_specific_packet` for ORION2 with RX1 at 192 kHz puts
     the config block at bytes 29–34 and sets enable mask byte 7
     to `0x04`.
   - `parse_ddc_iq_frames` with a 5776-byte buffer yields 4 frames
     with consecutive sequence numbers.

2. Replay test using `_iq_frame.txt` (real captured frame): assert
   `parse_ddc_iq_frame(real_frame)` returns 238 samples with values
   in the expected magnitude range.

---

## How to validate the fix end-to-end

1. Land the changes above behind a board-ID gate so HL2 (P1) and
   loopback paths don't regress.
2. Re-run `python -m lyra.protocol.p2.stream --ip 192.168.10.206
   --rate 192000 --freq 14250000 --seconds 10`. Expected output:
   `frames` should grow at ≈ 192000 / 238 ≈ 807 frames/sec — so
   ~8000 frames in 10 s. `seq_err` should stay 0.
3. Re-capture with dumpcap and diff our High Priority bytes against
   Thetis's. They should match byte-for-byte except for the
   sequence number, frequency value, and Alex bits.
4. Run the existing 47-test P2 suite plus the new ANAN-G2 tests.
5. Run loopback (`tools/p2_loopback.py`) to confirm no regression on
   the synthetic path.

---

## Reference artifacts

- `thetis_live.pcapng` — 8 s capture of working Thetis ↔ ANAN-G2.
  Filter to host→radio with `ip.src==192.168.10.173` to see the
  control packets; filter to `udp.srcport==1037` for IQ.
- `thetis_capture.pcapng` — earlier 19 MB capture of the radio
  streaming to a stale Thetis port (Thetis had crashed; useful for
  studying IQ format under varying RSC sizes).
- `_p1024.txt`, `_p1025.txt`, `_p1026.txt`, `_p1027.txt` — extracted
  hex of every host→radio packet on those ports.
- `_iq_frame.txt` — one real DDC2 IQ frame, used to validate the
  parser.
- pi-hpsdr `src/new_protocol.c` — the canonical client. When in
  doubt about a byte, search this file for it.
