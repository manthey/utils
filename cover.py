#!/usr/bin/python

import os
import re
import subprocess
import sys


def get_coverage(build, collection=None, onlyLocal=False):  # noqa
    """Return a dictionary of all files that are tracked.  Each key is the
     path and each entry is a dictionary of 'total': number of tracked
     statements, 'miss': number of uncovered statements, and 'lines': a
     dictionary of tracked lines with a hit number (0 for missed).
    Enter: build: build directory.
           collection: if present, only files listed in the keyed
                       collection are analyzed.
           onlyLocal: if True, only show local files.
    Exit:  cover: coverage dictionary."""
    files = {'py': 'coverage.xml', 'js': 'js_coverage.xml'}
    if collection:
        for key in files.keys():
            if not collection.get(key, None):
                del files[key]
    cover = {}
    for key in files:
        file = files[key]
        paths = [
            os.path.join(os.path.expanduser(build), file),
            os.path.join(os.path.expanduser(build),
                         '../dist/cobertura/phantomjs', file),
            os.path.join(os.path.expanduser(build), '../coverage/cobertura',
                         file),
        ]
        xml = None
        for path in paths:
            try:
                xml = open(path).read()
                break
            except IOError:
                pass
        if xml is None:
            xml = subprocess.Popen(
                'coverage xml -o -', cwd=os.path.expanduser(build), shell=True,
                stdout=subprocess.PIPE).stdout.read()
        basepath = ''
        if '<source>' in xml:
            basepath = xml.split('<source>', 1)[1].split('</source>')[0]
        else:
            path = os.path.join(os.path.expanduser(build), 'Makefile')
            if not os.path.exists(path):
                continue
            basepath = os.path.abspath(open(path).read().split(
                'CMAKE_SOURCE_DIR = ')[1].split('\n')[0])
        # print '-->', basepath
        parts = xml.split('<class ')
        for part in parts[1:]:
            filename = part.split('filename="', 1)[1].split('"', 1)[0]
            filename = os.path.join(basepath, filename)
            if os.path.realpath(filename).startswith(os.getcwd() + os.sep):
                filename = os.path.realpath(filename)
            if (os.path.abspath(filename) == filename and
                    filename.startswith(os.getcwd() + os.sep)):
                filename = filename[len(os.getcwd() + os.sep):]
            if ((onlyLocal and os.path.abspath(filename) == filename) or
                    'manthey' in filename):
                continue
            lines = {}
            for line in part.split('<line ')[1:]:
                number = int(line.split('number="')[1].split('"')[0])
                hits = int(line.split('hits="')[1].split('"')[0])
                lines[number] = hits
            if len(lines):
                cover[filename] = {
                    'lines': lines,
                    'total': len(lines),
                    'miss': len([line for line in lines if lines[line] <= 0])
                }
    return cover


def git_diff_coverage(cover, diffOptions, full=False):  # noqa
    """Reduce a coverage dictionary to only those lines that are altered
     or added according to git.
    Enter: cover: the original coverage dictionary.
           diffOptions: command line to use with `git diff -U0 (diffOptions)`.
           full: if True, include all lines on the selected files.
    Exit:  cover: the new coverage dictionary."""
    cmd = 'git diff -U0 '+diffOptions
    gitFiles = {}
    gitFile = None
    for line in os.popen(cmd).readlines():
        if line.startswith('+++ b/'):
            gitFile = line[6:].strip()
            diffList = []
            gitFiles[gitFile] = diffList
        elif line.startswith('@@ ') and gitFile:
            parts = line.split()
            lines = None
            if len(parts) >= 2 and parts[1].startswith('+'):
                lines = parts[1]
            elif len(parts) >= 3 and parts[2].startswith('+'):
                lines = parts[2]
            if lines:
                lines = [int(part) for part in lines[1:].split(',')]
                if len(lines) == 1:
                    diffList.append(lines[0])
                else:
                    diffList.extend(range(lines[0], lines[0]+lines[1]))
    newCover = {}
    for file in cover:
        if file in gitFiles:
            if full:
                newCover[file] = cover[file]
                continue
            lines = {}
            for line in cover[file]['lines']:
                try:
                    nextline = min([linenum for linenum in
                                    cover[file]['lines'] if linenum > line])
                except Exception:
                    nextline = None
                try:
                    gitline = min([linenum for linenum in gitFiles[file] if
                                   linenum >= line])
                except Exception:
                    gitline = None
                if (gitline is not None and (nextline is None or
                                             nextline > gitline)):
                    lines[line] = cover[file]['lines'][line]
            if len(lines):
                newCover[file] = {
                    'lines': lines,
                    'total': len(lines),
                    'miss': len([line for line in lines if lines[line] <= 0])
                }
    return newCover


def match_file(file, match=[]):
    """Check if a file matches any of a list of regex.
    Enter: file: the path of the file.
           match: a list of regex to check against.
    Exit:  match: True if the file matches."""
    for exp in match:
        if re.search(exp, file):
            return True
    return False


def show_file(cover, file, altpath=None):
    """Show a single file's source with the a leading character on each
     line to indicate which lines are covered.  > is covered, ! is
     uncovered, and ' ' is not considered.
    Enter: cover: the coverage dictionary returned from get_coverage.
           file: the path to the file.
           altpath: optional path where files may be found if not found
                    directly."""
    if altpath and not os.path.exists(file):
        altfile = os.path.join(altpath, os.path.basename(file))
        data = open(altfile, 'rb').readlines()
    elif os.path.exists(file):
        data = open(file, 'rb').readlines()
    else:
        print '   Cannot find file %s' % file
        return
    for i in xrange(len(data)):
        if not (i+1) in cover[file]['lines']:
            mark = ' '
        elif cover[file]['lines'][i+1] > 0:
            mark = '>'
        else:
            mark = '!'
        print '%c%s' % (mark, data[i].rstrip())


def show_files(cover, files=[], allfiles=False, include=[], exclude=[],
               altpath=None):
    """Show each file's source with the a leading character on each line
     to indicate which lines are covered.
    Enter: cover: the coverage dictionary returned from get_coverage.
           files: a list of files to show.
           allfiles: if True, show all files.  Otherwise, only show files
                     with missed lines.
           include: a list of regex of files to include.
           exclude: a list of regex of files to exclude.
           altpath: optional directory passed to show_file."""
    filelist = cover.keys()
    filelist.sort()
    for file in filelist:
        if ((not len(files) and not len(include)) or file in files or
                match_file(file, include)):
            if match_file(file, exclude):
                continue
            if not allfiles and cover[file]['miss'] <= 0:
                continue
            if len(files) != 1:
                print "==== %s ====" % (file[:68])
            show_file(cover, file, altpath)
            if len(files) != 1:
                print "^^== %s ==^^\n" % (file[:68])


def show_report(cover, files=[], include=[], exclude=[]):
    """Print a report of coverage.  If a set of files is specified, only
     include those.
    Enter: cover: the coverage dictionary returned from get_coverage.
           files: a list of files to restrict the report to.
           include: a list of regex of files to include.
           exclude: a list of regex of files to exclude."""
    filelist = cover.keys()
    filelist.sort()
    print "%-55s%6s%6s%7s\n%s" % ('Name', 'Stmts', 'Miss', 'Cover', '-'*74)
    total = 0
    miss = 0
    for file in filelist:
        if ((not len(files) and not len(include)) or file in files or
                match_file(file, include)):
            if match_file(file, exclude):
                continue
            total += cover[file]['total']
            miss += cover[file]['miss']
            percent = int(100 * (cover[file]['total'] - cover[file]['miss']) /
                          cover[file]['total'])
            if cover[file]['miss'] and percent == 100:
                percent = 99
            if cover[file]['miss'] != cover[file]['total'] and percent == 0:
                percent = 1
            print "%-55s%6d%6d%6d%%" % (
                file[-55:], cover[file]['total'], cover[file]['miss'], percent)
    print "%s" % ('-' * 74)
    if total:
        print "%-55s%6d%6d%9.2f%%" % ('TOTAL', total, miss,
                                      100. * (total - miss) / total)


if __name__ == '__main__':  # noqa
    help = False
    allfiles = None
    gitdiff = False
    gitdifffull = False
    build = '~/girder-build'
    for buildpath in ('_build', 'build'):
        if os.path.exists(buildpath):
            build = buildpath
            break
    altpath = None
    files = []
    exclude = []
    include = []
    report = None
    collection = {}
    onlyLocal = False
    for arg in sys.argv[1:]:
        if arg.startswith('-'):
            if arg == '--all':
                allfiles = True
            elif arg.startswith('--alt='):
                altpath = arg.split('=', 1)[1]
            elif arg.startswith('--build='):
                build = arg.split('=', 1)[1]
            elif arg == '--diff':
                gitdiff = 'master'
            elif arg.startswith('--diff='):
                gitdiff = arg.split('=', 1)[1]
            elif arg.startswith('--exclude='):
                exclude.append(arg.split('=', 1)[1])
            elif arg == '--full':
                gitdifffull = True
            elif arg == '--global':
                onlyLocal = False
            elif arg.startswith('--include='):
                include.append(arg.split('=', 1)[1])
            elif arg in ('--js', '--py'):
                collection[arg[2:]] = True
            elif arg == '--local':
                onlyLocal = True
            elif arg == '--report':
                report = True
            elif arg == '--show':
                report = False
            else:
                help = True
        elif arg == 'report':
            report = True
        else:
            files.append(arg)
    if help:
        print """Combine python and javascript coverage reports.

Syntax: cover.py [--report|--show] [--build=(build path)] [--js|--py] [--all]
                 [--local|--global] [--diff[=(diff options)] [--full]]
                 [--include=(regex)] [--exclude=(regex)] [--alt=(path)]
                 [(files ...)]

--all annotates all files.  Otherwise, only files that have missed statements
  are annotated.  This doesn't have any effect on reports.
--alt specifies a directory where files may exist if they aren't where they are
  listed in the coverage report.
--build specifies where the coverage files are located.  This defaults to
 ~/girder-build.
--diff only checks lines that were altered according to `git diff -U0 (diff
  options)`.  If no options are specified, "master" is used.
--exclude excludes files based on a regex.
--full, when used with --diff, does the full diff on the files selected by the
  --diff option, rather than on just lines that were altered.
--global shows all files regardless of their directory location.
--include includes files based on a regex.
--js and --py limit the files analyzed to that source.
--local only shows source files that are within the current working directory.
--report summarizes results rather than show annotated files.
--show displays annotated files rather than list a report.
Default is to list a report of all files.
If files are specified and report is not, an annotated line listing of those
files is given.  With report specified, just the summary of those files is
given."""
        sys.exit(0)
    cover = get_coverage(build, collection, onlyLocal)
    if gitdiff:
        cover = git_diff_coverage(cover, gitdiff, gitdifffull)
    if report or (not len(files) and report is not False):
        show_report(cover, files, include, exclude)
    else:
        show_files(cover, files, allfiles, include, exclude, altpath)
