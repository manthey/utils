#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "cachetools",
#   "chromadb>=0.5",
#   "docx2txt",
#   "fastapi>=0.111",
#   "filelock",
#   "gitpython>=3.1",
#   "httpx>=0.27",
#   "llama-index-embeddings-ollama>=0.8",
#   "llama-index-readers-file>=0.5",
#   "mcp[cli]>=1.9",
#   "msgpack>=1.0",
#   "pathspec",
#   "pypdf>=4.0",
#   "python-docx>=1.1",
#   "rank-bm25>=0.2",
#   "tqdm>=4.0",
#   "tree-sitter-languages>=1.10",
#   "tree-sitter>=0.21",
#   "tree_sitter_language_pack",
#   "uvicorn[standard]>=0.29",
# ]
# ///

import argparse
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Generator
from pathlib import Path

import cachetools
import chromadb
import fastapi
import fastapi.middleware.cors
import filelock
import git
import httpx
import mcp.server.stdio
import mcp.server.streamable_http_manager
import mcp.types
import msgpack
import pathspec
import rank_bm25
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

PREAMBLES = {
    'code':
        "The following code fragments were retrieved from the project's "
        "source files based on semantic similarity to the user's query. Each "
        'fragment is preceded by a header indicating the file path and line '
        'range. The fragments may not represent the complete contents of each '
        'file, and multiple fragments from the same file may not be '
        'contiguous. When referencing code in your response, cite the file '
        'path and line numbers. Base your response on the information '
        'provided in these fragments.',
    'directory':
        'The following document excerpts were retrieved based on semantic '
        "similarity to the user's query. Each excerpt is preceded by a header "
        'indicating the source file and position within that file. The '
        'excerpts may not represent the complete contents of each document, '
        'and multiple excerpts from the same document may not be contiguous. '
        'Base your response on the information provided in these excerpts.',
    'mixed':
        'The following excerpts from source code and documents were retrieved '
        "based on semantic similarity to the user's query. Each excerpt is "
        'preceded by a header indicating the file path and location. The '
        'excerpts may not represent the complete contents of each file, and '
        'multiple excerpts from the same file may not be contiguous. When '
        'referencing code, cite the file path and line numbers. Base your '
        'response on the information provided in these excerpts.',
}


class SourceConfig:
    """Configuration for a single source directory or git repo."""

    def __init__(
        self, source_path: str, source_type: str, source_sub_path: str,
        exclude: str, git_extensions: str, dir_suffixes: str,
    ):
        self.source_path = os.path.abspath(source_path)
        self.source_type = source_type
        self.source_sub_path = source_sub_path
        self.exclude = exclude
        self.git_extensions = git_extensions
        self.dir_suffixes = dir_suffixes


class BM25Index:
    def __init__(self, bm25: rank_bm25.BM25Okapi, documents: list[str], metadatas: list[dict]):
        self.bm25 = bm25
        self.documents = documents
        self.metadatas = metadatas


config: argparse.Namespace
source_configs: list[SourceConfig] = []
mcp_manager: mcp.server.streamable_http_manager.StreamableHTTPSessionManager | None = None
bm25_cache: dict[str, BM25Index] = {}


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

shutdown_event = threading.Event()
build_locks_mutex = threading.Lock()
build_locks: dict[str, threading.Lock] = {}


def file_index_lock(data_dir: Path) -> filelock.FileLock:
    return filelock.FileLock(data_dir / 'file_index.lock', timeout=60)


def file_atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix('.tmp.' + str(os.getpid()))
    try:
        tmp.write_text(data)
        try:
            tmp.replace(path)
        except OSError:
            shutil.copy2(str(tmp), str(path))
            tmp.unlink(missing_ok=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_file_index(data_dir: Path, held_lock: filelock.FileLock | None = None) -> dict:
    path = data_dir / 'file_index.json'
    if held_lock is not None:
        return json.loads(path.read_text()) if path.exists() else {}
    with file_index_lock(data_dir):
        return json.loads(path.read_text()) if path.exists() else {}


def save_file_index(
    data_dir: Path, index: dict, held_lock: filelock.FileLock | None = None,
) -> None:
    path = data_dir / 'file_index.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if held_lock is not None:
        file_atomic_write(path, json.dumps(index))
    else:
        with file_index_lock(data_dir):
            file_atomic_write(path, json.dumps(index))


def save_file_index_entry(data_dir: Path, cname: str, coll_entry: dict) -> None:
    lock = file_index_lock(data_dir)
    with lock:
        index = load_file_index(data_dir, held_lock=lock)
        index[cname] = coll_entry
        save_file_index(data_dir, index, held_lock=lock)


def make_pathspec(exclude: str) -> pathspec.PathSpec:
    patterns = [p.strip() for p in (exclude or '').split(',') if p.strip()]
    return pathspec.PathSpec.from_lines('gitwildmatch', patterns)


def list_paths(
    source_path: str, suffixes: list[str], exclude: str, sub_path: str = '',
) -> Generator[Path, None, None]:
    base = Path(source_path)
    source = base / sub_path if sub_path else base
    spec = make_pathspec(exclude)
    for p in sorted(source.rglob('*')):
        try:
            if p.is_file() and p.suffix.lower() in suffixes:
                if not spec.match_file(p.relative_to(base).as_posix()):
                    yield p
        except Exception:
            pass


def is_git_source(src: SourceConfig) -> bool:
    return src.source_type == 'git' or (
        src.source_type == 'auto' and
        os.path.exists(os.path.join(src.source_path, '.git'))
    )


def current_file_hashes_for_source(src: SourceConfig) -> dict[str, str]:
    if is_git_source(src):
        extensions = [e.strip() for e in src.git_extensions.split(',')]
        return {
            os.path.join(src.source_path, item.path): item.hexsha
            for item in iter_repo_blobs(
                src.source_path, extensions, src.source_sub_path, src.exclude)
        }
    suffixes = [e.strip() for e in src.dir_suffixes.split(',')]
    result = {}

    @cachetools.cached(cache=cachetools.TTLCache(ttl=3600, maxsize=10000))
    def get_hash(p, p_size):
        with open(p, 'rb') as fptr:
            return hashlib.file_digest(fptr, 'sha256').hexdigest()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for p in list_paths(src.source_path, suffixes, src.exclude, src.source_sub_path):
            p_size = os.path.getsize(p)
            logger.debug('%s (%d)', p, p_size)
            futures[executor.submit(get_hash, p, p_size)] = p
        for future, p in futures.items():
            rel = p.relative_to(src.source_path).as_posix()
            result[os.path.join(src.source_path, rel)] = future.result()
    return result


def collection_name_for_source(
    embed_model: str, chunk_size: int, chunk_overlap: int, source_path: str,
) -> str:
    name = 'rag_' + hashlib.sha256(
        f'{embed_model}:{chunk_size}:{chunk_overlap}:{source_path}'.encode(),
    ).hexdigest()[:16]
    logger.info('collection name %s (source %s)', name, source_path)
    return name


def resolve_chunk_size(for_query: bool = False) -> int:
    if config.chunk_size == 0 or for_query:
        context_length = get_model_context_length(config.embed_model)
        logger.info('model %s context length: %d', config.embed_model, context_length)
        chunk_size = max(256, (context_length * 3) // 4)
        if not for_query:
            chunk_size = min(chunk_size, 4096)
        logger.debug('auto chunk size: %d', chunk_size)
        return chunk_size
    return config.chunk_size


def strip_hop_by_hop_headers(headers: dict) -> dict:
    drop = {'content-length', 'transfer-encoding', 'content-encoding'}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def iter_repo_blobs(
    source_path: str, extensions: list[str], sub_path: str, exclude: str,
) -> Generator[git.objects.blob.Blob, None, None]:
    sub_path = sub_path.replace('\\', '/')
    prefix = sub_path.strip('/') + '/' if sub_path.strip('/') else ''
    spec = make_pathspec(exclude)
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


def load_file_docs(p: Path) -> list[Document]:
    suffix = p.suffix.lower()
    readers = {'.pdf': PDFReader, '.docx': DocxReader, '.md': MarkdownReader}
    limit = 256 * 1024 * 1024
    if suffix in readers:
        try:
            return readers[suffix]().load_data(p)
        except Exception as exc:
            logger.warning('reading failed for %s: %s', p, str(exc)[:40])
            raise
    if suffix != '.txt':
        proc = None
        try:
            proc = subprocess.Popen([
                'pandoc', str(p), '-t', 'plain', '--wrap=none'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=0)
            chunks = []
            size = 0
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    proc.kill()
                    return None
                chunks.append(chunk)
            proc.wait(timeout=30)
            return [Document(text=(''.join(chunks)),
                             metadata={'file_path': str(p)})]
        except Exception:
            if proc is not None:
                proc.kill()
    if p.stat().st_size > limit:
        return None
    raw_bytes = p.read_bytes()
    return [Document(text=raw_bytes.decode('utf-8', errors='ignore'),
                     metadata={'file_path': str(p)})]


def find_source_for_path(abs_path: str) -> tuple[SourceConfig, str] | None:
    """
    Given an absolute path, find the owning SourceConfig and the relative
    path within that source root.
    """
    for src in source_configs:
        prefix = src.source_path.replace('\\', '/').rstrip('/') + '/'
        if abs_path.replace('\\', '/').startswith(prefix):
            return src, abs_path[len(prefix):]
    return None


def load_single_file_document(abs_path: str) -> Document | None:
    found = find_source_for_path(abs_path)
    if found is None:
        return None
    src, rel_path = found
    if is_git_source(src):
        try:
            repo = git.Repo(src.source_path)
            blob = repo.tree() / rel_path
            text = blob.data_stream.read().decode('utf-8', errors='replace')
            return Document(text=text, metadata={
                'file_path': abs_path, 'file_sha': blob.hexsha,
                'file_size': blob.size, 'file_mtime': 0, 'rel_path': rel_path,
            })
        except Exception:
            return None
    p = Path(src.source_path) / rel_path
    if not p.is_file():
        return None
    try:
        docs = load_file_docs(p)
        return Document(
            text='\n'.join(d.text for d in docs),
            metadata={
                'file_path': abs_path,
                'file_sha': hashlib.file_digest(open(p, 'rb'), 'sha256').hexdigest(),
                'file_size': p.stat().st_size,
                'file_mtime': p.stat().st_mtime,
                'rel_path': rel_path,
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
    text_bytes = text.encode('utf-8', errors='ignore')
    if language:
        raw_chunks = CodeSplitter(
            language=language, chunk_lines=chunk_size // 16,
            chunk_lines_overlap=overlap // 16, max_chars=chunk_size,
        ).split_text(text)
        results = []
        search_start = 0
        for cidx, chunk in enumerate(raw_chunks):
            chunk_bytes = chunk.encode('utf-8', errors='ignore')
            idx = text_bytes.find(chunk_bytes, search_start)
            if idx == -1:
                idx = search_start
            elif cidx + 1 < len(raw_chunks):
                nidx = text_bytes.find(
                    raw_chunks[cidx + 1].encode('utf-8', errors='ignore'),
                    idx + len(chunk_bytes))
                if nidx > idx:
                    chunk = text_bytes[idx:nidx].decode('utf-8', errors='ignore')
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
        byte_offset = len(text[:start].encode('utf-8', errors='ignore'))
        line_start = text[:start].count('\n') + 1
        results.append({
            'text': chunk, 'byte_offset': byte_offset,
            'line_start': line_start, 'line_end': line_start + chunk.count('\n'),
        })
        start += chunk_size - overlap
    return results


def make_chunk_id(file_sha: str, byte_offset: int, text: str) -> str:
    return hashlib.sha256(f'{file_sha}:{byte_offset}:{text}'.encode()).hexdigest()[:32]


def check_shutdown() -> None:
    if shutdown_event.is_set():
        msg = 'Shutdown requested'
        raise RuntimeError(msg)


def embed_document_chunks(
    doc: Document, chunk_size: int, chunk_overlap: int, embed_model: OllamaEmbedding,
) -> tuple[list[str], list[list[float]], list[dict], list[str]]:
    meta = doc.metadata or {}
    file_path = meta.get('file_path', '')
    file_sha = meta.get('file_sha', '')
    rel_path = meta.get('rel_path', file_path)
    texts, embeddings, metadatas, ids = [], [], [], []
    for chunk_info in chunk_text(doc.text, chunk_size, chunk_overlap, file_path):
        check_shutdown()
        t = chunk_info['text']
        t = t.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
        embedding_input = f'### File: {rel_path}\n{t}' if file_path else t
        try:
            embedding = embed_model.get_text_embedding(embedding_input)
        except Exception:
            try:
                embedding = embed_model.get_text_embedding(embedding_input.encode(
                    'utf-8', errors='ignore').decode('utf-8', errors='ignore'))
            except Exception:
                embedding = None
        if not embedding:
            logger.warning('embedding failed for chunk in %s, skipping', file_path)
            continue
        texts.append(t)
        embeddings.append(embedding)
        metadatas.append({
            'file_path': file_path, 'file_sha': file_sha, 'rel_path': rel_path,
            'file_size': meta.get('file_size', 0), 'file_mtime': meta.get('file_mtime', 0),
            'byte_offset': chunk_info['byte_offset'],
            'line_start': chunk_info['line_start'], 'line_end': chunk_info['line_end'],
            'active': True,
        })
        ids.append(make_chunk_id(file_sha, chunk_info['byte_offset'], t))
    return texts, embeddings, metadatas, ids


def batched_collection_op(
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
    batched_collection_op(
        collection, ids, 'add',
        documents=texts, embeddings=embeddings, metadatas=metadatas,
    )


def set_chunks_active(
    collection: chromadb.Collection, chunk_ids: list[str], active: bool,
) -> None:
    batched_collection_op(
        collection, chunk_ids, 'update',
        metadatas=[{'active': active}] * len(chunk_ids),
    )


def delete_chunks(collection: chromadb.Collection, chunk_ids: list[str]) -> None:
    batched_collection_op(collection, list(set(chunk_ids)), 'delete')


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
    manifest_sha = hashlib.sha256('\n'.join(sorted(active_paths)).encode(
        'utf-8', errors='ignore')).hexdigest()
    coll_entry.setdefault('files', {})['__manifest__'] = {
        'active_sha': manifest_sha,
        'versions': {manifest_sha: ids},
    }


def bm25_path_for_collection(data_dir: Path, cname: str) -> Path:
    return data_dir / 'bm25' / f'{cname}.msgpack'


def save_bm25_index(data_dir: Path, cname: str, bm25_idx: BM25Index) -> None:
    path = bm25_path_for_collection(data_dir, cname)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'documents': bm25_idx.documents,
        'metadatas': bm25_idx.metadatas,
    }
    path.write_bytes(msgpack.packb(payload, use_bin_type=True))
    logger.info('saved BM25 index for %s (%d documents)', cname, len(bm25_idx.documents))


def load_bm25_index(data_dir: Path, cname: str) -> BM25Index | None:
    path = bm25_path_for_collection(data_dir, cname)
    if not path.exists():
        return None
    try:
        payload = msgpack.unpackb(path.read_bytes(), raw=False)
        documents = payload['documents']
        metadatas = payload['metadatas']
        tokenized = [doc.lower().split() for doc in documents]
        if not tokenized:
            tokenized = [['']]
        bm25 = rank_bm25.BM25Okapi(tokenized)
        return BM25Index(bm25, documents, metadatas)
    except Exception:
        logger.warning('failed to load BM25 index for %s', cname)
        return None


def build_bm25_from_collection(collection: chromadb.Collection) -> BM25Index:
    all_documents: list[str] = []
    all_metadatas: list[dict] = []
    count = collection.count()
    if count > 0:
        max_batch = collection._client.get_max_batch_size()
        for offset in range(0, count, max_batch):
            batch = collection.get(
                offset=offset,
                limit=min(max_batch, count - offset),
                where={'active': True},
                include=['documents', 'metadatas'],
            )
            all_documents.extend(batch.get('documents', []))
            all_metadatas.extend(batch.get('metadatas', []))
    tokenized = [doc.lower().split() for doc in all_documents]
    if not tokenized:
        tokenized = [['']]
    bm25 = rank_bm25.BM25Okapi(tokenized)
    return BM25Index(bm25, all_documents, all_metadatas)


def get_bm25_for_collection(
    collection: chromadb.Collection, data_dir: Path, cname: str,
    force_rebuild: bool = False,
) -> BM25Index:
    if not force_rebuild and cname in bm25_cache:
        return bm25_cache[cname]
    if not force_rebuild:
        loaded = load_bm25_index(data_dir, cname)
        if loaded is not None:
            bm25_cache[cname] = loaded
            return loaded
    bm25_idx = build_bm25_from_collection(collection)
    save_bm25_index(data_dir, cname, bm25_idx)
    bm25_cache[cname] = bm25_idx
    return bm25_idx


def rebuild_bm25_for_source(
    collection: chromadb.Collection, data_dir: Path, cname: str,
) -> None:
    bm25_idx = build_bm25_from_collection(collection)
    save_bm25_index(data_dir, cname, bm25_idx)
    bm25_cache[cname] = bm25_idx


def sync_collection(  # noqa
    collection: chromadb.Collection, data_dir: Path, cname: str,
    current_hashes: dict[str, str],
) -> None:
    check_embed_model_available(config.ollama_base_url, config.embed_model)
    embed_model = OllamaEmbedding(
        model_name=config.embed_model, base_url=config.ollama_base_url)
    chunk_size = resolve_chunk_size()
    lock = file_index_lock(data_dir)
    with lock:
        index = load_file_index(data_dir, held_lock=lock)
        coll_entry = index.setdefault(cname, {'files': {}})
        save_file_index(data_dir, index, held_lock=lock)
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
        logger.debug('deactivating deleted file: %s', fp)
        for version_ids in files_entry[fp].get('versions', {}).values():
            set_chunks_active(collection, version_ids, False)
        files_entry[fp]['active_sha'] = ''
    for fp in reactivate_paths:
        new_sha = current_hashes[fp]
        file_entry = files_entry[fp]
        old_sha = file_entry['active_sha']
        logger.debug('reactivating %s: %s -> %s', fp, old_sha, new_sha)
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
                check_shutdown()
                new_sha = current_hashes[fp]
                if fp in modified_paths:
                    old_sha = files_entry[fp].get('active_sha', '')
                    if old_sha and old_sha in files_entry[fp].get('versions', {}):
                        set_chunks_active(
                            collection, files_entry[fp]['versions'][old_sha], False)
                doc = load_single_file_document(fp)
                if doc is None:
                    logger.warning('failed to load %s, skipping', fp)
                    progress.update(1)
                    continue
                texts, embeddings, metadatas, ids = embed_document_chunks(
                    doc, chunk_size, config.chunk_overlap, embed_model)
                try:
                    add_chunks_to_collection(collection, texts, embeddings, metadatas, ids)
                except Exception:
                    logger.info('Failed to add chunks for %s (%r)', fp)
                    continue
                file_entry = files_entry.setdefault(fp, {'active_sha': '', 'versions': {}})
                file_entry['active_sha'] = new_sha
                file_entry['versions'][new_sha] = ids
                save_file_index_entry(data_dir, cname, coll_entry)
                logger.debug('embedded %s (%d chunks)', fp, len(ids))
                progress.update(1)
    httpx_logger.setLevel(saved_level)
    update_manifest(
        collection,
        [fp for fp in current_hashes if fp != '__manifest__'],
        chunk_size, config.chunk_overlap, embed_model, coll_entry,
    )
    save_file_index(data_dir, index, coll_entry)
    rebuild_bm25_for_source(collection, data_dir, cname)
    logger.info('sync complete')


def build_collection_for_source(src: SourceConfig) -> chromadb.Collection:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)
    cname = collection_name_for_source(
        config.embed_model, config.chunk_size, config.chunk_overlap, src.source_path)
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        chroma_client.delete_collection(cname)
    except Exception:
        pass
    collection = chroma_client.create_collection(cname)
    save_file_index_entry(data_dir, cname, {'files': {}})
    sync_collection(collection, data_dir, cname, current_file_hashes_for_source(src))


def source_unavailable(src: SourceConfig) -> bool:
    if is_git_source(src):
        try:
            git.Repo(src.source_path)
            return False
        except Exception:
            return True
    source = Path(src.source_path)
    return not source.exists() or not any(source.iterdir())


def get_collection_for_source(src: SourceConfig) -> chromadb.Collection | None:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)
    cname = collection_name_for_source(
        config.embed_model, config.chunk_size, config.chunk_overlap, src.source_path)
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    unavailable = source_unavailable(src)
    with build_locks_mutex:
        if cname not in build_locks:
            build_locks[cname] = threading.Lock()
        lock = build_locks[cname]
    with lock:
        if unavailable:
            try:
                collection = chroma_client.get_collection(cname)
                if collection.count() > 0:
                    logger.info(
                        'source %s unavailable; reusing existing embeddings '
                        '(%d chunks)', src.source_path, collection.count(),
                    )
                    return collection
            except Exception:
                pass
            return None
        hashes = current_file_hashes_for_source(src)
        try:
            collection = chroma_client.get_collection(cname)
        except Exception:
            return build_collection_for_source(src)
        index = load_file_index(data_dir)
        indexed_hashes = {
            fp: data['active_sha']
            for fp, data in index.get(cname, {}).get('files', {}).items()
            if fp != '__manifest__' and data.get('active_sha')
        }
        if hashes != indexed_hashes:
            sync_collection(collection, data_dir, cname, hashes)
        return collection
    bm25_path = bm25_path_for_collection(data_dir, cname)
    if not bm25_path.exists() and collection.count() > 0:
        logger.info('BM25 index missing for %s, building from existing collection', cname)
        rebuild_bm25_for_source(collection, data_dir, cname)
    return collection


def get_all_collections() -> list[chromadb.Collection]:
    """Return one collection per configured source, syncing each as needed."""
    collections = []
    for src in source_configs:
        coll = get_collection_for_source(src)
        if coll is not None and coll.count() > 0:
            collections.append(coll)
    return collections


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
    rel_file: str | None = None
    current_text: str = ''
    current_byte_start: int = 0
    current_byte_end: int = 0
    current_line_start: int = 1
    current_line_end: int = 1

    def flush() -> None:
        if not current_text:
            return
        if current_line_start > 0 and current_line_start != current_line_end:
            header = f'### File: {rel_file} (lines {current_line_start}-{current_line_end})'
        elif current_line_start > 0:
            header = f'### File: {rel_file} (line {current_line_start})'
        elif current_byte_start > 0:
            header = f'### File: {rel_file} (byte offset {current_byte_start})'
        else:
            header = f'### File: {rel_file}'
        parts.append(f'{header}\n{current_text}')

    for text, meta in chunks:
        file_path = meta.get('file_path', '')
        byte_offset = meta.get('byte_offset', 0)
        line_start = meta.get('line_start', 1)
        line_end = meta.get('line_end', line_start)
        text_bytes = text.encode('utf-8', errors='ignore')
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
            rel_file = meta.get('rel_path', file_path)
            current_text = text
            current_byte_start = byte_offset
            current_byte_end = chunk_end
            current_line_start = line_start
            current_line_end = line_end

    flush()
    return '\n\n'.join(parts)


def bm25_search(
    bm25_idx: BM25Index,
    query: str,
    top_n: int,
    path_filter: str | None = None,
) -> list[tuple[str, dict, float]]:
    if not bm25_idx.documents:
        return []
    tokenized_query = query.lower().split()
    scores = bm25_idx.bm25.get_scores(tokenized_query)
    indexed = list(zip(range(len(bm25_idx.documents)), scores, strict=True))
    if path_filter:
        indexed = [
            (i, s) for i, s in indexed
            if bm25_idx.metadatas[i].get('file_path') == path_filter
        ]
    indexed.sort(key=lambda x: x[1], reverse=True)
    return [
        (bm25_idx.documents[i], bm25_idx.metadatas[i], score)
        for i, score in indexed[:top_n]
        if score > 0
    ]


def reciprocal_rank_fusion(
    semantic_results: list[tuple[str, dict]],
    bm25_results: list[tuple[str, dict, float]],
    max_results: int,
    k: int = 60,
) -> list[tuple[str, dict]]:
    scores: dict[str, float] = {}
    doc_map: dict[str, tuple[str, dict]] = {}
    for rank, (text, meta) in enumerate(semantic_results):
        chunk_key = f"{meta.get('file_path', '')}:{meta.get('byte_offset', 0)}"
        scores[chunk_key] = scores.get(chunk_key, 0.0) + 1.0 / (k + rank + 1)
        doc_map[chunk_key] = (text, meta)
    for rank, (text, meta, _bm25_score) in enumerate(bm25_results):
        chunk_key = f"{meta.get('file_path', '')}:{meta.get('byte_offset', 0)}"
        scores[chunk_key] = scores.get(chunk_key, 0.0) + 1.0 / (k + rank + 1)
        doc_map[chunk_key] = (text, meta)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [
        doc_map[chunk_key]
        for chunk_key, rrf_score in ranked[:max_results]
    ]


def expand_context(
    fused: list[tuple[str, dict]],
    collections: list[chromadb.Collection],
    expansion_lines: int = 20,
    expansion_bytes: int = 4096,
) -> list[tuple[str, dict]]:
    expanded: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for text, meta in fused:
        file_path = meta.get('file_path', '')
        chunk_key = f"{file_path}:{meta.get('byte_offset', 0)}"
        if chunk_key in seen:
            continue
        seen.add(chunk_key)
        expanded.append((text, meta))
        if not file_path or file_path == '__manifest__':
            continue
        for collection in collections:
            try:
                results = collection.get(
                    where={'$and': [{'active': True}, {'file_path': {'$eq': file_path}}]},
                    include=['documents', 'metadatas'],
                )
            except Exception:
                continue
            for doc, m in zip(results.get('documents', []), results.get(
                    'metadatas', []), strict=True):
                neighbor_key = f"{file_path}:{m.get('byte_offset', 0)}"
                line_distance = abs(m.get('line_start', 0) - meta.get('line_start', 0))
                byte_distance = abs(m.get('byte_offset', 0) - meta.get('byte_offset', 0))
                if (neighbor_key not in seen and
                        line_distance <= expansion_lines and
                        byte_distance < expansion_bytes):
                    seen.add(neighbor_key)
                    expanded.append((doc, m))
    return expanded


def retrieve_context(  # noqa
    query: str, *, path_filter: str | None = None,
    path_pattern: str | None = None, top_k_override: int | None = None,
) -> str:
    collections = get_all_collections()
    if not collections:
        logger.info('no collections available, no context to retrieve')
        return ''
    pattern_matched_paths: list[str] | None = None
    if path_pattern is not None:
        all_active = get_active_file_paths()
        for abs_path in all_active:
            found = find_source_for_path(abs_path)
            rel_path = abs_path if found is None else found[1]
            if re.search(path_pattern, rel_path):
                if pattern_matched_paths is None:
                    pattern_matched_paths = []
                pattern_matched_paths.append(abs_path)
    embed_model = OllamaEmbedding(
        model_name=config.embed_model, base_url=config.ollama_base_url)
    check_shutdown()
    max_query_len = resolve_chunk_size(for_query=True)
    if len(query) > max_query_len:
        logger.info('query too long for embedding (%d chars), truncating to %d',
                    len(query), max_query_len)
        half = (max_query_len - 5) // 2
        query = query[:half] + '\n...\n' + query[-half:]
    query_embedding = embed_model.get_text_embedding(query)
    base_k = top_k_override if top_k_override is not None else config.top_k
    total_count = sum(c.count() for c in collections)
    min_top_k = min(max(1, base_k // 2), total_count)
    max_top_k = min(base_k * 2, total_count)
    active_condition: dict = {'active': True}
    if path_filter:
        where_filter = {'$and': [active_condition, {'file_path': {'$eq': path_filter}}]}
    elif pattern_matched_paths is not None:
        where_filter = {
            '$and': [
                active_condition,
                {'file_path': {'$in': pattern_matched_paths}},
            ],
        }
    else:
        where_filter = active_condition
    all_documents: list[str] = []
    all_distances: list[float] = []
    all_metadatas: list[dict] = []
    data_dir = Path(config.data_dir)
    for collection in collections:
        n = min(max_top_k, collection.count())
        if n == 0:
            continue
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=n,
            where=where_filter,
            include=['documents', 'distances', 'metadatas'],
        )
        all_documents.extend(results.get('documents', [[]])[0])
        all_distances.extend(results.get('distances', [[]])[0])
        all_metadatas.extend(results.get('metadatas', [[]])[0])
    if not all_documents:
        return ''
    semantic_ranked = sorted(zip(all_distances, all_documents, all_metadatas, strict=True),
                             key=lambda x: x[0])
    semantic_distances = [dist for dist, _, _ in semantic_ranked]
    semantic_chosen_k = select_top_k(semantic_distances, min_top_k, max_top_k)
    semantic_top = [
        (doc, meta) for _, doc, meta in semantic_ranked[:semantic_chosen_k]
    ]
    logger.info('semantic chosen: %d', semantic_chosen_k)

    all_bm25_results: list[tuple[str, dict, float]] = []
    for src in source_configs:
        cname = collection_name_for_source(
            config.embed_model, config.chunk_size, config.chunk_overlap, src.source_path)
        for collection in collections:
            if collection.name == cname:
                bm25_idx = get_bm25_for_collection(collection, data_dir, cname)
                all_bm25_results.extend(
                    bm25_search(bm25_idx, query, max_top_k, path_filter))
                break

    all_bm25_results.sort(key=lambda x: x[2], reverse=True)
    bm25_scores = [s for _, _, s in all_bm25_results]
    if bm25_scores:
        bm25_distances = [1.0 / (1.0 + s) for s in bm25_scores]
        bm25_chosen_k = select_top_k(bm25_distances, min_top_k, max_top_k)
    else:
        bm25_chosen_k = 0
    bm25_top = all_bm25_results[:bm25_chosen_k]
    logger.info('bm25 chosen: %d', bm25_chosen_k)
    fused = reciprocal_rank_fusion(semantic_top, bm25_top, max_top_k)
    logger.info('RRF fused: %d', len(fused))
    expansion_lines = max(20, config.chunk_size // 32) if config.chunk_size > 0 else 20
    expanded = expand_context(fused, collections, expansion_lines, resolve_chunk_size())
    logger.info('expanded context chunks: %d (from %d fused)', len(expanded), len(fused))
    final_documents = [text for text, _ in expanded]
    final_metadatas = [meta for _, meta in expanded]
    return format_chunks(final_documents, final_metadatas)


def get_active_file_paths() -> list[str]:
    data_dir = Path(config.data_dir)
    index = load_file_index(data_dir)
    all_paths: set[str] = set()
    for src in source_configs:
        cname = collection_name_for_source(
            config.embed_model, config.chunk_size, config.chunk_overlap, src.source_path)
        files = index.get(cname, {}).get('files', {})
        all_paths.update(
            fp for fp, entry in files.items()
            if fp != '__manifest__' and entry.get('active_sha')
        )
    return sorted(all_paths)


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
    doc = load_single_file_document(path)
    if doc is None:
        return f'Error: file not found: {path}'
    return doc.text


def create_mcp_server() -> mcp.server.Server:
    server = mcp.server.Server('ollama-rag')

    @server.list_tools()
    async def list_tools() -> list[mcp.types.Tool]:
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
    async def call_tool(name: str, arguments: dict) -> list[mcp.types.TextContent]:
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
    """Add the context to the first system prompt that exists."""
    gitcount = len([s for s in source_configs if is_git_source(s)])
    category = ('code' if gitcount == len(source_configs) else
                'directory' if not gitcount else 'mixed')
    system_content = PREAMBLES[category] + '\n\n' + context
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


def extract_rag_params(body: dict) -> dict:
    rag_params: dict = {}
    for key in list(body.keys()):
        if key.startswith('rag_'):
            rag_params[key] = body.pop(key)
    return rag_params


def apply_rag_params(
    rag_params: dict,
) -> tuple[int | None, str | None]:
    top_k_override: int | None = None
    path_pattern: str | None = None
    if 'rag_top_k' in rag_params:
        top_k_override = int(rag_params['rag_top_k'])
    if 'rag_path_pattern' in rag_params:
        path_pattern = str(rag_params['rag_path_pattern'])
    return top_k_override, path_pattern


@app.post('/v1/chat/completions')
async def chat_completions(request: fastapi.Request):
    body = await request.json()
    client_wants_stream = body.get('stream', False)
    query = extract_query_text(body.get('messages', []))
    logger.debug('query: %s, stream: %s', query, client_wants_stream)
    ollama_base_url = config.ollama_base_url
    rag_params = extract_rag_params(body)
    if rag_params:
        logger.info('rag params: %s', rag_params)
    top_k_override, path_pattern = apply_rag_params(rag_params)
    if query and source_configs:
        try:
            context = await asyncio.to_thread(
                retrieve_context, query, path_pattern=path_pattern, top_k_override=top_k_override)
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


def pad_list(shortList: list[str], length: int, default: str) -> list[str]:
    """Extend list with default values until it has the given length."""
    return shortList + [default] * max(0, length - len(shortList))


def build_source_configs(args: argparse.Namespace) -> list[SourceConfig]:
    """Build SourceConfig objects by pairing the multi-value arguments by
    index.  The Nth --exclude applies to the Nth --source-path, etc.
    """
    source_paths = args.source_path or []
    if not source_paths:
        return []
    n = len(source_paths)
    source_types = pad_list(args.source_type or [], n, 'auto')
    source_sub_paths = pad_list(args.source_sub_path or [], n, '')
    excludes = pad_list(args.exclude or [], n, '')
    git_exts = pad_list(
        args.git_extensions or [], n, args.git_extensions_default)
    dir_suffs = pad_list(
        args.dir_suffixes or [], n, args.dir_suffixes_default)
    configs = []
    for i in range(n):
        sp = source_paths[i]
        if not sp:
            continue
        configs.append(SourceConfig(
            source_path=sp,
            source_type=source_types[i],
            source_sub_path=source_sub_paths[i],
            exclude=excludes[i],
            git_extensions=git_exts[i],
            dir_suffixes=dir_suffs[i],
        ))
    return configs


def cmd_serve(args):
    global config
    global source_configs
    global mcp_manager

    config = args
    source_configs = build_source_configs(args)
    if source_configs:
        paths_str = ', '.join(src.source_path for src in source_configs)
        logger.info('configured %d source%s: %s', len(source_configs),
                    's' if len(source_configs) != 1 else '', paths_str)
    else:
        logger.warning('no source paths configured; RAG context retrieval is disabled')
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

    def handle_signal(signum, frame):
        shutdown_event.set()
        server.should_exit = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if args.initial:
        get_all_collections()
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
    global source_configs
    config = args
    source_configs = build_source_configs(args)
    if args.initial:
        get_all_collections()
    server = create_mcp_server()

    async def run():
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


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
    git_extensions = {
        'py', 'js', 'java', 'ts', 'md', 'rst', 'gradle', 'pro', 'ini', 'cfg',
        'properties', 'xml', 'toml', 'css', 'cmake', 'html', 'in', 'mako',
        'txt', 'pug', 'sh', 'styl', 'yaml', 'yml',
    }
    git_extension_default = ','.join('.' + e for e in sorted(git_extensions))
    doc_extensions = {'txt', 'md', 'pdf', 'docx', 'doc', 'rst', 'tex', 'bbl', 'sty', 'bst', 'bib'}
    doc_extension_default = ','.join('.' + e for e in sorted(doc_extensions))
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
            action='append', default=None,
            help='Source type for the corresponding --source-path.  Can be '
            'specified multiple times, paired by position.  Default is auto.',
        )
        sub.add_argument(
            '--source-path', '-s',
            action='append', default=None,
            help='The root of a git repo or document directory to embed.  '
            'Can be specified multiple times to index multiple sources '
            'into separate collections that are queried together.',
        )
        sub.add_argument(
            '--source-sub-path',
            action='append', default=None,
            help='For git repos, only embed files within this subpath.  '
            'Paired by position with --source-path.',
        )
        sub.add_argument(
            '--exclude', '-x',
            action='append', default=None,
            help='A comma-separated list of paths and file patterns to exclude.  '
            'Can be specified multiple times, one per --source-path.',
        )
        sub.add_argument(
            '--git-extensions',
            action='append', default=None,
            help=(
                'Only process specific file types in a git repo.  Can be '
                'specified multiple times, one per --source-path.  Default is '
                f'{git_extension_default}'
            ),
        )
        sub.add_argument(
            '--dir-suffixes',
            action='append', default=None,
            help=(
                'Only process specific file types in a non-git source folder.  '
                'Can be specified multiple times, one per --source-path.  '
                f'Default is {doc_extension_default}'
            ),
        )
        sub.add_argument(
            '--top-k', type=int,
            default=int(os.environ.get('RAG_TOP_K', '5')),
            help=(
                'How much context to fetch; default is 5.  Use larger values '
                'for general queries, smaller values for targeted queries.  '
                'A value between half and twice this will be chosen based on '
                'recall similarity.'
            ),
        )
        sub.add_argument(
            '--chunk-size', type=int,
            default=int(os.environ.get('RAG_CHUNK_SIZE', '0')),
            help='Embedding chunk size; default or 0 is determined by model.  '
            'Too big will fail',
        )
        sub.add_argument(
            '--chunk-overlap', type=int,
            default=int(os.environ.get('RAG_CHUNK_OVERLAP', '64')),
            help='Embedding chunk overlap; default is 64',
        )
        sub.add_argument(
            '--initial', action='store_true',
            help='Immediately embed source data on initial start rather than '
            'waiting for a query',
        )
        sub.set_defaults(
            git_extensions_default=git_extension_default,
            dir_suffixes_default=doc_extension_default,
        )
    return parser


def main():  # noqa
    parser = build_arg_parser()
    args = parser.parse_args()
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    if hasattr(args, 'source_path') and not args.source_path:
        env_paths = os.environ.get('RAG_SOURCE_PATH', '')
        if env_paths:
            args.source_path = [
                p.strip() for p in env_paths.split(os.pathsep) if p.strip()]
    if hasattr(args, 'source_type') and not args.source_type:
        env_val = os.environ.get('RAG_SOURCE_TYPE', '')
        if env_val:
            args.source_type = [env_val]
    if hasattr(args, 'source_sub_path') and not args.source_sub_path:
        env_val = os.environ.get('RAG_SOURCE_SUB_PATH', '')
        if env_val:
            args.source_sub_path = [env_val]
    if hasattr(args, 'exclude') and not args.exclude:
        env_val = os.environ.get('RAG_EXCLUDE', '')
        if env_val:
            args.exclude = [env_val]
    if hasattr(args, 'git_extensions') and not args.git_extensions:
        env_val = os.environ.get('RAG_GIT_EXTENSIONS', '')
        if env_val:
            args.git_extensions = [env_val]
    if hasattr(args, 'dir_suffixes') and not args.dir_suffixes:
        env_val = os.environ.get('RAG_DIR_SUFFIXES', '')
        if env_val:
            args.dir_suffixes = [env_val]
    if args.command == 'serve':
        cmd_serve(args)
    elif args.command == 'clear':
        cmd_clear(args)
    elif args.command == 'mcp':
        cmd_mcp(args)


if __name__ == '__main__':
    main()
