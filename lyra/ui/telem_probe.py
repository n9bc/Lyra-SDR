"""HL2 Telemetry Probe — reverse-engineer the C0 telemetry mapping
on a specific Hermes-Lite-2 firmware.

Why this exists
---------------
The HPSDR Protocol 1 EP6 frame carries telemetry by rotating the C0
byte's upper-bits "address" field through several values, each of
which carries a different 4-byte payload (C1..C4). Different HL2
firmware lineages use different address-to-AIN mappings, and the
public docs disagree about where temperature and supply voltage
land. This dialog captures live C&C bytes for a few seconds and
summarises them so the operator can:

  * Confirm the stream is healthy (frames of varied addresses arrive)
  * See raw min/max/last values per address
  * Eyeball which address has values that look like AD9866 temp
    (~1000-1500 ADC counts at room-temp idle) and which looks like
    a 12 V supply via 10:1 divider (~1300-1500 counts).

The output table can be pasted into a chat / issue so the per-rig
mapping in `_decode_hl2_telemetry` (lyra/protocol/stream.py) can be
adjusted in one line. This is much faster than guessing through
multiple firmware doc revisions.

The probe doesn't change anything — it's a passive tap on the
stream's `_decode_hl2_telemetry` path via FrameStats._probe_cb.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


class TelemetryProbeDialog(QDialog):
    """Modal dialog that captures HL2 C&C telemetry for a fixed
    duration and shows a per-address summary.

    Usage:
        TelemetryProbeDialog(radio, parent=window).exec()

    Requirements:
        radio.is_streaming must be True — telemetry only arrives
        while EP6 frames are flowing. The dialog refuses to start
        capture otherwise and displays a friendly note.
    """

    CAPTURE_SECONDS = 4

    def __init__(self, radio, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.radio = radio
        self.setWindowTitle("HL2 Telemetry Probe")
        self.setMinimumSize(720, 480)

        # Per-address aggregates: addr -> dict(count, c1234_last,
        # c12_min, c12_max, c34_min, c34_max)
        self._stats: dict[int, dict] = defaultdict(self._empty)

        self._build_ui()

        # Wire / refuse based on stream state.
        if not getattr(radio, "is_streaming", False):
            self.note.setText(
                "Stream is not running. Click ▶ Start on the toolbar "
                "first, then re-open this probe."
            )
            self.start_btn.setEnabled(False)

    # ── lifecycle ─────────────────────────────────────────────────────
    @staticmethod
    def _empty():
        return {
            "count": 0,
            "c1234_last": (0, 0, 0, 0),
            "c12_min": 0xFFFF, "c12_max": 0,
            "c34_min": 0xFFFF, "c34_max": 0,
        }

    def _build_ui(self):
        v = QVBoxLayout(self)

        intro = QLabel(
            "<b>What this does:</b> captures live HL2 telemetry bytes "
            f"for {self.CAPTURE_SECONDS} seconds, then groups them by "
            "C0 telemetry address so we can see exactly which address "
            "carries temperature, supply voltage, etc. on your specific "
            "HL2 firmware.<br><br>"
            "<b>What to do:</b> click <b>Start Capture</b>, wait for "
            "the bar to fill, then read the table below. Addresses with "
            "values in the ~1000-1500 ADC range are usually the AINs we "
            "care about (temp / supply / power). Copy the table to share."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        self.note = QLabel("")
        self.note.setStyleSheet("color: #ff8c3a; font-weight: 600;")
        v.addWidget(self.note)

        # Progress bar fills while capture runs
        self.progress = QProgressBar()
        self.progress.setRange(0, self.CAPTURE_SECONDS * 10)
        self.progress.setValue(0)
        v.addWidget(self.progress)

        # Results table
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Addr (C0>>3)",
            "Frames",
            "C1..C4 (last)",
            "C1:C2 BE",
            "C3:C4 BE",
            "C1:C2 LE",
            "C3:C4 LE",
        ])
        mono = QFont("Consolas")
        mono.setPointSize(9)
        self.table.setFont(mono)
        self.table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.table, 1)

        # Buttons
        h = QHBoxLayout()
        self.start_btn = QPushButton("▶ Start Capture")
        self.start_btn.clicked.connect(self._start_capture)
        h.addWidget(self.start_btn)
        self.copy_btn = QPushButton("Copy table to clipboard")
        self.copy_btn.clicked.connect(self._copy_to_clipboard)
        self.copy_btn.setEnabled(False)
        h.addWidget(self.copy_btn)
        h.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        h.addWidget(close_btn)
        v.addLayout(h)

    # ── capture engine ────────────────────────────────────────────────
    def _start_capture(self):
        if not getattr(self.radio, "is_streaming", False):
            self.note.setText(
                "Stream stopped — restart it and try again.")
            return

        # Reset aggregates and table
        self._stats.clear()
        self.table.setRowCount(0)
        self.progress.setValue(0)
        self.copy_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.note.setText("Capturing… keep the stream running.")

        # Install probe callback on the stream's FrameStats.
        # Decoder forwards (addr, C1..C4) for every block.
        stream = getattr(self.radio, "_stream", None)
        if stream is None or not hasattr(stream, "stats"):
            self.note.setText("No stream object available — open a stream first.")
            self.start_btn.setEnabled(True)
            return
        stream.stats._probe_cb = self._on_sample

        # Tick at 10 Hz to advance the progress bar.
        self._ticks = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(100)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

    def _on_tick(self):
        self._ticks += 1
        self.progress.setValue(self._ticks)
        if self._ticks >= self.CAPTURE_SECONDS * 10:
            self._tick_timer.stop()
            self._stop_capture()

    def _stop_capture(self):
        # Detach the probe so subsequent stream traffic doesn't keep
        # adding to our aggregates.
        try:
            stream = self.radio._stream
            stream.stats._probe_cb = None
        except Exception:
            pass

        self._render_table()
        self.note.setText(
            f"Capture complete — {sum(s['count'] for s in self._stats.values())} "
            f"telemetry blocks across {len(self._stats)} address(es). "
            "Look for an address whose 12-bit values land in 800-1500 "
            "(temperature) or 1200-1500 (supply on 10:1 divider)."
        )
        self.start_btn.setEnabled(True)
        self.copy_btn.setEnabled(bool(self._stats))

    # Called from the streaming thread — keep cheap, don't touch Qt!
    def _on_sample(self, addr: int, c1: int, c2: int, c3: int, c4: int):
        s = self._stats[addr]
        s["count"] += 1
        s["c1234_last"] = (c1, c2, c3, c4)
        v_be12 = (c1 << 8 | c2) & 0x0FFF
        v_be34 = (c3 << 8 | c4) & 0x0FFF
        s["c12_min"] = min(s["c12_min"], v_be12)
        s["c12_max"] = max(s["c12_max"], v_be12)
        s["c34_min"] = min(s["c34_min"], v_be34)
        s["c34_max"] = max(s["c34_max"], v_be34)

    def _render_table(self):
        rows = sorted(self._stats.items())
        self.table.setRowCount(len(rows))
        for r, (addr, s) in enumerate(rows):
            c1, c2, c3, c4 = s["c1234_last"]
            be12 = (c1 << 8 | c2) & 0x0FFF
            be34 = (c3 << 8 | c4) & 0x0FFF
            le12 = (c2 << 8 | c1) & 0x0FFF
            le34 = (c4 << 8 | c3) & 0x0FFF
            cells = [
                f"{addr}  (C0={addr<<3:#04x})",
                str(s["count"]),
                f"{c1:02X} {c2:02X} {c3:02X} {c4:02X}",
                f"{be12:4d}  ({s['c12_min']}..{s['c12_max']})",
                f"{be34:4d}  ({s['c34_min']}..{s['c34_max']})",
                f"{le12:4d}",
                f"{le34:4d}",
            ]
            for col, txt in enumerate(cells):
                item = QTableWidgetItem(txt)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(r, col, item)
        self.table.resizeColumnsToContents()

    def _copy_to_clipboard(self):
        from PySide6.QtGui import QGuiApplication
        # Tab-separated lines so it pastes cleanly into chat / issues.
        lines = ["addr\tframes\tC1..C4(last)\tC1:C2 BE (min..max)\tC3:C4 BE (min..max)\tC1:C2 LE\tC3:C4 LE"]
        for addr, s in sorted(self._stats.items()):
            c1, c2, c3, c4 = s["c1234_last"]
            be12 = (c1 << 8 | c2) & 0x0FFF
            be34 = (c3 << 8 | c4) & 0x0FFF
            le12 = (c2 << 8 | c1) & 0x0FFF
            le34 = (c4 << 8 | c3) & 0x0FFF
            lines.append(
                f"{addr}\t{s['count']}\t"
                f"{c1:02X} {c2:02X} {c3:02X} {c4:02X}\t"
                f"{be12} ({s['c12_min']}..{s['c12_max']})\t"
                f"{be34} ({s['c34_min']}..{s['c34_max']})\t"
                f"{le12}\t{le34}"
            )
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.note.setText("Table copied to clipboard ✓")
