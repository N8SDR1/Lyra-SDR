// Spectrum trace fill — fragment shader.
//
// Draws the gradient-filled area BELOW the spectrum trace.  Pairs
// with the trace.vert pass-through vertex shader and a triangle-
// strip vertex buffer that has two vertices per bin (the trace Y at
// the top edge of the strip + the viewport bottom Y at the bottom
// edge).  See spectrum_gpu.py's `_build_fill_vertices` for the
// CPU-side geometry.
//
// Visual intent: match the QPainter widget's QLinearGradient fill
// (alpha 100/255 at the TOP of the widget, alpha 10/255 at the
// BOTTOM).  Same single color throughout — only the alpha varies.
//
// gl_FragCoord.y is in screen-pixel space, with origin at the
// BOTTOM of the framebuffer.  CPU side passes `viewportHeight` so
// we can normalize y to [0, 1] independent of widget size.
//
// Operator-controllable:
//   - fillColor (vec4)       — RGB, alpha is multiplied by gradient
//   - viewportHeight (float) — pixels, for gradient normalization

#version 330 core

uniform vec4  fillColor;
uniform float viewportHeight;

out vec4 fragColor;

void main()
{
    // Normalized Y: 0 at viewport bottom, 1 at viewport top.
    float yNorm = clamp(gl_FragCoord.y / max(viewportHeight, 1.0),
                        0.0, 1.0);

    // Alpha gradient — matches the QPainter widget's fill:
    //   top of viewport (yNorm=1)    -> 100/255 ≈ 0.392
    //   bottom of viewport (yNorm=0) -> 10/255  ≈ 0.039
    float alpha = mix(10.0 / 255.0, 100.0 / 255.0, yNorm);

    // fillColor.rgb is operator-picked; multiply final alpha by
    // any alpha the operator passed in too (rarely non-1, but
    // future-proof against translucent picks).
    fragColor = vec4(fillColor.rgb, alpha * fillColor.a);
}
