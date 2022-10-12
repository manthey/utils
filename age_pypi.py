#!/usr/bin/env python3

import sys
import time

import dateutil.parser
import packaging.version
import requests

list1 = set(entry for entry in sys.argv[1].strip().replace('\n', ',').split(',')
            if '==' in entry and not entry.strip().startswith('#'))
age = float(sys.argv[2]) if len(sys.argv) >= 3 else None  # in days

packages = {entry.split('==')[0].strip(): entry.split('==')[1].strip() for entry in list1}
namelen = max([len(key) for key in packages])
verlen = max([len(val) for val in packages.values()])
now = time.time()
then = now if age is None else now - age * 86400

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
        if ver.is_prerelease or ver.is_devrelease:
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
    if instdate < then:
        once = (0, 0, None)
    sys.stdout.write(('%-' + str(namelen) + 's') % package)
    sys.stdout.write((' %-' + str(verlen) + 's %s') % (
        val, time.strftime(
            '%Y-%m-%d %H:%M', time.gmtime(instdate)) if instdate else (' ' * 16)))
    if once[2] != val and once[2]:
        sys.stdout.write((' %-' + str(verlen) + 's %s') % (
            once[2], time.strftime(
                '%Y-%m-%d %H:%M', time.gmtime(once[1])) if instdate else (' ' * 16)))
    else:
        sys.stdout.write(' ' * (verlen + 16 + 2))
    if latest[2] != val and latest[2] != once[2] and latest[2]:
        sys.stdout.write((' %-' + str(verlen) + 's %s') % (
            latest[2], time.strftime(
                '%Y-%m-%d %H:%M', time.gmtime(latest[1])) if instdate else (' ' * 16)))
    sys.stdout.write('\n')
    sys.stdout.flush()
