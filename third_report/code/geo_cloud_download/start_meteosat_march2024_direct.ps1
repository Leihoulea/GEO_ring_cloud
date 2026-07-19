$ErrorActionPreference = "Stop"

$PathConfig = Join-Path $PSScriptRoot "..\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1"
. $PathConfig

$ProjectRoot = $GeoRingThirdReportRoot
$DownloadRoot = $GeoRingExternalGeoCloudRoot
$PythonScript = Join-Path $ProjectRoot "code\geo_cloud_download\geo_cloud_downloader.py"
$PythonExe = $GeoRingPythonExe
$CredentialFile = $GeoRingEumetsatCredentialsFile
$StatusJson = Join-Path $ProjectRoot "code\geo_cloud_download\meteosat_march2024_status.json"
$RunLog = Join-Path $ProjectRoot "code\geo_cloud_download\meteosat_march2024_direct_run.log"

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

try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RunLog) | Out-Null
    New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
    Read-EumetsatCredentials

    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:HTTP_PROXY = "http://127.0.0.1:7897"
    $env:HTTPS_PROXY = "http://127.0.0.1:7897"
    $env:http_proxy = "http://127.0.0.1:7897"
    $env:https_proxy = "http://127.0.0.1:7897"
    Remove-Item Env:\ALL_PROXY -ErrorAction SilentlyContinue
    Remove-Item Env:\all_proxy -ErrorAction SilentlyContinue

    "[$((Get-Date).ToUniversalTime().ToString('o'))] direct Meteosat inventory start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    Write-Status -Phase "inventory" -Status "running" -Message "Building March 2024 Meteosat CLM/CTH inventory."
    & $PythonExe $PythonScript --root $DownloadRoot meteosat-inventory --start-date 2024-03-01 --end-date 2024-03-31
    if ($LASTEXITCODE -ne 0) {
        throw "meteosat-inventory failed with exit code $LASTEXITCODE"
    }

    "[$((Get-Date).ToUniversalTime().ToString('o'))] direct Meteosat download start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    Write-Status -Phase "download" -Status "running" -Message "Downloading March 2024 Meteosat CLM/CTH products."
    & $PythonExe $PythonScript --root $DownloadRoot download-meteosat-range --start-date 2024-03-01 --end-date 2024-03-31
    if ($LASTEXITCODE -ne 0) {
        throw "download-meteosat-range failed with exit code $LASTEXITCODE"
    }

    "[$((Get-Date).ToUniversalTime().ToString('o'))] direct Meteosat complete" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    Write-Status -Phase "complete" -Status "complete" -Message "March 2024 Meteosat CLM/CTH download finished."
}
catch {
    Write-Status -Phase "failed" -Status "failed" -Message $_.Exception.Message
    "[$((Get-Date).ToUniversalTime().ToString('o'))] direct Meteosat failed: $($_.Exception.Message)" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    throw
}
finally {
    Remove-Item Env:\EUMETSAT_CONSUMER_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\EUMETSAT_CONSUMER_SECRET -ErrorAction SilentlyContinue
}
