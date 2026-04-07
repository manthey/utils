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
import os
import re
import shutil
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


QUANT_PRIORITY = {
    # Full precision
    'F32': 1,
    'F16': 2,
    # "BF16": 3,   # disabled because of my specific GPUs
    # Near-lossless
    'Q8_0': 10,
    'Q8_1': 11,
    # High quality
    'Q6_K': 20,
    'Q6_K_L': 21,
    # Good quality
    'Q5_K_H': 30,
    'Q5_K_L': 31,
    'Q5_K_M': 32,
    'Q5_K_S': 33,
    'Q5_1': 34,
    'Q5_0': 35,
    # Recommended balance
    'Q4_K_L': 40,
    'Q4_K_M': 41,
    'Q4_K_S': 42,
    'IQ4_NL': 43,
    'IQ4_XS': 44,
    'Q4_1': 45,
    'Q4_0': 46,
    # Lower quality
    'Q3_K_XL': 50,
    'Q3_K_L': 51,
    'IQ3_M': 52,
    'Q3_K_M': 53,
    'IQ3_S': 54,
    'Q3_K_S': 55,
    'IQ3_XS': 56,
    'IQ3_XXS': 57,
    # Very low quality
    'Q2_K_L': 60,
    'Q2_K': 61,
    'Q2_K_S': 62,
    'IQ2_M': 63,
    'IQ2_S': 64,
    'IQ2_XS': 65,
    'IQ2_XXS': 66,
    # Desperate
    'IQ1_M': 70,
    'IQ1_S': 71,
    'Q1_0': 72,
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
    for quant in QUANT_PRIORITY:
        if quant in filename_normalized:
            return quant
    return 'UNKNOWN'


def estimate_memory_gb(file_size_bytes: int) -> float:
    return (file_size_bytes / (1024**3)) * 1.15


def matches_type(repo_id: str, model_type: str) -> bool:
    repo_lower = repo_id.lower()
    patterns = MODEL_PATTERNS.get(model_type)['patterns']
    return any(re.search(p, repo_lower) for p in patterns)


def has_gguf_files(siblings: list) -> bool:
    if not siblings:
        return False
    for sibling in siblings:
        filename = getattr(sibling, 'rfilename', None)
        if filename and filename.endswith('.gguf'):
            return True
    return False


@cache.memoize(expire=86400 * 10)
def fetch_gguf_file_sizes(api: huggingface_hub.HfApi, repo_id: str) -> list[tuple[str, int, bool]]:
    def fetch():
        return list(api.list_repo_tree(repo_id, recursive=False))

    try:
        files = rate_limited_call(fetch)
    except Exception:
        return []
    single_files = []
    chunked_groups = {}
    for f in files:
        filename = getattr(f, 'path', None)
        if not filename or not filename.endswith('.gguf'):
            continue
        if 'mmproj' in filename:
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
    candidates: list[ModelInfo], gpu_memory_gb: float, min_memory: float | None,
) -> ModelInfo | None:
    fitting = [m for m in candidates if (
        min_memory or 0) <= m.size_gb <= gpu_memory_gb and m.quantization in QUANT_PRIORITY]
    if not fitting:
        return None
    fitting.sort(key=lambda m: QUANT_PRIORITY.get(m.quantization, 99))
    return fitting[0]


@cache.memoize(expire=3600)
def fetch_models_for_tags(tags: set[str], limit: int, downloads: int) -> list:
    all_models = []
    for tag in tags:
        def fetch(t=tag):
            return list(huggingface_hub.list_models(
                filter=t,
                gated=False,
                expand=['siblings', 'createdAt', 'lastModified'],
                sort='downloads',
                limit=limit,
            ))
        print(f"  Fetching models with tag '{tag}'")
        models = rate_limited_call(fetch)
        all_models.extend(models)
    seen = set()
    unique = []
    for m in all_models:
        if m.id not in seen and m.downloads >= downloads:
            seen.add(m.id)
            unique.append(m)
    return unique


def discover_models(  # noqa
    api: huggingface_hub.HfApi, gpu_memory_gb: float, model_filter: str,
    limit: int, downloads: int, name_filter: str | None = None,
    min_memory: float | None = None,
) -> list[ModelInfo]:
    print(f'Fetching {model_filter} models from HuggingFace')
    tags = set()
    for key in MODEL_PATTERNS:
        if model_filter in {key, 'all'}:
            tags |= MODEL_PATTERNS[key]['tags']
    found_models = fetch_models_for_tags(tags, limit, downloads)
    print(f'Retrieved {len(found_models)} candidate models')
    with_gguf = []
    for model in found_models:
        if name_filter and not re.search(name_filter, model.id, re.IGNORECASE):
            continue
        siblings = getattr(model, 'siblings', None)
        if has_gguf_files(siblings):
            with_gguf.append(model)
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
        candidates = []
        quants = {}
        for filename, size_bytes, is_chunked in gguf_files:
            if is_chunked:
                continue
            quant = extract_quantization(filename)
            if quant == 'UNKNOWN':
                continue
            mem_gb = estimate_memory_gb(size_bytes)
            quants.setdefault(quant, {'file': filename, 'size': mem_gb})
            if mem_gb > quants[quant]['size']:
                quants[quant]['file'] = filename
                quants[quant]['size'] = mem_gb
        for quant in quants:
            filename = quants[quant]['file']
            mem_gb = quants[quant]['size']
            is_chunked = False
            candidates.append(ModelInfo(
                source='huggingface',
                repo_id=model.id,
                filename=filename,
                size_gb=mem_gb,
                quantization=quant,
                model_type=model_type,
                is_chunked=is_chunked,
                downloads=getattr(model, 'downloads', 0) or 0,
                created=getattr(model, 'created_at', None),
                modified=getattr(model, 'last_modified', None),
            ))
        best = select_best_quantization(candidates, gpu_memory_gb, min_memory)
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
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
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
    if any(x in arch for x in ('code', 'coder')) or any(
            re.search(p, name_lower) for p in MODEL_PATTERNS['code']['patterns']):
        return 'code'
    return None


def discover_ollama_models(  # noqa
    host: str, name_filter: str | None, gpu_memory_gb: float | None,
) -> list[ModelInfo]:
    try:
        tags_response = ollama_api_get(host, '/api/tags')
    except (urllib.error.URLError, OSError) as e:
        print(f'Could not reach ollama at {host}: {e}')
        return []
    raw_models = tags_response.get('models', [])
    models = []
    for entry in raw_models:
        name = entry.get('name', '')
        if name_filter and not re.search(name_filter, name, re.IGNORECASE):
            continue
        size_bytes = entry.get('size', 0)
        size_gb = size_bytes / (1024 ** 3)
        if gpu_memory_gb is not None and size_gb > gpu_memory_gb:
            continue
        modified_str = entry.get('modified_at', '')
        modified = None
        if modified_str:
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
            tag_part = ''.join(tag_part.upper().split('.GGUF')).split('.')[-1].split('-')[-1]
            quantization = tag_part.upper() or 'UNKNOWN'
        model_type = infer_model_type_from_details(details, name)
        models.append(ModelInfo(
            source='ollama',
            repo_id=name,
            filename=name,
            size_gb=size_gb,
            quantization=quantization,
            model_type=model_type,
            is_chunked=False,
            downloads=0,
            created=modified,
            modified=modified,
        ))
    return models


def main():
    parser = argparse.ArgumentParser(
        description='Find Ollama-compatible models from HuggingFace',
    )
    parser.add_argument(
        '-m', '--gpu-memory-gb', type=float, default=None,
        help='Available GPU memory in gigabytes (required unless --ollama)',
    )
    parser.add_argument(
        '--min', '--min-gpu-memory-gb', type=float,
        help='Minimum model GPU memory in gigabytes',
    )
    parser.add_argument(
        '-f', '--filter', choices=['code', 'vision', 'embed', 'all'], default='all',
        help='Filter by model type (default: all)',
    )
    parser.add_argument(
        '-l', '--limit', type=int, default=1000,
        help='Maximum models to fetch per category (default: 1000)',
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
        '--ollama', action='store_true', default=False,
        help='Inspect locally available ollama models instead of querying HuggingFace',
    )
    parser.add_argument(
        '--ollama-host', default=os.environ.get('OLLAMA_HOST', '127.0.0.1:11434'),
        help='Ollama server address (default: 127.0.0.1:11434 or OLLAMA_HOST env var)',
    )
    args = parser.parse_args()
    if not args.ollama and args.gpu_memory_gb is None:
        parser.error('-m/--gpu-memory-gb is required unless --ollama is specified')
    if args.ollama:
        models = discover_ollama_models(
            host=args.ollama_host,
            name_filter=args.regex,
            gpu_memory_gb=args.gpu_memory_gb,
        )
    else:
        api = huggingface_hub.HfApi()
        models = discover_models(
            api=api, gpu_memory_gb=args.gpu_memory_gb, model_filter=args.filter,
            limit=args.limit, downloads=args.downloads, name_filter=args.regex,
            min_memory=args.min,
        )
        if args.before or args.after:
            filtered = []
            before = dateutil.parser.parse(args.before).astimezone(
                datetime.timezone.utc) if args.before else None
            after = dateutil.parser.parse(args.after).astimezone(
                datetime.timezone.utc) if args.after else None
            for m in models:
                mdate = (m.modified if args.modified else m.created) or m.created
                if mdate and before is not None and mdate > before:
                    continue
                if mdate and after is not None and mdate < after:
                    continue
                filtered.append(m)
            models = filtered
    models.sort(key=lambda m: (-m.size_gb, m.repo_id))
    tw, _ = shutil.get_terminal_size()
    rw = tw - 7 - 1 - 5 - 1 - 8 - 1 - 6 - 1
    if args.output_format in {'table', 't'}:
        print(f"{'Repository':<{rw}} {'Quant':<7} {'GB':>5} {'Date':>8} {'Dwnlds':>6}")
        print('-' * tw)
        for m in models:
            # chunked_str = 'yes' if m.is_chunked else 'no'
            repo_short = m.repo_id[:rw - 3] + '...' if len(m.repo_id) > rw else m.repo_id
            mdate = (m.modified if args.modified else m.created) or m.created
            mdate_str = mdate.strftime('%Y%m%d') if mdate else ''
            print(
                f'{repo_short:<{rw}} {m.quantization:<7} {m.size_gb:5.1f} '
                f'{mdate_str:<8} {m.downloads:6}')
    else:
        print('# Ollama pull commands:')
        for m in models:
            # tag = format_ollama_tag(m.filename)
            tag = m.quantization
            print(f'ollama pull hf.co/{m.repo_id}:{tag}')
    print(f'Total: {len(models)} models')


if __name__ == '__main__':
    main()
