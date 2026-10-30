param([int]$Port = 8765, [switch]$NoBrowser, [string]$Python = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = if ($env:JOBFINDER_WEB_DATA) { [System.IO.Path]::GetFullPath($env:JOBFINDER_WEB_DATA) } else { Join-Path $projectRoot 'data\webapp' }
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$localUrl = "http://127.0.0.1:$Port"
function Get-LocalHealth {
    $request = [System.Net.HttpWebRequest]::Create("$localUrl/api/health")
    $request.Proxy = $null
    $request.Timeout = 2000
    $response = $request.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
}
try {
    $health = Get-LocalHealth
    if ($health.service -eq 'JobFinder') {
        if (-not $NoBrowser) { Start-Process $localUrl }
        Write-Output "JobFinder is running: $localUrl"
        exit 0
    }
} catch { }
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonExe = if ($Python) { $Python } elseif (Test-Path -LiteralPath $venvPython) { $venvPython } else { throw 'Run scripts/setup.ps1 first, or provide -Python with a configured interpreter.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web\dist\index.html'))) { throw 'Build the frontend first. See WEBAPP.md.' }
$entryFile = Join-Path $PSScriptRoot 'serve_web.py'
$argumentString = '"' + $entryFile + '" --port ' + $Port
$server = Start-Process -FilePath $pythonExe -ArgumentList $argumentString -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeDir 'server.out.log') -RedirectStandardError (Join-Path $runtimeDir 'server.err.log') -PassThru
$server.Id | Set-Content -LiteralPath (Join-Path $runtimeDir 'server.pid') -Encoding utf8
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($server.HasExited) { throw 'JobFinder exited. Check data/webapp/server.err.log.' }
    try {
        $health = Get-LocalHealth
        if ($health.service -eq 'JobFinder') {
            if (-not $NoBrowser) { Start-Process $localUrl }
            Write-Output "JobFinder started: $localUrl"
            exit 0
        }
    } catch { }
}
throw 'Startup timed out. Check data/webapp/server.err.log.'
