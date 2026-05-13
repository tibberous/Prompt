param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ArgsList
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptRoot

$tokens = @()
if ($ArgsList) { $tokens += $ArgsList }

# Compatibility shim: older project builds used build_qt6_msvc.ps1. Prompt is a
# Python/PySide app now, so this forwards into start.py while preserving the
# common --build / package / --force-rebuild workflow.
if ($tokens.Count -eq 0) {
    $tokens = @('--build')
}

$hasBuild = $false
foreach ($token in $tokens) {
    if (($token -eq '--build') -or ($token -eq 'build') -or ($token -eq '/build')) {
        $hasBuild = $true
        break
    }
}
if (-not $hasBuild) {
    $tokens = @('--build') + $tokens
}

Write-Host "[PromptBuildShim] Running: python start.py $($tokens -join ' ')"
& python start.py @tokens
exit $LASTEXITCODE
