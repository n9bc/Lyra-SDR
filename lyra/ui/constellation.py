"""Lyra constellation overlay for the panadapter.

Renders a stylized version of the Lyra constellation as a faint
watermark behind the spectrum trace. The shape is the classic Lyra
asterism: Vega at the top, the small triangle of Vega + ε + ζ, and
the parallelogram of β / γ / δ / ζ below. Real-sky-ish positions, but
tweaked for visual balance in a wide panadapter rectangle.

Visual treatment (per operator preference, see commit-log):
  - Stylized (B): brighter Vega, simplified parallelogram + triangle
  - Edge-faded (C): radial alpha falloff from the widget center so
    the constellation stays out of the trace's way in the middle
  - Vega pulse (C): slow sinusoidal alpha modulation on Vega only

Drawn as the FIRST overlay so passband, marker, notches, and labels
all sit on top.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen


# Star positions in normalized constellation-cell coordinates
# [(0..1, 0..1)]. Sky-up orientation: Vega top, parallelogram bottom.
# The triangle (Vega + ε + ζ) is intentionally COMPACT and the
# parallelogram (β / δ / γ) is intentionally WIDER than the triangle
# — that's the proper Lyra silhouette and makes the shape readable
# at a glance on the panadapter (vs. collapsing to a thin kite when
# the two are similar widths).
#
# Tuple layout: (name, nx, ny, base_brightness)
#   nx, ny  : 0..1 normalized position within the constellation cell
#   base    : 0..1 brightness multiplier (Vega = 1.0, dimmer stars < 1)
LYRA_STARS = [
    ("Vega",  0.50, 0.05, 1.00),  # α Lyr — brightest, gets the pulse
    ("eps",   0.42, 0.28, 0.55),  # ε Lyr (Double Double) — close to Vega
    ("zet",   0.58, 0.28, 0.55),  # ζ Lyr — close to Vega
    ("bet",   0.18, 0.62, 0.55),  # β Lyr (Sheliak) — wide-left of parallelogram
    ("del",   0.82, 0.62, 0.50),  # δ Lyr — wide-right of parallelogram
    ("gam",   0.50, 0.95, 0.55),  # γ Lyr (Sulafat) — bottom apex
]
LYRA_STARS_BY_NAME = {s[0]: s for s in LYRA_STARS}

# Connecting lines drawn between stars (constellation lines).
LYRA_LINES = [
    ("Vega", "eps"),
    ("Vega", "zet"),
    ("eps",  "zet"),  # base of the small triangle below Vega
    ("eps",  "bet"),  # parallelogram left side
    ("zet",  "del"),  # parallelogram right side
    ("bet",  "gam"),  # parallelogram bottom-left
    ("del",  "gam"),  # parallelogram bottom-right
]


# Visual tuning constants. The constellation should be visible
# everywhere, just dimmer in the central trace area than near the
# corners. First-pass numbers were too aggressive (BASE_ALPHA=110 +
# inner-fade-to-zero meant most stars were invisible on a wide
# panadapter); these are tuned to be readable on a black background.
BASE_ALPHA          = 180   # max alpha (0..255) for the brightest pixel
LINE_ALPHA_MULT     = 0.50  # lines dimmer than stars
EDGE_FADE_CENTER    = 0.40  # alpha multiplier at the widget center
EDGE_FADE_CORNER    = 1.00  # alpha multiplier at the corners
STAR_RADIUS_PX      = 2.5   # base star dot radius
VEGA_RADIUS_PX      = 4.5   # Vega is the showpiece
VEGA_PULSE_PERIOD_S = 3.5   # full sine cycle in seconds
VEGA_PULSE_MIN      = 0.55  # alpha at the dim end of the pulse
VEGA_PULSE_MAX      = 1.00  # alpha at the bright end
STAR_COLOR          = QColor(190, 215, 255)    # cool blue-white
VEGA_COLOR          = QColor(230, 240, 255)    # slightly bluer/brighter
LINE_COLOR          = QColor(160, 190, 225)


def _edge_fade(nx: float, ny: float) -> float:
    """Radial alpha multiplier ∈ [EDGE_FADE_CENTER, EDGE_FADE_CORNER].

    nx, ny are normalized to [0,1] across the widget. Stars near the
    widget center are dimmer (less likely to fight the trace); stars
    near the corners are at full alpha. Falloff is smooth (smoothstep)
    rather than a sharp cutoff. Never returns 0 — the operator wants
    the constellation visible everywhere, just attenuated centrally."""
    dx = nx - 0.5
    dy = ny - 0.5
    # Max possible radial distance in normalized space is sqrt(0.5) ~= 0.707.
    r = math.hypot(dx, dy) / 0.707  # 0 at center, 1 at corners
    # Smoothstep over [0, 1] for a soft sigmoid-ish curve.
    t = max(0.0, min(1.0, r))
    s = t * t * (3.0 - 2.0 * t)
    return EDGE_FADE_CENTER + (EDGE_FADE_CORNER - EDGE_FADE_CENTER) * s


def _vega_pulse_factor() -> float:
    """0..1 multiplier for Vega's alpha, animated over time.

    Sinusoid scaled to [VEGA_PULSE_MIN, VEGA_PULSE_MAX]. Driven by
    time.monotonic() so it's continuous across paint events without
    needing a separate QTimer — every panadapter repaint advances
    the phase naturally."""
    phase = (time.monotonic() % VEGA_PULSE_PERIOD_S) / VEGA_PULSE_PERIOD_S
    s = 0.5 * (1.0 - math.cos(2.0 * math.pi * phase))   # 0..1
    return VEGA_PULSE_MIN + s * (VEGA_PULSE_MAX - VEGA_PULSE_MIN)


def draw(painter: QPainter, w: int, h: int) -> None:
    """Render the Lyra constellation overlay into the given widget area.

    Both spectrum_gpu.SpectrumGpuWidget and spectrum.SpectrumWidget
    call this from their _draw_overlays method as the first overlay
    so subsequent draws (passband, marker, notches) sit on top."""
    if w <= 0 or h <= 0:
        return

    # Pre-compute pixel positions + edge-fade for each star.
    #
    # Width and height are scaled independently so the constellation
    # can stretch horizontally on a wide panadapter without becoming
    # a tall skinny kite. The vertical extent fills nearly the full
    # widget height; the horizontal extent is roughly 60% of the
    # widget width (capped to ~2x the height so the shape doesn't
    # become unrecognizably squashed on extremely wide displays).
    cell_h = int(h * 0.92)
    cell_w = min(int(w * 0.60), int(cell_h * 2.0))
    cx = w // 2
    cy = int(h * 0.50)
    star_px: dict[str, tuple[int, int, float, float]] = {}
    for name, nx, ny, base in LYRA_STARS:
        # Map (0..1, 0..1) within (cell_w, cell_h) centered on (cx, cy).
        x = cx + int((nx - 0.5) * cell_w)
        y = cy + int((ny - 0.5) * cell_h)
        # Edge-fade is computed in widget-normalized coords so a star
        # near the widget edge gets full alpha regardless of where it
        # lives inside the constellation cell.
        fade = _edge_fade(x / max(1, w), y / max(1, h))
        star_px[name] = (x, y, base, fade)

    pulse = _vega_pulse_factor()

    painter.setRenderHint(QPainter.Antialiasing, True)

    # Draw connecting lines first (they sit under the star dots).
    line_alpha_max = int(BASE_ALPHA * LINE_ALPHA_MULT)
    for a_name, b_name in LYRA_LINES:
        ax, ay, _, fa = star_px[a_name]
        bx, by, _, fb = star_px[b_name]
        # Line alpha is the average fade of its two endpoints — that
        # way a line whose endpoints straddle the trace area dims
        # uniformly along its length.
        alpha_f = 0.5 * (fa + fb)
        alpha = int(line_alpha_max * alpha_f)
        if alpha < 1:
            continue
        col = QColor(LINE_COLOR)
        col.setAlpha(alpha)
        painter.setPen(QPen(col, 1, Qt.SolidLine))
        painter.drawLine(ax, ay, bx, by)

    # Draw stars.
    for name, (x, y, base, fade) in star_px.items():
        alpha_f = base * fade
        is_vega = (name == "Vega")
        if is_vega:
            alpha_f *= pulse
        alpha = int(BASE_ALPHA * alpha_f)
        if alpha < 1:
            continue
        col = QColor(VEGA_COLOR if is_vega else STAR_COLOR)
        col.setAlpha(alpha)
        radius = VEGA_RADIUS_PX if is_vega else STAR_RADIUS_PX
        painter.setPen(Qt.NoPen)
        painter.setBrush(col)
        painter.drawEllipse(
            int(x - radius), int(y - radius),
            int(radius * 2), int(radius * 2),
        )
