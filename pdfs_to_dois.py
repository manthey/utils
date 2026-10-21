#!/usr/bin/env python3
# /// script
# requires-python = '>=3.11'
# dependencies = [
#   'pypdf',
#   'requests',
# ]
# ///
"""Find the DOIs of a directory of already-downloaded PDFs.

DOIs are taken from embedded PDF metadata, the text of the first pages, and,
when available, markdown conversions made by pdfs_to_markdown.py.  Every
candidate is resolved and its registered title compared to the paper before it
is accepted.  If no DOI is found, the paper's title is looked up on Crossref and
OpenAlex.

The output has one DOI per line with the source file as a comment, so it can be
passed to ``paper_search.py acknowledge --dois-file``.  Papers that could not be
identified are written as comment blocks; add DOI lines beneath them by hand to
include them.
"""

import argparse
import contextlib
import difflib
import hashlib
import json
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

import pypdf
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logging.getLogger('pypdf').setLevel(logging.ERROR)

DOI_RE = re.compile(r'\b(10\.\d{4,9}/[^\s"\'<>{}\[\]|\\^`]+)', re.IGNORECASE)
ARXIV_RE = re.compile(
    r'\barXiv:\s*(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?', re.IGNORECASE)
HEADING_RE = re.compile(r'^#{1,3}\s+(.+?)\s*#*\s*$')
REFERENCES_RE = re.compile(
    r'^#+\s*(?:\d+\.?\s*)?(references|bibliography|works cited|literature cited)\b',
    re.IGNORECASE | re.MULTILINE)
BAD_META_TITLE_RE = re.compile(
    r'(\.(docx?|pdf|dvi|tex|ps|indd|qxd)$|^untitled|^microsoft word|^title$|^slide \d)',
    re.IGNORECASE)
# Headings that end the title area of a paper's first page
BODY_HEADINGS = {
    'abstract', 'introduction', 'keywords', 'index terms', 'summary',
    'background', 'contents', 'table of contents', 'highlights',
}
MARKDOWN_HEAD_CHARS = 20000


def normalize_text(text: str) -> str:
    """Reduce text to lowercase alphanumeric words for comparisons."""
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = re.sub(r'-\s*\n\s*', '', text)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def clean_doi(raw: str) -> str:
    doi = raw.rstrip('.,;:')
    while doi and doi[-1] in ')]}' and doi.count('(') < doi.count(')'):
        doi = doi[:-1].rstrip('.,;:')
    return doi.lower()


def find_dois(text: str) -> list[str]:
    dois = []
    for match in DOI_RE.finditer(text or ''):
        doi = clean_doi(match.group(1))
        if '/' in doi and doi not in dois:
            dois.append(doi)
    return dois


def find_arxiv_dois(text: str) -> list[str]:
    dois = []
    for match in ARXIV_RE.finditer(text or ''):
        doi = f'10.48550/arxiv.{match.group(1).lower()}'
        if doi not in dois:
            dois.append(doi)
    return dois


def file_hash(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open('rb') as fptr:
        for chunk in iter(lambda: fptr.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def read_pdf(path: Path, pages: int) -> dict:
    """Read metadata DOIs, a plausible metadata title, and first-page text."""
    info = {'meta_dois': [], 'meta_title': None, 'text': ''}
    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted:
            reader.decrypt('')
    except Exception as exc:
        logger.debug('Cannot open %s: %s', path, exc)
        return info
    meta_text = []
    with contextlib.suppress(Exception):
        meta = reader.metadata or {}
        meta_text.extend(str(value) for value in meta.values())
        title = str(meta.get('/Title') or '').strip()
        if len(title) >= 10 and not BAD_META_TITLE_RE.search(title):
            info['meta_title'] = title
    with contextlib.suppress(Exception):
        root = reader.trailer['/Root']
        if '/Metadata' in root:
            xmp = root['/Metadata'].get_object().get_data()
            meta_text.append(xmp.decode('utf-8', 'replace'))
    info['meta_dois'] = find_dois('\n'.join(meta_text))
    texts = []
    for page in reader.pages[:pages]:
        with contextlib.suppress(Exception):
            texts.append(page.extract_text() or '')
    info['text'] = '\n'.join(texts)
    return info


def read_markdown_head(path: Path | None) -> str:
    """Return the start of a markdown file, stopping at the references."""
    if not path:
        return ''
    try:
        with path.open(encoding='utf-8', errors='replace') as fptr:
            text = fptr.read(MARKDOWN_HEAD_CHARS)
    except OSError as exc:
        logger.debug('Cannot read %s: %s', path, exc)
        return ''
    match = REFERENCES_RE.search(text)
    return text[:match.start()] if match else text


def markdown_titles(text: str, limit: int = 3) -> list[str]:
    """Return the first few headings before the abstract as title guesses."""
    titles = []
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if not match:
            continue
        title = re.sub(r'[*_`]', '', match.group(1)).strip()
        if normalize_text(title) in BODY_HEADINGS:
            break
        if len(title) >= 10:
            titles.append(title)
            if len(titles) >= limit:
                break
    return titles


def find_markdown(pdf_path: Path, pdf_root: Path, md_dirs: list[Path], suffix: str,
                  md_index: dict[str, list[Path]]) -> Path | None:
    """Locate the markdown conversion of a PDF.

    pdfs_to_markdown.py writes ``<stem><suffix>`` beside the PDF, flat in an
    output directory, or in a mirrored tree under it (``--deep``).
    """
    md_name = pdf_path.with_suffix(suffix).name
    rel_parent = pdf_path.parent.relative_to(pdf_root)
    paths = [pdf_path.with_suffix(suffix)]
    for md_dir in md_dirs:
        paths.extend([md_dir / rel_parent / md_name, md_dir / md_name])
    for path in paths:
        if path.is_file():
            return path
    matches = md_index.get(md_name, [])
    return matches[0] if len(matches) == 1 else None


class Resolver:
    """Look up DOIs and titles on doi.org, Crossref, and OpenAlex."""

    def __init__(self, email: str | None, delay: float):
        self.email = email
        self.delay = delay
        self.last_request = 0.0
        self.lookups = {}
        self.session = requests.Session()
        retry = Retry(total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      respect_retry_after_header=True)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        agent = 'pdfs_to_dois/1.0'
        if email:
            agent += f' (mailto:{email})'
        self.session.headers['User-Agent'] = agent

    def get(self, url: str, **kwargs) -> requests.Response:
        wait = self.last_request + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self.session.get(url, timeout=30, **kwargs)
        finally:
            self.last_request = time.monotonic()

    def lookup(self, doi: str) -> dict | None:
        """Return the registered DOI and title, or None if it does not resolve."""
        if doi not in self.lookups:
            resp = self.get(
                f'https://doi.org/{requests.utils.quote(doi, safe="/")}',
                headers={'Accept': 'application/vnd.citationstyles.csl+json'})
            if resp.status_code in (404, 406):
                self.lookups[doi] = None
            else:
                resp.raise_for_status()
                try:
                    record = resp.json()
                except ValueError:
                    record = {}
                title = record.get('title') or ''
                if isinstance(title, list):
                    title = title[0] if title else ''
                self.lookups[doi] = {
                    'doi': (record.get('DOI') or doi).lower(), 'title': title,
                } if title else None
        return self.lookups[doi]

    def search_crossref(self, title: str) -> list[dict]:
        params = {'query.bibliographic': title[:300], 'rows': 5, 'select': 'DOI,title'}
        if self.email:
            params['mailto'] = self.email
        resp = self.get('https://api.crossref.org/works', params=params)
        resp.raise_for_status()
        return [{'doi': item['DOI'].lower(), 'title': item['title'][0]}
                for item in resp.json().get('message', {}).get('items', [])
                if item.get('DOI') and item.get('title')]

    def search_openalex(self, title: str) -> list[dict]:
        params = {'search': title[:300], 'per-page': 5}
        if self.email:
            params['mailto'] = self.email
        resp = self.get('https://api.openalex.org/works', params=params)
        resp.raise_for_status()
        return [{'doi': item['doi'].replace('https://doi.org/', '').lower(),
                 'title': item['title']}
                for item in resp.json().get('results', [])
                if item.get('doi') and item.get('title')]

    def search_title(self, title: str, threshold: float) -> dict | None:
        """Return the closest title match at or above the threshold."""
        for search in (self.search_crossref, self.search_openalex):
            best, best_ratio = None, threshold
            for item in search(title):
                ratio = title_ratio(title, item['title'])
                if ratio >= best_ratio:
                    best, best_ratio = item, ratio
            if best:
                return best
        return None


def title_ratio(first: str, second: str) -> float:
    return difflib.SequenceMatcher(None, normalize_text(first), normalize_text(second)).ratio()


def title_matches(title: str, guesses: list[str], context: str, threshold: float) -> bool:
    """Check a registered title against title guesses and the paper's text."""
    if any(title_ratio(title, guess) >= threshold for guess in guesses):
        return True
    normalized = normalize_text(title)
    return len(normalized) >= 15 and f' {normalized} ' in f' {context} '


def doi_candidates(pdf: dict, md_head: str) -> list[tuple[str, str]]:
    candidates = []
    for method, dois in (
            ('metadata', pdf['meta_dois']),
            ('pdf-text', find_dois(pdf['text'])),
            ('markdown', find_dois(md_head)),
            ('arxiv-id', find_arxiv_dois(pdf['text'] + '\n' + md_head))):
        candidates.extend((doi, method) for doi in dois
                          if doi not in {c[0] for c in candidates})
    return candidates


def identify(pdf_path: Path, md_path: Path | None, resolver: Resolver,
             args: argparse.Namespace) -> dict:
    """Identify one PDF, returning a record with a status of found or unresolved."""
    pdf = read_pdf(pdf_path, args.pages)
    md_head = read_markdown_head(md_path)
    guesses = markdown_titles(md_head)
    if pdf['meta_title'] and pdf['meta_title'] not in guesses:
        guesses.append(pdf['meta_title'])
    context = normalize_text(pdf['text'] + '\n' + md_head)
    rejected = []
    for doi, method in doi_candidates(pdf, md_head)[:args.max_candidates]:
        found = resolver.lookup(doi)
        if found is None:
            rejected.append(f'{doi} (does not resolve)')
        elif title_matches(found['title'], guesses, context, args.threshold):
            return {'status': 'found', 'method': method, **found}
        elif method == 'metadata' and not context:
            return {'status': 'found', 'method': 'metadata-unverified', **found}
        else:
            rejected.append(f'{doi} (title mismatch: {found["title"][:80]})')
    for guess in guesses:
        found = resolver.search_title(guess, args.threshold)
        if found:
            return {'status': 'found', 'method': 'title-search', **found}
    return {'status': 'unresolved', 'guesses': guesses, 'rejected': rejected,
            'has_text': bool(context)}


def list_pdfs(root: Path, recurse: bool) -> list[Path]:
    paths = root.rglob('*') if recurse else root.iterdir()
    return sorted(p for p in paths if p.is_file() and p.suffix.lower() == '.pdf')


def index_markdown(md_dirs: list[Path], suffix: str) -> dict[str, list[Path]]:
    index = {}
    for md_dir in md_dirs:
        for path in md_dir.rglob(f'*{suffix}'):
            index.setdefault(path.name, []).append(path)
    return index


def load_cache(path: Path | None) -> dict:
    if path and path.is_file():
        return json.loads(path.read_text())
    return {}


def save_cache(path: Path | None, cache: dict):
    if path:
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(cache, indent=1))
        tmp.replace(path)


def write_unresolved(fptr, pdf_path: Path, record: dict):
    fptr.write(f'# unresolved: {pdf_path}\n')
    if not record.get('has_text'):
        fptr.write('#   no text found in the PDF or markdown\n')
    for guess in record.get('guesses', []):
        fptr.write(f'#   title guess: {guess}\n')
    for reject in record.get('rejected', []):
        fptr.write(f'#   rejected: {reject}\n')
    fptr.flush()


def process(args: argparse.Namespace, out, unresolved_out) -> dict:
    pdf_root = Path(args.pdf_dir)
    md_dirs = [Path(d) for d in args.markdown_dir or []]
    md_index = index_markdown(md_dirs, args.suffix)
    resolver = Resolver(args.email, args.delay)
    cache_path = Path(args.cache) if args.cache else None
    cache = load_cache(cache_path)
    pdfs = list_pdfs(pdf_root, args.recurse)
    counts = {'found': 0, 'duplicate': 0, 'unresolved': 0, 'error': 0, 'cached': 0}
    written = set()
    try:
        for idx, pdf_path in enumerate(pdfs, 1):
            logger.info('[%d/%d] %s', idx, len(pdfs), pdf_path)
            key = file_hash(pdf_path)
            record = cache.get(key)
            if record and (record['status'] == 'found' or not args.retry_unresolved):
                counts['cached'] += 1
            else:
                md_path = find_markdown(pdf_path, pdf_root, md_dirs, args.suffix, md_index)
                logger.debug('markdown for %s: %s', pdf_path.name, md_path)
                try:
                    record = identify(pdf_path, md_path, resolver, args)
                except requests.RequestException as exc:
                    logger.warning('Network error on %s: %s', pdf_path, exc)
                    counts['error'] += 1
                    continue
                cache[key] = record
                if idx % 25 == 0:
                    save_cache(cache_path, cache)
            if record['status'] != 'found':
                counts['unresolved'] += 1
                write_unresolved(unresolved_out, pdf_path, record)
            elif record['doi'] in written:
                counts['duplicate'] += 1
                logger.info('Duplicate of %s: %s', record['doi'], pdf_path)
            else:
                counts['found'] += 1
                written.add(record['doi'])
                out.write(f'{record["doi"]}  # {record["method"]}: {pdf_path}\n')
                out.flush()
    finally:
        save_cache(cache_path, cache)
    return counts


def main():
    parser = argparse.ArgumentParser(
        description='Find the DOIs of downloaded PDFs.  The output can be passed '
        'to "paper_search.py acknowledge --dois-file" so those papers are '
        'excluded from future searches.')
    parser.add_argument('pdf_dir', help='Directory of PDFs.')
    parser.add_argument(
        '--recurse', '-r', action='store_true', help='Recurse into the PDF directory.')
    parser.add_argument(
        '--markdown-dir', '--md', action='append', metavar='DIR',
        help='Directory of markdown conversions from pdfs_to_markdown.py.  Files '
        'beside the PDFs, directly in this directory, or in a mirrored '
        'subdirectory tree are found.  May be repeated.')
    parser.add_argument(
        '--suffix', '--ext', default='.description.md',
        help='Suffix of the markdown conversions.  Default %(default)s.')
    parser.add_argument(
        '--output', '-o', help='File to write DOIs to.  Default is stdout.')
    parser.add_argument(
        '--unresolved',
        help='File to write unresolved papers to.  By default, they are written '
        'as comments in the main output.')
    parser.add_argument(
        '--cache',
        help='JSON file of results keyed by file hash, so later runs skip files '
        'that were already processed.')
    parser.add_argument(
        '--retry-unresolved', action='store_true',
        help='Reprocess files that the cache lists as unresolved.')
    parser.add_argument(
        '--email',
        help='Contact email sent to Crossref and OpenAlex for their polite pools.')
    parser.add_argument(
        '--threshold', type=float, default=0.9,
        help='Minimum title similarity (0-1) to accept a match.  Default %(default)s.')
    parser.add_argument(
        '--pages', type=int, default=2,
        help='Number of PDF pages to search for DOIs.  Default %(default)s.')
    parser.add_argument(
        '--max-candidates', type=int, default=6,
        help='Maximum number of DOI candidates to check per file.  Default %(default)s.')
    parser.add_argument(
        '--delay', type=float, default=0.2,
        help='Minimum seconds between web requests.  Default %(default)s.')
    parser.add_argument(
        '--verbose', '-v', action='count', default=0, help='Increase verbosity.')
    args = parser.parse_args()
    logging.basicConfig(
        level=max(logging.DEBUG, logging.WARNING - 10 * args.verbose),
        format='%(levelname)s: %(message)s')
    logger.setLevel(max(logging.DEBUG, logging.WARNING - 10 * args.verbose))
    with contextlib.ExitStack() as stack:
        out = (stack.enter_context(open(args.output, 'w', encoding='utf-8'))
               if args.output else sys.stdout)
        unresolved_out = (stack.enter_context(open(args.unresolved, 'w', encoding='utf-8'))
                          if args.unresolved else out)
        try:
            counts = process(args, out, unresolved_out)
        except KeyboardInterrupt:
            print('Interrupted', file=sys.stderr)
            sys.exit(130)
    print(', '.join(f'{k}: {v}' for k, v in counts.items()), file=sys.stderr)


if __name__ == '__main__':
    main()
