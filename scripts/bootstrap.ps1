param([string]$Python = '3.13')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv is required. Install uv and rerun this script.'
    }
    & uv sync --frozen --python $Python --link-mode copy
    if ($LASTEXITCODE -ne 0) { throw 'Dependency synchronization failed.' }
    & '.\.venv\Scripts\python.exe' -c 'import sys; print(sys.version); print(sys.executable)'
    if ($LASTEXITCODE -ne 0) { throw 'Python environment check failed.' }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw 'Node.js 22 or newer with npm is required for the workbench.'
    }
    Push-Location -LiteralPath (Join-Path $projectRoot 'apps\web')
    try {
        & npm.cmd ci --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
} finally {
    Pop-Location
}
