<#
.SYNOPSIS
  Stops the AIRA backend, the OpenTelemetry Demo + Alertmanager containers, and Ollama.
  Prometheus history (named volume) and the incident database are kept.
#>
. "$PSScriptRoot\common.ps1"

Write-Step "AIRA backend"
$procs = Get-BackendProcess
if ($procs) {
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Ok "stopped backend (pid $($_.ProcessId))" }
} else { Write-Info 'backend was not running' }

Write-Step "Feature flags back to off (so the next start is clean)"
if (Test-Http 'http://localhost:8080/feature/api/read') {
    if (-not (Invoke-FlagCtl reset-all)) { Write-Warn2 'could not reset flags' }
} else { Write-Info 'flagd-ui not reachable - skipped' }

Write-Step "Containers (docker compose down; volumes kept)"
if (Test-DockerEngine) {
    Invoke-Compose down --remove-orphans | Select-String -Pattern 'Removed|Stopped|Error' | Select-Object -Last 3 | ForEach-Object { Write-Info "$_" }
    $left = docker ps --filter 'label=com.docker.compose.project=opentelemetry-demo' --format '{{.Names}}'
    if ($left) { Write-Fail "still running: $($left -join ', ')" } else { Write-Ok 'all demo containers stopped' }
} else { Write-Info 'Docker engine not running - nothing to stop' }

Write-Step "Ollama"
$ol = Get-Process -Name 'ollama', 'ollama app' -ErrorAction SilentlyContinue
if ($ol) { $ol | Stop-Process -Force; Write-Ok "stopped Ollama ($($ol.Count) process(es))" } else { Write-Info 'Ollama was not running' }

Write-Host "`nAIRA stack stopped. Docker Desktop itself is left running." -ForegroundColor Green
