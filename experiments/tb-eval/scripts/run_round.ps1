# Run one round of the stratified evaluation, resuming wherever it stopped.
#
#   .\run_round.ps1                 # both methods, pending tasks only
#   .\run_round.ps1 -Method spine   # one method
#   .\run_round.ps1 -Tasks hello-world,regex-log
#   .\run_round.ps1 -Status         # print the task state table and exit
#
# Resume rule: a case that already carries a result is never re-run, and a case
# that started an Agent attempt but never produced one is never silently
# retried -- it shows up as `started` in -Status and blocks the run, because
# repeating it would change what the evidence means.
param(
    [ValidateSet('both', 'spine', 'sentry')][string]$Method = 'both',
    [string]$Tasks = '',
    [switch]$Status,
    [switch]$NoExport
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'env.ps1')

$tbEval = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$harness = Join-Path $tbEval 'harness'
$python = if ($env:HW_PYTHON) { $env:HW_PYTHON } else { 'python' }

if ($Status) {
    & $python (Join-Path $harness 'status.py') --method $Method
    exit $LASTEXITCODE
}

# Same round directory the runners use (harness/paths.py): HW_ROUND_DIR wins.
$roundDir = if ($env:HW_ROUND_DIR) { $env:HW_ROUND_DIR } else { Join-Path $tbEval 'config' }
$images = Join-Path $roundDir 'base-images.json'
if (-not (Test-Path $images)) {
    throw "base-images.json missing from $roundDir : build the images and pin IDs first (bootstrap/setup.ps1)"
}

# The model server is shared by both tracks and must already be healthy; a
# silent 502 here would be recorded as an Agent failure, which it is not.
$baseUrl = if ($env:HW_MODEL_URL) { $env:HW_MODEL_URL } else { 'http://127.0.0.1:8080' }
try {
    Invoke-WebRequest -Uri "$baseUrl/health" -TimeoutSec 5 -UseBasicParsing | Out-Null
} catch {
    throw "model server not healthy at $baseUrl -- start it with scripts\launch_model.ps1"
}

# Provenance first: the record must exist before any result is looked at.
& $python (Join-Path $harness 'write_run_config.py')

$methods = if ($Method -eq 'both') { @('spine', 'sentry') } else { @($Method) }
$failures = @()
foreach ($m in $methods) {
    $selected = $Tasks
    if (-not $selected) {
        $selected = (& $python (Join-Path $harness 'status.py') --method $m --pending).Trim()
    }
    if (-not $selected) {
        Write-Host "[round] ${m}: nothing pending"
        continue
    }
    Write-Host "[round] ${m}: $selected"
    $started = Get-Date
    if ($m -eq 'spine') {
        node (Join-Path $harness 'spine\run.mjs') --tasks $selected
    } else {
        & $python (Join-Path $harness 'sentry\run_tb8.py') --tasks $selected
    }
    $runnerExit = $LASTEXITCODE
    Write-Host "[round] ${m} exit=$runnerExit after $([int]((Get-Date) - $started).TotalMinutes) min"
    # A failed runner still leaves whatever cases finished; summarize and
    # export them, but do not report success at the end.
    if ($runnerExit -ne 0) { $failures += $m }
}

& $python (Join-Path $harness 'summarize_spine.py')
& $python (Join-Path $harness 'sentry\summarize.py')

if (-not $NoExport) {
    & $python (Join-Path $harness 'export_results.py')
}

if ($failures.Count -gt 0) {
    Write-Host "[round] non-zero runner exit: $($failures -join ', ') -- the run directory is recorded in run-config.json"
    exit 1
}
