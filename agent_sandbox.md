# agent_sandbox.py - Windows Sandbox agent environments

`agent_sandbox.py` is the Windows counterpart of `agent_docker.py` (docker)
and `agent_tart.sh` (tart on macOS): a terminal command that produces an
isolated, disposable environment with pi installed, a copy of the project in
it, and nothing from the host's installed software. It uses Windows Sandbox,
which is built into Windows Pro/Enterprise and runs each sandbox as a
Hyper-V-isolated VM that is discarded when it stops.

## Host prerequisites

- Windows 11 (any recent build, 24H2 or later for the headless `wsb` CLI)
  or Windows 10 Pro 2004+ (window-based fallback, see below).
- Windows Sandbox enabled, then reboot:
  `Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All`
- The Windows OpenSSH client (`ssh`, `scp`, `ssh-keygen`; installed by
  default on current builds, otherwise Settings > Optional features).
- Python 3.10+ with pyyaml, or `uv run agent_sandbox.py ...` (the script has
  PEP 723 metadata).

## Reaching ollama on the host

pi in the sandbox is pointed at `http://127.0.0.1:11434`, the sandbox's own
loopback, and the host's ollama is forwarded there over ssh. Ollama stays
bound to loopback on the host (the default; "Expose Ollama to the network"
off), no firewall rule is needed because the connection is outbound from
the host, and nothing else on the network can reach ollama (it has no
authentication of its own).

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

`--ollama` keeps its `agent_docker.py` forms, adjusted to this: a bare port
(`--ollama 11435`) means the host's ollama on that port, forwarded the same
way (the printed command forwards that port); `host:port` and full urls are
used as given, with no tunnel. `update --ollama ...` changes a running
sandbox and prints the matching command.

The forwarded port listens only on the sandbox's own loopback. If the ssh
command exits (the host slept, the sandbox was recreated), pi loses ollama
until it is run again; pi's configuration does not need to change.

### Alternative: expose ollama and add a firewall rule

Without the tunnel, the sandbox has to reach the host through its default
gateway. It does so through a Hyper-V virtual adapter that Windows Firewall
treats as a Public network, where inbound connections are blocked unless a
rule allows them, so there is no way around a firewall rule on this route.
Turn on "Expose Ollama to the network" (or set `OLLAMA_HOST=0.0.0.0` and
restart ollama), confirm with `netstat -ano | findstr :11434` that it
listens on `0.0.0.0:11434`, and allow the sandbox subnet in through the
firewall, in an administrator PowerShell:

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

## Commands

```
agent_sandbox.py provision          build the payload (once; needs network)
agent_sandbox.py create [opts]      sandbox for the current directory, shell in
agent_sandbox.py start [opts]       same without the shell
agent_sandbox.py exec [cmd]         shell (or command) in the sandbox
agent_sandbox.py cp in:<path> <local>   / cp <local> in:<path>
agent_sandbox.py update --ollama 11435
agent_sandbox.py ip | list | stop
```

Options follow `agent_docker.py`: `--name`, `--num`, `--src`, `--ollama`,
`--local`, `--mount local:inside:ro`, `--skip`, `--gpu`, `--config`
(per-directory defaults in `agent_sandbox.yaml`, same scheme as
`agent_docker.yaml`), plus `--memory`, `--sshkey`, `--user` and
`--backend`.

`create`/`start` always destroy an existing sandbox of the same name and
rebuild it. `stop` destroys the sandbox. Results leave the sandbox only when
the host copies them out with `cp` (or through a read-write `--mount`).

## How it works

### provision

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
runs the shared `install_pi.sh` in that user's real home (`C:\Users\agent`)
so pi, its extensions and its configuration are installed at the same
absolute paths they will have later. The resulting `C:\tools` and home
directory are copied into the payload as `tools\` and `home\`, and the
sandbox is destroyed. `VERSIONS.txt` records what was fetched.

### create / start

1. The payload (read-only), the project directory (read-only), a small
   per-run exchange folder (`~\.agent_sandbox\<name>\run`, read-write), and
   any `--local`/`--mount` folders are mapped into a new sandbox.
2. `agent_sandbox_provision.ps1` runs inside as SYSTEM. It copies the tools
   to `C:\tools`, registers the pythons (PEP 514, which is how tox finds
   them), creates the user `agent` (member of Users only), installs sshd
   accepting only the host's public key, copies the home skeleton and the
   project (`~\<project>`, with `--skip` applied) into the user's home, links
   the mounted folders, runs `pre-commit install` if the project has the
   hook, points pi's ollama provider at the host, and writes the sandbox IP
   and a done marker into the run folder.
3. The host waits for the marker, then for ssh, and connects.

Provisioning progress is in `~\.agent_sandbox\<name>\run\provision.log`
(`-v` streams it). On failure the sandbox is left running for inspection;
with the cli backend `wsb connect --id <id>` opens its desktop.

### Inside the sandbox

ssh sessions land in bash (from Git for Windows) in the user's home, with
`pi`, `tox`, `pre-commit`, `python` (newest), `git`, `jq`, `rg`, `fd`, `uv`
and `vim` on PATH. `python3.12` etc. are not on PATH by name; use tox, or
`C:\tools\python\3.12\python.exe`. cmd and PowerShell are available as
usual. The project copy is at `~/<project>`, the mapped original at
`C:\mnt\project` (read-only). Mounted folders are junctions under the home
(`~/.vim`, `~/.vim_backup`, `~/.pi/agent/sessions` with `--local`;
`~/<inside>` for `--mount`).

The agent user is not an administrator: it cannot stop sshd, change the
firewall, or touch `C:\tools`. Everything else in the VM is fair game and is
discarded on `stop`.

### Backends

- `cli` (default when `wsb.exe` exists): headless; sandboxes are identified
  by id (`wsb list`); several can exist if the Windows build allows it
  (older builds allow one).
- `file` (Windows 10): a `.wsb` file is launched like a double-click, which
  opens the sandbox window; the provisioner runs from its LogonCommand as
  the elevated built-in account and reports through the run folder. The
  window is the VM: minimize it, but do not close it. Only one sandbox at a
  time.

## Layout on the host

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

## Things to know

- `--user WDAGUtilityAccount` uses the built-in account instead of creating
  one; it is removed from Administrators once provisioning is done.
- `--no-copy-tools` uses the tools directly from the read-only payload
  mapping instead of copying them to `C:\tools` (faster start, but depends
  on mapped folders being executable by the sandbox user).
- The sandbox memory defaults to 8192 MB (`--memory`); Windows Sandbox has
  no CPU count setting.
- Test services (redis, mongo, rabbitmq, memcached) and playwright browsers
  from the docker image are not provided.
