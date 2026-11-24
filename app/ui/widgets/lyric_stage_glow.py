"""Local glyph and sprite compositing into the stage's current OpenGL framebuffer."""
from array import array
from collections import OrderedDict
import logging
import math

from PySide6.QtGui import QVector2D, QVector3D, QVector4D
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture

logger = logging.getLogger(__name__)

VERTEX = """
attribute vec2 point;
attribute vec2 sourcePoint;
attribute float visibility;
uniform vec3 projectedX;
uniform vec3 projectedY;
uniform vec3 projectedW;
varying vec2 uv;
varying float alpha;

void main() {
    vec3 p = vec3(point, 1.0);
    gl_Position = vec4(dot(projectedX, p), dot(projectedY, p), 0.0, dot(projectedW, p));
    uv = sourcePoint;
    alpha = visibility;
}
"""

FRAGMENT = """
#ifdef GL_ES
precision highp float;
#endif
uniform sampler2D ink;
uniform sampler2D softInk;
uniform vec2 glyphSize;
uniform vec4 piece;
uniform vec3 firstColor;
uniform vec3 secondColor;
uniform vec4 light;
uniform int material;
varying vec2 uv;
varying float alpha;

float coverage(vec2 point) {
    vec2 inside = step(piece.xy, point) * step(point, piece.xy + piece.zw);
    return texture2D(ink, point / glyphSize).a * inside.x * inside.y;
}

float rim(float stepSize) {
    vec2 delta = vec2(stepSize, 0.0);
    return 0.25 * (coverage(uv + delta.xy) + coverage(uv - delta.xy)
                   + coverage(uv + delta.yx) + coverage(uv - delta.yx));
}

void main() {
    if (material == 3) {
        vec4 sprite = texture2D(ink, uv);
        gl_FragColor = vec4(sprite.rgb * sprite.a, sprite.a) * alpha;
        return;
    }

    float shape = coverage(uv);
    vec2 local = (uv - piece.xy) / piece.zw;
    float tilt = light.y;
    float phase = light.x;
    float energy = light.w;
    float sweep = light.z >= 0.0 ? light.z : 0.5 + 0.68 * sin(phase * 1.28);
    float diagonal = local.x + (0.24 + 0.22 * tilt) * (local.y - 0.52);
    float distance = diagonal - sweep;
    float gloss = exp(-22.0 * distance * distance);
    float streak = exp(-230.0 * (distance + 0.028) * (distance + 0.028));
    float top = 1.0 - smoothstep(0.04, 0.57, local.y);
    float nearInk = rim(1.65);
    float inner = clamp(shape - nearInk, 0.0, 1.0);
    float border = max(0.0, nearInk - shape);
    vec2 edge = min(uv - piece.xy, piece.xy + piece.zw - uv);
    float feather = smoothstep(-7.5, 2.0, min(edge.x, edge.y));
    // Reuse Qt's defocused glyph texture. Its low frequencies prevent distinct
    // offset copies of sharp strokes when the display pixel density is high.
    float bloom = max(0.0, texture2D(softInk, uv / glyphSize).a - shape) * feather;

    vec3 cold = mix(firstColor, vec3(0.91, 0.98, 1.0), 0.52);
    vec3 warm = mix(secondColor, vec3(1.0, 0.95, 0.86), 0.52);
    vec3 face = mix(cold, warm, 0.4 + 0.33 * sin(diagonal * 5.6 + phase * 1.45));
    vec3 halo = mix(firstColor, secondColor, 0.38 + 0.29 * sin(diagonal * 3.8 + phase));
    float strength = 0.51 + 0.32 * gloss + 0.13 * energy;
    if (material == 1) {
        face = mix(vec3(0.53, 0.81, 1.0), vec3(0.94, 1.0, 1.0), 0.4 + 0.55 * top);
        halo = mix(firstColor, vec3(0.40, 0.79, 1.0), 0.69);
        streak *= 0.70;
    } else if (material == 2) {
        float refraction = 0.5 + 0.5 * sin(diagonal * 8.0 - local.y * 3.8 + 4.6 * tilt + phase * 1.08);
        face = mix(cold, warm, refraction);
        face = mix(face, vec3(0.79, 0.95, 1.0), (1.0 - refraction) * gloss * 0.72);
        halo = mix(firstColor, secondColor, refraction);
        strength += 0.08;
    }
    face = mix(face, vec3(1.0, 0.984, 0.946), clamp(0.25 * top + 0.32 * streak + inner * 0.42, 0.0, 0.88));
    float faceAlpha = shape * strength;
    float haloAlpha = (border * (0.67 + 0.30 * energy) + bloom * (0.34 + 0.21 * gloss));
    gl_FragColor = vec4(face * faceAlpha + halo * haloAlpha, faceAlpha + haloAlpha) * alpha;
}
"""

GL_BLEND = 0x0BE2
GL_ONE = 1
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_FLOAT = 0x1406
GL_TRIANGLE_STRIP = 0x0005
GL_TRIANGLES = 0x0004


class GlyphGlow:
    def __init__(self):
        self._attempted = False
        self._program = None
        self._vertices = None
        self._textures = OrderedDict()
        self._locations = {}
        self._attributes = ()

    def _initialize(self):
        self._attempted = True
        program = QOpenGLShaderProgram()
        if not (program.addCacheableShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
                and program.addCacheableShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT)
                and program.link()):
            logger.info("Lyric effects using cached Qt materials: %s", program.log())
            return
        buffer = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        if not buffer.create():
            return
        buffer.setUsagePattern(QOpenGLBuffer.StreamDraw)
        self._program, self._vertices = program, buffer
        names = ("projectedX", "projectedY", "projectedW", "glyphSize",
                 "piece", "firstColor", "secondColor", "light", "material", "ink", "softInk")
        self._locations = {name: program.uniformLocation(name) for name in names}
        self._attributes = tuple((program.attributeLocation(name), count, offset)
                                 for name, count, offset in (("point", 2, 0), ("sourcePoint", 2, 8), ("visibility", 1, 16)))

    def _texture(self, pixmap):
        key = pixmap.cacheKey()
        texture = self._textures.pop(key, None)
        if texture is None:
            texture = QOpenGLTexture(pixmap.toImage(), QOpenGLTexture.DontGenerateMipMaps)
            if not texture.isCreated():
                return None
            texture.setWrapMode(QOpenGLTexture.ClampToEdge)
            texture.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
        self._textures[key] = texture
        # Only current glyphs and tiny sprites are uploaded here. Qt owns its
        # separate cache for the background, cover and unaccented lyric textures.
        if len(self._textures) > 5:
            _, retired = self._textures.popitem(last=False)
            retired.destroy()
        return texture

    def _uniform(self, name, value):
        self._program.setUniformValue(self._locations[name], value)

    def _draw(self, gl, primitive, vertices):
        packet = array('f', vertices).tobytes()
        self._vertices.allocate(packet, len(packet))
        # Positions, then UV in glyph units (text) or normalized (sprite), then alpha.
        for location, count, offset in self._attributes:
            self._program.enableAttributeArray(location)
            self._program.setAttributeBuffer(location, GL_FLOAT, offset, count, 20)
        gl.glDrawArrays(primitive, 0, len(vertices)//5)

    def _patches(self, gl, texture, soft, glyph, pieces, material, sample, opacity):
        self._uniform("material", material)
        self._uniform("glyphSize", QVector2D(glyph.width, glyph.height))
        soft.bind(1, QOpenGLTexture.ResetTextureUnit)
        texture.bind(0)
        for patch in pieces:
            source, dest = patch.source, patch.target
            self._uniform("piece", QVector4D(source.x(), source.y(), source.width(), source.height()))
            progress = -1. if patch.progress is None else patch.progress
            # The line entry already fades this phase in. Keeping it local avoids
            # a phase discontinuity at a minute boundary in a long cue.
            self._uniform("light", QVector4D(sample.age, sample.angle, progress, sample.sound))
            # Carry extra alpha samples through the same projective transform as
            # the actual text. This never shades outside the padded lyric patch.
            pad = 10.
            dx, dy = pad*dest.width()/source.width(), pad*dest.height()/source.height()
            x = (dest.left()-dx, dest.right()+dx)
            y = (dest.top()-dy, dest.bottom()+dy)
            u = (source.left()-pad, source.right()+pad)
            v = (source.top()-pad, source.bottom()+pad)
            vertices = [n for i,j in ((0,0),(1,0),(0,1),(1,1)) for n in (x[i],y[j],u[i],v[j],opacity)]
            self._draw(gl, GL_TRIANGLE_STRIP, vertices)
        texture.release(0)
        soft.release(1, QOpenGLTexture.ResetTextureUnit)

    def _sprites(self, gl, texture, sparks, opacity):
        vertices = []
        for spark in sparks:
            radians = math.radians(spark.rotation)
            sin, cos = math.sin(radians), math.cos(radians)
            corners = []
            for u, v in ((0.,0.), (1.,0.), (0.,1.), (1.,1.)):
                x, y = (u-.5)*spark.width, (v-.5)*spark.height
                corners.append((spark.x + cos*x - sin*y, spark.y + sin*x + cos*y,
                                u, v, opacity*spark.opacity))
            for index in (0,1,2,2,1,3):
                vertices.extend(corners[index])
        self._uniform("material", 3)
        texture.bind(0)
        self._draw(gl, GL_TRIANGLES, vertices)
        texture.release(0)

    def paint(self, p, context, size, glyph, patches, material, sprite, sparks, sample, gain):
        if not patches and not sparks:
            return True
        transform = p.deviceTransform()
        opacity = p.opacity()
        gl = context.functions()
        p.beginNativePainting()
        try:
            if not self._attempted:
                self._initialize()
            if self._program is None:
                return False
            ink = self._texture(glyph.sharp) if patches else None
            soft = self._texture(glyph.soft) if patches else None
            speck = self._texture(sprite) if sparks else None
            if (patches and (ink is None or soft is None)) or (sparks and speck is None):
                return False
            if not self._vertices.bind():
                return False
            if not self._program.bind():
                self._vertices.release()
                return False
            try:
                gl.glEnable(GL_BLEND)
                gl.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
                sx, sy = 2./size.width(), 2./size.height()
                w = QVector3D(transform.m13(),transform.m23(),transform.m33())
                x = QVector3D(transform.m11(),transform.m21(),transform.m31())
                y = QVector3D(transform.m12(),transform.m22(),transform.m32())
                self._uniform("projectedX", x*sx-w)
                self._uniform("projectedY", w-y*sy)
                self._uniform("projectedW", w)
                self._uniform("ink", 0)
                self._uniform("softInk", 1)
                for name, color in zip(("firstColor", "secondColor"), sample.colors):
                    self._uniform(name, QVector3D(color.redF(),color.greenF(),color.blueF()))
                if ink is not None:
                    self._patches(gl, ink, soft, glyph, patches, material, sample, opacity*gain)
                if speck is not None:
                    self._sprites(gl, speck, sparks, opacity)
            finally:
                for location, _, _ in self._attributes:
                    self._program.disableAttributeArray(location)
                self._vertices.release()
                self._program.release()
            return True
        finally:
            p.endNativePainting()

    def release(self):
        for texture in self._textures.values():
            texture.destroy()
        self._textures.clear()
        if self._vertices is not None:
            self._vertices.destroy()
        self._program = self._vertices = None
