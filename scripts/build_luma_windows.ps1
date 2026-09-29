param(
    [switch]$WithVoice,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venv = Join-Path $root ".luma-build-venv"
$python = Join-Path $venv "Scripts\python.exe"
$dist = Join-Path $root "dist"
$package = Join-Path $dist "LumaPackage"

if ($Clean) {
    Remove-Item -Recurse -Force $venv -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $dist "Luma") -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $package -ErrorAction SilentlyContinue
}

if (-not (Test-Path $python)) {
    py -3 -m venv $venv
}

& $python -m pip install --disable-pip-version-check -q --upgrade pip
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $root "agent\requirements.txt") pyinstaller

Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --clean (Join-Path $root "agent\luma.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with code $LASTEXITCODE" }
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $package | Out-Null
Copy-Item -Recurse -Force (Join-Path $dist "Luma") (Join-Path $package "app")
Copy-Item -Force (Join-Path $root "scripts\install_luma_windows.ps1") (Join-Path $package "install.ps1")

if ($WithVoice) {
    & (Join-Path $root "scripts\install_luma_voice.ps1") -Destination (Join-Path $package "models\tts")
}

@"
Luma Windows package
====================
1. Right-click install.ps1 -> Run with PowerShell
2. Or: powershell -ExecutionPolicy Bypass -File .\install.ps1
3. Luma is installed per-user to %LOCALAPPDATA%\Luma
"@ | Set-Content -Encoding UTF8 (Join-Path $package "README.txt")

Write-Host ""
Write-Host "Luma package ready:"
Write-Host "  $package"
Write-Host "  App: $(Join-Path $package 'app\Luma.exe')"
if ($WithVoice) { Write-Host "  Voice: bundled ru_RU-irina-medium model" }
