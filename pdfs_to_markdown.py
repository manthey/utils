#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = [
#   'docling',
#   'openai',
#   'pillow',
# ]
# ///
# This can be run via something like
# uv run --index-strategy unsafe-best-match --with torch==2.13.0+cu132
# --index https://download.pytorch.org/whl/cu132 ..scriptname.. ..options..

import argparse
import base64
import io
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

logging.getLogger('transformers').setLevel(logging.ERROR)
logging.getLogger('torch').setLevel(logging.ERROR)

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


def image_to_data_url(image):
    buffer = io.BytesIO()
    image = image.convert('L' if image.mode in {'L', 'LA'} else 'RGB')
    image.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f'data:image/png;base64,{encoded}'


def query_vision_model(client, model, image, prompt):
    response = client.chat.completions.create(
        model=model,
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': image_to_data_url(image)}},
            ],
        }],
    )
    return response.choices[0].message.content.strip()


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

    processed = 0
    for item, _ in doc.iterate_items():
        if getattr(item, 'label', None) != DocItemLabel.FORMULA:
            continue
        image = crop_item_image(doc, item)
        if image is None:
            continue
        try:
            logger.debug('Formula %d', processed + 1)
            latex = query_vision_model(client, model, image, FORMULA_PROMPT)
        except Exception as error:
            msg = f'Formula enrichment failed: {error}'
            logger.warning(msg)
            continue
        item.text = latex
        processed += 1
    msg = f'Processed {processed} formulas'
    logger.info(msg)


def enrich_pictures(doc, client, model):
    from docling_core.types.doc.document import (DescriptionMetaField,
                                                 PictureItem, PictureMeta)

    described = 0
    for item, _ in doc.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        image = item.get_image(doc) or crop_item_image(doc, item)
        if image is None:
            continue
        try:
            logger.debug('Picture %d', described + 1)
            description = query_vision_model(client, model, image, PICTURE_PROMPT)
        except Exception as error:
            msg = f'Picture description failed: {error}'
            logger.warning(msg)
            continue
        item.meta = PictureMeta(description=DescriptionMetaField(
            text=description, created_by=model))
        described += 1
    msg = f'Described {described} pictures'
    logger.info(msg)


def get_converter(args):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_page_images = True
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = args.images_scale
    pipeline_options.do_picture_classification = False
    pipeline_options.do_picture_description = True
    pipeline_options.do_code_enrichment = True
    pipeline_options.do_formula_enrichment = False
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
    )
    return converter


def process_file(converter, client, filepath, model, args):
    offload = converter is None
    if converter is None:
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
        enrich_pictures(doc, client, model)
        enrich_formulas(doc, client, model)
    finally:
        if offload:
            import requests

            try:
                requests.post(args.url.rstrip('/') + '/api/chat', json={
                    'model': args.model, 'messages': [], 'keep_alive': 0})
            except Exception:
                pass
    markdown = doc.export_to_markdown()
    return markdown


def process_directory(args):  # noqa
    from openai import OpenAI

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
                print(description)
                if not args.dry_run:
                    md_path.write_text(description, encoding='utf-8')
                    print(f'Created {md_path.name}')
                else:
                    print(f'Would have created {md_path.name}')
            except Exception as exc:
                print(f'Failed processing {filepath.name}: {exc}')
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
