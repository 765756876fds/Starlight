"""Glyph-local materials and bounded accents on the existing lyric clock."""
from collections import OrderedDict, deque
from dataclasses import dataclass
from random import Random
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient

from .lyric_stage_art import clamp, smooth, tint

TEXT_EFFECTS = (
    ("starlight", "星芒流光"),
    ("dust", "星尘随行"),
    ("ice", "冰晶飞雪"),
    ("prism", "幻彩折射"),
    ("trail", "光迹残影"),
    ("off", "关闭特效"),
)
DEFAULT_EFFECT = TEXT_EFFECTS[0][0]
MATERIALS = {"starlight": 0, "ice": 1, "prism": 2}
EMISSION = {"starlight": (.172, .81), "dust": (.105, .81), "ice": (.089, .94)}


@dataclass(frozen=True)
class InkPatch:
    source: QRectF
    target: QRectF
    progress: float | None


@dataclass(frozen=True)
class EffectSample:
    row: int
    position: float
    age: float
    remaining: float
    colors: tuple[QColor, QColor]
    angle: float
    sound: float
    motion: float
    strength: float
    low_power: bool


@dataclass(frozen=True)
class Spark:
    x: float
    y: float
    width: float
    height: float
    rotation: float
    opacity: float


def _variation():
    rng = Random(613)
    return tuple(tuple(rng.random() for _ in range(5)) for _ in range(128))


VARIATION = _variation()


def _sprite(color, ice):
    pixmap = QPixmap(72, 72)
    pixmap.fill(Qt.transparent)
    with QPainter(pixmap) as p:
        p.setRenderHint(QPainter.Antialiasing)
        center = QPointF(36, 36)
        glow = QRadialGradient(center, 35)
        for at, amount in ((0., .85), (.24, .44), (.52, .13), (1., 0.)):
            glow.setColorAt(at, tint(color, amount))
        p.fillRect(pixmap.rect(), glow)
        p.translate(center)
        if ice:
            p.setPen(QPen(QColor("#dcf7ff"), 1.75, Qt.SolidLine, Qt.RoundCap))
            for _ in range(6):
                p.drawLine(QPointF(), QPointF(0, -23))
                p.drawLine(QPointF(0, -15), QPointF(-6, -20))
                p.drawLine(QPointF(0, -15), QPointF(6, -20))
                p.rotate(60)
        else:
            glint = QLinearGradient(-28, 0, 23, 0)
            glint.setColorAt(0, tint(color, 0.))
            glint.setColorAt(.46, tint(color, .54))
            glint.setColorAt(.6, QColor("#fff8ec"))
            glint.setColorAt(1, tint(color, 0.))
            p.fillRect(QRectF(-28, -2, 51, 4), glint)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#fff8ec"))
            p.drawEllipse(QPointF(2, 0), 4.3, 4.3)
    return pixmap


class LyricEffects:
    def __init__(self):
        self._palette = None
        self._sprites = {}
        self._glazes = OrderedDict()
        self._trace_key = None
        self._traces = deque(maxlen=4)

    def clear_trail(self):
        self._trace_key = None
        self._traces.clear()

    def clear_colors(self):
        self._sprites.clear()
        self._glazes.clear()

    def _sparks(self, effect, glyph, cues, sample, gain):
        if not sample.motion:
            return ()
        ice = effect == "ice"
        interval, lifetime = EMISSION[effect]
        if sample.low_power:
            interval *= 1.8
        count = math.ceil(lifetime / interval)
        age, sound, motion = sample.age, sample.sound, sample.motion
        tick = math.floor(age / interval)
        sparks = []
        for index in range(max(0, tick-count+1), tick+1):
            progress = (age - index * interval) / lifetime
            if not 0. <= progress < 1.:
                continue
            r, s, t, u, v = VARIATION[(sample.row*53 + index*31) % len(VARIATION)]
            born = sample.position - progress*lifetime
            anchors = glyph.outline
            if cues:
                # Choose the contour that was singing at emission, even if the next
                # syllable has since started. Leave genuine gaps between cues dark.
                word = next((i for i in range(len(cues)-1, -1, -1) if cues[i].start <= born), -1)
                if word < 0:
                    continue
                cue = cues[word]
                end = cue.end if cue.end is not None else cue.start + .14
                if born >= end:
                    continue
                anchors = glyph.word_outline[word]
                if cue.end is not None:
                    amount = (born - cue.start) / max(.001, cue.end - cue.start)
                    r = clamp(.72*amount + .28*r, 0., .999)
            if not anchors:
                continue
            x, y, edge = anchors[int(r*len(anchors))]
            vx = (s-.5) * (34 + 28*sound)
            vy = edge * (15 + 26*t + 16*sound)
            x += vx*progress*motion + math.sin(progress*3.4 + u*5.) * progress * (9 if ice else 5)
            y += edge*3. + vy * (1 - (1-progress)**2) * motion
            if ice:
                y += 10*progress**2
            fade = smooth(progress/.16) * (1 - smooth((progress-.24)/.76))
            width = (15 + 11*v + 8*sound) * (.8 + .2*motion)
            height = width if ice else width * (.48 + .22*t)
            rotation = -60 + u*180 + progress*53 if ice else math.degrees(math.atan2(vy, vx))
            sparks.append(Spark(x, y, width, height, rotation, fade*gain*(.44 + .42*sound + .12*v)))
        return sparks

    def _glaze(self, glyph, effect, colors):
        key = (glyph.sharp.cacheKey(), effect)
        hit = self._glazes.pop(key, None)
        if hit is None:
            hit = glyph.sharp.copy()
            with QPainter(hit) as ink:
                ink.setCompositionMode(QPainter.CompositionMode_SourceIn)
                gradient = QLinearGradient(0., 0., hit.width()*.34, hit.height())
                gradient.setColorAt(0., QColor("#fff9ee"))
                gradient.setColorAt(.42, colors[1].lighter(152) if effect == "prism" else QColor("#f5fdff"))
                gradient.setColorAt(.78, QColor("#8cddfd") if effect == "ice" else colors[0].lighter(157))
                gradient.setColorAt(1., QColor("#f3f9ff"))
                ink.fillRect(hit.rect(), gradient)
        self._glazes[key] = hit
        if len(self._glazes) > 4:
            self._glazes.popitem(last=False)
        return hit

    @staticmethod
    def _source(pixmap, rect, glyph):
        sx, sy = pixmap.width()/glyph.width, pixmap.height()/glyph.height
        return QRectF(rect.x()*sx, rect.y()*sy, rect.width()*sx, rect.height()*sy)

    def _trail(self, p, glyph, patches, sample, gain):
        position = sample.position
        transform = p.worldTransform()
        opacity = p.opacity()
        # Real projected poses, sampled from this cue only. Keep at most 180 ms of
        # history and never integrate across a seek or replay of the same line.
        if self._traces and not 0. <= position-self._traces[-1][0] < .22:
            self._traces.clear()
        while self._traces and position-self._traces[0][0] >= .18:
            self._traces.popleft()
        if not self._traces or position-self._traces[-1][0] > .051:
            self._traces.append((position, transform, patches))
        pixmap = glyph.lit if glyph.word_boxes else glyph.soft
        p.save()
        for at, past, pieces in self._traces:
            age = position-at
            if age < .028:
                continue
            p.setWorldTransform(past)
            p.setOpacity(opacity * gain * sample.motion * (.30 + .36*sample.sound) * (1.-age/.18)**1.4)
            for piece in pieces:
                p.drawPixmap(piece.target, pixmap, self._source(pixmap, piece.source, glyph))
        p.restore()

    def draw(self, p, surface, effect, glyph, patches, cues, sample):
        key = (effect, glyph.sharp.cacheKey())
        if self._trace_key != key:
            self._traces.clear()
            self._trace_key = key
        gain = sample.strength * smooth(sample.age/.21) * smooth(sample.remaining/.3)
        if gain <= .009:
            return
        if effect == "trail":
            if sample.motion:
                self._trail(p, glyph, patches, sample, gain)
            return
        colors = sample.colors
        palette = tuple(c.rgb() for c in colors)
        if self._palette != palette:
            self._palette = palette
            self.clear_colors()
        material = MATERIALS.get(effect)
        sparks = ()
        sprite = None
        if effect in EMISSION:
            sparks = self._sparks(effect, glyph, cues, sample, gain)
            if sparks:
                ice = effect == "ice"
                if ice not in self._sprites:
                    self._sprites[ice] = _sprite(QColor("#8dd7fc") if ice else colors[1].lighter(142), ice)
                sprite = self._sprites[ice]
        if surface is not None and surface.paint_glyph_effect(
                p, glyph, patches if material is not None else (), material,
                sprite, sparks, sample, gain):
            return

        # Keep an inexpensive, shaped glaze and the same local sprites when native
        # shaders are unavailable. Reduced motion bypasses this layer entirely.
        p.save()
        opacity = p.opacity()
        if patches and material is not None:
            glaze = self._glaze(glyph, effect, colors)
            for piece in patches:
                p.setOpacity(opacity*gain*.62)
                p.drawPixmap(piece.target, glaze, self._source(glaze, piece.source, glyph))
        for spark in sparks:
            p.save()
            p.translate(spark.x, spark.y)
            p.rotate(spark.rotation)
            p.setOpacity(opacity*spark.opacity)
            p.drawPixmap(QRectF(-spark.width/2, -spark.height/2, spark.width, spark.height),
                         sprite, QRectF(sprite.rect()))
            p.restore()
        p.restore()
