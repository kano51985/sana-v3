$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "`n === Sana Agent ===`n" -ForegroundColor Cyan

if (-not $env:DEEPSEEK_API_KEY -and @($args) -notcontains "--api-key") {
    $savedKey = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
    if ($savedKey) {
        $env:DEEPSEEK_API_KEY = $savedKey
    } else {
        $env:DEEPSEEK_API_KEY = Read-Host "Input your DeepSeek API Key"
        [Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", $env:DEEPSEEK_API_KEY, "User")
    }
}

Write-Host "Starting..." -ForegroundColor Green
& "venv/Scripts/python" "interfaces/cli.py" @args
