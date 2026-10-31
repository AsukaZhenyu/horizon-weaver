# Build the Spine runtime volume from pinned public sources.
#
# The volume carries everything the task containers execute read-only at
# /opt/spine: the spine-plugin checkout at a pinned commit with its built
# packages and dependencies, the Pi 1.1.0 coding agent, and the Linux node
# runtime (binary plus the two shared libraries it needs in a minimal image).
#
# Everything is rebuilt from public inputs, so a new machine needs no transfer
# of the volume itself -- only network access and Docker.
#
#   pwsh -File experiments/tb-eval/bootstrap/build-runtime-volume.ps1
#   pwsh -File ... -Volume week8-spine-linux-20261030 -Force
#
# Optional environment:
#   HW_SPINE_VOLUME      volume name (default week8-spine-linux-20261030)
#   HW_SPINE_SRC         existing spine-plugin checkout to reuse
#   HW_SPINE_REPO        git URL (default https://github.com/GhabiX/spine-plugin.git)
#   HW_NODE_IMAGE        node image (default node:22-bookworm-slim, verified 22.23.4)
#   TB_BUILD_PROXY       proxy injected into the build container when set
param(
    [string]$Volume = $(if ($env:HW_SPINE_VOLUME) { $env:HW_SPINE_VOLUME } else { 'week8-spine-linux-20261030' }),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$cache = if ($env:HW_CACHE) { $env:HW_CACHE } else { Join-Path $repoRoot '.cache' }
$src = if ($env:HW_SPINE_SRC) { $env:HW_SPINE_SRC } else { Join-Path $cache 'upstream\spine-plugin' }
$repo = if ($env:HW_SPINE_REPO) { $env:HW_SPINE_REPO } else { 'https://github.com/GhabiX/spine-plugin.git' }
$nodeImage = if ($env:HW_NODE_IMAGE) { $env:HW_NODE_IMAGE } else { 'node:22-bookworm-slim' }
$commit = 'b599498be489d31b9356d77af678bfd3c7220fc0'
$piVersion = '1.1.0'

Write-Host "[runtime] volume   : $Volume"
Write-Host "[runtime] source   : $src @ $commit"
Write-Host "[runtime] node     : $nodeImage"

# 1. Pinned source checkout
if (-not (Test-Path (Join-Path $src '.git'))) {
    New-Item -ItemType Directory -Force (Split-Path $src) | Out-Null
    git clone --no-checkout $repo $src
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}
git -C $src rev-parse --verify --quiet "$commit^{commit}" | Out-Null
if ($LASTEXITCODE -ne 0) {
    git -C $src fetch --depth 1 origin $commit
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
}
git -C $src checkout --detach $commit
if ($LASTEXITCODE -ne 0) { throw "git checkout failed" }

# 2. Refuse to silently reuse a volume that already has a runtime in it
$volumes = docker volume ls --format '{{.Name}}'
$exists = $volumes -contains $Volume
if ($exists -and -not $Force) {
    $probe = docker run --rm --mount "type=volume,src=$Volume,dst=/opt/spine,readonly" $nodeImage `
        sh -c 'test -f /opt/spine/packages/plugin/dist/pi/extension.js && echo present || echo empty'
    if ($probe -eq 'present') {
        Write-Host "[runtime] volume already populated; use -Force to rebuild"
        exit 0
    }
}
docker volume create $Volume | Out-Null

# 3. Build inside a container so every artifact is Linux-native
$proxyArgs = @()
if ($env:TB_BUILD_PROXY) {
    $proxyArgs = @('--env', "HTTP_PROXY=$($env:TB_BUILD_PROXY)", '--env', "HTTPS_PROXY=$($env:TB_BUILD_PROXY)",
                   '--env', "http_proxy=$($env:TB_BUILD_PROXY)", '--env', "https_proxy=$($env:TB_BUILD_PROXY)")
}

$buildScript = @"
set -eu
cp -a /src/. /opt/spine/
mkdir -p /opt/spine/runtime/lib
cp /usr/local/bin/node /opt/spine/runtime/node
cp /usr/lib/x86_64-linux-gnu/libstdc++.so.6 /opt/spine/runtime/lib/
cp /lib/x86_64-linux-gnu/libgcc_s.so.1 /opt/spine/runtime/lib/
cd /opt/spine
npm ci --no-audit --no-fund
npm run build
npm install --no-save --no-audit --no-fund @earendil-works/pi-coding-agent@$piVersion
test -f /opt/spine/packages/plugin/dist/pi/extension.js
/opt/spine/runtime/node --version
"@

docker run --rm `
    --mount "type=volume,src=$Volume,dst=/opt/spine" `
    --mount "type=bind,src=$src,dst=/src,readonly" `
    @proxyArgs $nodeImage sh -euc $buildScript
if ($LASTEXITCODE -ne 0) { throw "runtime volume build failed" }

# 4. Record what was built, next to the run evidence
$verify = docker run --rm --mount "type=volume,src=$Volume,dst=/opt/spine,readonly" $nodeImage sh -c @'
node_ver=$(/opt/spine/runtime/node --version)
ext=$(sha256sum /opt/spine/packages/plugin/dist/pi/extension.js | cut -d' ' -f1)
wasm=$(sha256sum /opt/spine/packages/sdk/wasm/node/spine_wasm_bg.wasm | cut -d' ' -f1)
pi=$(node -p "require('/opt/spine/node_modules/@earendil-works/pi-coding-agent/package.json').version")
printf '{"node":"%s","pi":"%s","extensionSha256":"%s","wasmSha256":"%s"}\n' "$node_ver" "$pi" "$ext" "$wasm"
'@
$record = Join-Path $cache 'spine-runtime.json'
New-Item -ItemType Directory -Force (Split-Path $record) | Out-Null
$verify | Set-Content -Path $record -Encoding utf8
Write-Host "[runtime] built $Volume"
Write-Host "[runtime] $verify"
