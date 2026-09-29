param(
    [string]$Source = $PSScriptRoot,
    [switch]$NoAutostart,
    [switch]$NoDesktopShortcut,
    [switch]$NoLaunch,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$installRoot = Join-Path $env:LOCALAPPDATA "Luma"
$appDir = Join-Path $installRoot "app"
$modelDir = Join-Path $installRoot "models"
$exe = Join-Path $appDir "Luma.exe"
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startMenuLink = Join-Path $startMenuDir "Luma.lnk"
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "Luma.lnk"
$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$startupLink = Join-Path $startupDir "Luma.lnk"

function New-Link([string]$Path, [string]$Target) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.WorkingDirectory = Split-Path -Parent $Target
    $shortcut.Description = "Luma local AI assistant"
    $shortcut.Save()
}

if ($Uninstall) {
    foreach ($link in @($startMenuLink, $desktopLink, $startupLink)) {
        Remove-Item -Force $link -ErrorAction SilentlyContinue
    }
    Remove-Item -Recurse -Force $appDir -ErrorAction SilentlyContinue
    Write-Host "Luma application files removed."
    Write-Host "User data and models were kept in: $installRoot"
    exit 0
}

$sourceApp = Join-Path $Source "app"
if (-not (Test-Path (Join-Path $sourceApp "Luma.exe"))) {
    throw "Luma package not found: $sourceApp"
}

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
$staging = Join-Path $installRoot "app.new"
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
Copy-Item -Recurse -Force $sourceApp $staging

$old = Join-Path $installRoot "app.old"
Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue
if (Test-Path $appDir) { Move-Item -Force $appDir $old }
Move-Item -Force $staging $appDir
Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue

$sourceModels = Join-Path $Source "models"
if (Test-Path $sourceModels) {
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $sourceModels "*") $modelDir
}

New-Link $startMenuLink $exe
if (-not $NoDesktopShortcut) { New-Link $desktopLink $exe }
if ($NoAutostart) {
    Remove-Item -Force $startupLink -ErrorAction SilentlyContinue
} else {
    New-Link $startupLink $exe
}

Write-Host "Luma installed:"
Write-Host "  $exe"
Write-Host "  Start Menu shortcut: $startMenuLink"
if (-not $NoAutostart) { Write-Host "  Autostart: enabled" }

if (-not $NoLaunch) {
    Start-Process $exe
}
