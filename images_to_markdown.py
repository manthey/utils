#!/usr/bin/env python3
# /// script
# requires-python = '>=3.12'
# dependencies = [
#   'pillow',
#   'openai',
#   'large-image[sources]; sys_platform == "linux"',
#   'large-image[common]; sys_platform == "win32" or sys_platform == "darwin"',
#   'large-image[pil]; sys_platform == "android"',
# ]
# ///

import argparse
import base64
import io
import os
from pathlib import Path

import large_image
import openai
import PIL.Image

MAX_DIM = 1024


def prepare_image(filepath: Path) -> str:
    try:
        ts = large_image.open(filepath)
        w, h = ts.sizeX, ts.sizeY
        img = ts.getRegion(
            output={'maxWidth': MAX_DIM, 'maxHeight': MAX_DIM},
            format=large_image.constants.TILE_FORMAT_PIL)[0]
    except Exception:
        img = PIL.Image.open(filepath)
        w, h = img.width, img.height
    img = img.convert('RGB')
    width, height = img.size
    if width > MAX_DIM or height > MAX_DIM:
        scale = MAX_DIM / max(width, height)
        img = img.resize((int(width * scale), int(height * scale)), PIL.Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=85)
    return base64.b64encode(buffer.getvalue()).decode('utf-8'), w, h


def describe_image(url: str, model: str, b64_image: str, w: int, h: int) -> str:
    client = openai.OpenAI(base_url=f'{url}/v1', api_key='ollama', timeout=300)
    messages = [{
        'role': 'system',
        'content': [{
            'type': 'text',
            'text': 'You are an image analyst who describes images so that '
            'other tools know their contents.  You never use emojis, slang, '
            'or metaphors.',
        }],
    }, {
        'role': 'user',
        'content': [{
            'type': 'text',
            'text': 'Provide a complete and detailed description of the image '
            'in markdown format.  The original image resolution is '
            f'{w} x {h} pixels (you may be provided with a reduced scale '
            'version).',
        }, {
            'type': 'image_url',
            'image_url': {'url': f'data:image/jpeg;base64,{b64_image}'},
        }],
    }]
    response = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
    message = response.choices[0].message.content
    if '```' in message:
        message = message.split('```')[1].split('\n', 1)[0]
    return message


def process_directory(
    input_dir: str, model: str, url: str, overwrite: bool, dry_run: bool,
) -> None:
    target = Path(input_dir)
    for filepath in sorted(target.rglob('*')):
        if not filepath.is_file():
            continue
        if str(filepath).endswith('.pdf'):
            continue
        md_path = filepath.with_suffix('.description.md')
        if (not overwrite and md_path.exists() and
                md_path.stat().st_mtime > filepath.stat().st_mtime):
            continue
        try:
            b64_image, w, h = prepare_image(filepath)
        except Exception:
            continue
        try:
            print(filepath)
            description = describe_image(url, model, b64_image, w, h)
            print(description)
            if not dry_run:
                md_path.write_text(description)
                print(f'Created {md_path.name}')
        except Exception as exc:
            print(f'Failed processing {filepath.name}: {exc}')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate companion markdown for images using an Ollama vision model.')
    parser.add_argument('input_dir', help='Directory containing images to process')
    parser.add_argument('--model', '-m', required=True, help='Ollama vision model name')
    parser.add_argument(
        '--url', default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
        help='Ollama base URL')
    parser.add_argument(
        '--overwrite', '-o', action='store_true',
        help='Overwrite existing companion markdown files')
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='Do not actually write markdown files')
    args = parser.parse_args()
    process_directory(args.input_dir, args.model, args.url, args.overwrite, args.dry_run)


if __name__ == '__main__':
    main()
