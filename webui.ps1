param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "logs")]
    [string]$Command = "start"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$RunDir = Join-Path $Root "run"
$LogDir = Join-Path $Root "logs"
$PidFile = Join-Path $RunDir "webui.pid"
$LogFile = Join-Path $LogDir "webui.log"
$ErrFile = Join-Path $LogDir "webui.err.log"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

$BridgeScript = Join-Path $Root "tools\openbrowser_roxy_bridge.py"
$BridgePort = if ($env:BRIDGE_PORT) { $env:BRIDGE_PORT } else { "50100" }
$BridgePidFile = Join-Path $RunDir "openbrowser_bridge.pid"
$BridgeLogFile = Join-Path $LogDir "openbrowser_bridge.log"
$BridgeErrFile = Join-Path $LogDir "openbrowser_bridge.err.log"


$OpenBrowserApp = if ($env:OPENBROWSER_APP) { $env:OPENBROWSER_APP } else { "D:\OpenBrowser-1.0.27\runtime\OpenBrowser.exe" }
$OpenBrowserPort = "50327"
$OpenBrowserPidFile = Join-Path $RunDir "openbrowser_app.pid"

$HostAddr = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$Port = if ($env:PORT) { $env:PORT } else { "8000" }
$OpenBrowser = if ($env:OPEN_BROWSER) { $env:OPEN_BROWSER } else { "0" }
$AuthCode = if ($env:AUTH_CODE) { $env:AUTH_CODE } else { "" }

New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null

function Get-WebuiProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match "web\.py" -and $_.CommandLine -match "--port[ =]$Port(\s|$)" }
}

function Get-BridgeProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match "openbrowser_roxy_bridge\.py" }
}

function Get-OpenBrowserAppProcess {
    Get-CimInstance Win32_Process -Filter "Name='OpenBrowser.exe'" |
        Where-Object { $_.ExecutablePath -eq $OpenBrowserApp }
}

function Get-EnvValue {
    param([string]$Name)
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return "" }
    $line = Get-Content $envFile | Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return (($line -split "=", 2)[1]).Trim().Trim('"').Trim("'")
}

function Start-OpenBrowserApp {
    $running = Get-OpenBrowserAppProcess
    if ($running) {
        Write-Output "OpenBrowser 1.0.27 已在运行 (PID: $($running.ProcessId -join ', '))"
        return
    }
    if (-not (Test-Path $OpenBrowserApp)) {
        Write-Output "未找到 OpenBrowser：$OpenBrowserApp"
        return
    }
    $apiKey = Get-EnvValue "OPENBROWSER_API_KEY"
    if ($apiKey) { $env:OPENBROWSER_API_KEY = $apiKey }
    $env:OPENBROWSER_API_PORT = $OpenBrowserPort
    $proc = Start-Process -FilePath $OpenBrowserApp -WorkingDirectory (Split-Path -Parent $OpenBrowserApp) -PassThru
    Set-Content -Path $OpenBrowserPidFile -Value $proc.Id -Encoding ascii
    Start-Sleep -Seconds 6
    Write-Output "OpenBrowser 1.0.27 已启动 (PID: $($proc.Id))"
}

function Start-Chain {
    $running = Get-ChainProcess
    if ($running) {
        Write-Output "代理链已在运行 (PID: $($running.ProcessId -join ', '))：socks5://127.0.0.1:$ChainPort"
        return
    }
    if (-not (Test-Path $ChainScript)) {
        Write-Output "未找到代理链脚本：$ChainScript"
        return
    }
    $proc = Start-Process -FilePath $Python -ArgumentList @($ChainScript) -WorkingDirectory $Root `
        -RedirectStandardOutput $ChainLogFile -RedirectStandardError $ChainErrFile `
        -PassThru -WindowStyle Hidden
    Set-Content -Path $ChainPidFile -Value $proc.Id -Encoding ascii
    Write-Output "代理链已启动 (PID: $($proc.Id))：socks5://127.0.0.1:$ChainPort"
}

function Stop-Chain {
    $running = Get-ChainProcess
    if (-not $running) {
        Remove-Item $ChainPidFile -ErrorAction SilentlyContinue
        return
    }
    $running | ForEach-Object { Stop-Tree -ProcessId $_.ProcessId }
    Remove-Item $ChainPidFile -ErrorAction SilentlyContinue
    Write-Output "代理链已停止"
}

function Start-Bridge {
    $running = Get-BridgeProcess
    if ($running) {
        Write-Output "OpenBrowser 桥接已在运行 (PID: $($running.ProcessId -join ', '))：http://127.0.0.1:$BridgePort"
        return
    }
    if (-not (Test-Path $BridgeScript)) {
        Write-Output "未找到桥接脚本：$BridgeScript"
        return
    }
    $bridgeApiKey = Get-EnvValue "OPENBROWSER_API_KEY"
    if ($bridgeApiKey) { $env:OPENBROWSER_API_KEY = $bridgeApiKey }
    $proc = Start-Process -FilePath $Python -ArgumentList @($BridgeScript) -WorkingDirectory $Root `
        -RedirectStandardOutput $BridgeLogFile -RedirectStandardError $BridgeErrFile `
        -PassThru -WindowStyle Hidden
    Set-Content -Path $BridgePidFile -Value $proc.Id -Encoding ascii
    Write-Output "OpenBrowser 桥接已启动 (PID: $($proc.Id))：http://127.0.0.1:$BridgePort"
}

function Stop-Bridge {
    $running = Get-BridgeProcess
    if (-not $running) {
        Remove-Item $BridgePidFile -ErrorAction SilentlyContinue
        return
    }
    $running | ForEach-Object { Stop-Tree -ProcessId $_.ProcessId }
    Remove-Item $BridgePidFile -ErrorAction SilentlyContinue
    Write-Output "OpenBrowser 桥接已停止"
}

function Stop-Tree {
    param([int]$ProcessId)
    try {
        $null = cmd /c "taskkill /PID $ProcessId /T /F >nul 2>nul"
    } catch {
    }
}

function Start-Webui {
    Start-OpenBrowserApp
    Start-Bridge
    $running = Get-WebuiProcess
    if ($running) {
        Write-Output "WebUI 已在运行 (PID: $($running.ProcessId -join ', '))：http://${HostAddr}:$Port"
        return
    }
    if (-not (Test-Path $Python)) {
        throw "未找到虚拟环境：$Python。请先创建 .venv 并安装 requirements.txt"
    }

    $args = @("web.py", "--host", $HostAddr, "--port", $Port)
    if ($OpenBrowser -eq "1") { $args += "--open-browser" }
    if ($AuthCode) { $args += @("--auth-code", $AuthCode) }

    $proc = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Root `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile `
        -PassThru -WindowStyle Hidden
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
    Start-Sleep -Seconds 3
    Write-Output "WebUI 已启动 (PID: $($proc.Id))：http://${HostAddr}:$Port"
    Write-Output "日志：$LogFile"
}

function Stop-Webui {
    $running = Get-WebuiProcess
    if (-not $running) {
        Write-Output "WebUI 未运行"
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Stop-Bridge
        return
    }
    $running | ForEach-Object { Stop-Tree -ProcessId $_.ProcessId }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Output "WebUI 已停止"
    Stop-Bridge
}

switch ($Command) {
    "start"   { Start-Webui }
    "stop"    { Stop-Webui }
    "restart" { Stop-Webui; Start-Sleep -Seconds 1; Start-Webui }
    "status"  {
        $running = Get-WebuiProcess
        if ($running) {
            Write-Output "WebUI：运行中 http://${HostAddr}:$Port (PID: $($running.ProcessId -join ', '))"
        } else {
            Write-Output "WebUI：未运行"
        }
        $app = Get-OpenBrowserAppProcess
        if ($app) {
            Write-Output "OpenBrowser 1.0.27：运行中 (PID: $($app.ProcessId -join ', '))，API $OpenBrowserPort"
        } else {
            Write-Output "OpenBrowser 1.0.27：未运行"
        }
        $bridge = Get-BridgeProcess
        if ($bridge) {
            Write-Output "OpenBrowser 桥接：运行中 http://127.0.0.1:$BridgePort (PID: $($bridge.ProcessId -join ', '))"
        } else {
            Write-Output "OpenBrowser 桥接：未运行"
        }
    }
    "logs"    { if (Test-Path $LogFile) { Get-Content $LogFile -Wait } else { Write-Output "暂无日志" } }
}





