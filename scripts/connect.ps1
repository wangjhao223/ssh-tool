param(
    [string]$Server,
    [string]$Distribution = 'Ubuntu-26.04',
    [string]$WslUser = 'root',
    [switch]$InWindow,
    [switch]$Close
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wslRoot = (& wsl.exe -d $Distribution -u $WslUser --exec wslpath -u $projectRoot)
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the tool path in WSL.' }
$wslRoot = ($wslRoot -join "`n").Trim()
if ([string]::IsNullOrWhiteSpace($Server)) {
    $Server = (& wsl.exe -d $Distribution -u $WslUser --exec python3 -B "$wslRoot/mcp/local_connection.py" resolve-server)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the default server in WSL.' }
    $Server = ($Server -join "`n").Trim()
}
if ($Server.StartsWith('srv-')) { $Server = $Server.Substring(4) }
if ($Server -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$') { throw 'Invalid server name in configuration.' }

function Quote-Argument([string]$Value) {
    if ($Value.Contains('"')) { throw 'An argument contains an unsupported quote.' }
    return '"' + $Value + '"'
}

if (-not $InWindow) {
    # Deliberately visible: the user types the SSH password in this window.
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Quote-Argument $PSCommandPath), '-Server', (Quote-Argument $Server),
        '-Distribution', (Quote-Argument $Distribution), '-WslUser',
        (Quote-Argument $WslUser), '-InWindow')
    if ($Close) { $arguments += '-Close' }
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList $arguments -WindowStyle Normal | Out-Null
    exit 0
}

$exitCode = 1
do {
  try {
    $Host.UI.RawUI.WindowTitle = "remote-ws | $Server"
    Write-Host "Tool: $projectRoot"
    Write-Host "Server: $Server | WSL: $Distribution | User: $WslUser"
    Write-Host 'Enter your SSH password in this window. Kimi Code cannot read it.'
    $wslArgs = @('-d', $Distribution, '-u', $WslUser, '--exec',
        'bash', "$wslRoot/scripts/connect.sh", $Server)
    if ($Close) { $wslArgs += '--close' }
    & wsl.exe @wslArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { Write-Host "Connection failed (exit=$exitCode)." -ForegroundColor Red }
  } catch {
    $exitCode = 1
    Write-Host $_.Exception.Message -ForegroundColor Red
  }
  if ($exitCode -eq 0) { break }
  # Read only an explicit local retry choice, never SSH input or its output.
  $retryChoice = Read-Host 'Press R to retry in this window, or Enter to close'
} while ($retryChoice -eq 'r')
exit $exitCode
