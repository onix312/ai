param(
    [string]$Destination = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $Destination) {
    $Destination = Join-Path $repoRoot "models\tts"
}

$modelUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx?download=true"
$configUrl = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json?download=true"
$expectedSha256 = "8ff38212d23da300bbe3705c645e6e5b9475f0bfde01558eb17813e22acaaaaa"

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$modelPath = Join-Path $Destination "luma.onnx"
$configPath = Join-Path $Destination "luma.onnx.json"

if ((Test-Path $modelPath) -and -not $Force) {
    $current = (Get-FileHash -Algorithm SHA256 $modelPath).Hash.ToLowerInvariant()
    if ($current -eq $expectedSha256) {
        Write-Host "Luma voice model already installed: $modelPath"
    } else {
        throw "luma.onnx already exists with a different SHA256. Use -Force to replace it."
    }
} else {
    Write-Host "Downloading Luma voice: ru_RU-irina-medium..."
    Invoke-WebRequest -UseBasicParsing -Uri $modelUrl -OutFile $modelPath
}

$sha = (Get-FileHash -Algorithm SHA256 $modelPath).Hash.ToLowerInvariant()
if ($sha -ne $expectedSha256) {
    Remove-Item -Force $modelPath -ErrorAction SilentlyContinue
    throw "Voice model SHA256 mismatch: $sha"
}

if ((Test-Path $configPath) -and -not $Force) {
    Write-Host "Voice config already exists: $configPath"
} else {
    Invoke-WebRequest -UseBasicParsing -Uri $configUrl -OutFile $configPath
}

Write-Host ""
Write-Host "Installed Luma HQ voice:"
Write-Host "  Model:  $modelPath"
Write-Host "  Config: $configPath"

$piper = Get-Command piper -ErrorAction SilentlyContinue
if ($piper) {
    Write-Host "  Piper:  $($piper.Source)"
    Write-Host ""
    Write-Host "Luma will pick this model automatically on next agent start."
} else {
    Write-Warning "Piper executable is not on PATH. Install Piper or set its path in Luma > Settings > HQ Local TTS."
}
