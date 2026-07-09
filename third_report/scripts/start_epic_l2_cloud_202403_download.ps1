param(
    [string]$SourceList = 'D:\AAAresearch_paper\third_report\EPIC_L2_cloud_download_script_202403.txt',
    [string]$TargetDir = 'F:\DSCOVR_EPIC_L2_CLOUD_03_2024.03',
    [string]$Username = 'kingofkunlun'
)

$ErrorActionPreference = 'Stop'

if (-not $env:EARTHDATA_PASSWORD) {
    throw 'EARTHDATA_PASSWORD environment variable is required for this run.'
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $scriptDir
$reportDir = Join-Path $workspaceRoot 'reports\epic_l2_cloud_download_202403'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$urlListPath = Join-Path $reportDir "epic_l2_cloud_urls_$timestamp.txt"
$statusPath = Join-Path $reportDir 'download_status.json'
$logPath = Join-Path $reportDir 'download_run.log'
$manifestPath = Join-Path $reportDir "download_manifest_$timestamp.csv"

$baseUrl = 'https://data.asdc.earthdata.nasa.gov/asdc-prod-protected/DSCOVR/DSCOVR_EPIC_L2_CLOUD_03/2024.03'

$rawNames = Select-String -LiteralPath $SourceList -Pattern 'DSCOVR_EPIC_L2_CLOUD_03_[0-9]{14}_03\.nc4' -AllMatches |
    ForEach-Object { $_.Matches.Value }
$names = $rawNames | Sort-Object -Unique
if (-not $names -or $names.Count -eq 0) {
    throw "No EPIC L2 cloud filenames found in $SourceList"
}

$urls = foreach ($name in $names) {
    "$baseUrl/$name"
}
$urls | Set-Content -LiteralPath $urlListPath -Encoding UTF8

$manifest = foreach ($name in $names) {
    [pscustomobject]@{
        file_name = $name
        url = "$baseUrl/$name"
        target_path = Join-Path $TargetDir $name
    }
}
$manifest | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding UTF8

$workerPath = Join-Path $reportDir "download_worker_$timestamp.ps1"
$worker = @"
`$ErrorActionPreference = 'Continue'
`$ProgressPreference = 'SilentlyContinue'

`$env:HTTP_PROXY = ''
`$env:HTTPS_PROXY = ''
`$env:ALL_PROXY = ''
`$env:http_proxy = ''
`$env:https_proxy = ''
`$env:all_proxy = ''
`$env:NO_PROXY = '*'
`$env:no_proxy = '*'

`$statusPath = '$statusPath'
`$logPath = '$logPath'
`$targetDir = '$TargetDir'
`$urlListPath = '$urlListPath'
`$username = '$Username'
`$password = '$($env:EARTHDATA_PASSWORD)'

function Write-Status([string]`$phase, [string]`$state, [string]`$message, [hashtable]`$extra = @{}) {
    `$payload = [ordered]@{
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
        phase = `$phase
        state = `$state
        message = `$message
        target_dir = `$targetDir
        source_list = '$SourceList'
    }
    foreach (`$k in `$extra.Keys) { `$payload[`$k] = `$extra[`$k] }
    `$payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath `$statusPath -Encoding UTF8
}

function Log([string]`$message) {
    "[`$((Get-Date).ToUniversalTime().ToString('o'))] `$message" | Add-Content -LiteralPath `$logPath -Encoding UTF8
}

Write-Status 'init' 'running' 'Preparing Earthdata direct download worker.'
Log 'worker_start'

`$cookieJar = Join-Path `$env:TEMP ('epic_cookie_' + [guid]::NewGuid().ToString('N') + '.txt')
`$netrc = Join-Path `$env:TEMP ('epic_netrc_' + [guid]::NewGuid().ToString('N') + '.txt')
try {
    "machine urs.earthdata.nasa.gov login `$username password `$password" | Set-Content -LiteralPath `$netrc -Encoding ASCII
    `$(Get-Item -LiteralPath `$netrc).Attributes = 'Hidden'

    Write-Status 'download' 'running' 'Starting curl direct download loop.' @{ total_urls = @((Get-Content -LiteralPath `$urlListPath)).Count; completed = 0; failed = 0 }
    Log 'curl_loop_start'
    `$urls = Get-Content -LiteralPath `$urlListPath
    `$total = `$urls.Count
    `$completed = 0
    `$failed = 0
    foreach (`$url in `$urls) {
        `$name = Split-Path -Leaf `$url
        `$targetPath = Join-Path `$targetDir `$name
        if ((Test-Path -LiteralPath `$targetPath) -and ((Get-Item -LiteralPath `$targetPath).Length -gt 0)) {
            `$completed += 1
            Write-Status 'download' 'running' "Skipping existing `$name" @{ total_urls = `$total; completed = `$completed; failed = `$failed; current_file = `$name; current_url = `$url; last_result = 'skipped_existing' }
            Log "skip_existing `$name"
            continue
        }

        Log "curl_get `$name"
        & curl.exe --fail --location --continue-at - --cookie-jar `$cookieJar --cookie `$cookieJar --netrc-file `$netrc --output `$targetPath `$url 2>&1 | Tee-Object -FilePath `$logPath -Append
        `$code = `$LASTEXITCODE
        if (`$code -ne 0) {
            `$failed += 1
            Write-Status 'download' 'running' "Download failed for `$name" @{ total_urls = `$total; completed = `$completed; failed = `$failed; current_file = `$name; current_url = `$url; last_result = 'failed'; exit_code = `$code }
            Log "curl_failed `$name code=`$code"
            continue
        }

        `$completed += 1
        Write-Status 'download' 'running' "Downloaded `$name" @{ total_urls = `$total; completed = `$completed; failed = `$failed; current_file = `$name; current_url = `$url; last_result = 'downloaded' }
    }

    if (`$failed -gt 0) {
        Log "curl_loop_completed_with_failures failed=`$failed completed=`$completed total=`$total"
    } else {
        Log "curl_loop_completed completed=`$completed total=`$total"
    }

    `$files = Get-ChildItem -LiteralPath `$targetDir -File -Filter '*.nc4' -ErrorAction SilentlyContinue
    Write-Status 'complete' 'complete' 'EPIC L2 cloud March 2024 direct download finished.' @{
        file_count = `$files.Count
        total_bytes = ((`$files | Measure-Object -Property Length -Sum).Sum)
        total_urls = `$total
        completed = `$completed
        failed = `$failed
    }
    Log 'worker_complete'
}
finally {
    Remove-Item -LiteralPath `$cookieJar, `$netrc -Force -ErrorAction SilentlyContinue
}
"@
Set-Content -LiteralPath $workerPath -Value $worker -Encoding UTF8

$proc = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $workerPath) `
    -WorkingDirectory $reportDir `
    -WindowStyle Hidden `
    -PassThru

$launch = [pscustomobject]@{
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    source_list = $SourceList
    target_dir = $TargetDir
    report_dir = $reportDir
    url_count = $urls.Count
    worker_script = $workerPath
    status_json = $statusPath
    log_path = $logPath
    manifest_csv = $manifestPath
    pid = $proc.Id
}
$launch | ConvertTo-Json -Depth 4
