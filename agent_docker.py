#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = []
# ///

import argparse
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.rstrip(' .')
    return name or 'docker'


def list_known(docker_cmd: list[str], base_name: str):
    cmd = docker_cmd + ['ps', '-a', '--format', '{{.ID}}\t{{.Names}}\t{{.Image}}']
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
    parser.add_argument(
        '--gpu', '--gpus', action='store_true',
        help='Enable gpu access when starting a container.')
    parser.add_argument(
        '--ssh',
        help='Add the specified public key for the ubuntu user and expose '
        'port 2222.')
    parser.add_argument(
        '--local', action='store_true',
        help='Mount local utilities directories.  This removes some isolation.')
    parser.add_argument(
        '--docker', action='store_true',
        help='Mount docker sock with appropriate permissions.')
    parser.add_argument(
        '--mount', action='append', default=[],
        help='Mount a folder into the docker.  Recommended format is local:inside:ro')
    args = parser.parse_args()

    # add more commands: list, run <model> <text> --detach, check, log
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    current_dir = os.path.basename(os.getcwd())
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
        subprocess.run(docker_cmd + [
            'rm', '-f', container_name], stderr=subprocess.DEVNULL, check=False)
    if args.command in {'create', 'start'}:
        gateway = 'host-gateway'
        if is_windows and docker_cmd[0] == 'wsl':
            gateway = subprocess.check_output([
                'wsl', 'grep', 'nameserver', '/etc/resolv.conf']).decode().split()[1].strip()
        other_opts = []
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
            other_opts.extend(['-p', '2222:2222'])
        cmd = docker_cmd + [
            'run', '-d', '--rm', '--name', container_name,
            '--add-host', f'host.docker.internal:{gateway}',
            '--log-opt', 'max-size=10m', '--log-opt', 'max-file=5',
            '--shm-size', '1024M'] + other_opts + [
            '-t', 'manthey/agent:latest', 'bash', '-c', 'while true; do date; sleep 300; done',
        ]
        subprocess.check_call(cmd)
        with tempfile.SpooledTemporaryFile() as fp:
            with tarfile.open(fileobj=fp, mode='w') as tf:
                tf.add(os.path.join('..', current_dir), arcname=current_dir)
            fp.seek(0)
            subprocess.check_call(docker_cmd + [
                'exec', '-i', container_name, 'tar', '-xf', '-', '-C',
                '/home/ubuntu/'], stdin=fp)
    if args.command in {'create', 'start', 'exec', 'update'} and args.ollama:
        host = args.ollama
        if '/' not in args.ollama and ':' not in args.ollama:
            host = f'host.docker.internal:{host}'
        if '/' not in host:
            host = f'http://{host}'
        host = host.rstrip('/')
        subprocess.run(docker_cmd + [
            'exec', '-it', container_name, 'bash', '-c',
            'set_ollama.sh "' + host + '"'])
    if args.command in {'create', 'start'} and args.ssh:
        subprocess.check_call(docker_cmd + [
            'cp', args.ssh, container_name + ':/home/ubuntu/.ssh/authorized_keys'])
        subprocess.check_call(docker_cmd + [
            'exec', '-it', '--user', 'root', container_name, 'bash', '-c',
            'chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys && '
            'chmod 0600 /home/ubuntu/.ssh/authorized_keys'])
        subprocess.check_call(docker_cmd + [
            'exec', '-it', '--user', 'root', container_name, 'bash', '-c',
            '/usr/sbin/sshd'])
        if args.docker:
            subprocess.check_call(docker_cmd + [
                'exec', '-it', '--user', 'root', container_name, 'bash', '-c',
                'chmod 0777 /var/run/docker.sock'])
    if args.command in {'create', 'exec'}:
        subprocess.run(docker_cmd + ['exec', '-it', container_name, 'bash'])


if __name__ == '__main__':
    main()
