[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Guid]$CampaignId,
    [string]$Manifest = 'evals/shadow/cases-v1.jsonl',
    [switch]$OfflineFixture
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectName = 'sana-shadow-eval'
$AttestationPath = Join-Path $WorkspaceRoot 'var/shadow-eval/attestation.json'
$ManifestPath = Join-Path $WorkspaceRoot $Manifest
$CandidateServices = @('api', 'dispatcher', 'worker')
$RlsTables = @(
    'shadow_campaigns',
    'shadow_run_results',
    'shadow_gold_assertion_results',
    'shadow_manual_reviews'
)

function Invoke-DockerText([string[]]$DockerArguments, [string]$Operation) {
    $output = & docker @DockerArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed"
    }
    return ($output -join "`n").Trim()
}

function Get-ServiceContainer([string]$Service) {
    $container = Invoke-DockerText @(
        'ps',
        '--filter', "label=com.docker.compose.project=$ProjectName",
        '--filter', "label=com.docker.compose.service=$Service",
        '--format', '{{.ID}}'
    ) "resolve $Service container"
    if (-not $container) {
        throw "$Service container is not running"
    }
    return ($container -split "`n")[0].Trim()
}

function Get-ContainerEnvironment([string]$Container) {
    return Invoke-DockerText @(
        'inspect',
        '--format', '{{range .Config.Env}}{{println .}}{{end}}',
        $Container
    ) 'read container environment'
}

function Get-EnvironmentValue([string]$EnvironmentText, [string]$Name) {
    $prefix = "$Name="
    foreach ($line in ($EnvironmentText -split "`r?`n")) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            return $line.Substring($prefix.Length)
        }
    }
    return ''
}

function Assert-Equal($Actual, $Expected, [string]$Name) {
    if ($Actual -ne $Expected) {
        throw "$Name invariant failed"
    }
}

function Assert-Zero($Actual, [string]$Name) {
    if ([decimal]$Actual -ne 0) {
        throw "$Name must be zero"
    }
}

if (-not (Test-Path -LiteralPath $AttestationPath -PathType Leaf)) {
    throw 'shadow attestation is missing; run prepare first'
}
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw 'Campaign manifest is missing'
}

$attestation = Get-Content -LiteralPath $AttestationPath -Raw | ConvertFrom-Json
Assert-Equal $attestation.schema_version 'shadow-provenance-v2' 'attestation schema'
$expectedExecutionClass = if ($OfflineFixture) { 'OFFLINE_FIXTURE' } else { 'LIVE_DEEPSEEK' }
Assert-Equal $attestation.environment.execution_class $expectedExecutionClass 'execution class'

$head = (& git -C $WorkspaceRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'git rev-parse failed' }
Assert-Equal $head $attestation.candidate.commit_sha 'candidate commit'
$sourceStatus = (& git -C $WorkspaceRoot status --porcelain=v1 --untracked-files=all) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'git status failed' }
Assert-Equal $sourceStatus '' 'candidate source cleanliness'

$expectedImage = [string]$attestation.candidate.image_id
if ($expectedImage -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'attested candidate image ID is invalid'
}
foreach ($property in $attestation.environment.container_images.PSObject.Properties) {
    Assert-Equal ([string]$property.Value) $expectedImage "attested $($property.Name) image"
}
foreach ($service in $CandidateServices) {
    $container = Get-ServiceContainer $service
    $actualImage = Invoke-DockerText @('inspect', '--format', '{{.Image}}', $container) "inspect $service image"
    Assert-Equal $actualImage $expectedImage "$service image"
}
$attestedEnvironmentIdentity = Invoke-DockerText @(
    'run', '--rm', '--read-only',
    '--volume', "${AttestationPath}:/run/sana/attestation.json:ro",
    $expectedImage,
    'python', '-c',
    "from pathlib import Path; from sana.app.shadow_provenance import parse_shadow_attestation_bytes; print(parse_shadow_attestation_bytes(Path('/run/sana/attestation.json').read_bytes()).provenance.environment_identity_hash)"
) 'recompute attested environment identity'

$postgres = Get-ServiceContainer 'postgres'
$postgresEnvironment = Get-ContainerEnvironment $postgres
$databaseUser = Get-EnvironmentValue $postgresEnvironment 'POSTGRES_USER'
$databaseName = Get-EnvironmentValue $postgresEnvironment 'POSTGRES_DB'
if (-not $databaseUser -or -not $databaseName) {
    throw 'isolated PostgreSQL identity is incomplete'
}

$campaignLiteral = $CampaignId.ToString()
$campaignSql = @"
SELECT json_build_object(
  'tenant_id', tenant_id,
  'candidate_commit_sha', candidate_commit_sha,
  'candidate_source_clean', candidate_source_clean,
  'candidate_image_id', candidate_image_id,
  'candidate_oci_revision', candidate_oci_revision,
  'candidate_config_hash', candidate_config_hash,
  'harness_commit_sha', harness_commit_sha,
  'harness_source_clean', harness_source_clean,
  'harness_fileset_hash', harness_fileset_hash,
  'environment_identity_hash', environment_identity_hash,
  'alembic_head', alembic_head,
  'execution_class', environment_snapshot->>'execution_class',
  'status', status,
  'gate_status', gate_status,
  'planned_count', planned_count,
  'submitted_count', submitted_count,
  'collected_count', collected_count,
  'failed_count', failed_count,
  'observed_provider_calls', observed_provider_calls,
  'observed_prompt_tokens', observed_prompt_tokens,
  'observed_completion_tokens', observed_completion_tokens,
  'observed_estimated_cost', observed_estimated_cost::text,
  'report_bound', final_json_sha256 IS NOT NULL
      AND final_markdown_sha256 IS NOT NULL
      AND decision_hash IS NOT NULL,
  'json_sha256', final_json_sha256,
  'markdown_sha256', final_markdown_sha256,
  'result_count', (SELECT count(*) FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'distinct_run_count', (SELECT count(DISTINCT search_run_id) FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'max_submission_attempt_count', (SELECT coalesce(max(submission_attempt_count), 0) FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'active_reservation_count', (SELECT count(*) FROM shadow_run_results r WHERE r.campaign_id = c.id AND reservation_state = 'ACTIVE'),
  'result_model_calls', (SELECT coalesce(sum(model_call_count), 0) FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'result_prompt_tokens', (SELECT coalesce(sum(prompt_tokens), 0) FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'result_completion_tokens', (SELECT coalesce(sum(completion_tokens), 0) FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'result_estimated_cost', (SELECT coalesce(sum(estimated_cost), 0)::text FROM shadow_run_results r WHERE r.campaign_id = c.id),
  'invocation_count', (SELECT count(*) FROM model_invocations m WHERE m.run_id IN (SELECT search_run_id FROM shadow_run_results r WHERE r.campaign_id = c.id)),
  'provider_called_count', (SELECT count(*) FROM model_invocations m WHERE m.provider_called AND m.run_id IN (SELECT search_run_id FROM shadow_run_results r WHERE r.campaign_id = c.id)),
  'active_run_count', (SELECT count(*) FROM search_runs s WHERE s.status IN ('QUEUED', 'RUNNING', 'WAITING') AND s.id IN (SELECT search_run_id FROM shadow_run_results r WHERE r.campaign_id = c.id))
)::text
FROM shadow_campaigns c
WHERE c.id = '$campaignLiteral';
"@
$campaignJson = Invoke-DockerText @(
    'exec', $postgres,
    'psql', '-v', 'ON_ERROR_STOP=1', '-U', $databaseUser, '-d', $databaseName,
    '-Atc', $campaignSql
) 'audit Campaign ledger'
if (-not $campaignJson) {
    throw 'Campaign was not found in the isolated database'
}
$audit = $campaignJson | ConvertFrom-Json

Assert-Equal $audit.candidate_commit_sha $attestation.candidate.commit_sha 'Campaign candidate commit'
Assert-Equal $audit.candidate_source_clean $true 'Campaign candidate source cleanliness'
Assert-Equal $audit.candidate_image_id $attestation.candidate.image_id 'Campaign candidate image'
Assert-Equal $audit.candidate_oci_revision $attestation.candidate.oci_revision 'Campaign OCI revision'
Assert-Equal $audit.candidate_config_hash $attestation.candidate.config_hash 'Campaign candidate config'
Assert-Equal $audit.harness_commit_sha $attestation.harness.commit_sha 'Campaign harness commit'
Assert-Equal $audit.harness_source_clean $true 'Campaign harness source cleanliness'
Assert-Equal $audit.harness_fileset_hash $attestation.harness.fileset_hash 'Campaign harness fileset'
Assert-Equal $audit.environment_identity_hash $attestedEnvironmentIdentity 'Campaign environment identity'
Assert-Equal $audit.alembic_head $attestation.candidate.alembic_head 'Campaign migration head'
Assert-Equal $audit.execution_class $expectedExecutionClass 'Campaign execution class'
Assert-Equal $audit.status 'COMPLETED' 'Campaign status'
if ($audit.gate_status -notin @('PASS', 'FAIL', 'INSUFFICIENT_SAMPLE')) {
    throw 'Campaign gate is not final'
}
Assert-Equal $audit.report_bound $true 'final report binding'
Assert-Equal ([int]$audit.submitted_count) ([int]$audit.planned_count) 'submitted count'
Assert-Equal ([int]$audit.collected_count) ([int]$audit.planned_count) 'collected count'
Assert-Equal ([int]$audit.result_count) ([int]$audit.planned_count) 'Result count'
Assert-Equal ([int]$audit.distinct_run_count) ([int]$audit.result_count) 'SearchRun uniqueness'
Assert-Equal ([int]$audit.max_submission_attempt_count) 1 'submission attempt count'
Assert-Zero $audit.failed_count 'Campaign failed Result count'
Assert-Zero $audit.active_reservation_count 'active reservation count'
Assert-Zero $audit.active_run_count 'active Campaign SearchRun count'

$globalSql = @"
SELECT json_build_object(
  'pending_outbox', (SELECT count(*) FROM outbox_events WHERE published_at IS NULL),
  'active_runs', (SELECT count(*) FROM search_runs WHERE status IN ('QUEUED', 'RUNNING', 'WAITING')),
  'active_reservations', (SELECT count(*) FROM shadow_run_results WHERE reservation_state = 'ACTIVE'),
  'rls_table_count', (SELECT count(*) FROM pg_class WHERE relname = ANY(ARRAY['shadow_campaigns','shadow_run_results','shadow_gold_assertion_results','shadow_manual_reviews'])),
  'rls_enabled_count', (SELECT count(*) FROM pg_class WHERE relname = ANY(ARRAY['shadow_campaigns','shadow_run_results','shadow_gold_assertion_results','shadow_manual_reviews']) AND relrowsecurity AND relforcerowsecurity)
)::text;
"@
$globalAudit = (Invoke-DockerText @(
    'exec', $postgres,
    'psql', '-v', 'ON_ERROR_STOP=1', '-U', $databaseUser, '-d', $databaseName,
    '-Atc', $globalSql
) 'audit isolated database') | ConvertFrom-Json
Assert-Zero $globalAudit.pending_outbox 'pending Outbox count'
Assert-Zero $globalAudit.active_runs 'global active SearchRun count'
Assert-Zero $globalAudit.active_reservations 'global active reservation count'
Assert-Equal ([int]$globalAudit.rls_table_count) $RlsTables.Count 'RLS table count'
Assert-Equal ([int]$globalAudit.rls_enabled_count) $RlsTables.Count 'FORCE RLS count'

$redis = Get-ServiceContainer 'redis'
foreach ($queue in @($attestation.environment.queues)) {
    $depth = Invoke-DockerText @('exec', $redis, 'redis-cli', 'LLEN', [string]$queue) "read $queue queue depth"
    Assert-Zero $depth "$queue queue depth"
}

$worker = Get-ServiceContainer 'worker'
$workerState = Invoke-DockerText @(
    'inspect', '--format', '{{.State.Status}}|{{.State.Health.Status}}|{{.State.Paused}}', $worker
) 'inspect worker health'
Assert-Equal $workerState 'running|healthy|false' 'worker health'
$workerProcesses = Invoke-DockerText @('top', $worker, '-eo', 'pid,ppid,args') 'inspect worker processes'
$workerProcessRows = @(($workerProcesses -split "`r?`n") | Select-Object -Skip 1)
$celeryProcessRows = @(
    $workerProcessRows | Where-Object {
        $_.Contains('python -m celery -A sana.app.worker_entrypoint:app worker')
    }
)
$healthProbeRows = @(
    $workerProcessRows | Where-Object {
        $_.Contains('python -c import socket;') -and
        $_.Contains("socket.create_connection(('redis', 6379)") -and
        $_.Contains('PING')
    }
)
$unexpectedProcessRows = @(
    $workerProcessRows | Where-Object {
        $_ -notin $celeryProcessRows -and $_ -notin $healthProbeRows
    }
)
Assert-Equal $celeryProcessRows.Count 3 'Celery worker process count'
if ($healthProbeRows.Count -gt 1 -or $unexpectedProcessRows.Count -ne 0) {
    throw 'worker process allowlist invariant failed'
}

if ($OfflineFixture) {
    Assert-Zero $audit.observed_provider_calls 'Campaign provider calls'
    Assert-Zero $audit.observed_prompt_tokens 'Campaign prompt tokens'
    Assert-Zero $audit.observed_completion_tokens 'Campaign completion tokens'
    Assert-Zero $audit.observed_estimated_cost 'Campaign estimated cost'
    Assert-Zero $audit.result_model_calls 'Result model calls'
    Assert-Zero $audit.result_prompt_tokens 'Result prompt tokens'
    Assert-Zero $audit.result_completion_tokens 'Result completion tokens'
    Assert-Zero $audit.result_estimated_cost 'Result estimated cost'
    Assert-Zero $audit.invocation_count 'ModelInvocation count'
    Assert-Zero $audit.provider_called_count 'provider-called invocation count'
}

$api = Get-ServiceContainer 'api'
$apiEnvironment = Get-ContainerEnvironment $api
$databaseUrl = Get-EnvironmentValue $apiEnvironment 'SANA_DATABASE_URL'
$applicationPassword = ''
if ($databaseUrl -match '^postgresql\+asyncpg://[^:]+:([^@]+)@') {
    $applicationPassword = $Matches[1]
}
$protectedValues = @(
    (Get-EnvironmentValue $postgresEnvironment 'POSTGRES_PASSWORD'),
    $applicationPassword,
    $env:SANA_ACCESS_TOKEN,
    $(if ($OfflineFixture) { 'shadow-offline-fixture-no-provider-call' } else { $env:DEEPSEEK_API_KEY })
) | Where-Object { $_ -and $_.Length -ge 8 }
foreach ($line in Get-Content -LiteralPath $ManifestPath) {
    if (-not $line.Trim()) { continue }
    $case = $line | ConvertFrom-Json
    if ($case.prompt -and $case.prompt.Length -ge 8) {
        $protectedValues += [string]$case.prompt
    }
}

$serviceLogs = foreach ($service in $CandidateServices) {
    $container = Get-ServiceContainer $service
    Invoke-DockerText @('logs', $container) "read $service logs"
}
$logs = $serviceLogs -join "`n"
$logLeakCount = @($protectedValues | Where-Object { $logs.Contains($_) }).Count
if ($logLeakCount -ne 0) {
    throw "privacy scan detected $logLeakCount protected value(s) in logs"
}

$artifactScan = @'
import base64
import hashlib
import json
import sys
from pathlib import Path

root = Path('/reports') / sys.argv[1] / sys.argv[2]
digests = sys.argv[3:]
payloads = []
for digest in digests:
    path = root / digest[:2] / digest
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise SystemExit(2)
    payloads.append(payload)
needles = [base64.b64decode(item) for item in json.load(sys.stdin)]
if any(needle in payload for needle in needles for payload in payloads):
    raise SystemExit(3)
print('PASS')
'@
$encodedProtectedValues = @(
    $protectedValues | ForEach-Object {
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_))
    }
) | ConvertTo-Json -Compress
$artifactArguments = @(
    'run', '--rm', '-i', '--read-only',
    '--volume', 'sana-shadow-eval-campaign-reports:/reports:ro',
    $expectedImage,
    'python', '-c', $artifactScan,
    [string]$audit.tenant_id,
    $campaignLiteral,
    [string]$audit.json_sha256,
    [string]$audit.markdown_sha256
)
$artifactOutput = $encodedProtectedValues | & docker @artifactArguments 2>&1
if ($LASTEXITCODE -ne 0 -or ($artifactOutput -join "`n").Trim() -ne 'PASS') {
    throw 'Campaign report integrity/privacy scan failed'
}

Write-Output "campaign=$campaignLiteral"
Write-Output "execution_class=$expectedExecutionClass"
Write-Output "result_run_uniqueness=PASS ($($audit.result_count)/$($audit.distinct_run_count))"
Write-Output 'ledger_and_idle_state=PASS'
Write-Output 'image_and_worker_health=PASS'
Write-Output 'force_rls=PASS'
Write-Output 'report_integrity=PASS'
Write-Output 'privacy_scan=PASS'
if ($OfflineFixture) {
    Write-Output 'offline_zero_provider_usage=PASS'
}
