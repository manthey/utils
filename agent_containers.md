# Agent container scripts

This directory contains four scripts that build disposable agentic-programming
sandboxes (running pi) from the same mental model, one per platform:

| script            | platform              | isolation                     |
|-------------------|-----------------------|-------------------------------|
| `agent_docker.py` | linux, WSL, macOS     | docker containers             |
| `agent_tart.sh`   | macOS (Apple Silicon) | tart VMs                      |
| `agent_termux.sh` | Android (termux host) | nested termux under proot     |
| `agent_sandbox.py`| Windows               | Windows Sandbox (Hyper-V) VMs |

Each one produces an isolated, disposable environment with pi installed, a
copy of the project in it, and nothing from the host's installed software.
This file documents what they share and then the platform-specific details.

## Common behavior

### Commands

All four scripts use the same command set and semantics:

```
<agent> provision          build the base (image / VM / payload / rootfs);
                           run once, needs the network
<agent> create [opts]      sandbox for the current directory, shell in
<agent> start [opts]       same as create, but does not shell in
<agent> exec [cmd]         shell (or command) in the running sandbox
<agent> stop               destroy the sandbox
<agent> update --ollama .. apply --ollama to a running sandbox
<agent> list               list known sandboxes
<agent> cp in:<path> <local>   /  cp <local> in:<path>
<agent> claudeout --claude ..  save Claude Code state back to the host
```

agent_tart.sh and agent_sandbox.py also have:

```
<agent> ip                 print the sandbox IP (docker: with --ssh)
```

Sandboxes are ephemeral: `stop` destroys the sandbox, and `create`/`start`
always destroy an existing sandbox of the same name and build a fresh one
from the base. `create` is `start` followed by `exec`.

`cp` works like `docker cp`: exactly one of the two paths is prefixed `in:`,
directories are copied recursively, and relative guest paths are relative to
the guest user's home.

### Options

Common options (support varies slightly per platform):

- `--name` / `--num`: sandbox name and name suffix.
- `--src`: source path; defaults to the current working directory.
- `--ollama` / `--url`: replacement ollama base url for pi. A bare port
  means the host's ollama on that port (delivered per-platform, see below);
  `host:port` and full urls are used as given.
- `--local`: mount local utility directories (`.vimrc`, `.vim`,
  `.vim_backup`, pi sessions). Removes some isolation.
- `--mount local:inside:ro`: mount a folder into the sandbox; `inside` is
  relative to the guest home. Folders can also be mounted `:rw`, which removes
  some isolation.
- `--skip`: patterns of files or folders to exclude from the copy (comma
  separated, or repeat `--skip`).
- `--claude=DIR`: a local `.claude-persist` directory that contains
  `install_claude.sh`; `--claude=` with no value means the `claude`
  directory next to the script. Without `--claude`, Claude is not
  installed (`--no-claude` cancels one from the config file).
  `create`/`start` copy its contents to `~/.claude-persist` in the sandbox and run `install_claude.sh` there
  before shelling in; `claudeout` runs `install_claude.sh update` in the
  sandbox and copies `~/.claude-persist` back into `DIR`. This keeps
  Claude Code out of the provisioned images.
- `--sshkey`: which public key authorizes the host (default
  `~/.ssh/id_ed25519.pub`, then `id_ecdsa`, `id_rsa`; generated if none
  exists).
- `--config`: yaml file whose keys are working directories and whose values
  are arrays of default arguments (`agent_docker.yaml`, `agent_tart.yaml`,
  `agent_termux.yaml`, `agent_sandbox.yaml`). CLI arguments win; later
  scalars win; `--mount`/`--skip` accumulate from every source.
- `--verbose/-v`: more logging.

### Shared provisioning

`provision` builds the per-platform base once, with the network. In all
four flows pi and its configuration are installed by the shared
`install_pi.sh`, so the guest gets the same pi setup everywhere. The guest
tools are essentially the same on every platform: bash, git, node (pi's
runtime), several python versions with pip (newest also tox, pre-commit,
virtualenv), uv, jq, ripgrep, fd, vim.

### Data flow

The project is copied in at `create`/`start` (with `--skip` applied).
Results leave the sandbox only when the host copies them out with `cp` (or
through a read-write `--mount`). The guest user is unprivileged everywhere.

---

## agent_docker.py (linux, WSL, macOS)

- `provision` runs `docker build --force-rm -t manthey/agent -f
  agent.Dockerfile .`, so building the image uses the repo as build
  context.
- On a Windows host the script drives docker through `wsl docker`.
- `--ssh` adds a public key for the `ubuntu` user and exposes ssh on
  `--sshport` (default 2222), optionally finding a free port
  (`--sshfreeport`).
- `--fuse` passes options so fuse works in the container; `--gpu` enables
  GPU access; `--docker` mounts the docker socket.
- ollama: the container reaches the host directly as
  `host.docker.internal` (mapped to the host's gateway with
  `--add-host`); no tunnel is needed.
- Config file: `agent_docker.yaml`.

## agent_tart.sh (macOS, Apple Silicon)

- Host requirements: `brew install jq sshpass bash`, plus
  `brew tap openai/tools && brew install openai/tools/tart`.
- `provision` clones the base image
  (`ghcr.io/cirruslabs/macos-golden-gate-base:latest`) into a base VM
  (`agent_base`) and bootstraps the ssh key into it using the published
  admin/admin credentials; each `create`/`start` clones the base VM.
- VM sizing: `--cpus` (default 4), `--memory` (MB, default 8192),
  `--disk` (GB, default 150).
- Logs in `~/.agent_tart/logs`; ssh state in `~/.agent_tart/ssh`.
- ollama: the VM reaches the host over the network like docker does.
- Config file: `agent_tart.yaml`.

## agent_termux.sh (Android, termux host)

- Host requirements (termux packages): `proot`, `unzip`, `openssh`, `jq`,
  `git`. The bootstrap zip is cached under `~/.agent_termux`.
- Each sandbox is a nested termux: a termux bootstrap rootfs extracted
  under `~/.agent_termux/<name>/root` and run through `proot -r`. The
  guest is real bionic termux (its own `$PREFIX`, `pkg`, termux-packaged
  node) and cannot reach any other host path - writes land in the sandbox
  or fail.
- A running sandbox is a proot-wrapped sshd on a localhost-only port
  (starting at `--sshport`/`TERMUX_SSHPORT`, default 2222); `exec` and
  `cp` go over ssh like the other agents.
- ollama: the guest shares the device's network namespace, so its default
  ollama url (`localhost:11434`) reaches the same ollama as the host
  termux; no tunnel or firewall work is needed.
- Mounts are read-write (proot has no read-only enforcement); without an
  explicit `--mount` of shared storage the guest has no sdcard access.
- The base and each sandbox are each hundreds of MB; the script refuses to
  start when the home filesystem cannot hold them.
- Config file: `agent_termux.yaml`.

## agent_sandbox.py (Windows, Windows Sandbox)

Windows Sandbox is built into Windows Pro/Enterprise and runs each sandbox
as a Hyper-V-isolated VM that is discarded when it stops (no WSL, no
docker).

### Host prerequisites

- Windows 11 24H2 or later (headless `wsb` CLI backend, `cli`) or
  Windows 10 Pro 2004+ (`.wsb` file backend, `file`).
- Windows Sandbox enabled, then reboot:

  ```powershell
  Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All
  ```

- The Windows OpenSSH client (`ssh`, `scp`, `ssh-keygen`; installed by
  default on current builds, otherwise Settings > Optional features).
- Python 3.10+ with pyyaml, or `uv run agent_sandbox.py ...` (the script
  has PEP 723 metadata).

### Backends

- `cli` (default when `wsb.exe` exists): headless; sandboxes are
  identified by id (`wsb list`); several can exist if the Windows build
  allows it (older builds allow one).
- `file` (Windows 10): a `.wsb` file is launched like a double-click,
  which opens the sandbox window; the provisioner runs from its
  LogonCommand as the elevated built-in account and reports through the
  run folder. The window is the VM: minimize it, but do not close it.
  Only one sandbox at a time.

### Windows Sandbox: reaching ollama

pi in the sandbox is pointed at `http://127.0.0.1:11434`, the sandbox's
own loopback, and the host's ollama is forwarded there over ssh. Ollama
stays bound to loopback on the host (the default; "Expose Ollama to the
network" off), no firewall rule is needed because the connection is
outbound from the host, and nothing else on the network can reach ollama
(it has no authentication of its own).

`create` and `start` print the tunnel command with the sandbox's address
filled in, for example:

```
ssh -N -R 11434:127.0.0.1:11434 -o UserKnownHostsFile=C:\Users\me\.agent_sandbox\known_hosts -o StrictHostKeyChecking=no -o ServerAliveInterval=30 agent@172.19.189.186
```

Keep it running in a host terminal while you work. The known_hosts options
matter: sandbox IPs get reused with new host keys, and with your normal
known_hosts file ssh would refuse the next sandbox at a reused address.
(`-i <key>` is added when `--sshkey` chose a key ssh would not try by
default.)

A bare port (`--ollama 11435`) means the host's ollama on that port,
forwarded the same way (the printed command forwards that port); `host:port`
and full urls are used as given, with no tunnel. `update --ollama ...`
changes a running sandbox and prints the matching command.

The forwarded port listens only on the sandbox's own loopback. If the ssh
command exits (the host slept, the sandbox was recreated), pi loses ollama
until it is run again; pi's configuration does not need to change.

Alternative: expose ollama and add a firewall rule. Without the tunnel,
the sandbox has to reach the host through its default gateway, a Hyper-V
virtual adapter that Windows Firewall treats as a Public network, where
inbound connections are blocked unless a rule allows them. Turn on
"Expose Ollama to the network" (or set `OLLAMA_HOST=0.0.0.0` and restart
ollama), confirm with `netstat -ano | findstr :11434` that it listens on
`0.0.0.0:11434`, and allow the sandbox subnet in through the firewall, in
an administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Ollama from Windows Sandbox" -Direction Inbound `
  -Protocol TCP -LocalPort 11434 -RemoteAddress 172.16.0.0/12 -Profile Any -Action Allow
```

`172.16.0.0/12` is the private range Windows draws NAT subnets from; the
sandbox's specific subnet can change across reboots, so a narrower rule
breaks. Note that ollama then also listens on your LAN interfaces, so the
firewall is the only thing protecting it. If you ever dismissed a Windows
Firewall prompt for ollama, Windows created Block rules for `ollama.exe`
that override any allow rule; check with
`Get-NetFirewallRule -DisplayName *ollama*`. Point pi at the host directly
with `--ollama host.docker.internal:11434` (on `create`, `update`, or in
`agent_sandbox.yaml`); the provisioner maps `host.docker.internal` to the
sandbox's default gateway, which is the host.

To check either route from inside the sandbox:

```
grep baseUrl ~/.pi/agent/models.json
curl -m 5 http://127.0.0.1:11434/api/version     # tunnel
curl -m 5 http://host.docker.internal:11434/api/version   # firewall rule
```

A timeout means the connection is being dropped (firewall); "connection
refused" means nothing is listening at that address (ollama bound to
loopback, or the tunnel is not running).

### How it works

#### provision

Runs once. The script starts a sandbox with `~\.agent_sandbox\payload`
mapped read-write and runs `agent_sandbox_payload.ps1` inside it, so the
host never executes any of the downloaded tools. The sandbox fetches:

- PortableGit (bash, git, vim, ssh client)
- node 22 (pi's runtime)
- python 3.10-3.14 as python.org NuGet builds (`--pythons` to change),
  with pip; the newest also gets tox, pre-commit and virtualenv
- uv, jq, ripgrep, fd
- Win32-OpenSSH (the sandbox's sshd)

It then creates the non-administrator user, installs and starts sshd, and
runs the shared `install_pi.sh` in that user's real home
(`C:\Users\agent`) so pi, its extensions and its configuration are
installed at the same absolute paths they will have later. The resulting
`C:\tools` and home directory are copied into the payload as `tools\` and
`home\`, and the sandbox is destroyed. `VERSIONS.txt` records what was
fetched.

#### create / start

1. The payload (read-only), the project directory (read-only), a small
   per-run exchange folder (`~\.agent_sandbox\<name>\run`, read-write),
   and any `--local`/`--mount` folders are mapped into a new sandbox.
2. `agent_sandbox_provision.ps1` runs inside as SYSTEM. It copies the
   tools to `C:\tools`, registers the pythons (PEP 514, which is how tox
   finds them), creates the user `agent` (member of Users only), installs
   sshd accepting only the host's public key, copies the home skeleton
   and the project (`~\<project>`, with `--skip` applied) into the user's
   home, links the mounted folders, runs `pre-commit install` if the
   project has the hook, points pi's ollama provider at the host, and
   writes the sandbox IP and a done marker into the run folder.
3. The host waits for the marker, then for ssh, and connects.

Provisioning progress is in `~\.agent_sandbox\<name>\run\provision.log`
(`-v` streams it). On failure the sandbox is left running for inspection;
with the cli backend `wsb connect --id <id>` opens its desktop.

#### Inside the sandbox

ssh sessions land in bash (from Git for Windows) in the user's home, with
`pi`, `tox`, `pre-commit`, `python` (newest), `git`, `jq`, `rg`, `fd`,
`uv` and `vim` on PATH. `python3.12` etc. are not on PATH by name; use
tox, or `C:\tools\python\3.12\python.exe`. cmd and PowerShell are
available as usual. The project copy is at `~/<project>`, the mapped
original at `C:\mnt\project` (read-only). Mounted folders are junctions
under the home (`~/.vim`, `~/.vim_backup`, `~/.pi/agent/sessions` with
`--local`; `~/<inside>` for `--mount`).

The agent user is not an administrator: it cannot stop sshd, change the
firewall, or touch `C:\tools`. Everything else in the VM is fair game and
is discarded on `stop`.

### Windows-specific options

- `--memory` (MB, default 8192); Windows Sandbox has no CPU count setting.
- `--backend auto|cli|file` (default: `cli` when `wsb.exe` is on PATH).
- `--user` (default `agent`); `--user WDAGUtilityAccount` uses the
  built-in account instead of creating one; it is removed from
  Administrators once provisioning is done.
- `--no-copy-tools` uses the tools directly from the read-only payload
  mapping instead of copying them to `C:\tools` (faster start, but depends
  on mapped folders being executable by the sandbox user).
- `--gpu` enables the virtual GPU in the sandbox.

### Host layout

```
~\.agent_sandbox\
  payload\            tools\, home\, scripts\ (copied from the repo on every
                      run so edits to the .ps1/.sh files take effect), VERSIONS.txt
  known_hosts
  <name>\
    id                sandbox id (cli) or marker (file)
    run\              args.json, authorized_keys in; provision.log, ip, done out
    <name>.wsb        (file backend only)
```

### Things to know

- Test services (redis, mongo, rabbitmq, memcached) and playwright
  browsers from the docker image are not provided in the Windows payload.
