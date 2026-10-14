<#
.SYNOPSIS
  Installs Stratum-Memory from the WiX MSI attached to the GitHub Release.

.DESCRIPTION
  Contract: docs/distribution.md. The MSI is installed silently; Chocolatey
  records the uninstall key so `choco uninstall stratum-memory` works. The
  version, the download URL and the checksum below are rewritten by
  .github/workflows/release.yml on every tag.
#>

$ErrorActionPreference = 'Stop'

$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

$packageArgs = @{
  packageName    = $env:ChocolateyPackageName
  fileType       = 'msi'
  url64bit       = 'https://github.com/TotalBassJunkie/stratum-memory/releases/download/v0.1.0/Stratum-0.1.0-x64.msi'
  # updated by .github/workflows/release.yml
  checksum64     = '0000000000000000000000000000000000000000000000000000000000000000'
  checksumType64 = 'sha256'
  silentArgs     = '/qn /norestart'
  validExitCodes = @(0, 3010, 1641)
  softwareName   = 'Stratum-Memory*'
}

Install-ChocolateyPackage @packageArgs
