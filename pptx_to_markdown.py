#!/usr/bin/env python3
# /// script
# requires-python = '>=3.12'
# dependencies = [
#     'pillow',
#     'python-pptx',
#     'openai',
# ]
# ///
import argparse
import base64
import io
from pathlib import Path
from typing import Any

import openai
import PIL.Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


def describe_image(
    url: str, model: str, b64_image: str, system: str, user: str,
    options: dict[str, Any] | None = None, is_png: bool = False,
) -> str:
    client = openai.OpenAI(base_url=f'{url}/v1', api_key='ollama', timeout=300)
    messages = [{
        'role': 'system',
        'content': [{'type': 'text', 'text': system}],
    }, {
        'role': 'user',
        'content': [{
            'type': 'text',
            'text': user,
        }, {
            'type': 'image_url',
            'image_url': {'url': f'data:image/jpeg;base64,{b64_image}'},
        }],
    }]
    if not system:
        messages[0:1] = []
    response = client.chat.completions.create(
        model=model, messages=messages, **(options or {}))
    message = response.choices[0].message.content
    if '```' in message:
        message = message.split('```')[1].split('\n', 1)[-1]
    return message


def extract_text(shape) -> list[str]:
    if not shape.has_text_frame:
        return []
    paragraphs = [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
    paragraphs = [p for p in paragraphs if p != '‹#›']
    return ['\n'.join(paragraphs)] if paragraphs else []


def extract_table(shape) -> list[str]:
    if not shape.has_table:
        return []
    table = shape.table
    rows = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        rows.append(' | '.join(cells))
    return ['\n'.join(rows)] if rows else []


def process_slide(slide, slide_index: int, args) -> str:
    lines = [f'## Slide {slide_index}\n']
    text_blocks = []
    image_index = 0
    for shape in slide.shapes:
        if shape.has_text_frame:
            text_blocks.extend(extract_text(shape))
        if shape.has_table:
            text_blocks.extend(extract_table(shape))
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            image_index += 1
            img = shape.image.blob
            is_png = img.startswith(b'\x89PNG')
            if not is_png and img[:1] != b'\xff':
                img = PIL.Image.open(io.BytesIO(img))
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=95)
                img = buf.getvalue()
            b64 = base64.b64encode(img).decode('utf-8')
            description = describe_image(
                url=args.url,
                model=args.model,
                b64_image=b64,
                system=args.system,
                user=args.user,
                is_png=is_png,
            )
            text_blocks.append(f'**Image {image_index}:** {description}')
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for child in shape.shapes:
                if child.has_text_frame:
                    text_blocks.extend(extract_text(child))
                if child.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    image_index += 1
                    print('B', shape.image.blob[:60])
                    b64 = base64.b64encode(child.image.blob).decode('utf-8')
                    description = describe_image(
                        url=args.url,
                        model=args.model,
                        b64_image=b64,
                        system=args.system,
                        user=args.user,
                    )
                    text_blocks.append(f'**Image {image_index}:** {description}')
    if text_blocks:
        lines.append('\n\n'.join(text_blocks))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Convert PPTX to markdown with image descriptions.')
    parser.add_argument('pptx', type=Path, help='Path to the PPTX file')
    parser.add_argument('--url', default='http://localhost:11434', help='Ollama base URL')
    parser.add_argument('--model', default='qwen3.5:4b', help='Vision model name')
    parser.add_argument(
        '--system',
        default='You describe images concisely for document summarization '
        'and search retrieval.  You never use emojis, slang, or metaphors.',
        help='System prompt for image description')
    parser.add_argument(
        '--user', default='Describe this image in detail.',
        help='User prompt for image description')
    args = parser.parse_args()
    presentation = Presentation(args.pptx)
    print(f'# {args.pptx.name}\n')
    for index, slide in enumerate(presentation.slides, start=1):
        print(process_slide(slide, index, args))
        print()


if __name__ == '__main__':
    main()
