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
import pathspec
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
    '.py': 'python',
    '.java': 'java',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.js': 'javascript',
    '.jsx': 'javascript',
    '.go': 'go',
    '.rs': 'rust',
    '.cs': 'c_sharp',
    '.cpp': 'cpp',
    '.c': 'c',
    '.rb': 'ruby',
    '.kt': 'kotlin',
    '.swift': 'swift',
}


app = fastapi.FastAPI()
app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
config: argparse.Namespace

_shutdown_event = threading.Event()
_build_locks_mutex = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}


def load_file_index(data_dir: Path) -> dict:
    path = data_dir / 'file_index.json'
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_file_index(data_dir: Path, index: dict) -> None:
    path = data_dir / 'file_index.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index))


def list_paths(
    source_path: str, suffixes: list[str], exclude: list[str],
) -> Generator[Path, None, None]:
    source = Path(source_path)
    exclude_patterns = [p.strip() for p in (exclude or '').split(',') if p.strip()]
    spec = pathspec.PathSpec.from_lines('gitwildmatch', exclude_patterns)
    for p in sorted(source.rglob('*')):
        if not p.is_file():
            continue
        if p.suffix.lower() not in suffixes:
            continue
        relative = p.relative_to(source).as_posix()
        if spec.match_file(relative):
            continue
        yield p


def current_file_hashes_directory(
    source_path: str, suffixes: list[str], exclude: str,
) -> dict[str, str]:
    result = {}
    for p in list_paths(source_path, suffixes, exclude):
        rel = p.relative_to(source_path).as_posix()
        logger.debug('%s (%d)', p, os.path.getsize(p))
        result[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def current_file_hashes_git(
    source_path: str, extensions: list[str], sub_path: str, exclude: str,
) -> dict[str, str]:
    return {item.path: item.hexsha
            for item in repo_item(source_path, extensions, sub_path, exclude)}


def collection_name(embed_model: str, chunk_size: int, chunk_overlap: int) -> str:
    name = 'rag_' + hashlib.sha256(
        f'{embed_model}:{chunk_size}:{chunk_overlap}'.encode(),
    ).hexdigest()[:16]
    logger.info('collection name %s', name)
    return name


def is_git_source() -> bool:
    return config.source_type == 'git' or (
        config.source_type == 'auto' and os.path.exists(os.path.join(config.source_path, '.git')))


def resolve_chunk_size(forQuery: bool = False) -> int:
    if config.chunk_size == 0 or forQuery:
        context_length = get_model_context_length(config.embed_model)
        logger.info('model %s context length: %d', config.embed_model, context_length)
        chunk_size = max(256, (context_length * 3) // 4)
        if not forQuery:
            chunk_size = min(chunk_size, 8192)
        logger.info('auto chunk size: %d', chunk_size)
        return chunk_size
    return config.chunk_size


def strip_hop_by_hop_headers(headers: dict) -> dict:
    drop = {'content-length', 'transfer-encoding', 'content-encoding'}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def load_documents_from_directory(
    source_path: str, suffixes: list[str], exclude: list[str],
) -> tuple[list[Document], int]:
    documents = []
    count = 0
    for p in list_paths(source_path, suffixes, exclude):
        suffix = p.suffix.lower()
        try:
            if suffix == '.pdf':
                docs = PDFReader().load_data(p)
                raw_bytes = p.read_bytes()
            elif suffix == '.docx':
                docs = DocxReader().load_data(p)
                raw_bytes = p.read_bytes()
            elif suffix == '.md':
                docs = MarkdownReader().load_data(p)
                raw_bytes = p.read_bytes()
            else:
                raw_bytes = p.read_bytes()
                text = raw_bytes.decode(errors='replace')
                docs = [Document(text=text, metadata={'file_path': str(p)})]
            file_sha = hashlib.sha256(raw_bytes).hexdigest()
            file_size = len(raw_bytes)
            file_mtime = p.stat().st_mtime
            for doc in docs:
                if not doc.metadata:
                    doc.metadata = {}
                doc.metadata['file_path'] = doc.metadata.get('file_path') or str(p)
                doc.metadata['file_sha'] = file_sha
                doc.metadata['file_size'] = file_size
                doc.metadata['file_mtime'] = file_mtime
            documents.extend(docs)
            count += 1
        except Exception:
            pass
    return documents, count


def repo_item(
    source_path: str, extensions: list[str], sub_path: str, exclude: list[str],
) -> Generator[git.objects.blob.Blob, None, None]:
    sub_path = sub_path.replace('\\', '/')
    prefix = sub_path.strip('/') + '/' if sub_path.strip('/') else ''
    exclude_patterns = [p.strip() for p in (exclude or '').split(',') if p.strip()]
    spec = pathspec.PathSpec.from_lines('gitwildmatch', exclude_patterns)
    repo = git.Repo(source_path)
    for item in sorted(repo.tree().traverse(), key=lambda i: i.path):
        if item.type != 'blob':
            continue
        if prefix and not item.path.startswith(prefix):
            continue
        if not any(item.path.endswith(ext) for ext in extensions):
            continue
        if spec.match_file(item.path):
            continue
        yield item


def load_documents_from_git(
    source_path: str, extensions: list[str], sub_path: str, exclude: str,
) -> tuple[list[Document], int]:
    documents = []
    count = 0
    for item in repo_item(source_path, extensions, sub_path, exclude):
        try:
            text = item.data_stream.read().decode(errors='replace')
            documents.append(Document(text=text, metadata={
                'file_path': item.path,
                'file_sha': item.hexsha,
                'file_size': item.size,
                'file_mtime': 0,
            }))
            count += 1
        except Exception:
            pass
    return documents, count


def load_single_file_document(source_path: str, rel_path: str, file_sha: str) -> Document | None:
    if is_git_source():
        try:
            repo = git.Repo(source_path)
            blob = repo.tree() / rel_path
            text = blob.data_stream.read().decode(errors='replace')
            return Document(text=text, metadata={
                'file_path': rel_path,
                'file_sha': blob.hexsha,
                'file_size': blob.size,
                'file_mtime': 0,
            })
        except Exception:
            return None
    else:
        p = Path(source_path) / rel_path
        if not p.is_file():
            return None
        try:
            suffix = p.suffix.lower()
            if suffix == '.pdf':
                docs = PDFReader().load_data(p)
                raw_bytes = p.read_bytes()
            elif suffix == '.docx':
                docs = DocxReader().load_data(p)
                raw_bytes = p.read_bytes()
            elif suffix == '.md':
                docs = MarkdownReader().load_data(p)
                raw_bytes = p.read_bytes()
            else:
                raw_bytes = p.read_bytes()
                text = raw_bytes.decode(errors='replace')
                docs = [Document(text=text, metadata={'file_path': str(p)})]
            computed_sha = hashlib.sha256(raw_bytes).hexdigest()
            combined_text = '\n'.join(d.text for d in docs)
            return Document(text=combined_text, metadata={
                'file_path': rel_path,
                'file_sha': computed_sha,
                'file_size': len(raw_bytes),
                'file_mtime': p.stat().st_mtime,
            })
        except Exception:
            return None


def build_file_manifest_document(active_paths: list[str]) -> Document:
    text = 'Repository file listing:\n' + '\n'.join(sorted(active_paths))
    return Document(text=text, metadata={'file_path': '__manifest__'})


def check_embed_model_available(base_url: str, model_name: str) -> None:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f'{base_url}/api/show',
                json={'name': model_name},
            )
            if response.status_code != 200:
                msg = (f'Embedding model {model_name!r} is not available '
                       f'(HTTP {response.status_code}).  Run: ollama pull '
                       f'{model_name}'
                       )
                raise RuntimeError(msg)
    except httpx.ConnectError as exc:
        msg = f'Could not connect to Ollama at {base_url}: {exc}'
        raise RuntimeError(msg) from exc


def get_model_context_length(model_name: str) -> int:
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f'{config.ollama_base_url}/api/show',
                json={'name': model_name},
            )
            data = response.json()
            modelinfo = data.get('model_info', {})
            for key, value in modelinfo.items():
                if key.endswith('.context_length'):
                    return int(value)
            parameters = data.get('parameters', '')
            for line in parameters.splitlines():
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
            language=language, chunk_lines=chunk_size // 16, chunk_lines_overlap=overlap // 16,
            max_chars=chunk_size).split_text(text)
        results = []
        search_start = 0
        for chunk in raw_chunks:
            chunk_bytes = chunk.encode('utf-8')
            idx = text_bytes.find(chunk_bytes, search_start)
            if idx == -1:
                idx = search_start
            byte_offset = idx
            line_start = text_bytes[:byte_offset].count(b'\n') + 1
            line_end = line_start + chunk.count('\n')
            results.append({
                'text': chunk,
                'byte_offset': byte_offset,
                'line_start': line_start,
                'line_end': line_end,
            })
            search_start = idx + len(chunk_bytes)
        return results

    results = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        byte_offset = len(text[:start].encode('utf-8'))
        line_start = text[:start].count('\n') + 1
        line_end = line_start + chunk.count('\n')
        results.append({
            'text': chunk,
            'byte_offset': byte_offset,
            'line_start': line_start,
            'line_end': line_end,
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
    file_path = (doc.metadata or {}).get('file_path', '')
    file_sha = (doc.metadata or {}).get('file_sha', '')
    file_size = (doc.metadata or {}).get('file_size', 0)
    file_mtime = (doc.metadata or {}).get('file_mtime', 0)

    texts, embeddings, metadatas, ids = [], [], [], []
    for chunk_info in chunk_text(doc.text, chunk_size, chunk_overlap, file_path):
        _check_shutdown()
        t = chunk_info['text']
        try:
            embedding = embed_model.get_text_embedding(t)
        except Exception:
            embedding = None
        if not embedding:
            logger.warning('embedding failed for chunk in %s, skipping', file_path)
            continue
        chunk_id = make_chunk_id(file_sha, chunk_info['byte_offset'], t)
        texts.append(t)
        embeddings.append(embedding)
        metadatas.append({
            'file_path': file_path,
            'file_sha': file_sha,
            'file_size': file_size,
            'file_mtime': file_mtime,
            'byte_offset': chunk_info['byte_offset'],
            'line_start': chunk_info['line_start'],
            'line_end': chunk_info['line_end'],
            'active': True,
        })
        ids.append(chunk_id)
    return texts, embeddings, metadatas, ids


def add_chunks_to_collection(
    collection: chromadb.Collection,
    texts: list[str], embeddings: list[list[float]],
    metadatas: list[dict], ids: list[str],
) -> None:
    if not texts:
        return
    max_batch = collection._client.get_max_batch_size()
    for start in range(0, len(texts), max_batch):
        end = min(start + max_batch, len(texts))
        collection.add(
            documents=texts[start:end],
            embeddings=embeddings[start:end],
            metadatas=metadatas[start:end],
            ids=ids[start:end],
        )


def set_chunks_active(collection: chromadb.Collection, chunk_ids: list[str], active: bool) -> None:
    if not chunk_ids:
        return
    max_batch = collection._client.get_max_batch_size()
    for start in range(0, len(chunk_ids), max_batch):
        end = min(start + max_batch, len(chunk_ids))
        batch_ids = chunk_ids[start:end]
        collection.update(
            ids=batch_ids,
            metadatas=[{'active': active}] * len(batch_ids),
        )


def delete_chunks(collection: chromadb.Collection, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    max_batch = collection._client.get_max_batch_size()
    for start in range(0, len(chunk_ids), max_batch):
        end = min(start + max_batch, len(chunk_ids))
        collection.delete(ids=chunk_ids[start:end])


def update_manifest(
    collection: chromadb.Collection, active_paths: list[str],
    chunk_size: int, chunk_overlap: int, embed_model: OllamaEmbedding,
    file_index_entry: dict,
) -> None:
    old_manifest = file_index_entry.get('files', {}).get('__manifest__', {})
    old_ids = []
    for version_ids in old_manifest.get('versions', {}).values():
        old_ids.extend(version_ids)
    delete_chunks(collection, old_ids)

    manifest_doc = build_file_manifest_document(active_paths)
    texts, embeddings, metadatas, ids = embed_document_chunks(
        manifest_doc, chunk_size, chunk_overlap, embed_model)
    add_chunks_to_collection(collection, texts, embeddings, metadatas, ids)

    manifest_sha = hashlib.sha256('\n'.join(sorted(active_paths)).encode()).hexdigest()
    files = file_index_entry.setdefault('files', {})
    files['__manifest__'] = {
        'active_sha': manifest_sha,
        'versions': {manifest_sha: ids},
    }


def sync_collection(
    collection: chromadb.Collection, data_dir: Path, cname: str,
    current_hashes: dict[str, str],
) -> None:
    check_embed_model_available(config.ollama_base_url, config.embed_model)
    embed_model = OllamaEmbedding(
        model_name=config.embed_model,
        base_url=config.ollama_base_url,
    )
    chunk_size = resolve_chunk_size()

    index = load_file_index(data_dir)
    coll_entry = index.setdefault(cname, {'files': {}})
    files_entry = coll_entry['files']

    indexed_paths = {fp for fp in files_entry if fp != '__manifest__'}
    current_paths = set(current_hashes.keys())

    new_paths = current_paths - indexed_paths
    deleted_paths = indexed_paths - current_paths
    common_paths = current_paths & indexed_paths

    modified_paths = set()
    reactivate_paths = set()
    unchanged_paths = set()

    for fp in common_paths:
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

    needs_update = bool(new_paths or deleted_paths or modified_paths or reactivate_paths)

    if not needs_update:
        logger.info('collection is up to date')
        return

    httpx_logger = logging.getLogger('httpx')
    httpx_logger_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)

    for fp in deleted_paths:
        logger.info('deactivating deleted file: %s', fp)
        file_entry = files_entry[fp]
        for version_ids in file_entry.get('versions', {}).values():
            set_chunks_active(collection, version_ids, False)
        file_entry['active_sha'] = ''

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
                        set_chunks_active(collection, files_entry[fp]['versions'][old_sha], False)

                doc = load_single_file_document(config.source_path, fp, new_sha)
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

                logger.info('embedded %s (%d chunks)', fp, len(ids))
                progress.update(1)

    httpx_logger.setLevel(httpx_logger_level)

    active_paths = [fp for fp in current_hashes if fp != '__manifest__']
    update_manifest(
        collection, active_paths, chunk_size, config.chunk_overlap, embed_model, coll_entry)

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

    if is_git_source():
        extensions = [e.strip() for e in config.git_extensions.split(',')]
        current_hashes = current_file_hashes_git(
            config.source_path, extensions, config.source_sub_path, config.exclude)
    else:
        suffixes = [e.strip() for e in config.dir_suffixes.split(',')]
        current_hashes = current_file_hashes_directory(
            config.source_path, suffixes, config.exclude)

    index = load_file_index(data_dir)
    index[cname] = {'files': {}}
    save_file_index(data_dir, index)

    sync_collection(collection, data_dir, cname, current_hashes)
    return collection


def get_collection() -> chromadb.Collection:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(config.source_path)
    source_unavailable = False
    if is_git_source():
        try:
            git.Repo(config.source_path)
        except Exception:
            source_unavailable = True
    else:
        if not source_path.exists() or not any(source_path.iterdir()):
            source_unavailable = True

    cname = collection_name(config.embed_model, config.chunk_size, config.chunk_overlap)
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
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

        if is_git_source():
            extensions = [e.strip() for e in config.git_extensions.split(',')]
            current_hashes = current_file_hashes_git(
                config.source_path, extensions, config.source_sub_path, config.exclude)
        else:
            suffixes = [e.strip() for e in config.dir_suffixes.split(',')]
            current_hashes = current_file_hashes_directory(
                config.source_path, suffixes, config.exclude)

        try:
            collection = chroma_client.get_collection(cname)
        except Exception:
            return build_collection()

        index = load_file_index(data_dir)
        indexed_files = index.get(cname, {}).get('files', {})
        indexed_hashes = {fp: data['active_sha'] for fp, data in indexed_files.items()
                          if fp != '__manifest__' and data.get('active_sha')}

        if current_hashes != indexed_hashes:
            sync_collection(collection, data_dir, cname, current_hashes)

        return collection


def select_top_k(distances: list[float], min_k: int, max_k: int) -> int:
    if len(distances) < 2:
        return len(distances)
    best = distances[0]
    worst = distances[-1]
    spread = worst - best
    if spread < 1e-6:
        return max_k
    drop_after_first = distances[1] - best
    sharpness = drop_after_first / spread
    if sharpness > 0.5:
        return min_k
    ratio = 1.0 - sharpness
    return round(min_k + ratio * (max_k - min_k))


def retrieve_context(query: str) -> str:
    collection = get_collection()
    if collection is None:
        return ''
    embed_model = OllamaEmbedding(
        model_name=config.embed_model,
        base_url=config.ollama_base_url,
    )
    _check_shutdown()
    count = collection.count()
    if not count:
        logger.info('collection is empty, no context to retrieve')
        return ''
    max_query_len = resolve_chunk_size(forQuery=True)
    if len(query) > max_query_len:
        logger.info('query too long for embedding (%d chars), truncating to %d',
                    len(query), max_query_len)
        half = (max_query_len - 5) // 2
        query = query[:half] + '\n...\n' + query[-half:]
    query_embedding = embed_model.get_text_embedding(query)
    min_top_k = min(max(1, config.top_k // 2), count)
    max_top_k = min(config.top_k * 2, count)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=max_top_k,
        where={'active': True},
        include=['documents', 'distances'],
    )
    documents = results.get('documents', [[]])[0]
    distances = results.get('distances', [[]])[0]
    chosen_k = select_top_k(distances, min_top_k, max_top_k)
    logger.info('Adding context result documents: %d', chosen_k)
    return '\n\n---\n\n'.join(documents[:chosen_k])


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
    messages = body.get('messages', [])
    system_content = (
        "Use the following retrieved context to help answer the user's question.\n\n" +
        context
    )
    new_messages = []
    inserted = False
    for message in messages:
        if message.get('role') == 'system' and not inserted:
            existing = message.get('content', '')
            new_messages.append(
                {'role': 'system', 'content': existing + '\n\n' + system_content},
            )
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
    messages = body.get('messages', [])
    query = extract_query_text(messages)
    logger.debug('query: %s, stream: %s', query, client_wants_stream)
    ollama_base_url = config.ollama_base_url
    if query and config.source_path:
        try:
            context = await asyncio.to_thread(retrieve_context, query)
            logger.debug('context length: %d', len(context))
            body = inject_context(body, context)
        except Exception:
            logger.exception('retrieval failed, proceeding without context')
    if client_wants_stream:
        body['stream'] = True

        async def generate():
            async with httpx.AsyncClient(timeout=None) as client, client.stream(
                'POST',
                f'{ollama_base_url}/v1/chat/completions',
                json=body,
                headers={'Content-Type': 'application/json'},
            ) as response:
                logger.debug('stream status: %d', response.status_code)
                async for chunk in response.aiter_bytes():
                    yield chunk

        return fastapi.responses.StreamingResponse(generate(), media_type='text/event-stream')
    body['stream'] = False

    def fetch():
        with httpx.Client(timeout=None) as client:
            response = client.post(
                f'{ollama_base_url}/v1/chat/completions',
                json=body,
                headers={'Content-Type': 'application/json'},
            )
            logger.debug('upstream status: %d', response.status_code)
            logger.debug('upstream body: %s', response.text[:500])
            if len(response.text) > 500:
                logger.debug('upstream body end: ...%s', response.text[500:][-500:])
            logger.debug('upstream content length: %d', len(response.content))
            return response.content, response.status_code, dict(response.headers)

    content, status_code, headers = await asyncio.to_thread(fetch)
    headers = strip_hop_by_hop_headers(headers)
    return fastapi.responses.Response(
        content=content,
        status_code=status_code,
        headers=headers,
    )


@app.api_route(
    '/{path:path}',
    methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'],
)
async def proxy_passthrough(request: fastapi.Request, path: str):
    url = f'{config.ollama_base_url}/{path}'
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.request(
            method=request.method,
            url=url,
            headers={k: v for k, v in request.headers.items() if k.lower() != 'host'},
            content=await request.body(),
            params=request.query_params,
        )
    logger.debug('response headers: %s', dict(response.headers))
    headers = strip_hop_by_hop_headers(dict(response.headers))
    logger.debug('response content length: %d', len(response.content))
    return fastapi.responses.Response(
        content=response.content,
        status_code=response.status_code,
        headers=headers,
    )


def cmd_serve(args):
    global config
    config = args
    uv_config = uvicorn.Config(app, host=args.host, port=args.port)
    server = uvicorn.Server(uv_config)
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
    chroma_dir = data_dir / 'chroma'
    file_index = data_dir / 'file_index.json'
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir)
    if file_index.exists():
        file_index.unlink()
    print('Cleared.')


def build_arg_parser() -> argparse.ArgumentParser:
    default_data_dir = str(Path.home() / '.local' / 'share' / 'rag_proxy')
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        '--data-dir',
        default=os.environ.get('RAG_DATA_DIR', default_data_dir),
        help=f'Cache directory; default is {default_data_dir}',
    )
    shared.add_argument(
        '--ollama-base-url',
        default=os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434'),
        help='Ollama URL; default is http://localhost:11434',
    )
    shared.add_argument(
        '--embed-model', '-e',
        default=os.environ.get('RAG_EMBED_MODEL', 'nomic-embed-text'),
        help='Embedding model; default is nomic-embed-text.  Others are '
        'mxbai-embed-large (good for code), all-minilm (small), '
        'snowflake-arctic-embed, bge-m3 (multilingual), bge-large (English). '
        'Do ollama pull on the model before using it.',
    )
    shared.add_argument(
        '--source-type',
        choices=['directory', 'git', 'auto'],
        default=os.environ.get('RAG_SOURCE_TYPE', 'auto'),
        help='Auto checks for a .git folder.  If git, only tracked files are used.',
    )
    shared.add_argument(
        '--source-path', '-s',
        default=os.environ.get('RAG_SOURCE_PATH', ''),
        help='The root of the git repo or documents to embed',
    )
    shared.add_argument(
        '--source-sub-path',
        default=os.environ.get('RAG_SOURCE_SUB_PATH', ''),
        help='For git repos, only embed files within this subpath',
    )
    shared.add_argument(
        '--exclude', '-x',
        default=os.environ.get('RAG_EXCLUDE', ''),
        help='A comma-separated list of paths and file signatures to exclude',
    )
    shared.add_argument(
        '--git-extensions',
        default=os.environ.get('RAG_GIT_EXTENSIONS', '.py,.js,.java,.ts,.md,.rst'),
        help='Only process specific file types in a git repo; default is '
        "'.py,.js,.java,.ts,.md,.rst'.",
    )
    shared.add_argument(
        '--dir-suffixes',
        default=os.environ.get('RAG_DIR_SUFFIXES', '.txt,.md,.pdf,.docx,.rst'),
        help='Only process specific file types in a non-git source folder; '
        "default is '.txt,.md,.pdf,.docx,.rst'",
    )
    shared.add_argument(
        '--top-k',
        type=int,
        default=int(os.environ.get('RAG_TOP_K', '5')),
        help='How much to context to fetch; default is 5.  Use larger values '
        'for general queries, smaller values for targeted queries.  A value '
        'between half and twice this will be chosen based on recall '
        'similarity.',
    )
    shared.add_argument(
        '--host',
        type=str,
        default='0.0.0.0',
        help='Proxy host; default is 0.0.0.0.  Using localhost is more secure.',
    )
    shared.add_argument(
        '--port',
        type=int,
        default=int(os.environ.get('RAG_PROXY_PORT', '11435')),
        help='Proxy port; default is 11435',
    )
    shared.add_argument(
        '--chunk-size',
        type=int,
        default=int(os.environ.get('RAG_CHUNK_SIZE', '0')),
        help='Embedding chunk size; default or 0 is determined by model.  Too big will fail',
    )
    shared.add_argument(
        '--chunk-overlap',
        type=int,
        default=int(os.environ.get('RAG_CHUNK_OVERLAP', '64')),
        help='Embedding chunk overlap; default is 64',
    )
    shared.add_argument(
        '--verbose', '-v', action='count', default=0, help='Increase verbosity')
    parser = argparse.ArgumentParser(description='Local RAG proxy for Ollama')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('serve', parents=[shared])
    subparsers.add_parser('clear', parents=[shared])
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    if args.command == 'serve':
        cmd_serve(args)
    elif args.command == 'clear':
        cmd_clear(args)


if __name__ == '__main__':
    main()
