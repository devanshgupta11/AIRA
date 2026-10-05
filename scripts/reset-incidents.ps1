<#
.SYNOPSIS
  Deletes all incidents and logged webhook payloads from the AIRA database (fresh demo).
.PARAMETER Force
  Skip the confirmation prompt.
#>
param([switch]$Force)
. "$PSScriptRoot\common.ps1"

$db = Join-Path $BackendDir 'data\aira.db'
if (-not (Test-Path $db)) { Write-Info "no database at $db - nothing to reset"; exit 0 }

Push-Location $BackendDir
try {
    $count = & $VenvPython -c "from app import db; c = db.connect(); print(c.execute('SELECT COUNT(*) FROM incidents').fetchone()[0])"
    Write-Step "Reset incident database ($db)"
    Write-Info "$count incident(s) will be deleted permanently."
    if (-not $Force) {
        $answer = Read-Host '  Type YES to delete them'
        if ($answer -ne 'YES') { Write-Info 'cancelled - nothing deleted'; exit 0 }
    }
    $deleted = & $VenvPython -c "from app import db; print(db.reset())"
    if ($LASTEXITCODE -ne 0) { Write-Fail 'reset failed'; exit 1 }
    $left = & $VenvPython -c "from app import db; c = db.connect(); print(c.execute('SELECT COUNT(*) FROM incidents').fetchone()[0])"
    if ($left -eq '0') { Write-Ok "deleted $deleted incident(s); database is empty" } else { Write-Fail "$left incident(s) remain"; exit 1 }
} finally { Pop-Location }
