$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\AAAresearch_paper\third_report"
$DownloadRoot = "E:\GEO_Cloud_2024"
$PythonScript = Join-Path $ProjectRoot "code\geo_cloud_download\geo_cloud_downloader.py"
$PythonExe = "D:\anaconda\envs\pytorch\python.exe"
$CredentialFile = Join-Path $ProjectRoot "eumetsat_dataservices_API.txt"
$RunLog = Join-Path $ProjectRoot "code\geo_cloud_download\meteosat_march2024_run.log"
$StatusJson = Join-Path $ProjectRoot "code\geo_cloud_download\meteosat_march2024_status.json"

function Write-Status {
    param(
        [string]$Phase,
        [string]$Status,
        [string]$Message = ""
    )
    $payload = [ordered]@{
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
        phase = $Phase
        status = $Status
        message = $Message
        download_root = $DownloadRoot
        log = $RunLog
        inventory = (Join-Path $DownloadRoot "manifests\manifest_meteosat_inventory.csv")
        downloaded_manifest = (Join-Path $DownloadRoot "manifests\manifest_meteosat_downloaded.csv")
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatusJson -Encoding UTF8
}

function Read-EumetsatCredentials {
    if (-not (Test-Path -LiteralPath $CredentialFile)) {
        throw "Credential file not found: $CredentialFile"
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

function Invoke-CondaPythonWithRetry {
    param(
        [string[]]$PythonArgs,
        [string]$Label,
        [int]$Attempts = 5
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        "[$((Get-Date).ToUniversalTime().ToString('o'))] $Label attempt $attempt/$Attempts" | Add-Content -LiteralPath $RunLog -Encoding UTF8
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $PythonExe @PythonArgs *>> $RunLog
        $exitCode = $LASTEXITCODE
        $ErrorActionPreference = $oldPreference
        if ($exitCode -eq 0) {
            return
        }
        $waitSeconds = @(10, 30, 60, 120, 180)[$attempt - 1]
        "[$((Get-Date).ToUniversalTime().ToString('o'))] $Label failed with exit code $exitCode; waiting $waitSeconds seconds" | Add-Content -LiteralPath $RunLog -Encoding UTF8
        Start-Sleep -Seconds $waitSeconds
    }
    throw "$Label failed after $Attempts attempts"
}

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:http_proxy = "http://127.0.0.1:7897"
$env:https_proxy = "http://127.0.0.1:7897"
Remove-Item Env:\ALL_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:\all_proxy -ErrorAction SilentlyContinue

try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RunLog) | Out-Null
    New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
    Read-EumetsatCredentials

    Write-Status -Phase "smoke" -Status "complete" -Message "Smoke already verified; starting March 2024 Meteosat inventory."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] meteosat smoke skipped after verified one-day inventory" | Add-Content -LiteralPath $RunLog -Encoding UTF8

    Write-Status -Phase "inventory" -Status "running" -Message "Building March 2024 Meteosat CLM/CTH inventory."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] meteosat inventory start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    Invoke-CondaPythonWithRetry -Label "meteosat-inventory" -PythonArgs @($PythonScript, "--root", $DownloadRoot, "meteosat-inventory", "--start-date", "2024-03-01", "--end-date", "2024-03-31")

    Write-Status -Phase "download" -Status "running" -Message "Downloading March 2024 Meteosat CLM/CTH products."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] meteosat download start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    Invoke-CondaPythonWithRetry -Label "download-meteosat-range" -PythonArgs @($PythonScript, "--root", $DownloadRoot, "download-meteosat-range", "--start-date", "2024-03-01", "--end-date", "2024-03-31")

    Write-Status -Phase "complete" -Status "complete" -Message "March 2024 Meteosat CLM/CTH download finished."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] meteosat complete" | Add-Content -LiteralPath $RunLog -Encoding UTF8
}
catch {
    Write-Status -Phase "failed" -Status "failed" -Message $_.Exception.Message
    "[$((Get-Date).ToUniversalTime().ToString('o'))] meteosat failed: $($_.Exception.Message)" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    throw
}
finally {
    Remove-Item Env:\EUMETSAT_CONSUMER_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\EUMETSAT_CONSUMER_SECRET -ErrorAction SilentlyContinue
}
