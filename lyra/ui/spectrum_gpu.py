"""GPU-accelerated panadapter — Phase A skeleton.

This module is **opt-in** and parallel to `spectrum.py`. The existing
QPainter-based `SpectrumWidget` / `WaterfallWidget` remain the default
production renderers. `SpectrumGpuWidget` is a from-scratch
implementation built directly on Qt's QOpenGLWidget, which gives us
GPU-accelerated rendering through the platform's OpenGL driver
(NVIDIA, AMD, Intel HD/Iris/Arc — all supported).

Design rationale
----------------
We chose QOpenGLWidget over QRhi/Vulkan because:

  - QRhi PySide6 bindings (Qt 6.7+) are still very new — initial
    Phase A.3 attempts hit deep crashes inside Qt's D3D11 backend
    that aren't easily debuggable from Python. See the parked
    `feature/qrhi-panadapter` branch (tag: experiment-qrhi-attempt)
    for the journey.
  - QOpenGLWidget has been in PySide6 for years, has hundreds of
    working examples, and is the path most Qt+Python apps take
    when they need GPU rendering.
  - On Win10/11 with modern GPU drivers, OpenGL Just Works™. If a
    machine's native OpenGL is broken, Qt automatically falls back
    to ANGLE (which translates to D3D11), so we get D3D coverage
    indirectly without writing D3D code.
  - macOS uses Apple's OpenGL implementation (deprecated but still
    functional). Long-term Mac support would migrate to Metal — that's
    a separate project, not a v0.0.5/0.0.6 concern.
  - Vulkan can be revisited later via QRhi if/when PySide6 bindings
    mature, OR if a real performance need arises that OpenGL can't
    handle. Today neither is true.

The Settings → Visuals → Graphics backend combo will gain a third
choice ("OpenGL — GPU-accelerated panadapter"), with the existing
"Software (QPainter)" remaining as the unconditional fallback.
Vulkan stays in the combo as "(future)" — greyed out but visible —
so the operator-facing UI hook is preserved if we ever revisit.

Phase A scope (this file's progress)
------------------------------------
A.2: widget skeleton with shader compile + clear. Proven on the dev
machine.

A.3 (THIS COMMIT): vertex-buffer trace draw with synthetic data.
  - Dynamic VBO sized for MAX_BINS vec2 points
  - VAO that bundles the vertex attribute state (required in 3.3+
    core profile — no default VAO like the legacy compatibility
    profile had)
  - paintGL: bind program + VAO + VBO, upload current trace data
    via QOpenGLBuffer.write(), draw with GL_LINE_STRIP — ONE call
    per frame instead of the per-pixel drawLine loop the QPainter
    widget does
  - Built-in animated sine-wave generator runs until set_spectrum()
    is called, so the widget is self-testing without a Radio
    attached. The standalone demo (Phase A.5) will use the public
    set_spectrum() API; same path Lyra's panel system will use in
    Phase B.
  - 30 Hz repaint timer drives the animation while in synthetic
    mode. Disabled when set_spectrum() takes over.

A.4 will add: streaming-texture waterfall + working draw call
A.5 will add: standalone demo runner (separate file)
A.6 will add: external profile pass

Phase B will integrate into Lyra (Settings UI, real Radio data).
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFunctions_4_3_Core, QOpenGLShader,
    QOpenGLShaderProgram, QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


# Background color for the panadapter (RGB normalized 0..1) — matches
# the QPainter widget's `BG = QColor(12, 20, 32)` so visuals stay
# continuous when the operator switches renderers in Settings.
_BG_R, _BG_G, _BG_B = 12 / 255.0, 20 / 255.0, 32 / 255.0

# Default trace color — Lyra's TRACE QColor(94, 200, 255) normalized.
_DEFAULT_TRACE = (94 / 255.0, 200 / 255.0, 255 / 255.0, 1.0)

# Vertex buffer capacity in number of POINTS (not bytes). Each point
# is a vec2 (8 bytes). 8192 covers any FFT size Lyra uses today and
# leaves headroom for future increases. Allocated once at
# initializeGL() time — reallocating GL buffers is expensive.
MAX_BINS = 8192
BYTES_PER_POINT = 8  # vec2 = 2 × float32

# OpenGL constants we use directly (matches the GL spec values exactly
# so they're stable across drivers / Qt versions). Imported here once
# rather than scattered throughout paintGL bodies.
GL_COLOR_BUFFER_BIT = 0x4000
GL_LINE_STRIP       = 0x0003

# Where the GLSL source files live, relative to this module.
_SHADER_DIR = Path(__file__).resolve().parent / "spectrum_gpu_shaders"


def lyra_gl_format() -> QSurfaceFormat:
    """Return the QSurfaceFormat all Lyra OpenGL widgets should use.

    Centralized so the widget itself, the demo runner (Phase A.5),
    and the validation script all request the same context profile
    and version. Caller is responsible for setting this on the
    widget BEFORE first show — once a context is created with one
    format, changing the format requires recreating the widget.

    OpenGL 4.3 core profile — covers every Win10/11 GPU since 2013.
    Adds compute shaders + debug output + SSBOs as future-feature
    options. Individual shader sources can stay at #version 330
    core unless they need newer GLSL features.
    """
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    fmt.setSamples(0)
    fmt.setSwapInterval(1)  # vsync on
    return fmt


class SpectrumGpuWidget(QOpenGLWidget):
    """GPU-rendered spectrum + (eventually) waterfall panadapter.

    Phase A.3 state: draws a self-generated synthetic sine-wave
    trace using one draw call per frame against a dynamic vertex
    buffer. The trace animates so successful operation is obvious
    visually. Switches to operator-supplied data on first
    set_spectrum() call.

    Public API:
        set_spectrum(spec_db, min_db=-130, max_db=-30)
            Upload one frame of spectrum data. dB → NDC.y mapping
            uses min_db/max_db as the scale window. Bins below
            min_db render at the bottom of the widget; bins above
            max_db at the top.
        set_trace_color(QColor)
            Set the trace line color. Applied via the trace.frag
            `traceColor` uniform on the next paint.
    """

    # Synthetic-data point count — mimics Lyra's typical FFT size
    # (4096) so the test exercises the same draw cost as real usage.
    _SYNTHETIC_N = 4096

    # How often to repaint while in synthetic-data mode. 30 Hz is a
    # comfortable visual rate that exercises the upload+draw cycle
    # without burning CPU on a passive demo.
    _SYNTHETIC_HZ = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        # Per-widget format (vs setting the global default) keeps the
        # GL context choice local to this widget tree.
        self.setFormat(lyra_gl_format())

        # GL function table — bound to the active context in
        # initializeGL once the context is current. Native 4.3 core
        # access without depending on PyOpenGL.
        self._gl: Optional[QOpenGLFunctions_4_3_Core] = None

        # GPU-side handles. All None until initializeGL runs; cleaned
        # up automatically by QObject parent ownership when the widget
        # is destroyed.
        self._prog_trace: Optional[QOpenGLShaderProgram] = None
        self._vbo_trace: Optional[QOpenGLBuffer] = None
        self._vao_trace: Optional[QOpenGLVertexArrayObject] = None
        # Cached attribute / uniform locations resolved once at link
        # time. -1 means "not found" (defensive — shouldn't happen
        # if the shader compiled correctly).
        self._loc_position: int = -1
        self._loc_trace_color: int = -1

        # CPU-side trace data. Pre-allocated to MAX_BINS so paintGL
        # never allocates. Shape (N, 2) float32; column 0 = NDC.x,
        # column 1 = NDC.y. _trace_n is the number of valid points
        # currently in the buffer prefix.
        self._trace_xy = np.zeros((MAX_BINS, 2), dtype=np.float32)
        self._trace_n = 0

        # Trace color — operator-overridable via set_trace_color.
        self._trace_color: tuple[float, float, float, float] = _DEFAULT_TRACE

        # Synthetic-data animation state. _synthetic_active toggles
        # OFF the moment set_spectrum() is called (real data takes
        # over). Until then, paintGL regenerates the test sine wave
        # each frame so motion is visible.
        self._synthetic_active = True
        self._t0 = time.monotonic()

        # Drives synthetic-mode animation. Real data path doesn't
        # need this — set_spectrum's caller (Radio in Phase B) will
        # request repaints via update() at FFT rate.
        self._synth_timer = QTimer(self)
        self._synth_timer.setInterval(int(1000 / self._SYNTHETIC_HZ))
        self._synth_timer.timeout.connect(self.update)
        self._synth_timer.start()

    # ── Public data API ────────────────────────────────────────────

    def set_spectrum(self, spec_db: np.ndarray,
                     min_db: float = -130.0,
                     max_db: float = -30.0) -> None:
        """Upload a frame of spectrum data for the next render.

        spec_db: 1-D numpy array of dB values (one per FFT bin).
        min_db/max_db: scale window — bins below min_db render at
            the bottom of the widget, bins above max_db at the top.

        Stops the synthetic animation timer on first call (operator
        data takes over). The caller is responsible for triggering
        subsequent repaints via the widget's update() — typically
        Radio.spectrum_ready will already be doing that at FFT rate.
        """
        n = int(min(spec_db.shape[0], MAX_BINS))
        if n < 2:
            return
        # Map bins → NDC.x: linear from -1 (left) to +1 (right).
        xs = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        # Map dB → NDC.y: linear from -1 (bottom = min_db) to +1
        # (top = max_db). Clipped so out-of-range values stick to
        # the edges instead of going off-screen.
        span = max(1e-6, max_db - min_db)
        ys = ((spec_db.astype(np.float32) - min_db) / span) * 2.0 - 1.0
        np.clip(ys, -1.0, 1.0, out=ys)
        self._trace_xy[:n, 0] = xs
        self._trace_xy[:n, 1] = ys
        self._trace_n = n
        # Real data takes over — disable synthetic generator.
        if self._synthetic_active:
            self._synthetic_active = False
            self._synth_timer.stop()
        self.update()

    def set_trace_color(self, color: QColor) -> None:
        """Set the trace line color.

        Applied via the trace.frag `traceColor` uniform on the next
        paint. Stored as float tuple for cheap upload.
        """
        self._trace_color = (
            color.redF(), color.greenF(), color.blueF(), color.alphaF(),
        )
        self.update()

    # ── QOpenGLWidget virtual method overrides ─────────────────────

    def initializeGL(self) -> None:
        """Called by Qt once after the OpenGL context becomes current.

        This is where we build all GPU-side resources: shader
        programs, vertex buffers, vertex array objects. Fires once
        at first show; if the widget is reparented to a different
        top-level window with a different GL context, Qt MAY call
        this again — be idempotent (drop and rebuild).
        """
        # GL function table for the current context.
        self._gl = QOpenGLFunctions_4_3_Core()
        self._gl.initializeOpenGLFunctions()

        # ── Shader program ────────────────────────────────────────
        # If initializeGL fires again, drop any prior program first.
        if self._prog_trace is not None:
            self._prog_trace.removeAllShaders()
            self._prog_trace.deleteLater()
            self._prog_trace = None
        prog = QOpenGLShaderProgram(self)
        ok = (prog.addShaderFromSourceFile(
                  QOpenGLShader.ShaderTypeBit.Vertex,
                  str(_SHADER_DIR / "trace.vert"))
              and prog.addShaderFromSourceFile(
                  QOpenGLShader.ShaderTypeBit.Fragment,
                  str(_SHADER_DIR / "trace.frag")))
        if not ok:
            raise RuntimeError(
                "Trace shader compile failed:\n" + prog.log())
        if not prog.link():
            raise RuntimeError(
                "Trace shader link failed:\n" + prog.log())
        self._prog_trace = prog
        # Cache locations so paintGL doesn't have to resolve them
        # by string lookup every frame.
        self._loc_position    = prog.attributeLocation("position")
        self._loc_trace_color = prog.uniformLocation("traceColor")

        # ── Vertex Buffer Object (VBO) ─────────────────────────────
        # Dynamic = will be re-uploaded every frame. Allocated to
        # MAX_BINS × 8 bytes; we'll only write/draw the prefix in use.
        if self._vbo_trace is not None:
            self._vbo_trace.destroy()
        self._vbo_trace = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._vbo_trace.setUsagePattern(QOpenGLBuffer.UsagePattern.DynamicDraw)
        self._vbo_trace.create()
        self._vbo_trace.bind()
        self._vbo_trace.allocate(MAX_BINS * BYTES_PER_POINT)

        # ── Vertex Array Object (VAO) ──────────────────────────────
        # OpenGL 3.3+ core profile requires an explicit VAO — there's
        # no default one like in the compatibility profile. The VAO
        # bundles together: which buffer is bound to which attribute,
        # what the attribute layout is. Bind it once here; bind it
        # again in paintGL before drawing.
        if self._vao_trace is not None:
            self._vao_trace.destroy()
        self._vao_trace = QOpenGLVertexArrayObject(self)
        self._vao_trace.create()
        self._vao_trace.bind()

        # Hook the position attribute up to the bound VBO. Format:
        #   location 0 (matches `layout(location = 0)` in trace.vert)
        #   2 floats per vertex (vec2)
        #   stride = 8 bytes (one vec2)
        #   offset = 0 (position starts at byte 0)
        prog.bind()
        prog.enableAttributeArray(self._loc_position)
        prog.setAttributeBuffer(
            self._loc_position,
            0x1406,            # GL_FLOAT
            0,                 # offset
            2,                 # tupleSize (vec2)
            BYTES_PER_POINT,   # stride
        )
        prog.release()

        # Release VAO so other code doesn't accidentally modify it.
        self._vao_trace.release()
        self._vbo_trace.release()

    def resizeGL(self, w: int, h: int) -> None:
        """Called by Qt when the widget is resized.

        QOpenGLWidget already updates the GL viewport for us before
        this is invoked. We only need to override if we maintain
        view/projection matrices that depend on aspect ratio. Phase
        A trace path uses NDC throughout, so this is a hook for
        Phase B (overlays / axis labels).
        """
        pass

    def paintGL(self) -> None:
        """Called by Qt per frame to draw.

        QOpenGLWidget binds the framebuffer for us before this fires
        and swaps the buffer after we return — we only need to issue
        actual GL draw calls.

        Phase A.3 work:
          1. Optionally regenerate synthetic test data (if no real
             frames have arrived via set_spectrum yet)
          2. Upload current trace vertices to the dynamic VBO via
             QOpenGLBuffer.write
          3. Bind shader program + VAO, set the traceColor uniform,
             issue ONE glDrawArrays call with GL_LINE_STRIP topology
             — the GPU rasterizes the whole connected line in one
             shot, no per-segment Python overhead

        On a 1500-pixel-wide trace this replaces ~1500 individual
        QPainter drawLine calls with one GL draw call. That's the
        core architectural win.
        """
        if self._gl is None or self._prog_trace is None:
            return

        # ── Phase A test data ─────────────────────────────────────
        # Synthetic moving sine wave runs until set_spectrum is
        # called. _synth_timer drives the repaints.
        if self._synthetic_active:
            self._generate_synthetic()

        # ── Clear ─────────────────────────────────────────────────
        self._gl.glClearColor(_BG_R, _BG_G, _BG_B, 1.0)
        self._gl.glClear(GL_COLOR_BUFFER_BIT)

        n = self._trace_n
        if n < 2:
            return  # nothing to draw yet

        # ── Upload current vertex data ────────────────────────────
        # QOpenGLBuffer.write takes (offset, data, count_in_bytes).
        # Slice the prefix actually in use; .tobytes() copies into a
        # contiguous bytes object for the upload.
        self._vbo_trace.bind()
        self._vbo_trace.write(0, self._trace_xy[:n].tobytes(),
                              n * BYTES_PER_POINT)

        # ── Draw ──────────────────────────────────────────────────
        self._prog_trace.bind()
        # Set trace color uniform (cheap — 4 floats per frame).
        if self._loc_trace_color >= 0:
            r, g, b, a = self._trace_color
            self._prog_trace.setUniformValue(
                self._loc_trace_color, r, g, b, a)
        self._vao_trace.bind()
        self._gl.glDrawArrays(GL_LINE_STRIP, 0, n)
        self._vao_trace.release()
        self._prog_trace.release()
        self._vbo_trace.release()

    # ── Internal: synthetic data generator (Phase A test only) ─────
    def _generate_synthetic(self) -> None:
        """Fill the trace buffer with a time-varying sine wave so we
        can visually confirm the GPU upload+draw cycle is firing on
        every frame. Removed in Phase B once set_spectrum is being
        driven by a real source.
        """
        n = self._SYNTHETIC_N
        xs = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        # 3 cycles + a slow time-based phase offset so the wave
        # visibly scrolls left over time. Amplitude 0.7 keeps the
        # trace inside the widget without touching the edges.
        t = time.monotonic() - self._t0
        ys = (np.sin(xs * math.pi * 6.0 - t * 2.0) * 0.7).astype(np.float32)
        self._trace_xy[:n, 0] = xs
        self._trace_xy[:n, 1] = ys
        self._trace_n = n
