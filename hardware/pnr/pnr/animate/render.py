"""Raster frames of a timeline (Pillow).

Frames are drawn at ``SUPERSAMPLE`` times the output size and reduced with a Lanczos filter
(Pillow draws without anti-aliasing). Draw order: background, substrate, zones (bottom layer
first), tracks (``B.Cu``, ``In2.Cu``, ``In1.Cu``, ``F.Cu``), pads, vias (on top, as KiCad draws
them, so a via on a pad stays visible), courtyards, reference labels (only when legible), KiCad
DRC finding markers, ratsnest, highlights, outline, overlay. Translucent colours are
blended with the substrate up front, so a frame needs no alpha compositing except for the
congestion heat map and zone reveals.

A header that lists placement constraints (or a comparison's reference overlay) adds two passes
(:mod:`.highlight`): tints and a line group's guide line right after the substrate, and the
rigid bodies, target edges and tethers after the outline. Copper a route keeps as it is (a
hierarchical knit's block copper, ``View.fixed``) is drawn dimmed. Without either, a frame is
drawn exactly as before.

Overlay text is whitelisted: :func:`safe_text` rejects anything that looks like a path or an
e-mail address, and every other string comes from the fixed tables of :mod:`.theme`.
"""

from __future__ import annotations

import math
import re

from PIL import Image, ImageDraw, ImageFont

from . import highlight, theme
from .timeline import montage_score_text

_UNSAFE = re.compile(r"\w/\w|~|[A-Za-z]:\\|\\\\|[\w.+-]+@[\w-]+\.[\w.]+|://")
LAYER_ORDER = ("B.Cu", "In2.Cu", "In1.Cu", "F.Cu")


def safe_text(text):
    """``text`` if it is fit for an overlay (no path, no e-mail address); else ValueError."""
    text = str(text)
    if _UNSAFE.search(text):
        raise ValueError("overlay text looks like a path or an address: %r" % text[:40])
    return text


def rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    a, b = rgb(a) if isinstance(a, str) else a, rgb(b) if isinstance(b, str) else b
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


_FONTS = {}


def font(size):
    size = max(6, int(round(size)))
    if size not in _FONTS:
        _FONTS[size] = ImageFont.load_default(size)
    return _FONTS[size]


def layer_color(name):
    return rgb(theme.LAYERS.get(name, theme.LAYERS["F.Cu"]))


class Transform:
    """Engine micrometres to pixels of an image region (y flipped)."""

    def __init__(self, camera, width, height, ox=0.0, oy=0.0):
        x0, y0, x1, y1 = camera
        self.s = min(width / max(1.0, x1 - x0), height / max(1.0, y1 - y0))
        self.cx, self.cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        self.hx, self.hy = ox + width / 2.0, oy + height / 2.0

    def __call__(self, x, y):
        return (self.hx + (x - self.cx) * self.s, self.hy - (y - self.cy) * self.s)

    def length(self, value):
        return value * self.s


def _rotate(x, y, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def courtyard_rect(comp, pose):
    """``(cx, cy, w, h)`` (um) of a header component's courtyard under ``pose``: the
    ``courtyard`` centred on the pose, or (PNR_COMPACT) its off-centre ``body`` box,
    mirrored when the pose's side differs from the header's and turned with the part."""
    x, y, rot, side = pose
    body = comp.get("body")
    if not body:
        w, h = comp["courtyard"]
        if int(round(rot / 90.0)) % 2 == 1:
            w, h = h, w
        return x, y, w, h
    x0, y0, x1, y1 = body
    if side != comp["side"]:
        y0, y1 = -y1, -y0
    q = int(round(rot / 90.0)) % 4
    pts = [((px, py), (-py, px), (-px, -py), (py, -px))[q] for px in (x0, x1) for py in (y0, y1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (
        x + (min(xs) + max(xs)) / 2.0,
        y + (min(ys) + max(ys)) / 2.0,
        max(xs) - min(xs),
        max(ys) - min(ys),
    )


class Renderer:
    """Draws :class:`pnr.animate.timeline.View` frames of one trace header."""

    def __init__(self, header, subject, width=800):
        self.header = header
        self.subject = subject
        self.width = int(width)
        self.layers = list(header["copper_layers"])
        self.components = {c["ref"]: c for c in header["components"]}
        self.pins = {n["name"]: [tuple(p) for p in n["pins"]] for n in header["nets"]}
        self.pad_index = {}
        for comp in header["components"]:
            for pad in comp["pads"]:
                self.pad_index.setdefault((comp["ref"], pad["name"]), pad)
        w, h = header["outline"]["w"], header["outline"]["h"]
        m = theme.MARGIN * max(w, h)
        board_h = self.width * (h + 2 * m) / max(1.0, (w + 2 * m))
        board_h = int(min(1.2 * self.width, max(0.45 * self.width, board_h)))
        self.board_box = (0, theme.HEADER_PX, self.width, board_h)
        height = theme.HEADER_PX + board_h + theme.FOOTER_PX
        self.height = height + (height % 2)
        self.title = safe_text(subject.get("title") or "")
        self.stats = safe_text(
            "%d parts · %d nets · %d layers"
            % (subject["parts"], subject["nets"], subject["layers"])
        )
        self.description = safe_text(subject.get("description") or "")
        self.strings = {self.title, self.stats, self.description}
        self._last = None
        # Constraint highlighting (section 6.2 of the constraint animation design): the
        # header's own constraints, or another design's as a neutral reference overlay.
        self.constraints = highlight.constraints_of(header)
        self.reference = []
        self.compact = False  # comparison panels and hierarchy tiles: smaller montage text

    # --- frames -------------------------------------------------------------------------
    def frame(self, view):
        """The RGB image of one :class:`View`."""
        if self._last is not None and self._last[0] is view:
            return self._last[1]
        image = self._compose(view)
        if view.blend is not None:
            other, t = view.blend
            image = Image.blend(image, self._compose(other), max(0.0, min(1.0, t)))
        self._overlay(image, view)
        self._last = (view, image)
        return image

    def _compose(self, view):
        """Everything but the header and footer."""
        image = Image.new("RGB", (self.width, self.height), rgb(theme.BACKGROUND))
        x, y, w, h = self.board_box
        if view.card is not None and view.card.get("kind") == "title":
            backdrop = view.card.get("backdrop")
            if backdrop is not None:
                board = self.board(backdrop, w, h, labels=False)
                dim = Image.new("RGB", board.size, rgb(theme.BACKGROUND))
                image.paste(Image.blend(dim, board, 0.16), (x, y))
            self._title_card(image)
            return image
        if view.montage is not None:
            board = self._montage(view, w, h)
        else:
            board = self.board(view, w, h)
        image.paste(board, (x, y))
        if view.card is not None and view.card.get("kind") == "end":
            self._end_card(image, view.card)
        return image

    def board(self, view, width, height, ss=theme.SUPERSAMPLE, labels=True):
        """The board region of ``view`` at ``width`` x ``height`` pixels."""
        if 0.0 < view.native_mix < 1.0:
            engine = self.board(view.copy(native_mix=0.0), width, height, ss, labels)
            native = self.board(view.copy(native_mix=1.0), width, height, ss, labels)
            return Image.blend(engine, native, view.native_mix)
        big = Image.new("RGB", (width * ss, height * ss), rgb(theme.BACKGROUND))
        tf = Transform(view.camera, width * ss, height * ss)
        draw = ImageDraw.Draw(big)
        self._substrate(draw, tf)
        marks = self._highlights()
        for constraints, reference in marks:
            highlight.draw_under(self, draw, tf, view.poses, constraints, reference, ss)
        if view.native is not None and view.native_mix >= 1.0:
            self._zones(big, tf, view.native.get("zones", []), view.zone_reveal)
            draw = ImageDraw.Draw(big)
            layers = [({"": view.native}, {})]
        else:
            layers = self._engine_copper(view)
            if view.fixed is not None:  # a hierarchical knit's block copper, kept as it is
                layers = [({"": view.fixed}, {"": "fixed"})] + layers
        for nets, styles in layers:
            self._copper(draw, tf, nets, styles, ss)
        self._pads(draw, tf, view.poses, ss)
        for nets, styles in layers:
            self._vias(draw, tf, nets, styles)
        self._courtyards(draw, tf, view, ss)
        if labels:
            self._labels(draw, tf, view.poses, ss)
        self._findings(draw, tf, view.findings, ss)
        self._ratsnest(draw, tf, view, ss)
        self._outline(draw, tf, ss)
        for constraints, reference in marks:
            highlight.draw_over(self, draw, tf, view.poses, constraints, reference, ss)
        self._decorate(big, draw, tf, view, ss)
        if view.heat is not None:
            big = self._heat(big, tf, view.heat)
        return big.resize((width, height), Image.LANCZOS)

    def _highlights(self):
        """``[(constraints, reference)]`` to draw: none for a design without constraints."""
        marks = []
        if self.constraints:
            marks.append((self.constraints, False))
        if self.reference:
            marks.append((self.reference, True))
        return marks

    def _decorate(self, image, draw, tf, view, ss):
        """A hook for subclasses (block outlines of the hierarchical renderer)."""

    # --- board layers -------------------------------------------------------------------
    def _outline_points(self, tf):
        poly = self.header["outline"].get("polygon")
        if poly:
            return [tf(x, y) for x, y in poly]
        w, h = self.header["outline"]["w"], self.header["outline"]["h"]
        return [tf(0, 0), tf(w, 0), tf(w, h), tf(0, h)]

    def _substrate(self, draw, tf):
        draw.polygon(self._outline_points(tf), fill=rgb(theme.SUBSTRATE))

    def _outline(self, draw, tf, ss):
        points = self._outline_points(tf)
        draw.line(points + [points[0]], fill=rgb(theme.OUTLINE), width=max(1, int(1.5 * ss)))

    def _zones(self, image, tf, zones, reveal):
        if not zones:
            return
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        order = {name: i for i, name in enumerate(LAYER_ORDER)}
        for index, _net, rings in sorted(zones, key=lambda z: order.get(self._layer_name(z[0]), 0)):
            color = mix(theme.SUBSTRATE, layer_color(self._layer_name(index)), 0.25)
            for ring in rings:
                if len(ring) >= 3:
                    draw.polygon([tf(x, y) for x, y in ring], fill=color + (255,))
        if reveal < 1.0:
            cut = int(image.size[0] * max(0.0, reveal))
            mask = layer.getchannel("A")
            ImageDraw.Draw(mask).rectangle((cut, 0, image.size[0], image.size[1]), fill=0)
            layer.putalpha(mask)
        image.paste(layer, (0, 0), layer)

    def _layer_name(self, index):
        return self.layers[index] if 0 <= index < len(self.layers) else "F.Cu"

    @staticmethod
    def _engine_copper(view):
        """``[(nets, styles)]`` of the engine's copper in draw order: committed, ripped,
        provisional."""
        styles = {}
        for net in view.committed:
            styles[net] = "flash" if view.flash.get(net) else "solid"
        ripped = {n: c for n, (c, _k) in view.ripped.items()}
        provisional = {n: c for n, c in view.provisional.items() if n not in view.committed}
        return [
            (view.committed, styles),
            (ripped, {n: "ripped" for n in ripped}),
            (provisional, {n: "provisional" for n in provisional}),
        ]

    def _copper(self, draw, tf, nets, styles, ss):
        rank = {name: i for i, name in enumerate(LAYER_ORDER)}
        rows = []
        for net in sorted(nets):
            copper = nets[net]
            if not copper:
                continue
            style = styles.get(net, "solid")
            for track in copper.get("tracks", []):
                name = self._layer_name(track[0])
                rows.append((rank.get(name, 3), style, name, track))
        rows.sort(key=lambda r: (r[0], r[1] != "solid"))
        for _rank, style, name, (_layer, x0, y0, x1, y1, width) in rows:
            a, b = tf(x0, y0), tf(x1, y1)
            w = max(1.0, tf.length(width))
            if style == "provisional":
                self._dashed(draw, a, b, max(1.0, w * 0.6), rgb(theme.PROVISIONAL), tf, ss)
                continue
            color = {
                "flash": rgb(theme.NEW),
                "ripped": rgb(theme.RIPPED),
            }.get(style, layer_color(name))
            if style == "fixed":
                color = mix(theme.SUBSTRATE, layer_color(name), 0.55)
            if style == "ripped":
                self._dashed(draw, a, b, w, color, tf, ss)
                continue
            self._segment(draw, a, b, w, color)

    def _vias(self, draw, tf, nets, styles):
        for net in sorted(nets):
            copper = nets[net] or {}
            style = styles.get(net, "solid")
            ring = rgb(theme.NEW) if style == "flash" else rgb(theme.VIA_RING)
            if style == "provisional":
                ring = rgb(theme.PROVISIONAL)
            if style == "ripped":
                ring = rgb(theme.RIPPED)
            if style == "fixed":
                ring = mix(theme.SUBSTRATE, theme.VIA_RING, 0.55)
            for x, y, diameter, drill in copper.get("vias", []):
                cx, cy = tf(x, y)
                r = max(1.5, tf.length(diameter) / 2.0)
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ring)
                h = max(0.75, tf.length(drill) / 2.0)
                draw.ellipse((cx - h, cy - h, cx + h, cy + h), fill=rgb(theme.VIA_HOLE))

    @staticmethod
    def _segment(draw, a, b, width, color):
        draw.line([a, b], fill=color, width=max(1, int(round(width))))
        if width >= 3:
            r = width / 2.0
            for x, y in (a, b):
                draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    def _dashed(self, draw, a, b, width, color, tf, ss):
        length = math.dist(a, b)
        if length < 1e-6:
            return
        dash, gap = tf.length(600), tf.length(400)
        dash, gap = max(dash, 3.0 * ss), max(gap, 2.0 * ss)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        t = 0.0
        while t < length:
            e = min(length, t + dash)
            self._segment(
                draw, (a[0] + ux * t, a[1] + uy * t), (a[0] + ux * e, a[1] + uy * e), width, color
            )
            t = e + gap

    def pad_shapes(self, poses):
        """``(ref, pad, polygon or box, shape, drill)`` of every pad under ``poses``."""
        out = []
        for ref in sorted(poses):
            comp = self.components.get(ref)
            if comp is None:
                continue
            x, y, rot, side = poses[ref]
            flip = side != comp["side"]
            for pad in comp["pads"]:
                ox, oy = pad["offset"]
                if flip:
                    oy = -oy
                dx, dy = _rotate(ox, oy, rot)
                out.append((ref, pad, (x + dx, y + dy), (rot + (pad.get("angle") or 0.0)) % 360))
        return out

    def _pads(self, draw, tf, poses, ss):
        fill, hole = rgb(theme.PAD), rgb(theme.VIA_HOLE)
        for _ref, pad, (px, py), angle in self.pad_shapes(poses):
            w, h = pad["size"]
            if w <= 0 or h <= 0:
                continue
            cx, cy = tf(px, py)
            quarter = round(angle / 90.0) * 90.0
            if abs(angle - quarter) < 1e-3:
                if int(quarter) % 180 == 90:
                    w, h = h, w
                hw, hh = max(0.75, tf.length(w) / 2.0), max(0.75, tf.length(h) / 2.0)
                box = (cx - hw, cy - hh, cx + hw, cy + hh)
                shape = pad.get("shape") or "rect"
                if shape == "circle":
                    draw.ellipse(box, fill=fill)
                elif shape == "oval":
                    draw.rounded_rectangle(box, radius=min(hw, hh), fill=fill)
                elif shape == "roundrect" and pad.get("corner"):
                    draw.rounded_rectangle(box, radius=tf.length(pad["corner"]), fill=fill)
                else:
                    draw.rectangle(box, fill=fill)
            else:
                corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
                points = []
                for u, v in corners:
                    du, dv = _rotate(u, v, angle)
                    points.append(tf(px + du, py + dv))
                draw.polygon(points, fill=fill)
            if pad.get("drill"):
                r = max(0.75, tf.length(min(pad["drill"])) / 2.0)
                draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=hole)

    def _findings(self, draw, tf, findings, ss):
        """A ring around each KiCad DRC finding (its first item, e.g. the via)."""
        color = rgb(theme.FAIL)
        r = max(4.0 * ss, tf.length(700))
        for x, y in findings or ():
            cx, cy = tf(x, y)
            draw.ellipse(
                (cx - r, cy - r, cx + r, cy + r), outline=color, width=max(1, int(1.5 * ss))
            )

    def _courtyard_box(self, tf, ref, pose):
        x, y, w, h = courtyard_rect(self.components[ref], pose)
        a, b = tf(x - w / 2.0, y + h / 2.0), tf(x + w / 2.0, y - h / 2.0)
        return a + b

    def _courtyards(self, draw, tf, view, ss):
        marked = set(view.marked)
        for ref in sorted(view.poses):
            if ref not in self.components:
                continue
            box = self._courtyard_box(tf, ref, view.poses[ref])
            if view.failed:
                color, width = rgb(theme.FAIL), int(1.5 * ss)
            elif ref in marked:
                color, width = rgb(theme.ACCENT), 2 * ss
            else:
                color, width = rgb(theme.COURTYARD), ss
            draw.rectangle(box, outline=color, width=max(1, width))

    def _labels(self, draw, tf, poses, ss):
        size = tf.length(900)
        if size < 7 * ss:
            return
        size = min(size, 14 * ss)
        face = font(size)
        color = mix(theme.SUBSTRATE, theme.TEXT, 0.55)
        for ref in sorted(poses):
            if ref not in self.components:
                continue
            x, y = tf(poses[ref][0], poses[ref][1])
            draw.text((x, y), ref, fill=color, font=face, anchor="mm")

    def pin_xy(self, poses):
        out = {}
        for ref, pad, xy, _angle in self.pad_shapes(poses):
            out.setdefault((ref, pad["name"]), xy)
        return out

    def ratsnest(self, view):
        """Ratsnest lines (µm): KiCad's open pairs, else a minimum spanning tree per net
        between its pad groups (nearest pad pair per group pair)."""
        if view.open_pairs is not None:
            return [((a, b), (c, d)) for a, b, c, d in view.open_pairs]
        xy = self.pin_xy(view.poses)
        lines = []
        for net in sorted(self.pins):
            pins = self.pins[net]
            if len(pins) < 2:
                continue
            groups = view.groups.get(net) or [[i] for i in range(len(pins))]
            points = [[xy[pins[i]] for i in g if i < len(pins) and pins[i] in xy] for g in groups]
            points = [p for p in points if p]
            if len(points) < 2:
                continue
            lines.extend(_group_mst(points))
        return lines

    def _ratsnest(self, draw, tf, view, ss):
        color = mix(theme.SUBSTRATE, theme.RATSNEST, 0.55)
        for a, b in self.ratsnest(view):
            draw.line([tf(*a), tf(*b)], fill=color, width=max(1, ss))

    def _heat(self, image, tf, heat):
        cells, pitch = heat.get("cells") or [], heat.get("pitch") or 1
        alpha = float(heat.get("alpha", 1.0))
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for i, row in enumerate(cells):
            for j, value in enumerate(row):
                if value <= 0:
                    continue
                t = value / 255.0
                color = mix(theme.HEAT_LOW, theme.HEAT_HIGH, t)
                a = int(round(255 * alpha * (0.15 + 0.5 * t)))
                x0, y0 = tf(i * pitch, (j + 1) * pitch)
                x1, y1 = tf((i + 1) * pitch, j * pitch)
                draw.rectangle((x0, y0, x1, y1), fill=color + (a,))
        base = image.convert("RGBA")
        base.alpha_composite(layer)
        return base.convert("RGB")

    # --- montage ------------------------------------------------------------------------
    def _montage(self, view, width, height):
        m = view.montage
        tiles = m["tiles"]
        count = len(tiles) + (1 if m.get("more") else 0)
        cols = count if count <= 4 else 4
        rows = int(math.ceil(count / float(cols)))
        gap, label = (8, 28) if self.compact else (10, 18)
        tw = (width - gap * (cols + 1)) // cols
        th = (height - gap * (rows + 1) - label * rows) // rows
        ow, oh = self.header["outline"]["w"], self.header["outline"]["h"]
        tw, th = min(tw, int(th * ow / oh)), min(th, int(tw * oh / ow))
        total_w = cols * tw + (cols - 1) * gap
        total_h = rows * (th + label) + (rows - 1) * gap
        x0, y0 = (width - total_w) // 2, (height - total_h) // 2
        image = Image.new("RGB", (width, height), rgb(theme.BACKGROUND))
        draw = ImageDraw.Draw(image)
        face, small = font(12), font(11)
        zoom = m.get("zoom", 0.0)
        chosen_box = None
        for index, tile in enumerate(tiles):
            c, r = index % cols, index // cols
            box = (x0 + c * (tw + gap), y0 + r * (th + label + gap), tw, th)
            if tile["chosen"]:
                chosen_box = box
                if zoom > 0:
                    continue
            fade = 1.0 - zoom
            if fade <= 0:
                continue
            picture = self.board(tile["view"], tw, th, labels=False)
            if not tile["lit"]:
                picture = Image.blend(
                    Image.new("RGB", picture.size, rgb(theme.BACKGROUND)), picture, 0.35
                )
            if fade < 1.0:
                picture = Image.blend(
                    Image.new("RGB", picture.size, rgb(theme.BACKGROUND)), picture, fade
                )
            image.paste(picture, box[:2])
            text = safe_text(tile["label"])
            score = montage_score_text(m.get("criterion"), tile.get("score"))
            if score and not self.compact:
                text += " · " + safe_text(score)
            tone = theme.TEXT if tile["lit"] else theme.MUTED
            if tile["chosen"]:
                draw.rectangle(
                    (box[0] - 2, box[1] - 2, box[0] + tw + 1, box[1] + th + 1),
                    outline=rgb(theme.ACCENT),
                    width=2,
                )
                text = "chosen · " + text
                tone = theme.ACCENT
            if self.compact:
                self._tile_caption(draw, box, tw, th, text, score, tile, tone, fade)
                continue
            draw.text(
                (box[0] + tw / 2.0, box[1] + th + 3),
                text,
                fill=mix(theme.BACKGROUND, tone, fade),
                font=face,
                anchor="ma",
            )
        if m.get("more") and zoom < 1.0:
            index = len(tiles)
            c, r = index % cols, index // cols
            bx, by = x0 + c * (tw + gap), y0 + r * (th + label + gap)
            draw.text(
                (bx + tw / 2.0, by + th / 2.0),
                "+%d more" % m["more"],
                fill=mix(theme.BACKGROUND, theme.MUTED, 1.0 - zoom),
                font=small,
                anchor="mm",
            )
        if zoom > 0 and chosen_box is not None:
            full = self._fit_box(width, height)
            bx = [a + (b - a) * zoom for a, b in zip(chosen_box, full)]
            tile = next(t for t in tiles if t["chosen"])
            w, h = max(2, int(bx[2])), max(2, int(bx[3]))
            picture = self.board(tile["view"], w, h, labels=zoom > 0.9)
            image.paste(picture, (int(bx[0]), int(bx[1])))
        alpha = m.get("alpha", 1.0)
        if alpha < 1.0:
            # Cross-fade with the board on screen (never through an empty frame).
            under = self.board(view.copy(montage=None), width, height)
            image = Image.blend(under, image, max(0.0, alpha))
        return image

    def _fit_box(self, width, height):
        return (0, 0, width, height)

    def _tile_caption(self, draw, box, tw, th, text, score, tile, tone, fade):
        """Two short lines under a compact montage tile: the label, then its score (and, for
        a design with edge alignments, the order of its edge parts)."""
        lines = [text]
        extra = [safe_text(score)] if score else []
        for _edge, refs in highlight.edge_order(tile["view"].poses, self.constraints):
            extra.append(safe_text("\u00b7".join(refs)))
        if extra:
            lines.append(" · ".join(extra))
        for k, line in enumerate(lines):
            size, line = _fit(draw, line, tw + 6, (11, 10, 9) if k == 0 else (10, 9))
            self.strings.add(line)
            draw.text(
                (box[0] + tw / 2.0, box[1] + th + 3 + 13 * k),
                line,
                fill=mix(theme.BACKGROUND, tone if k == 0 else theme.MUTED, fade),
                font=font(size),
                anchor="ma",
            )

    # --- overlay and cards --------------------------------------------------------------
    def _overlay(self, image, view):
        draw = ImageDraw.Draw(image)
        header, footer = theme.HEADER_PX, theme.FOOTER_PX
        draw.rectangle((0, 0, self.width, header - 1), fill=rgb(theme.BACKGROUND))
        draw.text((12, header / 2), self.title, fill=rgb(theme.TEXT), font=font(16), anchor="lm")
        draw.text(
            (self.width - 12, header / 2),
            self.stats,
            fill=rgb(theme.MUTED),
            font=font(13),
            anchor="rm",
        )
        self._footer(draw, view, self.height - footer)

    def _footer(self, draw, view, top):
        """The footer strip from ``top``: phase, experiment, "% routed" bar and step."""
        footer = theme.FOOTER_PX
        draw.rectangle((0, top, self.width, top + footer), fill=rgb(theme.BACKGROUND))
        phase = theme.PHASE_TEXT.get(view.phase, "")
        if view.caption:
            phase = (phase + " · " if phase else "") + safe_text(view.caption)
        self.strings.add(phase)
        done, total, source = view.progress
        fraction = 0.0 if not total else max(0.0, min(1.0, done / float(total)))
        bar_w, bar_h = (150, 8) if self.width >= 640 else (90, 8)
        bx = self.width - 12 - (110 if self.width >= 640 else 80) - bar_w
        label = "%d%% routed" % int(math.floor(100 * fraction + 1e-9))
        room = bx - 8 - draw.textlength(label, font=font(12)) - 12 - 16
        size, text = _fit(draw, phase, room, (13, 12, 11))
        draw.text((12, top + footer / 2), text, fill=rgb(theme.TEXT), font=font(size), anchor="lm")
        by = top + (footer - bar_h) // 2
        draw.rounded_rectangle(
            (bx, by, bx + bar_w, by + bar_h), radius=4, fill=mix(theme.BACKGROUND, theme.MUTED, 0.3)
        )
        color = theme.PROVISIONAL if source == "router-provisional" else theme.ACCENT
        if view.phase == "result" and done < total:
            color = theme.FAIL
        if view.ghost is not None:
            g_done, g_total, _source = view.ghost
            ghost = 0.0 if not g_total else max(0.0, min(1.0, g_done / float(g_total)))
            if ghost > fraction:  # negotiation: provisional connections, lighter, behind
                draw.rounded_rectangle(
                    (bx, by, bx + max(bar_h, bar_w * ghost), by + bar_h),
                    radius=4,
                    fill=mix(theme.BACKGROUND, theme.PROVISIONAL, 0.45),
                )
        if fraction > 0:
            draw.rounded_rectangle(
                (bx, by, bx + max(bar_h, bar_w * fraction), by + bar_h), radius=4, fill=rgb(color)
            )
        draw.text(
            (bx - 8, top + footer / 2), label, fill=rgb(theme.TEXT), font=font(12), anchor="rm"
        )
        if view.step is not None:
            i, n, unit = view.step
            step = "%s %d/%d" % (unit, i, n) if n else ""
            draw.text(
                (self.width - 12, top + footer / 2),
                step,
                fill=rgb(theme.MUTED),
                font=font(11),
                anchor="rm",
            )

    def _title_card(self, image):
        draw = ImageDraw.Draw(image)
        x, y, w, h = self.board_box
        cx, cy = self.width / 2.0, y + h / 2.0
        draw.text((cx, cy - 36), self.title, fill=rgb(theme.TEXT), font=font(30), anchor="mm")
        lines = _wrap(self.description, 70)
        for i, line in enumerate(lines[:3]):
            draw.text(
                (cx, cy + 4 + 20 * i), line, fill=rgb(theme.MUTED), font=font(14), anchor="mm"
            )
        draw.text(
            (cx, cy + 16 + 20 * min(3, len(lines))),
            self.stats,
            fill=rgb(theme.ACCENT),
            font=font(14),
            anchor="mm",
        )

    def _end_card(self, image, card):
        result = card.get("result") or {}
        x, y, w, h = self.board_box
        panel_h = 64
        top = y + h - panel_h
        overlay = Image.new("RGBA", (w, panel_h), rgb(theme.BACKGROUND) + (215,))
        region = image.crop((x, top, x + w, top + panel_h)).convert("RGBA")
        region.alpha_composite(overlay)
        image.paste(region.convert("RGB"), (x, top))
        draw = ImageDraw.Draw(image)
        opens, violations = result.get("opens"), result.get("violations")
        if isinstance(violations, dict):
            violations = sum(violations.values())
        passed = result.get("passed")
        if opens is None and violations is None:
            verdict = "Engine result (no KiCad stage in this trace)"
            color = theme.MUTED
        else:
            verdict = "KiCad DRC: %s unconnected · %s violations" % (opens, violations)
            color = theme.ACCENT if passed else theme.FAIL
            rules = _rule_names(result.get("rules"))
            if violations and rules:
                verdict += " (%s)" % rules
        metrics = []
        if result.get("vias") is not None:
            metrics.append("%d vias" % result["vias"])
        if result.get("copper_length_mm") is not None:
            metrics.append("%.1f mm copper" % result["copper_length_mm"])
        if self.subject.get("seed") is not None:
            metrics.append("seed %s" % self.subject["seed"])
        rejected = card.get("rejected") or {}
        if rejected:
            parts = []
            for key in sorted(rejected):
                parts.append("%d by %s" % (rejected[key], safe_text(theme.criterion_text(key))))
            metrics.append("rivals set aside: " + ", ".join(parts))
        line = safe_text(" · ".join(metrics))
        verdict = safe_text(verdict)
        size, verdict = _fit(draw, verdict, w - 28, (16, 15, 14, 13))
        self.strings.update((verdict, line))
        draw.text((x + 14, top + 20), verdict, fill=rgb(color), font=font(size), anchor="lm")
        draw.text((x + 14, top + 44), line, fill=rgb(theme.MUTED), font=font(12), anchor="lm")


def _rule_names(rules, most=2):
    """The rules KiCad's findings break, most frequent first: "rule-a 10, rule-b 4, 1 more"."""
    if not isinstance(rules, dict) or not rules:
        return ""
    ranked = sorted(rules.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
    names = [str(k) for k, _n in ranked if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(k))]
    if not names:
        return ""
    if len(ranked) == 1:
        return names[0]
    text = ", ".join("%s %d" % (k, rules[k]) for k in names[:most])
    rest = len(ranked) - min(most, len(names))
    return text + (", %d more" % rest if rest > 0 else "")


def _fit(draw, text, room, sizes):
    """``(size, text)``: the largest of ``sizes`` at which ``text`` fits ``room`` pixels, else the
    smallest with the text cut to fit and an ellipsis."""
    for size in sizes:
        if draw.textlength(text, font=font(size)) <= room:
            return size, text
    size = sizes[-1]
    while text and draw.textlength(text + "\u2026", font=font(size)) > room:
        text = text[:-1]
    return size, text.rstrip(" \u00b7") + "\u2026"


def _wrap(text, width):
    words, lines, line = str(text).split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        lines.append(line)
    return lines


def _group_mst(groups):
    """Prim's tree over pad groups; edge weight: the nearest pad pair between two groups."""
    n = len(groups)
    best = [(math.inf, None)] * n
    used = [False] * n
    used[0] = True

    def nearest(a, b):
        return min(
            ((math.dist(p, q), p, q) for p in groups[a] for q in groups[b]), key=lambda t: t[0]
        )

    for j in range(1, n):
        d, p, q = nearest(0, j)
        best[j] = (d, (p, q))
    lines = []
    for _ in range(n - 1):
        j = min((k for k in range(n) if not used[k]), key=lambda k: (best[k][0], k))
        used[j] = True
        lines.append(best[j][1])
        for k in range(n):
            if not used[k]:
                d, p, q = nearest(j, k)
                if d < best[k][0]:
                    best[k] = (d, (p, q))
    return lines
