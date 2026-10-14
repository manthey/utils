# agent_sandbox_common.ps1 - functions shared by agent_sandbox_payload.ps1
# (payload assembly) and agent_sandbox_provision.ps1 (every sandbox). Both run
# INSIDE Windows Sandbox: as SYSTEM via `wsb exec` (cli backend) or as the
# elevated WDAGUtilityAccount via the .wsb LogonCommand (file backend).
#
# Fixed guest paths (agent_sandbox.py relies on these):
#   C:\payload   the host's payload folder (read-write while provisioning,
#                read-only otherwise); C:\payload\scripts holds these scripts
#   C:\mnt\run   a read-write per-run folder: args.json and authorized_keys
#                in, provision.log, ip and a done/failed marker out
#   C:\tools     the toolchain, copied from C:\payload\tools (or a junction
#                to it with copy_tools false)
#   C:\Users\<user>  the sandbox user's home; the payload's home skeleton is
#                provisioned here and copied back, so absolute paths in it
#                are the same during provision and create

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Off

$script:Payload = 'C:\payload'
$script:RunDir = 'C:\mnt\run'
$script:Tools = 'C:\tools'
$script:SshdDir = 'C:\Program Files\OpenSSH'
$script:SshData = 'C:\ProgramData\ssh'
$script:UserPassword = $null

function Log([string]$Message) {
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message)
}

function Start-ProvisionLog {
    New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $script:RunDir 'done'),
        (Join-Path $script:RunDir 'failed')
    Start-Transcript -Path (Join-Path $script:RunDir 'provision.log') -Force | Out-Null
}

function Read-RunArgs {
    $path = Join-Path $script:RunDir 'args.json'
    if (-not (Test-Path $path)) { throw "missing $path" }
    return Get-Content -Raw $path | ConvertFrom-Json
}

function Write-Marker([string]$Name, [string]$Message = '') {
    Set-Content -Path (Join-Path $script:RunDir $Name) -Value $Message
}

function Fail([string]$Message) {
    Log "FAILED: $Message"
    Write-Marker 'failed' $Message
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

function Invoke-Checked([string]$Description, [scriptblock]$Block) {
    Log $Description
    try {
        & $Block
    } catch {
        Fail "$Description`: $($_.Exception.Message)"
    }
}

function Get-ArgOr($RunArgs, [string]$Name, $Default) {
    if ($RunArgs.PSObject.Properties[$Name] -and $null -ne $RunArgs.$Name) { return $RunArgs.$Name }
    return $Default
}

# --- toolchain --------------------------------------------------------------

function Copy-Tree([string]$Source, [string]$Dest, [string[]]$ExcludeDirs = @(),
                   [string[]]$ExcludeFiles = @()) {
    # robocopy exit codes below 8 are success.
    $extra = @()
    foreach ($d in $ExcludeDirs) { $extra += @('/XD', $d) }
    foreach ($f in $ExcludeFiles) { $extra += @('/XF', $f) }
    & robocopy $Source $Dest /E /XJ /MT:16 /R:2 /W:2 /NFL /NDL /NJH /NJS /NP @extra | Out-Null
    $code = $LASTEXITCODE
    if ($code -ge 8) { throw "robocopy $Source -> $Dest failed with exit code $code" }
}

function Get-ToolPaths {
    $newest = Get-NewestPython
    $paths = @(
        (Join-Path $script:Tools 'git\cmd'),
        (Join-Path $script:Tools 'node'),
        (Join-Path $script:Tools 'bin'),
        $script:SshdDir
    )
    if ($newest) {
        $paths += @($newest, (Join-Path $newest 'Scripts'))
    }
    return $paths
}

function Get-PythonDirs {
    $root = Join-Path $script:Tools 'python'
    if (-not (Test-Path $root)) { return @() }
    return @(Get-ChildItem $root -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'python.exe') } |
        Sort-Object { [version]$_.Name } | ForEach-Object { $_.FullName })
}

function Get-NewestPython {
    $dirs = @(Get-PythonDirs)
    if ($dirs.Count -eq 0) { return $null }
    return $dirs[-1]
}

function Install-Tools([bool]$Copy, [string]$User) {
    $source = Join-Path $script:Payload 'tools'
    if (-not (Test-Path $source)) { throw "payload tools not found at $source; run provision" }
    if (Test-Path $script:Tools) { Remove-Item -Recurse -Force $script:Tools -ErrorAction SilentlyContinue }
    if ($Copy) {
        Log "copying $source -> $script:Tools"
        Copy-Tree $source $script:Tools
    } else {
        Log "linking $script:Tools -> $source"
        New-Item -ItemType Junction -Path $script:Tools -Target $source | Out-Null
    }
    # Users inherit read/execute from C:\; the tools are not writable by
    # the agent.
    Set-ToolchainEnvironment $User
}

function Set-ToolchainEnvironment([string]$User) {
    # Machine PATH: ssh sessions build their environment from the registry
    # at logon, so this is what the sandbox user and tox/pre-commit see.
    $homeDir = "C:\Users\$User"
    $paths = @((Join-Path $homeDir '.local\bin'), (Join-Path $homeDir '.npm-global')) + (Get-ToolPaths)
    $current = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts = @($current -split ';' | Where-Object { $_ -and ($paths -notcontains $_) })
    [Environment]::SetEnvironmentVariable('Path', (($paths + $parts) -join ';'), 'Machine')
    [Environment]::SetEnvironmentVariable('NPM_CONFIG_PREFIX', (Join-Path $homeDir '.npm-global'), 'Machine')
    [Environment]::SetEnvironmentVariable('PI_OFFLINE', '1', 'Machine')
    [Environment]::SetEnvironmentVariable('PI_SKIP_VERSION_CHECK', '1', 'Machine')
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', '1', 'Machine')
    $env:Path = (($paths + @($env:Path -split ';')) -join ';')
    $env:NPM_CONFIG_PREFIX = Join-Path $homeDir '.npm-global'
}

function Register-Pythons {
    # PEP 514 registration (HKLM, so every user sees it): this is how tox
    # and virtualenv find python3.10 ... python3.14 without a py launcher.
    foreach ($dir in Get-PythonDirs) {
        $minor = Split-Path -Leaf $dir
        $exe = Join-Path $dir 'python.exe'
        $full = (& $exe -c 'import platform; print(platform.python_version())' 2>$null)
        if (-not $full) { $full = $minor }
        $root = "HKLM:\SOFTWARE\Python\PythonCore\$minor"
        New-Item -Path $root -Force | Out-Null
        Set-ItemProperty -Path $root -Name 'DisplayName' -Value "Python $minor (64-bit)"
        Set-ItemProperty -Path $root -Name 'SupportUrl' -Value 'https://www.python.org/'
        Set-ItemProperty -Path $root -Name 'Version' -Value $full
        Set-ItemProperty -Path $root -Name 'SysVersion' -Value $minor
        Set-ItemProperty -Path $root -Name 'SysArchitecture' -Value '64bit'
        New-Item -Path "$root\InstallPath" -Force | Out-Null
        Set-ItemProperty -Path "$root\InstallPath" -Name '(default)' -Value "$dir\"
        Set-ItemProperty -Path "$root\InstallPath" -Name 'ExecutablePath' -Value $exe
        Set-ItemProperty -Path "$root\InstallPath" -Name 'WindowedExecutablePath' -Value (Join-Path $dir 'pythonw.exe')
        New-Item -Path "$root\PythonPath" -Force | Out-Null
        Set-ItemProperty -Path "$root\PythonPath" -Name '(default)' -Value "$dir\Lib;$dir\DLLs"
        Log "registered python $full at $dir"
    }
}

# --- sandbox user -----------------------------------------------------------

function New-RandomPassword {
    $chars = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $bytes = New-Object byte[] 28
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function New-SandboxUser([string]$User) {
    if ($User -ieq 'WDAGUtilityAccount') {
        Log 'using the built-in WDAGUtilityAccount'
        return
    }
    $script:UserPassword = New-RandomPassword
    & net user $User >$null 2>&1
    $exists = ($LASTEXITCODE -eq 0)
    if ($exists) {
        & net user $User $script:UserPassword | Out-Null
    } else {
        & net user $User $script:UserPassword /add /y /expires:never /passwordchg:no | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "net user $User /add failed" }
    }
    # cmd swallows the "already a member" / "not a member" chatter that
    # PowerShell would otherwise report as errors.
    & cmd.exe /c "net localgroup Users $User /add >nul 2>&1"
    & cmd.exe /c "net localgroup Administrators $User /delete >nul 2>&1"
    try { Set-LocalUser -Name $User -PasswordNeverExpires $true -ErrorAction Stop } catch { }
    Log "created non-administrator user $User"
}

function Remove-AdminRights([string]$User) {
    if ($User -ieq 'WDAGUtilityAccount') {
        & cmd.exe /c "net localgroup Administrators $User /delete >nul 2>&1"
        Log "removed $User from Administrators"
    }
}

function Confirm-UserProfile([string]$User) {
    # The profile directory must be created by Windows (a pre-made folder
    # would make Windows create C:\Users\<user>.<machine> instead). Logging
    # in over ssh does that and also proves sshd and the shell work; the
    # fallbacks use the password.
    $homeDir = "C:\Users\$User"
    if (Test-Path $homeDir) { return $homeDir }
    $bootstrap = Join-Path $env:TEMP 'agent_bootstrap_key'
    $known = Join-Path $env:TEMP 'agent_bootstrap_known_hosts'
    if (Test-Path "$bootstrap.pub") {
        Log 'creating the user profile via an ssh login'
        for ($i = 0; $i -lt 10 -and -not (Test-Path $homeDir); $i++) {
            & (Join-Path $script:SshdDir 'ssh.exe') -i $bootstrap -o StrictHostKeyChecking=no `
                -o "UserKnownHostsFile=$known" -o ConnectTimeout=5 -o BatchMode=yes `
                "$User@127.0.0.1" 'exit' 2>&1 | ForEach-Object { Log "ssh: $_" }
            if (-not (Test-Path $homeDir)) { Start-Sleep -Seconds 3 }
        }
    }
    if (-not (Test-Path $homeDir) -and $script:UserPassword) {
        Log 'creating the user profile via a scheduled task'
        & schtasks /Create /TN agent_profile /TR 'cmd.exe /c exit' /SC ONCE /ST 00:00 `
            /RU $User /RP $script:UserPassword /F | Out-Null
        & schtasks /Run /TN agent_profile | Out-Null
        for ($i = 0; $i -lt 20 -and -not (Test-Path $homeDir); $i++) { Start-Sleep -Seconds 2 }
        & schtasks /Delete /TN agent_profile /F 2>$null | Out-Null
    }
    if (-not (Test-Path $homeDir) -and $script:UserPassword) {
        Log 'creating the user profile via Start-Process -Credential'
        $secure = ConvertTo-SecureString $script:UserPassword -AsPlainText -Force
        $cred = New-Object System.Management.Automation.PSCredential ($User, $secure)
        try {
            Start-Process cmd.exe -ArgumentList '/c', 'exit' -Credential $cred -LoadUserProfile -Wait -WindowStyle Hidden
        } catch {
            Log "Start-Process: $($_.Exception.Message)"
        }
    }
    if (-not (Test-Path $homeDir)) { throw "the profile $homeDir was not created" }
    return $homeDir
}

function Set-HomeOwner([string]$User, [string]$HomeDir) {
    # Files copied in by SYSTEM must belong to the user: git refuses to work
    # in repositories owned by someone else, and the agent needs full
    # control of its own home.
    & icacls $HomeDir /setowner $User /T /C /Q | Out-Null
    & icacls $HomeDir /grant "$User`:(OI)(CI)F" /T /C /Q | Out-Null
}

# --- sshd -------------------------------------------------------------------

function Install-Sshd([string]$User) {
    $source = Join-Path $script:Tools 'openssh'
    if (-not (Test-Path (Join-Path $source 'sshd.exe'))) { throw "OpenSSH not found at $source" }
    if (-not (Test-Path (Join-Path $script:SshdDir 'sshd.exe'))) {
        New-Item -ItemType Directory -Force -Path $script:SshdDir | Out-Null
        Copy-Tree $source $script:SshdDir
    }
    Push-Location $script:SshdDir
    try {
        if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $script:SshdDir 'install-sshd.ps1') 2>&1 |
                ForEach-Object { Log "install-sshd: $_" }
        }
    } finally {
        Pop-Location
    }
    if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) { throw 'the sshd service was not registered' }
    New-Item -ItemType Directory -Force -Path $script:SshData, (Join-Path $script:SshData 'authorized_keys') | Out-Null
    $bash = Join-Path $script:Tools 'git\bin\bash.exe'
    @(
        'Port 22',
        'PubkeyAuthentication yes',
        'PasswordAuthentication no',
        'AuthorizedKeysFile C:/ProgramData/ssh/authorized_keys/%u',
        "AllowUsers $User",
        'Subsystem sftp sftp-server.exe'
    ) | Set-Content -Path (Join-Path $script:SshData 'sshd_config') -Encoding ASCII
    # Interactive and command sessions land in bash (Git for Windows), so
    # `exec` behaves like docker's `bash`; cmd and powershell remain usable.
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name 'DefaultShell' -Value $bash
    Set-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name 'DefaultShellCommandOption' -Value '-c'
    # Host keys are generated by the service itself on first start, with
    # the restrictive permissions sshd insists on.
    & netsh advfirewall firewall delete rule name='agent-sandbox-sshd' 2>$null | Out-Null
    & netsh advfirewall firewall add rule name='agent-sandbox-sshd' dir=in action=allow protocol=TCP localport=22 | Out-Null
    Set-Service sshd -StartupType Automatic
}

function Set-AuthorizedKeys([string]$User, [bool]$WithBootstrap) {
    $keys = @()
    $hostKeys = Join-Path $script:RunDir 'authorized_keys'
    if (Test-Path $hostKeys) { $keys += Get-Content $hostKeys | Where-Object { $_.Trim() } }
    if ($WithBootstrap) {
        $bootstrap = Join-Path $env:TEMP 'agent_bootstrap_key'
        if (-not (Test-Path "$bootstrap.pub")) {
            & (Join-Path $script:SshdDir 'ssh-keygen.exe') -q -t ed25519 -N '""' -f $bootstrap 2>&1 | Out-Null
        }
        if (Test-Path "$bootstrap.pub") { $keys += Get-Content "$bootstrap.pub" }
    }
    $file = Join-Path $script:SshData "authorized_keys\$User"
    $keys | Set-Content -Path $file -Encoding ASCII
    # sshd requires that only SYSTEM and Administrators can write the file.
    & icacls $file /inheritance:r /grant '*S-1-5-18:F' /grant '*S-1-5-32-544:F' | Out-Null
}

function Remove-BootstrapKey([string]$User) {
    $bootstrap = Join-Path $env:TEMP 'agent_bootstrap_key'
    if (-not (Test-Path "$bootstrap.pub")) { return }
    $pub = (Get-Content "$bootstrap.pub").Trim()
    $file = Join-Path $script:SshData "authorized_keys\$User"
    (Get-Content $file) | Where-Object { $_.Trim() -ne $pub } | Set-Content -Path $file -Encoding ASCII
    Remove-Item -Force -ErrorAction SilentlyContinue $bootstrap, "$bootstrap.pub"
}

function Start-Sshd {
    Restart-Service sshd -ErrorAction Stop
    for ($i = 0; $i -lt 30; $i++) {
        $listening = Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
        if ($listening) { return }
        Start-Sleep -Seconds 1
    }
    throw 'sshd did not start listening on port 22'
}

# --- networking -------------------------------------------------------------

function Get-SandboxIP {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' } |
        Sort-Object InterfaceIndex
    if ($addresses) { return $addresses[0].IPAddress }
    return $null
}

function Get-DefaultGateway {
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
    if ($route) { return $route.NextHop }
    return $null
}

function Add-HostGatewayEntry {
    # The analog of docker's --add-host host.docker.internal:<gateway>: the
    # sandbox's default gateway is the host.
    $gateway = Get-DefaultGateway
    if (-not $gateway) { Log 'WARNING: no default gateway found; host.docker.internal not added'; return }
    $hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
    $lines = @()
    if (Test-Path $hosts) { $lines = Get-Content $hosts | Where-Object { $_ -notmatch 'host\.docker\.internal' } }
    $lines += "$gateway`thost.docker.internal"
    $lines | Set-Content -Path $hosts -Encoding ASCII
    Log "host.docker.internal -> $gateway"
}

function Publish-IP {
    for ($i = 0; $i -lt 60; $i++) {
        $ip = Get-SandboxIP
        if ($ip) {
            Set-Content -Path (Join-Path $script:RunDir 'ip') -Value $ip
            Log "sandbox ip: $ip"
            return $ip
        }
        Start-Sleep -Seconds 2
    }
    Log 'WARNING: no IPv4 address found'
    return $null
}

# --- bash -------------------------------------------------------------------

function ConvertTo-MsysPath([string]$Path) {
    return '/' + $Path.Substring(0, 1).ToLower() + $Path.Substring(2).Replace('\', '/')
}

function Invoke-Bash([string]$User, [string]$Command) {
    # Run a bash command the way an ssh session for the user would see it:
    # HOME and the profile variables point at the user's home, PATH carries
    # the toolchain. Runs as the current (SYSTEM/admin) account; ownership
    # is fixed afterwards by Set-HomeOwner.
    $homeDir = "C:\Users\$User"
    $saved = @{}
    foreach ($name in 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP') {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        $env:HOME = $homeDir
        $env:USERPROFILE = $homeDir
        $env:APPDATA = Join-Path $homeDir 'AppData\Roaming'
        $env:LOCALAPPDATA = Join-Path $homeDir 'AppData\Local'
        $env:TEMP = Join-Path $homeDir 'AppData\Local\Temp'
        $env:TMP = $env:TEMP
        New-Item -ItemType Directory -Force -Path $env:APPDATA, $env:TEMP | Out-Null
        $bash = Join-Path $script:Tools 'git\bin\bash.exe'
        & $bash -lc $Command 2>&1 | ForEach-Object { Write-Host $_ }
        return $LASTEXITCODE
    } finally {
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
        }
    }
}

# --- common sequence --------------------------------------------------------

function Initialize-Sandbox($RunArgs, [bool]$Bootstrap, [switch]$SkipTools) {
    # The steps shared by provision and create, in order: toolchain, user,
    # sshd, profile. Returns the user name and home directory. With
    # -SkipTools, C:\tools was already built in place (payload assembly).
    $user = Get-ArgOr $RunArgs 'user' 'agent'
    $copy = [bool](Get-ArgOr $RunArgs 'copy_tools' $true)
    if ($SkipTools) {
        Invoke-Checked 'wiring the toolchain environment' { Set-ToolchainEnvironment $user }
    } else {
        Invoke-Checked 'installing the toolchain' { Install-Tools $copy $user }
    }
    Invoke-Checked 'registering pythons' { Register-Pythons }
    Invoke-Checked 'creating the sandbox user' { New-SandboxUser $user }
    Invoke-Checked 'installing sshd' { Install-Sshd $user }
    Invoke-Checked 'installing authorized keys' { Set-AuthorizedKeys $user $Bootstrap }
    Invoke-Checked 'starting sshd' { Start-Sshd }
    Invoke-Checked 'ensuring the user profile exists' { $script:HomeDir = Confirm-UserProfile $user }
    $homeDir = $script:HomeDir
    Invoke-Checked 'adding host.docker.internal' { Add-HostGatewayEntry }
    return @{ User = $user; Home = $homeDir }
}
