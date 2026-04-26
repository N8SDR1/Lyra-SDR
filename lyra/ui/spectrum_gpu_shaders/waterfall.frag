// Waterfall — fragment shader.
//
// Phase A scope: sample the magnitude texture, output grayscale.
// Confirms the texture upload + sample path works end-to-end.
//
// Phase B will replace the grayscale output with a 256-entry 1D LUT
// for the palette (Classic / Heatmap / etc.) so palette swap is one
// texture update, not a re-render of the whole waterfall buffer.
//
// Phase B will also handle the rolling buffer: the CPU writes new
// rows to a circular position in the texture, and a `rowOffset`
// uniform tells this shader where "row 0" currently lives. We
// sample with `mod(v_texcoord.y - rowOffset, 1.0)` so the visible
// image always shows newest-at-top without ever copying texture
// data. That's the surgical fix for the np.roll-style scroll cost
// in the existing QPainter waterfall (the Phase 1a perf work
// fingered as a major bottleneck candidate).

#version 330 core

in vec2 v_texcoord;

uniform sampler2D waterfallTex;

out vec4 fragColor;

void main()
{
    float v = texture(waterfallTex, v_texcoord).r;
    fragColor = vec4(v, v, v, 1.0);
}
