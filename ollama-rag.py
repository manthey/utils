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
import time
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


def load_state(data_dir: Path) -> dict:
    state_file = data_dir / 'state.json'
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {}


def save_state(data_dir: Path, state: dict) -> None:
    state_file = data_dir / 'state.json'
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))


def list_paths(
    source_path: str, suffixes: list[str], exclude: [str],
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


def source_fingerprint_directory(source_path: str, suffixes: list[str], exclude: str) -> str:
    hasher = hashlib.sha256()
    logging.getLogger('uvicorn').setLevel(logging.CRITICAL)
    for p in list_paths(source_path, suffixes, exclude):
        logger.debug('%s (%d)', p, os.path.getsize(p))
        hasher.update(str(p).encode())
        hasher.update(str(p.stat().st_mtime).encode())
    return hasher.hexdigest()


def source_fingerprint_git(
    source_path: str, extensions: list[str], sub_path: str, exclude: str,
) -> str:
    hasher = hashlib.sha256()
    for item in repo_item(source_path, extensions, sub_path, exclude):
        logger.debug('%s (%d)', item.path, item.size)
        hasher.update(item.path.encode())
        hasher.update(item.hexsha.encode())
    return hasher.hexdigest()


def collection_name(model_name: str, source_path: str, embed_model: str,
                    chunk_size: int, chunk_overlap: int) -> str:
    name = 'rag_' + hashlib.sha256(
        f'{model_name}:{source_path}:{embed_model}:{chunk_size}:{chunk_overlap}'.encode(),
    ).hexdigest()[:16]
    logger.info('collection name %s', name)
    return name


def is_git_source() -> bool:
    return config.source_type == 'git' or (
        config.source_type == 'auto' and os.path.exists(os.path.join(config.source_path, '.git')))


def resolve_chunk_size() -> int:
    if config.chunk_size == 0:
        chunk_size = get_chunk_size_for_model(config.embed_model)
        logger.info('auto chunk size: %d', chunk_size)
        return chunk_size
    return config.chunk_size


def strip_hop_by_hop_headers(headers: dict) -> dict:
    drop = {'content-length', 'transfer-encoding', 'content-encoding'}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def load_documents_from_directory(
    source_path: str, suffixes: list[str], exclude: [str],
) -> list[Document]:
    documents = []
    for p in list_paths(source_path, suffixes, exclude):
        suffix = p.suffix.lower()
        try:
            if suffix == '.pdf':
                docs = PDFReader().load_data(p)
            elif suffix == '.docx':
                docs = DocxReader().load_data(p)
            elif suffix == '.md':
                docs = MarkdownReader().load_data(p)
            else:
                text = p.read_text(errors='replace')
                docs = [Document(text=text, metadata={'file_p': str(p)})]
            documents.extend(docs)
        except Exception:
            pass
    return documents


def repo_item(
    source_path: str, extensions: list[str], sub_path: str, exclude: [str],
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
) -> list[Document]:
    documents = []
    for item in repo_item(source_path, extensions, sub_path, exclude):
        try:
            text = item.data_stream.read().decode(errors='replace')
            documents.append(Document(text=text, metadata={'file_path': item.path}))
        except Exception:
            pass
    return documents


def build_file_manifest_document(documents: list[Document]) -> Document:
    """Return a single Document listing all file paths in the corpus."""
    paths = []
    for doc in documents:
        path = doc.metadata.get('file_path') or doc.metadata.get('file_p', '')
        if path and path not in paths:
            paths.append(path)
    text = 'Repository file listing:\n' + '\n'.join(paths)
    return Document(text=text, metadata={'file_path': '__manifest__'})


def check_embed_model_available(base_url: str, model_name: str) -> None:
    """
    Raise RuntimeError if the embedding model cannot be reached or is not
    listed.
    """
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
        raise RuntimeError(msg)


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


def get_chunk_size_for_model(model_name: str) -> int:
    context_length = get_model_context_length(model_name)
    logger.info('model %s context length: %d', model_name, context_length)
    return max(256, (context_length * 3) // 4)


def chunk_text(
    text: str, chunk_size: int = 512, overlap: int = 64, filename: str = '',
) -> list[str]:
    ext = '.' + filename.rsplit('.', 1)[-1] if '.' in filename else ''
    language = EXTENSION_TO_LANGUAGE.get(ext)

    if language:
        return CodeSplitter(
            language=language, chunk_lines=chunk_size // 16, chunk_lines_overlap=overlap // 16,
            max_chars=chunk_size).split_text(text)

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def build_collection(model_name: str) -> chromadb.Collection:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)

    cname = collection_name(
        model_name, config.source_path, config.embed_model, config.chunk_size,
        config.chunk_overlap)
    chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
    try:
        chroma_client.delete_collection(cname)
    except Exception:
        pass
    collection = chroma_client.create_collection(cname)

    if is_git_source():
        extensions = [e.strip() for e in config.git_extensions.split(',')]
        documents = load_documents_from_git(
            config.source_path, extensions, config.source_sub_path, config.exclude)
    else:
        suffixes = [e.strip() for e in config.dir_suffixes.split(',')]
        documents = load_documents_from_directory(config.source_path, suffixes, config.exclude)
    logger.info('loaded %d documents', len(documents))
    check_embed_model_available(config.ollama_base_url, config.embed_model)
    logger.info('embedding model %s', config.embed_model)

    manifest = build_file_manifest_document(documents)
    documents = [manifest] + documents

    embed_model = OllamaEmbedding(
        model_name=config.embed_model,
        base_url=config.ollama_base_url,
    )

    all_texts = []
    all_metadatas = []

    chunk_size = resolve_chunk_size()
    for doc in documents:
        for chunk in chunk_text(doc.text, chunk_size, config.chunk_overlap,
                                doc.metadata.get('file_path', '')):
            all_texts.append(chunk)
            all_metadatas.append(doc.metadata)

    logger.debug('embedding %d chunks from %d documents', len(all_texts), len(documents))

    all_ids = [hashlib.sha256(f"{m.get('file_path','')}:{i}:{t}".encode()).hexdigest()[:32]
               for i, (t, m) in enumerate(zip(all_texts, all_metadatas, strict=False))]
    max_batch_size = chroma_client.get_max_batch_size()
    logger.debug('chromadb max batch size: %d', max_batch_size)
    # Suppress the per-request HTTP logs from httpx during the tight embedding
    # loop; they drown out everything useful.  Restore the level afterwards.
    httpx_logger = logging.getLogger('httpx')
    httpx_logger_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)

    kept_texts = []
    kept_embeddings = []
    kept_metadatas = []
    kept_ids = []
    total = len(all_texts)
    with tqdm.tqdm(
        total=total,
        desc='Embedding',
        unit='chunk',
        dynamic_ncols=True,
    ) as progress:
        for i, (t, m, doc_id) in enumerate(zip(all_texts, all_metadatas, all_ids, strict=False)):
            if _shutdown_event.is_set():
                msg = 'Shutdown'
                raise RuntimeError(msg)
            try:
                embedding = embed_model.get_text_embedding(t)
            except Exception:
                embedding = None
            if not embedding:
                logger.warning(
                    'embedding chunk %d of %d failed or empty, skipping',
                    i + 1, total,
                )
                progress.update(1)
                continue
            kept_texts.append(t)
            kept_embeddings.append(embedding)
            kept_metadatas.append(m)
            kept_ids.append(doc_id)
            progress.update(1)

    httpx_logger.setLevel(httpx_logger_level)

    logger.info('adding %d of %d chunks to collection', len(kept_texts), total)
    kept_total = len(kept_texts)
    for batch_start in range(0, kept_total, max_batch_size):
        batch_end = min(batch_start + max_batch_size, kept_total)
        logger.info(
            'adding batch %d-%d of %d to collection',
            batch_start + 1, batch_end, kept_total,
        )
        collection.add(
            documents=kept_texts[batch_start:batch_end],
            embeddings=kept_embeddings[batch_start:batch_end],
            metadatas=kept_metadatas[batch_start:batch_end],
            ids=kept_ids[batch_start:batch_end],
        )
    logger.info(
        'collection built with %d chunks (%d skipped)',
        kept_total, total - kept_total,
    )
    return collection


def get_collection(model_name: str) -> chromadb.Collection:
    data_dir = Path(config.data_dir)
    chroma_dir = data_dir / 'chroma'
    chroma_dir.mkdir(parents=True, exist_ok=True)

    state_key = f'{model_name}:{config.source_path}:{config.embed_model}'

    source_unavailable = False
    source_path = Path(config.source_path)
    if is_git_source():
        try:
            git.Repo(config.source_path)
        except Exception:
            source_unavailable = True
    else:
        if not source_path.exists() or not any(source_path.iterdir()):
            source_unavailable = True

    if is_git_source():
        extensions = [e.strip() for e in config.git_extensions.split(',')]
        fingerprint = source_fingerprint_git(
            config.source_path, extensions, config.source_sub_path, config.exclude)
    else:
        suffixes = [e.strip() for e in config.dir_suffixes.split(',')]
        fingerprint = source_fingerprint_directory(config.source_path, suffixes, config.exclude)
    with _build_locks_mutex:
        if fingerprint not in _build_locks:
            _build_locks[fingerprint] = threading.Lock()
        lock = _build_locks[fingerprint]
    with lock:
        state = load_state(data_dir)
        cached = state.get(state_key, {})
        chroma_client = chromadb.PersistentClient(path=str(chroma_dir))
        cname = collection_name(
            model_name, config.source_path, config.embed_model,
            config.chunk_size, config.chunk_overlap)
        if source_unavailable and cached.get('fingerprint'):
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
        if cached.get('fingerprint') != fingerprint:
            collection = build_collection(model_name)
            state[state_key] = {'fingerprint': fingerprint, 'built_at': time.time()}
            save_state(data_dir, state)
            return collection
        return chroma_client.get_or_create_collection(cname)


def retrieve_context(model_name: str, query: str) -> str:
    collection = get_collection(model_name)
    embed_model = OllamaEmbedding(
        model_name=config.embed_model,
        base_url=config.ollama_base_url,
    )
    if _shutdown_event.is_set():
        msg = 'Shutdown'
        raise RuntimeError(msg)
    max_query_len = resolve_chunk_size()
    if len(query) > max_query_len:
        logger.info('query too long for embedding (%d chars), truncating to %d',
                    len(query), max_query_len)
        # Keep start and end; the center is probably the least interesting
        half = (max_query_len - 5) // 2
        query = query[:half] + '\n...\n' + query[-half:]
    query_embedding = embed_model.get_text_embedding(query)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=config.top_k,
    )
    documents = results.get('documents', [[]])[0]
    logger.info('Adding context result documents: %d', len(documents))
    return '\n\n---\n\n'.join(documents)


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
    model_name = body.get('model', '')
    messages = body.get('messages', [])
    query = extract_query_text(messages)
    logger.debug('query: %s, stream: %s', query, client_wants_stream)
    ollama_base_url = config.ollama_base_url

    if query and config.source_path:
        try:
            context = await asyncio.to_thread(retrieve_context, model_name, query)
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
    uv_config = uvicorn.Config(app, host='0.0.0.0', port=args.port)
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
    state_file = data_dir / 'state.json'
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir)
    if state_file.exists():
        state_file.unlink()
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
        'for general queries, smaller values for targeted queries.',
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
