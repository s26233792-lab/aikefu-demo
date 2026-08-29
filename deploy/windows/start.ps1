$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DataDir = Join-Path $ProjectDir "data"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$KeyFile = Join-Path $DataDir "deepseek-key.dpapi"
$AgentUrl = "http://127.0.0.1:18081"
$CdpPort = 19222
$CdpUrl = "http://127.0.0.1:$CdpPort"
$ApiPidFile = Join-Path $DataDir "windows-api.pid"
$WorkerPidFile = Join-Path $DataDir "windows-worker.pid"
$QianfanPidFile = Join-Path $DataDir "windows-qianfan.pid"

function Get-Health {
    try {
        return Invoke-RestMethod -Uri "$AgentUrl/health" -TimeoutSec 3
    } catch {
        return $null
    }
}

function Test-Cdp {
    try {
        Invoke-RestMethod -Uri "$CdpUrl/json/version" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-RecordedProcess([string]$PidFile, [string]$AllowedFragment) {
    if (-not (Test-Path $PidFile)) { return $null }
    $SavedPid = (Get-Content $PidFile -Raw).Trim()
    if ($SavedPid -notmatch '^\d+$') { return $null }
    $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" -ErrorAction SilentlyContinue
    if (-not $ProcessInfo -or $ProcessInfo.CommandLine -notlike "*$AllowedFragment*") {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    return Get-Process -Id ([int]$SavedPid) -ErrorAction SilentlyContinue
}

function Find-QianfanExecutable {
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\eva\千帆客服工作台.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\千帆客服工作台\千帆客服工作台.exe"),
        (Join-Path $env:ProgramFiles "千帆客服工作台\千帆客服工作台.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) { return $Candidate }
    }
    $ProgramsDir = Join-Path $env:LOCALAPPDATA "Programs"
    if (Test-Path $ProgramsDir) {
        $Found = Get-ChildItem -Path $ProgramsDir -Filter "千帆客服工作台.exe" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($Found) { return $Found.FullName }
    }
    return $null
}

Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not (Test-Path $VenvPython)) {
    throw "尚未安装依赖，请先双击 deploy\windows\install.bat。"
}
if (-not (Test-Path $KeyFile)) {
    throw "尚未配置 DeepSeek API Key，请先双击 deploy\windows\install.bat。"
}

$SecureKey = Get-Content $KeyFile -Raw | ConvertTo-SecureString
$KeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
try {
    $PlainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($KeyPointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($KeyPointer)
}
$env:XHS_LLM_API_KEY = $PlainKey

$Health = Get-Health
if ($Health -and -not $Health.llm_ready) {
    $RecordedApi = Get-RecordedProcess $ApiPidFile "run.py web"
    if ($RecordedApi) {
        Stop-Process -Id $RecordedApi.Id -Force
        Start-Sleep -Seconds 1
    } else {
        throw "端口 18081 已被其他服务占用，请先关闭该服务。"
    }
    $Health = $null
}

if (-not $Health) {
    $ApiProcess = Start-Process -FilePath $VenvPython `
        -ArgumentList @("-u", "run.py", "web") `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput (Join-Path $DataDir "windows-api.log") `
        -RedirectStandardError (Join-Path $DataDir "windows-api-error.log") `
        -PassThru
    Set-Content -Path $ApiPidFile -Value $ApiProcess.Id -Encoding ASCII
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        Start-Sleep -Milliseconds 500
        $Health = Get-Health
        if ($Health -and $Health.llm_ready) { break }
    }
}

$env:XHS_LLM_API_KEY = $null
$PlainKey = $null
if (-not $Health -or -not $Health.llm_ready) {
    throw "客服 API 未正常启动，请查看 data\windows-api-error.log。"
}

$QianfanExe = Find-QianfanExecutable
if (-not $QianfanExe) {
    throw "未找到千帆客服工作台。请先安装客户端，或将其安装到当前用户的 Programs 目录。"
}

if (-not (Test-Cdp)) {
    $ExistingQianfan = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -eq $QianfanExe }
    if ($ExistingQianfan) {
        $Choice = Read-Host "千帆需要重启以开启配对端口。保存输入内容后，输入 Y 继续"
        if ($Choice -notmatch '^[Yy]$') { return }
        $ExistingQianfan | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    $QianfanProcess = Start-Process -FilePath $QianfanExe `
        -ArgumentList @("--remote-debugging-port=$CdpPort", "--remote-allow-origins=*") `
        -PassThru
    Set-Content -Path $QianfanPidFile -Value $QianfanProcess.Id -Encoding ASCII
}

for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    if (Test-Cdp) { break }
    Start-Sleep -Milliseconds 500
}
if (-not (Test-Cdp)) {
    throw "千帆配对端口未启动，请完全退出千帆后重试。"
}

$WorkerProcess = Get-RecordedProcess $WorkerPidFile "run.py desktop"
if (-not $WorkerProcess) {
    $env:XHS_DECISION_URL = $AgentUrl
    $env:XHS_CDP_URL = $CdpUrl
    $env:PYTHONUNBUFFERED = "1"
    $WorkerProcess = Start-Process -FilePath $VenvPython `
        -ArgumentList @("-u", "run.py", "desktop") `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput (Join-Path $DataDir "windows-worker.log") `
        -RedirectStandardError (Join-Path $DataDir "windows-worker-error.log") `
        -PassThru
    Set-Content -Path $WorkerPidFile -Value $WorkerProcess.Id -Encoding ASCII
}

Start-Process $AgentUrl
Write-Host ""
Write-Host "栀夏客服 Agent 已启动：DeepSeek、千帆和自动回复 Worker 均已连接。" -ForegroundColor Green
