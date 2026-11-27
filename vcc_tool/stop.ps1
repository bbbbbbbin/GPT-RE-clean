# 停止正在运行的 PlusVCC（替换 exe / 重新打包前用）
# 用法：powershell -ExecutionPolicy Bypass -File vcc_tool\stop.ps1
#
# 先按监听端口调 /api/quit 让它自己退（配置已落盘，不会有副作用），
# 2 秒后还有残留进程才强制结束。
$ErrorActionPreference = 'SilentlyContinue'

$procs = @(Get-Process PlusVCC)
if ($procs.Count -eq 0) {
    Write-Output "PlusVCC 没在运行，可以直接替换 exe。"
    exit 0
}

$asked = $false
foreach ($proc in $procs) {
    $ports = @(Get-NetTCPConnection -OwningProcess $proc.Id -State Listen | Select-Object -ExpandProperty LocalPort -Unique)
    foreach ($port in $ports) {
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/quit" -TimeoutSec 3 | Out-Null
            Write-Output "已请求优雅退出（端口 $port）"
            $asked = $true
        } catch {}
    }
}

if ($asked) { Start-Sleep -Seconds 2 }
$left = @(Get-Process PlusVCC)
if ($left.Count -gt 0) {
    $left | Stop-Process -Force
    Write-Output "已强制结束 $($left.Count) 个进程。"
} else {
    Write-Output "已退出。"
}
Write-Output "现在可以替换 dist\PlusVCC.exe 了。"
