# agent_sandbox_provision.ps1 - prepare a fresh agent sandbox. Runs INSIDE
# every Windows Sandbox started by `agent_sandbox.py create` / `start` (as
# SYSTEM via `wsb exec`, or from the .wsb LogonCommand on Windows 10), with:
#   C:\payload      the provisioned payload (read-only)
#   C:\mnt\project  the host project directory (read-only)
#   C:\mnt\run      the per-run exchange folder (read-write)
#   C:\mnt\mountN   --local / --mount folders
#
# Steps, mirroring agent_docker.py / agent_tart.sh:
#   1. toolchain to C:\tools, pythons registered (PEP 514), machine PATH
#   2. non-administrator sandbox user, sshd with the host's public key only
#   3. the payload home skeleton (pi and friends) into the user's home
#   4. the project copied to ~\<project> (never the mapped original), with
#      --skip patterns applied
#   5. --local / --mount folders linked into the home
#   6. pre-commit install when the project has the hook, ollama url set
#   7. IP published to C:\mnt\run\ip and a done marker written
#
# See agent_sandbox_common.ps1 for the shared machinery.

. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'agent_sandbox_common.ps1')

Start-ProvisionLog
Invoke-Checked 'reading arguments' { $script:runArgs = Read-RunArgs }
$runArgs = $script:runArgs
$project = Get-ArgOr $runArgs 'project' ''
$skips = @(Get-ArgOr $runArgs 'skips' @())
$mounts = @(Get-ArgOr $runArgs 'mounts' @())
$ollama = Get-ArgOr $runArgs 'ollama' ''

if (-not (Test-Path (Join-Path $script:Payload 'home'))) {
    Fail 'the payload has no home skeleton; run agent_sandbox.py provision'
}

$state = Initialize-Sandbox $runArgs $true
$user = $state.User
$homeDir = $state.Home

# --- 3. home skeleton -------------------------------------------------------
Invoke-Checked 'installing the home skeleton' {
    Copy-Tree (Join-Path $script:Payload 'home') $homeDir
}

# --- 4. project copy --------------------------------------------------------
Invoke-Checked "copying the project to $homeDir\$project" {
    $source = 'C:\mnt\project'
    if (-not $project) { throw 'no project name given' }
    if (-not (Test-Path $source)) { throw "mapped project $source not found" }
    $dest = Join-Path $homeDir $project
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    $xd = @()
    $xf = @()
    foreach ($pattern in $skips) {
        if ($pattern -match '[\\/]') {
            # A path pattern: anchored at the project root (agent_docker.py
            # semantics), applied to directories and files alike.
            $full = Join-Path $source ($pattern -replace '/', '\')
            $xd += $full
            $xf += $full
        } else {
            # A name pattern: matches directories and files anywhere.
            $xd += $pattern
            $xf += $pattern
        }
    }
    Copy-Tree $source $dest -ExcludeDirs $xd -ExcludeFiles $xf
    # The copy must be writable regardless of attributes on the source.
    & attrib -R (Join-Path $dest '*') /S /D 2>$null | Out-Null
}

# --- 5. --local / --mount ---------------------------------------------------
Invoke-Checked 'linking mounted folders' {
    foreach ($mount in $mounts) {
        $guest = $mount.guest
        $dst = [string]$mount.dst
        if (-not (Test-Path $guest)) {
            Log "WARNING: mapped folder $guest is missing; skipping"
            continue
        }
        if ($dst -match '^[A-Za-z]:') {
            $target = $dst
        } else {
            $dst = $dst -replace '^~[\\/]', '' -replace '/', '\'
            $target = Join-Path $homeDir $dst
        }
        $parent = Split-Path -Parent $target
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }
        try {
            New-Item -ItemType Junction -Path $target -Target $guest -ErrorAction Stop | Out-Null
        } catch {
            & cmd.exe /c mklink /D "$target" "$guest" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "could not link $target -> $guest" }
        }
        Log "linked $target -> $guest ($(if ($mount.readonly) { 'ro' } else { 'rw' }))"
    }
    $msvc = Join-Path $script:Payload 'scripts\install_msvc.py'
    if (Test-Path $msvc) {
        New-Item -ItemType Directory -Force -Path (Join-Path $homeDir '.local\bin') | Out-Null
        Copy-Item $msvc (Join-Path $homeDir '.local\bin\install_msvc.py') -Force
    }
    $vimrc = Join-Path $script:RunDir 'vimrc'
    if (Test-Path $vimrc) {
        Copy-Item $vimrc (Join-Path $homeDir '.vimrc') -Force
    }
}

# --- 6. project hooks and ollama --------------------------------------------
Invoke-Checked 'pre-commit hook' {
    if (Test-Path (Join-Path $homeDir "$project\.git\hooks\pre-commit")) {
        Invoke-Bash $user "cd ~/'$project' && pre-commit install" | Out-Null
    }
}

Invoke-Checked 'ollama url' {
    $arg = ''
    if ($ollama) { $arg = " '$ollama'" }
    $code = Invoke-Bash $user "~/.local/bin/set_ollama.sh$arg"
    if ($code -ne 0) { Log 'WARNING: set_ollama.sh failed' }
}

Invoke-Checked 'fixing ownership' { Set-HomeOwner $user $homeDir }

# --- 7. finish --------------------------------------------------------------
Invoke-Checked 'finishing' {
    Remove-BootstrapKey $user
    Remove-AdminRights $user
    Publish-IP | Out-Null
    # The run folder is the host's channel; keep the agent out of it (this
    # may not be honored on a mapped folder, in which case the agent can at
    # worst write files into ~\.agent_sandbox\<name>\run on the host).
    & icacls $script:RunDir /deny "$user`:(OI)(CI)F" 2>$null | Out-Null
}

Log "sandbox ready: $user@$(Get-SandboxIP) with the project at $homeDir\$project"
Write-Marker 'done'
try { Stop-Transcript | Out-Null } catch { }
