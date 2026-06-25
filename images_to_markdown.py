#!/usr/bin/env python3
# /// script
# requires-python = '>=3.12'
# dependencies = [
#   'pillow',
#   'openai',
#   'pyyaml',
#   'large-image[sources]; sys_platform == "linux"',
#   'large-image[common]; sys_platform == "win32" or sys_platform == "darwin"',
#   'large-image[pil]; sys_platform == "android"',
# ]
# ///

import argparse
import base64
import io
import logging
import os
import re
import sys
from pathlib import Path

import large_image
import openai
import PIL.Image
import yaml

os.environ['GDAL_PAM_ENABLED'] = 'NO'

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

IMAGE_SIZE = 1024
DEFAULT_MODEL = 'qwen3.5:4b'
DEFAULT_SYSTEM = (
    'You are an image analyst who describes images so that other tools know '
    'their contents.  You never use emojis, slang, or metaphors.')
DEFAULT_USER = (
    'Provide a complete and detailed description of the image in markdown '
    'format.  The original image resolution is {w} x {h} pixels (you may be '
    'provided with a reduced scale version).  Describe colors only using basic '
    'color terms without adjectives (black, red, orange, yellow, green, teal, '
    'blue, purple, maroon, pink, gold, peach, beige, brown, olive, gray, '
    'lavender, magenta, lime, white) unless describing people.')


YAML_DESCRIPTION = """A yaml file can specify the LLM prompt(s).  As an example:

---
# This is a list of tasks, each with its own user prompt.
-
  # The task key is optional but allows selecting specific tasks rather than
  # running all of them
  task: walkable1
  # the command line option overrides the list of models
  models:
    - qwen3.5:9b
  # If not specified, a default system prompt is used.  Use an empty string for
  # no system prompt.
  system: >
    You are a geospatial analyst. Answer the following question about the
    provided image.
  # Always specify a user prompt
  user: Is this city walkable?
  # Images are scaled so their maximal dimension is no larger than this
  size: 2000
- task: walkable2
  models:
    - qwen3.5:9b
    - hf.co/mradermacher/Geo-R1-GGUF:Q8_0
  system: ""
  user: >
    You are a geospatial analyst. Answer the following question about the
    provided image. Is this city walkable?
  size: 1000
  # If unspecified, the default model temperature is used
  temperature: 0.15
"""


def prepare_image(filepath: Path, max_dim: int) -> tuple[str, int, int]:
    try:
        ts = large_image.open(filepath)
        # w, h = ts.sizeX, ts.sizeY
        img = ts.getRegion(
            output={'maxWidth': max_dim, 'maxHeight': max_dim},
            format=large_image.constants.TILE_FORMAT_PIL)[0]
    except Exception:
        img = PIL.Image.open(filepath)
        # w, h = img.width, img.height
    img = img.convert('RGB')
    width, height = img.width, img.height
    if width > max_dim or height > max_dim:
        scale = max_dim / max(width, height)
        img = img.resize((int(width * scale), int(height * scale)), PIL.Image.LANCZOS)
        width, height = img.width, img.height
    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=85)
    return base64.b64encode(buffer.getvalue()).decode('utf-8'), width, height


def describe_image(
    url: str, model: str, b64_image: str, system: str, user: str,
    temperature: float | None, reasoning_effort: str | None,
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
    opts = {}
    if temperature is not None:
        opts['temperature'] = temperature
    if reasoning_effort is not None:
        opts['reasoning_effort'] = reasoning_effort
    if not system:
        messages[0:1] = []
    response = client.chat.completions.create(
        model=model, messages=messages, **opts)
    message = response.choices[0].message.content
    if '```' in message:
        message = message.split('```')[1].split('\n', 1)[-1]
    return message


def load_specs(yaml_path: str | None, model_override: list[str] | None) -> list[dict]:
    if yaml_path:
        entries = yaml.safe_load(Path(yaml_path).read_text())
        specs = []
        for idx, entry in enumerate(entries, 1):
            models = model_override if model_override else entry.get('models') or [DEFAULT_MODEL]
            specs.append({
                'task': entry.get('task', f'task{idx}'),
                'models': models,
                'system': entry.get('system', DEFAULT_SYSTEM),
                'user': entry.get('user', DEFAULT_USER),
                'max_dim': entry.get('size', IMAGE_SIZE),
            })
        return specs
    return [{
        'task': 'default',
        'models': [model_override or DEFAULT_MODEL],
        'system': DEFAULT_SYSTEM,
        'user': DEFAULT_USER,
        'max_dim': IMAGE_SIZE,
    }]


def describe_file(url: str, specs: list[dict], filepath: Path) -> str:
    logger.info('Processing %s', url)
    total = sum(len(spec['models']) for spec in specs)
    cache: dict[int, tuple[str, int, int]] = {}
    blocks = []
    for spec in specs:
        if spec['max_dim'] not in cache:
            cache[spec['max_dim']] = prepare_image(filepath, spec['max_dim'])
        b64_image, w, h = cache[spec['max_dim']]
        user = spec['user'].format(w=w, h=h)
        logger.info(' Asking on %d x %d image: %s', w, h, user)
        for model in spec['models']:
            logger.info(' Running %s', model)
            try:
                response = describe_image(
                    url, model, b64_image, spec['system'], user,
                    spec.get('temperature'), spec.get('reasoning_effort'))
            except Exception:
                continue
            if total > 1:
                blocks.append(f'# {model}\n**Prompt**: {user}\n\n{response}')
            else:
                blocks.append(response)
            logger.info(' Response: %s', blocks[-1])
    return '\n\n---\n\n'.join(blocks)


def process_directory(
    input_dir: str, suffix: str, specs: list[dict], url: str, overwrite: bool,
    dry_run: bool,
) -> None:
    suffix = f'.{suffix.lstrip(".")}'
    target = Path(input_dir)
    for filepath in (sorted(target.rglob('*')) if not target.is_file() else [target]):
        if not filepath.is_file():
            continue
        if str(filepath).endswith('.pdf'):
            continue
        md_path = filepath.with_suffix(suffix)
        if (not overwrite and md_path.exists() and
                md_path.stat().st_mtime > filepath.stat().st_mtime):
            continue
        try:
            print(filepath)
            description = describe_file(url, specs, filepath)
            print(description)
            if not dry_run:
                md_path.write_text(description)
                print(f'Created {md_path.name}')
        except Exception as exc:
            print(f'Failed processing {filepath.name}: {exc}')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate companion markdown or text for images using an '
        'Ollama vision model.')
    parser.add_argument('input_dir', help='Directory containing images to process')
    parser.add_argument(
        '--yaml', help='YAML file containing a list of prompt specifications.')
    parser.add_argument(
        '--example-yaml', action='store_true', help='Show an example YAML file.')
    parser.add_argument(
        '--task', '-t', action='append',
        help='Run the matching task; can be specified multiple times')
    parser.add_argument(
        '--task-regex', '-r',
        help='Regular expression to match task names')
    parser.add_argument(
        '--list-tasks', action='store_true',
        help='List tasks from the YAML file')
    parser.add_argument(
        '--suffix', '--ext', default='.description.md',
        help='File extension to use for description files.')
    parser.add_argument(
        '--model', '-m', default=None, action='append',
        help='Ollama vision model name; overrides models in the yaml spec')
    parser.add_argument(
        '--url', default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
        help='Ollama base URL')
    parser.add_argument(
        '--overwrite', '-o', action='store_true',
        help='Overwrite existing companion markdown files')
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='Do not actually write markdown files')
    parser.add_argument(
        '--verbose', '-v', action='count', default=0,
        help='Increase verbosity')
    args = parser.parse_args()
    if args.example_yaml:
        print(YAML_DESCRIPTION)
        sys.exit(0)
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.debug('Parsed arguments: %r', args)
    specs = load_specs(args.yaml, args.model)
    if args.task or args.task_regex:
        specs = [spec for spec in specs
                 if spec['task'] in (args.task or []) or
                 (args.task_regex and re.search(args.task_regex, spec['task']))]
    if args.list_tasks:
        print('Tasks')
        for task in sorted(spec['task'] for spec in specs):
            print(f'  {task}')
        sys.exit(0)
    process_directory(args.input_dir, args.suffix, specs, args.url, args.overwrite, args.dry_run)


if __name__ == '__main__':
    main()
