param(
    [ValidateSet("test", "build", "build-release", "run")][string]$Action = "test",
    [string]$WslDistro = $env:WSL_DISTRO_NAME,
    [string]$RepoRoot,
    [string]$BundleIdentifier,
    [string]$BuildRoot
)
$ErrorActionPreference = "Stop"

# WSL-launched PowerShell may omit PATHEXT, causing native commands to launch
# through file associations without a usable exit code.
if (($env:PATHEXT -split ';') -notcontains '.EXE') {
    $env:PATHEXT = ".COM;.EXE;.BAT;.CMD;$env:PATHEXT"
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$env:Path = (Join-Path $env:USERPROFILE ".cargo\bin") + ";" + $userPath + ";" + $machinePath
if (!$BuildRoot) { $BuildRoot = Join-Path $env:LOCALAPPDATA "TomatoAgent\task1-target" }
$env:CARGO_TARGET_DIR = $BuildRoot
if (!$RepoRoot) { $RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent }
$repoRoot = (Resolve-Path -LiteralPath $RepoRoot).ProviderPath
$desktopRoot = Join-Path $repoRoot "desktop"
Set-Location $desktopRoot

if ($Action -eq "test") {
    & cargo.exe test --manifest-path (Join-Path $desktopRoot "src-tauri\Cargo.toml") --locked
    exit $LASTEXITCODE
}

if ($Action -eq "build") {
    $cli = Join-Path $env:LOCALAPPDATA "TomatoAgent\task1-tools\node_modules\@tauri-apps\cli\tauri.js"
    if (!(Test-Path -LiteralPath $cli)) { throw "Windows-local Tauri CLI cache not found: $cli" }
    & node.exe $cli build -- --locked
    exit $LASTEXITCODE
}

if ($Action -eq "run") {
    & (Join-Path $env:CARGO_TARGET_DIR "release\tomato-agent-desktop.exe")
    exit $LASTEXITCODE
}

if (!$WslDistro -and $repoRoot -match '^\\\\wsl(?:\.localhost)?\\([^\\]+)\\(.+)$') {
    $WslDistro = $Matches[1]
    $repoRootLinux = "/" + ($Matches[2] -replace '\\', '/')
} else {
    $repoRootLinux = $null
}
if (!$WslDistro) { throw "Could not determine the WSL distribution. Pass -WslDistro explicitly." }
if (!$repoRootLinux) {
    $repoRootLinux = (& wsl.exe --distribution $WslDistro --exec wslpath -u $repoRoot).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve the repository path in WSL distribution '$WslDistro'." }
}
if (!$repoRootLinux.StartsWith("/")) { throw "Resolved WSL repository path is invalid: $repoRootLinux" }

$frontendDist = Join-Path $repoRoot "desktop\ui\dist\index.html"
$frontendDistTimeBefore = if (Test-Path -LiteralPath $frontendDist) { (Get-Item -LiteralPath $frontendDist).LastWriteTimeUtc } else { [DateTime]::MinValue }
$env:TA_BUILD_REPO = $repoRootLinux
$env:WSLENV = if ($env:WSLENV) { "TA_BUILD_REPO:$env:WSLENV" } else { "TA_BUILD_REPO" }
$frontendBuild = 'cd -- "$TA_BUILD_REPO/desktop/ui" && node "$HOME/.cache/node/corepack/pnpm/10.34.5/bin/pnpm.cjs" run build'
& wsl.exe --distribution $WslDistro --exec bash -lc $frontendBuild
if ($LASTEXITCODE -ne 0) { throw "Desktop UI build failed with exit code $LASTEXITCODE." }
if (!(Test-Path -LiteralPath $frontendDist)) { throw "Frontend build did not produce $frontendDist." }
if ((Get-Item -LiteralPath $frontendDist).LastWriteTimeUtc -le $frontendDistTimeBefore) {
    throw "Frontend output timestamp did not advance; refusing to package potentially stale assets."
}

$python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (!(Test-Path -LiteralPath $python)) { throw "Bundled-engine build requires Windows Python 3.12: $python" }
$engineBuildRoot = Join-Path $env:LOCALAPPDATA "TomatoAgent\engine-build"
$engineVenv = Join-Path $engineBuildRoot "venv"
New-Item -ItemType Directory -Path $engineBuildRoot -Force | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $engineVenv "Scripts\python.exe"))) {
    & $python -m venv $engineVenv
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the Windows engine build environment." }
}
$enginePython = Join-Path $engineVenv "Scripts\python.exe"
& $enginePython -m pip install --disable-pip-version-check -e (Join-Path $repoRoot "backend") "pyinstaller==6.17.0"
if ($LASTEXITCODE -ne 0) { throw "Could not install the pinned Windows engine build dependencies." }
$engineOutput = Join-Path $repoRoot "backend\dist"
$engineWork = Join-Path $engineBuildRoot "work"
New-Item -ItemType Directory -Path $engineOutput -Force | Out-Null
& $enginePython -m PyInstaller --noconfirm --clean --onefile --name tomato-agent-engine `
    --paths (Join-Path $repoRoot "backend") --collect-all tiktoken --collect-all uvicorn `
    --hidden-import tiktoken_ext.openai_public `
    --distpath $engineOutput --workpath $engineWork (Join-Path $repoRoot "backend\app\desktop\bootstrap.py")
if ($LASTEXITCODE -ne 0) { throw "Windows Python engine packaging failed." }

$vcvars = "D:\Dev-Tools\MSVC\tool\VC\Auxiliary\Build\vcvars64.bat"
if (!(Test-Path -LiteralPath $vcvars)) { throw "MSVC vcvars64.bat was not found: $vcvars" }
$previousLocation = Get-Location
Set-Location $env:TEMP
try {
    $vcEnvironment = & cmd.exe /d /s /c "`"$vcvars`" >nul && set" 2>$null
    $vcvarsExitCode = $LASTEXITCODE
} finally {
    Set-Location $previousLocation
}
if ($vcvarsExitCode -ne 0) { throw "MSVC environment initialization failed with exit code $vcvarsExitCode." }
foreach ($line in $vcEnvironment) {
    if ($line -match "^([^=]+)=(.*)$") {
        Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2]
    }
}

$cli = Join-Path $env:LOCALAPPDATA "TomatoAgent\task1-tools\node_modules\@tauri-apps\cli\tauri.js"
if (!(Test-Path -LiteralPath $cli)) { throw "Windows-local Tauri CLI cache not found: $cli" }
$override = Join-Path $env:TEMP "tomato-agent-tauri-chat-build-override.json"
$overrideConfig = @{ build = @{ beforeBuildCommand = "" } }
if ($BundleIdentifier) {
    if ($BundleIdentifier -notmatch '^[A-Za-z0-9]+(?:\.[A-Za-z0-9-]+)+$') {
        throw "BundleIdentifier must be a reverse-domain identifier."
    }
    $overrideConfig.identifier = $BundleIdentifier
}
$overrideJson = ConvertTo-Json -InputObject $overrideConfig -Depth 4 -Compress
[IO.File]::WriteAllText($override, $overrideJson, [Text.UTF8Encoding]::new($false))
try {
    & node.exe $cli build -c $override -b nsis -- --locked
    if ($LASTEXITCODE -ne 0) { throw "Windows chat Release/NSIS build failed with exit code $LASTEXITCODE." }
} finally {
    Remove-Item -LiteralPath $override -ErrorAction SilentlyContinue
}

$artifacts = @(
    (Join-Path $env:CARGO_TARGET_DIR "release\tomato-agent-desktop.exe"),
    (Join-Path $env:CARGO_TARGET_DIR "release\bundle\nsis\Tomato Agent_0.1.0_x64-setup.exe")
)
foreach ($artifact in $artifacts) {
    if (!(Test-Path -LiteralPath $artifact)) { throw "Expected build artifact not found: $artifact" }
    $item = Get-Item -LiteralPath $artifact
    $hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    Write-Output ("Artifact: {0}; bytes={1}; SHA256={2}" -f $item.FullName, $item.Length, $hash.Hash)
}
$installer = Get-Item -LiteralPath $artifacts[1]
$installerHash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash
$gitCommit = (& wsl.exe --distribution $WslDistro --exec git -C $repoRootLinux rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not determine the source Git commit for the archive manifest." }
$gitStatus = & wsl.exe --distribution $WslDistro --exec git -C $repoRootLinux status --porcelain
if ($LASTEXITCODE -ne 0) { throw "Could not determine whether the source worktree is dirty." }
$archiveRoot = Join-Path $env:LOCALAPPDATA "TomatoAgent\installers"
New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
$archiveTimestamp = Get-Date
$archiveDirectory = Join-Path $archiveRoot $archiveTimestamp.ToString("yyyyMMdd-HHmmss")
if (Test-Path -LiteralPath $archiveDirectory) {
    throw "Installer archive already exists; refusing to overwrite: $archiveDirectory"
}
New-Item -ItemType Directory -Path $archiveDirectory -ErrorAction Stop | Out-Null
$archivedInstaller = Join-Path $archiveDirectory "TomatoAgent-setup.exe"
Copy-Item -LiteralPath $installer.FullName -Destination $archivedInstaller -ErrorAction Stop

$manifest = [ordered]@{
    archivedAtLocal = $archiveTimestamp.ToString("yyyy-MM-dd'T'HH:mm:ss.fffzzz")
    applicationVersion = "0.1.0"
    applicationIdentifier = if ($BundleIdentifier) { $BundleIdentifier } else { "dev.tomato.agent" }
    gitCommit = $gitCommit
    gitWorktree = if ($gitStatus) { "dirty" } else { "clean" }
    target = "windows-x64"
    buildCommand = "desktop/scripts/windows.ps1 -Action build-release"
    installerFile = [ordered]@{
        name = [IO.Path]::GetFileName($archivedInstaller)
        sizeBytes = (Get-Item -LiteralPath $archivedInstaller).Length
        sha256 = $installerHash
    }
}
$manifestPath = Join-Path $archiveDirectory "manifest.json"
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
$archivedHash = (Get-FileHash -LiteralPath $archivedInstaller -Algorithm SHA256).Hash
if ($archivedHash -ne $installerHash) {
    throw "Archived installer hash does not match the build output: $archiveDirectory"
}
Write-Output "Archived installer: $archivedInstaller"
Write-Output "Archive manifest: $manifestPath"
exit 0
