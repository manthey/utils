#!/usr/bin/env python3

import argparse
import glob
import os
import subprocess

LANGUAGE_MAP = {
    '.css': 'css',
    '.html': 'html',
    '.js': 'javascript',
    '.json': 'json',
    '.md': 'markdown',
    '.mjs': 'javascript',
    '.py': 'python',
    '.sh': 'bash',
    '.toml': 'toml',
    '.ts': 'typescript',
    '.txt': 'text',
    '.xml': 'xml',
    '.yaml': 'yaml',
    '.yml': 'yaml',
}


def is_git_tracked(path):
    if not os.path.exists('.git'):
        return True
    result = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def is_binary(file_path):
    try:
        with open(file_path, 'rb') as f:
            head = f.read(1024**2)
            head.decode()
            return b'\x00' in head
    except Exception:
        return True


def expand_paths(paths, exclude_paths):
    total = set()
    for path in paths:
        if os.path.isdir(path):
            found = glob.glob(os.path.join(path, '**'), recursive=True)
        else:
            found = glob.glob(path, recursive=True)
        total |= {os.path.relpath(p) for p in found}
    for path in exclude_paths:
        if os.path.isdir(path):
            found = glob.glob(os.path.join(path, '**'), recursive=True)
        else:
            found = glob.glob(path, recursive=True)
        total -= {os.path.relpath(p) for p in found}
    return [p for p in sorted(total)
            if os.path.isfile(p) and is_git_tracked(p) and not is_binary(p)]


def escape_backticks(content):
    return content.replace('```', r'\`\`\`')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('paths', nargs='+',
                        help='Files, globs, or directories')
    parser.add_argument('-x', '--exclude', action='append', default=[],
                        help='Exclude paths and globs')
    args = parser.parse_args()

    for path in expand_paths(args.paths, args.exclude):
        ext = os.path.splitext(path)[1]
        lang = LANGUAGE_MAP.get(ext, ext[1:])

        print(f'File: {path}')
        print(f'```{lang}')
        with open(path) as f:
            content = f.read()
            print(escape_backticks(content).rstrip())
        print('```')


if __name__ == '__main__':
    main()
