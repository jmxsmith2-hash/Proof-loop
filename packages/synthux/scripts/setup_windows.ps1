# One-time setup on Windows. From the project folder run:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
# (or double-click setup.bat)
#
# Python isn't needed beforehand: when no Python 3.11+ is found, the setup fetches uv
# (https://docs.astral.sh/uv/, a single program installed for your user only, no admin rights)
# and uv downloads its own Python 3.12 for this folder.
# ClearLoop (loop files, the printer monitor) is installed too when it is found next to this
# folder (..\Something-B or ..\clearloop) or can be fetched with git; without it everything else works.
param(
    [switch]$ForceUv,        # use uv even if Python is installed (CI uses this to test that path)
    [switch]$NoClearLoop,    # don't install ClearLoop
    [switch]$NoShortcuts     # don't add desktop shortcuts
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Step($what) {  # native commands don't stop the script on failure; check each one
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$what failed (exit code $LASTEXITCODE). Scroll up for the reason, fix it and run this again." -ForegroundColor Red
        exit 1
    }
}

# Python 3.11+: the "py" launcher first (installed with python.org Python), then python / python3 on PATH.
$found = $null
if (-not $ForceUv) {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.13") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    $probe = "import sys; print('%d.%d' % sys.version_info[:2])"
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
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if ($found) {
        $py = ("{0} {1}" -f $found.Exe, ($found.Args -join " ")).Trim()
        Write-Host "Using $py"
        & $found.Exe @($found.Args) -m venv .venv; Step "Creating .venv"
    } else {
        Write-Host "No Python 3.11+ found: getting uv to download one just for synthux." -ForegroundColor Yellow
        $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if (-not $uv) {
            $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
            if (-not (Test-Path $uv)) {
                powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
                Step "Installing uv"
            }
        }
        if (-not (Test-Path $uv)) {
            Write-Host "uv could not be installed. Install Python instead with:  winget install Python.Python.3.12" -ForegroundColor Red
            Write-Host "(or from https://www.python.org/downloads/, ticking 'Add python.exe to PATH'), then run this again."
            exit 1
        }
        $env:UV_PYTHON_PREFERENCE = "only-managed"   # uv's own Python, kept in your user folder
        & $uv venv --python 3.12 --seed .venv; Step "Creating .venv with uv"
    }
}
$venvPy = ".\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip; Step "Updating pip"
& $venvPy -m pip install -e ".[dev]"; Step "Installing synthux"
& $venvPy -m playwright install chromium; Step "Downloading Chromium"
if (-not (Test-Path "config\settings.yaml")) { Copy-Item "config\settings.example.yaml" "config\settings.yaml" }
& ".\.venv\Scripts\synthux.exe" check; Step "synthux check"

# ClearLoop: a copy next to this folder first, else fetched with git. Optional, so failures only warn.
$clearloop = $false
if (-not $NoClearLoop) {
    $nearby = @("..\Something-B", "..\something-b", "..\clearloop", "..\ClearLoop") |
        Where-Object { Test-Path (Join-Path $_ "src\clearloop\__init__.py") } | Select-Object -First 1
    $ErrorActionPreference = "Continue"
    if ($nearby) {
        Write-Host "Installing ClearLoop from $nearby"
        & $venvPy -m pip install -e $nearby
    } elseif (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "Installing ClearLoop from GitHub"
        if ($env:CI) { $env:GIT_TERMINAL_PROMPT = "0"; $env:GCM_INTERACTIVE = "never" }  # no login prompts in CI
        & $venvPy -m pip install "clearloop @ git+https://github.com/jmxsmith2-hash/Something-B"
    } else {
        Write-Host "(ClearLoop wasn't found next to this folder and git isn't installed; skipping it.)"
        $global:LASTEXITCODE = 1
    }
    $clearloop = ($LASTEXITCODE -eq 0) -and (Test-Path ".\.venv\Scripts\clearloop.exe")
    $ErrorActionPreference = "Stop"
    if ($clearloop) { Write-Host "ClearLoop installed: loop files and the printer monitor are on." -ForegroundColor Green }
    else { Write-Host "ClearLoop isn't installed; synthux works without it (no loop files)." -ForegroundColor Yellow }
}

function Shortcut($name, $target, $about) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath("Desktop"), "$name.lnk"))
        $link.TargetPath = (Resolve-Path $target).Path
        $link.WorkingDirectory = (Get-Location).Path
        $link.WindowStyle = 7
        $link.Description = $about
        $link.Save()
        Write-Host "Added a $name shortcut to the desktop." -ForegroundColor Green
    } catch { Write-Host "(Could not add a desktop shortcut; double-click $target instead.)" }
}
if (-not $NoShortcuts) {
    Shortcut "Synthux" "Synthux.bat" "Check CAD files and G-code, or test a website"
    if ($clearloop) { Shortcut "ClearLoop" "ClearLoop.bat" "Make loop files and watch your printers" }
}
Write-Host ""
Write-Host "Done. Double-click Synthux on the desktop (or Synthux.bat here) to open the app." -ForegroundColor Green
Write-Host "For the command line, activate it in a new PowerShell window with:"
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:  synthux physical check examples\cad\phone_stand_v1.stl --design examples\cad\phone_stand.yaml"
