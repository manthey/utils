# PLAN.md - Windows-native agentic coding sandbox

Status and design notes for the Windows Sandbox based agent sandbox, so work
can resume after a context reset. The live implementation is
`agent_sandbox.py` plus its two PowerShell helpers; this file records the
decisions, the current state, and what remains.

## Background and goal

Two existing flows build ephemeral agentic-programming sandboxes (running pi)
for a project directory:

- `agent_docker.py` + `agent.Dockerfile`: docker containers on Linux (or
  docker-in-WSL on Windows). `stop` destroys the container; sandboxes are
  ephemeral by design. The project is tar-copied into the container so the
  agent edits a copy, never the host tree.
- `agent_tart.sh` + `agent_tart_provision.sh`: tart VMs on Apple Silicon
  macOS. Same model: provision a base, clone it, ssh in, sync the project.

Goal: a conceptually equivalent flow on Windows, native (no WSL, no docker).
The agent may do horrible things; the remedy is teardown and restart. Windows
Pro is available on both target machines (64 GB and 128 GB RAM).

## Decisions made

1. **Backend: Windows Sandbox** (`WindowsSandbox.exe`, `.wsb` config files).
   Ephemeral by design (everything is discarded on close, matching
   `docker stop` on a `--rm` container), built into Pro, genuine security
   boundary. Windows-native: the sandbox itself runs Windows.
2. **One instance at a time** is a hard limit of Windows Sandbox. For parallel
   sandboxes the plan is a Hyper-V backend (see Next steps); Windows Sandbox
   remains the quick single-run path.
3. **Copy-in isolation is deliberate.** The project directory is mapped
   READ-ONLY into the sandbox and copied to `C:\work\<project>` at logon. The
   agent only ever edits the copy. There is deliberately NO `--out` folder:
   copies to and from the sandbox are host-initiated `cp` transfers, never
   agent-initiated pushes.
4. **Transfer channel: sshd in the sandbox.** The provisioner installs OpenSSH
   Server, installs the host's public key into
   `C:\ProgramData\ssh\administrators_authorized_keys`, and starts sshd. The
   host does `agent_sandbox.py cp <local path> in:<sandbox path>` or
   `agent_sandbox.py cp in:<sandbox path> <local path>` (scp, docker-cp style;
   exactly one endpoint is prefixed `in:`). The
   agent has no write channel to the host except the `--local` mounts.
5. **`--local` mirrors agent_docker.py's mounts:**
   - `~/.vimrc` and `~/.vim` read-only (single file staged into a mapped
     folder since .wsb maps folders only; the provisioner copies/junctions
     them into the sandbox profile)
   - `~/.vim_backup` read-write (junction to the mapped folder)
   - `~/.pi/agent/sessions` read-write (junction) so session history survives
     and accumulates on the host, like the docker mount.
   Extra `--mount local:ro|rw` entries appear at `Desktop\<basename>` in the
   sandbox (Windows Sandbox does not allow choosing the guest-side path).
6. **No code mimicry of install_pi.sh.** `install_pi.sh` itself runs inside
   the sandbox under PortableGit bash with a small platform branch:
   - `uname` MINGW/MSYS/CYGWIN sets `PI_SKIP_NVM=1` (nvm-sh does not support
     Windows), requiring node on PATH (portable node from the payload).
   - `NPM_CONFIG_PREFIX=$HOME/.npm-global` because the portable node
     directory is mapped read-only; npm shims (`pi.cmd`) live in the prefix.
   - The `$BINDIR/pi` bash wrapper is skipped on Windows (npm shims do that
     job); the final self-test uses `pi --help` from PATH.
   - `set_ollama.sh` gained a MINGW arm: default gateway via
     `route print -4` (locale-independent), which in the sandbox is the host.
     The provisioner also adds a `host.docker.internal` hosts-file entry
     pointing at that gateway (the analog of docker's `--add-host`), so
     `--ollama` values and `update` resolve exactly as they do in docker.
   Everything else (npm install of pi, all config heredocs, the `pi install`
   extension list, `set_hf.sh`, profile block) is shared verbatim across
   docker/tart/sandbox. Drift surface: ~25 lines in one shared script.
7. **Dropped features vs agent_docker.py:** docker-socket mount, fuse (and
   dokany), mini-swe-agent, pyenv/python-version machinery (Dockerfile
   territory), ssh port exposure (`--ssh`): the sandbox window plus our ssh
   channel replace it; the tar-copy is replaced by the robocopy at logon.
8. **Sandbox sizing:** `--cpus` (default 4) and `--memory` MB (default 8192)
   in the `.wsb`. The Hyper-V backend will use dynamic memory instead so
   parallel sandboxes consume memory generically (min ~1 GB, max generous;
   Windows guests balloon via built-in integration services).

## Architecture / data flow

Files in this repo:

- `agent_sandbox.py` - host-side driver (run on Windows; pyyaml PEP 723
  script). Commands: `provision`, `create`, `start`, `list`, `stop`, `ip`,
  `exec`, `update`, `cp`. Config merge from `agent_sandbox.yaml` (same scheme as
  `agent_docker.yaml`: keys are working directories, values are argument
  arrays, `all` applies everywhere).
- `agent_sandbox_payload.ps1` - run once (`agent_sandbox.py provision`);
  assembles `~\.agent_sandbox\agent_payload\`:
  - `git\` PortableGit (provides bash for install_pi.sh)
  - `node\` portable node 22 (pi's runtime)
  - `bin\` uv.exe, uvx.exe, jq.exe, rg.exe, fd.exe
  - `ssh\authorized_keys` (host keypair lives at `~\.agent_sandbox\ssh\`)
  - `install_pi.sh`, `agent_sandbox_provision.ps1`, `VERSIONS.txt`
- `agent_sandbox_provision.ps1` - runs inside the sandbox at logon (via the
  `.wsb` LogonCommand). Transcript at `%TEMP%\agent_sandbox_provision.log`.
  Steps:
  1. PATH wiring (payload dirs prepended to user PATH; process PATH keeps
     the machine PATH)
  2. robocopy of the read-only mapped project to `C:\work\<name>`, with
     `--skip` patterns applied to both dirs and files
  3. `--local` support folders (copies `.vimrc`, junctions `.vim`,
     `.vim_backup`, `.pi\agent\sessions`)
  4. sshd install (Windows capability), host key ACL via icacls, firewall
     rule, service start; prints sandbox IP and default-route gateway
  5. `install_pi.sh` via PortableGit bash
  6. hosts entry mapping `host.docker.internal` to the default-route gateway
     (the analog of docker's `--add-host`), then `set_ollama.sh` with
     `--ollama` expanded exactly like agent_docker.py expands it
  7. interactive shell left in `C:\work\<name>`
- `agent_sandbox.yaml` - per-project default arguments.
- `install_pi.sh` - shared with docker/tart (modified as described above).

`create` flow (and `start`, which is identical except that `-NoShell` is
appended to the logon command so provisioning ends without the interactive
shell; the sandbox stays running to be used via `exec` and `cp`): chdir to
`--src`, merge config, sandbox name
`agent_<project>[_<num>]`, refuse to run if `WindowsSandbox.exe` is already
running, stage `--local` files, build the `.wsb` (VGpu/Networking enabled,
audio/printer/clipboard disabled, payload + project + local mappings), snapshot
the ARP table, launch via `cmd /c start`, then poll for the sandbox IP (new
ARP entries whose port 22 answers) and store it in `~\.agent_sandbox\<name>\ip`.

Mapped folders always land at `C:\Users\WDAGUtilityAccount\Desktop\<host
basename>`; the payload folder is named `agent_payload` so its sandbox path is
predictable. ssh runs as `WDAGUtilityAccount` with the host's ed25519 key.

## How to test (on Windows)

1. One-time: `Enable-WindowsOptionalFeature -Online -FeatureName
   "Containers-DisposableClientVM" -All`, reboot.
2. `python agent_sandbox.py provision` (network needed once).
3. `python agent_sandbox.py start` in a project; watch
   `%TEMP%\agent_sandbox_provision.log` in the sandbox. The sandbox window
   is the VM's display: it must stay open (minimize it), and closing it
   destroys the sandbox. Work from the host terminal: `exec`, `cp`,
   `update`.
   `python agent_sandbox.py start` is the same but leaves no interactive
   shell; enter with `python agent_sandbox.py exec`.
4. `python agent_sandbox.py cp in:C:/work/<proj>/somefile.txt .`
5. Check `~\.pi\agent\sessions` on the host fills from sandbox use.
6. Close the sandbox (or `python agent_sandbox.py stop`), `create` again:
   must come up pristine.
7. Try launching a second `.wsb` while one runs to see whether this Windows
   build still enforces the one-instance limit.

## Known risk points (untested yet; everything was written on Linux)

1. `Add-WindowsCapability -Online` for OpenSSH Server inside Windows Sandbox
   - believed to work, least-certain call; if it fails, cp is unavailable
     but everything else works. Offline fallback: carry the capability files
     in the payload.
2. npm shim resolution in Git Bash with the custom prefix (the `pi` ->
   `pi.cmd`/sh-shim -> portable node chain); install_pi.sh's self-test will
   show this immediately.
3. ARP-diff IP discovery (new ARP entries with port 22 open). Fallback:
   `agent_sandbox.py ip` rescans; last resort would be a one-file handshake
   mapping (deliberately not built).
4. First `create` needs network for the OpenSSH capability, pi's npm install,
   and the `pi install` extension list.
5. `.wsb` details: LogonCommand quoting, `<ReadOnly>` semantics, whether the
   sandbox user profile is `C:\Users\WDAGUtilityAccount` on all builds.

## Next steps

1. Test the Windows Sandbox path on a Windows Pro machine (sequence above)
   and fix what breaks.
2. Hyper-V backend (`--backend hv`) for parallel sandboxes and dynamic
   memory:
   - one-time base: build a Windows VM, sysprep, keep its VHDX as a
     read-only parent (analogous to `docker build` / tart `provision`)
   - per sandbox: `New-VHD -Differencing` (seconds) + `New-VM` with dynamic
     memory (min ~1 GB, max 48/96 GB), boot, sshd (same provision script)
   - ephemeral reset: `Stop-VM` + `Remove-VM` + delete the child VHDX;
     optional `Checkpoint-VM` as a "keep this one" escape hatch
   - IP is directly available via `Get-VM | Get-VMNetworkAdapter`
   - the provision script and payload are shared unchanged
3. Consider a short `agent_sandbox.md` user doc in the style of
   `agent_tart.md` once the flow is proven.

## Terminology

- `provision` (command) = assemble the payload once (tart's `provision`
  builds the base VM; here the OS-managed sandbox image replaces the base
  VM, so provisioning is the payload assembly).
- `payload` (noun) = the read-only mapped toolchain folder.
- `create` = launch a fresh sandbox for a project (docker `create`/tart
  `create` analog; `create` is `start` plus the interactive shell, matching
  docker's "create is the same as start followed by exec"). There is no
  `start`/`stop` resume pair because Windows Sandbox cannot persist;
  `stop` = destroy.
