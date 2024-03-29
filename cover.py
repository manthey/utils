#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys


Verbose = 0


def localizeFilename(filename):
    """
    Convert a filename to a path relative to the current working directory, if
    reasonable.  This resolves bind mounts, too.

    Enter: filename: the name of the file to convert.
    Exit:  filename: the possibly localized filename.
           isLocal: true if the filename is relative to the cwd.
    """
    cwd = os.getcwd() + os.sep
    filename = os.path.realpath(filename)
    if (os.path.abspath(filename) == filename and filename.startswith(cwd)):
        filename = filename[len(cwd):]
    if '/site-packages/' in filename and os.path.exists(filename.split('/site-packages/')[1]):
        filename = filename.split('/site-packages/')[1]
    isLocal = filename != os.path.abspath(filename)
    if not isLocal:
        try:
            mounts = [mount for mount in json.loads(
                os.popen('findmnt -m -J 2>/dev/null').read())['filesystems']
                if mount.get('target') and mount.get('source') and
                '[' in mount['source']]
            for mount in mounts:
                if filename.startswith(mount['target'] + os.sep):
                    altname = (mount['source'].split('[', 1)[1].split(']')[0] +
                               filename[len(mount['target']):])
                    if os.path.exists(altname) and altname.startswith(cwd):
                        filename = altname[len(cwd):]
                        isLocal = True
                        break
        except Exception:
            pass
    return filename, isLocal


def add_xml_to_coverage(xml, cover, onlyLocal=False):
    """
    Add an xml coverage record to the total coverage data.

    Enter: xml: xml with coverage information.
           cover: coverage dictionary to modify.
           onlyLocal: if True, omit files that are in different root
                      directories.
    """
    basepath = ''
    if '<source>' in xml:
        basepath = xml.split('<source>', 1)[1].split('</source>')[0]
    else:
        path = os.path.join(os.path.expanduser(build), 'Makefile')
        if not os.path.exists(path):
            return
        basepath = os.path.abspath(open(path, 'rt').read().split(
            'CMAKE_SOURCE_DIR = ')[1].split('\n')[0])
    if Verbose >= 1:
        print('basepath: %s' % basepath)
    parts = xml.split('<class ')
    for part in parts[1:]:
        filename = part.split('filename="', 1)[1].split('"', 1)[0]
        filename = os.path.join(basepath, filename)
        filename, isLocal = localizeFilename(filename)
        if (filename.startswith('build/') or filename.startswith('_build/') or
                filename.startswith('.tox/')):
            isLocal = False
        if ((onlyLocal and not isLocal) or 'manthey' in filename):
            continue
        lines = {}
        partial = {}
        for line in part.split('<line ')[1:]:
            number = int(line.split('number="')[1].split('"')[0])
            hits = int(line.split('hits="')[1].split('"')[0])
            branch = ([int(val) for val in
                       line.split('condition-coverage="')[1].split()[1].split(
                           '"')[0].strip('()').split('/')]
                      if 'condition-coverage' in line else None)
            lines[number] = hits
            if branch and branch[0] and branch[0] != branch[1]:
                partial[number] = branch
        if len(lines):
            if filename in cover:
                for number in cover[filename]['lines']:
                    lines[number] = max(
                        lines.get(number) or 0,
                        cover[filename]['lines'][number])
                    if number in partial and number not in cover[filename]['partial']:
                        del partial[number]
            cover[filename] = {
                'lines': lines,
                'total': len(lines),
                'miss': len([line for line in lines if lines[line] <= 0]),
                'partial': partial,
                'npartial': (len(partial),
                             sum([partial[p][0] for p in partial]),
                             sum([partial[p][1] for p in partial])),
            }


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
    files = {'coverage.xml': 'py', 'py_coverage.xml': 'py',
             'js_coverage.xml': 'js', 'cobertura-coverage.xml': 'js'}
    if collection:
        for file in list(files.keys()):
            if not collection.get(files[file], None):
                del files[file]
    cover = {}
    for file in files:
        paths = [
            os.path.join(os.path.expanduser(build), file),
            os.path.join(os.path.expanduser(build), 'coverage', file),
            os.path.join(os.path.expanduser(build), '../coverage/cobertura',
                         file),
            os.path.join(os.path.expanduser(build),
                         '../build/test/coverage', file),
            os.path.join(os.path.expanduser(build),
                         '../build/test/coverage/web', file),
            os.path.join(os.path.expanduser(build),
                         '../build/test/artifacts/web_coverage', file),
            os.path.join(os.path.expanduser(build), '../.tox/coverage', file),
        ]
        root = os.path.join(os.path.expanduser(build), '../dist/cobertura')
        if os.path.isdir(root):
            for subdir in os.listdir(root):
                if os.path.isdir(os.path.join(root, subdir)):
                    paths.append(os.path.join(root, subdir, file))
        paths.append(None)
        anyPath = False
        for path in paths:
            if Verbose >= 3:
                print('Check: %s' % path)
            xml = None
            if path is not None and os.path.exists(path):
                try:
                    xml = open(path, 'rt').read()
                    if Verbose >= 1:
                        print('XML: %s' % path)
                    anyPath = True
                except IOError:
                    continue
            if path is None and not anyPath:
                xml = subprocess.Popen(
                    'coverage xml -o -', cwd=os.path.expanduser(build),
                    shell=True,
                    stdout=subprocess.PIPE).stdout.read()
                if xml and not isinstance(xml, str):
                    xml = xml.decode()
                if xml and Verbose >= 1:
                    print('XML: coverage %s' % build)
            if xml:
                add_xml_to_coverage(xml, cover, onlyLocal)
    return cover


def git_diff_coverage(cover, diffOptions, full=False):  # noqa
    """Reduce a coverage dictionary to only those lines that are altered
     or added according to git.
    Enter: cover: the original coverage dictionary.
           diffOptions: command line to use with `git diff -U0 (diffOptions)`.
           full: if True, include all lines on the selected files.
    Exit:  cover: the new coverage dictionary."""
    cmd = 'git diff -U0 ' + diffOptions
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
            partial = {}
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
                    if line in cover[file]['partial']:
                        partial[line] = cover[file]['partial'][line]
            if len(lines):
                newCover[file] = {
                    'lines': lines,
                    'total': len(lines),
                    'miss': len([line for line in lines if lines[line] <= 0]),
                    'partial': partial,
                    'npartial': (len(partial),
                                 sum([partial[p][0] for p in partial]),
                                 sum([partial[p][1] for p in partial])),
                }
    return newCover


def match_file(file, match=[]):
    """
    Check if a file matches any of a list of regex.

    Enter: file: the path of the file.
           match: a list of regex to check against.
    Exit:  match: True if the file matches.
    """
    for exp in match:
        if re.search(exp, file):
            return True
    return False


def show_file(cover, file, altpath=None, reportPartial=False):
    """
    Show a single file's source with the a leading character on each line to
    indicate which lines are covered.  > is covered, ! is uncovered, and ' '
    is not considered.

    Enter: cover: the coverage dictionary returned from get_coverage.
           file: the path to the file.
           altpath: optional path where files may be found if not found
                    directly.
           reportPartial: True to include branch coverage.
    """
    if altpath and not os.path.exists(file):
        altfile = os.path.join(altpath, os.path.basename(file))
        data = open(altfile, 'rt').readlines()
    elif os.path.exists(file):
        data = open(file, 'rt').readlines()
    else:
        print('   Cannot find file %s' % file)
        return
    for i in range(len(data)):
        if not (i+1) in cover[file]['lines']:
            mark = ' '
        elif reportPartial and cover[file]['partial'].get(i+1):
            mark = (('%d' % cover[file]['partial'][i+1][0])
                    if cover[file]['partial'][i+1][0] < 10 else '+')
        elif cover[file]['lines'][i+1] > 0:
            mark = '>'
        else:
            mark = '!'
        print('%c%s' % (mark, data[i].rstrip()))


def show_files(cover, files=[], allfiles=False, include=[], exclude=[],
               altpath=None, reportPartial=False):
    """
    Show each file's source with the a leading character on each line to
    indicate which lines are covered.

    Enter: cover: the coverage dictionary returned from get_coverage.
           files: a list of files to show.
           allfiles: if True, show all files.  Otherwise, only show files
                     with missed lines.
           include: a list of regex of files to include.
           exclude: a list of regex of files to exclude.
           altpath: optional directory passed to show_file.
           reportPartial: True to include branch coverage.
    """
    filelist = list(cover.keys())
    filelist.sort()
    for file in filelist:
        if ((not len(files) and not len(include)) or file in files or
                match_file(file, include)):
            if match_file(file, exclude):
                continue
            if (not allfiles and cover[file]['miss'] <= 0 and
                    (not reportPartial or cover[file]['npartial'][0] <= 0)):
                continue
            if len(files) != 1:
                print('==== %s ====' % (file[:68]))
            show_file(cover, file, altpath, reportPartial)
            if len(files) != 1:
                print('^^== %s ==^^\n' % (file[:68]))


def show_report(cover, files=[], include=[], exclude=[], reportPartial=False):
    """
    Print a report of coverage.  If a set of files is specified, only include
    those.

    Enter: cover: the coverage dictionary returned from get_coverage.
           files: a list of files to restrict the report to.
           include: a list of regex of files to include.
           exclude: a list of regex of files to exclude.
           reportPartial: True to include branch coverage.
    """
    filelist = list(cover.keys())
    filelist.sort()
    if reportPartial:
        print('%-51s%6s%6s%6s%7s\n%s' % ('Name', 'Stmts', 'Part', 'Miss', 'Cover', '-'*76))
    else:
        print('%-55s%6s%6s%7s\n%s' % ('Name', 'Stmts', 'Miss', 'Cover', '-'*74))
    total = 0
    miss = 0
    partial = [0, 0, 0]
    for file in filelist:
        if ((not len(files) and not len(include)) or file in files or
                match_file(file, include)):
            if match_file(file, exclude):
                continue
            ftotal = cover[file]['total']
            fmiss = cover[file]['miss']
            fpartial = cover[file]['npartial']
            if not reportPartial:
                fpartial = (0, 0, 0)
            fhit = ftotal - fmiss - fpartial[0]
            total += ftotal
            miss += fmiss
            partial[0] += fpartial[0]
            partial[1] += fpartial[1]
            partial[2] += fpartial[2]
            if reportPartial and fpartial[0]:
                percent = int(100 * (
                    fhit + fpartial[0] * float(fpartial[1]) / fpartial[2]) / ftotal)
            else:
                percent = int(100 * fhit / ftotal)
            if (fmiss or fpartial[0]) and percent == 100:
                percent = 99
            if fmiss != ftotal and percent == 0:
                percent = 1
            if reportPartial:
                print('%-51s%6d%6d%6d%6d%%' % (
                    file[-51:], ftotal, fpartial[0], fmiss, percent))
            else:
                print('%-55s%6d%6d%6d%%' % (
                    file[-55:], ftotal, fmiss, percent))
    if not total:
        print('No lines to check')
    elif reportPartial:
        print('%s\n%-51s%6d%6d%6d%9.2f%%' % (
            '-'*76, 'TOTAL', total, partial[0], miss, 100.0 * (
                total - miss - partial[0] * float(partial[2] - partial[1]) /
                (partial[2] or 1)) / total))
    else:
        print('%s\n%-55s%6d%6d%9.2f%%' % (
            '-'*74, 'TOTAL', total, miss, 100.0 * (total - miss) / total))


if __name__ == '__main__':  # noqa
    if not os.path.isdir('.git') and os.path.isdir('../.git'):
        os.chdir('..')
    help = False
    allfiles = None
    gitdiff = False
    gitdifffull = False
    build = '~/girder-build'
    for buildpath in ('_build', 'build', '~/girder/_build', '.tox', 'coverage'):
        if os.path.exists(buildpath):
            build = buildpath
            break
    altpath = None
    files = []
    exclude = []
    include = []
    report = None
    reportPartial = False
    collection = {}
    onlyLocal = True
    for arg in sys.argv[1:]:
        if arg.startswith('-'):
            if arg == '--all':
                allfiles = True
            elif arg.startswith('--alt='):
                altpath = arg.split('=', 1)[1]
            elif arg in ('--branch', '--partial'):
                reportPartial = True
            elif arg.startswith('--build='):
                build = arg.split('=', 1)[1]
            elif arg == '--diff':
                try:
                    gitdiff = os.popen('git merge-base HEAD master').read().strip()
                    if len(gitdiff) != 40:
                        gitdiff = False
                except Exception:
                    pass
                if not gitdiff:
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
            elif arg == '--line':
                reportPartial = False
            elif arg == '--local':
                onlyLocal = True
            elif arg == '--report':
                report = True
            elif arg == '--show':
                report = False
            elif arg in ('--verbose', '-v'):
                Verbose += 1
            else:
                help = True
        elif arg == 'report':
            report = True
        else:
            files.append(arg)
    if help:
        print("""Combine python and javascript coverage reports.

Syntax: cover.py [--report|--show] [--build=(build path)] [--js|--py] [--all]
                 [--local|--global] [--diff[=(diff options)] [--full]]
                 [--branch|--line] [--include=(regex)] [--exclude=(regex)]
                 [--alt=(path)] [(files ...)]

--all annotates all files.  Otherwise, only files that have missed statements
  are annotated.  This doesn't have any effect on reports.
--alt specifies a directory where files may exist if they aren't where they are
  listed in the coverage report.
--branch reports partial (branch) coverage.
--build specifies where the coverage files are located.  This defaults to
 ~/girder-build.
--diff only checks lines that were altered according to `git diff -U0 (diff
  options)`.  If no options are specified, "master" is used (technically
  `git merge-base HEAD master`).
--exclude excludes files based on a regex.
--full, when used with --diff, does the full diff on the files selected by the
  --diff option, rather than on just lines that were altered.
--global shows all files regardless of their directory location.
--include includes files based on a regex.
--js and --py limit the files analyzed to that source.
--line reports line coverage (partial coverage is considered coverage).
--local only shows source files that are within the current working directory.
--report summarizes results rather than show annotated files.
--show displays annotated files rather than list a report.
Default is to list a report of all files.
If files are specified and report is not, an annotated line listing of those
files is given.  With report specified, just the summary of those files is
given.""")
        sys.exit(0)
    cover = get_coverage(build, collection, onlyLocal)
    if gitdiff:
        cover = git_diff_coverage(cover, gitdiff, gitdifffull)
    if report or (not len(files) and report is not False):
        show_report(cover, files, include, exclude, reportPartial)
    else:
        show_files(cover, files, allfiles, include, exclude, altpath, reportPartial)
