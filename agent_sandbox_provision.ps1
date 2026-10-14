# agent_sandbox_provision.ps1 - runs inside a Windows agent sandbox at logon
# (via the .wsb LogonCommand). It is carried inside the read-only payload
# folder assembled by agent_sandbox_payload.ps1.
#
# What it does, in order:
#   1. wires the payload tools (git, node, uv, jq, rg, fd) onto PATH
#   2. copies the read-only mapped project into a local C:\work\<name> tree
#      (the copy is the isolation: the agent only ever edits this copy)
#   3. links/copies the --local support folders (.vimrc, .vim, .vim_backup,
#      pi sessions) into the sandbox user profile
#   4. installs and starts sshd so the HOST can copy files to and from the
#      sandbox on demand
#      (scp from outside; the sandbox has no other egress channel)
#   5. runs install_pi.sh unmodified under PortableGit bash
#   6. points pi's ollama provider at the sandbox host gateway
#   7. leaves an interactive shell in C:\work\<project> (skipped with -NoShell,
#      for `start`: the sandbox keeps running; work with it via exec/cp)
#
# Everything except the deliberately mapped folders and the ssh key vanishes
# when the sandbox closes.

param(
    [Parameter(Mandatory = $true)][string]$ProjectName,
    [string]$Skips = '',
    [string]$Ollama = '',
    [switch]$NoShell
)

$ErrorActionPreference = 'Continue'
$desktop = [Environment]::GetFolderPath('Desktop')
# Prefer the read-write mapped artifacts folder, so the host can read the
# transcript and the detected IP without opening the sandbox window.
$artifacts = Join-Path $desktop 'artifacts'
if (Test-Path $artifacts) {
    $transcript = Join-Path $artifacts 'provision.log'
} else {
    # Fall back to the user's temp directory rather than C:\ root.
    $transcript = Join-Path $env:TEMP 'agent_sandbox_provision.log'
}
Start-Transcript -Path $transcript -Force | Out-Null
Write-Host "Provisioning log: $transcript"
try {
    $homeDir = [Environment]::GetFolderPath('UserProfile')
    $payload = Join-Path $desktop 'agent_payload'
    $workRoot = 'C:\work'
    $work = Join-Path $workRoot $ProjectName

    if (-not (Test-Path (Join-Path $payload 'install_pi.sh'))) {
        Write-Warning "Payload not found at $payload; aborting provisioning"
        exit 1
    }

    # --- 1. PATH wiring ------------------------------------------------------
    $payloadPath = @(
        (Join-Path $payload 'git\cmd'),
        (Join-Path $payload 'node'),
        (Join-Path $payload 'bin'),
        (Join-Path $homeDir '.npm-global'),
        (Join-Path $homeDir '.local\bin')
    ) -join ';'
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $newPath = "$payloadPath;$userPath"
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    # The process PATH is machine PATH + user PATH; prepend the payload
    # without dropping the system directories.
    $env:Path = "$payloadPath;$env:Path"

    # --- 2. Copy the project: the agent's writable world --------------------
    $src = Join-Path $desktop $ProjectName
    if (-not (Test-Path $src)) {
        Write-Warning "Mapped project $src not found; aborting provisioning"
        exit 1
    }
    New-Item -ItemType Directory -Force -Path $workRoot | Out-Null
    $skipArgs = @()
    foreach ($pattern in ($Skips -split ',') | Where-Object { $_ }) {
        # Apply every pattern to both dirs and files; robocopy treats a
        # non-matching list entry as a no-op, and patterns like ".venv" may
        # legitimately be either.
        $skipArgs += @('/XD', $pattern)
        $skipArgs += @('/XF', $pattern)
    }
    if (Test-Path $work) { Remove-Item -Recurse -Force $work }
    Write-Host "Copying $src -> $work ..."
    # robocopy exit codes 0-7 are success-ish; 8+ are failures.
    & robocopy $src $work /E @skipArgs /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        Write-Warning "robocopy failed with exit code $LASTEXITCODE"
    }
    $LASTEXITCODE = 0

    # --- 3. --local support folders -----------------------------------------
    # agent_sandbox.py maps, when --local was given:
    #   Desktop\local\        (read-only; contains .vimrc and .vim)
    #   Desktop\vim_backup    (read-write)
    #   Desktop\sessions      (read-write; pi's ~/.pi/agent/sessions)
    $localDir = Join-Path $desktop 'local'
    if (Test-Path $localDir) {
        $vimrc = Join-Path $localDir '.vimrc'
        if (Test-Path $vimrc) { Copy-Item $vimrc (Join-Path $homeDir '.vimrc') -Force }
        $vim = Join-Path $localDir '.vim'
        if (Test-Path $vim) {
            & cmd /c mklink /J "$homeDir\.vim" $vim | Out-Null
        }
    }
    foreach ($pair in @(
        @('vim_backup', '.vim_backup'),
        @('sessions', '.pi\agent\sessions')
    )) {
        $mapped = Join-Path $desktop $pair[0]
        if (Test-Path $mapped) {
            $target = Join-Path $homeDir $pair[1]
            New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
            if (-not (Test-Path $target)) {
                & cmd /c mklink /J $target $mapped | Out-Null
            }
        }
    }

    # --- 4. sshd: the host-initiated export channel --------------------------
    Write-Host 'Installing sshd ...'
    $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' |
        Where-Object { $_.Name -like 'OpenSSH.Server~~~~0.0.1.0*' } |
        Select-Object -First 1
    if ($capability -and $capability.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name $capability.Name | Out-Null
    }
    $authKeys = 'C:\ProgramData\ssh\administrators_authorized_keys'
    $pubkey = Join-Path $payload 'ssh\authorized_keys'
    if (Test-Path $pubkey) {
        New-Item -ItemType Directory -Force -Path (Split-Path $authKeys) | Out-Null
        Copy-Item $pubkey $authKeys -Force
        # sshd requires a restrictive ACL on this file.
        & icacls $authKeys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
    }
    & netsh advfirewall firewall add rule name='agent-sandbox-sshd' dir=in `
        action=allow protocol=TCP localport=22 | Out-Null
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '169.254*' -and
            $_.InterfaceAlias -notmatch 'Loopback' } |
        Select-Object -Last 1).IPAddress
    Write-Host "sshd is running; sandbox IP: $ip"
    if (Test-Path $artifacts) {
        Set-Content -Path (Join-Path $artifacts 'ip') -Value $ip
    }
    $gateway = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' |
        Sort-Object RouteMetric | Select-Object -First 1).NextHop

    # --- 5. install_pi.sh, unmodified ---------------------------------------
    Write-Host 'Running install_pi.sh ...'
    $msysPayload = '/' + $payload.Substring(0, 1).ToLower() +
        $payload.Substring(2).Replace('\', '/')
    $bash = Join-Path $payload 'git\bin\bash.exe'
    & $bash -lc "bash '$msysPayload/install_pi.sh'"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "install_pi.sh failed with exit code $LASTEXITCODE"
        Stop-Transcript | Out-Null
        exit 1
    }

    # --- 6. ollama: point pi at the sandbox host -----------------------------
    # Map host.docker.internal to the default-route gateway (the host) in
    # the guest hosts file. This is the analog of docker's --add-host
    # host.docker.internal:<gateway> argument in agent_docker.py, and it
    # makes bare-port --ollama values resolve here exactly like they do in
    # a docker container.
    $hostsFile = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
    if (-not (Select-String -Path $hostsFile -SimpleMatch 'host.docker.internal' -Quiet)) {
        Add-Content -Path $hostsFile -Value "$gateway`thost.docker.internal"
    }
    $msysHome = '/' + $homeDir.Substring(0, 1).ToLower() +
        $homeDir.Substring(2).Replace('\', '/')
    if ($Ollama) {
        # Expand the --ollama value exactly like agent_docker.py: a bare
        # value (a port or hostname) goes to host.docker.internal; a value
        # without a scheme gets http:// added.
        if ($Ollama -notmatch '[/:]') { $Ollama = "host.docker.internal:$Ollama" }
        if ($Ollama -notmatch '/') { $Ollama = "http://$Ollama" }
        $Ollama = $Ollama.TrimEnd('/')
        $ollamaArg = " '$Ollama'"
    } else {
        $ollamaArg = ''
    }
    & $bash -lc "'$msysHome/.local/bin/set_ollama.sh'$ollamaArg"

    # --- 7. Hand off to an interactive shell in the copied tree --------------
    Write-Host ''
    Write-Host "Provisioning complete. Agent tree: $work"
    if ($NoShell) {
        Write-Host 'NoShell requested; leaving the sandbox running without a shell.'
        return
    }
    Set-Location $work
    powershell -NoLogo -NoExit
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
