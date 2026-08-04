# /// script
# requires-python = ">=3.8"
# ///

import argparse
import json
import os
import re
import sys
from pathlib import Path

DIGEST_RE = re.compile(r'sha256:[0-9a-fA-F]{64}')


def extract_digests(obj) -> set[str]:
    digests: set[str] = set()
    if isinstance(obj, dict):
        for value in obj.values():
            digests.update(extract_digests(value))
        return digests
    if isinstance(obj, list):
        for value in obj:
            digests.update(extract_digests(value))
        return digests
    if isinstance(obj, str):
        match = DIGEST_RE.search(obj)
        if match:
            digests.add(match.group(0))
        return digests
    return digests


def get_referenced_digests(manifests_dir: Path) -> set[str]:
    referenced: set[str] = set()
    if not manifests_dir.exists():
        return referenced
    for root, _, files in os.walk(manifests_dir):
        root_path = Path(root)
        for fname in files:
            fpath = root_path / fname
            try:
                with open(fpath, encoding='utf-8') as f:
                    data = json.load(f)
                referenced.update(extract_digests(data))
            except (json.JSONDecodeError, UnicodeDecodeError):
                try:
                    with open(fpath, encoding='utf-8', errors='ignore') as f:
                        text = f.read()
                    for match in DIGEST_RE.finditer(text):
                        referenced.add(match.group(0))
                except Exception:
                    pass
            except Exception:
                pass
    return referenced


def get_blob_filenames(blobs_dir: Path) -> set[str]:
    blobs: set[str] = set()
    if not blobs_dir.exists():
        return blobs
    try:
        for fname in os.listdir(blobs_dir):
            fpath = blobs_dir / fname
            if fpath.is_file():
                blobs.add(fname)
    except Exception:
        pass
    return blobs


def main() -> None:
    parser = argparse.ArgumentParser(description='Scan Ollama model directory for unused blobs')
    parser.add_argument(
        'model_dir', type=str,
        help='Path to Ollama model directory (e.g., e:\\p\\.ollama\\models\\)')
    args = parser.parse_args()
    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f'model directory not found: {model_dir}', file=sys.stderr)
        sys.exit(1)
    manifests_dir = model_dir / 'manifests'
    blobs_dir = model_dir / 'blobs'
    referenced_digests = get_referenced_digests(manifests_dir)
    referenced_filenames: set[str] = set()
    for d in referenced_digests:
        referenced_filenames.add(d.replace(':', '-', 1))
    blob_filenames = get_blob_filenames(blobs_dir)
    unused = blob_filenames - referenced_filenames
    for name in sorted(unused):
        print(name)
        os.unlink(blobs_dir / name)


if __name__ == '__main__':
    main()
