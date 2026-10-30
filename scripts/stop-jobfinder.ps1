$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = if ($env:JOBFINDER_WEB_DATA) { [System.IO.Path]::GetFullPath($env:JOBFINDER_WEB_DATA) } else { Join-Path $projectRoot 'data\webapp' }
$pidFile = Join-Path $runtimeDir 'server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No JobFinder PID file.'; exit 0 }
$serverProcessId = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$serverProcessId"
$expected = Join-Path $PSScriptRoot 'serve_web.py'
if ($process -and $process.CommandLine.Contains($expected)) {
    # Stop only the worker children of this verified launcher, then its server.
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$serverProcessId" | Where-Object { $_.Name -match '^python' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
    Stop-Process -Id $serverProcessId -ErrorAction SilentlyContinue
    Write-Output 'JobFinder stopped. Task state is retained for recovery.'
} elseif ($process) { throw 'PID belongs to another process; nothing was stopped.' }
Remove-Item -LiteralPath $pidFile -Force
