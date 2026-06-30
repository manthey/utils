#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = []
# ///

import argparse
import os
import platform
import re
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'command',
        choices=['create', 'start', 'stop', 'exec', 'list', 'run'],
        help='Command.  create is the same as start followed by exec.')
    parser.add_argument('--name', help='Docker container name')
    parser.add_argument('--num', type=int, help='Docker container suffix')
    parser.add_argument('--src', help='Source path.  Defaults to current working directory.')
    parser.add_argument(
        '--ollama', help='Replacement url for ollama.  This can be just a '
        'port, a host and port, or a full base url.')
    parser.add_argument(
        '--fuse', action='store_true',
        help='Pass options to allow fuse to work when starting a container.')
    args = parser.parse_args()

    # add more commands: list, run <model> <text> --detach, check, log
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    current_dir = os.path.basename(os.getcwd())
    basename = args.name or f'agent_{safe_filename(current_dir)}'
    container_name = basename + (f'_{args.num}' if args.num is not None else '')
    is_windows = platform.system().lower() == 'windows'
    docker_cmd = ['wsl', 'docker'] if is_windows else ['docker']
    if args.command in {'list'}:
        list_known(docker_cmd, container_name)
    if args.command in {'create', 'start', 'stop'}:
        subprocess.run(docker_cmd + [
            'rm', '-f', container_name], stderr=subprocess.DEVNULL, check=False)
    if args.command in {'create', 'start'}:
        gateway = 'host-gateway'
        if is_windows:
            gateway = subprocess.check_output([
                'wsl', 'grep', 'nameserver', '/etc/resolv.conf']).decode().split()[1].strip()
        other_opts = []
        if args.fuse:
            other_opts.extend([
                '--device', '/dev/fuse:/dev/fuse',
                '--security-opt', 'apparmor=unconfined',
                '--cap-add', 'SYS_ADMIN'])
        subprocess.check_call(docker_cmd + [
            'run', '-d', '--rm', '--name', container_name,
            '--add-host', f'host.docker.internal:{gateway}',
            '--shm-size', '1024M'] + other_opts + [
            '-t', 'manthey/agent:latest', 'bash', '-c', 'while true; do sleep 86400; done',
        ])
        with tempfile.SpooledTemporaryFile() as fp:
            with tarfile.open(fileobj=fp, mode='w') as tf:
                tf.add(os.path.join('..', current_dir), arcname=current_dir)
            fp.seek(0)
            subprocess.check_call(docker_cmd + [
                'exec', '-i', container_name, 'tar', '-xf', '-', '-C',
                '/home/ubuntu/'], stdin=fp)
    if args.command in {'create', 'start', 'exec'} and args.ollama:
        host = args.ollama
        if '/' not in args.ollama and ':' not in args.ollama:
            host = f'host.docker.internal:{host}'
        if '/' not in host:
            host = f'http://{host}'
        host = host.rstrip('/')
        subprocess.run(docker_cmd + [
            'exec', '-it', container_name, 'bash', '-c',
            'sed -i "s|^\\(.*_API_BASE=\\)https\\?://[^/]*|\\1' + host +
            '|" /home/ubuntu/.config/mini-swe-agent/.env'])
        subprocess.run(docker_cmd + [
            'exec', '-it', container_name, 'bash', '-c',
            'sed -i \'s|\\("baseUrl": "\\)https\\?://[^/"]*|\\1' + host +
            "|' /home/ubuntu/.pi/agent/local-providers.json"])
        subprocess.run(docker_cmd + [
            'exec', '-it', container_name, 'bash', '-c',
            'sed -i \'s|\\("baseUrl": "\\)https\\?://[^/"]*|\\1' + host +
            "|' /home/ubuntu/.pi/agent/models.json"])
    if args.command in {'create', 'exec'}:
        subprocess.run(docker_cmd + ['exec', '-it', container_name, 'bash'])


if __name__ == '__main__':
    main()
