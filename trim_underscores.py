import ast
import sys
from pathlib import Path


def get_underscore_offsets(filepath):
    with open(filepath, encoding='utf-8') as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    offsets = []
    for node in ast.walk(tree):
        # 1. Functions, Async Functions, and Classes (excluding dunders like __init__)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith('_') and not node.name.startswith('__'):
                offsets.append((node.lineno - 1, node.col_offset))
        # 2. Variable Assignments (e.g., _foo = 1)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # Allows exact single underscore "_" for unused variables
                    if target.id.startswith('_') and not target.id.startswith('__') and target.id != '_':
                        offsets.append((target.lineno - 1, target.col_offset))
        # 3. Function Arguments (e.g., def foo(_bar):)
        elif isinstance(node, ast.arg):
            if node.arg.startswith('_') and not node.arg.startswith('__') and node.arg != '_':
                offsets.append((node.lineno - 1, node.col_offset))
        # 4. Attributes (e.g., self._foo)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith('_') and not node.attr.startswith('__'):
                attr_col = node.end_col_offset - len(node.attr)
                offsets.append((node.lineno - 1, attr_col))
        # 5. Keyword Arguments (e.g., foo(_bar=1))
        elif isinstance(node, ast.keyword):
            if node.arg and node.arg.startswith('_') and not node.arg.startswith('__'):
                offsets.append((node.lineno - 1, node.col_offset))
    return offsets


def fix_file(filepath):
    offsets = get_underscore_offsets(filepath)
    if not offsets:
        return False
    with open(filepath, encoding='utf-8') as f:
        lines = f.readlines()
    # Sort offsets in reverse order (bottom-up, right-left)
    # This ensures deleting a character doesn't shift the col_offset of preceding characters
    offsets.sort(reverse=True)
    for line_idx, col_idx in offsets:
        line = lines[line_idx]
        # Safety check: ensure the character at the offset is actually an underscore
        if col_idx < len(line) and line[col_idx] == '_':
            lines[line_idx] = line[:col_idx] + line[col_idx + 1:]
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    return True


MIN_GAP = 3


def get_indent(line):
    return len(line) - len(line.lstrip())


def fix_blanks(filepath):
    source = Path(filepath).read_text()
    lines = source.splitlines(keepends=True)

    output_lines = []
    pending_blank = False
    code_lines_since_last_blank = 0

    prev_indent = 0
    prev_text = ''
    curr_indent = 0
    curr_text = ''

    # Track what started the current indent block (e.g., 'def', 'class', 'other')
    indent_cause = {0: 'other'}
    for line in lines:
        curr_text = line.strip()
        curr_indent = get_indent(line)

        if not curr_text:  # It's a blank line
            pending_blank = True
            continue
        # If we just had a blank line, decide whether to keep it
        if pending_blank:
            keep_blank = False

            # PROTECTED: End of an import block
            if prev_text.startswith(('import ', 'from ')) and not curr_text.startswith(('import ', 'from ')):
                keep_blank = True
            # PROTECTED: Inside an import block (let isort manage grouping)
            elif prev_text.startswith(('import ', 'from ')) and curr_text.startswith(('import ', 'from ')):
                keep_blank = True
            # PROTECTED: Before a function/class/decorator definition
            elif curr_text.startswith(('def ', 'async def ', 'class ', '@')):
                keep_blank = True
            # PROTECTED: Outdenting from a function/class block
            elif prev_indent > curr_indent:
                # We are closing one or more blocks. If ANY of the closed
                # blocks were 'def' or 'class', keep the blank line.
                for indent_level in range(curr_indent + 4, prev_indent + 4, 4):
                    if indent_cause.get(indent_level) in ('def', 'class'):
                        keep_blank = True
                        break
            # ALLOWED: Long logical section inside same-indent code
            elif prev_indent == curr_indent:
                if code_lines_since_last_blank >= MIN_GAP:
                    keep_blank = True
            if keep_blank:
                output_lines.append('\n')
                code_lines_since_last_blank = 0
            pending_blank = False
        # Update indent cause if indent increases (we entered a new block)
        if curr_indent > prev_indent:
            if prev_text.startswith(('def ', 'async def ')):
                indent_cause[curr_indent] = 'def'
            elif prev_text.startswith('class '):
                indent_cause[curr_indent] = 'class'
            else:
                indent_cause[curr_indent] = 'other'
        output_lines.append(line)
        code_lines_since_last_blank += 1
        prev_indent = curr_indent
        prev_text = curr_text
    # Preserve a single trailing newline at the end of the file if it existed
    if pending_blank:
        output_lines.append('\n')
    new_source = ''.join(output_lines)

    if new_source != source:
        Path(filepath).write_text(new_source)
        return True
    return False


if __name__ == '__main__':
    changed = False
    for filepath in sys.argv[1:]:
        if filepath.endswith('.py'):
            if fix_file(filepath):
                print(f'Stripped leading underscores from {filepath}')
                changed = True
            # if fix_blanks(filepath):
            #     print(f'Stripped excessive blank lines from {filepath}')
            #     changed = True
    # This tells pre-commit the files changed, forcing the agent to re-stage and re-commit.
    sys.exit(1 if changed else 0)


# TODO:
# - use argparse to optionally remove underscores and optionally remove blank
# lines rather than always doing it.  The default should be off for both
# features.
# - when removing leading underscores, we _want_ to leave them on actually
# unused variables
# - when removing blank lines, we need to NOT conflict with any existing
# flake8/ruff rules in this repository.  During development, we should make
# some clear tests and iterate until we never introduce such conflicts
# - Event though we have no none-core dependencies, we should make this script
# pep 723 compliant
# - Ideally, we want to run this in pre-commit when the code is on github;
# document how to that
# - This script should pass our local pre-commit rules
#
# The underscore removal is intended to be aggressive.  LLMs somehow think all
# programs are libraries and variables somehow benefit from underscores.
# Realistically, unless the ruff rule about unused variables should start with
# an underscore would be invoked, we want to remove the underscore
#
# Similarly, pep8 recommends using blank lines SPARINGLY for conceptual blocks.
# These are clearly NEVER useful before or after an indent or outdent, or
# before or after a comment.  LLMs have somehow taken this to mean "almost
# always", and we are fighting that.  We want blank lines between functions and
# methods (two at the top level, one inside clases), after imports, and around
# nested functions.  We MOSTLY we want to get rid of other blank lines, but
# conceed that when there are 4 or more statements in a row, there MIGHT be
# utility in a blank line somewhere.  Ideally that MIN_GAP parameter would be
# an optional argument.
