param(
    [string]$Python = "backend\.venv\Scripts\python.exe",
    [string]$PublicKey = "",
    [string]$PublicKeyFile = "",
    [switch]$SkipInstall,
    [switch]$SkipBrowsers,
    [string]$BuildDir = ""
)

# Сборка ядра Nuitka в build\backend\pf-backend.dist.
# Публичный ключ лицензии: -PublicKey <base64> или -PublicKeyFile license_keys\ed25519_public.txt.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$build = if ($BuildDir) { $BuildDir } else { Join-Path $root "build" }
New-Item -ItemType Directory -Path $build -Force | Out-Null

if (-not (Test-Path $Python)) {
    throw "Не найден Python: $Python (создайте backend\.venv)"
}
$Python = (Resolve-Path $Python).Path

if (-not $SkipInstall) {
    & $Python -m pip install --quiet --upgrade "nuitka>=2.4" "ordered-set" "zstandard"
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить Nuitka" }
}

$key = $PublicKey.Trim()
if (-not $key -and $PublicKeyFile) {
    $key = (Get-Content $PublicKeyFile -Raw).Trim()
}

Push-Location backend
try {
    & $Python -m nuitka `
        --standalone `
        --assume-yes-for-downloads `
        --output-dir="$build\nuitka" `
        --output-filename=pf-backend.exe `
        --windows-console-mode=force `
        --include-data-files="app/flows/selectors/ozon.json=app/flows/selectors/ozon.json" `
        --include-package=app `
        --include-package=uvicorn `
        --include-package=fastapi `
        --include-package=starlette `
        --include-package=pydantic `
        --include-package=pydantic_core `
        --include-package=aiosqlite `
        --include-package=openpyxl `
        --include-package=gspread `
        --include-package=google `
        --include-module=playwright.async_api `
        --include-module=patchright.async_api `
        --include-package=psutil `
        --include-package-data=playwright `
        --include-package-data=patchright `
        --jobs=2 `
        --include-package-data=gspread `
        --include-package-data=google `
        --nofollow-import-to=tests,pytest,test_license `
        run_backend.py
    if ($LASTEXITCODE -ne 0) { throw "Nuitka завершился с ошибкой" }
} finally {
    Pop-Location
}

$dist = Get-ChildItem "$build\nuitka" -Directory -Filter "*.dist" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $dist) { throw "Nuitka не создал *.dist: проверьте лог выше" }

$target = "$build\backend\pf-backend.dist"
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
Move-Item $dist.FullName $target

if ($key) {
    Set-Content -Path "$target\license_pubkey.txt" -Value $key -Encoding utf8
    Write-Host "Публичный ключ лицензии записан в сборку"
} else {
    Write-Warning "Публичный ключ не задан: собранное ядро запустится в режиме разработки (без лицензии)"
}

$exe = "$target\pf-backend.exe"
if (-not (Test-Path $exe)) { throw "Нет pf-backend.exe в $target" }

# Браузеры Playwright кладём рядом с ядром: установщик работает на чистой машине.
# Только Chromium нужной ревизии + ffmpeg: полный кэш весит больше 2 ГБ.
$browsers = Join-Path $env:LOCALAPPDATA "ms-playwright"
if (-not $SkipBrowsers -and (Test-Path $browsers)) {
    New-Item -ItemType Directory -Path "$target\browsers" -Force | Out-Null
    $chromeExe = (& $Python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()" 2>$null | Select-Object -Last 1)
    $revDir = $null
    if ($chromeExe -and (Test-Path $chromeExe)) {
        $revDir = Split-Path (Split-Path $chromeExe -Parent) -Parent
    }
    if (-not $revDir) {
        $revDir = (Get-ChildItem $browsers -Directory | Where-Object { $_.Name -match '^chromium-\d+$' } |
            Sort-Object Name -Descending | Select-Object -First 1).FullName
    }
    if ($revDir -and (Test-Path $revDir)) {
        Copy-Item $revDir -Destination "$target\browsers" -Recurse -Force
        $ffmpeg = Get-ChildItem $browsers -Directory -Filter "ffmpeg-*" |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($ffmpeg) { Copy-Item $ffmpeg.FullName -Destination "$target\browsers" -Recurse -Force }
        $size = [math]::Round((Get-ChildItem "$target\browsers" -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
        Write-Host "Браузеры Playwright добавлены в сборку: $size МБ"
    } else {
        Write-Warning "Chromium не найден в кэше Playwright"
    }
} else {
    Write-Warning "Браузеры Playwright не найдены. Выполните: python -m playwright install chromium"
}

$port = Get-Random -Minimum 18700 -Maximum 18900
$env:PF_PORT = "$port"
$env:PF_APP_DATA = "$build\smoke"
Remove-Item "$build\smoke" -Recurse -Force -ErrorAction SilentlyContinue
$process = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden -RedirectStandardOutput "$build\smoke.log" -RedirectStandardError "$build\smoke.err.log"
try {
    $ok = $false
    for ($i = 0; $i -lt 80; $i++) {
        Start-Sleep -Milliseconds 500
        $runtimeFile = "$build\smoke\pf-automation\runtime.json"
        if (Test-Path $runtimeFile) {
            try {
                $runtime = Get-Content $runtimeFile -Raw | ConvertFrom-Json
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 2 -Headers @{ "x-token" = $runtime.token }
                if ($r.StatusCode -eq 200) { $ok = $true; break }
            } catch { }
        }
        if ($process.HasExited) { break }
    }
    if (-not $ok) {
        if (Test-Path "$build\smoke.err.log") { Get-Content "$build\smoke.err.log" -Tail 40 }
        throw "Собранное ядро не отвечает на /api/health"
    }
    Write-Host "Собранное ядро отвечает: http://127.0.0.1:$port/api/health (pid $($process.Id))"
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item Env:PF_PORT -ErrorAction SilentlyContinue
    Remove-Item Env:PF_APP_DATA -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
}

Write-Host "Готово: $target"
