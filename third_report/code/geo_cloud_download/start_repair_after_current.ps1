$ErrorActionPreference = "Stop"

$PathConfig = Join-Path $PSScriptRoot "..\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1"
. $PathConfig

$ProjectRoot = $GeoRingThirdReportRoot
$CurrentPidFile = Join-Path $ProjectRoot "code\geo_cloud_download\current_download_pid.txt"
$RepairScript = Join-Path $ProjectRoot "code\geo_cloud_download\repair_goes_himawari_march2024_low_parallel.ps1"
$WatcherLog = Join-Path $ProjectRoot "code\geo_cloud_download\goes_himawari_march2024_repair_watcher.log"

$CurrentPid = 38408
if (Test-Path -LiteralPath $CurrentPidFile) {
    $text = Get-Content -LiteralPath $CurrentPidFile -Raw
    if ($text.Trim() -match '^\d+$') {
        $CurrentPid = [int]$text.Trim()
    }
}

"[$((Get-Date).ToUniversalTime().ToString('o'))] waiting for current process $CurrentPid" | Add-Content -LiteralPath $WatcherLog -Encoding UTF8
while (Get-Process -Id $CurrentPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 60
}
"[$((Get-Date).ToUniversalTime().ToString('o'))] current process ended; starting low-parallel repair" | Add-Content -LiteralPath $WatcherLog -Encoding UTF8
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $RepairScript *>> $WatcherLog
"[$((Get-Date).ToUniversalTime().ToString('o'))] watcher complete" | Add-Content -LiteralPath $WatcherLog -Encoding UTF8
