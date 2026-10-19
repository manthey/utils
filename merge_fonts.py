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

Fonts are given as FILE[,FACE][,X[,Y]], the same layout as a Windows
FontLink SystemLink entry.  FACE is ignored.  X and Y are percentages
applied after scaling to the base font's units per em (Y defaults to X);
without them the scale is chosen to match the cells.

Usage:
    merge_fonts.py --out 'Courier DWM' cour.ttf JuliaMono-Regular.ttf,100,98 \
        NotoEmoji-Regular.ttf UnifontExMono.ttf,120,114 unifont-16.0.01.otf,120,114
"""

import argparse
import collections
import datetime
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
        Print how many characters each Unicode block would add.

        :param paths: the fallback font files.
        :param exclude: a list of values from parse_exclusion to skip.
        """
        blocks = collections.Counter()
        for path in paths:
            with TTFont(path, fontNumber=0, lazy=True) as src:
                for cp in self.wanted_codepoints(src.getBestCmap(), exclude):
                    self.cmap[cp] = None
                    blocks[fontunicodedata.block(cp)] += 1
        for block, count in blocks.most_common():
            print(f'{count:7d}  {block}')
        total = len(self.order) + sum(blocks.values())
        print(f'{total:7d}  glyphs in total, including {len(self.order)} from the base font '
              f'(limit {MaxGlyphs})')

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
        '--list-blocks', action='store_true',
        help='List how many characters each Unicode block would add, then exit.')
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
