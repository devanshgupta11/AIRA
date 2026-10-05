<#
.SYNOPSIS
  Starts the whole AIRA mid-semester stack and waits until every part is healthy.
  Docker Desktop -> OpenTelemetry Demo + Alertmanager -> Ollama (+ model warm-up) -> AIRA backend.
#>
. "$PSScriptRoot\common.ps1"
$total = [Diagnostics.Stopwatch]::StartNew()
$failed = $false

Write-Step "Docker engine"
if (-not (Test-DockerEngine)) {
    $dd = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    Write-Info "Docker engine not running - starting Docker Desktop"
    Start-Process $dd
    if (-not (Wait-Until { Test-DockerEngine } 240 'Docker engine running' 5)) { throw 'Docker Desktop did not start' }
} else { Write-Ok 'Docker engine running' }

Write-Step "OpenTelemetry Demo + Alertmanager (docker compose up -d)"
Invoke-Compose up -d --remove-orphans | Select-String -Pattern 'Error|error' | ForEach-Object { Write-Warn2 $_ }
$null = Wait-Until { Test-Http 'http://localhost:8080/' } 240 'Astronomy Shop responds on :8080'
$null = Wait-Until { Test-Http 'http://localhost:9090/-/ready' } 120 'Prometheus ready on :9090'
$null = Wait-Until { Test-Http 'http://localhost:9093/-/ready' } 120 'Alertmanager ready on :9093'
$null = Wait-Until { Test-Http 'http://localhost:8080/feature/api/read' } 120 'flagd-ui API responds'

# The collector's docker_stats receiver misses containers that start at the same moment as the
# collector (seen: cart, payment, flagd-ui had no container metrics). Restarting it once all
# containers run makes it discover every container.
Write-Step "Collector restart (container metrics discovery)"
docker restart otel-collector | Out-Null
# Wait past the 15s window so only samples sent by the restarted collector count.
Start-Sleep -Seconds 20
$need = @('cart', 'payment', 'ad', 'recommendation', 'product-catalog', 'frontend')
$ok = Wait-Until {
    try {
        $names = (Invoke-RestMethod 'http://localhost:9090/api/v1/query?query=count%20by%20(container_name)%20(count_over_time(container_memory_usage_total_bytes%5B15s%5D))' -TimeoutSec 5).data.result.metric.container_name
        -not ($need | Where-Object { $names -notcontains $_ })
    } catch { $false }
} 120 "container metrics for $($need -join ', ')" 5
if (-not $ok) { $failed = $true }

$bad = docker ps -a --filter 'label=com.docker.compose.project=opentelemetry-demo' --format '{{.Names}} {{.Status}}' |
       Where-Object { $_ -match 'Exited|Restarting|unhealthy' }
$running = (docker ps --filter 'label=com.docker.compose.project=opentelemetry-demo' --format '{{.Names}}' | Measure-Object).Count
if ($bad) { $bad | ForEach-Object { Write-Warn2 "container: $_" }; $failed = $true } else { Write-Ok "$running containers running, none exited/unhealthy" }

Write-Step "Ollama (models in $OllamaModels)"
if (-not (Test-Http 'http://localhost:11434/api/version')) {
    $env:OLLAMA_MODELS = $OllamaModels
    $env:OLLAMA_KEEP_ALIVE = '60m'
    Start-Process -FilePath $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'ollama.out.log') -RedirectStandardError (Join-Path $LogDir 'ollama.log')
    if (-not (Wait-Until { Test-Http 'http://localhost:11434/api/version' } 60 'Ollama server up on :11434')) { $failed = $true }
} else { Write-Ok 'Ollama server already running on :11434' }

Write-Info "Warming up $OllamaModel (first load into GPU memory can take ~30s)"
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $body = @{ model = $OllamaModel; prompt = 'Reply with the single word: ready'; stream = $false; keep_alive = '60m' } | ConvertTo-Json
    $r = Invoke-RestMethod -Method Post 'http://localhost:11434/api/generate' -Body $body -ContentType 'application/json' -TimeoutSec 180
    Write-Ok ("model warm: replied '{0}' in {1:N1}s" -f $r.response.Trim(), $sw.Elapsed.TotalSeconds)
    $ps = (Invoke-RestMethod 'http://localhost:11434/api/ps').models | Where-Object name -eq $OllamaModel
    if ($ps) { Write-Info ("loaded: {0:N1} GB, {1:N1} GB in GPU memory" -f ($ps.size / 1GB), ($ps.size_vram / 1GB)) }
} catch { Write-Fail "model warm-up failed: $_"; $failed = $true }

Write-Step "AIRA backend (FastAPI on :8000)"
if (-not (Get-BackendProcess)) {
    Start-Process -FilePath $VenvPython -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
        -WorkingDirectory $BackendDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'uvicorn.out.log') -RedirectStandardError (Join-Path $LogDir 'uvicorn.err.log')
} else { Write-Info 'backend process already running' }
if (Wait-Until { Test-Http 'http://localhost:8000/health' } 90 'backend /health responds') {
    $h = Invoke-RestMethod 'http://localhost:8000/health'
    foreach ($k in $h.checks.PSObject.Properties) {
        if ($k.Value.ok) { Write-Ok "backend -> $($k.Name): $($k.Value.detail)" } else { Write-Fail "backend -> $($k.Name): $($k.Value.detail)"; $failed = $true }
    }
} else { $failed = $true }

Write-Step "Webhook path (Alertmanager container -> host.docker.internal:8000)"
$probe = docker exec alertmanager wget -qO- -T 5 http://host.docker.internal:8000/health 2>&1
if ("$probe" -match '"ok"') { Write-Ok 'Alertmanager can reach the AIRA backend' } else { Write-Fail "Alertmanager cannot reach the backend: $probe"; $failed = $true }

Write-Step ("URLs (total start time {0:N0}s)" -f $total.Elapsed.TotalSeconds)
$Urls.GetEnumerator() | ForEach-Object { Write-Host ("  {0,-18} {1}" -f $_.Key, $_.Value) }
if ($failed) { Write-Host "`nStarted WITH PROBLEMS - see [FAIL]/[WARN] lines above." -ForegroundColor Red; exit 1 }
Write-Host "`nAIRA stack is up and healthy." -ForegroundColor Green
