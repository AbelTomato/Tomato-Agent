param(
    [ValidateSet("test", "build", "build-chat", "run")][string]$Action = "test",
    [string]$WslDistro = $env:WSL_DISTRO_NAME,
    [string]$RepoRoot
)
$ErrorActionPreference = "Stop"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$env:Path = (Join-Path $env:USERPROFILE ".cargo\bin") + ";" + $userPath + ";" + $machinePath
$env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA "TomatoAgent\task1-target"
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

$frontendDist = Join-Path $repoRoot "frontend\dist\index.html"
$frontendDistTimeBefore = if (Test-Path -LiteralPath $frontendDist) { (Get-Item -LiteralPath $frontendDist).LastWriteTimeUtc } else { [DateTime]::MinValue }
$env:TA_BUILD_REPO = $repoRootLinux
$env:WSLENV = if ($env:WSLENV) { "TA_BUILD_REPO:$env:WSLENV" } else { "TA_BUILD_REPO" }
$frontendBuild = 'cd -- "$TA_BUILD_REPO/frontend" && node "$HOME/.cache/node/corepack/pnpm/10.34.5/bin/pnpm.cjs" run build:chat'
& wsl.exe --distribution $WslDistro --exec bash -lc $frontendBuild
if ($LASTEXITCODE -ne 0) { throw "WSL frontend chat build failed with exit code $LASTEXITCODE." }
if (!(Test-Path -LiteralPath $frontendDist)) { throw "Frontend build did not produce $frontendDist." }
if ((Get-Item -LiteralPath $frontendDist).LastWriteTimeUtc -le $frontendDistTimeBefore) {
    throw "Frontend output timestamp did not advance; refusing to package potentially stale assets."
}

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
[IO.File]::WriteAllText($override, '{"build":{"beforeBuildCommand":""}}', [Text.UTF8Encoding]::new($false))
try {
    & node.exe $cli build -c src-tauri/tauri.chat.conf.json -c $override -f desktop-chat -b nsis -- --locked
    if ($LASTEXITCODE -ne 0) { throw "Windows chat Release/NSIS build failed with exit code $LASTEXITCODE." }
} finally {
    Remove-Item -LiteralPath $override -ErrorAction SilentlyContinue
}

foreach ($artifact in @(
    (Join-Path $env:CARGO_TARGET_DIR "release\tomato-agent-desktop.exe"),
    (Join-Path $env:CARGO_TARGET_DIR "release\bundle\nsis\Tomato Agent_0.1.0_x64-setup.exe")
)) {
    if (!(Test-Path -LiteralPath $artifact)) { throw "Expected build artifact not found: $artifact" }
    $item = Get-Item -LiteralPath $artifact
    $hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    Write-Output ("Artifact: {0}; bytes={1}; SHA256={2}" -f $item.FullName, $item.Length, $hash.Hash)
}
exit 0