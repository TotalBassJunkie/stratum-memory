<#
.SYNOPSIS
    Build the Windows EXE installer (Inno Setup) for Stratum-Memory.

.DESCRIPTION
    Produces the release asset named by docs/distribution.md:

        Stratum-<version>-setup.exe

    The version is NEVER hard-coded: it must be passed with -Version (or, when
    omitted, read from src/stratum/__init__.py) and reaches the Inno Setup
    preprocessor as `/DVersion=<version>`.

    Steps:
      1. make sure Inno Setup 6 is installed (winget, falling back to choco),
      2. freeze the CLI with PyInstaller (packaging/windows/stratum.spec) into dist/stratum.exe,
      3. compile packaging/windows/stratum.iss with ISCC,
      4. leave the artefact in dist/ under the contract name.

.PARAMETER Version
    Application version, e.g. "0.1.0". Defaults to the value of `__version__`
    in src/stratum/__init__.py (the single source of truth per docs/distribution.md).

.PARAMETER OutputDirectory
    Where the setup .exe is written. Default: <repo>\dist.

.PARAMETER Python
    Python interpreter used for PyInstaller and the version lookup.
    Default: the repository virtual environment if present, else "python".

.EXAMPLE
    .\packaging\windows\build-exe.ps1 -Version 0.1.0
#>
[CmdletBinding()]
param(
    [string] $Version,
    [string] $OutputDirectory,
    [string] $Python
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$SpecFile = Join-Path $PSScriptRoot 'stratum.spec'
$IssFile = Join-Path $PSScriptRoot 'stratum.iss'
$ExePath = Join-Path $RepoRoot 'dist\stratum.exe'
$IconPath = Join-Path $RepoRoot 'assets\logo\favicon.ico'

if (-not $OutputDirectory) { $OutputDirectory = Join-Path $RepoRoot 'dist' }
if (-not (Test-Path $OutputDirectory)) { New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null }
$OutputDirectory = (Resolve-Path $OutputDirectory).Path

if (-not $Python) {
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}

# --------------------------------------------------------------- version ---
if (-not $Version) {
    $initFile = Join-Path $RepoRoot 'src\stratum\__init__.py'
    $match = Select-String -Path $initFile -Pattern '^__version__\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $match) {
        throw "Could not read __version__ from $initFile - pass -Version explicitly."
    }
    $Version = $match.Matches[0].Groups[1].Value
}
if (-not $Version) { throw '-Version must not be empty.' }

$SetupName = "Stratum-$Version-setup.exe"
$SetupPath = Join-Path $OutputDirectory $SetupName

Write-Host "build-exe: version  $Version"
Write-Host "build-exe: output   $SetupPath"

# ------------------------------------------------------------ Inno Setup ---
function Resolve-Iscc {
    <# Return a usable ISCC.exe path, or $null. #>
    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $base) { continue }
        $candidate = Join-Path $base 'Inno Setup 6\ISCC.exe'
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$Iscc = Resolve-Iscc
if (-not $Iscc) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host 'build-exe: installing Inno Setup 6 (winget)'
        # A non-zero exit code usually just means "already installed", so the
        # result is re-checked with Resolve-Iscc below instead of trusted.
        & winget install --id JRSoftware.InnoSetup --exact --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "winget exited with $LASTEXITCODE; checking whether Inno Setup is usable anyway."
        }
    } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Host 'build-exe: installing Inno Setup 6 (choco)'
        & choco install innosetup --yes --no-progress
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "choco exited with $LASTEXITCODE; checking whether Inno Setup is usable anyway."
        }
    } else {
        throw "Inno Setup 6 is not installed and neither 'winget' nor 'choco' is available. Install it from https://jrsoftware.org/isdl.php and re-run."
    }
    $Iscc = Resolve-Iscc
}
if (-not $Iscc) {
    throw "ISCC.exe not found. Install Inno Setup 6 (https://jrsoftware.org/isdl.php), open a new shell and re-run."
}
Write-Host "build-exe: ISCC     $Iscc"

# -------------------------------------------------------------- PyInstaller --
Write-Host 'build-exe: freezing stratum.exe with PyInstaller'
Push-Location $RepoRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $RepoRoot 'dist') `
        --workpath (Join-Path $RepoRoot 'build') $SpecFile
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
if (-not (Test-Path $ExePath)) { throw "PyInstaller did not produce $ExePath" }

# --------------------------------------------------------------- compile ---
Write-Host 'build-exe: compiling the installer with Inno Setup'
# `/F` sets the output base file name, so the artefact matches the contract name
# even though the .iss also composes OutputBaseFilename from the version.
& $Iscc "/DVersion=$Version" `
    "/DExePath=$ExePath" `
    "/DIconPath=$IconPath" `
    "/DLicenseFile=$(Join-Path $RepoRoot 'LICENSE')" `
    "/O$OutputDirectory" `
    "/FStratum-$Version-setup" `
    $IssFile
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $SetupPath)) { throw "ISCC did not produce $SetupPath" }

Write-Host "build-exe: wrote $SetupPath"
Write-Host "Install it by double-clicking $SetupName, or silently: `"$SetupName`" /VERYSILENT"
