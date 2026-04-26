// Spectrum trace — vertex shader.
//
// Phase A scope:
//   Single attribute: vertex position in clip space (NDC, -1..+1 on
//   both axes). CPU-side code maps bin index → NDC.x and dB value →
//   NDC.y, writing the result into the vertex buffer once per frame.
//
// Phase B may move the bin→NDC math to the GPU via a uniform block
// carrying the dB-range / zoom state. Doing that lets us upload the
// raw spec_db values once and let the vertex shader handle the
// transformation, which means the dB range can change without
// re-uploading vertex data. Not necessary for the initial GPU win.
//
// GLSL 330 core targets desktop OpenGL 3.3+ — covers every Win10/11
// machine with even a 2010-era GPU. macOS Big Sur+ and modern Linux
// also fine. Programmable pipeline only — no fixed-function fallback.

#version 330 core

layout(location = 0) in vec2 position;

void main()
{
    gl_Position = vec4(position, 0.0, 1.0);
}
