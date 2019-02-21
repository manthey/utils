#!/usr/bin/python

import os
import sys

Verbose = 0


def format_number(val):
    """Format a number of bytes using no more than five digits, a space,
     and a two letter suffix.
    Enter: val: a number to express as bytes.
    Exit:  formatted_val: the formatted value."""
    factors = " kMGTP"
    if val < 100000:
        return "%5d " % val
    for pos in xrange(len(factors)):
        suffix = factors[pos].strip()
        frm = "%5.3f" % (float(val) / (1 << (pos * 10)))
        d = frm.find(".")
        if d < 0 or d > 3:
            continue
        return frm[:5]+suffix
    return frm.split(".")[0]+suffix


if __name__ == "__main__":  # noqa
    bases = []
    help = False
    depth = 1
    sort = "name"
    for pos in xrange(1, len(sys.argv)):
        arg = sys.argv[pos]
        if arg.startswith("--depth="):
            depth = int(arg.split("=")[1])
        elif arg == "--size":
            sort = "len"
        elif arg == "-v":
            Verbose += 1
        elif not arg.startswith("-") and not arg.startswith("/"):
            bases.append(arg)
        else:
            help = True
    if help:
        print """Find the size of all items in the current path.

Syntax: dirsize.py [(root directory) ...] --depth=(depth) --size -v

If no directory is specified, the current directory is used.
--depth specifies which directories to fully enumerate.  0 shows just the root
  directory, 1 shows all files and directories in the root directory, 2 shows
  all files in the root, and all files and directories in subdirectories of the
  root.  Default is 1.
--size sorts the results by size, smallest to largest.
-v increases verbosity."""
        sys.exit(0)
    if not len(bases):
        bases.append(".")
    for base in bases:
        list = {}
        absroot = os.path.abspath(base)
        absroot = absroot.rstrip('"')
        if len(absroot) > 1:
            absroot = absroot.rstrip("\\")
        orig_absroot = absroot + ('\\' if absroot[-1:] == ':' else '')
        rootlen = len(absroot.replace("\\", "/").split("/"))
        for root, dirs, files in os.walk(orig_absroot, True):
            if Verbose >= 1:
                print root
            for file in files:
                path = os.path.abspath(os.path.join(base, root, file))
                if Verbose >= 2:
                  print path
                pos = len("/".join(path.replace("\\", "/").split(
                    "/")[:rootlen + depth]))
                key = path[len(absroot) + 1:pos]
                if key not in list:
                    list[key] = {
                        "base": base,
                        "name": key,
                        "root": path[len(absroot) + 1:pos],
                        "len": 0
                    }
                try:
                    flen = os.path.getsize(path)
                    list[key]["len"] += flen
                except Exception:
                    pass
        keys = [skey for (sval, skey) in sorted(
            [(list[lkey][sort], lkey) for lkey in list])]
        for key in keys:
            print format_number(list[key]["len"]), key if depth else absroot
