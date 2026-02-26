# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "diskcache",
#     "huggingface-hub>=0.20.0",
# ]
# ///

import argparse
import os
import re
import time
from dataclasses import dataclass

import diskcache
import huggingface_hub

cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
cache = diskcache.Cache(cache_path)


@dataclass
class ModelInfo:
    repo_id: str
    filename: str
    size_gb: float
    quantization: str
    model_type: str
    is_chunked: bool
    downloads: int


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

CODING_TAGS = ['code', 'conversational']
VISION_TAGS = ['image-text-to-text']

CODING_PATTERNS = [
    r'code', r'coder', r'codestral', r'starcoder', r'codellama',
    r'wizardcoder', r'phind', r'magicoder', r'codegen', r'replit',
    r'stable-code', r'granite-code', r'qwen.*coder', r'deepseek.*code',
    r'claude', r'teichai',
]

VISION_PATTERNS = [
    r'vision', r'llava', r'bakllava', r'moondream', r'cogvlm', r'minicpm-v',
    r'internvl', r'paligemma', r'qwen.*vl', r'yi-vl', r'bunny',
    r'nanollava', r'obsidian', r'pixtral', r'llama.*vision',
]


def rate_limited_call(func, max_retries=8, base_delay=5):
    for attempt in range(max_retries):
        try:
            return func()
        except huggingface_hub.utils.HfHubHTTPError as e:
            if '429' in str(e) or 'rate limit' in str(e).lower():
                delay = base_delay * (2 ** attempt)
                print(f'  Rate limited. Waiting {delay}s (attempt {attempt + 1}/{max_retries})...')
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
    patterns = CODING_PATTERNS if model_type == 'coding' else VISION_PATTERNS
    return any(re.search(p, repo_lower) for p in patterns)


def has_gguf_files(siblings: list) -> bool:
    if not siblings:
        return False
    for sibling in siblings:
        filename = getattr(sibling, 'rfilename', None)
        if filename and filename.endswith('.gguf'):
            return True
    return False


@cache.memoize(expire=86400)
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


def select_best_quantization(candidates: list[ModelInfo], gpu_memory_gb: float) -> ModelInfo | None:
    fitting = [m for m in candidates if m.size_gb <=
               gpu_memory_gb and m.quantization in QUANT_PRIORITY]
    if not fitting:
        return None
    fitting.sort(key=lambda m: QUANT_PRIORITY.get(m.quantization, 99))
    return fitting[0]


def fetch_models_for_type(model_type: str, limit: int, downloads: int) -> list:
    tags = CODING_TAGS if model_type == 'coding' else VISION_TAGS
    all_models = []

    for tag in tags:
        def fetch(t=tag):
            return list(huggingface_hub.list_models(
                filter=t,
                gated=False,
                expand=['siblings'],
                sort='downloads',
                limit=limit,
            ))
        print(f"  Fetching models with tag '{tag}'...")
        models = rate_limited_call(fetch)
        all_models.extend(models)

    seen = set()
    unique = []
    for m in all_models:
        if m.id not in seen and m.downloads >= downloads:
            seen.add(m.id)
            unique.append(m)
    return unique


def discover_models(
    api: huggingface_hub.HfApi, gpu_memory_gb: float, model_filter: str, limit: int,
    downloads: int,
) -> list[ModelInfo]:
    print(f'Fetching {model_filter} models from HuggingFace...')

    if model_filter == 'all':
        all_models = []
        for mt in ['coding', 'vision']:
            all_models.extend(fetch_models_for_type(mt, limit, downloads))
    else:
        all_models = fetch_models_for_type(model_filter, limit, downloads)

    print(f'Retrieved {len(all_models)} candidate models')

    with_gguf = []
    for model in all_models:
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
            print(f'  Processing {i + 1}/{len(with_gguf)}...')

        model_type = model_filter if model_filter != 'all' else (
            'coding' if matches_type(model.id, 'coding') else
            'vision' if matches_type(model.id, 'vision') else None
        )

        if model_filter == 'all' and model_type is None:
            skipped_name_mismatch += 1
            continue

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
                repo_id=model.id,
                filename=filename,
                size_gb=mem_gb,
                quantization=quant,
                model_type=model_type,
                is_chunked=is_chunked,
                downloads=getattr(model, 'downloads', 0) or 0,
            ))

        best = select_best_quantization(candidates, gpu_memory_gb)
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


def main():
    parser = argparse.ArgumentParser(
        description='Discover Ollama-compatible coding/vision models from HuggingFace',
    )
    parser.add_argument(
        '-m', '--gpu-memory-gb', type=float, required=True,
        help='Available GPU memory in gigabytes',
    )
    parser.add_argument(
        '-f', '--filter', choices=['coding', 'vision', 'all'], default='all',
        help='Filter by model type (default: all)',
    )
    parser.add_argument(
        '-l', '--limit', type=int, default=500,
        help='Maximum models to fetch per category (default: 500)',
    )
    parser.add_argument(
        '-d', '--downloads', type=int, default=0,
        help='Minimum downloads to include (default: 0)',
    )
    parser.add_argument(
        '-o', '--output-format', choices=['table', 'commands'], default='table',
        help='Output format (default: table)',
    )
    args = parser.parse_args()
    api = huggingface_hub.HfApi()
    models = discover_models(
        api=api, gpu_memory_gb=args.gpu_memory_gb, model_filter=args.filter,
        limit=args.limit, downloads=args.downloads,
    )
    models.sort(key=lambda m: (-m.size_gb, m.repo_id))
    if args.output_format == 'table':
        print(f"{'Repository':<50} {'Quant':<7} {'GB':<5} Type {'Dwnlds':<6} Chk")
        print('-' * 80)
        for m in models:
            chunked_str = 'yes' if m.is_chunked else 'no'
            repo_short = m.repo_id[:47] + '...' if len(m.repo_id) > 50 else m.repo_id
            print(
                f'{repo_short:<50} {m.quantization:<7} {m.size_gb:<5.1f} '
                f'{m.model_type[:4]:<4} {m.downloads:6} {chunked_str:<3}')
    else:
        print('# Ollama pull commands:')
        for m in models:
            tag = format_ollama_tag(m.filename)
            print(f'ollama pull hf.co/{m.repo_id}:{tag}')
    print(f'Total: {len(models)} models')


if __name__ == '__main__':
    main()
