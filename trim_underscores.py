#!/usr/bin/env python3
# /// script
# name = "trim_underscores"
# requires-python = ">=3.9"
# dependencies = []
# ///

import argparse
import ast
import sys
from pathlib import Path


def get_offset_for_name(node, src_lines):
    """Return (line_idx, col) for a single leading underscore on a name node."""
    lineno = getattr(node, 'lineno', None)
    if lineno is None or lineno - 1 >= len(src_lines):
        return None
    line_text = src_lines[lineno - 1].strip()
    base_col = node.col_offset
    prefix_len = -1

    # Calculate column where '_name' starts after the keyword (def/class).
    if line_text.startswith('async def '):
        prefix_len = 9
    elif line_text.startswith(('def ', 'class ')):
        prefix_len = 4
    if prefix_len != -1:
        target_col = base_col + prefix_len
        src_line = src_lines[lineno - 1]
        if target_col < len(src_line) and src_line[target_col] == '_':
            return (lineno - 1, target_col)
    return None


def get_offset_for_simple_node(node):
    """Return (line_idx, col) for simple nodes where col_offset is exact."""
    lineno = getattr(node, 'lineno', None)
    if lineno is None:
        return None

    def is_dunder_check(val):
        return isinstance(val, str) and val.startswith('_') \
            and not val.startswith('__')

    if hasattr(node, 'arg') and isinstance(node.arg, str):
        arg = node.arg
        if is_dunder_check(arg) and arg != '_':
            return (node.lineno - 1, getattr(node, 'col_offset', -1))
    if hasattr(node, 'attr') and is_dunder_check(node.attr):
        attr_col = node.end_col_offset - len(node.attr)
        return (node.lineno - 1, attr_col)
    return None


def get_underscore_offsets(filepath):  # noqa
    """Parse source using AST to find single leading underscores."""
    try:
        src_lines = Path(filepath).read_text(encoding='utf-8').splitlines(keepends=True)
        tree = ast.parse(''.join(src_lines))
    except SyntaxError:
        return []
    offsets = []
    for node in ast.walk(tree):
        # 1. Functions, Async Functions, and Classes (excluding dunders like __init__)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = getattr(node, 'name', '')
            if hasattr(name, 'startswith') and name.startswith('_') and \
                    not name.startswith('__'):
                off = get_offset_for_name(node, src_lines)
                if off:
                    offsets.append(off)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    tid = target.id
                    if hasattr(tid, 'startswith') and \
                            tid.startswith('_') and not \
                            tid.startswith('__') and tid != '_':
                        offsets.append((target.lineno - 1,
                                        getattr(target, 'col_offset', -1)))
        elif isinstance(node, ast.arg):
            off = get_offset_for_simple_node(node)
            if off:
                offsets.append(off)
        elif isinstance(node, ast.Attribute):
            off = get_offset_for_simple_node(node)
            if off:
                offsets.append(off)
        elif isinstance(node, ast.keyword) and node.arg:
            arg = node.arg
            if hasattr(arg, 'startswith') and arg.startswith('_') \
                    and not arg.startswith('__'):
                offsets.append((node.lineno - 1, node.col_offset))
    return offsets


def fix_file(filepath):
    """Remove underscores identified by AST parsing."""
    offsets = get_underscore_offsets(filepath)
    if not offsets:
        return False
    src_lines = Path(filepath).read_text(encoding='utf-8').splitlines(keepends=True)

    # Sort offsets in reverse order (bottom-up, right-left) to preserve indices.
    offsets.sort(reverse=True)
    deleted_count = 0
    for line_idx, col_idx in offsets:
        if line_idx < len(src_lines):
            src_line = src_lines[line_idx]
            # Double check bounds and exact character match.
            if 0 <= col_idx < len(src_line) and src_line[col_idx] == '_':
                src_lines[line_idx] = (src_line[:col_idx] +
                                       src_line[col_idx + 1:])
                deleted_count += 1
    if deleted_count > 0:
        Path(filepath).write_text(''.join(src_lines), encoding='utf-8')
        return True
    return False


def should_keep_blank_line(
    prev_text, prev_indent, curr_text, curr_indent,
    indent_cause, code_lines_since_last_blank, min_gap,
):
    """Determine if a pending blank line should be retained based on rules."""
    prev_starts_import = prev_text.startswith(('import ', 'from '))
    curr_starts_import = curr_text.startswith(('import ', 'from '))

    # Rule 1: Keep for import blocks (start/end/middle).
    keep_for_imports = \
        (prev_starts_import and not curr_starts_import) or \
        (prev_starts_import and curr_starts_import)
    # Rule 2: Keep before a function/class/decorator definition.
    def_or_class_def = any(
        curr_text.startswith(pfx) for pfx in ('def ', 'async def ',
                                              'class ', '@'))
    if keep_for_imports or def_or_class_def:
        return True
    # Rule 3: Outdenting from a function/class block.
    if prev_indent > curr_indent:
        for indent_level in range(curr_indent + 4, prev_indent + 4, 4):
            if indent_cause.get(indent_level) in ('def', 'class'):
                return True
    # Rule 4: Long logical section inside same-indent code.
    gap_reached = code_lines_since_last_blank >= min_gap
    same_indent = prev_indent == curr_indent
    return same_indent and gap_reached


def fix_blanks(filepath, min_gap=3):
    """Remove excessive blank lines between code blocks."""
    source = Path(filepath).read_text(encoding='utf-8')
    lines = source.splitlines(keepends=True)

    output_lines = []
    pending_blank = False
    code_lines_since_last_blank = 0

    prev_indent = 0
    prev_text = ''
    # Track what started the current indent block (e.g., 'def', 'class', etc.)
    # Standard blocks are typically multiples of 4.
    indent_cause = {0: 'other'}

    for line in lines:
        curr_text = line.strip()
        curr_indent = len(line) - len(line.lstrip())

        if not curr_text:  # It's a blank or whitespace-only line.
            pending_blank = True
            continue
        # If we just had a blank line, decide whether to keep it.
        if pending_blank:
            should_keep = should_keep_blank_line(
                prev_text, prev_indent, curr_text, curr_indent,
                indent_cause, code_lines_since_last_blank, min_gap,
            )

            if should_keep:
                output_lines.append('\n')
                code_lines_since_last_blank = 0
            pending_blank = False
        # Update indent cause for the next block.
        if curr_indent > prev_indent:
            is_prev_def = prev_text.startswith(('def ', 'async def '))
            if is_prev_def:
                indent_cause[curr_indent] = 'def'
            elif prev_text.startswith('class '):
                indent_cause[curr_indent] = 'class'
            else:
                indent_cause[curr_indent] = 'other'
        output_lines.append(line)
        code_lines_since_last_blank += 1
        prev_indent = curr_indent
        prev_text = curr_text
    # Preserve a single trailing newline at the end if it existed.
    if pending_blank:
        output_lines.append('\n')
    new_source = ''.join(output_lines)

    if new_source != source:
        Path(filepath).write_text(new_source, encoding='utf-8')
        return True
    return False


if __name__ == '__main__':
    parser_description = ('Trim intentional leading underscores and '
                          'excessive blank lines from Python scripts generated '
                          'by LLMs.')
    doc_under = (
        'Remove single leading underscores from identifiers. Rationale: '
        'LLMs often pretend scripts are libraries by prefixing everything '
        'with underscores to appease linters. This aggressively strips them '
        'unless they are dunders (__*) or the exact single underscore (_) '
        'for explicitly unused vars. If a variable is assigned but truly used '
        'elsewhere, stripping this prefix intentionally triggers linter '
        'warnings, prompting us to use it or delete the code block.'
    )

    doc_blank = (
        'Remove excessive blank lines between code blocks. Default skips '
        'structural ones and allows one every %(default)s+ lines if '
        'applicable. Rationale: PEP8 recommends sparing blank lines for '
        'conceptual blocks, but LLMs overuse them. This keeps necessary '
        'structural spacing (imports, defs/classes outdent/in-dent) while '
        'clutters fewer logical sections. Note that your local flake8/ruff '
        'rules should not enforce extra spacing (e.g., pycodestyle E302) '
        'before using in CI.'
    )

    parser = argparse.ArgumentParser(description=parser_description)
    parser.add_argument('files', nargs='+', help='List of .py files to process.')
    parser.add_argument('--remove-underscores', '--underscores',
                        action='store_true', default=False, help=doc_under)
    parser.add_argument('--remove-blank-lines', '--blanks',
                        action='store_true', default=False, help=doc_blank)
    parser.add_argument(
        '--blank-lines-gap', type=int, default=3,
        help=(
            'Minimum number of consecutive code lines required before a new '
            'blank line is permitted elsewhere. Default is %(default)s.'
        ),
    )
    args = parser.parse_args()
    changed = False
    for filepath in args.files:
        if filepath.endswith('.py'):
            try:
                if args.remove_underscores and fix_file(filepath):
                    print(f'Stripped leading underscores from {filepath}')
                    changed = True
                if args.remove_blank_lines:
                    gap_val = args.blank_lines_gap
                    if fix_blanks(filepath, min_gap=gap_val):
                        print(f'Stripped excessive blank lines from {filepath}')
                        changed = True
            except Exception as e:
                print(f'Failed to process {filepath}: {e}', file=sys.stderr)
        else:
            print(f'Skipping non-Python file: {filepath}', file=sys.stderr)
    sys.exit(1 if changed else 0)
