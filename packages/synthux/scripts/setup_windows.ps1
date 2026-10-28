# One-time setup on Windows. From the project folder run:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
# (or double-click setup.bat)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Split-Cmd($cmd) {
    $parts = $cmd.Split(" ")
    $rest = @()
    if ($parts.Length -gt 1) { $rest = $parts[1..($parts.Length - 1)] }  # 1..0 would count backwards
    return ,@($parts[0], $rest)
}

function Find-Python {
    foreach ($cmd in @("py -3.12", "py -3.11", "py -3.13", "python")) {
        try {
            $exe, $rest = Split-Cmd $cmd
            $v = & $exe @rest -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { return $cmd }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.11 or newer was not found." -ForegroundColor Red
    Write-Host "Install it with:  winget install Python.Python.3.12   (or from https://www.python.org/downloads/)"
    Write-Host "Tick 'Add python.exe to PATH' during install, then run this script again."
    exit 1
}
Write-Host "Using $py"
$exe, $rest = Split-Cmd $py
if (-not (Test-Path ".venv")) { & $exe @rest -m venv .venv }
$venvPy = ".\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -e ".[dev]"
& $venvPy -m playwright install chromium
if (-not (Test-Path "config\settings.yaml")) { Copy-Item "config\settings.example.yaml" "config\settings.yaml" }
& ".\.venv\Scripts\synthux.exe" check
Write-Host ""
Write-Host "Done. In a new PowerShell window, activate it with:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:  synthux physical check examples\cad\phone_stand_v1.stl --design examples\cad\phone_stand.yaml"
