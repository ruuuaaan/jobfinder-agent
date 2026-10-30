param([string]$Python = '', [switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = 'py' }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = 'python' }
    else { throw 'Install Python 3.11+ first, or supply -Python with the full python.exe path.' }
}
& $Python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-web.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; check network access and retry.' }
if (-not $SkipFrontend) {
    if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
        throw 'Frontend source build needs Node.js 22.12+ and pnpm 11.25.0. A release with web/dist can use -SkipFrontend.'
    }
    Push-Location (Join-Path $projectRoot 'web')
    try {
        & pnpm install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        & pnpm build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web\dist\index.html'))) { throw 'Frontend is missing. Rerun without -SkipFrontend.' }
& $venvPython -c "import fastapi, uvicorn, httpx, playwright, cryptography; from zoneinfo import ZoneInfo; print('Ready:', ZoneInfo('Asia/Shanghai'))"
if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed.' }
Write-Output 'Setup complete. Run scripts/start-jobfinder.ps1 or the startup CMD file. Browser search also needs Microsoft Edge.'
