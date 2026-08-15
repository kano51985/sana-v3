[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('prepare', 'down', 'create', 'list', 'resume', 'pause', 'abort', 'review', 'report')]
    [string]$Command,
    [string]$CampaignKey,
    [ValidateSet('docker-smoke-v1', 'shadow-full-v1')]
    [string]$Profile = 'docker-smoke-v1',
    [string]$CampaignId,
    [string]$ParentSmokeCampaignId,
    [string]$Manifest = 'evals/shadow/cases-v1.jsonl'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ComposeFile = Join-Path $WorkspaceRoot 'deployment/docker-compose.shadow-eval.yml'
$AttestationPath = Join-Path $WorkspaceRoot 'var/shadow-eval/attestation.json'
$ProjectName = 'sana-shadow-eval'
$CandidateImage = if ($env:SANA_SHADOW_IMAGE) { $env:SANA_SHADOW_IMAGE } else { 'sana-shadow-eval:local' }
$ComposeArgs = @('--project-name', $ProjectName, '-f', $ComposeFile)

function Assert-LastExitCode([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

function Get-Sha256Text([string]$Value) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [System.Security.Cryptography.SHA256]::HashData($bytes)
    return [Convert]::ToHexString($hash).ToLowerInvariant()
}

function Set-SecretEnvironment([string]$Name, [string]$Prompt) {
    $current = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($current) { return }
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if (-not $value) { throw "$Name cannot be empty" }
        [Environment]::SetEnvironmentVariable($Name, $value, 'Process')
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Invoke-Compose([string[]]$Arguments) {
    & docker compose @ComposeArgs @Arguments
    Assert-LastExitCode "docker compose $($Arguments -join ' ')"
}

function Get-ComposeOutput([string[]]$Arguments) {
    $output = & docker compose @ComposeArgs @Arguments
    Assert-LastExitCode "docker compose $($Arguments -join ' ')"
    return ($output -join "`n").Trim()
}

function Assert-NoPublishedPort([string]$Service, [string]$Port) {
    $output = & docker compose @ComposeArgs port $Service $Port 2>$null
    if ($LASTEXITCODE -eq 0 -and (($output -join "`n").Trim())) {
        throw "$Service must not publish port $Port"
    }
}

function Assert-CleanSource {
    $status = (& git -C $WorkspaceRoot status --porcelain=v1 --untracked-files=all) -join "`n"
    Assert-LastExitCode 'git status'
    if ($status) {
        throw 'Candidate/Harness worktree must be completely clean before prepare'
    }
}

function Get-TrackedFilesetHash {
    $paths = @(& git -C $WorkspaceRoot ls-files -- sana scripts deployment alembic evals pyproject.toml alembic.ini)
    Assert-LastExitCode 'git ls-files'
    $entries = foreach ($path in ($paths | Sort-Object)) {
        $digest = (& git -C $WorkspaceRoot hash-object -- $path).Trim()
        Assert-LastExitCode "git hash-object $path"
        "$path`0$digest`n"
    }
    return Get-Sha256Text ($entries -join '')
}

function Get-TopologyHash([hashtable]$Topology) {
    $python = Join-Path $WorkspaceRoot 'venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
    $json = $Topology | ConvertTo-Json -Depth 20 -Compress
    $digest = $json | & $python -c "import json,sys; from sana.modules.shadow_campaign.domain import snapshot_hash; print(snapshot_hash(json.load(sys.stdin)))"
    Assert-LastExitCode 'topology hash calculation'
    return ($digest -join '').Trim()
}

function Prepare-ShadowEnvironment {
    Assert-CleanSource
    Set-SecretEnvironment 'SANA_SHADOW_OWNER_DB_PASSWORD' 'Shadow database owner password'
    Set-SecretEnvironment 'SANA_SHADOW_APP_DB_PASSWORD' 'Shadow database application password'
    Set-SecretEnvironment 'DEEPSEEK_API_KEY' 'DeepSeek API key'
    Set-SecretEnvironment 'SANA_ACCESS_TOKEN' 'Local Sana token (tenant UUID:user UUID)'

    $commit = (& git -C $WorkspaceRoot rev-parse HEAD).Trim()
    Assert-LastExitCode 'git rev-parse HEAD'
    $env:SANA_CANDIDATE_COMMIT_SHA = $commit
    $env:SANA_SHADOW_IMAGE = $CandidateImage
    $env:SANA_SHADOW_ATTESTATION_PATH = $AttestationPath

    $renderedConfig = Get-ComposeOutput @('config', '--no-interpolate', '--format', 'json')
    if ($renderedConfig.ToLowerInvariant().Contains('docker.sock')) {
        throw 'Campaign Runner topology must not mount the Docker socket'
    }
    $configHash = Get-Sha256Text $renderedConfig

    Invoke-Compose @('build', 'migrate')
    $imageId = (& docker image inspect --format '{{.Id}}' $CandidateImage).Trim()
    Assert-LastExitCode 'docker image inspect candidate'
    $revision = (& docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' $CandidateImage).Trim()
    Assert-LastExitCode 'docker image inspect OCI revision'
    if ($revision -ne $commit) { throw 'OCI revision label does not equal candidate commit' }

    Invoke-Compose @('up', '-d', '--wait', 'postgres', 'provision-db-role', 'redis', 'migrate', 'artifact-init', 'api', 'dispatcher', 'worker')

    $apiBinding = Get-ComposeOutput @('port', 'api', '8000')
    if (-not $apiBinding.StartsWith('127.0.0.1:')) {
        throw 'Shadow API is not bound exclusively to loopback'
    }
    Assert-NoPublishedPort 'postgres' '5432'
    Assert-NoPublishedPort 'redis' '6379'

    $candidateServices = @('migrate', 'artifact-init', 'api', 'dispatcher', 'worker')
    foreach ($service in $candidateServices) {
        $serviceImage = Get-ComposeOutput @('images', '-q', $service)
        if ($serviceImage -ne $imageId) { throw "$service does not use the candidate image ID" }
    }

    $migrationHead = Get-ComposeOutput @('exec', '-T', 'postgres', 'psql', '-U', 'sana_shadow_owner', '-d', 'sana_shadow', '-Atc', 'SELECT version_num FROM alembic_version')
    if ($migrationHead -ne '0010_shadow_collector_audit') {
        throw "Unexpected migration head: $migrationHead"
    }
    $activeRuns = [int](Get-ComposeOutput @('exec', '-T', 'postgres', 'psql', '-U', 'sana_shadow_owner', '-d', 'sana_shadow', '-Atc', "SELECT count(*) FROM search_runs r WHERE r.status IN ('QUEUED','RUNNING') AND NOT EXISTS (SELECT 1 FROM shadow_run_results s WHERE s.tenant_id=r.tenant_id AND s.search_run_id=r.id)"))
    $pendingOutbox = [int](Get-ComposeOutput @('exec', '-T', 'postgres', 'psql', '-U', 'sana_shadow_owner', '-d', 'sana_shadow', '-Atc', 'SELECT count(*) FROM outbox_events WHERE published_at IS NULL'))
    $queueDepth = 0
    foreach ($queue in @('fast', 'research', 'crawl', 'maintenance')) {
        $queueDepth += [int](Get-ComposeOutput @('exec', '-T', 'redis', 'redis-cli', '-n', '0', 'LLEN', $queue))
    }
    if ($activeRuns -ne 0 -or $pendingOutbox -ne 0 -or $queueDepth -ne 0) {
        throw 'Shadow environment is not empty before Campaign creation'
    }

    $networkId = (& docker network inspect --format '{{.Id}}' 'sana-shadow-eval-net').Trim()
    Assert-LastExitCode 'docker network inspect'
    $volumeNames = @(
        'sana-shadow-eval-postgres',
        'sana-shadow-eval-redis',
        'sana-shadow-eval-search-artifacts',
        'sana-shadow-eval-campaign-reports'
    )
    $volumeIds = [ordered]@{}
    foreach ($volume in $volumeNames) {
        $volumeIds[$volume] = (& docker volume inspect --format '{{.Name}}' $volume).Trim()
        Assert-LastExitCode "docker volume inspect $volume"
    }
    $images = [ordered]@{}
    foreach ($service in @('migrate', 'artifact-init', 'api', 'dispatcher', 'worker', 'campaign-runner')) {
        $images[$service] = $imageId
    }
    $resourceLimits = [ordered]@{
        api = [ordered]@{ cpus = '1.0'; memory = '512m' }
        worker = [ordered]@{ cpus = '2.0'; memory = '2g' }
        'campaign-runner' = [ordered]@{ cpus = '1.0'; memory = '512m' }
    }
    $topology = [ordered]@{
        container_images = $images
        network = 'sana-shadow-eval-net'
        network_id = $networkId
        volume_ids = $volumeIds
        api_loopback = '127.0.0.1'
        database_published = $false
        redis_published = $false
        worker_concurrency = 2
        queues = @('crawl', 'fast', 'maintenance', 'research')
        resource_limits = $resourceLimits
        docker_socket_mounted = $false
    }
    $attestation = [ordered]@{
        schema_version = 'shadow-provenance-v1'
        candidate = [ordered]@{
            commit_sha = $commit
            source_clean = $true
            image_id = $imageId
            oci_revision = $revision
            alembic_head = $migrationHead
            config_hash = $configHash
        }
        harness = [ordered]@{
            commit_sha = $commit
            source_clean = $true
            fileset_hash = Get-TrackedFilesetHash
            collector_schema_version = 'shadow-collector-v1'
        }
        environment = [ordered]@{
            compose_project = $ProjectName
            container_images = $images
            network = 'sana-shadow-eval-net'
            network_id = $networkId
            volume_ids = $volumeIds
            api_loopback = '127.0.0.1'
            database_published = $false
            redis_published = $false
            worker_concurrency = 2
            queues = @('fast', 'research', 'crawl', 'maintenance')
            resource_limits = $resourceLimits
            docker_socket_mounted = $false
            initial_queue_depth = $queueDepth
            active_non_campaign_runs = $activeRuns
            pending_outbox = $pendingOutbox
            migration_head = $migrationHead
            config_hash = $configHash
            topology_hash = Get-TopologyHash $topology
        }
    }
    $directory = Split-Path -Parent $AttestationPath
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $json = $attestation | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($AttestationPath, $json, [System.Text.UTF8Encoding]::new($false))
    Write-Output "Shadow environment prepared; attestation: $AttestationPath"
}

function Invoke-RunnerCommand {
    Set-SecretEnvironment 'SANA_SHADOW_OWNER_DB_PASSWORD' 'Shadow database owner password'
    Set-SecretEnvironment 'SANA_SHADOW_APP_DB_PASSWORD' 'Shadow database application password'
    Set-SecretEnvironment 'DEEPSEEK_API_KEY' 'DeepSeek API key'
    Set-SecretEnvironment 'SANA_ACCESS_TOKEN' 'Local Sana token (tenant UUID:user UUID)'
    if (-not (Test-Path -LiteralPath $AttestationPath)) {
        throw 'Run prepare before invoking a Campaign command'
    }
    $env:SANA_SHADOW_ATTESTATION_PATH = $AttestationPath
    $env:SANA_SHADOW_IMAGE = $CandidateImage
    $env:SANA_CANDIDATE_COMMIT_SHA = (& git -C $WorkspaceRoot rev-parse HEAD).Trim()
    Assert-LastExitCode 'git rev-parse HEAD'
    $arguments = @('run', '--rm', 'campaign-runner', 'python', 'scripts/run_shadow_campaign.py', $Command, '--api-url', 'http://api:8000')
    switch ($Command) {
        'create' {
            if (-not $CampaignKey) { throw 'create requires -CampaignKey' }
            $arguments += @('--confirm-live', '--campaign-key', $CampaignKey, '--manifest', $Manifest, '--profile', $Profile)
            if ($Profile -eq 'shadow-full-v1') {
                if (-not $ParentSmokeCampaignId) { throw 'Full create requires -ParentSmokeCampaignId' }
                $arguments += @('--parent-smoke-campaign-id', $ParentSmokeCampaignId)
            }
        }
        'list' { }
        default {
            if (-not $CampaignId) { throw "$Command requires -CampaignId" }
            $arguments += @('--campaign-id', $CampaignId)
            if ($Command -in @('resume', 'pause', 'abort')) {
                $arguments += @('--manifest', $Manifest)
            }
        }
    }
    $composeCommand = @('--profile', 'runner') + $arguments
    Invoke-Compose $composeCommand
}

switch ($Command) {
    'prepare' { Prepare-ShadowEnvironment }
    'down' {
        if (-not $env:SANA_SHADOW_OWNER_DB_PASSWORD) { $env:SANA_SHADOW_OWNER_DB_PASSWORD = 'unused-for-down' }
        if (-not $env:SANA_SHADOW_APP_DB_PASSWORD) { $env:SANA_SHADOW_APP_DB_PASSWORD = 'unused-for-down' }
        if (-not $env:DEEPSEEK_API_KEY) { $env:DEEPSEEK_API_KEY = 'unused-for-down' }
        if (-not $env:SANA_ACCESS_TOKEN) { $env:SANA_ACCESS_TOKEN = 'unused-for-down' }
        if (-not $env:SANA_CANDIDATE_COMMIT_SHA) { $env:SANA_CANDIDATE_COMMIT_SHA = '0000000000000000000000000000000000000000' }
        if (-not $env:SANA_SHADOW_ATTESTATION_PATH) { $env:SANA_SHADOW_ATTESTATION_PATH = $AttestationPath }
        Invoke-Compose @('down', '--remove-orphans')
        Write-Output 'Shadow containers/network removed; evidence volumes were preserved'
    }
    default { Invoke-RunnerCommand }
}
