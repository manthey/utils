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
import hashlib
import io
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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
    'Translate this text to English. Preserve all markdown formatting, '
    'headers, image/figure markers, tables, and code blocks exactly as they '
    'are. Output only the translated text without explanations.'
)


def honor_ctrlc(timeout=5):
    import os
    import signal
    import sys
    import threading
    import time

    shutdown = threading.Event()
    count = {'n': 0}
    lock = threading.Lock()

    def force_exit():
        sys.stderr.write('\nForce exiting\n')
        os._exit(1)

    def handler(signum, frame):
        with lock:
            count['n'] += 1
        if count['n'] >= 2:
            force_exit()
        shutdown.set()
        msg = f'Signal {signum}'
        raise KeyboardInterrupt(msg)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    threading.Thread(
        target=lambda: (shutdown.wait(), time.sleep(timeout), force_exit()),
        daemon=True).start()


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

    return lingua.LanguageDetectorBuilder.from_all_languages().build()


def detect_language(text):
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
            results[lang] = results.get('name', 0) + 1
    lang = None
    if not len(results):
        lang = max(results, key=results.get)
    if not lang:
        return 'English'
    return lang.name.capitalize()


def process_ocr_text(client, model, text, parallel=1):
    """Apply fair copy processing to clean up OCR text."""
    chunks = chunk_text(text)
    total_tokens = 0

    def process_chunk(i_chunk):
        i, chunk = i_chunk
        logger.info('OCR fair copy chunk %d / %d (%d)', i + 1, len(chunks), len(chunk))
        lasterr = ''
        minlen, maxlen = len(chunk) // 4, len(chunk) * 3 // 2
        for retries in range(5, -1, -1):
            try:
                cleaned, tokens = query_llm(
                    client, model, f'{FAIR_COPY_PROMPT}\n\n{chunk}', stop_after=maxlen + 1)
                if retries and (len(cleaned) < minlen or len(cleaned) > maxlen):
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


def process_translation(client, model, text, src_lang=None, parallel=1):
    if src_lang.lower() == 'english':
        return text, 0
    chunks = chunk_text(text)
    total_tokens = 0

    def translate_chunk(i_chunk):
        i, chunk = i_chunk
        logger.info('Translation chunk %d / %d (%d)', i + 1, len(chunks), len(chunk))
        lasterr = ''
        minlen, maxlen = len(chunk) // 4, len(chunk) * 2
        for retries in range(5, -1, -1):
            try:
                translated, tokens = query_llm(
                    client, model,
                    f'{TRANSLATE_PROMPT}\n\nOriginal ({src_lang}):\n{chunk}',
                    stop_after=maxlen + 1)
                if retries and (len(translated) < minlen or len(translated) > maxlen):
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


def enrich_formulas(doc, client, model, parallel=1):  # noqa
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
            formula, tokens = query_vision_model(
                client, model, image, FORMULA_PROMPT, max_tokens=2048)
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


def image_to_hash(image):
    """Compute a hash of an image for duplicate detection."""
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def enrich_pictures(doc, client, model, parallel=1):
    import threading

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
    seen_hashes = {}
    hash_lock = threading.Lock()

    def process_picture(idx_item):
        idx, (item, image) = idx_item
        try:
            logger.debug('Picture %d / %d', idx + 1, count)
            img_hash = image_to_hash(image)
            with hash_lock:
                if img_hash in seen_hashes:
                    orig_idx, _ = seen_hashes[img_hash]
                    description = f'Identical to image {orig_idx + 1}'
                    logger.debug(description)
                    return idx, item, description, 0, None
            description, tokens = query_vision_model(
                client, model, image, PICTURE_PROMPT, max_tokens=16384)
            logger.debug(description)
            with hash_lock:
                seen_hashes[img_hash] = (idx, description)
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


def get_converter(args):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

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
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
                # backend=PyPdfiumDocumentBackend,
            ),
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
    pictures = {}
    formulas = {}
    try:
        pictures, tokens = enrich_pictures(doc, client, model, parallel=args.parallel)
        formulas, ftokens = enrich_formulas(doc, client, model, parallel=args.parallel)
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
                proc_client, proc_model, markdown, parallel=proc_parallel)
            total_tokens += ocr_tok
            logger.info('OCR fair copy complete (%d tokens)', ocr_tok)
            output += '\n\n## FAIR COPY\n\n' + fair_copy_text
            source_text = fair_copy_text
        # Detect source language first (needed for translation)
        src_lang = detect_language(source_text)
        logger.debug('Detected source language: %s', src_lang)
        if process_mode in ('translate', 'all') and src_lang != 'English':
            translated_text, trans_tok = process_translation(
                proc_client, proc_model, source_text, src_lang=src_lang, parallel=proc_parallel)
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


def process_directory(args):  # noqa
    from openai import OpenAI

    if args.no_cuda:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    converter = None
    if not args.offload:
        converter = get_converter(args)
    client = OpenAI(base_url=args.url.rstrip('/') + '/v1', api_key=args.api_key, max_retries=10)
    proc_client = client
    if args.processing_url:
        proc_client = OpenAI(
            base_url=args.processing_url.rstrip('/') + '/v1', api_key=args.api_key, max_retries=10)
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
        '--processing-parallel', type=int,
        help='Number of parallel jobs for processing tasks (OCR cleanup, '
        'translation). Defaults to --parallel value.')
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
    honor_ctrlc()
    process_directory(args)


if __name__ == '__main__':
    main()
