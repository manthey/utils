#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "python-dateutil",
#     "diskcache",
#     "huggingface-hub>=0.20.0",
#     "tqdm",
# ]
# ///

import argparse
import datetime
import inspect
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import dateutil.parser
import diskcache
import huggingface_hub
import tqdm

cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')
cache = diskcache.Cache(cache_path)


@dataclass
class ModelArchParams:
    param_count: int | None
    num_layers: int | None
    num_kv_heads: int | None
    head_dim: int | None
    context_length: int | None
    embedding_length: int | None


@dataclass
class ModelInfo:
    source: str
    repo_id: str
    repo_name: str
    filename: str
    size_gb: float
    quantization: str
    model_type: str
    is_chunked: bool
    downloads: int
    created: datetime.datetime | None
    modified: datetime.datetime | None
    context_size: int | None = None
    arch_params: ModelArchParams | None = None
    memory_burden_gb: float | None = None
    has_tools: bool = False
    has_reasoning: bool = False
    has_vision: bool = False
    has_embedding: bool = False


QUANT_PRIORITY_BITS = {
    # Full precision
    'F32': (1, 32.0),
    'BF16': (None, 16.0),  # disabled because of my specific GPUs
    'F16': (3, 16.0),      # list after BF16 for eager parsing
    # Near-lossless
    'Q8_0': (10, 8.5),
    'Q8_1': (11, 9.0),
    # High quality
    'Q6_K': (20, 6.5625),
    'Q6_K_L': (21, 6.5625),
    # Good quality
    'Q5_K_H': (30, 5.5),
    'Q5_K_L': (31, 5.5),
    'Q5_K_M': (32, 5.5),
    'Q5_K_S': (33, 5.5),
    'Q5_1': (34, 6.0),
    'Q5_0': (35, 5.0),
    # Recommended balance
    'Q4_K_L': (40, 4.5),
    'Q4_K_M': (41, 4.5),
    'Q4_K_S': (42, 4.5),
    'IQ4_NL': (43, 4.5),
    'IQ4_XS': (44, 4.25),
    'Q4_1': (45, 5.0),
    'Q4_0': (46, 4.5),
    # Lower quality
    'Q3_K_XL': (50, 3.9375),
    'Q3_K_L': (51, 3.875),
    'IQ3_M': (52, 3.7),
    'Q3_K_M': (53, 3.875),
    'IQ3_S': (54, 3.5),
    'Q3_K_S': (55, 3.5),
    'IQ3_XS': (56, 3.3),
    'IQ3_XXS': (57, 3.06),
    # Very low quality
    'Q2_K_L': (60, 2.625),
    'Q2_K': (61, 2.625),
    'Q2_K_S': (62, 2.5),
    'IQ2_M': (63, 2.7),
    'IQ2_S': (64, 2.5),
    'IQ2_XS': (65, 2.31),
    'IQ2_XXS': (66, 2.0625),
    # Desperate
    'IQ1_M': (70, 1.75),
    'IQ1_S': (71, 1.5625),
    'Q1_0': (72, 1.0),
}

MODEL_PATTERNS = {
    'code': {
        'tags': {'code', 'conversational'},
        'arch': {'coder'},
        'patterns': {
            r'code', r'coder', r'codestral', r'starcoder', r'codellama',
            r'wizardcoder', r'phind', r'magicoder', r'codegen', r'replit',
            r'stable-code', r'granite-code', r'qwen.*coder', r'deepseek.*code',
            r'claude', r'teichai'}},
    'embed': {
        'tags': {'embedding', 'text-embeddings-inference'},
        'patterns': {r'embed'}},
    'vision': {
        'tags': {'image-text-to-text', 'conversational'},
        'arch': {'clip', 'llava'},
        'patterns': {
            r'vision', r'llava', r'bakllava', r'moondream', r'cogvlm', r'minicpm-v',
            r'internvl', r'paligemma', r'qwen.*vl', r'yi-vl', r'bunny',
            r'nanollava', r'obsidian', r'pixtral', r'llama.*vision', '-vl',
        }},
    'medical': {
        'tags': {'medical', 'image-feature-extraction'},
        'patterns': {
            r'medical', r'extract', r'path',
        }},
    'geo': {
        'tags': {
            'geospatial', 'earth-observation', 'image-feature-extraction',
            'zero-shot-image-classification', 'image-classification', 'gis'},
        'patterns': {
            r'geo', r'extract', r'path',
        }},
}


def rate_limited_call(func, max_retries=8, base_delay=5):
    for attempt in range(max_retries):
        try:
            return func()
        except (urllib.error.HTTPError, huggingface_hub.utils.HfHubHTTPError) as e:
            if '429' in str(e) or 'rate limit' in str(e).lower():
                delay = base_delay * (2 ** attempt)
                print(f'  Rate limited. Waiting {delay}s (attempt {attempt + 1}/{max_retries})')
                time.sleep(delay)
            else:
                raise
    msg = 'Max retries exceeded due to rate limiting'
    raise Exception(msg)


def extract_quantization(filename: str) -> str:
    filename_normalized = filename.upper().replace('-', '_')
    for quant in QUANT_PRIORITY_BITS:
        if quant in filename_normalized:
            return quant
    return 'UNKNOWN'


def matches_type(repo_id: str, model_type: str) -> bool:
    repo_lower = repo_id.lower()
    return any(re.search(p, repo_lower) for p in MODEL_PATTERNS[model_type]['patterns'])


def has_gguf_files(siblings: list) -> bool:
    if not siblings:
        return False
    return any(
        getattr(s, 'rfilename', '').endswith('.gguf')
        for s in siblings
    )


@cache.memoize(expire=86400 * 10)
def fetch_config_json(repo_id: str) -> dict:
    url = f'https://huggingface.co/{repo_id}/resolve/main/config.json'
    req = urllib.request.Request(url, headers={'User-Agent': 'huggingface-hub'})
    try:
        with rate_limited_call(lambda: urllib.request.urlopen(req, timeout=30)) as resp:
            config = json.loads(resp.read().decode())
            return config
    except Exception:
        return {}


class StreamingBuffer:
    def __init__(self, response):
        self.response = response
        self.buffer = bytearray()

    def read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.response.read(65536)
            if not chunk:
                break
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:n])
        self.buffer = self.buffer[n:]
        return result


def parse_gguf_metadata(stream: 'StreamingBuffer') -> dict:  # noqa
    GGUF_MAGIC = 0x46554747
    GGUF_VALUE_FORMATS = {
        0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i',
        6: '<f', 7: '<B', 10: '<Q', 11: '<q', 12: '<d',
    }

    def read_fmt(fmt):
        size = struct.calcsize(fmt)
        raw = stream.read(size)
        if len(raw) < size:
            raise BufferError
        return struct.unpack(fmt, raw)[0]

    def read_string():
        length = read_fmt('<Q')
        raw = stream.read(length)
        if len(raw) < length:
            raise BufferError
        return raw.decode('utf-8', errors='replace')

    def read_value(value_type):
        if value_type == 8:
            return read_string()
        if value_type == 9:
            elem_type = read_fmt('<I')
            count = read_fmt('<Q')
            return [read_value(elem_type) for _ in range(count)]
        fmt = GGUF_VALUE_FORMATS.get(value_type)
        if fmt is None:
            raise BufferError
        return read_fmt(fmt)

    try:
        if read_fmt('<I') != GGUF_MAGIC:
            return {}
        read_fmt('<I')
        read_fmt('<Q')
        metadata_kv_count = read_fmt('<Q')
    except BufferError:
        return {}
    metadata = {}
    for _ in range(metadata_kv_count):
        try:
            key = read_string()
            value_type = read_fmt('<I')
            value = read_value(value_type)
            if not key.startswith('tokenizer.ggml.'):
                metadata[key] = value
        except (BufferError, ValueError):
            break
    return metadata


@cache.memoize(expire=86400 * 10)
def fetch_gguf_metadata_from_repo(repo_id: str, filename: str) -> dict:
    url = f'https://huggingface.co/{repo_id}/resolve/main/{filename}'
    req = urllib.request.Request(url, headers={'User-Agent': 'huggingface-hub'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return parse_gguf_metadata(StreamingBuffer(resp))
    except Exception:
        return {}


def arch_params_from_config_json(config: dict, param_count_hint: int | None) -> ModelArchParams:
    config = config or {}

    def first_match(pattern):
        for key, value in config.items():
            if re.fullmatch(pattern, key):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    pass
        return None

    num_layers = first_match(r'num_hidden_layers|n_layers?')
    num_attention_heads = first_match(r'num_attention_heads|n_heads?')
    num_kv_heads = first_match(r'num_key_value_heads|n_head_kv|num_kv_heads') or num_attention_heads
    hidden_size = first_match(r'hidden_size|d_model|n_embd|model_dim')
    context_length = first_match(r'max_position_embeddings|max_seq_len|seq_length|n_positions')
    embedding_length = first_match(r'embedding_length')
    head_dim = hidden_size // num_attention_heads if hidden_size and num_attention_heads else None
    return ModelArchParams(
        param_count=param_count_hint,
        num_layers=num_layers,
        num_kv_heads=num_kv_heads,
        head_dim=head_dim,
        context_length=context_length,
        embedding_length=embedding_length,
    )


def arch_params_from_ollama_model_info(model_info: dict) -> ModelArchParams:
    model_info = model_info or {}
    TARGETS = {
        r'.+\.block_count': 'num_layers',
        r'.+\.head_count_kv': 'num_kv_heads',
        r'.+\.head_count': 'head_count',
        r'.+\.embedding_length': 'embedding_length',
        r'.+\.context_length': 'context_length',
        r'.+\.parameter_count': 'param_count',
        r'.+\.attention\.key_length': 'head_dim',
    }
    gathered = {}
    for key, value in model_info.items():
        if any(part in key.lower() for part in (
                '.vision.', '.visual.', 'mmproj', 'projector')):
            continue
        for pattern, target in TARGETS.items():
            if target not in gathered and re.fullmatch(pattern, key):
                try:
                    gathered[target] = (
                        int(math.ceil(sum(value) / len(value)))
                        if isinstance(value, list) else int(value))
                except (ValueError, TypeError):
                    pass
                break
    head_count = gathered.pop('head_count', None)
    embedding_length = gathered.pop('embedding_length', None)
    head_dim = gathered.pop(
        'head_dim',
        embedding_length // head_count if embedding_length and head_count else None)
    return ModelArchParams(
        param_count=gathered.get('param_count'),
        num_layers=gathered.get('num_layers'),
        num_kv_heads=gathered.get('num_kv_heads'),
        head_dim=head_dim,
        context_length=gathered.get('context_length'),
        embedding_length=embedding_length,
    )


def extract_capabilities_from_gguf(metadata: dict, filenames: list[str]) -> dict:
    caps = {'has_tools': False, 'has_reasoning': False, 'has_vision': False, 'has_embedding': False}
    arch = str(metadata.get('general.architecture', '')).lower()
    chat_template = str(metadata.get('tokenizer.chat_template', '')).lower()
    vision_archs = ['llava', 'clip', 'mllama', 'minicpmv', 'qwen2vl',
                    'qwen3vl', 'paligemma', 'cogvlm', 'internvl', 'pixtral']
    tags = metadata.get('general.tags', [])
    if any(v in arch for v in vision_archs):
        caps['has_vision'] = True
    if any('mmproj' in f for f in filenames):
        caps['has_vision'] = True
    if any('vision' in k for k in metadata) or any('vision' in t.lower() for t in tags):
        caps['has_vision'] = True
    if any('pooling_type' in k for k in metadata) or any('embed' in t.lower() for t in tags):
        caps['has_embedding'] = True
    if 'bert' in arch or 'nomic' in arch or 'embedding' in arch:
        caps['has_embedding'] = True
    if 'tools' in chat_template or 'function' in chat_template or 'tool_call' in chat_template:
        caps['has_tools'] = True
    if metadata.get('general.reasoning'):
        caps['has_reasoning'] = True
    if 'think' in chat_template or 'reasoning' in chat_template:
        caps['has_reasoning'] = True
    return caps


def extract_capabilities_from_ollama(show_response: dict) -> dict:
    caps = {'has_tools': False, 'has_reasoning': False, 'has_vision': False, 'has_embedding': False}
    capabilities = show_response.get('capabilities', []) or show_response.get(
        'details', {}).get('capabilities', [])
    for cap in capabilities:
        cap_lower = cap.lower()
        if cap_lower == 'tools':
            caps['has_tools'] = True
        elif cap_lower in ('thinking', 'reasoning'):
            caps['has_reasoning'] = True
        elif cap_lower == 'vision':
            caps['has_vision'] = True
        elif cap_lower == 'embedding':
            caps['has_embedding'] = True
    return caps


def compute_memory_burden_gb(
    arch: ModelArchParams, quantization: str, requested_context: int,
    resident_weight_gb: float | None = None,
) -> float | None:
    bits_per_weight = QUANT_PRIORITY_BITS.get(quantization, (0, 16))[1]
    weights_bytes = None if resident_weight_gb is None else resident_weight_gb * 1024 ** 3
    if bits_per_weight is not None and arch is not None and arch.param_count is not None:
        weights_bytes = max(weights_bytes or 0, arch.param_count * bits_per_weight / 8)
    if weights_bytes is None:
        return None
    if arch is None:
        return weights_bytes / 1024 ** 3
    effective_context = requested_context
    if arch.context_length is not None:
        effective_context = min(requested_context, arch.context_length)
    kv_bytes = 0
    if arch.num_layers is not None and arch.num_kv_heads is not None and arch.head_dim is not None:
        kv_bytes = 4 * arch.num_layers * arch.num_kv_heads * arch.head_dim * effective_context
    return (weights_bytes + kv_bytes) / (1024 ** 3)


def estimate_ollama_memory_gb(
    size_gb: float, quantization: str, context_size: int | None,
) -> float | None:
    """
    Estimate memory burden for Ollama models using file size and quantization.

    For GGUF models without full architecture info, estimate based on file
    size, quantizations, and context length.
    """
    bits_per_weight = QUANT_PRIORITY_BITS.get(quantization, (None, 16))[1]
    if size_gb is None or size_gb <= 0:
        return None
    if bits_per_weight and bits_per_weight > 0:
        quant_factor = bits_per_weight / 32.0
        if context_size and context_size > 0:
            # Estimate KV cache based on context size
            # Without arch params, assume reasonable defaults
            # Cap at 4x for large contexts
            context_factor = min(context_size / 32768.0, 4.0)
            overhead = 1.5 + (quant_factor * 0.5) + context_factor * 0.5
            return size_gb * overhead
        # Without context info, just add overhead
        return size_gb * 2.0
    return size_gb * 1.5


@cache.memoize(expire=86400 * 10)
def fetch_gguf_file_sizes(api: huggingface_hub.HfApi, repo_id: str) -> list[tuple[str, int, bool]]:
    try:
        files = rate_limited_call(lambda: list(api.list_repo_tree(repo_id, recursive=False)))
    except Exception:
        return []
    single_files = []
    chunked_groups = {}
    for f in files:
        filename = getattr(f, 'path', None)
        if not filename or not filename.endswith('.gguf') or 'mmproj' in filename:
            continue
        size = getattr(f, 'size', None)
        if not size:
            continue
        chunk_match = re.match(r'(.+)-(\d{5})-of-(\d{5})\.gguf$', filename)
        if chunk_match:
            base = chunk_match.group(1)
            chunked_groups[base] = chunked_groups.get(base, 0) + size
        else:
            single_files.append((filename, size, False))
    for base, total_size in chunked_groups.items():
        single_files.append((f'{base}.gguf', total_size, True))
    return single_files


@cache.memoize(expire=86400 * 10)
def fetch_gguf_auxiliary_size_bytes(api: huggingface_hub.HfApi, repo_id: str) -> int:
    try:
        files = rate_limited_call(lambda: list(api.list_repo_tree(repo_id, recursive=False)))
    except Exception:
        return 0
    sizes = []
    for f in files:
        filename = getattr(f, 'path', '') or ''
        if filename.endswith('.gguf') and re.search(
                r'(^|[-_.])(mmproj|projector)([-_.]|$)', filename.lower()):
            sizes.append(getattr(f, 'size', 0) or 0)
    return max(sizes, default=0)


def select_best_quantization(
    candidates: list[ModelInfo], gpu_memory_gb: float,
    min_memory: float | None, context_limit_gb: float | None = None,
) -> ModelInfo | None:
    fitting = [
        m for m in candidates if QUANT_PRIORITY_BITS.get(m.quantization, (None, 0))[0] is not None]
    if context_limit_gb is not None:
        fitting = [
            m for m in fitting if m.memory_burden_gb is not None and
            (min_memory or 0) <= m.memory_burden_gb <= context_limit_gb]
    else:
        fitting = [m for m in fitting if (min_memory or 0) <= m.size_gb <= gpu_memory_gb]
    if not fitting:
        return None
    fitting.sort(key=lambda m: QUANT_PRIORITY_BITS.get(m.quantization, (99, 0))[0])
    return fitting[0]


@cache.memoize(expire=86400)
def fetch_models_for_tag(tag, limit: int) -> list:
    def fetch(t=tag):
        kwargs = {}
        expsib = ['siblings']
        if 'apps' in inspect.signature(huggingface_hub.list_models).parameters:
            kwargs['apps'] = 'ollama'
            expsib = []
        models = []
        for m in huggingface_hub.list_models(
            filter=t,
            # gated=False,
            expand=['createdAt', 'lastModified', 'gguf'] + expsib,
            sort='downloads',
            limit=limit if limit else None,
            **kwargs,
        ):
            models.append(argparse.Namespace(**{k: getattr(m, k) for k in {
                'author', 'card_data', 'config', 'created_at', 'downloads',
                'gated', 'gguf', 'id', 'last_modified', 'siblings', 'tags',
            } if hasattr(m, k)}))
        return models

    print(f"  Fetching models with tag '{tag}'")
    return rate_limited_call(fetch)


def fetch_models_for_tags(tags: set[str], limit: int, downloads: int) -> list:
    all_models = []
    for tag in tags:
        all_models.extend(fetch_models_for_tag(tag, limit))
    seen = set()
    unique = []
    for m in all_models:
        if m.id not in seen and m.downloads >= downloads:
            seen.add(m.id)
            unique.append(m)
    return unique


def discover_models(  # noqa
    api: huggingface_hub.HfApi, gpu_memory_gb: float | None, model_filter: str,
    limit: int, downloads: int, name_filter: str | None = None,
    min_memory: float | None = None, context_memory: int = 32768,
    context_limit_gb: float | None = None,
) -> list[ModelInfo]:
    print(f'Fetching {model_filter} models from HuggingFace')
    tags = set()
    for key in MODEL_PATTERNS:
        if model_filter in {key, 'all'}:
            tags |= MODEL_PATTERNS[key]['tags']
    found_models = fetch_models_for_tags(tags, limit, downloads)
    print(f'Retrieved {len(found_models)} candidate models')
    all_gguf = 'apps' in inspect.signature(huggingface_hub.list_models).parameters
    with_gguf = [
        m for m in found_models
        if (not name_filter or re.search(name_filter, m.id, re.IGNORECASE)) and
        (all_gguf or has_gguf_files(getattr(m, 'siblings', None)))
    ]
    discovered = {}
    skipped_no_fit = 0
    skipped_fetch_failed = 0
    tw, _ = shutil.get_terminal_size()
    if model_filter != 'all':
        with_gguf = [m for m in with_gguf if matches_type(m.id, model_filter)]
    for model in tqdm.tqdm(with_gguf, ncols=tw):
        model_type = model_filter if model_filter != 'all' else (
            'code' if matches_type(model.id, 'code') else
            'vision' if matches_type(model.id, 'vision') else None
        )
        gguf_files = fetch_gguf_file_sizes(api, model.id)
        if not gguf_files:
            skipped_fetch_failed += 1
            continue
        if context_limit_gb is not None and gguf_files:
            gguf_files = [f for f in gguf_files if f[1] / 1024**3 <= context_limit_gb]
            if not gguf_files:
                skipped_no_fit += 1
                continue
        auxiliary_size_gb = fetch_gguf_auxiliary_size_bytes(api, model.id) / (1024 ** 3)
        gguf_meta = getattr(model, 'gguf', {}) or {}
        gguf_candidate = next((f for f, _, chunked in gguf_files if not chunked), None)
        gguf_meta_parsed = fetch_gguf_metadata_from_repo(
            model.id, gguf_candidate) if gguf_candidate else {}
        caps = extract_capabilities_from_gguf(gguf_meta_parsed, [f for f, _, _ in gguf_files])
        try:
            param_count_hint = int(gguf_meta['total']) if 'total' in gguf_meta else None
        except (ValueError, TypeError):
            param_count_hint = None
        try:
            hf_context_length = int(
                gguf_meta['context_length']) if 'context_length' in gguf_meta else None
        except (ValueError, TypeError):
            hf_context_length = None
        arch = arch_params_from_config_json(fetch_config_json(model.id), param_count_hint)
        if arch.context_length is None and hf_context_length is not None:
            arch = ModelArchParams(
                param_count=arch.param_count, num_layers=arch.num_layers,
                num_kv_heads=arch.num_kv_heads, head_dim=arch.head_dim,
                context_length=hf_context_length,
                embedding_length=arch.embedding_length,
            )
        if arch.num_layers is None or arch.num_kv_heads is None or arch.head_dim is None:
            gguf_candidate = next((f for f, _, chunked in gguf_files if not chunked), None)
            if gguf_candidate:
                gguf_meta_parsed = fetch_gguf_metadata_from_repo(model.id, gguf_candidate)
                if gguf_meta_parsed:
                    gguf_arch = arch_params_from_ollama_model_info(gguf_meta_parsed)
                    arch = ModelArchParams(
                        param_count=arch.param_count or gguf_arch.param_count,
                        num_layers=arch.num_layers or gguf_arch.num_layers,
                        num_kv_heads=arch.num_kv_heads or gguf_arch.num_kv_heads,
                        head_dim=arch.head_dim or gguf_arch.head_dim,
                        context_length=arch.context_length or gguf_arch.context_length,
                        embedding_length=arch.embedding_length or gguf_arch.embedding_length,
                    )
        candidates = []
        quants = {}
        for filename, size_bytes, is_chunked in gguf_files:
            if is_chunked:
                continue
            quant = extract_quantization(filename)
            if quant == 'UNKNOWN':
                continue
            mem_gb = size_bytes / (1024 ** 3)
            if quant not in quants or mem_gb > quants[quant]['size']:
                quants[quant] = {'file': filename, 'size': mem_gb}
        for quant, info in quants.items():
            candidates.append(ModelInfo(
                source='huggingface',
                repo_id=model.id,
                repo_name=model.id,
                filename=info['file'],
                size_gb=info['size'] + auxiliary_size_gb,
                quantization=quant,
                model_type=model_type,
                is_chunked=False,
                downloads=getattr(model, 'downloads', 0) or 0,
                created=getattr(model, 'created_at', None),
                modified=getattr(model, 'last_modified', None),
                context_size=arch.context_length,
                arch_params=arch,
                memory_burden_gb=compute_memory_burden_gb(
                    arch, quant, context_memory, info['size'] + auxiliary_size_gb),
                has_tools=caps['has_tools'],
                has_reasoning=caps['has_reasoning'],
                has_vision=caps['has_vision'],
                has_embedding=caps['has_embedding'],
            ))
        best = select_best_quantization(candidates, gpu_memory_gb, min_memory, context_limit_gb)
        if best:
            discovered[model.id] = best
        else:
            skipped_no_fit += 1
    print(f'Found {len(discovered)} matching models')
    print(f'  Skipped (fetch failed): {skipped_fetch_failed}')
    print(f'  Skipped (all too large): {skipped_no_fit}')
    return list(discovered.values())


def format_ollama_tag(filename: str) -> str:
    base = re.sub(r'\.gguf$', '', filename, flags=re.IGNORECASE)
    base = re.sub(r'-\d{5}-of-\d{5}$', '', base)
    tag = base.split('-')[-1] if '-' in base else base.split('_')[-1]
    return tag.upper()


def ollama_api_get(host: str, path: str) -> object:
    if '://' not in host:
        url = f'http://{host}{path}'
    else:
        url = f'{host}{path}'
    with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
        return json.loads(resp.read().decode())


def ollama_api_post(host: str, path: str, body: dict) -> object:
    if '://' not in host:
        url = f'http://{host}{path}'
    else:
        url = f'{host}{path}'
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def infer_model_type_from_details(details: dict, name: str) -> str | None:
    arch = (details.get('family', '') or details.get('architecture', '') or '').lower()
    name_lower = name.lower()
    for key in MODEL_PATTERNS:
        if key in arch or any(x in arch for x in MODEL_PATTERNS[key].get('arch', set())):
            return key
        if any(re.search(p, name_lower) for p in MODEL_PATTERNS[key]['patterns']):
            return key
    return None


def infer_quantization_from_info_block(metadata: dict, size_bytes: int) -> str:
    try:
        # Use a small reduction for overhead
        bits = size_bytes * 8 * 0.98 / metadata['general.parameter_count']
        closest = None
        quant = 'UNKNOWN'
        for key, (_, quantbits) in QUANT_PRIORITY_BITS.items():
            dist = abs(bits - quantbits)
            if closest is None or dist < closest:
                closest = dist
                quant = key
        return quant
    except Exception:
        pass
    return 'UNKNOWN'


def discover_ollama_models(  # noqa
    host: str, name_filter: str | None, gpu_memory_gb: float | None,
    context_memory: int = 32768, context_limit_gb: float | None = None,
) -> list[ModelInfo]:
    try:
        tags_response = ollama_api_get(host, '/api/tags')
    except (urllib.error.URLError, OSError) as e:
        print(f'Could not reach ollama at {host}: {e}')
        return []
    models = []
    for entry in tags_response.get('models', []):
        name = entry.get('name', '')
        if name_filter and not re.search(name_filter, name, re.IGNORECASE):
            continue
        size_bytes = entry.get('size', 0)
        size_gb = size_bytes / (1024 ** 3)
        if gpu_memory_gb is not None and size_gb > gpu_memory_gb:
            continue
        if context_limit_gb is not None and size_gb > context_limit_gb:
            continue
        modified = None
        if modified_str := entry.get('modified_at', ''):
            try:
                modified = dateutil.parser.parse(modified_str).astimezone(datetime.timezone.utc)
            except (ValueError, OverflowError):
                pass
        try:
            show_response = ollama_api_post(host, '/api/show', {'name': name, 'verbose': True})
        except (urllib.error.URLError, OSError) as e:
            print(f'  Could not fetch details for {name}: {e}')
            show_response = {}
        details = show_response.get('details', {}) or {}
        caps = extract_capabilities_from_ollama(show_response)
        model_info_block = show_response.get('model_info', {}) or {}
        quantization = (details.get('quantization_level', '') or '').upper()
        if not quantization or quantization == 'UNKNOWN':
            quantization = infer_quantization_from_info_block(model_info_block, size_bytes)
        if not quantization or quantization == 'UNKNOWN':
            for key in model_info_block:
                if 'quantization' in key.lower() and 'version' not in key.lower():
                    quantization = str(model_info_block[key]).upper()
                    break
        if not quantization or quantization == 'UNKNOWN':
            tag_part = name.rsplit(':', 1)[1] if ':' in name else ''
            quantization = ''.join(tag_part.upper().split('.GGUF')).split(
                '.')[-1].split('-')[-1] or 'UNKNOWN'
        arch = arch_params_from_ollama_model_info(model_info_block)
        memory_burden_gb = compute_memory_burden_gb(arch, quantization, context_memory, size_gb)
        if context_limit_gb is not None and (
                memory_burden_gb is None or memory_burden_gb > context_limit_gb):
            continue
        models.append(ModelInfo(
            source='ollama',
            repo_id=name,
            repo_name=name,
            filename=name,
            size_gb=size_gb,
            quantization=quantization,
            model_type=infer_model_type_from_details(details, name),
            is_chunked=False,
            downloads=0,
            created=modified,
            modified=modified,
            context_size=arch.context_length,
            arch_params=arch,
            memory_burden_gb=memory_burden_gb,
            has_tools=caps['has_tools'],
            has_reasoning=caps['has_reasoning'],
            has_vision=caps['has_vision'],
            has_embedding=caps['has_embedding'],
        ))
    return models


OLLAMA_HTMX_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'HX-Request': 'true',
    'Accept': 'text/html',
}

OLLAMA_PLAIN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Accept': 'text/html',
}


def extract_pull_number(text: str) -> int | None:
    """Extract a numeric pull count from raw span text like '15.8M', '2', etc.

    Returns None when the text does not look like a valid pull count (e.g. model param
    sizes such as '4b', bare integers less than 1.0, or non-numeric strings).
    """
    t = text.strip()
    if not t:
        return None
    # Strip trailing commas and whitespace
    t = t.rstrip(',')
    multipliers: dict[str, int] = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}
    for suffix, mult in multipliers.items():
        if t.upper().endswith(suffix):
            num_part = t[:-1].strip()
            try:
                n = float(num_part.replace(',', ''))
                return int(n * mult)
            except ValueError:
                continue  # e.g. 'e2b' — strip 'b' leaves just 'e', not a valid float
    try:
        n = int(t)
        return n if n > 0 else None
    except ValueError:
        return None


def score_model(m: ModelInfo, gpu_memory: float | None,
                context_limit: float | None) -> float:
    """Score a model for selection. Higher = better.

    Priority within a single search result (which all share one repo_id):
    1. GPU memory available: prefer larger models whose size fits GPU memory.
    2. Among equal-size fits, prefer higher-quantization depth.
    """
    size_gb = m.size_gb or 0
    if gpu_memory is not None and size_gb > gpu_memory:
        return -1.0  # won't fit — never selected when GPU constrained
    bits, _ = QUANT_PRIORITY_BITS.get(m.quantization, (None, 0))
    if bits is None:
        return -1.0  # no valid quant = skip
    # Primary: use more of the available GPU memory
    # Secondary: higher bit-depth among same-size matches.
    score = size_gb * 1000.0 + bits
    return float(score)


def _select_best_model(models_in: list[ModelInfo], gpu_memory: float | None,
                       context_limit: float | None) -> ModelInfo | None:
    """Pick one model from a group of candidates that share the same repo_id."""
    best: ModelInfo | None = None
    best_score: float = -1.0
    for m in models_in:
        s = score_model(m, gpu_memory, context_limit)
        if s > best_score:
            best_score = s
            best = m
    return best


def parse_pull_count(text: str) -> int:
    text = text.strip().replace(',', '')
    multipliers = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}
    for suffix, mult in multipliers.items():
        if text.upper().endswith(suffix):
            return int(float(text[:-1]) * mult)
    try:
        return int(text)
    except ValueError:
        return 0


def parse_size_text(text: str) -> float:
    text = text.strip().upper()
    if text.endswith('GB'):
        return float(text[:-2])
    if text.endswith('MB'):
        return float(text[:-2]) / 1024.0
    if text.endswith('TB'):
        return float(text[:-2]) * 1024.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_context_text(text: str) -> int | None:
    text = text.strip().upper()
    if text.endswith('K'):
        return int(float(text[:-1]) * 1024)
    if text.endswith('M'):
        return int(float(text[:-1]) * 1024 * 1024)
    try:
        return int(text)
    except ValueError:
        return None


@cache.memoize(expire=86400)
def fetch_ollama_search_page(page: int, query: str) -> str:
    sys.stdout.write(f'  Fetching search page {page}\r')
    sys.stdout.flush()
    url = f'https://ollama.com/search?page={page}&q={query}'
    req = urllib.request.Request(url, headers=OLLAMA_HTMX_HEADERS)
    with rate_limited_call(lambda: urllib.request.urlopen(req, timeout=30)) as resp:
        return resp.read().decode('utf-8', errors='replace')


@cache.memoize(expire=86400)
def fetch_ollama_tags_page(model_path: str) -> str:
    url = f'https://ollama.com{model_path}/tags'
    req = urllib.request.Request(url, headers=OLLAMA_PLAIN_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode('utf-8', errors='replace')


def scrape_ollama_search_models(query: str, limit: int, downloads_min: int) -> list[dict]:  # noqa
    models: list[dict] = []
    seen: set[str] = set()
    page = 1
    while True:
        try:
            html = fetch_ollama_search_page(page, query)
        except (urllib.error.URLError, OSError) as e:
            print(f'  Failed to fetch page {page}: {e}')
            break
        model_blocks = re.findall(
            r'<li[^>]*class="[^"]*items-baseline[^"]*"[^>]*>(.*?)</a>\s*</li>',
            html, re.DOTALL)
        if not model_blocks:
            break
        for block in model_blocks:
            # Extract href (e.g. "/user/ModelName-Q5_K_M")
            href_match = re.search(r'href="(/[^"]*?/[^"]+)"', block)
            if not href_match:
                continue
            link_path = href_match.group(1).rstrip('"')
            # Extract all title attributes early — we need them for datae
            # parsing and as a fallback for display names when span isn't
            # available.
            all_titles: list[str] = re.findall(r'title="([^"]*)"', block)
            # Prefer <h2><span>user/name</span> over title="" because the
            # span includes the full user/repo name which is essential for
            # anchored regex patterns (e.g. "^qwen3.5" must not match
            # "user/qwen3.5...").
            displayed: str | None = None
            span_match = re.search(
                r'<h2[^>]*>\s*<span[^>]*>([^<]+)</span>', block, re.DOTALL)
            if span_match:
                span_name = span_match.group(1).strip()
                if span_name and span_name not in seen:
                    displayed = span_name
            if not displayed:
                for t in all_titles:
                    if 'AM' in t or 'PM' in t or re.search(
                            r'\d{1,2}:\d{2}\s*(AM|PM)', t, re.IGNORECASE):
                        continue
                    stripped_t = t.strip()
                    if stripped_t and stripped_t not in seen:
                        displayed = stripped_t
                        break
            if not displayed:
                continue
            seen.add(displayed)
            # Extract pull count: look for the <span> before "Pulls" label
            # Handle suffixed values like "15.8M" and raw integers.
            pull_num: str = '0'
            all_pull_spans = list(re.finditer(r'<span[^>]*>([^<]*)</span>', block))
            for pm in all_pull_spans:
                if pm.group(1).strip().lower() == 'pulls':
                    # Look backward ~200 chars for a <span> number
                    idx = pm.start()
                    prior = block[max(0, idx - 200):idx]
                    num_match = re.search(r'<span[^>]*>([^<]*)</span>', prior)
                    if num_match:
                        nval = extract_pull_number(num_match.group(1))
                        if nval:
                            pull_num = str(nval)
                            break
            # Fallback: look forward from the label span
            if pull_num == '0':
                for pm in all_pull_spans:
                    if pm.group(1).strip().lower() == 'pulls':
                        fwd = block[pm.end():]
                        fs = re.search(r'<span[^>]*>([^<]*)</span>', fwd)
                        if fs:
                            nval = extract_pull_number(fs.group(1))
                            if nval:
                                pull_num = str(nval)
                                break
                        break
            # Fallback: pick the largest span value that looks like a
            # suffixed count
            if pull_num == '0':
                large = 0
                for m in all_pull_spans:
                    s = m.group(1).strip()
                    nval = extract_pull_number(s)
                    if nval and nval > large:
                        large = nval
            if pull_num != '0' and parse_pull_count(pull_num) < 100:
                has_m_suffix = any(
                    m.group(1).strip().upper().endswith(('M', 'K')) for m in all_pull_spans)
                tags_spans = [sp.group(1).strip() for sp in all_pull_spans]
                if not has_m_suffix and any('tags' in t.lower() for t in tags_spans):
                    pull_num = '0'  # Discard — likely a tag count, not pulls
            pull_count = parse_pull_count(pull_num)
            # Extract capabilities from colored badge spans (rounded-md class)
            capabilities: list[str] = re.findall(
                r'class="[^"]*rounded-md[^"]*"[^>]*>([^<]+)', block)
            capabilities = [c.strip().lower() for c in capabilities]
            all_badges: list[str] = re.findall(
                r'class="[^"]*rounded-md[^"]*"[^>]*>([^<]+)', block)
            badges_set = {b.strip().lower() for b in all_badges}
            is_cloud: bool = 'cloud' in badges_set
            sizes: list[str] = []
            # Extract last-modified date from title attributes with UTC/AM/PM
            updated_title: str = ''
            for dt in all_titles:
                try:
                    import dateutil.parser as _parser_dt
                    parsed_dt = _parser_dt.parse(dt)
                    if parsed_dt.tzinfo is None:
                        continue
                    updated_title = dt
                    break
                except (ValueError, OverflowError):
                    continue
            models.append({
                'name': displayed,
                'href': link_path,
                'pulls': pull_count,
                'capabilities': capabilities,
                'is_cloud': is_cloud,
                'param_sizes': sizes,
                'updated_title': updated_title,
            })
            if limit and len(models) >= limit:
                break
        if limit and len(models) >= limit:
            break
        # Check for next page via hx-get="...page=N" pattern
        all_hx_get = re.findall(r'hx-get="([^"]*)"', html)
        if not any(f'page={page + 1}' in h for h in all_hx_get):
            break
        page += 1
    return models


def scrape_ollama_tags_for_model(model_info: dict) -> list[dict]:
    """Scrape the tags table from an Ollama model's /tags page using regex.

    Uses stable text patterns from within tag rows rather than specific class
    combinations. Future-proof against DOM refactors.
    """
    try:
        html = fetch_ollama_tags_page(model_info['href'])
    except (urllib.error.URLError, OSError) as e:
        print(f"  Failed to fetch tags for {model_info['name']}: {e}")
        return []
    tags: list[dict] = []
    tags_by_hash: dict[str, dict] = {}
    # Locate the table header row to find where tag data begins
    hdr_pos = html.find('grid-cols-12 text-neutral-900">')
    if hdr_pos < 0:
        return []
    body_after_header = html[hdr_pos:]
    tags_seen: set[str] = set()
    # Find all tag links matching /user/model:tag pattern
    for m in re.finditer(r'href="(/([^/:]+/[^/:]+):([^"]+))"', body_after_header):
        full_path, _, tag_suffix = m.groups()
        if not tag_suffix or tag_suffix in tags_seen:
            continue
        tags_seen.add(tag_suffix)
        # Gather context before and after the link for size/ctx extraction
        ctx_before: str = body_after_header[0:m.start()]
        ctx_after: str = body_after_header[m.end():m.end() + 1500]
        combined_context: str = ctx_before[-600:] + ' ' + ctx_after
        # Extract sha hash from font-mono span
        hash_match = re.search(r'<span class="font-mono">\s*(\w{12,})', combined_context)
        hash_text: str = hash_match.group(1).strip() if hash_match else ''
        # Size: extract GB/MB patterns (NOT model parameter sizes like "7B")
        sizes_found: list[str] = re.findall(
            r'([\d,.]+\s*GB|[\d,.]+\s*MB)', combined_context)
        size_text: str = sizes_found[0].strip() if sizes_found else ''
        # Context window size (e.g. "125K") near the word "context"
        context_match = re.search(
            r'(\d[\d,]*\s*K)\s*(?:context|window)', combined_context, re.IGNORECASE)
        context_text: str = context_match.group(1).strip() if context_match else ''
        # Input type (e.g., "Text input")
        input_m = re.search(r'([A-Za-z]+?)\s*input', combined_context, re.IGNORECASE)
        input_prefix: str = input_m.group(1)[0].lower() + 'nput' if input_m else ''
        # Detect cloud-only entries: tags like ':cloud' or ':xxx-cloud' have
        # no local file — their size_text leaks from neighbor rows in
        # combined_context
        is_cloud_api_entry = bool(
            re.search(r'cloud', tag_suffix, re.IGNORECASE) and
            not any(s.upper().startswith('MLX') for s in sizes_found))
        # For library model tags pages, cloud entries have a SHA hash but no
        # dedicated size row. If we found 397b-cloud or similar with no
        # actual GB near the SHA line, mark as cloud API-only regardless of
        # noisy context-window sizes.
        tag_info: dict[str, object] = {
            'tag': ':' + tag_suffix,
            'taglist': [':' + tag_suffix],
            'size_text': size_text if not is_cloud_api_entry else '',
            'context_text': context_text,
            'input_text': input_prefix,
            'hash': hash_text,
            '_is_cloud_api': is_cloud_api_entry,  # internal flag for discover filter
        }
        if hash_text and hash_text in tags_by_hash:
            tags_by_hash[hash_text]['taglist'].append(':' + tag_suffix)
        else:
            tags.append(tag_info)
            if hash_text:
                tags_by_hash[hash_text] = tag_info
    return tags


def discover_ollama_registry_models(  # noqa
    name_filter: str | None, gpu_memory_gb: float | None,
    model_filter: str, limit: int, downloads: int,
    context_memory: int = 32768, context_limit_gb: float | None = None,
    min_memory: float | None = None,
) -> list[ModelInfo]:
    print('Fetching models from Ollama registry')
    search_results = scrape_ollama_search_models('.', limit, downloads)
    print(f'Retrieved {len(search_results)} models from search')
    # Filter on search page titles before expensive per-model API calls.
    # Note: these lack tag suffixes (e.g.:7b-fp16), so filters like '.*7b'
    # won't match here.
    if name_filter:
        search_results = [
            m for m in search_results
            if re.search(name_filter, m['name'], re.IGNORECASE)]
        print(f'  After title filter: {len(search_results)} models')
    models = []
    skipped_cloud = 0
    skipped_no_fit = 0
    skipped_type_mismatch = 0
    tw, _ = shutil.get_terminal_size()
    for search_model in tqdm.tqdm(search_results, ncols=tw):
        tag_details = scrape_ollama_tags_for_model(search_model)
        if not tag_details:
            skipped_no_fit += 1
            continue
        # Filter by name using the full model name (base + tag suffix) since
        # search page titles alone may lack version info like ':7b-fp16'.
        if name_filter:
            all_candidate_names = [f'{search_model["name"]}{t.get("tag", "")}' for t in tag_details]
            if not any(re.search(name_filter, n, re.IGNORECASE) for n in all_candidate_names):
                continue
        if search_model['is_cloud'] and not any(t.get('size_text') for t in tag_details):
            skipped_cloud += 1
            continue
        caps = {
            'has_tools': 'tools' in search_model['capabilities'],
            'has_reasoning': 'thinking' in search_model['capabilities'],
            'has_vision': 'vision' in search_model['capabilities'],
            'has_embedding': 'embedding' in search_model['capabilities'],
        }
        model_type = infer_model_type_from_details(
            {'capabilities': search_model['capabilities']}, search_model['name'])
        if caps['has_vision']:
            model_type = model_type or 'vision'
        if caps['has_embedding']:
            model_type = model_type or 'embed'
        if model_filter != 'all' and model_type != model_filter:
            if not matches_type(search_model['name'], model_filter):
                skipped_type_mismatch += 1
                continue
        modified = None
        if search_model['updated_title']:
            try:
                modified = dateutil.parser.parse(
                    search_model['updated_title']).astimezone(datetime.timezone.utc)
            except (ValueError, OverflowError):
                pass
        candidates = []
        for tag_info in tag_details:
            tag_name = tag_info['tag']
            # Skip Apple hardware
            if any(t in tag_name.lower() for t in {'mlx', 'mx', 'int8', 'nvfp4', 'int4'}):
                continue
            # Skip cloud-API-only tags (no local download available)
            if tag_info.get('_is_cloud_api'):
                continue
            tag_suffix = tag_name.split(':', 1)[-1]
            size_gb = parse_size_text(tag_info['size_text'])
            if not size_gb:
                continue
            context_size = parse_context_text(tag_info['context_text'])
            quant = extract_quantization(tag_suffix)
            if quant == 'UNKNOWN':
                for subtag in tag_info.get('taglist', []):
                    quant = extract_quantization(subtag.split(':', 1)[-1])
                    if quant != 'UNKNOWN':
                        break
            if quant == 'UNKNOWN':
                quant = 'Q4_K_M'
            if gpu_memory_gb is not None and size_gb > gpu_memory_gb:
                continue
            if context_limit_gb is not None and size_gb > context_limit_gb:
                continue
            candidates.append(ModelInfo(
                source='ollama-registry',
                repo_id=search_model['name'],
                repo_name=f'{search_model["name"]}:{tag_suffix}',
                filename=tag_name,
                size_gb=size_gb,
                quantization=quant,
                model_type=model_type,
                is_chunked=False,
                downloads=search_model['pulls'],
                created=modified,
                modified=modified,
                context_size=context_size,
                arch_params=ModelArchParams(
                    param_count=None, num_layers=None,
                    num_kv_heads=None, head_dim=None,
                    context_length=context_size,
                    embedding_length=None,
                ),
                memory_burden_gb=estimate_ollama_memory_gb(size_gb, quant, context_size),
                has_tools=caps['has_tools'],
                has_reasoning=caps['has_reasoning'],
                has_vision=caps['has_vision'],
                has_embedding=caps['has_embedding'],
            ))
        if not candidates:
            skipped_no_fit += 1
            continue
        # Filter and add all valid candidates (per size/quant combination)
        fitting_candidates = []
        for m in candidates:
            quant_priority = QUANT_PRIORITY_BITS.get(m.quantization, (None, 0))[0]
            if quant_priority is None:
                continue
            if context_limit_gb is not None and m.memory_burden_gb is not None:
                # Use estimated memory burden for filtering
                if (min_memory or 0) <= m.memory_burden_gb <= context_limit_gb:
                    fitting_candidates.append(m)
            else:
                # Size-based filtering with GPU memory constraint
                if (min_memory or 0) <= m.size_gb <= (gpu_memory_gb or math.inf):
                    fitting_candidates.append(m)
        # Within one search result (one repository) consolidate quant-only
        # variants of the same physical GGUF file.  Keep exactly one ModelInfo
        # per (repo, size_bucket) and prefer the highest-quantized version that
        # still fits GPU constraints.
        size_buckets: dict[tuple[str, int], ModelInfo] = {}
        for m in fitting_candidates:
            key = (m.repo_id, round(m.size_gb))
            candidate_score = score_model(m, gpu_memory_gb, context_limit_gb)
            if key not in size_buckets or candidate_score > score_model(
                    size_buckets[key], gpu_memory_gb, context_limit_gb):
                size_buckets[key] = m
        fitting_candidates = list(size_buckets.values())
        models.extend(fitting_candidates)
        if not fitting_candidates:
            skipped_no_fit += 1
    print(f'  Skipped (cloud only): {skipped_cloud}')
    print(f'  Skipped (type mismatch): {skipped_type_mismatch}')
    print(f'  Skipped (no fit): {skipped_no_fit}')
    return models


def main():  # noqa
    parser = argparse.ArgumentParser(
        description='Find Ollama-compatible models from HuggingFace',
    )
    parser.add_argument(
        '-m', '--gpu-memory-gb', type=float, default=None,
        help='Available GPU memory in gigabytes (required unless --local)',
    )
    parser.add_argument(
        '--min', '--min-gpu-memory-gb', type=float,
        help='Minimum model GPU memory in gigabytes',
    )
    parser.add_argument(
        '-f', '--filter', choices=sorted(MODEL_PATTERNS) + ['all'], default='all',
        help='Filter by model type (default: all)',
    )
    parser.add_argument(
        '-l', '--limit', type=int, default=0,
        help='Maximum models to fetch per category, 0 for all (default: 0)',
    )
    parser.add_argument(
        '-d', '--downloads', type=int, default=0,
        help='Minimum downloads to include (default: 0)',
    )
    parser.add_argument(
        '-o', '--output-format', choices=['table', 'pull', 't', 'p'],
        default='table',
        help='Output format (default: table)',
    )
    parser.add_argument('-r', '--regex', help='Filter model names via a case-insensitive regex.')
    parser.add_argument(
        '--modified', action='store_true', default=False,
        help='Show modified date rather than created date')
    parser.add_argument(
        '--before', help='Only show models before this date')
    parser.add_argument(
        '--after', '--since', help='Only show models after this date')
    parser.add_argument(
        '--source', '-s', default='hf',
        help='Model source: hf/huggingface, local (ollama server), ollama (registry)',
    )
    parser.add_argument(
        '--local', action='store_true', default=False,
        help='Shorthand for --source local',
    )
    parser.add_argument(
        '--ollama-host', default=os.environ.get('OLLAMA_HOST', '127.0.0.1:11434'),
        help='Ollama server address (default: 127.0.0.1:11434 or OLLAMA_HOST env var)',
    )
    parser.add_argument(
        '--context-memory', '-c', type=int, default=32768,
        help='Context size to use when estimating memory burden (default: 32768)',
    )
    parser.add_argument(
        '-x', '--context-limit', type=float, default=None,
        help='Use context memory as the measure for choosing models',
    )
    args = parser.parse_args()
    if args.local:
        args.source = 'local'
    source = set(args.source.split(','))
    columns = {
        'repo': {
            'name': 'Repository', 'format': 'rw',
            'func': lambda m, rw: m.repo_name[:rw - 3] + '...'
            if len(m.repo_name) > rw else m.repo_name},
        'quantization': {'name': 'Quant', 'format': ' <7', 'func': lambda m: m.quantization},
        'size_gb': {'name': 'SizGB', 'format': ' 5.1f', 'func': lambda m: m.size_gb},
        'memory_burden': {
            'name': 'CtxGB', 'format': ' 5.1f',
            'func': lambda m: m.memory_burden_gb or 0},
        'context': {
            'name': 'Ctx', 'format': ' >5',
            'func': lambda m: '' if not m.context_size else
            f'{m.context_size // 1024 // 1024}M' if m.context_size >= 10240000 else
            f'{m.context_size // 1024}k' if m.context_size >= 10000 else f'{m.context_size} '},
        'tools': {
            'name': 'T', 'format': ' 1',
            'func': lambda m: 'Y' if m.has_tools else ' '},
        'reasoning': {
            'name': 'R', 'format': '1',
            'func': lambda m: 'Y' if m.has_reasoning else ' '},
        'vision': {
            'name': 'V', 'format': '1',
            'func': lambda m: 'Y' if m.has_vision else ' '},
        'embedding': {
            'name': 'E', 'format': '1',
            'func': lambda m: 'Y' if m.has_embedding else ' '},
        'chunked': {
            'name': 'C', 'format': '1',
            'func': lambda m: 'Y' if m.is_chunked else 'n', 'show': False},
        'date': {
            'name': 'Date', 'format': ' >8',
            'func': lambda m: mdate.strftime('%Y%m%d') if (
                mdate := (m.modified if args.modified else m.created) or m.created) else ''},
        'downloads': {
            'name': 'Dwnlds', 'format': ' >6',
            'func': lambda m: m.downloads if m.downloads < 1e6 else
            f'{int(m.downloads // 1e3)}k' if m.downloads < 1e9 else f'{int(m.downloads // 1e6)}M'},
    }
    models = []
    if 'local' in source:
        if args.gpu_memory_gb is None and args.context_limit is None:
            pass
        models.extend(discover_ollama_models(
            host=args.ollama_host, name_filter=args.regex,
            gpu_memory_gb=args.gpu_memory_gb,
            context_memory=args.context_memory,
            context_limit_gb=args.context_limit,
        ))
        columns['downloads']['show'] = False
    if 'ollama' in source:
        if args.context_limit:
            args.gpu_memory_gb = args.gpu_memory_gb or args.context_limit
            args.context_limit = None
        models.extend(discover_ollama_registry_models(
            name_filter=args.regex,
            gpu_memory_gb=args.gpu_memory_gb,
            model_filter=args.filter,
            limit=args.limit,
            downloads=args.downloads,
            context_memory=args.context_memory,
            context_limit_gb=args.context_limit,
            min_memory=args.min,
        ))
        columns['memory_burden']['show'] = False
    if 'hf' in source or 'huggingface' in source:
        if args.gpu_memory_gb is None and args.context_limit is None:
            parser.error('-m/--gpu-memory-gb is required unless --source local or --source ollama')
        api = huggingface_hub.HfApi()
        models.extend(discover_models(
            api=api, gpu_memory_gb=args.gpu_memory_gb, model_filter=args.filter,
            limit=args.limit, downloads=args.downloads, name_filter=args.regex,
            min_memory=args.min, context_memory=args.context_memory,
            context_limit_gb=args.context_limit,
        ))
    if args.before or args.after:
        before = dateutil.parser.parse(args.before).astimezone(
            datetime.timezone.utc) if args.before else None
        after = dateutil.parser.parse(args.after).astimezone(
            datetime.timezone.utc) if args.after else None
        filtered = []
        for m in models:
            mdate = (m.modified if args.modified else m.created) or m.created
            if mdate and before is not None and mdate > before:
                continue
            if mdate and after is not None and mdate < after:
                continue
            filtered.append(m)
        models = filtered
    models.sort(key=lambda m: (
        -m.size_gb if args.context_limit is None else (-m.memory_burden_gb or 0),
        -m.size_gb, m.repo_id))
    if args.output_format in {'table', 't'}:
        tw, _ = shutil.get_terminal_size()
        rw = tw
        for col in columns.values():
            if col.get('show') is False or col['format'] == 'rw':
                continue
            rw -= int(col['format'].strip().lstrip('<').lstrip('>').split('.')[0]) + (
                1 if col['format'][0] == ' ' else 0)
        for col in columns.values():
            if col.get('show') is False:
                continue
            if col['format'][0] == ' ':
                sys.stdout.write(' ')
            form = col['format'].lstrip()
            if '.' in form:
                form = '>' + form.split('.')[0]
            if form == 'rw':
                form = '<' + str(rw)
            sys.stdout.write(f'{col["name"]:{form}}')
        sys.stdout.write('\n' + ('-' * tw) + '\n')
        for m in models:
            for col in columns.values():
                if col.get('show') is False:
                    continue
                if col['format'][0] == ' ':
                    sys.stdout.write(' ')
                form = col['format'].lstrip()
                if form == 'rw':
                    form = '<' + str(rw)
                    sys.stdout.write(f'{col["func"](m, rw):{form}}')
                else:
                    sys.stdout.write(f'{col["func"](m):{form}}')
            sys.stdout.write('\n')
            sys.stdout.flush()
    else:
        print('# Ollama pull commands:')
        for m in models:
            if m.source == 'ollama-registry':
                # Use the filename (tag_name) which includes quantization info
                print(f'ollama pull {m.repo_name}')
            else:
                print(f'ollama pull hf.co/{m.repo_id}:{m.quantization}')
    print(f'Total: {len(models)} models')


if __name__ == '__main__':
    main()
