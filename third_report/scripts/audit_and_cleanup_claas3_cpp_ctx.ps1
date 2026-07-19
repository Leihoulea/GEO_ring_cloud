$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot "..\code\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1")

$cmsafRoot = $GeoRingClaas3Root
$workspaceRoot = $GeoRingThirdReportRoot
$reportDir = Join-Path $workspaceRoot 'reports\claas3_cleanup_audit'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

$fullMonthStart = [datetime]'2024-03-01T00:00:00Z'
$fullMonthEnd = [datetime]'2024-03-31T23:00:00Z'
$downloadedStart = [datetime]'2024-03-05T00:00:00Z'
$downloadedEnd = [datetime]'2024-03-31T23:00:00Z'
$quarterMinutes = @(0, 15, 30, 45)

function Get-ExpectedHours {
    param(
        [datetime]$StartUtc,
        [datetime]$EndUtc
    )
    $set = [System.Collections.Generic.HashSet[string]]::new()
    $cursor = $StartUtc
    while ($cursor -le $EndUtc) {
        [void]$set.Add($cursor.ToString('yyyy-MM-ddTHH:mm:ssZ'))
        $cursor = $cursor.AddHours(1)
    }
    return $set
}

function Get-ExpectedQuarterHoursForDir {
    param(
        [datetime]$StartDateUtc,
        [datetime]$EndDateUtc
    )
    $set = [System.Collections.Generic.HashSet[string]]::new()
    $cursor = $StartDateUtc.Date
    while ($cursor -le $EndDateUtc.Date) {
        for ($hour = 0; $hour -lt 24; $hour++) {
            foreach ($minute in $quarterMinutes) {
                $dt = [datetime]::SpecifyKind($cursor.AddHours($hour).AddMinutes($minute), [System.DateTimeKind]::Utc)
                [void]$set.Add($dt.ToString('yyyy-MM-ddTHH:mm:ssZ'))
            }
        }
        $cursor = $cursor.AddDays(1)
    }
    return $set
}

function Parse-ClaasTimestamp {
    param([string]$FileName)
    if ($FileName -notmatch '^(?<prod>CPP|CTX)in(?<stamp>\d{14})\d+SVMSGI1MD\.nc$') {
        return $null
    }
    return [datetime]::ParseExact($Matches['stamp'], 'yyyyMMddHHmmss', [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal)
}

function Get-DirDateRangeFromName {
    param([string]$DirName)
    if ($DirName -match '^(?:CLAAS3)_(?:CPP|CTX)_(?<d1>\d{4}-\d{2}-\d{2})(?:_to_(?<d2>\d{4}-\d{2}-\d{2}))?$') {
        $start = [datetime]::SpecifyKind([datetime]::ParseExact($Matches['d1'], 'yyyy-MM-dd', $null), [System.DateTimeKind]::Utc)
        $end = if ($Matches['d2']) {
            [datetime]::SpecifyKind([datetime]::ParseExact($Matches['d2'], 'yyyy-MM-dd', $null), [System.DateTimeKind]::Utc)
        } else {
            $start
        }
        return @($start, $end)
    }
    throw "Unsupported CLAAS3 folder name: $DirName"
}

$fullMonthExpected = Get-ExpectedHours -StartUtc $fullMonthStart -EndUtc $fullMonthEnd
$downloadedExpected = Get-ExpectedHours -StartUtc $downloadedStart -EndUtc $downloadedEnd
$dirs = Get-ChildItem -LiteralPath $cmsafRoot -Directory | Where-Object { $_.Name -like 'CLAAS3_CPP*' -or $_.Name -like 'CLAAS3_CTX*' } | Sort-Object Name

$fileRows = @()
$dirRows = @()
$productRows = @()
$deleteCandidates = @()

foreach ($dir in $dirs) {
    $product = if ($dir.Name -like 'CLAAS3_CPP*') { 'CPP' } else { 'CTX' }
    $dateRange = Get-DirDateRangeFromName -DirName $dir.Name
    $dirStart = $dateRange[0]
    $dirEnd = $dateRange[1]
    $expectedQuarterSet = Get-ExpectedQuarterHoursForDir -StartDateUtc $dirStart -EndDateUtc $dirEnd

    $files = Get-ChildItem -LiteralPath $dir.FullName -Recurse -File -Filter '*.nc' | Sort-Object Name
    $actualQuarterSet = [System.Collections.Generic.HashSet[string]]::new()
    $actualHourSet = [System.Collections.Generic.HashSet[string]]::new()
    $nonHourlyCount = 0
    $nonHourlyBytes = 0

    foreach ($file in $files) {
        $ts = Parse-ClaasTimestamp -FileName $file.Name
        if ($null -eq $ts) {
            continue
        }
        $stamp = $ts.ToString('yyyy-MM-ddTHH:mm:ssZ')
        [void]$actualQuarterSet.Add($stamp)
        if ($ts.Minute -eq 0 -and $ts.Second -eq 0) {
            [void]$actualHourSet.Add($stamp)
        } else {
            $nonHourlyCount += 1
            $nonHourlyBytes += $file.Length
            $deleteCandidates += $file
        }
        $fileRows += [pscustomobject]@{
            product = $product
            dir_name = $dir.Name
            file_name = $file.Name
            file_path = $file.FullName
            timestamp_utc = $stamp
            is_hourly = ($ts.Minute -eq 0 -and $ts.Second -eq 0)
            length_bytes = $file.Length
        }
    }

    $missingQuarter = $expectedQuarterSet.Count - $actualQuarterSet.Count
    $expectedHourlyInDir = [int]($expectedQuarterSet.Count / 4)
    $dirRows += [pscustomobject]@{
        product = $product
        dir_name = $dir.Name
        dir_start_utc = $dirStart.ToString('yyyy-MM-dd')
        dir_end_utc = $dirEnd.ToString('yyyy-MM-dd')
        total_nc_files = $files.Count
        hourly_files = $actualHourSet.Count
        non_hourly_files = $nonHourlyCount
        expected_hourly_files = $expectedHourlyInDir
        expected_quarterhour_files = $expectedQuarterSet.Count
        actual_quarterhour_files = $actualQuarterSet.Count
        missing_quarterhour_files = $missingQuarter
        quarterhour_complete = ($missingQuarter -eq 0)
        non_hourly_total_bytes = $nonHourlyBytes
    }
}

foreach ($product in @('CPP', 'CTX')) {
    $productFiles = $fileRows | Where-Object { $_.product -eq $product }
    $productHourSet = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($row in ($productFiles | Where-Object { $_.is_hourly })) {
        [void]$productHourSet.Add($row.timestamp_utc)
    }

    $missingFullMonth = 0
    foreach ($key in $fullMonthExpected) {
        if (-not $productHourSet.Contains($key)) {
            $missingFullMonth += 1
        }
    }

    $missingDownloaded = 0
    foreach ($key in $downloadedExpected) {
        if (-not $productHourSet.Contains($key)) {
            $missingDownloaded += 1
        }
    }

    $productDirRows = $dirRows | Where-Object { $_.product -eq $product }
    $productRows += [pscustomobject]@{
        product = $product
        full_month_expected_hourly = $fullMonthExpected.Count
        full_month_actual_hourly = $productHourSet.Count
        full_month_missing_hourly = $missingFullMonth
        full_month_complete = ($missingFullMonth -eq 0)
        downloaded_window_expected_hourly = $downloadedExpected.Count
        downloaded_window_actual_hourly = ($downloadedExpected | Where-Object { $productHourSet.Contains($_) }).Count
        downloaded_window_missing_hourly = $missingDownloaded
        downloaded_window_complete = ($missingDownloaded -eq 0)
        dirs_with_non_hourly = ($productDirRows | Where-Object { $_.non_hourly_files -gt 0 }).Count
        dirs_with_complete_quarterhour = ($productDirRows | Where-Object { $_.non_hourly_files -gt 0 -and $_.quarterhour_complete }).Count
    }
}

$timestamp = (Get-Date).ToString('yyyyMMdd_HHmmss')
$fileCsv = Join-Path $reportDir "claas3_cpp_ctx_file_inventory_$timestamp.csv"
$dirCsv = Join-Path $reportDir "claas3_cpp_ctx_dir_summary_$timestamp.csv"
$productCsv = Join-Path $reportDir "claas3_cpp_ctx_product_summary_$timestamp.csv"
$fileRows | Export-Csv -LiteralPath $fileCsv -NoTypeInformation -Encoding UTF8
$dirRows | Export-Csv -LiteralPath $dirCsv -NoTypeInformation -Encoding UTF8
$productRows | Export-Csv -LiteralPath $productCsv -NoTypeInformation -Encoding UTF8

$canCleanup = $true
foreach ($row in $productRows) {
    if (-not $row.downloaded_window_complete) {
        $canCleanup = $false
    }
}
foreach ($row in ($dirRows | Where-Object { $_.non_hourly_files -gt 0 })) {
    if (-not $row.quarterhour_complete) {
        $canCleanup = $false
    }
}

$deletedCount = 0
$deletedBytes = 0
if ($canCleanup) {
    foreach ($file in $deleteCandidates) {
        $deletedCount += 1
        $deletedBytes += $file.Length
        Remove-Item -LiteralPath $file.FullName -Force
    }
}

$result = [pscustomobject]@{
    file_inventory_csv = $fileCsv
    dir_summary_csv = $dirCsv
    product_summary_csv = $productCsv
    can_cleanup = $canCleanup
    deleted_file_count = $deletedCount
    deleted_total_bytes = $deletedBytes
}
$result | ConvertTo-Json -Depth 4
