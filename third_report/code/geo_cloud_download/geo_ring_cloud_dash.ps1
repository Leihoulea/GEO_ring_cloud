# Geo Ring Cloud local dashboard service root.
# component_role: data_transfer_dashboard_service
# related_stage_ids: stage_00

param(
    [Parameter(Mandatory = $true)] [string]$BatchRoot,
    [string]$PythonExe = "",
    [string]$IdentityFile = ""
)

$ErrorActionPreference = "Stop"
$PathConfig = Join-Path $PSScriptRoot "..\geo_ring_cloud_stage1\geo_ring_cloud_path_configuration.ps1"
. $PathConfig
$Dashboard = Join-Path $PSScriptRoot "geo_ring_cloud_transfer_dashboard.py"
$ResolvedPythonExe = if ($PythonExe) { [System.IO.Path]::GetFullPath($PythonExe) } else { $GeoRingPythonExe }
$ResolvedIdentityFile = if ($IdentityFile) {
    [System.IO.Path]::GetFullPath($IdentityFile)
} else {
    $AutomationIdentity = Join-Path $env:USERPROFILE ".ssh\id_ed25519_node05_automation"
    if (Test-Path -LiteralPath $AutomationIdentity -PathType Leaf) {
        $AutomationIdentity
    } else {
        Join-Path $env:USERPROFILE ".ssh\id_ed25519_node05"
    }
}
if (-not (Test-Path -LiteralPath $ResolvedPythonExe -PathType Leaf)) { throw "Python executable does not exist: $ResolvedPythonExe" }
if (-not (Test-Path -LiteralPath $Dashboard -PathType Leaf)) { throw "Dashboard script does not exist: $Dashboard" }
if (-not (Test-Path -LiteralPath $ResolvedIdentityFile -PathType Leaf)) { throw "SSH identity file does not exist: $ResolvedIdentityFile" }
$TransferDirectory = Join-Path $BatchRoot "transfer"
New-Item -ItemType Directory -Path $TransferDirectory -Force | Out-Null
$ServiceLog = Join-Path $TransferDirectory "dashboard_service.log"

# Task Scheduler has occasionally ended the local HTTP process with exit code
# zero while downloads/uploads were still active.  Keep this small supervisor
# alive and relaunch only the dashboard UI host; it never deletes or changes
# payload data.  Upload workers retain their ledger and therefore remain safe
# to resume explicitly after an unexpected host exit.
while ($true) {
    try {
        Add-Content -LiteralPath $ServiceLog -Value ("dashboard_supervisor_start " + [DateTime]::UtcNow.ToString("o"))
    } catch {}
    & $ResolvedPythonExe $Dashboard `
        "--batch-root" $BatchRoot `
        "--host" "127.0.0.1" `
        "--port" "8765" `
        "--ssh-target" "dhr@210.45.127.28" `
        "--identity-file" $ResolvedIdentityFile `
        "--auto-upload-root" "/data04/1/dhr/geo_ring_cloud_auto_upload" `
        "--allowed-server-parent" "/data04/1/dhr" `
        "--conda-environment" "pytorch" *>> $ServiceLog
    $dashboardExitCode = $LASTEXITCODE
    try {
        Add-Content -LiteralPath $ServiceLog -Value ("dashboard_supervisor_exit code=" + $dashboardExitCode + " at=" + [DateTime]::UtcNow.ToString("o"))
    } catch {}
    Start-Sleep -Seconds 5
}
