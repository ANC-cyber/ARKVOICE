$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$settings = Join-Path $root 'settings.local.bat'
$python = ''

if (Test-Path -LiteralPath $settings) {
    $line = Select-String -LiteralPath $settings -Pattern '^set "ARKVOICE_SOVITS_DIR=(.+)"$' | Select-Object -First 1
    if ($line) {
        $sovits = $line.Matches[0].Groups[1].Value
        $python = Join-Path $sovits 'runtime\python.exe'
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = Read-Host 'Enter the full path to GPT-SoVITS runtime\python.exe'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python executable not found: $python"
}

$target = Join-Path $root '.deps'
New-Item -ItemType Directory -Force -Path $target | Out-Null
& $python -m pip install --disable-pip-version-check --target $target -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'ArkVoice dependencies could not be installed.' }
Write-Host 'Dependencies installed. RapidOCR may download its model files on first launch.'
