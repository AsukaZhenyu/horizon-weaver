# Start the local OpenAI-compatible model server used by both tracks.
#
#   .\launch_model.ps1 -Exe <llama-server.exe> -Model <gguf>
#   .\launch_model.ps1 -Stop
#
# The launch record (executable, file, exact arguments, sha256 of the weights)
# is written to .cache\server\launch.json, which the run provenance record reads.
# Arguments are the frozen protocol conditions from DESIGN.md section 3.
param(
    [string]$Exe = $env:HW_LLAMA_SERVER,
    [string]$Model = $env:HW_MODEL_GGUF,
    [int]$Port = 8080,
    [int]$Context = 16384,
    [int]$GpuLayers = 99,
    [int]$Threads = 4,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$cache = if ($env:HW_CACHE) { $env:HW_CACHE } else { Join-Path $repoRoot '.cache' }
$serverDir = Join-Path $cache 'server'
$meta = Join-Path $serverDir 'launch.json'

if ($Stop) {
    $running = Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue
    if (-not $running) { Write-Host 'no llama-server running'; exit 0 }
    $running | Stop-Process -Force
    Write-Host "stopped $($running.Count) llama-server process(es)"
    exit 0
}

if (-not $Exe) { throw 'llama-server executable not given: pass -Exe or set HW_LLAMA_SERVER' }
if (-not $Model) { $Model = Join-Path $cache 'models\qwen3.5-9b-q4_k_m.gguf' }
if (-not (Test-Path -LiteralPath $Model)) { throw "model file missing: $Model" }
if (Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue) { throw 'an existing llama-server is running; stop it first' }

$arguments = @(
    '-m', "`"$Model`"",
    '--ctx-size', "$Context",
    '--parallel', '1',
    '-ngl', "$GpuLayers",
    '--cache-type-k', 'bf16',
    '--cache-type-v', 'bf16',
    '--no-mmap',
    '--reasoning', 'off',
    '--threads', "$Threads",
    '--threads-http', '2',
    '--host', '127.0.0.1',
    '--port', "$Port",
    '--alias', 'qwen3.5-9b',
    '--cache-ram', '256'
)

New-Item -ItemType Directory -Force $serverDir | Out-Null
$proc = Start-Process -FilePath $Exe -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $serverDir 'stdout.log') -RedirectStandardError (Join-Path $serverDir 'stderr.log')

$hash = (Get-FileHash -LiteralPath $Model -Algorithm SHA256).Hash.ToLower()
[ordered]@{
    pid = $proc.Id
    executable = $Exe
    model_file = $Model
    model_sha256 = $hash
    arguments = $arguments
    started_at = $proc.StartTime.ToString('o')
    purpose = 'stratified Terminal-Bench development run; frozen protocol conditions'
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $meta -Encoding utf8

$ok = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
Write-Host "health ok: $ok after $([int]($i*2))s"
if (-not $ok) { throw 'model server did not become healthy' }

# Trim the working set so the Docker named-pipe does not time out on a
# memory-tight machine. This releases standby pages; it does not free RAM the
# model is actually using.
Add-Type -Name Mem -Namespace Win32 -MemberDefinition '[DllImport("kernel32.dll")] public static extern bool SetProcessWorkingSetSize(IntPtr p, IntPtr min, IntPtr max);'
$p = Get-Process -Id $proc.Id
[Win32.Mem]::SetProcessWorkingSetSize($p.Handle, [IntPtr](-1), [IntPtr](-1)) | Out-Null
$p.Refresh()
Write-Host "pid=$($proc.Id) ws_mb=$([int]($p.WorkingSet64/1MB)) sha256=$hash"
Write-Host "record: $meta"
