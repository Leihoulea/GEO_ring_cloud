$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\AAAresearch_paper\third_report"
$DownloadRoot = "E:\GEO_Cloud_2024"
$PythonScript = Join-Path $ProjectRoot "code\geo_cloud_download\geo_cloud_downloader.py"
$RunLog = Join-Path $ProjectRoot "code\geo_cloud_download\goes_himawari_march2024_run.log"
$StatusJson = Join-Path $ProjectRoot "code\geo_cloud_download\goes_himawari_march2024_status.json"

$env:PYTHONDONTWRITEBYTECODE = "1"
Remove-Item Env:\HTTP_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:\HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:\http_proxy -ErrorAction SilentlyContinue
Remove-Item Env:\https_proxy -ErrorAction SilentlyContinue
Remove-Item Env:\ALL_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:\all_proxy -ErrorAction SilentlyContinue

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
        manifest = (Join-Path $DownloadRoot "manifests\manifest_inventory.csv")
        downloaded_manifest = (Join-Path $DownloadRoot "manifests\manifest_downloaded.csv")
        log = $RunLog
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatusJson -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RunLog) | Out-Null
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null

try {
    Write-Status -Phase "inventory" -Status "running" -Message "Building March 2024 GOES/Himawari inventory."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] inventory start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    conda run -n pytorch python $PythonScript --root $DownloadRoot inventory --skip-meteosat --start-date 2024-03-01 --end-date 2024-03-31 *>> $RunLog
    if ($LASTEXITCODE -ne 0) {
        throw "Inventory command failed with exit code $LASTEXITCODE"
    }

    Write-Status -Phase "download" -Status "running" -Message "Downloading March 2024 GOES/Himawari S3 products."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] download start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    conda run -n pytorch python $PythonScript --root $DownloadRoot download-s3-range --start-date 2024-03-01 --end-date 2024-03-31 --max-workers 8 *>> $RunLog
    if ($LASTEXITCODE -ne 0) {
        throw "Download command failed with exit code $LASTEXITCODE"
    }

    Write-Status -Phase "complete" -Status "complete" -Message "March 2024 GOES/Himawari download finished."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] complete" | Add-Content -LiteralPath $RunLog -Encoding UTF8
}
catch {
    Write-Status -Phase "failed" -Status "failed" -Message $_.Exception.Message
    "[$((Get-Date).ToUniversalTime().ToString('o'))] failed: $($_.Exception.Message)" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    throw
}
