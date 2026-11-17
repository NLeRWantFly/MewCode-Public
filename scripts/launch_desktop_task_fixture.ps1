[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-CSharpCompiler {
    $command = Get-Command csc.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    $windowsRoot = $env:WINDIR
    if ([string]::IsNullOrWhiteSpace($windowsRoot)) {
        throw 'The Windows .NET Framework C# compiler was not found.'
    }
    $candidates = @(
        (Join-Path $windowsRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $windowsRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw 'The Windows .NET Framework C# compiler was not found.'
}

function Resolve-RepositoryPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

$repoRoot = Resolve-RepositoryPath (Join-Path $PSScriptRoot '..')
$tempRoot = Resolve-RepositoryPath (Join-Path $repoRoot '.tmp')
$launcherSource = Join-Path $repoRoot 'tests\DesktopFixtureLauncher.cs'
$fixtureSource = Join-Path $repoRoot 'tests\ui_task_lifecycle_fixture.py'
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$tauriExecutable = Join-Path $repoRoot 'src-tauri\target\debug\mewcode-tauri.exe'

foreach ($requiredFile in @($launcherSource, $fixtureSource, $pythonPath, $tauriExecutable)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw 'A required fixture source, project interpreter, or debug Tauri executable is missing.'
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'frontend\server.py') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -PathType Leaf)) {
    throw 'The selected directory does not look like the MewCode project root.'
}

if (Test-Path -LiteralPath $tempRoot) {
    $tempItem = Get-Item -LiteralPath $tempRoot -Force
    if (-not $tempItem.PSIsContainer -or
        (($tempItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw 'The project .tmp path must be a normal directory, not a file or reparse point.'
    }
} else {
    New-Item -ItemType Directory -Path $tempRoot | Out-Null
}

$fixtureBase = Join-Path $tempRoot 'desktop-task-fixture'
if (Test-Path -LiteralPath $fixtureBase) {
    $fixtureBaseItem = Get-Item -LiteralPath $fixtureBase -Force
    if (-not $fixtureBaseItem.PSIsContainer -or
        (($fixtureBaseItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw 'The desktop fixture directory must be a normal directory under project .tmp.'
    }
} else {
    New-Item -ItemType Directory -Path $fixtureBase | Out-Null
}

$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$dataRoot = Resolve-RepositoryPath (Join-Path $fixtureBase $runId)
$tempPrefix = $tempRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $dataRoot.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The generated fixture data directory escaped the project .tmp directory.'
}
if (Test-Path -LiteralPath $dataRoot) {
    throw 'The generated fixture data directory already exists; refusing to reuse it.'
}
New-Item -ItemType Directory -Path $dataRoot | Out-Null

$launcherExecutable = Join-Path $dataRoot 'desktop-fixture-launcher.exe'
$manifestPath = Join-Path $dataRoot 'fixture-manifest.json'
$pidReceiptPath = Join-Path $dataRoot 'launched-pid.txt'
$tauriStdoutLog = Join-Path $dataRoot 'tauri.stdout.log'
$tauriStderrLog = Join-Path $dataRoot 'tauri.stderr.log'
$backendStdoutLog = Join-Path $dataRoot 'backend.out.log'
$backendStderrLog = Join-Path $dataRoot 'backend.err.log'

$compiler = Get-CSharpCompiler
$compilerArguments = @(
    '/nologo',
    '/target:exe',
    "/out:$launcherExecutable",
    $launcherSource
)
& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $launcherExecutable -PathType Leaf)) {
    throw 'Compiling the isolated desktop fixture launcher failed.'
}

$receipt = [ordered]@{
    status = if ($DryRun) { 'dry_run_validated' } else { 'starting' }
    tauri_started = $false
    pid = $null
    data_directory = $dataRoot
    launcher_executable = $launcherExecutable
    tauri_executable = $tauriExecutable
    tauri_stdout_log = $tauriStdoutLog
    tauri_stderr_log = $tauriStderrLog
    backend_stdout_log = $backendStdoutLog
    backend_stderr_log = $backendStderrLog
    fixture_manifest = $manifestPath
    pid_receipt = $pidReceiptPath
}

if ($DryRun) {
    $receipt | ConvertTo-Json -Depth 4
    exit 0
}

$environmentValues = [ordered]@{
    MEWCODE_REPO_ROOT = $repoRoot
    MEWCODE_FIXTURE_ROOT = $repoRoot
    MEWCODE_BACKEND = $launcherExecutable
    MEWCODE_USER_DATA_DIR = $dataRoot
}
$previousEnvironment = @{}
foreach ($name in $environmentValues.Keys) {
    $previousEnvironment[$name] = [System.Environment]::GetEnvironmentVariable(
        $name, [System.EnvironmentVariableTarget]::Process)
}

$desktopProcess = $null
try {
    foreach ($name in $environmentValues.Keys) {
        [System.Environment]::SetEnvironmentVariable(
            $name, $environmentValues[$name], [System.EnvironmentVariableTarget]::Process)
    }
    $desktopProcess = Start-Process -FilePath $tauriExecutable `
        -WorkingDirectory $repoRoot `
        -WindowStyle Normal `
        -RedirectStandardOutput $tauriStdoutLog `
        -RedirectStandardError $tauriStderrLog `
        -PassThru
} finally {
    foreach ($name in $environmentValues.Keys) {
        [System.Environment]::SetEnvironmentVariable(
            $name, $previousEnvironment[$name], [System.EnvironmentVariableTarget]::Process)
    }
}

if ($null -eq $desktopProcess) {
    throw 'The visible debug Tauri process did not start.'
}
[System.IO.File]::WriteAllText($pidReceiptPath, [string]$desktopProcess.Id,
    [System.Text.Encoding]::ASCII)
$receipt.status = 'started_waiting_for_manifest'
$receipt.tauri_started = $true
$receipt.pid = $desktopProcess.Id
$receipt | ConvertTo-Json -Depth 4

$deadline = [DateTime]::UtcNow.AddSeconds(150)
while (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    if ($desktopProcess.HasExited) {
        $receipt.status = 'tauri_exited_before_manifest'
        $receipt | ConvertTo-Json -Depth 4
        exit 1
    }
    if ([DateTime]::UtcNow -ge $deadline) {
        $receipt.status = 'manifest_startup_timeout'
        $receipt | ConvertTo-Json -Depth 4
        exit 1
    }
    Start-Sleep -Seconds 1
}

$receipt.status = 'fixture_ready'
$receipt | ConvertTo-Json -Depth 4
