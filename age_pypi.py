#!/usr/bin/env python3

import argparse
import sys
import time

import dateutil.parser
import packaging.version
import requests


def age_pypi(liststr, ageInDays=0):  # noqa
    list1 = set(entry for entry in liststr.strip().replace('\n', ',').split(',')
                if '==' in entry and not entry.strip().startswith('#'))
    age = float(ageInDays)

    packages = {entry.split('==')[0].strip(): entry.split('==')[1].strip() for entry in list1}
    namelen = max([len(key) for key in packages])
    verlen = max([len(val) for val in packages.values()])
    now = time.time()
    then = now - age * 86400

    sys.stdout.write(('%-' + str(namelen) + 's') % 'Package')
    sys.stdout.write((' %-' + str(verlen + 17) + 's') % ('Installed'))
    sys.stdout.write((' %-' + str(verlen + 17) + 's') % ('Previous'))
    sys.stdout.write((' %-' + str(verlen + 17) + 's') % ('Latest'))
    sys.stdout.write('\n')

    for package, val in sorted(packages.items()):  # noqa
        req = requests.get('https://pypi.org/pypi/%s/json' % package)
        pver = packaging.version.parse(val)
        try:
            info = req.json()
        except Exception:
            print('%s - failed' % package)
            continue
        latest = (1e10, 0, None)
        once = (1e100, 0, None)
        instdate = None
        for rkey, release in info['releases'].items():
            ver = packaging.version.parse(rkey)
            if (ver.is_prerelease or ver.is_devrelease) and rkey != val:
                continue
            try:
                rel = next(rentry for rentry in release if not rentry.get('yanked'))
            except Exception:
                continue
            stamp = dateutil.parser.isoparse(rel['upload_time_iso_8601']).timestamp()
            if rkey == val:
                instdate = stamp
            delta = abs(stamp - now)
            if delta < latest[0]:
                latest = (delta, stamp, rkey)
            delta = abs(stamp - then)
            if delta < once[0] and ver <= pver:
                once = (delta, stamp, rkey)
        for dstamp in (latest[2], once[2]):
            verlen = max(verlen, len(dstamp) if dstamp is not None else verlen)
        if instdate and instdate < then:
            once = (0, 0, None)
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
        if latest[2] != val and latest[2] != once[2] and latest[2]:
            sys.stdout.write((' %-' + str(verlen) + 's %s') % (
                latest[2], time.strftime(
                    '%Y-%m-%d %H:%M', time.gmtime(latest[1])) if latest[1] else (' ' * 16)))
        sys.stdout.write('\n')
        sys.stdout.flush()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Check pypi for when a list of packages were released and '
        'if there are recent changes or newer versions.  This helps answer '
        'the question of what changed since a previous install if you do '
        'not have a record of the first install.')
    parser.add_argument(
        'list', help='A of pip packages with versions as reported by pip '
        'install or pip freeze.')
    parser.add_argument(
        '-a', '--age', help='An age in days for checking previous versions.')
    args = parser.parse_args()
    age_pypi(args.list, args.age or 0)
