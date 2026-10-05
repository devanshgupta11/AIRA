<#
.SYNOPSIS
  Resets every feature flag to off and restarts any scenario container that is stopped.
#>
. "$PSScriptRoot\common.ps1"
$failed = $false

Write-Step "Feature flags -> off"
if (-not (Invoke-FlagCtl reset-all)) { Write-Fail 'could not reset all flags'; $failed = $true }

Write-Step "Scenario containers"
foreach ($p in (Get-Scenarios).PSObject.Properties) {
    $c = $p.Value.container
    if (-not $c) { continue }
    $st = docker inspect $c --format '{{.State.Status}}' 2>$null
    if ($st -eq 'running') { Write-Ok "$c running"; continue }
    Write-Info "$c is $st - starting"
    docker start $c | Out-Null
    if (-not (Wait-Until { (docker inspect $c --format '{{.State.Status}}') -eq 'running' } 60 "$c running")) { $failed = $true }
}

if ($failed) { exit 1 }
Write-Host "`nAll faults cleared." -ForegroundColor Green
