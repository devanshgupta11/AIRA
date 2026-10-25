# Shared helpers for the AIRA PowerShell scripts. Dot-source it: . "$PSScriptRoot\common.ps1"

# 'Continue', not 'Stop': in Windows PowerShell 5.1, native tools that print progress on stderr
# (docker compose does) would otherwise abort the script. Every step checks its own result instead.
$ErrorActionPreference = 'Continue'

$AiraRoot     = Split-Path -Parent $PSScriptRoot
$DemoDir      = Join-Path $AiraRoot 'opentelemetry-demo'
$BackendDir   = Join-Path $AiraRoot 'backend'
$LogDir       = Join-Path $AiraRoot 'logs'
$VenvPython   = Join-Path $BackendDir '.venv\Scripts\python.exe'
$FlagCtl      = Join-Path $PSScriptRoot 'flagctl.py'
$ScenarioFile = Join-Path $PSScriptRoot 'scenarios.json'
$OllamaExe    = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$OllamaModels = Join-Path $AiraRoot 'ollama-models'
$OllamaModel  = 'qwen2.5:7b-instruct'

$Urls = [ordered]@{
    'AIRA console'       = 'http://localhost:8000/'
    'Astronomy Shop'     = 'http://localhost:8080/'
    'Grafana'            = 'http://localhost:8080/grafana/'
    'Jaeger'             = 'http://localhost:8080/jaeger/ui/'
    'Feature flags'      = 'http://localhost:8080/feature/'
    'Load generator'     = 'http://localhost:8080/loadgen/'
    'Prometheus'         = 'http://localhost:9090/'
    'Prometheus alerts'  = 'http://localhost:9090/alerts'
    'Alertmanager'       = 'http://localhost:9093/'
    'RCA agent card'     = 'http://127.0.0.1:8001/.well-known/agent-card.json'
    'MCP tools server'   = 'http://127.0.0.1:8002/mcp'
}

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "         $msg" }

# Runs docker compose with exactly the upstream "make start" files plus the AIRA layer.
# Deliberately a simple function using $args: an advanced function (param + [Parameter]) would
# swallow "-d" as its own -Debug common parameter and run compose attached.
function Invoke-Compose {
    $ComposeArgs = @($args | Where-Object { $_ -ne '-Tolerant' })
    $tolerant = @($args) -contains '-Tolerant'
    Push-Location $DemoDir
    try {
        $base = @('compose', '--env-file', '.env', '--env-file', '.env.override', '--env-file', '..\infra\aira.env',
                  '-f', 'compose.yaml', '-f', 'compose.full.yaml', '-f', 'compose.observability.yaml',
                  '-f', 'compose.extras.yaml', '-f', '..\infra\compose.aira.yaml')
        & docker @base @ComposeArgs 2>&1 | ForEach-Object { "$_" }
        if ($LASTEXITCODE -ne 0) {
            $msg = "docker compose $($ComposeArgs -join ' ') exited $LASTEXITCODE"
            if ($tolerant) { Write-Warn2 "$msg - continuing; the health checks below decide" }
            else { throw "$msg" }
        }
    } finally { Pop-Location }
}

function Test-Http($url, [int]$timeoutSec = 5) {
    try { $r = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec $timeoutSec; return $r.StatusCode -lt 400 }
    catch { return $false }
}

# Polls a script block until it returns $true or the timeout expires.
function Wait-Until([scriptblock]$Check, [int]$TimeoutSec, [string]$What, [int]$IntervalSec = 2) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if (& $Check) { Write-Ok ("{0} ({1:N0}s)" -f $What, $sw.Elapsed.TotalSeconds); return $true }
        Start-Sleep -Seconds $IntervalSec
    }
    Write-Fail "$What - not ready after ${TimeoutSec}s"
    return $false
}

function Test-DockerEngine {
    docker info --format '{{.ServerVersion}}' *> $null
    return $LASTEXITCODE -eq 0
}

function Get-Scenarios { Get-Content $ScenarioFile -Raw | ConvertFrom-Json }

function Get-Scenario($name) {
    $all = Get-Scenarios
    $sc = $all.$name
    if (-not $sc) {
        $names = ($all.PSObject.Properties.Name) -join ', '
        throw "Unknown scenario '$name'. Valid scenarios: $names"
    }
    return $sc
}

function Invoke-FlagCtl {
    & $VenvPython $FlagCtl @args 2>&1 | ForEach-Object { Write-Info "$_" }
    return $LASTEXITCODE -eq 0
}

function Get-BackendProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' }
}

# The RCA agent runs as its own process so the backend reaches it over A2A rather than by import -
# a real protocol boundary, which is the point of exposing it that way.
function Get-RcaProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*uvicorn*app.agents.rca_server:app*' }
}

# Same reasoning for the evidence tools: they are served over MCP, so they are a service with a
# declared interface rather than a Python import.
function Get-McpToolsProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*uvicorn*app.mcp_servers.tools_server:app*' }
}

# MCP has no plain GET health endpoint - the protocol needs an initialize handshake - so health is
# asked through the client, which also proves the tools are actually advertised. The query lives in
# scripts\mcp_tools_list.py rather than inline: PowerShell strips the quotes when passing code to
# python -c on Windows, which made this helper fail with a SyntaxError on every call.
function Test-McpTools {
    $out = & $VenvPython (Join-Path $PSScriptRoot 'mcp_tools_list.py') 2>$null
    if ($LASTEXITCODE -eq 0 -and "$out".Trim()) { return "$out".Trim() }
    return $null
}
