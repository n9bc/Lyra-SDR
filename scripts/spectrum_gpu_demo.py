"""Standalone trace + waterfall demo for the GPU panadapter widgets.

Runnable via:
    python scripts/spectrum_gpu_demo.py
    python scripts/spectrum_gpu_demo.py --backend opengl
    python scripts/spectrum_gpu_demo.py --fps 30
    python scripts/spectrum_gpu_demo.py --bins 8192

Purpose
-------
1. Visually validate that SpectrumGpuWidget + WaterfallGpuWidget
   work end-to-end as a paired display the way they will when
   integrated into Lyra's main window in Phase B.
2. Provide a profile-able artifact for Phase A.6 — runnable under
   `py-spy record` / `cProfile` / etc. so we can capture real per-
   frame cost numbers WITHOUT in-process instrumentation perturbing
   the measurement (the lesson from the perf-experiment rollback
   earlier today).
3. Serve as the canonical "this is how to drive these widgets from
   external data" example for anyone integrating them later.

Synthetic spectrum content
--------------------------
Mimics what Lyra would actually display from an HL2:
  - Realistic noise floor at ~-110 dBFS with Gaussian jitter
  - Three stationary signals at fixed bin positions (CW carrier,
    SSB-style burst, weak digital tone)
  - One slow-sweeping carrier that walks across the band over ~20
    seconds (drives the waterfall pattern visibly)
  - All values fed to BOTH widgets via the public set_spectrum /
    push_row APIs — same path Phase B integration will use.

Frame loop
----------
A QTimer runs at the requested FPS (default 30 Hz) and:
  - Generates one frame of synthetic spectrum data
  - Calls SpectrumGpuWidget.set_spectrum() for the trace
  - Calls WaterfallGpuWidget.push_row() for the waterfall

The widgets handle their own repaints — set_spectrum / push_row
both call self.update() internally.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QSplitter,
    QStatusBar, QVBoxLayout, QWidget,
)

from lyra.ui.spectrum_gpu import SpectrumGpuWidget, WaterfallGpuWidget


# ── Synthetic spectrum generator ──────────────────────────────────────


class SyntheticSpectrum:
    """Generates one frame of realistic-looking spectrum data per call.

    The contents are designed to exercise both widgets visibly:
      - Noise floor: tells us the dB→pixel mapping is right
      - Stationary signals: peaks should appear in the SAME columns
        on every frame for both trace and waterfall
      - Sweeping carrier: scrolls across the screen over ~20 sec,
        drives a visible pattern in the waterfall

    All output is in dBFS-like units (range roughly -130 to -30).
    """

    def __init__(self, n_bins: int = 4096):
        self.n = n_bins
        self.t0 = time.monotonic()
        # Stationary signals: (bin_index, peak_dB, width_bins)
        # Picked to be at ¼, ½, ¾ of the spectrum width so they
        # don't overlap and we can visually verify each in its
        # expected position.
        self._stationary = [
            (n_bins // 4,     -55.0,  3),    # narrow CW-style spike
            (n_bins // 2,     -45.0, 12),    # SSB-like burst
            (3 * n_bins // 4, -85.0,  6),    # weak digital tone
        ]
        # Pre-build a baseline noise envelope so the noise floor has
        # mild structure (more realistic than pure white noise).
        rng = np.random.default_rng(seed=42)
        bumps = rng.normal(0, 1.5, n_bins)
        # Smooth with a small window to give "1/f-ish" character
        from numpy.lib.stride_tricks import sliding_window_view
        kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1])
        padded = np.concatenate([bumps[:2], bumps, bumps[-2:]])
        smooth = np.convolve(padded, kernel, mode="valid")
        self._nf_envelope = (smooth - smooth.mean()).astype(np.float32)

    def next_frame(self) -> np.ndarray:
        """Return one (n_bins,) float32 spectrum frame in dBFS."""
        t = time.monotonic() - self.t0
        # Noise floor with low-amplitude per-frame jitter
        noise = np.random.normal(0, 1.5, self.n).astype(np.float32)
        spec = -110.0 + self._nf_envelope + noise
        # Stationary signals — Gaussian peaks
        x = np.arange(self.n, dtype=np.float32)
        for bin_idx, peak_db, width in self._stationary:
            bump = (peak_db - (-110.0)) * np.exp(
                -((x - bin_idx) ** 2) / (2.0 * width * width))
            spec = np.maximum(spec, -110.0 + bump.astype(np.float32))
        # Slow-sweeping carrier — moves across full band every ~20 sec.
        # 20 sec = 2π radians, so freq = 0.1π rad/sec.
        sweep_pos = (
            self.n * 0.5
            + (self.n * 0.4) * math.sin(t * 0.1 * math.pi))
        sweep_width = 4.0
        sweep_peak = -50.0
        sweep_bump = (sweep_peak - (-110.0)) * np.exp(
            -((x - sweep_pos) ** 2) / (2.0 * sweep_width * sweep_width))
        spec = np.maximum(spec, -110.0 + sweep_bump.astype(np.float32))
        return spec


# ── Demo window ───────────────────────────────────────────────────────


class GpuDemoWindow(QMainWindow):
    """Top-level window: trace on top, waterfall below, status bar
    showing FPS achieved + frame counter."""

    def __init__(self, fps: int, bins: int):
        super().__init__()
        self.setWindowTitle(f"Lyra GPU Panadapter Demo — {bins} bins @ {fps} fps")
        self.resize(1200, 700)

        # Build the data source first so the widgets get a real frame
        # immediately (not the synthetic sine/bump test patterns the
        # widgets default to before set_spectrum / push_row is called).
        self._source = SyntheticSpectrum(n_bins=bins)
        self._fps = fps
        self._frame_count = 0
        self._fps_window: list[float] = []   # timestamps for FPS calc

        # ── Widget layout ────────────────────────────────────────
        # Trace on top, waterfall below — same arrangement Lyra's
        # panadapter panel uses. Splitter so the operator can resize
        # the trace/waterfall ratio at will (also a stress test for
        # our per-frame viewport setting).
        self._trace = SpectrumGpuWidget()
        self._water = WaterfallGpuWidget()
        # Initial sizes: ~40% trace, ~60% waterfall feels right
        # ergonomically. Splitter will respect drag-resize after.
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._trace)
        splitter.addWidget(self._water)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        splitter.setHandleWidth(2)
        self.setCentralWidget(splitter)

        # Status bar — FPS achieved + frame counter, updated 1×/sec.
        self._status = QStatusBar()
        self._status_label = QLabel("starting…")
        self._status.addPermanentWidget(self._status_label)
        self.setStatusBar(self._status)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._tick_status)
        self._status_timer.start()

        # ── Frame timer ─────────────────────────────────────────
        # Drives the per-frame data generation + push to widgets.
        # Interval is 1000/fps milliseconds.
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(max(1, int(1000 / fps)))
        self._frame_timer.timeout.connect(self._tick_frame)
        self._frame_timer.start()

    def _tick_frame(self) -> None:
        """Generate one frame and push to both widgets."""
        spec = self._source.next_frame()
        # Same dB range as Lyra's typical default — gives a similar
        # visual look to what operators see in the live app.
        self._trace.set_spectrum(spec, min_db=-130.0, max_db=-30.0)
        self._water.push_row(spec, min_db=-130.0, max_db=-30.0)
        # FPS bookkeeping
        self._frame_count += 1
        now = time.monotonic()
        self._fps_window.append(now)
        cutoff = now - 1.0
        while self._fps_window and self._fps_window[0] < cutoff:
            self._fps_window.pop(0)

    def _tick_status(self) -> None:
        achieved = len(self._fps_window)
        self._status_label.setText(
            f"frame {self._frame_count}  ·  achieved {achieved} fps  "
            f"·  target {self._fps} fps")


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fps", type=int, default=30,
                   help="Target frame rate (Hz). Default 30.")
    p.add_argument("--bins", type=int, default=4096,
                   help="Synthetic FFT bin count. Default 4096.")
    args = p.parse_args()

    app = QApplication(sys.argv)
    win = GpuDemoWindow(fps=args.fps, bins=args.bins)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
