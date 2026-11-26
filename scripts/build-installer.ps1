param(
    [string]$PublicKey = "",
    [string]$PublicKeyFile = "backend\license_keys\ed25519_public.txt",
    [switch]$SkipBackend
)

# Полная сборка: ядро Nuitka -> рендерер/Electron -> NSIS-установщик в ui\release.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $SkipBackend) {
    & "$PSScriptRoot\build-backend.ps1" -PublicKey $PublicKey -PublicKeyFile $PublicKeyFile
    if ($LASTEXITCODE -ne 0) { throw "Сборка ядра не удалась" }
}

Push-Location ui
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Сборка UI не удалась" }
    npx electron-builder --win nsis
    if ($LASTEXITCODE -ne 0) { throw "electron-builder завершился с ошибкой" }
} finally {
    Pop-Location
}

Get-ChildItem "$root\ui\release" -Filter "*.exe" -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Host "Установщик: $($_.FullName)" }
