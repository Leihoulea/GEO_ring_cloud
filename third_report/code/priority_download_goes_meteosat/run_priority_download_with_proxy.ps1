$ErrorActionPreference = "Stop"
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:http_proxy = "http://127.0.0.1:7897"
$env:https_proxy = "http://127.0.0.1:7897"
$env:PRIORITY_DOWNLOAD_WORKERS = "6"
$DownloadScript = Join-Path $PSScriptRoot "download_goes_meteosat_missing_data.py"
$VerifyScript = Join-Path $PSScriptRoot "verify_goes_meteosat_downloads.py"
conda run -n pytorch python $DownloadScript
conda run -n pytorch python $VerifyScript
