#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = [
#   'pyyaml',
# ]
# ///
r"""agent_sandbox.py - Windows-native equivalent of agent_docker.py.

agent_docker.py builds agentic-programming sandboxes (running pi) in docker
containers; agent_tart.sh does the same on macOS with tart VMs. This script
does the same on Windows using Windows Sandbox, with no WSL and no docker:

  provision
           assemble the portable toolchain payload (run once, needs network)
  create   write a .wsb config, launch the sandbox, provision pi inside
  start    like create, but does not open an interactive shell in the
           sandbox (work with it via exec and cp)
  list     list known sandboxes
  ip       print the sandbox IP (or discover it if create did not wait)
  exec     interactive shell in the sandbox (or run a one-off command in it;
           both over ssh)
  update   apply --ollama to a running sandbox (execs set_ollama.sh in it)
  cp       copy a file or directory to or from the sandbox (host-initiated;
           exactly one endpoint is prefixed in:)
  stop     destroy the running sandbox (ephemeral by design: closing it
           discards everything, like `docker stop` on a --rm container)

cp usage (like docker cp; directories are copied recursively):
  agent_sandbox.py cp [--name NAME] <local path> in:<sandbox path>
  agent_sandbox.py cp [--name NAME] in:<sandbox path> <local path>

Isolation model (mirrors the docker flow):
  - The project directory is mapped READ-ONLY and copied to C:\\work inside
    the sandbox. The agent only ever edits the copy.
  - --local maps ~/.vimrc and ~/.vim read-only and ~/.vim_backup and pi's
    ~/.pi/agent/sessions read-write, exactly like agent_docker.py mounts.
  - Extra --mount entries appear at Desktop\\<basename> in the sandbox
    (Windows Sandbox does not allow choosing the guest-side path).
  - Results move only when the host copies them with cp.

The Hyper-V backend (for parallel sandboxes and dynamic memory) is planned;
only the Windows Sandbox backend is implemented. See agent_sandbox_provision.ps1
for what happens inside the sandbox and agent_sandbox_payload.ps1 for the
one-time payload assembly.
"""

import argparse
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

import yaml

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
SANDBOX_USER = 'WDAGUtilityAccount'
# The sandbox user's home in msys form, as seen by the payload bash.
SANDBOX_MSYS_HOME = f'/c/Users/{SANDBOX_USER}'
SANDBOX_PORT = 22
# The Microsoft Hyper-V virtual MAC OUI: the sandbox guest's ARP entry
# carries one of these, unlike physical LAN hosts.
HYPERV_MAC_PREFIX = '00-15-5d'


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.rstrip(' .')
    return name or 'sandbox'


def sandbox_running() -> bool:
    output = subprocess.check_output(
        ['tasklist', '/FI', 'IMAGENAME eq WindowsSandbox.exe'],
        text=True, stderr=subprocess.DEVNULL)
    return 'WindowsSandbox.exe' in output


def sandbox_pids() -> set:
    """The PIDs of all running WindowsSandbox.exe processes."""
    output = subprocess.check_output(
        ['tasklist', '/FI', 'IMAGENAME eq WindowsSandbox.exe',
         '/FO', 'CSV', '/NH'],
        text=True, stderr=subprocess.DEVNULL)
    pids = set()
    for line in output.splitlines():
        parts = line.split('","')
        if len(parts) > 1 and parts[0].strip('"') == 'WindowsSandbox.exe':
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def minimize_sandbox_windows(timeout: float = 120) -> None:
    """Minimize the sandbox windows once they appear.

    Windows Sandbox has no headless mode, so the window is the VM's
    display; minimizing keeps the VM running while the host workflow stays
    terminal-only. Gives up silently after timeout seconds, or as soon as
    the sandbox process goes away.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        from ctypes import wintypes
    except (AttributeError, ImportError, OSError):
        logger.debug('Windows API unavailable; not minimizing the window.')
        return
    SW_MINIMIZE = 6
    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (enum_proc_type, wintypes.LPARAM)
    user32.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
    deadline = time.time() + timeout
    seen_process = False
    while time.time() < deadline:
        pids = sandbox_pids()
        if pids:
            seen_process = True
        elif seen_process:
            # The sandbox is gone; stop waiting.
            break
        windows = []

        def callback(hwnd, _lparam, pids=pids, windows=windows):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                user32.ShowWindowAsync(hwnd, SW_MINIMIZE)
                windows.append(hwnd)
            return True

        user32.EnumWindows(enum_proc_type(callback), 0)
        if windows:
            logger.debug('Minimized %d sandbox window(s).', len(windows))
            return
        time.sleep(1)


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
        return parser.parse_intermixed_args(sys.argv[1:2] + args)
    except Exception as exc:
        print('Failed to merge config file', args, exc)
        return args


def arp_table() -> dict:
    """Map of IP to lowercase MAC for all entries in the host ARP table."""
    output = subprocess.check_output(['arp', '-a'], text=True)
    found = {}
    for line in output.splitlines():
        match = re.match(
            r'\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F-]+)\s+\w+', line)
        if match:
            found[match.group(1)] = match.group(2).lower()
    return found


def port_open(ip: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0


def sandbox_subnets() -> list:
    """The /24 prefixes of the host's Windows Sandbox virtual adapters.

    Sandbox guests live on the NAT subnet of the host's Windows Sandbox
    virtual adapter, which is disjoint from the LAN, so discovery can
    ignore unrelated hosts (a corporate gateway answering on port 22, say).
    Returns an empty list if no adapter is found (e.g. before the first
    sandbox launch); discovery then falls back to other candidate signals.
    """
    try:
        output = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             ('Get-NetIPAddress -AddressFamily IPv4 | Where-Object { '
              "$_.InterfaceAlias -like '*Windows Sandbox*' } | "
              'Select-Object -ExpandProperty IPAddress')],
            text=True, stderr=subprocess.DEVNULL)
    except subprocess.SubprocessError as exc:
        logger.debug('Windows Sandbox subnet query failed: %s', exc)
        return []
    subnets = [line.strip().rsplit('.', 1)[0] + '.'
               for line in output.splitlines() if line.strip()]
    if not subnets:
        # Nothing matched: dump the host's IPv4 interfaces so a -vv run
        # shows what the sandbox adapter is actually called here.
        try:
            listing = subprocess.check_output(
                ['powershell', '-NoProfile', '-Command',
                 ('Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { '
                  '"{0}: {1}" -f $_.InterfaceAlias, $_.IPAddress }')],
                text=True, stderr=subprocess.DEVNULL)
            logger.debug('Host IPv4 interfaces: %s',
                         [line for line in listing.splitlines()
                          if line.strip()])
        except subprocess.SubprocessError as exc:
            logger.debug('Interface listing failed: %s', exc)
    logger.debug('Windows Sandbox subnets: %s', subnets)
    return subnets


def discover_ip(timeout: float) -> str | None:
    """Find the sandbox IP: ARP entries whose port 22 answers.

    Candidates are restricted to the Windows Sandbox NAT subnet when that
    can be determined, and to Hyper-V virtual MACs (the guest is a Hyper-V
    VM, so this identifies it even when the adapter query fails); a
    physical host like a LAN gateway answering on port 22 can never match.
    """
    subnets = sandbox_subnets()
    if not subnets:
        logger.warning(
            'Could not determine the Windows Sandbox subnet; falling back '
            'to ARP entries with Hyper-V virtual MACs.')
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        table = arp_table()
        # Hyper-V virtual MACs are the sandbox (or another Hyper-V VM); a
        # physical host like a LAN gateway never has one.
        candidates = {ip for ip, mac in table.items()
                      if mac.startswith(HYPERV_MAC_PREFIX)}
        if subnets:
            # The subnet filter needs no snapshot: it cannot match a LAN
            # host, and dropping it would mask a sandbox that reused a
            # previous sandbox's still-cached ARP entry.
            candidates |= {ip for ip in table
                           if any(ip.startswith(s) for s in subnets)}
        seen |= candidates
        logger.debug('IP discovery candidates: %s', sorted(candidates))
        for ip in sorted(candidates):
            if port_open(ip, SANDBOX_PORT):
                return ip
        time.sleep(2)
    if seen:
        logger.warning(
            'Saw candidate ARP entries %s but none answered on port %s; is '
            'sshd running in the sandbox? Check '
            '%%TEMP%%\\agent_sandbox_provision.log in the sandbox window.',
            sorted(seen), SANDBOX_PORT)
    else:
        logger.warning(
            'No candidate ARP entries appeared; the sandbox may not be '
            'running or has not networked yet.')
    return None


def ssh_options(key: str) -> list:
    return [
        '-i', key,
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=NUL',
        '-o', 'LogLevel=ERROR',
    ]


def get_ip(base_dir: str, name: str, wait: float = 120) -> str:
    """Known sandbox IP: the guest-published one, then ARP discovery."""
    run_dir = os.path.join(base_dir, name)
    for ip_file in (os.path.join(run_dir, 'artifacts', 'ip'),
                    os.path.join(run_dir, 'ip')):
        if not os.path.exists(ip_file):
            continue
        cached = open(ip_file).read().strip()
        subnets = sandbox_subnets()
        if subnets:
            ok = any(cached.startswith(s) for s in subnets)
        else:
            # Without a subnet, trust the cache only if its ARP entry has a
            # Hyper-V MAC, or is absent (it may have aged out).
            ok = arp_table().get(
                cached, HYPERV_MAC_PREFIX).startswith(HYPERV_MAC_PREFIX)
        if ok:
            return cached
        # A cached address that cannot be the sandbox (e.g. a false
        # discovery or a stale file) is dropped.
        logger.warning(
            'Cached ip %s in %s cannot be the sandbox; removing it.',
            cached, ip_file)
        os.remove(ip_file)
    ip = discover_ip(wait)
    if not ip:
        msg = (f'Could not determine the IP for sandbox {name}. '
               'Is it running? Try create again with waiting enabled.')
        raise SystemExit(msg)
    ip_file = os.path.join(run_dir, 'ip')
    os.makedirs(os.path.dirname(ip_file), exist_ok=True)
    with open(ip_file, 'w') as handle:
        handle.write(ip + '\n')
    return ip


def wait_published_ip(artifacts: str, timeout: float) -> str | None:
    """Wait for the guest to publish its IP into the artifacts folder."""
    published = os.path.join(artifacts, 'ip')
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(published):
            ip = open(published).read().strip()
            if ip:
                return ip
        time.sleep(2)
    return None


def wait_sandbox_ip(artifacts: str, timeout: float) -> str | None:
    """The guest-published IP, or ARP discovery as a fallback."""
    ip = wait_published_ip(artifacts, timeout)
    if not ip:
        logger.debug('No ip published; falling back to ARP discovery.')
        ip = discover_ip(timeout)
    return ip


def stage_local(run_dir: str) -> list:
    """Stage the --local support files that must be mapped as a folder.

    Windows Sandbox maps whole folders at fixed Desktop paths, so single
    files (like .vimrc) are copied into a staging folder and mapped from
    there; the provisioner puts them in the sandbox profile.
    """
    stage = os.path.join(run_dir, 'local')
    os.makedirs(stage, exist_ok=True)
    for candidate in (os.path.expanduser('~/.vimrc'), os.path.expanduser('~/_vimrc')):
        if os.path.exists(candidate):
            shutil.copy(candidate, os.path.join(stage, '.vimrc'))
            break
    vim_dir = os.path.expanduser('~/.vim')
    target = os.path.join(stage, '.vim')
    if os.path.isdir(vim_dir) and not os.path.exists(target):
        # A junction avoids copying the whole vim runtime; the mapping is
        # read-only anyway.
        result = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', target, vim_dir],
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            shutil.copytree(vim_dir, target, dirs_exist_ok=True)
    return [stage]


def build_wsb(mappings: list, logon_command: str, cpus: int, memory: int) -> str:
    folders = ''.join(
        '    <MappedFolder>\n'
        f'      <HostFolder>{escape(path)}</HostFolder>\n'
        f'      <ReadOnly>{"true" if mode == "ro" else "false"}</ReadOnly>\n'
        '    </MappedFolder>\n'
        for path, mode in mappings)
    return f"""<Configuration>
  <VGpu>Enable</VGpu>
  <Networking>Enable</Networking>
  <AudioInput>Disable</AudioInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MemoryInMB>{memory}</MemoryInMB>
  <CpuCount>{cpus}</CpuCount>
  <MappedFolders>
{folders}  </MappedFolders>
  <LogonCommand>
    <Command>{escape(logon_command)}</Command>
  </LogonCommand>
</Configuration>
"""


def wait_sandbox_started(timeout: float = 15) -> bool:
    """Wait briefly for the Windows Sandbox process to appear."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sandbox_running():
            return True
        time.sleep(1)
    return False


def launch_sandbox(wsb_path: str) -> None:
    """Launch a .wsb configuration the way a double-click would.

    os.startfile is the ShellExecute path Explorer takes, which works where
    `cmd /c start` may not; fall back to running WindowsSandbox.exe
    directly if the association cannot be resolved.
    """
    try:
        os.startfile(wsb_path)
    except OSError as exc:
        logger.warning(
            'os.startfile failed (%s); trying WindowsSandbox.exe directly.',
            exc)
        sandbox_exe = os.path.join(
            os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32',
            'WindowsSandbox.exe')
        try:
            subprocess.Popen([sandbox_exe, wsb_path])
        except OSError as exc2:
            msg = f'Could not launch Windows Sandbox: {exc2}'
            raise SystemExit(msg)


def create(args, parser):
    if args.backend != 'wsb':
        msg = ('Only the Windows Sandbox (wsb) backend is implemented; the '
               'Hyper-V backend for parallel sandboxes is not yet written.')
        raise SystemExit(msg)
    if sandbox_running():
        msg = ('A Windows Sandbox is already running and only one can exist at '
               'a time. Run `agent_sandbox.py stop` first.')
        raise SystemExit(msg)
    base_dir = os.path.expanduser('~/.agent_sandbox')
    payload_dir = os.path.join(base_dir, 'agent_payload')
    if not os.path.exists(os.path.join(payload_dir, 'install_pi.sh')):
        msg = 'Payload not assembled. Run: agent_sandbox.py provision'
        raise SystemExit(msg)
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    project_name = os.path.basename(os.getcwd())
    args = add_config(args, os.getcwd(), parser)
    name = args.name or f'agent_{safe_filename(project_name)}'
    if args.num is not None:
        name = f'{name}_{args.num}'
    run_dir = os.path.join(base_dir, name)
    os.makedirs(run_dir, exist_ok=True)
    logger.debug('Parsed arguments: %r', args)

    # Mapped folders appear on the sandbox Desktop under their host basename.
    # A read-write artifacts folder is the guest's channel back to the host:
    # the provisioner publishes its transcript and detected IP there, so
    # neither the window nor ARP discovery is needed to follow progress.
    artifacts = os.path.join(run_dir, 'artifacts')
    os.makedirs(artifacts, exist_ok=True)
    mappings = [(payload_dir, 'ro'), (os.getcwd(), 'ro'), (artifacts, 'rw')]
    if args.local:
        for staged in stage_local(run_dir):
            mappings.append((staged, 'ro'))
        vim_backup = os.path.expanduser('~/.vim_backup')
        os.makedirs(vim_backup, exist_ok=True)
        mappings.append((vim_backup, 'rw'))
        sessions = os.path.expanduser('~/.pi/agent/sessions')
        os.makedirs(sessions, exist_ok=True)
        mappings.append((sessions, 'rw'))
    for entry in args.mount:
        # The mode suffix is stripped by suffix match rather than by splitting
        # on ':', so Windows paths with drive letters (C:\tools:rw) survive.
        mode = 'ro'
        match = re.search(r':(ro|rw)$', entry, re.IGNORECASE)
        if match:
            mode = match.group(1).lower()
            path = entry[:match.start()]
        else:
            path = entry
        if not os.path.isdir(path):
            msg = f'Mount path does not exist or is not a folder: {path}'
            raise SystemExit(msg)
        mappings.append((path, mode))
    # The logon command runs as the sandbox user; mapped folders are always
    # at C:\Users\WDAGUtilityAccount\Desktop\<host basename>.
    desktop = f'C:\\Users\\{SANDBOX_USER}\\Desktop'
    logon_command = (
        'powershell -ExecutionPolicy Bypass -NoProfile -File '
        f'"{desktop}\\agent_payload\\provision.ps1"'
        f' -ProjectName "{project_name}"'
        f' -Skips "{",".join(args.skips or [])}"' +
        (f' -Ollama "{args.ollama}"' if args.ollama else '') +
        (' -NoShell' if args.command == 'start' else ''))
    wsb = build_wsb(
        mappings, logon_command, args.cpus, args.memory)
    wsb_path = os.path.join(run_dir, 'agent.wsb')
    with open(wsb_path, 'w', encoding='utf-8') as handle:
        handle.write(wsb)
    logger.info('Launching %s', wsb_path)
    launch_sandbox(wsb_path)
    if args.minimize:
        threading.Thread(
            target=minimize_sandbox_windows, daemon=True).start()
    # Windows Sandbox takes a moment to appear; if its process never shows
    # up, the feature is likely not enabled and waiting for an IP would be
    # pointless.
    if not wait_sandbox_started():
        msg = ('Windows Sandbox did not start. Is the Windows Sandbox '
               'feature enabled (in an admin prompt: Enable-'
               'WindowsOptionalFeature -Online -FeatureName '
               '"Containers-DisposableClientVM")? Windows Sandbox needs '
               'Windows Pro or Enterprise. Test by double-clicking '
               f'{wsb_path} by hand.')
        raise SystemExit(msg)
    # The guest publishes its IP once the provisioner brings up sshd; ARP
    # discovery is only the fallback for when that never happens.
    print(f'Waiting up to {args.wait_timeout} seconds for the sandbox IP; '
          'run with -vv to watch discovery.')
    ip = wait_sandbox_ip(artifacts, args.wait_timeout)
    if ip:
        with open(os.path.join(run_dir, 'ip'), 'w') as handle:
            handle.write(ip + '\n')
        print(f'Sandbox {name} is up at {ip}')
    else:
        logger.warning(
            'Could not discover the sandbox IP yet; provisioning may still '
            'be running. Use `agent_sandbox.py ip %s` later, and see '
            '%%TEMP%%\\agent_sandbox_provision.log in the sandbox window.',
            name)
    print(f'Copy files with: agent_sandbox.py [--name {name}] cp <local path> '
          f'in:<sandbox path> or agent_sandbox.py [--name {name}] cp '
          'in:<sandbox path> <local path>')


def list_sandboxes(base_dir: str):
    if not os.path.isdir(base_dir):
        return
    running = sandbox_running()
    for entry in sorted(os.listdir(base_dir)):
        run_dir = os.path.join(base_dir, entry)
        if (not os.path.isdir(run_dir) or
                not os.path.exists(os.path.join(run_dir, 'agent.wsb'))):
            continue
        ip = ''
        ip_file = os.path.join(run_dir, 'ip')
        if os.path.exists(ip_file):
            ip = open(ip_file).read().strip()
        state = 'running' if running else 'closed'
        print(entry, state, ip)


def stop():
    if not sandbox_running():
        print('No sandbox is running.')
        return
    subprocess.check_call(['taskkill', '/F', '/IM', 'WindowsSandbox.exe'])
    # Everything in the sandbox is gone at this point; drop stale IPs.
    base_dir = os.path.expanduser('~/.agent_sandbox')
    if os.path.isdir(base_dir):
        for entry in os.listdir(base_dir):
            ip_file = os.path.join(base_dir, entry, 'ip')
            if os.path.exists(ip_file):
                os.remove(ip_file)


def run_provision(args):
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'agent_sandbox_payload.ps1')
    cmd = ['powershell', '-ExecutionPolicy', 'Bypass', '-NoProfile', '-File', script]
    if args.payload_dir:
        cmd += ['-PayloadDir', args.payload_dir]
    if args.force:
        cmd += ['-Force']
    subprocess.check_call(cmd)


def ssh_target(base_dir: str, args, parser) -> tuple:
    """Resolve the ssh key and user@ip target for the current sandbox."""
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    project_name = os.path.basename(os.getcwd())
    name = args.name or f'agent_{safe_filename(project_name)}'
    if args.num is not None:
        name = f'{name}_{args.num}'
    args = add_config(args, os.getcwd(), parser)
    key = os.path.join(base_dir, 'ssh', 'id_ed25519')
    ip = get_ip(base_dir, name)
    return key, f'{SANDBOX_USER}@{ip}'


def run_cp(args, key: str, target: str):
    """docker-cp-style copy: exactly one endpoint is prefixed in: and refers
    to a path in the sandbox (relative paths are relative to the sandbox
    user's home). scp -r copies directories recursively and is harmless for
    plain files.
    """
    if len(args.extras) != 2:
        msg = ('cp: usage agent_sandbox.py [--name <name>] cp '
               '<local path> in:<sandbox path> or agent_sandbox.py '
               '[--name <name>] cp in:<sandbox path> <local path>')
        raise SystemExit(msg)
    src, dst = args.extras
    src_in = src.startswith('in:')
    dst_in = dst.startswith('in:')
    if src_in == dst_in:
        msg = 'cp: exactly one endpoint must be prefixed in:'
        raise SystemExit(msg)
    remote = (src if src_in else dst)[3:].replace('\\', '/')
    local = dst if src_in else src
    cmd = (['scp', '-r'] + ssh_options(key) +
           ([local, f'{target}:{remote}'] if dst_in else
            [f'{target}:{remote}', local]))
    logger.info(cmd)
    subprocess.check_call(cmd)


def run_remote(key: str, target: str, command: str) -> str:
    """Run a command in the sandbox over ssh and return its output."""
    return subprocess.check_output(
        ['ssh'] + ssh_options(key) + [target, command], text=True)


def ollama_url(args) -> str:
    """Expand the --ollama value exactly like agent_docker.py: a bare value
    (a port or hostname) points at host.docker.internal, which the
    provisioner maps to the sandbox host in the guest hosts file (the analog
    of docker's --add-host argument).
    """
    host = args.ollama
    if '/' not in host and ':' not in host:
        host = f'host.docker.internal:{host}'
    if '/' not in host:
        host = f'http://{host}'
    return host.rstrip('/')


def run_update(args, key: str, target: str):
    """Apply --ollama to a running sandbox by execing set_ollama.sh in it,
    like agent_docker.py's update command.
    """
    if not args.ollama:
        msg = 'update requires --ollama/--url.'
        raise SystemExit(msg)
    url = ollama_url(args)
    logger.info('setting the ollama url to %s', url)
    # The sandbox default shell is cmd.exe, which cannot execute a .sh, so
    # route through the payload bash like the provisioner does.
    bash = f'C:\\Users\\{SANDBOX_USER}\\Desktop\\agent_payload\\git\\bin\\bash.exe'
    run_remote(
        key, target,
        f"{bash} -lc '{SANDBOX_MSYS_HOME}/.local/bin/set_ollama.sh {url}'")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'command',
        choices=['create', 'start', 'list', 'stop', 'ip', 'exec', 'update',
                 'cp', 'provision'],
        help='Command. create writes a .wsb and launches the sandbox; start '
        'does the same without the interactive shell. cp: exactly one '
        'endpoint is prefixed in:.')
    parser.add_argument('--name', help='Sandbox name')
    parser.add_argument('--num', type=int, help='Sandbox name suffix')
    parser.add_argument('--src', help='Source path. Defaults to the current working directory.')
    parser.add_argument(
        '--backend', default='wsb', choices=['wsb', 'hv'],
        help='Only wsb (Windows Sandbox) is implemented. Default %(default)s')
    parser.add_argument(
        '--ollama', '--url',
        help='Replacement url for ollama. This can be just a port, a host and '
        'port, or a full base url. Defaults to the ollama on the host.')
    parser.add_argument(
        '--local', action='store_true',
        help='Expose .vimrc, .vim, .vim_backup, and pi sessions to the sandbox. '
        'This removes some isolation.')
    parser.add_argument('--no-local', dest='local', action='store_false')
    parser.add_argument(
        '--minimize', action='store_true', default=True,
        help='Minimize the sandbox window after launch; Windows Sandbox has '
        'no headless mode, so the window is the VM display. Default on.')
    parser.add_argument(
        '--no-minimize', dest='minimize', action='store_false',
        help='Leave the sandbox window as-is after launch.')
    parser.add_argument(
        '--mount', action='append', default=[],
        help='Map an extra folder into the sandbox. Appears at Desktop/<basename>. '
        'Format is <path>:ro or <path>:rw (read-only if the suffix is omitted).')
    parser.add_argument(
        '--skip', action='append', dest='skips',
        help='Files or folders to exclude from the copy. Use commas for multiple '
        'patterns in one --skip or repeat --skip.')
    parser.add_argument(
        '--cpus', default=4, type=int, help='Sandbox cpu count. Default %(default)s')
    parser.add_argument(
        '--memory', default=8192, type=int,
        help='Sandbox memory in MB. Default %(default)s')
    parser.add_argument(
        '--wait-timeout', default=300, type=int,
        help='Seconds to wait for the sandbox IP after launch. Default %(default)s')
    parser.add_argument(
        '--config',
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent_sandbox.yaml'),
        help='Config file with keys as the working directories with arrays of '
        'default arguments for this script. Default %(default)s')
    parser.add_argument(
        'extras', nargs='*',
        help='exec: remote command (interactive shell if omitted). cp: the '
        'two endpoints, one of which is prefixed in:')
    parser.add_argument('--force', action='store_true', help='provision: redownload tools')
    parser.add_argument('--payload-dir', help='provision: payload directory to assemble')
    parser.add_argument('--verbose', '-v', action='count', default=0, help='Increase verbosity')
    args = parser.parse_intermixed_args()
    if args.command in {'provision'}:
        run_provision(args)
        return
    if platform.system().lower() != 'windows':
        logger.warning('This script only manages sandboxes on Windows.')
    base_dir = os.path.expanduser('~/.agent_sandbox')
    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))

    if args.command in {'create', 'start'}:
        create(args, parser)
    elif args.command == 'list':
        list_sandboxes(base_dir)
    elif args.command == 'stop':
        stop()
    elif args.command in {'ip', 'exec', 'cp', 'update'}:
        key, target = ssh_target(base_dir, args, parser)
        if args.command == 'ip':
            print(target.split('@')[-1])
        elif args.command == 'exec':
            if args.extras:
                subprocess.check_call(['ssh'] + ssh_options(key) + [target] + args.extras)
            else:
                # Interactive shell, like `docker exec -it ... bash` and
                # agent_tart.sh's `ssh -t`.
                subprocess.check_call(['ssh', '-t'] + ssh_options(key) + [target])
        elif args.command == 'cp':
            run_cp(args, key, target)
        elif args.command == 'update':
            run_update(args, key, target)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
