$COMPONENT_ROLE = "presentation_lineage"

function Write-GeoRingPresentationManifest {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$GeneratingScript,
    [Parameter(Mandatory = $true)][string[]]$InputPaths,
    [Parameter(Mandatory = $true)][string[]]$OutputPaths,
    [Parameter(Mandatory = $true)][hashtable]$Parameters,
    [Parameter(Mandatory = $true)][string]$ProjectRoot
  )

  $commit = ""
  try {
    $commit = (& git -C $ProjectRoot rev-parse HEAD 2>$null | Select-Object -First 1).Trim()
  } catch {
    $commit = ""
  }

  $payload = [ordered]@{
    project_id = "geo_ring_cloud"
    canonical_stage_id = ""
    component_role = "presentation_builder"
    related_stage_ids = @(
      "stage_08", "stage_08b", "stage_08c", "stage_08d", "stage_08e",
      "stage_08f", "stage_08g", "stage_08h", "stage_08i", "stage_08j", "stage_08k"
    )
    generating_script = $GeneratingScript
    input_paths = @($InputPaths)
    output_paths = @($OutputPaths)
    parameter_summary = $Parameters
    timestamp_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    code_commit = $commit
  }

  $parent = Split-Path -Parent $Path
  if ($parent) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
  }
  $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
  return $Path
}
