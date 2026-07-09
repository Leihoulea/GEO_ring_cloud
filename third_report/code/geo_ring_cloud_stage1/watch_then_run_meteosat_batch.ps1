param(
  [string]$WatchPattern = 'run_epic_georing_sample_batch.py --role east_asia_priority --max-samples 3',
  [string]$BatchScript = 'D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\run_epic_georing_sample_batch.py',
  [string]$CondaExe = 'D:\anaconda\Scripts\conda.exe',
  [string]$CondaEnv = 'pytorch',
  [string]$OutDir = 'D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\epic_202403_overnight_watch',
  [int]$PollSeconds = 120
)

$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$watchLog = Join-Path $OutDir 'watch_then_run_meteosat.log'
$meteosatLog = Join-Path $OutDir 'meteosat_batch_stdout_stderr.log'
$heartbeat = Join-Path $OutDir 'watcher_heartbeat.txt'
$eastStatus = 'D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\epic_202403_batch_runs\epic_georing_sample_batch_status.csv'

function Write-WatchLog {
  param([string]$Message)
  $line = "$(Get-Date -Format o) $Message"
  Add-Content -LiteralPath $watchLog -Value $line -Encoding UTF8
  Set-Content -LiteralPath $heartbeat -Value $line -Encoding UTF8
}

Write-WatchLog "watcher started; pattern=$WatchPattern"

while ($true) {
  $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
      $_.CommandLine -and $_.CommandLine.Contains($WatchPattern)
    })
  if ($procs.Count -eq 0) {
    Write-WatchLog "watched east_asia batch no longer running"
    break
  }
  $statusTail = ''
  if (Test-Path -LiteralPath $eastStatus) {
    try {
      $statusTail = (Get-Content -LiteralPath $eastStatus -Tail 5 -ErrorAction SilentlyContinue) -join ' || '
    } catch {
      $statusTail = "status tail unavailable: $($_.Exception.Message)"
    }
  }
  Write-WatchLog "east_asia still running; process_count=$($procs.Count); status_tail=$statusTail"
  Start-Sleep -Seconds $PollSeconds
}

$existingMeteosat = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains('run_epic_georing_sample_batch.py') -and $_.CommandLine.Contains('METEOSAT_DOMINANT_CONTROL')
  })
if ($existingMeteosat.Count -gt 0) {
  Write-WatchLog "Meteosat batch already running; process_count=$($existingMeteosat.Count); watcher exits"
  exit 0
}

Write-WatchLog "starting Meteosat dominant batch"
Add-Content -LiteralPath $meteosatLog -Value "$(Get-Date -Format o) starting Meteosat batch" -Encoding UTF8
& $CondaExe run -n $CondaEnv python $BatchScript --candidate-class METEOSAT_DOMINANT_CONTROL --max-samples 2 --skip-existing *>> $meteosatLog
$rc = $LASTEXITCODE
Write-WatchLog "Meteosat batch finished; exit_code=$rc"
exit $rc
