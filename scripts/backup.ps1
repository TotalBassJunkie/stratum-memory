<#
.SYNOPSIS
    Stratum-Memory backup (WP-15) for the Windows development host.

.DESCRIPTION
    Windows counterpart of scripts/backup.sh. It drives the running Compose
    stack via `docker compose exec`, so it produces the very same artefacts:

      * <dir>\stratum-YYYYmmdd-HHMMSS.dump   (pg_dump --format=custom)
      * <dir>\vault-YYYYmmdd-HHMMSS.tar.gz   (tar of the `kb` Git vault)

    and then applies name-based retention ("keep the newest N per family").
    The retention rule is the normative mirror of the pure-Python reference
    scripts/backup_lib.py::select_backups_to_prune().

    File-name contract (see docs/backup-restore.md):
      database dump    stratum-YYYYmmdd-HHMMSS.dump   (regex: stratum-\d{8}-\d{6}\.dump)
      vault archive    vault-YYYYmmdd-HHMMSS.tar.gz   (regex: vault-\d{8}-\d{6}\.tar\.gz)

    Non-interactive; all inputs are parameters or environment variables.

    Exit codes: 0 ok, 1 usage, 2 dump failed, 3 vault failed, 4 retention failed,
    5 missing dependency.

.EXAMPLE
    .\backup.ps1 -DryRun
    .\backup.ps1 -Dir D:\ssd\stratum\backups -Keep 14
#>
[CmdletBinding()]
param(
    # Backup target directory. Empty -> $env:STRATUM_BACKUP_DIR or ..\backups.
    [string]$Dir,
    # Number of newest archives kept per family (0 = keep none).
    [int]$Keep = 14,
    # Compose file used for `docker compose -f`. Empty -> ..\docker-compose.yml.
    [string]$ComposeFile,
    # Compose service exposing Postgres.
    [string]$DbService = $(if ($env:STRATUM_BACKUP_DB_SERVICE) { $env:STRATUM_BACKUP_DB_SERVICE } else { 'stratum-db' }),
    # Database name to dump.
    [string]$DbName = $(if ($env:STRATUM_BACKUP_DB_NAME) { $env:STRATUM_BACKUP_DB_NAME } else { 'stratum' }),
    # Database role used by pg_dump.
    [string]$DbUser = $(if ($env:STRATUM_BACKUP_DB_USER) { $env:STRATUM_BACKUP_DB_USER } else { 'stratum' }),
    # Compose service exposing the vault volume.
    [string]$VaultService = $(if ($env:STRATUM_BACKUP_VAULT_SERVICE) { $env:STRATUM_BACKUP_VAULT_SERVICE } else { 'stratum-core' }),
    # In-container path of the vault.
    [string]$VaultPath = $(if ($env:STRATUM_BACKUP_VAULT_PATH) { $env:STRATUM_BACKUP_VAULT_PATH } else { '/var/stratum/kb' }),
    # Print the work without writing or deleting anything.
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:LogFile = $null

function Get-Stamp {
    (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
}

function Write-StratumLog {
    param([string]$Level, [string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = '{0} [{1}] {2}' -f $stamp, $Level, $Message
    Write-Host $line
    if ($script:LogFile) {
        Add-Content -LiteralPath $script:LogFile -Value $line -ErrorAction SilentlyContinue
    }
}

function Stop-Stratum {
    param([int]$Code, [string]$Message)
    Write-StratumLog -Level 'ERROR' -Message $Message
    exit $Code
}

function Get-StratumPruneList {
    <#
    .SYNOPSIS
        Names of the archives to delete so only the newest $Keep remain.
    .DESCRIPTION
        Name-based retention, port of backup_lib.select_backups_to_prune().
        Names without a valid YYYYmmdd-HHMMSS stamp are never pruned.
    #>
    param([string[]]$Names, [int]$Keep)

    if ($Keep -lt 0) { throw 'Keep must be >= 0' }

    $stamped = @()
    foreach ($name in @($Names)) {
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $match = [regex]::Match($name, '\d{8}-\d{6}')
        if (-not $match.Success) { continue }
        $parsed = [datetime]::MinValue
        $styles = [System.Globalization.DateTimeStyles]::None
        $ok = [datetime]::TryParseExact(
            $match.Value, 'yyyyMMdd-HHmmss', [System.Globalization.CultureInfo]::InvariantCulture,
            $styles, [ref]$parsed)
        if (-not $ok) { continue }
        $stamped += [pscustomobject]@{ Name = $name; Stamp = $parsed }
    }

    $sorted = @($stamped | Sort-Object -Property Stamp, Name -Descending)
    if ($sorted.Count -le $Keep) { return @() }
    return @(
        $sorted |
            Select-Object -Skip $Keep |
            Sort-Object -Property Stamp, Name |
            ForEach-Object { $_.Name }
    )
}

function Get-DockerExe {
    # Prefer PATH (an explicit STRATUM_DOCKER still wins), so a docker.cmd shim
    # is honoured on Windows and standard installs resolve naturally.
    foreach ($candidate in @($env:STRATUM_DOCKER, 'docker.exe', 'docker')) {
        if (-not $candidate) { continue }
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    Stop-Stratum -Code 5 -Message 'required command not found: docker'
}

function Invoke-ComposeCapture {
    <#
    .SYNOPSIS
        Run `docker compose exec -T <service> <cmd...>` and capture stdout as bytes.
    .DESCRIPTION
        Start-Process redirection performs a byte-level copy, so binary payloads
        (custom-format dumps, gzip streams) survive intact — unlike PowerShell's
        text pipelines.
    #>
    param([string]$Service, [string[]]$Cmd, [string]$OutFile, [switch]$FromStdin, [string]$InFile)

    $docker = Get-DockerExe
    $scriptArgs = @('compose', '-f', $ComposeFile, 'exec', '-T', $Service) + $Cmd

    if ($DryRun) {
        $shown = 'docker ' + ($scriptArgs -join ' ')
        if ($FromStdin) { $shown += " < $InFile" } else { $shown += " > $OutFile" }
        Write-StratumLog -Level 'INFO' -Message $shown
        return 0
    }

    $outTmp = "$OutFile.stdout.tmp"
    $errTmp = "$OutFile.stderr.tmp"
    $startArgs = @{
        FilePath               = $docker
        ArgumentList           = $scriptArgs
        NoNewWindow            = $true
        Wait                   = $true
        PassThru               = $true
        RedirectStandardOutput = $outTmp
        RedirectStandardError  = $errTmp
    }
    if ($FromStdin) {
        $startArgs.RedirectStandardInput = $InFile
        $startArgs.Remove('RedirectStandardOutput') | Out-Null
    }

    $proc = Start-Process @startArgs
    $err = ''
    if (Test-Path -LiteralPath $errTmp) { $err = (Get-Content -LiteralPath $errTmp -Raw) }
    Remove-Item -LiteralPath $errTmp -Force -ErrorAction SilentlyContinue

    if (-not $FromStdin) {
        if ($proc.ExitCode -ne 0) {
            $msg = "docker compose exec failed (exit $($proc.ExitCode)): $err"
            Remove-Item -LiteralPath $outTmp -Force -ErrorAction SilentlyContinue
            throw $msg
        }
        if (-not (Test-Path -LiteralPath $outTmp) -or (Get-Item -LiteralPath $outTmp).Length -eq 0) {
            Remove-Item -LiteralPath $outTmp -Force -ErrorAction SilentlyContinue
            throw "captured output is empty: $OutFile"
        }
        Move-Item -LiteralPath $outTmp -Destination $OutFile -Force
    } else {
        if ($proc.ExitCode -ne 0) { throw "docker compose exec failed (exit $($proc.ExitCode)): $err" }
    }
    return 0
}

function Backup-Database {
    param([string]$OutFile)
    Write-StratumLog -Level 'INFO' -Message "pg_dump service '$DbService' -> $(Split-Path -Leaf $OutFile)"
    $cmd = @('pg_dump', '-U', $DbUser, '-d', $DbName, '--format=custom', '--no-owner', '--no-privileges')
    try {
        Invoke-ComposeCapture -Service $DbService -Cmd $cmd -OutFile $OutFile | Out-Null
    } catch {
        Stop-Stratum -Code 2 -Message "database dump failed: $_"
    }
}

function Backup-Vault {
    param([string]$OutFile)
    Write-StratumLog -Level 'INFO' -Message "tar service '$VaultService' ($VaultPath) -> $(Split-Path -Leaf $OutFile)"
    $cmd = @('tar', '-C', $VaultPath, '-czf', '-', '.')
    try {
        Invoke-ComposeCapture -Service $VaultService -Cmd $cmd -OutFile $OutFile | Out-Null
    } catch {
        Stop-Stratum -Code 3 -Message "vault archive failed: $_"
    }
}

function Invoke-Retention {
    param([string]$Prefix, [string]$Suffix)
    $pattern = "$Prefix*$Suffix"
    $names = @()
    if (Test-Path -LiteralPath $Dir) {
        $names = @(
            Get-ChildItem -LiteralPath $Dir -File -Filter $pattern |
                Select-Object -ExpandProperty Name
        )
    }
    $toPrune = @(Get-StratumPruneList -Names $names -Keep $Keep)

    $removed = 0
    foreach ($name in $toPrune) {
        $target = Join-Path $Dir $name
        if ($DryRun) {
            Write-StratumLog -Level 'INFO' -Message "Remove-Item $target"
            $removed++
        } else {
            # Stop on error so a failed delete is never counted as removed; then
            # confirm the file is really gone before incrementing the counter.
            Remove-Item -LiteralPath $target -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $target)) {
                $removed++
            } else {
                Write-StratumLog -Level 'WARN' -Message "could not remove $name (still present)"
            }
        }
    }
    Write-StratumLog -Level 'INFO' -Message ("retention {0}: removed {1}, kept {2}" -f $pattern, $removed, ($names.Count - $toPrune.Count))
}

# --------------------------------------------------------------------- main
# `$PSScriptRoot` is only reliable once the script runs, so the path defaults
# that depend on it are resolved here rather than in the param() block.
if (-not $Dir) {
    $Dir = if ($env:STRATUM_BACKUP_DIR) { $env:STRATUM_BACKUP_DIR } else { Join-Path $PSScriptRoot '..\backups' }
}
if (-not $ComposeFile) {
    $ComposeFile = Join-Path $PSScriptRoot '..\docker-compose.yml'
}

$Dir = [System.IO.Path]::GetFullPath($Dir)
$ComposeFile = [System.IO.Path]::GetFullPath($ComposeFile)

Write-StratumLog -Level 'INFO' -Message "target dir: $Dir (keep=$Keep, dryRun=$($DryRun.IsPresent))"

if (-not $DryRun) {
    if (-not (Test-Path -LiteralPath $Dir)) {
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    }
}
if (Test-Path -LiteralPath $Dir) {
    $script:LogFile = Join-Path $Dir 'backup.log'
}

$stamp = Get-Stamp
$dumpFile = Join-Path $Dir "stratum-$stamp.dump"
$vaultFile = Join-Path $Dir "vault-$stamp.tar.gz"

Write-StratumLog -Level 'INFO' -Message "=== Stratum backup $stamp starting ==="
Backup-Database -OutFile $dumpFile
Backup-Vault -OutFile $vaultFile
Invoke-Retention -Prefix 'stratum-' -Suffix '.dump'
Invoke-Retention -Prefix 'vault-' -Suffix '.tar.gz'
Write-StratumLog -Level 'INFO' -Message "=== Stratum backup finished (dump=$(Split-Path -Leaf $dumpFile), vault=$(Split-Path -Leaf $vaultFile)) ==="
exit 0
