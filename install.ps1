#requires -Version 5.1
<#
.SYNOPSIS
    Install ww-agent-cli dependencies (Windows / PowerShell).

.DESCRIPTION
    Creates a virtual environment (.venv by default) and installs the project
    in editable mode using pyproject.toml as the source of truth. Optional
    dependency groups map to pyproject [project.optional-dependencies]:
      -Dev   -> dev    (pytest, black, flake8, mypy, bandit, pip-audit, trustme)
      -Docs  -> docs   (python-docx, pypdf, openpyxl, pandas, …)
      -Ppt   -> skill-ppt-master (PyMuPDF, edge-tts, python-pptx, …)
      -All   -> all of the above

.EXAMPLE
    .\install.ps1                 # runtime only, into .venv
    .\install.ps1 -Dev            # runtime + dev tools
    .\install.ps1 -All            # everything
    .\install.ps1 -NoVenv         # install into the active interpreter
    .\install.ps1 -Plain          # use requirements.txt instead of pyproject extras
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Docs,
    [switch]$Ppt,
    [switch]$All,
    [switch]$NoVenv,
    [switch]$Plain,
    [string]$Python = "python",
    [string]$VenvPath = ".venv"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- 1. Verify Python >= 3.11 -------------------------------------------------
Write-Step "Checking Python interpreter ($Python)"
try {
    $ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
} catch {
    throw "Could not run '$Python'. Install Python 3.11+ or pass -Python <path>."
}
$parts = $ver.Trim().Split('.')
$major = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    throw "Python 3.11+ required, found $ver."
}
Write-Host "    Python $ver OK"

# --- 2. Virtual environment ---------------------------------------------------
if ($NoVenv) {
    $py = $Python
    Write-Step "Using active interpreter (no venv)"
} else {
    if (-not (Test-Path $VenvPath)) {
        Write-Step "Creating virtual environment at $VenvPath"
        & $Python -m venv $VenvPath
    } else {
        Write-Step "Reusing existing virtual environment at $VenvPath"
    }
    $py = Join-Path $VenvPath "Scripts\python.exe"
    if (-not (Test-Path $py)) { throw "venv python not found at $py" }
}

# --- 3. Upgrade pip -----------------------------------------------------------
Write-Step "Upgrading pip"
& $py -m pip install --upgrade pip

# --- 4. Install ---------------------------------------------------------------
if ($Plain) {
    Write-Step "Installing from requirements.txt"
    & $py -m pip install -r requirements.txt
} else {
    $extras = @()
    if ($All) {
        $extras = @("dev", "docs", "skill-ppt-master")
    } else {
        if ($Dev)  { $extras += "dev" }
        if ($Docs) { $extras += "docs" }
        if ($Ppt)  { $extras += "skill-ppt-master" }
    }
    if ($extras.Count -gt 0) {
        $spec = ".[" + ($extras -join ",") + "]"
    } else {
        $spec = "."
    }
    Write-Step "Installing editable package: $spec"
    & $py -m pip install -e $spec
}

# --- 5. Done ------------------------------------------------------------------
Write-Step "Done."
if (-not $NoVenv) {
    Write-Host ""
    Write-Host "Activate the environment with:" -ForegroundColor Yellow
    Write-Host "    $VenvPath\Scripts\Activate.ps1"
    Write-Host "Then run the CLI:" -ForegroundColor Yellow
    Write-Host "    ww-agent        # or: python cli.py"
}
