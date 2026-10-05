<#
.SYNOPSIS
  Injects one real fault into the OpenTelemetry Demo and verifies it took effect.
.PARAMETER Scenario
  high-cpu | cart-failure | payment-failure | product-catalog-failure | memory-leak | homepage-flood | service-down
.EXAMPLE
  .\inject-fault.ps1 -Scenario payment-failure
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('high-cpu', 'cart-failure', 'payment-failure', 'product-catalog-failure', 'memory-leak', 'homepage-flood', 'service-down')]
    [string]$Scenario
)
. "$PSScriptRoot\common.ps1"
$sc = Get-Scenario $Scenario

Write-Step "Inject '$Scenario': $($sc.description)"
if ($sc.flag) {
    Write-Info "feature flag $($sc.flag) -> $($sc.on)  (via flagd-ui API, verified with flagd)"
    if (-not (Invoke-FlagCtl set $sc.flag $sc.on)) { Write-Fail 'flag change could not be verified'; exit 1 }
} else {
    Write-Info "docker stop $($sc.container)"
    docker stop $sc.container | Out-Null
    $st = docker inspect $sc.container --format '{{.State.Status}}'
    if ($st -eq 'exited') { Write-Ok "container $($sc.container) is $st" } else { Write-Fail "container $($sc.container) is $st"; exit 1 }
}
Write-Ok ("fault active since {0}" -f (Get-Date -Format 'HH:mm:ss'))

if ($sc.expect -and $sc.expect.Count) {
    $exp = ($sc.expect | ForEach-Object { "$($_[0]) for $($_[1])" }) -join ', '
    Write-Info "Expected alert: $exp (measured time-to-fire: see docs/midsem-progress.md)"
} else {
    Write-Info 'No AIRA alert rule targets this scenario yet; watch Grafana / the shop for its effect.'
}
Write-Info "Watch: http://localhost:9090/alerts  ->  http://localhost:9093  ->  http://localhost:8000"
Write-Info "Undo:  .\clear-fault.ps1 -Scenario $Scenario"
