"""openHPSDR Protocol 2 RX streaming session.

Single-DDC, 24-bit, RX-only. TX (DUC, DUCIQ, mic) is intentionally out
of scope for v1 — the design doc has the rationale.

The public surface mirrors `lyra.protocol.stream.HL2Stream` closely so
the eventual `lyra.radio.Radio` integration can pick the right stream
class with minimal branching.

Run from the command line:
    python -m lyra.protocol.p2.stream --ip 192.168.1.50 --freq 14250000
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from lyra.protocol.p2.packets import (
    DEFAULT_DDC_COMMAND_PORT,
    DEFAULT_DDC_IQ_BASE_PORT,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_HIGH_PRIORITY_HOST_PORT,
    DdcConfig,
    GeneralPacketConfig,
    HighPriorityConfig,
    build_ddc_specific_packet,
    build_general_packet,
    build_high_priority_packet,
    parse_ddc_iq_frame,
)


@dataclass
class P2FrameStats:
    """Mirror of P1's FrameStats with P2-relevant fields.

    `seq_expected = -1` flags "first frame not yet seen". Once primed,
    every received seq# increments expected by one and any mismatch
    bumps `seq_errors`.
    """
    frames: int = 0
    samples: int = 0
    seq_expected: int = -1
    seq_errors: int = 0
    last_timestamp: int = 0
    high_priority_resends: int = 0
    last_high_priority_send_ms: float = 0.0


class P2Stream:
    """Open a P2 RX stream to an Apache (or compatible) SDR.

    Lifecycle:
        s = P2Stream("192.168.1.50", sample_rate=192000)
        s.start(on_samples=lambda samples, stats: ...,
                rx_freq_hz=14_250_000)
        ...
        s.stop()
    """

    HIGH_PRIORITY_REFRESH_S = 1.0     # spec recommends periodic refresh

    def __init__(self, radio_ip: str, sample_rate: int = 192_000):
        self.radio_ip = radio_ip
        self.sample_rate = sample_rate

        # Sockets — opened in start(), closed in stop()
        self._send_sock: Optional[socket.socket] = None
        self._iq_sock: Optional[socket.socket] = None
        self._iq_local_port: int = 0      # ephemeral; set after bind

        # Threads
        self._rx_thread: Optional[threading.Thread] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Sequence counters (one per host→radio port, per spec).
        self._seq_general = 0
        self._seq_ddc_specific = 0
        self._seq_high_priority = 0

        # Latest known control state — held here so the periodic refresh
        # thread re-sends consistent values.
        self._rx_freq_hz: int = 7_200_000
        self._high_priority_lock = threading.Lock()

        self.stats = P2FrameStats()

    # ─── public API ──────────────────────────────────────────────────────

    def start(
        self,
        on_samples: Callable[[np.ndarray, P2FrameStats], None],
        rx_freq_hz: Optional[int] = None,
        lna_gain_db: Optional[int] = None,
    ) -> None:
        if self._rx_thread and self._rx_thread.is_alive():
            raise RuntimeError("P2Stream already started")
        if rx_freq_hz is not None:
            self._rx_freq_hz = int(rx_freq_hz)

        self._stop_event.clear()
        self.stats = P2FrameStats()

        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._send_sock.bind(("0.0.0.0", 0))

        self._iq_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._iq_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._iq_sock.bind(("0.0.0.0", 0))
        self._iq_sock.settimeout(0.5)
        self._iq_local_port = self._iq_sock.getsockname()[1]

        # 1. General Packet — declare the local port the radio should use
        #    as the destination for DDC0 IQ data.
        gen_cfg = GeneralPacketConfig(
            ddc_iq_destination_port=self._iq_local_port,
            high_priority_from_pc_port=DEFAULT_HIGH_PRIORITY_HOST_PORT,
            ddc_command_port=DEFAULT_DDC_COMMAND_PORT,
        )
        self._send(
            build_general_packet(self._seq_general, gen_cfg),
            DEFAULT_DISCOVERY_PORT,
        )
        self._seq_general += 1

        # 2. DDC Specific — enable DDC0 only, single ADC, 24-bit, requested rate.
        ddc_cfg = DdcConfig(
            adc_source=0,
            sample_rate_hz=self.sample_rate,
            sample_size_bits=24,
        )
        self._send(
            build_ddc_specific_packet(
                self._seq_ddc_specific,
                n_adcs=1,
                ddc_enable_mask=0x01,
                ddcs={0: ddc_cfg},
            ),
            DEFAULT_DDC_COMMAND_PORT,
        )
        self._seq_ddc_specific += 1

        # 3. High Priority — set RX1 freq, run=True. Spec note: the radio
        #    starts streaming when it has run=1 plus DDC0 enabled.
        self._send_high_priority_locked()

        # Launch worker threads.
        self._rx_thread = threading.Thread(
            target=self._rx_loop, args=(on_samples,), daemon=True
        )
        self._rx_thread.start()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, daemon=True
        )
        self._refresh_thread.start()

        if lna_gain_db is not None:
            self.set_lna_gain_db(lna_gain_db)

    def stop(self) -> None:
        self._stop_event.set()

        # Try to send a clean stop (run=0) so the radio releases its
        # connection state. Failure to send is non-fatal — we're tearing
        # down anyway.
        if self._send_sock is not None:
            try:
                with self._high_priority_lock:
                    cfg = HighPriorityConfig(
                        run=False,
                        ddc_freqs_hz={0: self._rx_freq_hz},
                    )
                    self._send_sock.sendto(
                        build_high_priority_packet(self._seq_high_priority, cfg),
                        (self.radio_ip, DEFAULT_HIGH_PRIORITY_HOST_PORT),
                    )
                    self._seq_high_priority += 1
            except OSError:
                pass

        if self._rx_thread is not None:
            self._rx_thread.join(timeout=2.0)
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2.0)

        if self._iq_sock is not None:
            self._iq_sock.close()
            self._iq_sock = None
        if self._send_sock is not None:
            self._send_sock.close()
            self._send_sock = None

    def set_rx_freq_hz(self, hz: int) -> None:
        """Re-tune RX1 (DDC0). Sends a fresh High Priority packet."""
        self._rx_freq_hz = int(hz)
        if self._send_sock is None:
            return
        with self._high_priority_lock:
            self._send_high_priority_locked()

    def set_sample_rate(self, rate: int) -> None:
        """Change the DDC0 sample rate on a running stream."""
        self.sample_rate = rate
        if self._send_sock is None:
            return
        ddc_cfg = DdcConfig(adc_source=0, sample_rate_hz=rate, sample_size_bits=24)
        self._send(
            build_ddc_specific_packet(
                self._seq_ddc_specific,
                n_adcs=1,
                ddc_enable_mask=0x01,
                ddcs={0: ddc_cfg},
            ),
            DEFAULT_DDC_COMMAND_PORT,
        )
        self._seq_ddc_specific += 1

    def set_lna_gain_db(self, gain_db: int) -> None:
        """v1: not yet wired (Apache LNA control lives in High Priority too,
        but the byte layout differs across boards). Stored for future use."""
        # Accept the value silently; effective when the per-board gain
        # mapping is implemented in Phase 6.
        self._pending_lna_gain_db = gain_db

    def queue_tx_audio(self, audio) -> None:
        """v1 raises — TX over P2 is out of scope. Kept on the surface so
        higher-level code can call this method on either stream class."""
        raise NotImplementedError("P2 TX path not implemented in v1 (RX-only)")

    def clear_tx_audio(self) -> None:
        """No-op in v1 (no TX queue to drain). Safe to call from sink code."""

    # ─── internal: send helpers ──────────────────────────────────────────

    def _send(self, payload: bytes, dest_port: int) -> None:
        if self._send_sock is None:
            return
        self._send_sock.sendto(payload, (self.radio_ip, dest_port))

    def _send_high_priority_locked(self) -> None:
        """Caller MUST hold self._high_priority_lock."""
        if self._send_sock is None:
            return
        cfg = HighPriorityConfig(
            run=True,
            ddc_freqs_hz={0: self._rx_freq_hz},
        )
        self._send_sock.sendto(
            build_high_priority_packet(self._seq_high_priority, cfg),
            (self.radio_ip, DEFAULT_HIGH_PRIORITY_HOST_PORT),
        )
        self._seq_high_priority += 1
        self.stats.last_high_priority_send_ms = time.monotonic() * 1000.0

    # ─── internal: worker loops ──────────────────────────────────────────

    def _rx_loop(self, on_samples: Callable[[np.ndarray, P2FrameStats], None]) -> None:
        assert self._iq_sock is not None
        while not self._stop_event.is_set():
            try:
                data, _addr = self._iq_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            frame = parse_ddc_iq_frame(data)
            if frame is None:
                continue

            if self.stats.seq_expected == -1:
                self.stats.seq_expected = (frame.seq + 1) & 0xFFFFFFFF
            else:
                if frame.seq != self.stats.seq_expected:
                    self.stats.seq_errors += 1
                self.stats.seq_expected = (frame.seq + 1) & 0xFFFFFFFF

            self.stats.frames += 1
            self.stats.samples += frame.samples_per_frame
            self.stats.last_timestamp = frame.timestamp

            try:
                on_samples(frame.samples, self.stats)
            except Exception as exc:
                # Don't let consumer exceptions kill the RX thread —
                # just log via print (matches HL2Stream's defensive style)
                print(f"[p2.stream] on_samples raised: {exc!r}")

    def _refresh_loop(self) -> None:
        """Re-send the High Priority packet ~once per second so the radio's
        view of state stays current. Spec recommends this; it's cheap."""
        while not self._stop_event.wait(self.HIGH_PRIORITY_REFRESH_S):
            try:
                with self._high_priority_lock:
                    self._send_high_priority_locked()
                self.stats.high_priority_resends += 1
            except OSError:
                # Socket has gone away (stop() ran first). Exit cleanly.
                return


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="openHPSDR Protocol 2 RX session")
    parser.add_argument("--ip", required=True, help="Radio IP address")
    parser.add_argument("--rate", type=int, default=192_000,
                        help="Sample rate in Hz (must be one of "
                             "48000/96000/192000/384000/768000/1536000)")
    parser.add_argument("--freq", type=int, default=14_250_000,
                        help="RX1 (DDC0) frequency in Hz")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="How long to stream before stopping")
    args = parser.parse_args()

    stream = P2Stream(args.ip, sample_rate=args.rate)

    def _on_samples(samples, stats):
        # Quiet — main loop prints periodic summary.
        pass

    stream.start(on_samples=_on_samples, rx_freq_hz=args.freq)

    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(1.0)
            print(
                f"frames={stream.stats.frames}  "
                f"samples={stream.stats.samples}  "
                f"seq_err={stream.stats.seq_errors}  "
                f"hp_resends={stream.stats.high_priority_resends}"
            )
    finally:
        stream.stop()
        print("Stopped.")
