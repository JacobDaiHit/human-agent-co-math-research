param(
    [int]$Port = 8000,
    [ValidateSet('fake', 'deepseek', 'glm')][string[]]$Providers = @('fake'),
    [switch]$NoWorker,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiProcess = $null
$workerProcess = $null
$previousOrigin = $env:MATHAGENT_FRONTEND_ORIGIN
Push-Location -LiteralPath $projectRoot
try {
    $interpreter = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $interpreter)) { throw 'Run scripts/bootstrap.ps1 first.' }
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $probe.Connect('127.0.0.1', $Port)
        if ($probe.Connected) { throw "Port $Port is already in use. Open the existing workbench or select -Port." }
    } catch [System.Net.Sockets.SocketException] {
        # A refused connection means this requested local port is free.
    } finally { $probe.Dispose() }
    if (-not $SkipBuild) {
        Push-Location -LiteralPath (Join-Path $projectRoot 'apps\web')
        try {
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed. Run scripts/bootstrap.ps1 if dependencies are missing.' }
        } finally { Pop-Location }
    }
    if (-not (Test-Path -LiteralPath 'apps\web\dist\index.html')) { throw 'Build the frontend before starting.' }
    $runtimeDir = Join-Path $projectRoot 'data'
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $logDir = Join-Path $runtimeDir "logs\$Port"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $env:MATHAGENT_FRONTEND_ORIGIN = "http://127.0.0.1:$Port"
    $apiProcess = Start-Process -FilePath $interpreter -ArgumentList @('-X', 'utf8', '-m', 'uvicorn', 'mathagent.api.app:create_app', '--factory', '--host', '127.0.0.1', '--port', "$Port", '--no-access-log') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'api.stdout.log') -RedirectStandardError (Join-Path $logDir 'api.stderr.log') -PassThru
    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($apiProcess.HasExited) { throw "API startup failed. See data/logs/$Port/api.stderr.log." }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1 -UseBasicParsing
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $ready) { throw 'API did not become healthy within 20 seconds.' }
    if (-not $NoWorker) {
        $workerArgs = @('-X', 'utf8', '-m', 'mathagent.runtime.worker', '--api-url', "http://127.0.0.1:$Port", '--providers') + $Providers
        $workerProcess = Start-Process -FilePath $interpreter -ArgumentList $workerArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'worker.stdout.log') -RedirectStandardError (Join-Path $logDir 'worker.stderr.log') -PassThru
    }
    Write-Host "MathAgent: http://127.0.0.1:$Port"
    Write-Host "Worker providers: $($Providers -join ', '). Press Ctrl+C to stop this launcher."
    Write-Host 'Data and service logs are under data/. Existing work remains in SQLite.'
    while (-not $apiProcess.HasExited) {
        if ($workerProcess -and $workerProcess.HasExited) { throw "Worker exited. See data/logs/$Port/worker.stderr.log." }
        Start-Sleep -Seconds 1
    }
    throw 'API exited. See data/api.stderr.log.'
} finally {
    if ($workerProcess -and -not $workerProcess.HasExited) { Stop-Process -Id $workerProcess.Id -ErrorAction SilentlyContinue }
    if ($apiProcess -and -not $apiProcess.HasExited) { Stop-Process -Id $apiProcess.Id -ErrorAction SilentlyContinue }
    $env:MATHAGENT_FRONTEND_ORIGIN = $previousOrigin
    Pop-Location
}
