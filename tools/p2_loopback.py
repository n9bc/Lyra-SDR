"""Synthetic openHPSDR Protocol 2 radio for testing P2Stream without hardware.

Pretends to be an Apache ANAN G2 (board ID 10 / SATURN). Responds to
P2 discovery on UDP 1024 and, after a General Packet + DDC Specific +
High Priority handshake, streams synthetic 24-bit complex IQ frames
to the host's chosen DDC0 destination port.

Usage:

    Terminal 1:    python tools/p2_loopback.py
    Terminal 2:    python -m lyra.protocol.p2.discovery --target 127.0.0.1
    Terminal 3:    python -m lyra.protocol.p2.stream --ip 127.0.0.1 --freq 14250000

Stop with Ctrl+C in terminal 1.

The synthetic signal is a complex tone at +5 kHz IF offset, scaled to
~ -20 dBFS. That's just enough to verify the parser end-to-end and to
let you see a peak in any spectrum view downstream.
"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import threading
import time
from typing import Optional

import numpy as np


# Match defaults in lyra.protocol.p2.packets
DEFAULT_DISCOVERY_PORT = 1024
DEFAULT_DDC_COMMAND_PORT = 1025
DEFAULT_HIGH_PRIORITY_HOST_PORT = 1027


def _build_discovery_reply(*, mac: bytes = b"\x02\x00\xDE\xAD\xBE\xEF",
                           board_id: int = 10) -> bytes:
    """Build a 60-byte SATURN-shaped discovery reply."""
    pkt = bytearray(60)
    pkt[4] = 0x02              # status idle
    pkt[5:11] = mac
    pkt[11] = board_id          # 10 = SATURN (ANAN-G2)
    pkt[12] = 104               # protocol v10.4
    pkt[13] = 50                # firmware code version
    pkt[19] = 0                 # metis version (unused on SATURN)
    pkt[20] = 8                 # 8 DDCs implemented
    pkt[21] = 0                 # frequency word, not phase
    pkt[22] = 0x01              # big-endian only
    pkt[23] = 0                 # not beta
    return bytes(pkt)


class P2Loopback:
    """Network listener that emulates a P2 SDR for end-to-end testing."""

    def __init__(self, bind_ip: str = "0.0.0.0", verbose: bool = False):
        self.bind_ip = bind_ip
        self.verbose = verbose

        # Sockets, bound in start()
        self._sock_disco: Optional[socket.socket] = None
        self._sock_general: Optional[socket.socket] = None
        self._sock_ddc_cmd: Optional[socket.socket] = None
        self._sock_high_priority: Optional[socket.socket] = None

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        # State updated by control packets, read by the streaming thread.
        self._client_iq_addr: Optional[tuple[str, int]] = None
        self._sample_rate_hz = 192_000
        self._running = False
        self._rx_freq_hz = 0
        self._state_lock = threading.Lock()

        self._iq_seq = 0  # per-port counter for DDC0 IQ stream
        self._mac = b"\x02\x00\xDE\xAD\xBE\xEF"

    # ─── public API ──────────────────────────────────────────────────────

    def start(self) -> None:
        # Discovery / General Packet share port 1024.
        self._sock_general = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_general.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_general.bind((self.bind_ip, DEFAULT_DISCOVERY_PORT))
        self._sock_general.settimeout(0.25)

        self._sock_ddc_cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_ddc_cmd.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_ddc_cmd.bind((self.bind_ip, DEFAULT_DDC_COMMAND_PORT))
        self._sock_ddc_cmd.settimeout(0.25)

        self._sock_high_priority = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_high_priority.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_high_priority.bind((self.bind_ip, DEFAULT_HIGH_PRIORITY_HOST_PORT))
        self._sock_high_priority.settimeout(0.25)

        self._threads = [
            threading.Thread(target=self._listen_disco_general, daemon=True),
            threading.Thread(target=self._listen_ddc_cmd, daemon=True),
            threading.Thread(target=self._listen_high_priority, daemon=True),
            threading.Thread(target=self._stream_iq, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for s in (self._sock_disco, self._sock_general,
                  self._sock_ddc_cmd, self._sock_high_priority):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=1.0)

    # ─── listeners ───────────────────────────────────────────────────────

    def _listen_disco_general(self) -> None:
        """Port 1024: byte[4]=0x02 → discovery request; byte[4]=0x00 → General Packet."""
        sock = self._sock_general
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 5:
                continue
            cmd = data[4]
            if cmd == 0x02:
                if self.verbose:
                    print(f"[loopback] discovery from {addr}, replying as SATURN")
                sock.sendto(_build_discovery_reply(mac=self._mac), addr)
            elif cmd == 0x00 and len(data) >= 19:
                ddc0_iq_port = struct.unpack(">H", data[17:19])[0]
                if ddc0_iq_port:
                    with self._state_lock:
                        self._client_iq_addr = (addr[0], ddc0_iq_port)
                    if self.verbose:
                        print(f"[loopback] general packet from {addr}, "
                              f"DDC0 IQ → {addr[0]}:{ddc0_iq_port}")

    def _listen_ddc_cmd(self) -> None:
        """Port 1025: DDC Specific. Read DDC0 sample rate (bytes 18-19)."""
        sock = self._sock_ddc_cmd
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) >= 20:
                rate_khz = struct.unpack(">H", data[18:20])[0]
                if rate_khz:
                    with self._state_lock:
                        self._sample_rate_hz = rate_khz * 1000
                    if self.verbose:
                        print(f"[loopback] DDC0 rate set to {rate_khz} kHz")

    def _listen_high_priority(self) -> None:
        """Port 1027: High Priority. Read run bit (byte 4 bit 0) and DDC0 freq (bytes 9-12)."""
        sock = self._sock_high_priority
        assert sock is not None
        while not self._stop.is_set():
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) >= 13:
                run_bit = bool(data[4] & 0x01)
                freq = struct.unpack(">I", data[9:13])[0]
                with self._state_lock:
                    prev = (self._running, self._rx_freq_hz)
                    self._running = run_bit
                    self._rx_freq_hz = freq
                if self.verbose and (prev[0] != run_bit or prev[1] != freq):
                    print(f"[loopback] HP: run={run_bit}, freq={freq} Hz")

    # ─── synthetic IQ streamer ───────────────────────────────────────────

    def _stream_iq(self) -> None:
        """Emit 1444-byte DDC0 IQ frames at the requested sample rate when
        running. The signal is a complex exponential at +5 kHz IF offset
        scaled to about -20 dBFS so anything downstream can see a peak."""
        n = 238  # samples per frame in the 1-DDC × 24-bit config
        scale = 0.1 * (1 << 23)   # ~-20 dBFS in 24-bit two's complement
        offset_hz = 5_000.0
        phase = 0.0
        timestamp = 0
        while not self._stop.is_set():
            with self._state_lock:
                running = self._running
                rate = self._sample_rate_hz
                addr = self._client_iq_addr

            if not (running and addr):
                time.sleep(0.05)
                continue

            packet_period = n / rate

            # Compute samples for this frame.
            phases = phase + (2.0 * math.pi * offset_hz / rate) * np.arange(n, dtype=np.float64)
            i_samples = (np.cos(phases) * scale).astype(np.int32)
            q_samples = (np.sin(phases) * scale).astype(np.int32)
            phase = (phases[-1] + (2.0 * math.pi * offset_hz / rate)) % (2.0 * math.pi)

            # Pack header.
            pkt = bytearray(1444)
            struct.pack_into(">I", pkt, 0, self._iq_seq & 0xFFFFFFFF)
            struct.pack_into(">Q", pkt, 4, timestamp & 0xFFFFFFFFFFFFFFFF)
            struct.pack_into(">H", pkt, 12, 24)
            struct.pack_into(">H", pkt, 14, n)

            # Pack 24-bit BE samples.
            for k in range(n):
                base = 16 + k * 6
                iv = int(i_samples[k]) & 0xFFFFFF
                qv = int(q_samples[k]) & 0xFFFFFF
                pkt[base + 0] = (iv >> 16) & 0xFF
                pkt[base + 1] = (iv >> 8) & 0xFF
                pkt[base + 2] = iv & 0xFF
                pkt[base + 3] = (qv >> 16) & 0xFF
                pkt[base + 4] = (qv >> 8) & 0xFF
                pkt[base + 5] = qv & 0xFF

            try:
                # Re-use the high-priority socket for sending — any of our
                # bound sockets works; the destination IP/port is what matters.
                if self._sock_high_priority is not None:
                    self._sock_high_priority.sendto(bytes(pkt), addr)
            except OSError:
                pass

            self._iq_seq = (self._iq_seq + 1) & 0xFFFFFFFF
            timestamp += n
            time.sleep(packet_period)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthetic openHPSDR Protocol 2 radio (SATURN/ANAN-G2 emulation)"
    )
    parser.add_argument("--bind", default="0.0.0.0",
                        help="Local IP to bind on (default: all NICs)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-packet log output")
    args = parser.parse_args()

    loopback = P2Loopback(bind_ip=args.bind, verbose=not args.quiet)
    loopback.start()
    print(f"P2 loopback running on {args.bind}:1024 (discovery/general), "
          f":1025 (DDC cmd), :1027 (high-priority). Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        loopback.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()
