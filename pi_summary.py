#!/usr/bin/env python3
import argparse
import json
import os


def parse_args():
    parser = argparse.ArgumentParser(
        description='Summarize traces from the pi agentic coding agent.',
    )
    parser.add_argument(
        '--branch',
        choices=['all', 'recent', 'longest'],
        default='recent',
        help=(
            "Branch selection mode: 'all' shows every user message, "
            "'recent' selects the branch with the most recent activity, "
            "'longest' selects the deepest tree branch. (default: %(default)s)"
        ),
    )
    parser.add_argument(
        '--sessions-dir',
        default='~/.pi/agent/sessions',
        help='Root session directory (default: %(default)s)',
    )
    return parser.parse_args()


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
    """Build lookup structures from message-type records.

    Returns (msg_id -> record dict, parent_id -> [child_ids]).
    """
    msg_recs = [r for r in records if r.get('type') == 'message']
    msg_id_to_rec = {}
    children = {}  # parentId -> list of child ids

    for rec in msg_recs:
        mid = rec.get('id')
        parent_id = rec.get('parentId')
        if not mid or not isinstance(mid, str):
            continue
        msg_id_to_rec[mid] = rec
        if parent_id and isinstance(parent_id, str) and parent_id != mid:
            children.setdefault(parent_id, []).append(mid)
    return msg_id_to_rec, children


def find_roots(msg_id_to_rec, children):
    """Return sorted list of message ids that have no parent in the file."""
    all_children = set()
    for kids in children.values():
        all_children.update(kids)
    return sorted(mid for mid in msg_id_to_rec if mid not in all_children)


def collect_subtree(root_id, children_map):
    """Return a set of all message ids (including root) in the subtree."""
    result = set()
    stack = [root_id]
    while stack:
        mid = stack.pop()
        if mid in result or mid not in children_map:
            continue
        result.add(mid)
        for child_id in children_map.get(mid, []):
            if child_id not in result and child_id != mid:
                stack.append(child_id)
    return result


def summary_from_ids(msg_ids, msg_id_to_rec):
    """Compute count, usage, and user-content data from a set of message ids."""
    count = {'user': 0, 'assistant': 0}
    usage_sum = {'input': 0, 'output': 0, 'max': 0}
    data = []

    for mid in msg_ids:
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
        if msg.get('role') == 'user':
            for c in msg.get('content', []):
                text = c.get('text', '')
                if text and text not in data:
                    data.append(text)
    return count, usage_sum, data


def select_branch(roots, msg_id_to_rec, children_map, mode):
    """Select which branch to process based on args.branch.

    Returns the chosen set of message ids for this trace file.
    """
    # Gather each root subtree and its latest timestamp
    branches = []
    for root in roots:
        subtree = collect_subtree(root, children_map)
        if not subtree:
            continue
        latest_ts = max(
            (msg_id_to_rec[mid].get('timestamp', '') for mid in subtree),
            default='',
        )
        branches.append((root, subtree, latest_ts))
    if not branches:
        return set(msg_id_to_rec.keys())
    chosen = None
    if mode == 'recent':
        chosen = max(branches, key=lambda b: b[2])
    elif mode == 'longest':
        chosen = max(branches, key=lambda b: len(b[1]))
    return set(chosen[1]) if chosen else set()


def main():
    args = parse_args()

    sessions_dir = os.path.expanduser(args.sessions_dir)
    totalcount = 0
    if not os.path.isdir(sessions_dir):
        print(f'Sessions directory not found: {sessions_dir}', flush=True)
        return
    for session_dir in sorted(os.listdir(sessions_dir)):
        full_dir = os.path.join(sessions_dir, session_dir)
        if not os.path.isdir(full_dir):
            continue
        files = sorted(
            f for f in os.listdir(full_dir)
            if f.endswith('.jsonl') and os.path.isfile(os.path.join(full_dir, f))
        )
        if not files:
            continue
        for fname in files:
            filepath = os.path.join(full_dir, fname)
            records = load_records(filepath)
            msg_id_to_rec, children_map = build_tree(records)
            roots = find_roots(msg_id_to_rec, children_map)

            if args.branch == 'all' or not roots:
                chosen_ids = set(msg_id_to_rec.keys())
            else:
                chosen_ids = select_branch(roots, msg_id_to_rec, children_map, args.branch)
                if not chosen_ids:
                    chosen_ids = set(msg_id_to_rec.keys())
            count, usage_sum, data = summary_from_ids(chosen_ids, msg_id_to_rec)
            if not data:
                continue
            print(f'## {filepath}')
            branch_label = args.branch.upper()
            if args.branch == 'recent':
                branch_label += ' (most recent branch)'
            elif args.branch == 'longest':
                branch_label += ' (longest branch)'
            print('- Calls (%s): ' % branch_label + ', '.join(
                f'{k}: {count[k]}' for k in sorted(count)
            ))
            display_usage = {
                k: v for k, v in usage_sum.items() if not k.startswith('_')
            }
            print('- Usage: ' + ', '.join(
                f'{k}: {display_usage.get(k, 0)}' for k in sorted(display_usage)
            ))
            for entry in data:
                print(entry)
            print()
            totalcount += 1
    print(f'## Summary\n- Sessions: {totalcount}')


if __name__ == '__main__':
    main()
