#!/usr/bin/env python3

import argparse


def diffpip(list1str, list2str):
    list1 = set(entry for entry in list1str.strip().replace('\n', ',').split(',') if '==' in entry)
    list2 = set(entry for entry in list2str.strip().replace('\n', ',').split(',') if '==' in entry)
    diff1 = list1 - list2
    diff2 = list2 - list1
    prefix1 = {entry.split('==')[0]: entry.split('==', 1)[-1] for entry in diff1}
    prefix2 = {entry.split('==')[0]: entry.split('==', 1)[-1] for entry in diff2}
    keys = sorted(set(prefix1) | set(prefix2))
    if not len(keys):
        print('No differences')
        return
    lenkey = max(max(len(entry) for entry in keys), 13)
    len1 = max(max(len(val) for val in prefix1.values()), 7) if len(prefix1) else 7
    len2 = max(max(len(val) for val in prefix2.values()), 7) if len(prefix2) else 7
    print(('%%-%ds %%-%ds %%-%ds' % (lenkey, len1, len2)) % ('-- Package --', '-- A --', '-- B --'))
    inst1to2 = []
    inst2to1 = []
    uninst1to2 = []
    uninst2to1 = []
    for entry in keys:
        val1 = prefix1.get(entry, '')
        val2 = prefix2.get(entry, '')
        print(('%%-%ds %%-%ds %%-%ds' % (lenkey, len1, len2)) % (entry, val1, val2))
        if val1:
            inst2to1.append('%s==%s' % (entry, val1))
        else:
            uninst2to1.append(entry)
        if val2:
            inst1to2.append('%s==%s' % (entry, val2))
        else:
            uninst1to2.append(entry)
    print('-- A to B --')
    if len(uninst1to2):
        print('pip uninstall ' + ' '.join(uninst1to2))
    if len(inst1to2):
        print('pip install ' + ' '.join(inst1to2))
    print('-- B to A --')
    if len(uninst2to1):
        print('pip uninstall ' + ' '.join(uninst2to1))
    if len(inst2to1):
        print('pip install ' + ' '.join(inst2to1))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Show the difference betweeb two sets of pip installs.  '
        'Given two reports of what pip installed or "pip freeze" like '
        'outputs, compare them and show how they differ.  Editable installs '
        'are ignored.')
    parser.add_argument('list1', help='The first list of pip packages with versions.')
    parser.add_argument('list2', help='The first list of pip packages with versions.')
    args = parser.parse_args()
    diffpip(args.list1, args.list2)
