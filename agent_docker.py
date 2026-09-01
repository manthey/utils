#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = [
#   'pyyaml',
# ]
# ///

import argparse
import fnmatch
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import yaml

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.rstrip(' .')
    return name or 'docker'


def list_known(docker_cmd: list[str], base_name: str):
    cmd = docker_cmd + ['ps', '-a', '--format', '{{.ID}}\t{{.Names}}\t{{.Image}}']
    logger.info(cmd)
    output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    found = []
    for line in output.strip().split('\n'):
        parts = (line or '').split('\t')
        if len(parts) != 3:
            continue
        container_id, name, image = tuple(parts)
        if (name != base_name and not re.search(r'^agent_', name) and
                not re.search(r'/agent', image)):
            continue
        found.append((name, image, container_id))
    found.sort()
    for name, image, container_id in found:
        print(name, container_id, image)


def rename_if_exists(docker_cmd, container_name):
    cmd = docker_cmd + ['ps', '-a', '--format', '{{.ID}}\t{{.Names}}']
    logger.info(cmd)
    output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    for line in output.strip().split('\n'):
        parts = tuple(line.split('\t'))
        if len(parts) == 2 and parts[1] == container_name:
            cmd = docker_cmd + ['rename', container_name, parts[0]]
            logger.info(cmd)
            subprocess.run(cmd, stderr=subprocess.DEVNULL, check=False)
            return parts[0]


def get_mount_args(is_windows, more=None):
    mounts = [{
        'src': [os.path.expanduser('~/.vimrc'), os.path.expanduser('~/_vimrc')],
        'dst': '/home/ubuntu/.vimrc',
        'mode': 'ro',
    }, {
        'src': [os.path.expanduser('~/.vim_backup')],
        'dst': '/home/ubuntu/.vim_backup',
        'mode': 'rw',
    }, {
        'src': [os.path.expanduser('~/.vim')],
        'dst': '/home/ubuntu/.vim',
        'mode': 'ro',
    }, {
        'src': [os.path.expanduser('~/.pi/agent/sessions')],
        'dst': '/home/ubuntu/.pi/agent/sessions',
        'mode': 'rw',
    }]
    for entry in (more or []):
        mode = 'ro'
        parts = entry.split(':')
        if parts[-1] in {'ro', 'rw'}:
            mode = parts[-1]
            parts = parts[:-1]
        mounts.append({'src': [':'.join(parts[:-1])], 'dst': parts[-1], 'mode': mode})
    args = []
    for mount in mounts:
        paths = [path for path in mount['src'] if os.path.exists(path)]
        if not len(paths):
            continue
        path = paths[0]
        if is_windows and path[1] == ':':
            path = '/mnt/' + path[0].lower() + '/' + path[2:].replace('\\', '/')
        if ':' not in path:
            args.extend(['-v', f'{path}:{mount["dst"]}:{mount["mode"]}'])
    return args


def free_port(docker_cmd, start):
    used = {}
    report = subprocess.check_output(
        docker_cmd + ['container', 'ls', '--format', '{{.Ports}}', '-a']).decode()
    for entry in report.replace(',', '\n').replace('\r', '\n').split('\n'):
        val = entry.split('->')[0].split('/')[0].split(':')[-1]
        if not val:
            continue
        if '-' in val:
            parts = val.split('-')
            for p in range(int(parts[0]), int(parts[1]) + 1, 1):
                used[p] = True
        else:
            used[int(val)] = True
    logger.debug('Used ports: %r', sorted(used.keys()))
    port = start
    while port in used:
        port += 1
    return port


def add_config(args, src, parser):
    if not os.path.exists(args.config):
        return args
    try:
        config = yaml.safe_load(open(args.config))
    except Exception as exc:
        print('Failed to parse config file', exc)
        return args
    args = sys.argv[2:]
    try:
        if src in config:
            args = config[src] + args
        if 'all' in config:
            args = config['all'] + args
        return parser.parse_args(sys.argv[1:2] + args)
    except Exception as exc:
        print('Failed to merge config file', args, exc)
        return args


def main():  # noqa
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'command',
        choices=['create', 'start', 'stop', 'exec', 'update', 'list', 'run'],
        help='Command.  create is the same as start followed by exec.')
    parser.add_argument('--name', help='Docker container name')
    parser.add_argument('--num', type=int, help='Docker container suffix')
    parser.add_argument('--src', help='Source path.  Defaults to current working directory.')
    parser.add_argument(
        '--ollama', '--url',
        help='Replacement url for ollama.  This can be just a port, a host '
        'and port, or a full base url.')
    parser.add_argument(
        '--fuse', action='store_true',
        help='Pass options to allow fuse to work when starting a container.')
    parser.add_argument('--no-fuse', dest='fuse', action='store_false')
    parser.add_argument(
        '--gpu', '--gpus', action='store_true',
        help='Enable gpu access when starting a container.')
    parser.add_argument('--no-gpu', '--no-gpus', dest='gpu', action='store_false')
    parser.add_argument(
        '--ssh',
        help='Add the specified public key for the ubuntu user and expose a '
        'port.')
    parser.add_argument(
        '--sshport', default=2222, type=int,
        help='The port to expose for ssh. Default %(default)s')
    parser.add_argument(
        '--sshfreeport', action='store_true',
        help='If specified, find the first free port for ssh starting at the '
        'specified port.')
    parser.add_argument('--no-sshfreeport', dest='sshfreeport', action='store_false')
    parser.add_argument(
        '--local', action='store_true',
        help='Mount local utilities directories.  This removes some isolation.')
    parser.add_argument('--no-local', dest='local', action='store_false')
    parser.add_argument(
        '--docker', action='store_true',
        help='Mount docker sock with appropriate permissions.')
    parser.add_argument('--no-docker', dest='docker', action='store_false')
    parser.add_argument(
        '--mount', action='append', default=[],
        help='Mount a folder into the docker.  Recommended format is local:inside:ro')
    parser.add_argument(
        '--skip',
        action='append',
        dest='skips',
        help='Files or folders to exclude from the copy. '
             'Use commas for multiple patterns in one --skip (e.g., ".venv,*.pyc"). '
             'Repeat --skip for separate groups.')
    parser.add_argument(
        '--config',
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent_docker.yaml'),
        help='Config file with keys as the working directories with arrays of '
        'default arguments for this script. Default %(default)s')
    parser.add_argument(
        '--verbose', '-v', action='count', default=0,
        help='Increase verbosity')
    args = parser.parse_args()
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    current_dir = os.path.basename(os.getcwd())
    args = add_config(args, os.getcwd(), parser)

    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.debug('Parsed arguments: %r', args)
    # add more commands: list, run <model> <text> --detach, check, log

    basename = args.name or f'agent_{safe_filename(current_dir)}'
    container_name = basename + (f'_{args.num}' if args.num is not None else '')
    is_windows = platform.system().lower() == 'windows'
    docker_cmd = ['wsl', 'docker'] if is_windows else ['docker']
    if is_windows and shutil.which('docker') and not str(shutil.which(
            'docker')).lower().endswith(('.bat', '.cmd')):
        docker_cmd = ['docker']
    if args.command in {'list'}:
        list_known(docker_cmd, container_name)
    if args.command in {'create', 'start', 'stop'}:
        renamed_id = rename_if_exists(docker_cmd, container_name)
        if renamed_id:
            cmd = docker_cmd + ['rm', '-f', renamed_id]
            logger.info(cmd)
            subprocess.run(cmd, stderr=subprocess.DEVNULL, check=False)
    if args.command in {'create', 'start'}:
        skip_patterns = []
        if args.skips:
            for s in args.skips:
                skip_patterns.extend(p.rstrip('/') for p in s.split(','))
        gateway = 'host-gateway'
        if is_windows and docker_cmd[0] == 'wsl':
            cmd = ['wsl', 'grep', 'nameserver', '/etc/resolv.conf']
            logger.info(cmd)
            gateway = subprocess.check_output(cmd).decode().split()[1].strip()
        other_opts = ['--ulimit', 'nofile=64000:64000']
        if args.fuse:
            other_opts.extend([
                '--device', '/dev/fuse:/dev/fuse',
                '--security-opt', 'apparmor=unconfined',
                '--cap-add', 'SYS_ADMIN'])
        if args.gpu:
            other_opts.extend(['--gpus', 'all'])
        if args.local:
            other_opts.extend(get_mount_args(is_windows, args.mount))
        if args.docker:
            other_opts.extend(['-v', '/var/run/docker.sock:/var/run/docker.sock'])
        if args.ssh:
            port = free_port(docker_cmd, args.sshport)
            other_opts.extend(['-p', f'{port}:2222'])
            if port != args.sshport:
                logger.warning('Using ssh port %s', port)
        cmd = docker_cmd + [
            'run', '-d', '--rm', '--name', container_name,
            '--add-host', f'host.docker.internal:{gateway}',
            '--log-opt', 'max-size=10m', '--log-opt', 'max-file=5',
            '--shm-size', '1024M'] + other_opts + [
            '-t', 'manthey/agent:latest', 'bash', '-c', 'while true; do date; sleep 300; done',
        ]
        logger.info(cmd)
        subprocess.check_call(cmd)

        def tar_exclusion_filter(tarinfo):
            basename = os.path.basename(tarinfo.name)
            for pat in skip_patterns:
                if fnmatch.fnmatch(basename, pat):
                    return None
                parts = tarinfo.name.split('/')
                if any(fnmatch.fnmatch(part, pat) for part in parts):
                    return None
            return tarinfo
        with tempfile.SpooledTemporaryFile() as fp:
            with tarfile.open(fileobj=fp, mode='w') as tf:
                tf.add(os.path.join('..', current_dir), filter=tar_exclusion_filter,
                       arcname=current_dir)
            fp.seek(0)
            cmd = docker_cmd + [
                'exec', '-i', container_name, 'tar', '-xf', '-', '-C', '/home/ubuntu/']
            logger.info(cmd)
            subprocess.check_call(cmd, stdin=fp)
    if args.command in {'create', 'start', 'exec', 'update'} and args.ollama:
        host = args.ollama
        if '/' not in args.ollama and ':' not in args.ollama:
            host = f'host.docker.internal:{host}'
        if '/' not in host:
            host = f'http://{host}'
        host = host.rstrip('/')
        cmd = docker_cmd + [
            'exec', '-it', container_name, 'bash', '-c',
            'set_ollama.sh "' + host + '"']
        logger.info(cmd)
        subprocess.run(cmd)
    if args.command in {'create', 'start'} and args.ssh:
        cmd = docker_cmd + [
            'cp', args.ssh, container_name + ':/home/ubuntu/.ssh/authorized_keys']
        logger.info(cmd)
        subprocess.check_call(cmd)
        cmd = docker_cmd + [
            'exec', '-it', '--user', 'root', container_name, 'bash', '-c',
            'chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys && '
            'chmod 0600 /home/ubuntu/.ssh/authorized_keys']
        logger.info(cmd)
        subprocess.check_call(cmd)
        cmd = docker_cmd + [
            'exec', '-it', '--user', 'root', container_name, 'bash', '-c',
            '/usr/sbin/sshd']
        logger.info(cmd)
        subprocess.check_call(cmd)
        if args.docker:
            cmd = docker_cmd + [
                'exec', '-it', '--user', 'root', container_name, 'bash', '-c',
                'chmod 0777 /var/run/docker.sock']
            logger.info(cmd)
            subprocess.check_call(cmd)
    if args.command in {'create', 'exec'}:
        cmd = docker_cmd + ['exec', '-it', container_name, 'bash']
        logger.info(cmd)
        subprocess.check_call(cmd)


if __name__ == '__main__':
    main()
