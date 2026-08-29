$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DataDir = Join-Path $ProjectDir "data"

function Stop-RecordedProcess([string]$PidFile, [string[]]$AllowedFragments) {
    if (-not (Test-Path $PidFile)) { return }
    $SavedPid = (Get-Content $PidFile -Raw).Trim()
    if ($SavedPid -match '^\d+$') {
        $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" -ErrorAction SilentlyContinue
        if ($ProcessInfo) {
            $Allowed = $false
            foreach ($Fragment in $AllowedFragments) {
                if ($ProcessInfo.CommandLine -like "*$Fragment*") { $Allowed = $true; break }
            }
            if ($Allowed) {
                Stop-Process -Id ([int]$SavedPid) -Force -ErrorAction SilentlyContinue
            }
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

Stop-RecordedProcess (Join-Path $DataDir "windows-worker.pid") @("run.py desktop")
Stop-RecordedProcess (Join-Path $DataDir "windows-api.pid") @("run.py web")

$QianfanPidFile = Join-Path $DataDir "windows-qianfan.pid"
if (Test-Path $QianfanPidFile) {
    $Choice = Read-Host "API 和 Worker 已停止。是否同时退出本次启动的千帆？输入 Y 退出"
    if ($Choice -match '^[Yy]$') {
        $SavedPid = (Get-Content $QianfanPidFile -Raw).Trim()
        if ($SavedPid -match '^\d+$') {
            Stop-Process -Id ([int]$SavedPid) -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item $QianfanPidFile -Force -ErrorAction SilentlyContinue
}

Write-Host "栀夏客服 Agent 已停止。" -ForegroundColor Green
