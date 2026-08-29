$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DataDir = Join-Path $ProjectDir "data"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$KeyFile = Join-Path $DataDir "deepseek-key.dpapi"

Set-Location $ProjectDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCommand = "py"
    $PythonPrefix = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCommand = "python"
} else {
    throw "未找到 Python。请先安装 Python 3.11 或更高版本，并勾选 Add Python to PATH。"
}

& $PythonCommand @PythonPrefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 版本过低，需要 Python 3.11 或更高版本。"
}

if (-not (Test-Path $VenvPython)) {
    & $PythonCommand @PythonPrefix -m venv .venv
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e .

if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Copy-Item (Join-Path $ProjectDir ".env.example") (Join-Path $ProjectDir ".env")
}

if (-not (Test-Path $KeyFile)) {
    $SecureKey = Read-Host "请输入 DeepSeek API Key（输入内容不会显示）" -AsSecureString
    if ($SecureKey.Length -eq 0) { throw "DeepSeek API Key 不能为空。" }
    $SecureKey | ConvertFrom-SecureString | Set-Content -Path $KeyFile -Encoding ASCII
}

Write-Host ""
Write-Host "Windows 安装完成。请双击 deploy\windows\start.bat 启动。" -ForegroundColor Green
