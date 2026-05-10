<#
.SYNOPSIS
  Upload local per-symbol CSVs to the VPS Postgres over an SSH tunnel.

.DESCRIPTION
  Opens an SSH tunnel local 6543 -> VPS 127.0.0.1:5432, runs
  tools/load_history.py against it, then closes the tunnel. The VPS Postgres
  never needs to be exposed to the public internet.

  Requires the OpenSSH client (built in to Windows 10/11).

.PARAMETER Src
  Directory of {SYMBOL}.csv files. Default: ..\i-want-to-build-an-algo\data\angel_symbols
  (the existing algo project on this machine).

.PARAMETER Interval
  Candle interval label written to the candles table. Default: 5m.

.PARAMETER VpsUser
  SSH user @ VPS. Required.

.PARAMETER VpsHost
  VPS hostname or IP. Required.

.PARAMETER LocalPort
  Local port the tunnel listens on. Default: 6543. Change if 6543 is taken.

.PARAMETER PgUser
  Postgres role on the VPS. Default: paper.

.PARAMETER PgDb
  Postgres database. Default: paper_trading.

.PARAMETER PgPassword
  Postgres password. If omitted, prompts securely.

.EXAMPLE
  .\tools\upload_to_vps.ps1 -VpsUser ubuntu -VpsHost 203.0.113.10
#>

[CmdletBinding()]
param(
  [string]$Src = "..\i-want-to-build-an-algo\data\angel_symbols",
  [string]$Interval = "5m",
  [Parameter(Mandatory = $true)] [string]$VpsUser,
  [Parameter(Mandatory = $true)] [string]$VpsHost,
  [int]$LocalPort = 6543,
  [string]$PgUser = "paper",
  [string]$PgDb = "paper_trading",
  [string]$PgPassword
)

$ErrorActionPreference = "Stop"

# Resolve repo root (this script lives at tools/upload_to_vps.ps1)
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

# Validate source
$resolvedSrc = Resolve-Path -ErrorAction SilentlyContinue $Src
if (-not $resolvedSrc) {
  Write-Error "CSV source directory not found: $Src"
  exit 1
}
Write-Host "Source     : $resolvedSrc"
Write-Host "Target     : ${PgUser}@${VpsHost} -> 127.0.0.1:5432 / $PgDb"
Write-Host "Local port : $LocalPort"
Write-Host "Interval   : $Interval"
Write-Host ""

# Prompt for password if not supplied
if (-not $PgPassword) {
  $secure = Read-Host -AsSecureString "Postgres password for $PgUser"
  $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    $PgPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
  } finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

# Open SSH tunnel in background.
# -N = no remote command, -T = no PTY, -L = local forward.
# We use `Start-Process` so we get a PID we can kill.
$sshArgs = @(
  "-N", "-T",
  "-o", "ExitOnForwardFailure=yes",
  "-o", "ServerAliveInterval=30",
  "-L", "${LocalPort}:127.0.0.1:5432",
  "${VpsUser}@${VpsHost}"
)

Write-Host "Opening SSH tunnel..."
$tunnel = Start-Process -FilePath "ssh" -ArgumentList $sshArgs -PassThru -WindowStyle Hidden

# Wait briefly and verify the tunnel actually came up
Start-Sleep -Seconds 2
if ($tunnel.HasExited) {
  Write-Error "SSH tunnel failed to open (exit code $($tunnel.ExitCode)). Run the ssh command manually to see why."
  exit 1
}

try {
  Write-Host "Running loader..."
  $env:PG_PASSWORD = $PgPassword
  & python -m tools.load_history `
      --src $resolvedSrc `
      --interval $Interval `
      --pg-host 127.0.0.1 `
      --pg-port $LocalPort `
      --pg-user $PgUser `
      --pg-db $PgDb
  $loaderExit = $LASTEXITCODE
} finally {
  Remove-Item Env:PG_PASSWORD -ErrorAction SilentlyContinue
  Write-Host ""
  Write-Host "Closing SSH tunnel (PID $($tunnel.Id))..."
  if (-not $tunnel.HasExited) {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  }
}

if ($loaderExit -ne 0) {
  Write-Error "Loader exited with code $loaderExit"
  exit $loaderExit
}

Write-Host "Upload complete."
