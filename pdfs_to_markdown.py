#!/usr/bin/env python3
# /// script
# requires-python = '>=3.12'
# dependencies = [
#   'docling',
#   'interruptible',
#   'lingua-language-detector',
#   'numpy',
#   'openai',
#   'pillow',
#   'requests',
# ]
# ///
# This can be run via something like
# uv run --index-strategy unsafe-best-match --with torch==2.13.0+cu132
# --index https://download.pytorch.org/whl/cu132 ..scriptname.. ..options..

import argparse
import base64
import ctypes
import functools
import io
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import interruptible
import numpy as np
import requests

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logging.getLogger('transformers').setLevel(logging.ERROR)
logging.getLogger('torch').setLevel(logging.ERROR)
logging.getLogger('RapidOCR').setLevel(logging.ERROR)
os.environ.setdefault('TRANSFORMERS_VERBOSITY', 'warning')

FORMULA_PROMPT = (
    'Transcribe the mathematical formula in this image using standard LaTeX '
    'notation. Wrap the entire equation strictly inside $$ ... $$. Do not '
    'include any surrounding text, explanations, or code blocks. Return ONLY '
    'the string content between the dollar signs.'
)
PICTURE_PROMPT = (
    'Describe this figure or image in precise, accurate detail.  Convey the '
    'composition of the image.  Include any visible text, axis labels, '
    'legends, numerical values, the overall meaning of the image, and '
    'interesting salient details.  Use no more than 250 words.'
)
FAIR_COPY_PROMPT = (
    'Clean up this OCR text by fixing typos, character recognition errors, '
    'and formatting issues while preserving the original meaning and '
    'structure. Specifically, convert any "long s" (ſ) typically found in '
    'older texts to standard lowercase "s". Output only the cleaned text '
    'without explanations.'
)
TRANSLATE_PROMPT = (
    'You are an expert translator for all languages, subjects, and eras. '
    'Translate this text to English. Preserve all markdown formatting, '
    'headers, image/figure markers, tables, and code blocks exactly as they '
    'are. Output only the translated text without explanations; if the source '
    'text is not English, you must produce an English translation.'
)
#: Substrings that identify a transient, timeout-like failure worth retrying
#: indefinitely when the user has not capped retries.  Matched
#: case-insensitively against the string form of the exception.
TRANSIENT_ERROR_MARKERS = (
    'timed out',
    'timeout',
    'read timed out',
    'connection reset',
    'connection aborted',
    'connection error',
    'temporarily unavailable',
    'server disconnected',
    'bad gateway',
    'service unavailable',
    'gateway timeout',
)


def is_transient_error(error):
    """Return True for timeout/connection errors that are worth retrying."""
    if isinstance(error, KeyboardInterrupt):
        return False
    text = str(error).lower()
    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


def run_with_retries(func, *, retries, base_delay=1.0, max_delay=60.0,
                     description='request', log=None):
    """Call ``func`` with retries for transient errors.

    Args:
        func: Zero-argument callable to invoke.
        retries: Maximum number of retries for transient errors, or ``None``
            for unlimited retries (timeouts will be retried forever).
        base_delay: Initial backoff delay in seconds.
        max_delay: Maximum backoff delay in seconds.
        description: Label used in log messages.
        log: Logger to use (defaults to the module logger).

    Returns:
        Whatever ``func`` returns.  Non-transient exceptions are raised
        immediately; transient errors are retried until they succeed or the
        retry budget is exhausted, at which point the last error is raised.

    """
    log = log or logger
    attempt = 0
    delay = base_delay
    while True:
        try:
            return func()
        except Exception as error:
            if not is_transient_error(error):
                raise
            attempt += 1
            if retries is not None and attempt > retries:
                raise
            wait = min(delay, max_delay)
            progress = (f'attempt {attempt}' if retries is None
                        else f'attempt {attempt} / {retries + 1}')
            log.warning('%s: %s (%s, retrying in %.1fs)',
                        description, error, progress, wait)
            time.sleep(wait)
            delay = min(delay * 2, max_delay)


def chat_create_process(client, stop_after=None, **kwargs):
    stream = client.chat.completions.create(**kwargs)
    result = []
    usage = 0
    for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            result.append(delta.content)
            if '</think>' in result[-1]:
                result[:-1] = []
                result[-1] = result[-1].split('</think>')[-1]
            if stop_after and len(''.join(result)) > stop_after:
                break
        if chunk.usage is not None:
            usage += chunk.usage.total_tokens
    return ''.join(result), usage


def chat_create_with_reasoning(client, level='low', **kwargs):
    """Create a chat completion, trying with a reasoning_effort first."""
    kwargs = kwargs.copy()
    kwargs['stream'] = True
    kwargs['stream_options'] = {'include_usage': True}
    kwargs['reasoning_effort'] = level
    try:
        return chat_create_process(client, **kwargs)
    except Exception as exc:
        # If reasoning effort is unsupported, retry without it
        if 'reasoning_effort' in str(exc).lower() or 'thinking' in str(exc).lower():
            kwargs.pop('reasoning_effort')
            return chat_create_process(client, **kwargs)
        raise exc


def image_to_data_url(image):
    buffer = io.BytesIO()
    image = image.convert('L' if image.mode in {'L', 'LA'} else 'RGB')
    image.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f'data:image/png;base64,{encoded}'


def query_vision_model(client, model, image, prompt, **kwargs):
    content, tokens = chat_create_with_reasoning(
        client,
        model=model,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': image_to_data_url(image)}},
            ],
        }],
        level='none',
        **kwargs,
    )
    return content, tokens


def query_llm(client, model, prompt, **kwargs):
    """Query an LLM (text-only) and return its text response."""
    content, tokens = chat_create_with_reasoning(
        client,
        model=model,
        messages=[{'role': 'user', 'content': prompt}],
        **kwargs,
    )
    return content, tokens


def is_ocr_used(result):
    """Detect if OCR was used by checking docling confidence scores."""
    conf = getattr(result, 'confidence', None)
    pages_conf = getattr(conf, 'pages', None) if conf else None
    if isinstance(pages_conf, dict):
        for score in pages_conf.values():
            ocr = getattr(score, 'ocr_score', 0)
            if ocr is not None and float(ocr) > 0:
                return True
    # Fallback: check parsed_page flag or page metadata
    if hasattr(result, 'pages'):
        for page in result.pages:
            parsed = getattr(page, 'parsed_page', None)
            if parsed and getattr(parsed, 'has_ocr', False):
                return True
            if getattr(page, 'has_ocr', False):
                return True
    return False


def chunk_text(text, limit=None):
    """Split markdown text into logical chunks based on headers or paragraphs."""
    if not text:
        return []

    estimated_chars_per_token = 4
    token_limit = limit or 8192
    target_bytes = max(int(token_limit * estimated_chars_per_token // 2), 6000)

    # Split by Markdown headers to respect document structure
    segments = re.split(r'(^#{1,6}\s+.*$)', text, flags=re.MULTILINE)
    merged_segments = []
    header_pattern = re.compile(r'^#{1,6}\s+')
    for i, seg in enumerate(segments):
        if not seg.strip():
            continue
        if header_pattern.match(seg):
            chunk = f'\n\n{seg}\n'
            # Merge with the next paragraph block if it exists
            if i + 1 < len(segments):
                chunk += segments[i + 1].strip() + '\n'
            merged_segments.append(chunk)
        else:
            merged_segments.append(seg.strip() + '\n\n')
    chunks, current_chunk, current_len = [], '', 0

    def try_finalize(chunks_list, curr_str):
        if len(curr_str) > 50 and curr_str.strip():
            chunks_list.append(curr_str)

    for segment in merged_segments:
        seg_len = len(segment.encode('utf-8'))
        if seg_len > target_bytes:
            sub_blocks = re.split(r'\n{1,3}', segment)
            for blk in sub_blocks:
                if not blk.strip():
                    continue
                blk_len = len(blk.encode('utf-8'))
                need_new_chunk = (current_chunk == '') or (current_len + blk_len > target_bytes)
                if need_new_chunk:
                    try_finalize(chunks, current_chunk)
                    current_chunk, current_len = blk, blk_len
                else:
                    current_chunk += '\n' + blk
                    current_len += blk_len + 1
            continue
        # Respect byte limit when logically merging segments
        need_new_segment = (not current_chunk) or (current_len + seg_len > target_bytes)
        if need_new_segment:
            try_finalize(chunks, current_chunk)
            current_chunk, current_len = segment, seg_len
        else:
            current_chunk += '\n\n' + segment
            current_len += 2 + seg_len
    try_finalize(chunks, current_chunk)
    return chunks or [text]


@functools.cache
def get_lang_detector():
    import lingua

    lang = lingua.Language.all() - {
        lingua.Language.AZERBAIJANI, lingua.Language.SOTHO,
        lingua.Language.TSONGA, lingua.Language.YORUBA}
    return lingua.LanguageDetectorBuilder.from_languages(*tuple(lang)).build()


def detect_language(text):
    import lingua

    if len(text.strip()) <= 50:
        return 'English'
    chunks = chunk_text(text)
    detector = get_lang_detector()
    results = {}
    for chunk in chunks:
        if len(chunk.strip()) <= 50:
            continue
        lang = detector.detect_language_of(chunk)
        if lang:
            results[lang] = results.get(lang, 0) + 1
    logger.info('Detect language %r', results)
    lang = None
    if len(results):
        lang = max(results, key=results.get)
        if results[lang] == results.get(lingua.Language.ENGLISH, 0):
            lang = lingua.Language.ENGLISH
    if not lang:
        return 'English'
    return lang.name.capitalize()


def process_ocr_text(client, model, text, parallel=1, retries=None):
    """Apply fair copy processing to clean up OCR text."""
    chunks = chunk_text(text)
    total_tokens = 0

    def process_chunk(i_chunk):
        i, chunk = i_chunk
        logger.info('OCR fair copy chunk %d / %d (%d)', i + 1, len(chunks), len(chunk))
        lasterr = ''
        minlen, maxlen = len(chunk) // 4, len(chunk) * 3 // 2
        for attempt in range(5, -1, -1):
            try:
                cleaned, tokens = run_with_retries(
                    lambda: query_llm(
                        client, model, f'{FAIR_COPY_PROMPT}\n\n{chunk}',
                        stop_after=maxlen + 1),
                    retries=retries, description=f'OCR fair copy chunk {i + 1}',
                    log=logger)
                if attempt and (len(cleaned) < minlen or len(cleaned) > maxlen):
                    continue
                return i, cleaned, tokens, None
            except Exception as err:
                lasterr = err
        logger.warning('OCR fair copy chunk %d failed: %s', i, lasterr)
        return i, chunk, 0, lasterr

    indexed_chunks = list(enumerate(chunks))
    results = [None] * len(indexed_chunks)
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        futures = {executor.submit(process_chunk, i_chunk): i_chunk
                   for i_chunk in indexed_chunks}
        for future in as_completed(futures):
            result_i, result_text, tokens, error = future.result()
            results[result_i] = result_text
            total_tokens += tokens
    return '\n'.join(results), total_tokens


def process_translation(client, model, text, src_lang=None, parallel=1, retries=None):
    if src_lang.lower() == 'english':
        return text, 0
    chunks = chunk_text(text)
    total_tokens = 0

    def translate_chunk(i_chunk):
        i, chunk = i_chunk
        logger.info('Translation chunk %d / %d (%d)', i + 1, len(chunks), len(chunk))
        lasterr = ''
        minlen, maxlen = len(chunk) // 4, len(chunk) * 2
        for attempt in range(5, -1, -1):
            try:
                translated, tokens = run_with_retries(
                    lambda: query_llm(
                        client, model,
                        f'{TRANSLATE_PROMPT}\n\nOriginal ({src_lang}):\n{chunk}',
                        stop_after=maxlen + 1),
                    retries=retries, description=f'Translation chunk {i + 1}',
                    log=logger)
                if attempt and (len(translated) < minlen or len(translated) > maxlen):
                    continue
                return i, translated, tokens, None
            except Exception as err:
                lasterr = err
        logger.warning('Translation chunk %d failed: %s', i, lasterr)
        return i, chunk, 0, lasterr

    indexed_chunks = list(enumerate(chunks))
    results = [None] * len(indexed_chunks)
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        futures = {executor.submit(translate_chunk, i_chunk): i_chunk
                   for i_chunk in indexed_chunks}
        for future in as_completed(futures):
            result_i, result_text, tokens, error = future.result()
            results[result_i] = result_text
            total_tokens += tokens
    return '\n'.join(results), total_tokens


def crop_item_image(doc, item):
    if not item.prov:
        return None
    prov = item.prov[0]
    page = doc.pages.get(prov.page_no)
    if page is None or page.image is None:
        return None
    page_image = page.image.pil_image
    bbox = prov.bbox.to_top_left_origin(page_height=page.size.height)
    scale_x = page_image.width / page.size.width
    scale_y = page_image.height / page.size.height
    left = max(0, int(bbox.l * scale_x) - 4)
    top = max(0, int(bbox.t * scale_y) - 4)
    right = min(page_image.width, int(bbox.r * scale_x) + 4)
    bottom = min(page_image.height, int(bbox.b * scale_y) + 4)
    if right <= left or bottom <= top:
        return None
    return page_image.crop((left, top, right, bottom))


def enrich_formulas(doc, client, model, parallel=1, retries=None):  # noqa
    from docling_core.types.doc.labels import DocItemLabel

    formula_items = []
    for item, _ in doc.iterate_items():
        if getattr(item, 'label', None) == DocItemLabel.FORMULA:
            image = crop_item_image(doc, item)
            if image is not None:
                formula_items.append((item, image))
    count = len(formula_items)
    if count == 0:
        return {}, 0
    formulas = {}
    max_tokens = 0

    def process_formula(idx_item):
        idx, (item, image) = idx_item
        try:
            logger.debug('Formula %d / %d', idx + 1, count)
            formula, tokens = run_with_retries(
                lambda: query_vision_model(
                    client, model, image, FORMULA_PROMPT, max_tokens=2048),
                retries=retries, description=f'Formula {idx + 1}', log=logger)
            formula = formula.strip()
            if '```' in formula:
                parts = formula.split('```')
                if '\n' in parts[1] and parts[1].split('\n', 1)[1].strip():
                    formula = parts[1].split('\n', 1)[1].strip()
                elif parts[1].strip():
                    formula = parts[1].strip()
            while '$$' in formula and len(formula.split('$$')[1]):
                formula = formula.split('$$')[1].strip()
            while '$' in formula and len(formula.split('$')[1]):
                formula = formula.split('$')[1].strip()
            logger.debug(formula.strip())
            return idx, item, formula, tokens, None
        except Exception as error:
            msg = f'Formula enrichment failed: {error}'
            logger.warning(msg)
            return idx, item, None, 0, error

    indexed_items = list(enumerate(formula_items))
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        futures = {executor.submit(process_formula, idx_item): idx_item
                   for idx_item in indexed_items}
        for future in as_completed(futures):
            result_idx, result_item, formula, tokens, error = future.result()
            if error is None:
                result_item.text = f'formula_{result_idx}'
                formulas[result_idx] = formula
                max_tokens = max(tokens, max_tokens)

    if formulas:
        msg = f'Processed {len(formulas)} formula{"" if len(formulas) == 1 else "s"}'
        logger.info(msg)
    return formulas, max_tokens


def images_are_similar(image1, image2, max_diff=20, rms=8.0):
    """Check if two images are functionally identical (compression-tolerant)."""
    if image1.size != image2.size or image1.mode != image2.mode:
        return False
    arr1 = np.array(image1.convert('RGB')).astype(np.float32)
    arr2 = np.array(image2.convert('RGB')).astype(np.float32)
    diff = np.abs(arr1 - arr2)
    return np.max(diff) <= max_diff and np.sqrt(np.mean(diff ** 2)) <= rms


def enrich_pictures(doc, client, model, parallel=1, retries=None):
    from docling_core.types.doc.document import (DescriptionMetaField,
                                                 PictureItem, PictureMeta)

    picture_items = []
    for item, _ in doc.iterate_items():
        if isinstance(item, PictureItem):
            image = item.get_image(doc) or crop_item_image(doc, item)
            if image is not None:
                picture_items.append((item, image))

    count = len(picture_items)
    if count == 0:
        return {}, 0
    pictures = {}
    max_tokens = 0
    seen_images = {}  # idx -> (size, mode, image)
    img_lock = threading.Lock()

    def process_picture(idx_item):
        idx, (item, image) = idx_item
        try:
            logger.debug('Picture %d / %d', idx + 1, count)
            with img_lock:
                for orig_idx, (_orig_size, _orig_mode, orig_img) in seen_images.items():
                    if images_are_similar(image, orig_img):
                        description = f'Identical to image {orig_idx + 1}'
                        logger.debug(description)
                        return idx, item, description, 0, None
            description, tokens = run_with_retries(
                lambda: query_vision_model(
                    client, model, image, PICTURE_PROMPT, max_tokens=16384),
                retries=retries, description=f'Picture {idx + 1}', log=logger)
            logger.debug(description)
            with img_lock:
                seen_images[idx] = (image.size, image.mode, image)
            return idx, item, description, tokens, None
        except Exception as error:
            msg = f'Picture description failed: {error}'
            logger.warning(msg)
            return idx, item, None, 0, error

    indexed_items = list(enumerate(picture_items))
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        futures = {executor.submit(process_picture, idx_item): idx_item
                   for idx_item in indexed_items}
        for future in as_completed(futures):
            result_idx, result_item, description, tokens, error = future.result()
            if error is None:
                result_item.meta = PictureMeta(description=DescriptionMetaField(
                    text=f'<!-- picture_{result_idx} -->', created_by=model))
                pictures[result_idx] = description
                max_tokens = max(tokens, max_tokens)
    if pictures:
        msg = f'Processed {len(pictures)} picture{"" if len(pictures) == 1 else "s"}'
        logger.info(msg)
    return pictures, max_tokens


#: cmap subtable formats (except 4) as size-bounded fixed or counted
#: records: (header bytes, count offset, bytes per counted record).  A
#: subtable is truncated when the bytes it declares do not fit in the cmap
#: table.  Format 4 is handled separately because it carries an explicit
#: length field.
CMAP_SUBTABLE_LAYOUTS = {
    0: (6, None, 256),        # 256 glyph ids, no count field
    6: (10, 8, 2),            # entryCount, uint16 glyph ids
    12: (16, 12, 12),         # nGroups, 12-byte groups
}


def text_mapping_is_usable(font_data):
    """Return True when a font's embedded mapping can extract text.

    A subset font (``ABCDEF+Name``) is normal and renders fine; the fault
    this checks for is narrower: its ``cmap`` table is truncated or has no
    usable subtable, so the bytes in the content stream cannot be mapped
    back to Unicode.  The page still renders correctly (the glyph outlines
    are intact), but extraction yields gibberish, for example ``PROOF``
    becomes ``mollc``.  Standard fonts, which carry no embedded program, are
    reported as usable so they are never flagged.
    """
    if len(font_data) < 12 or font_data[:4] not in (
            b'\x00\x01\x00\x00', b'true', b'OTTO'):
        # Not a single embedded SFNT program (collections are unused here).
        return True
    tables = {}
    for i in range(int.from_bytes(font_data[4:6], 'big')):
        entry = font_data[12 + 16 * i:28 + 16 * i]
        if len(entry) < 16:
            return False
        tables[entry[:4]] = (int.from_bytes(entry[8:12], 'big'),
                             int.from_bytes(entry[12:16], 'big'))
    if b'cmap' not in tables:
        return False
    start, length = tables[b'cmap']
    cmap = font_data[start:start + length]
    for i in range(int.from_bytes(cmap[2:4], 'big') if len(cmap) > 3 else 0):
        if 12 + 8 * i > len(cmap):
            return False
        offset = int.from_bytes(cmap[8 + 8 * i:12 + 8 * i], 'big')
        if offset + 2 > len(cmap):
            return False
        fmt = int.from_bytes(cmap[offset:offset + 2], 'big')
        if fmt == 4:
            if offset + 14 > len(cmap):
                return False
            declared = int.from_bytes(cmap[offset + 2:offset + 4], 'big')
            segments = int.from_bytes(cmap[offset + 6:offset + 8], 'big')
            if (offset + declared > len(cmap) or
                    offset + 16 + segments * 4 > len(cmap)):
                return False
        elif fmt in CMAP_SUBTABLE_LAYOUTS:
            header, count_at, per_entry = CMAP_SUBTABLE_LAYOUTS[fmt]
            if offset + header > len(cmap):
                return False
            count = (int.from_bytes(
                cmap[offset + count_at:offset + count_at + 2], 'big')
                if count_at is not None else 1)
            if offset + header + count * per_entry > len(cmap):
                return False
    return True


def pdf_has_unreliable_text_mapping(filepath):
    """Detect PDFs whose fonts cannot map bytes back to text.

    Subsetters and converters sometimes emit fonts whose TrueType ``cmap``
    is truncated or missing.  The font still renders correctly, but text
    extraction yields gibberish that language detection can misclassify (for
    instance reading English as Turkish).  Every page is walked, each
    embedded font program is collected via pypdfium2, and the document is
    flagged when any program has an unusable mapping.  Fonts are
    de-duplicated by name, so the work is bounded by the number of distinct
    fonts rather than the page count.

    Returns ``True`` when at least one font has an unusable text mapping.
    """
    import pypdfium2

    def program_bytes(font):
        size = ctypes.c_ulong(0)
        pypdfium2.raw.FPDFFont_GetFontData(
            font.raw, None, 0, ctypes.byref(size))
        buffer = (ctypes.c_ubyte * size.value)()
        pypdfium2.raw.FPDFFont_GetFontData(
            font.raw, buffer, size.value, ctypes.byref(size))
        return bytes(buffer[:size.value])

    try:
        doc = pypdfium2.PdfDocument(str(filepath))
    except Exception as exc:
        logger.warning('Failed to inspect PDF fonts: %s', exc)
        return False
    try:
        seen = set()
        for page_number in range(len(doc)):
            try:
                objects = doc[page_number].get_objects()
            except Exception:
                continue
            for obj in objects:
                if type(obj).__name__ != 'PdfTextObj':
                    continue
                try:
                    font = obj.get_font()
                    name = font.get_base_name()
                    if name in seen or not font.is_embedded:
                        seen.add(name)
                        continue
                    seen.add(name)
                    data = program_bytes(font)
                except Exception:
                    continue
                if not text_mapping_is_usable(data):
                    logger.debug(
                        'PDF %s: font %r has unusable text mapping (%d bytes)',
                        filepath, name, len(data))
                    return True
        return False
    finally:
        doc.close()


def text_is_probably_garbled(text):
    """Heuristic test for extraction garbage from an unusable mapping.

    Such a mapping usually lands ASCII letters on Latin-1 high code points,
    so the extracted text is dominated by accented characters (``é``, ``Ü``,
    ``ç``, ``ê``).  Genuine prose in any Western European language keeps
    accented letters well under a fifth of its characters, while non-Latin
    scripts (Greek, Cyrillic, CJK, Arabic) lie outside the Latin-1 range
    entirely.  A high ratio of Latin-1 bytes is therefore a reliable
    indicator of a garbled text layer.  Short samples are ignored to avoid
    false positives.
    """
    stripped = text.strip()
    if len(stripped) < 100:
        return False
    alpha = sum(1 for ch in stripped if ch.isalpha())
    if alpha < 50:
        return False
    high = sum(1 for ch in stripped if 0x80 <= ord(ch) <= 0xFF)
    return high / len(stripped) > 0.35


def pdf_has_good_embedded_text(filepath):
    import pypdfium2

    try:
        doc = pypdfium2.PdfDocument(str(filepath))
        if len(doc) == 0:
            return False
        pages_with_text = 0
        total_text_chars = 0
        suspicious_char_count = 0
        total_char_count = 0
        garbled_pages = 0

        # Patterns that suggest poor OCR quality even when embedded
        # - Unusual Unicode replacement characters
        # - Long runs of the same character
        repeated_punct_pattern = re.compile(r'([.,;:!?])\1{3,}')
        replacement_char = '\ufffd'
        for i in range(len(doc)):
            page = doc[i]
            text_page = page.get_textpage()
            text = text_page.get_text_bounded()

            if text and text.strip():
                pages_with_text += 1
                stripped = text.strip()
                total_text_chars += len(stripped)
                total_char_count += len(stripped)
                if text_is_probably_garbled(stripped):
                    garbled_pages += 1
                # Count suspicious patterns
                suspicious_char_count += len(re.findall(repeated_punct_pattern, text))
                suspicious_char_count += text.count(replacement_char)
        if len(doc) == 0 or total_char_count == 0:
            return False
        text_ratio = pages_with_text / len(doc)
        avg_chars_per_page = total_text_chars / len(doc)
        suspicious_ratio = suspicious_char_count / max(1, total_char_count)
        garbled_ratio = garbled_pages / max(1, pages_with_text)

        # Criteria for "good" text:
        # 1. At least 50% of pages have text
        # 2. Average > 200 chars per page (filters out pages with just
        # headers/footers)
        # 3. Less than 1% suspicious characters
        # 4. Fewer than half the pages look garbled by a font whose text
        #    mapping does not describe the rendered glyphs
        has_text = (
            text_ratio >= 0.5 and
            avg_chars_per_page > 200 and
            suspicious_ratio < 0.01 and
            garbled_ratio < 0.5
        )
        logger.debug(
            'PDF %s: %d/%d pages with text (%.1f%%), avg %d chars/page, '
            'suspicious ratio %.4f, garbled %d/%d -> has_good_text=%s',
            filepath, pages_with_text, len(doc), text_ratio * 100,
            avg_chars_per_page, suspicious_ratio, garbled_pages,
            pages_with_text, has_text,
        )
        return has_text
    except Exception as exc:
        logger.warning('Failed to check PDF for embedded text: %s', exc)
        return False


def get_converter(args, force_ocr=False):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (OcrAutoOptions,
                                                    PdfPipelineOptions)
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_page_images = True
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = args.images_scale
    pipeline_options.do_picture_classification = False
    pipeline_options.do_picture_description = False
    pipeline_options.do_code_enrichment = True
    pipeline_options.do_formula_enrichment = False

    # Skip OCR if PDF has good embedded text and OCR not forced.  When forced,
    # OCR the full page: a document whose fonts have an unusable text
    # mapping still exposes that bad mapping to the backend, so partial OCR
    # is not enough.
    if not force_ocr:
        pipeline_options.do_ocr = False
        # Use backend's native text extraction when available
        pipeline_options.force_backend_text = True
    else:
        pipeline_options.do_ocr = True
        pipeline_options.force_backend_text = False
        pipeline_options.ocr_options = OcrAutoOptions(force_full_page_ocr=True)
    format_option_kwargs = {'pipeline_options': pipeline_options}
    if args.alt_backend:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        format_option_kwargs['backend'] = PyPdfiumDocumentBackend
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(**format_option_kwargs),
        },
    )
    return converter


def offload_ollama(url):
    url = url.rstrip('/')
    resp = requests.get(f'{url}/api/ps')
    try:
        resp.raise_for_status()
        models = resp.json().get('models', [])
    except Exception:
        return
    for entry in models:
        try:
            model = entry['model']
            requests.post(f'{url}/api/chat', json={
                'model': model, 'messages': [], 'keep_alive': 0})
        except Exception:
            pass


def clean_blanks(text):
    """
    Trim trailing whitespace on any line; maximum newline sequence of 2.
    """
    return re.sub(r'\n{3,}', '\n\n', re.sub(r'[ \t]+$', '', text.replace(
        '\r\n', '\n').replace('\r', '\n'), flags=re.M))


def process_file(converter, client, proc_client, filepath, model, args):
    offload = converter is None
    has_good_text = pdf_has_good_embedded_text(filepath)
    if has_good_text and pdf_has_unreliable_text_mapping(filepath):
        # The font renders correctly but its cmap cannot map the content
        # bytes back to Unicode, so the extracted text is gibberish.  The
        # glyphs are fine, so fall over to OCR.
        logger.info(
            'PDF %s has fonts with an unusable text mapping; forcing OCR.',
            filepath)
        has_good_text = False
    if converter is None:
        offload_ollama(args.url)
        converter = get_converter(args, force_ocr=not has_good_text)
    else:
        converter = converter[0] if has_good_text else converter[1]
    try:
        result = converter.convert(filepath)
        doc = result.document
    finally:
        if offload:
            converter = None
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
    pictures = {}
    formulas = {}
    try:
        pictures, tokens = enrich_pictures(
            doc, client, model, parallel=args.parallel, retries=args.max_retries)
        formulas, ftokens = enrich_formulas(
            doc, client, model, parallel=args.parallel, retries=args.max_retries)
        tokens = max(tokens, ftokens)
        logger.debug('Max tokens in any vision request: %d', tokens)
    finally:
        result.input._backend.unload()
        if offload:
            offload_ollama(args.url)
    markdown = doc.export_to_markdown()
    markdown = '\n\n'.join([p.strip() for p in markdown.split('<!-- image -->')])
    markdown = clean_blanks(markdown)
    # Apply OCR text processing if requested or OCR was detected
    process_mode = getattr(args, 'process', 'none')
    proc_model = getattr(args, 'processing_model', '') or model

    ocr_used = is_ocr_used(result)
    needs_process = process_mode != 'none' or ocr_used
    output = markdown

    if needs_process:
        logger.info('OCR detected: %s; applying text processing '
                    '(mode=%s)',
                    ocr_used, process_mode)
        source_text = markdown
        total_tokens = 0
        proc_parallel = args.processing_parallel or args.parallel
        if process_mode in ('ocr', 'all') and ocr_used:
            fair_copy_text, ocr_tok = process_ocr_text(
                proc_client, proc_model, markdown, parallel=proc_parallel,
                retries=args.max_retries)
            total_tokens += ocr_tok
            logger.info('OCR fair copy complete (%d tokens)', ocr_tok)
            output += '\n\n## FAIR COPY\n\n' + fair_copy_text
            source_text = fair_copy_text
        # Detect source language first (needed for translation)
        src_lang = detect_language(source_text)
        logger.debug('Detected source language: %s', src_lang)
        if process_mode in ('translate', 'all') and src_lang != 'English':
            translated_text, trans_tok = process_translation(
                proc_client, proc_model, source_text, src_lang=src_lang,
                parallel=proc_parallel, retries=args.max_retries)
            total_tokens += trans_tok
            logger.info('Translation complete (%d tokens)', trans_tok)
            output += '\n\n## TRANSLATION\n\n' + translated_text
    for k in pictures:
        template = f'<!-- picture_{k} -->'
        if template not in output:
            msg = f'Missing picture template {template}'
            raise Exception(msg)
        output = output.replace(template, f'IMAGE {k + 1}\n\n{pictures[k]}\n\nENDIMAGE {k + 1}\n')
    for k in formulas:
        template = f'$$formula_{k}$$'
        if template not in output:
            msg = 'Missing formula template {template}'
            raise Exception(msg)
        output = output.replace(template, f'$${formulas[k]}$$')
    output = clean_blanks(output)
    return output


def sort_file_list(file_list, sort):
    new_list = []
    for f in file_list:
        if not os.path.isfile(f):
            continue
        metric = f
        if sort == 'shortest':
            metric = os.path.getsize(f)
        new_list.append((metric, f))
    return [entry[-1] for entry in sorted(new_list)]


def make_client(url, api_key, args):
    """Build an OpenAI-compatible client for an Ollama-style endpoint.

    The client imposes no artificial request timeout by default (``None``
    waits as long as the server needs), which matters for slow local models.
    Transient errors are additionally retried by ``run_with_retries`` around
    each request.
    """
    from openai import OpenAI

    timeout = None if args.timeout is None else args.timeout
    return OpenAI(
        base_url=url.rstrip('/') + '/v1', api_key=api_key,
        timeout=timeout, max_retries=args.client_retries)


def process_directory(args):  # noqa
    if args.no_cuda:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    converter = None
    if not args.offload:
        converter = get_converter(args), get_converter(args, force_ocr=True)
    client = make_client(args.url, args.api_key, args)
    proc_client = client
    if args.processing_url:
        proc_client = make_client(args.processing_url, args.api_key, args)
    suffix = f'.{args.suffix.lstrip(".")}'
    for input_path in args.inputs:
        target = Path(input_path)
        if target.is_file():
            file_list = [target]
        elif target.is_dir():
            file_list = sorted(target.rglob('*')) if args.recurse else sorted(target.iterdir())
        else:
            continue
        if args.sort:
            file_list = sort_file_list(file_list, args.sort)
        for filepath in file_list:
            if not filepath.is_file():
                continue
            if not str(filepath).lower().endswith('.pdf') and filepath not in args.inputs:
                continue
            md_path = filepath.with_suffix(suffix)
            if args.out:
                if os.path.isdir(args.out):
                    outdir = Path(args.out)
                    if args.deep:
                        outdir /= filepath.parent.relative_to(Path(input_path))
                        outdir.mkdir(parents=True, exist_ok=True)
                    md_path = outdir / md_path.name
                else:
                    md_path = Path(args.out)
            if (not args.overwrite and md_path.exists() and
                    md_path.stat().st_mtime > filepath.stat().st_mtime):
                continue
            if args.out and not os.path.isdir(args.out):
                args.overwrite = False
            if args.list:
                print(f'{filepath} -> {md_path}')
                continue
            try:
                print(filepath)
                description = process_file(
                    converter, client, proc_client, filepath, args.model, args)
                logger.info(description)
                if not args.dry_run:
                    md_path.parent.mkdir(parents=True, exist_ok=True)
                    md_path.write_text(description, encoding='utf-8')
                    print(f'Created {md_path.name}')
                else:
                    print(f'Would have created {md_path.name}')
            except Exception as exc:
                msg = f'Failed processing {filepath.name}: {exc}'
                logger.debug(msg)
                if args.raise_errors:
                    raise


def main():
    parser = argparse.ArgumentParser(
        description='Convert PDFs to Markdown using Docling with LLM-based '
        'image descriptions.',
    )
    parser.add_argument(
        'inputs', nargs='+',
        help='One or more files or directories to process.')
    parser.add_argument(
        '--recurse', '-r', action='store_true',
        help='Recurse into input directories')
    parser.add_argument(
        '--suffix', '--ext', default='.description.md',
        help='File extension to use for description files.')
    parser.add_argument(
        '--out', '--output',
        help='If an existing directory, the location to store outputs.  If a '
        'single path or non-existent path, write the first description to '
        'this file and then stop.')
    parser.add_argument(
        '--deep', action='store_true',
        help='If out is a directory, reconstruct source-path relative '
        'directories to store outputs. Multiple sources will all be relative '
        'to the out directory.')
    parser.add_argument(
        '--url', default=os.environ.get('OLLAMA_HOST', 'http://localhost:11434'),
        help='Ollama base URL.  Default %(default)s.')
    parser.add_argument(
        '--api-key', default=os.environ.get('OPENAI_API_KEY', 'ollama'),
        help='API key sent to the endpoint.  Default %(default)s.')
    parser.add_argument(
        '--model', '-m', default='qwen2.5vl:7b',
        help='Vision model identifier.  Default %(default)s.  A smaller '
        'context than the default is fine, for instance, one model could '
        'be\nqwen2.5vl-pdf.Modelfile\n```Modelfile\nFROM qwen2.5vl:7b\n'
        'PARAMETER num_ctx 12288\n```\n, ingested with `ollama create '
        'qwen2.5vl:7b-pdf -f qwen2.5vl-pdf.Modelfile`.')
    parser.add_argument(
        '--processing-model', '-p', dest='processing_model', default='',
        help='Text-only LLM for OCR cleanup/translation (uses --model if '
        'empty).  A smaller context than default is fine, for instances, one '
        'model could be\nqwen3.5-pdf.Modelfile\n```Modelfile\nFROM '
        'qwen3.5:9b\nPARAMETER num_ctx 32768\nPARAMETER temperature 0.25\n'
        'PARAMETER repeat_penalty 1.5\n```\n, ingested with `ollama create '
        'qwen3.5:9b-pdf -f qwen3.5-pdf.Modelfile`. Anecdotally, a full model '
        'is needed to be reliable.')
    parser.add_argument(
        '--processing-url', '--process-url',
        help='Ollama URL for processing.  Defaults to --url value.')
    parser.add_argument(
        '--process', choices=['none', 'ocr', 'translate', 'all'], default='all',
        help='Apply text processing: none=skip, ocr=fair copy only, '
        'translate=translate to English, all=both.')
    parser.add_argument(
        '--images-scale', type=float, default=2.0,
        help='Rendering scale for page images.  Default %(default)s.')
    parser.add_argument(
        '--timeout', type=float, default=None,
        help='Per-request client timeout in seconds.  The default (None) '
        'waits indefinitely, which is useful for slow local models.')
    parser.add_argument(
        '--max-retries', type=int, default=None,
        help='Maximum number of retries for transient (timeout/connection) '
        'errors on each vision or processing request.  The default (None) '
        'retries indefinitely, so a slow local model is never abandoned due '
        'to a timeout.  Set to 0 to disable retries.')
    parser.add_argument(
        '--client-retries', type=int, default=10,
        help='Number of retries the OpenAI client performs internally.  '
        'Default %(default)s.  This is independent of --max-retries.')
    parser.add_argument(
        '--overwrite', '-y', action='store_true',
        help='Overwrite existing companion markdown files')
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='Do not actually write markdown files')
    parser.add_argument(
        '--offload', '-o', action='store_true',
        help='Offload torch models between pdfs.')
    parser.add_argument(
        '--no-cuda', action='store_true',
        default=bool(os.environ.get('PDFS_TO_MARKDOWN_NO_CUDA')),
        help='Avoid using cuda for OCR.')
    parser.add_argument(
        '--sort',
        help='Sort files before processing. "shortest" will sort by size.')
    parser.add_argument(
        '--list', '-l', action='store_true',
        help='Just list what files would be processed without actually doing anything.')
    parser.add_argument(
        '--raise', dest='raise_errors', action='store_true',
        help='Raise on errors instead of ignoring them.')
    parser.add_argument(
        '--verbose', '-v', action='count', default=0,
        help='Increase verbosity')
    parser.add_argument(
        '--parallel', '--jobs', '-j', type=int, default=1,
        help='Number of parallel jobs for vision tasks (image descriptions, '
        'formula transcription). Default: %(default)s')
    parser.add_argument(
        '--processing-parallel', '--processing-jobs', type=int,
        help='Number of parallel jobs for processing tasks (OCR cleanup, '
        'translation). Defaults to --parallel value.')
    parser.add_argument(
        '--alt-backend', action='store_true',
        help='Use the pdfium backend instead of the default docling backend '
        '(useful when the default backend crashes on certain PDFs).')
    args = parser.parse_args()
    if os.environ.get('PDFS_TO_MARKDOWN_OFFLOAD'):
        offload = os.environ.get('PDFS_TO_MARKDOWN_OFFLOAD').lower()
        if offload == 'offload':
            args.offload = True
            args.no_cuda = False
        elif offload in {'no-cuda', 'no_cuda', 'nocuda'}:
            args.offload = False
            args.no_cuda = True
        else:
            args.offload = False
            args.no_cuda = False
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.debug('Parsed arguments: %r', args)
    process_directory(args)


if __name__ == '__main__':
    sys.exit(interruptible.run(main))
