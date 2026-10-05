<#
.SYNOPSIS
  Health of every AIRA component, active alerts, active faults and incidents.
#>
. "$PSScriptRoot\common.ps1"
$problems = 0

Write-Step "Docker"
if (Test-DockerEngine) {
    Write-Ok "engine running ($(docker info --format '{{.ServerVersion}}'))"
    $all = docker ps -a --filter 'label=com.docker.compose.project=opentelemetry-demo' --format '{{.Names}}|{{.Status}}'
    $up = $all | Where-Object { $_ -match '\|Up' -and $_ -notmatch 'unhealthy' }
    $bad = $all | Where-Object { $_ -notmatch '\|Up' -or $_ -match 'unhealthy' }
    Write-Info "$(@($up).Count) of $(@($all).Count) demo containers up"
    $bad | ForEach-Object { Write-Warn2 ($_ -replace '\|', ' : '); $problems++ }
} else { Write-Fail 'Docker engine not running'; $problems++ }

Write-Step "Endpoints"
$checks = [ordered]@{
    'Astronomy Shop :8080'  = 'http://localhost:8080/'
    'Grafana'               = 'http://localhost:8080/grafana/api/health'
    'Jaeger'                = 'http://localhost:8080/jaeger/ui/'
    'flagd-ui API'          = 'http://localhost:8080/feature/api/read'
    'Load generator'        = 'http://localhost:8080/loadgen/'
    'Prometheus :9090'      = 'http://localhost:9090/-/ready'
    'Alertmanager :9093'    = 'http://localhost:9093/-/ready'
    'Ollama :11434'         = 'http://localhost:11434/api/version'
    'AIRA backend :8000'    = 'http://localhost:8000/health'
}
foreach ($c in $checks.GetEnumerator()) {
    if (Test-Http $c.Value) { Write-Ok $c.Key } else { Write-Fail "$($c.Key)  ($($c.Value))"; $problems++ }
}

if (Test-Http 'http://localhost:8000/health') {
    $h = Invoke-RestMethod 'http://localhost:8000/health'
    foreach ($k in $h.checks.PSObject.Properties) {
        if ($k.Value.ok) { Write-Ok "backend -> $($k.Name): $($k.Value.detail)" } else { Write-Fail "backend -> $($k.Name): $($k.Value.detail)"; $problems++ }
    }
}
if (Test-Http 'http://localhost:11434/api/version') {
    $m = (Invoke-RestMethod 'http://localhost:11434/api/ps').models
    if ($m) { $m | ForEach-Object { Write-Info ("model loaded: {0} ({1:N1} GB in GPU memory)" -f $_.name, ($_.size_vram / 1GB)) } }
    else { Write-Info 'no model loaded right now (first agent run will take ~30s longer)' }
}

Write-Step "Prometheus alert rules"
try {
    $rules = (Invoke-RestMethod 'http://localhost:9090/api/v1/rules').data.groups | ForEach-Object { $_.rules }
    foreach ($r in $rules) {
        $line = "{0,-22} health={1} state={2}" -f $r.name, $r.health, $r.state
        if ($r.health -eq 'ok') { Write-Ok $line } else { Write-Warn2 $line }
    }
    $alerts = (Invoke-RestMethod 'http://localhost:9090/api/v1/alerts').data.alerts
    if ($alerts) { $alerts | ForEach-Object { Write-Warn2 ("ALERT {0}/{1} is {2}" -f $_.labels.alertname, $_.labels.service_name, $_.state) } }
    else { Write-Ok 'no alerts pending or firing' }
} catch { Write-Fail "Prometheus API: $_"; $problems++ }

Write-Step "Fault injection state"
try {
    $flags = (Invoke-RestMethod 'http://localhost:8080/feature/api/read').flags
    $on = foreach ($p in $flags.PSObject.Properties) {
        $f = $p.Value
        $v = if ($f.targeting -and $f.targeting.if -and $f.targeting.if.Count -eq 3) { $f.targeting.if[1] } else { $f.defaultVariant }
        if ($v -ne 'off') { "$($p.Name)=$v" }
    }
    if ($on) { $on | ForEach-Object { Write-Warn2 "flag ON: $_" } } else { Write-Ok 'all feature flags off' }
} catch { Write-Fail "flagd-ui API: $_" }
foreach ($p in (Get-Scenarios).PSObject.Properties) {
    if ($p.Value.container) {
        $st = docker inspect $p.Value.container --format '{{.State.Status}}' 2>$null
        if ($st -ne 'running') { Write-Warn2 "container $($p.Value.container) is $st (scenario $($p.Name))" }
    }
}

Write-Step "AIRA incidents"
try {
    $inc = (Invoke-WebRequest -UseBasicParsing 'http://localhost:8000/incidents').Content | ConvertFrom-Json
    $inc = @($inc)
    $firing = @($inc | Where-Object status -eq 'firing')
    Write-Info "$($inc.Count) incidents, $($firing.Count) firing"
    $inc | Select-Object -First 8 | ForEach-Object {
        Write-Info ("#{0,-3} {1,-8} {2,-22} {3,-16} agent={4}" -f $_.id, $_.status, $_.alertname, $_.service, $_.agent_status)
    }
} catch { Write-Info 'backend not reachable' }

if ($problems) { Write-Host "`n$problems problem(s) found." -ForegroundColor Yellow; exit 1 }
Write-Host "`nAll components healthy." -ForegroundColor Green
