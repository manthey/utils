#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = []
# ///

import argparse
import os
import platform
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='mswea_docker', help='Docker container name')
    parser.add_argument('--num', type=int, help='Docker container suffix')
    args = parser.parse_args()

    container_name = f'{args.name}_{args.num}' if args.num is not None else args.name
    current_dir = os.path.basename(os.getcwd())
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
    tar_proc = subprocess.Popen([
        'tar', '-cf', '-', '-C', os.path.pardir, current_dir], stdout=subprocess.PIPE)
    subprocess.check_call(docker_cmd + [
        'exec', '-i', container_name, 'tar', '-xf', '-', '-C',
        '/home/ubuntu/'], stdin=tar_proc.stdout)
    subprocess.run(docker_cmd + ['exec', '-it', container_name, 'bash'])


if __name__ == '__main__':
    main()
