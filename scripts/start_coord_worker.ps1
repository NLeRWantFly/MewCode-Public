param(
    [switch]$Foreground,
    [switch]$TeamV2,
    [string]$GrokHome = $null,
    [double]$PollSeconds = 2,
    [double]$HeartbeatSeconds = 15
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$GrokHome = if ($GrokHome) { $GrokHome } else { Join-Path $Root '.mewcode\grok-home' }
if (-not [IO.Path]::IsPathRooted($GrokHome)) { $GrokHome = Join-Path $Root $GrokHome }
$GrokHome = [IO.Path]::GetFullPath($GrokHome)
New-Item -ItemType Directory -Force -Path $GrokHome | Out-Null
$Grok = Join-Path $GrokHome 'bin\grok.exe'
$State = Join-Path $Root '.mewcode\coordination'
$Log = Join-Path $State 'worker.log'
$ErrorLog = Join-Path $State 'worker.error.log'
$env:UV_CACHE_DIR = Join-Path $Root '.uv-cache'
$env:GROK_HOME = $GrokHome

if (-not (Test-Path -LiteralPath $Grok)) {
    throw "Grok executable not found: $Grok"
}
New-Item -ItemType Directory -Force -Path $State | Out-Null
New-Item -ItemType Directory -Force -Path $env:UV_CACHE_DIR | Out-Null

$arguments = @(
    'run', '--project', $Root, 'mewcode-coord-worker',
    '--root', $Root,
    '--state-dir', $State,
    '--grok-executable', $Grok,
    '--grok-home', $GrokHome,
    '--poll-seconds', $PollSeconds,
    '--heartbeat-seconds', $HeartbeatSeconds
)
if ($TeamV2) { $arguments += '--team-v2' }

if ($Foreground) {
    & uv @arguments
    exit $LASTEXITCODE
}

$process = Start-Process -WindowStyle Hidden -FilePath 'uv' -ArgumentList $arguments `
    -WorkingDirectory $Root -RedirectStandardOutput $Log -RedirectStandardError $ErrorLog `
    -PassThru
Write-Output ("coord-worker started: PID={0}; log={1}" -f $process.Id, $Log)
