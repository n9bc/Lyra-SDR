# Bringing up Protocol 2 against an Apache radio

This is the test recipe for verifying the new openHPSDR Protocol 2
support against real Apache hardware (ANAN G2, Brick II) or against the
synthetic loopback. It does **not** require any UI work — Phase 5
(wiring P2 into the `Radio` class so the UI dropdown shows it) is a
follow-up after this design lands.

If you've never touched the P2 layer before, read
`docs/superpowers/specs/2026-04-26-protocol-2-apache-design.md` first.

## What you should have

- A clean Lyra checkout on the `feat/protocol-2-apache` branch.
- Python 3.11+ on PATH (check: `py -3.14 --version` or `python --version`).
- One of the following:
  - An Apache ANAN G2 (or any P2-capable Apache rig) on the same
    subnet as your PC, and the rig powered on.
  - A Lin00bs / EU1SW Brick II (or any other Hermes-Lite-derived
    homebrew rig). It identifies itself as **Hermes Lite (board ID
    6)** on the wire and is supported automatically — discovery
    just reports `Hermes Lite`, no special handling needed.
  - Neither — use the loopback below instead.

No new Python packages are needed; the P2 layer uses stdlib + numpy
(already a Lyra dependency).

## Step 1 — discover the radio

```
py -3.14 -m lyra.protocol.p2.discovery
```

Expected output (one line per radio):

```
192.168.1.50    AA:BB:CC:DD:EE:FF  ANAN-G2  proto=v10.4  fw=50  ddcs=8  busy=False
```

Discovery now broadcasts in parallel from every local IPv4 interface
(WiFi + wired NIC + Hyper-V/WSL virtual switches), so a multi-NIC host
should find the radio without any extra flags. If discovery still
turns up empty:

- Confirm the radio is reachable (`ping <radio-ip>`).
- If broadcast is suppressed by your network/firewall, try unicast:
  `py -3.14 -m lyra.protocol.p2.discovery --target 192.168.1.50`.
- Force a single specific NIC if multi-NIC fan-out somehow misbehaves:
  `py -3.14 -m lyra.protocol.p2.discovery --bind 192.168.1.20`
  (replace with the IP of the NIC the radio is on).
- Allow Python through Windows Defender Firewall (UDP 1024 inbound).
- Add `--raw` to see the full 60-byte reply if you want to verify the
  parse field-by-field.

## Step 2 — start an RX session

```
py -3.14 -m lyra.protocol.p2.stream --ip 192.168.1.50 --rate 192000 --freq 14250000 --seconds 30
```

You should see a status line every second:

```
frames=1140  samples=271320  seq_err=0  hp_resends=2
```

What to look for:
- **frames** climbs roughly at `sample_rate / 238` (e.g. 192 kHz / 238 ≈
  806 frames/sec). Anything within ±5% is healthy.
- **seq_err** stays at 0. A small handful (single digits) over a long
  session means UDP packet loss; consistently growing means either bad
  network conditions or our parser is rejecting valid frames (file an
  issue with the `--raw` discovery dump).
- **hp_resends** = elapsed seconds (the High Priority refresh ticks at
  1 Hz).

## Step 3 — try with the loopback (no hardware)

If you don't have an Apache radio handy, run the synthetic radio in
one terminal and either CLI in another. The loopback emulates a SATURN
(ANAN-G2) and emits a complex tone at +5 kHz IF offset.

Terminal 1:
```
py -3.14 tools/p2_loopback.py
```

Terminal 2:
```
py -3.14 -m lyra.protocol.p2.discovery --target 127.0.0.1
py -3.14 -m lyra.protocol.p2.stream --ip 127.0.0.1 --freq 14250000 --seconds 5
```

The loopback doesn't enforce sample rate precisely — it's a Python
loop, not an FPGA — so the samples-per-second figure will be lower than
requested. The point of the loopback is to verify the wire-level
exchange. Real Apache hardware will hit the requested rate.

## Step 4 — run the unit tests

```
py -3.14 -m unittest discover -s lyra/protocol/p2/tests -v
```

47 tests, ~0 seconds. They never touch the network.

## Brick II / Hermes-Lite homebrew rigs

The Lin00bs / EU1SW "Brick II" is **not an Apache product** — it's a
homebrew transceiver that mimics an ANAN-10E mechanically but runs
Hermes-Lite gateware with P2 support. On the wire it presents as
**board ID 6 (Hermes Lite)** and rides the same `BoardSpec(6, ...)`
row as any HL-class board. Stock HL2 firmware is P1-only, so in
practice anything that arrives via P2 discovery on board ID 6 is a
Brick II — but since the wire format is identical, no special
handling is needed and the discovery output just reports `Hermes Lite`.

## Reporting a genuinely unknown board ID

If discovery returns `Unknown(id=N)` for a board that's neither in our
table nor a Hermes-Lite derivative:

1. Re-run with `--raw` to capture the full 60-byte reply hex.
2. Open an issue (or note for the maintainer) with:
   - The reported ID
   - The raw bytes
   - The radio model and firmware version
3. We'll add the row to `lyra/protocol/p2/boards.py`.

The discovery / stream code branches on board ID for the DDC slot
offset (Hermes-class = DDC0, ORION2-class = DDC2). An unknown ID
defaults to DDC0; if the radio is actually Apache ORION-family it
won't stream until we add the right offset.
