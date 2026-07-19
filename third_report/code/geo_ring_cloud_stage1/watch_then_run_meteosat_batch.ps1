param(
  [string]$WatchPattern = 'run_epic_georing_sample_batch.py --role east_asia_priority --max-samples 3',
  [string]$BatchScript,
  [string]$CondaExe,
  [string]$CondaEnv = 'pytorch',
  [string]$OutDir,
  [int]$PollSeconds = 120
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot "geo_ring_cloud_path_configuration.ps1")

if (-not $PSBoundParameters.ContainsKey('BatchScript')) {
  $BatchScript = Join-Path $GeoRingCoreCodeRoot 'run_epic_georing_sample_batch.py'
}
if (-not $PSBoundParameters.ContainsKey('CondaExe')) {
  $CondaExe = $GeoRingCondaExe
}
if (-not $PSBoundParameters.ContainsKey('OutDir')) {
  $OutDir = Join-Path $GeoRingRunsRoot 'epic_202403_overnight_watch'
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$watchLog = Join-Path $OutDir 'watch_then_run_meteosat.log'
$meteosatLog = Join-Path $OutDir 'meteosat_batch_stdout_stderr.log'
$heartbeat = Join-Path $OutDir 'watcher_heartbeat.txt'
$eastStatus = Join-Path $GeoRingRunsRoot 'epic_202403_batch_runs\epic_georing_sample_batch_status.csv'

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
