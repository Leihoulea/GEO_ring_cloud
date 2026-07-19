param(
  [string]$SummaryRoot,
  [string]$SlideJson,
  [string]$OutDir
)

$ErrorActionPreference = "Stop"
$target = Join-Path $PSScriptRoot "tools\presentation\geo_ring_cloud_epic_group_meeting_cn.ps1"
& $target @PSBoundParameters
