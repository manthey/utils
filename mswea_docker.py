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
        if (name != base_name and not re.search(r'^mswea_', name) and
                not re.search(r'/mswea', image)):
            continue
        found.append((name, image, container_id))
    found.sort()
    for name, image, container_id in found:
        print(name, container_id, image)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'command', nargs='?', default='create',
        choices=['create', 'stop', 'exec', 'list'],
        help='Command. Defaults to "%(default)s".')
    parser.add_argument('--name', help='Docker container name')
    parser.add_argument('--num', type=int, help='Docker container suffix')
    parser.add_argument('--src', help='Source path.  Defaults to current working directory.')
    parser.add_argument(
        '--ollama', help='Replacement url for ollama.  This can be just a '
        'port, a host and port, or a full base url.')
    args = parser.parse_args()

    # add more commands: list, run <model> <text> --detach, check, log
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    current_dir = os.path.basename(os.getcwd())
    basename = args.name or f'mswea_{safe_filename(current_dir)}'
    container_name = basename + (f'_{args.num}' if args.num is not None else '')
    is_windows = platform.system().lower() == 'windows'
    docker_cmd = ['wsl', 'docker'] if is_windows else ['docker']
    if args.command in {'list'}:
        list_known(docker_cmd, container_name)
    if args.command in {'create', 'stop'}:
        subprocess.run(docker_cmd + [
            'rm', '-f', container_name], stderr=subprocess.DEVNULL, check=False)
    if args.command in {'create'}:
        gateway = 'host-gateway'
        if is_windows:
            gateway = subprocess.check_output([
                'wsl', 'grep', 'nameserver', '/etc/resolv.conf']).decode().split()[1].strip()
        subprocess.check_call(docker_cmd + [
            'run', '-d', '--rm', '--name', container_name,
            '--add-host', f'host.docker.internal:{gateway}',
            '--shm-size', '512M',
            '-t', 'manthey/mswea:latest', 'bash', '-c', 'while true; do sleep 86400; done',
        ])
        if is_windows:
            subprocess.check_call(
                docker_cmd + ['cp', f'../{current_dir}', f'{container_name}:/home/ubuntu/.'])
        else:
            tar_proc = subprocess.Popen([
                'tar', '-cf', '-', '-C', os.path.pardir, current_dir], stdout=subprocess.PIPE)
            subprocess.check_call(docker_cmd + [
                'exec', '-i', container_name, 'tar', '-xf', '-', '-C',
                '/home/ubuntu/'], stdin=tar_proc.stdout)
    if args.command in {'create', 'exec'} and args.ollama:
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
    if args.command in {'create', 'exec'}:
        subprocess.run(docker_cmd + ['exec', '-it', container_name, 'bash'])


if __name__ == '__main__':
    main()
