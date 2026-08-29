$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PidFile = Join-Path $ProjectDir "data\windows-douyin-worker.pid"
if (Test-Path $PidFile) {
    $SavedPid = (Get-Content $PidFile -Raw).Trim()
    if ($SavedPid -match '^\d+$') {
        $Info = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" -ErrorAction SilentlyContinue
        if ($Info -and $Info.CommandLine -like "*run.py douyin*") {
            Stop-Process -Id ([int]$SavedPid) -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}
Write-Host "抖店飞鸽 Worker 已停止；专用 Chrome 可手动关闭。" -ForegroundColor Green
