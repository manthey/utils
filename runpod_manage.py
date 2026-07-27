# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "runpod",
#     "tomli",
#     "requests",
# ]
# ///

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import runpod

API_BASE = 'https://api.runpod.io/graphql'


def get_api_key():
    path = Path.home() / '.runpod' / 'config.toml'
    with open(path, 'rb') as file:
        import tomli
        config = tomli.load(file)
    if 'default' in config and 'api_key' in config['default']:
        return config['default']['api_key']
    if 'api_key' in config:
        return config['api_key']
    print('api key not found', file=sys.stderr)
    sys.exit(1)


def gql_query(query):
    api_key = get_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {'query': query}
    response = requests.post(API_BASE, json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()
    if 'errors' in data:
        raise Exception(data['errors'])
    return data['data']


def get_all_gpu_types(disk_in_gb):
    query = """
    query GpuTypes {
      gpuTypes {
        id
        displayName
        memoryInGb
        secureCloud
        communityCloud
        securePrice
        communityPrice
        secureSpotPrice
        communitySpotPrice
        maxGpuCountCommunityCloud
        maxGpuCountSecureCloud
        lowestPrice: lowestPrice(input: {
          gpuCount: 1
          minDisk: %d
          secureCloud: true
        }) {
          uninterruptablePrice
          stockStatus
        }
        communityLowestPrice: lowestPrice(input: {
          gpuCount: 1
          minDisk: %d
          secureCloud: false
        }) {
          uninterruptablePrice
          stockStatus
        }
      }
    }
    """ % (disk_in_gb, disk_in_gb)
    result = gql_query(query)
    return result['gpuTypes']


def get_effective_price(gpu, secure_only):
    keys = set()
    if gpu.get('secureCloud'):
        keys.add('securePrice')
    if not secure_only and gpu.get('communityCloud'):
        keys.add('communityPrice')
    prices = [(gpu.get(k), k.split('Price')[0]) for k in keys if gpu.get(k)]
    if not prices:
        return None
    return min(prices)


def find_gpus(min_memory_gb, secure_only, disk_in_gb):
    gpu_types = get_all_gpu_types(disk_in_gb)
    compatible = []
    for gpu in gpu_types:
        mem_gb = gpu.get('memoryInGb', 0)
        if mem_gb < min_memory_gb:
            continue
        if secure_only and not gpu.get('secureCloud', False):
            continue
        if not gpu.get('secureCloud', False) and not gpu.get('communityCloud', False):
            continue
        comm = False
        if gpu.get('lowestPrice', {}).get('stockStatus') is None or (
                not secure_only and gpu.get('communityLowestPrice', {}).get(
                    'stockStatus') is not None):
            if gpu.get('lowestPrice', {}).get('stockStatus') is None or (
                    gpu['communityLowestPrice']['uninterruptablePrice'] <
                    gpu['lowestPrice']['uninterruptablePrice']):
                gpu['lowestPrice'] = gpu['communityLowestPrice']
                comm = True
        if not comm and gpu.get('communityCloud', False):
            gpu['communityCloud'] = False
        if gpu.get('lowestPrice', {}).get('stockStatus') is None:
            continue
        lowest_price, source = get_effective_price(gpu, secure_only)
        if not lowest_price:
            continue
        compatible.append({
            'id': gpu['id'],
            'displayName': gpu['displayName'],
            'memoryGb': mem_gb,
            'secureCloud': gpu.get('secureCloud', False) and source == 'secure',
            'communityCloud': gpu.get('communityCloud', False) and source == 'community',
            'lowestPrice': lowest_price,
        })
    compatible.sort(key=lambda x: x['lowestPrice'])
    return compatible


def add_weights(compatible, args):  # noqa
    table = json.loads(requests.get(
        'https://owensgroup.github.io/gpustats/plots/'
        'Memory%20Bandwidth%20over%20Time.html').text.split(
            'spec = ')[1].split('\n')[0].rstrip(';'))
    table = table['datasets'][list(table['datasets'].keys())[0]]
    subtable = {}
    for m in table:
        if m.get('Model'):
            bw = tf = None
            for k, v in m.items():
                try:
                    v = float(v)
                except Exception:
                    continue
                if v is None or math.isnan(v) or 'tracing' in k:
                    continue
                if k == 'Memory Bandwidth (GB/s)':
                    bw = v
                mult = 1
                if 'sparse' in k or 'INT8' in k or 'FP4' in k:
                    mult = 0.5
                if 'TFLOPS' in k and (tf is None or v * mult > tf):
                    tf = v * mult
            if bw is not None and tf is not None:
                subtable[m['Model']] = {
                    'name': m['Model'], 'bandwidth': bw, 'tflops': tf,
                    'ws': set(m['Model'].lower().split())}
    for cm in compatible:
        ws = set((cm['id'] + ' ' + cm['displayName']).lower().split())
        best = None
        for m in subtable.values():
            if not len(ws & m['ws']):
                continue
            score = len(ws & m['ws']) * 2 - len(ws - m['ws']) - len(m['ws'] - ws)
            if best is None or score > best[0]:
                best = score, m
        if best is not None:
            if args.weight.startswith('b'):
                cm['weight'] = best[1]['bandwidth']
            else:
                cm['weight'] = best[1]['tflops']


def cmd_check(args):
    compatible = find_gpus(args.mem, args.secure, args.vol + args.disk)
    if not compatible:
        print(f'No GPUs found with at least {args.mem} GB memory.')
        sys.exit(1)
    basis = None
    if args.weight:
        add_weights(compatible, args)
        basis = compatible[0].get('weight')
    print(f'GPUs with >= {args.mem} GB memory (cheapest first):')
    for gpu in compatible:
        cloud_type = 'secure+community'
        if gpu['secureCloud'] and not gpu['communityCloud']:
            cloud_type = 'secure'
        elif gpu['communityCloud'] and not gpu['secureCloud']:
            cloud_type = 'community'
        price = f"${gpu['lowestPrice']:.2f}/hr"
        weight = ''
        if basis and gpu.get('weight'):
            factor = gpu['weight'] / basis
            fprice = gpu['lowestPrice'] / factor
            weight = f' ({factor:.2f}x base: ${fprice:.2f}/hr)'
        print(f"  {gpu['id']}: {gpu['displayName']} ({gpu['memoryGb']} GB) "
              f'[{cloud_type}] {price}{weight}')


def cmd_start(args):  # noqa
    runpod.api_key = get_api_key()
    compatible = find_gpus(args.mem, args.secure, args.vol + args.disk)
    if not compatible:
        print('No available GPU found matching criteria.', file=sys.stderr)
        sys.exit(1)
    if args.gpu:
        gpu_info = [g for g in compatible if args.gpu in {g['id'], g['displayName']}][0]
    else:
        gpu_info = compatible[0]
    gpu_type_id = gpu_info['id']
    price_str = (f"${gpu_info['lowestPrice']:.2f}/hr"
                 if gpu_info['lowestPrice'] is not None else '-')
    print(f"Starting pod with GPU: {gpu_info['displayName']} "
          f"({gpu_info['memoryGb']} GB) - {price_str}")
    cloud_type = 'COMMUNITY'
    if args.secure:
        cloud_type = 'SECURE'
    elif gpu_info.get('secureCloud'):
        cloud_type = 'SECURE'
    pod = runpod.create_pod(
        name=f'ollama-{gpu_type_id}',
        image_name='ollama/ollama:latest',
        gpu_type_id=gpu_type_id,
        container_disk_in_gb=args.disk,
        env={'OLLAMA_CONTEXT_LENGTH': '262144'},
        ports='11434/http',
        volume_in_gb=args.vol,
        volume_mount_path='/root/.ollama',
        cloud_type=cloud_type,
    )
    print(f'Pod created: {pod["id"]}')
    if getattr(args, 'model', []):
        args.no_wait = False
    status = pod
    while True:
        try:
            status = runpod.get_pod(pod['id'])
            print(f'  Status: {status.get("desiredStatus", "unknown")}', end='\r')
            if status.get('desiredStatus') == 'RUNNING' or args.no_wait:
                break
            time.sleep(5)
        except Exception as e:
            print(f'\nError checking pod status: {e}')
            break
    url = f'https://{pod["id"]}-11434.proxy.runpod.net'
    print(f'\n  Use {url}')
    while not args.no_wait:
        try:
            resp = requests.get(f'{url}/api/tags', timeout=5)
            if resp.status_code == 200 and 'models' in resp.json():
                break
        except Exception:
            pass
        time.sleep(5)
    print(f'  Status: {status.get("desiredStatus", "unknown")}')
    models_to_pull = getattr(args, 'model', [])
    if models_to_pull:
        pod_env = os.environ.copy()
        pod_env['OLLAMA_HOST'] = url
        for model in models_to_pull:
            print(f'  Pulling {model}')
            subprocess.run(['ollama', 'pull', model], env=pod_env)
            print(f'  Pulled {model}')


def cmd_list(args):
    runpod.api_key = get_api_key()
    try:
        pods = runpod.get_pods()
        if not pods:
            print('No pods found.')
            return
        print(f'{"ID":<30} {"Name":<30} {"GPU":<20} {"Status":<15} {"Image":<30}')
        print('-' * 130)
        for pod in pods:
            pod_id = pod.get('id', 'unknown')
            name = pod.get('name', 'unknown')
            gpu = pod.get('gpuTypeId', 'unknown')
            status = pod.get('desiredStatus', pod.get('status', 'unknown'))
            image = pod.get('imageName', 'unknown')
            print(f'{pod_id:<30} {name:<30} {gpu:<20} {status:<15} {image:<30}')
    except Exception as e:
        print(f'Error listing pods: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_stop(args):
    runpod.api_key = get_api_key()
    if args.all:
        try:
            pods = runpod.get_pods()
            if not pods:
                print('No pods to stop.')
                return
            for pod in pods:
                pod_id = pod['id']
                print(f'Stopping pod {pod_id}...')
                runpod.terminate_pod(pod_id)
            print(f'Stopped {len(pods)} pod(s).')
        except Exception as e:
            print(f'Error stopping pods: {e}', file=sys.stderr)
            sys.exit(1)
    elif args.pod:
        try:
            print(f'Stopping pod {args.pod}...')
            runpod.terminate_pod(args.pod)
            print(f'Pod {args.pod} stopped.')
        except Exception as e:
            print(f'Error stopping pod {args.pod}: {e}', file=sys.stderr)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Manage RunPod Ollama instances')
    subparsers = parser.add_subparsers(dest='command', required=True)

    check_parser = subparsers.add_parser('check', help='Check available GPUs')
    check_parser.add_argument('--mem', type=int, default=96, help='Minimum GPU memory in GB')
    check_parser.add_argument('--secure', action='store_true', help='Secure cloud only')
    check_parser.add_argument(
        '--disk', type=int, default=25, help='Contianer disk (nvme) volume size in GB')
    check_parser.add_argument('--vol', type=int, default=75, help='Volume size in GB')
    check_parser.add_argument(
        '--weight', choices=['bandwidth', 'b', 'compute', 'c'],
        help='Weigh pricing based on a metric')

    start_parser = subparsers.add_parser('start', help='Start an Ollama pod')
    start_parser.add_argument('--gpu', help='GPU type to use (otherwise, use cheapest available)')
    start_parser.add_argument('--mem', type=int, default=96, help='Minimum GPU memory in GB')
    start_parser.add_argument('--secure', action='store_true', help='Secure cloud only')
    start_parser.add_argument(
        '--disk', type=int, default=25,
        help='Contianer disk (nvme) volume size in GB')
    start_parser.add_argument(
        '--vol', type=int, default=75,
        help='Volume size in GB (where models are stored)')
    start_parser.add_argument(
        '--no-wait', action='store_true', help='Do not wait for pod to be ready before exiting')
    start_parser.add_argument(
        '--model', action='append', default=[],
        help='Model to pull after creation (can be used multiple times)')

    subparsers.add_parser('list', help='List running pods')

    stop_parser = subparsers.add_parser('stop', help='Stop a pod')
    stop_group = stop_parser.add_mutually_exclusive_group(required=True)
    stop_group.add_argument('--pod', type=str, help='Pod ID to stop')
    stop_group.add_argument('--all', action='store_true', help='Stop all pods')

    args = parser.parse_args()
    if args.command == 'check':
        cmd_check(args)
    elif args.command == 'start':
        cmd_start(args)
    elif args.command == 'list':
        cmd_list(args)
    elif args.command == 'stop':
        cmd_stop(args)


if __name__ == '__main__':
    main()
