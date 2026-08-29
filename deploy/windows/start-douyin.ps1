$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DataDir = Join-Path $ProjectDir "data"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$AgentUrl = "http://127.0.0.1:18081"
$CdpPort = 19223
$CdpUrl = "http://127.0.0.1:$CdpPort"
$WorkerPidFile = Join-Path $DataDir "windows-douyin-worker.pid"
$ProfileDir = Join-Path $DataDir "douyin-profile"

function Test-Cdp {
    try { Invoke-RestMethod -Uri "$CdpUrl/json/version" -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}

function Get-Worker {
    if (-not (Test-Path $WorkerPidFile)) { return $null }
    $SavedPid = (Get-Content $WorkerPidFile -Raw).Trim()
    if ($SavedPid -notmatch '^\d+$') { return $null }
    $Info = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" -ErrorAction SilentlyContinue
    if (-not $Info -or $Info.CommandLine -notlike "*run.py douyin*") {
        Remove-Item $WorkerPidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return Get-Process -Id ([int]$SavedPid) -ErrorAction SilentlyContinue
}

function Find-Chrome {
    $Candidates = @()
    foreach ($Root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($Root) { $Candidates += (Join-Path $Root "Google\Chrome\Application\chrome.exe") }
    }
    if ($env:LOCALAPPDATA) {
        $Candidates += (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    }
    foreach ($Candidate in $Candidates) { if ($Candidate -and (Test-Path $Candidate)) { return $Candidate } }
    return $null
}

Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path $DataDir, $ProfileDir | Out-Null
if (-not (Test-Path $VenvPython)) { throw "尚未安装依赖，请先运行 install.bat。" }
try { Invoke-RestMethod -Uri "$AgentUrl/health" -TimeoutSec 3 | Out-Null }
catch { throw "栀夏客服 Agent 尚未启动。请先运行 start.bat，再启动抖店。" }

$Chrome = Find-Chrome
if (-not $Chrome) { throw "未找到 Google Chrome，请先安装 Chrome。" }
if (-not (Test-Cdp)) {
    Start-Process -FilePath $Chrome -ArgumentList @(
        "--remote-debugging-port=$CdpPort", "--remote-allow-origins=*",
        "--no-first-run", "--no-default-browser-check",
        "--user-data-dir=`"$ProfileDir`"", "https://fxg.jinritemai.com/"
    ) | Out-Null
}
for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    if (Test-Cdp) { break }
    Start-Sleep -Milliseconds 500
}
if (-not (Test-Cdp)) { throw "Chrome 配对端口没有启动，请关闭专用 Chrome 后重试。" }

if (-not (Get-Worker)) {
    $env:DOUYIN_DECISION_URL = $AgentUrl
    $env:DOUYIN_CDP_URL = $CdpUrl
    $env:PYTHONUNBUFFERED = "1"
    $Worker = Start-Process -FilePath $VenvPython `
        -ArgumentList @("-u", "run.py", "douyin") `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput (Join-Path $DataDir "windows-douyin-worker.log") `
        -RedirectStandardError (Join-Path $DataDir "windows-douyin-worker-error.log") `
        -PassThru
    Set-Content -Path $WorkerPidFile -Value $Worker.Id -Encoding ASCII
}

Start-Process -FilePath $Chrome -ArgumentList @(
    "--remote-debugging-port=$CdpPort", "--user-data-dir=`"$ProfileDir`"", "https://fxg.jinritemai.com/"
) | Out-Null
Write-Host "抖店飞鸽已配对。首次使用请登录抖店，进入飞鸽客服并打开一个会话。" -ForegroundColor Green
