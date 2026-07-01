# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "runpod",
#     "tomli",
#     "requests",
# ]
# ///

import argparse
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


def get_all_gpu_types():
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
        lowestPrice(input: {gpuCount: 1}) {
          minimumBidPrice
        }
      }
    }
    """
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


def find_gpus(min_memory_gb, secure_only):
    gpu_types = get_all_gpu_types()
    compatible = []
    for gpu in gpu_types:
        mem_gb = gpu.get('memoryInGb', 0)
        if mem_gb < min_memory_gb:
            continue
        if secure_only and not gpu.get('secureCloud', False):
            continue
        if not gpu.get('secureCloud', False) and not gpu.get('communityCloud', False):
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


def cmd_check(args):
    compatible = find_gpus(args.mem, args.secure)
    if not compatible:
        print(f'No GPUs found with at least {args.mem} GB memory.')
        sys.exit(1)
    print(f'GPUs with >= {args.mem} GB memory (cheapest first):')
    for gpu in compatible:
        cloud_type = 'secure+community'
        if gpu['secureCloud'] and not gpu['communityCloud']:
            cloud_type = 'secure'
        elif gpu['communityCloud'] and not gpu['secureCloud']:
            cloud_type = 'community'
        price = f"${gpu['lowestPrice']:.2f}/hr"
        print(f"  {gpu['id']}: {gpu['displayName']} ({gpu['memoryGb']} GB) [{cloud_type}] {price}")


def cmd_start(args):
    runpod.api_key = get_api_key()
    compatible = find_gpus(args.mem, args.secure)
    if not compatible:
        print('No available GPU found matching criteria.', file=sys.stderr)
        sys.exit(1)
    if args.gpu:
        gpu_info = next([g for g in compatible if g['id'] == args.gpu])
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
        container_disk_in_gb=args.vol,
        env={'OLLAMA_CONTEXT_LENGTH': '262144'},
        ports='11434/http',
        volume_in_gb=args.vol,
        volume_mount_path='/root/.ollama',
        cloud_type=cloud_type,
    )
    print(f'Pod created: {pod["id"]}')
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
            if requests.head(url, timeout=5).status_code < 400:
                break
        except Exception:
            pass
        time.sleep(5)
    print(f'  Status: {status.get("desiredStatus", "unknown")}')


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

    start_parser = subparsers.add_parser('start', help='Start an Ollama pod')
    start_parser.add_argument('--gpu', help='GPU type to use (otherwise, use cheapest available)')
    start_parser.add_argument('--mem', type=int, default=96, help='Minimum GPU memory in GB')
    start_parser.add_argument('--secure', action='store_true', help='Secure cloud only')
    start_parser.add_argument('--vol', type=int, default=75, help='Volume size in GB')
    start_parser.add_argument(
        '--no-wait', action='store_true', help='Do not wait for pod to be ready before exiting')

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
