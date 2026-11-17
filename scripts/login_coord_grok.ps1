param(
    [string]$GrokHome = $null,
    [switch]$OAuth
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$GrokHome = if ($GrokHome) { $GrokHome } else { Join-Path $Root '.mewcode\grok-home' }
if (-not [IO.Path]::IsPathRooted($GrokHome)) { $GrokHome = Join-Path $Root $GrokHome }
$GrokHome = [IO.Path]::GetFullPath($GrokHome)
New-Item -ItemType Directory -Force -Path $GrokHome | Out-Null
$Grok = Join-Path $GrokHome 'bin\grok.exe'
$env:GROK_HOME = $GrokHome

if (-not (Test-Path -LiteralPath $Grok)) {
    throw "Grok executable not found: $Grok"
}

$loginArguments = if ($OAuth) { @('login', '--oauth') } else { @('login', '--device-auth') }
& $Grok @loginArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Grok 'models'
exit $LASTEXITCODE
