#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fonttools>=4.56.0",
# ]
# ///
r"""
merge_fonts.py - Merge fallback fonts into a monospace TrueType base font.

The base font is kept as is, including its hinting, embedded bitmaps, and
layout tables.  Each later font only contributes characters that no earlier
font has.  Each font's terminal cell is its full block character (U+2588)
or, without one, its ascent and descent.  Added glyphs are scaled so that
cell matches the base font's cell, and its top is aligned with the base
cell's top, which keeps block and box drawing characters seamless.  Each
glyph is one cell wide, or two for East Asian wide and fullwidth
characters, and glyphs that would spill out of their cells are shifted or
squeezed to fit.  CFF (.otf) and composite glyphs are converted to simple
TrueType outlines, so the result is a plain .ttf that works on Windows and
Linux.

Windows (GDI and DirectWrite) renders a font whose family name is exactly
"Courier New" with extra weight under ClearType; the same file under any
other name looks lighter.  --embolden thickens the base font's strokes to
approximate that.

Fonts are given as FILE[,FACE][,X[,Y]], the same layout as a Windows
FontLink SystemLink entry.  FACE is ignored.  X and Y are percentages
applied after scaling to the base font's units per em (Y defaults to X);
without them the scale is chosen to match the cells.

Usage:
    uv run ../merge_fonts.py --embolden 20 --out "Courier DWM20b" cour.ttf JuliaMono-Regular.ttf,100,98 NotoEmoji-Regular.ttf UnifontExMono.ttf,120,114 unifont-16.0.01.otf,120,114 --exclude "CJK Unified Ideographs Extension" --embolden-chars "$,2TY=16" --embolden-chars "H=18" --embolden-chars "/X\=22" --embolden-chars "F=25" --embolden-chars "&N^=26" --embolden-chars "4MQV=28" --embolden-chars "Av=30" --embolden-chars "y=32"  # noqa
"""

import argparse
import collections
import datetime
import math
import os
import re
import unicodedata

from fontTools import unicodedata as fontunicodedata
from fontTools.pens.boundsPen import BoundsPen, ControlBoundsPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

MaxGlyphs = 65535
# Tables that need an entry per glyph, or that the changes made here would
# invalidate.  VDMX is kept: Windows takes each pixel size's ascent and
# descent from it, so the base font's line height is unchanged, and added
# glyphs are fitted inside the base font's cell.
DropTables = ['DSIG', 'hdmx', 'LTSH', 'vhea', 'vmtx', 'VORG']
# Name IDs that identify the font family; all of these are replaced.
FamilyNameIDs = [1, 2, 3, 4, 6, 16, 17, 18, 21, 22, 25]


def parse_font_spec(spec):
    """
    Parse FILE[,FACE][,X[,Y]].

    :param spec: the command line value.
    :returns: (path, xscale, yscale) with the scales as fractions, or None
        when they should be determined automatically.
    """
    if os.path.exists(spec):
        return spec, None, None
    parts = spec.split(',')
    nums = []
    while len(parts) > 1 and re.fullmatch(r'\d+(\.\d*)?', parts[-1].strip()):
        nums.insert(0, float(parts.pop()))
    if len(nums) > 2:
        msg = f'Too many scale values in {spec!r}'
        raise argparse.ArgumentTypeError(msg)
    path = parts[0]
    if len(parts) > 2:
        msg = f'Cannot parse font specification {spec!r}'
        raise argparse.ArgumentTypeError(msg)
    if not os.path.exists(path):
        msg = f'Font file {path!r} does not exist'
        raise argparse.ArgumentTypeError(msg)
    xscale = nums[0] / 100 if nums else None
    yscale = nums[1] / 100 if len(nums) > 1 else xscale
    return path, xscale, yscale


def parse_exclusion(value):
    """
    Parse a hexadecimal code point or range, such as 4E00-9FFF or U+E000, or
    a Unicode block name prefix, such as "CJK Unified Ideographs Extension".

    :param value: the command line value.
    :returns: (low, high) inclusive, or a lower case block name prefix.
    """
    m = re.fullmatch(r'(?:U\+)?([0-9A-Fa-f]+)(?:-(?:U\+)?([0-9A-Fa-f]+))?', value.strip())
    if not m:
        prefix = value.strip().lower()
        if not any(block.lower().startswith(prefix) for block in unicode_blocks()):
            msg = f'{value!r} is neither a code point range nor the start of a Unicode block name'
            raise argparse.ArgumentTypeError(msg)
        return prefix
    low = int(m.group(1), 16)
    high = int(m.group(2), 16) if m.group(2) else low
    return min(low, high), max(low, high)


def is_excluded(cp, exclude):
    """
    Check if a code point matches any exclusion.

    :param cp: a code point.
    :param exclude: a list of values from parse_exclusion.
    :returns: True if excluded.
    """
    block = None
    for entry in exclude:
        if isinstance(entry, str):
            if block is None:
                block = fontunicodedata.block(cp).lower()
            if block.startswith(entry):
                return True
        elif entry[0] <= cp <= entry[1]:
            return True
    return False


def unicode_blocks():
    """
    List the Unicode block names.

    :returns: a set of block names.
    """
    return set(fontunicodedata.Blocks.VALUES)


def block_range(name):
    """
    Find the code point range of a Unicode block.

    :param name: a block name.
    :returns: (low, high) inclusive, or None if unknown.
    """
    starts, names = fontunicodedata.Blocks.RANGES, fontunicodedata.Blocks.VALUES
    for i, block in enumerate(names):
        if block == name:
            high = starts[i + 1] - 1 if i + 1 < len(starts) else 0x10FFFF
            return starts[i], high
    return None


def summarize_ranges(codepoints, limit=8):
    """
    Describe a set of code points as a short list of ranges.

    :param codepoints: an iterable of code points.
    :param limit: the maximum number of ranges to list.
    :returns: a string.
    """
    ranges = []
    for cp in sorted(codepoints):
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    text = ', '.join(f'{lo:04X}' if lo == hi else f'{lo:04X}-{hi:04X}' for lo, hi in ranges[:limit])
    if len(ranges) > limit:
        text += f', ... ({len(ranges) - limit} more ranges)'
    return text


def cell_count(cp):
    """
    Return the number of terminal cells a character occupies.

    :param cp: a code point.
    :returns: 1 or 2.
    """
    return 2 if unicodedata.east_asian_width(chr(cp)) in ('W', 'F') else 1


def is_addable(cp):
    """
    Check if a code point should get a glyph from a fallback font.

    :param cp: a code point.
    :returns: True if it is an assigned character a terminal might draw
        (private use characters included).
    """
    return unicodedata.category(chr(cp)) not in ('Cc', 'Cs', 'Cn')


def single_cell_width(font, cmap):
    """
    Determine the advance width of one terminal cell in a font.

    :param font: a TTFont.
    :param cmap: the font's best cmap.
    :returns: the advance width in font units.
    """
    hmtx = font['hmtx']
    # Each character's advance divided by the cells it uses in a terminal
    # votes for the cell width.
    votes = collections.Counter(
        hmtx[gname][0] / cell_count(cp) for cp, gname in cmap.items() if hmtx[gname][0])
    return round(votes.most_common(1)[0][0])


def cell_box(font, cmap, glyphset, ascent, descent):
    """
    Determine the vertical extent of a terminal cell in a font: the bounds
    of its full block character, if it has one, since monospace fonts draw
    that to fill the cell.

    :param font: a TTFont.
    :param cmap: the font's best cmap.
    :param glyphset: the font's glyph set.
    :param ascent, descent: the extent to use if there is no full block.
    :returns: (bottom, top) in font units.
    """
    if 0x2588 in cmap:
        pen = BoundsPen(glyphset)
        glyphset[cmap[0x2588]].draw(pen)
        if pen.bounds and pen.bounds[3] > pen.bounds[1]:
            return pen.bounds[1], pen.bounds[3]
    return -abs(descent), ascent


def embolden_contour(points, units):
    """
    Move each point of a closed contour outward along the bisector of its
    two edges, so every stroke becomes thicker by the given amount on each
    side.  On-curve and off-curve points are treated alike, which is a good
    approximation for small offsets.

    :param points: a list of (x, y) tuples, in TrueType order (outer
        contours clockwise with y up).
    :param units: the offset in font units; negative values make strokes
        thinner.
    :returns: a list of (x, y) tuples.
    """
    count = len(points)
    if count < 3:
        return list(points)
    limit = 3 * abs(units)
    result = []
    for i, (x, y) in enumerate(points):
        prev = (i - 1) % count
        while points[prev] == (x, y) and prev != i:
            prev = (prev - 1) % count
        after = (i + 1) % count
        while points[after] == (x, y) and after != i:
            after = (after + 1) % count
        if prev == i or after == i:
            result.append((x, y))
            continue
        ix, iy = x - points[prev][0], y - points[prev][1]
        ox, oy = points[after][0] - x, points[after][1] - y
        li, lo = math.hypot(ix, iy), math.hypot(ox, oy)
        ix, iy, ox, oy = ix / li, iy / li, ox / lo, oy / lo
        # The outward normal of a clockwise contour is to the left of the
        # direction of travel; the sum of the two edge normals divided by
        # 1 + cos(angle) is the miter offset.
        denom = 1 + ix * ox + iy * oy
        if denom < 1e-6:
            dx, dy = -iy * units, ix * units
        else:
            dx, dy = (-iy - oy) * units / denom, (ix + ox) * units / denom
            length = math.hypot(dx, dy)
            if length > limit:
                dx, dy = dx * limit / length, dy * limit / length
        result.append((x + dx, y + dy))
    return result


def signed_area(contours):
    """
    Compute the total signed area of a set of contours (positive when
    counterclockwise with y up).

    :param contours: a list of lists of (x, y) tuples.
    :returns: the area.
    """
    area = 0
    for points in contours:
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True):
            area += x0 * y1 - x1 * y0
    return area / 2


def fit_span(lo, hi, target_lo, target_hi):
    """
    Compute a scale and offset that move a span inside a target span.  The
    span is shifted if it fits and squeezed if it does not.

    :param lo, hi: the span.
    :param target_lo, target_hi: the span it must fit inside.
    :returns: (scale, offset) such that x' = x * scale + offset.
    """
    if hi - lo > target_hi - target_lo:
        scale = (target_hi - target_lo) / (hi - lo)
        return scale, target_lo - lo * scale
    if lo < target_lo:
        return 1.0, target_lo - lo
    if hi > target_hi:
        return 1.0, target_hi - hi
    return 1.0, 0.0


class Merger:
    def __init__(self, base_path, fit=True):
        """
        Load the base font.

        :param base_path: path to a TrueType font.
        :param fit: if True, keep added glyphs inside their cells.
        """
        self.font = TTFont(base_path)
        if 'glyf' not in self.font:
            msg = f'The base font {base_path} must have TrueType (glyf) outlines'
            raise ValueError(msg)
        if 'fvar' in self.font:
            msg = f'The base font {base_path} must not be a variable font'
            raise ValueError(msg)
        self.fit = fit
        self.upem = self.font['head'].unitsPerEm
        self.cmap = dict(self.font.getBestCmap())
        self.cell = single_cell_width(self.font, self.cmap)
        os2 = self.font['OS/2']
        self.box_lo, self.box_hi = cell_box(
            self.font, self.cmap, self.font.getGlyphSet(), os2.usWinAscent, os2.usWinDescent)
        self.glyf = self.font['glyf']
        self.hmtx = self.font['hmtx']
        self.order = list(self.font.getGlyphOrder())
        self.names = set(self.order)
        self.new_glyphs = []
        self.sources = [os.path.basename(base_path)]

    def glyph_name(self, cp):
        """
        Pick an unused glyph name for a code point.

        :param cp: a code point.
        :returns: a glyph name.
        """
        name = f'uni{cp:04X}' if cp <= 0xFFFF else f'u{cp:05X}'
        base, idx = name, 1
        while name in self.names:
            name = f'{base}.{idx}'
            idx += 1
        self.names.add(name)
        return name

    def convert_glyph(self, rec, adv, cp, info):
        """
        Scale and place one source glyph and convert it to TrueType.

        :param rec: a DecomposingRecordingPen with the source outline.
        :param adv: the source advance width.
        :param cp: the code point.
        :param info: per-source-font values.
        :returns: (glyph, advance width).
        """
        sx, sy = info['sx'], info['sy']
        ox = 0
        bounds_pen = ControlBoundsPen(None)
        rec.replay(bounds_pen)
        bounds = bounds_pen.bounds
        if not adv:
            # A zero-width glyph, such as a combining mark, is drawn over the
            # previous character; in a terminal it shares that cell.
            adv = info['cell']
            if bounds and bounds[0] + bounds[2] < 0:
                ox = adv
        width = self.cell * cell_count(cp)
        # Center the source advance box in the target cell(s).
        dx = (width - adv * sx) / 2 + ox * sx
        dy = info['dy']
        if bounds and self.fit:
            fx, offx = fit_span(bounds[0] * sx + dx, bounds[2] * sx + dx, 0, width)
            fy, offy = fit_span(
                bounds[1] * sy + dy, bounds[3] * sy + dy, self.box_lo, self.box_hi)
            sx, dx = sx * fx, dx * fx + offx
            sy, dy = sy * fy, dy * fy + offy
        tt_pen = TTGlyphPen(None)
        pen = Cu2QuPen(tt_pen, max_err=1.0, reverse_direction=info['cubic'])
        rec.replay(TransformPen(pen, (sx, 0, 0, sy, dx, dy)))
        glyph = tt_pen.glyph()
        glyph.recalcBounds(self.glyf)
        return glyph, width

    def wanted_codepoints(self, src_cmap, exclude):
        """
        List the code points a fallback font would add.

        :param src_cmap: the fallback font's best cmap.
        :param exclude: a list of values from parse_exclusion to skip.
        :returns: a sorted list of code points.
        """
        return sorted(
            cp for cp in src_cmap
            if cp not in self.cmap and is_addable(cp) and not is_excluded(cp, exclude))

    def list_blocks(self, paths, exclude):
        """
        Print how many characters each Unicode block would add, with the
        values that --exclude accepts for it.

        :param paths: the fallback font files.
        :param exclude: a list of values from parse_exclusion to skip.
        """
        blocks = collections.Counter()
        for path in paths:
            with TTFont(path, fontNumber=0, lazy=True) as src:
                for cp in self.wanted_codepoints(src.getBestCmap(), exclude):
                    self.cmap[cp] = None
                    blocks[fontunicodedata.block(cp)] += 1
        names = unicode_blocks()
        print(f'{"added":>7s}  {"--exclude range":16s} --exclude block name')
        for block, count in blocks.most_common():
            span = block_range(block)
            span = f'{span[0]:04X}-{span[1]:04X}' if span else ''
            # A block name is matched as a prefix, so one that starts
            # another block's name would exclude that block too.
            others = sorted(
                n for n in names if n != block and n.lower().startswith(block.lower()))
            note = f'  (as a prefix this also excludes {", ".join(others)})' if others else ''
            print(f'{count:7d}  {span:16s} "{block}"{note}')
        total = len(self.order) + sum(blocks.values())
        print(f'{total:7d}  glyphs in total, including {len(self.order)} from the base font '
              f'(limit {MaxGlyphs})')
        if total > MaxGlyphs:
            print(f'{total - MaxGlyphs:7d}  glyphs over the limit; exclude at least that many, '
                  'e.g. --exclude 4E00-9FFF or --exclude "Hangul Syllables"')

    def add_font(self, path, xscale, yscale, exclude):
        """
        Add glyphs for code points not yet covered.

        :param path: the font file.
        :param xscale, yscale: scale factors relative to matching the base
            font's units per em, or None to match the base font's cell.
        :param exclude: a list of values from parse_exclusion to skip.
        :returns: (number of glyphs added, set of code points that did not
            fit, (x, y) scale used relative to the units per em).
        """
        src = TTFont(path, fontNumber=0)
        src_cmap = src.getBestCmap()
        wanted = self.wanted_codepoints(src_cmap, exclude)
        room = MaxGlyphs - len(self.order) - len(self.new_glyphs)
        dropped = set(wanted[max(room, 0):])
        wanted = wanted[:max(room, 0)]
        glyphset = src.getGlyphSet()
        em = self.upem / src['head'].unitsPerEm
        src_cell = single_cell_width(src, src_cmap)
        src_lo, src_hi = cell_box(
            src, src_cmap, glyphset, src['hhea'].ascent, src['hhea'].descent)
        sx = em * xscale if xscale else self.cell / src_cell
        sy = em * yscale if yscale else (self.box_hi - self.box_lo) / (src_hi - src_lo)
        info = {
            'sx': sx,
            'sy': sy,
            # Align the top of the source cell with the top of the base cell.
            'dy': self.box_hi - src_hi * sy,
            'cell': src_cell,
            'cubic': 'CFF ' in src or 'CFF2' in src,
        }
        src_hmtx = src['hmtx']
        for cp in wanted:
            src_name = src_cmap[cp]
            rec = DecomposingRecordingPen(glyphset)
            glyphset[src_name].draw(rec)
            glyph, width = self.convert_glyph(rec, src_hmtx[src_name][0], cp, info)
            name = self.glyph_name(cp)
            self.new_glyphs.append((cp, name, glyph, width))
            self.cmap[cp] = name
        src.close()
        self.sources.append(os.path.basename(path))
        return len(wanted), dropped, (sx / em, sy / em)

    def stem_widths(self):
        """
        Measure the base font's stem widths from the straight edges of some
        sample glyphs: each distance between two parallel straight edges of
        a glyph is a candidate, and the common ones are the stems.

        :returns: a list of widths in font units.
        """
        limit = self.upem / 8
        counts = collections.Counter()
        for ch in 'lIHTEFiLmn-=+':
            name = self.cmap.get(ord(ch))
            if name is None or self.glyf[name].numberOfContours <= 0:
                continue
            glyph = self.glyf[name]
            coords, flags = list(glyph.coordinates), list(glyph.flags)
            start = 0
            for end in glyph.endPtsOfContours:
                points, on_curve = coords[start:end + 1], flags[start:end + 1]
                start = end + 1
                edges = {'x': [], 'y': []}
                for i, (x0, y0) in enumerate(points):
                    j = (i + 1) % len(points)
                    x1, y1 = points[j]
                    if not (on_curve[i] & 1 and on_curve[j] & 1):
                        continue
                    if x0 == x1 and abs(y1 - y0) >= limit:
                        edges['x'].append(x0)
                    elif y0 == y1 and abs(x1 - x0) >= limit:
                        edges['y'].append(y0)
                for values in edges.values():
                    for a in values:
                        for b in values:
                            if 0 < b - a <= limit:
                                counts[b - a] += 1
        if not counts:
            return []
        most = counts.most_common(1)[0][1]
        return sorted(w for w, c in counts.items() if c >= most / 4)

    def embolden(self, units, adjust_cvt=False, per_glyph=None):
        """
        Thicken every stroke of the base font's glyphs.  Windows renders a
        font named "Courier New" with extra weight under ClearType (in both
        GDI and DirectWrite); a merged font under another name does not get
        that, so this approximates it in the outlines.  Advance widths are
        unchanged, so glyphs grow symmetrically within their cells.

        At small sizes the hinting snaps stems to the widths stored in the
        cvt table, which mostly undoes the change; widening the cvt entries
        that match the font's stem widths makes small sizes match better,
        but stems then become two pixels wide at a smaller size than in the
        original font (at 1.5 pixels, so for Courier New with 10 units at
        30 instead of 37 pixels per em), which is much too heavy there.

        :param units: font units added on each side of each stroke.
        :param adjust_cvt: if True, also widen matching cvt entries.
        :param per_glyph: an optional dictionary of glyph name to units,
            for glyphs that need a different amount (diagonal strokes are
            hinted differently from stems and need more).
        :returns: (stem widths found, number of cvt entries adjusted).
        """
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates

        widths = []
        adjusted = 0
        if adjust_cvt and 'cvt ' in self.font:
            widths = self.stem_widths()
            cvt = self.font['cvt '].values
            for i, value in enumerate(cvt):
                if any(abs(abs(value) - w) <= 1 for w in widths):
                    cvt[i] = round(value + 2 * units * (1 if value > 0 else -1))
                    adjusted += 1
        per_glyph = per_glyph or {}
        for name in self.order:
            glyph = self.glyf[name]
            amount = per_glyph.get(name, units)
            if glyph.numberOfContours <= 0 or not amount:
                continue
            coords = list(glyph.coordinates)
            contours = []
            start = 0
            for end in glyph.endPtsOfContours:
                contours.append(coords[start:end + 1])
                start = end + 1
            # Outer contours are clockwise in TrueType; if the glyph as a
            # whole is drawn the other way, expand in the other direction.
            offset = -amount if signed_area(contours) > 0 else amount
            new_coords = []
            for points in contours:
                new_coords.extend(embolden_contour(points, offset))
            old_xmin = glyph.xMin
            glyph.coordinates = GlyphCoordinates(
                [(round(x), round(y)) for x, y in new_coords])
            glyph.recalcBounds(self.glyf)
            advance, lsb = self.hmtx[name]
            self.hmtx[name] = (advance, lsb + glyph.xMin - old_xmin)
        return widths, adjusted

    def build_cmap(self):
        """Replace the cmap with Unicode subtables covering every mapping."""
        cmap = self.font['cmap']
        bmp = {cp: name for cp, name in self.cmap.items() if cp <= 0xFFFF}
        subtables = []
        for platform, encoding, fmt, mapping in [
                (0, 3, 4, bmp), (3, 1, 4, bmp), (0, 4, 12, self.cmap), (3, 10, 12, self.cmap)]:
            if fmt == 12 and len(mapping) == len(bmp):
                continue
            sub = CmapSubtable.newSubtable(fmt)
            sub.platformID, sub.platEncID, sub.language = platform, encoding, 0
            sub.cmap = dict(mapping)
            subtables.append(sub)
        # Keep a format 14 (variation sequence) subtable; it still refers to
        # valid base glyphs.
        subtables.extend(sub for sub in cmap.tables if sub.format == 14)
        cmap.tables = subtables

    def set_names(self, family):
        """
        Rename the font family.

        :param family: the new family name.
        """
        name = self.font['name']
        style = name.getDebugName(2) or 'Regular'
        version = name.getDebugName(5) or 'Version 1.0'
        for name_id in FamilyNameIDs:
            name.removeNames(nameID=name_id)
        ps_name = re.sub(r'[^A-Za-z0-9]', '', family) + '-' + re.sub(r'[^A-Za-z0-9]', '', style)
        unique = f'{family} {style}: merged {datetime.datetime.now(datetime.UTC).date()}'
        description = 'Merged from ' + ', '.join(self.sources)
        for platform, encoding, lang in [(3, 1, 0x409), (1, 0, 0)]:
            for name_id, value in [
                    (1, family), (2, style), (3, unique), (4, f'{family} {style}'),
                    (5, version), (6, ps_name), (10, description)]:
                name.setName(value, name_id, platform, encoding, lang)

    def save(self, path, family):
        """
        Assemble and write the merged font.

        :param path: the output file.
        :param family: the new family name.
        """
        self.new_glyphs.sort()
        new_order = self.order + [name for _, name, _, _ in self.new_glyphs]
        for _, name, glyph, width in self.new_glyphs:
            self.glyf.glyphs[name] = glyph
            self.hmtx.metrics[name] = (width, getattr(glyph, 'xMin', 0))
        self.font.setGlyphOrder(new_order)
        self.glyf.setGlyphOrder(new_order)
        self.build_cmap()
        self.set_names(family)
        for tag in DropTables:
            if tag in self.font:
                del self.font[tag]
        # Glyph names for this many glyphs only add size.
        self.font['post'].formatType = 3.0
        self.font['post'].isFixedPitch = 1
        os2 = self.font['OS/2']
        os2.recalcUnicodeRanges(self.font)
        os2.panose.bProportion = 9  # monospaced
        head_flags = self.font['head'].flags
        self.font.save(path)
        # Recalculating the bounding boxes on save also sets head flag bit 1
        # (left sidebearing at x=0); keep the base font's flags instead.
        saved = TTFont(path, recalcBBoxes=False)
        if saved['head'].flags != head_flags:
            saved['head'].flags = head_flags
            saved.save(path)
        saved.close()


def main():
    parser = argparse.ArgumentParser(
        description='Merge fallback fonts into a monospace TrueType base font, producing a '
        'single .ttf for Windows and Linux.  Each later font only adds characters that no '
        'earlier font has.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Fonts are FILE[,FACE][,X[,Y]], the layout of a Windows FontLink entry.  FACE is\n'
        "ignored.  X and Y are percentages for scaling that font's glyphs horizontally and\n"
        "vertically after matching the base font's units per em (Y defaults to X).  Without\n"
        'them, the font is scaled so its cell (its full block character) matches the base\n'
        "font's; the scale used is printed.\n\n"
        'Example:\n'
        "  merge_fonts.py --out 'Courier DWM' cour.ttf JuliaMono-Regular.ttf,100,98 \\\n"
        '      NotoEmoji-Regular.ttf UnifontExMono.ttf,120,114 unifont-16.0.01.otf,120,114')
    parser.add_argument(
        '--out', required=True, help='Family name of the merged font, such as "Courier DWM".')
    parser.add_argument(
        '--file', help='Output path.  Defaults to the family name without spaces plus .ttf.')
    parser.add_argument(
        '--exclude', action='append', type=parse_exclusion, default=[], metavar='RANGE|BLOCK',
        help='Do not add these characters from the fallback fonts: a hexadecimal code point '
        'range, such as 4E00-9FFF, or the start of a Unicode block name (case insensitive), '
        'such as "CJK Unified Ideographs Extension" or "Linear B".  May be repeated.  Useful '
        'to make room when the 65535 glyph limit is reached.')
    parser.add_argument(
        '--embolden', type=float, default=0, metavar='UNITS',
        help='Thicken each stroke of the base font by this many font units on each side.  '
        'Windows draws a font named "Courier New" heavier under ClearType than the same '
        'outlines under any other name; this approximates that extra weight.  For Courier '
        'New, 20 gives the same total ink as the real font at 17 pixels per em (13 points at '
        '96 dpi), and 8 at 35 pixels per em (13 points at 192 dpi).')
    parser.add_argument(
        '--embolden-chars', action='append', default=[], metavar='CHARS=UNITS',
        help='Use a different number of units for these characters, given as a string of '
        'characters or U+ code points separated by spaces.  Diagonal strokes are hinted '
        'differently from stems and need more; for Courier New at 17 pixels per em, '
        '"/\\" wants about 25 and "yvAV" about 30 when the rest uses 20.  May be repeated.')
    parser.add_argument(
        '--embolden-cvt', action='store_true',
        help='With --embolden, also widen the hinting control values that match the base '
        "font's stem widths.  Sizes up to about 24 pixels per em then match Courier New "
        'much better, but stems turn two pixels wide from about 30 pixels per em instead '
        'of 37, which is far too heavy until 37.')
    parser.add_argument(
        '--list-blocks', action='store_true',
        help='List how many characters each Unicode block would add, with the code point '
        'range and block name that --exclude accepts for it, then exit.  Blocks already '
        'excluded are left out, so this shows the effect of --exclude options too.')
    parser.add_argument(
        '--no-fit', dest='fit', action='store_false',
        help='Do not shift or squeeze added glyphs to keep them inside their cells.')
    parser.add_argument('base', help='The base TrueType font, such as cour.ttf.')
    parser.add_argument(
        'fonts', nargs='*', type=parse_font_spec,
        help='Fallback fonts, in order of preference, as FILE[,FACE][,X[,Y]].')
    args = parser.parse_args()
    out_path = args.file or re.sub(r'\s+', '', args.out) + '.ttf'
    merger = Merger(args.base, fit=args.fit)
    if args.list_blocks:
        merger.list_blocks([path for path, _, _ in args.fonts], args.exclude)
        return
    print(f'{args.base}: {len(merger.cmap)} characters, {len(merger.order)} glyphs')
    if args.embolden or args.embolden_chars:
        per_glyph = {}
        for spec in args.embolden_chars:
            chars, _, value = spec.rpartition('=')
            try:
                value = float(value)
            except ValueError:
                parser.error(f'--embolden-chars {spec!r} must end with =UNITS')
            if re.fullmatch(r'(U\+[0-9A-Fa-f]+\s*)+', chars.strip()):
                cps = [int(cp[2:], 16) for cp in chars.split()]
            else:
                cps = [ord(ch) for ch in chars]
            for cp in cps:
                if cp in merger.cmap:
                    per_glyph[merger.cmap[cp]] = value
        widths, adjusted = merger.embolden(args.embolden, args.embolden_cvt, per_glyph)
        print(f'Emboldened by {args.embolden:g} units per side', end='')
        if per_glyph:
            print(f' ({len(per_glyph)} glyphs with their own value)', end='')
        if args.embolden_cvt:
            print(f'; stem widths {", ".join(str(w) for w in widths) or "not found"}, '
                  f'{adjusted} cvt entries widened', end='')
        print()
    for path, xscale, yscale in args.fonts:
        added, dropped, scale = merger.add_font(path, xscale, yscale, args.exclude)
        print(f'{path},{scale[0] * 100:.0f},{scale[1] * 100:.0f}: added {added} characters')
        if dropped:
            print(f'  {len(dropped)} characters did not fit in the {MaxGlyphs} glyph limit: '
                  f'{summarize_ranges(dropped)}')
    merger.save(out_path, args.out)
    print(f'Saved {out_path}: {len(merger.cmap)} characters, '
          f'{len(merger.order) + len(merger.new_glyphs)} glyphs')


if __name__ == '__main__':
    main()
