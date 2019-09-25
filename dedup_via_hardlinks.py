#!/usr/bin/env python3

import hashlib
import os
import re
import sys

Verbose = 0


def sha512sum(filename):
    h = hashlib.sha512()
    b = bytearray(128*1024)
    mv = memoryview(b)
    with open(filename, 'rb', buffering=0) as f:
        for n in iter(lambda: f.readinto(mv), 0):
            h.update(mv[:n])
    return h.hexdigest()


def stat_to_dict(stats):
    return {k: getattr(stats, k) for k in dir(stats) if k.startswith('st_')}


if __name__ == '__main__':  # noqa
    bases = []
    excludes = []
    help = False
    simulate = False
    for pos in range(1, len(sys.argv)):
        arg = sys.argv[pos]
        if arg.startswith('--exclude='):
            excludes.append(re.compile(arg.split('=', 1)[1]))
        elif arg == '-s':
            simulate = True
        elif arg == '-v':
            Verbose += 1
        elif not arg.startswith('-'):
            bases.append(arg)
        else:
            help = True
    if help:
        print("""Walk directories and make all identical files hardlinks of each other.

Syntax: dedup_via_hardlink.py [(root directory) ...] -s -v --exclude=(regex)

If no directory is specified, the current directory is used.
--excludes paths based on a regular expression.  For instance, "\\.py$"
 excludes python files, and "/\.git/" excludes .git directories on linux.
-s simulates the action, printing what would occur but not doing it.
-v increases verbosity.""")
        sys.exit(0)
    reduced = 0
    files = {}
    if not len(bases):
        bases.append('.')
    for root in bases:
        absroot = os.path.abspath(os.path.expanduser(root))
        for base, dirs, filenames in os.walk(absroot):
            if os.sep + '$RECYCLE.BIN' + os.sep in base:
                continue
            for filename in filenames:
                src = os.path.join(root, base, filename)
                if any(exc.search(src) for exc in excludes):
                    continue
                stats = os.stat(src, follow_symlinks=False)
                if not stats.st_ino or not stats.st_dev:
                    continue
                if not os.path.isfile(src) or os.path.islink(src):
                    continue
                files[src] = stat_to_dict(stats)
    if Verbose >= 1:
        print('Collected %d files' % len(files))
    filelist = sorted(files)
    for idx, src in enumerate(filelist):
        if not files[src]['st_size']:
            continue
        matched_ino = set()
        skipped_ino = set()
        for other in filelist[idx+1:]:
            if files[src]['st_size'] != files[other]['st_size']:
                continue
            if files[src]['st_dev'] != files[other]['st_dev']:
                continue
            if os.path.samefile(src, other):
                continue
            if Verbose >= 2:
                print('Comparing %s to %s' % (src, other))
            if 'sha' not in files[src]:
                try:
                    files[src]['sha'] = sha512sum(src)
                except PermissionError:  # noqa
                    break
            if files[other]['st_ino'] in skipped_ino:
                continue
            if not files[other]['st_ino'] in matched_ino:
                if 'sha' not in files[other]:
                    try:
                        files[other]['sha'] = sha512sum(other)
                    except PermissionError:  # noqa
                        continue
                if files[src]['sha'] != files[other]['sha']:
                    skipped_ino.add(files[other]['st_ino'])
                    continue
                reduced += files[other]['st_size']
            if Verbose >= 1:
                print('Link %s to %s (reduced %d)' % (src, other, reduced))
            matched_ino.add(files[other]['st_ino'])
            if not simulate:
                try:
                    os.unlink(other)
                except Exception:
                    if Verbose >= 1:
                        print('Cannot remove %s' % other)
                    continue
                try:
                    os.link(src, other)
                except Exception:
                    print('Failed to link %s to %s' % (src, other))
                    raise
            files[other] = stat_to_dict(os.stat(other, follow_symlinks=False))
            files[other]['sha'] = files[src]['sha']
