#!/usr/bin/env python3
# /// script
# requires-python = '>=3.12'
# dependencies = [
#   'docling',
#   'lingua-language-detector',
#   'openai',
#   'pillow',
# ]
# ///
# This can be run via something like
# uv run --index-strategy unsafe-best-match --with torch==2.13.0+cu132
# --index https://download.pytorch.org/whl/cu132 ..scriptname.. ..options..

import argparse
import base64
import functools
import io
import logging
import os
import re
import sys
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logging.getLogger('transformers').setLevel(logging.ERROR)
logging.getLogger('torch').setLevel(logging.ERROR)
logging.getLogger('RapidOCR').setLevel(logging.ERROR)
os.environ.setdefault('TRANSFORMERS_VERBOSITY', 'warning')

FORMULA_PROMPT = (
    'Convert the mathematical formula in this image into a single LaTeX '
    'expression. Respond with only the LaTeX code, without explanations, '
    'without surrounding dollar signs, and without code fences.'
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
    'structure. Specifically, convert any "long s" (ſ) typically found in older texts '
    'to standard lowercase "s". Output only the cleaned text without explanations.'
)
DETECT_LANGUAGE_PROMPT = (
    'What is the primary language of this text? Reply with just the language '
    'name in English (e.g., "French", "Chinese", "Japanese") and nothing else.'
)
TRANSLATE_PROMPT = (
    'Translate this text to English. Preserve all markdown formatting, '
    'headers, image/figure markers, tables, and code blocks exactly as they '
    'are. Output only the translated text without explanations.'
)
# Unique marker for joining processed chunks back together.


def chat_create_with_reasoning(client, **kwargs):
    """Create a chat completion, trying 'reasoning_effort=low' first."""
    kwargs['reasoning_effort'] = 'low'
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:
        # If reasoning effort is unsupported, retry without it
        if 'reasoning_effort' in str(exc).lower():
            kwargs.pop('reasoning_effort')
            return client.chat.completions.create(**kwargs)
        raise exc


def image_to_data_url(image):
    buffer = io.BytesIO()
    image = image.convert('L' if image.mode in {'L', 'LA'} else 'RGB')
    image.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f'data:image/png;base64,{encoded}'


def query_vision_model(client, model, image, prompt):
    response = chat_create_with_reasoning(
        client,
        model=model,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': image_to_data_url(image)}},
            ],
        }],
    )
    content = response.choices[0].message.content.strip()
    tokens = response.usage.total_tokens if response.usage else 0
    return content, tokens


def query_llm(client, model, prompt):
    """Query an LLM (text-only) and return its text response."""
    response = chat_create_with_reasoning(
        client,
        model=model,
        messages=[{'role': 'user', 'content': prompt}],
    )
    content = response.choices[0].message.content.strip()
    tokens = response.usage.total_tokens if response.usage else 0
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

    return lingua.LanguageDetectorBuilder.from_all_languages().build()


def detect_language(text):
    if len(text.strip()) <= 10:
        return 'English'
    detector = get_lang_detector()
    lang = detector.detect_language_of(text)
    if not lang:
        return 'English'
    return lang.name.capitalize()


def process_ocr_text(client, model, text):
    """Apply fair copy processing to clean up OCR text."""
    chunks = chunk_text(text)
    results, total_tokens = [], 0
    for i, chunk in enumerate(chunks):
        logger.info('OCR fair copy chunk %d / %d (%d)', i + 1, len(chunks), len(chunk))
        try:
            cleaned, tokens = query_llm(
                client, model,
                f'{FAIR_COPY_PROMPT}\n\n{chunk}')
            total_tokens += tokens
            results.append(cleaned)
        except Exception as err:
            logger.warning('OCR fair copy chunk %d failed: %s', i, err)
            results.append(chunk)
    return '\n'.join(results), total_tokens


def process_translation(client, model, text, src_lang=None):
    if src_lang.lower() == 'english':
        return text, 0
    chunks = chunk_text(text)
    results, total_tokens = [], 0
    for i, chunk in enumerate(chunks):
        logger.info('Translation chunk %d / %d (%d)', i + 1, len(chunks), len(chunk))
        try:
            translated, tok = query_llm(
                client, model,
                f'{TRANSLATE_PROMPT}\n\nOriginal ({src_lang}):\n{chunk}')
            total_tokens += tok
            results.append(translated)
        except Exception as err:
            logger.warning('Translation chunk %d failed: %s', i, err)
            results.append(chunk)
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


def enrich_formulas(doc, client, model):
    from docling_core.types.doc.labels import DocItemLabel

    max_tokens = 0
    processed = 0
    count = len([item for item, _ in doc.iterate_items()
                 if getattr(item, 'label', None) == DocItemLabel.FORMULA])
    for item, _ in doc.iterate_items():
        if getattr(item, 'label', None) != DocItemLabel.FORMULA:
            continue
        image = crop_item_image(doc, item)
        if image is None:
            count -= 1
            continue
        try:
            logger.debug('Formula %d / %d', processed + 1, count)
            latex, tokens = query_vision_model(client, model, image, FORMULA_PROMPT)
            max_tokens = max(tokens, max_tokens)
        except Exception as error:
            msg = f'Formula enrichment failed: {error}'
            logger.warning(msg)
            count -= 1
            continue
        item.text = latex
        processed += 1
    if processed:
        msg = f'Processed {processed} formulas'
        logger.info(msg)
    return max_tokens


def enrich_pictures(doc, client, model):
    from docling_core.types.doc.document import (DescriptionMetaField,
                                                 PictureItem, PictureMeta)
    max_tokens = 0
    described = 0
    count = len([item for item, _ in doc.iterate_items() if isinstance(item, PictureItem)])
    for item, _ in doc.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        image = item.get_image(doc) or crop_item_image(doc, item)
        if image is None:
            count -= 1
            continue
        try:
            logger.debug('Picture %d / %d', described + 1, count)
            description, tokens = query_vision_model(client, model, image, PICTURE_PROMPT)
            max_tokens = max(tokens, max_tokens)
        except Exception as error:
            msg = f'Picture description failed: {error}'
            logger.warning(msg)
            count -= 1
            continue
        item.meta = PictureMeta(description=DescriptionMetaField(
            text=description, created_by=model))
        described += 1
    if described:
        msg = f'Described {described} pictures'
        logger.info(msg)
    return max_tokens


def get_converter(args):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_page_images = True
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = args.images_scale
    pipeline_options.do_picture_classification = False
    pipeline_options.do_picture_description = False
    pipeline_options.do_code_enrichment = True
    pipeline_options.do_formula_enrichment = False

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
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


def process_file(converter, client, filepath, model, args):
    offload = converter is None
    if converter is None:
        offload_ollama(args.url)
        converter = get_converter(args)
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
    try:
        tokens = enrich_pictures(doc, client, model)
        tokens = max(tokens, enrich_formulas(doc, client, model))
        logger.debug('Max tokens in any vision request: %d', tokens)
    finally:
        result.input._backend.unload()
        if offload:
            offload_ollama(args.url)
    markdown = doc.export_to_markdown()
    # Apply OCR text processing if requested or OCR was detected
    process_mode = getattr(args, 'process', 'none')
    proc_model = getattr(args, 'processing_model', '') or model

    ocr_used = is_ocr_used(result)  # Detect OCR from page confidences
    needs_process = process_mode != 'none' or ocr_used
    final_output = markdown

    if needs_process:
        logger.info('OCR detected: %s; applying text processing '
                    '(mode=%s)',
                    ocr_used, process_mode)
        source_text = markdown
        total_tokens = 0
        if process_mode in ('ocr', 'all') and ocr_used:
            fair_copy_text, ocr_tok = process_ocr_text(client, proc_model, markdown)
            total_tokens += ocr_tok
            logger.info('OCR fair copy complete (%d tokens)', ocr_tok)
            final_output += '\n\n## FAIR COPY\n\n' + fair_copy_text
            source_text = fair_copy_text
        # Detect source language first (needed for translation)
        src_lang = detect_language(source_text)
        logger.debug('Detected source language: %s', src_lang)
        if process_mode in ('translate', 'all') and src_lang != 'English':
            translated_text, trans_tok = process_translation(
                client, proc_model, source_text, src_lang=src_lang)
            total_tokens += trans_tok
            logger.info('Translation complete (%d tokens)', trans_tok)
            final_output += '\n\n## TRANSLATION\n\n' + translated_text
    return final_output


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


def process_directory(args):  # noqa
    from openai import OpenAI

    if args.no_cuda:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    converter = None
    if not args.offload:
        converter = get_converter(args)
    client = OpenAI(base_url=args.url.rstrip('/') + '/v1', api_key=args.api_key)
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
            if not str(filepath).endswith('.pdf') and filepath not in args.inputs:
                continue
            md_path = filepath.with_suffix(suffix)
            if args.out:
                if os.path.isdir(args.out):
                    md_path = Path(args.out) / md_path.name
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
                description = process_file(converter, client, filepath, args.model, args)
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
        '--url', default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
        help='Ollama base URL.  Default %(default)s.')
    parser.add_argument(
        '--api-key', default='ollama',
        help='API key sent to the endpoint.  Default %(default)s.')
    parser.add_argument(
        '--model', '-m', default='qwen2.5vl:7b',
        help='Vision model identifier.  Default %(default)s.')
    parser.add_argument(
        '--processing-model', '-p', dest='processing_model', default='',
        help='Text-only LLM for OCR cleanup/translation (uses --model if empty).')
    parser.add_argument(
        '--process', choices=['none', 'ocr', 'translate', 'all'], default='all',
        help='Apply text processing: none=skip, ocr=fair copy only, '
        'translate=translate to English, all=both.')
    parser.add_argument(
        '--images-scale', type=float, default=2.0,
        help='Rendering scale for page images.  Default %(default)s.')
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
        '--no-cuda', action='store_true', help='Avoid using cuda for OCR.')
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
    args = parser.parse_args()
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.debug('Parsed arguments: %r', args)
    process_directory(args)


if __name__ == '__main__':
    main()
