$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot "..\code\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1")

$cmsafRoot = $GeoRingClaas3Root
$targetDirs = @(
    'CMA, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-12 - 2024-03-12',
    'CMA, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-05 - 2024-03-05',
    'CPP, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-05 - 2024-03-05',
    'CPP, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-06 - 2024-03-11',
    'CPP, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-12 - 2024-03-12',
    'CTX, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-05 - 2024-03-05',
    'CTX, SEVIRI on MSG, Instantaneous, (none), Version 004, Satellite projection MSG-Seviri, METEOSAT disk (CM SAF definition), NetCDF4Default, 2024-03-12 - 2024-03-12'
)

$workspaceRoot = $GeoRingThirdReportRoot
$reportDir = Join-Path $workspaceRoot 'reports\claas3_cleanup'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

function Test-IsHourlyFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FileName
    )

    if ($FileName -notmatch '^[A-Z]{3}in(?<stamp>\d{14})\d+SVMSGI1MD\.nc$') {
        return $false
    }

    $stamp = $Matches['stamp']
    $minute = [int]$stamp.Substring(10, 2)
    $second = [int]$stamp.Substring(12, 2)
    return ($minute -eq 0 -and $second -eq 0)
}

function Resolve-VerifiedTarget {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativeName
    )

    $path = Join-Path $cmsafRoot $RelativeName
    $resolved = (Resolve-Path -LiteralPath $path).Path
    if (-not $resolved.StartsWith($cmsafRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to touch path outside CMSAF root: $resolved"
    }
    return $resolved
}

$rows = @()
$deleteFiles = @()

foreach ($relative in $targetDirs) {
    $dir = Resolve-VerifiedTarget -RelativeName $relative
    $files = Get-ChildItem -LiteralPath $dir -Recurse -File -Filter '*.nc' -ErrorAction Stop
    foreach ($file in $files) {
        $isHourly = Test-IsHourlyFile -FileName $file.Name
        $row = [pscustomobject]@{
            target_dir = $relative
            full_path = $file.FullName
            file_name = $file.Name
            length_bytes = $file.Length
            keep_reason = if ($isHourly) { 'KEEP_HOURLY' } else { 'DELETE_NON_HOURLY' }
        }
        $rows += $row
        if (-not $isHourly) {
            $deleteFiles += $file
        }
    }
}

$timestamp = (Get-Date).ToString('yyyyMMdd_HHmmss')
$manifestPath = Join-Path $reportDir "claas3_non_hourly_cleanup_manifest_$timestamp.csv"
$rows | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding UTF8

$summary = $rows |
    Group-Object { '{0}||{1}' -f $_.target_dir, $_.keep_reason } |
    ForEach-Object {
        $parts = $_.Name -split '\|\|', 2
        [pscustomobject]@{
            target_dir = $parts[0]
            status = $parts[1]
            file_count = $_.Count
            total_bytes = ($_.Group | Measure-Object -Property length_bytes -Sum).Sum
        }
    }
$summaryPath = Join-Path $reportDir "claas3_non_hourly_cleanup_summary_$timestamp.csv"
$summary | Export-Csv -LiteralPath $summaryPath -NoTypeInformation -Encoding UTF8

$bytesToDelete = ($deleteFiles | Measure-Object -Property Length -Sum).Sum
Write-Host "Manifest: $manifestPath"
Write-Host "Summary:  $summaryPath"
Write-Host ("Files to delete: {0}" -f $deleteFiles.Count)
Write-Host ("Bytes to delete: {0}" -f $bytesToDelete)

foreach ($file in $deleteFiles) {
    Remove-Item -LiteralPath $file.FullName -Force
}

$removedDirs = @()
foreach ($relative in $targetDirs) {
    $dir = Resolve-VerifiedTarget -RelativeName $relative
    $subdirs = Get-ChildItem -LiteralPath $dir -Recurse -Directory | Sort-Object FullName -Descending
    foreach ($subdir in $subdirs) {
        $hasChildren = Get-ChildItem -LiteralPath $subdir.FullName -Force | Select-Object -First 1
        if (-not $hasChildren) {
            Remove-Item -LiteralPath $subdir.FullName -Force
            $removedDirs += $subdir.FullName
        }
    }
}

$result = [pscustomobject]@{
    manifest_path = $manifestPath
    summary_path = $summaryPath
    deleted_file_count = $deleteFiles.Count
    deleted_total_bytes = $bytesToDelete
    removed_empty_dir_count = $removedDirs.Count
}
$result | ConvertTo-Json -Depth 4
