<#
.SYNOPSIS
    Build the Windows MSI (WiX v4) for Stratum-Memory.

.DESCRIPTION
    Produces the release asset named by docs/distribution.md:

        Stratum-<version>-x64.msi

    The version is NEVER hard-coded: it must be passed with -Version (or, when
    omitted, read from src/stratum/__init__.py) and is handed to WiX as
    `-d Version=<version>`.

    Steps:
      1. make sure the WiX .NET tool is installed (dotnet tool install --global wix),
      2. freeze the CLI with PyInstaller (packaging/windows/stratum.spec) into dist/stratum.exe,
      3. compile + link packaging/windows/stratum.wxs with `wix build -arch x64`,
      4. leave the artefact in dist/ under the contract name.

.PARAMETER Version
    Product version, e.g. "0.1.0". Defaults to the value of `__version__` in
    src/stratum/__init__.py (the single source of truth per docs/distribution.md).

.PARAMETER OutputDirectory
    Where the MSI is written. Default: <repo>\dist.

.PARAMETER Python
    Python interpreter used for PyInstaller and the version lookup.
    Default: the repository virtual environment if present, else "python".

.EXAMPLE
    .\packaging\windows\build-msi.ps1 -Version 0.1.0
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
$WxsFile = Join-Path $PSScriptRoot 'stratum.wxs'
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

$MsiName = "Stratum-$Version-x64.msi"
$MsiPath = Join-Path $OutputDirectory $MsiName

Write-Host "build-msi: version  $Version"
Write-Host "build-msi: output   $MsiPath"

# ------------------------------------------------------------------- WiX ---
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        throw "Neither 'wix' nor 'dotnet' is on PATH. Install the .NET SDK, then re-run."
    }
    Write-Host 'build-msi: installing the WiX .NET tool (dotnet tool install --global wix)'
    & dotnet tool install --global wix
    if ($LASTEXITCODE -ne 0) { throw "dotnet tool install --global wix failed with exit code $LASTEXITCODE" }
    $dotnetTools = Join-Path $env:USERPROFILE '.dotnet\tools'
    if (Test-Path $dotnetTools) { $env:PATH = "$dotnetTools;$env:PATH" }
}
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw "'wix' is still not on PATH after installation. Open a new shell or add ~/.dotnet/tools to PATH."
}
& wix --version

# -------------------------------------------------------------- PyInstaller --
Write-Host 'build-msi: freezing stratum.exe with PyInstaller'
Push-Location $RepoRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $RepoRoot 'dist') `
        --workpath (Join-Path $RepoRoot 'build') $SpecFile
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
if (-not (Test-Path $ExePath)) { throw "PyInstaller did not produce $ExePath" }

# -------------------------------------------------------------------- MSI --
Write-Host 'build-msi: building the MSI with WiX v4'
& wix build -arch x64 `
    -d "Version=$Version" `
    -d "ExePath=$ExePath" `
    -d "IconPath=$IconPath" `
    -o $MsiPath `
    $WxsFile
if ($LASTEXITCODE -ne 0) { throw "wix build failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $MsiPath)) { throw "wix build did not produce $MsiPath" }

Write-Host "build-msi: wrote $MsiPath"
Write-Host "Install it with: msiexec /i `"$MsiName`""
