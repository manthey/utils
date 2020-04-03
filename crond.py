#!/usr/bin/env python

import crontab
import os
import servicemanager
import subprocess
import sys
import threading
import time
import win32event
import win32service
import win32serviceutil


LogLock = threading.Lock()
LogPath = '~'


class CronSvc(win32serviceutil.ServiceFramework):
    _svc_name_ = 'CronSvc'
    _svc_display_name_ = 'Cron for Windows Service'
    _svc_description_ = (
        'Provide cron for Windows.')

    # import crond
    # svcPath = (os.path.splitext(os.path.abspath(crond.__file__))[0] + '.' +
    #            _svc_name_)
    svcPath = (os.path.splitext(os.path.abspath(__file__))[0] + '.' +
               _svc_name_)

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.waitStop = win32event.CreateEvent(None, 0, 0, None)
        self.halt = False
        # self.service = None

    def SvcStop(self):
        self.halt = True
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.waitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ''))
        status = {}
        while not self.halt:
            cron(status)
        # self.service = threading.Thread(target=self.run_service)
        # self.service.start()

    def run_service(self):
        status = {}
        while not self.halt:
            cron(status)


def cron(status, maxdelay=5, verbose=0):  # noqa
    """
    Run a cron service.  Check if the file has changed, wait for an event, and
    trigger services as needed.

    Enter: status: an object to track status.
           maxdelay: maximum delay in seconds.
           verbose: verbosity level.
    """
    if 'path' not in status:
        path = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])),
                            'crontab.txt')
        if not os.path.exists(path):
            path = os.path.realpath(os.path.expanduser('~/.cron/crontab.txt'))
        if not os.path.exists(path):
            path = os.path.realpath(os.path.expanduser('~/crontab.txt'))
        status['path'] = path
        # This raises an appropriate error if the path doesn't exist
        mtime = os.path.getmtime(status['path'])
        global LogPath
        LogPath = os.path.dirname(status['path'])
    mtime = os.path.getmtime(status['path'])
    if mtime != status.get('mtime'):
        old = []
        if 'table' in status:
            for entry in status['table']:
                if 'thread' in entry:
                    entry['thread'].join(0)
                    if not entry['thread'].isAlive():
                        del entry['thread']
                    else:
                        entry['old'] = True
                        old.append(entry)
        status['mtime'] = mtime
        lines = open(status['path']).readlines()
        lines = [line.strip() for line in lines]
        if verbose >= 3:
            print('  All lines')
            print('\n'.join(lines))
        lines = [line for line in lines if not line.startswith('#')]
        lines = [line for line in lines if len(line.split()) >= 6]
        if verbose >= 2:
            print('  All usable lines')
            print('\n'.join(lines))
        entries = []
        for line in lines:
            try:
                entries.append({
                    'cronrecord': ' '.join(line.split()[:5]),
                    'cron': crontab.CronTab(' '.join(line.split()[:5])),
                    'command': line.split(None, 5)[-1],
                    'running': False,
                })
            except Exception:
                if verbose >= 1:
                    print('Failed to parse %s' % line)
        curtime = time.time()
        for entry in entries:
            entry['next'] = time.time() + entry['cron'].next(default_utc=False) + 0.1
        if verbose >= 1:
            print('  Delay until next run:')
            for entry in entries:
                print('%5.3fs - %s' % (
                    entry['next'] - curtime, entry['command']))
        log('Loaded crontab.txt with %d active entries' % len(entries), 1)
        entries.extend(old)
        status['table'] = entries
    curtime = time.time()
    delay = maxdelay
    for entry in status['table']:
        if 'thread' in entry:
            entry['thread'].join(0)
            if not entry['thread'].isAlive():
                del entry['thread']
        if entry.get('old'):
            continue
        if curtime > entry['next'] and 'thread' not in entry:
            entry['next'] = time.time() + entry['cron'].next(default_utc=False) + 0.1
            start_command(entry, verbose)
        else:
            delay = min(entry['next'] + 0.1 - curtime, delay)
    if delay <= 0:
        delay = maxdelay
    if verbose >= 3:
        print('wait: %5.3f' % delay)
    time.sleep(delay)


def log(msg, verbose=0):
    """
    Write a message to the log file, and possibly to stdout.

    Enter: msg: the message to write.
           verbose: verbosity, used to determine if the log should write to
                    stdout.
    """
    data = '%s - %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg.strip())
    path = os.path.join(LogPath, 'cron.log')
    with LogLock:
        open(path, 'at').write(data + '\n')
        if verbose >= 1:
            print(data)


def run_command(entry, verbose=0):
    """
    Run a command and store the result.

    Enter: entry: an entry containing the command to run and a location to
                  store the result.
    """
    try:
        process = subprocess.Popen(entry['command'], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if not process.returncode:
            entry['result'] = 'Finished %s' % entry['command']
        else:
            entry['result'] = 'Failed %s (returned %r)' % (
                entry['command'], process.returncode)
            entry['result'] += '\n  stdout:\n' + stdout
            entry['result'] += '\n  stderr:\n' + stderr
    except Exception as exc:
        entry['result'] = 'Exception %s\n%r' % (entry['command'], exc)
    log(entry['result'], verbose)


def start_command(entry, verbose=0):
    """
    Start running a command, logging the results.
    """
    log('Running %s' % entry['command'], verbose)
    entry['thread'] = threading.Thread(target=run_command, args=(entry, verbose))
    entry['thread'].daemon = True
    entry['thread'].start()


if __name__ == '__main__':  # noqa
    help = False
    verbose = 1
    windowsService = False
    for arg in sys.argv[1:]:
        if arg in ('-q', '/q', '--quiet', '/quiet'):
            verbose -= 1
        elif arg in ('-v', '/v', '--verbose', '/verbose'):
            verbose += 1
        elif arg in ('install', 'remove', 'start', 'stop'):
            windowsService = True
        elif arg in ('-h', '/h', '/?', '--help', '/help'):
            help = 'help'
        else:
            help = True
    if (help and not windowsService) or help == 'help':
        print("""Run a cron service on windows.

Syntax: cron.py install|remove|start|stop
  -q -v

A crontab.txt file must be in the same directory as this python script.

If one of 'install', 'remove', 'start', or 'stop' is used, this program will be
treated as a Windows service.  The other command line parameters are ignored,
and a different set of options are available (which must be placed before the
service action):
  Options for 'install' and 'update' commands only:
    --username (domain\\username) : username the service is to run under.  The
      domain is probably the computer name, not the workgroup.
    --password (password) : password for the username.
    --startup [manual|auto|disabled] : how the service starts (default is
      manual).
  Options for 'start' and 'stop' commands only:
    --wait (seconds): wait for the service to actually start or stop.
-q or --quiet decreases the verbosity.
-v or --verbose increases the verbosity.""")
        sys.exit(0)
    if windowsService or len(sys.argv) == 1:
        if len(sys.argv) == 1:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(CronSvc)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            win32serviceutil.HandleCommandLine(
                CronSvc, argv=sys.argv, serviceClassString=CronSvc.svcPath)
        sys.exit(0)
    status = {}
    while True:
        cron(status, 15, verbose)
