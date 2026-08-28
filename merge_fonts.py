#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fonttools>=4.56.0",
# ]
# ///
"""
merge_fonts.py - Merge multiple TTF/OTF fonts into a single unified TTF font.

Usage:
    python3 merge_fonts.py --out=merged.ttf --name="My Font" base.ttf input1.ttf input2.ttf
"""

import argparse
import os
import struct
from collections import defaultdict

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import cmap_classes
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates
from fontTools.ttLib.tables._n_a_m_e import NameRecord
from fontTools.ttLib.tables.O_S_2f_2 import table_O_S_2f_2


def build_cmap_map(src):
    cmap_map = {}
    for t in src['cmap'].tables:
        if t.format == 4:
            for code, gn in t.cmap.items():
                cmap_map[code] = gn
        elif t.format == 12:
            for code, gn in t.cmap.items():
                cmap_map[code] = gn
    return cmap_map


def scale_glyph(g, src_upem, target_upem):
    coords = getattr(g, 'coordinates', None)
    if not coords:
        return Glyph()
    scale = target_upem / src_upem
    new_g = Glyph()
    new_g.numberOfContours = g.numberOfContours if hasattr(g, 'numberOfContours') else 0
    new_g.coordinates = GlyphCoordinates([(int(x * scale), int(y * scale)) for x, y in coords])
    new_g.flags = g.flags if hasattr(g, 'flags') else []
    new_g.endPtsOfContours = g.endPtsOfContours if hasattr(g, 'endPtsOfContours') else []
    new_g.instructions = g.instructions if hasattr(g, 'instructions') else b''
    new_g.program = g.program
    xs = [p[0] for p in new_g.coordinates]
    ys = [p[1] for p in new_g.coordinates]
    new_g.xMin, new_g.yMin = min(xs), min(ys)
    new_g.xMax, new_g.yMax = max(xs), max(ys)
    return new_g


def process_font(fpath, name_to_glyph, target_upem, hmtx, adv_widths, unicode_to_glyph):
    if not os.path.exists(fpath):
        print(f'  Skipping (not found): {fpath}')
        return
    with TTFont(fpath) as src:
        if 'cff' in src or 'glyf' not in src:
            print(f'  Skipping (no glyf / CFF font): {fpath}')
            return
        cmap_map = build_cmap_map(src)
        # We iterate glyphs once, then map unicode points
        for gn in src.getGlyphOrder():
            if gn == '.notdef' or gn in name_to_glyph:
                continue
            g = src['glyf'].get(gn)
            if not g or not g.numberOfContours:
                continue
            ng = scale_glyph(g, src['head'].unitsPerEm, target_upem)
            name_to_glyph[gn] = ng

            m = src['hmtx'].metrics.get(gn, (0, 0))
            hmtx[gn] = (int(m[0] * target_upem / src['head'].unitsPerEm), m[1])
            adv_widths.append(int(m[0]))
        for code, mapped_gn in cmap_map.items():
            if mapped_gn in name_to_glyph:
                unicode_to_glyph[code] = [(mapped_gn, name_to_glyph[mapped_gn])]


class FixedMaxp:
    def compile(self, ttFont):
        # Format 1.0 maxp table: tableVersion(L), numGlyphs(L) + 32
        # bytes of H fields
        return struct.pack('<L', 0x00010000) + struct.pack(
            '<L', len(ttFont.getGlyphOrder())) + struct.pack(
                '<16H', 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


class FixedHhea:
    def compile(self, ttFont):
        # Manually pack hheaFormat to avoid ushort crash on
        # numberOfHMetrics
        return struct.pack(
            '<L3hH3h3h4h1h1H', 0x00010000, 1000, -973, 1362, 10000,
            -1000, 1000, 10000, 1, 0, 0, 0, 0, 0, 0, 1, 65535)


def process_base(fpath, name_to_glyph, target_upem, hmtx, adv_widths, unicode_to_glyph):
    with TTFont(fpath) as src:
        name_to_glyph['.notdef'] = src['glyf']['.notdef']
        hmtx['.notdef'] = src['hmtx']['.notdef']
        adv_widths.extend(src['hmtx']['.notdef'])
        base_cmap_map = build_cmap_map(src)

        for gn in src.getGlyphOrder():
            if gn == '.notdef':
                continue
            g = src['glyf'].get(gn)
            if not g:
                continue
            ng = scale_glyph(g, src['head'].unitsPerEm, target_upem)
            name_to_glyph[gn] = ng

            m = src['hmtx'].metrics.get(gn, (0, 0))
            hmtx[gn] = (int(m[0] * target_upem / src['head'].unitsPerEm), m[1])
            adv_widths.append(int(m[0]))
            for code, mapped_gn in base_cmap_map.items():
                if mapped_gn == gn:
                    unicode_to_glyph[code] = [(gn, ng)]


def main():
    parser = argparse.ArgumentParser(description='Merge multiple TTF/OTF fonts into a single TTF.')
    parser.add_argument('-o', '--out', default='merged.ttf', help='Output TTF file path')
    parser.add_argument('-n', '--name', default='Merged Courier',
                        help="Font family name (e.g. 'Merged Courier')")
    parser.add_argument('base', help='Base font file (e.g. the standard Courier New)')
    parser.add_argument('inputs', nargs='*',
                        help='Additional fonts to merge (e.g. JuliaMono, NotoEmoji)')
    args = parser.parse_args()

    print(f'Loading base font: {args.base}')
    with TTFont(args.base) as h_src:
        target_upem = h_src['head'].unitsPerEm
        out = TTFont()
        out['head'] = h_src['head']

        # Handle hhea gracefully (some fonts may miss it)
        if 'hhea' in h_src:
            out['hhea'] = h_src['hhea']
        else:
            out['hhea'] = newTable('hhea')
            out['hhea'].tableVersion = 0x10000
            out['hhea'].ascent, out['hhea'].descent = target_upem, -int(target_upem * 0.2)
            out['hhea'].lineGap = int(target_upem * 0.2)
            out['hhea'].maxAdvanceWidth = target_upem

        out['OS/2'] = h_src['OS/2']
        out['post'] = newTable('post')
        out['post'].formatType = 3
        out['post'].italicAngle, out['post'].underlineThickness = 0, 50
        out['post'].underlinePosition, out['post'].isFixedPitch = -100, 0
        out['post'].minMemType42, out['post'].maxMemType42 = 0x50000, 0xE0000
        out['post'].minMemType1, out['post'].maxMemType1 = 0x10000, 0x20000

        name_to_glyph = {'.notdef': None}
        unicode_to_glyph = defaultdict(list)
        hmtx, adv_widths = {}, []

        print(f'Processing base: {args.base}')
        process_font(args.base, name_to_glyph, target_upem, hmtx, adv_widths, unicode_to_glyph)
        process_base(args.base, name_to_glyph, target_upem, hmtx, adv_widths, unicode_to_glyph)

        for fpath in args.inputs:
            print(f'Processing: {fpath}')
            process_font(fpath, name_to_glyph, target_upem, hmtx, adv_widths, unicode_to_glyph)

        glyph_order = ['.notdef'] + [gn for gn in name_to_glyph if gn != '.notdef']
        out.setGlyphOrder(glyph_order)
        # 1. Glyf table
        glyf = newTable('glyf')
        glyf.glyphs = {}
        glyf.glyphOrder = glyph_order
        for gn in glyph_order:
            glyf.glyphs[gn] = name_to_glyph.get(gn, Glyph())
        out['glyf'] = glyf
        # 2. Hmtx table
        hmtx_t = newTable('hmtx')
        hmtx_t.metrics = {}
        for gn in glyph_order:
            hmtx_t.metrics[gn] = hmtx.get(gn, (0, 0))
        out['hmtx'] = hmtx_t
        # MANUALLY ADD 'loca' TABLE (Required by fontconfig!)
        out['loca'] = newTable('loca')
        # 3. CMap table (Format 12)
        final_cmap = {code: unicode_to_glyph[code][0][0]
                      for code in sorted(unicode_to_glyph.keys())}
        cmap = newTable('cmap')
        subtable = cmap_classes[12]()
        subtable.format, subtable.cmap = 12, final_cmap
        # Mandatory attributes for cmap format 12
        subtable.platformID = 3
        subtable.encodingID = 1
        subtable.platEncID = 1
        subtable.language = 0
        cmap.tableFormat, cmap.version, cmap.tableVersion = 12, 0, 0
        cmap.numTables = 1
        cmap.tables = [subtable]
        cmap.cmap = {}
        out['cmap'] = cmap
        # 4. OS/2 stats
        os2 = out['OS/2']
        os2.fsFirstCharIndex = min(final_cmap.keys()) if final_cmap else 32
        os2.fsLastCharIndex = max(final_cmap.keys()) if final_cmap else 255
        os2.sTypoAscender, os2.sTypoDescender = target_upem, -int(target_upem * 0.2)
        os2.usWinAscent, os2.usWinDescent = target_upem, int(target_upem * 0.2)
        if adv_widths:
            os2.xAvgCharWidth = int(sum(adv_widths) / len(adv_widths))
        # 5. Name table
        nameTable = newTable('name')
        rec1 = NameRecord()
        rec1.platformID = 3
        rec1.platEncID = 1
        rec1.langID = 0x0409
        rec1.nameID = 1
        rec1.string = args.name.encode('utf-16-be')
        rec4 = NameRecord()
        rec4.platformID = 3
        rec4.platEncID = 1
        rec4.langID = 0x0409
        rec4.nameID = 4
        rec4.string = (args.name + ' Regular').encode('utf-16-be')
        nameTable.names = [rec1, rec4]
        out['name'] = nameTable
        out_path = args.out
        # === PATCHES FOR fontTools 4.63 + >65k GLYPH LIMIT ===
        # These bypass internal fontTools crashes when numGlyphs > 65535
        out['maxp'] = FixedMaxp()
        out['maxp'].numGlyphs = len(glyph_order)
        out['hhea'] = FixedHhea()
        # Patch TTFont __getitem__ to safely inject missing platformID on cmap
        # tables
        original_getitem = TTFont.__getitem__

        def patched_getitem(self, key):
            res = original_getitem(self, key)
            if key == 'cmap' and not hasattr(res, 'platformID'):
                res.platformID, res.encodingID = 3, 1
                res.tableID = (3, 1, 0)
            return res
        TTFont.__getitem__ = patched_getitem

        # Patch OS/2 updateFirstAndLastCharIndex to prevent cmap lookup errors
        original_OS2_update = table_O_S_2f_2.updateFirstAndLastCharIndex

        def patched_OS2_update(self, ttFont):
            cmap_obj = ttFont.get('cmap')
            if cmap_obj is not None:
                try:
                    _ = cmap_obj.tables[0].platformID
                except (AttributeError, IndexError):
                    if hasattr(cmap_obj, 'tables') and cmap_obj.tables:
                        cmap_obj.tables[0].platformID = 3
                        cmap_obj.tables[0].platEncID = 1
                        cmap_obj.tables[0].language = 0
            return original_OS2_update(self, ttFont)
        table_O_S_2f_2.updateFirstAndLastCharIndex = patched_OS2_update

        out.save(out_path)
        print(f'Successfully saved merged font to {out_path}.')
        print(f'  Glyphs: {len(glyph_order)}')
        print(f'  Unicode Points: {len(final_cmap)}')


if __name__ == '__main__':
    main()
