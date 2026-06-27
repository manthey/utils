#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "openai",
#     "python-dateutil",
#     "pyyaml",
#     "requests",
# ]
# ///

import argparse
import base64
import datetime
import html
import json
import multiprocessing
import os
import queue
import re
import signal
import struct
import subprocess
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import dateutil.parser
import requests
import yaml
from openai import OpenAI

ClientKwargs = {}


@dataclass
class TestResult:
    passed: bool | list[int, int] | None
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    timestamp: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class TestDefinition:
    name: str
    description: str
    skip: bool | str
    version: int
    run: Callable[[OpenAI, str, str], TestResult]


TEST_REGISTRY: list[TestDefinition] = []


def register_test(name: str, description: str, skip: bool | str = False, version: int = 0):
    def decorator(func: Callable[[OpenAI, str, str], TestResult]) -> Callable:
        func._version = version
        TEST_REGISTRY.append(TestDefinition(
            name=name, description=description, run=func, skip=skip, version=version,
        ))
        return func

    return decorator


def extract_vision_information(  # noqa
    model_info: dict[str, Any], architecture: str, families: list[str],
    capabilities: list[str], proj: Any, tensors: Any,
) -> dict[str, Any] | None:
    has_vision = (
        any(f.lower() in ('clip', 'siglip', 'mllama', 'moonshot') for f in families) or
        'vision' in capabilities or
        any('vision' in k.lower() or 'clip' in k.lower() or 'projector' in k.lower()
            for k in model_info) or proj is not None)
    if not has_vision:
        return None
    longest_key = next(
        (k for k in model_info if k.endswith('.longest_edge')), None)
    if longest_key:
        vp = longest_key.replace('.longest_edge', '')
        return {
            'min_pixels': int(model_info.get(f'{vp}.shortest_edge', 0)),
            'max_pixels': int(model_info.get(f'{vp}.longest_edge', 0)),
            'fixed_aspect_ratio': False,
        }
    for key, value in list(model_info.items()) + list(proj.items() if proj else []):
        lower = key.lower()
        if lower.endswith(('.image_size', '.resolution')):
            try:
                return {
                    'min_pixels': int(value) * int(value),
                    'max_pixels': int(value) * int(value),
                    'fixed_aspect_ratio': True,
                }
            except (ValueError, TypeError):
                pass
    patch = None
    position = None
    for t in tensors or []:
        lower = t['name'].lower()
        value = t['shape']
        if 'patch_embd' in lower.split('.'):
            patch = value
        if 'position_embd' in lower.split('.'):
            position = value
    if patch and position:
        try:
            return {
                'min_pixels': int(patch[0] * patch[1]),
                'max_pixels': int(patch[0] * patch[1] * position[1]),
                'fixed_aspect_ratio': False,
            }
        except Exception:
            pass
    known = {
        'granite': 384, 'llava': 336, 'moondream': 378,
        'bakllava': 336, 'nanollava': 384, 'obsidian': 384,
        'qwen2vl': (56 ** 2, 3584 ** 2),
        'qwen35': (256 ** 2, 4096 ** 2),
        'internvl': (64 ** 2, 4096 ** 2),
        'minicpmv': (49152, 1128_960),
    }
    for name, res in known.items():
        if name in architecture.lower():
            if isinstance(res, int):
                return {
                    'min_pixels': res * res,
                    'max_pixels': res * res,
                    'fixed_aspect_ratio': True,
                }
            return {
                'min_pixels': res[0],
                'max_pixels': res[1],
                'fixed_aspect_ratio': False,
            }


def get_model_metadata(ollama_base_url: str, model_name: str) -> dict[str, Any]:
    response = requests.post(
        f'{ollama_base_url}/api/show',
        json={'name': model_name, 'verbose': True},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    details = data.get('details') or {}
    model_info = data.get('model_info') or {}
    parameters_text = data.get('parameters') or ''
    capabilities = [c.lower() for c in data.get('capabilities', []) or
                    data.get('details', {}).get('capabilities', [])]
    template = data.get('template') or ''
    parameter_size = details.get('parameter_size', 'unknown')
    parameter_count = model_info.get('general.parameter_count')
    context_length = None
    for key, value in model_info.items():
        if key.endswith('.context_length'):
            context_length = value
            break
    for line in parameters_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == 'num_ctx':
            try:
                context_length = int(parts[1])
            except ValueError:
                pass
    quantization = details.get('quantization_level', 'unknown')
    family = details.get('family', 'unknown')
    families = details.get('families') or []
    model_format = details.get('format', 'unknown')
    modified_at = data.get('modified_at', 'unknown')
    source = determine_source(model_name)
    has_vision = any(
        f.lower() in ('clip', 'mllama') for f in families
    ) or any(
        'clip' in k.lower() or 'vision' in k.lower() or 'projector' in k.lower()
        for k in model_info
    ) or 'vision' in capabilities or 'projector_info' in data
    has_tool = (
        '.Tools' in template or 'tools' in template.lower() or
        'tool_call' in template.lower() or 'tools' in capabilities
    )
    has_reasoning = (
        '<think>' in template or '<|thinking|>' in template or
        'thinking' in capabilities or 'reasoning' in capabilities)
    vision = extract_vision_information(
        model_info, details.get('architecture', '').lower(), families,
        capabilities, data.get('projector_info', {}), data.get('tensors', {}))
    return {
        'name': model_name,
        'source': source,
        'family': family,
        'families': families,
        'format': model_format,
        'parameter_size': parameter_size,
        'parameter_count': parameter_count,
        'context_length': context_length,
        'quantization': quantization,
        'has_vision': has_vision,
        'has_tool': has_tool,
        'has_reasoning': has_reasoning,
        'has_embedding': 'embedding' in capabilities,
        'modified_at': modified_at,
        **(vision or {}),
    }


def determine_source(model_name: str) -> str:
    if model_name.startswith('hf.co/') or 'huggingface' in model_name.lower():
        return 'huggingface'
    return 'ollama'


def generate_red_png_base64() -> str:
    width = 64
    height = 64
    scanlines = b''
    for _ in range(height):
        scanlines += b'\x00' + (b'\xff\x00\x00' * width)
    compressed = zlib.compress(scanlines)

    def make_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
        content = chunk_type + chunk_data
        crc = struct.pack('>I', zlib.crc32(content) & 0xFFFFFFFF)
        return struct.pack('>I', len(chunk_data)) + content + crc

    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    png = b'\x89PNG\r\n\x1a\n'
    png += make_chunk(b'IHDR', ihdr_data)
    png += make_chunk(b'IDAT', compressed)
    png += make_chunk(b'IEND', b'')
    return base64.b64encode(png).decode('ascii')


def fence_code_block(text: str) -> str:
    longest_backtick_run = 0
    current_run = 0
    for char in text:
        if char == '`':
            current_run += 1
            longest_backtick_run = max(longest_backtick_run, current_run)
        else:
            current_run = 0
    fence = '`' * max(3, longest_backtick_run + 1)
    return f'{fence}\n{text}\n{fence}'


def chat_completion_with_usage(*args, **kwargs) -> dict[str, Any]:
    result = chat_completion(*args, **kwargs)
    if not result.get('usage') and not result.get('timeout') and 'use_stream' not in kwargs:
        try:
            with_usage = chat_completion(*args, use_stream=False, **kwargs)
            result['usage'] = with_usage.get('usage')
        except Exception:
            pass
    return result


def chat_completion_worker(  # noqa
    client: OpenAI,
    model_name: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int = 16384,
    tools: list[dict] | None = None,
    use_stream: bool = True,
    reasoning_effort: str | None = None,
    queue=None,
) -> dict[str, Any]:
    start = time.time()
    kwargs: dict[str, Any] = {
        'model': model_name,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'stream': use_stream,
        'tools': tools,
        'reasoning_effort': reasoning_effort,
    }
    if use_stream:
        kwargs['stream_options'] = {'include_usage': True}
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    sys.stdout.write('.')
    sys.stdout.flush()
    response = client.chat.completions.create(**kwargs)
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage = None

    def snapshot() -> dict[str, Any]:
        return {'content': ''.join(content_parts), 'tool_calls': tool_calls,
                'usage': usage, 'duration': time.time() - start}

    for chunk in (response if use_stream else [response]):
        if use_stream:
            delta = chunk.choices[0].delta if chunk.choices else None
        else:
            delta = chunk.choices[0].message
        if chunk.usage is not None:
            usage = {
                'prompt_tokens': chunk.usage.prompt_tokens,
                'completion_tokens': chunk.usage.completion_tokens,
                'total_tokens': chunk.usage.total_tokens,
            }
        if not delta:
            continue
        if delta.content:
            content_parts.append(delta.content)
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                index = tc_delta.index or 0
                while len(tool_calls) <= index:
                    tool_calls.append({
                        'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                tc = tool_calls[index]
                if tc_delta.id:
                    tc['id'] = tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        tc['function']['name'] += tc_delta.function.name
                    if tc_delta.function.arguments:
                        tc['function']['arguments'] += tc_delta.function.arguments
        if queue is not None and use_stream:
            queue.put(snapshot())
    return snapshot()


def chat_completion_wrapper(
    queue: multiprocessing.Queue,
    client_kwargs,
    model_name: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    tools: list[dict] | None,
    use_stream: bool,
    reasoning_effort: str | None = None,
) -> None:
    client = OpenAI(**client_kwargs)
    try:
        queue.put(chat_completion_worker(
            client, model_name, messages, temperature, max_tokens, tools, use_stream,
            reasoning_effort, queue))
    except BaseException as exc:
        queue.put(exc)


def chat_completion(  # noqa
    client: OpenAI,
    model_name: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int = 16384,
    tools: list[dict] | None = None,
    use_stream: bool = True,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    timeout = ClientKwargs.get('timeout', 300)
    mp_queue = multiprocessing.Queue()
    start = time.time()
    process = multiprocessing.Process(
        target=chat_completion_wrapper,
        args=(mp_queue, ClientKwargs, model_name, messages,
              temperature, max_tokens, tools, use_stream, reasoning_effort),
    )
    process.start()
    last = None
    timed_out = False
    try:
        while process.is_alive():
            process.join(timeout=0.1)
            while True:
                try:
                    item = mp_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, BaseException):
                    raise item
                last = item
            if time.time() - start > timeout:
                timed_out = True
                break
    except KeyboardInterrupt:
        process.terminate()
        process.join(timeout=1)
        raise
    if timed_out:
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    while True:
        try:
            item = mp_queue.get_nowait()
        except queue.Empty:
            break
        if isinstance(item, BaseException):
            raise item
        last = item
    if last is None:
        msg = 'Timeout exceeded' if timed_out else 'No result'
        raise TimeoutError(msg)
    if timed_out:
        last['timeout'] = True
    return last


def extract_answer_from_reasoning(content: str) -> str:
    if '</think>' in content:
        return content.split('</think>')[-1].strip()
    return content.strip()


@register_test('first_load', 'First load and memory use')
def test_first_load(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    try:
        result = chat_completion(
            client,
            model_name,
            messages=[{'role': 'user', 'content': 'Echo "this is a test"'}],
            max_tokens=1,
            use_stream=True,
            reasoning_effort='none',
        )
    except Exception:
        try:
            start = time.time()
            sys.stdout.write('.')
            sys.stdout.flush()
            client.embeddings.create(model=model_name, input='Echo "this is a test"')
            result = {'duration': time.time() - start}
        except Exception:
            pass
    ps = requests.get(f'{ollama_base_url}/api/ps', timeout=10)
    ps.raise_for_status()
    ps = ps.json()
    found = None
    for model in ps['models']:
        if model['name'] == model_name:
            found = model
    if not found:
        for model in ps['models']:
            if model['name'].startswith(model_name):
                found = model
    return TestResult(
        passed=True, output=ps,
        metadata={
            'size': found['size'],
            'size_vram': found['size_vram'],
            'vram_percentage': round(100 * found['size_vram'] / found['size'], 2),
            'context_length': found['context_length'],
        },
        details={
            'duration': result.get('duration'),
        },
        timestamp=get_timestamp(),
        usage=result.get('usage'),
    )


def result_passed(result: TestResult):
    return result.passed is True or (
        isinstance(result.passed, (tuple, list)) and result.passed[0] == result.passed[1])


def result_complete_fail(result: TestResult):
    return result.passed is False or (
        isinstance(result.passed, (tuple, list)) and result.passed[0] == 0)


def chat_test(client: OpenAI, model_name: str, test):
    effort = test['chat'].get('reasoning_effort')
    effort = [effort] if effort is not None else ['none', 'low', 'medium', 'high']
    best = None
    for eff in effort:
        etest = test.copy()
        etest['chat'] = etest['chat'].copy()
        etest['chat']['reasoning_effort'] = eff
        try:
            res = chat_test_worker(client, model_name, etest)
        except TimeoutError:
            raise
        except Exception:
            if best is not None:
                return best
            raise
        if len(effort) > 1:
            res.details['reasoning_effort'] = eff
        if result_passed(res):
            return res
        best = best or res
        if (isinstance(best.passed, (tuple, list)) and isinstance(res.passed, (tuple, list)) and
                res.passed[0] > best.passed[0]):
            best = res
    return best


def chat_test_worker(client: OpenAI, model_name: str, test):
    result = chat_completion_with_usage(
        client,
        model_name,
        **test['chat'],
    )
    raw_answer = result['content']
    answer = extract_answer_from_reasoning(raw_answer)
    details = {'extracted_answer': answer,
               'duration': result.get('duration')}
    count = needed = 0
    for exp in test.get('present', []):
        needed += 1
        found = re.search(exp, answer)
        details[f'{exp} present'] = bool(found)
        if bool(found):
            count += 1
    for exp in test.get('absent', []):
        needed += 1
        found = re.search(exp, answer)
        details[f'{exp} absent'] = not bool(found)
        if not bool(found):
            count += 1
    found = re.search(
        r'['
        r'\U0001F300-\U0001F9FF'  # Misc symbols, emoticons, transport
        r'\U0001FA00-\U0001FAFF'  # Chess, shapes, symbols extended
        r'\U00002600-\U000027BF'  # Misc symbols, dingbats
        r'\U0001F000-\U0001F02F'  # Games
        r'\U0001F0A0-\U0001F0FF'  # Playing cards
        r'\uFE00-\uFE0F'          # Variation selectors
        r'\u2460-\u24FF'          # Circled letters
        r'\u2500-\u259F'          # Box drawing, block elements
        r'\u2190-\u21FF'          # Arrows
        r'\u2700-\u27BF'          # Stars, bullets, decorative marks
        r'\uFE0F'                 # Variation selector 16
        r'\u2610\u2611\u2612'     # Checkboxes
        r'\u2713\u2717\u2718'     # Check and cross marks
        r'\u25C9\u25CB\u25CF'     # Radio-button style bullets
        r'\u25B6\u25B7\u25BA'     # Decorative arrow bullets
        r'\u2022\u2023\u2043'     # Fancy bullets
        r']',
        answer,
        re.UNICODE,
    )

    details['Disallowed characters absent'] = not bool(found)
    needed += 1
    if not found:
        count += 1
    passed = [count, needed]
    return TestResult(
        passed=passed, output=raw_answer, details=details,
        timestamp=get_timestamp(),
        usage=result.get('usage'),
    )


def bash_test(client: OpenAI, model_name: str, test):  # noqa
    commands = test['bash']
    start_commands = test.get('start', [])
    stop_commands = test.get('stop', [])
    env = os.environ.copy()
    localenv = {str(k): str(v if v != '{model}' else model_name)
                for k, v in test.get('env', {}).items()}
    env.update(localenv)
    timeout = test.get('timeout', ClientKwargs.get('timeout', 300))
    output = ''
    needed = len(commands)
    count = 0
    command_details = []
    details = {}
    start = time.time()
    stop = False
    for stage, cmds in [('start', start_commands), ('main', commands), ('stop', stop_commands)]:
        if stage != 'stop' and stop:
            continue
        for command in cmds:
            command_start = time.time()
            command = command.replace('{model}', model_name)
            try:
                result = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    env=env, cwd=test.get('cwd'), timeout=timeout,
                    encoding='utf8')
                if stage == 'main':
                    output = (result.stdout or '') + (result.stderr or '')
                return_code = result.returncode
            except Exception as exc:
                if stage == 'main':
                    try:
                        output = (result.stdout or '') + (result.stderr or '')
                    except Exception:
                        output = ''
                    if len(output) > 65536:
                        output = output[:32768] + '\n...\n' + output[-32768:]
                    if isinstance(exc, subprocess.TimeoutExpired):
                        output += f'\nTimeout after {timeout} seconds'
                return_code = None
            if stage == 'main':
                command_details.append({
                    'command': command, 'return_code': return_code,
                    'duration': time.time() - command_start})
            if return_code != 0:
                stop = True
                break
            if stage == 'main':
                count += 1
    answer = output if count >= needed - 1 else None
    for exp in test.get('present', []):
        needed += 1
        if answer is None:
            continue
        found = re.search(exp, answer)
        details[f'{exp} present'] = bool(found)
        if bool(found):
            count += 1
    for exp in test.get('absent', []):
        needed += 1
        if answer is None:
            continue
        found = re.search(exp, answer)
        details[f'{exp} absent'] = not bool(found)
        if not bool(found):
            count += 1
    details.update({
        'extracted_answer': re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', output.strip()),
        'commands': json.dumps(command_details),
        'duration': time.time() - start})
    return TestResult(
        passed=[count, needed], output=output.strip(),
        details=details,
        timestamp=get_timestamp())


@register_test('basic_question', 'Basic question answering', version=1)
def test_basic_question(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'user',
            'content': 'What is the capital of France? Answer with only the city name.',
        }]},
        'present': [r'(?i)\bparis\b'],
    })


@register_test('coding', 'Basic code generation')
def test_coding(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a helpful assistant who never uses metaphors, slang, emojis, '
        'or decorative characters. You will answer only the questions asked, '
        'and not offer to do additional work. Your code is impeccably correct '
        'and carefully considered, using clear variable names and few to no '
        'comments.')
    prompt = (
        "Write a Python function called 'fibonacci' that takes an integer n "
        'and returns the nth Fibonacci number (0-indexed, so fibonacci(0)=0, '
        'fibonacci(1)=1, fibonacci(6)=8). Use iteration, not recursion. '
        'Return only the function with no explanation.')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'def fibonacci', r'return'],
    })


@register_test('python_argparse', 'Python argparse use', version=1)
def test_code_python_argparse(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a helpful assistant who never uses metaphors, slang, emojis, '
        'or decorative characters. You will answer only the questions asked, '
        'and not offer to do additional work. Your code is impeccably correct '
        'and carefully considered, using clear variable names and few to no '
        'comments.')
    prompt = (
        'Is there a way to programmatically have the default in a python '
        'argparse help string without defining it in another statement?  That '
        "is, I have `parser.add_argument('command', nargs='?', "
        "default='create', choices=['create', 'list'],  help='Command. "
        'Defaults to "..."\')`, and I want that `...` to be taken from the '
        'specified default.')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'%\(default\)s'],
    })


@register_test('python_yaml', 'Python yaml library use', version=1)
def test_code_python_yaml(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a helpful assistant who never uses metaphors, slang, emojis, '
        'or decorative characters. You will answer only the questions asked, '
        'and not offer to do additional work. Your code is impeccably correct '
        'and carefully considered, using clear variable names and few to no '
        'comments.')
    prompt = (
        'Write a Python program that uses pep 723 (inline script metadata) '
        'and argparse to take a yaml or json input file (as the first command '
        'line parameter), and output either yaml or json, either as compact '
        'as possible or nicely formatted; for instance, if outputting yaml, '
        'the compact form could deduplicate repeated data, but the nice '
        'formatting would not.')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'argparse', r'(?i)pyyaml', r'import yaml', r'safe_load', r'/// script'],
    })


@register_test('code_editing', 'Code editing', version=1)
def test_code_editing(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a helpful assistant who never uses metaphors, slang, emojis, '
        'or decorative characters. You will answer only the questions asked, '
        'and not offer to do additional work. Your code is impeccably correct '
        'and carefully considered, using clear variable names and few to no '
        'comments.')
    prompt = (
        'Below is a program to test llm models and generate model cards. '
        'Modify the `basic_question` method to test for the capital of '
        'Canada rather than France.  Remember, more compact code with clear '
        'variables and few to no comments is preferred. Never use emojis, '
        'slang, or metaphors. Do not prefix variables or functions with '
        'underscores unless they are unused. Do not add separator comments. '
        'Do not add needless blank lines inside functions.  Show code changes '
        'less than 50 lines in git diff format, more than 100 lines as '
        'complete files.')
    src = open(os.path.realpath(__file__), encoding='utf-8').read()
    prompt += (
        f'\n\n##### File: {os.path.basename(__file__)}\n```' + '`python\n' +
        src.strip() + '\n`' + '```\n')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'diff', r'@@', r'\n\+'],
    })


@register_test('java_simple', 'Basic java question')
def test_java_simple(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a helpful assistant who never uses metaphors, slang, emojis, '
        'or decorative characters. You will answer only the questions asked, '
        'and not offer to do additional work. Your code is impeccably correct '
        'and carefully considered.')
    prompt = (
        'In java I have a `java.util.LinkedHashMap`. What is the most '
        'efficient, compact way to get the 0-based index of a key within the '
        '`LinkedHashMap`?  Just show code to get the position, no '
        'commentary, and no need to show surrounding code.  That is, the '
        '`LinkedHashMap` might be `<string, string>`, and the keys could be '
        'in order alpha, beta, gamma, delta, epsilon, then I want, given a '
        'string, get the position, so delta would be 3.')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'new ArrayList', r'keySet', r'indexOf'],
    })


@register_test('embedding', 'Embedding generation support', version=1)
def test_embedding(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    sys.stdout.write('.')
    sys.stdout.flush()
    response = client.embeddings.create(
        model=model_name,
        input='The quick brown fox jumps over the lazy dog.',
    )
    vector = response.data[0].embedding
    dimensions = len(vector)
    has_nonzero = any(v != 0.0 for v in vector)
    results = [dimensions > 0, has_nonzero]
    passed = [len([r for r in results if r]), len(results)]
    ps = requests.get(f'{ollama_base_url}/api/ps', timeout=10)
    ps.raise_for_status()
    ps = ps.json()
    found = None
    for model in ps['models']:
        if model['name'] == model_name:
            found = model
    return TestResult(
        passed=passed,
        output=f'Generated embedding with {dimensions} dimensions',
        metadata={
            'embedding_dimensions': dimensions,
        },
        details={
            'has_nonzero_values': has_nonzero,
            'size': found['size'],
            'size_vram': found['size_vram'],
            'vram_percentage': round(100 * found['size_vram'] / found['size'], 2),
            'context_length': found['context_length'],
        },
        timestamp=get_timestamp(),
    )


@register_test('vision', 'Image understanding')
def test_vision(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': 'What color is this image? Answer with only the color name.',
                }, {
                    'type': 'image_url',
                    'image_url': {'url': f'data:image/png;base64,{generate_red_png_base64()}'},
                },
            ],
        }]},
        'present': [r'(?i)red'],
    })


@register_test('histology', 'Histology image understanding')
def test_histology(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    img = base64.b64encode(open(os.path.join(os.path.dirname(
        __file__), 'model_card_test_image.png'), 'rb').read()).decode('utf-8')
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': 'Describe the biology visible in this image.',
                }, {
                    'type': 'image_url',
                    'image_url': {'url': f'data:image/png;base64,{img}'},
                },
            ],
        }]},
        'present': [r'(?i)cell', r'(?i)nuclei'],
    })


@register_test('photo', 'Photo understanding', version=1)
def test_photo(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    img = base64.b64encode(open(os.path.join(os.path.dirname(
        __file__), 'model_card_test_image2.png'), 'rb').read()).decode('utf-8')
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': 'Describe what is in this photograph.  What color are the eyes?',
                }, {
                    'type': 'image_url',
                    'image_url': {'url': f'data:image/png;base64,{img}'},
                },
            ],
        }]},
        'present': [r'(?i)dog', r'(?i)(brindle|mix)', r'(?i)(hazel|brown)'],
    })


@register_test('geospatial_image', 'Geospatial understanding', version=2)
def test_geospatial_image(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    img = base64.b64encode(open(os.path.join(os.path.dirname(
        __file__), 'model_card_test_image3.jpg'), 'rb').read()).decode('utf-8')
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': 'Describe what is in this photograph in three sentences.',
                }, {
                    'type': 'image_url',
                    'image_url': {'url': f'data:image/jpeg;base64,{img}'},
                },
            ],
        }]},
        'present': [r'(?i)(road|street)'],
    })


@register_test('geospatial_analysis', 'Geospatial image description for tools', version=2)
def test_geospatial_analysis(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    img = base64.b64encode(open(os.path.join(os.path.dirname(
        __file__), 'model_card_test_image3.jpg'), 'rb').read()).decode('utf-8')
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'system',
            'content': [{
                'type': 'text',
                'text': 'You are an image analyst who describes images so that '
                'other tools know their contents.  You never use emojis, '
                'slang, or metaphors.',
            }],
        }, {
            'role': 'user',
            'content': [{
                'type': 'text',
                'text': 'Provide a complete and detailed description of the '
                'image in markdown format.  This may be a reduced scale '
                'version of the original image.',
            }, {
                'type': 'image_url',
                'image_url': {'url': f'data:image/jpeg;base64,{img}'},
            }],
        }]},
        'present': [r'(?i)(road|street)', r'(?i)(building|house)'],
    })


@register_test('tool_use', 'Tool use')
def test_tool_use(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    tools = [
        {
            'type': 'function',
            'function': {
                'name': 'get_weather',
                'description': 'Get the current weather for a location',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'city': {
                            'type': 'string',
                            'description': 'The city name',
                        },
                    },
                    'required': ['city'],
                },
            },
        },
    ]
    result = chat_completion_with_usage(
        client,
        model_name,
        messages=[{
            'role': 'user',
            'content': 'What is the current weather in London?',
        }],
        tools=tools,
    )
    if result['tool_calls'] and len(result['tool_calls']) > 0:
        tool_call = result['tool_calls'][0]
        function_name = tool_call['function']['name']
        arguments = tool_call['function']['arguments']
        passed = function_name == 'get_weather'
        return TestResult(
            passed=passed,
            output=f'Called {function_name} with arguments: {arguments}',
            details={
                'function_name': function_name,
                'arguments': arguments,
                'duration': result.get('duration'),
            },
            timestamp=get_timestamp(),
            usage=result.get('usage'),
        )
    content = result['content'] or '(no content)'
    return TestResult(
        passed=False, output=f'No tool call made. Response: {content}',
        timestamp=get_timestamp(),
    )


@register_test('temperature_variation', 'Response variation across temperatures')
def test_temperature_variation(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    prompt = ('List three types of fruit that are yellow; just give their '
              'names without commentary or numbering')
    temperatures = [0.0, 0.5, 1.0, 1.5]
    responses: dict[str, str] = {}
    usage = None
    duration = 0
    for temp in temperatures:
        result = chat_completion_with_usage(
            client,
            model_name,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=temp,
            reasoning_effort='none',
            max_tokens=2048,
        )
        if result.get('duration') and duration is not None:
            duration += result['duration']
        else:
            duration = None
        raw_content = result['content']
        if result.get('usage'):
            if usage is None:
                usage = result['usage']
            else:
                for k in result['usage']:
                    usage[k] = usage.get(k, 0) + result['usage'][k]
        content = extract_answer_from_reasoning(raw_content)
        responses[str(temp)] = content
    output_lines = []
    for temp in temperatures:
        temp_key = str(temp)
        if temp_key in responses:
            output_lines.append(f'Temperature {temp_key}: {responses[temp_key]}')
    passed = len(responses) > 0
    return TestResult(
        passed=passed,
        output='\n'.join(output_lines),
        details={
            'responses': responses,
            'duration': duration,
        },
        timestamp=get_timestamp(),
        usage=usage,
    )


@register_test('knowledge_recency', 'Knowledge recency')
def test_knowledge_recenecy(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a helpful assistant who never uses metaphors, slang, emojis, '
        'or decorative characters.  You will answer only the questions asked, '
        'and not offer to do additional work.')
    prompt = (
        'Is there an item to add to .pre-commit-config.yaml to prettify json '
        'files?  I really only want to prettify selected json files (like '
        'package.json).')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'pretty-format-json'],
    })


@register_test('storytelling', 'Storytelling', skip=True, version=1)
def test_storytelling(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a creative storytelling agent.  Your stories are novel and '
        'detailed, avoiding tropes and emojis and using full sophisticated '
        'English.')
    prompt = (
        'Tell a detailed story, around 2000 words, told as if it is a '
        'journal of a diplomat traveling between outposts or settlements '
        'where her journal is mostly focused on how coffee, tea, or other '
        'invigorating drinks and their variations are served and only '
        'slightly about building a coalition for environmental policy.  There '
        'should be a gradual reveal that common culture results in success.')
    result = chat_completion_with_usage(
        client,
        model_name,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ],
    )
    raw_answer = result['content']
    answer = extract_answer_from_reasoning(raw_answer)
    has_words = 'espresso' in answer.lower() or 'brew' in answer.lower()
    word_length = len(answer.split())
    results = [has_words, 1000 < word_length < 3000]
    passed = [len([r for r in results if r]), len(results)]
    return TestResult(
        passed=passed, output=raw_answer,
        details={
            'has_key_words': has_words,
            'word_length': word_length,
            'extracted_answer': answer,
            'duration': result.get('duration')},
        timestamp=get_timestamp(),
        usage=result.get('usage'),
    )


def get_metadata_table(metadata: dict[str, Any]) -> list[tuple[str, Any]]:
    parameter_count_display = (
        f"{metadata['parameter_count']:,}"
        if metadata['parameter_count']
        else 'unknown'
    )
    context_length_display = (
        f"{metadata['context_length']:,}"
        if metadata['context_length']
        else 'unknown'
    )
    families_display = (
        ', '.join(metadata['families']) if metadata['families'] else 'none'
    )
    min_pixel_display = (
        f'{int(metadata["min_pixels"] ** 0.5):,}' if metadata.get('min_pixels') else '')
    max_pixel_display = (
        f'{int(metadata["max_pixels"] ** 0.5):,}' if metadata.get('max_pixels') else '')
    mod_str = metadata['modified_at']
    try:
        mod_str = get_timestamp(mod_str)
    except Exception:
        pass
    rows = [
        ('Name', metadata['name']),
        ('Source', metadata['source']),
        ('Family', metadata['family']),
        ('Families', families_display),
        ('Format', metadata['format']),
        ('Parameter Size', metadata['parameter_size']),
        ('Parameter Count', parameter_count_display),
        ('Vision Min Size', min_pixel_display),
        ('Vision Max Size', max_pixel_display),
        ('Model Context Length', context_length_display),
        ('Quantization', metadata['quantization']),
        ('Vision (metadata)', 'yes' if metadata['has_vision'] else 'no'),
        ('Tool Use (metadata)', 'yes' if metadata['has_tool'] else 'no'),
        ('Reasoning (metadata)', 'yes' if metadata['has_reasoning'] else 'no'),
        ('Embedding (metadata)', 'yes' if metadata['has_embedding'] else 'no'),
        ('Modified', mod_str),
    ]
    return rows


def format_metadata_table(metadata: dict[str, Any]) -> str:
    rows = get_metadata_table(metadata)
    lines = ['| Property | Value |', '|---|---|']
    for prop, value in rows:
        lines.append(f'| {prop} | {value} |')
    return '\n'.join(lines)


def escape_markdown(text, maxlen=None, always=False):
    if isinstance(text, (int, float)):
        return text
    text = str(text)
    if re.search(
            r'(?:[\*_`\[\]()]|[#\-=]+(?=\s|$)|[>+]|(?:\r?\n){2,}|\>\s+.*|[`]{1,3}|[\\]{1,2}|\!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\))',  # noqa
            text, re.VERBOSE | re.MULTILINE) is None and not always:
        return text
    needed = 3
    while ('`' * needed) in text:
        needed += 1
    if maxlen:
        text = text[:maxlen] + '...'
    text = '\n' + ('`' * needed) + '\n' + text + '\n' + ('`' * needed) + '\n'
    return text


def passed_to_status(passed):
    if passed is True or (isinstance(passed, (tuple, list)) and passed[0] == passed[1]):
        return 'PASSED'
    if passed is False:
        return 'Failed'
    if isinstance(passed, (tuple, list)):
        return f'{passed[0]}/{passed[1]}'
    return 'Unknown'


def format_test_result(test_def: TestDefinition, result: TestResult) -> str:
    truncated_output = result.output
    if len(truncated_output) > 1000:
        truncated_output = (
            truncated_output[:1000] +
            f'\n... (truncated, {len(result.output)} total characters)'
        )
    lines = [
        f'### {test_def.description}',
        f'**Test**: `{test_def.name}`',
        f'**Result**: {passed_to_status(result.passed)}',
    ]
    if result.timestamp:
        lines.append(
            f'**Completed**: {result.timestamp}')
    if result.usage:
        if result.usage.get('prompt_tokens'):
            lines.append(f'**Prompt Tokens**: {result.usage["prompt_tokens"]}')
        if result.usage.get('completion_tokens'):
            lines.append(f'**Response Tokens**: {result.usage["completion_tokens"]}')
    if 'duration' in result.details:
        lines.append(
            '**Duration**: '
            f"{result.details['duration']:4.2f}s")
    lines.append('**Output**:')
    lines.append(fence_code_block(truncated_output))
    display_details = {
        k: v
        for k, v in (list(result.metadata.items()) + list(result.details.items()))
        if k != 'duration' and v is not None
    }
    if display_details:
        lines.append('**Details**:')
        for key, value in display_details.items():
            lines.append(f'- {key}: {escape_markdown(value, 1000)}')
    return '\n'.join(lines)


def result_record(
    metadata: dict[str, Any],
    test_results: list[tuple[TestDefinition, TestResult]],
) -> dict[str, Any]:
    return {
        'metadata': metadata,
        'tests': [
            {'name': test_def.name, 'description': test_def.description,
             'result': asdict(result)}
            for test_def, result in test_results
        ],
    }


def load_existing_results(path: str | None) -> dict[str, TestResult]:
    if not path or not os.path.exists(path):
        return {}, {}
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
        match = re.search(r'\n## Result JSON\n```json\n(.*?)\n```\s*$', text, re.S)
        record = json.loads(match.group(1)) if match else {}
    except Exception:
        return {}, {}
    results = {}
    for entry in record.get('tests', []):
        data = entry.get('result') or {}
        if entry.get('name'):
            results[entry['name']] = TestResult(
                version=data.get('version', 0), passed=data.get('passed'),
                output=data.get('output', ''),
                metadata=data.get('metadata') or {},
                timestamp=data.get('timestamp'),
                details=data.get('details') or {}, usage=data.get('usage'))
    return record.get('metadata'), results


def get_timestamp(val=None):
    if not val:
        return datetime.datetime.now(datetime.timezone.utc).strftime(
            '%Y-%m-%d %H:%M:%S UTC')
    tval = dateutil.parser.parse(val)
    if tval.tzinfo is None:
        tval = tval.replace(tzinfo=datetime.timezone.utc)
    return tval.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def generate_report(
    metadata: dict[str, Any],
    test_results: list[tuple[TestDefinition, TestResult]],
) -> str:
    sections = [
        f"# Model Card: {metadata['name']}",
        f'Generated: {get_timestamp()}',
        '## Metadata',
        format_metadata_table(metadata),
    ]
    next_metadata = len(sections)
    if test_results:
        sections.append('## Test Results')
        sections.append('| Test | Result | Duration | Tokens |')
        sections.append('|---|---|---:|---:|')
        for test_def, result in test_results:
            if result is None:
                continue
            status = passed_to_status(result.passed)
            duration = result.details.get('duration') or 0
            sections.append(
                f'| {test_def.description} | {status} | {duration:4.2f}s | '
                f'{result.usage["completion_tokens"] if result.usage else ""} |',
            )
        for test_def, result in test_results:
            if result is None:
                continue
            sections.append(format_test_result(test_def, result))
            if result.metadata:
                for k, v in result.metadata.items():
                    k_str = str(k).replace('_', ' ').title()
                    v_str = (f'{v:,}' if (isinstance(v, int) or
                             (isinstance(v, float) and v.is_integer()))
                             else v)
                    sections[next_metadata:next_metadata] = [f'| {k_str} | {v_str} |']
                    next_metadata += 1
    sections.extend([
        '## Result JSON',
        '```json',
        json.dumps(result_record(metadata, test_results), indent=2, default=str),
        '```',
    ])
    return '\n'.join(sections) + '\n'


def run_tests(
    client: OpenAI,
    model_name: str,
    ollama_base_url: str,
    test_names: list[str] | None,
    save_progress: Callable[[list[tuple[TestDefinition, TestResult]]], None] | None = None,
    raise_errors: bool = False,
    require_first_load: bool = False,
) -> list[tuple[TestDefinition, TestResult]]:
    results = []
    for test_def in TEST_REGISTRY:
        if test_names is not None and test_def.name not in test_names:
            continue
        sys.stderr.write(f'Running test: {test_def.name} ')
        sys.stderr.flush()
        start = time.time()
        try:
            result = test_def.run(client, model_name, ollama_base_url)
        except Exception as exc:
            result = TestResult(passed=False, output=f'Error: {exc}',
                                timestamp=get_timestamp())
            if test_def.name == 'first_load' and require_first_load:
                return None
            if raise_errors:
                raise
        result.version = test_def.version
        elapsed = time.time() - start
        if not result.details.get('duration', 0):
            result.details['duration'] = elapsed
        sys.stderr.write(f' {passed_to_status(result.passed)} ({elapsed:4.2f}s)\n')
        results.append((test_def, result))
        if save_progress:
            save_progress(results)
    return results


def list_models(ollama_base_url: str) -> list[str]:
    response = requests.get(f'{ollama_base_url}/api/tags', timeout=30)
    response.raise_for_status()
    return [m['name'] for m in response.json().get('models', [])]


def safe_filename(model_name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', model_name)
    name = name.rstrip(' .')
    return name or 'model'


def restart_command(cmd):
    if not cmd:
        return
    # try
    # 'pkill -f "[o]llama" || true; nohup ollama serve >/dev/null 2>&1 & sleep 5'
    # "taskkill /F /IM ollama.exe >NUL & ollama ls >NUL"
    subprocess.check_call(cmd, shell=True, start_new_session=True)


def write_text_atomic(path: str, text: str) -> None:
    temp_path = f'{path}.tmp'
    interrupted = False
    old_handler = signal.getsignal(signal.SIGINT)

    def handle_sigint(signum, frame):
        nonlocal interrupted
        interrupted = True

    try:
        signal.signal(signal.SIGINT, handle_sigint)
        with open(temp_path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        for retries in range(5, -1, -1):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if not retries:
                    raise
                time.sleep(5)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    finally:
        signal.signal(signal.SIGINT, old_handler)
    if interrupted:
        raise KeyboardInterrupt


def add_to_summary(summary, model, metadata, test_results):
    if 'models' not in summary:
        summary['models'] = {}
        summary['columns'] = []
        summary['tests'] = []
    if model in summary['models']:
        return
    meta_col = dict(get_metadata_table(metadata))
    for col in meta_col:
        if col not in summary['columns']:
            summary['columns'].append(col)
    summary['models'][model] = {'metadata': meta_col, 'tests': {}}
    for test_name, result in test_results.items():
        if test_name not in summary['tests']:
            summary['tests'].append(test_name)
        if result.metadata:
            for k, v in result.metadata.items():
                k_str = str(k).replace('_', ' ').title()
                v_str = (f'{v:,}' if (isinstance(v, int) or
                         (isinstance(v, float) and v.is_integer()))
                         else v)
                if k_str not in summary['columns']:
                    summary['columns'].append(k_str)
                summary['models'][model]['metadata'][k_str] = v_str
        summary['models'][model]['tests'][test_name] = {
            'status': passed_to_status(result.passed),
            'duration': f'{result.details.get("duration") or 0:4.2f}s',
            'tokens': result.usage['completion_tokens'] if result.usage else '',
            'output': result.details.get('extracted_answer') or result.output,
            'timestamp': result.timestamp,
        }


def int_from_val(val):
    try:
        return int(str(val).replace(',', ''))
    except Exception:
        return 0


def covered_by(model, summary):
    mcl = max(int_from_val(model['metadata'].get('Model Context Length')),
              int_from_val(model['metadata'].get('Context Length')))
    me = int_from_val(model['metadata'].get('Embedding Dimensions'))
    for check in summary['models'].values():
        if check == model:
            continue
        ccl = max(int_from_val(check['metadata'].get('Model Context Length')),
                  int_from_val(check['metadata'].get('Context Length')))
        if mcl > ccl:
            continue
        if me > int_from_val(check['metadata'].get('Embedding Dimensions')):
            continue
        sval = []
        stime = []
        cval = []
        ctime = []
        for t in summary['tests']:
            if t not in model['tests']:
                continue
            s = model['tests'][t].get('status', '')
            st = model['tests'][t].get('duration', '')
            c = check['tests'].get(t, {}).get('status', 'Failed')
            ct = check['tests'].get(t, {}).get('duration', '')
            sval.append(1 if s == 'PASSED' else 0 if s == 'Failed' else
                        int(s.split('/')[0]) / int(s.split('/')[1]))
            cval.append(1 if c == 'PASSED' else 0 if c == 'Failed' else
                        int(c.split('/')[0]) / int(c.split('/')[1]))
            stime.append(10000 if not st else float(st[:-1]))
            ctime.append(10000 if not ct else float(ct[:-1]))
        if any(sval[idx] == 1 and cval[idx] != 1 for idx in range(len(sval))):
            continue
        if not any(sval[idx] != 1 and cval[idx] == 1 for idx in range(len(sval))):
            if any(sval[idx] > cval[idx] for idx in range(len(sval))):
                continue
        if any(sval[idx] == 1 and max(1, stime[idx]) * 1.5 < ctime[idx]
               for idx in range(len(sval))):
            continue
        return check['metadata']['Name']
    return ''


def model_rank(model, summary):
    passed = 0
    ptime = 0
    sval = []
    stime = []
    for t in summary['tests']:
        if t not in model['tests']:
            sval.append(0)
            stime.append(10000)
            continue
        s = model['tests'][t].get('status', '')
        st = model['tests'][t].get('duration', '')
        sval.append(1 if s == 'PASSED' else 0 if s == 'Failed' else
                    int(s.split('/')[0]) / int(s.split('/')[1]))
        stime.append(10000 if not st else float(st[:-1]))
        if sval[-1] == 1:
            passed += 1
            ptime += stime[-1]
    rank = (-passed, ptime, -sum(sval), sum(stime))
    return rank


def summary_table(summary, models):
    models = set(models or [])
    known = {t.name: t.description for t in TEST_REGISTRY}
    cols = list(summary['columns'])
    rows = []
    for t in summary['tests']:
        cols += [f'{known.get(t, t)}', 'Duration', 'Tokens']
    cols += ['Covered', 'Present', 'Rank']
    for idx, model in enumerate([m[-1] for m in sorted([
            (model_rank(m, summary), m['metadata']['Name'], m)
            for m in summary['models'].values()])]):
        row = [model['metadata'].get(col, '') for col in summary['columns']]
        for t in summary['tests']:
            tval = model['tests'].get(t, {})
            row += [tval.get('status', ''), tval.get('duration', ''), tval.get('tokens', '')]
        row.append(covered_by(model, summary))
        row.append('Yes' if model['metadata']['Name'] in models else '')
        row.append(str(idx + 1))
        rows.append(row)
    # Get rid of columns with all identical values
    for idx in range(len(cols) - 1, -1, -1):
        if len({r[idx] for r in rows}) != 1 or rows[0][idx] == 'PASSED':
            continue
        for r in rows:
            r[idx:idx + 1] = []
        cols[idx:idx + 1] = []
    return cols, rows


def create_summary_html(timestamp, cols, rows):
    th_cells = ''.join(
        f'<th>{html.escape(str(c))}</th>' for c in cols)
    th_cells += '<th data-sort-method="none"></th>'
    tr_rows = ''.join(
        '<tr>' + ''.join(f'<td>{html.escape(str(r))}</td>' for r in row) + '<td></td></tr>'
        for row in rows
    )
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Model Card Summary</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/tablesort@5.7.1/tablesort.css">
  <style>
    html, body {
      height: 100%;
      margin: 0;
    }
    body {
      display: flex;
      flex-direction: column;
    }
    .header-wrap {
      padding: 10px;
    }
    .header-wrap h1,
    .header-wrap p {
      margin: 0;
    }
    .table-wrap {
      flex: 1 1 auto;
      min-height: 0;
      overflow: auto;
      padding: 0;
    }
    table {
      border-collapse: separate;
      border-spacing: 0;
      counter-reset: row-number;
    }
    tbody tr {
      counter-increment: row-number;
    }
    th, td {
      border-left: 0 transparent;
      border-top: 0 transparent;
      border-right: 1px solid;
      border-bottom: 1px solid;
      padding: 0 3px;
    }
    table thead th {
      position: sticky !important;
      top: 0;
      z-index: 20;
      background-color: Canvas;
      border-top: 1px solid;
    }
    table thead th:first-child,
    table tbody td:first-child,
    table tbody th:first-child {
      position: sticky !important;
      left: 0;
      z-index: 10;
      background-color: Canvas;
      border-left: 1px solid;
    }
    table thead th:first-child {
      z-index: 30;
    }
    td:last-child::before {
      content: counter(row-number);
    }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/tablesort@5.7.1/dist/tablesort.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/tablesort@5.7.1/dist/sorts/tablesort.number.min.js">
  </script>
</head>
""" + f"""
<body>
  <div class="header-wrap">
    <h1>Model Card Summary</h1>
    <p>Generated: {html.escape(timestamp)}</p>
  </div>
  <div class="table-wrap">
    <table id="summary">
      <thead><tr>{th_cells}</tr></thead>
      <tbody>{tr_rows}</tbody>
    </table>
  </div>
  <script>
    new Tablesort(document.getElementById('summary'), {{ descending: false }});
  </script>
</body>
</html>
"""


def create_summary(summary_path, output_dir, summary, models):
    timestamp = get_timestamp()
    cols, rows = summary_table(summary, models)
    if summary_path.endswith('.html'):
        record = create_summary_html(timestamp, cols, rows)
    else:
        sections = [
            '---',
            'cssclasses: scrollable-table',
            '---',
            '# Model Card Summary',
            f'Generated: {timestamp}',
            '',
        ]
        sections.append('| ' + ' | '.join(cols) + ' |')
        sections.append('|' + '---|' * len(cols))
        for row in rows:
            sections.append('| ' + ' | '.join([str(r) for r in row]) + ' |')
        record = '\n'.join(sections) + '\n'
    out_path = (summary_path if os.path.dirname(summary_path) or
                not os.path.isdir(output_dir) else os.path.join(output_dir, summary_path))
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        f.write(record)


def create_report(report_spec, output_dir, summary):
    timestamp = get_timestamp()
    sections = [
        '# Test Report',
        f'Generated: {timestamp}',
        '',
    ]
    only = None
    report_path = report_spec.split(',', 1)[0]
    if ',' in report_spec:
        only = report_path.split(',')[1:]
    for t in summary['tests']:
        if only and t not in only:
            continue
        found = {}
        for m in summary['models'].values():
            if t not in m['tests']:
                continue
            mt = m['tests'].get(t)
            if (not mt or not mt['output'] or
                    not isinstance(mt['output'], str) or
                    mt['status'] != 'PASSED'):
                continue
            val = mt['output']
            dur = mt['duration']
            if val not in found:
                found[val] = []
            found[val].append((float(dur[:-1]) if dur else 9999, dur,
                               mt['timestamp'], m['metadata']['Name']))
            found[val].sort()
        if not len(found):
            continue
        sections.append(f'## {t}')
        for _, k, f in sorted((f[0][0], k, f) for k, f in found.items()):
            for _, dur, timestamp, mn in f:
                esc_name = mn.replace('.', '\\.')
                sections.append(f'- **{esc_name}** ({dur} - {timestamp})')
            sections.append(escape_markdown(k, always=True).strip())
    record = '\n'.join(sections) + '\n'
    out_path = (report_path if os.path.dirname(report_path) or
                not os.path.isdir(output_dir) else os.path.join(output_dir, report_path))
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        f.write(record)


def load_yaml_tests():
    dirname = os.path.dirname(__file__)
    paths = [
        os.path.join(dirname, name) for name in os.listdir(dirname)
        if name.startswith(os.path.splitext(os.path.basename(__file__))[0]) and
        os.path.splitext(name)[1] in {'.yml', '.yaml'} and
        os.path.isfile(os.path.join(dirname, name))]
    for path in sorted(paths):
        tests = yaml.safe_load(open(path, encoding='utf-8').read())

        def make_test(test):
            if 'chat' in test['test'] and 'messages' in test['test']['chat']:
                for m in test['test']['chat']['messages']:
                    if 'append' in m:
                        val = subprocess.check_output(m['append'], shell=True, encoding='utf8')
                        if isinstance(val, bytes):
                            val = val.decode()
                        print(f'Appending {len(val)} characters')
                        m['content'] = m.get('content', '') + val
                        m.pop('append')

            def test_func(
                client: OpenAI, model_name: str, ollama_base_url: str,
            ) -> TestResult:
                if 'chat' in test['test']:
                    return chat_test(client, model_name, test['test'])
                return bash_test(client, model_name, test['test'])

            register_test(test['name'], test['description'],
                          test.get('skip', False), test.get('version', 0))(test_func)

        for test in tests:
            make_test(test)


def sort_models(summary, output_dir, models):
    oldlist = None
    for summary_path in summary.split(','):
        try:
            out_path = (summary_path if os.path.dirname(summary_path) or
                        not os.path.isdir(output_dir) else os.path.join(output_dir, summary_path))
            if os.path.isfile(out_path):
                summ = open(out_path, encoding='utf-8').read()
                if '<tr><td>' in summ:
                    oldlist = [r.split('</td>', 1)[0] for r in summ.split('<tr><td>')[1:]]
                else:
                    oldlist = [r.split(' | ', 1)[0] for r in summ.split('\n| ')[2:]]
        except Exception:
            pass
        if oldlist:
            break
    if not oldlist:
        return models
    oldset = set(oldlist)
    newlist = [record[-1] for record in sorted([
        (-1 if m not in oldset else oldlist.index(m), idx, m)
        for idx, m in enumerate(models)])]
    return newlist


def main():  # noqa
    parser = argparse.ArgumentParser(
        description='Generate a model card for an Ollama model.',
        epilog='If there are files that starts with the same base name as '
        'this source file and ends with .yml or .yaml, they can contain a '
        'list of additional tests.\n'
        'All tests have "name", "description", "version" as an optional '
        'integer, "skip" as optional boolean, and "test".  "test" must either '
        'have "chat" or "bash".\n"chat" contains "messages" and any other '
        'values to pass to the chat test.\n'
        '"bash" contains a list of bash commands or shell commands to execute '
        'in order.  The "test" also has optional "start" and "stop" command '
        'lists.  If a "start" command fails, the main "bash" commands are '
        'never run.  The "stop" commands are run regardless of success. '
        '"test" also has optional "timeout" in seconds per shell command, '
        '"env" a dictionary of environment variables, "cwd" and optional '
        'working directory.  The "env" values and the "bash" commands can '
        'contain the substring "{model}" which will be replaced with the '
        'current model name without any escaping.\n'
        ' The "test" key can also contain "present" and "absent" as optional '
        'lists of regex to match with the output of the chat or final main '
        'bash command.')
    parser.add_argument(
        'model', nargs='?',
        help='Exact model name (e.g. llama3.2:latest).  Use --models for '
        'filtering by regex.')
    parser.add_argument(
        '--models',
        help='If specified, run on all models that match this regex. Use an '
        'empty string to match all of them.')
    parser.add_argument(
        '--restart', help='Shell command to run between models')
    parser.add_argument(
        '--base-url',
        default='http://localhost:11434',
        help='Ollama server base URL (default: http://localhost:11434)')
    parser.add_argument(
        '-o', '--output', help='Output file path (default: stdout) or directory')
    parser.add_argument(
        '-t', '--tests',
        help='Comma-separated list of test names to run.  Defaults to all '
        'tests not marked "skip".  Add "all" to include all tests, "default" '
        'to include the non-skip tests.')
    parser.add_argument(
        '-x', '--skip-tests',
        help='Comma-separated list of test names to skip')
    parser.add_argument(
        '--remove-tests',
        help='Comma-separated list of test names to remove')
    parser.add_argument(
        '--list-tests', '-l', action='store_true',
        help='List available tests and exit')
    parser.add_argument(
        '--metadata-only', action='store_true',
        help='Collect metadata only, skip all tests')
    parser.add_argument(
        '--timeout', type=float, default=300,
        help='Per-request timeout in seconds (default: 300)')
    parser.add_argument(
        '--skip', '-s', action='store_true',
        help='Skip checking a model if the output file already exists.')
    parser.add_argument(
        '--raise', dest='raise_errors', action='store_true',
        help='Raise test errors for debugging')
    parser.add_argument(
        '--missing-tests', '--missing', '-m', action='store_true',
        help='Read an existing model card and run only missing tests plus'
        'first_load.')
    parser.add_argument(
        '--failed-tests', '--failed', '-f',
        choices={'false', 'partial', 'full'},
        help='Read an existing model card and run only failed tests plus'
        'first_load.  Using "full" will only rerun tests with no partial '
        'success.')
    parser.add_argument(
        '--require-first-load', '-r', action='store_true',
        help='If the first load fails, do not write a model card or proceed '
        'with other tests.')
    parser.add_argument(
        '--after', '--since',
        help='Any test older than this is considered missing.')
    parser.add_argument(
        '--summary',
        help='If specified, the name of a summary file to write.  If this '
        'does not include a directory, it will be written in the --output '
        'directory.  Use --collect to collect model cards in the output '
        'directory that were not processed in this run.  The summary is in '
        'markdown unless the name ends in .html.  Use a comma separated '
        'list for multiple summary files.')
    parser.add_argument(
        '--report',
        help='Generate a test report in markdown format.  If a '
        'comma-separated list, the first value is the output path and '
        'subsequent values are the tests to include.')
    parser.add_argument(
        '--collect', action='store_true',
        help='Collect older model cards for the summary and report.')
    args = parser.parse_args()
    load_yaml_tests()
    if args.list_tests:
        for t in TEST_REGISTRY:
            if t.skip == 'always':
                continue
            sys.stdout.write(f'{t.name:25s} {t.description}{" (skip)" if t.skip else ""}\n')
        sys.exit(0)
    restart_command(args.restart)
    ollama_base_url = args.base_url.rstrip('/')
    if args.remove_tests and not args.tests:
        args.tests = 'skip_all_tests'
    if not args.model or args.models is not None:
        models = list_models(ollama_base_url)
        if args.models:
            pattern = re.compile(args.models, re.IGNORECASE)
            models = [m for m in models if pattern.search(m)]
        if args.summary:
            models = sort_models(args.summary, args.output, models)
    else:
        models = [args.model]
    model_metadata = {}
    summary = {}
    for model in models:
        out_path = None
        if args.output:
            if args.output and os.path.isdir(args.output):
                out_path = os.path.join(args.output, f'{safe_filename(model)}.md')
            else:
                out_path = args.output
        if args.skip and out_path and os.path.exists(out_path):
            continue
        sys.stderr.write(f'Fetching metadata for {model}\n')
        try:
            metadata = get_model_metadata(ollama_base_url, model)
        except requests.exceptions.ConnectionError:
            sys.stderr.write(
                f'Error: cannot connect to Ollama at {ollama_base_url}\n',
            )
            sys.exit(1)
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                sys.stderr.write(f"Error: model '{model}' not found\n")
            else:
                sys.stderr.write(f'Error: {exc}\n')
            sys.exit(1)
        test_results: list[tuple[TestDefinition, TestResult]] = []
        if not args.metadata_only:
            ClientKwargs.update(dict(
                base_url=f'{ollama_base_url}/v1',
                api_key='ollama',
                timeout=args.timeout,
            ))
            client = OpenAI(**ClientKwargs)
            sel_tests = set(args.tests.split(',')) if args.tests else set()
            if 'all' in sel_tests:
                sel_tests = {t.name for t in TEST_REGISTRY if t.skip != 'always'}
            if 'default' in sel_tests or not args.tests:
                sel_tests |= {t.name for t in TEST_REGISTRY if not t.skip}
            if args.skip_tests:
                sel_tests -= set(args.skip_tests.split(','))
            _, existing_results = load_existing_results(out_path)
            removed = False
            if args.remove_tests:
                for t in args.remove_tests.split(','):
                    if t in existing_results:
                        existing_results.pop(t, None)
                        removed = True
            if args.missing_tests:
                known = {t.name: t for t in TEST_REGISTRY}
                after = ''
                if args.after:
                    after = get_timestamp(args.after)
                sel_tests -= {name for name, r in existing_results.items()
                              if name not in known or
                              (r.version == known[name].version and (
                                  r.timestamp or '') >= after)}
            if args.failed_tests and str(args.failed_tests) != 'false':
                known = {t.name: t for t in TEST_REGISTRY}
                if args.failed_tests != 'full':
                    sel_tests &= {name for name, r in existing_results.items()
                                  if name in known and not result_passed(r)}
                else:
                    sel_tests &= {name for name, r in existing_results.items()
                                  if name in known and result_complete_fail(r)}
            sel_tests.add('first_load')
            run_names = [t.name for t in TEST_REGISTRY if t.name in sel_tests]
            if len(run_names) <= 1:
                if removed:
                    run_names = []
                else:
                    model_metadata[metadata['name']] = metadata
                    continue

            def save_func(out_path, metadata, existing_results):
                def save_progress(results):
                    if not out_path:
                        return
                    new_by_name = {test_def.name: result for test_def, result in results}
                    merged = [(test_def, new_by_name.get(
                        test_def.name, existing_results.get(test_def.name)))
                        for test_def in TEST_REGISTRY]
                    write_text_atomic(out_path, generate_report(
                        metadata, [r for r in merged if r[1] is not None]))
                return save_progress

            new_results = run_tests(client, model, ollama_base_url, run_names, save_func(
                out_path, metadata, existing_results), args.raise_errors,
                args.require_first_load)
            if new_results is None:
                restart_command(args.restart)
                continue
            new_by_name = {test_def.name: result for test_def, result in new_results}
            test_results = [(test_def, new_by_name.get(
                test_def.name, existing_results.get(test_def.name)))
                for test_def in TEST_REGISTRY
            ]
            test_results = [r for r in test_results if r[1] is not None]
        report = generate_report(metadata, test_results)
        if args.output:
            write_text_atomic(out_path, report)
            sys.stderr.write(f'Report written to {out_path}\n')
        else:
            sys.stdout.write(report)
        add_to_summary(summary, model, metadata, {t.name: r for t, r in test_results})
        restart_command(args.restart)
    if (args.summary or args.report) and args.collect and os.path.isdir(args.output):
        for filename in os.listdir(args.output):
            path = os.path.join(args.output, filename)
            try:
                metadata, results = load_existing_results(path)
                metadata = model_metadata.pop(metadata['name'], metadata)
                if args.remove_tests and (set(results) & set(args.remove_tests.split(','))):
                    sys.stderr.write(f'Removing tests from {metadata["name"]}\n')
                    for t in args.remove_tests.split(','):
                        results.pop(t, None)
                    generate_report(metadata, results)
                add_to_summary(summary, metadata['name'], metadata, results)
            except Exception:
                pass
    if args.summary:
        for summ in args.summary.split(','):
            create_summary(summ, args.output, summary, models)
    if args.report:
        create_report(args.report, args.output, summary)


if __name__ == '__main__':
    main()
