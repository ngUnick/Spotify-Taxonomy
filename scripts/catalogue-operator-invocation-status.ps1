[CmdletBinding()]
param(
    [string]$DataDir,
    [string]$InvocationId
)

$ErrorActionPreference = 'Stop'

if ($DataDir) {
    $runtimeRoot = [IO.Path]::GetFullPath($DataDir)
}
elseif ($env:SPOTIFY_TAXONOMY_DATA_DIR) {
    $runtimeRoot = [IO.Path]::GetFullPath($env:SPOTIFY_TAXONOMY_DATA_DIR)
}
elseif ($env:LOCALAPPDATA) {
    $runtimeRoot = [IO.Path]::Combine($env:LOCALAPPDATA, 'SpotifyPlaylistTaxonomy')
}
else {
    throw 'DataDir is required when LOCALAPPDATA is unavailable.'
}

$receiptDirectory = Join-Path $runtimeRoot 'operator-invocations'
if (-not (Test-Path -LiteralPath $receiptDirectory)) {
    throw 'No catalogue operator invocation receipts exist for this runtime.'
}

if ($InvocationId) {
    $receiptPath = Join-Path $receiptDirectory "$InvocationId.json"
    if (-not (Test-Path -LiteralPath $receiptPath)) {
        throw 'Unknown catalogue operator invocation ID.'
    }
}
else {
    $latest = Get-ChildItem -LiteralPath $receiptDirectory -Filter '*.json' -File |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($null -eq $latest) {
        throw 'No catalogue operator invocation receipts exist for this runtime.'
    }
    $receiptPath = $latest.FullName
}

try {
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'Catalogue operator invocation receipt is unreadable.'
}

$command = @($receipt.command) -join ' '
Write-Output "invocation_id: $($receipt.invocation_id)"
Write-Output "command: $command"
Write-Output "purpose: $($receipt.purpose)"
Write-Output "wrapper_state: $($receipt.wrapper_state)"
Write-Output "child_pid: $(if ($null -eq $receipt.child_pid) { '-' } else { $receipt.child_pid })"
Write-Output "started_at: $($receipt.started_at)"
Write-Output "finished_at: $(if ($null -eq $receipt.finished_at) { '-' } else { $receipt.finished_at })"
Write-Output "exit_code: $(if ($null -eq $receipt.exit_code) { '-' } else { $receipt.exit_code })"
Write-Output "stdout_path: $($receipt.stdout_path)"
Write-Output "stderr_path: $($receipt.stderr_path)"
