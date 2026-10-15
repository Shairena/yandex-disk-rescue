param([string]$Python = (Join-Path $PSScriptRoot '.buildenv\Scripts\python.exe'))
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Create .buildenv and install the pinned build dependency in README first, or pass -Python.' }
Push-Location $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name YandexDiskRescue main.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    & $Python -m PyInstaller --noconfirm --onefile --console --name YandexDiskRescueMCP main.py
    if ($LASTEXITCODE -ne 0) { throw 'MCP console build failed' }
    & $Python (Join-Path $PSScriptRoot 'package_release.py') --dist (Join-Path $PSScriptRoot 'dist')
    if ($LASTEXITCODE -ne 0) { throw 'Portable package/notice collection failed' }
    Write-Output 'Built portable ZIP in dist. Keep runtime license notices.'
} finally { Pop-Location }
