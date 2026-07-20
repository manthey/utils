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
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import PIL.Image

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def describe_sequence(
    url: str, api_key: str, model: str, frames_b64: list[str],
    times: list[float], system: str, user: str, overview: str | None = None,
    previous: str | None = None, transcript: str | None = None,
    options: dict[str, Any] | None = None,
) -> tuple[str, int]:
    import openai

    client = openai.OpenAI(base_url=f'{url}/v1', api_key=api_key, timeout=300)
    content: list[dict[str, Any]] = []
    if overview:
        content.append({'type': 'text', 'text': f'Overall video context:\n{overview}'})
    if previous:
        content.append({'type': 'text', 'text': f'Previous segment description:\n{previous}'})
    if transcript:
        content.append({
            'type': 'text', 'text': f'Audio transcript for this segment:\n{transcript}'})
    content.append({'type': 'text', 'text': user})
    for i, b64 in enumerate(frames_b64):
        t = f'{times[i]:4.2f}'.rstrip('0').rstrip('.')
        content.append({'type': 'text', 'text': f'Frame at {t}s:'})
        content.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}})
    messages = [
        {'role': 'system', 'content': [{'type': 'text', 'text': system}]},
        {'role': 'user', 'content': content},
    ]
    if not system:
        messages[0:1] = []
    response = client.chat.completions.create(
        model=model, messages=messages, **(options or {}))
    message = response.choices[0].message.content
    if '```' in message:
        message = message.split('```')[1].split('\n', 1)[-1]
    tokens = response.usage.total_tokens if response.usage else 0
    return message, tokens


def get_duration(video_path: Path, ffmpeg_bin: str) -> float:
    cmd = [
        ffmpeg_bin, '-hide_banner', '-loglevel', 'info', '-i', str(video_path),
        '-map', '0:v:0', '-vf', 'showinfo', '-f', 'null', '-',
    ]
    # logger.debug('Command %s', ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    matches = re.compile(r'\bpts_time:([0-9]+(?:\.[0-9]+)?)\b').findall(
        (result.stderr or '') + '\n' + (result.stdout or ''))
    return max(0, float(matches[-1]) - 0.001) if matches else 0.0


def detect_scene_changes(video_path: Path, ffmpeg_bin: str, threshold: float) -> list[float]:
    cmd = [
        ffmpeg_bin, '-i', str(video_path),
        '-vf', f"select='gt(scene,{threshold})',showinfo",
        '-f', 'null', '-',
    ]
    # logger.debug('Command %s', ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    times = []
    for line in result.stderr.splitlines():
        if 'pts_time:' in line:
            try:
                times.append(float(line.split('pts_time:')[1].split()[0]))
            except (ValueError, IndexError):
                continue
    return times


def compute_keypoints(
    duration: float, scene_times: list[float], min_interval: float, max_interval: float,
) -> list[float]:
    candidates = sorted({0.0} | {t for t in scene_times if 0 < t < duration})
    keypoints = [0.0]
    for t in candidates[1:]:
        while t - keypoints[-1] > max_interval:
            keypoints.append(keypoints[-1] + max_interval)
        if t - keypoints[-1] >= min_interval:
            keypoints.append(t)
    while duration - keypoints[-1] > max_interval:
        keypoints.append(keypoints[-1] + max_interval)
    return keypoints


def sequence_timestamps(start: float, end: float, count: int) -> list[float]:
    if end <= start or count <= 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + step * idx for idx in range(count)]


def extract_frames_at(
    video_path: Path, ffmpeg_bin: str, timestamps: list[float], max_size: int,
) -> list[tuple[float, bytes]]:
    frames = []
    with tempfile.TemporaryDirectory() as temp_dir:
        for i, ts in enumerate(timestamps):
            out = Path(temp_dir) / f'frame_{i:04d}.jpg'
            ft = math.floor(ts * 1000) / 1000
            cmd = [
                ffmpeg_bin, '-y', '-ss', f'{ft:.3f}', '-i', str(video_path),
                '-frames:v', '1', '-q:v', '2',
            ]
            if max_size:
                cmd += [
                    '-vf',
                    f"scale='min(iw,{max_size})':'min(ih,{max_size})':"
                    'force_original_aspect_ratio=decrease',
                ]
            cmd.append(str(out))
            # logger.debug('Command %s', ' '.join(cmd))
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if out.exists():
                frames.append((ts, out.read_bytes()))
    return frames


def build_montage(frames_bytes: list[bytes], cols: int, cell_size: int) -> str:
    thumbs = []
    for data in frames_bytes:
        img = PIL.Image.open(io.BytesIO(data)).convert('RGB')
        img.thumbnail((cell_size, cell_size))
        thumbs.append(img)
    rows = math.ceil(len(thumbs) / cols)
    cell_w = max(t.width for t in thumbs)
    cell_h = max(t.height for t in thumbs)
    montage = PIL.Image.new('RGB', (cols * cell_w, rows * cell_h), (0, 0, 0))
    for i, thumb in enumerate(thumbs):
        row, col = divmod(i, cols)
        montage.paste(thumb, (col * cell_w, row * cell_h))
    buf = io.BytesIO()
    montage.save(buf, format='JPEG', quality=90)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def build_overviews(
    filepath: Path, args, ffmpeg_bin: str, duration: float,
) -> tuple[list[tuple[float, float, str]], int]:
    if not args.montage:
        return []
    interval = args.montage_interval if args.montage_interval > 0 else max(duration, 1.0)
    count = args.montage_grid * args.montage_grid
    cell_size = args.montage_max_size // args.montage_grid
    overviews = []
    start = 0.0
    max_tokens = 0
    while start < max(duration, 1.0):
        end = min(start + interval, duration) if duration else interval
        ts = sequence_timestamps(start, end, count)
        frames = extract_frames_at(filepath, ffmpeg_bin, ts, cell_size)
        if frames:
            montage_b64 = build_montage(
                [b for _, b in frames], args.montage_grid, cell_size)
            logger.info('Describing montage for %s to %s',
                        format_timestamp(start), format_timestamp(end))
            text, tokens = describe_sequence(
                url=args.url, api_key=args.api_key, model=args.model,
                frames_b64=[montage_b64], times=ts, system=args.system,
                user=args.montage_prompt)
            max_tokens = max(tokens, max_tokens)
            logger.debug(text)
            overviews.append((start, end, text))
        start = end
        if not duration:
            break
    return overviews, tokens


def overview_for(overviews: list[tuple[float, float, str]], timestamp: float) -> str | None:
    for start, end, text in overviews:
        if start <= timestamp < end:
            return text
    return overviews[-1][2] if overviews else None


def transcribe_audio(video_path: Path, whisper_model: str) -> list[dict[str, Any]]:
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(whisper_model, device='cpu', compute_type='int8')
        segments, _ = model.transcribe(str(video_path), beam_size=5)
        return [{'start': s.start, 'end': s.end, 'text': s.text.strip()} for s in segments]
    except Exception:
        logger.info('No audio')
        return []


def transcript_for_segment(
    transcript: list[dict[str, Any]], start: float, end: float,
) -> str | None:
    segments = [s for s in transcript if s['end'] > start and s['start'] < end]
    if not segments:
        return None
    return '\n'.join(
        f'[{format_timestamp(s["start"])} - {format_timestamp(s["end"])}] {s["text"]}'
        for s in segments)


def format_timestamp(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f'{mins:02d}:{secs:02d}'


def to_base64(frame_data: bytes) -> str:
    return base64.b64encode(frame_data).decode('utf-8')


def process_file(filepath: Path, args, ffmpeg_bin: str) -> str:
    desc = [f'# Video Summary: {filepath.name}\n']
    duration = get_duration(filepath, ffmpeg_bin)
    logger.info('Duration of %s is %.1f seconds', filepath.name, duration)
    scene_times = (
        detect_scene_changes(filepath, ffmpeg_bin, args.scene_threshold)
        if args.scene_threshold > 0 else [])
    logger.info('Detected %d scene changes in %s', len(scene_times), filepath.name)
    keypoints = compute_keypoints(duration, scene_times, args.min_interval, args.max_interval)
    logger.info('Using %d keypoints for %s', len(keypoints), filepath.name)
    logger.info('Transcribing audio from %s', filepath.name)
    max_tokens = 0
    transcript = transcribe_audio(filepath, args.whisper_model)
    if transcript:
        desc.append('## Audio Transcript\n')
        for segment in transcript:
            time_str = (
                f'[{format_timestamp(segment["start"])} - {format_timestamp(segment["end"])}]')
            desc.append(f'{time_str} {segment["text"]}')
        desc.append('')
    overviews, tokens = build_overviews(filepath, args, ffmpeg_bin, duration)
    max_tokens = max(tokens, max_tokens)
    if overviews:
        desc.append('## Overview\n')
        for start, end, text in overviews:
            desc.append(
                f'### Overview {format_timestamp(start)} to {format_timestamp(end)}\n\n{text}\n')
    desc.append('## Visual Timeline and Activity\n')
    previous_description = None
    for idx, start in enumerate(keypoints):
        end = keypoints[idx + 1] if idx + 1 < len(keypoints) else duration
        overview = overview_for(overviews, start)
        seq_ts = sequence_timestamps(start, end, args.frames_per_segment)
        frames = extract_frames_at(filepath, ffmpeg_bin, seq_ts, args.frame_max_size)
        frames_b64 = [to_base64(data) for _, data in frames]
        segment_transcript = transcript_for_segment(transcript, start, end)
        prompt = args.user
        logger.info('Describing segment %s to %s',
                    format_timestamp(start), format_timestamp(end))
        description, tokens = describe_sequence(
            url=args.url, api_key=args.api_key, model=args.model,
            frames_b64=frames_b64, times=seq_ts, system=args.system,
            user=prompt, overview=overview, previous=previous_description,
            transcript=segment_transcript)
        max_tokens = max(tokens, max_tokens)
        logger.debug(description)
        label = ' (Initial State)' if idx == 0 else ''
        desc.append(
            f'### Time {format_timestamp(start)} to {format_timestamp(end)}{label}\n\n'
            f'{description}\n')
        previous_description = description
    logger.info('Maximum tokens used in any one query: %d', max_tokens)
    return '\n'.join(desc)


def process_directory(args):  # noqa
    import pyffmpeg

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
        description='Generate markdown description of video content offline '
        'using scene-aware keypoints, frame sequences, a montage overview, '
        'and aligned audio transcripts.')
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
        help='If an existing directory, the location to store outputs.')
    parser.add_argument(
        '--url', default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
        help='Ollama base URL. Default %(default)s.')
    parser.add_argument(
        '--api-key', default='ollama',
        help='API key sent to the endpoint. Default %(default)s.')
    parser.add_argument(
        '--model', '-m', default='qwen3.6:35b',
        help='Vision model identifier. Default %(default)s.')
    parser.add_argument(
        '--whisper-model', default='small',
        help='Whisper model size to use for transcriptions. Default '
        '%(default)s.')
    parser.add_argument(
        '--scene-threshold', type=float, default=0.4,
        help='Scene change detection threshold from 0 to 1, 0 disables. '
        'Default %(default)s.')
    parser.add_argument(
        '--min-interval', type=float, default=2,
        help='Minimum seconds between keypoints. Default %(default)s.')
    parser.add_argument(
        '--max-interval', type=float, default=10,
        help='Maximum seconds between keypoints regardless of scene changes. '
        'Default %(default)s.')
    parser.add_argument(
        '--frames-per-segment', type=int, default=5,
        help='Number of frames sampled per segment for change analysis. '
        'Default %(default)s.')
    parser.add_argument(
        '--frame-max-size', type=int, default=1024,
        help='Longest side in pixels for analysis frames, 0 keeps original. '
        'Default %(default)s.')
    parser.add_argument(
        '--montage', action=argparse.BooleanOptionalAction, default=True,
        help='Build a montage overview to provide global context. Default '
        'enabled.')
    parser.add_argument(
        '--montage-grid', type=int, default=4,
        help='Montage grid dimension, producing this value squared tiles. '
        'Default %(default)s.')
    parser.add_argument(
        '--montage-max-size', type=int, default=1280,
        help='Longest side in pixels of the composited montage. Default '
        '%(default)s.')
    parser.add_argument(
        '--montage-interval', type=float, default=0.0,
        help='Seconds covered by each montage, 0 uses one montage for the '
        'whole video. Default %(default)s.')
    parser.add_argument(
        '--system',
        default='You describe images and identify actions, state changes, '
        'and visual transitions across sequential frames. You never use '
        'emojis, slang, or metaphors.',
        help='System prompt for descriptions.')
    parser.add_argument(
        '--user',
        default='These frames are sampled in order from one segment of the '
        'video. Describe what happens across them, including movement, '
        'actions, new elements, and scene changes.  Do not mention frame '
        'numbers or repeat what was stated earlier. If nothing significant '
        'changes, give a brief synopsis of the settings, subjects, and scene. '
        'Do not speculate on other properties or on what viewers are doing. '
        'Be precise; do not use vague terminology or state that this is a '
        'summary.',
        help='User prompt for describing a segment of frames.')
    parser.add_argument(
        '--montage-prompt',
        default='This image is a grid of still frames sampled in '
        'chronological order from a segment of a video, read left to right '
        'and top to bottom. Provide a concise overview of the setting, '
        'subjects, and overall activity.',
        help='User prompt for describing the montage overview.')
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
