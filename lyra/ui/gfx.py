"""Graphics backend selector for the painted widgets.

Controls how the panadapter trace + waterfall get drawn. Four
operator-facing choices, each with progressively more GPU involvement:

- **Software (QPainter on CPU)** — the existing `SpectrumWidget` /
  `WaterfallWidget` rendered into a plain `QWidget`. CPU rasterizes
  every line + pixel. Always works. Slowest but most compatible.
- **OpenGL — accelerated QPainter** — same widgets, but their base
  class becomes `QOpenGLWidget`. Same `QPainter` calls, but
  rasterization and compositing happen on the GPU. Smoother resize
  / fullscreen, reduces audio stutter on weaker CPUs.
- **GPU panadapter (beta)** — NEW, switches to the from-scratch
  `SpectrumGpuWidget` / `WaterfallGpuWidget` (Phase A work — see
  `lyra/ui/spectrum_gpu.py`). Vertex-buffer trace + texture-streaming
  waterfall, both via custom GLSL shaders against an OpenGL 4.3 core
  context. Fastest path; missing some QPainter overlays for now
  (notches, spots, band plan, peak markers) — those land in
  successive commits without breaking the opt-out.
- **Vulkan (future)** — placeholder, greyed out. Reserved in case
  PySide6's QRhi bindings mature enough to make it worth the work,
  or a real performance need surfaces that OpenGL can't satisfy.

**Read-at-import**: backend is resolved *once* at module-load time
by reading the user's Visuals → Graphics backend preference from
QSettings. Changing the setting therefore requires restarting Lyra,
which is clearly stated in the Visuals tab UI. We take this tradeoff
so per-widget creation logic stays simple.

**Fallback chain**: if the requested backend fails to initialize at
load time (PySide6 missing `QtOpenGLWidgets`, no GL driver,
`SpectrumGpuWidget` import error, etc.), we silently fall back to
software and expose the resolved backend via `ACTIVE_BACKEND` so
Settings can show what's actually in use.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QWidget

# Canonical backend identifiers used in QSettings and the UI combo.
BACKEND_SOFTWARE   = "software"
BACKEND_OPENGL     = "opengl"
BACKEND_GPU_OPENGL = "gpu_opengl"
BACKEND_VULKAN     = "vulkan"

BACKEND_LABELS = {
    BACKEND_SOFTWARE:   "Software (QPainter on CPU)",
    BACKEND_OPENGL:     "OpenGL — accelerated QPainter",
    BACKEND_GPU_OPENGL: "GPU panadapter (beta — opt-in)",
    BACKEND_VULKAN:     "Vulkan (future, not implemented)",
}


def _pick_base():
    """Return (base_class, active_backend_id). Reads QSettings, tries
    to import the requested backend, falls back to QWidget on any
    failure.

    The returned `base_class` is what the QPainter widgets in
    spectrum.py inherit from. For BACKEND_GPU_OPENGL the QPainter
    widgets aren't used at all — panels.py instantiates the
    SpectrumGpuWidget / WaterfallGpuWidget classes from spectrum_gpu.py
    directly. For that case we still return a sane base_class
    (QWidget) so any code that imports ACCELERATED_BASE keeps working.
    """
    choice = str(QSettings("N8SDR", "Lyra").value(
        "visuals/graphics_backend", BACKEND_SOFTWARE)).lower()

    if choice == BACKEND_GPU_OPENGL:
        # Verify the GPU widget module is actually importable. If
        # something is broken (missing QOpenGLWidget, GLSL compile
        # failure at import, etc.) we degrade quietly to software so
        # the operator's Lyra still launches.
        try:
            # Don't actually instantiate — just confirm the import
            # works. Real instantiation happens in panels.py.
            from lyra.ui import spectrum_gpu  # noqa: F401
            from PySide6.QtOpenGLWidgets import QOpenGLWidget  # noqa: F401
            # Return QWidget as the QPainter base since we won't be
            # using QPainter widgets at all in this mode.
            return QWidget, BACKEND_GPU_OPENGL
        except ImportError:
            pass

    if choice == BACKEND_OPENGL:
        try:
            from PySide6.QtOpenGLWidgets import QOpenGLWidget
            # Verify we can actually construct one — some headless/
            # test environments have the class but no GL context.
            return QOpenGLWidget, BACKEND_OPENGL
        except ImportError:
            # QtOpenGLWidgets missing — fall through to software.
            pass

    # Vulkan path intentionally not wired. Always software fallback.
    return QWidget, BACKEND_SOFTWARE


ACCELERATED_BASE, ACTIVE_BACKEND = _pick_base()


def is_gpu_panadapter_active() -> bool:
    """Convenience predicate for panels.py — True iff the operator
    selected the BACKEND_GPU_OPENGL renderer AND it loaded successfully.
    Use this rather than comparing ACTIVE_BACKEND directly so the
    intent of the call site is obvious."""
    return ACTIVE_BACKEND == BACKEND_GPU_OPENGL
