"""Cached glyphs, perspective geometry and album light fields for the lyric stage."""
from dataclasses import dataclass
from functools import cached_property
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QImage, QLinearGradient, QPainter, QPixmap,
    QPolygonF, QRadialGradient, QTextLayout, QTextOption,
)

W, H = 1200., 574.
CENTER = QPointF(W / 2, H / 2)
WHITE = QColor("#f3f9ff")
FIELD_W, FIELD_H = 460, 220


def clamp(value, lower=0., upper=1.):
    return min(upper, max(lower, value))


def mix(start, end, amount):
    return start + (end - start) * amount


def smooth(value):
    value = clamp(value)
    return value * value * (3. - 2. * value)


def layout_font(size, weight=QFont.DemiBold, family="Microsoft YaHei UI"):
    font = QFont()
    font.setFamilies([family, "Microsoft YaHei UI", "Microsoft YaHei"])
    font.setPixelSize(round(size))
    font.setWeight(weight)
    return font


def tint(color, opacity):
    color = QColor(color)
    color.setAlphaF(min(1., max(0., opacity)))
    return color


def wash(painter, rect, center, radius, color, strength):
    field = QRadialGradient(center, radius)
    for at, weight in ((0., 1.), (.32, .65), (.66, .16), (1., 0.)):
        field.setColorAt(at, tint(color, strength * weight))
    painter.fillRect(rect, field)


def extract_palette(image):
    """Pick two distinct hues from saturation-weighted cover pixel samples."""
    sample = image.scaled(54, 54, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    bins = [0.] * 24
    for y in range(sample.height()):
        for x in range(sample.width()):
            hue, saturation, value, _ = sample.pixelColor(x, y).getHsvF()
            if hue >= 0. and saturation > .18 and value > .22:
                bins[round(hue * len(bins)) % len(bins)] += saturation * value ** 1.7
    if max(bins) == 0:
        return QColor("#b0c9d3"), QColor("#aaa6b8")
    primary = max(range(24), key=bins.__getitem__)
    secondary = max((n for n in range(24) if min(abs(n - primary), 24 - abs(n - primary)) >= 4),
                    key=bins.__getitem__)
    if bins[secondary] < bins[primary] * .065:
        secondary = (primary + 6) % 24
    return (QColor.fromHsvF(primary / 24., .72, .93),
            QColor.fromHsvF(secondary / 24., .59, 1.))


def light_sprite(color, width, height, elongated=False):
    texture = QPixmap(width, height)
    texture.fill(Qt.transparent)
    with QPainter(texture) as p:
        p.scale(width / 200., height / 200.)
        center = QPointF(100, 100)
        rect = QRectF(0, 0, 200, 200)
        wash(p, rect, center, 100, color, .84 if elongated else .78)
    return texture


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    z: float
    yaw: float = 0.
    pitch: float = 0.
    roll: float = 0.

    def towards(self, other, value, bend=0.):
        arc = math.sin(math.pi * value) * bend
        return Pose(mix(self.x, other.x, value) + arc,
                    mix(self.y, other.y, value) - arc * .37,
                    mix(self.z, other.z, value),
                    mix(self.yaw, other.yaw, value), mix(self.pitch, other.pitch, value),
                    mix(self.roll, other.roll, value))


@dataclass(frozen=True)
class Shot:
    hero: Pose
    arrival: Pose
    exit: Pose
    bend: float
    linger_side: float


SHOTS = (
    Shot(Pose(472, 252, 34, -17, 5, -7), Pose(1094, 99, 744, -39, 12, 11),
         Pose(133, 499, 680, 31, -7, -17), 100, -1),
    Shot(Pose(704, 253, 58, 19, -7, 6), Pose(119, 596, 748, 37, -13, -12),
         Pose(177, -63, 705, 34, 13, -12), -120, -1),
    Shot(Pose(553, 294, 19, -11, -6, -4), Pose(1157, 498, 860, -48, -14, 19),
         Pose(403, -74, 592, 22, 29, -12), 128, -1),
)


def cylinder_pose(pose, pointer, depth_push=0.):
    """Projective plane rotation around a real camera depth, with restrained parallax."""
    z = pose.z + depth_push
    sway = 7 + min(24., z * .047)
    x = pose.x + pointer.x() * sway
    y = pose.y + pointer.y() * sway * .6
    return Pose(x, y, z, pose.yaw + pointer.x() * 4.2,
                pose.pitch - pointer.y() * 3.0, pose.roll)


def projected_quad(pose, width, height):
    yaw, pitch, roll = map(math.radians, (pose.yaw, pose.pitch, pose.roll))
    sy, cy = math.sin(yaw), math.cos(yaw)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sr, cr = math.sin(roll), math.cos(roll)
    corners = []
    for u, v in ((-width/2, -height/2), (width/2, -height/2),
                 (width/2, height/2), (-width/2, height/2)):
        xx = u * cy
        zz = -u * sy
        yy = v * cp - zz * sp
        zz = v * sp + zz * cp
        eye = 1050. / max(300., 1050. + pose.z + zz)
        corners.append(QPointF(CENTER.x() + (pose.x - CENTER.x() + xx * cr - yy * sr) * eye,
                               CENTER.y() + (pose.y - CENTER.y() + xx * sr + yy * cr) * eye))
    return QPolygonF(corners)


def occlusion(front, back):
    area = back.width() * back.height()
    overlap = front.intersected(back)
    return smooth(3.1 * overlap.width() * overlap.height() / max(1., area))


@dataclass
class Glyph:
    sharp: QPixmap
    soft: QPixmap
    width: float
    height: float
    content_height: float
    rows: int
    lit: QPixmap
    word_boxes: tuple

    @cached_property
    def outline(self):
        """Sample both shores of the ink once for glyph-local particle births."""
        w, h = max(3, round(self.width*.22)), max(3, round(self.height*.22))
        alpha = self.sharp.toImage().scaled(w, h).convertToFormat(QImage.Format_Alpha8)
        pixels, stride = alpha.constBits(), alpha.bytesPerLine()
        dx, dy = self.width/w, self.height/h
        points = []
        for x in range(1, w-1, 2):
            for y in range(1, h-1):
                at = y*stride + x
                if pixels[at] > 126:
                    if pixels[at-stride] < 68:
                        points.append(((x+.5)*dx, y*dy, -1))
                    if pixels[at+stride] < 68:
                        points.append(((x+.5)*dx, (y+1)*dy, 1))
        return tuple(points)

    @cached_property
    def word_outline(self):
        return tuple(tuple(point for point in self.outline
                           if any(box.contains(point[0], point[1]) for box in boxes))
                     for boxes in self.word_boxes)


def glyph_texture(words, color, text_size, max_width, family, weight=QFont.Bold, *, spans=(), composition=None):
    """Lay out the whole line using Qt shaping, then cache its pixels for animation."""
    options = QTextOption()
    options.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    size = text_size
    while True:
        layout = QTextLayout(words, layout_font(size, weight, family))
        layout.setTextOption(options)
        lines = []
        y = 0.
        layout.beginLayout()
        while (line := layout.createLine()).isValid():
            line.setLineWidth(max_width)
            lines.append((line, y))
            y += line.height() * .98
        layout.endLayout()
        if y <= 185 or size <= 26:
            break
        size = max(26, size - 6)

    stagger = min(54., size * .7) if composition is not None and len(lines) > 1 else 0.
    text_width = stagger + max(line.naturalTextWidth() for line, _ in lines)
    margin = 17
    width, height = math.ceil(text_width + 2 * margin), math.ceil(y + 2 * margin)
    for j, (line, baseline) in enumerate(lines):
        inset = (text_width - line.naturalTextWidth()) / 2
        if stagger:
            left = (j + composition) % 2 == 0
            inset = 0. if left else text_width - line.naturalTextWidth()
        line.setPosition(QPointF(margin + inset, margin + baseline))
    # QTextLayout cursor positions are UTF-16 indices. Keep shaping, ligatures and
    # wrapping exactly as drawn; no per-letter font metrics or rasterization per frame.
    offsets = [0]
    for ch in words:
        offsets.append(offsets[-1] + (2 if ord(ch) > 0xffff else 1))
    boxes = []
    for word in spans:
        pieces = []
        if word.last <= len(words):
            for line, _ in lines:
                left = max(offsets[word.first], line.textStart())
                right = min(offsets[word.last], line.textStart() + line.textLength())
                if left < right:
                    a, b = line.cursorToX(left)[0], line.cursorToX(right)[0]
                    pieces.append(QRectF(min(a,b), line.y(), abs(a-b), line.height()))
        boxes.append(tuple(pieces))
    detail = 1.25
    sharp = QPixmap(round(width * detail), round(height * detail))
    sharp.fill(Qt.transparent)
    with QPainter(sharp) as p:
        p.setRenderHint(QPainter.TextAntialiasing)
        p.scale(detail, detail)
        p.setPen(WHITE)
        layout.draw(p, QPointF())
    # One tiny colored raster stands in for both bloom and defocused distant glyphs.
    soft = sharp.scaled(max(1, width // 15), max(1, height // 15),
                        Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    with QPainter(soft) as p:
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(soft.rect(), color.lighter(136))
    lit = QPixmap()
    if spans:
        lit = sharp.copy()
        with QPainter(lit) as ink:
            ink.setCompositionMode(QPainter.CompositionMode_SourceIn)
            light = QLinearGradient(0, 0, 0, lit.height())
            light.setColorAt(0., QColor("#ffffff"))
            light.setColorAt(.52, color.lighter(177))
            light.setColorAt(1., color.lighter(130))
            ink.fillRect(lit.rect(), light)
    return Glyph(sharp, soft, width, height, y, len(lines), lit, tuple(boxes))


def album_landscape(cover):
    """Prepare two soft scales of the actual artwork once, before composition."""
    bounds = QRectF(0, 0, FIELD_W, FIELD_H)
    image = QPixmap(FIELD_W, FIELD_H)
    image.fill(QColor("#111827"))
    with QPainter(image) as p:
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        size = cover.size().scaled(image.size(), Qt.KeepAspectRatioByExpanding)
        crop = QRectF(0, 0, size.width(), size.height())
        crop.moveCenter(bounds.center())
        p.drawPixmap(crop, cover, QRectF(cover.rect()))

    detail = image.scaled(158, 76, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    diffusion = detail.scaled(40, 19, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    sample = diffusion.toImage()
    luminances = []
    for y in range(1, sample.height(), 3):
        for x in range(1, sample.width(), 3):
            color = sample.pixelColor(x, y)
            luminances.append((.2126 * color.red() + .7152 * color.green() + .0722 * color.blue()) / 255)
    luminances.sort()
    highlight = luminances[int(.94 * len(luminances))]
    veil = .15 + .47 * smooth((highlight - .15) / .78)
    with QPainter(image) as p:
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(bounds, detail, QRectF(detail.rect()))
        p.setOpacity(.31)
        p.drawPixmap(bounds, diffusion, QRectF(diffusion.rect()))
        p.setOpacity(1.)
        p.fillRect(bounds, tint("#090f1c", veil))
        # Lower contrast near the moving type while preserving the artwork's
        # silhouette and tonal differences across the rest of the stage.
        wash(p, bounds, QPointF(222, 107), 162, "#090e18", .20 + .19 * highlight)
        falloff = QRadialGradient(QPointF(230, 104), 265)
        falloff.setColorAt(0., tint("#080b15", 0.))
        falloff.setColorAt(.57, tint("#080b15", .05))
        falloff.setColorAt(1., tint("#080b15", .73))
        p.fillRect(bounds, falloff)
    return image


class Atmosphere:
    def __init__(self, primary, secondary, style, album):
        self.style = style
        self.base = album if not album.isNull() else QPixmap(FIELD_W, FIELD_H)
        self.frame = QPixmap(FIELD_W, FIELD_H)
        self.near_light = light_sprite(primary, 244, 159)
        self.back_light = light_sprite(secondary, 198, 161)
        self.ribbon = light_sprite(primary, 330, 58, elongated=True)
        self.reflection = light_sprite(secondary, 284, 38, elongated=True)
        self.key = None
        if album.isNull():
            with QPainter(self.base) as p:
                backdrop = QLinearGradient(0, 0, 360, 213)
                backdrop.setColorAt(0., QColor("#141f33"))
                backdrop.setColorAt(.54, QColor("#131728"))
                backdrop.setColorAt(1., QColor("#090e1b"))
                p.fillRect(self.base.rect(), backdrop)

    def texture(self, second, light, quality, direction):
        key = second if quality == 0 else int(second * 13) if quality == 1 else -1
        if key == self.key:
            return self.frame
        self.key = key
        drift = math.sin(second * .27) if quality < 2 else 0.
        wave = math.sin(second * .32 + .6) if quality < 2 else 0.
        pulse = (.08 * light.accent + .045 * light.entrance) if quality < 2 else 0.
        width = 13. + 12. * light.energy
        with QPainter(self.frame) as p:
            p.drawPixmap(0, 0, self.base)
            p.setOpacity(.20 + light.energy * .14 + pulse)
            p.drawPixmap(QPointF(-18 + drift * width, 41 - wave * 15), self.near_light)
            p.setOpacity(.18 + light.energy * .10 + pulse * .7)
            p.drawPixmap(QPointF(288 - wave * 23, 32 + drift * 17), self.back_light)
            p.setOpacity(.15 + light.energy * .12 + pulse * .55)
            p.drawPixmap(QPointF(99 - 28 * wave, 159 + 9 * wave), self.reflection)
            p.setOpacity(.20 + light.energy * .11 + pulse)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            y = (154 if self.style == 0 else 85) + 17 * wave
            p.translate(244., y)
            p.rotate((2 if self.style == 0 else -16) + 4 * drift + 7 * pulse * direction)
            p.drawPixmap(QPointF(-165, -29), self.ribbon)
        return self.frame
