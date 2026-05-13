$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

$Root = (Get-Location).Path
$StatePath = Join-Path $Root ".autoload-state"

function Write-Log {
    param([string]$Message)
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$stamp] $Message"
}

function New-EmptyState {
    return [pscustomobject]@{
        ZipPath         = ""
        ZipFileMtimeUtc = ""
    }
}

function Normalize-State {
    param($State)

    $empty = New-EmptyState
    if (-not $State) {
        return $empty
    }

    $zipPath = ""
    $zipFileMtimeUtc = ""

    if ($State.PSObject.Properties['ZipPath']) {
        $zipPath = [string]$State.ZipPath
    }

    if ($State.PSObject.Properties['ZipFileMtimeUtc']) {
        $zipFileMtimeUtc = [string]$State.ZipFileMtimeUtc
    } elseif ($State.PSObject.Properties['ZipWriteTicks']) {
        # Legacy state compatibility.
        $zipFileMtimeUtc = [string]$State.ZipWriteTicks
    }

    return [pscustomobject]@{
        ZipPath         = $zipPath
        ZipFileMtimeUtc = $zipFileMtimeUtc
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

    return New-EmptyState
}

function Save-State {
    param($State)
    $normalized = Normalize-State $State
    $normalized | ConvertTo-Json -Depth 5 | Set-Content -Path $StatePath -Encoding UTF8
}

function Get-NewestZip {
    Get-ChildItem -Path $Root -File -Filter *.zip -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
}

function Resolve-StartPy {
    $rootStart = Join-Path $Root "start.py"
    if (Test-Path $rootStart) {
        return (Get-Item $rootStart)
    }

    return Get-ChildItem -Path $Root -Recurse -File -Filter start.py -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
}

function Extract-ZipOverRoot {
    param([string]$ZipPath)

    Write-Log "Extracting zip over CWD: $ZipPath"

    try {
        Expand-Archive -Path $ZipPath -DestinationPath $Root -Force
        return
    } catch {
        Write-Log "Expand-Archive failed, trying .NET extraction..."
    }

    $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("autoload_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temp | Out-Null

    try {
        [System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $temp)

        Get-ChildItem -Path $temp -Recurse -Force | ForEach-Object {
            $relative = $_.FullName.Substring($temp.Length).TrimStart('\\','/')
            if ([string]::IsNullOrWhiteSpace($relative)) { return }

            $dest = Join-Path $Root $relative

            if ($_.PSIsContainer) {
                if (-not (Test-Path $dest)) {
                    New-Item -ItemType Directory -Path $dest -Force | Out-Null
                }
            } else {
                $destDir = Split-Path $dest -Parent
                if (-not (Test-Path $destDir)) {
                    New-Item -ItemType Directory -Path $destDir -Force | Out-Null
                }
                Copy-Item $_.FullName $dest -Force
            }
        }
    } finally {
        Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return [pscustomobject]@{
            FilePath   = $python.Source
            PrefixArgs = @()
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return [pscustomobject]@{
            FilePath   = $py.Source
            PrefixArgs = @("-3")
        }
    }

    throw "Could not find python or py on PATH."
}

function Stop-ProcessSafe {
    param($Proc)

    if (-not $Proc) { return }

    try {
        if (-not $Proc.HasExited) {
            Write-Log "Stopping PID $($Proc.Id)"
            try { $null = $Proc.CloseMainWindow() } catch {}
            Start-Sleep -Milliseconds 800

            if (-not $Proc.HasExited) {
                Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}
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

function Launch-StartPy {
    param(
        $StartFile,
        [ref]$CurrentProc
    )

    if ($CurrentProc.Value -and -not $CurrentProc.Value.HasExited) {
        Stop-ProcessSafe $CurrentProc.Value
    }

    $py = Get-PythonCommand
    $args = @()
    $args += $py.PrefixArgs
    $args += @($StartFile.FullName)

    Write-Log "Launching: $($py.FilePath) $($args -join ' ')"
    $CurrentProc.Value = Start-Process -FilePath $py.FilePath `
                                       -ArgumentList $args `
                                       -WorkingDirectory $StartFile.DirectoryName `
                                       -PassThru
}

$state = Load-State
$currentProc = $null
$lastLaunchedStartPath = ""
$lastLaunchedStartMtimeUtc = ""
$exitIdleNoticeSent = $false

Write-Log "Autoload watcher started in: $Root"
Write-Host ""
Write-Host "R = rerun start.py"
Write-Host "Q = quit watcher"
Write-Host ""

while ($true) {
    try {
        $hotkeys = Read-Hotkeys

        if ($hotkeys.Quit) {
            Write-Log "Quit requested."
            if ($currentProc -and -not $currentProc.HasExited) {
                Stop-ProcessSafe $currentProc
            }
            break
        }

        $manualRestart = [bool]$hotkeys.Restart
        if ($manualRestart) {
            Write-Log "Manual restart requested."
        }

        $zip = Get-NewestZip
        $zipChanged = $false

        if ($zip) {
            $zipMtimeUtc = $zip.LastWriteTimeUtc.ToString("o")
            if ($state.ZipPath -ne $zip.FullName -or $state.ZipFileMtimeUtc -ne $zipMtimeUtc) {
                Extract-ZipOverRoot -ZipPath $zip.FullName
                $state.ZipPath = $zip.FullName
                $state.ZipFileMtimeUtc = $zipMtimeUtc
                $zipChanged = $true
                Save-State $state
            }
        }

        $startFile = Resolve-StartPy
        if (-not $startFile) {
            Write-Log "No start.py found yet."
            Start-Sleep -Milliseconds 500
            continue
        }

        $startMtimeUtc = $startFile.LastWriteTimeUtc.ToString("o")
        $startVersionChanged = (
            $lastLaunchedStartPath -ne $startFile.FullName -or
            $lastLaunchedStartMtimeUtc -ne $startMtimeUtc
        )

        $shouldLaunch = $false

        if ($manualRestart) {
            $shouldLaunch = $true
        } elseif ([string]::IsNullOrWhiteSpace($lastLaunchedStartPath)) {
            # First launch for this watcher session.
            $shouldLaunch = $true
        } elseif ($zipChanged -or $startVersionChanged) {
            # Relaunch only when the extracted bundle changed, start.py filemtime changed,
            # or the user explicitly pressed R.
            $shouldLaunch = $true
        }

        if ($shouldLaunch) {
            Launch-StartPy -StartFile $startFile -CurrentProc ([ref]$currentProc)
            $lastLaunchedStartPath = $startFile.FullName
            $lastLaunchedStartMtimeUtc = $startMtimeUtc
            $exitIdleNoticeSent = $false
            continue
        }

        if ($currentProc -and $currentProc.HasExited -and -not $exitIdleNoticeSent) {
            Write-Log "start.py exited. Waiting for filemtime change or R to rerun."
            $exitIdleNoticeSent = $true
        }

    } catch {
        Write-Log ("ERROR: " + $_.Exception.Message)
    }

    Start-Sleep -Milliseconds 500
}
