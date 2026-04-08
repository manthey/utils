# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "python-dateutil",
#     "diskcache",
#     "huggingface-hub>=0.20.0",
# ]
# ///

import argparse
import datetime
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

cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')
cache = diskcache.Cache(cache_path)


@dataclass
class ModelArchParams:
    param_count: int | None
    num_layers: int | None
    num_kv_heads: int | None
    head_dim: int | None
    context_length: int | None


@dataclass
class ModelInfo:
    source: str
    repo_id: str
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


QUANT_PRIORITY_BITS = {
    # Full precision
    'F32': (1, 32.0),
    'F16': (2, 16.0),
    'BF16': (None, 16.0),   # disabled because of my specific GPUs
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
}


def rate_limited_call(func, max_retries=8, base_delay=5):
    for attempt in range(max_retries):
        try:
            return func()
        except huggingface_hub.utils.HfHubHTTPError as e:
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


def estimate_memory_gb(file_size_bytes: int) -> float:
    return (file_size_bytes / (1024**3)) * 1.15


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


def parse_gguf_metadata(data: bytes) -> dict:  # noqa
    GGUF_MAGIC = 0x46554747
    GGUF_VALUE_FORMATS = {
        0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i',
        6: '<f', 7: '<B', 10: '<Q', 11: '<q', 12: '<d',
    }
    offset = 0

    def read_fmt(fmt):
        nonlocal offset
        size = struct.calcsize(fmt)
        if offset + size > len(data):
            raise BufferError
        value = struct.unpack_from(fmt, data, offset)[0]
        offset += size
        return value

    def read_string():
        nonlocal offset
        length = read_fmt('<Q')
        if offset + length > len(data):
            raise BufferError
        s = data[offset:offset + length].decode('utf-8', errors='replace')
        offset += length
        return s

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
            metadata[key] = value
        except (BufferError, ValueError):
            break
    return metadata


@cache.memoize(expire=86400 * 10)
def fetch_gguf_metadata_from_repo(repo_id: str, filename: str) -> dict:
    GGUF_FETCH_BYTES = 256 * 1024
    url = f'https://huggingface.co/{repo_id}/resolve/main/{filename}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'huggingface-hub',
        'Range': f'bytes=0-{GGUF_FETCH_BYTES - 1}',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return parse_gguf_metadata(resp.read(GGUF_FETCH_BYTES))
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
    head_dim = hidden_size // num_attention_heads if hidden_size and num_attention_heads else None
    return ModelArchParams(
        param_count=param_count_hint,
        num_layers=num_layers,
        num_kv_heads=num_kv_heads,
        head_dim=head_dim,
        context_length=context_length,
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
    }
    gathered = {}
    for key, value in model_info.items():
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
    head_dim = embedding_length // head_count if embedding_length and head_count else None
    return ModelArchParams(
        param_count=gathered.get('param_count'),
        num_layers=gathered.get('num_layers'),
        num_kv_heads=gathered.get('num_kv_heads'),
        head_dim=head_dim,
        context_length=gathered.get('context_length'),
    )


def compute_memory_burden_gb(
    arch: ModelArchParams, quantization: str, requested_context: int,
) -> float | None:
    bits_per_weight = QUANT_PRIORITY_BITS.get(quantization, (0, 16.0))[1]
    if bits_per_weight is None or arch is None or arch.param_count is None:
        return None
    weights_bytes = (arch.param_count * bits_per_weight) / 8.0
    effective_context = requested_context
    if arch.context_length is not None:
        effective_context = min(requested_context, arch.context_length)
    kv_bytes = 0.0
    if arch.num_layers is not None and arch.num_kv_heads is not None and arch.head_dim is not None:
        kv_bytes = 4.0 * arch.num_layers * arch.num_kv_heads * arch.head_dim * effective_context
    return (weights_bytes + kv_bytes) / (1024.0 ** 3)


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


def select_best_quantization(
    candidates: list[ModelInfo], gpu_memory_gb: float,
    min_memory: float | None, context_limit_gb: float | None = None,
) -> ModelInfo | None:
    if context_limit_gb is not None:
        fitting = [
            m for m in candidates if m.memory_burden_gb is not None and
            (min_memory or 0) <= m.memory_burden_gb <= context_limit_gb and
            QUANT_PRIORITY_BITS.get(m.quantization, (None, 0))[0] is not None]
    else:
        fitting = [
            m for m in candidates if (min_memory or 0) <= m.size_gb <= gpu_memory_gb and
            QUANT_PRIORITY_BITS.get(m.quantization, (None, 0))[0] is not None]
    if not fitting:
        return None
    fitting.sort(key=lambda m: QUANT_PRIORITY_BITS.get(m.quantization, (99, 0))[0])
    return fitting[0]


@cache.memoize(expire=3600)
def fetch_models_for_tags(tags: set[str], limit: int, downloads: int) -> list:
    all_models = []
    for tag in tags:
        def fetch(t=tag):
            return list(huggingface_hub.list_models(
                filter=t,
                gated=False,
                expand=['siblings', 'createdAt', 'lastModified', 'gguf'],
                sort='downloads',
                apps='ollama',
                limit=limit if limit else None,
            ))
        print(f"  Fetching models with tag '{tag}'")
        all_models.extend(rate_limited_call(fetch))
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
    with_gguf = [
        m for m in found_models
        if (not name_filter or re.search(name_filter, m.id, re.IGNORECASE)) and
        has_gguf_files(getattr(m, 'siblings', None))
    ]
    print(f'Found {len(with_gguf)} models with GGUF files')
    discovered = {}
    skipped_name_mismatch = 0
    skipped_no_fit = 0
    skipped_fetch_failed = 0
    for i, model in enumerate(with_gguf):
        if (i + 1) % 20 == 0:
            print(f'  Processing {i + 1}/{len(with_gguf)}')
        model_type = model_filter if model_filter != 'all' else (
            'code' if matches_type(model.id, 'code') else
            'vision' if matches_type(model.id, 'vision') else None
        )
        if model_filter != 'all' and not matches_type(model.id, model_filter):
            skipped_name_mismatch += 1
            continue
        gguf_files = fetch_gguf_file_sizes(api, model.id)
        if not gguf_files:
            skipped_fetch_failed += 1
            continue
        gguf_meta = getattr(model, 'gguf', {}) or {}
        try:
            param_count_hint = int(gguf_meta['total']) if 'total' in gguf_meta else None
        except (ValueError, TypeError):
            param_count_hint = None
        try:
            hf_context_length = int(gguf_meta['context_length'],
                                    ) if 'context_length' in gguf_meta else None
        except (ValueError, TypeError):
            hf_context_length = None
        arch = arch_params_from_config_json(fetch_config_json(model.id), param_count_hint)
        if arch.context_length is None and hf_context_length is not None:
            arch = ModelArchParams(
                param_count=arch.param_count, num_layers=arch.num_layers,
                num_kv_heads=arch.num_kv_heads, head_dim=arch.head_dim,
                context_length=hf_context_length,
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
                    )
        candidates = []
        quants = {}
        for filename, size_bytes, is_chunked in gguf_files:
            if is_chunked:
                continue
            quant = extract_quantization(filename)
            if quant == 'UNKNOWN':
                continue
            mem_gb = estimate_memory_gb(size_bytes)
            if quant not in quants or mem_gb > quants[quant]['size']:
                quants[quant] = {'file': filename, 'size': mem_gb}
        for quant, info in quants.items():
            candidates.append(ModelInfo(
                source='huggingface',
                repo_id=model.id,
                filename=info['file'],
                size_gb=info['size'],
                quantization=quant,
                model_type=model_type,
                is_chunked=False,
                downloads=getattr(model, 'downloads', 0) or 0,
                created=getattr(model, 'created_at', None),
                modified=getattr(model, 'last_modified', None),
                context_size=arch.context_length,
                arch_params=arch,
                memory_burden_gb=compute_memory_burden_gb(arch, quant, context_memory),
            ))
        best = select_best_quantization(candidates, gpu_memory_gb, min_memory, context_limit_gb)
        if best:
            discovered[model.id] = best
        else:
            skipped_no_fit += 1
    print(f'Found {len(discovered)} matching models')
    print(f'  Skipped (name mismatch): {skipped_name_mismatch}')
    print(f'  Skipped (fetch failed): {skipped_fetch_failed}')
    print(f'  Skipped (all too large): {skipped_no_fit}')
    return list(discovered.values())


def format_ollama_tag(filename: str) -> str:
    base = re.sub(r'\.gguf$', '', filename, flags=re.IGNORECASE)
    base = re.sub(r'-\d{5}-of-\d{5}$', '', base)
    tag = base.split('-')[-1] if '-' in base else base.split('_')[-1]
    return tag.upper()


def ollama_api_get(host: str, path: str) -> object:
    url = f'http://{host}{path}'
    with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
        return json.loads(resp.read().decode())


def ollama_api_post(host: str, path: str, body: dict) -> object:
    url = f'http://{host}{path}'
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def infer_model_type_from_details(details: dict, name: str) -> str | None:
    arch = (details.get('family', '') or details.get('architecture', '') or '').lower()
    name_lower = name.lower()
    if any(x in arch for x in ('clip', 'llava')) or any(
            re.search(p, name_lower) for p in MODEL_PATTERNS['vision']['patterns']):
        return 'vision'
    if 'embed' in arch or any(
            re.search(p, name_lower) for p in MODEL_PATTERNS['embed']['patterns']):
        return 'embed'
    if 'medical' in arch or any(
            re.search(p, name_lower) for p in MODEL_PATTERNS['medical']['patterns']):
        return 'medical'
    if any(x in arch for x in ('code', 'coder')) or any(
            re.search(p, name_lower) for p in MODEL_PATTERNS['code']['patterns']):
        return 'code'
    return None


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
        model_info_block = show_response.get('model_info', {}) or {}
        quantization = (details.get('quantization_level', '') or '').upper()
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
        memory_burden_gb = compute_memory_burden_gb(arch, quantization, context_memory)
        if context_limit_gb is not None and (
                memory_burden_gb is None or memory_burden_gb > context_limit_gb):
            continue
        models.append(ModelInfo(
            source='ollama',
            repo_id=name,
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
        ))
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
        '-f', '--filter', choices=['code', 'vision', 'embed', 'medical', 'all'], default='all',
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
        '-o', '--output-format', choices=['table', 'commands', 'push', 't', 'c', 'p'],
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
        '--after', help='Only show models after this date')
    parser.add_argument(
        '--local', action='store_true', default=False,
        help='Inspect locally available ollama models instead of querying HuggingFace',
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
    columns = {
        'repo': {
            'name': 'Repository', 'format': 'rw',
            'func': lambda m, rw: m.repo_id[:rw - 3] + '...' if len(m.repo_id) > rw else m.repo_id},
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
        'date': {
            'name': 'Date', 'format': ' >8',
            'func': lambda m: mdate.strftime('%Y%m%d') if (
                mdate := (m.modified if args.modified else m.created) or m.created) else ''},
        'downloads': {'name': 'Dwnlds', 'format': ' >6', 'func': lambda m: m.downloads},
        'chunked': {
            'name': 'C', 'format': '1',
            'func': lambda m: 'Y' if m.is_chunked else 'n', 'show': False},
    }
    if not args.local and args.gpu_memory_gb is None and args.context_limit is None:
        parser.error('-m/--gpu-memory-gb is required unless --local is specified')
    if args.local:
        models = discover_ollama_models(
            host=args.ollama_host, name_filter=args.regex,
            gpu_memory_gb=args.gpu_memory_gb,
            context_memory=args.context_memory,
            context_limit_gb=args.context_limit,
        )
        columns['downloads']['show'] = False
    else:
        api = huggingface_hub.HfApi()
        models = discover_models(
            api=api, gpu_memory_gb=args.gpu_memory_gb, model_filter=args.filter,
            limit=args.limit, downloads=args.downloads, name_filter=args.regex,
            min_memory=args.min, context_memory=args.context_memory,
            context_limit_gb=args.context_limit,
        )
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
    models.sort(key=lambda m: (-m.size_gb, m.repo_id))
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
            print(f'ollama pull hf.co/{m.repo_id}:{m.quantization}')
    print(f'Total: {len(models)} models')


if __name__ == '__main__':
    main()
