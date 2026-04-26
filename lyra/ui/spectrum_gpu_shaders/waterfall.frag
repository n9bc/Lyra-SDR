// Waterfall — fragment shader.
//
// Phase A.4 implementation: samples the magnitude texture using a
// rolling-buffer row offset so we never have to copy texture data
// when adding new rows. The CPU side writes new rows into a
// circular position in the texture and bumps `uRowOffset` to point
// at "newest row". This shader handles the wrap-around so the
// visible image always shows newest-at-top regardless of which
// physical texture row is currently newest.
//
// Why this matters: the existing QPainter waterfall does
//   self._data[1:] = self._data[:-1]   # full buffer memcpy
// on EVERY new row — that's ~10 MB of memmove per tick on a typical
// waterfall buffer. The circular-buffer + rowOffset approach moves
// ZERO bytes around. New row = one glTexSubImage2D call covering
// one row's worth of pixels.
//
// Phase B will extend this with a 256-entry palette LUT (Classic /
// Heatmap / etc.) so the operator's palette pick is one texture
// sample per fragment instead of recomputing the whole waterfall.

#version 330 core

in vec2 v_texcoord;     // x: 0..1 left→right, y: 0..1 TOP→bottom

uniform sampler2D waterfallTex;
uniform float uRowOffset;   // physical-row index of NEWEST data
uniform float uRowCount;    // total rows in the texture

out vec4 fragColor;

void main()
{
    // Map widget-y (0=top, 1=bottom) to physical texture row,
    // accounting for the wrap. v_texcoord.y * uRowCount gives the
    // logical "rows from newest" offset; adding uRowOffset and
    // taking mod uRowCount gives the physical row.
    float row = mod(uRowOffset + v_texcoord.y * uRowCount, uRowCount);
    // Sample at the centre of the texel to avoid bleed between rows
    // when GL_LINEAR filtering is on.
    vec2 uv = vec2(v_texcoord.x, (row + 0.5) / uRowCount);
    float v = texture(waterfallTex, uv).r;
    // Phase A.4: grayscale output. Phase B replaces this with a
    // palette LUT lookup (texture(uPaletteTex, vec2(v, 0.5)).rgb).
    fragColor = vec4(v, v, v, 1.0);
}
