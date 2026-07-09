$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\AAAresearch_paper\third_report"
$DownloadRoot = "E:\GEO_Cloud_2024"
$PythonScript = Join-Path $ProjectRoot "code\geo_cloud_download\geo_cloud_downloader.py"
$RunLog = Join-Path $ProjectRoot "code\geo_cloud_download\goes_himawari_march2024_repair.log"
$StatusJson = Join-Path $ProjectRoot "code\geo_cloud_download\goes_himawari_march2024_repair_status.json"

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:GEO_CLOUD_GOES_PROXY = "http://127.0.0.1:7897"
$env:GEO_CLOUD_FAST_SKIP_EXISTING = "1"
$env:GEO_CLOUD_PRIORITIZE_GOES = "1"
Remove-Item Env:\GEO_CLOUD_HIMAWARI_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:\GEO_CLOUD_S3_PROXY -ErrorAction SilentlyContinue
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
        log = $RunLog
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatusJson -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RunLog) | Out-Null
    Write-Status -Phase "repair" -Status "running" -Message "Retrying March 2024 GOES/Himawari with max-workers=4; GOES uses VPN proxy 127.0.0.1:7897 and existing valid files are skipped."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] repair download start" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    conda run -n pytorch python $PythonScript --root $DownloadRoot download-s3-range --start-date 2024-03-01 --end-date 2024-03-31 --max-workers 4 *>> $RunLog
    if ($LASTEXITCODE -ne 0) {
        throw "Repair command failed with exit code $LASTEXITCODE"
    }
    Write-Status -Phase "complete" -Status "complete" -Message "Low-parallel repair pass finished."
    "[$((Get-Date).ToUniversalTime().ToString('o'))] repair complete" | Add-Content -LiteralPath $RunLog -Encoding UTF8
}
catch {
    Write-Status -Phase "failed" -Status "failed" -Message $_.Exception.Message
    "[$((Get-Date).ToUniversalTime().ToString('o'))] repair failed: $($_.Exception.Message)" | Add-Content -LiteralPath $RunLog -Encoding UTF8
    throw
}
