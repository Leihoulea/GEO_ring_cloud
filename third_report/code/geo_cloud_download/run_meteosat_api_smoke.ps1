$ErrorActionPreference = "Stop"

$ProjectRoot = "D:\AAAresearch_paper\third_report"
$DownloadRoot = "E:\GEO_Cloud_2024"
$PythonScript = Join-Path $ProjectRoot "code\geo_cloud_download\geo_cloud_downloader.py"
$PythonExe = "D:\anaconda\envs\pytorch\python.exe"
$CredentialFile = Join-Path $ProjectRoot "eumetsat_dataservices_API.txt"

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
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:http_proxy = "http://127.0.0.1:7897"
$env:https_proxy = "http://127.0.0.1:7897"
Remove-Item Env:\ALL_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:\all_proxy -ErrorAction SilentlyContinue

try {
    & $PythonExe $PythonScript --root $DownloadRoot meteosat-options
    if ($LASTEXITCODE -ne 0) {
        throw "meteosat-options failed with exit code $LASTEXITCODE"
    }

    & $PythonExe $PythonScript --root $DownloadRoot meteosat-smoke --date 2024-03-12 --hour 0 --minute 0
    if ($LASTEXITCODE -ne 0) {
        throw "meteosat-smoke failed with exit code $LASTEXITCODE"
    }
}
finally {
    Remove-Item Env:\EUMETSAT_CONSUMER_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:\EUMETSAT_CONSUMER_SECRET -ErrorAction SilentlyContinue
}
