param(
    [ValidateRange(1, 65535)][int]$Port = 8501,
    [string]$ApiUrl = "",
    [switch]$Legacy
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

if (-not $ApiUrl) {
    $ApiUrl = if ($env:SANA_API_URL) { $env:SANA_API_URL } else { "http://localhost:8000" }
}
$env:SANA_API_URL = $ApiUrl

$legacyRequested = $Legacy -or $env:SANA_UI_MODE -eq "legacy"
$entrypoint = if ($legacyRequested) {
    Write-Warning "Launching the legacy rollback UI; do not use it for production multi-user traffic."
    "interfaces\streamlit_app.py"
} else {
    "sana\clients\streamlit\app.py"
}

Write-Host "Starting Sana UI on port $Port against $ApiUrl" -ForegroundColor Green
& $python -m streamlit run $entrypoint --server.port $Port
exit $LASTEXITCODE
