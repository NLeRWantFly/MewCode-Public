param(
  [string]$EnvironmentRoot = "D:\enviorments\mewcode-tauri",
  [switch]$SkipSdkInstall
)

$ErrorActionPreference = "Stop"
$sdkRoot = Join-Path $EnvironmentRoot "android-sdk"
$cmdlineRoot = Join-Path $EnvironmentRoot "android-cmdline-tools"
$cmdlineZip = Join-Path $EnvironmentRoot "android-commandlinetools.zip"
$toolsUrl = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
$sdkManager = Join-Path $cmdlineRoot "latest\bin\sdkmanager.bat"

New-Item -ItemType Directory -Force $EnvironmentRoot | Out-Null
if (-not $SkipSdkInstall -and -not (Test-Path -LiteralPath $sdkManager)) {
  Write-Host "Downloading Android command-line tools..."
  if (-not (Test-Path -LiteralPath $cmdlineZip)) {
    Invoke-WebRequest -Uri $toolsUrl -OutFile $cmdlineZip
  }
  New-Item -ItemType Directory -Force $cmdlineRoot | Out-Null
  Expand-Archive -LiteralPath $cmdlineZip -DestinationPath $cmdlineRoot -Force
  $unzipped = Join-Path $cmdlineRoot "cmdline-tools"
  if (Test-Path -LiteralPath $unzipped) {
    New-Item -ItemType Directory -Force (Join-Path $cmdlineRoot "latest") | Out-Null
    Get-ChildItem -LiteralPath $unzipped | Move-Item -Destination (Join-Path $cmdlineRoot "latest") -Force
    Remove-Item -LiteralPath $unzipped -Recurse -Force
  }
}
if (-not (Test-Path -LiteralPath $sdkManager)) { throw "sdkmanager.bat not found at $sdkManager" }
$env:ANDROID_HOME = $sdkRoot
$env:ANDROID_SDK_ROOT = $sdkRoot
New-Item -ItemType Directory -Force $sdkRoot | Out-Null
$acceptCommand = 'for /L %i in (1,1,100) do @echo y'
& cmd.exe /c $acceptCommand | & $sdkManager --sdk_root=$sdkRoot --licenses | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Android SDK license acceptance failed ($LASTEXITCODE)" }
& $sdkManager --sdk_root=$sdkRoot "platform-tools" "platforms;android-35" "build-tools;35.0.0" "ndk;27.0.12077973"
if ($LASTEXITCODE -ne 0) { throw "Android SDK package installation failed ($LASTEXITCODE)" }
$env:NDK_HOME = Join-Path $sdkRoot "ndk\27.0.12077973"
foreach ($path in @(
  (Join-Path $sdkRoot "platform-tools\adb.exe"),
  (Join-Path $sdkRoot "platforms\android-35\android.jar"),
  (Join-Path $sdkRoot "build-tools\35.0.0\aapt.exe"),
  (Join-Path $env:NDK_HOME "source.properties")
)) {
  if (-not (Test-Path -LiteralPath $path)) { throw "Android SDK verification failed: $path is missing" }
}
Write-Host "ANDROID_HOME=$env:ANDROID_HOME"
Write-Host "NDK_HOME=$env:NDK_HOME"
Write-Host "Android environment ready."
