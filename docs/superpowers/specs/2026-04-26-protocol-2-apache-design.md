# Protocol 2 (Apache ANAN G2 / Brick II) Support — Design

**Status:** Draft, written autonomously while user is asleep. Awaiting user review on wake-up.
**Date:** 2026-04-26
**Author:** Claude (auto mode)
**Branch:** `feat/protocol-2-apache`
**Spec source:** `openHPSDR Ethernet Protocol v4.4` (TAPR/OpenHPSDR-Firmware), extracted to `/tmp/p2spec/p2-v4.4.txt` during research.

---

## 1. Goal & non-goals

**Goal.** Add openHPSDR Protocol 2 (P2) support to Lyra so that Apache Labs ANAN G2 (board ID 10 = "SATURN") and the Apache Brick II can be discovered and used as RX sources. Keep the existing Hermes Lite 2 / 2+ Protocol 1 path bit-for-bit unchanged.

**Non-goals (this spec).**
- TX over P2 (DUC, DUCIQ, mic-in routing, PA/Alex control on TX). Risk of unintended emission with malformed packets; user has explicitly OK'd RX-first.
- Pre-distortion / PureSignal over P2.
- Reworking the P1 path. P1 stays exactly as-is.
- Changing the UI to expose protocol selection beyond a "what we discovered" list.
- Hardware capability negotiation beyond what discovery returns (no XML hardware description / `0xFE` / `0xFF` reply parsing in v1 — only the simple Appendix-A reply form).
- Multi-DDC simultaneous streaming, VITA-49, time-stamping, GPS-locked sample clocks. Single DDC, single ADC.

**Open scope decisions made autonomously** (override on wake):
1. RX-only first; TX deferred (matches the project's existing trajectory: "RX functional, TX in progress").
2. Maintainer not consulted; design assumes upstream merge is the goal but the **first** action when user wakes is to file an issue on `N8SDR1/Lyra-SDR` to confirm scope direction. **Do not push or PR until that conversation happens.**
3. Brick II's board ID is unknown — not in v4.4 spec. Treat any unknown-but-Apache-shaped reply as a P2 device with conservative defaults; surface the raw board ID byte in the UI/log so the user can identify it on first connect.
4. Build coexists: P1 path untouched, P2 layered alongside under `lyra/protocol/p2/`. Existing `lyra/protocol/{discovery,stream}.py` remain the P1 implementation. No moves, no renames, no shims into the P1 namespace.

---

## 2. Protocol-level facts that drive the design

Pulled from spec v4.4 (Mar 2019). Numbers are byte offsets within UDP payload.

### 2.1 Discovery

- **Port:** UDP 1024 (same as P1).
- **Host → radio (60 bytes):**
  - `[0..3]` Sequence number, big-endian uint32. Set to `0x00000000`.
  - `[4]` `0x02` — Command = Discovery.
  - `[5..59]` zero.
- **Radio → host (60 bytes, from port 1024):**
  - `[0..3]` `0x00000000` (seq=0).
  - `[4]` `0x02` (idle) or `0x03` (busy / connected to another host). Special values `0xFE`/`0xFF` indicate the long-form (XML or full hardware description) reply — out of scope.
  - `[5..10]` MAC, MSB first.
  - `[11]` Board Type (see table).
  - `[12]` openHPSDR Protocol version supported (decimal/10, e.g. 104 = v10.4).
  - `[13]` Firmware Code Version.
  - `[14..18]` Mercury 0..3 + Penny code versions (Atlas only; ignore for non-Atlas).
  - `[19]` Metis Code Version.
  - `[20]` Number of DDCs implemented.
  - `[21]` Frequency-or-phase-word (0=freq, 1=phase).
  - `[22]` Available Endian modes (bitmask).
  - `[23]` Beta version (0=false).
  - `[24..59]` reserved.

**Board Type lookup (v4.4 Appendix A):**

| ID  | board                    |
|-----|--------------------------|
| 0   | ATLAS                    |
| 1   | HERMES (ANAN-10, 100)    |
| 2   | HERMES (ANAN-10E, 100B)  |
| 3   | ANGELIA (ANAN-100D)      |
| 4   | ORION (ANAN-200D)        |
| 5   | ORION Mk II (ANAN-7/8000DLE) |
| 6   | Hermes Lite              |
| 10  | SATURN (ANAN-G2)         |
| 254 | XML hardware description follows |
| 255 | Full hardware description follows |

**Brick II.** Not in v4.4. Likely either (a) reports as one of 1–6 with a different code/firmware version, or (b) is in a newer spec revision. v1 implementation: log-and-show any unknown ID and treat the reply as a generic P2 device. User confirms the actual ID on first connect.

### 2.2 Port plan (post-General-Packet defaults)

| port | direction        | purpose                                 |
|------|------------------|-----------------------------------------|
| 1024 | bidir            | Discovery + General Packet (Command Reply) |
| 1025 | host → radio     | DDC Specific Command (per-DDC config)   |
| 1025 | radio → host     | High Priority Status (PTT, ADC overflow, PLL) |
| 1026 | host → radio     | DUC Specific Command (TX) — **out of scope v1** |
| 1026 | radio → host     | Mic / Line samples — **out of scope v1** |
| 1027 | host → radio     | High Priority From PC (PTT, freqs, alex) |
| 1028 | host → radio     | DDC Audio (RX speaker out — sent to radio for analog out) — **out of scope v1**, we play audio on the PC |
| 1029 | host → radio     | DUC0 I&Q (TX baseband) — **out of scope v1** |
| 1035 + N | radio → host | DDCn I&Q (RX baseband). DDC0 = 1035, DDC1 = 1036, ... |

The General Packet (port 1024, byte `[4] = 0x00`) is sent first after discovery to confirm/override these port assignments. We send the defaults explicitly so we never rely on stale state from a prior session.

### 2.3 DDC I&Q packet (radio → host on 1035 for DDC0)

Per spec, 1444 bytes per packet for the standard "1 DDC × 24 bits per sample × 238 samples" config:

- `[0..3]` seq #, BE uint32 (per-port counter, resets to 0 on stop/power-on).
- `[4..11]` 64-bit sample-clock timestamp (VITA-49 §6.1.5.1). Optional — we read it but don't depend on it.
- `[12..13]` bits per sample, BE uint16 (FPGA currently always 24).
- `[14..15]` I&Q samples per frame, BE uint16 (typically 238 for 24-bit, single DDC).
- `[16..1443]` 238 × (I3 + Q3) samples, each I/Q a 24-bit big-endian signed two's-complement integer.

Decimation rate is set per-DDC via the DDC Specific packet. Sample-rate codes are different from P1 (P1 uses two bits; P2 has a per-DDC 8-bit rate field with much wider range).

### 2.4 General Packet (host → radio:1024) — first packet after discovery

Sets the port assignments, wideband config, endian mode. Byte `[4]` = `0x00` distinguishes it from discovery (`0x02`). 60-byte payload.

### 2.5 High Priority From PC (host → radio:1027)

Sets PTT, RX/TX frequencies (one 32-bit word per DDC and per DUC), Alex filter selections, OC pins, attenuators. Sent on every state change; spec recommends periodic refresh (~once per second). For RX-only, we set RX frequencies and zero out everything TX-related.

### 2.6 DDC Specific Command (host → radio:1025)

Per-DDC: ADC source, sample rate, gain, sync/multiplex. Sent on every change.

### 2.7 What's qualitatively different from P1

| dimension         | P1                                      | P2                                            |
|-------------------|-----------------------------------------|-----------------------------------------------|
| transport         | one socket, multiplexed by EP id (`0x02 EP2`, `0x06 EP6`) | multiple UDP sockets, one per logical endpoint |
| sequence numbers  | one per direction                       | per-port (each endpoint has its own counter)  |
| start/stop        | special `0x04` packet                   | RX starts when General Packet sent + DDC enabled; stops by command |
| keepalive         | mandatory EP2 every EP6 frame           | not required; periodic High Priority refresh recommended |
| samples / frame   | 63 per USB-block × 2 blocks (126)       | 238 per packet (single DDC, 24-bit)           |
| sample encoding   | 24-bit BE I + 24-bit BE Q + 16-bit mic  | 24-bit BE I + 24-bit BE Q (mic is its own port) |
| audio             | inline in EP2 (Left+Right slots)        | separate "DDC Audio" port (out of scope v1)   |
| C&C               | 5-byte C0..C4 fields piggybacked on every USB block | dedicated High Priority + DDC/DUC Specific packets |

These differences mean P2 cannot be implemented as an "adapter" over the P1 stream — it needs its own socket-management layer.

---

## 3. Architecture

### 3.1 Module layout

```
lyra/protocol/
├── discovery.py        # P1 discovery (UNCHANGED)
├── stream.py           # P1 stream (UNCHANGED)
├── __init__.py         # NEW: re-exports + unified discover_all()
└── p2/
    ├── __init__.py
    ├── discovery.py    # P2 discovery handshake + RadioInfo (P2 variant)
    ├── packets.py      # General Packet, High Priority, DDC Specific encoders
    ├── stream.py       # P2Stream — multi-socket RX session
    └── boards.py       # board ID → name + capability table (Saturn, Hermes, etc.)
```

Why a sub-package, not a side-by-side flat module: the P2 layer naturally splits into ~4 files (discovery, packet encoders, the streaming runtime, board metadata). Putting them in `lyra/protocol/p2/` keeps the P1 namespace clean and makes "what is P2 code?" visually obvious in `git log` and code review.

### 3.2 Discovery aggregation

`lyra/protocol/__init__.py` will expose:

```python
def discover_all(timeout_s: float = 1.5, attempts: int = 2,
                 local_bind: str = "0.0.0.0",
                 target_ip: Optional[str] = None) -> list[DiscoveredRadio]
```

Where `DiscoveredRadio` is a small union-style dataclass:

```python
@dataclass
class DiscoveredRadio:
    protocol: Literal["P1", "P2"]
    ip: str
    mac: str
    board_id: int
    board_name: str           # human-readable
    code_version: int
    is_busy: bool
    raw: object               # the protocol-specific RadioInfo for callers that need detail
```

`discover_all` runs P1 discovery (existing code) and P2 discovery (new), merges results by MAC, and returns the union. If a single radio happens to respond to both (unlikely, but theoretically possible if a unit speaks both), prefer the P2 entry — P2 is strictly newer.

This is the **only** seam between the two protocols. Callers that don't need the union can still import `lyra.protocol.discovery.discover` (P1) or `lyra.protocol.p2.discovery.discover` (P2) directly.

### 3.3 P2Stream surface

P2Stream mirrors HL2Stream's public surface as closely as practical so the (eventual) Radio integration is small:

```python
class P2Stream:
    def __init__(self, radio_ip: str, sample_rate: int = 48000)
    def start(self, on_samples, rx_freq_hz=None, lna_gain_db=None)
    def stop(self)
    def set_sample_rate(self, rate: int)
    def set_lna_gain_db(self, gain_db: int)
    def queue_tx_audio(self, audio)        # raises NotImplementedError in v1
    def clear_tx_audio(self)               # no-op in v1
    @property stats: P2FrameStats
```

`on_samples(samples: np.ndarray, stats: P2FrameStats)` — same callback contract as P1. `samples` is `complex64`, normalized to `[-1, 1)`. Stats carries packets-received / sequence errors / radio-side ADC overflow indicator from High Priority Status.

Internally:
- One UDP socket bound to an ephemeral port for sending — General Packet, DDC Specific, High Priority From PC all go to `(radio_ip, 1024|1025|1027)` from this socket.
- One UDP socket bound to the ephemeral port that the radio will use as the *destination* for DDC0 IQ data and High Priority Status. We tell the radio about this port via the General Packet.
- A receive thread runs `select()` on the IQ socket, parses 1444-byte frames, and invokes `on_samples`.
- A small periodic timer (1 Hz) re-sends High Priority From PC to refresh state (matches spec recommendation).

### 3.4 Board capability table

`lyra/protocol/p2/boards.py`:

```python
@dataclass(frozen=True)
class BoardSpec:
    id: int
    name: str
    short_name: str          # "ANAN-G2", "ANAN-7000DLE", etc.
    family: str              # "Apache", "HermesLite", "Atlas"
    max_sample_rate_hz: int
    n_adcs: int
    notes: str

BOARDS: dict[int, BoardSpec] = {
    0:  BoardSpec(0,  "Atlas",                "Atlas",          "Atlas",       384_000, 4, "..."),
    1:  BoardSpec(1,  "HERMES (ANAN-10/100)", "ANAN-10",        "Apache",      384_000, 1, "..."),
    2:  BoardSpec(2,  "HERMES (ANAN-10E/100B)", "ANAN-10E",     "Apache",      384_000, 1, "..."),
    3:  BoardSpec(3,  "ANGELIA (ANAN-100D)",  "ANAN-100D",      "Apache",      384_000, 2, "..."),
    4:  BoardSpec(4,  "ORION (ANAN-200D)",    "ANAN-200D",      "Apache",      384_000, 2, "..."),
    5:  BoardSpec(5,  "ORION Mk II",          "ANAN-7000DLE",   "Apache",      384_000, 2, "..."),
    6:  BoardSpec(6,  "Hermes Lite",          "HL2",            "HermesLite",   384_000, 1, "..."),
    10: BoardSpec(10, "SATURN (ANAN-G2)",     "ANAN-G2",        "Apache",    1_536_000, 2, "..."),
}

def lookup(board_id: int) -> BoardSpec | None: ...
```

Purpose: clean place to record what we know about each radio's caps without scattering `if board_id == X` across the codebase. When the user fires up Brick II and we see a new ID, adding it is one row.

### 3.5 Radio-class integration (deferred — not in v1 commits)

`lyra/radio.py` currently does `from lyra.protocol.stream import HL2Stream, SAMPLE_RATES` and instantiates HL2Stream directly. Wiring P2 in needs:

1. A `protocol` field on the discovered radio (already returned by `discover_all`).
2. At connect time, `Radio` picks `HL2Stream` or `P2Stream` based on protocol.
3. The two stream classes need a shared minimal interface (already the case if P2Stream mirrors HL2Stream's surface).

This is a small, focused change once P2Stream is proven. v1 commits stop short of this — Radio integration is a follow-up commit so the diff is reviewable in isolation. Until then, P2 is exercised via `python -m lyra.protocol.p2.discovery` and (Phase-3+) `python -m lyra.protocol.p2.stream` CLIs.

---

## 4. Data flow (RX, single DDC)

```
1. User clicks "Start" in UI (or runs CLI tool).
2. discover_all() — P1 broadcast + P2 broadcast on UDP 1024.
   Both listen for replies; merge by MAC; return union.
3. UI / caller picks one DiscoveredRadio.
4. If protocol == "P2":
    a. Open send-socket (ephemeral port, used for everything host→radio).
    b. Open recv-socket bound to ephemeral port P_rx.
    c. Send General Packet (host→1024) declaring P_rx as the DDC0 destination port.
    d. Send DDC Specific Command (host→1025) — DDC0 enabled, sample rate, ADC0, 24-bit.
    e. Send High Priority From PC (host→1027) — RX1 freq, no PTT.
    f. Recv loop on recv-socket: parse 1444-byte DDC0 frames, decode to complex64, fire on_samples.
    g. Periodic 1Hz re-send of High Priority for state refresh.
5. On stop: send DDC Specific with DDC0 disabled, then close sockets.
```

For P1, flow is unchanged from today.

---

## 5. Error handling

- **No discovery reply.** `discover_all` returns an empty list. Caller (UI / CLI) shows "no radios found" — same UX as today.
- **Discovery reply with unknown board ID.** Logged, included in `discover_all` results with `board_name = f"Unknown ({board_id})"`. UI shows it; user can still try to connect (P2 with default config is reasonable for any IDed radio that responds with `0x02`/`0x03`).
- **Radio busy (status `0x03`).** Returned via `is_busy=True`. Caller decides whether to display, refuse to connect, or warn.
- **Long-form discovery reply (status `0xFE`/`0xFF`).** Parsed minimally (board ID still at byte 11), full XML/hardware description ignored. Logged at debug level. Future work to parse properly.
- **Sequence-number gap on DDC IQ.** Increment `stats.seq_errors`, continue. Do NOT drop the packet — the samples are still valid, only ordering is suspect. Same policy as the P1 path (`HL2Stream._rx_loop`).
- **Malformed packet (wrong size, bad seq parsing).** Drop, increment a malformed-frame counter. No loud errors — UDP loses packets and consumer gear sometimes burps.
- **Socket errors (ECONNREFUSED on send, ENETUNREACH).** Stop the stream cleanly, surface to the caller via the stats object. This matches what P1 does on `OSError` in `_rx_loop`.
- **Periodic High Priority send failure.** Log once, continue. The radio will keep streaming with stale state for at least several seconds per the spec; the next successful send will catch up.

---

## 6. Testing

### 6.1 Unit tests (no hardware needed)

`lyra/protocol/p2/tests/`:
- `test_discovery_packets.py` — encode discovery request, decode known reply byte-strings (one per board ID), verify all fields.
- `test_packets.py` — encode General Packet, DDC Specific, High Priority From PC; assert byte layouts match spec tables.
- `test_iq_decode.py` — feed a synthetic 1444-byte DDC frame with known I/Q sample values, verify `complex64` output matches expected float values to within float-precision tolerance.
- `test_boards.py` — lookup table coverage, unknown-ID fallback.

These run on Linux/Windows/Mac, no SDR required, < 1 second total.

### 6.2 Loopback test (no hardware needed)

`tools/p2_loopback.py` (CLI tool, not part of the lyra package): a tiny UDP server that pretends to be a P2 SATURN, replies to discovery, sends synthetic DDC IQ frames at a chosen sample rate. Useful for validating the consumer side of `P2Stream` end-to-end without an Apache radio.

### 6.3 Hardware smoke test (user runs against G2)

Doc in `docs/p2-bringup.md`:
1. Power on G2, confirm same subnet.
2. `python -m lyra.protocol.p2.discovery` — expect one line of output: IP, MAC, board name "SATURN (ANAN-G2)", code version, num DDCs.
3. `python -m lyra.protocol.p2.stream --freq 14250000` — expect status output every second: packets/sec, samples/sec, seq errors. ADC overflow flag from High Priority Status visible too.
4. (post-Radio integration) launch Lyra UI, pick the G2 from the connection dropdown, verify spectrum + audio.

### 6.4 P1 regression

After every P2 commit, run:
```
python -m lyra.protocol.discovery
python -m lyra.ui.app          # smoke-launch, confirm it starts and the HL2 path imports cleanly
```
P1 imports must remain unaffected. Build the executable (`pyinstaller --noconfirm --clean build/lyra.spec`) and confirm no new warnings about missing modules.

---

## 7. Phasing — what gets built when

**Phase 0 — Spec & branch (this commit).**
- `feat/protocol-2-apache` branch created.
- This design doc.

**Phase 1 — P2 discovery.**
- `lyra/protocol/p2/__init__.py`, `lyra/protocol/p2/boards.py`, `lyra/protocol/p2/discovery.py`.
- CLI: `python -m lyra.protocol.p2.discovery`.
- Unit tests for boards lookup and discovery packet encode/decode.
- `lyra/protocol/__init__.py` adds `discover_all()` aggregator.
- Commit, run build, confirm HL2 path still works.

**Phase 2 — P2 RX streaming.**
- `lyra/protocol/p2/packets.py` (General Packet, DDC Specific, High Priority From PC encoders).
- `lyra/protocol/p2/stream.py` (P2Stream class — sockets, recv loop, periodic refresh).
- CLI: `python -m lyra.protocol.p2.stream`.
- Unit tests for packet encoders + IQ decode.
- Commit, run build.

**Phase 3 — Loopback test harness.**
- `tools/p2_loopback.py` synthetic radio.
- A combined unit+integration test that spins up the loopback in a background thread and runs P2Stream against it for ~1 second.
- Commit.

**Phase 4 — Documentation.**
- `docs/p2-bringup.md` user-facing test recipe.
- Update `docs/protocol_notes.md` with a "Protocol 2" appendix pointing to v4.4 spec.
- Update `docs/backlog.md` to mark P2-RX as in-progress and TX-over-P2 as a backlog item.
- Commit.

**Stop here** for the autonomous run. Awake-user reviews. If approved, the next phase (deferred to user-driven session):

**Phase 5 — Radio class integration.** Wire `P2Stream` into `lyra/radio.py` so the UI can connect to a discovered P2 radio. Small, focused diff once Phases 1-4 are landed and reviewed.

**Phase 6+ (future).** TX over P2, mic-in via port 1026, DDC Audio out via port 1028, multi-DDC, capability XML parsing.

---

## 8. Risks & mitigations

| risk                                                          | mitigation                                                                              |
|---------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| Spec v4.4 is from 2019; G2 firmware may have moved on         | v1 sticks to fields stable since v3.x. CLI dumps raw bytes for unknown reply tail. User can capture wireshark on first connect to verify. |
| Brick II board ID not in v4.4                                 | Unknown-ID fallback connects with conservative defaults. User identifies real ID on first try, we add to `BOARDS` table. |
| User has no Apache hardware available right now (only G2)     | Phase 1 testable via packet captures + unit tests; Phase 2 testable via loopback harness; Phase 3+ requires actual G2. User can verify when hardware is on. |
| P2 broadcast collides with P1 broadcast, both replies arrive on the same port | Both protocols use UDP 1024 for discovery. The reply byte `[4]` has different valid values (`0x02`/`0x03` for both, but P1 reply is shorter and has `0xEF 0xFE` at front). Disambiguate by reply-length + first two bytes. |
| Ephemeral port collisions when running multiple P2 sessions   | OS handles ephemeral port allocation. v1 supports one P2Stream per process (matches P1 behavior). |
| Maintainer rejects the scope expansion                        | Design doc + Phase-1-only branch can be presented as "here's what it would look like". Worst case: user maintains on fork. **File issue first** before any push. |
| TX over P2 is a major separate effort                         | Explicitly out of scope. v1 raises `NotImplementedError` from `queue_tx_audio`. |

---

## 9. What this design intentionally does NOT do

- Does not refactor `lyra/radio.py`. That file is 2722 lines and tightly coupled to P1 byte layouts. Refactoring it to support both protocols cleanly is its own design exercise; v1 keeps it untouched and adds P2 alongside.
- Does not introduce a "Protocol" abstract base class. A shared shape on `P2Stream` and `HL2Stream` is enough; ABC ceremony adds taxes for one more class. If a third protocol shows up later, then introduce the ABC.
- Does not change the UI. v1 P2 is exercised via CLIs. UI integration follows Radio integration in Phase 5.
- Does not touch the build pipeline (`build/lyra.spec`). New modules under `lyra/` are picked up automatically by PyInstaller.

---

## 10. Files touched in v1 (Phases 0-4)

**New:**
- `docs/superpowers/specs/2026-04-26-protocol-2-apache-design.md` (this file)
- `lyra/protocol/p2/__init__.py`
- `lyra/protocol/p2/boards.py`
- `lyra/protocol/p2/discovery.py`
- `lyra/protocol/p2/packets.py`
- `lyra/protocol/p2/stream.py`
- `lyra/protocol/p2/tests/__init__.py`
- `lyra/protocol/p2/tests/test_discovery_packets.py`
- `lyra/protocol/p2/tests/test_packets.py`
- `lyra/protocol/p2/tests/test_iq_decode.py`
- `lyra/protocol/p2/tests/test_boards.py`
- `tools/p2_loopback.py`
- `docs/p2-bringup.md`

**Modified:**
- `lyra/protocol/__init__.py` (new file in practice — currently `lyra/protocol/` has no `__init__.py`; add one with `discover_all()` aggregator).
- `docs/protocol_notes.md` (add "Protocol 2" appendix).
- `docs/backlog.md` (mark P2-RX as in-progress, TX-over-P2 as backlog).

**Untouched:**
- `lyra/protocol/discovery.py`
- `lyra/protocol/stream.py`
- `lyra/radio.py`
- `lyra/ui/*`
- `build/*`
- `requirements.txt`

---

## 11. After this branch

1. User reviews this design doc and the Phase 1-4 commits.
2. Issue filed at `https://github.com/N8SDR1/Lyra-SDR/issues` describing the proposal: scope, why P2 matters for Apache hardware coverage, link to the spec, link to the branch (private until maintainer agrees).
3. If maintainer says go: open PR for Phases 1-4, get review, merge.
4. Then design Phase 5 (Radio integration).
5. Then design Phase 6 (TX over P2) — separate spec, more diligence.
