#!/usr/bin/env python3
# /// script
# requires-python = '>=3.10'
# dependencies = [
#   'pyyaml',
# ]
# ///
r"""agent_sandbox.py - Windows Sandbox equivalent of agent_docker.py.
"""

import argparse
import ctypes
import json
import logging
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from xml.sax.saxutils import escape

import yaml

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.expanduser(os.environ.get('AGENT_SANDBOX_DIR', '~/.agent_sandbox'))
PAYLOAD_DIR = os.path.join(BASE_DIR, 'payload')
KNOWN_HOSTS = os.path.join(BASE_DIR, 'known_hosts')
# Guest-side paths.  The payload is mapped read-only (read-write while
# provisioning); each sandbox also gets a read-write "run" folder through
# which the provisioner receives its arguments and reports its log, the
# guest IP and a done marker.
GUEST_PAYLOAD = r'C:\payload'
GUEST_RUN = r'C:\mnt\run'
GUEST_PROJECT = r'C:\mnt\project'
SCRIPTS = ('agent_sandbox_common.ps1', 'agent_sandbox_payload.ps1',
           'agent_sandbox_provision.ps1', 'install_pi.sh', '.vimrc', 'cover.py')
# Windows 10 sandbox processes (the file backend has no other handle).
SANDBOX_PROCESSES = ('WindowsSandbox.exe', 'WindowsSandboxClient.exe',
                     'WindowsSandboxRemoteSession.exe', 'WindowsSandboxServer.exe')
GUID_RE = re.compile(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}')
IPV4_RE = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b')


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.rstrip(' .')
    return name or 'docker'


def clear_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_readonly)
    else:
        shutil.rmtree(path, onerror=clear_readonly)


def die(msg: str):
    msg = f'agent_sandbox: {msg}'
    raise SystemExit(msg)


def info(msg: str):
    print(f'agent_sandbox: {msg}', file=sys.stderr)


def run(cmd, **kwargs):
    logger.info(subprocess.list2cmdline(cmd))
    return subprocess.run(cmd, **kwargs)


def host_memory_mb() -> int:
    """Return the host's total physical memory in MB, or 0 if unknown."""

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ('dwLength', ctypes.c_ulong),
            ('dwMemoryLoad', ctypes.c_ulong),
            ('ullTotalPhys', ctypes.c_ulonglong),
            ('ullAvailPhys', ctypes.c_ulonglong),
            ('ullTotalPageFile', ctypes.c_ulonglong),
            ('ullAvailPageFile', ctypes.c_ulonglong),
            ('ullTotalVirtual', ctypes.c_ulonglong),
            ('ullAvailVirtual', ctypes.c_ulonglong),
            ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
        ]

    try:
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0
        return status.ullTotalPhys // (1024 * 1024)
    except Exception:
        return 0


def add_config(args, src, parser):
    """Per-working-directory defaults, exactly like agent_docker.py."""
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


def pick_backend(requested: str) -> str:
    if requested != 'auto':
        return requested
    return 'cli' if shutil.which('wsb') else 'file'


def wsb(*args, check=True) -> str:
    cmd = ['wsb', *args]
    # list is polled every couple of seconds; keep it out of -v output.
    logger.log(logging.DEBUG if args[0] == 'list' else logging.INFO,
               subprocess.list2cmdline(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode:
        die(f'{subprocess.list2cmdline(cmd)} failed ({result.returncode}): '
            f'{result.stdout.strip()} {result.stderr.strip()}')
    return result.stdout + result.stderr


def cli_running_ids() -> set:
    output = wsb('list', '--raw', check=False)
    return {match.lower() for match in GUID_RE.findall(output)}


def cli_ip(sandbox_id: str) -> str | None:
    output = wsb('ip', '--id', sandbox_id, check=False)
    for ip in IPV4_RE.findall(output):
        if not ip.startswith('127.') and not ip.startswith('169.254.'):
            return ip
    return None


def file_sandbox_running() -> bool:
    output = subprocess.run(
        ['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True).stdout
    return any(f'"{name}"' in output for name in SANDBOX_PROCESSES)


def file_stop_sandbox():
    for name in SANDBOX_PROCESSES:
        subprocess.run(['taskkill', '/F', '/IM', name], capture_output=True)


class Sandbox:
    """A named sandbox and its host-side state directory."""

    def __init__(self, name: str, backend: str):
        self.name = name
        self.backend = backend
        self.dir = os.path.join(BASE_DIR, name)
        self.run_dir = os.path.join(self.dir, 'run')
        self.id_file = os.path.join(self.dir, 'id')

    @property
    def sandbox_id(self) -> str | None:
        if os.path.exists(self.id_file):
            value = open(self.id_file).read().strip()
            return value or None
        return None

    def running(self) -> bool:
        if self.backend == 'cli':
            sid = self.sandbox_id
            return bool(sid) and sid.lower() in cli_running_ids()
        return os.path.exists(self.id_file) and file_sandbox_running()

    def ip(self) -> str | None:
        ip = None
        if self.backend == 'cli' and self.sandbox_id:
            ip = cli_ip(self.sandbox_id)
        if not ip:
            published = os.path.join(self.run_dir, 'ip')
            if os.path.exists(published):
                ip = open(published).read().strip() or None
        return ip

    def stop(self):
        if self.backend == 'cli':
            sid = self.sandbox_id
            if sid and sid.lower() in cli_running_ids():
                info(f'stopping {self.name} ({sid})')
                wsb('stop', '--id', sid, check=False)
        elif os.path.exists(self.id_file) and file_sandbox_running():
            info(f'stopping {self.name}')
            file_stop_sandbox()
        if os.path.exists(self.id_file):
            os.remove(self.id_file)

    def start(self, config_xml: str, logon_command: str | None):
        """Launch the sandbox; records its id (cli) or a marker (file)."""
        os.makedirs(self.dir, exist_ok=True)
        if self.backend == 'cli':
            # One line: the configuration travels on the command line.
            output = wsb('start', '--config', re.sub(r'\n\s*', '', config_xml))
            match = GUID_RE.search(output)
            if not match:
                die(f'wsb start did not report a sandbox id: {output.strip()}')
            with open(self.id_file, 'w') as fp:
                fp.write(match.group(0) + '\n')
            info(f'started {self.name} ({match.group(0)})')
            return
        if file_sandbox_running():
            die('a Windows Sandbox is already running and Windows 10 allows '
                'only one; stop it first (agent_sandbox.py stop)')
        if logon_command:
            config_xml = config_xml.replace(
                '</Configuration>',
                f'  <LogonCommand>\n    <Command>{escape(logon_command)}</Command>\n'
                '  </LogonCommand>\n</Configuration>')
        wsb_path = os.path.join(self.dir, f'{self.name}.wsb')
        with open(wsb_path, 'w', encoding='utf-8') as fp:
            fp.write(config_xml)
        with open(self.id_file, 'w') as fp:
            fp.write('file\n')
        info(f'launching {wsb_path} (keep the sandbox window open; closing it '
             'destroys the sandbox)')
        os.startfile(wsb_path)
        deadline = time.time() + 60
        while time.time() < deadline and not file_sandbox_running():
            time.sleep(1)
        if not file_sandbox_running():
            os.remove(self.id_file)
            die('Windows Sandbox did not start; is the feature enabled? '
                '(Enable-WindowsOptionalFeature -Online -FeatureName '
                'Containers-DisposableClientVM, then reboot)')

    def exec_system(self, command: str) -> subprocess.Popen | None:
        """Cli backend: start a command in the sandbox as SYSTEM (no output
        capture is possible; the provisioners report through the run folder).
        """
        if self.backend != 'cli':
            return None
        cmd = ['wsb', 'exec', '--id', self.sandbox_id, '-r', 'System', '-c', command]
        logger.info(subprocess.list2cmdline(cmd))
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_provisioner(self, command: str, timeout: float) -> bool:  # noqa
        """Run a provisioner script and wait for its done/failed marker.

        With the cli backend the script is started with `wsb exec`, which
        can fail while the sandbox is still booting, so it is retried until
        the script's log shows it started.  With the file backend the
        LogonCommand already started it.
        """
        done = os.path.join(self.run_dir, 'done')
        failed = os.path.join(self.run_dir, 'failed')
        log = os.path.join(self.run_dir, 'provision.log')
        deadline = time.time() + timeout
        proc = self.exec_system(command)
        attempts = 1
        last_size = 0
        while time.time() < deadline:
            if os.path.exists(failed):
                info(f'provisioning failed; see {log}')
                try:
                    print(open(failed, errors='replace').read(), file=sys.stderr)
                except OSError:
                    pass
                return False
            if os.path.exists(done):
                return True
            if not self.running():
                info('the sandbox is no longer running')
                return False
            if proc is not None and proc.poll() is not None:
                if proc.returncode and not os.path.exists(log):
                    if attempts >= 20:
                        info(f'wsb exec kept failing ({proc.returncode}); giving up')
                        return False
                    attempts += 1
                    logger.info('wsb exec failed before the script started; retrying')
                    time.sleep(5)
                    proc = self.exec_system(command)
                    continue
                if proc.returncode:
                    # The script ran but exited abnormally; give the marker
                    # a moment, then report.
                    time.sleep(3)
                    if os.path.exists(done):
                        return True
                    if not os.path.exists(failed):
                        info(f'the provisioner exited with {proc.returncode} '
                             f'without finishing; see {log}')
                        return False
                    continue
                proc = None
            try:
                size = os.path.getsize(log)
            except OSError:
                size = 0
            if size != last_size and logger.isEnabledFor(logging.INFO):
                with open(log, 'rb') as fp:
                    fp.seek(last_size)
                    chunk = fp.read()
                sys.stderr.write(chunk.decode('utf-8-sig', errors='replace'))
                last_size = size
            time.sleep(2)
        info(f'timed out after {timeout} seconds waiting for provisioning; see {log}')
        return False


def mapped_folder(host: str, guest: str, readonly: bool) -> str:
    return (
        '    <MappedFolder>\n'
        f'      <HostFolder>{escape(os.path.normpath(host))}</HostFolder>\n'
        f'      <SandboxFolder>{escape(guest)}</SandboxFolder>\n'
        f'      <ReadOnly>{"true" if readonly else "false"}</ReadOnly>\n'
        '    </MappedFolder>\n')


def build_config(mappings: list, memory: int, gpu: bool) -> str:
    folders = ''.join(mapped_folder(*m) for m in mappings)
    return (
        '<Configuration>\n'
        f'  <vGPU>{"Enable" if gpu else "Disable"}</vGPU>\n'
        '  <Networking>Enable</Networking>\n'
        '  <AudioInput>Disable</AudioInput>\n'
        '  <VideoInput>Disable</VideoInput>\n'
        '  <PrinterRedirection>Disable</PrinterRedirection>\n'
        '  <ClipboardRedirection>Disable</ClipboardRedirection>\n'
        f'  <MemoryInMB>{int(memory)}</MemoryInMB>\n'
        '  <MappedFolders>\n'
        f'{folders}'
        '  </MappedFolders>\n'
        '</Configuration>\n')


def provisioner_command(script: str) -> str:
    return ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File '
            f'{GUEST_PAYLOAD}\\scripts\\{script}')


def refresh_scripts():
    """Copy the in-sandbox scripts into the payload so edits take effect on
    the next run (the payload itself is otherwise only built by provision).
    """
    scripts = os.path.join(PAYLOAD_DIR, 'scripts')
    os.makedirs(scripts, exist_ok=True)
    for name in SCRIPTS:
        src = os.path.join(SELF_DIR, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(scripts, name))
        elif name.startswith('agent_sandbox') or name == 'install_pi.sh':
            die(f'{src} is missing')


def prepare_run_dir(sandbox: Sandbox, args_dict: dict, pubkey: str):
    """Write the provisioner's inputs into a fresh run folder."""
    os.makedirs(sandbox.run_dir, exist_ok=True)
    with open(os.path.join(sandbox.run_dir, 'args.json'), 'w') as fp:
        json.dump(args_dict, fp, indent=2)
    with open(os.path.join(sandbox.run_dir, 'authorized_keys'), 'w') as fp:
        fp.write(pubkey.strip() + '\n')


def ensure_ssh_key(sshkey: str | None) -> str:
    """Return the private key path; same policy as agent_tart.sh."""
    if sshkey:
        pub = sshkey if sshkey.endswith('.pub') else sshkey + '.pub'
        if not os.path.exists(pub):
            die(f'ssh key {pub} not found')
        return pub[:-4]
    for name in ('id_ed25519', 'id_ecdsa', 'id_rsa'):
        key = os.path.expanduser(f'~/.ssh/{name}')
        if os.path.exists(key + '.pub'):
            return key
    key = os.path.expanduser('~/.ssh/id_ed25519')
    os.makedirs(os.path.dirname(key), exist_ok=True)
    info(f'generating a new ssh keypair at {key}')
    result = run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', key, '-q'])
    if result.returncode:
        die('ssh-keygen failed; is the Windows OpenSSH client installed?')
    return key


def ssh_base(key: str, batch: bool = True) -> list:
    opts = ['-i', key,
            '-o', f'UserKnownHostsFile={KNOWN_HOSTS}',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'LogLevel=ERROR',
            '-o', 'ConnectTimeout=10']
    if batch:
        opts += ['-o', 'BatchMode=yes']
    return opts


def port_open(ip: str, port: int = 22, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ssh(sandbox: Sandbox, key: str, user: str, timeout: float) -> str:
    """Wait until the sandbox has an IP and accepts our key over ssh."""
    deadline = time.time() + timeout
    ip = None
    while time.time() < deadline:
        ip = sandbox.ip()
        if ip and port_open(ip):
            result = subprocess.run(
                ['ssh'] + ssh_base(key) + [f'{user}@{ip}', 'exit'],
                capture_output=True, text=True)
            if result.returncode == 0:
                return ip
            logger.debug('ssh probe: %s', result.stderr.strip())
        if not sandbox.running():
            die(f'{sandbox.name} is not running')
        time.sleep(3)
    die(f'{sandbox.name} did not become reachable over ssh (ip {ip}); see '
        f'{os.path.join(sandbox.run_dir, "provision.log")}')


def require_running(sandbox: Sandbox, key: str, user: str) -> str:
    if not sandbox.running():
        die(f'{sandbox.name} is not running; create it in the project '
            'directory (or use --src / --name)')
    return wait_ssh(sandbox, key, user, 60)


def ssh_interactive(key: str, user: str, ip: str, command: list):
    cmd = ['ssh', '-t'] + ssh_base(key, batch=False) + [f'{user}@{ip}'] + command
    logger.info(subprocess.list2cmdline(cmd))
    raise SystemExit(subprocess.call(cmd))


def guest_path(path: str) -> str:
    """Normalize an in: path for scp: forward slashes; relative paths stay
    relative to the sandbox user's home.
    """
    path = path.replace('\\', '/')
    if path.startswith('~/'):
        path = path[2:]
    return path


def do_cp(key: str, user: str, ip: str, src: str, dst: str):
    src_in, dst_in = src.startswith('in:'), dst.startswith('in:')
    if src_in == dst_in:
        die('cp requires exactly one endpoint prefixed in:')
    target = f'{user}@{ip}'
    if src_in:
        cmd = ['scp', '-r'] + ssh_base(key) + [
            f'{target}:{guest_path(src[3:])}', dst]
    else:
        cmd = ['scp', '-r'] + ssh_base(key) + [
            src, f'{target}:{guest_path(dst[3:])}']
    result = run(cmd)
    if result.returncode:
        die(f'scp failed ({result.returncode})')


def claude_in(key: str, user: str, ip: str, claude_dir: str):
    """Copy a .claude-persist directory into the sandbox and run its
    install_claude.sh.
    """
    claude_dir = os.path.expanduser(claude_dir)
    if not os.path.isfile(os.path.join(claude_dir, 'install_claude.sh')):
        die(f'{claude_dir} does not contain install_claude.sh')
    info(f'copying {claude_dir} to ~/.claude-persist')
    with tempfile.TemporaryFile() as fp:
        with tarfile.open(fileobj=fp, mode='w') as tf:
            tf.add(claude_dir, arcname='.')
        fp.seek(0)
        result = run(['ssh'] + ssh_base(key) + [
            f'{user}@{ip}',
            'mkdir -p ~/.claude-persist && tar -xf - -C ~/.claude-persist'], stdin=fp)
    if result.returncode:
        die('copying the claude directory failed')
    result = run(['ssh'] + ssh_base(key) + [
        f'{user}@{ip}', 'bash ~/.claude-persist/install_claude.sh'])
    if result.returncode:
        info('install_claude.sh failed')


def claude_out(key: str, user: str, ip: str, claude_dir: str):
    """Run install_claude.sh update in the sandbox and copy the files it
    changed, which it puts in ~/.claude-outbox, back out.
    """
    claude_dir = os.path.expanduser(claude_dir)
    result = run(['ssh'] + ssh_base(key) + [
        f'{user}@{ip}', 'bash ~/.claude-persist/install_claude.sh update'])
    if result.returncode:
        die('install_claude.sh update failed')
    os.makedirs(claude_dir, exist_ok=True)
    info(f'copying ~/.claude-outbox to {claude_dir}')
    cmd = ['ssh'] + ssh_base(key) + [f'{user}@{ip}', 'tar -C ~/.claude-outbox -cf - .']
    logger.info(subprocess.list2cmdline(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdout, mode='r|') as tf:
        if hasattr(tarfile, 'data_filter'):
            tf.extractall(claude_dir, filter='data')
        else:
            tf.extractall(claude_dir)
    if proc.wait():
        die(f'copying ~/.claude-outbox failed ({proc.returncode})')


DEFAULT_OLLAMA_PORT = '11434'


def ollama_url(value: str | None) -> str:
    """Expand --ollama like agent_docker.py, adjusted for how the sandbox
    reaches the host: the host's ollama is forwarded into the sandbox with
    an ssh tunnel, so the default and a bare port point at the sandbox's own
    loopback.  host:port and full urls are used as given (the provisioner
    maps host.docker.internal to the sandbox's default gateway, which needs
    a host firewall rule; see agent_containers.md).
    """
    host = value or DEFAULT_OLLAMA_PORT
    if '/' not in host and ':' not in host:
        host = f'127.0.0.1:{host}'
    if '/' not in host:
        host = f'http://{host}'
    return host.rstrip('/')


def tunnel_port(url: str) -> int | None:
    """The port to forward when the ollama url points at the sandbox's own
    loopback, else None.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in {'127.0.0.1', 'localhost'}:
        return None
    return parsed.port or (443 if parsed.scheme == 'https' else 80)


def tunnel_note(args, key: str, ip: str, url: str):
    """Print the host-side ssh command that forwards ollama into the
    sandbox (agent_containers.md, "Windows Sandbox: reaching ollama").
    """
    port = tunnel_port(url)
    if port is None:
        return
    cmd = ['ssh', '-N', '-R', f'{port}:127.0.0.1:{port}']
    if args.sshkey:
        cmd += ['-i', key]
    # Sandbox IPs are reused with new host keys; the script's own
    # known_hosts file avoids a host-key-changed refusal on the next one.
    cmd += ['-o', f'UserKnownHostsFile={KNOWN_HOSTS}',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ServerAliveInterval=30',
            f'{args.user}@{ip}']
    info(f"pi uses ollama at {url}; to reach the host's ollama, keep this "
         'running in a host terminal:')
    print(f'  {subprocess.list2cmdline(cmd)}', file=sys.stderr)


def set_ollama(key: str, user: str, ip: str, value: str):
    url = ollama_url(value)
    info(f'setting ollama url to {url}')
    result = run(['ssh'] + ssh_base(key) + [
        f'{user}@{ip}', f"~/.local/bin/set_ollama.sh '{url}'"])
    if result.returncode:
        die('set_ollama.sh failed')


def collect_mounts(args, sandbox: Sandbox) -> tuple[list, list]:
    r"""Return (mappings, mount specs) for --local and --mount.

    Mappings are (host, guest, readonly) for the configuration.  Mount specs
    tell the provisioner where to link each mapped folder inside the sandbox
    user's home (docker: /home/ubuntu/...; here ~\\...).
    """
    mappings, specs = [], []
    count = 0

    def add(src, dst, readonly):
        nonlocal count
        src = os.path.expanduser(src)
        if not os.path.isdir(src):
            info(f'skipping mount of missing directory {src}')
            return
        count += 1
        if not dst:
            dst = os.path.basename(src.rstrip('/\\'))
        guest = f'C:\\mnt\\mount{count}'
        mappings.append((src, guest, readonly))
        specs.append({'guest': guest, 'dst': dst, 'readonly': readonly})
        info(f'sharing {src} as {dst or guest} ({"ro" if readonly else "rw"})')

    if args.local:
        os.makedirs(os.path.expanduser('~/.vim_backup'), exist_ok=True)
        os.makedirs(os.path.expanduser('~/.pi/agent/sessions'), exist_ok=True)
        add('~/.vim', '.vim', True)
        add('~/.vim_backup', '.vim_backup', False)
        add('~/.pi/agent/sessions', '.pi/agent/sessions', False)
        # .vimrc is a single file; Windows Sandbox maps folders only, so it
        # is staged into the run folder and copied by the provisioner.
        for candidate in ('~/.vimrc', '~/_vimrc'):
            candidate = os.path.expanduser(candidate)
            if os.path.exists(candidate):
                shutil.copy(candidate, os.path.join(sandbox.run_dir, 'vimrc'))
                break
    for entry in args.mount:
        # agent_docker.py's format: local:inside:ro.  Windows drive letters
        # contain colons, so parse from the right.
        mode = 'rw'
        rest = entry
        if re.search(r':(ro|rw)$', rest):
            mode = rest[-2:]
            rest = rest[:-3]
        drive = ''
        match = re.match(r'^([A-Za-z]:)([\\/].*)$', rest)
        if match:
            drive, rest = match.group(1), match.group(2)
        src, _, dst = rest.partition(':')
        src = drive + src
        # docker-style absolute guest paths under the home map to ~.
        dst = re.sub(r'^/home/[^/]+/', '', dst)
        add(src, dst, mode == 'ro')
    return mappings, specs


def cmd_provision(args, backend: str, key: str):
    if os.path.isdir(PAYLOAD_DIR):
        info(f'rebuilding the payload at {PAYLOAD_DIR} from scratch')
        for entry in os.listdir(PAYLOAD_DIR):
            path = os.path.join(PAYLOAD_DIR, entry)
            if os.path.isdir(path):
                remove_tree(path)
            else:
                os.chmod(path, stat.S_IWRITE)
                os.remove(path)
    os.makedirs(PAYLOAD_DIR, exist_ok=True)
    refresh_scripts()
    sandbox = Sandbox('agent_provision', backend)
    sandbox.stop()
    if os.path.isdir(sandbox.run_dir):
        remove_tree(sandbox.run_dir)
    prepare_run_dir(sandbox, {
        'mode': 'provision',
        'user': args.user,
        'pythons': args.pythons.split(','),
        'pip_tools': ['tox', 'pre-commit', 'virtualenv'],
    }, open(key + '.pub').read())
    config = build_config(
        [(PAYLOAD_DIR, GUEST_PAYLOAD, False), (sandbox.run_dir, GUEST_RUN, False)],
        args.memory, False)
    command = provisioner_command('agent_sandbox_payload.ps1')
    sandbox.start(config, command if backend == 'file' else None)
    info('provisioning (downloads git, node, pythons, OpenSSH and installs pi; '
         f'follow {os.path.join(sandbox.run_dir, "provision.log")} or use -v)')
    if not sandbox.run_provisioner(command, args.wait_timeout or 3600):
        die(f'provisioning failed; {sandbox.name} left running for debugging '
            '(agent_sandbox.py stop --name agent_provision)')
    sandbox.stop()
    versions = os.path.join(PAYLOAD_DIR, 'VERSIONS.txt')
    if os.path.exists(versions):
        print(open(versions).read().strip())
    info(f'done. Create sandboxes with: {sys.argv[0]} create')


def cmd_create(args, backend: str, key: str, sandbox: Sandbox, project: str):
    if not os.path.exists(os.path.join(PAYLOAD_DIR, 'home')):
        die(f'payload not provisioned at {PAYLOAD_DIR}; run {sys.argv[0]} provision')
    refresh_scripts()
    # Like agent_docker.py, create and start always destroy an existing
    # sandbox and rebuild it from the payload.
    sandbox.stop()
    os.makedirs(sandbox.dir, exist_ok=True)
    if os.path.isdir(sandbox.run_dir):
        remove_tree(sandbox.run_dir)
    os.makedirs(sandbox.run_dir)
    mappings = [(PAYLOAD_DIR, GUEST_PAYLOAD, True),
                (os.getcwd(), GUEST_PROJECT, True),
                (sandbox.run_dir, GUEST_RUN, False)]
    extra, specs = collect_mounts(args, sandbox)
    mappings += extra
    skips = []
    for s in args.skips or []:
        skips.extend(p.strip().rstrip('/').replace('/', '\\') for p in s.split(',') if p.strip())
    prepare_run_dir(sandbox, {
        'mode': 'create',
        'user': args.user,
        'project': project,
        'skips': skips,
        'mounts': specs,
        'ollama': ollama_url(args.ollama),
        'copy_tools': bool(args.copy_tools),
    }, open(key + '.pub').read())
    config = build_config(mappings, args.memory, args.gpu)
    command = provisioner_command('agent_sandbox_provision.ps1')
    sandbox.start(config, command if backend == 'file' else None)
    info(f'provisioning {sandbox.name} (follow '
         f'{os.path.join(sandbox.run_dir, "provision.log")} or use -v)')
    if not sandbox.run_provisioner(command, args.wait_timeout or 900):
        die(f'{sandbox.name} failed to provision; it is left running for '
            f'debugging (agent_sandbox.py stop --name {sandbox.name})')
    ip = wait_ssh(sandbox, key, args.user, 120)
    info(f'{sandbox.name} is ready at {ip}')
    tunnel_note(args, key, ip, ollama_url(args.ollama))
    if args.claude:
        claude_in(key, args.user, ip, args.claude)
    if args.command == 'create':
        ssh_interactive(key, args.user, ip, [])
    info(f'connect with: {sys.argv[0]} exec --name {sandbox.name}')


def cmd_list(backend: str):
    if not os.path.isdir(BASE_DIR):
        return
    running_ids = cli_running_ids() if backend == 'cli' else set()
    file_running = file_sandbox_running() if backend == 'file' else False
    for entry in sorted(os.listdir(BASE_DIR)):
        sandbox = Sandbox(entry, backend)
        if not os.path.exists(sandbox.id_file):
            continue
        sid = sandbox.sandbox_id or ''
        if backend == 'cli':
            state = 'running' if sid.lower() in running_ids else 'stopped'
        else:
            state = 'running' if file_running else 'stopped'
        print(f'{entry}\t{state}\t{sandbox.ip() or ""}\t{sid}')


def main():  # noqa
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'command',
        choices=['create', 'start', 'stop', 'exec', 'update', 'list', 'cp', 'provision', 'ip',
                 'claudeout'],
        help='Command. create is the same as start followed by exec. start '
        'destroys an existing container, if any, and creates a new one. stop '
        'destroys the container. exec opens a shell into an existing '
        'container and optionally runs a command. update sets the ollama url '
        'in a container. list shows running containers. cp copies into or out '
        'of a container via cp <src> <dest>, where the path inside the '
        'container is prefixed with in: and exactly one of src and dest must '
        'include in:. provision creates the container image. ip shows the '
        'container ip. claudeout runs install_claude.sh update in the '
        'container and copies the files that changed back to the --claude '
        'directory.')
    parser.add_argument(
        'operands', nargs='*', metavar='operand',
        help='For cp: two paths, exactly one prefixed in:.  For exec: an '
        'optional command to run instead of an interactive shell.')
    parser.add_argument('--name', help='Sandbox name')
    parser.add_argument('--num', type=int, help='Sandbox name suffix')
    parser.add_argument('--src', help='Source path.  Defaults to current working directory.')
    parser.add_argument(
        '--ollama', '--url',
        help='Replacement url for ollama.  This can be just a port, a host '
        'and port, or a full base url.  A bare port (default 11434) means '
        "the host's ollama forwarded into the sandbox over an ssh tunnel "
        '(the command is printed by create and start).')
    parser.add_argument(
        '--gpu', '--gpus', action='store_true',
        help='Enable the virtual GPU in the sandbox.')
    parser.add_argument('--no-gpu', '--no-gpus', dest='gpu', action='store_false')
    parser.add_argument(
        '--local', action='store_true',
        help='Mount local utilities directories (.vimrc, .vim, .vim_backup, '
        'pi sessions).  This removes some isolation.')
    parser.add_argument('--no-local', dest='local', action='store_false')
    parser.add_argument(
        '--mount', action='append', default=[],
        help='Mount a folder into the sandbox.  Recommended format is '
        'local:inside:ro, where inside is relative to the sandbox home '
        '(or ~ or a C:\\ path).')
    parser.add_argument(
        '--claude', nargs='?', const=os.path.join(SELF_DIR, 'claude'),
        type=lambda value: value or os.path.join(SELF_DIR, 'claude'),
        help='Local .claude-persist directory (containing install_claude.sh).  '
        'On create and start, its contents are copied to ~/.claude-persist in '
        'the sandbox and install_claude.sh is run.  claudeout copies back to '
        'it.  Without a value (--claude=), this is %(const)s.  Without '
        '--claude, Claude is not installed.')
    parser.add_argument('--no-claude', dest='claude', action='store_const', const=None)
    parser.add_argument(
        '--skip', action='append', dest='skips',
        help='Files or folders to exclude from the copy. Use commas for '
        'multiple patterns in one --skip (e.g., ".venv,*.pyc") or repeat '
        '--skip.')
    parser.add_argument(
        '--memory', default=max(8192, host_memory_mb() // 2), type=int,
        help='Sandbox memory in MB.  Default is the greater of 8192 and half '
        'of the host memory (only one sandbox can run at a time), '
        '%(default)s')
    parser.add_argument(
        '--sshkey', help='Public key to use (default ~/.ssh/id_ed25519.pub, '
        'id_ecdsa, or id_rsa; generated if none exists).')
    parser.add_argument(
        '--user', default='agent',
        help='Non-administrator sandbox user to create and log in as.  '
        'WDAGUtilityAccount uses the built-in account instead (demoted from '
        'Administrators).  Default %(default)s')
    parser.add_argument(
        '--backend', default='auto', choices=['auto', 'cli', 'file'],
        help='cli: wsb.exe (Windows 11 24H2+); file: .wsb file launch '
        '(Windows 10).  Default: cli when wsb.exe is on PATH.')
    parser.add_argument(
        '--pythons', default='3.10,3.11,3.12,3.13,3.14',
        help='provision: python minor versions to include.  Default %(default)s')
    parser.add_argument(
        '--copy-tools', action='store_true', default=True,
        help='Copy the toolchain to C:\\tools in the sandbox (default) rather '
        'than using it from the read-only payload mapping.')
    parser.add_argument('--no-copy-tools', dest='copy_tools', action='store_false')
    parser.add_argument(
        '--wait-timeout', type=int,
        help='Seconds to wait for provisioning (default 3600 for provision, '
        '900 for create).')
    parser.add_argument(
        '--config',
        default=os.path.join(SELF_DIR, 'agent_sandbox.yaml'),
        help='Config file with keys as the working directories with arrays of '
        'default arguments for this script. Default %(default)s')
    parser.add_argument(
        '--verbose', '-v', action='count', default=0,
        help='Increase verbosity')
    args = parser.parse_intermixed_args()
    if args.src:
        os.chdir(os.path.expanduser(args.src))
    current_dir = os.path.basename(os.getcwd())
    args = add_config(args, os.getcwd(), parser)

    logger.setLevel(max(1, logging.WARNING - args.verbose * 10))
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.debug('Parsed arguments: %r', args)

    if platform.system().lower() != 'windows':
        die('this script manages Windows Sandbox and must run on Windows')
    for tool in ('ssh', 'scp'):
        if not shutil.which(tool):
            die(f'{tool} not found; install the Windows OpenSSH client '
                '(Settings > Optional features)')
    os.makedirs(BASE_DIR, exist_ok=True)
    backend = pick_backend(args.backend)
    if backend == 'cli' and not shutil.which('wsb'):
        die('wsb.exe not found; use --backend file on this Windows version')
    logger.info('backend: %s', backend)

    basename = args.name or f'agent_{safe_filename(current_dir)}'
    name = basename + (f'_{args.num}' if args.num is not None else '')
    sandbox = Sandbox(name, backend)
    key = None
    if args.command not in {'list', 'stop', 'ip'}:
        key = ensure_ssh_key(args.sshkey)
    if args.command == 'provision':
        cmd_provision(args, backend, key)
    elif args.command in {'create', 'start'}:
        cmd_create(args, backend, key, sandbox, current_dir)
    elif args.command == 'stop':
        sandbox.stop()
    elif args.command == 'list':
        cmd_list(backend)
    elif args.command == 'ip':
        ip = sandbox.ip() if sandbox.running() else None
        if not ip:
            die(f'no IP for {name}')
        print(ip)
    elif args.command == 'exec':
        ip = require_running(sandbox, key, args.user)
        ssh_interactive(key, args.user, ip, args.operands)
    elif args.command == 'update':
        if not args.ollama:
            die('update requires --ollama/--url')
        ip = require_running(sandbox, key, args.user)
        set_ollama(key, args.user, ip, args.ollama)
        tunnel_note(args, key, ip, ollama_url(args.ollama))
    elif args.command == 'cp':
        if len(args.operands) != 2:
            parser.error('cp requires exactly two operands')
        ip = require_running(sandbox, key, args.user)
        do_cp(key, args.user, ip, *args.operands)
    elif args.command == 'claudeout':
        if not args.claude:
            parser.error('claudeout requires --claude')
        ip = require_running(sandbox, key, args.user)
        claude_out(key, args.user, ip, args.claude)


if __name__ == '__main__':
    main()
