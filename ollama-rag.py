#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "chromadb>=0.5",
#   "fastapi>=0.111",
#   "gitpython>=3.1",
#   "httpx>=0.27",
#   "llama-index-embeddings-ollama>=0.8",
#   "llama-index-readers-file>=0.5",
#   "mcp[cli]>=1.9",
#   "pathspec",
#   "pypdf>=4.0",
#   "python-docx>=1.1",
#   "tqdm>=4.0",
#   "tree-sitter-languages>=1.10",
#   "tree-sitter>=0.21",
#   "tree_sitter_language_pack",
#   "uvicorn[standard]>=0.29",
# ]
# ///

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
import threading
from collections.abc import Generator
from pathlib import Path

import chromadb
import fastapi
import fastapi.middleware.cors
import git
import httpx
import mcp.server.stdio
import mcp.server.streamable_http_manager
import mcp.types
import pathspec
import starlette.responses
import starlette.routing
import tqdm
import uvicorn
from llama_index.core.node_parser import CodeSplitter
from llama_index.core.schema import Document
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.readers.file import DocxReader, MarkdownReader, PDFReader

os.environ['ANONYMIZED_TELEMETRY'] = 'False'
os.environ['CHROMA_TELEMETRY'] = 'False'

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

EXTENSION_TO_LANGUAGE = {
    '.py': 'python', '.java': 'java', '.ts': 'typescript', '.tsx': 'typescript',
    '.js': 'javascript', '.jsx': 'javascript', '.go': 'go', '.rs': 'rust',
    '.cs': 'c_sharp', '.cpp': 'cpp', '.c': 'c', '.rb': 'ruby',
    '.kt': 'kotlin', '.swift': 'swift',
}

config: argparse.Namespace
mcp_manager: mcp.server.streamable_http_manager.StreamableHTTPSessionManager | None = None


@contextlib.asynccontextmanager
async def lifespan(app):
    if mcp_manager is not None:
        async with mcp_manager.run():
            yield
    else:
        yield

app = fastapi.FastAPI(lifespan=lifespan)
app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=['*'], allow_credentials=True,
    allow_methods=['*'], allow_headers=['*'],
)

_shutdown_event = threading.Event()
_build_locks_mutex = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}


def load_file_index(data_dir: Path) -> dict:
    path = data_dir / 'file_index.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_file_index(data_dir: Path, index: dict) -> None:
    path = data_dir / 'file_index.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index))


def _make_pathspec(exclude: str) -> pathspec.PathSpec:
    patterns = [p.strip() for p in (exclude or '').split(',') if p.strip()]
    return pathspec.PathSpec.from_lines('gitwildmatch', patterns)


def list_paths(
    source_path: str, suffixes: list[str], exclude: str,
) -> Generator[Path, None, None]:
    source = Path(source_path)
    spec = _make_pathspec(exclude)
    for p in sorted(source.rglob('*')):
        if p.is_file() and p.suffix.lower() in suffixes:
            if not spec.match_file(p.relative_to(source).as_posix()):
                yield p


def is_git_source() -> bool:
    return config.source_type == 'git' or (
        config.source_type == 'auto' and
        os.path.exists(os.path.join(config.source_path, '.git'))
    )


def current_file_hashes() -> dict[str, str]:
    if is_git_source():
        extensions = [e.strip() for e in config.git_extensions.split(',')]
        return {
            item.path: item.hexsha
            for item in _iter_repo_blobs(
                config.source_path, extensions, config.source_sub_path, config.exclude)
        }
    suffixes = [e.strip() for e in config.dir_suffixes.split(',')]
    result = {}
    for p in list_paths(config.source_path, suffixes, config.exclude):
        rel = p.relative_to(config.source_path).as_posix()
        logger.debug('%s (%d)', p, os.path.getsize(p))
        result[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def collection_name(embed_model: str, chunk_size: int, chunk_overlap: int) -> str:
    name = 'rag_' + hashlib.sha256(
        f'{embed_model}:{chunk_size}:{chunk_overlap}'.encode(),
    ).hexdigest()[:16]
    logger.info('collection name %s', name)
    return name


def resolve_chunk_size(for_query: bool = False) -> int:
    if config.chunk_size == 0 or for_query:
        context_length = get_model_context_length(config.embed_model)
        logger.info('model %s context length: %d', config.embed_model, context_length)
        chunk_size = max(256, (context_length * 3) // 4)
        if not for_query:
            chunk_size = min(chunk_size, 8192)
        logger.info('auto chunk size: %d', chunk_size)
        return chunk_size
    return config.chunk_size


def strip_hop_by_hop_headers(headers: dict) -> dict:
    drop = {'content-length', 'transfer-encoding', 'content-encoding'}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _iter_repo_blobs(
    source_path: str, extensions: list[str], sub_path: str, exclude: str,
) -> Generator[git.objects.blob.Blob, None, None]:
    sub_path = sub_path.replace('\\', '/')
    prefix = sub_path.strip('/') + '/' if sub_path.strip('/') else ''
    spec = _make_pathspec(exclude)
    repo = git.Repo(source_path)
    for item in sorted(repo.tree().traverse(), key=lambda i: i.path):
        if item.type != 'blob':
            continue
        if prefix and not item.path.startswith(prefix):
            continue
        if not any(item.path.endswith(ext) for ext in extensions):
            continue
        if not spec.match_file(item.path):
            yield item


def _load_file_docs(p: Path) -> tuple[list[Document], bytes]:
    suffix = p.suffix.lower()
    raw_bytes = p.read_bytes()
    if suffix == '.pdf':
        return PDFReader().load_data(p), raw_bytes
    if suffix == '.docx':
        return DocxReader().load_data(p), raw_bytes
    if suffix == '.md':
        return MarkdownReader().load_data(p), raw_bytes
    return [Document(text=raw_bytes.decode(errors='replace'),
                     metadata={'file_path': str(p)})], raw_bytes


def load_single_file_document(source_path: str, rel_path: str) -> Document | None:
    if is_git_source():
        try:
            repo = git.Repo(source_path)
            blob = repo.tree() / rel_path
            text = blob.data_stream.read().decode(errors='replace')
            return Document(text=text, metadata={
                'file_path': rel_path, 'file_sha': blob.hexsha,
                'file_size': blob.size, 'file_mtime': 0,
            })
        except Exception:
            return None
    p = Path(source_path) / rel_path
    if not p.is_file():
        return None
    try:
        docs, raw_bytes = _load_file_docs(p)
        return Document(
            text='\n'.join(d.text for d in docs),
            metadata={
                'file_path': rel_path,
                'file_sha': hashlib.sha256(raw_bytes).hexdigest(),
                'file_size': len(raw_bytes),
                'file_mtime': p.stat().st_mtime,
            },
        )
    except Exception:
        return None


def build_file_manifest_document(active_paths: list[str]) -> Document:
    return Document(
        text='Repository file listing:\n' + '\n'.join(sorted(active_paths)),
        metadata={'file_path': '__manifest__'},
    )


def check_embed_model_available(base_url: str, model_name: str) -> None:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(f'{base_url}/api/show', json={'name': model_name})
            if response.status_code != 200:
                msg = (
                    f'Embedding model {model_name!r} is not available '
                    f'(HTTP {response.status_code}).  Run: ollama pull {model_name}'
                )
                raise RuntimeError(msg)
    except httpx.ConnectError as exc:
        msg = f'Could not connect to Ollama at {base_url}: {exc}'
        raise RuntimeError(msg) from exc


def get_model_context_length(model_name: str) -> int:
    try:
        with httpx.Client(timeout=10) as client:
            data = client.post(
                f'{config.ollama_base_url}/api/show', json={'name': model_name},
            ).json()
        for key, value in data.get('model_info', {}).items():
            if key.endswith('.context_length'):
                return int(value)
        for line in data.get('parameters', '').splitlines():
            if line.strip().lower().startswith('num_ctx'):
                return int(line.split()[-1])
    except Exception:
        pass
    return 2048


def chunk_text(
    text: str, chunk_size: int = 512, overlap: int = 64, filename: str = '',
) -> list[dict]:
    ext = '.' + filename.rsplit('.', 1)[-1] if '.' in filename else ''
    language = EXTENSION_TO_LANGUAGE.get(ext)
    text_bytes = text.encode('utf-8')
    if language:
        raw_chunks = CodeSplitter(
            language=language, chunk_lines=chunk_size // 16,
            chunk_lines_overlap=overlap // 16, max_chars=chunk_size,
        ).split_text(text)
        results = []
        search_start = 0
        for cidx, chunk in enumerate(raw_chunks):
            chunk_bytes = chunk.encode('utf-8')
            idx = text_bytes.find(chunk_bytes, search_start)
            if idx == -1:
                idx = search_start
            elif cidx + 1 < len(raw_chunks):
                nidx = text_bytes.find(raw_chunks[cidx + 1].encode('utf-8'), idx + len(chunk_bytes))
                if nidx > idx:
                    chunk = text_bytes[idx:nidx].decode('utf-8')
            line_start = text_bytes[:idx].count(b'\n') + 1
            results.append({
                'text': chunk, 'byte_offset': idx,
                'line_start': line_start, 'line_end': line_start + chunk.count('\n'),
            })
            search_start = idx + len(chunk_bytes)
        return results
    results = []
    start = 0
    while start < len(text):
        chunk = text[start:min(start + chunk_size, len(text))]
        byte_offset = len(text[:start].encode('utf-8'))
        line_start = text[:start].count('\n') + 1
        results.append({
            'text': chunk, 'byte_offset': byte_offset,
            'line_start': line_start, 'line_end': line_start + chunk.count('\n'),
        })
        start += chunk_size - overlap
    return results


def make_chunk_id(file_sha: str, byte_offset: int, text: str) -> str:
    return hashlib.sha256(f'{file_sha}:{byte_offset}:{text}'.encode()).hexdigest()[:32]


def _check_shutdown() -> None:
    if _shutdown_event.is_set():
        msg = 'Shutdown requested'
        raise RuntimeError(msg)


def embed_document_chunks(
    doc: Document, chunk_size: int, chunk_overlap: int, embed_model: OllamaEmbedding,
) -> tuple[list[str], list[list[float]], list[dict], list[str]]:
    meta = doc.metadata or {}
    file_path = meta.get('file_path', '')
    file_sha = meta.get('file_sha', '')
    texts, embeddings, metadatas, ids = [], [], [], []
    for chunk_info in chunk_text(doc.text, chunk_size, chunk_overlap, file_path):
        _check_shutdown()
        t = chunk_info['text']
        embedding_input = f'### File: {file_path}\n{t}' if file_path else t
        try:
            embedding = embed_model.get_text_embedding(embedding_input)
        except Exception:
            embedding = None
        if not embedding:
            logger.warning('embedding failed for chunk in %s, skipping', file_path)
            continue
        texts.append(t)
        embeddings.append(embedding)
        metadatas.append({
            'file_path': file_path, 'file_sha': file_sha,
            'file_size': meta.get('file_size', 0), 'file_mtime': meta.get('file_mtime', 0),
            'byte_offset': chunk_info['byte_offset'],
            'line_start': chunk_info['line_start'], 'line_end': chunk_info['line_end'],
            'active': True,
        })
        ids.append(make_chunk_id(file_sha, chunk_info['byte_offset'], t))
    return texts, embeddings, metadatas, ids


def _batched_collection_op(
    collection: chromadb.Collection, ids: list[str], op: str, **kwargs,
) -> None:
    if not ids:
        return
    max_batch = collection._client.get_max_batch_size()
    fn = getattr(collection, op)
    for start in range(0, len(ids), max_batch):
        batch_ids = ids[start:start + max_batch]
        if op == 'update':
            fn(ids=batch_ids, **{k: v[:len(batch_ids)] for k, v in kwargs.items()})
        elif op == 'delete':
            fn(ids=batch_ids)
        else:
            fn(ids=batch_ids, **{k: v[start:start + max_batch] for k, v in kwargs.items()})


def add_chunks_to_collection(
    collection: chromadb.Collection,
    texts: list[str], embeddings: list[list[float]],
    metadatas: list[dict], ids: list[str],
) -> None:
    _batched_collection_op(
        collection, ids, 'add',
        documents=texts, embeddings=embeddings, metadatas=metadatas,
    )


def set_chunks_active(
    collection: chromadb.Collection, chunk_ids: list[str], active: bool,
) -> None:
    _batched_collection_op(
        collection, chunk_ids, 'update',
        metadatas=[{'active': active}] * len(chunk_ids),
    )


def delete_chunks(collection: chromadb.Collection, chunk_ids: list[str]) -> None:
    _batched_collection_op(collection, chunk_ids, 'delete')


def update_manifest(
    collection: chromadb.Collection, active_paths: list[str],
    chunk_size: int, chunk_overlap: int, embed_model: OllamaEmbedding,
    coll_entry: dict,
) -> None:
    old_manifest = coll_entry.get('files', {}).get('__manifest__', {})
    delete_chunks(collection, [
        chunk_id
        for version_ids in old_manifest.get('versions', {}).values()
        for chunk_id in version_ids
    ])
    manifest_doc = build_file_manifest_document(active_paths)
    texts, embeddings, metadatas, ids = embed_document_chunks(
        manifest_doc, chunk_size, chunk_overlap, embed_model)
    add_chunks_to_collection(collection, texts, embeddings, metadatas, ids)
    manifest_sha = hashlib.sha256('\n'.join(sorted(active_paths)).encode()).hexdigest()
    coll_entry.setdefault('files', {})['__manifest__'] = {
        'active_sha': manifest_sha,
        'versions': {manifest_sha: ids},
    }


def sync_collection(
    collection: chromadb.Collection, data_dir: Path, cname: str,
    current_hashes: dict[str, str],
) -> None:
    check_embed_model_available(config.ollama_base_url, config.embed_model)
    embed_model = OllamaEmbedding(
        model_name=config.embed_model, base_url=config.ollama_base_url)
    chunk_size = resolve_chunk_size()
    index = load_file_index(data_dir)
    coll_entry = index.setdefault(cname, {'files': {}})
    files_entry = coll_entry['files']
    indexed_paths = {fp for fp in files_entry if fp != '__manifest__'}
    current_paths = set(current_hashes.keys())
    new_paths = current_paths - indexed_paths
    deleted_paths = indexed_paths - current_paths
    modified_paths, reactivate_paths, unchanged_paths = set(), set(), set()
    for fp in current_paths & indexed_paths:
        new_sha = current_hashes[fp]
        active_sha = files_entry[fp].get('active_sha', '')
        if new_sha == active_sha:
            unchanged_paths.add(fp)
        elif new_sha in files_entry[fp].get('versions', {}):
            reactivate_paths.add(fp)
        else:
            modified_paths.add(fp)
    logger.info(
        'sync: %d new, %d modified, %d reactivate, %d deleted, %d unchanged',
        len(new_paths), len(modified_paths), len(reactivate_paths),
        len(deleted_paths), len(unchanged_paths),
    )
    if not (new_paths or deleted_paths or modified_paths or reactivate_paths):
        logger.info('collection is up to date')
        return
    httpx_logger = logging.getLogger('httpx')
    saved_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    for fp in deleted_paths:
        logger.info('deactivating deleted file: %s', fp)
        for version_ids in files_entry[fp].get('versions', {}).values():
            set_chunks_active(collection, version_ids, False)
        files_entry[fp]['active_sha'] = ''
    for fp in reactivate_paths:
        new_sha = current_hashes[fp]
        file_entry = files_entry[fp]
        old_sha = file_entry['active_sha']
        logger.info('reactivating %s: %s -> %s', fp, old_sha, new_sha)
        if old_sha and old_sha in file_entry.get('versions', {}):
            set_chunks_active(collection, file_entry['versions'][old_sha], False)
        set_chunks_active(collection, file_entry['versions'][new_sha], True)
        file_entry['active_sha'] = new_sha
    paths_to_embed = sorted(new_paths | modified_paths)
    if paths_to_embed:
        with tqdm.tqdm(
            total=len(paths_to_embed), desc='Embedding files',
            unit='file', dynamic_ncols=True,
        ) as progress:
            for fp in paths_to_embed:
                _check_shutdown()
                new_sha = current_hashes[fp]
                if fp in modified_paths:
                    old_sha = files_entry[fp].get('active_sha', '')
                    if old_sha and old_sha in files_entry[fp].get('versions', {}):
                        set_chunks_active(
                            collection, files_entry[fp]['versions'][old_sha], False)
                doc = load_single_file_document(config.source_path, fp)
                if doc is None:
                    logger.warning('failed to load %s, skipping', fp)
                    progress.update(1)
                    continue
                texts, embeddings, metadatas, ids = embed_document_chunks(
                    doc, chunk_size, config.chunk_overlap, embed_model)
                add_chunks_to_collection(collection, texts, embeddings, metadatas, ids)
                file_entry = files_entry.setdefault(fp, {'active_sha': '', 'versions': {}})
                file_entry['active_sha'] = new_sha
                file_entry['versions'][new_sha] = ids
                logger.debug('embedded %s (%d chunks)', fp, len(ids))
                progress.update(1)
    httpx_logger.setLevel(saved_level)
    update_manifest(
        collection,
        [fp for fp in current_hashes if fp != '__manifest__'],
        chunk_size, config.chunk_overlap, embed_model, coll_entry,
    )
    save_file_index(data_dir, index)
    logger.info('sync complete')


def build_collection() -> chromadb.Collection:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)
    cname = collection_name(config.embed_model, config.chunk_size, config.chunk_overlap)
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        chroma_client.delete_collection(cname)
    except Exception:
        pass
    collection = chroma_client.create_collection(cname)
    index = load_file_index(data_dir)
    index[cname] = {'files': {}}
    save_file_index(data_dir, index)
    sync_collection(collection, data_dir, cname, current_file_hashes())
    return collection


def get_collection() -> chromadb.Collection | None:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)
    cname = collection_name(config.embed_model, config.chunk_size, config.chunk_overlap)
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    source_unavailable = False
    if is_git_source():
        try:
            git.Repo(config.source_path)
        except Exception:
            source_unavailable = True
    else:
        source = Path(config.source_path)
        source_unavailable = not source.exists() or not any(source.iterdir())

    with _build_locks_mutex:
        if cname not in _build_locks:
            _build_locks[cname] = threading.Lock()
        lock = _build_locks[cname]
    with lock:
        if source_unavailable:
            try:
                collection = chroma_client.get_collection(cname)
                if collection.count() > 0:
                    logger.info(
                        'source path %r is unavailable; reusing existing embeddings',
                        config.source_path,
                    )
                    return collection
            except Exception:
                pass
            return None
        hashes = current_file_hashes()
        try:
            collection = chroma_client.get_collection(cname)
        except Exception:
            return build_collection()
        index = load_file_index(data_dir)
        indexed_hashes = {
            fp: data['active_sha']
            for fp, data in index.get(cname, {}).get('files', {}).items()
            if fp != '__manifest__' and data.get('active_sha')
        }
        if hashes != indexed_hashes:
            sync_collection(collection, data_dir, cname, hashes)
        return collection


def select_top_k(distances: list[float], min_k: int, max_k: int) -> int:
    if len(distances) < 2:
        return len(distances)
    best, worst = distances[0], distances[-1]
    spread = worst - best
    if spread < 1e-6:
        return max_k
    sharpness = (distances[1] - best) / spread
    if sharpness > 0.5:
        return min_k
    return round(min_k + (1.0 - sharpness) * (max_k - min_k))


def format_chunks(documents: list[str], metadatas: list[dict]) -> str:
    chunks = sorted(
        zip(documents, metadatas, strict=True),
        key=lambda x: (x[1].get('file_path', ''), x[1].get('byte_offset', 0)),
    )
    parts = []
    current_file: str | None = None
    current_text: str = ''
    current_byte_start: int = 0
    current_byte_end: int = 0
    current_line_start: int = 1
    current_line_end: int = 1

    def flush() -> None:
        if not current_text:
            return
        if current_line_start > 0 and current_line_start != current_line_end:
            header = f'### File: {current_file} (lines {current_line_start}-{current_line_end})'
        elif current_line_start > 0:
            header = f'### File: {current_file} (line {current_line_start})'
        elif current_byte_start > 0:
            header = f'### File: {current_file} (byte offset {current_byte_start})'
        else:
            header = f'### File: {current_file}'
        parts.append(f'{header}\n{current_text}')

    for text, meta in chunks:
        file_path = meta.get('file_path', '')
        byte_offset = meta.get('byte_offset', 0)
        line_start = meta.get('line_start', 1)
        line_end = meta.get('line_end', line_start)
        text_bytes = text.encode('utf-8')
        chunk_end = byte_offset + len(text_bytes)

        if file_path == current_file and byte_offset <= current_byte_end:
            overlap = current_byte_end - byte_offset
            if overlap < len(text_bytes):
                current_text += text_bytes[overlap:].decode('utf-8', errors='replace')
                current_byte_end = max(current_byte_end, chunk_end)
                current_line_end = max(current_line_end, line_end)
        else:
            flush()
            current_file = file_path
            current_text = text
            current_byte_start = byte_offset
            current_byte_end = chunk_end
            current_line_start = line_start
            current_line_end = line_end

    flush()
    return '\n\n'.join(parts)


def retrieve_context(
    query: str, *, path_filter: str | None = None, top_k_override: int | None = None,
) -> str:
    collection = get_collection()
    if collection is None or not collection.count():
        logger.info('collection is empty or unavailable, no context to retrieve')
        return ''
    embed_model = OllamaEmbedding(
        model_name=config.embed_model, base_url=config.ollama_base_url)
    _check_shutdown()
    max_query_len = resolve_chunk_size(for_query=True)
    if len(query) > max_query_len:
        logger.info('query too long for embedding (%d chars), truncating to %d',
                    len(query), max_query_len)
        half = (max_query_len - 5) // 2
        query = query[:half] + '\n...\n' + query[-half:]
    query_embedding = embed_model.get_text_embedding(query)
    count = collection.count()
    base_k = top_k_override if top_k_override is not None else config.top_k
    min_top_k = min(max(1, base_k // 2), count)
    max_top_k = min(base_k * 2, count)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=max_top_k,
        where={'$and': [{'active': True}, {'file_path': {'$eq': path_filter}}],
               } if path_filter else {'active': True},
        include=['documents', 'distances', 'metadatas'],
    )
    documents = results.get('documents', [[]])[0]
    distances = results.get('distances', [[]])[0]
    metadatas = results.get('metadatas', [[]])[0]
    chosen_k = select_top_k(distances, min_top_k, max_top_k)
    logger.info('Adding context result documents: %d', chosen_k)
    return format_chunks(documents[:chosen_k], metadatas[:chosen_k])


def get_active_file_paths() -> list[str]:
    data_dir = Path(config.data_dir)
    cname = collection_name(config.embed_model, config.chunk_size, config.chunk_overlap)
    index = load_file_index(data_dir)
    files = index.get(cname, {}).get('files', {})
    return sorted(
        fp for fp, entry in files.items()
        if fp != '__manifest__' and entry.get('active_sha')
    )


def mcp_search_codebase(
    query: str, path_filter: str | None = None, top_k: int | None = None,
) -> str:
    return retrieve_context(query, path_filter=path_filter, top_k_override=top_k)


def mcp_list_files(path_prefix: str | None = None) -> list[str]:
    paths = get_active_file_paths()
    if path_prefix:
        paths = [p for p in paths if p.startswith(path_prefix)]
    return paths


def mcp_get_file(path: str) -> str:
    doc = load_single_file_document(config.source_path, path)
    if doc is None:
        return f'Error: file not found: {path}'
    return doc.text


def create_mcp_server() -> mcp.server.Server:
    server = mcp.server.Server('ollama-rag')

    @server.list_tools()
    async def _list_tools() -> list[mcp.types.Tool]:
        return [
            mcp.types.Tool(
                name='search_codebase',
                description='Search the indexed codebase using semantic '
                'similarity. Returns relevant code or document chunks.',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string', 'description': 'The search query'},
                        'path_filter': {
                            'type': 'string',
                            'description': 'Optional: restrict results to this file path'},
                        'top_k': {
                            'type': 'integer',
                            'description': 'Optional: number of results (default from config)'},
                    },
                    'required': ['query'],
                },
            ),
            mcp.types.Tool(
                name='list_files',
                description='List indexed file paths, optionally filtered by a path prefix.',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'path_prefix': {
                            'type': 'string', 'description': 'Optional: filter by path prefix'},
                    },
                },
            ),
            mcp.types.Tool(
                name='get_file',
                description='Retrieve the full contents of a single indexed file by its path.',
                inputSchema={
                    'type': 'object',
                    'properties': {
                        'path': {
                            'type': 'string',
                            'description': 'File path relative to the source root'},
                    },
                    'required': ['path'],
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[mcp.types.TextContent]:
        dispatch = {
            'search_codebase': lambda a: mcp_search_codebase(
                a['query'], a.get('path_filter'), a.get('top_k')),
            'list_files': lambda a: '\n'.join(mcp_list_files(a.get('path_prefix'))),
            'get_file': lambda a: mcp_get_file(a['path']),
        }
        result = await asyncio.to_thread(dispatch[name], arguments)
        return [mcp.types.TextContent(type='text', text=result)]

    return server


def extract_query_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get('role') == 'user':
            content = message.get('content', '')
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return ' '.join(
                    part.get('text', '')
                    for part in content
                    if isinstance(part, dict) and part.get('type') == 'text'
                )
    return ''


def inject_context(body: dict, context: str) -> dict:
    system_content = (
        "Use the following retrieved context to help answer the user's question.\n\n" +
        context
    )
    new_messages = []
    inserted = False
    for message in body.get('messages', []):
        if message.get('role') == 'system' and not inserted:
            new_messages.append(
                {'role': 'system',
                 'content': message.get('content', '') + '\n\n' + system_content})
            inserted = True
        else:
            new_messages.append(message)
    if not inserted:
        new_messages.insert(0, {'role': 'system', 'content': system_content})
    body['messages'] = new_messages
    return body


@app.post('/v1/chat/completions')
async def chat_completions(request: fastapi.Request):
    body = await request.json()
    client_wants_stream = body.get('stream', False)
    query = extract_query_text(body.get('messages', []))
    logger.debug('query: %s, stream: %s', query, client_wants_stream)
    ollama_base_url = config.ollama_base_url
    if query and config.source_path:
        try:
            context = await asyncio.to_thread(retrieve_context, query)
            logger.info('context length: %d', len(context))
            logger.debug('context:\n%s', context)
            body = inject_context(body, context)
        except Exception:
            logger.exception('retrieval failed, proceeding without context')
    body['stream'] = client_wants_stream
    if client_wants_stream:
        async def generate():
            async with httpx.AsyncClient(timeout=None) as client, client.stream(
                'POST', f'{ollama_base_url}/v1/chat/completions',
                json=body, headers={'Content-Type': 'application/json'},
            ) as response:
                logger.debug('stream status: %d', response.status_code)
                async for chunk in response.aiter_bytes():
                    yield chunk
        return fastapi.responses.StreamingResponse(generate(), media_type='text/event-stream')

    def fetch():
        with httpx.Client(timeout=None) as client:
            response = client.post(
                f'{ollama_base_url}/v1/chat/completions',
                json=body, headers={'Content-Type': 'application/json'},
            )
            logger.debug('upstream status: %d', response.status_code)
            logger.debug('upstream body: %s', response.text[:500])
            if len(response.text) > 500:
                logger.debug('upstream body end: ...%s', response.text[500:][-500:])
            return response.content, response.status_code, dict(response.headers)

    content, status_code, headers = await asyncio.to_thread(fetch)
    return fastapi.responses.Response(
        content=content, status_code=status_code,
        headers=strip_hop_by_hop_headers(headers),
    )


@app.api_route(
    '/{path:path}',
    methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'],
)
async def proxy_passthrough(request: fastapi.Request, path: str):
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.request(
            method=request.method,
            url=f'{config.ollama_base_url}/{path}',
            headers={k: v for k, v in request.headers.items() if k.lower() != 'host'},
            content=await request.body(),
            params=request.query_params,
        )
    return fastapi.responses.Response(
        content=response.content,
        status_code=response.status_code,
        headers=strip_hop_by_hop_headers(dict(response.headers)),
    )


async def mcp_redirect(request: fastapi.Request):
    return fastapi.responses.RedirectResponse(
        url=str(request.url).rstrip('/') + '/',
        status_code=307,
    )


async def mcp_endpoint(scope, receive, send):
    if mcp_manager is None:
        response = starlette.responses.JSONResponse({'error': 'MCP not enabled'}, status_code=404)
        await response(scope, receive, send)
        return
    await mcp_manager.handle_request(scope, receive, send)


def cmd_serve(args):
    global config
    global mcp_manager

    config = args
    if args.mcp_http_path:
        mcp_manager = mcp.server.streamable_http_manager.StreamableHTTPSessionManager(
            app=create_mcp_server(),
            event_store=None,
            stateless=True,
        )
        mcp_path = args.mcp_http_path.rstrip('/')
        app.router.routes.insert(0, starlette.routing.Route(
            mcp_path, endpoint=mcp_redirect, methods=['GET', 'POST', 'DELETE']))
        app.router.routes.insert(0, starlette.routing.Mount(mcp_path, app=mcp_endpoint))
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
    server.install_signal_handlers = lambda: None

    def _handle_signal(signum, frame):
        _shutdown_event.set()
        server.should_exit = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=0.5)
    logging.getLogger('uvicorn.error').setLevel(logging.INFO)
    logging.getLogger('uvicorn.access').setLevel(logging.INFO)


def cmd_clear(args):
    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        return
    if not args.purge_inactive:
        shutil.rmtree(data_dir)
        print('Cleared.')
        return
    chroma_dir = data_dir / 'chroma'
    if not chroma_dir.exists():
        return
    index = load_file_index(data_dir)
    if not index:
        return
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    total_deleted = 0
    for cname, coll_entry in index.items():
        try:
            collection = chroma_client.get_collection(cname)
        except Exception:
            continue
        files_entry = coll_entry.get('files', {})
        ids_to_delete = []
        files_to_remove = []
        for fp, file_entry in files_entry.items():
            if fp == '__manifest__':
                continue
            active_sha = file_entry.get('active_sha', '')
            versions = file_entry.get('versions', {})
            for sha in [sha for sha in versions if sha != active_sha]:
                ids_to_delete.extend(versions.pop(sha))
            if not active_sha:
                files_to_remove.append(fp)
        delete_chunks(collection, ids_to_delete)
        for fp in files_to_remove:
            del files_entry[fp]
        total_deleted += len(ids_to_delete)
        print(
            f'Collection {cname}: deleted {len(ids_to_delete)} inactive chunks, '
            f'removed {len(files_to_remove)} deleted-file entries.',
        )
    save_file_index(data_dir, index)
    print(f'Total chunks deleted: {total_deleted}.')


def cmd_mcp(args):
    global config
    config = args
    get_collection()
    server = create_mcp_server()

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(_run())


def build_arg_parser() -> argparse.ArgumentParser:
    default_data_dir = str(Path.home() / '.local' / 'share' / 'rag_proxy')
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        '--data-dir',
        default=os.environ.get('RAG_DATA_DIR', default_data_dir),
        help=f'Cache directory; default is {default_data_dir}',
    )
    shared.add_argument('--verbose', '-v', action='count', default=0,
                        help='Increase verbosity')
    parser = argparse.ArgumentParser(description='Local RAG proxy for Ollama')
    subparsers = parser.add_subparsers(dest='command', required=True)
    serve = subparsers.add_parser('serve', parents=[shared])
    clear = subparsers.add_parser('clear', parents=[shared])
    mcp = subparsers.add_parser('mcp', parents=[shared])
    serve.add_argument(
        '--host', type=str, default='0.0.0.0',
        help='Proxy host; default is 0.0.0.0.  Using localhost is more secure.',
    )
    serve.add_argument(
        '--port', type=int,
        default=int(os.environ.get('RAG_PROXY_PORT', '11435')),
        help='Proxy port; default is 11435',
    )
    serve.add_argument(
        '--mcp-http-path',
        default=os.environ.get('RAG_MCP_HTTP_PATH', '/mcp'),
        help='Path for the MCP streamable-HTTP endpoint; default is /mcp. An '
        'empty string disabled the endpoint',
    )
    clear.add_argument(
        '--purge-inactive', action='store_true',
        help='Remove inactive chunks and deleted-file entries without destroying active data',
    )
    for sub in (serve, mcp):
        sub.add_argument(
            '--ollama-base-url',
            default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
            help='Ollama URL; default is http://localhost:11434',
        )
        sub.add_argument(
            '--embed-model', '-e',
            default=os.environ.get('RAG_EMBED_MODEL', 'nomic-embed-text'),
            help=(
                'Embedding model; default is nomic-embed-text.  Others are '
                'mxbai-embed-large (good for code), all-minilm (small), '
                'snowflake-arctic-embed, bge-m3 (multilingual), bge-large '
                '(English). Do ollama pull on the model before using it.'
            ),
        )
        sub.add_argument(
            '--source-type', choices=['directory', 'git', 'auto'],
            default=os.environ.get('RAG_SOURCE_TYPE', 'auto'),
            help='Auto checks for a .git folder.  If git, only tracked files are used.',
        )
        sub.add_argument(
            '--source-path', '-s',
            default=os.environ.get('RAG_SOURCE_PATH', ''),
            help='The root of the git repo or documents to embed',
        )
        sub.add_argument(
            '--source-sub-path',
            default=os.environ.get('RAG_SOURCE_SUB_PATH', ''),
            help='For git repos, only embed files within this subpath',
        )
        sub.add_argument(
            '--exclude', '-x',
            default=os.environ.get('RAG_EXCLUDE', ''),
            help='A comma-separated list of paths and file signatures to exclude',
        )
        sub.add_argument(
            '--git-extensions',
            default=os.environ.get('RAG_GIT_EXTENSIONS', '.py,.js,.java,.ts,.md,.rst'),
            help=(
                'Only process specific file types in a git repo; '
                "default is '.py,.js,.java,.ts,.md,.rst'."
            ),
        )
        sub.add_argument(
            '--dir-suffixes',
            default=os.environ.get('RAG_DIR_SUFFIXES', '.txt,.md,.pdf,.docx,.rst'),
            help=(
                'Only process specific file types in a non-git source folder; '
                "default is '.txt,.md,.pdf,.docx,.rst'"
            ),
        )
        sub.add_argument(
            '--top-k', type=int,
            default=int(os.environ.get('RAG_TOP_K', '5')),
            help=(
                'How much context to fetch; default is 5.  Use larger values for general queries, '
                'smaller values for targeted queries.  A value between half and twice this will be '
                'chosen based on recall similarity.'
            ),
        )
        sub.add_argument(
            '--chunk-size', type=int,
            default=int(os.environ.get('RAG_CHUNK_SIZE', '0')),
            help='Embedding chunk size; default or 0 is determined by model.  Too big will fail',
        )
        sub.add_argument(
            '--chunk-overlap', type=int,
            default=int(os.environ.get('RAG_CHUNK_OVERLAP', '64')),
            help='Embedding chunk overlap; default is 64',
        )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    if args.command == 'serve':
        cmd_serve(args)
    elif args.command == 'clear':
        cmd_clear(args)
    elif args.command == 'mcp':
        cmd_mcp(args)


if __name__ == '__main__':
    main()
