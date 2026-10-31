# One-time setup of a machine for the Terminal-Bench stratified evaluation.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File experiments\tb-eval\bootstrap\setup.ps1
#   ... -Stage sources        # only clone/download pinned third-party inputs
#   ... -Stage images         # only (re)build the task images and pin their IDs
#   ... -Check                # report what is ready and what is missing, change nothing
#
# Every step is idempotent: an existing clone, volume, wheel cache or image is
# reused rather than rebuilt.  Everything lands under <repo>\.cache (gitignored)
# except the pinned image IDs in experiments\tb-eval\config\base-images.json,
# which are committed so another machine can compare its build against them.
param(
    [ValidateSet('all', 'prereqs', 'sources', 'tools', 'runtime', 'images', 'pin')]
    [string]$Stage = 'all',
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$tbEval = Join-Path $repoRoot 'experiments\tb-eval'
$cache = if ($env:HW_CACHE) { $env:HW_CACHE } else { Join-Path $repoRoot '.cache' }
$tbHome = if ($env:HW_TB_HOME) { $env:HW_TB_HOME } else { Join-Path $cache 'terminal-bench' }
$venv = if ($env:HW_VENV) { $env:HW_VENV } else { Join-Path $cache 'venv' }
$python = if ($env:HW_PYTHON) { $env:HW_PYTHON } else { Join-Path $venv 'Scripts\python.exe' }
$venvPython = Join-Path $venv 'Scripts\python.exe'
$grading = if ($env:HW_GRADING_TOOLS) { $env:HW_GRADING_TOOLS } else { Join-Path $cache 'grading-tools' }
$upstream = Join-Path $cache 'upstream'

$TB_COMMIT = 'd28711d0da2675d0bb1d56de45ae5df6082438a3'
$SENTRY_COMMIT = '1d91bca14fa6a4e17908856cc787eb8e8354395f'
$PYTEST_VERSION = '8.4.1'
$UV_VERSION = '0.7.13'

function Step($name) { Write-Host ""; Write-Host "=== $name ===" }

function Want($s) { return ($Stage -eq 'all' -or $Stage -eq $s) }

function Require-Tool($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd not found. $hint" }
}

function Sync-Repo($dir, $url, $commit) {
    if (-not (Test-Path (Join-Path $dir '.git'))) {
        New-Item -ItemType Directory -Force (Split-Path $dir) | Out-Null
        git clone --no-checkout $url $dir
        if ($LASTEXITCODE -ne 0) { throw "clone failed: $url" }
    }
    git -C $dir rev-parse --verify --quiet "$commit^{commit}" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        git -C $dir fetch --depth 1 origin $commit
        if ($LASTEXITCODE -ne 0) { throw "fetch failed: $url @ $commit" }
    }
    git -C $dir checkout --detach $commit
    if ($LASTEXITCODE -ne 0) { throw "checkout failed: $dir @ $commit" }
    Write-Host "  $dir @ $commit"
}

# ---------------------------------------------------------------- prereqs
Step 'prerequisites'
$docker = docker version --format '{{.Server.Version}}' 2>$null
Write-Host "  docker server : $(if ($docker) { $docker } else { 'NOT RUNNING' })"
Write-Host "  node          : $((node --version) 2>$null)"
Write-Host "  python        : $((Get-Command python -ErrorAction SilentlyContinue).Source)"
Write-Host "  git           : $((git --version) 2>$null)"
Write-Host "  cache         : $cache"
Write-Host "  venv python   : $venvPython"
Require-Tool -cmd 'node' -hint 'Install Node 22 (the Spine host requires >= 22.19).'
Require-Tool -cmd 'git' -hint 'Install git.'

if ($Check) {
    Step 'artifact check'
    $items = [ordered]@{
        'terminal-bench checkout' = $tbHome
        'Sentry checkout'         = (Join-Path $upstream 'sentry')
        'spine-plugin checkout'   = (Join-Path $upstream 'spine-plugin')
        'python venv'             = $venvPython
        'runner node_modules'     = (Join-Path $tbEval 'harness\node_modules\yaml')
        'uv (linux)'              = (Join-Path $grading 'uv')
        'pytest wheels'           = (Join-Path $grading 'pytest-wheels')
        'pinned images'           = (Join-Path $tbEval 'config\base-images.json')
    }
    foreach ($k in $items.Keys) {
        $ok = Test-Path $items[$k]
        Write-Host ("  [{0}] {1}  {2}" -f $(if ($ok) { 'x' } else { ' ' }), $k.PadRight(24), $items[$k])
    }
    $vol = if ($env:HW_SPINE_VOLUME) { $env:HW_SPINE_VOLUME } else { 'week8-spine-linux-20261030' }
    $hasVol = (docker volume ls --format '{{.Name}}') -contains $vol
    Write-Host ("  [{0}] spine runtime volume   {1}" -f $(if ($hasVol) { 'x' } else { ' ' }), $vol)
    exit 0
}

# ---------------------------------------------------------------- sources
if (Want 'sources') {
    Step 'pinned sources'
    Sync-Repo -dir $tbHome -url 'https://github.com/laude-institute/terminal-bench.git' -commit $TB_COMMIT
    Sync-Repo -dir (Join-Path $upstream 'sentry') -url 'https://github.com/nuglifeleoji/Sentry.git' -commit $SENTRY_COMMIT
    if ($env:HW_SPINE_SRC) {
        Write-Host "  spine-plugin  : using HW_SPINE_SRC=$($env:HW_SPINE_SRC)"
    } else {
        Sync-Repo -dir (Join-Path $upstream 'spine-plugin') -url 'https://github.com/GhabiX/spine-plugin.git' -commit 'b599498be489d31b9356d77af678bfd3c7220fc0'
    }

    Step "python environment ($venv)"
    if (-not (Test-Path $venvPython)) {
        python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
    }
    & $venvPython -m pip install --upgrade pip wheel | Out-Null
    & $venvPython -m pip install -e $tbHome
    if ($LASTEXITCODE -ne 0) { throw 'terminal-bench install failed' }
    & $venvPython -m pip install "PyYAML>=6,<7"
    if ($LASTEXITCODE -ne 0) { throw 'PyYAML install failed' }

    Step 'node dependencies of the runners'
    npm --prefix (Join-Path $tbEval 'harness') ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed for harness/package.json' }
}

# ---------------------------------------------------------------- tools
if (Want 'tools') {
    Step 'grading tools (offline uv + pytest wheels)'
    New-Item -ItemType Directory -Force $grading | Out-Null
    $uv = Join-Path $grading 'uv'
    if (-not (Test-Path $uv)) {
        $tarball = Join-Path $grading "uv-v$UV_VERSION-linux.tar.gz"
        if (-not (Test-Path $tarball)) {
            Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" -OutFile $tarball
        }
        $tmp = Join-Path $grading 'uv-extract'
        New-Item -ItemType Directory -Force $tmp | Out-Null
        tar -xzf $tarball -C $tmp
        Move-Item (Join-Path $tmp "uv-x86_64-unknown-linux-gnu\uv") $uv -Force
        Remove-Item -Recurse -Force $tmp
        Write-Host "  uv $UV_VERSION -> $uv"
    }
    $wheels = Join-Path $grading 'pytest-wheels'
    if (-not (Test-Path $wheels)) {
        & $venvPython -m pip download "pytest==$PYTEST_VERSION" -d $wheels
        if ($LASTEXITCODE -ne 0) { throw 'pytest wheel download failed' }
    }
    Write-Host "  wheels: $((Get-ChildItem $wheels).Count) file(s)"
}

# ---------------------------------------------------------------- runtime
if (Want 'runtime') {
    Step 'spine runtime volume'
    $vol = if ($env:HW_SPINE_VOLUME) { $env:HW_SPINE_VOLUME } else { 'week8-spine-linux-20261030' }
    & (Join-Path $PSScriptRoot 'build-runtime-volume.ps1') -Volume $vol
    if ($LASTEXITCODE -ne 0) { throw 'runtime volume build failed' }
}

# ---------------------------------------------------------------- images
if (Want 'images') {
    Step 'task images'
    if (-not (Test-Path $venvPython)) { throw 'venv missing: run -Stage sources first' }
    $env:HW_TB_HOME = $tbHome
    & $venvPython (Join-Path $tbEval 'tools\build_images.py')
    if ($LASTEXITCODE -ne 0) { throw 'image build failed' }
}

if (Want 'pin') {
    Step 'pin image IDs'
    if (-not (Test-Path $venvPython)) { throw 'venv missing: run -Stage sources first' }
    $env:HW_TB_HOME = $tbHome
    & $venvPython (Join-Path $tbEval 'tools\gen_base_images.py')
    if ($LASTEXITCODE -ne 0) { throw 'pinning failed' }
}

# ---------------------------------------------------------------- model
Step 'model weights'
$gguf = if ($env:HW_MODEL_GGUF) { $env:HW_MODEL_GGUF } else { Join-Path $cache 'models\qwen3.5-9b-q4_k_m.gguf' }
$expected = '03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8'
if (Test-Path $gguf) {
    $actual = (Get-FileHash $gguf -Algorithm SHA256).Hash.ToLower()
    Write-Host "  $gguf"
    Write-Host "  sha256 $actual $(if ($actual -eq $expected) { 'MATCH' } else { 'MISMATCH - protocol requires ' + $expected })"
} else {
    Write-Host "  MISSING: $gguf"
    Write-Host "  Place Qwen3.5-9B-Q4_K_M.gguf there (or set HW_MODEL_GGUF)."
    Write-Host "  Required sha256: $expected"
}

Step 'done'
Write-Host 'Next: scripts\run_round.ps1 -Status, then scripts\run_round.ps1'
