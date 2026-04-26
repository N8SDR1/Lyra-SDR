// Waterfall — vertex shader for the fullscreen quad.
//
// The waterfall widget covers its rect with a single textured quad
// (two triangles). The texture itself holds the rolling image of
// past spectrum rows; CPU code writes new rows into a circular
// position in the texture and passes a `row_offset` uniform telling
// the fragment shader where "row 0" currently lives. No buffer
// scroll, no Python list shuffles — that whole class of bug from
// the existing QPainter waterfall vanishes.
//
// Phase A scope: pass the position straight through to clip space,
// hand off the texcoord. The quad geometry is built once CPU-side
// and never changes, so the vertex buffer is a Static (write-once)
// buffer rather than the trace's Dynamic one.

#version 330 core

layout(location = 0) in vec2 position;
layout(location = 1) in vec2 texcoord;

out vec2 v_texcoord;

void main()
{
    gl_Position = vec4(position, 0.0, 1.0);
    v_texcoord = texcoord;
}
