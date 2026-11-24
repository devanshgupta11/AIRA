<#
.SYNOPSIS
  Keeps every part of AIRA reachable: checks each one every 15 seconds and restarts whatever stops
  answering.

.DESCRIPTION
  start-all.ps1 starts this in the background; stop-all.ps1 stops it first, so it never restarts
  something that was stopped on purpose. Every check and every repair is written to
  logs\watchdog.log, and the last lines of a process's error log are kept in logs\crash\ before
  it is restarted, so the reason it died is not overwritten by the restart.

  What it keeps up, and how:
    Docker engine        starts Docker Desktop if it is not running
    monitoring stack     prometheus, alertmanager, otel-collector, jaeger, opensearch, grafana,
                         flagd, flagd-ui, frontend-proxy: started again if stopped, restarted if
                         running but not answering
    Ollama   :11434      restarted if it stops answering
    RCA      :8001       restarted if the process is gone, or its agent card stops answering
    MCP      :8002       restarted if the process is gone, or tools/list stops answering
    backend  :8000       restarted if the process is gone, or /health stops answering
    tracing, memory      read from the backend's own /health; repaired when they stay down

  What it deliberately does NOT restart: the shop's application containers (cart, payment,
  recommendation, ...). Those are the system AIRA monitors. When one of them fails, that is an
  incident for AIRA to detect and investigate, and the service-down fault stops one on purpose.

  A process that is gone is restarted at once. One that is running but not answering is given a
  minute first, because an agent can be busy, and killing it mid-investigation would fail that
  investigation for nothing. After a restart each part gets time to start up before it is checked
  again, and a part that keeps failing is retried less often instead of in a tight loop.

    .\watchdog.ps1          run in this window (Ctrl+C to stop)
    .\watchdog.ps1 -Once    check everything once, repair what is down, and exit
#>
param([int]$IntervalSec = 15, [switch]$Once)
. "$PSScriptRoot\common.ps1"

# ---------------------------------------------------------------- one copy only

$mutex = New-Object System.Threading.Mutex($false, 'Global\AIRA-watchdog')
try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { Write-Host 'The AIRA watchdog is already running.'; exit 0 }

# ---------------------------------------------------------------- logging

$WatchLog = Join-Path $LogDir 'watchdog.log'
$CrashDir = Join-Path $LogDir 'crash'
New-Item -ItemType Directory -Force $CrashDir | Out-Null

function Write-Log([string]$Level, [string]$Message) {
    $line = '{0} {1,-5} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try {
        if ((Test-Path $WatchLog) -and (Get-Item $WatchLog).Length -gt 5MB) {
            Move-Item $WatchLog (Join-Path $LogDir 'watchdog.1.log') -Force
        }
        Add-Content -Path $WatchLog -Value $line -Encoding UTF8
    } catch {}
    Write-Host $line
}

# Keeps the end of a process's error log before the restart overwrites it.
function Save-CrashLog([string]$Name, [string]$ErrLog) {
    $src = Join-Path $LogDir $ErrLog
    if (-not (Test-Path $src)) { return }
    $dst = Join-Path $CrashDir ('{0}-{1}.log' -f $Name, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    try {
        Get-Content $src -Tail 200 -ErrorAction Stop | Set-Content $dst -Encoding UTF8
        Write-Log 'INFO' "  kept the last 200 lines of $ErrLog in logs\crash\$(Split-Path $dst -Leaf)"
    } catch {}
    # Only the 30 most recent are kept, so this can never fill the disk.
    Get-ChildItem $CrashDir -Filter '*.log' | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- probes
#
# curl.exe rather than Invoke-WebRequest: PowerShell 5.1's web client timed out on the MCP server's
# POST while curl answered in 70 ms, and curl's -m is a hard limit on the whole request.

function Test-Url([string]$Url, [int]$TimeoutSec = 5) {
    $code = & curl.exe -s -o NUL -w '%{http_code}' -m $TimeoutSec $Url 2>$null
    return ("$code" -match '^[23]')
}

$McpProbeBody = Join-Path $env:TEMP 'aira-watchdog-mcp-probe.json'
'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | Set-Content $McpProbeBody -Encoding ASCII

# The tools server runs stateless, so one request is a complete check: no session is left open.
function Test-McpServer {
    $out = & curl.exe -s -m 5 -X POST 'http://127.0.0.1:8002/mcp' -H 'Content-Type: application/json' `
        -H 'Accept: application/json, text/event-stream' --data-binary "@$McpProbeBody" 2>$null
    return ("$out" -match 'collect_prometheus_evidence')
}

function Get-Json([string]$Url, [int]$TimeoutSec = 20) {
    $out = & curl.exe -s -m $TimeoutSec $Url 2>$null
    if (-not $out) { return $null }
    try { return ("$out" | ConvertFrom-Json) } catch { return $null }
}

# True while an incident is being investigated. Repairs that are only for tracing or memory wait
# for this to be false, so they never interrupt an investigation.
function Test-Investigating {
    $list = Get-Json 'http://localhost:8000/incidents?limit=50' 10
    if ($null -eq $list) { return $true }   # unknown counts as busy
    return [bool](@($list) | Where-Object { $_.agent_status -in @('pending', 'running') })
}

# ---------------------------------------------------------------- bookkeeping per part

$fails = @{}        # consecutive failed checks
$quietUntil = @{}   # no checks until this time (a restart is still starting up)
$restarts = @{}     # times of recent restarts, for back-off

function Test-Quiet([string]$Name) {
    return ($quietUntil.ContainsKey($Name) -and (Get-Date) -lt $quietUntil[$Name])
}

# Records a failed check; true once it has failed $Threshold times in a row.
function Add-Failure([string]$Name, [int]$Threshold) {
    if (-not $fails.ContainsKey($Name)) { $fails[$Name] = 0 }
    $fails[$Name]++
    return ($fails[$Name] -ge $Threshold)
}

function Clear-Failure([string]$Name) {
    if ($fails.ContainsKey($Name) -and $fails[$Name] -gt 0) {
        Write-Log 'OK' "$Name answering again"
    }
    $fails[$Name] = 0
}

# After a repair: wait $GraceSec before judging it, longer if it keeps needing repairs.
function Set-Repaired([string]$Name, [int]$GraceSec) {
    $now = Get-Date
    $recent = @(@($restarts[$Name]) | Where-Object { $_ -and $_ -gt $now.AddMinutes(-15) }) + $now
    $restarts[$Name] = $recent
    $grace = $GraceSec
    if ($recent.Count -ge 3) {
        $grace = [Math]::Min(600, $GraceSec * ($recent.Count - 1))
        Write-Log 'ERROR' "$Name has needed $($recent.Count) restarts in 15 minutes; retrying every $grace s. See logs\crash\ for why it keeps stopping."
    }
    $quietUntil[$Name] = $now.AddSeconds($grace)
    # 1 rather than 0, so the first good check after the repair is logged as "answering again".
    $fails[$Name] = 1
}

# ---------------------------------------------------------------- the checks

$InfraContainers = @('prometheus', 'alertmanager', 'otel-collector', 'jaeger', 'opensearch',
                     'grafana', 'flagd', 'flagd-ui', 'frontend-proxy')

function Watch-Docker {
    if (Test-Quiet 'docker') { return $false }
    if (Test-DockerEngine) { Clear-Failure 'docker'; return $true }
    if (-not (Add-Failure 'docker' 2)) { return $false }
    if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        Write-Log 'FIX' 'Docker engine not running: starting Docker Desktop'
        Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    } else {
        Write-Log 'WARN' 'Docker Desktop is running but its engine is not answering yet; waiting'
    }
    Set-Repaired 'docker' 240
    return $false
}

function Watch-Containers {
    $state = @{}
    docker ps -a --format '{{.Names}}|{{.State}}' 2>$null | ForEach-Object {
        $p = "$_".Split('|'); if ($p.Count -eq 2) { $state[$p[0]] = $p[1] }
    }
    foreach ($c in $InfraContainers) {
        $key = "container $c"
        if (Test-Quiet $key) { continue }
        if ($state[$c] -eq 'running') { Clear-Failure $key; continue }
        if (-not (Add-Failure $key 1)) { continue }
        if ($state.ContainsKey($c)) {
            Write-Log 'FIX' "$c container is '$($state[$c])': starting it"
            docker start $c 2>&1 | Out-Null
        } else {
            Write-Log 'FIX' "$c container does not exist: creating it with docker compose"
            Invoke-Compose up -d $c -Tolerant | Out-Null
        }
        Set-Repaired $key 60
    }
    # Running is not the same as answering. These two are the ones AIRA queries directly.
    foreach ($svc in @(@('prometheus', 'http://localhost:9090/-/ready'),
                       @('alertmanager', 'http://localhost:9093/-/ready'))) {
        $key = "$($svc[0]) http"
        if ($state[$svc[0]] -ne 'running' -or (Test-Quiet $key)) { continue }
        if (Test-Url $svc[1] 5) { Clear-Failure $key; continue }
        if (-not (Add-Failure $key 4)) { continue }
        Write-Log 'FIX' "$($svc[0]) is running but has not answered for a minute: restarting the container"
        docker restart $svc[0] 2>&1 | Out-Null
        Set-Repaired $key 90
    }
}

function Watch-Ollama {
    $name = 'ollama'
    if (Test-Quiet $name) { return }
    if (Test-Url 'http://localhost:11434/api/version' 5) { Clear-Failure $name; return }
    # /api/version answers even while a model is generating, so two misses mean it is really down.
    if (-not (Add-Failure $name 2)) { return }
    $p = Get-Process -Name 'ollama' -ErrorAction SilentlyContinue
    if ($p) {
        Write-Log 'FIX' 'Ollama is running but not answering on :11434: restarting it'
        $p | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    } else {
        Write-Log 'FIX' 'Ollama is not running: starting it'
    }
    Save-CrashLog 'ollama' 'ollama.log'
    Start-OllamaServer
    Set-Repaired $name 60
}

# One shape for the three Python services: gone -> restart now; not answering -> restart after
# $Threshold misses; then wait $GraceSec for it to start before judging it again.
function Watch-PythonService([string]$Name, [scriptblock]$GetProcess, [scriptblock]$Probe,
                             [scriptblock]$Start, [string]$ErrLog, [int]$Threshold, [int]$GraceSec) {
    if (Test-Quiet $Name) { return }
    $procs = @(& $GetProcess)
    if ($procs.Count -gt 0 -and (& $Probe)) { Clear-Failure $Name; return }

    if ($procs.Count -eq 0) {
        Write-Log 'FIX' "$Name process is not running: starting it"
    } else {
        if (-not (Add-Failure $Name $Threshold)) {
            Write-Log 'WARN' "$Name not answering ($($fails[$Name]) of $Threshold checks before a restart)"
            return
        }
        Write-Log 'FIX' "$Name has not answered for $($Threshold * $IntervalSec) s: restarting it"
        $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
    Save-CrashLog $Name $ErrLog
    & $Start
    Set-Repaired $Name $GraceSec
}

function Watch-Rca {
    Watch-PythonService 'RCA agent' { Get-RcaProcess } `
        { Test-Url 'http://127.0.0.1:8001/.well-known/agent-card.json' 5 } { Start-RcaAgent } `
        'rca.err.log' 4 180
}

function Watch-Mcp {
    Watch-PythonService 'MCP tools' { Get-McpToolsProcess } { Test-McpServer } { Start-McpTools } `
        'mcp-tools.err.log' 4 90
}

function Watch-Backend {
    Watch-PythonService 'backend' { Get-BackendProcess } `
        { Test-Url 'http://localhost:8000/health' 20 } { Start-Backend } `
        'uvicorn.err.log' 4 180
}

# Tracing and memory live inside the backend, so they are read from its /health. Repairs here wait
# for any running investigation to finish first.
function Watch-BackendParts {
    if ((Test-Quiet 'backend') -or ((Test-Quiet 'tracing') -and (Test-Quiet 'memory'))) { return }
    $h = Get-Json 'http://localhost:8000/health' 20
    if ($null -eq $h -or $null -eq $h.checks) { return }

    $t = $h.checks.tracing
    if ($t -and -not (Test-Quiet 'tracing')) {
        if ($t.detail -like '*exporting to the collector*') { Clear-Failure 'tracing' }
        elseif (Add-Failure 'tracing' 8) {
            if ($t.detail -like '*unreachable*') {
                Write-Log 'FIX' "tracing: $($t.detail); restarting the otel-collector container"
                docker restart otel-collector 2>&1 | Out-Null
                Set-Repaired 'tracing' 120
            } elseif (-not (Test-Investigating)) {
                # Tracing is set up once when the backend starts; if the collector was missing then,
                # only a backend restart turns it on.
                Write-Log 'FIX' "tracing: $($t.detail); restarting the backend (no investigation running)"
                Get-BackendProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                Start-Sleep -Seconds 2
                Save-CrashLog 'backend' 'uvicorn.err.log'
                Start-Backend
                Set-Repaired 'tracing' 180
                Set-Repaired 'backend' 180
                return
            } else {
                Write-Log 'WARN' "tracing: $($t.detail); waiting for the running investigation to finish before restarting the backend"
            }
        }
    }

    $m = $h.checks.memory
    if ($m -and -not (Test-Quiet 'memory')) {
        if ($m.ok) { Clear-Failure 'memory' }
        elseif (Add-Failure 'memory' 8) {
            if (-not (Test-Investigating)) {
                Write-Log 'FIX' "memory: $($m.detail); restarting the backend (no investigation running)"
                Get-BackendProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
                Start-Sleep -Seconds 2
                Save-CrashLog 'backend' 'uvicorn.err.log'
                Start-Backend
                Set-Repaired 'memory' 180
                Set-Repaired 'backend' 180
            } else {
                Write-Log 'WARN' "memory: $($m.detail); waiting for the running investigation to finish before restarting the backend"
            }
        }
    }
}

# ---------------------------------------------------------------- the loop

Write-Log 'START' "watchdog started (pid $PID), checking every $IntervalSec s"
try {
    while ($true) {
        try {
            if (Watch-Docker) { Watch-Containers }
            Watch-Ollama
            Watch-Mcp
            Watch-Rca
            Watch-Backend
            Watch-BackendParts
        } catch {
            # A failure in the watchdog itself must never stop it watching.
            Write-Log 'ERROR' "watchdog check failed: $($_.Exception.Message)"
        }
        if ($Once) { break }
        Start-Sleep -Seconds $IntervalSec
    }
} finally {
    Write-Log 'STOP' "watchdog stopped (pid $PID)"
    try { $mutex.ReleaseMutex() } catch {}
}
