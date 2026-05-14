$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$Root = "C:\prompt"
$Dist = Join-Path $Root "dist"
$StatusFile = Join-Path $Root ".orchestrator-status.json"
$WatcherScript = Join-Path $Root "auto_build_installers.ps1"
$LogFile = Join-Path $Root ".orchestrator.log"
$TargetCount = 25
$MaxIterations = 4

function Log {
    param([string]$Msg)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Count-Installers {
    if (-not (Test-Path $Dist)) { return 0 }
    return (Get-ChildItem -Path $Dist -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^PromptSetup-.*\.(exe|msi|msix)$' }).Count
}

function Write-Status {
    param([string]$Phase, [int]$Count, [int]$Iter, [int]$ChildPid)
    $obj = [ordered]@{
        phase     = $Phase
        count     = $Count
        target    = $TargetCount
        iteration = $Iter
        pid       = $ChildPid
        timestamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
        done      = ($Count -ge $TargetCount)
    }
    $json = $obj | ConvertTo-Json -Compress
    Set-Content -Path $StatusFile -Value $json -Encoding UTF8
}

Set-Content -Path $LogFile -Value $null -Encoding UTF8
Log "Orchestrator starting. Target=$TargetCount MaxIterations=$MaxIterations"

# Wait for any pre-existing python build process (the watcher's child) to exit.
# We look for python.exe processes running from C:\Prompt context — they spawn
# from the watcher and their cwd or commandline mentions Prompt.
function Get-ActiveBuildPids {
    $pids = @()
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            $cmd = [string]$p.CommandLine
            # Must be a Prompt-project process: cmdline mentions \Prompt\ (case-insensitive)
            # OR uses --force-rebuild or run_prompt_release. Avoids matching C:\247ch.at\start.py
            # which also contains start.py and would otherwise be a false positive.
            if ($cmd -match "(?i)\\Prompt\\" -or $cmd -match "run_prompt_release" -or $cmd -match "--force-rebuild") {
                $pids += [int]$p.ProcessId
            }
        }
    } catch {}
    return $pids
}

function Wait-ForBuildIdle {
    param([int]$MaxSeconds = 7200)
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        $active = Get-ActiveBuildPids
        if (-not $active -or $active.Count -eq 0) { return $true }
        $count = Count-Installers
        Write-Status -Phase "waiting" -Count $count -Iter 0 -ChildPid ($active[0])
        Log "Build still running pids=$($active -join ',') installers=$count"
        Start-Sleep -Seconds 30
    }
    return $false
}

for ($iter = 0; $iter -le $MaxIterations; $iter++) {
    $count = Count-Installers
    Log "Iteration $iter start: installer count = $count"
    Write-Status -Phase "checking" -Count $count -Iter $iter -ChildPid 0

    if ($count -ge $TargetCount) {
        Log "Target reached: $count >= $TargetCount. Done."
        Write-Status -Phase "complete" -Count $count -Iter $iter -ChildPid 0
        break
    }

    # If a build is already in flight, wait for it.
    $active = Get-ActiveBuildPids
    if ($active -and $active.Count -gt 0) {
        Log "Detected in-flight build pids=$($active -join ',') — waiting before launching a new one"
        Write-Status -Phase "waiting-existing" -Count $count -Iter $iter -ChildPid ($active[0])
        Wait-ForBuildIdle -MaxSeconds 7200 | Out-Null
        Start-Sleep -Seconds 8  # let log writers flush
        $count = Count-Installers
        Log "After in-flight build finished: installers=$count"
        if ($count -ge $TargetCount) {
            Write-Status -Phase "complete" -Count $count -Iter $iter -ChildPid 0
            break
        }
    }

    if ($iter -ge $MaxIterations) {
        Log "Max iterations reached without target. Stopping."
        Write-Status -Phase "max-iterations" -Count $count -Iter $iter -ChildPid 0
        break
    }

    Log "Launching watcher: $WatcherScript"
    Write-Status -Phase "launching" -Count $count -Iter $iter -ChildPid 0

    # Touch start.py so the watcher sees a change and triggers a rebuild.
    $startPy = Join-Path $Root "start.py"
    if (Test-Path $startPy) { (Get-Item $startPy).LastWriteTime = Get-Date }

    # Invoke the watcher in the foreground from THIS orchestrator process so we
    # know exactly when it exits.
    $watcherArgs = @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $WatcherScript)
    Log "Starting: pwsh.exe $($watcherArgs -join ' ')"
    $proc = Start-Process -FilePath "pwsh.exe" -ArgumentList $watcherArgs -WorkingDirectory $Root -PassThru -NoNewWindow
    Write-Status -Phase "running" -Count (Count-Installers) -Iter $iter -ChildPid $proc.Id
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 30
        $cur = Count-Installers
        Write-Status -Phase "running" -Count $cur -Iter $iter -ChildPid $proc.Id
        Log "Iteration $iter running (watcher pid=$($proc.Id)): installers=$cur"
        if ($cur -ge $TargetCount) {
            Log "Target reached during iteration $iter — leaving watcher to finish naturally"
            # Don't kill — let it exit cleanly
        }
    }
    Log "Iteration ${iter}: watcher exited code=$($proc.ExitCode). Counting..."
    Start-Sleep -Seconds 8
    $count = Count-Installers
    Log "Iteration $iter end: installers=$count"

    # Wait for any spawned build child to finish before counting next loop.
    Wait-ForBuildIdle -MaxSeconds 1200 | Out-Null
}

$final = Count-Installers
Write-Status -Phase ($(if ($final -ge $TargetCount) { "complete" } else { "failed" })) -Count $final -Iter -1 -ChildPid 0
Log "Orchestrator finished. final=$final target=$TargetCount"
