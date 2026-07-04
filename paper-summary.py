# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx",
#   "openai",
#   "pymupdf",
# ]
# ///

import argparse
import json
import os
import re
import sys
from urllib.parse import quote

import httpx
import openai
import pymupdf

DEFAULT_ENDPOINT = 'http://localhost:11434/v1'
DEFAULT_MAX_CHARS = 262144
DEFAULT_API_KEY = 'ollama'
TARGET_MIN = 2000
TARGET_MAX = 4000
MAX_RETRIES = 5

SYSTEM_PROMPT = """You are an academic research assistant. When given the text
of a scholarly document, you produce a structured markdown summary. You follow
instructions precisely regarding output length and format. You never use
emojis, slang, or metaphors."""

USER_PROMPT_TEMPLATE = """Summarize the following document and return a JSON
document. Include exactly these fields:
- "Title": title of the paper
- "Authors": full names of all authors
- "Source": name of the journal or conference proceedings; an empty string if unknown
- "Date": year and month if available, in format YYYY-MM; an empty string if unknown
- "DOI": DOI if present; an empty string if unknown
- "Reference": full Chicago Manual of Style Notes-Bibliography format
  reference for this paper; an empty string if unknown
- "Summary": a prose summary, not bullet points, describing the document's
  purpose, methods, results, and conclusions in sufficient detail to be
  useful; if more than four or five sentences, this may be multiple
  paragraphs.

The entire output must be between {min_chars} and {max_chars} characters. Do
not truncate any section to meet this limit; instead balance the length
across all sections, with the summary being the longest section.  It must be
valid JSON with the 7 specified keys and values.  As an example, the entire
result would be `{{"Title": "Example", "Authors": "Example",
"Source": "Example", "Date": "1234-56", "DOI": "Example",
"Reference": "Example".  "Summary": "Example"}}`, except with real
information rather than Example and 1234-56.

Document text:
{document_text}"""

RETRY_PROMPT_TEMPLATE = """Your previous response was {actual} characters long, but the required length is between {min_chars} and {max_chars} characters. Please rewrite your response so that it falls within that range. {direction}Keep all required sections. The entire output must be between {min_chars} and {max_chars} characters."""  # noqa


def extract_text(pdf_path: str, max_chars: int) -> str:
    document = pymupdf.open(pdf_path)
    pages = [page.get_text() for page in document]
    document.close()
    full_text = '\n'.join(pages)
    return full_text[:max_chars]


def distance_from_target(length: int) -> float:
    if TARGET_MIN <= length <= TARGET_MAX:
        return 0.0
    return min(abs(length - TARGET_MIN), abs(length - TARGET_MAX))


def build_initial_messages(document_text: str) -> list[dict]:
    user_content = USER_PROMPT_TEMPLATE.format(
        min_chars=TARGET_MIN,
        max_chars=TARGET_MAX,
        document_text=document_text,
    )
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]


def lookup_doi(reference: str, title: str = '', author: str = '', source: str = '') -> str:
    if re.match(r'[a-f0-9]{8}-[A-Za-z]+\.[0-9]{4}\.[0-9]+', os.path.basename(source)):
        return '10.1109/' + os.path.basename(source)[9:-4].upper()
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        queries = []
        if title and author:
            queries.append(
                'https://api.crossref.org/works'
                f'?query.title={quote(title)}&query.author={quote(author)}&rows=5',
            )
        if title:
            queries.append(
                'https://api.crossref.org/works'
                f'?query.title={quote(title)}&rows=5',
            )
        if reference:
            queries.append(
                'https://api.crossref.org/works'
                f'?query.bibliographic={quote(reference)}&rows=5',
            )

        for url in queries:
            response = client.get(
                url,
                headers={'User-Agent': 'paper-summary-script/1.0 (mailto:your-email@example.com)'},
            )
            response.raise_for_status()
            items = response.json().get('message', {}).get('items', [])
            for item in items:
                doi = item.get('DOI', '')
                item_title = ' '.join(item.get('title', []))
                if doi and (not title or title.lower() in item_title.lower() or
                            item_title.lower() in title.lower()):
                    return doi
    return ''


def get_parts(response):
    try:
        response = response[response.index('{'):response.rindex('}') + 1]
        parts = json.loads(response)
        if len(set(parts)) != 7:
            return None, None
        if parts['DOI'] and not re.match(r'[0-9]', parts['DOI']):
            parts['DOI'] = ''
        summary = (
            f'**Title**: {parts["Title"]}\n\n'
            f'**Authors**: {parts["Authors"]}\n\n'
            f'**Source**: {parts["Source"]}\n\n'
            f'**Date**: {parts["Date"]}\n\n'
            f'**DOI**: {parts["DOI"]}\n\n'
            f'**Reference**: {parts["Reference"]}\n\n'
            f'**Summary**:\n\n{parts["Summary"]}\n')
        return parts, summary
    except Exception as exc:
        print(str(exc)[:38], response[:40])
        return None, None


def check_doi(parts, summary, src):
    if 'DOI' in parts and re.match(r'[0-9]', parts.get('DOI')) is None and parts.get('Reference'):
        sys.stderr.write('doi ')
        sys.stderr.flush()
        try:
            doi = lookup_doi(
                reference=parts['Reference'], title=parts.get('Title', ''),
                author=parts.get('Authors', ''), source=str(src))
        except Exception:
            doi = None
        if doi:
            summary = summary.replace('**DOI**:', f'**DOI**: {doi}')
        else:
            sys.stderr.write('x ')
    return summary


def build_retry_messages(
    messages: list[dict], previous_response: str, actual_length: int | None,
) -> list[dict]:
    if actual_length is None:
        retry_content = 'Your previous response did not match the requested format. There should be a "Title", "Authors", "Source", "Date", "DOI", "Reference", and "Summary".  Please rewrite your response so that it matches the requested format.'  # noqa
    else:
        if actual_length < TARGET_MIN:
            direction = 'Your response was too short; expand the summary section. '
        else:
            direction = 'Your response was too long; shorten the summary section. '
        retry_content = RETRY_PROMPT_TEMPLATE.format(
            actual=actual_length,
            min_chars=TARGET_MIN,
            max_chars=TARGET_MAX,
            direction=direction,
        )
    return messages + [
        {'role': 'assistant', 'content': previous_response},
        {'role': 'user', 'content': retry_content},
    ]


def query_llm(client: openai.OpenAI | None, model: str, messages: list[dict]) -> str:
    num_ctx = min((len(str(messages)) + TARGET_MAX * 2) // 2, 128 * 1024)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=TARGET_MAX * 2,
        extra_body={'num_ctx': num_ctx},
        response_format={'type': 'json_object'},
    )
    return response.choices[0].message.content.replace('\r', '\n')


def get_summary(client: openai.OpenAI | None, model: str, document_text: str) -> str:
    messages = build_initial_messages(document_text)
    best_response = None
    best_distance = float('inf')
    for attempt in range(MAX_RETRIES * 5):
        if attempt >= MAX_RETRIES and best_response is not None:
            break
        sys.stderr.write(f'query ({len(str(messages))})')
        sys.stderr.flush()
        response = query_llm(client, model, messages)
        sys.stderr.write('. ')
        sys.stderr.flush()
        parts, summary = get_parts(response)
        if parts is None:
            messages = build_retry_messages(messages, response, None)
            sys.stderr.write('f ')
            continue
        response_length = len(response)
        distance = distance_from_target(response_length)
        if distance < best_distance:
            best_distance = distance
            best_response = parts, summary
        if distance < 100:
            return best_response
        messages = build_retry_messages(messages, response, response_length)
        sys.stderr.write('< ' if distance < TARGET_MIN else '> ')
    return best_response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Summarize a PDF document using an LLM via an OpenAI-compatible endpoint.',
    )
    parser.add_argument(
        'pdf_path',
        help='Path to the PDF file to summarize.  If a directory, all pdfs '
        'in this directory will be processed in alphabetical order if a '
        'matching output file does not yet exist.',
    )
    parser.add_argument(
        '--model', '-m',
        required=True,
        help='Name of the LLM model to use.',
    )
    parser.add_argument(
        '--output', '-o',
        default=None,
        help='Path to the output file. Defaults to stdout.  If a directory '
        'is used as an input, this should be a directory.',
    )
    parser.add_argument(
        '--endpoint',
        default=DEFAULT_ENDPOINT,
        help=f'Base URL of the OpenAI-compatible API endpoint. Defaults to {DEFAULT_ENDPOINT}.',
    )
    parser.add_argument(
        '--api-key',
        default=DEFAULT_API_KEY,
        help="API key for the endpoint. Defaults to 'ollama'.",
    )
    parser.add_argument(
        '--max-chars',
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f'Maximum number of characters of PDF text to send to the LLM. Defaults to {DEFAULT_MAX_CHARS}.',  # noqa
    )
    parser.add_argument(
        '--reverse', '-r', action='store_true', default=False,
        help='Process in reverse order')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = openai.OpenAI(base_url=args.endpoint, api_key=args.api_key, timeout=86400)
    srcs = [args.pdf_path]
    if os.path.isdir(args.pdf_path):
        srcs = sorted([os.path.join(args.pdf_path, s)
                       for s in os.listdir(args.pdf_path)
                       if s.lower().endswith('.pdf')], reverse=args.reverse)
    for src in srcs:
        dest = args.output
        if dest is not None and os.path.isdir(dest):
            dest = os.path.join(dest, os.path.basename(src).rsplit('.', 1)[0] + '.md')
            if os.path.exists(dest):
                continue
        if len(srcs) > 1:
            sys.stdout.write(f'{src}\n')
        sys.stderr.write('extract ')
        sys.stderr.flush()
        document_text = extract_text(src, args.max_chars)
        parts, summary = get_summary(client, args.model, document_text)
        summary = check_doi(parts, summary, src)
        if dest:
            with open(dest, 'w', encoding='utf-8', newline='\n') as output_file:
                output_file.write(summary)
        else:
            sys.stdout.write(summary)
        sys.stderr.write('\n')


if __name__ == '__main__':
    main()
