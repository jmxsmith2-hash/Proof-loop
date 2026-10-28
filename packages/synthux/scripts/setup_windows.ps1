# One-time setup on Windows. From the project folder run:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
# (or double-click setup.bat)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Python 3.11+: the "py" launcher first (installed with python.org Python), then python / python3 on PATH.
$candidates = @(
    @{ Exe = "py"; Args = @("-3.12") },
    @{ Exe = "py"; Args = @("-3.13") },
    @{ Exe = "py"; Args = @("-3.11") },
    @{ Exe = "python"; Args = @() },
    @{ Exe = "python3"; Args = @() }
)
$probe = "import sys; print('%d.%d' % sys.version_info[:2])"
$found = $null
foreach ($c in $candidates) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    $ErrorActionPreference = "Continue"          # a missing version prints to stderr; that is not fatal
    $out = & $c.Exe @($c.Args) -c $probe 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    $v = "$out".Trim()
    Write-Host ("  checked {0} {1}: {2}" -f $c.Exe, ($c.Args -join " "), $(if ($code -eq 0) { $v } else { "not usable" }))
    if ($code -eq 0 -and $v -match '^\d+\.\d+$' -and [version]$v -ge [version]"3.11") { $found = $c; break }
}
if (-not $found) {
    Write-Host "Python 3.11 or newer was not found." -ForegroundColor Red
    Write-Host "Install it with:  winget install Python.Python.3.12   (or from https://www.python.org/downloads/)"
    Write-Host "Tick 'Add python.exe to PATH' during install, then run this script again."
    exit 1
}
$exe = $found.Exe
$rest = @($found.Args)
$py = ("{0} {1}" -f $exe, ($rest -join " ")).Trim()
Write-Host "Using $py"

function Step($what) {  # native commands don't stop the script on failure; check each one
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$what failed (exit code $LASTEXITCODE). Scroll up for the reason, fix it and run this again." -ForegroundColor Red
        exit 1
    }
}
if (-not (Test-Path ".venv")) { & $exe @rest -m venv .venv; Step "Creating .venv" }
$venvPy = ".\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip; Step "Updating pip"
& $venvPy -m pip install -e ".[dev]"; Step "Installing synthux"
& $venvPy -m playwright install chromium; Step "Downloading Chromium"
if (-not (Test-Path "config\settings.yaml")) { Copy-Item "config\settings.example.yaml" "config\settings.yaml" }
& ".\.venv\Scripts\synthux.exe" check; Step "synthux check"
try {
    # Desktop shortcut that opens the app window
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath("Desktop"), "Synthux.lnk"))
    $link.TargetPath = (Resolve-Path "Synthux.bat").Path
    $link.WorkingDirectory = (Get-Location).Path
    $link.WindowStyle = 7
    $link.Description = "Check CAD files and G-code, or test a website"
    $link.Save()
    Write-Host "Added a Synthux shortcut to the desktop." -ForegroundColor Green
} catch { Write-Host "(Could not add a desktop shortcut; double-click Synthux.bat instead.)" }
Write-Host ""
Write-Host "Done. Double-click Synthux on the desktop (or Synthux.bat here) to open the app." -ForegroundColor Green
Write-Host "For the command line, activate it in a new PowerShell window with:"
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:  synthux physical check examples\cad\phone_stand_v1.stl --design examples\cad\phone_stand.yaml"
