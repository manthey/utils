#!/usr/bin/env python3
"""install_msvc.py - install MSVC (with ATL and MFC) and the Windows SDK
into ~/.msvc without administrator rights or the Visual Studio installer.

The packages come straight from Microsoft's Visual Studio manifest: the
compiler's .vsix packages are zip files, and the SDK's .msi packages are
unpacked with an administrative install (msiexec /a), which needs no
elevation.  Payload hashes from the manifest are checked.  vctip.exe, the
compiler's usage reporter, is removed.

The compiler environment (INCLUDE, LIB, LIBPATH and PATH additions) is
written to the user's environment in the registry, so new sessions,
including ssh ones, have cl.exe, link.exe, lib.exe, nmake.exe and rc.exe.
For the current bash session, source ~/.msvc/env.sh.  MSBuild is not
included; build with cl, nmake, or CMake with the Ninja or NMake generator.

Using these tools is subject to the Visual Studio Build Tools license:
https://visualstudio.microsoft.com/license-terms/vs2022-ga-diagnosticbuildtools/
"""

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CHANNEL = 'https://aka.ms/vs/17/release/channel'
DEST = Path.home() / '.msvc'
DOWNLOADS = DEST / 'downloads'
# Packages (Microsoft.VC.<version>.<suffix>) for x64 host and target.
MSVC_PACKAGES = (
    'Tools.HostX64.TargetX64.base',
    'Tools.HostX64.TargetX64.Res.base',
    'CRT.Headers.base',
    'CRT.x64.Desktop.base',
    'CRT.x64.Store.base',
    'CRT.Redist.X64.base',
    'ATL.Headers.base',
    'ATL.X64.base',
    'MFC.Headers.base',
    'MFC.X64.base',
    'MFC.X64.Debug.base',
    'MFC.MBCS.X64.base',
    'MFC.MBCS.X64.Debug.base',
    'MFC.Redist.X64.base',
)
SDK_MSIS = (
    'Windows SDK Desktop Headers x64-x86_en-us.msi',
    'Windows SDK Desktop Libs x64-x86_en-us.msi',
    'Windows SDK Desktop Tools x64-x86_en-us.msi',
    'Windows SDK OnecoreUap Headers x64-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Headers-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Libs-x86_en-us.msi',
    'Windows SDK for Windows Store Apps Tools-x86_en-us.msi',
    'Universal CRT Headers Libraries and Sources-x86_en-us.msi',
)


def log(msg):
    print(f'install_msvc: {msg}', flush=True)


def get_json(url):
    with urllib.request.urlopen(url) as resp:
        return json.load(resp)


def version_key(text):
    return tuple(int(part) for part in text.split('.'))


def load_manifest():
    channel = get_json(CHANNEL)
    item = next(entry for entry in channel['channelItems']
                if entry['id'] == 'Microsoft.VisualStudio.Manifests.VisualStudio')
    return get_json(item['payloads'][0]['url'])['packages']


def pick_versions(packages):
    tools = re.compile(r'microsoft\.vc\.([\d.]+)\.tools\.hostx64\.targetx64\.base$')
    msvc = max((match.group(1) for match in (tools.match(pkg['id'].lower()) for pkg in packages)
                if match), key=version_key)
    sdk_component = re.compile(r'microsoft\.visualstudio\.component\.windows1[01]sdk\.(\d+)$')
    components = [(int(match.group(1)), pkg) for pkg in packages
                  for match in [sdk_component.match(pkg['id'].lower())] if match]
    sdk_package = next(iter(max(components, key=lambda entry: entry[0])[1]['dependencies']))
    return msvc, sdk_package


def msvc_payloads(packages, version):
    wanted = {f'microsoft.vc.{version}.{suffix}'.lower() for suffix in MSVC_PACKAGES}
    payloads = []
    for pkg in packages:
        if pkg['id'].lower() in wanted and pkg.get('language') in (None, 'neutral', 'en-US'):
            payloads.extend(pkg.get('payloads', []))
    found = {pkg['id'].lower() for pkg in packages if pkg['id'].lower() in wanted}
    if found != wanted:
        sys.exit(f'install_msvc: missing packages: {sorted(wanted - found)}')
    return payloads


def download(payload, folder):
    path = folder / Path(payload['fileName'].replace('\\', '/')).name
    digest = payload['sha256'].lower()
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
        return path
    partial = path.with_name(path.name + '.partial')
    sha = hashlib.sha256()
    with urllib.request.urlopen(payload['url']) as resp, open(partial, 'wb') as fp:
        while chunk := resp.read(1 << 20):
            sha.update(chunk)
            fp.write(chunk)
    if sha.hexdigest() != digest:
        partial.unlink()
        msg = f'sha256 mismatch for {payload["fileName"]}'
        raise RuntimeError(msg)
    partial.replace(path)
    return path


def download_all(payloads, folder):
    folder.mkdir(parents=True, exist_ok=True)
    total = sum(payload.get('size', 0) for payload in payloads)
    log(f'downloading {len(payloads)} files ({total / 2 ** 20:.0f} MB)')
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda payload: download(payload, folder), payloads))


def extract_vsix(path):
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = urllib.parse.unquote(info.filename)
            if not name.startswith('Contents/') or name.endswith('/'):
                continue
            target = DEST / name[len('Contents/'):]
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)


def install_sdk(packages, sdk_package):
    pkg = next(pkg for pkg in packages if pkg['id'] == sdk_package)
    by_name = {payload['fileName'].replace('\\', '/').split('/')[-1]: payload
               for payload in pkg['payloads']}
    missing = [name for name in SDK_MSIS if name not in by_name]
    if missing:
        sys.exit(f'install_msvc: {sdk_package} lacks {missing}')
    folder = DOWNLOADS / 'sdk'
    msis = download_all([by_name[name] for name in SDK_MSIS], folder)
    cabs = set()
    for msi in msis:
        cabs.update(name.decode() for name in re.findall(rb'[0-9a-f]{32}\.cab', msi.read_bytes()))
    download_all([by_name[name] for name in sorted(cabs) if name in by_name], folder)
    msiexec = Path(os.environ['SYSTEMROOT']) / 'System32' / 'msiexec.exe'
    for msi in msis:
        log(f'unpacking {msi.name}')
        subprocess.run([str(msiexec), '/a', str(msi), '/quiet', '/qn',
                        f'TARGETDIR={DEST}'], check=True)
    # An administrative install leaves a copy of each .msi in TARGETDIR.
    for leftover in DEST.glob('*.msi'):
        leftover.unlink()


def latest_dir(parent, required=None):
    dirs = [path for path in parent.iterdir()
            if path.is_dir() and re.fullmatch(r'[\d.]+', path.name)]
    if required:
        dirs = [path for path in dirs if (path / required).exists()]
    if not dirs:
        sys.exit(f'install_msvc: nothing found in {parent}')
    return max(dirs, key=lambda path: version_key(path.name))


def build_environment():
    vc = latest_dir(DEST / 'VC' / 'Tools' / 'MSVC')
    kits = DEST / 'Windows Kits' / '10'
    sdkv = latest_dir(kits / 'Include', 'um').name
    if not (vc / 'atlmfc' / 'include' / 'afx.h').exists():
        sys.exit('install_msvc: MFC headers are missing')
    paths = [vc / 'bin' / 'Hostx64' / 'x64', kits / 'bin' / sdkv / 'x64']
    # Runtime DLLs (CRT, MFC) so that dynamically linked programs run.
    redist = DEST / 'VC' / 'Redist' / 'MSVC'
    if redist.exists():
        paths += sorted(path for path in redist.glob('*/x64/*') if path.is_dir())
        paths += sorted(path for path in redist.glob('*/debug_nonredist/x64/*') if path.is_dir())
    include = [vc / 'include', vc / 'atlmfc' / 'include'] + [
        kits / 'Include' / sdkv / sub for sub in ('ucrt', 'shared', 'um', 'winrt', 'cppwinrt')]
    lib = [vc / 'lib' / 'x64', vc / 'atlmfc' / 'lib' / 'x64',
           kits / 'Lib' / sdkv / 'ucrt' / 'x64', kits / 'Lib' / sdkv / 'um' / 'x64']
    env = {
        'INCLUDE': ';'.join(str(path) for path in include),
        'LIB': ';'.join(str(path) for path in lib),
        'LIBPATH': ';'.join(str(path) for path in lib[:2]),
        'VCToolsInstallDir': f'{vc}\\',
        'VCToolsVersion': vc.name,
        'WindowsSdkDir': f'{kits}\\',
        'WindowsSDKVersion': f'{sdkv}\\',
        'Platform': 'x64',
        # setuptools then uses this compiler from the environment instead of
        # looking for a Visual Studio installation.
        'DISTUTILS_USE_SDK': '1',
        'MSSdk': '1',
        'VSCMD_SKIP_SENDTELEMETRY': '1',
    }
    return env, [str(path) for path in paths], vc.name, sdkv


def write_environment(env, paths):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_ALL_ACCESS) as key:
        for name, value in env.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        try:
            current, kind = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            current, kind = '', winreg.REG_EXPAND_SZ
        # Drop entries from an earlier install, then append ours: the user
        # PATH follows the machine PATH, so the sandbox's git, python and
        # node keep precedence.
        msvc_prefix = str(DEST).lower()
        kept = [entry for entry in current.split(';')
                if entry and not entry.lower().startswith(msvc_prefix)]
        winreg.SetValueEx(key, 'Path', 0, kind, ';'.join(kept + paths))
    # Older Visual Studio components read this per-user opt-out.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\VSCommon\17.0\SQM') as key:
        winreg.SetValueEx(key, 'OptIn', 0, winreg.REG_DWORD, 0)
    lines = ['# Source this for the MSVC environment in an existing bash session.']
    for name, value in env.items():
        lines.append(f"export {name}='{value}'")
    msys = [re.sub(r'^([A-Za-z]):', lambda match: '/' + match.group(1).lower(), path)
            for path in paths]
    msys = [path.replace('\\', '/') for path in msys]
    lines.append(f"export PATH=\"$PATH:{':'.join(msys)}\"")
    (DEST / 'env.sh').write_text('\n'.join(lines) + '\n', newline='\n')


def main():
    if sys.platform != 'win32':
        sys.exit('install_msvc: this installs the Windows compiler and must run on Windows')
    log('reading the Visual Studio manifest')
    packages = load_manifest()
    msvc, sdk_package = pick_versions(packages)
    log(f'MSVC {msvc} with ATL and MFC, and {sdk_package}, into {DEST}')
    log('use is subject to the Visual Studio Build Tools license')
    if DEST.exists():
        for entry in DEST.iterdir():
            if entry == DOWNLOADS:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for vsix in download_all(msvc_payloads(packages, msvc), DOWNLOADS / 'msvc'):
        log(f'extracting {vsix.name}')
        extract_vsix(vsix)
    install_sdk(packages, sdk_package)
    for vctip in DEST.rglob('vctip.exe'):
        vctip.unlink()
    env, paths, vc_version, sdk_version = build_environment()
    write_environment(env, paths)
    shutil.rmtree(DOWNLOADS, ignore_errors=True)
    log(f'installed MSVC {vc_version} and Windows SDK {sdk_version}')
    log('new sessions have the compiler; in this one run: . ~/.msvc/env.sh')


if __name__ == '__main__':
    main()
