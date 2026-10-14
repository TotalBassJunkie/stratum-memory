<#
.SYNOPSIS
    Stratum-Memory - one-line installer for Windows (PowerShell 5.1+).

.DESCRIPTION
    irm https://get.stratum.dev/install.ps1 | iex

    This script installs exactly one thing: the `stratum` command-line tool
    (from PyPI, via pipx and falling back to pip). It downloads nothing else
    and never pipes a remote script into a shell. The Docker stack (Postgres +
    pgvector, Caddy, worker) is NOT installed here - run `stratum install`
    afterwards to bootstrap it.

.PARAMETER Version
    Version to install, e.g. "0.1.0". Defaults to the latest release.

.PARAMETER DryRun
    Print what would happen and change nothing.

.EXAMPLE
    irm https://get.stratum.dev/install.ps1 | iex

.EXAMPLE
    $s = irm https://get.stratum.dev/install.ps1; & ([scriptblock]::Create($s)) -Version 0.1.0 -DryRun
#>
[CmdletBinding()]
param(
    [string] $Version = 'latest',
    [switch] $DryRun
)

# `iex` (Invoke-Expression) invokes the script body without binding parameters,
# so a piped script can never receive -Version/-DryRun that way. The
# environment variables below are the equivalent knobs for that path; an
# explicit parameter always wins.
if (-not $PSBoundParameters.ContainsKey('Version') -and $env:STRATUM_VERSION) {
    $Version = $env:STRATUM_VERSION
}
if (-not $PSBoundParameters.ContainsKey('DryRun') -and $env:STRATUM_DRY_RUN -eq '1') {
    $DryRun = $true
}

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Package = 'stratum-memory'
$NpmPackage = 'stratum-memory'
$MinPython = [version]'3.12'

# ------------------------------------------------------------------ helpers

function Write-Step {
    param([Parameter(Mandatory)][string] $Message)
    Write-Host $Message
}

function Write-Warn {
    param([Parameter(Mandatory)][string] $Message)
    Write-Warning $Message
}

function Fail {
    param([Parameter(Mandatory)][string] $Message)
    Write-Error $Message
    exit 1
}

function Test-Command {
    param([Parameter(Mandatory)][string] $Name)
    return [bool] (Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Invoke-Step {
    <# Run a native command, or only describe it when -DryRun is set. #>
    param(
        [Parameter(Mandatory)][string] $Exe,
        [Parameter(Mandatory)][string[]] $Arguments
    )
    if ($DryRun) {
        Write-Host ("  [dry-run] {0} {1}" -f $Exe, ($Arguments -join ' '))
        return $true
    }
    & $Exe @Arguments
    return ($LASTEXITCODE -eq 0)
}

# ------------------------------------------------------------ version spec

$Spec = $Package
if ($Version -and $Version -ne 'latest') {
    $Spec = '{0}=={1}' -f $Package, ($Version -replace '^v', '')
}

# ------------------------------------------------------- Python interpreter

$PythonExe = $null
$PythonArgs = @()
$PythonVersion = 'unknown'

function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() },
        @{ Exe = 'python3'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Test-Command $candidate.Exe)) { continue }
        try {
            $probe = & $candidate.Exe @($candidate.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>&1
            if ($LASTEXITCODE -ne 0) { continue }
            $versionText = & $candidate.Exe @($candidate.Args) -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1
            $script:PythonExe = $candidate.Exe
            $script:PythonArgs = @($candidate.Args)
            $script:PythonVersion = "$versionText".Trim()
            return $true
        }
        catch {
            continue
        }
    }
    return $false
}

function Show-MissingPython {
    Write-Host ''
    Write-Error @"
error: Stratum-Memory needs Python >= 3.12, and no suitable interpreter was found.

Install it and re-run this script:

  winget install --id Python.Python.3.12 -e
  # or: https://www.python.org/downloads/windows/

Then: irm https://get.stratum.dev/install.ps1 | iex
"@
    exit 1
}

# -------------------------------------------------------------- installation

$InstalledWith = ''

function Install-Cli {
    if (Test-Command 'pipx') {
        Write-Step "Installing $Spec with pipx ..."
        if (Invoke-Step -Exe 'pipx' -Arguments @('install', $Spec)) {
            $script:InstalledWith = 'pipx'
            return
        }
        Write-Warn 'pipx install failed; falling back to pip.'
    }
    else {
        Write-Step 'pipx not found; falling back to pip.'
    }

    $pipArgs = @('-m', 'pip', 'install', '--user', $Spec)
    $pipOk = $false
    if (-not $DryRun) {
        try {
            & $PythonExe @PythonArgs -m pip --version > $null 2>&1
            $pipOk = ($LASTEXITCODE -eq 0)
        }
        catch {
            $pipOk = $false
        }
    }
    else {
        $pipOk = $true
    }

    if ($pipOk) {
        Write-Step "Installing $Spec with $PythonExe -m pip install --user ..."
        if (Invoke-Step -Exe $PythonExe -Arguments (@($PythonArgs) + $pipArgs)) {
            $script:InstalledWith = 'pip (--user)'
            return
        }
        Write-Warn 'pip install failed.'
    }
    else {
        Write-Warn "no working pip for $PythonExe ('$PythonExe -m pip' is unavailable)."
    }

    if (Test-Command 'npm') {
        Fail @"
could not install $Spec with pipx or pip. Try another channel instead:

  winget install --id TotalBassJunkie.StratumMemory -e
  npm i -g $NpmPackage

then re-run: stratum install
"@
    }
    Fail @"
could not install $Spec with pipx or pip, and npm is not available.

  winget install --id TotalBassJunkie.StratumMemory -e

then re-run: stratum install
"@
}

# ------------------------------------------------------------------ verify

function Find-StratumBin {
    $command = Get-Command -Name 'stratum' -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $candidates = @(
        (Join-Path $env:APPDATA 'Python\Scripts\stratum.exe'),
        (Join-Path $env:LOCALAPPDATA ('Programs\Python\Scripts\stratum.exe')),
        (Join-Path $env:USERPROFILE ('.local\bin\stratum.exe')),
        (Join-Path $env:USERPROFILE ('pipx\venvs\{0}\Scripts\stratum.exe' -f $Package))
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Test-Cli {
    if ($DryRun) {
        Write-Step "dry run: skipping the 'stratum version' check."
        return
    }

    $stratumBin = Find-StratumBin
    if (-not $stratumBin) {
        Fail @"
installed $Spec with $InstalledWith, but 'stratum' is not on PATH.
Add the Scripts directory to PATH and open a new shell, e.g.:
  %APPDATA%\Python\Scripts
  %USERPROFILE%\.local\bin
"@
    }

    $output = & $stratumBin version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Step "verified: $output"
        return
    }
    $output = & $stratumBin --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Step "verified: $output"
        return
    }
    Fail "installed $Spec with $InstalledWith, but '$stratumBin version' failed:`n$output"
}

# --------------------------------------------------------------- reporting

function Show-NextSteps {
    Write-Host ''
    Write-Host 'Next steps:'
    Write-Host '  stratum install    # bootstrap the local stack (compose + .env, pull image)'
    Write-Host '  stratum up         # start the stack (docker compose up -d)'
    Write-Host '  stratum status     # container + health summary'
    Write-Host '  stratum doctor     # environment diagnostics (python, docker, ports)'
    if ($InstalledWith -eq 'pip (--user)') {
        Write-Host ''
        Write-Host "If 'stratum' is not found in a new shell, add to PATH:"
        Write-Host '  %APPDATA%\Python\Scripts'
    }
}

# --------------------------------------------------------------------- main

Write-Step 'Stratum-Memory installer'
Write-Step "  package:  $Spec"
Write-Step "  dry run:  $([bool]$DryRun)"

if (-not (Find-Python)) {
    Show-MissingPython
}
Write-Step "  python:   $PythonExe $PythonVersion"

Install-Cli
Test-Cli
Show-NextSteps
