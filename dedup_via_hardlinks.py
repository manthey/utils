#!/usr/bin/env python3
import hashlib
import os
import re
import stat as statlib
import sys
from collections import defaultdict

Verbose = 0
SAMPLE_BYTES = 4096
READ_CHUNK = 128 * 1024


def stat_to_dict(stats):
    return {k: getattr(stats, k) for k in dir(stats) if k.startswith('st_')}


def sha512sum(path, entry=None):
    """Return the hex digest of the file's current contents, or None if it
    could not be read or if it changed while being read.  If entry is given,
    it is updated with the stat of the data that was actually read.
    """
    h = hashlib.sha512()
    buf = bytearray(READ_CHUNK)
    mv = memoryview(buf)
    try:
        f = open(path, 'rb', buffering=0)
    except OSError as e:
        if Verbose >= 1:
            print('Cannot read %s: %s' % (path, e))
        return None
    with f:
        try:
            before = os.fstat(f.fileno())
            for n in iter(lambda: f.readinto(mv), 0):
                h.update(mv[:n])
            after = os.fstat(f.fileno())
        except OSError as e:
            if Verbose >= 1:
                print('Cannot read %s: %s' % (path, e))
            return None
    if (before.st_size, before.st_mtime_ns, before.st_ino) != \
            (after.st_size, after.st_mtime_ns, after.st_ino):
        if Verbose >= 1:
            print('%s changed while being read, skipping it' % path)
        return None
    if entry is not None:
        entry.update(stat_to_dict(after))
    return h.hexdigest()


def read_sample(path, entry, tail=False):
    """Read up to SAMPLE_BYTES from the start (or end) of path, updating
    entry with the stat of the open file.  Returns bytes, or None if the
    file could not be read (it may have vanished or be unreadable).
    """
    try:
        f = open(path, 'rb')
    except OSError as e:
        if Verbose >= 1:
            print('Cannot open %s: %s' % (path, e))
        return None
    with f:
        try:
            st = os.fstat(f.fileno())
            if tail and st.st_size > SAMPLE_BYTES:
                f.seek(st.st_size - SAMPLE_BYTES)
            data = f.read(SAMPLE_BYTES)
            st = os.fstat(f.fileno())
        except OSError as e:
            if Verbose >= 1:
                print('Cannot read %s: %s' % (path, e))
            return None
    if not statlib.S_ISREG(st.st_mode):
        return None
    entry.update(stat_to_dict(st))
    return data


def volume_key(path, entry):
    """Return a key identifying the volume the file resides on.  On Windows
    st_dev is the volume serial number; using the drive portion of the path
    as well also distinguishes volumes on Python versions or filesystems
    that report st_dev as 0.
    """
    return os.path.splitdrive(path)[0].upper(), entry['st_dev']


def inode_key(path, entry):
    """Return a key identifying the file inode, or None when the filesystem
    does not provide file indexes (for instance FAT or exFAT, which also do
    not support hardlinks).
    """
    if not entry['st_ino']:
        return None
    return volume_key(path, entry) + (entry['st_ino'],)


def unchanged(path, entry):
    """Check that the file at path still refers to the same inode with the
    same size and modification time as when its contents were read.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (st.st_ino == entry['st_ino'] and st.st_dev == entry['st_dev'] and
            st.st_size == entry['st_size'] and
            st.st_mtime_ns == entry['st_mtime_ns'])


def refine(files, keyfunc, label):
    """Group the files by keyfunc(path, entry); drop files whose key is None
    (they vanished or became unreadable) and files that land in singleton
    groups (they cannot have a duplicate).  files is modified in place.
    Returns the list of multi-member groups, each a sorted list of paths.
    """
    groups = defaultdict(list)
    for path in sorted(files):
        key = keyfunc(path, files[path])
        if key is None:
            del files[path]
            continue
        groups[key].append(path)
    keep = set()
    result = []
    for paths in groups.values():
        if len(paths) > 1:
            keep.update(paths)
            result.append(paths)
    for path in list(files):
        if path not in keep:
            del files[path]
    if Verbose >= 1:
        print('%s: %d files remain' % (label, len(files)))
    return result


def make_tmp_link(src, other):
    """Link src to a temporary name in other's directory.  Returns the
    temporary path, or None on failure (a message is printed).
    """
    for attempt in range(1000):
        tmp = '%s.dedup-tmp-%d-%d' % (other, os.getpid(), attempt)
        try:
            os.link(src, tmp)
            return tmp
        except FileExistsError:
            continue
        except OSError as e:
            print('Failed to link %s to %s: %s' % (src, tmp, e))
            return None
    print('Failed to find a temporary name for replacing %s' % other)
    return None


def try_link(src, other, other_entry, simulate):
    """Replace other with a hardlink to src.  Returns the bytes saved (0 if
    nothing was done).  The replacement is done by linking src to a
    temporary name and atomically renaming it over other, so other is
    never left damaged if something goes wrong mid-way.
    """
    if not unchanged(other, other_entry):
        if Verbose >= 1:
            print('%s changed or vanished during the run, skipping it' % other)
        return 0
    try:
        src_stat = os.stat(src, follow_symlinks=False)
    except OSError:
        return 0
    if other_entry['st_ino'] and \
            (src_stat.st_ino, src_stat.st_dev) == \
            (other_entry['st_ino'], other_entry['st_dev']):
        return 0  # already a hardlink of src
    if simulate:
        return other_entry['st_size']
    tmp = make_tmp_link(src, other)
    if tmp is None:
        return 0
    try:
        os.replace(tmp, other)
    except OSError as e:
        print('Failed to replace %s with a link to %s: %s' % (other, src, e))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    return other_entry['st_size']


def on_walk_error(err):
    if Verbose >= 3:
        print('Cannot walk: %s' % err)


if __name__ == '__main__':  # noqa
    bases = []
    excludes = []
    help_ = False
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
            help_ = True
    if help_:
        print("""Walk directories and make all identical files hardlinks of each other.
Syntax: dedup_via_hardlinks.py [(root directory) ...] -s -v --exclude=(regex)

If no directory is specified, the current directory is used.
--excludes paths based on a regular expression, tested against both the native
  path separators and forward slashes.  For instance, "\\.py$" excludes python
  files, and "/[.]git/" excludes .git directories.
-s simulates the action, printing what would occur but not doing it.
-v increases verbosity.

Works on both Linux and Windows.  Hardlinks require a filesystem that supports
them (NTFS on Windows; not FAT/exFAT/network shares without hardlink support).

Files that vanish or change during the run are skipped rather than causing
errors.  Comparison is staged like rdfind: files with unique sizes are
discarded first, then files with unique first bytes, then unique last bytes,
and only the remainder is fully hashed.""")
        sys.exit(0)
    if not len(bases):
        bases.append('.')
    files = {}
    for root in bases:
        absroot = os.path.abspath(os.path.expanduser(root))
        for base, _dirs, filenames in os.walk(absroot, onerror=on_walk_error):
            if os.sep + '$RECYCLE.BIN' + os.sep in base:
                continue
            for filename in filenames:
                src = os.path.join(base, filename)
                # Test the exclusion regex against both the native path and
                # a forward-slash version, so patterns like "/[.]git/" work
                # on Windows as well as on Linux.
                slashed = src.replace(os.sep, '/')
                if any(exc.search(src) or exc.search(slashed)
                       for exc in excludes):
                    continue
                try:
                    stats = os.stat(src, follow_symlinks=False)
                except OSError:
                    continue  # vanished or unreadable during the walk
                if not stats.st_ino and not stats.st_dev:
                    continue  # no inode identity available at all
                if not statlib.S_ISREG(stats.st_mode):
                    continue  # also excludes symlinks
                files[src] = stat_to_dict(stats)
    if Verbose >= 1:
        print('Collected %d files' % len(files))
    # Empty files are not candidates, as before.
    files = {path: entry for path, entry in files.items() if entry['st_size']}
    linked = set()
    by_inode = {}
    for path in sorted(files):
        entry = files[path]
        if entry['st_nlink'] > 1:
            inode = inode_key(path, entry)
            if inode is None:
                continue  # filesystem gives no file index (e.g. FAT)
            if inode in by_inode:
                linked.add(path)
            else:
                by_inode[inode] = path
    for path in linked:
        del files[path]
    if Verbose >= 1 and linked:
        print('Dropped %d paths that are already hardlinks of other paths'
              % len(linked))
    refine(files, lambda p, e: e['st_size'],
           'After discarding files with unique sizes')
    if len(files) > 1:
        refine(files, lambda p, e: (e['st_size'], read_sample(p, e)),
               'After discarding files with unique first bytes')
    if len(files) > 1:
        refine(files, lambda p, e: (e['st_size'], read_sample(p, e, tail=True)),
               'After discarding files with unique last bytes')
    if len(files) > 1:
        groups = refine(files, lambda p, e: (e['st_size'], sha512sum(p, e)),
                        'After discarding files with unique hashes')
    else:
        groups = []
    reduced = 0
    for paths in groups:
        # Paths that are already hardlinks of one another (same inode) only
        # need to be considered once: keep the first path of each inode.
        reps = []
        seen_inodes = set()
        for path in paths:
            inode = inode_key(path, files[path])
            if inode is None:
                reps.append(path)  # cannot tell; try linking anyway
            elif inode not in seen_inodes:
                seen_inodes.add(inode)
                reps.append(path)
        if len(reps) < 2:
            continue
        # Hardlinks can only be made within one volume, so link the paths
        # of each volume to that volume's first representative.
        by_volume = defaultdict(list)
        for path in reps:
            by_volume[volume_key(path, files[path])].append(path)
        for volume_paths in by_volume.values():
            if len(volume_paths) < 2:
                continue
            src = volume_paths[0]
            if not unchanged(src, files[src]):
                if Verbose >= 1:
                    print('%s changed during the run, skipping it' % src)
                continue
            for other in volume_paths[1:]:
                saved = try_link(src, other, files[other], simulate)
                reduced += saved
                if saved and Verbose >= 1:
                    print('Link %s to %s (reduced %d)' % (src, other, reduced))
    if Verbose >= 1:
        print('Done: reduced %d bytes in total' % reduced)
