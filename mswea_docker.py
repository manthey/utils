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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', help='Docker container name')
    parser.add_argument('--num', type=int, help='Docker container suffix')
    parser.add_argument('--src', help='Source path.  Defaults to current working directory.')
    args = parser.parse_args()

    if args.src:
        os.chdir(os.expanduser(args.src))
    current_dir = os.path.basename(os.getcwd())
    basename = args.name or f'mswea_{safe_filename(current_dir)}'
    container_name = basename + (f'_{args.num}' if args.num is not None else '')
    is_windows = platform.system().lower() == 'windows'
    docker_cmd = ['wsl', 'docker'] if is_windows else ['docker']
    subprocess.run(docker_cmd + [
        'rm', '-f', container_name], stderr=subprocess.DEVNULL, check=False)
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
    subprocess.run(docker_cmd + ['exec', '-it', container_name, 'bash'])


if __name__ == '__main__':
    main()
