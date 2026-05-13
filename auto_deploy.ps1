$ErrorActionPreference = "Stop"

$AutoMode = "deploy"
$AutoLaunchArgs = @()
$AutoDescription = "extract latest Prompt zip, delete it, then launch start.py"

Add-Type -AssemblyName System.IO.Compression.FileSystem

$Root = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = (Get-Location).Path
}

$StatePath = Join-Path $Root ".auto-deploy-state"
$CombinedLogPath = Join-Path $Root ".auto-deploy.log"
$AutoTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'PromptAutoDeploy'
if (-not (Test-Path $AutoTempRoot)) { New-Item -ItemType Directory -Force -Path $AutoTempRoot | Out-Null }
$StdoutLogPath = Join-Path $AutoTempRoot ("prompt-auto-" + $AutoMode + "-" + [guid]::NewGuid().ToString("N") + ".stdout.log")
$StderrLogPath = Join-Path $AutoTempRoot ("prompt-auto-" + $AutoMode + "-" + [guid]::NewGuid().ToString("N") + ".stderr.log")
$DebugLogPath = Join-Path $Root 'debug.log'

$AutoLogMaxBytes = 20MB
$AutoLogKeepBytes = 5MB
if ($env:PROMPT_AUTO_LOG_MAX_MB) {
    try { $AutoLogMaxBytes = [int]$env:PROMPT_AUTO_LOG_MAX_MB * 1MB } catch { $AutoLogMaxBytes = 20MB }
}
if ($env:PROMPT_AUTO_LOG_KEEP_MB) {
    try { $AutoLogKeepBytes = [int]$env:PROMPT_AUTO_LOG_KEEP_MB * 1MB } catch { $AutoLogKeepBytes = 5MB }
}
$script:SuppressedStdoutLines = 0
$script:SuppressedStderrLines = 0

# AUTO-LOG-RESET-BOUND v175: each watcher invocation starts with fresh logs so .auto*.log cannot balloon across builds.
try {
    $logParent = Split-Path -Parent $CombinedLogPath
    if ($logParent) { New-Item -ItemType Directory -Force -Path $logParent | Out-Null }
    Set-Content -Path $CombinedLogPath -Value $null -Encoding UTF8
    if (-not ($env:PROMPT_APPEND_DEBUG_LOG -match '^(1|true|yes|on)$')) { Set-Content -Path $DebugLogPath -Value $null -Encoding UTF8 }
    if (-not ($env:PROMPT_APPEND_RUN_LOG -match '^(1|true|yes|on)$')) {
        foreach ($rootLogName in @('run.log', 'run_faults.log', 'errors.log')) {
            $rootLogPath = Join-Path $Root $rootLogName
            Set-Content -Path $rootLogPath -Value $null -Encoding UTF8
        }
    }
    if (-not ($env:PROMPT_APPEND_BUILD_LOGS -match '^(1|true|yes|on)$')) {
        $logsDir = Join-Path $Root 'logs'
        if (Test-Path $logsDir) {
            Get-ChildItem -Path $logsDir -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '(?i)(^early_build_|^release_|\.raw\.log$|final_.*md5s\.log$|build_pipeline\.log$)' } |
                Remove-Item -Force -ErrorAction SilentlyContinue
        }
    }
    Set-Content -Path $StdoutLogPath -Value $null -Encoding UTF8
    Set-Content -Path $StderrLogPath -Value $null -Encoding UTF8
    Get-ChildItem -Path $Root -Filter '.auto-*-child*.log' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
} catch {
    Write-Host ("[auto-log-reset-warning] " + $_.Exception.Message)
}


function Trim-LogFileIfNeeded {
    param([string]$Path)

    if ($env:PROMPT_DISABLE_AUTO_LOG_TRIM -match '^(1|true|yes|on)$') {
        return
    }
    if (-not (Test-Path $Path)) {
        return
    }
    try {
        $file = Get-Item $Path -ErrorAction Stop
        if ($file.Length -le $AutoLogMaxBytes) {
            return
        }
        $keepBytes = [Math]::Min([int64]$AutoLogKeepBytes, [Math]::Max([int64]1024, [int64]$AutoLogMaxBytes))
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            if ($stream.Length -gt $keepBytes) {
                $stream.Seek(-$keepBytes, [System.IO.SeekOrigin]::End) | Out-Null
            }
            $buffer = New-Object byte[] ([int]([Math]::Min($keepBytes, $stream.Length)))
            $read = $stream.Read($buffer, 0, $buffer.Length)
        } finally {
            $stream.Dispose()
        }
        $marker = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] [auto-log-trim] $Path exceeded $AutoLogMaxBytes bytes; kept newest $read bytes. Full old prefix was discarded to prevent runaway 80MB+ logs.`r`n"
        $markerBytes = [System.Text.Encoding]::UTF8.GetBytes($marker)
        $out = [System.IO.File]::Open($Path, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
        try {
            $out.Write($markerBytes, 0, $markerBytes.Length)
            $out.Write($buffer, 0, $read)
        } finally {
            $out.Dispose()
        }
    } catch {
        Write-Host ("[auto-log-trim-warning] " + $_.Exception.Message)
    }
}

function Test-ImportantChildLine {
    param([string]$Line)

    if ($env:PROMPT_AUTO_VERBOSE_CHILD_LOG -match '^(1|true|yes|on)$') {
        return $true
    }
    if ([string]::IsNullOrWhiteSpace($Line)) {
        return $false
    }
    return ($Line -match '(?i)(\[STAGE|\[BUILD|\[WARN|\[ERROR|\[FATAL|Traceback|Exception|FAILED|SUCCESS|Now creating|PROMPT-RELEASE|PyInstaller|Nuitka|cx_Freeze|py2exe|PyOxidizer|PyApp|Briefcase|BeeWare|MSI|NSIS|WiX|installer|artifact|md5|raw-log|exit code|Process exited)')
}

function Write-SuppressedChildSummary {
    param(
        [string]$StreamName,
        [int]$Count,
        [string]$RawPath
    )

    if ($Count -gt 0) {
        Write-Log "[child:$StreamName] suppressed $Count low-signal lines; full raw stream is temporarily captured at $RawPath. Set PROMPT_AUTO_VERBOSE_CHILD_LOG=1 to mirror every line."
    }
}

function Write-Log {
    param([string]$Message)

    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$stamp] $Message"
    Write-Host $line
    Add-Content -Path $CombinedLogPath -Value $line -Encoding UTF8
    try { Add-Content -Path $DebugLogPath -Value $line -Encoding UTF8 } catch {}
    Trim-LogFileIfNeeded -Path $CombinedLogPath
}


function Write-DistSnapshot {
    param([string]$Context)

    try {
        $dist = Join-Path $Root 'dist'
        if (-not (Test-Path $dist)) {
            Write-Log "DIST:SNAPSHOT context=$Context count=0 total_bytes=0 dist=$dist missing=1"
            return
        }
        $files = @(Get-ChildItem -Path $dist -Recurse -File -ErrorAction SilentlyContinue | Sort-Object FullName)
        $total = 0L
        foreach ($f in $files) { $total += [int64]$f.Length }
        Write-Log "DIST:SNAPSHOT context=$Context count=$($files.Count) total_bytes=$total dist=$dist"
        foreach ($f in ($files | Select-Object -First 80)) {
            $rel = $f.FullName.Substring($dist.Length).TrimStart('\','/')
            $hash = ''
            try { $hash = (Get-FileHash -Algorithm MD5 -Path $f.FullName).Hash.ToLowerInvariant() } catch {}
            Write-Log "DIST:FILE context=$Context name=$rel bytes=$($f.Length) md5=$hash"
        }
        if ($files.Count -gt 80) { Write-Log "DIST:SNAPSHOT context=$Context omitted=$($files.Count - 80)" }
    } catch {
        Write-Log ("DIST:SNAPSHOT:ERROR context={0} error={1}" -f $Context, $_.Exception.Message)
    }
}

function New-State {
    return [pscustomobject]@{
        LastLaunchedPath        = ""
        LastLaunchedMtimeUtc    = ""
        LastArchivePath         = ""
        LastArchiveMtimeUtc     = ""
    }
}

function Normalize-State {
    param($State)

    $empty = New-State
    if (-not $State) {
        return $empty
    }

    $lastLaunchedPath = ""
    $lastLaunchedMtimeUtc = ""
    $lastArchivePath = ""
    $lastArchiveMtimeUtc = ""

    if ($State.PSObject.Properties['LastLaunchedPath']) {
        $lastLaunchedPath = [string]$State.LastLaunchedPath
    }

    if ($State.PSObject.Properties['LastLaunchedMtimeUtc']) {
        $lastLaunchedMtimeUtc = [string]$State.LastLaunchedMtimeUtc
    }

    if ($State.PSObject.Properties['LastArchivePath']) {
        $lastArchivePath = [string]$State.LastArchivePath
    }

    if ($State.PSObject.Properties['LastArchiveMtimeUtc']) {
        $lastArchiveMtimeUtc = [string]$State.LastArchiveMtimeUtc
    }

    return [pscustomobject]@{
        LastLaunchedPath        = $lastLaunchedPath
        LastLaunchedMtimeUtc    = $lastLaunchedMtimeUtc
        LastArchivePath         = $lastArchivePath
        LastArchiveMtimeUtc     = $lastArchiveMtimeUtc
    }
}

function Load-State {
    if (Test-Path $StatePath) {
        try {
            $raw = Get-Content $StatePath -Raw | ConvertFrom-Json
            return Normalize-State $raw
        } catch {
            Write-Log "State file unreadable. Resetting."
        }
    }

    return New-State
}

function Save-State {
    param($State)

    $normalized = Normalize-State $State
    $normalized | ConvertTo-Json -Depth 5 | Set-Content -Path $StatePath -Encoding UTF8
}

function Get-NewestArchive {
    $patterns = @("*.zip")
    $all = @()
    foreach ($pattern in $patterns) {
        $all += Get-ChildItem -Path $Root -File -Filter $pattern -ErrorAction SilentlyContinue
    }

    return $all |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
}

function Expand-ArchiveToTemp {
    param([string]$ArchivePath)

    $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("auto_deploy_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temp -Force | Out-Null

    try {
        Expand-Archive -Path $ArchivePath -DestinationPath $temp -Force
    } catch {
        Write-Log "Expand-Archive failed, trying .NET extraction..."
        try {
            [System.IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $temp)
        } catch {
            Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
            throw
        }
    }

    return $temp
}

function Get-PayloadRoot {
    param([string]$ExtractedRoot)

    $current = Get-Item $ExtractedRoot

    while ($true) {
        $directories = @(Get-ChildItem -Path $current.FullName -Directory -Force -ErrorAction SilentlyContinue)
        $files = @(Get-ChildItem -Path $current.FullName -File -Force -ErrorAction SilentlyContinue)

        if ($files.Count -eq 0 -and $directories.Count -eq 1) {
            $current = $directories[0]
            continue
        }

        return $current.FullName
    }
}

function Copy-TreeOverRoot {
    param(
        [string]$SourceRoot,
        [string]$DestinationRoot
    )

    $sourceItem = Get-Item $SourceRoot
    $sourcePrefixLength = $sourceItem.FullName.Length

    Get-ChildItem -Path $SourceRoot -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourcePrefixLength).TrimStart([char[]]@('\','/'))
        if ([string]::IsNullOrWhiteSpace($relative)) {
            return
        }

        $destination = Join-Path $DestinationRoot $relative

        if ($_.PSIsContainer) {
            if (-not (Test-Path $destination)) {
                New-Item -ItemType Directory -Path $destination -Force | Out-Null
            }
            return
        }

        $destinationDirectory = Split-Path $destination -Parent
        if (-not (Test-Path $destinationDirectory)) {
            New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        }

        Copy-Item -Path $_.FullName -Destination $destination -Force
    }
}

function Extract-ArchiveOverRoot {
    param($ArchiveFile)

    Write-Log "Extracting archive over root: $($ArchiveFile.FullName)"
    $temp = $null

    try {
        $temp = Expand-ArchiveToTemp -ArchivePath $ArchiveFile.FullName
        $payloadRoot = Get-PayloadRoot -ExtractedRoot $temp
        Write-Log "Payload root: $payloadRoot"
        Copy-TreeOverRoot -SourceRoot $payloadRoot -DestinationRoot $Root
        Remove-Item $ArchiveFile.FullName -Force -ErrorAction SilentlyContinue
        Write-Log "Deleted archive: $($ArchiveFile.FullName)"
    } finally {
        if ($temp -and (Test-Path $temp)) {
            Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}


$RequiredPromptSourceFiles = @(
    "start.py",
    "frozen_prompt_entry.py",
    "prompt_app.py",
    (Join-Path "tools" "run_prompt_release.py")
)

function Get-MissingRequiredPromptSourceFiles {
    $missing = @()
    foreach ($relativePath in $RequiredPromptSourceFiles) {
        $candidate = Join-Path $Root $relativePath
        if (-not (Test-Path $candidate -PathType Leaf)) {
            $missing += $candidate
        }
    }
    return @($missing)
}

function Test-RequiredPromptSourceFiles {
    return ((Get-MissingRequiredPromptSourceFiles).Count -eq 0)
}

function Test-ZipLooksLikePromptPayload {
    param([string]$ArchivePath)

    try {
        $zip = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
        try {
            $names = @{}
            foreach ($entry in $zip.Entries) {
                $normalized = ($entry.FullName -replace '\\','/').TrimStart('/')
                if ([string]::IsNullOrWhiteSpace($normalized)) { continue }
                $lower = $normalized.ToLowerInvariant()
                $names[$lower] = $true
                $parts = $lower.Split('/')
                if ($parts.Count -gt 1) {
                    $tail = ($parts[1..($parts.Count-1)] -join '/')
                    if (-not [string]::IsNullOrWhiteSpace($tail)) { $names[$tail] = $true }
                }
            }
            foreach ($required in @('start.py','frozen_prompt_entry.py','prompt_app.py','tools/run_prompt_release.py')) {
                if (-not $names.ContainsKey($required)) { return $false }
            }
            return $true
        } finally {
            $zip.Dispose()
        }
    } catch {
        Write-Log ("Zip inspection failed for {0}: {1}" -f $ArchivePath, $_.Exception.Message)
        return $false
    }
}

function Get-NewestPromptArchive {
    $archives = @(Get-ChildItem -Path $Root -File -Filter '*.zip' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending)
    foreach ($candidate in $archives) {
        if (Test-ZipLooksLikePromptPayload -ArchivePath $candidate.FullName) {
            return $candidate
        }
        Write-Log "Skipping zip that is not a Prompt payload: $($candidate.FullName)"
    }
    return $null
}

function Ensure-PromptSourcesAvailable {
    param([switch]$ThrowOnFailure)

    $missing = @(Get-MissingRequiredPromptSourceFiles)
    if ($missing.Count -eq 0) {
        return $true
    }

    Write-Log "Required Prompt source files are missing before launch/preflight; attempting zip bootstrap extraction."
    foreach ($item in $missing) {
        Write-Log "  missing: $item"
    }

    $archive = Get-NewestPromptArchive
    if (-not $archive) {
        $message = "No Prompt payload zip found in $Root; cannot bootstrap missing source files. Copy the latest Prompt_CWV*.zip or Prompt.zip into this folder and rerun this script."
        Write-Log $message
        if ($ThrowOnFailure) { throw $message }
        return $false
    }

    Write-Log "Bootstrap archive selected: $($archive.FullName)"
    Extract-ArchiveOverRoot -ArchiveFile $archive

    $missingAfter = @(Get-MissingRequiredPromptSourceFiles)
    if ($missingAfter.Count -gt 0) {
        Write-Log "Pre-flight FAIL after extraction; required source files are still missing:"
        foreach ($item in $missingAfter) {
            Write-Log "  missing: $item"
        }
        $message = "Pre-flight failed after extracting $($archive.Name): missing $($missingAfter.Count) required source file(s)."
        if ($ThrowOnFailure) { throw $message }
        return $false
    }

    Write-Log "Pre-flight OK after bootstrap extraction: required Prompt source files are present."
    return $true
}

function Resolve-LaunchTarget {
    $rootStart = Join-Path $Root "start.py"
    if (Test-Path $rootStart) {
        return (Get-Item $rootStart)
    }

    $rootMain = Join-Path $Root "main.py"
    if (Test-Path $rootMain) {
        return (Get-Item $rootMain)
    }

    $nestedStart = Get-ChildItem -Path $Root -Recurse -File -Filter start.py -ErrorAction SilentlyContinue |
        Sort-Object FullName |
        Select-Object -First 1
    if ($nestedStart) {
        return $nestedStart
    }

    $nestedMain = Get-ChildItem -Path $Root -Recurse -File -Filter main.py -ErrorAction SilentlyContinue |
        Sort-Object FullName |
        Select-Object -First 1
    if ($nestedMain) {
        return $nestedMain
    }

    return $null
}

function Test-WindowsAppsPythonAlias {
    param([string]$Path)

    $text = [string]$Path
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $false
    }
    return ($text -match '(?i)\\WindowsApps\\python(\.exe)?$')
}

function Test-PythonCandidate {
    param(
        [string]$FilePath,
        [string[]]$PrefixArgs = @(),
        [switch]$PreferBuildSafe
    )

    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        return $false
    }
    if ((Test-WindowsAppsPythonAlias -Path $FilePath) -and -not ($env:PROMPT_ALLOW_WINDOWSAPPS_PYTHON -match '^(1|true|yes|on)$')) {
        return $false
    }
    try {
        $probe = @()
        foreach ($prefix in $PrefixArgs) { $probe += [string]$prefix }
        if ($PreferBuildSafe) {
            $probe += @('-c', 'import sys; raise SystemExit(0 if ((3,10) <= sys.version_info[:2] <= (3,13)) else 7)')
        } else {
            $probe += @('-c', 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 7)')
        }
        $probeOut = [System.IO.Path]::GetTempFileName()
        $probeErr = [System.IO.Path]::GetTempFileName()
        $p = Start-HiddenPythonProcess -FilePath $FilePath -Arguments $probe -StdoutPath $probeOut -StderrPath $probeErr -Wait
        try { Remove-Item $probeOut, $probeErr -Force -ErrorAction SilentlyContinue } catch {}
        return ($p.ExitCode -eq 0)
    } catch {
        return $false
    }
}

function New-PythonCommandObject {
    param(
        [string]$FilePath,
        [string[]]$PrefixArgs = @(),
        [string]$Reason = ''
    )
    return [pscustomobject]@{
        FilePath   = $FilePath
        PrefixArgs = @($PrefixArgs)
        Reason     = $Reason
    }
}

function Get-PythonCommand {
    $preferBuildSafe = ($AutoMode -match '(?i)build')
    $envNames = @('PROMPT_BUILD_PYTHON', 'PROMPT_PYTHON', 'PYTHON312', 'PYTHON_EXE')
    foreach ($name in $envNames) {
        $value = [string](Get-Item -Path ("Env:" + $name) -ErrorAction SilentlyContinue).Value
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $candidate = $value.Trim('"')
            if ((Test-Path $candidate) -and (Test-PythonCandidate -FilePath $candidate -PreferBuildSafe:$preferBuildSafe)) {
                return (New-PythonCommandObject -FilePath $candidate -Reason ("env:" + $name))
            }
            Write-Log ("Python candidate skipped from {0}: {1}" -f $name, $candidate)
        }
    }

    if ($IsWindows -or $env:OS -match 'Windows') {
        $explicit = @(
            'C:\Python313\python.exe',
            'C:\Python312\python.exe',
            'C:\Python311\python.exe',
            'C:\Python310\python.exe'
        )
        $localAppData = [string]$env:LOCALAPPDATA
        if (-not [string]::IsNullOrWhiteSpace($localAppData)) {
            $explicit += @(
                (Join-Path $localAppData 'Programs\Python\Python313\python.exe'),
                (Join-Path $localAppData 'Programs\Python\Python312\python.exe'),
                (Join-Path $localAppData 'Programs\Python\Python311\python.exe'),
                (Join-Path $localAppData 'Programs\Python\Python310\python.exe')
            )
        }
        foreach ($candidate in $explicit) {
            if ((Test-Path $candidate) -and (Test-PythonCandidate -FilePath $candidate -PreferBuildSafe:$preferBuildSafe)) {
                return (New-PythonCommandObject -FilePath $candidate -Reason 'explicit-python-install')
            }
        }

        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            foreach ($versionArg in @('-3.13', '-3.12', '-3.11', '-3.10')) {
                if (Test-PythonCandidate -FilePath $py.Source -PrefixArgs @($versionArg) -PreferBuildSafe:$preferBuildSafe) {
                    return (New-PythonCommandObject -FilePath $py.Source -PrefixArgs @($versionArg) -Reason ("py-launcher:" + $versionArg))
                }
            }
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and (Test-PythonCandidate -FilePath $python.Source -PreferBuildSafe:$preferBuildSafe)) {
        return (New-PythonCommandObject -FilePath $python.Source -Reason 'path-python')
    }

    if ($python -and (Test-WindowsAppsPythonAlias -Path $python.Source)) {
        Write-Log "Python candidate skipped because it is the WindowsApps alias: $($python.Source). Install/use Python 3.12 or set PROMPT_BUILD_PYTHON=C:\Python312\python.exe."
    }

    $pyFallback = Get-Command py -ErrorAction SilentlyContinue
    if ($pyFallback -and (Test-PythonCandidate -FilePath $pyFallback.Source -PrefixArgs @('-3') -PreferBuildSafe:$false)) {
        return (New-PythonCommandObject -FilePath $pyFallback.Source -PrefixArgs @('-3') -Reason 'py-launcher-fallback')
    }

    if ($python -and ($env:PROMPT_ALLOW_WINDOWSAPPS_PYTHON -match '^(1|true|yes|on)$')) {
        return (New-PythonCommandObject -FilePath $python.Source -Reason 'windowsapps-allowed')
    }

    throw "Could not find a usable Python. For builds, install Python 3.12/3.13 or set PROMPT_BUILD_PYTHON=C:\Python312\python.exe. The WindowsApps python alias is intentionally skipped."
}

function Format-ArgumentString {
    param([string[]]$Arguments)

    $parts = foreach ($argument in $Arguments) {
        $text = [string]$argument
        if ([string]::IsNullOrEmpty($text)) {
            '""'
        } elseif ($text -match '[\s"]') {
            '"' + ($text -replace '"', '\\"') + '"'
        } else {
            $text
        }
    }

    return ($parts -join ' ')
}


function Start-HiddenPythonProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = '',
        [string]$StdoutPath = '',
        [string]$StderrPath = '',
        [switch]$Wait
    )

    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $WorkingDirectory = $Root
    }
    $argumentString = Format-ArgumentString -Arguments $Arguments
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $argumentString
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.EnableRaisingEvents = $true

    if ($Wait) {
        $null = $process.Start()
        $stdoutText = $process.StandardOutput.ReadToEnd()
        $stderrText = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if (-not [string]::IsNullOrWhiteSpace($StdoutPath)) {
            [System.IO.File]::WriteAllText($StdoutPath, [string]$stdoutText, [System.Text.Encoding]::UTF8)
        }
        if (-not [string]::IsNullOrWhiteSpace($StderrPath)) {
            [System.IO.File]::WriteAllText($StderrPath, [string]$stderrText, [System.Text.Encoding]::UTF8)
        }
        return $process
    }

    $stdoutTarget = if ([string]::IsNullOrWhiteSpace($StdoutPath)) { [System.IO.Path]::GetTempFileName() } else { $StdoutPath }
    $stderrTarget = if ([string]::IsNullOrWhiteSpace($StderrPath)) { [System.IO.Path]::GetTempFileName() } else { $StderrPath }
    [System.IO.File]::WriteAllText($stdoutTarget, '', [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText($stderrTarget, '', [System.Text.Encoding]::UTF8)

    # Do NOT use PowerShell scriptblock DataReceived handlers here.
    # PowerShell 7 can invoke those callbacks on a ThreadPool thread with no
    # Runspace, crashing the whole host with "There is no Runspace available".
    # Let the OS/cmd.exe redirect child stdout/stderr into files, then the
    # watcher tails those files normally via Read-LogDelta.
    $redirectProcess = New-Object System.Diagnostics.Process
    $redirectPsi = New-Object System.Diagnostics.ProcessStartInfo
    if ($IsWindows -or $env:OS -match 'Windows') {
        $comspec = if ([string]::IsNullOrWhiteSpace($env:ComSpec)) { 'cmd.exe' } else { $env:ComSpec }
        $redirectPsi.FileName = $comspec
        $cmdLine = '"' + $FilePath + '" ' + $argumentString + ' 1>>"' + $stdoutTarget + '" 2>>"' + $stderrTarget + '"'
        $redirectPsi.Arguments = '/d /s /c "' + $cmdLine + '"'
    } else {
        $redirectPsi.FileName = '/bin/sh'
        $shellFile = "'" + ([string]$FilePath).Replace("'", "'\''") + "'"
        $shellOut = "'" + ([string]$stdoutTarget).Replace("'", "'\''") + "'"
        $shellErr = "'" + ([string]$stderrTarget).Replace("'", "'\''") + "'"
        $redirectPsi.Arguments = '-lc "' + $shellFile + ' ' + $argumentString.Replace('"', '\"') + ' 1>>' + $shellOut + ' 2>>' + $shellErr + '"'
    }
    $redirectPsi.WorkingDirectory = $WorkingDirectory
    $redirectPsi.UseShellExecute = $false
    $redirectPsi.CreateNoWindow = $true
    $redirectPsi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $redirectPsi.RedirectStandardOutput = $false
    $redirectPsi.RedirectStandardError = $false
    $redirectProcess.StartInfo = $redirectPsi
    $null = $redirectProcess.Start()
    return $redirectProcess
}

function Stop-ChildProcess {
    param($Process)

    if (-not $Process) {
        return
    }

    try {
        if (-not $Process.HasExited) {
            Write-Log "Stopping PID $($Process.Id)"
            try {
                $null = $Process.CloseMainWindow()
            } catch {
            }
            Start-Sleep -Milliseconds 800

            if (-not $Process.HasExited) {
                Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
    }
}

function Reset-ChildLogFiles {
    Set-Content -Path $StdoutLogPath -Value $null -Encoding UTF8
    Set-Content -Path $StderrLogPath -Value $null -Encoding UTF8
}

function Start-ChildProcess {
    param($TargetFile)

    $pythonCommand = Get-PythonCommand
    $allArgs = @()
    $allArgs += $pythonCommand.PrefixArgs
    $allArgs += @($TargetFile.FullName)
    foreach ($autoArg in $AutoLaunchArgs) {
        $allArgs += @([string]$autoArg)
    }
    $argumentString = Format-ArgumentString -Arguments $allArgs

    Reset-ChildLogFiles

    $env:PYTHONIOENCODING = 'utf-8'
    if ($AutoMode -match '(?i)build') {
        $env:PROMPT_BUILD_MODE = '1'
        if (-not $env:PROMPT_AUTO_INSTALL_INSTALLER_TOOLS) { $env:PROMPT_AUTO_INSTALL_INSTALLER_TOOLS = '1' }
        if (-not $env:PROMPT_AUTO_INSTALL_BUILD_PYTHON) { $env:PROMPT_AUTO_INSTALL_BUILD_PYTHON = '1' }
        if (-not $env:PROMPT_BUILD_PYTHON_COOLDOWN_HOURS) { $env:PROMPT_BUILD_PYTHON_COOLDOWN_HOURS = '24' }
        if (-not $env:PROMPT_INSTALLER_TOOL_COOLDOWN_HOURS) { $env:PROMPT_INSTALLER_TOOL_COOLDOWN_HOURS = '24' }
        $env:PROMPT_CLI_ONLY = '1'
        $env:PROMPT_NO_GUI_DURING_BUILD = '1'
        $env:PYTHONDONTWRITEBYTECODE = '1'
        $env:PROMPT_WINDOW_MONITOR = '0'
        if (-not $env:PROMPT_BUILD_RESUME) { $env:PROMPT_BUILD_RESUME = '1' }
        # Do not force PROMPT_BUILD_PYTHON to the launcher interpreter here.
        # The release runner must be free to pick a backend-safe Python for
        # Nuitka/py2exe, or to experiment with Python 3.14 when no safer one
        # exists.  Preserve an explicitly supplied PROMPT_BUILD_PYTHON, and
        # log the launcher interpreter separately for debugging.
        if ($pythonCommand.FilePath -match '(?i)python(\.exe)?$') {
            $env:PROMPT_LAUNCH_PYTHON = $pythonCommand.FilePath
            if ($env:PROMPT_BUILD_PYTHON -and ($env:PROMPT_BUILD_PYTHON -ieq $pythonCommand.FilePath) -and -not ($env:PROMPT_KEEP_BUILD_PYTHON -match '^(1|true|yes|on)$')) {
                Remove-Item Env:PROMPT_BUILD_PYTHON -ErrorAction SilentlyContinue
                Write-Log "PROMPT_BUILD_PYTHON matched launcher Python and was cleared so the release runner can probe backend-safe Python. Set PROMPT_KEEP_BUILD_PYTHON=1 to force it. Launcher python=$($pythonCommand.FilePath)"
            } elseif (-not $env:PROMPT_BUILD_PYTHON) {
                Write-Log "PROMPT_BUILD_PYTHON not forced by auto script; release runner will discover backend-safe Python. Launcher python=$($pythonCommand.FilePath)"
            } else {
                Write-Log "PROMPT_BUILD_PYTHON was explicitly set; preserving value=$env:PROMPT_BUILD_PYTHON"
            }
        }
    }

    Write-Log "Python selected: $($pythonCommand.FilePath) prefix=[$(($pythonCommand.PrefixArgs -join ' '))] reason=$($pythonCommand.Reason)"
    Write-Log "Launching: $($pythonCommand.FilePath) $argumentString"

    return Start-HiddenPythonProcess -FilePath $pythonCommand.FilePath `
                                     -Arguments $allArgs `
                                     -WorkingDirectory $TargetFile.DirectoryName `
                                     -StdoutPath $StdoutLogPath `
                                     -StderrPath $StderrLogPath
}

function Read-LogDelta {
    param(
        [string]$Path,
        [ref]$Offset,
        [ref]$Partial
    )

    if (-not (Test-Path $Path)) {
        return @()
    }

    $fileInfo = Get-Item $Path
    if ($Offset.Value -gt $fileInfo.Length) {
        $Offset.Value = 0
        $Partial.Value = ""
    }

    if ($Offset.Value -eq $fileInfo.Length) {
        return @()
    }

    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)

    try {
        $stream.Seek($Offset.Value, [System.IO.SeekOrigin]::Begin) | Out-Null
        $reader = New-Object System.IO.StreamReader($stream)
        $text = $reader.ReadToEnd()
        $Offset.Value = $stream.Position
    } finally {
        if ($reader) {
            $reader.Dispose()
        } elseif ($stream) {
            $stream.Dispose()
        }
    }

    if ([string]::IsNullOrEmpty($text)) {
        return @()
    }

    $combined = [string]$Partial.Value + $text
    $combined = $combined -replace "`r`n", "`n"
    $combined = $combined -replace "`r", "`n"
    $segments = $combined -split "`n", -1

    $lines = @()
    if ($combined.EndsWith("`n")) {
        $Partial.Value = ""
        $lines = $segments | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
    } else {
        if ($segments.Length -gt 1) {
            $Partial.Value = $segments[-1]
            $lines = $segments[0..($segments.Length - 2)] | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
        } else {
            $Partial.Value = $combined
            $lines = @()
        }
    }

    return ,$lines
}

function Flush-ChildLogs {
    param(
        [ref]$StdoutOffset,
        [ref]$StdoutPartial,
        [ref]$StderrOffset,
        [ref]$StderrPartial,
        [switch]$FlushPartial
    )

    $stdoutLines = Read-LogDelta -Path $StdoutLogPath -Offset $StdoutOffset -Partial $StdoutPartial
    foreach ($line in $stdoutLines) {
        if (Test-ImportantChildLine -Line $line) {
            Write-SuppressedChildSummary -StreamName "stdout" -Count $script:SuppressedStdoutLines -RawPath $StdoutLogPath
            $script:SuppressedStdoutLines = 0
            Write-Log "[child:stdout] $line"
        } else {
            $script:SuppressedStdoutLines += 1
        }
    }

    $stderrLines = Read-LogDelta -Path $StderrLogPath -Offset $StderrOffset -Partial $StderrPartial
    foreach ($line in $stderrLines) {
        if (Test-ImportantChildLine -Line $line) {
            Write-SuppressedChildSummary -StreamName "stderr" -Count $script:SuppressedStderrLines -RawPath $StderrLogPath
            $script:SuppressedStderrLines = 0
            Write-Log "[child:stderr] $line"
        } else {
            $script:SuppressedStderrLines += 1
        }
    }

    if ($FlushPartial) {
        if (-not [string]::IsNullOrWhiteSpace([string]$StdoutPartial.Value)) {
            if (Test-ImportantChildLine -Line ([string]$StdoutPartial.Value)) {
                Write-SuppressedChildSummary -StreamName "stdout" -Count $script:SuppressedStdoutLines -RawPath $StdoutLogPath
                $script:SuppressedStdoutLines = 0
                Write-Log "[child:stdout] $($StdoutPartial.Value)"
            } else {
                $script:SuppressedStdoutLines += 1
            }
            $StdoutPartial.Value = ""
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$StderrPartial.Value)) {
            if (Test-ImportantChildLine -Line ([string]$StderrPartial.Value)) {
                Write-SuppressedChildSummary -StreamName "stderr" -Count $script:SuppressedStderrLines -RawPath $StderrLogPath
                $script:SuppressedStderrLines = 0
                Write-Log "[child:stderr] $($StderrPartial.Value)"
            } else {
                $script:SuppressedStderrLines += 1
            }
            $StderrPartial.Value = ""
        }
        Write-SuppressedChildSummary -StreamName "stdout" -Count $script:SuppressedStdoutLines -RawPath $StdoutLogPath
        Write-SuppressedChildSummary -StreamName "stderr" -Count $script:SuppressedStderrLines -RawPath $StderrLogPath
        $script:SuppressedStdoutLines = 0
        $script:SuppressedStderrLines = 0
    }
}

function Read-Hotkeys {
    $result = [pscustomobject]@{
        Restart = $false
        Quit    = $false
    }

    try {
        while ([Console]::KeyAvailable) {
            $keyInfo = [Console]::ReadKey($true)
            switch ($keyInfo.Key) {
                ([ConsoleKey]::R) { $result.Restart = $true }
                ([ConsoleKey]::Q) { $result.Quit = $true }
            }
        }
    } catch {
    }

    return $result
}

$state = Load-State
$currentProcess = $null
$sessionHasLaunched = $false
$sessionLastLaunchedPath = ""
$sessionLastLaunchedMtimeUtc = ""
$sessionLastArchivePath = ""
$sessionLastArchiveMtimeUtc = ""
$stdoutOffset = 0L
$stderrOffset = 0L
$stdoutPartial = ""
$stderrPartial = ""
$exitIdleNoticeSent = $false

Write-Log "Auto deploy watcher started in: $Root"
Write-Log "R = relaunch app | Q = quit watcher"
Ensure-PromptSourcesAvailable -ThrowOnFailure | Out-Null

while ($true) {
    try {
        $hotkeys = Read-Hotkeys

        if ($hotkeys.Quit) {
            Write-Log "Quit requested."
            Flush-ChildLogs -StdoutOffset ([ref]$stdoutOffset) -StdoutPartial ([ref]$stdoutPartial) -StderrOffset ([ref]$stderrOffset) -StderrPartial ([ref]$stderrPartial) -FlushPartial
            if ($currentProcess) {
                Stop-ChildProcess $currentProcess
            }
            break
        }

        $manualRestart = [bool]$hotkeys.Restart
        if ($manualRestart) {
            Write-Log "Manual relaunch requested."
        }

        Ensure-PromptSourcesAvailable -ThrowOnFailure | Out-Null

        $archiveExtracted = $false
        $archive = Get-NewestArchive
        if ($archive) {
            $archiveMtimeUtc = $archive.LastWriteTimeUtc.ToString("o")
            $archiveChanged = (
                $sessionLastArchivePath -ne $archive.FullName -or
                $sessionLastArchiveMtimeUtc -ne $archiveMtimeUtc
            )

            if ($archiveChanged) {
                Extract-ArchiveOverRoot -ArchiveFile $archive
                $archiveExtracted = $true
                $sessionLastArchivePath = $archive.FullName
                $sessionLastArchiveMtimeUtc = $archiveMtimeUtc
                $state.LastArchivePath = $archive.FullName
                $state.LastArchiveMtimeUtc = $archiveMtimeUtc
                Save-State $state
                Write-Log "Archive filemtime changed. Relaunch will use the freshly extracted files."
            }
        }

        $launchTarget = Resolve-LaunchTarget
        if (-not $launchTarget) {
            Write-Log "No start.py or main.py found."
            Start-Sleep -Milliseconds 500
            continue
        }

        $launchMtimeUtc = $launchTarget.LastWriteTimeUtc.ToString("o")
        $launchChanged = (
            $sessionLastLaunchedPath -ne $launchTarget.FullName -or
            $sessionLastLaunchedMtimeUtc -ne $launchMtimeUtc
        )

        $shouldLaunch = $false
        if ($manualRestart) {
            $shouldLaunch = $true
        } elseif ($archiveExtracted) {
            $shouldLaunch = $true
        } elseif (-not $sessionHasLaunched) {
            $shouldLaunch = $true
        } elseif ($launchChanged) {
            $shouldLaunch = $true
        }

        if ($shouldLaunch) {
            if ($currentProcess) {
                Flush-ChildLogs -StdoutOffset ([ref]$stdoutOffset) -StdoutPartial ([ref]$stdoutPartial) -StderrOffset ([ref]$stderrOffset) -StderrPartial ([ref]$stderrPartial) -FlushPartial
                Stop-ChildProcess $currentProcess
            }

            $stdoutOffset = 0L
            $stderrOffset = 0L
            $stdoutPartial = ""
            $stderrPartial = ""
            $currentProcess = Start-ChildProcess -TargetFile $launchTarget
            $sessionHasLaunched = $true
            $sessionLastLaunchedPath = $launchTarget.FullName
            $sessionLastLaunchedMtimeUtc = $launchMtimeUtc
            $state.LastLaunchedPath = $launchTarget.FullName
            $state.LastLaunchedMtimeUtc = $launchMtimeUtc
            Save-State $state
            $exitIdleNoticeSent = $false
            Start-Sleep -Milliseconds 300
        }

        if ($currentProcess) {
            Flush-ChildLogs -StdoutOffset ([ref]$stdoutOffset) -StdoutPartial ([ref]$stdoutPartial) -StderrOffset ([ref]$stderrOffset) -StderrPartial ([ref]$stderrPartial)

            if ($currentProcess.HasExited -and -not $exitIdleNoticeSent) {
                Flush-ChildLogs -StdoutOffset ([ref]$stdoutOffset) -StdoutPartial ([ref]$stdoutPartial) -StderrOffset ([ref]$stderrOffset) -StderrPartial ([ref]$stderrPartial) -FlushPartial
                Write-Log "Process exited with code $($currentProcess.ExitCode). Waiting for filemtime change or R to relaunch."
                Write-DistSnapshot -Context ("auto-process-exited-code-" + $currentProcess.ExitCode)
                $exitIdleNoticeSent = $true
            }
        }
    } catch {
        Write-Log ("ERROR: " + $_.Exception.Message)
    }

    Start-Sleep -Milliseconds 500
}
try {
    Remove-Item $StdoutLogPath -Force -ErrorAction SilentlyContinue
    Remove-Item $StderrLogPath -Force -ErrorAction SilentlyContinue
} catch {}
