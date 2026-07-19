$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot "..\code\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1")

$cmsafRoot = $GeoRingClaas3Root
$ctxLongName = 'CTX, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-13 - 2024-03-31'
$workspaceRoot = $GeoRingThirdReportRoot
$reportDir = Join-Path $workspaceRoot 'reports\claas3_cleanup'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

function Test-IsHourlyFile {
    param([Parameter(Mandatory = $true)][string]$FileName)
    if ($FileName -notmatch '^[A-Z]{3}in(?<stamp>\d{14})\d+SVMSGI1MD\.nc$') {
        return $false
    }
    $stamp = $Matches['stamp']
    $minute = [int]$stamp.Substring(10, 2)
    $second = [int]$stamp.Substring(12, 2)
    return ($minute -eq 0 -and $second -eq 0)
}

function Resolve-VerifiedPath {
    param([Parameter(Mandatory = $true)][string]$PathText)
    $resolved = (Resolve-Path -LiteralPath $PathText).Path
    if (-not $resolved.StartsWith($cmsafRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside CMSAF root: $resolved"
    }
    return $resolved
}

function Get-ShortName {
    param([Parameter(Mandatory = $true)][string]$LongName)

    if ($LongName -notmatch '^(?<prod>CMA|CPP|CTX), .*?, (?<start>\d{4}-\d{2}-\d{2}) - (?<end>\d{4}-\d{2}-\d{2})$') {
        throw "Unsupported CMSAF folder name: $LongName"
    }
    $prod = $Matches['prod']
    $start = $Matches['start']
    $end = $Matches['end']
    if ($start -eq $end) {
        return "CLAAS3_${prod}_${start}"
    }
    return "CLAAS3_${prod}_${start}_to_${end}"
}

$timestamp = (Get-Date).ToString('yyyyMMdd_HHmmss')

# 1) Cleanup target CTX folder
$ctxPath = Resolve-VerifiedPath -PathText (Join-Path $cmsafRoot $ctxLongName)
$cleanupRows = @()
$deleteFiles = @()
$files = Get-ChildItem -LiteralPath $ctxPath -Recurse -File -Filter '*.nc' -ErrorAction Stop
foreach ($file in $files) {
    $isHourly = Test-IsHourlyFile -FileName $file.Name
    $cleanupRows += [pscustomobject]@{
        target_dir = $ctxLongName
        full_path = $file.FullName
        file_name = $file.Name
        length_bytes = $file.Length
        status = if ($isHourly) { 'KEEP_HOURLY' } else { 'DELETE_NON_HOURLY' }
    }
    if (-not $isHourly) {
        $deleteFiles += $file
    }
}

$cleanupManifest = Join-Path $reportDir "claas3_ctx_late_march_cleanup_manifest_$timestamp.csv"
$cleanupRows | Export-Csv -LiteralPath $cleanupManifest -NoTypeInformation -Encoding UTF8

$cleanupSummary = $cleanupRows |
    Group-Object status |
    ForEach-Object {
        [pscustomobject]@{
            status = $_.Name
            file_count = $_.Count
            total_bytes = (($_.Group | Measure-Object -Property length_bytes -Sum).Sum)
        }
    }
$cleanupSummaryPath = Join-Path $reportDir "claas3_ctx_late_march_cleanup_summary_$timestamp.csv"
$cleanupSummary | Export-Csv -LiteralPath $cleanupSummaryPath -NoTypeInformation -Encoding UTF8

$deletedBytes = ($deleteFiles | Measure-Object -Property Length -Sum).Sum
foreach ($file in $deleteFiles) {
    Remove-Item -LiteralPath $file.FullName -Force
}

# 2) Rename CLAAS3 directories
$renameRows = @()
$dirs = Get-ChildItem -LiteralPath $cmsafRoot -Directory |
    Where-Object { $_.Name -match '^(CMA|CPP|CTX), .*2024-\d{2}-\d{2} - 2024-\d{2}-\d{2}$' } |
    Sort-Object Name

foreach ($dir in $dirs) {
    $oldPath = Resolve-VerifiedPath -PathText $dir.FullName
    $newName = Get-ShortName -LongName $dir.Name
    $newPath = Join-Path $cmsafRoot $newName
    if (Test-Path -LiteralPath $newPath) {
        throw "Target short-name path already exists: $newPath"
    }
    Move-Item -LiteralPath $oldPath -Destination $newPath
    $renameRows += [pscustomobject]@{
        old_name = $dir.Name
        new_name = $newName
        old_path = $oldPath
        new_path = $newPath
    }
}

$renameMapPath = Join-Path $reportDir "claas3_directory_rename_map_$timestamp.csv"
$renameRows | Export-Csv -LiteralPath $renameMapPath -NoTypeInformation -Encoding UTF8

$result = [pscustomobject]@{
    cleanup_manifest_path = $cleanupManifest
    cleanup_summary_path = $cleanupSummaryPath
    rename_map_path = $renameMapPath
    deleted_file_count = $deleteFiles.Count
    deleted_total_bytes = $deletedBytes
    renamed_directory_count = $renameRows.Count
}
$result | ConvertTo-Json -Depth 4
