#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "openai",
#     "python-dateutil",
#     "requests",
# ]
# ///

import argparse
import base64
import datetime
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
from openai import OpenAI

ClientKwargs = {}


@dataclass
class TestResult:
    passed: bool | None
    output: str
    details: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] | None = None


@dataclass
class TestDefinition:
    name: str
    description: str
    skip: bool
    run: Callable[[OpenAI, str, str], TestResult]


TEST_REGISTRY: list[TestDefinition] = []


def register_test(name: str, description: str, skip: bool = False):
    def decorator(func: Callable[[OpenAI, str, str], TestResult]) -> Callable:
        TEST_REGISTRY.append(
            TestDefinition(name=name, description=description, run=func, skip=skip),
        )
        return func

    return decorator


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
    ) or 'vision' in capabilities
    has_tool = (
        '.Tools' in template or 'tools' in template.lower() or
        'tool_call' in template.lower() or 'tools' in capabilities
    )
    has_reasoning = (
        '<think>' in template or '<|thinking|>' in template or
        'thinking' in capabilities or 'reasoning' in capabilities)
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
    }
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
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
        if chunk.usage is not None:
            usage = {
                'prompt_tokens': chunk.usage.prompt_tokens,
                'completion_tokens': chunk.usage.completion_tokens,
                'total_tokens': chunk.usage.total_tokens,
            }
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
) -> None:
    client = OpenAI(**client_kwargs)
    try:
        queue.put(chat_completion_worker(
            client, model_name, messages, temperature, max_tokens, tools, use_stream, queue))
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
) -> dict[str, Any]:
    timeout = ClientKwargs.get('timeout', 300)
    mp_queue = multiprocessing.Queue()
    start = time.time()
    process = multiprocessing.Process(
        target=chat_completion_wrapper,
        args=(mp_queue, ClientKwargs, model_name, messages,
              temperature, max_tokens, tools, use_stream),
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
        )
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
        found = ps['models'][0]
    return TestResult(
        passed=True, output=ps,
        details={
            'size': found['size'],
            'size_vram': found['size_vram'],
            'vram_percentage': round(100 * found['size_vram'] / found['size'], 2),
            'context_length': found['context_length'],
            'duration': result.get('duration')},
        usage=result.get('usage'),
    )


def chat_test(
    client: OpenAI, model_name: str,
        test):
    result = chat_completion_with_usage(
        client,
        model_name,
        **test['chat'],
    )
    raw_answer = result['content']
    answer = extract_answer_from_reasoning(raw_answer)
    details = {'extracted_answer': answer,
               'duration': result.get('duration')}
    passed = True
    for exp in test.get('present', []):
        found = re.search(exp, answer)
        details[f'{exp} present'] = bool(found)
        passed = passed and bool(found)
    return TestResult(
        passed=passed, output=raw_answer, details=details,
        usage=result.get('usage'),
    )


@register_test('basic_question', 'Basic question answering')
def test_basic_question(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    return chat_test(client, model_name, {
        'chat': {'messages': [{
            'role': 'user',
            'content': 'What is the capital of France? Answer with only the city name.',
        }]},
        'present': [r'(?i)paris'],
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


@register_test('python_yaml', 'Python yaml library use')
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
        'Write a Python program that uses pep 723 and argparse to take a yaml '
        'or json input file (as the first command line parameter), and '
        'output either yaml or json, either as compact as possible or nicely '
        'formatted; for instance, if outputting yaml, the compact form could '
        'deduplicate repeated data, but the nice formatting would not.')
    return chat_test(client, model_name, {
        'chat': {'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]},
        'present': [r'argparse', r'(?i)pyyaml', r'import yaml', r'safe_load', r'/// script'],
    })


@register_test('code_editing', 'Code editing')
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
        'Modify the `chat_test` method to fail any test that ever generates '
        'an emoji. Remember, more compact code with clear variables and few '
        'to no comments is preferred. Never use emojis, slang, or metaphors. '
        'Do not prefix variables or functions with underscores unless they '
        'are unused. Do not add separator comments. Do not add needless blank '
        'lines inside functions.  Show code changes less than 50 lines in git '
        'diff format, more than 100 lines as complete files.')
    src = open(os.path.realpath(__file__), encoding='utf-8').read()
    prompt += (
        f'\n\n##### File: {os.path.basename(__file__)}\n```python\n' +
        src.replace('```', '\\`\\`\\`').strip() + '\n```\n')
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


@register_test('embedding', 'Embedding generation support')
def test_embedding(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    response = client.embeddings.create(
        model=model_name,
        input='The quick brown fox jumps over the lazy dog.',
    )
    vector = response.data[0].embedding
    dimensions = len(vector)
    has_nonzero = any(v != 0.0 for v in vector)
    passed = dimensions > 0 and has_nonzero
    return TestResult(
        passed=passed,
        output=f'Generated embedding with {dimensions} dimensions',
        details={'dimensions': dimensions, 'has_nonzero_values': has_nonzero},
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
            usage=result.get('usage'),
        )
    content = result['content'] or '(no content)'
    return TestResult(
        passed=False, output=f'No tool call made. Response: {content}',
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


@register_test('storytelling', 'Storytelling', skip=True)
def test_storytelling(
    client: OpenAI, model_name: str, ollama_base_url: str,
) -> TestResult:
    system_prompt = (
        'You are a creative storytelling agent.  Your stories are novel and '
        'detailed, avoiding tropes and emojis and using full sophisticated '
        'English.')
    prompt = (
        'Tell a detailed story, around 2000 words, told as if it is a '
        'journal of a diplomat travelling between outposts or settlements '
        'where her journal is mostly focused on how coffee, tea, or other '
        'non-intoxicating drinks are served and only slightly about building '
        'a coalition for environmental policy.  There should be a gradual '
        'reveal that common culture results in success.')
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
    passed = 'espresso' in answer.lower() or 'brew' in answer.lower()
    passed = passed and 1000 < len(answer.split()) < 3000
    return TestResult(
        passed=passed, output=raw_answer,
        details={'duration': result.get('duration')},
        usage=result.get('usage'),
    )


def format_metadata_table(metadata: dict[str, Any]) -> str:
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
    mod_str = metadata['modified_at']
    try:
        mod_str = dateutil.parser.parse(mod_str).astimezone(
            datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
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
        ('Context Length', context_length_display),
        ('Quantization', metadata['quantization']),
        ('Vision (metadata)', 'yes' if metadata['has_vision'] else 'no'),
        ('Tool Use (metadata)', 'yes' if metadata['has_tool'] else 'no'),
        ('Reasoning (metadata)', 'yes' if metadata['has_reasoning'] else 'no'),
        ('Embedding (metadata)', 'yes' if metadata['has_embedding'] else 'no'),
        ('Modified', mod_str),
    ]
    lines = ['| Property | Value |', '|---|---|']
    for prop, value in rows:
        lines.append(f'| {prop} | {value} |')
    return '\n'.join(lines)


def escape_markdown(text, maxlen=None):
    if not isinstance(text, str):
        return text
    if re.search(
            r'(?:[\*_`\[\]()]|[#\-=]+(?=\s|$)|[>+]|(?:\r?\n){2,}|\>\s+.*|[`]{1,3}|[\\]{1,2}|\!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\))',  # noqa
            text, re.VERBOSE | re.MULTILINE) is None:
        return text
    needed = 3
    while ('`' * needed) in text:
        needed += 1
    if maxlen:
        text = text[:maxlen] + '...'
    text = '\n' + ('`' * needed) + '\n' + text + '\n' + ('`' * needed) + '\n'
    return text


def format_test_result(test_def: TestDefinition, result: TestResult) -> str:
    if result.passed is True:
        status = 'PASSED'
    elif result.passed is False:
        status = 'FAILED'
    else:
        status = 'INCONCLUSIVE'
    truncated_output = result.output
    if len(truncated_output) > 1000:
        truncated_output = (
            truncated_output[:1000] +
            f'\n... (truncated, {len(result.output)} total characters)'
        )
    lines = [
        f'### {test_def.description}',
        f'**Test**: `{test_def.name}`',
        f'**Result**: {status}',
    ]
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
        for k, v in result.details.items()
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
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
        match = re.search(r'\n## Result JSON\n```json\n(.*?)\n```\s*$', text, re.S)
        record = json.loads(match.group(1)) if match else {}
    except Exception:
        return {}
    results = {}
    for entry in record.get('tests', []):
        data = entry.get('result') or {}
        if entry.get('name'):
            results[entry['name']] = TestResult(
                passed=data.get('passed'), output=data.get('output', ''),
                details=data.get('details') or {}, usage=data.get('usage'))
    return results


def generate_report(
    metadata: dict[str, Any],
    test_results: list[tuple[TestDefinition, TestResult]],
) -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S UTC')
    sections = [
        f"# Model Card: {metadata['name']}",
        f'Generated: {timestamp}',
        '## Metadata',
        format_metadata_table(metadata),
    ]
    if test_results:
        sections.append('## Test Results')
        sections.append('| Test | Result | Duration | Tokens |')
        sections.append('|---|---|---:|---:|')
        for test_def, result in test_results:
            if result is None:
                continue
            if result.passed is True:
                status = 'PASSED'
            elif result.passed is False:
                status = 'FAILED'
            else:
                status = 'INCONCLUSIVE'
            duration = result.details.get('duration') or 0
            sections.append(
                f'| {test_def.description} | {status} | {duration:4.2f}s | '
                f'{result.usage["completion_tokens"] if result.usage else ""} |',
            )
        for test_def, result in test_results:
            if result is None:
                continue
            sections.append(format_test_result(test_def, result))
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
) -> list[tuple[TestDefinition, TestResult]]:
    results = []
    for test_def in TEST_REGISTRY:
        if test_names is not None and test_def.name not in test_names:
            continue
        sys.stderr.write(f'Running test: {test_def.name} ... ')
        sys.stderr.flush()
        start = time.time()
        try:
            result = test_def.run(client, model_name, ollama_base_url)
        except Exception as exc:
            result = TestResult(passed=False, output=f'Error: {exc}')
        elapsed = time.time() - start
        if not result.details.get('duration', 0):
            result.details['duration'] = elapsed
        if result.passed is True:
            sys.stderr.write(f'PASSED ({elapsed:4.2f}s)\n')
        elif result.passed is False:
            sys.stderr.write(f'FAILED ({elapsed:4.2f}s)\n')
        else:
            sys.stderr.write(f'INCONCLUSIVE ({elapsed:4.2f}s)\n')
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
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(text)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    finally:
        signal.signal(signal.SIGINT, old_handler)
    if interrupted:
        raise KeyboardInterrupt


def main():  # noqa
    parser = argparse.ArgumentParser(
        description='Generate a model card for an Ollama model.')
    parser.add_argument(
        'model', nargs='?', help='Ollama model name (e.g. llama3.2:latest)',
    )
    parser.add_argument(
        '--models', '--model-regex',
        help='If specified, run on all models that match this regex. Use an '
        'empty string to match all of them.',
    )
    parser.add_argument(
        '--restart', help='Shell command to run between models',
    )
    parser.add_argument(
        '--base-url',
        default='http://localhost:11434',
        help='Ollama server base URL (default: http://localhost:11434)',
    )
    parser.add_argument(
        '-o', '--output', help='Output file path (default: stdout) or directory',
    )
    parser.add_argument(
        '-t', '--tests',
        help='Comma-separated list of test names to run (default: all)',
    )
    parser.add_argument(
        '--skip-tests', '-x',
        help='Comma-separated list of test names to skip',
    )
    parser.add_argument(
        '--list-tests', '-l', action='store_true',
        help='List available tests and exit',
    )
    parser.add_argument(
        '--metadata-only', action='store_true',
        help='Collect metadata only, skip all tests',
    )
    parser.add_argument(
        '--timeout', type=float, default=300,
        help='Per-request timeout in seconds (default: 300)',
    )
    parser.add_argument(
        '--skip', '-s', action='store_true',
        help='Skip checking a model if the output file already exists.',
    )
    parser.add_argument(
        '--missing-tests', '-m', action='store_true',
        help='Read an existing model card and run only missing tests plus first_load.',
    )
    args = parser.parse_args()
    if args.list_tests:
        for t in TEST_REGISTRY:
            sys.stdout.write(f'{t.name:25s} {t.description}{" (skip)" if t.skip else ""}\n')
        sys.exit(0)
    restart_command(args.restart)
    ollama_base_url = args.base_url.rstrip('/')
    if not args.model or args.models is not None:
        models = list_models(ollama_base_url)
        if args.models:
            pattern = re.compile(args.models, re.IGNORECASE)
            models = [m for m in models if pattern.search(m)]
    else:
        models = [args.model]
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
                sel_tests = {t.name for t in TEST_REGISTRY}
            if 'default' in sel_tests or not args.tests:
                sel_tests |= {t.name for t in TEST_REGISTRY if not t.skip}
            if args.skip_tests:
                sel_tests -= set(args.skip_tests.split(','))
            existing_results = load_existing_results(out_path)
            if args.missing_tests:
                sel_tests -= set(existing_results)
            sel_tests.add('first_load')
            run_names = [t.name for t in TEST_REGISTRY if t.name in sel_tests]
            if len(run_names) <= 1:
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
                out_path, metadata, existing_results))
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
        restart_command(args.restart)


if __name__ == '__main__':
    main()
