param(
    [string]$RepoRoot
)
$ErrorActionPreference = "Stop"

if (!$RepoRoot) { $RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent }
$repoRoot = (Resolve-Path -LiteralPath $RepoRoot).ProviderPath
$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (!(Test-Path -LiteralPath $python)) { throw "Bundled-engine build requires Windows Python 3.12: $python" }

$engineBuildRoot = Join-Path $env:LOCALAPPDATA "TomatoAgent\engine-build"
$engineVenv = Join-Path $engineBuildRoot "venv"
$uv = Get-Command uv.exe -ErrorAction SilentlyContinue
if (!$uv) { throw "Bundled-engine build requires uv on PATH to install backend/uv.lock." }

New-Item -ItemType Directory -Path $engineBuildRoot -Force | Out-Null
$uvTemp = Join-Path $engineBuildRoot "uv-temp"
$uvCache = Join-Path $engineBuildRoot "uv-cache"
New-Item -ItemType Directory -Path $uvTemp, $uvCache -Force | Out-Null
$env:TEMP = $uvTemp
$env:TMP = $uvTemp
$env:UV_CACHE_DIR = $uvCache
$env:UV_PROJECT_ENVIRONMENT = $engineVenv

$backendRoot = Join-Path $repoRoot "backend"
& $uv.Source sync --locked --project $backendRoot --python $python --no-dev --group build
if ($LASTEXITCODE -ne 0) { throw "Could not install locked Windows engine build dependencies." }

$enginePython = Join-Path $engineVenv "Scripts\python.exe"
$engineOutput = Join-Path $backendRoot "dist"
$engineWork = Join-Path $engineBuildRoot "work"
New-Item -ItemType Directory -Path $engineOutput -Force | Out-Null
& $enginePython -m PyInstaller --noconfirm --clean --onefile --name tomato-agent-engine `
    --paths $backendRoot --collect-all tiktoken --collect-all uvicorn `
    --hidden-import tiktoken_ext.openai_public `
    --distpath $engineOutput --workpath $engineWork (Join-Path $backendRoot "app\desktop\bootstrap.py")
if ($LASTEXITCODE -ne 0) { throw "Windows Python engine packaging failed." }

$engineExecutable = Join-Path $engineOutput "tomato-agent-engine.exe"
if (!(Test-Path -LiteralPath $engineExecutable -PathType Leaf)) {
    throw "The bundled Python engine was not created: $engineExecutable"
}
Write-Output "Built Python engine: $engineExecutable"
