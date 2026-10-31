# Apply the machine-local environment for the Terminal-Bench evaluation harness.
#
#   . .\experiments\tb-eval\scripts\env.ps1
#
# Drop overrides for this machine in experiments\tb-eval\.local\env.ps1
# (gitignored, never committed).  Anything not set there falls back to the
# defaults in harness/paths.py: everything under <repo>\.cache.

$tbEval = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$localEnv = Join-Path $tbEval '.local\env.ps1'
if (Test-Path $localEnv) {
    . $localEnv
    Write-Host "[env] machine overrides from $localEnv"
}

$cache = if ($env:HW_CACHE) { $env:HW_CACHE } else { Join-Path $tbEval '..\..\.cache' }
New-Item -ItemType Directory -Force $cache | Out-Null
New-Item -ItemType Directory -Force (Join-Path $cache 'runs') | Out-Null

Write-Host "[env] cache : $cache"
Write-Host "[env] tb    : $(if ($env:HW_TB_HOME) { $env:HW_TB_HOME } else { Join-Path $cache 'terminal-bench' })"
Write-Host "[env] runs  : $(if ($env:HW_RUNS) { $env:HW_RUNS } else { Join-Path $cache 'runs' })"
