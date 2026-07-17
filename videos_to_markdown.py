#!/usr/bin/env python3
# /// script
# requires-python = '>=3.12'
# dependencies = [
#     'pillow',
#     'openai',
#     'pyffmpeg',
#     'faster-whisper',
# ]
# ///
import argparse
import base64
import io
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import PIL.Image
import pyffmpeg
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def describe_changes(
    url: str, model: str, b64_frame1: str, b64_frame2: str, system: str, user: str,
    options: dict[str, Any] | None = None,
) -> str:
    import openai

    client = openai.OpenAI(base_url=f'{url}/v1', api_key='ollama', timeout=300)
    messages = [{
        'role': 'system',
        'content': [{'type': 'text', 'text': system}],
    }, {
        'role': 'user',
        'content': [
            {'type': 'text', 'text': f'{user}\n\nImage 1 (earlier):'},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64_frame1}'}},
            {'type': 'text', 'text': 'Image 2 (later):'},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64_frame2}'}},
        ],
    }]
    if not system:
        messages[0:1] = []
    response = client.chat.completions.create(
        model=model, messages=messages, **(options or {}))
    message = response.choices[0].message.content
    if '```' in message:
        message = message.split('```')[1].split('\n', 1)[-1]
    return message


def describe_single_image(
    url: str, model: str, b64_image: str, system: str, user: str,
    options: dict[str, Any] | None = None,
) -> str:
    import openai

    client = openai.OpenAI(base_url=f'{url}/v1', api_key='ollama', timeout=300)
    messages = [{
        'role': 'system',
        'content': [{'type': 'text', 'text': system}],
    }, {
        'role': 'user',
        'content': [
            {'type': 'text', 'text': user},
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64_image}'}},
        ],
    }]
    if not system:
        messages[0:1] = []
    response = client.chat.completions.create(
        model=model, messages=messages, **(options or {}))
    message = response.choices[0].message.content
    if '```' in message:
        message = message.split('```')[1].split('\n', 1)[-1]
    return message


def extract_frames(video_path: Path, ffmpeg_bin: str, interval: int) -> list[tuple[float, bytes]]:
    frames = []
    with tempfile.TemporaryDirectory() as temp_dir:
        output_pattern = Path(temp_dir) / 'frame_%04d.jpg'
        cmd = [
            ffmpeg_bin,
            '-y',
            '-i', str(video_path),
            '-vf', f'fps=1/{interval}',
            '-vsync', 'vfr',
            '-q:v', '2',
            str(output_pattern),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        for i, frame_file in enumerate(sorted(Path(temp_dir).glob('*.jpg'))):
            timestamp = i * interval
            with open(frame_file, 'rb') as f:
                frames.append((timestamp, f.read()))
    return frames


def transcribe_audio(video_path: Path, whisper_model: str) -> list[dict[str, Any]]:
    try:
        model = WhisperModel(whisper_model, device='cpu', compute_type='int8')
        segments, _ = model.transcribe(str(video_path), beam_size=5)
        return [{'start': s.start, 'end': s.end, 'text': s.text.strip()} for s in segments]
    except Exception as exc:
        logger.warning('Audio transcription failed or no audio present: %s', exc)
        return []


def format_timestamp(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f'{mins:02d}:{secs:02d}'


def to_jpeg_base64(frame_data: bytes) -> str:
    img = PIL.Image.open(io.BytesIO(frame_data))
    if img.mode in {'RGBA', 'P', 'LA'}:
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def process_file(filepath: Path, args, ffmpeg_bin: str) -> str:
    desc = [f'# Video Summary: {filepath.name}\n']
    logger.info('Extracting frames from %s', filepath.name)
    frames = extract_frames(filepath, ffmpeg_bin, args.frame_interval)
    logger.info('Transcribing audio from %s', filepath.name)
    transcript = transcribe_audio(filepath, args.whisper_model)
    if transcript:
        desc.append('## Audio Transcript\n')
        for segment in transcript:
            time_str = (
                f'[{format_timestamp(segment["start"])} - {format_timestamp(segment["end"])}]')
            desc.append(f'{time_str} {segment["text"]}')
        desc.append('')
    desc.append('## Visual Timeline and Activity\n')
    previous_b64 = None
    for idx, (timestamp, frame_data) in enumerate(frames):
        time_str = format_timestamp(timestamp)
        current_b64 = to_jpeg_base64(frame_data)
        if idx == 0:
            logger.info('Describing initial frame at %s', time_str)
            description = describe_single_image(
                url=args.url,
                model=args.model,
                b64_image=current_b64,
                system=args.system,
                user=args.user,
            )
            desc.append(f'### Time {time_str} (Initial State)\n\n{description}\n')
        else:
            prev_time_str = format_timestamp(frames[idx - 1][0])
            logger.info('Describing changes from %s to %s', prev_time_str, time_str)
            description = describe_changes(
                url=args.url,
                model=args.model,
                b64_frame1=previous_b64,
                b64_frame2=current_b64,
                system=args.system,
                user=args.change_prompt,
            )
            desc.append(f'### Time {prev_time_str} to {time_str}\n\n{description}\n')
        previous_b64 = current_b64
    return '\n'.join(desc)


def process_directory(args):  # noqa
    suffix = f'.{args.suffix.lstrip(".")}'
    ffmpeg_bin = pyffmpeg.FFmpeg().get_ffmpeg_bin()
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
            if filepath.suffix.lower() not in {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv'}:
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
                description = process_file(filepath, args, ffmpeg_bin)
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
        description='Generate markdown summary of video content offline comparing adjacent frames.')
    parser.add_argument(
        'inputs', nargs='+',
        help='One or more files or directories to process.')
    parser.add_argument(
        '--recurse', '-r', action='store_true',
        help='Recurse into input directories')
    parser.add_argument(
        '--suffix', '--ext', default='.summary.md',
        help='File extension to use for description files.')
    parser.add_argument(
        '--out', '--output',
        help='If an existing directory, the location to store outputs.')
    parser.add_argument(
        '--url', default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
        help='Ollama base URL. Default %(default)s.')
    parser.add_argument(
        '--api-key', default='ollama',
        help='API key sent to the endpoint. Default %(default)s.')
    parser.add_argument(
        '--model', '-m', default='qwen2.5vl:7b',
        help='Vision model identifier. Default %(default)s.')
    parser.add_argument(
        '--whisper-model', default='base',
        help='Whisper model size to use for transcriptions. Default %(default)s.')
    parser.add_argument(
        '--frame-interval', type=int, default=10,
        help='Extract frames every N seconds. Default %(default)s.')
    parser.add_argument(
        '--system',
        default='You describe images and identify actions, state changes, '
        'and visual transitions between sequential frames. You never use '
        'emojis, slang, or metaphors.',
        help='System prompt for image description')
    parser.add_argument(
        '--user', default='Describe this initial image in detail.',
        help='User prompt for describing the first frame.')
    parser.add_argument(
        '--change-prompt',
        default='Compare Image 1 and Image 2. Describe what has changed, '
        'including any movement, actions, new elements, or scene changes. '
        'If no significant activity occurred, state that the frame is '
        'identical or near-identical.',
        help='User prompt for comparing sequential frames.')
    parser.add_argument(
        '--overwrite', '-y', action='store_true',
        help='Overwrite existing companion markdown files')
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='Do not actually write markdown files')
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
