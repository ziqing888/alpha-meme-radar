Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Required = @(
    'README.md',
    'docs\architecture\system-boundaries.md',
    'packages\contracts\monitor.snapshot.v1.schema.json',
    'packages\contracts\intelligence.overlay.v1.schema.json',
    'packages\contracts\execution.signal.v1.schema.json',
    'vendor-notes\README.md'
)
foreach ($Relative in $Required) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $Relative))) {
        throw "required repository file is missing: $Relative"
    }
}

$IgnoreText = Get-Content -LiteralPath (Join-Path $Root '.gitignore') -Raw
foreach ($Pattern in @('.env*', 'outputs/', 'runtime/', '**/node_modules/', '**/dist/', 'work/external/', '**/.git/')) {
    if ($IgnoreText -notmatch [regex]::Escape($Pattern)) {
        throw "missing ignore rule: $Pattern"
    }
}

$Scratch = Join-Path ([IO.Path]::GetTempPath()) ('alpha-repo-hygiene-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Scratch | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $Root '.gitignore') -Destination (Join-Path $Scratch '.gitignore')
    foreach ($Relative in @('.env', '.env.local', 'outputs\state.json', 'runtime\orders.json', 'web\node_modules\x.js', 'web\dist\x.js', 'work\external\sample\.git\config')) {
        $Path = Join-Path $Scratch $Relative
        New-Item -ItemType Directory -Path (Split-Path $Path) -Force | Out-Null
        Set-Content -LiteralPath $Path -Value 'sensitive' -NoNewline
    }
    git -C $Scratch init --quiet
    foreach ($Relative in @('.env', '.env.local', 'outputs\state.json', 'runtime\orders.json', 'web\node_modules\x.js', 'web\dist\x.js', 'work\external\sample\.git\config')) {
        git -C $Scratch check-ignore --quiet -- $Relative
        if ($LASTEXITCODE -ne 0) { throw "ignore rule is ineffective: $Relative" }
    }
} finally {
    Remove-Item -LiteralPath $Scratch -Recurse -Force
}

$ScanRoots = @(
    (Join-Path $Root 'docs'),
    (Join-Path $Root 'ops'),
    (Join-Path $Root 'packages'),
    (Join-Path $Root 'web\src'),
    (Join-Path $Root 'work\alpha-pool-radar')
)
$SecretPatterns = @(
    'sk-or-v1-[A-Za-z0-9]{20,}',
    'sk-[A-Za-z0-9]{20,}',
    'github_pat_[A-Za-z0-9_]{20,}',
    'ghp_[A-Za-z0-9]{20,}',
    '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'
)
$Extensions = @('.py', '.pyi', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.ps1', '.md', '.json', '.toml', '.yml', '.yaml')
foreach ($Base in $ScanRoots) {
    if (-not (Test-Path -LiteralPath $Base)) { continue }
    Get-ChildItem -LiteralPath $Base -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Length -lt 2MB -and
            $_.Extension -in $Extensions -and
            $_.FullName -notmatch '[\\/](node_modules|dist|outputs|runtime|external|\.git|\.pytest_cache|__pycache__)[\\/]'
        } |
        ForEach-Object {
            $Text = Get-Content -LiteralPath $_.FullName -Raw
            foreach ($Pattern in $SecretPatterns) {
                foreach ($Match in [regex]::Matches($Text, $Pattern)) {
                    $Value = $Match.Value
                    $Suffix = $Value -replace '^(sk-or-v1-|sk-|github_pat_|ghp_)', ''
                    $ObviousFixture = (
                        $Suffix -match '^(.)\1+$' -or
                        $Suffix -match '(?i)(test|dummy|example|fake)' -or
                        ($Value -match 'PRIVATE KEY' -and $Text -match 'MAMCAQA=')
                    )
                    if (-not $ObviousFixture) {
                        throw "secret-like value found in candidate file: $($_.FullName)"
                    }
                }
            }
        }
}

$PublicConfigFiles = @(
    'work\alpha-pool-radar\server\provision_remote_runtime.ps1',
    'work\alpha-pool-radar\server\alpha_remote_sync.py',
    'work\alpha-pool-radar\server\sync_remote_execution.ps1',
    'work\alpha-pool-radar\OKX_DEX_SDK_LIVE.md',
    'work\alpha-pool-radar\alpha_stage_model.py',
    'work\alpha-pool-radar\register_alpha_monitoring_tasks.ps1'
)
$PrivateIdentifierPatterns = @(
    'C:\\Users\\[^\\\s"'']+',
    '(?i)(RemoteHost\s*=|--remote-host"?,\s*default=)[^\r\n]*\b\d{1,3}(?:\.\d{1,3}){3}\b',
    '(?i)(ExpectedWallet\s*=|-ExpectedWallet\s+")0x[0-9a-f]{40}',
    '(?i)(RemoteUser\s*=|--remote-user"?,\s*default=)[^\r\n]*["'']root["'']',
    '(?i)\.ssh[\\/][a-z0-9_.-]+'
)
foreach ($Relative in $PublicConfigFiles) {
    $Path = Join-Path $Root $Relative
    $Text = Get-Content -LiteralPath $Path -Raw
    foreach ($Pattern in $PrivateIdentifierPatterns) {
        if ($Text -match $Pattern) {
            throw "private operational identifier found in public file: $Relative"
        }
    }
}

foreach ($SchemaName in @('monitor.snapshot.v1.schema.json', 'intelligence.overlay.v1.schema.json', 'execution.signal.v1.schema.json')) {
    $Schema = Get-Content -LiteralPath (Join-Path $Root "packages\contracts\$SchemaName") -Raw | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$Schema.'$id')) { throw "schema id missing: $SchemaName" }
    if ($Schema.type -ne 'object') { throw "schema root must be object: $SchemaName" }
}

Write-Output 'repository hygiene passed'
