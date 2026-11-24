"""Three lyric stages over the music controller's confirmed playback clock."""
from collections import OrderedDict
from dataclasses import dataclass
import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from ..theme.motion import EASE_OUT
from .lyric_stage_art import (
    Atmosphere, CENTER, H, W, SHOTS, WHITE, Pose, clamp, cylinder_pose, extract_palette,
    album_landscape, glyph_texture, layout_font, mix, occlusion, projected_quad, smooth, tint,
)
from .lyric_stage_effects import DEFAULT_EFFECT, EffectSample, InkPatch, LyricEffects, TEXT_EFFECTS

STAGE_STYLES = (("space", "三维悬浮"), ("flow", "流光"), ("cinema", "影院"))
COVER_TRANSITION = QRectF(448, 135, 304, 304)
SURFACE = QRectF(0, 0, W, H)


@dataclass(frozen=True)
class Light:
    energy: float
    peak: float
    accent: float
    entrance: float


class LyricStage(QWidget):
    meter_requested = Signal(object)

    def __init__(self, parent, *, warm_graphics=False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setAccessibleName("沉浸歌词舞台")
        self.engine = None
        self.style_index = 0
        self.quality = 0
        self.effect = DEFAULT_EFFECT
        self._effects = LyricEffects()
        self._effect_motion = False
        self.font_size = 24
        self.family = "Microsoft YaHei UI"
        self.glyphs = OrderedDict()
        self.shots = []
        self.lines = None
        self.artwork = QPixmap()
        self.plate = QPixmap()
        self.album = QPixmap()
        self.previous_field = QPixmap()
        self.last_field = QPixmap()
        self.cover_mix = 1.
        self.palette_pair = (QColor("#79bed2"), QColor("#b093ce"))
        self.fields = {}
        self.pointer = QPointF()
        self.pointer_target = QPointF()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._advance)
        self._last_tick = time.perf_counter()
        self._last_still = None
        self._meter_key = self._envelope_track = None
        self.levels = None
        self.level_time = 0.
        self._arrival_time = 0.
        self.energy, self.peak = .25, 0.
        self.position = 0.
        self.row = -1
        self.reveal = 1.
        self._spoken_cue = None
        self._layered = False
        self._cadence = None
        self._due = 0.
        self._visual_time = None
        self._visual_stamp = 0.
        self._floor = None
        self._dynamics_target = self.dynamics = self.impulse = 0.
        self._impulse_at_sample = 0.
        self._surface = None
        if QApplication.platformName() == "windows":
            try:
                from .lyric_stage_surface import StageSurface
            except ImportError:
                StageSurface = None
            if StageSurface is not None and StageSurface.available():
                self._surface = StageSurface(self)
                if warm_graphics:
                    self._surface.warm_driver()

    def bind(self, engine):
        self.engine = engine
        if self.lines is not engine._lines:
            self.lines = engine._lines
            self.glyphs.clear()
            self._effects.clear_trail()
            self.shots = []
            for i, (_, words) in enumerate(self.lines):
                length = len(words.strip())
                shot = 2 if length <= 3 or length >= 23 else (i + (length // 5)) % 3
                if self.shots and shot == self.shots[-1]:
                    shot = (shot + 1 + i % 2) % 3
                self.shots.append(shot)
            self._last_still = None
            self._visual_stamp = 0.
        self.sync()

    def configure(self, style, quality, family, size):
        state = style, quality, family, size
        if state == (self.style_index, self.quality, self.family, self.font_size):
            return
        self.style_index, self.quality, self.family, self.font_size = state
        self._cadence = None
        self.glyphs.clear()
        self._last_still = None
        if quality:
            self.pointer = QPointF()
            if self._surface is not None:
                self._surface.suspend()
        self.sync()
        self._present()

    def set_effect(self, key):
        key = next((name for name, _ in TEXT_EFFECTS if name == key), DEFAULT_EFFECT)
        if key == self.effect:
            return
        self.effect = key
        self._effects.clear_trail()
        if key == "off":
            self._effects.clear_colors()
            if self._surface is not None:
                self._surface.retire_effects()
        self.sync()
        self._present()

    def set_artwork(self, pixmap):
        if self.artwork.cacheKey() == pixmap.cacheKey():
            return
        # Freeze the displayed blend at bounded texture size so a third cover
        # can interrupt a crossfade without flashing back to either source.
        if self.cover_mix < 1. and not self.previous_field.isNull() and not self.last_field.isNull():
            old = self.last_field.copy()
            with QPainter(old) as painter:
                painter.setOpacity(1. - self.cover_mix)
                self._pixmap(painter, QRectF(old.rect()), self.previous_field)
            self.previous_field = old
        else:
            self.previous_field = self.last_field
        self.artwork = pixmap
        self.fields.clear()
        self._effects.clear_trail()
        self._effects.clear_colors()
        self.glyphs.clear()
        if pixmap.isNull():
            self.album = QPixmap()
            self.plate = QPixmap()
            self.palette_pair = QColor("#79bed2"), QColor("#b093ce")
        else:
            self.palette_pair = extract_palette(pixmap.toImage())
            self.album = album_landscape(pixmap)
            self.plate = QPixmap(720, 352)
            with QPainter(self.plate) as p:
                p.setRenderHint(QPainter.SmoothPixmapTransform)
                size = pixmap.size().scaled(self.plate.size(), Qt.KeepAspectRatioByExpanding)
                rect = QRectF(0, 0, size.width(), size.height())
                rect.moveCenter(QRectF(self.plate.rect()).center())
                self._pixmap(p, rect, pixmap)
                shade = QLinearGradient(0, 0, 0, 352)
                for at, alpha in ((0., .47), (.45, .63), (1., .82)):
                    shade.setColorAt(at, tint("#091322", alpha))
                p.fillRect(self.plate.rect(), shade)
        self._present()

    def blend_cover(self, amount):
        self.cover_mix = amount
        if amount >= 1.:
            self.previous_field = QPixmap()
        if self.isVisible():
            self._present()

    def _set_meter(self, key):
        if key != self._meter_key:
            self._meter_key = key
            self.levels = None
            self._floor = None
            if key and key != self._envelope_track:
                self._envelope_track = key
                self._impulse_at_sample = self.impulse = self.dynamics = self._dynamics_target = 0.
                self.energy, self.peak = .25, 0.
            self.meter_requested.emit(key)

    def accept_levels(self, key, levels, stamp):
        if key == self._meter_key and key and time.monotonic() - stamp <= .55:
            rms, peak = levels
            gap = stamp - self.level_time
            if self._floor is None or gap > .55:
                self._floor = rms
                self._impulse_at_sample = self._dynamics_target = 0.
            elif gap <= 0:
                return
            else:
                rise = max(0., rms - self.levels[0], (peak - self.levels[1]) * .72)
                contrast = rms - self._floor
                self._impulse_at_sample = max(self._impulse_at_sample * math.exp(-gap * 6.5),
                                              clamp(5. * rise + 2. * max(0., contrast) - .07))
                self._dynamics_target = mix(self._dynamics_target, clamp(abs(contrast) * 9. + rise * 3.),
                                            1. - math.exp(-gap * 2.6))
                self._floor = mix(self._floor, rms, 1. - math.exp(-gap * 1.3))
            self.levels = levels
            self.level_time = stamp
            self._arrival_time = time.perf_counter()

    def set_entrance_progress(self, progress):
        if progress != self.reveal:
            self.reveal = progress
            if self.isVisible():
                self._present()

    def _read_position(self):
        position = self.engine.lyric_position()
        if position is not None:
            self.position = position
        self.row = self.engine.line_at(self.position)
        if self.row >= 0 and self.position < self.engine._lines[0][0]:
            self.row = -1
        cue = self.engine._lines[self.row][1] if self.row >= 0 else ""
        if self._spoken_cue != cue:
            self._spoken_cue = cue
            self.setAccessibleDescription(cue)
            self.setToolTip(cue if len(cue) > 160 else "")

    def _present(self):
        self.update()
        if self._layered and self.isVisible():
            # Qt 6 Windows submits the bounding rect of the dirty region for layered
            # windows. Include its opposite corners without repainting the whole UI.
            top = self.window()
            top.update(0, 0, 2, 2)
            top.update(top.width() - 2, top.height() - 2, 2, 2)

    def _arm_frame(self, now):
        self._due += self._cadence
        if self._due <= now:
            self._due += (math.floor((now - self._due) / self._cadence) + 1) * self._cadence
        self.timer.start(max(1, round(1000 * (self._due - now))))

    def sync(self):
        if not self.isVisible() or self.window().isMinimized() or self.engine is None:
            self.timer.stop()
            self._effect_motion = False
            self._effects.clear_trail()
            self._set_meter(None)
            return
        self._read_position()
        playing = self.engine.is_playing() and bool(self.engine.current_track)
        use_meter = self.style_index != 2 or self.effect == "trail"
        key = self.engine.current_track.get("key") if playing and self.quality < 2 and use_meter else None
        self._set_meter(key)
        self._effect_motion = playing and self.quality < 2
        if playing and self.quality < 2:
            screen = self.screen()
            hz = max(30., screen.refreshRate() if screen else 60.)
            limit = (30., 24.)[self.quality] if self.style_index == 2 else (144., 30.)[self.quality]
            # Use an integral number of display refreshes, with a fractional deadline.
            # The low-power preference retains its established 33 ms timer.
            cadence = math.ceil(hz / limit - 1e-5) / hz
            if not self.timer.isActive() or self._cadence != cadence:
                self._last_still = None
                self._cadence = cadence
                self._last_tick = self._due = time.perf_counter()
                self.timer.setSingleShot(self.quality == 0)
                if self.quality == 0:
                    self._arm_frame(self._due)
                else:
                    self.timer.start(42 if self.style_index == 2 else 33)
                self._present()
        else:
            self.timer.stop()
            self._effects.clear_trail()
            if self._visual_time is None or abs(self.position - self._visual_time) > .08:
                self._visual_time = self.position
            self._visual_stamp = 0.
            spans = getattr(self.engine, "_word_cues", {}).get(self.row, ())
            sample = sum(w.start <= self.position for w in spans) if self.quality == 2 else self.position
            still = (self.row, sample, self.style_index, self.quality, self.effect)
            if still != self._last_still:
                self._last_still = still
                self._present()

    def _advance(self):
        if not self.isVisible() or self.window().isMinimized() or not self.engine.is_playing():
            self.sync()
            return
        now = time.perf_counter()
        elapsed = clamp(now - self._last_tick, 0., .12)
        self._last_tick = now
        self._read_position()
        controller = getattr(self.engine, "_controller", None)
        speed = getattr(controller, "_speed", 1.)
        speed = float(speed) if isinstance(speed, (int, float)) else 1.
        projected = self.position if self._visual_time is None else self._visual_time + elapsed * speed
        # Only decorative motion is damped. Syllable boundaries and the selected
        # line always use the unfiltered, offset-corrected transport position.
        if not self._visual_stamp or abs(self.position - projected) > .14:
            self._visual_time = self.position
            self._effects.clear_trail()
        else:
            self._visual_time = mix(projected, self.position, 1. - math.exp(-elapsed * 6.))
        self._visual_stamp = now
        fresh = self.levels is not None and now - self._arrival_time < .55
        accent = self._impulse_at_sample * math.exp(-max(0., now-self._arrival_time)*6.5) if fresh else 0.
        self.impulse = mix(self.impulse, accent, 1. - math.exp(-elapsed * (23. if accent > self.impulse else 8.)))
        self.dynamics = mix(self.dynamics, self._dynamics_target if fresh else 0., 1.-math.exp(-elapsed*2.3))
        energy, peak = self.levels if fresh else (.25, 0.)
        self.energy = mix(self.energy, energy, 1. - math.exp(-elapsed * (13 if energy > self.energy else 5)))
        self.peak = mix(self.peak, peak, 1. - math.exp(-elapsed * 10))
        if self.quality == 0:
            self.pointer += (self.pointer_target - self.pointer) * (1. - math.exp(-elapsed * 6.7))
        self._present()
        if self.quality == 0:
            self._arm_frame(now)

    def showEvent(self, event):
        super().showEvent(event)
        self._layered = QApplication.platformName() == "windows" and self.window().testAttribute(Qt.WA_TranslucentBackground)
        self.sync()

    def hideEvent(self, event):
        self.timer.stop()
        self._effect_motion = False
        self._effects.clear_trail()
        if self._surface is not None:
            self._surface.suspend()
        self._visual_stamp = 0.
        self._set_meter(None)
        self.pointer_target = QPointF()
        self.pointer = QPointF()
        super().hideEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position()
        self.pointer_target = QPointF(clamp(2 * pos.x() / max(1, self.width()) - 1, -1., 1.),
                                      clamp(2 * pos.y() / max(1, self.height()) - 1, -1., 1.))

    def leaveEvent(self, event):
        self.pointer_target = QPointF()
        super().leaveEvent(event)

    def viewport_transform(self):
        scale = min(self.width() / W, self.height() / H)
        return QTransform.fromTranslate((self.width() - W * scale) / 2, (self.height() - H * scale) / 2).scale(scale, scale)

    def cover_rect(self):
        return self.viewport_transform().mapRect(COVER_TRANSITION)

    def _shot(self, row):
        return SHOTS[self.shots[row]]

    def _glyph(self, row):
        hit = self.glyphs.pop(row, None)
        if hit is None:
            words = self.lines[row][1]
            size, width = ((83, 782), (75, 805), (79, 867))[self.style_index]
            if self.style_index == 0:
                count = len(words.replace(" ", ""))
                if count <= 3:
                    size = 132
                elif count <= 10:
                    size = 105
            size = round(size * clamp(self.font_size / 24, .57, 1.7))
            # Full text remains available in the reader for unusually long single LRC cues.
            if len(words) > 160:
                words = words[:159] + "…"
            space = self.style_index == 0
            spans = getattr(self.engine, "_word_cues", {}).get(row, ())
            weight = QFont.DemiBold if space and len(words) > 9 else QFont.Bold
            hit = glyph_texture(words or "♪", self.palette_pair[0], size, width, self.family,
                                weight, spans=spans, composition=self.shots[row] if space else None)
        self.glyphs[row] = hit
        if len(self.glyphs) > 13:
            self.glyphs.popitem(last=False)
        return hit

    @staticmethod
    def _pixmap(p, rect, pixmap):
        p.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))

    @staticmethod
    def _map_pixmap(p, pixmap, quad, opacity):
        source = QPolygonF(QRectF(pixmap.rect()))
        transform = QTransform()
        if QTransform.quadToQuad(source, quad, transform):
            p.save()
            p.setOpacity(p.opacity() * clamp(opacity))
            p.setWorldTransform(transform, True)
            p.drawPixmap(0, 0, pixmap)
            p.restore()

    def _plane(self, p, glyph, pose, opacity, light):
        if opacity < .012:
            return
        quad = projected_quad(pose, glyph.width, glyph.height)
        soft = pose.z > 360
        # Distant text remains recognizable while the halo signals focus change.
        if soft and self.quality == 0:
            self._map_pixmap(p, glyph.soft, quad, opacity * .52)
        self._map_pixmap(p, glyph.sharp, quad, opacity * (.84 if soft else 1.))

    def _hero_pose(self, row, position, light):
        shot = self._shot(row)
        age = max(0., position - self.lines[row][0])
        hero = shot.hero
        if self.quality == 2:
            return hero
        # These are three camera paths, not a beat grid. The extra displacement
        # and spring compression follow measured changes in the decoded audio.
        index = self.shots[row]
        travel = (.98, -.74, .69)[index]
        phase = age * (.96, 1.15, .78)[index] + (row % 5) * .83
        breath, drift = math.sin(phase), math.sin(.62 * phase + .7)
        strength = .4 + .56 * light.energy + .77 * self.dynamics
        kick = self.impulse
        return Pose(hero.x + travel * (19. + 15. * strength) * drift,
                    hero.y + (9. + 10. * strength) * breath - kick * 9.,
                    hero.z + (20. + 20. * strength) * math.sin(phase + .7) - kick * 51.,
                    hero.yaw + 4.8 * drift, hero.pitch + 2.5 * breath,
                    hero.roll + travel * (2.4 + 1.65 * strength) * math.sin(phase * .8))

    def _voice_ink(self, painter, glyph, row, position, collect=False):
        spans = self.engine._word_cues[row]
        sx, sy = glyph.lit.width()/glyph.width, glyph.lit.height()/glyph.height
        original = painter.opacity()
        patches = []
        for cue, boxes in zip(spans, glyph.word_boxes):
            if position < cue.start:
                break
            running = cue.end is not None and cue.start <= position < cue.end
            progress = clamp((position-cue.start)/max(.001, cue.end-cue.start)) if running else 1.
            # After a final onset with no recorded end, just light the word.
            lift = 0.
            if running and self.quality < 2 and self.style_index != 2:
                lift = math.sin(math.pi * progress) * (1. if self.style_index == 0 else .5)
            mark = collect and (running or cue.end is None and position-cue.start <= .24)
            if mark:
                patches.clear()
            for box in boxes:
                if box.width() <= 0:
                    continue
                source = QRectF(box.x()*sx, box.y()*sy, box.width()*sx, box.height()*sy)
                target = QRectF(box)
                if lift:
                    bump = 1. + .055 * lift
                    target.setSize(box.size() * bump)
                    target.moveCenter(box.center() + QPointF(0, -lift*(5. + self.impulse*5.)))
                if mark and box.height() > 0:
                    # Onset-only cues have a fixed glint and no invented end.
                    patches.append(InkPatch(box, QRectF(target), progress if running else .5))
                if running and self.quality < 2:
                    painter.setOpacity(original * .72)
                    painter.drawPixmap(target, glyph.lit, source)
                    painter.setOpacity(original)
                    target.setWidth(target.width() * progress)
                    source.setWidth(source.width() * progress)
                painter.drawPixmap(target, glyph.lit, source)
        return tuple(patches)

    def _effect_ink(self, p, glyph, row, patches, angle):
        if not glyph.word_boxes:
            bounds = QRectF(0., 0., glyph.width, glyph.height)
            patches = (InkPatch(bounds, bounds, None),)
        age = self.position-self.lines[row][0]
        tail = self.lines[row+1][0]-self.position if row+1 < len(self.lines) else .3
        motion = (1., .78, .5)[self.style_index]
        sound = clamp(.86*max(0., self.energy-.11) + .63*self.impulse)
        sample = EffectSample(
            row=row, position=self.position, age=age, remaining=tail,
            colors=self.palette_pair, angle=angle, sound=sound,
            motion=motion if self._effect_motion else 0.,
            strength=(.94, .83, .69)[self.style_index], low_power=self.quality == 1,
        )
        surface = self._surface if self.quality == 0 else None
        self._effects.draw(p, surface, self.effect, glyph, patches, self.engine._word_cues.get(row, ()), sample)

    def _lead_plane(self, p, glyph, pose, opacity, row, light):
        quad = projected_quad(pose, glyph.width, glyph.height)
        box = QRectF(0., 0., glyph.width, glyph.height)
        transform = QTransform()
        if not QTransform.quadToQuad(QPolygonF(box), quad, transform):
            return
        p.save()
        p.setWorldTransform(transform, True)
        alpha = p.opacity() * opacity
        p.setOpacity(alpha * (.29 + .24 * light.energy + .3 * self.impulse))
        self._pixmap(p, box.adjusted(-3., -3., 3., 3.), glyph.soft)
        timed = bool(glyph.word_boxes)
        p.setOpacity(alpha * (.51 if timed else 1.))
        self._pixmap(p, box, glyph.sharp)
        accent = self.effect != "off" and self.quality < 2
        p.setOpacity(alpha)
        patches = self._voice_ink(p, glyph, row, self.position, collect=accent) if timed else ()
        if accent:
            self._effect_ink(p, glyph, row, patches, pose.yaw/45.)
        p.restore()


    def _space(self, p, position, row, light, visibility):
        lines = self.engine._lines
        shot = self._shot(row)
        attack = mix(.98, .61, light.energy)
        since = position - lines[row][0]
        elapsed = clamp(since / attack)
        progress = EASE_OUT.valueForProgress(elapsed) if self.quality < 2 else 1.
        visual = self._visual_time if self._visual_time is not None else position
        hero = self._hero_pose(row, visual, light)
        lead = shot.arrival.towards(hero, progress, shot.bend)
        dolly = (1. - smooth(visibility)) * 640
        lead = cylinder_pose(lead, self.pointer, dolly)
        foreground = self._glyph(row)
        front_bounds = projected_quad(lead, foreground.width - 26.,
                                      foreground.content_height).boundingRect()
        layers = [(foreground, lead, mix(.39, 1., progress))]
        preview_layer = None
        if row + 1 < len(lines):
            next_shot = self._shot(row + 1)
            peek = next_shot.arrival
            wait = lines[row + 1][0] - position
            reveal = smooth(1. - wait / 1.3) if self.quality < 2 else .45
            # The next line enters depth early, then moves into its own shot.
            if self.quality == 0:
                drift = 14. * math.sin(position * .54)
                peek = Pose(peek.x + drift, peek.y + 20. * (1. - reveal),
                            peek.z + 73. * (1. - reveal), peek.yaw, peek.pitch, peek.roll)
            peek = cylinder_pose(peek, self.pointer, dolly)
            preview = self._glyph(row + 1)
            preview_bounds = projected_quad(peek, preview.width - 26.,
                                             preview.content_height).boundingRect()
            separation = 1. - .95 * occlusion(front_bounds, preview_bounds)
            preview_layer = preview, peek, (.25 + .13 * reveal) * separation
        if row:
            old = self._shot(row - 1)
            retreat = 1. if self.quality == 2 else smooth(since / mix(1.43, .96, light.energy))
            last_hero = self._hero_pose(row - 1, lines[row][0], light)
            tail = last_hero.towards(old.exit, retreat, 105 * old.linger_side)
            tail = cylinder_pose(tail, self.pointer, dolly)
            previous = self._glyph(row - 1)
            previous_bounds = projected_quad(tail, previous.width - 26.,
                                              previous.content_height).boundingRect()
            separation = 1. - .92 * progress * occlusion(front_bounds, previous_bounds)
            if preview_layer is not None:
                separation *= 1. - .83 * retreat * occlusion(preview_bounds, previous_bounds)
                preview, peek, preview_opacity = preview_layer
                preview_opacity *= 1. - .9 * (1. - retreat) * occlusion(previous_bounds, preview_bounds)
                preview_layer = preview, peek, preview_opacity
            layers.append((previous, tail, mix(.92, .22, retreat) * separation))
        if preview_layer is not None:
            layers.append(preview_layer)
        for glyph, pose, opacity in sorted(layers, key=lambda entry: entry[1].z, reverse=True):
            if glyph is foreground:
                self._lead_plane(p, glyph, pose, opacity, row, light)
            else:
                self._plane(p, glyph, pose, opacity, light)

    def _flat_glyph(self, p, row, onto, scale, opacity, bloom=0., *, current=False):
        glyph = self._glyph(row)
        rect = QRectF(0, 0, glyph.width * scale, glyph.height * scale)
        rect.moveCenter(onto)
        p.save()
        original_opacity = p.opacity()
        if bloom:
            p.setOpacity(original_opacity * opacity * bloom)
            self._pixmap(p, rect, glyph.soft)
        timed = current and bool(glyph.word_boxes)
        alpha = original_opacity * opacity
        p.setOpacity(alpha * (.54 if timed else 1.))
        self._pixmap(p, rect, glyph.sharp)
        accent = current and self.effect != "off" and self.quality < 2
        if timed or accent:
            p.translate(rect.topLeft())
            p.scale(scale, scale)
            p.setOpacity(alpha)
            patches = self._voice_ink(p, glyph, row, self.position, collect=accent) if timed else ()
            if accent:
                self._effect_ink(p, glyph, row, patches, 0.)
        p.restore()

    def _flow(self, p, position, row, light, visibility):
        lines = self.engine._lines
        duration = mix(.78, .51, light.energy)
        age = position - lines[row][0]
        reveal = EASE_OUT.valueForProgress(clamp(age / duration)) if self.quality < 2 else 1.
        shift = 78 * (1. - visibility)
        if row and reveal < 1.:
            self._flat_glyph(p, row - 1, QPointF(594 - reveal * 122, 249 - reveal * 40),
                             mix(1., .86, reveal), (1. - reveal) * .78)
        self._flat_glyph(p, row, QPointF(594 + (1. - reveal) * 144 + shift, 249 + (1. - reveal) * 42),
                         mix(.86, 1., reveal), mix(.35, 1., reveal),
                         (.19 + .56 * light.energy + .16 * light.accent) if self.quality < 2 else .24, current=True)
        divider_y = max(326., 249 + self._glyph(row).content_height / 2 + 17.)
        if row + 1 < len(lines):
            self._flat_glyph(p, row + 1, QPointF(592, divider_y + 55), .47, .38)
        color = self.palette_pair[0]
        spread = (light.energy * 89 + light.accent * 24) if self.quality < 2 else 0.
        p.setPen(QPen(tint(color, .62), 1.4))
        half = 82 + spread
        p.drawLine(QPointF(594 - half, divider_y), QPointF(594 + half, divider_y))

    def _cinema(self, p, position, row, visibility):
        lines = self.engine._lines
        # One lead per cue. The old crossfade drew two full-size lyrics on
        # the same baseline, especially conspicuous with two wrapped lines.
        arrive = depart = 1.
        if self.quality < 2:
            arrive = smooth((position - lines[row][0]) / .24)
            if row + 1 < len(lines):
                depart = smooth((lines[row + 1][0] - position) / .16)
        offset = 24 * (1. - visibility) + 9 * (1. - arrive)
        opacity = min(mix(.65, 1., arrive), mix(.45, 1., depart))
        self._flat_glyph(p, row, QPointF(600, 264 + offset), 1., opacity, current=True)
        if row + 1 < len(lines):
            self._flat_glyph(p, row + 1, QPointF(600, 395), .46, .45)

    def _background(self, p, light, direction):
        rect = QRectF(self.rect())
        second = self._visual_time if self._visual_time is not None else self.position
        sway = math.sin(second * .22) if self.quality < 2 else 0.
        if self.style_index == 2 and not self.plate.isNull():
            field = self.plate
            extra = 7. + 5. * sway if self.quality < 2 else 0.
        else:
            if self.style_index not in self.fields:
                self.fields[self.style_index] = Atmosphere(*self.palette_pair, self.style_index, self.album)
            field = self.fields[self.style_index].texture(second, light, self.quality, direction)
            extra = 5. if self.quality < 2 else 0.
        rect.adjust(-extra * 2, -extra, extra * 2, extra)
        rect.translate(4 * sway, 2 * sway)
        self._background_pixmap(p, rect, field)
        if self.cover_mix < 1. and not self.previous_field.isNull():
            p.save()
            p.setOpacity(1. - self.cover_mix)
            self._background_pixmap(p, rect, self.previous_field)
            p.restore()
        self.last_field = field

    @staticmethod
    def _background_pixmap(p, target, field):
        # Center-crop the cached artwork across resize and DPI changes.
        scale = max(target.width() / field.width(), target.height() / field.height())
        source = QRectF(0, 0, target.width() / scale, target.height() / scale)
        source.moveCenter(QRectF(field.rect()).center())
        p.drawPixmap(target, field, source)

    def paintEvent(self, event):
        if self._surface is not None and self.quality == 0 and self.isVisible():
            image = self._surface.frame(self, self._paint_scene)
            if not image.isNull():
                with QPainter(self) as painter:
                    painter.drawImage(0, 0, image)
                return
        with QPainter(self) as painter:
            self._paint_scene(painter)

    def _paint_scene(self, p):
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        position, row = self.position, self.row
        lines = self.lines or ()
        valid = 0 <= row < len(lines)
        age = max(0., position - lines[row][0]) if valid else 0.
        entrance = math.sin(math.pi * clamp(age / .96)) ** 2 if self.quality < 2 else 0.
        light = Light(self.energy, self.peak, clamp(.35 * (self.peak - self.energy) + self.impulse), entrance)
        direction = (-1., 1., .65)[self.shots[row]] if valid else 0.
        self._background(p, light, direction)
        p.setWorldTransform(self.viewport_transform(), True)
        if valid:
            if self.style_index == 0:
                self._space(p, position, row, light, self.reveal)
            elif self.style_index == 1:
                self._flow(p, position, row, light, self.reveal)
            else:
                self._cinema(p, position, row, self.reveal)
        elif lines:
            self._plane(p, self._glyph(0), self._shot(0).arrival, .44, light)
        else:
            p.setPen(WHITE)
            p.setFont(layout_font(36))
            status = self.engine.lyric_status if self.engine is not None else "idle"
            message = {"idle": "播放歌曲后显示歌词", "loading": "正在查找歌词…",
                       "missing": "这首歌暂无歌词"}.get(status, "暂无歌词")
            p.drawText(QRectF(80, 164, 1040, 178), Qt.AlignCenter, message)
