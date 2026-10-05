<#
.SYNOPSIS
  Removes one injected fault and verifies the service is back to normal configuration.
.EXAMPLE
  .\clear-fault.ps1 -Scenario payment-failure
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('high-cpu', 'cart-failure', 'payment-failure', 'product-catalog-failure', 'memory-leak', 'homepage-flood', 'service-down')]
    [string]$Scenario
)
. "$PSScriptRoot\common.ps1"
$sc = Get-Scenario $Scenario

Write-Step "Clear '$Scenario'"
if ($sc.flag) {
    Write-Info "feature flag $($sc.flag) -> off"
    if (-not (Invoke-FlagCtl set $sc.flag off)) { Write-Fail 'flag change could not be verified'; exit 1 }
} else {
    Write-Info "docker start $($sc.container)"
    docker start $sc.container | Out-Null
    $ok = Wait-Until { (docker inspect $sc.container --format '{{.State.Status}}') -eq 'running' } 60 "container $($sc.container) running"
    if (-not $ok) { exit 1 }
}
Write-Ok ("fault cleared at {0}. The alert resolves once the metrics recover (typically 30-90s)." -f (Get-Date -Format 'HH:mm:ss'))
