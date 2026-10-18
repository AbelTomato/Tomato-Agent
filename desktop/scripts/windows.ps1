param(
    [ValidateSet("test", "build", "run")][string]$Action = "test"
)
$ErrorActionPreference = "Stop"
$env:PATH = (Join-Path $env:USERPROFILE ".cargo\bin") + ";" + $env:PATH
$env:CARGO_TARGET_DIR = Join-Path $env:LOCALAPPDATA "TomatoAgent\task1-target"
$desktopRoot = Split-Path $PSScriptRoot -Parent
Set-Location $desktopRoot
if ($Action -eq "test") {
    & cargo.exe test --manifest-path (Join-Path $desktopRoot "src-tauri\Cargo.toml") --locked
} elseif ($Action -eq "build") {
    # Windows-local CLI cache avoids sharing Linux native node_modules binaries.
    $cli = Join-Path $env:LOCALAPPDATA "TomatoAgent\task1-tools\node_modules\@tauri-apps\cli\tauri.js"
    if (!(Test-Path $cli)) { throw "Install the declared @tauri-apps/cli version in the Windows-local task1-tools cache first." }
    & node.exe $cli build -- --locked
} else {
    & (Join-Path $env:CARGO_TARGET_DIR "release\tomato-agent-desktop.exe")
}
exit $LASTEXITCODE