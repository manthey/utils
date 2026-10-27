# agent_sandbox_payload.ps1 - assemble the agent sandbox payload. Runs INSIDE
# a Windows Sandbox started by `agent_sandbox.py provision`, with the host's
# payload folder mapped read-write at C:\payload and its download cache at
# C:\mnt\cache. This is the equivalent of building agent.Dockerfile or
# tart's base VM: nothing is installed on the host; the sandbox downloads and
# arranges everything, then the result is a plain folder the host maps
# read-only into every later sandbox.
#
# Result (C:\payload):
#   tools\git\        PortableGit (bash, vim, ssh client)
#   tools\node\       portable node 22 (pi's runtime)
#   tools\python\3.x\ python.org builds (NuGet packages) with pip; the
#                     default (default.txt, else the newest) also has tox,
#                     pre-commit and virtualenv and is first on PATH
#   tools\bin\        uv.exe, uvx.exe, jq.exe, rg.exe, fd.exe,
#                     psmux.exe (exposed as tmux), pmux.exe
#   tools\openssh\    Win32-OpenSSH (sshd for the sandbox)
#   home\             the sandbox user's home after install_pi.sh ran in it
#                     (pi, its extensions and config, helper scripts, .bashrc)
#   VERSIONS.txt
#
# See agent_sandbox_common.ps1 for the shared machinery.

. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'agent_sandbox_common.ps1')

Start-ProvisionLog
$runArgs = $null
Invoke-Checked 'reading arguments' { $script:runArgs = Read-RunArgs }
$runArgs = $script:runArgs
$pythons = @(Get-ArgOr $runArgs 'pythons' @('3.12'))
$defaultPython = Get-ArgOr $runArgs 'default_python' ''
$pipTools = @(Get-ArgOr $runArgs 'pip_tools' @())
$user = Get-ArgOr $runArgs 'user' 'agent'
$useCache = [bool](Get-ArgOr $runArgs 'cache' $true)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$dl = 'C:\dl'
# Downloads are cached by url, so a newer release is still fetched; entries
# this run did not use are pruned once the payload is complete.
$cacheDir = 'C:\mnt\cache'
$cacheUsed = @{}
New-Item -ItemType Directory -Force -Path $dl, $script:Tools, (Join-Path $script:Tools 'bin') | Out-Null
$versions = @()

function Get-Json([string]$Url) {
    return Invoke-RestMethod -Uri $Url -Headers @{ 'User-Agent' = 'agent_sandbox' }
}

function Get-File([string]$Url, [string]$Name) {
    $dest = Join-Path $dl $Name
    $hash = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Url))
    $key = (-join ($hash[0..7] | ForEach-Object { $_.ToString('x2') })) + "-$Name"
    $cached = Join-Path $cacheDir $key
    $script:cacheUsed[$key] = $true
    if ($useCache -and (Test-Path $cached)) {
        Log "using cached $Url"
    } else {
        Log "downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile "$cached.partial" -UseBasicParsing -Headers @{ 'User-Agent' = 'agent_sandbox' }
        Move-Item -Force "$cached.partial" $cached
    }
    Copy-Item $cached $dest -Force
    return $dest
}

function Get-GitHubAsset([string]$Repo, [string]$Pattern) {
    # Download url of the asset matching the regex in the latest stable
    # release.  This reads the release web pages rather than the REST api,
    # which allows only 60 unauthenticated requests an hour per ip address.
    $headers = @{ 'User-Agent' = 'agent_sandbox' }
    $resp = Invoke-WebRequest -Uri "https://github.com/$Repo/releases/latest" -UseBasicParsing -Headers $headers
    # Windows PowerShell and PowerShell 7 expose the redirected url differently.
    $final = if ($resp.BaseResponse.ResponseUri) { $resp.BaseResponse.ResponseUri } else { $resp.BaseResponse.RequestMessage.RequestUri }
    if ($final.AbsoluteUri -notmatch '/releases/tag/(.+)$') { throw "no latest release for $Repo" }
    $tag = $Matches[1]
    $page = Invoke-WebRequest -Uri "https://github.com/$Repo/releases/expanded_assets/$tag" -UseBasicParsing -Headers $headers
    $linkPattern = 'href="(/' + [regex]::Escape($Repo) + '/releases/download/[^"]+)"'
    foreach ($link in [regex]::Matches($page.Content, $linkPattern)) {
        $path = $link.Groups[1].Value
        if ([uri]::UnescapeDataString($path.Split('/')[-1]) -match $Pattern) {
            return @{ Url = "https://github.com$path"; Tag = [uri]::UnescapeDataString($tag) }
        }
    }
    throw "no asset matching $Pattern in $Repo release $tag"
}

function Expand-Zip([string]$Zip, [string]$Dest) {
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    # Expand-Archive insists on a .zip extension.
    if (-not $Zip.EndsWith('.zip')) {
        Copy-Item $Zip "$Zip.zip" -Force
        $Zip = "$Zip.zip"
    }
    Expand-Archive -Path $Zip -DestinationPath $Dest -Force
}

function Find-File([string]$Root, [string]$Name) {
    $found = Get-ChildItem $Root -Recurse -Filter $Name | Select-Object -First 1
    if (-not $found) { throw "$Name not found under $Root" }
    return $found.FullName
}

# --- PortableGit
Invoke-Checked 'PortableGit' {
    $asset = Get-GitHubAsset 'git-for-windows/git' '^PortableGit-.*-64-bit\.7z\.exe$'
    $file = Get-File $asset.Url 'PortableGit.7z.exe'
    $dest = Join-Path $script:Tools 'git'
    $proc = Start-Process -FilePath $file -ArgumentList @("-o`"$dest`"", '-y') -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) { throw "PortableGit extraction failed with $($proc.ExitCode)" }
    if (-not (Test-Path (Join-Path $dest 'bin\bash.exe'))) { throw 'bash.exe missing after extraction' }
    $script:versions += "git: $($asset.Tag)"
}

# --- node 22
Invoke-Checked 'node' {
    $index = Get-Json 'https://nodejs.org/dist/index.json'
    $entry = $index | Where-Object { $_.version -like 'v22.*' } | Select-Object -First 1
    if (-not $entry) { throw 'no node v22 release found' }
    $name = "node-$($entry.version)-win-x64"
    $file = Get-File "https://nodejs.org/dist/$($entry.version)/$name.zip" "$name.zip"
    Expand-Zip $file (Join-Path $dl 'node')
    $dest = Join-Path $script:Tools 'node'
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Move-Item (Join-Path $dl "node\$name") $dest
    $script:versions += "node: $($entry.version)"
}

# --- single-file tools
Invoke-Checked 'uv, jq, ripgrep, fd, hadolint, psmux' {
    $bin = Join-Path $script:Tools 'bin'
    $asset = Get-GitHubAsset 'astral-sh/uv' '^uv-x86_64-pc-windows-msvc\.zip$'
    Expand-Zip (Get-File $asset.Url 'uv.zip') (Join-Path $dl 'uv')
    Copy-Item (Find-File (Join-Path $dl 'uv') 'uv.exe') $bin -Force
    Copy-Item (Find-File (Join-Path $dl 'uv') 'uvx.exe') $bin -Force
    $script:versions += "uv: $($asset.Tag)"

    $asset = Get-GitHubAsset 'jqlang/jq' '^jq-windows-amd64\.exe$'
    Copy-Item (Get-File $asset.Url 'jq.exe') (Join-Path $bin 'jq.exe') -Force
    $script:versions += "jq: $($asset.Tag)"

    $asset = Get-GitHubAsset 'BurntSushi/ripgrep' '^ripgrep-.*-x86_64-pc-windows-msvc\.zip$'
    Expand-Zip (Get-File $asset.Url 'ripgrep.zip') (Join-Path $dl 'rg')
    Copy-Item (Find-File (Join-Path $dl 'rg') 'rg.exe') $bin -Force
    $script:versions += "ripgrep: $($asset.Tag)"

    $asset = Get-GitHubAsset 'sharkdp/fd' '^fd-v.*-x86_64-pc-windows-msvc\.zip$'
    Expand-Zip (Get-File $asset.Url 'fd.zip') (Join-Path $dl 'fd')
    Copy-Item (Find-File (Join-Path $dl 'fd') 'fd.exe') $bin -Force
    $script:versions += "fd: $($asset.Tag)"

    $asset = Get-GitHubAsset 'hadolint/hadolint' '^hadolint-Windows-x86_64\.exe$'
    Copy-Item (Get-File $asset.Url 'hadolint.exe') (Join-Path $bin 'hadolint.exe') -Force
    $script:versions += "hadolint: $($asset.Tag)"

    # psmux: native tmux for Windows (Rust).  The release zip already ships
    # psmux, pmux and tmux; when a release only ships psmux.exe, expose it
    # as tmux by copying so both commands always work.
    $asset = Get-GitHubAsset 'psmux/psmux' '^psmux-v.*-windows-x64\.zip$'
    Expand-Zip (Get-File $asset.Url 'psmux.zip') (Join-Path $dl 'psmux')
    foreach ($name in 'psmux.exe', 'pmux.exe', 'tmux.exe') {
        $source = Find-File (Join-Path $dl 'psmux') $name
        Copy-Item $source $bin -Force
    }
    if (-not (Test-Path (Join-Path $bin 'tmux.exe'))) {
        Copy-Item (Join-Path $bin 'psmux.exe') (Join-Path $bin 'tmux.exe') -Force
    }
    $script:versions += "psmux: $($asset.Tag)"
}

# --- Win32-OpenSSH
Invoke-Checked 'OpenSSH' {
    $asset = Get-GitHubAsset 'PowerShell/Win32-OpenSSH' '^OpenSSH-Win64\.zip$'
    Expand-Zip (Get-File $asset.Url 'openssh.zip') (Join-Path $dl 'openssh')
    $sshd = Find-File (Join-Path $dl 'openssh') 'sshd.exe'
    $dest = Join-Path $script:Tools 'openssh'
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Move-Item (Split-Path -Parent $sshd) $dest
    $script:versions += "openssh: $($asset.Tag)"
}

# --- pythons (python.org NuGet packages)
Invoke-Checked "pythons $($pythons -join ', ')" {
    $index = Get-Json 'https://api.nuget.org/v3-flatcontainer/python/index.json'
    $root = Join-Path $script:Tools 'python'
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    foreach ($minor in $pythons) {
        $candidates = @($index.versions | Where-Object { $_ -like "$minor.*" -and $_ -notmatch '-' })
        if ($candidates.Count -eq 0) {
            Log "WARNING: no stable python $minor on nuget.org; skipping"
            continue
        }
        $full = ($candidates | Sort-Object { [version]$_ } | Select-Object -Last 1)
        $file = Get-File "https://api.nuget.org/v3-flatcontainer/python/$full/python.$full.nupkg" "python-$full.nupkg"
        Expand-Zip $file (Join-Path $dl "python-$full")
        $dest = Join-Path $root $minor
        if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
        Move-Item (Join-Path $dl "python-$full\tools") $dest
        $exe = Join-Path $dest 'python.exe'
        & $exe -m ensurepip --upgrade 2>&1 | Out-Null
        & $exe -m pip install --quiet --no-warn-script-location --upgrade pip setuptools wheel 2>&1 | ForEach-Object { Write-Host $_ }
        & $exe -m compileall -q (Join-Path $dest 'Lib') 2>&1 | Out-Null
        $script:versions += "python: $full"
    }
    if ($defaultPython) {
        Set-Content -Path (Join-Path $root 'default.txt') -Value $defaultPython -Encoding ascii
    }
    $default = Get-DefaultPython
    if (-not $default) { throw 'no python was installed' }
    if ($defaultPython -and (Split-Path -Leaf $default) -ne $defaultPython) {
        Log "WARNING: default python $defaultPython is not installed; using $default"
    }
    if ($pipTools.Count -gt 0) {
        Log "installing $($pipTools -join ', ') into $default"
        & (Join-Path $default 'python.exe') -m pip install --quiet --no-warn-script-location @pipTools 2>&1 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw 'pip install of the tools failed' }
    }
}

# --- user, sshd and profile (same path as every sandbox)
$state = Initialize-Sandbox $runArgs $true -SkipTools
$homeDir = $state.Home

# --- pi, via the shared install_pi.sh
Invoke-Checked 'install_pi.sh' {
    $script = ConvertTo-MsysPath (Join-Path $script:Payload 'scripts\install_pi.sh')
    $code = Invoke-Bash $user "bash '$script'"
    if ($code -ne 0) { throw "install_pi.sh exited with $code" }
}

Invoke-Checked 'home directory extras' {
    $scripts = Join-Path $script:Payload 'scripts'
    if (Test-Path (Join-Path $scripts '.vimrc')) {
        Copy-Item (Join-Path $scripts '.vimrc') (Join-Path $homeDir '.vimrc') -Force
    }
    if (Test-Path (Join-Path $scripts 'cover.py')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $homeDir '.local\bin') | Out-Null
        Copy-Item (Join-Path $scripts 'cover.py') (Join-Path $homeDir '.local\bin\cover.py') -Force
    }
    $bashrc = Join-Path $homeDir '.bashrc'
    if (-not (Test-Path $bashrc) -or -not (Select-String -Path $bashrc -SimpleMatch 'NPM_CONFIG_PREFIX' -Quiet)) {
        Add-Content -Path $bashrc -Value @(
            '',
            '# agent sandbox: npm global installs go to the home directory',
            'export NPM_CONFIG_PREFIX="$HOME/.npm-global"',
            'export PATH="$NPM_CONFIG_PREFIX:$PATH"',
            '# Bracketed paste does not survive the sshd ConPTY layer: a right-click',
            '# paste leaves readline waiting for an end marker that never arrives.',
            "bind 'set enable-bracketed-paste off' 2>/dev/null",
            "bind 'set bell-style none' 2>/dev/null"
        )
    }
    Invoke-Bash $user 'git config --global user.name Container && git config --global user.email container@example.com' | Out-Null
    Set-HomeOwner $user $homeDir
    Remove-BootstrapKey $user
}

# --- copy the results into the payload
Invoke-Checked 'copying tools and home into the payload' {
    Copy-Tree $script:Tools (Join-Path $script:Payload 'tools')
    $dest = Join-Path $script:Payload 'home'
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    # Registry hives and caches must not travel: Windows creates fresh hives
    # with each profile, and the caches are large and disposable.
    Copy-Tree $homeDir $dest -ExcludeDirs @(
        (Join-Path $homeDir 'AppData\Local\Temp'),
        (Join-Path $homeDir 'AppData\Local\npm-cache'),
        (Join-Path $homeDir 'AppData\Local\pip'),
        (Join-Path $homeDir 'AppData\Local\Microsoft'),
        (Join-Path $homeDir 'AppData\Roaming\Microsoft')
    ) -ExcludeFiles @('ntuser*', 'NTUSER*', 'UsrClass*')
    $script:versions | Set-Content -Path (Join-Path $script:Payload 'VERSIONS.txt')
}

try {
    Get-ChildItem $cacheDir -File | Where-Object { -not $cacheUsed.ContainsKey($_.Name) } | Remove-Item -Force
} catch {
    Log "WARNING: pruning the download cache failed: $($_.Exception.Message)"
}

Log 'payload complete'
Write-Marker 'done'
try { Stop-Transcript | Out-Null } catch { }
