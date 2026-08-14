param(
    [ValidateSet("worker", "dispatcher")][string]$Role = "worker",
    [ValidateSet("fast", "research", "crawl", "maintenance", "all")]
    [string]$Queue = "all",
    [ValidateRange(1, 128)][int]$Concurrency = 2,
    [switch]$Once
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

if ($Role -eq "dispatcher") {
    $arguments = @("-m", "sana.app.outbox_dispatcher")
    if ($Once) { $arguments += "--once" }
    Write-Host "Starting tenant-aware outbox dispatcher" -ForegroundColor Green
    & $python @arguments
    exit $LASTEXITCODE
}

if (-not $env:SANA_STEP_HANDLER_FACTORY) {
    throw "SANA_STEP_HANDLER_FACTORY must name a concrete module:function before a Worker can start."
}

$queues = if ($Queue -eq "all") {
    "fast,research,crawl,maintenance"
} else {
    $Queue
}

Write-Host "Starting Sana Worker for queue(s): $queues" -ForegroundColor Green
& $python -m celery -A "sana.app.worker:create_app" worker `
    --loglevel INFO `
    --queues $queues `
    --concurrency $Concurrency `
    --hostname "sana-$Queue@%h"
exit $LASTEXITCODE
