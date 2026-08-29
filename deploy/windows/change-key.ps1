$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DataDir = Join-Path $ProjectDir "data"
$KeyFile = Join-Path $DataDir "deepseek-key.dpapi"
$ApiPidFile = Join-Path $DataDir "windows-api.pid"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$SecureKey = Read-Host "请输入新的 DeepSeek API Key（输入内容不会显示）" -AsSecureString
if ($SecureKey.Length -eq 0) { throw "DeepSeek API Key 不能为空。" }
$SecureKey | ConvertFrom-SecureString | Set-Content -Path $KeyFile -Encoding ASCII

if (Test-Path $ApiPidFile) {
    $SavedPid = (Get-Content $ApiPidFile -Raw).Trim()
    if ($SavedPid -match '^\d+$') {
        $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" -ErrorAction SilentlyContinue
        if ($ProcessInfo -and $ProcessInfo.CommandLine -like "*run.py web*") {
            Stop-Process -Id ([int]$SavedPid) -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item $ApiPidFile -Force -ErrorAction SilentlyContinue
}

& (Join-Path $PSScriptRoot "start.ps1")
