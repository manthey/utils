#!/usr/bin/env python3
# /// script
# requires-python = '>=3.11'
# dependencies = [
#     'uv',
# ]
# ///

import re
import subprocess
import sys


def detect_pytorch_index():
    try:
        out = subprocess.check_output(
            ['nvidia-smi'], text=True, stderr=subprocess.DEVNULL)
        m = re.search(r'CUDA Version:\s+(\d+)\.(\d+)', out)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            supported = ['118', '121', '124', '126', '128', '129']
            for tag in reversed(supported):
                t_major = int(tag[:2])
                t_minor = int(tag[2:])
                if (t_major < major) or (t_major == major and t_minor <= minor):
                    return f'https://download.pytorch.org/whl/cu{tag}'
    except Exception:
        pass
    return 'https://download.pytorch.org/whl/cpu'


subprocess.check_call([
    'uv', 'run',
    '--extra-index-url', 'https://girder.github.io/large_image_wheels',
    '--extra-index-url', detect_pytorch_index(),
    '--index-strategy', 'unsafe-best-match',
] + sys.argv[1:])
