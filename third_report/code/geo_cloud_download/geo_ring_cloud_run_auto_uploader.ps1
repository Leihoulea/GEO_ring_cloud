# Launch the Python uploader beneath a breakaway PowerShell parent.
# component_role: automated_data_uploader_launcher
# related_stage_ids: stage_00

param(
    [Parameter(Mandatory = $true)] [string]$PythonExe,
    [Parameter(Mandatory = $true)] [string]$UploaderScript,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$UploaderArgs
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable does not exist: $PythonExe"
}
if (-not (Test-Path -LiteralPath $UploaderScript -PathType Leaf)) {
    throw "Uploader script does not exist: $UploaderScript"
}

& $PythonExe $UploaderScript @UploaderArgs
exit $LASTEXITCODE
