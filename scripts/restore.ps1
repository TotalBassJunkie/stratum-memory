<#
.SYNOPSIS
    Stratum-Memory restore (WP-15) for the Windows development host.

.DESCRIPTION
    Windows counterpart of scripts/restore.sh. Non-interactive: every input is a
    parameter. Restores a pg_dump custom-format archive into a target database
    and/or extracts a vault tarball.

    File-name contract (see docs/backup-restore.md):
      database dump    stratum-YYYYmmdd-HHMMSS.dump   (regex: stratum-\d{8}-\d{6}\.dump)
      vault archive    vault-YYYYmmdd-HHMMSS.tar.gz   (regex: vault-\d{8}-\d{6}\.tar\.gz)

      .\restore.ps1 -Dump .\backups\stratum-20260101-030000.dump -TargetDb stratum
      .\restore.ps1 -Dump .\backups\stratum-20260101-030000.dump -Verify
      .\restore.ps1 -Vault .\backups\vault-20260101-030000.tar.gz -TargetDir .\kb-restored

    Exit codes: 0 ok, 1 usage/verification, 2 dump missing, 3 pg_restore failed,
    4 vault missing, 5 vault extraction failed, 6 missing dependency.

.EXAMPLE
    .\restore.ps1 -Dump .\backups\stratum-20260101-030000.dump -TargetDb stratum_restore_drill -Force
#>
[CmdletBinding()]
param(
    # pg_dump custom-format archive to restore.
    [string]$Dump,
    # Vault tarball (.tar.gz) to extract.
    [string]$Vault,
    # Database to restore INTO (required for a real restore).
    [string]$TargetDb,
    # Directory the vault archive is extracted into (required with -Vault).
    [string]$TargetDir,
    # Only check the dump's integrity (pg_restore --list); no writes.
    [switch]$Verify,
    # Pass --clean --if-exists so existing objects are dropped first.
    [switch]$Force,
    # Compose file used for `docker compose -f`. Empty -> ..\docker-compose.yml.
    [string]$ComposeFile,
    # Compose service exposing Postgres.
    [string]$DbService = $(if ($env:STRATUM_RESTORE_DB_SERVICE) { $env:STRATUM_RESTORE_DB_SERVICE } else { 'stratum-db' }),
    # Database role used to connect.
    [string]$DbUser = $(if ($env:STRATUM_RESTORE_DB_USER) { $env:STRATUM_RESTORE_DB_USER } else { 'stratum' }),
    # Print the work without writing anything.
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-StratumLog {
    param([string]$Level, [string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Write-Host ('{0} [{1}] {2}' -f $stamp, $Level, $Message)
}

function Stop-Stratum {
    param([int]$Code, [string]$Message)
    Write-StratumLog -Level 'ERROR' -Message $Message
    exit $Code
}

function Get-DockerExe {
    # Prefer PATH (an explicit STRATUM_DOCKER still wins), so a docker.cmd shim
    # is honoured on Windows and standard installs resolve naturally.
    foreach ($candidate in @($env:STRATUM_DOCKER, 'docker.exe', 'docker')) {
        if (-not $candidate) { continue }
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    Stop-Stratum -Code 6 -Message 'required command not found: docker'
}

function Assert-Readable {
    param([string]$Path, [int]$Code)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Stop-Stratum -Code $Code -Message "archive not found: $Path"
    }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) {
        Stop-Stratum -Code $Code -Message "archive is empty: $Path"
    }
}

function Invoke-Compose {
    <#
    .SYNOPSIS
        Run `docker compose exec -T <service> <cmd...>`, optionally feeding stdin.
    #>
    param([string]$Service, [string[]]$Cmd, [string]$InFile)
    $docker = Get-DockerExe
    $scriptArgs = @('compose', '-f', $ComposeFile, 'exec', '-T', $Service) + $Cmd

    if ($DryRun) {
        $shown = 'docker ' + ($scriptArgs -join ' ')
        if ($InFile) { $shown += " < $InFile" }
        Write-StratumLog -Level 'INFO' -Message $shown
        return 0
    }

    $errTmp = [System.IO.Path]::GetTempFileName()
    $startArgs = @{
        FilePath              = $docker
        ArgumentList          = $scriptArgs
        NoNewWindow           = $true
        Wait                  = $true
        PassThru              = $true
        RedirectStandardError = $errTmp
    }
    if ($InFile) { $startArgs.RedirectStandardInput = $InFile }

    $proc = Start-Process @startArgs
    $err = (Get-Content -LiteralPath $errTmp -Raw -ErrorAction SilentlyContinue)
    Remove-Item -LiteralPath $errTmp -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { throw "docker compose exec failed (exit $($proc.ExitCode)): $err" }
    return 0
}

function Verify-Dump {
    param([string]$File)
    Write-StratumLog -Level 'INFO' -Message "verify: $(Split-Path -Leaf $File)"
    try {
        Invoke-Compose -Service $DbService -Cmd @('pg_restore', '--list') -InFile $File | Out-Null
    } catch {
        Stop-Stratum -Code 1 -Message "dump failed integrity check: $_"
    }
    Write-StratumLog -Level 'INFO' -Message 'verify: OK'
}

function Restore-Dump {
    param([string]$File, [string]$Db)
    Write-StratumLog -Level 'INFO' -Message "restore: $(Split-Path -Leaf $File) -> db '$Db' (force=$($Force.IsPresent))"
    $cmd = @('pg_restore', '-U', $DbUser, '-d', $Db, '--no-owner', '--no-privileges', '--single-transaction')
    if ($Force) { $cmd += @('--clean', '--if-exists') }

    try {
        # A missing target database cannot be created by pg_restore; create it first.
        $exists = $false
        if (-not $DryRun) {
            $docker = Get-DockerExe
            $scriptArgs = @('compose', '-f', $ComposeFile, 'exec', '-T', $DbService,
                'psql', '-U', $DbUser, '-d', 'postgres', '-tAc',
                "SELECT 1 FROM pg_database WHERE datname = '$Db'")
            $probe = (& $docker @scriptArgs 2>$null)
            $exists = ($probe -match '^\s*1\s*$')
        }
        if (-not $exists -and -not $DryRun) {
            Write-StratumLog -Level 'WARN' -Message "target database '$Db' does not exist, creating it"
            $docker = Get-DockerExe
            $mkArgs = @('compose', '-f', $ComposeFile, 'exec', '-T', $DbService, 'createdb', '-U', $DbUser, $Db)
            & $docker @mkArgs
            if ($LASTEXITCODE -ne 0) { throw "could not create target database '$Db'" }
        }
        Invoke-Compose -Service $DbService -Cmd $cmd -InFile $File | Out-Null
    } catch {
        Stop-Stratum -Code 3 -Message "pg_restore failed: $_"
    }
    Write-StratumLog -Level 'INFO' -Message 'restore: OK'
}

function Restore-Vault {
    param([string]$File, [string]$Dir)
    Write-StratumLog -Level 'INFO' -Message "extract vault: $(Split-Path -Leaf $File) -> $Dir"
    if ($DryRun) {
        Write-StratumLog -Level 'INFO' -Message "New-Item -ItemType Directory $Dir; tar -C $Dir -xzf $File"
        return
    }
    if (-not (Test-Path -LiteralPath $Dir)) {
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    }
    $tarCmd = Get-Command tar -ErrorAction SilentlyContinue
    if (-not $tarCmd) { Stop-Stratum -Code 6 -Message 'required command not found: tar' }
    & $tarCmd.Source -C $Dir -xzf $File
    if ($LASTEXITCODE -ne 0) { Stop-Stratum -Code 5 -Message 'vault extraction failed' }
    Write-StratumLog -Level 'INFO' -Message 'extract: OK'
}

# --------------------------------------------------------------------- main
# `$PSScriptRoot` is reliable only at run time; resolve the dependent default now.
if (-not $ComposeFile) {
    $ComposeFile = Join-Path $PSScriptRoot '..\docker-compose.yml'
}
$ComposeFile = [System.IO.Path]::GetFullPath($ComposeFile)

if (-not $Dump -and -not $Vault) {
    Stop-Stratum -Code 1 -Message 'nothing to restore: pass -Dump and/or -Vault'
}
if ($Dump -and -not $Verify -and -not $TargetDb) {
    Stop-Stratum -Code 1 -Message '-TargetDb is required for a real restore (or use -Verify)'
}
if ($Vault -and -not $TargetDir) {
    Stop-Stratum -Code 1 -Message '-TargetDir is required to extract a vault archive'
}

if ($Dump) {
    Assert-Readable -Path $Dump -Code 2
    Verify-Dump -File $Dump
    if ($Verify) {
        Write-StratumLog -Level 'INFO' -Message 'verify-only: no data written'
    } else {
        Restore-Dump -File $Dump -Db $TargetDb
    }
}

if ($Vault) {
    Assert-Readable -Path $Vault -Code 4
    Restore-Vault -File $Vault -Dir $TargetDir
}

Write-StratumLog -Level 'INFO' -Message '=== restore finished ==='
exit 0
