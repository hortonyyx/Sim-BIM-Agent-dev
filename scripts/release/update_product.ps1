<#
Update one fixed installation from a release-manifest.json distribution.
Keep one compressed previous payload on D:; Git owns older versions.
Rollback also recovers an interrupted update. No models or sign-in are used.
#>
[CmdletBinding()]
param(
    [ValidateSet('plan', 'update', 'rollback', 'status')][string]$Action = 'plan',
    [string]$Release,
    [string]$InstallRoot = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sim BIM Agent'),
    [string]$BackupRoot = 'D:\Sim-BIM-Agent\archive\product-backup',
    [string]$WorkRoot = 'D:\Sim-BIM-Agent\cache\product-update',
    [string]$VerificationRoot,
    [switch]$Offline
)
$ErrorActionPreference = 'Stop'
$script:Verifier = Join-Path $PSScriptRoot 'verify_product.py'

function Write-Record($Path, $Value) {
    $temporary = $Path + '.tmp'
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 30), (New-Object Text.UTF8Encoding $false))
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $Path) }
}

function Full-Path([string]$Path, [string]$CheckedRoot = '') {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full.Length -le [IO.Path]::GetPathRoot($full).Length) { throw 'A filesystem root is not a product directory' }
    $cursor = $full
    while ($cursor -and $cursor -ne $CheckedRoot) {
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Reparse points are not permitted: $cursor"
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Within([string]$Path, [string]$Root) {
    return $Path.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Payload-Path([string]$Root, [string]$Relative) {
    if ($Relative -match '[\\:*?"<>|]' -or $Relative.StartsWith('/') -or $Relative -match '(^|/)(\.|\.\.|\.venv|\.git)(/|$)' -or $Relative -match '(^|/)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|/|$)' -or $Relative -match '[. ](/|$)' -or $Relative.Contains('//')) {
        throw "Unsafe payload path: $Relative"
    }
    $path = Full-Path (Join-Path $Root $Relative) $Root
    if (-not (Within $path $Root)) { throw 'Payload path escapes installation' }
    return $path
}

function Digest($Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Parse-Manifest($Text) {
    $m = $Text | ConvertFrom-Json
    if ($m.schema_version -ne 'sim-bim-release.v1' -or $m.source_commit -notmatch '^[a-f0-9]{40}$' -or -not $m.runtime_version -or -not $m.domain_version) { throw 'Invalid release identity' }
    $files = @{}
    foreach ($item in $m.files.PSObject.Properties) {
        $null = Payload-Path $script:Install $item.Name
        if ($item.Name -eq 'release-manifest.json' -or $files.ContainsKey($item.Name) -or $item.Value -notmatch '^[a-f0-9]{64}$') { throw 'Invalid manifest member' }
        $files[$item.Name] = $item.Value
    }
    foreach ($required in @('sim_bim.py', 'Start.cmd', 'uv.lock', 'pyproject.toml', '.python-version', 'src/agent_runtime/agent_versions.json')) {
        if (-not $files.ContainsKey($required)) { throw "Incomplete product: $required" }
    }
    return @{ Raw = $m; Files = $files }
}

function Read-Release($Root) {
    $Root = Full-Path $Root
    if (Test-Path -LiteralPath (Join-Path $Root '.git')) { throw 'Use a built release, not a Git checkout' }
    $m = Parse-Manifest ([IO.File]::ReadAllText((Payload-Path $Root 'release-manifest.json')))
    foreach ($name in $m.Files.Keys) {
        $path = Payload-Path $Root $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Digest $path) -ne $m.Files[$name]) { throw "Payload missing or modified: $name" }
    }
    $m.Root = $Root
    $m.Hash = Digest (Join-Path $Root 'release-manifest.json')
    return $m
}

function Copy-Payload($Source, $Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($name in @($Source.Files.Keys) + @('release-manifest.json')) {
        $target = Payload-Path $Destination $name
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath (Payload-Path $Source.Root $name) -Destination $target
    }
    return Read-Release $Destination
}

function Pack-Payload($Source, $Zip) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::Open($Zip, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($name in @($Source.Files.Keys) + @('release-manifest.json')) {
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Payload-Path $Source.Root $name), $name, [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $archive.Dispose() }
}

function Unpack-Payload($Zip, $Destination) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $entry = $archive.GetEntry('release-manifest.json')
        if (-not $entry) { throw 'Backup has no manifest' }
        $reader = New-Object IO.StreamReader($entry.Open())
        try { $m = Parse-Manifest ($reader.ReadToEnd()) } finally { $reader.Dispose() }
        $seen = @{}
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            $null = Payload-Path $Destination $name
            if ($seen.ContainsKey($name) -or ($name -ne 'release-manifest.json' -and -not $m.Files.ContainsKey($name))) { throw 'Unexpected backup member' }
            $seen[$name] = $true
        }
        if ($seen.Count -ne $m.Files.Count + 1) { throw 'Incomplete backup' }
        foreach ($entry in $archive.Entries) {
            $target = Payload-Path $Destination $entry.FullName
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
        }
    } finally { $archive.Dispose() }
    return Read-Release $Destination
}

function Assert-Stopped {
    $active = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -match '^(pythonw?|sim-bim-agent|uv)\.exe$' -and
        (($_.ExecutablePath -and (Within $_.ExecutablePath $script:Install)) -or ($_.CommandLine -and $_.CommandLine.Contains($script:Install)))
    })
    if ($active.Count) { throw 'Product is running; finish or stop its run before maintenance' }
}

function Assert-Target($Before, $After, [switch]$Recovering) {
    foreach ($name in $After.Files.Keys) {
        $target = Payload-Path $script:Install $name
        if (Test-Path -LiteralPath $target -PathType Container) { throw "Directory conflicts with payload: $name" }
        if (-not $Before.Files.ContainsKey($name) -and (Test-Path -LiteralPath $target) -and -not ($Recovering -and (Digest $target) -eq $After.Files[$name])) { throw "Unmanaged file would be overwritten: $name" }
        $parent = Split-Path -Parent $target
        while ($parent -ne $script:Install) {
            if (Test-Path -LiteralPath $parent -PathType Leaf) { throw "File conflicts with payload directory: $parent" }
            $parent = Split-Path -Parent $parent
        }
    }
    if (-not $Recovering) {
        foreach ($name in @($Before.Files.Keys) + @($After.Files.Keys) + @('release-manifest.json')) {
            if (Test-Path -LiteralPath ((Payload-Path $script:Install $name) + '.sim-bim-update.tmp')) { throw 'Existing update temporary file requires inspection' }
        }
    }
    if ($Recovering) {
        foreach ($name in @($Before.Files.Keys) + @($After.Files.Keys) | Select-Object -Unique) {
            $path = Payload-Path $script:Install $name
            if ((Test-Path -LiteralPath $path) -and (Digest $path) -notin @($Before.Files[$name], $After.Files[$name])) { throw "File changed outside update; preserve and inspect it: $name" }
        }
    }
}

function Clear-UpdateTemps($Before, $After) {
    # Preflight established that these exact reserved names did not exist before the pending update.
    foreach ($name in @($Before.Files.Keys) + @($After.Files.Keys) + @('release-manifest.json') | Select-Object -Unique) {
        $temporary = Full-Path ((Payload-Path $script:Install $name) + '.sim-bim-update.tmp')
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Set-Payload($From, $To) {
    # Each replacement is atomic on the install volume. Only manifest-owned files are removed.
    foreach ($name in @($To.Files.Keys) + @('release-manifest.json')) {
        $target = Payload-Path $script:Install $name
        if ($name -ne 'release-manifest.json' -and $From.Files[$name] -eq $To.Files[$name] -and (Test-Path -LiteralPath $target -PathType Leaf)) { continue }
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        $temporary = $target + '.sim-bim-update.tmp'
        if (Test-Path -LiteralPath $temporary) { throw "Unexpected temporary file: $temporary" }
        Copy-Item -LiteralPath (Payload-Path $To.Root $name) -Destination $temporary
        try {
            if ((Digest $temporary) -ne (Digest (Payload-Path $To.Root $name))) { throw 'Copy verification failed' }
            if (Test-Path -LiteralPath $target) { [IO.File]::Replace($temporary, $target, [NullString]::Value) }
            else { [IO.File]::Move($temporary, $target) }
        } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
    }
    foreach ($name in $From.Files.Keys) {
        if ($To.Files.ContainsKey($name)) { continue }
        $target = Payload-Path $script:Install $name
        if (Test-Path -LiteralPath $target) {
            if ((Digest $target) -ne $From.Files[$name]) { throw "Obsolete file modified: $name" }
            Remove-Item -LiteralPath $target
        }
        $parent = Split-Path -Parent $target
        while ((Within $parent $script:Install) -and (Test-Path -LiteralPath $parent) -and @(Get-ChildItem -LiteralPath $parent -Force).Count -eq 0) {
            Remove-Item -LiteralPath $parent
            $parent = Split-Path -Parent $parent
        }
    }
    $null = Read-Release $script:Install
}

function Sync-Dependencies {
    $previous = $env:UV_PROJECT_ENVIRONMENT
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $script:Install '.venv'
        $python = ([IO.File]::ReadAllText((Join-Path $script:Install '.python-version'))).Trim()
        $arguments = @('sync', '--project', $script:Install, '--frozen', '--no-dev', '--no-install-project', '--python', $python, '--link-mode', 'copy')
        if ($Offline) { $arguments += '--offline' }
        & uv @arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
    } finally { $env:UV_PROJECT_ENVIRONMENT = $previous }
}

function Verify-Product($Output) {
    if ($script:Evidence) { $Output = Join-Path $script:Evidence ([Guid]::NewGuid().ToString('N')) }
    & (Join-Path $script:Install '.venv/Scripts/python.exe') -I -B $script:Verifier --product $script:Install --output $Output | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Product validation failed: $Output" }
    $report = Get-Content -LiteralPath (Join-Path $Output 'report.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $report.passed -or $report.live_model_requests -ne 0 -or
        (Full-Path $report.product) -ne $script:Install -or
        $report.release_manifest_sha256 -ne (Digest (Join-Path $script:Install 'release-manifest.json'))) {
        throw 'Product verifier did not pass for this exact installation and manifest'
    }
    return @{ passed = $true; elapsed_seconds = $report.elapsed_seconds; live_model_requests = 0;
        report = if ($script:Evidence) { Join-Path $Output 'report.json' } else { $null } }
}

function Remove-Work($Path) {
    $resolved = Full-Path $Path
    if ((Split-Path -Parent $resolved) -ne $script:Work -or (Split-Path -Leaf $resolved) -notmatch '^[a-f0-9]{32}$') { throw 'Work cleanup escapes an owned invocation directory' }
    # The invocation created this exact directory; never delete the installation or backup root.
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}

function Invoke-ProductUpdate {
    $script:Install = Full-Path $InstallRoot
    $script:Backup = Full-Path $BackupRoot
    $script:Work = Full-Path $WorkRoot
    $script:Evidence = if ($VerificationRoot) { Full-Path $VerificationRoot } else { $null }
    foreach ($a in @($script:Install, $script:Backup, $script:Work)) {
        foreach ($b in @($script:Install, $script:Backup, $script:Work)) {
            if ($a -ne $b -and (Within $a $b)) { throw 'Install, backup and work roots must be separate' }
        }
    }
    if (@($script:Install, $script:Backup, $script:Work | Select-Object -Unique).Count -ne 3) { throw 'Storage roots must differ' }
    if ($script:Evidence) {
        foreach ($root in @($script:Install, $script:Backup, $script:Work)) {
            if ($script:Evidence -eq $root -or (Within $script:Evidence $root) -or (Within $root $script:Evidence)) { throw 'Persistent verification evidence must be separate from install, backup and temporary work' }
        }
        New-Item -ItemType Directory -Path $script:Evidence -Force | Out-Null
    }
    New-Item -ItemType Directory -Path $script:Backup -Force | Out-Null
    $lock = [IO.File]::Open((Join-Path $script:Backup 'update.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $work = $null; $completed = $false; $pendingWritten = $false
    try {
        $recordPath = Join-Path $script:Backup 'state.json'
        $zipPath = Join-Path $script:Backup 'previous.zip'
        $state = if (Test-Path -LiteralPath $recordPath) { Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
        if ($state -and $state.install_root -ne $script:Install) { throw 'Backup directory belongs to another installation' }
        if ($Action -eq 'status') { return $state }
        if ($state -and $state.status -eq 'pending' -and $Action -ne 'rollback') { throw 'Interrupted update: run -Action rollback first' }
        Assert-Stopped
        $recovering = $state -and $state.status -eq 'pending'
        if (-not $recovering) { $before = Read-Release $script:Install }
        $work = Join-Path $script:Work ([Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $work -Force | Out-Null
        if ($Action -eq 'rollback') {
            $backupInput = $zipPath
            if ($recovering -and (Test-Path -LiteralPath ($zipPath + '.tmp')) -and (Digest ($zipPath + '.tmp')) -eq $state.backup_sha256) { $backupInput = $zipPath + '.tmp' }
            if (-not $state -or -not (Test-Path -LiteralPath $backupInput)) { throw 'No previous release is available' }
            if ((Digest $backupInput) -ne $state.backup_sha256) { throw 'Recovery backup was modified' }
            $after = Unpack-Payload $backupInput (Join-Path $work 'incoming')
            if ($recovering) {
                $before = Parse-Manifest ($state.after | ConvertTo-Json -Depth 20)
                # Names newly introduced by the interrupted update are also owned for recovery.
                Assert-Target $before $after -Recovering
            }
        } else {
            if (-not $Release) { throw '-Release is required for plan/update' }
            $source = Read-Release $Release
            if ($source.Root -eq $script:Install -or (Within $source.Root $script:Install) -or (Within $script:Install $source.Root)) { throw 'Release must be outside the installation' }
            $after = Copy-Payload $source (Join-Path $work 'incoming')
        }
        if (-not $recovering) { Assert-Target $before $after }
        $dependencyChange = -not (Test-Path -LiteralPath (Join-Path $script:Install '.venv/Scripts/python.exe'))
        foreach ($name in @('uv.lock', 'pyproject.toml', '.python-version')) { if ($before.Files[$name] -ne $after.Files[$name]) { $dependencyChange = $true } }
        $plan = @{ action = $Action; install_root = $script:Install; from = $before.Raw.source_commit; to = $after.Raw.source_commit;
            changed = @($after.Files.Keys | Where-Object { $before.Files[$_] -ne $after.Files[$_] }).Count;
            removed = @($before.Files.Keys | Where-Object { -not $after.Files.ContainsKey($_) }).Count; sync_dependencies = $dependencyChange }
        if ($Action -eq 'plan' -or (-not $recovering -and $before.Hash -eq $after.Hash)) { $completed = $true; return $plan }
        if (-not $recovering) {
            $nextZip = Join-Path $work 'previous.zip'
            Pack-Payload $before $nextZip
            $saved = Unpack-Payload $nextZip (Join-Path $work 'before')
            if ($saved.Hash -ne $before.Hash) { throw 'Backup verification failed' }
            Copy-Item -LiteralPath $nextZip -Destination ($zipPath + '.tmp') -Force
            $state = [pscustomobject]@{ schema_version = 'sim-bim-update.v1'; status = 'pending'; install_root = $script:Install;
                before = $before.Raw; after = $after.Raw; backup_sha256 = (Digest ($zipPath + '.tmp')); work = $work;
                started_at = [DateTime]::UtcNow.ToString('o'); finished_at = $null; validation = $null; error = $null }
            Write-Record $recordPath $state
        }
        $pendingWritten = $true
        # Record the verified new backup before promotion, so a crash in either order is recoverable.
        if (-not $recovering -or $backupInput -eq ($zipPath + '.tmp')) {
            if (Test-Path -LiteralPath $zipPath) { [IO.File]::Replace(($zipPath + '.tmp'), $zipPath, [NullString]::Value) }
            else { [IO.File]::Move(($zipPath + '.tmp'), $zipPath) }
        }
        try {
            if ($recovering) { Clear-UpdateTemps $before $after }
            Set-Payload $before $after
            if ($dependencyChange) { Sync-Dependencies }
            $state.validation = Verify-Product (Join-Path $work 'verification')
            $state.status = if ($recovering) { 'recovered' } else { 'completed' }
            $state.error = $null
            $completed = $true
        } catch {
            $failure = $_.Exception.Message
            if ($recovering) { $state.error = $failure; Write-Record $recordPath $state; throw }
            try {
                Assert-Target $after $saved -Recovering
                Clear-UpdateTemps $after $saved
                Set-Payload $after $saved
                if ($dependencyChange) { Sync-Dependencies }
                $state.validation = Verify-Product (Join-Path $work 'rollback-verification')
                $state.status = 'recovered'; $completed = $true
            } catch { $state.error = "$failure; automatic recovery failed: $($_.Exception.Message)"; Write-Record $recordPath $state; throw $state.error }
            $state.error = $failure
        }
        $state.finished_at = [DateTime]::UtcNow.ToString('o')
        $previousWork = $state.work
        $state.work = $null
        Write-Record $recordPath $state
        if ($recovering -and $previousWork -and $previousWork -ne $work) { Remove-Work $previousWork }
        if ($state.error) { throw "Update failed and the previous version was restored: $($state.error)" }
        return @{ status = $state.status; install_root = $script:Install; source_commit = $after.Raw.source_commit;
            runtime_version = $after.Raw.runtime_version; domain_version = $after.Raw.domain_version;
            backup = $zipPath; backup_bytes = (Get-Item -LiteralPath $zipPath).Length; validation = $state.validation }
    } finally {
        $lock.Dispose()
        if ($work -and ($completed -or -not $pendingWritten)) { Remove-Work $work }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-ProductUpdate | ConvertTo-Json -Depth 10 }
    catch { Write-Error $_; exit 1 }
}
