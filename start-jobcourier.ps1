$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"          # 中文 Windows 默认 GBK，Python 读 UTF-8 文件会炸
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  JobCourier 启动中..." -ForegroundColor Cyan
Write-Host "  前后端都在 http://127.0.0.1:8000，不需要管其他端口。" -ForegroundColor DarkGray
Write-Host "  首次启动会自动补齐依赖并构建前端；之后直接起服务。" -ForegroundColor DarkGray
Write-Host "  前端改动后先在 frontend 目录执行 npm run build，再启动。" -ForegroundColor DarkGray
Write-Host ""

# 首次启动：缺什么补什么（后端 .venv、前端 node_modules、前端产物 dist）
if (-not (Test-Path "$PSScriptRoot\.venv\Scripts\python.exe")) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "  未找到 uv，请先安装：https://docs.astral.sh/uv/" -ForegroundColor Yellow
        pause
        exit 1
    }
    Write-Host "  首次启动：安装后端依赖，约 1 分钟..." -ForegroundColor DarkGray
    uv sync
}
if (-not (Test-Path "$PSScriptRoot\frontend\node_modules")) {
    Write-Host "  首次启动：安装前端依赖，约 1 分钟..." -ForegroundColor DarkGray
    Push-Location "$PSScriptRoot\frontend"
    npm ci
    Pop-Location
}
if (-not (Test-Path "$PSScriptRoot\frontend\dist\index.html")) {
    Write-Host "  首次启动：构建前端，约 1 分钟..." -ForegroundColor DarkGray
    Push-Location "$PSScriptRoot\frontend"
    npm run build
    Pop-Location
}

# 后端起来后再自动开浏览器，避免又看到"正在启动"卡住
$opener = Start-Job {
    for ($i = 0; $i -lt 150; $i++) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:8000/ping" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { Start-Process "http://127.0.0.1:8000"; break }
        } catch {}
    }
}

& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\main.py"
