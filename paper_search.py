# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "paperscraper>=0.3",
#     "pypaperretriever",
#     "pyalex>=0.14",
#     "requests>=2.31",
#     "pyyaml>=6",
#     "ratelimit>=2.2",
# ]
# ///
"""Discover, rank, and download scientific papers from multiple academic archives."""

import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from ratelimit import limits, sleep_and_retry

logging.getLogger('paperscraper.load_dumps').setLevel(logging.WARNING + 1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('paper_search')


@dataclass
class Paper:
    title: str
    authors: list[str]
    doi: str | None
    url: str | None
    abstract: str | None
    source: str
    published_date: str | None
    citation_count: int = 0
    is_open_access: bool = False
    is_peer_reviewed: bool = False
    pdf_url: str | None = None
    relevance_score: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def identity(self) -> str:
        if self.doi:
            return f'doi:{self.doi}'
        normalized = self.title.strip().lower()
        return f'title:{hashlib.sha256(normalized.encode()).hexdigest()}'


class PaperDatabase:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.create_tables()

    def create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen_papers (
                identity TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                doi TEXT,
                source TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                search_name TEXT,
                reported INTEGER DEFAULT 0,
                acknowledged INTEGER DEFAULT 0,
                downloaded INTEGER DEFAULT 0,
                pdf_path TEXT,
                score REAL DEFAULT 0.0,
                metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_seen_search
                ON seen_papers(search_name, reported);
            CREATE INDEX IF NOT EXISTS idx_seen_doi
                ON seen_papers(doi);
            CREATE TABLE IF NOT EXISTS download_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity TEXT NOT NULL,
                attempted TEXT NOT NULL,
                success INTEGER NOT NULL,
                pdf_path TEXT,
                error TEXT,
                FOREIGN KEY (identity) REFERENCES seen_papers(identity)
            );

            CREATE TABLE IF NOT EXISTS report_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_name TEXT NOT NULL,
                reported_at TEXT NOT NULL,
                paper_count INTEGER NOT NULL,
                identities TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def is_known(self, identity: str) -> bool:
        row = self.conn.execute(
            'SELECT 1 FROM seen_papers WHERE identity = ?', (identity,),
        ).fetchone()
        return row is not None

    def add_paper(self, paper: Paper, search_name: str, score: float):
        if self.is_known(paper.identity):
            return False
        self.conn.execute(
            """INSERT INTO seen_papers
               (identity, title, doi, source, first_seen, search_name, score, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.identity,
                paper.title,
                paper.doi,
                paper.source,
                datetime.now(UTC).isoformat(),
                search_name,
                score,
                json.dumps({
                    'authors': paper.authors,
                    'abstract': paper.abstract,
                    'url': paper.url,
                    'pdf_url': paper.pdf_url,
                    'citation_count': paper.citation_count,
                    'is_open_access': paper.is_open_access,
                    'is_peer_reviewed': paper.is_peer_reviewed,
                    'published_date': paper.published_date,
                    'relevance_score': paper.relevance_score,
                    'extra': paper.extra,
                }),
            ),
        )
        self.conn.commit()
        return True

    def get_unreported(self, search_name: str, limit: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM seen_papers
               WHERE search_name = ? AND reported = 0 AND acknowledged = 0
               ORDER BY score DESC
               LIMIT ?""",
            (search_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reported(self, identities: list[str], search_name: str):
        now = datetime.now(UTC).isoformat()
        for identity in identities:
            self.conn.execute(
                'UPDATE seen_papers SET reported = 1 WHERE identity = ?',
                (identity,),
            )
        self.conn.execute(
            'INSERT INTO report_log (search_name, reported_at, paper_count, '
            'identities) VALUES (?, ?, ?, ?)',
            (search_name, now, len(identities), json.dumps(identities)),
        )
        self.conn.commit()

    def mark_acknowledged(self, identity: str):
        self.conn.execute(
            'UPDATE seen_papers SET acknowledged = 1 WHERE identity = ?',
            (identity,),
        )
        self.conn.commit()

    def mark_downloaded(self, identity: str, pdf_path: str, success: bool, error: str = None):
        now = datetime.now(UTC).isoformat()
        if success:
            self.conn.execute(
                'UPDATE seen_papers SET downloaded = 1, pdf_path = ? WHERE identity = ?',
                (pdf_path, identity),
            )
        self.conn.execute(
            'INSERT INTO download_log (identity, attempted, success, '
            'pdf_path, error) VALUES (?, ?, ?, ?, ?)',
            (identity, now, int(success), pdf_path, error),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


def compute_score(paper: Paper, config: dict) -> float:
    weights = config.get('scoring', {})
    w_relevance = weights.get('relevance_weight', 0.35)
    w_citations = weights.get('citation_weight', 0.20)
    w_access = weights.get('access_weight', 0.25)
    w_review = weights.get('peer_review_weight', 0.15)
    w_recency = weights.get('recency_weight', 0.05)
    relevance_component = paper.relevance_score
    citation_log = 0.0
    if paper.citation_count > 0:
        import math
        citation_log = math.log1p(paper.citation_count) / math.log1p(1000)
        citation_log = min(citation_log, 1.0)
    access_component = 1.0 if paper.is_open_access else 0.2

    review_component = 0.0
    if paper.is_peer_reviewed:
        review_component = 1.0
    elif paper.source in ('arxiv', 'biorxiv', 'medrxiv', 'chemrxiv'):
        review_component = 0.5
    recency_component = 0.0
    if paper.published_date:
        try:
            pub = datetime.fromisoformat(paper.published_date[:10]).replace(tzinfo=timezone.utc)
            days_old = (datetime.now(UTC) - pub).days
            recency_component = max(0.0, 1.0 - days_old / 365.0)
        except ValueError:
            pass
    score = (
        w_relevance * relevance_component +
        w_citations * citation_log +
        w_access * access_component +
        w_review * review_component +
        w_recency * recency_component
    )
    return round(score, 6)


class ArchiveBackend(ABC):
    name: str = 'base'

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        ...


class ArxivBackend(ArchiveBackend):
    name = 'arxiv'

    @sleep_and_retry
    @limits(calls=1, period=3)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        import tempfile

        from paperscraper.arxiv import get_and_dump_arxiv_papers
        papers = []
        with tempfile.NamedTemporaryFile(suffix='.jsonl', delete=False, mode='w') as tmp:
            tmp_path = tmp.name
        try:
            get_and_dump_arxiv_papers(
                query_terms,
                output_filepath=tmp_path,
                max_results=max_results,
            )
            with open(tmp_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    paper = Paper(
                        title=rec.get('title', ''),
                        authors=rec.get('authors', []),
                        doi=rec.get('doi'),
                        url=rec.get('url'),
                        abstract=rec.get('abstract'),
                        source='arxiv',
                        published_date=rec.get('date'),
                        citation_count=0,
                        is_open_access=True,
                        is_peer_reviewed=False,
                        pdf_url=rec.get('url', '').replace(
                            '/abs/', '/pdf/') if rec.get('url') else None,
                        relevance_score=1.0,
                    )
                    papers.append(paper)
        except Exception as exc:
            log.error('arxiv search failed: %s', exc)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return papers


class PubmedBackend(ArchiveBackend):
    name = 'pubmed'

    @sleep_and_retry
    @limits(calls=3, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        import tempfile

        from paperscraper.pubmed import get_and_dump_pubmed_papers
        papers = []
        with tempfile.NamedTemporaryFile(suffix='.jsonl', delete=False, mode='w') as tmp:
            tmp_path = tmp.name
        try:
            get_and_dump_pubmed_papers(
                query_terms,
                output_filepath=tmp_path,
            )
            with open(tmp_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    paper = Paper(
                        title=rec.get('title', ''),
                        authors=rec.get('authors', []),
                        doi=rec.get('doi'),
                        url=f'https://pubmed.ncbi.nlm.nih.gov/{rec["pubmed_id"]}/' if rec.get(
                            'pubmed_id') else None,
                        abstract=rec.get('abstract'),
                        source='pubmed',
                        published_date=rec.get('date'),
                        citation_count=0,
                        is_open_access=False,
                        is_peer_reviewed=True,
                        relevance_score=0.9,
                        extra={'pubmed_id': rec.get('pubmed_id')},
                    )
                    papers.append(paper)
        except Exception as exc:
            log.error('pubmed search failed: %s', exc)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return papers[:max_results]


class BiorxivBackend(ArchiveBackend):
    name = 'biorxiv'

    @sleep_and_retry
    @limits(calls=1, period=5)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        flat_terms = []
        for group in query_terms:
            flat_terms.extend(group)
        try:
            base_url = 'https://api.biorxiv.org/details/biorxiv'
            yesterday = (datetime.now(UTC) - timedelta(days=30)).strftime('%Y-%m-%d')
            today = datetime.now(UTC).strftime('%Y-%m-%d')
            url = f'{base_url}/{yesterday}/{today}/0/100'
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('collection', []):
                title_lower = (rec.get('title', '') + ' ' + rec.get('abstract', '')).lower()
                if all(any(t in title_lower for t in group_lower)
                       for group_lower in [[t.lower() for t in g] for g in query_terms]):
                    doi = rec.get('doi')
                    paper = Paper(
                        title=rec.get('title', ''),
                        authors=rec.get('authors', '').split('; ') if rec.get('authors') else [],
                        doi=doi,
                        url=f'https://doi.org/{doi}' if doi else None,
                        abstract=rec.get('abstract'),
                        source='biorxiv',
                        published_date=rec.get('date'),
                        citation_count=0,
                        is_open_access=True,
                        is_peer_reviewed=False,
                        pdf_url=(
                            f'https://www.biorxiv.org/content/{doi}v1.full.pdf'
                            if doi else None,
                        ),
                        relevance_score=0.85,
                    )
                    papers.append(paper)
        except Exception as exc:
            log.error('biorxiv search failed: %s', exc)
        return papers[:max_results]


class MedrxivBackend(ArchiveBackend):
    name = 'medrxiv'

    @sleep_and_retry
    @limits(calls=1, period=5)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        try:
            base_url = 'https://api.biorxiv.org/details/medrxiv'
            yesterday = (datetime.now(UTC) - timedelta(days=30)).strftime('%Y-%m-%d')
            today = datetime.now(UTC).strftime('%Y-%m-%d')
            url = f'{base_url}/{yesterday}/{today}/0/100'
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('collection', []):
                title_lower = (rec.get('title', '') + ' ' + rec.get('abstract', '')).lower()
                if all(any(t.lower() in title_lower for t in group) for group in query_terms):
                    doi = rec.get('doi')
                    paper = Paper(
                        title=rec.get('title', ''),
                        authors=rec.get('authors', '').split('; ') if rec.get('authors') else [],
                        doi=doi,
                        url=f'https://doi.org/{doi}' if doi else None,
                        abstract=rec.get('abstract'),
                        source='medrxiv',
                        published_date=rec.get('date'),
                        citation_count=0,
                        is_open_access=True,
                        is_peer_reviewed=False,
                        pdf_url=(
                            f'https://www.medrxiv.org/content/{doi}v1.full.pdf'
                            if doi else None,
                        ),
                        relevance_score=0.85,
                    )
                    papers.append(paper)
        except Exception as exc:
            log.error('medrxiv search failed: %s', exc)
        return papers[:max_results]


class ChemrxivBackend(ArchiveBackend):
    name = 'chemrxiv'
    _logged_api_key_warning = False

    @sleep_and_retry
    @limits(calls=1, period=5)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        flat_terms = []
        for group in query_terms:
            flat_terms.extend(group)
        query_string = ' AND '.join(flat_terms)
        try:
            # Check if we have been rate-limited / need auth
            url = 'https://chemrxiv.org/engage/chemrxiv/public-api/v1/items'
            params = {'term': query_string, 'limit': min(max_results, 50)}
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 403:
                if not ChemrxivBackend._logged_api_key_warning:
                    log.warning(
                        'Chemrxiv search returned 403 (Forbidden). '
                        'This backend may require an api_key in your config; '
                        'skipping chemrxiv for this session.',
                    )
                    ChemrxivBackend._logged_api_key_warning = True
                return papers  # Return empty results instead of retrying
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('itemHits', []):
                item = rec.get('item', {})
                doi = item.get('doi')
                paper = Paper(
                    title=item.get('title', ''),
                    authors=[a.get('name', '') for a in item.get('authors', [])],
                    doi=doi,
                    url=f'https://doi.org/{doi}' if doi else None,
                    abstract=item.get('abstract'),
                    source='chemrxiv',
                    published_date=item.get('publishedDate', '')[
                        :10] if item.get('publishedDate') else None,
                    citation_count=0,
                    is_open_access=True,
                    is_peer_reviewed=False,
                    relevance_score=0.85,
                )
                papers.append(paper)
        except Exception as exc:
            log.error('chemrxiv search failed: %s', exc)
        return papers[:max_results]


class OpenAlexBackend(ArchiveBackend):
    name = 'openalex'

    @sleep_and_retry
    @limits(calls=10, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        from pyalex import Works
        from pyalex import config as pyalex_config
        api_key = self.config.get('api_key')
        email = self.config.get('email')

        if api_key:
            pyalex_config.api_key = api_key
        if email:
            pyalex_config.email = email
        pyalex_config.max_retries = 3
        pyalex_config.retry_backoff_factor = 0.5
        pyalex_config.retry_http_codes = [429, 500, 503]

        flat_terms = []
        for group in query_terms:
            flat_terms.append('(' + ' OR '.join(f'"{t}"' for t in group) + ')')
        search_string = ' AND '.join(flat_terms)
        papers = []
        try:
            results = (
                Works()
                .search(search_string)
                .sort(cited_by_count='desc')
                .get(per_page=min(max_results, 200))
            )
            for rec in results:
                oa = rec.get('open_access', {})
                doi_raw = rec.get('doi', '')
                doi = doi_raw.replace('https://doi.org/', '') if doi_raw else None
                best_oa_url = rec.get('best_oa_location', {})
                pdf_url = None
                if best_oa_url:
                    pdf_url = best_oa_url.get('pdf_url') or best_oa_url.get('landing_page_url')
                authorships = rec.get('authorships', [])
                author_names = []
                for a in authorships:
                    name = a.get('author', {}).get('display_name')
                    if name:
                        author_names.append(name)
                pub_date = rec.get('publication_date')
                cite_count = rec.get('cited_by_count', 0)
                is_oa = oa.get('is_oa', False)
                paper_type = rec.get('type', '')
                is_reviewed = paper_type in ('journal-article', 'proceedings-article')
                abstract_index = rec.get('abstract_inverted_index')
                abstract_text = None
                if abstract_index and isinstance(abstract_index, dict):
                    word_positions = []
                    for word, positions in abstract_index.items():
                        for pos in positions:
                            word_positions.append((pos, word))
                    word_positions.sort()
                    abstract_text = ' '.join(w for _, w in word_positions)
                paper = Paper(
                    title=rec.get('title', '') or '',
                    authors=author_names,
                    doi=doi,
                    url=doi_raw or rec.get('id'),
                    abstract=abstract_text,
                    source='openalex',
                    published_date=pub_date,
                    citation_count=cite_count,
                    is_open_access=is_oa,
                    is_peer_reviewed=is_reviewed,
                    pdf_url=pdf_url,
                    relevance_score=0.9,
                    extra={
                        'openalex_id': rec.get('id'),
                        'type': paper_type,
                    },
                )
                papers.append(paper)
        except Exception as exc:
            log.error('openalex search failed: %s', exc)
        return papers


class SemanticScholarBackend(ArchiveBackend):
    name = 'semantic_scholar'

    @sleep_and_retry
    @limits(calls=1, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        flat_terms = []
        for group in query_terms:
            flat_terms.extend(group)
        query_string = ' '.join(flat_terms)
        try:
            url = 'https://api.semanticscholar.org/graph/v1/paper/search'
            params = {
                'query': query_string,
                'limit': min(max_results, 100),
                'fields': (
                    'title,authors,abstract,doi,url,'
                    'year,citationCount,isOpenAccess,'
                    'openAccessPdf,publicationTypes'
                ),
            }
            api_key = self.config.get('api_key')
            headers = {}
            if api_key:
                headers['x-api-key'] = api_key
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('data', []):
                doi = rec.get('doi')
                oa_pdf = rec.get('openAccessPdf')
                pdf_url = oa_pdf.get('url') if oa_pdf else None
                pub_types = rec.get('publicationTypes') or []
                is_reviewed = 'JournalArticle' in pub_types or 'Conference' in pub_types
                paper = Paper(
                    title=rec.get('title', ''),
                    authors=[a.get('name', '') for a in rec.get('authors', [])],
                    doi=doi,
                    url=rec.get('url'),
                    abstract=rec.get('abstract'),
                    source='semantic_scholar',
                    published_date=str(rec.get('year')) if rec.get('year') else None,
                    citation_count=rec.get('citationCount', 0),
                    is_open_access=rec.get('isOpenAccess', False),
                    is_peer_reviewed=is_reviewed,
                    pdf_url=pdf_url,
                    relevance_score=0.9,
                )
                papers.append(paper)
        except Exception as exc:
            log.error('semantic_scholar search failed: %s', exc)
        return papers


class CoreBackend(ArchiveBackend):
    name = 'core'

    @sleep_and_retry
    @limits(calls=1, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        api_key = self.config.get('api_key')
        if not api_key:
            log.warning('CORE backend requires an api_key in config; skipping')
            return papers
        flat_terms = []
        for group in query_terms:
            flat_terms.append('(' + ' OR '.join(group) + ')')
        query_string = ' AND '.join(flat_terms)
        try:
            url = 'https://api.core.ac.uk/v3/search/works'
            params = {'q': query_string, 'limit': min(max_results, 100)}
            headers = {'Authorization': f'Bearer {api_key}'}
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('results', []):
                doi = rec.get('doi')
                paper = Paper(
                    title=rec.get('title', ''),
                    authors=rec.get('authors', []) if isinstance(rec.get('authors'), list) else [],
                    doi=doi,
                    url=rec.get('downloadUrl') or rec.get('sourceFulltextUrls', [None])[
                        0] if rec.get('sourceFulltextUrls') else None,
                    abstract=rec.get('abstract'),
                    source='core',
                    published_date=rec.get('publishedDate'),
                    citation_count=rec.get('citationCount', 0),
                    is_open_access=True,
                    is_peer_reviewed=False,
                    pdf_url=rec.get('downloadUrl'),
                    relevance_score=0.8,
                )
                papers.append(paper)
        except Exception as exc:
            log.error('core search failed: %s', exc)
        return papers


class DOAJBackend(ArchiveBackend):
    name = 'doaj'

    @sleep_and_retry
    @limits(calls=1, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        parts = []
        for group in query_terms:
            parts.append('(' + ' OR '.join(f'"{t}"' for t in group) + ')')
        query_string = ' AND '.join(parts)
        try:
            url = 'https://doaj.org/api/search/articles/' + requests.utils.quote(query_string)
            params = {'pageSize': min(max_results, 100)}
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('results', []):
                bibjson = rec.get('bibjson', {})
                doi = None
                for ident in bibjson.get('identifier', []):
                    if ident.get('type') == 'doi':
                        doi = ident.get('id')
                        break
                links = bibjson.get('link', [])
                pdf_url = None
                web_url = None
                for link in links:
                    if link.get('type') == 'fulltext':
                        if link.get('content_type') == 'application/pdf':
                            pdf_url = link.get('url')
                        else:
                            web_url = link.get('url')
                author_names = [a.get('name', '') for a in bibjson.get('author', [])]
                paper = Paper(
                    title=bibjson.get('title', ''),
                    authors=author_names,
                    doi=doi,
                    url=web_url or pdf_url,
                    abstract=bibjson.get('abstract'),
                    source='doaj',
                    published_date=None,
                    citation_count=0,
                    is_open_access=True,
                    is_peer_reviewed=True,
                    pdf_url=pdf_url,
                    relevance_score=0.85,
                )
                papers.append(paper)
        except Exception as exc:
            log.error('doaj search failed: %s', exc)
        return papers


class IEEEBackend(ArchiveBackend):
    name = 'ieee'

    @sleep_and_retry
    @limits(calls=3, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        api_key = self.config.get('api_key')
        if not api_key:
            log.warning('IEEE backend requires an api_key in config; skipping')
            return papers
        parts = []
        for group in query_terms:
            parts.append('(' + ' OR '.join(f'"{t}"' for t in group) + ')')
        query_string = ' AND '.join(parts)
        try:
            url = 'https://ieeexploreapi.ieee.org/api/v1/search/articles'
            params = {
                'apikey': api_key,
                'querytext': query_string,
                'max_records': min(max_results, 200),
                'sort_field': 'article_number',
                'sort_order': 'desc',
            }
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for rec in data.get('articles', []):
                doi = rec.get('doi')
                is_oa = rec.get('access_type', '') == 'OPEN_ACCESS'
                pdf_url_val = rec.get('pdf_url')
                paper = Paper(
                    title=rec.get('title', ''),
                    authors=[a.get('full_name', '')
                             for a in rec.get('authors', {}).get('authors', [])],
                    doi=doi,
                    url=rec.get('html_url'),
                    abstract=rec.get('abstract'),
                    source='ieee',
                    published_date=rec.get('publication_date'),
                    citation_count=rec.get('citing_paper_count', 0),
                    is_open_access=is_oa,
                    is_peer_reviewed=True,
                    pdf_url=pdf_url_val if is_oa else None,
                    relevance_score=0.7 if not is_oa else 0.9,
                )
                papers.append(paper)
        except Exception as exc:
            log.error('ieee search failed: %s', exc)
        return papers


class EuropePMCBackend(ArchiveBackend):
    name = 'europe_pmc'

    @sleep_and_retry
    @limits(calls=3, period=1)
    def search(self, query_terms: list[list[str]], max_results: int) -> list[Paper]:
        papers = []
        parts = []
        for group in query_terms:
            parts.append('(' + ' OR '.join(f'"{t}"' for t in group) + ')')
        query_string = ' AND '.join(parts)
        try:
            url = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
            params = {
                'query': query_string,
                'format': 'json',
                'pageSize': min(max_results, 100),
                'resultType': 'core',
            }
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            result_list = data.get('resultList', {}).get('result', [])
            for rec in result_list:
                doi = rec.get('doi')
                is_oa = rec.get('isOpenAccess', 'N') == 'Y'
                pmcid = rec.get('pmcid')
                pdf_url = None
                if pmcid:
                    base_url = 'https://europepmc.org/backend/ptpmcrender.fcgi'
                    pdf_url = f'{base_url}?accid={pmcid}&blobtype=pdf'
                paper = Paper(
                    title=rec.get('title', ''),
                    authors=[
                        f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                        for a in rec.get('authorList', {}).get('author', [])
                    ],
                    doi=doi,
                    url=f'https://doi.org/{doi}' if doi else None,
                    abstract=rec.get('abstractText'),
                    source='europe_pmc',
                    published_date=rec.get('firstPublicationDate'),
                    citation_count=rec.get('citedByCount', 0),
                    is_open_access=is_oa,
                    is_peer_reviewed=rec.get('source') in ('MED', 'AGR', 'CBA'),
                    pdf_url=pdf_url,
                    relevance_score=0.85,
                    extra={'pmcid': pmcid, 'pmid': rec.get('pmid')},
                )
                papers.append(paper)
        except Exception as exc:
            log.error('europe_pmc search failed: %s', exc)
        return papers


BACKEND_REGISTRY: dict[str, type[ArchiveBackend]] = {
    'arxiv': ArxivBackend,
    'pubmed': PubmedBackend,
    'biorxiv': BiorxivBackend,
    'medrxiv': MedrxivBackend,
    'chemrxiv': ChemrxivBackend,
    'openalex': OpenAlexBackend,
    'semantic_scholar': SemanticScholarBackend,
    'core': CoreBackend,
    'doaj': DOAJBackend,
    'ieee': IEEEBackend,
    'europe_pmc': EuropePMCBackend,
}


class PaperDownloader:
    def __init__(
        self,
        download_dir: Path,
        min_free_bytes: int,
        email: str,
        allow_scihub: bool = False,
    ):
        self.download_dir = download_dir
        self.min_free_bytes = min_free_bytes
        self.email = email
        self.allow_scihub = allow_scihub
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def has_disk_space(self) -> bool:
        usage = shutil.disk_usage(self.download_dir)
        return usage.free > self.min_free_bytes

    def download(self, paper: Paper) -> tuple[bool, str | None, str | None]:
        if not self.has_disk_space():
            return False, None, 'insufficient disk space'
        safe_title = ''.join(
            c if c.isalnum() or c in ' -_' else '_' for c in paper.title[:80]).strip()
        filename = f'{safe_title}.pdf'
        filepath = self.download_dir / filename
        if filepath.exists():
            return True, str(filepath), None
        if paper.pdf_url:
            success, error = self.download_from_url(paper.pdf_url, filepath)
            if success:
                return True, str(filepath), None
            log.debug('direct pdf_url download failed for %s: %s', paper.title[:60], error)
        if paper.doi:
            success, error = self.download_via_paperscraper(paper.doi, filepath)
            if success:
                return True, str(filepath), None
            log.debug('paperscraper download failed for %s: %s', paper.title[:60], error)
            success, error = self.download_via_pypaperretriever(paper.doi, filepath)
            if success:
                return True, str(filepath), None
            log.debug('pypaperretriever download failed for %s: %s', paper.title[:60], error)
        return False, None, 'all download methods exhausted'

    def download_from_url(self, url: str, filepath: Path) -> tuple[bool, str | None]:
        try:
            resp = requests.get(url, timeout=60, stream=True, headers={
                                'User-Agent': 'paper_search/1.0'})
            resp.raise_for_status()
            content_type = resp.headers.get('content-type', '')
            if 'pdf' not in content_type and not url.endswith('.pdf'):
                return False, f'unexpected content-type: {content_type}'
            with open(filepath, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            if filepath.stat().st_size < 1000:
                filepath.unlink(missing_ok=True)
                return False, 'downloaded file too small'
            return True, None
        except Exception as exc:
            filepath.unlink(missing_ok=True)
            return False, str(exc)

    def download_via_paperscraper(self, doi: str, filepath: Path) -> tuple[bool, str | None]:
        try:
            from paperscraper.pdf import save_pdf
            result = save_pdf({'doi': doi}, filepath=str(filepath))

            if result and filepath.exists() and filepath.stat().st_size > 1000:
                return True, None
            filepath.unlink(missing_ok=True)
            return False, 'save_pdf returned falsy or file too small'
        except Exception as exc:
            filepath.unlink(missing_ok=True)
            return False, str(exc)

    def download_via_pypaperretriever(self, doi: str, filepath: Path) -> tuple[bool, str | None]:
        try:
            from pypaperretriever import PaperRetriever

            retriever = PaperRetriever(
                email=self.email,
                doi=doi,
                download_directory=str(filepath.parent),
                allow_scihub=self.allow_scihub,
            )
            retriever.download()
            downloaded_files = list(filepath.parent.glob(f'*{doi.replace("/", "_")}*'))
            if not downloaded_files:
                downloaded_files = sorted(
                    filepath.parent.glob('*.pdf'),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            success = False
            if downloaded_files:
                newest = downloaded_files[0]
                if newest != filepath and newest.exists():
                    newest.rename(filepath)
                if filepath.exists() and filepath.stat().st_size > 1000:
                    success = True
            filepath.unlink(missing_ok=True)
            for subdir in list(filepath.parent.iterdir()):
                if subdir.name.startswith('doi-') and subdir.is_dir():
                    try:
                        if not any(subdir.iterdir()):
                            shutil.rmtree(str(subdir), ignore_errors=True)
                    except OSError:
                        pass
            if success:
                return True, None
            return False, 'pypaperretriever did not produce a valid file'
        except Exception as exc:
            filepath.unlink(missing_ok=True)
            return False, str(exc)


def load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def build_backends(config: dict) -> list[ArchiveBackend]:
    backends = []
    archives_config = config.get('archives', {})
    for archive_name, archive_conf in archives_config.items():
        if not archive_conf.get('enabled', False):
            continue
        backend_class = BACKEND_REGISTRY.get(archive_name)
        if backend_class is None:
            log.warning('unknown archive backend: %s', archive_name)
            continue
        backends.append(backend_class(archive_conf))
        log.info('enabled backend: %s', archive_name)
    return backends


def parse_search_queries(config: dict) -> list[dict]:
    return config.get('searches', [])


def run_search(
    db: PaperDatabase,
    backends: list[ArchiveBackend],
    search_def: dict,
    config: dict,
):
    search_name = search_def['name']
    query_terms = search_def['terms']
    max_results_per_backend = search_def.get('max_results', 50)
    log.info('running search: %s', search_name)
    all_papers = []
    for backend in backends:
        log.info('  querying %s ...', backend.name)
        try:
            papers = backend.search(query_terms, max_results_per_backend)
            log.info('  %s returned %d papers', backend.name, len(papers))
            all_papers.extend(papers)
        except Exception as exc:
            log.error('  %s raised: %s', backend.name, exc)
    new_count = 0
    for paper in all_papers:
        score = compute_score(paper, config)
        if db.add_paper(paper, search_name, score):
            new_count += 1
    log.info('search "%s": %d total results, %d new papers',
             search_name, len(all_papers), new_count)


def report_top_k(db: PaperDatabase, search_name: str, top_k: int) -> list[dict]:
    unreported = db.get_unreported(search_name, top_k)
    if not unreported:
        log.info('search "%s": no unreported papers', search_name)
        return []
    log.info('search "%s": reporting top %d papers:', search_name, len(unreported))
    for i, row in enumerate(unreported, 1):
        meta = json.loads(row.get('metadata', '{}'))
        oa_marker = 'OA' if meta.get('is_open_access') else 'closed'
        reviewed_marker = 'peer-reviewed' if meta.get('is_peer_reviewed') else 'preprint'
        print(f'  {i:3d}. [{row["source"]}] [{oa_marker}] [{reviewed_marker}] '
              f'(score={row["score"]:.4f}) {row["title"]}')
        if row.get('doi'):
            print(f'       DOI: {row["doi"]}')
    identities = [r['identity'] for r in unreported]
    db.mark_reported(identities, search_name)
    return unreported


def download_top_l(
    db: PaperDatabase,
    downloader: PaperDownloader,
    search_name: str,
    top_l: int,
):
    rows = db.conn.execute(
        """SELECT * FROM seen_papers
           WHERE search_name = ? AND downloaded = 0
           ORDER BY score DESC
           LIMIT ?""",
        (search_name, top_l),
    ).fetchall()
    if not rows:
        log.info('search "%s": no papers to download', search_name)
        return
    for row in rows:
        row = dict(row)
        meta = json.loads(row.get('metadata', '{}'))
        if not meta.get('is_open_access') and not meta.get('pdf_url'):
            log.info('  skipping non-OA paper without pdf_url: %s', row['title'][:60])
            continue
        paper = Paper(
            title=row['title'],
            authors=meta.get('authors', []),
            doi=row.get('doi'),
            url=meta.get('url'),
            abstract=meta.get('abstract'),
            source=row['source'],
            published_date=meta.get('published_date'),
            citation_count=meta.get('citation_count', 0),
            is_open_access=meta.get('is_open_access', False),
            is_peer_reviewed=meta.get('is_peer_reviewed', False),
            pdf_url=meta.get('pdf_url'),
            relevance_score=meta.get('relevance_score', 0.0),
        )
        log.info('  downloading: %s', paper.title[:60])
        success, pdf_path, error = downloader.download(paper)
        db.mark_downloaded(row['identity'], pdf_path or '', success, error)
        if success:
            log.info('    saved to %s', pdf_path)
        else:
            log.warning('    failed: %s', error)
        if not downloader.has_disk_space():
            log.warning('disk space reserve reached; stopping downloads')
            break


def cmd_search(args, config):
    db = PaperDatabase(Path(config['database']))
    backends = build_backends(config)
    searches = parse_search_queries(config)
    top_k = config.get('top_k', 10)
    try:
        for search_def in searches:
            run_search(db, backends, search_def, config)
            report_top_k(db, search_def['name'], top_k)
    finally:
        db.close()


def cmd_download(args, config):
    db = PaperDatabase(Path(config['database']))
    download_conf = config.get('download', {})
    download_dir = Path(download_conf.get('directory', 'papers'))
    min_free_gb = download_conf.get('min_free_gb', 5)
    email = config.get('email', 'user@example.com')
    top_l = download_conf.get('top_l', 5)
    allow_scihub = download_conf.get('allow_scihub', False)
    downloader = PaperDownloader(download_dir, min_free_gb * (1024 ** 3),
                                 email, allow_scihub=allow_scihub)
    searches = parse_search_queries(config)
    try:
        for search_def in searches:
            download_top_l(db, downloader, search_def['name'], top_l)
    finally:
        db.close()


def cmd_acknowledge(args, config):
    db = PaperDatabase(Path(config['database']))
    try:
        for identity in args.identities:
            db.mark_acknowledged(identity)
            log.info('acknowledged: %s', identity)
    finally:
        db.close()


def cmd_list(args, config):
    db = PaperDatabase(Path(config['database']))
    try:
        filter_clause = '1=1'
        params = []
        if args.search_name:
            filter_clause = 'search_name = ?'
            params.append(args.search_name)
        status_filter = ''
        if args.status == 'unreported':
            status_filter = ' AND reported = 0 AND acknowledged = 0'
        elif args.status == 'reported':
            status_filter = ' AND reported = 1'
        elif args.status == 'downloaded':
            status_filter = ' AND downloaded = 1'
        elif args.status == 'acknowledged':
            status_filter = ' AND acknowledged = 1'
        rows = db.conn.execute(
            (
                'SELECT * FROM seen_papers WHERE '
                f'{filter_clause}{status_filter}'
                ' ORDER BY score DESC LIMIT ?'
            ), params + [args.limit],
        ).fetchall()
        for i, row in enumerate(rows, 1):
            row = dict(row)
            flags = []
            if row['reported']:
                flags.append('R')
            if row['acknowledged']:
                flags.append('A')
            if row['downloaded']:
                flags.append('D')
            flag_str = ','.join(flags) if flags else '-'
            score_val = row['score']
            line_a = f'{i:3d}. [{flag_str}] (score={score_val:.4f}) '
            print(line_a + f'[{row["source"]}] {row["title"]}')
            print(f'     id: {row["identity"]}')
    finally:
        db.close()


def cmd_init_config(args, config):
    sample = {
        'database': 'paper_search.db',
        'email': 'your.email@example.com',
        'top_k': 10,
        'scoring': {
            'relevance_weight': 0.35,
            'citation_weight': 0.20,
            'access_weight': 0.25,
            'peer_review_weight': 0.15,
            'recency_weight': 0.05,
        },
        'download': {
            'directory': 'papers',
            'min_free_gb': 5,
            'top_l': 5,
        },
        'archives': {
            'arxiv': {'enabled': True},
            'pubmed': {'enabled': True},
            'biorxiv': {'enabled': True},
            'medrxiv': {'enabled': False},
            'chemrxiv': {'enabled': False},
            'openalex': {
                'enabled': True,
                'api_key': '',
                'email': 'your.email@example.com',
            },
            'semantic_scholar': {
                'enabled': False,
                'api_key': '',
            },
            'core': {
                'enabled': False,
                'api_key': '',
            },
            'doaj': {'enabled': False},
            'ieee': {
                'enabled': False,
                'api_key': '',
            },
            'europe_pmc': {'enabled': False},
        },
        'searches': [
            {
                'name': 'holonomic_traction',
                'terms': [['holonomic'], ['traction']],
                'max_results': 50,
            },
            {
                'name': 'tps_registration',
                'terms': [['thin plate spline'], ['registration']],
                'max_results': 50,
            },
        ],
    }
    out_path = Path(args.output)
    if out_path.exists() and not args.force:
        print(f'{out_path} already exists; use --force to overwrite')
        sys.exit(1)
    with open(out_path, 'w') as f:
        yaml.dump(sample, f, default_flow_style=False, sort_keys=False)
    print(f'wrote sample config to {out_path}')


def main():
    parser = argparse.ArgumentParser(
        description='paper_search: discover, rank, and download scientific papers',
    )
    parser.add_argument(
        '-c', '--config',
        default='paper_search.yaml',
        help='path to YAML config file (default: paper_search.yaml)',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('search', help='run configured searches and report top-k new papers')
    subparsers.add_parser('download', help='download top-l undownloaded papers per search')
    ack_parser = subparsers.add_parser('acknowledge', help='mark papers as acknowledged')
    ack_parser.add_argument('identities', nargs='+', help='paper identity strings to acknowledge')
    list_parser = subparsers.add_parser('list', help='list known papers')
    list_parser.add_argument('-s', '--search-name', default=None, help='filter by search name')

    list_parser.add_argument(
        '--status',
        choices=['all', 'unreported', 'reported', 'downloaded', 'acknowledged'],
        default='all',
    )
    list_parser.add_argument('-n', '--limit', type=int, default=50)
    init_parser = subparsers.add_parser('init', help='generate a sample config file')
    init_parser.add_argument('-o', '--output', default='paper_search.yaml')
    init_parser.add_argument('-f', '--force', action='store_true')
    args = parser.parse_args()
    if args.command == 'init':
        cmd_init_config(args, {})
        return
    config_path = Path(args.config)
    if not config_path.exists():
        print(f'config file not found: {config_path}', file=sys.stderr)
        print('run "paper_search init" to generate a sample config', file=sys.stderr)
        sys.exit(1)
    config = load_config(config_path)

    commands = {
        'search': cmd_search,
        'download': cmd_download,
        'acknowledge': cmd_acknowledge,
        'list': cmd_list,
    }
    commands[args.command](args, config)


if __name__ == '__main__':

    main()
