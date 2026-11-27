# PlusVCC 打包脚本（Windows）
# 用法：powershell -ExecutionPolicy Bypass -File vcc_tool\build_exe.ps1
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $Python = 'python'
}

Write-Output "使用 Python: $Python"
& $Python -m pip install --upgrade pyinstaller | Out-Host

# 旧实例在运行会占住 dist\PlusVCC.exe，PyInstaller 覆盖时会报拒绝访问。
# 先优雅退出（/api/quit），不行再强杀。
$running = @(Get-Process PlusVCC -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Output "检测到 PlusVCC 正在运行，先停止它..."
    foreach ($proc in $running) {
        $ports = @(Get-NetTCPConnection -OwningProcess $proc.Id -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty LocalPort -Unique)
        foreach ($port in $ports) {
            try {
                Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/quit" -TimeoutSec 3 | Out-Null
            } catch {}
        }
    }
    Start-Sleep -Seconds 2
    $left = @(Get-Process PlusVCC -ErrorAction SilentlyContinue)
    if ($left.Count -gt 0) {
        $left | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

$Args = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean', '--onefile', '--noconsole',
    '--name', 'PlusVCC',
    '--paths', $Root,
    '--collect-all', 'curl_cffi',
    '--collect-submodules', 'cryptography',
    '--hidden-import', 'socks',
    '--hidden-import', 'vcc_tool.app',
    '--hidden-import', 'vcc_tool.shortlink',
    '--hidden-import', 'vcc_tool.vcc_client',
    '--hidden-import', 'vcc_tool.config_store',
    '--hidden-import', 'vcc_tool.page',
    # sentinel：checkout 建单必须带 openai-sentinel-token，
    # 生成链路是 core/sentinel.py（造 p）+ node 跑 sentinel/sdk.js。
    '--hidden-import', 'core.sentinel',
    '--hidden-import', 'core.sentinel_runner',
    '--hidden-import', 'config',
    # 用相对路径：PyInstaller 6.x 的 --add-data 分隔符是 ":"，绝对路径 C:\... 里的冒号会被误判
    '--add-data', 'sentinel:sentinel',
    (Join-Path $Root 'vcc_tool\launcher.py')
)

Push-Location $Root
try {
    & $Python @Args
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

$Exe = Join-Path $Root 'dist\PlusVCC.exe'
if (Test-Path $Exe) {
    $Size = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
    Write-Output "完成: $Exe ($Size MB)"
} else {
    Write-Output "未找到输出文件，请检查上面日志"
}
