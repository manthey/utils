#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import sys


def load_records(filepath):
    """Return a list of parsed JSON records from a .jsonl file."""
    records = []
    with open(filepath, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def build_tree(records):
    """Build lookup structures from pi records.

    Every pi entry (not just messages) has an id and parentId, and the first
    message's parent is typically a non-message entry such as model_change, so
    the tree is built over all entries.

    Returns (id -> record dict, parent_id -> [child_ids]).
    """
    msg_id_to_rec = {}
    children = {}  # parentId -> list of child ids
    for rec in records:
        mid = rec.get('id')
        parent_id = rec.get('parentId')
        if not mid or not isinstance(mid, str):
            continue
        msg_id_to_rec[mid] = rec
        if parent_id and isinstance(parent_id, str) and parent_id != mid:
            children.setdefault(parent_id, []).append(mid)
    return msg_id_to_rec, children


def summary_from_ids(msg_ids, msg_id_to_rec):
    """Compute count, usage, and user-content data from a set of message ids."""
    count = {'user': 0, 'assistant': 0}
    # _answers counts replies that ended the agent's turn (rather than
    # making tool calls or being aborted)
    usage_sum = {'input': 0, 'output': 0, 'max': 0, '_answers': 0}
    data = []
    for mid in sorted(msg_ids, key=lambda m: (msg_id_to_rec[m].get('timestamp', ''), str(m))):
        rec = msg_id_to_rec.get(mid)
        if not rec or rec.get('type') != 'message':
            continue
        msg = rec.get('message', {})
        role = msg.get('role')
        count[role] = count.get(role, 0) + 1
        usage = msg.get('usage') or {}
        for k in ('input', 'output'):
            if k in usage:
                usage_sum[k] += usage[k]
        usage_sum['max'] = max(usage_sum['max'], usage.get('totalTokens', 0))
        if role == 'assistant' and msg.get('stopReason') in {'stop', 'length'}:
            usage_sum['_answers'] += 1
        if msg.get('role') == 'user':
            for c in msg.get('content', []):
                text = c.get('text', '')
                if text and text not in data:
                    data.append(text)
    return count, usage_sum, data


# Directory prefixes (home directories) that are stripped when matching
# --src.  These are compared after normalization, so separators and drive
# punctuation do not matter.
SRC_PREFIXES = {'/home/ubuntu', '/Users/admin', 'C:\\Users\\agent'}
CLAUDE_COMMAND_RE = re.compile(
    r'<command-name>(?P<name>.*?)</command-name>.*?'
    r'(?:<command-args>(?P<args>.*?)</command-args>|$)', re.DOTALL)
CLAUDE_BASH_RE = re.compile(r'<bash-input>(?P<cmd>.*?)</bash-input>', re.DOTALL)
CLAUDE_SKIP_PREFIXES = (
    '[Request interrupted by user',
    '<local-command-stdout>',
    '<local-command-stderr>',
    '<local-command-caveat>',
    '<bash-stdout>',
    '<bash-stderr>',
    '<task-notification>',
)


def normalize_path_name(name):
    """
    Normalize a project path or session directory name for comparison.

    pi encodes /home/ubuntu/large_image as --home-ubuntu-large_image-- and
    C:/Users/agent/large_image as --C--Users-agent-large_image-; Claude
    Code encodes them as -home-ubuntu-large-image and
    C--Users-agent-large-image.  Collapsing every run of non-alphanumeric
    characters to a single dash, trimming dashes, and lowercasing makes all of
    these comparable.
    """
    return re.sub(r'[^0-9a-z]+', '-', name.lower()).strip('-')


def src_matches(session_dir, src):
    """Return True if a session directory name corresponds to a source path."""
    if not src:
        return True
    name = normalize_path_name(session_dir)
    target = normalize_path_name(src)
    if not target:
        return False
    if name == target:
        return True
    prefixes = SRC_PREFIXES | {os.path.expanduser('~')}
    return any(
        name == f'{normalize_path_name(prefix)}-{target}'
        for prefix in prefixes if normalize_path_name(prefix))


def claude_user_texts(rec):
    """Return the list of texts the user typed in a Claude Code record."""
    if rec.get('type') == 'attachment':
        att = rec.get('attachment') or {}
        if att.get('type') != 'queued_command':
            return []
        prompt = att.get('prompt')
        if isinstance(prompt, list):
            prompt = '\n'.join(
                c.get('text', '') for c in prompt if isinstance(c, dict))
        texts = [prompt] if isinstance(prompt, str) else []
    elif rec.get('type') == 'user':
        if rec.get('isMeta') or rec.get('isCompactSummary') or rec.get('isSidechain'):
            return []
        origin = rec.get('origin')
        if isinstance(origin, dict) and origin.get('kind', 'human') != 'human':
            return []
        content = (rec.get('message') or {}).get('content')
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [
                c.get('text', '') for c in content
                if isinstance(c, dict) and c.get('type') == 'text']
        else:
            texts = []
    else:
        return []
    result = []
    for text in texts:
        text = text.strip()
        if not text or text.startswith(CLAUDE_SKIP_PREFIXES):
            continue
        match = CLAUDE_COMMAND_RE.search(text)
        if match:
            text = (match.group('name').strip() + ' ' + (
                match.group('args') or '').strip()).strip()
        else:
            match = CLAUDE_BASH_RE.search(text)
            if match:
                text = '! ' + match.group('cmd').strip()
        if text:
            result.append(text)
    return result


def claude_build_tree(records):
    """
    Build lookup structures from Claude Code records.

    Returns (uuid -> record dict, parent uuid -> [child uuids]).  Compaction
    boundaries link to the pre-compaction conversation via logicalParentUuid,
    so that is used when there is no direct parent.
    """
    uuid_to_rec = {}
    children = {}
    for rec in records:
        uid = rec.get('uuid')
        if not uid or not isinstance(uid, str):
            continue
        uuid_to_rec[uid] = rec
    for uid, rec in uuid_to_rec.items():
        parent = rec.get('parentUuid') or rec.get('logicalParentUuid')
        if parent and isinstance(parent, str) and parent != uid and parent in uuid_to_rec:
            children.setdefault(parent, []).append(uid)
    return uuid_to_rec, children


def select_branch(id_to_rec, children_map, mode):
    """
    Select the conversation path to show.

    In both pi and Claude Code, branches (from navigating the session tree,
    rewinding, or editing a prompt) share a parent, so a branch is the path
    from a leaf back to the root.  "recent" picks the leaf with the latest
    timestamp, "longest" the deepest leaf, and "all" every entry.
    """
    if mode == 'all':
        return set(id_to_rec)
    parents = {
        child: parent for parent, kids in children_map.items() for child in kids}

    def path_to(uid):
        path = []
        seen = set()
        while uid and uid not in seen:
            seen.add(uid)
            path.append(uid)
            uid = parents.get(uid)
        return path

    leaves = [uid for uid in id_to_rec if not children_map.get(uid)]
    if not leaves:
        return set(id_to_rec)
    if mode == 'recent':
        leaf = max(leaves, key=lambda u: (id_to_rec[u].get('timestamp', ''), u))
        return set(path_to(leaf))
    paths = [path_to(leaf) for leaf in leaves]
    return set(max(paths, key=len))


def claude_summary_from_ids(uuids, uuid_to_rec):
    """Compute count, usage, and user-content data from Claude Code records."""
    count = {'user': 0, 'assistant': 0}
    # _answers counts replies that ended the agent's turn (rather than
    # making tool calls or being aborted)
    usage_sum = {'input': 0, 'output': 0, 'max': 0, '_answers': 0}
    data = []
    seen_assistant = set()
    for uid in sorted(uuids, key=lambda u: (uuid_to_rec[u].get('timestamp', ''), u)):
        rec = uuid_to_rec[uid]
        if rec.get('type') == 'assistant':
            msg = rec.get('message') or {}
            # One API response is stored as one record per content block,
            # each repeating the usage, so count each message id once.
            msg_id = msg.get('id') or uid
            if msg_id in seen_assistant:
                continue
            seen_assistant.add(msg_id)
            count['assistant'] += 1
            usage = msg.get('usage') or {}
            inp = usage.get('input_tokens', 0) or 0
            out = usage.get('output_tokens', 0) or 0
            usage_sum['input'] += inp
            usage_sum['output'] += out
            total = (inp + out + (usage.get('cache_read_input_tokens', 0) or 0) +
                     (usage.get('cache_creation_input_tokens', 0) or 0))
            usage_sum['max'] = max(usage_sum['max'], total)
            # An interrupted reply is stored without a stop reason
            if msg.get('stop_reason') in {'end_turn', 'stop_sequence', 'max_tokens'}:
                usage_sum['_answers'] += 1
            continue
        texts = claude_user_texts(rec)
        if texts:
            count['user'] += 1
        for text in texts:
            if text not in data:
                data.append(text)
    return count, usage_sum, data


def should_prune(count, usage_sum, data):
    """
    Decide if a session is one that was abandoned before it got started.

    This is true if the user typed nothing, or typed a single prompt and the
    agent never answered it (it did nothing, or only made tool calls before
    being stopped).
    """
    return not data or (not usage_sum['_answers'] and count['user'] <= 1)


def pi_sessions(sessions_dir, args):
    """Yield (filepath, count, usage_sum, data) for each pi session."""
    for session_dir in sorted(os.listdir(sessions_dir)):
        if not src_matches(session_dir, args.src):
            continue
        full_dir = os.path.join(sessions_dir, session_dir)
        if not os.path.isdir(full_dir):
            continue
        files = sorted(
            f for f in os.listdir(full_dir)
            if f.endswith('.jsonl') and os.path.isfile(os.path.join(full_dir, f))
        )
        for fname in files:
            filepath = os.path.abspath(os.path.join(full_dir, fname))
            records = load_records(filepath)
            msg_id_to_rec, children_map = build_tree(records)
            # Pruning deletes the whole file, so judge it on every branch
            if args.rmzero and should_prune(*summary_from_ids(
                    set(msg_id_to_rec), msg_id_to_rec)):
                os.unlink(filepath)
                continue
            chosen_ids = select_branch(msg_id_to_rec, children_map, args.branch)
            count, usage_sum, data = summary_from_ids(chosen_ids, msg_id_to_rec)
            yield filepath, count, usage_sum, data


def claude_sessions(sessions_dir, args):
    """Yield (filepath, count, usage_sum, data) for each Claude Code session."""
    for session_dir in sorted(os.listdir(sessions_dir)):
        if not src_matches(session_dir, args.src):
            continue
        full_dir = os.path.join(sessions_dir, session_dir)
        if not os.path.isdir(full_dir):
            continue
        # Claude Code session files are named by uuid, so order them by the
        # time of their first record.
        sessions = []
        for fname in os.listdir(full_dir):
            filepath = os.path.abspath(os.path.join(full_dir, fname))
            if not fname.endswith('.jsonl') or not os.path.isfile(filepath):
                continue
            records = load_records(filepath)
            start = min((r['timestamp'] for r in records
                         if isinstance(r.get('timestamp'), str)), default='')
            sessions.append((start, filepath, records))
        for _, filepath, records in sorted(sessions, key=lambda s: (s[0], s[1])):
            uuid_to_rec, children_map = claude_build_tree(records)
            # Pruning deletes the whole file, so judge it on every branch
            if args.rmzero and should_prune(*claude_summary_from_ids(
                    set(uuid_to_rec), uuid_to_rec)):
                os.unlink(filepath)
                # Claude Code keeps subagent transcripts and large tool results
                # in a directory named after the session.
                extra_dir = filepath[:-len('.jsonl')]
                if os.path.isdir(extra_dir):
                    shutil.rmtree(extra_dir)
                continue
            chosen_ids = select_branch(uuid_to_rec, children_map, args.branch)
            count, usage_sum, data = claude_summary_from_ids(chosen_ids, uuid_to_rec)
            yield filepath, count, usage_sum, data


def default_claude_dir():
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join('~', '.claude')
    if not os.path.exists(config_dir):
        config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'claude')
    return os.path.join(config_dir, 'projects')


def parse_args():
    parser = argparse.ArgumentParser(
        description='Summarize what the user typed in sessions of the pi and '
        'Claude Code agentic coding agents.',
    )
    agent = parser.add_mutually_exclusive_group()
    agent.add_argument(
        '--pi', dest='agent', action='store_const', const='pi',
        help='Only summarize pi sessions.',
    )
    agent.add_argument(
        '--claude', dest='agent', action='store_const', const='claude',
        help='Only summarize Claude Code sessions.',
    )
    agent.add_argument(
        '--all', dest='agent', action='store_const', const='all',
        help='Summarize both pi and Claude Code sessions (default).',
    )
    parser.set_defaults(agent='all')
    parser.add_argument(
        '--branch', choices=['all', 'recent', 'longest'], default='recent',
        help='"all" shows every user message, "recent" shows the branch with '
        'the most recent activity, "longest" shows the deepest tree branch. '
        '(default: %(default)s)',
    )
    parser.add_argument(
        '--dir-pi', default=os.path.join('~', '.pi', 'agent', 'sessions'),
        help='Root pi session directory (default: %(default)s)',
    )
    parser.add_argument(
        '--dir-claude', default=default_claude_dir(),
        help='Root Claude Code projects directory (default: %(default)s; '
        'based on CLAUDE_CONFIG_DIR if set)',
    )
    parser.add_argument(
        '--rmzero', action='store_true',
        help='Remove traces with no more than one user prompt where the agent '
        'never gave an answer (it was stopped before responding or while '
        'still making tool calls). These are terminated sessions.',
    )
    parser.add_argument(
        '--src', help='Only show sessions for the specified source path.  '
        'This can be a full path or a directory name in a home directory '
        '(e.g., "HistomicsUI" or "large_image_wheels").  Separators, case, '
        'and the difference between "_" and "-" are ignored.',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    # Windows consoles may not be able to encode everything a user typed.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    sources = []
    if args.agent in ('pi', 'all'):
        sources.append(('pi', args.dir_pi, pi_sessions))
    if args.agent in ('claude', 'all'):
        sources.append(('claude', args.dir_claude, claude_sessions))
    totals = {}
    for agent, sessions_dir, func in sources:
        sessions_dir = os.path.expanduser(sessions_dir)
        if not os.path.isdir(sessions_dir):
            print(f'Sessions directory not found: {sessions_dir}', file=sys.stderr)
            continue
        totals[agent] = 0
        for filepath, count, usage_sum, data in func(sessions_dir, args):
            if not data:
                continue
            print(f'## {filepath}')
            branch_label = args.branch.upper()
            print(f'- Agent: {agent}')
            print(f'- Calls ({branch_label}): ' + ', '.join(
                f'{k}: {count[k]}' for k in sorted(count)
            ))
            print('- Usage: ' + ', '.join(
                f'{k}: {usage_sum.get(k, 0)}' for k in sorted(usage_sum)
                if not k.startswith('_')
            ))
            for entry in data:
                print(entry)
            print()
            totals[agent] += 1
    print(f'## Summary\n- Sessions: {sum(totals.values())}')
    if len(totals) > 1:
        for agent, total in totals.items():
            print(f'- Sessions ({agent}): {total}')


if __name__ == '__main__':
    main()
