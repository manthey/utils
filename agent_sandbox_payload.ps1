# agent_sandbox_payload.ps1 - assemble the portable toolchain payload used by
# the Windows agent sandbox (agent_sandbox.py + agent_sandbox_provision.ps1).
#
# Run this on the Windows host:
#   powershell -ExecutionPolicy Bypass -File agent_sandbox_payload.ps1
#
# The payload is rebuilt from scratch on every run, so results are
# reproducible; it is a directory of portable, self-contained tools that is
# mapped read-only into every sandbox:
#   agent_payload\git\          PortableGit (provides bash for install_pi.sh)
#   agent_payload\node\         portable node 22 (pi's runtime)
#   agent_payload\bin\          uv, jq, rg, fd
#   agent_payload\ssh\          host's authorized_keys for sandbox sshd
#   agent_payload\install_pi.sh agent_sandbox_provision.ps1
#
# Options:
#   -PayloadDir <dir>  payload location (default ~\.agent_sandbox\agent_payload)
#   -RepoDir <dir>     directory holding install_pi.sh and the provisioner
#                      (default: the directory containing this script)

param(
    [string]$PayloadDir = '',
    [string]$RepoDir = ''
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $PayloadDir) {
    $PayloadDir = Join-Path $HOME '.agent_sandbox\agent_payload'
}
if (-not $RepoDir) {
    $RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$tempRoot = Join-Path $PayloadDir '_downloads'

function Get-LatestAsset([string]$Repo, [string]$Pattern) {
    # Return the browser download url for the latest release asset matching
    # the given regex in the given github repository.
    Write-Host "Querying latest $Repo release ..."
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match $Pattern } |
        Select-Object -First 1
    if (-not $asset) {
        throw "No asset matching '$Pattern' in the latest $Repo release"
    }
    return $asset.browser_download_url
}

function Get-Url([string]$Url, [string]$Dest) {
    if (Test-Path $Dest) { Remove-Item -Force $Dest }
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
    return $Dest
}

function Expand-ZipInto([string]$Zip, [string]$Dest) {
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Expand-Archive -Path $Zip -DestinationPath $Dest -Force
}

function Copy-ExeFromZip([string]$ZipDir, [string]$Exe, [string]$Dest) {
    # Copy an exe out of an extracted release zip. Some zips keep the exe at
    # the archive root (uv); others wrap it in a versioned directory
    # (ripgrep, fd), so search the whole tree.
    $found = Get-ChildItem $ZipDir -Recurse -Filter $Exe |
        Select-Object -First 1
    if (-not $found) {
        throw "$Exe not found in $ZipDir"
    }
    Copy-Item $found.FullName $Dest -Force
}

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
}

Ensure-Dir $PayloadDir
Ensure-Dir (Join-Path $PayloadDir 'bin')
Ensure-Dir (Join-Path $PayloadDir 'ssh')
Ensure-Dir $tempRoot
$versions = @()

# --- PortableGit: provides bash.exe so install_pi.sh runs unmodified. --------
$gitDir = Join-Path $PayloadDir 'git'
$url = Get-LatestAsset 'git-for-windows/git' 'PortableGit-.*-64-bit\.7z\.exe$'
$file = Get-Url $url (Join-Path $tempRoot 'PortableGit.7z.exe')
Write-Host "Extracting PortableGit ..."
$extract = Start-Process -FilePath $file -ArgumentList @(
    '-o', "`"$gitDir`"", '-y') -Wait -PassThru -NoNewWindow
if ($extract.ExitCode -ne 0) {
    throw "PortableGit self-extractor failed with exit code $($extract.ExitCode)"
}
$versions += "git: $((Get-Item $file).LastWriteTime.ToString('yyyy-MM-dd'))"

# --- Portable node 22: pi's runtime. -----------------------------------------
$nodeDir = Join-Path $PayloadDir 'node'
$index = Invoke-WebRequest -Uri 'https://nodejs.org/dist/latest-v22.x/' `
    -UseBasicParsing
$match = [regex]::Match($index.Content, 'node-v22\.\d+\.\d+-win-x64\.zip')
if (-not $match.Success) {
    throw 'Could not find a node v22 win-x64 zip on nodejs.org'
}
$name = $match.Value
$url = "https://nodejs.org/dist/latest-v22.x/$name"
$file = Get-Url $url (Join-Path $tempRoot $name)
Write-Host "Extracting $name ..."
$zipDir = Join-Path $tempRoot 'nodezip'
if (Test-Path $zipDir) { Remove-Item -Recurse -Force $zipDir }
Expand-ZipInto $file $zipDir
if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
$inner = Get-ChildItem $zipDir -Directory | Select-Object -First 1
Move-Item $inner.FullName $nodeDir
$nodeVersion = $name -replace '^node-(v[\d.]+)-win-x64\.zip$', '$1'
$versions += "node: $nodeVersion"

# --- Single-file tools into payload\bin. -------------------------------------
$binDir = Join-Path $PayloadDir 'bin'

# uv (and uvx)
$url = Get-LatestAsset 'astral-sh/uv' 'uv-x86_64-pc-windows-msvc\.zip$'
$file = Get-Url $url (Join-Path $tempRoot 'uv.zip')
$zipDir = Join-Path $tempRoot 'uvzip'
Expand-ZipInto $file $zipDir
Copy-Item (Join-Path $zipDir 'uv.exe') $binDir -Force
Copy-Item (Join-Path $zipDir 'uvx.exe') $binDir -Force
$versions += 'uv: latest'

# jq
$url = Get-LatestAsset 'jqlang/jq' 'jq-windows-amd64\.exe$'
$file = Get-Url $url (Join-Path $tempRoot 'jq.exe')
Copy-Item $file (Join-Path $binDir 'jq.exe') -Force
$versions += 'jq: latest'

# ripgrep (rg.exe)
$url = Get-LatestAsset 'BurntSushi/ripgrep' 'ripgrep-.*-x86_64-pc-windows-msvc\.zip$'
$file = Get-Url $url (Join-Path $tempRoot 'ripgrep.zip')
$zipDir = Join-Path $tempRoot 'rgzip'
Expand-ZipInto $file $zipDir
Copy-ExeFromZip $zipDir 'rg.exe' $binDir
$versions += 'ripgrep: latest'

# fd
$url = Get-LatestAsset 'sharkdp/fd' 'fd-v.*-x86_64-pc-windows-msvc\.zip$'
$file = Get-Url $url (Join-Path $tempRoot 'fd.zip')
$zipDir = Join-Path $tempRoot 'fdzip'
Expand-ZipInto $file $zipDir
Copy-ExeFromZip $zipDir 'fd.exe' $binDir
$versions += 'fd: latest'

# --- ssh keypair: the host's key that the sandbox sshd will accept. ----------
$sshDir = Join-Path $HOME '.agent_sandbox\ssh'
Ensure-Dir $sshDir
$key = Join-Path $sshDir 'id_ed25519'
if (-not (Test-Path $key)) {
    Write-Host 'Generating sandbox ssh key ...'
    & ssh-keygen -t ed25519 -N '""' -f $key -C 'agent-sandbox' 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'ssh-keygen failed; is the Windows OpenSSH client installed?'
    }
}
Copy-Item "$key.pub" (Join-Path $PayloadDir 'ssh\authorized_keys') -Force

# --- Shared scripts from the repo. -------------------------------------------
Copy-Item (Join-Path $RepoDir 'install_pi.sh') $PayloadDir -Force
Copy-Item (Join-Path $RepoDir 'agent_sandbox_provision.ps1') $PayloadDir -Force

$versions | Set-Content (Join-Path $PayloadDir 'VERSIONS.txt')
Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
Write-Host ''
Write-Host "Payload ready at $PayloadDir"
Get-Content (Join-Path $PayloadDir 'VERSIONS.txt')
