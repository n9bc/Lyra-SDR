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

from lyra.protocol.p2.boards import lookup_board
from lyra.protocol.p2.packets import (
    DDC_IQ_FRAME_LEN_24BIT,
    DEFAULT_DDC_COMMAND_PORT,
    DEFAULT_DUC_COMMAND_PORT,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_HIGH_PRIORITY_HOST_PORT,
    DdcConfig,
    GeneralPacketConfig,
    HighPriorityConfig,
    build_ddc_specific_packet,
    build_duc_specific_packet,
    build_general_packet,
    build_high_priority_packet,
    parse_ddc_iq_frames,
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

    def __init__(
        self,
        radio_ip: str,
        sample_rate: int = 192_000,
        *,
        board_id: Optional[int] = None,
    ):
        """Open a P2 RX session against ``radio_ip``.

        ``board_id`` is the value the radio reported in discovery
        (byte 11 of the discovery reply). It controls which DDC slot
        carries RX1: Hermes / Atlas / Hermes-Lite use DDC0; ANGELIA /
        ORION / ORION-MkII / SATURN reserve DDC0+DDC1 internally and
        start user receivers at DDC2. Passing ``None`` defaults to
        DDC0 (Hermes-class behavior) — works with the synthetic
        loopback and any pre-Apache board, but produces zero IQ
        frames against a real ANAN-G2.
        """
        self.radio_ip = radio_ip
        self.sample_rate = sample_rate
        self.board_id = board_id

        # Pick the DDC slot for RX1 from the board table. Unknown
        # boards fall back to DDC0 — which is what the loopback uses.
        self._rx1_ddc_index = 0
        if board_id is not None:
            spec = lookup_board(board_id)
            if spec is not None:
                self._rx1_ddc_index = spec.ddc_offset_for_rx1

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
        self._seq_duc_specific = 0
        self._seq_high_priority = 0

        # Latest known control state — held here so the periodic refresh
        # thread re-sends consistent values.
        self._rx_freq_hz: int = 7_200_000
        # Open-collector output byte for an external filter board hooked
        # to Apache's OC pins. Zero by default (no filter board / unused
        # pins). Updated via `set_oc_bits` on every band change so
        # external relays follow the operator's tuning. The bit-to-band
        # mapping is the operator's choice (we just route bits onto the
        # wire); for an N2ADR-style board, callers pass the same byte
        # they'd write to HL2's C2[7:1] register.
        self._oc_bits: int = 0
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

        # Apache firmware uses the SOURCE port of incoming control packets
        # as the IQ destination, ignoring the port declared in the
        # General Packet body. Wire captures against ANAN-G2 confirmed
        # this: a control packet declared body=1035 but the radio sent
        # IQ back to the host's ephemeral source port (51538 in that
        # capture). To match that behavior we use a single shared socket
        # for sending control packets and receiving IQ — the OS-assigned
        # ephemeral source port becomes the IQ destination automatically.
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Big recv buffer so NIC-coalesced 4-8x1444 IQ frames fit in one recv.
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262_144)
        self._send_sock.bind(("0.0.0.0", 0))
        self._send_sock.settimeout(0.5)
        self._iq_sock = self._send_sock                        # alias
        self._iq_local_port = self._send_sock.getsockname()[1]
        # Loopback path still honors the declared body port; real Apache
        # ignores it. Set body = local_port - rx1_ddc_index so the
        # synthetic loopback can still compute (base + ddc) = local_port.
        iq_base_port = self._iq_local_port - self._rx1_ddc_index

        # 1. General Packet — declare the local port the radio should use
        #    as the IQ base, plus the Apache "magic" bytes (phase mode,
        #    HW timer, PA enable, dual-Alex enable) baked into the
        #    GeneralPacketConfig defaults.
        gen_cfg = GeneralPacketConfig(
            ddc_iq_destination_port=iq_base_port,
            high_priority_from_pc_port=DEFAULT_HIGH_PRIORITY_HOST_PORT,
            ddc_command_port=DEFAULT_DDC_COMMAND_PORT,
        )
        self._send(
            build_general_packet(self._seq_general, gen_cfg),
            DEFAULT_DISCOVERY_PORT,
        )
        self._seq_general += 1

        # 2. DDC Specific — enable RX1's DDC slot at the requested rate.
        ddc_cfg = DdcConfig(
            adc_source=0,
            sample_rate_hz=self.sample_rate,
            sample_size_bits=24,
        )
        # ANAN-G2 has 2 ADCs; pre-Apache boards have 1. Default to the
        # board's count if known, else 1.
        n_adcs = 1
        if self.board_id is not None:
            spec = lookup_board(self.board_id)
            if spec is not None:
                n_adcs = spec.n_adcs
        # Apache (DDC2-offset boards) ships dither on ADC0+1+2; Hermes-
        # class radios just leave it off. Wire-capture dither byte = 0x07.
        dither = 0x07 if self._rx1_ddc_index >= 2 else 0x00
        self._send(
            build_ddc_specific_packet(
                self._seq_ddc_specific,
                n_adcs=n_adcs,
                dither_mask=dither,
                ddc_enable_mask=(1 << self._rx1_ddc_index),
                ddcs={self._rx1_ddc_index: ddc_cfg},
            ),
            DEFAULT_DDC_COMMAND_PORT,
        )
        self._seq_ddc_specific += 1

        # 3. DUC Specific — pi-hpsdr always sends this at startup (even
        #    RX-only). Apache FPGA wants a DUC config before it fully
        #    arms the streaming engine.
        self._send(
            build_duc_specific_packet(self._seq_duc_specific),
            DEFAULT_DUC_COMMAND_PORT,
        )
        self._seq_duc_specific += 1

        # 4. High Priority — set RX1 freq, run=True.
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
                        ddc_freqs_hz={self._rx1_ddc_index: self._rx_freq_hz},
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

        # _iq_sock is an alias of _send_sock — close once.
        if self._send_sock is not None:
            self._send_sock.close()
            self._send_sock = None
            self._iq_sock = None

    def set_rx_freq_hz(self, hz: int) -> None:
        """Re-tune RX1 (DDC0). Sends a fresh High Priority packet."""
        self._rx_freq_hz = int(hz)
        if self._send_sock is None:
            return
        with self._high_priority_lock:
            self._send_high_priority_locked()

    def set_sample_rate(self, rate: int) -> None:
        """Change RX1's DDC sample rate on a running stream."""
        self.sample_rate = rate
        if self._send_sock is None:
            return
        ddc_cfg = DdcConfig(adc_source=0, sample_rate_hz=rate, sample_size_bits=24)
        n_adcs = 1
        if self.board_id is not None:
            spec = lookup_board(self.board_id)
            if spec is not None:
                n_adcs = spec.n_adcs
        self._send(
            build_ddc_specific_packet(
                self._seq_ddc_specific,
                n_adcs=n_adcs,
                ddc_enable_mask=(1 << self._rx1_ddc_index),
                ddcs={self._rx1_ddc_index: ddc_cfg},
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

    def set_oc_bits(self, pattern: int) -> None:
        """Set the open-collector output byte for the next High Priority
        send. Used by Radio's band-change pipeline to drive an external
        filter board hooked to the radio's OC pins. The bits are passed
        through verbatim — the caller decides the bit-to-band mapping.

        Idempotent on no-change: if the new pattern matches the current
        value the next refresh fires it anyway (cheap), so callers don't
        have to dedupe themselves.
        """
        with self._high_priority_lock:
            self._oc_bits = pattern & 0xFF
            # Push immediately if the stream is live — otherwise the
            # refresh-thread loop will pick it up at its next tick (1 s).
            # Swallow socket errors here: if the stream is mid-stop the
            # next refresh handles cleanup; we don't want band-change
            # in the GUI to blow up because of a network hiccup.
            if self._send_sock is not None:
                try:
                    self._send_high_priority_locked()
                except OSError:
                    pass

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
        # Write the RX1 frequency into DDC0/DDC1/RX1's slot. Wire captures
        # show this is required on ANAN-G2 even though only the active
        # DDC streams — the firmware appears to use DDC0 as a fallback
        # tuning state, and without it the IQ pipeline doesn't arm.
        # Cheap to do for all boards (extra zero writes are harmless on
        # Hermes-class).
        ddc_freqs = {0: self._rx_freq_hz, 1: self._rx_freq_hz}
        ddc_freqs[self._rx1_ddc_index] = self._rx_freq_hz
        # OC bits track band-change updates from `set_oc_bits`. Default
        # 0 = unused; the wire-capture default `0x90` from the working
        # 20m capture is preserved as a fallback when no caller has
        # written a band-specific value yet.
        oc_byte = self._oc_bits if self._oc_bits != 0 else (
            0x90 if self._rx1_ddc_index >= 2 else 0)
        cfg = HighPriorityConfig(
            run=True,
            ddc_freqs_hz=ddc_freqs,
            # Wire-captured Alex control words for an idle ANAN-G2 on 20 m.
            # Future work: derive per-band from the current frequency
            # (needs Apache hardware doc bit-to-band map).
            alex0_word=0x01100010 if self._rx1_ddc_index >= 2 else 0,
            alex1_word=0x01100002 if self._rx1_ddc_index >= 2 else 0,
            open_collector_outputs=oc_byte,
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
        # Big enough to hold up to ~5 native 1444-byte frames if the NIC
        # coalesces them (Windows RSC, Linux UDP-GRO).
        recv_size = DDC_IQ_FRAME_LEN_24BIT * 8
        while not self._stop_event.is_set():
            try:
                data, _addr = self._iq_sock.recvfrom(recv_size)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows surfaces ICMP "port unreachable" from a previous
                # send as ECONNRESET on the next recvfrom. Common while
                # the radio is starting up its listeners — keep going.
                continue
            except OSError:
                # Socket truly closed (stop() ran). Exit.
                break

            for frame in parse_ddc_iq_frames(data):
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
    parser.add_argument("--board-id", type=int, default=None,
                        help="Discovery byte-11 board ID. Defaults to None "
                             "(DDC0 / Hermes-class). Use 10 for ANAN-G2 / "
                             "SATURN, 5 for ORION-MkII, etc. — picks the "
                             "right DDC slot for the board.")
    args = parser.parse_args()

    stream = P2Stream(args.ip, sample_rate=args.rate, board_id=args.board_id)

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
