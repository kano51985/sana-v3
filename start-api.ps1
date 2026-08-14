param(
    [string]$ListenAddress = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [switch]$SkipMigrations,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot "venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

if (-not $SkipMigrations) {
    Write-Host "Applying PostgreSQL migrations..." -ForegroundColor Cyan
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$arguments = @(
    "-m", "uvicorn", "sana.app.api.main:app",
    "--host", $ListenAddress,
    "--port", $Port.ToString()
)
if ($Reload) { $arguments += "--reload" }

Write-Host "Starting Sana API on ${ListenAddress}:$Port" -ForegroundColor Green
& $python @arguments
exit $LASTEXITCODE
