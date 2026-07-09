$ErrorActionPreference = 'Stop'

$cmsafRoot = 'E:\GEO_Cloud_2024\CMSAF'
$targetDir = Join-Path $cmsafRoot 'CLAAS3_CTX_2024-03-06_to_2024-03-11'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $workspaceRoot 'reports\claas3_cleanup_audit'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

function Parse-ClaasTimestamp {
    param([string]$FileName)
    if ($FileName -notmatch '^CTXin(?<stamp>\d{14})\d+SVMSGI1MD\.nc$') {
        return $null
    }
    return [datetime]::ParseExact($Matches['stamp'], 'yyyyMMddHHmmss', [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal)
}

$resolvedTarget = (Resolve-Path -LiteralPath $targetDir).Path
if (-not $resolvedTarget.StartsWith($cmsafRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to operate outside CMSAF root: $resolvedTarget"
}

$timestamp = (Get-Date).ToString('yyyyMMdd_HHmmss')
$allRows = @()
$deleteFiles = @()

$files = Get-ChildItem -LiteralPath $resolvedTarget -Recurse -File -Filter '*.nc' | Sort-Object FullName
$hourlyRows = foreach ($file in $files) {
    $dt = Parse-ClaasTimestamp -FileName $file.Name
    if ($null -eq $dt) { continue }
    if ($dt.Minute -eq 0 -and $dt.Second -eq 0) {
        [pscustomobject]@{
            timestamp_utc = $dt.ToString('yyyy-MM-ddTHH:mm:ssZ')
            full_path = $file.FullName
            file_name = $file.Name
            length_bytes = $file.Length
        }
    }
}

foreach ($group in ($hourlyRows | Group-Object timestamp_utc)) {
    $ordered = $group.Group | Sort-Object full_path
    $keep = $ordered[0]
    foreach ($row in $ordered) {
        $action = if ($row.full_path -eq $keep.full_path) { 'KEEP' } else { 'DELETE_DUPLICATE' }
        $allRows += [pscustomobject]@{
            timestamp_utc = $row.timestamp_utc
            action = $action
            kept_path = $keep.full_path
            candidate_path = $row.full_path
            file_name = $row.file_name
            length_bytes = $row.length_bytes
        }
        if ($action -eq 'DELETE_DUPLICATE') {
            $deleteFiles += Get-Item -LiteralPath $row.full_path
        }
    }
}

$mapPath = Join-Path $reportDir "claas3_ctx_hourly_dedup_map_$timestamp.csv"
$allRows | Export-Csv -LiteralPath $mapPath -NoTypeInformation -Encoding UTF8

$deletedBytes = ($deleteFiles | Measure-Object -Property Length -Sum).Sum
foreach ($file in $deleteFiles) {
    Remove-Item -LiteralPath $file.FullName -Force
}

$summary = [pscustomobject]@{
    target_dir = $resolvedTarget
    dedup_map_path = $mapPath
    duplicate_files_deleted = $deleteFiles.Count
    duplicate_total_bytes_deleted = $deletedBytes
}
$summary | ConvertTo-Json -Depth 4
