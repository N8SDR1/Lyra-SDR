// Spectrum trace — fragment shader.
//
// Phase A scope: solid color from a uniform. CPU side sets the
// uniform once at pipeline build (or whenever the operator's color
// pick changes); the per-fragment cost is one uniform read.
//
// Phase B will add an alpha-fade-by-y-position option for the
// gradient-fill effect the QPainter widget has under the trace
// (see SpectrumWidget's drawPolygon with QLinearGradient). That's
// a small extension — same shader, one extra uniform.

#version 330 core

uniform vec4 traceColor;

out vec4 fragColor;

void main()
{
    fragColor = traceColor;
}
