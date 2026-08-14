param(
    [Parameter(Mandatory = $true)]
    [string]$BatchRoot,
    [Parameter(Mandatory = $true)]
    [string]$ServerRoot,
    [string]$StartDate = "2024-04-01",
    [string]$EndDate = "2024-04-01",
    [string]$CondaEnvironment = "pytorch",
    [string]$Platforms = "GOES-16,GOES-18",
    [ValidateRange(1, 16)]
    [int]$InventoryWorkers = 8,
    [ValidateRange(1, 16)]
    [int]$DownloadWorkers = 4,
    [switch]$AdaptiveDownload,
    [ValidateRange(1, 16)]
    [int]$DownloadMinWorkers = 2,
    [ValidateRange(1, 16)]
    [int]$DownloadInitialWorkers = 4,
    [ValidateRange(1, 64)]
    [int]$S3RangeMiB = 4,
    [switch]$RefreshInventory,
    [switch]$SkipGoes,
    [switch]$SkipMeteosat
)

$ErrorActionPreference = "Stop"

$PathConfig = Join-Path $PSScriptRoot "..\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1"
. $PathConfig
$COMPONENT_ROLE = "data_transfer_orchestrator"

$Downloader = Join-Path $PSScriptRoot "geo_cloud_downloader.py"
$TransferTool = Join-Path $PSScriptRoot "geo_ring_cloud_transfer_batch.py"
$BatchRoot = [System.IO.Path]::GetFullPath($BatchRoot)
$TransferRoot = Join-Path $BatchRoot "transfer"
$StatusPath = Join-Path $TransferRoot "batch_status.json"
$RunLog = Join-Path $TransferRoot "batch_run.log"
$LockPath = Join-Path $TransferRoot "batch_run.lock"
$CondaExe = $GeoRingCondaExe
$AllowedPlatforms = @("GOES-16", "GOES-18", "Himawari-9", "Meteosat-0deg", "Meteosat-IODC")
$SelectedPlatforms = @(
    $Platforms.Split(",") |
        ForEach-Object { $_.Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
)
foreach ($Platform in $SelectedPlatforms) {
    if ($Platform -notin $AllowedPlatforms) {
        throw "Unsupported platform: $Platform"
    }
}
if ($SkipGoes) {
    $SelectedPlatforms = @($SelectedPlatforms | Where-Object { -not $_.StartsWith("GOES-") })
}
if ($SkipMeteosat) {
    $SelectedPlatforms = @($SelectedPlatforms | Where-Object { -not $_.StartsWith("Meteosat-") })
}
if ($SelectedPlatforms.Count -eq 0) {
    throw "At least one platform must be selected."
}
$S3Platforms = @($SelectedPlatforms | Where-Object { $_.StartsWith("GOES-") -or $_ -eq "Himawari-9" })
$MeteosatPlatforms = @($SelectedPlatforms | Where-Object { $_.StartsWith("Meteosat-") })

$ScriptSha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
$ProjectPrefix = $GeoRingProjectRoot.TrimEnd("\") + "\"
$ScriptRelativePath = if ($PSCommandPath.StartsWith($ProjectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    $PSCommandPath.Substring($ProjectPrefix.Length).Replace("\", "/")
} else {
    ""
}
$CodeCommit = ""
$CommitBlob = ""
$WorktreeBlob = ""
$GitState = "outside_repository"
if ($ScriptRelativePath) {
    $CodeCommit = ((& git -C $GeoRingProjectRoot rev-parse HEAD 2>$null) | Select-Object -First 1)
    $TrackedOutput = ((& git -C $GeoRingProjectRoot ls-files --error-unmatch -- $ScriptRelativePath 2>$null) | Select-Object -First 1)
    $IsTracked = $LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($TrackedOutput)
    $WorktreeBlob = ((& git -C $GeoRingProjectRoot hash-object -- $ScriptRelativePath 2>$null) | Select-Object -First 1)
    $CommitBlob = ((& git -C $GeoRingProjectRoot rev-parse "HEAD:$ScriptRelativePath" 2>$null) | Select-Object -First 1)
    $StatusLine = ((& git -C $GeoRingProjectRoot status --porcelain=v1 --untracked-files=all -- $ScriptRelativePath 2>$null) | Select-Object -First 1)
    if (-not $IsTracked -or ($StatusLine -and $StatusLine.StartsWith("??"))) {
        $GitState = "untracked"
    } elseif ([string]::IsNullOrEmpty($StatusLine)) {
        $GitState = "clean"
    } elseif ($StatusLine.Length -ge 2 -and $StatusLine[0] -ne " " -and $StatusLine[1] -ne " ") {
        $GitState = "staged_and_modified"
    } elseif ($StatusLine.Length -ge 1 -and $StatusLine[0] -ne " ") {
        $GitState = "staged"
    } else {
        $GitState = "modified"
    }
}
$CommitRepresentsScript = (
    $GitState -eq "clean" -and
    -not [string]::IsNullOrWhiteSpace($CommitBlob) -and
    $CommitBlob -eq $WorktreeBlob
)
$GeneratingScriptState = [ordered]@{
    path = $PSCommandPath
    repository_relative_path = $ScriptRelativePath
    sha256 = $ScriptSha256
    git_state = $GitState
    git_tracked = $IsTracked
    worktree_blob = $WorktreeBlob
    commit_blob = $CommitBlob
    commit_represents_script = $CommitRepresentsScript
}

function Write-BatchStatus {
    param([string]$Phase, [string]$Status, [string]$Message = "")
    $payload = [ordered]@{
        project_id = "geo_ring_cloud"
        canonical_stage_id = ""
        component_role = $COMPONENT_ROLE
        related_stage_ids = @("stage_00", "stage_00f")
        generating_script = $PSCommandPath
        code_commit = $CodeCommit
        code_commit_scope = "repository_head_at_run_start"
        generating_script_state = $GeneratingScriptState
        lineage_warnings = $(
            if ($CommitRepresentsScript) {
                @()
            } else {
                @("code_commit does not fully represent the generating script content")
            }
        )
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
        phase = $Phase
        status = $Status
        message = $Message
        start_date = $StartDate
        end_date = $EndDate
        batch_root = $BatchRoot
        server_root = $ServerRoot
        platforms = $SelectedPlatforms
        inventory_workers = $InventoryWorkers
        download_workers = $DownloadWorkers
        download_parallelism_mode = $(if ($AdaptiveDownload) { "adaptive" } else { "fixed" })
        download_min_workers = $DownloadMinWorkers
        download_initial_workers = $DownloadInitialWorkers
        s3_range_mib = $S3RangeMiB
        network_mode = "direct_only"
        automatic_delete = $false
    }
    $json = $payload | ConvertTo-Json -Depth 4
    $lastError = $null
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $temporaryPath = "{0}.{1}.{2}.tmp" -f $StatusPath, $PID, [DateTime]::UtcNow.Ticks
        try {
            [System.IO.File]::WriteAllText(
                $temporaryPath,
                $json,
                (New-Object System.Text.UTF8Encoding($false))
            )
            Move-Item -LiteralPath $temporaryPath -Destination $StatusPath -Force -ErrorAction Stop
            return
        }
        catch {
            $lastError = $_
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 12) {
                Start-Sleep -Milliseconds 250
            }
        }
    }
    throw $lastError
}

function Invoke-DownloadPython {
    param([string[]]$Arguments)
    # EUMETSAT's client writes recoverable retry notices (for example HTTP 503)
    # to stderr even when the command ultimately succeeds.  With the script-wide
    # ErrorActionPreference=Stop, PowerShell otherwise promotes that native
    # stderr record to a terminating NativeCommandError before LASTEXITCODE can
    # be inspected.  Capture the complete diagnostic stream, then decide solely
    # from the native process exit code.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $commandOutput = & $CondaExe run -n $CondaEnvironment python @Arguments 2>&1
        $commandExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($commandOutput) {
        $commandOutput | Add-Content -LiteralPath $RunLog -Encoding UTF8
    }
    if ($commandExitCode -ne 0) {
        throw "Download command failed with exit code $commandExitCode"
    }
}

function Clear-DownloadProxy {
    Remove-Item Env:\HTTP_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\HTTPS_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\http_proxy -ErrorAction SilentlyContinue
    Remove-Item Env:\https_proxy -ErrorAction SilentlyContinue
    Remove-Item Env:\ALL_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\all_proxy -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_CLOUD_GOES_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_CLOUD_HIMAWARI_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_CLOUD_S3_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_RING_LOCAL_PROXY -ErrorAction SilentlyContinue
    $env:NO_PROXY = "*"
    $env:no_proxy = "*"
}

function Read-EumetsatCredentials {
    $CredentialFile = $GeoRingEumetsatCredentialsFile
    if (-not (Test-Path -LiteralPath $CredentialFile)) {
        throw "EUMETSAT credential file not found: $CredentialFile"
    }
    $text = Get-Content -LiteralPath $CredentialFile -Raw
    $keyMatch = [regex]::Match($text, '(?im)^\s*Consumer\s+key\s*[:=]\s*(\S+)\s*$')
    $secretMatch = [regex]::Match($text, '(?im)^\s*Consumer\s+secret\s*[:=]\s*(\S+)\s*$')
    if (-not $keyMatch.Success -or -not $secretMatch.Success) {
        throw "Could not parse Consumer key/Consumer secret from credential file."
    }
    $env:EUMETSAT_CONSUMER_KEY = $keyMatch.Groups[1].Value.Trim()
    $env:EUMETSAT_CONSUMER_SECRET = $secretMatch.Groups[1].Value.Trim()
}

New-Item -ItemType Directory -Force -Path $BatchRoot, $TransferRoot | Out-Null
$env:PYTHONDONTWRITEBYTECODE = "1"
$BatchLockStream = $null

try {
    $BatchLockStream = [System.IO.File]::Open(
        $LockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch {
    throw "Another transfer process is already using this batch: $BatchRoot"
}

$lockMetadata = [ordered]@{
    pid = $PID
    opened_at = (Get-Date).ToUniversalTime().ToString("o")
    batch_root = $BatchRoot
    code_commit = $CodeCommit
} | ConvertTo-Json -Compress
$lockBytes = [System.Text.Encoding]::UTF8.GetBytes($lockMetadata)
$BatchLockStream.SetLength(0)
$BatchLockStream.Write($lockBytes, 0, $lockBytes.Length)
$BatchLockStream.Flush()

try {
    Write-BatchStatus -Phase "initialization" -Status "running"
    Clear-DownloadProxy
    if ($MeteosatPlatforms.Count -gt 0) {
        Read-EumetsatCredentials
    }

    Write-BatchStatus -Phase "inventory" -Status "running" -Message "Daily parallel inventory; matching cache will be reused."
    $InventoryArguments = @(
        $Downloader, "--root", $BatchRoot, "inventory",
        "--start-date", $StartDate, "--end-date", $EndDate,
        "--inventory-workers", $InventoryWorkers.ToString()
    )
    if ($MeteosatPlatforms.Count -eq 0) {
        $InventoryArguments += "--skip-meteosat"
    }
    if ($RefreshInventory) {
        $InventoryArguments += "--refresh-inventory"
    }
    foreach ($Platform in $SelectedPlatforms) {
        $InventoryArguments += @("--platform", $Platform)
    }
    Invoke-DownloadPython -Arguments $InventoryArguments

    if ($S3Platforms.Count -gt 0) {
        Write-BatchStatus -Phase "s3_download" -Status "running"
        $S3Arguments = @(
            $Downloader, "--root", $BatchRoot, "download-s3-range",
            "--start-date", $StartDate, "--end-date", $EndDate,
            "--max-workers", $DownloadWorkers.ToString(),
            "--range-mib", $S3RangeMiB.ToString()
        )
        if ($AdaptiveDownload) {
            $S3Arguments += @(
                "--adaptive-workers",
                "--min-workers", $DownloadMinWorkers.ToString(),
                "--initial-workers", $DownloadInitialWorkers.ToString()
            )
        }
        foreach ($Platform in $S3Platforms) {
            $S3Arguments += @("--platform", $Platform)
        }
        Invoke-DownloadPython -Arguments $S3Arguments
    }

    if ($MeteosatPlatforms.Count -gt 0) {
        Write-BatchStatus -Phase "meteosat_download" -Status "running"
        $MeteosatWorkers = [Math]::Min($DownloadWorkers, 8)
        $MeteosatArguments = @(
            $Downloader, "--root", $BatchRoot, "download-meteosat-range",
            "--start-date", $StartDate, "--end-date", $EndDate,
            "--max-workers", $MeteosatWorkers.ToString()
        )
        if ($AdaptiveDownload) {
            $MeteosatArguments += @(
                "--adaptive-workers",
                "--min-workers", ([Math]::Min($DownloadMinWorkers, $MeteosatWorkers)).ToString(),
                "--initial-workers", ([Math]::Min($DownloadInitialWorkers, $MeteosatWorkers)).ToString()
            )
        }
        foreach ($Platform in $MeteosatPlatforms) {
            $MeteosatArguments += @("--platform", $Platform)
        }
        Invoke-DownloadPython -Arguments $MeteosatArguments
    }

    Clear-DownloadProxy
    Write-BatchStatus -Phase "manifest" -Status "running" -Message "Computing SHA-256 checksums."
    Invoke-DownloadPython -Arguments @(
        $TransferTool, "prepare", "--batch-root", $BatchRoot,
        "--output-dir", $TransferRoot, "--server-root", $ServerRoot,
        "--start-date", $StartDate, "--end-date", $EndDate
    )
    Write-BatchStatus -Phase "ready_for_xftp" -Status "complete" -Message "Local batch is verified and ready for manual Xftp upload. No local files were deleted."
}
catch {
    Write-BatchStatus -Phase "failed" -Status "failed" -Message $_.Exception.Message
    throw
}
finally {
    Remove-Item Env:\EUMETSAT_CONSUMER_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\EUMETSAT_CONSUMER_SECRET -ErrorAction SilentlyContinue
    if ($BatchLockStream) {
        $BatchLockStream.Dispose()
        Remove-Item -LiteralPath $LockPath -ErrorAction SilentlyContinue
    }
}
