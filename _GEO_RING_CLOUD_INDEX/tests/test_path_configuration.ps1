$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$ConfigPath = Join-Path $ProjectRoot "third_report\code\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1"
$FixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) "geo_ring_path_contract"

$scriptRoots = @(
    (Join-Path $ProjectRoot "third_report\code\geo_cloud_download"),
    (Join-Path $ProjectRoot "third_report\code\geo_ring_cloud_stage1"),
    (Join-Path $ProjectRoot "third_report\code\priority_download_goes_meteosat"),
    (Join-Path $ProjectRoot "third_report\scripts")
)
foreach ($scriptRoot in $scriptRoots) {
    foreach ($script in Get-ChildItem -LiteralPath $scriptRoot -Filter "*.ps1" -File -Recurse) {
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $script.FullName,
            [ref]$tokens,
            [ref]$parseErrors
        )
        if ($parseErrors.Count -gt 0) {
            throw "PowerShell parse failure in $($script.FullName): $($parseErrors[0].Message)"
        }
    }
}

$expected = @{
    Project = Join-Path $FixtureRoot "project"
    External = Join-Path $FixtureRoot "external_geo"
    EpicL2 = Join-Path $FixtureRoot "epic_l2"
    Composite = Join-Path $FixtureRoot "epic_composite"
    Credentials = Join-Path $FixtureRoot "secrets\eumetsat.txt"
}

try {
    $env:GEO_RING_PROJECT_ROOT = $expected.Project
    $env:GEO_RING_EXTERNAL_GEO_CLOUD_ROOT = $expected.External
    $env:GEO_RING_EXTERNAL_EPIC_L2_ROOT = $expected.EpicL2
    $env:GEO_RING_EXTERNAL_EPIC_COMPOSITE_ROOT = $expected.Composite
    $env:GEO_RING_EUMETSAT_CREDENTIALS_FILE = $expected.Credentials

    . $ConfigPath

    $actual = @{
        Project = $GeoRingProjectRoot
        External = $GeoRingExternalGeoCloudRoot
        EpicL2 = $GeoRingExternalEpicL2Root
        Composite = $GeoRingExternalEpicCompositeRoot
        Credentials = $GeoRingEumetsatCredentialsFile
    }
    foreach ($name in $expected.Keys) {
        if ([System.IO.Path]::GetFullPath($actual[$name]) -ne [System.IO.Path]::GetFullPath($expected[$name])) {
            throw "$name override mismatch: expected=$($expected[$name]) actual=$($actual[$name])"
        }
    }
    if ($GeoRingThirdReportRoot -ne (Join-Path $expected.Project "third_report")) {
        throw "ThirdReport default did not derive from GEO_RING_PROJECT_ROOT"
    }

    Write-Output "PowerShell path configuration contract: OK"
} finally {
    Remove-Item Env:\GEO_RING_PROJECT_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_RING_EXTERNAL_GEO_CLOUD_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_RING_EXTERNAL_EPIC_L2_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_RING_EXTERNAL_EPIC_COMPOSITE_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:\GEO_RING_EUMETSAT_CREDENTIALS_FILE -ErrorAction SilentlyContinue
}
