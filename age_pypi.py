#!/usr/bin/env python3

import argparse
import os
import sys
import time

import dateutil.parser
import packaging.specifiers
import packaging.version
import requests


def show_header(header, namelen, verlen):
    if not header:
        sys.stdout.write(('%-' + str(namelen) + 's') % 'Package')
        sys.stdout.write((' %-' + str(verlen + 17) + 's') % ('Installed'))
        sys.stdout.write((' %-' + str(verlen + 17) + 's') % ('Previous'))
        sys.stdout.write((' %-' + str(verlen + 17) + 's') % ('Latest'))
        sys.stdout.write('\n')
    return True


def age_pypi(liststr, ageInDays=0, onlyDifferent=False, pyversion=None, onlyBinary=False):  # noqa
    list1 = {entry for entry in liststr.strip().replace('\n', ',').split(',')
             if '==' in entry and not entry.strip().startswith('#')}
    age = float(ageInDays)

    packages = {entry.split('==')[0].strip(): entry.split('==')[1].strip() for entry in list1}
    namelen = max([len(key) for key in packages] + [7])
    verlen = max([len(val) for val in packages.values()] + [5])
    now = time.time()
    then = now - age * 86400

    if pyversion:
        pyversion = packaging.version.Version(pyversion)

    header = False
    for package, val in sorted(packages.items()):  # noqa
        req = requests.get('https://pypi.org/pypi/%s/json' % package)
        pver = packaging.version.parse(val)
        try:
            info = req.json()
        except Exception:
            header = show_header(header, namelen, verlen)
            print('%s - failed' % package)
            continue
        latest = (1e10, 0, None)
        once = (1e100, 0, None)
        instdate = None
        if 'releases' not in info:
            header = show_header(header, namelen, verlen)
            print('%s - failed (no releases)' % package)
            continue
        for rkey, release in info['releases'].items():
            try:
                ver = packaging.version.parse(rkey)
            except Exception:
                continue
            if (ver.is_prerelease or ver.is_devrelease) and rkey != val:
                continue
            rel = None
            try:
                for rentry in release:
                    if rentry.get('yanked'):
                        continue
                    if pyversion and rentry.get('requires_python'):
                        if pyversion not in packaging.specifiers.SpecifierSet(
                                rentry['requires_python']):
                            continue
                    if pyversion and rentry.get('python_version'):
                        pv = rentry.get('python_version')
                        if pv.startswith('py') and pyversion.major == int(pv[2]):
                            pass
                        elif pv.startswith('cp'):
                            if pyversion.major != int(pv[2]) or pyversion.minor != int(pv[3:]):
                                continue
                        elif pv == 'source':
                            if rel is None and onlyBinary == 'prefer':
                                rel = rentry
                            if onlyBinary:
                                continue
                        else:
                            continue
                    rel = rentry
                    break
            except Exception:
                continue
            if not rel:
                continue
            stamp = dateutil.parser.isoparse(rel['upload_time_iso_8601']).timestamp()
            if rkey == val:
                instdate = stamp
            delta = abs(stamp - now)
            if delta < latest[0]:
                latest = (delta, stamp, rkey)
            delta = abs(stamp - then)
            if delta < once[0] and ver < pver:
                once = (delta, stamp, rkey)
        for dstamp in (latest[2], once[2]):
            verlen = max(verlen, len(dstamp) if dstamp is not None else verlen)
        if instdate and instdate < then:
            once = (0, 0, None)

        if (onlyDifferent and (not instdate or (not once[2] or once[2] == val)) and
                (not latest[2] or latest[2] == val or (once[2] and latest[2] == once[2]))):
            continue

        header = show_header(header, namelen, verlen)
        sys.stdout.write(('%-' + str(namelen) + 's') % package)
        sys.stdout.write((' %-' + str(verlen) + 's %s') % (
            val, time.strftime(
                '%Y-%m-%d %H:%M', time.gmtime(instdate)) if instdate else (' ' * 16)))
        if once[2] != val and once[2]:
            sys.stdout.write((' %-' + str(verlen) + 's %s') % (
                once[2], time.strftime(
                    '%Y-%m-%d %H:%M', time.gmtime(once[1])) if once[1] else (' ' * 16)))
        else:
            sys.stdout.write(' ' * (verlen + 16 + 2))
        if latest[2] != val and (not once[2] or latest[2] != once[2]) and latest[2]:
            sys.stdout.write((' %-' + str(verlen) + 's %s') % (
                latest[2], time.strftime(
                    '%Y-%m-%d %H:%M', time.gmtime(latest[1])) if latest[1] else (' ' * 16)))
        sys.stdout.write('\n')
        sys.stdout.flush()
    return header


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Check pypi for when a list of packages were released and '
        'if there are recent changes or newer versions.  This helps answer '
        'the question of what changed since a previous install if you do '
        'not have a record of the first install.')
    parser.add_argument(
        'list', help='A list of pip packages with versions as reported by pip '
        'install or pip freeze.  This can also be a file with such a record.')
    parser.add_argument(
        '-a', '--age', help='An age in days for checking previous '
        'versions.  0 for only report if there are newer versions.')
    parser.add_argument(
        '--python', '-p', help='The python version to check (e.g., 3.11).')
    parser.add_argument(
        '--binary', '-b', action='store_true', help='Packages must have '
        'wheels.')
    parser.add_argument(
        '--prefer-binary', '--prefer', dest='binary', action='store_const',
        const='prefer', help='Prefer packages with wheels.')
    parser.add_argument(
        '-q', '--only', action='store_true', help='Only report packages '
        'that have reported previous or newer versions.')
    args = parser.parse_args()
    if os.path.isfile(args.list):
        args.list = open(args.list).read()
    anyShown = age_pypi(args.list, args.age or 0, args.only, args.python, args.binary)
    if args.only and not anyShown:
        sys.exit(1)
