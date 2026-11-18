#version 120

// The pack's options, in the forms options() reads them (8.2).

//#define SHADOWS
#define SHADOW_QUALITY 2 // [1 2 3]
//#define SHADOW_SOFTNESS 1 // [0 1 2]

//#define LIGHTING
#define SUN_PATH_ROTATION 0 // [0 90 180 270]

//#define PERFORMANCE
//#define WAVING_LEAVES
//#define RAIN_PUDDLES

uniform sampler2D colortex0;

varying vec2 texcoord;

void main() {
    gl_Position = ftransform();
    texcoord = (gl_TextureMatrix[0] * gl_MultiTexCoord0).xy;
}
