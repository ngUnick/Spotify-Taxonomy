[CmdletBinding()]
param(
    [string]$DataDir,
    [string]$Purpose,
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Executable,
    [Parameter(ValueFromRemainingArguments = $true, Position = 1)]
    [string[]]$ChildArguments
)

$ErrorActionPreference = 'Stop'

function Resolve-RuntimeDataDirectory {
    param([string]$Requested)

    if ($Requested) {
        return [IO.Path]::GetFullPath($Requested)
    }
    if ($env:SPOTIFY_TAXONOMY_DATA_DIR) {
        return [IO.Path]::GetFullPath($env:SPOTIFY_TAXONOMY_DATA_DIR)
    }
    if (-not $env:LOCALAPPDATA) {
        throw 'DataDir is required when LOCALAPPDATA is unavailable.'
    }
    return [IO.Path]::Combine($env:LOCALAPPDATA, 'SpotifyPlaylistTaxonomy')
}

function Write-ReceiptAtomically {
    param([hashtable]$Receipt, [string]$Path)

    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Add-Backslashes {
    param([Text.StringBuilder]$Builder, [int]$Count)

    for ($index = 0; $index -lt $Count; $index++) {
        [void]$Builder.Append('\')
    }
}

function ConvertTo-WindowsCommandLineArgument {
    param([string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            Add-Backslashes $builder (($backslashes * 2) + 1)
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        Add-Backslashes $builder $backslashes
        $backslashes = 0
        [void]$builder.Append($character)
    }
    Add-Backslashes $builder ($backslashes * 2)
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Protect-ReceiptCommand {
    param([string[]]$Values)

    $protected = @()
    $redactNext = $false
    foreach ($value in $Values) {
        if ($redactNext) {
            $protected += '<redacted>'
            $redactNext = $false
            continue
        }
        if ($value -match '^(--?(?:token|secret|credential|password|authorization|client-id))=(.*)$') {
            $protected += "$($Matches[1])=<redacted>"
            continue
        }
        if ($value -match '^--?(?:token|secret|credential|password|authorization|client-id)$') {
            $protected += $value
            $redactNext = $true
            continue
        }
        $protected += $value
    }
    return ,$protected
}

if (-not $Executable) {
    throw 'Specify one child executable followed by its arguments.'
}

$runtimeRoot = Resolve-RuntimeDataDirectory $DataDir
$receiptDirectory = Join-Path $runtimeRoot 'operator-invocations'
New-Item -ItemType Directory -Force -Path $receiptDirectory | Out-Null

$invocationId = [guid]::NewGuid().ToString('N')
$receiptPath = Join-Path $receiptDirectory "$invocationId.json"
$stdoutPath = Join-Path $receiptDirectory "$invocationId.stdout.txt"
$stderrPath = Join-Path $receiptDirectory "$invocationId.stderr.txt"
$safeCommand = Protect-ReceiptCommand (@($Executable) + @($ChildArguments))
$startedAt = [DateTime]::UtcNow.ToString('o')
[IO.File]::WriteAllText($stdoutPath, '', [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($stderrPath, '', [Text.Encoding]::UTF8)
$receipt = @{
    schema = 'catalogue-operator-invocation-receipt.v1'
    invocation_id = $invocationId
    command = $safeCommand
    purpose = if ($Purpose) { $Purpose } else { $Executable }
    wrapper_state = 'starting'
    child_pid = $null
    started_at = $startedAt
    finished_at = $null
    exit_code = $null
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
}
Write-ReceiptAtomically $receipt $receiptPath

$argumentLine = @($ChildArguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument $_ }) -join ' '
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $Executable
$startInfo.Arguments = $argumentLine
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo

try {
    if (-not $process.Start()) {
        throw 'Child process did not start.'
    }
}
catch {
    $receipt.wrapper_state = 'failed_to_start'
    $receipt.finished_at = [DateTime]::UtcNow.ToString('o')
    Write-ReceiptAtomically $receipt $receiptPath
    Write-Error 'Child process could not be started; inspect the durable receipt and captured stderr path.'
    exit 1
}

$receipt.wrapper_state = 'running'
$receipt.child_pid = $process.Id
Write-ReceiptAtomically $receipt $receiptPath

try {
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $process.Refresh()
    $childExitCode = $process.ExitCode
    if ($null -eq $childExitCode) {
        throw "Child terminated but did not expose an exit code (type=$($process.GetType().FullName), exited=$($process.HasExited))."
    }
    [IO.File]::WriteAllText($stdoutPath, $stdoutTask.GetAwaiter().GetResult(), [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($stderrPath, $stderrTask.GetAwaiter().GetResult(), [Text.Encoding]::UTF8)
    $receipt.wrapper_state = 'finished'
    $receipt.exit_code = [int]$childExitCode
    $receipt.finished_at = [DateTime]::UtcNow.ToString('o')
    Write-ReceiptAtomically $receipt $receiptPath
}
catch {
    # The child outcome is unknown unless this wrapper actually observed termination.
    Write-ReceiptAtomically $receipt $receiptPath
    Write-Error 'Wrapper could not observe child termination; receipt remains running/unknown.'
    exit 1
}

Write-Output "invocation_id: $invocationId"
Write-Output "wrapper_state: finished"
Write-Output "exit_code: $($receipt.exit_code)"
Write-Output "stdout_path: $stdoutPath"
Write-Output "stderr_path: $stderrPath"
exit ([int]$receipt.exit_code)
