param(
  [string]$SummaryRoot,
  [string]$OutDir
)

$ErrorActionPreference = "Stop"
$target = Join-Path $PSScriptRoot "tools\presentation\geo_ring_cloud_epic_group_meeting.ps1"
& $target @PSBoundParameters
