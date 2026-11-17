param(
  [string]$EnvironmentRoot = "D:\enviorments\mewcode-tauri",
  [string]$BackendUrl = "",
  [ValidateSet("aarch64", "armv7", "i686", "x86_64", "universal")]
  [string]$Target = "aarch64",
  [switch]$DebugBuild,
  [string]$GradleUserHome = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\src-tauri")).Path
$androidProject = Join-Path $projectRoot "gen\android"
$mobileFrontend = Join-Path $projectRoot "..\frontend\static"
$mobileDist = Join-Path $projectRoot "mobile-dist"
if (-not (Test-Path -LiteralPath $androidProject)) {
  throw "Android project directory $androidProject doesn't exist. Run tauri android init and try again."
}
if (-not (Test-Path -LiteralPath (Join-Path $mobileFrontend "mobile.html"))) {
  throw "Mobile frontend entrypoint is missing at $mobileFrontend\mobile.html"
}
# Stage only the static frontend at the same path used by the Android window URL.
if (Test-Path -LiteralPath $mobileDist) {
  Remove-Item -LiteralPath $mobileDist -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $mobileDist | Out-Null
Copy-Item -LiteralPath $mobileFrontend -Destination (Join-Path $mobileDist "static") -Recurse -Force
# WebViewAssetLoader maps https://<domain>/static/... onto android_asset/static/...
$assetRoot = Join-Path $androidProject "app\src\main\assets"
foreach ($stale in @("_up_", "frontend")) {
  $stalePath = Join-Path $assetRoot $stale
  if (Test-Path -LiteralPath $stalePath) {
    Remove-Item -LiteralPath $stalePath -Recurse -Force
  }
}
New-Item -ItemType Directory -Force -Path $assetRoot | Out-Null
$staticDest = Join-Path $assetRoot "static"
if (Test-Path -LiteralPath $staticDest) {
  Remove-Item -LiteralPath $staticDest -Recurse -Force
}
Copy-Item -LiteralPath $mobileFrontend -Destination $staticDest -Recurse -Force
$sdkRoot = Join-Path $EnvironmentRoot "android-sdk"
$ndkRoot = Join-Path $sdkRoot "ndk\27.0.12077973"
if (-not (Test-Path -LiteralPath (Join-Path $sdkRoot "platform-tools"))) { throw "Android SDK is missing. Run scripts/setup-android.ps1 first." }
$env:ANDROID_HOME = $sdkRoot
$env:ANDROID_SDK_ROOT = $sdkRoot
$env:NDK_HOME = $ndkRoot
$llvmBin = Join-Path $ndkRoot "toolchains\llvm\prebuilt\windows-x86_64\bin"
if (-not (Test-Path -LiteralPath (Join-Path $llvmBin "aarch64-linux-android24-clang.cmd"))) { throw "Android NDK clang is missing at $llvmBin" }
$env:CC_aarch64_linux_android = Join-Path $llvmBin "aarch64-linux-android24-clang.cmd"
$env:CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER = $env:CC_aarch64_linux_android
$env:AR_aarch64_linux_android = Join-Path $llvmBin "llvm-ar.exe"
$env:CC_armv7_linux_androideabi = Join-Path $llvmBin "armv7a-linux-androideabi24-clang.cmd"
$env:CARGO_TARGET_ARMV7_LINUX_ANDROIDEABI_LINKER = $env:CC_armv7_linux_androideabi
$env:AR_armv7_linux_androideabi = Join-Path $llvmBin "llvm-ar.exe"
$env:CC_i686_linux_android = Join-Path $llvmBin "i686-linux-android24-clang.cmd"
$env:CARGO_TARGET_I686_LINUX_ANDROID_LINKER = $env:CC_i686_linux_android
$env:AR_i686_linux_android = Join-Path $llvmBin "llvm-ar.exe"
$env:CC_x86_64_linux_android = Join-Path $llvmBin "x86_64-linux-android24-clang.cmd"
$env:CARGO_TARGET_X86_64_LINUX_ANDROID_LINKER = $env:CC_x86_64_linux_android
$env:AR_x86_64_linux_android = Join-Path $llvmBin "llvm-ar.exe"
if ($BackendUrl) {
  $backendUri = [Uri]$BackendUrl
  if ($backendUri.Scheme -ne "https" -or -not $backendUri.Host) { throw "BackendUrl must be an absolute HTTPS URL, typically a Tailscale HTTPS name." }
  if ($backendUri.Host -eq "example.ts.net" -or $backendUri.Host.EndsWith(".example.ts.net")) { throw "BackendUrl cannot use the example placeholder domain." }
  $env:MEWCODE_MOBILE_BACKEND_URL = $backendUri.GetLeftPart([System.UriPartial]::Authority)
} else {
  Remove-Item Env:MEWCODE_MOBILE_BACKEND_URL -ErrorAction SilentlyContinue
}
$env:RUSTUP_HOME = Join-Path $EnvironmentRoot "rustup"
$env:CARGO_HOME = Join-Path $EnvironmentRoot "cargo"
$env:CARGO_TARGET_DIR = Join-Path $EnvironmentRoot "target"
$env:JAVA_HOME = Join-Path $EnvironmentRoot "jdk-17"
$env:ANDROID_NDK_HOME = $ndkRoot
$tmpDir = Join-Path $EnvironmentRoot "tmp"
foreach ($dir in @($env:CARGO_HOME, $env:CARGO_TARGET_DIR, $tmpDir, $env:JAVA_HOME)) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
$env:TMP = $tmpDir
$env:TEMP = $tmpDir
if (-not $env:HTTP_PROXY) { $env:HTTP_PROXY = "http://127.0.0.1:7897" }
if (-not $env:HTTPS_PROXY) { $env:HTTPS_PROXY = $env:HTTP_PROXY }
if (-not $env:http_proxy) { $env:http_proxy = $env:HTTP_PROXY }
if (-not $env:https_proxy) { $env:https_proxy = $env:HTTPS_PROXY }
$proxyUri = [Uri]$env:HTTP_PROXY
$env:JAVA_TOOL_OPTIONS = "-Dhttp.proxyHost=$($proxyUri.Host) -Dhttp.proxyPort=$($proxyUri.Port) -Dhttps.proxyHost=$($proxyUri.Host) -Dhttps.proxyPort=$($proxyUri.Port) -Dhttp.nonProxyHosts=localhost|127.0.0.1"
if (-not (Test-Path -LiteralPath (Join-Path $env:JAVA_HOME "bin\java.exe"))) {
  throw "JAVA_HOME is missing java.exe at $($env:JAVA_HOME)\bin\java.exe"
}
if (-not (Test-Path -LiteralPath (Join-Path $env:RUSTUP_HOME "toolchains\stable-x86_64-pc-windows-msvc\bin\rustc.exe"))) {
  throw "Rustup toolchain is missing under $env:RUSTUP_HOME. Copy ~/.rustup there so NDK lld never sees a non-ASCII user path."
}
$cargo = Join-Path $env:CARGO_HOME "bin\cargo.exe"
$userCargo = Join-Path $env:USERPROFILE ".cargo"
if (-not (Test-Path -LiteralPath $cargo)) {
  if (-not (Test-Path -LiteralPath (Join-Path $userCargo "bin\cargo.exe"))) {
    throw "Cargo not found at $cargo or $userCargo\bin\cargo.exe"
  }
  Write-Host "Seeding ASCII CARGO_HOME from $userCargo"
  & robocopy $userCargo $env:CARGO_HOME /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "Failed to copy Cargo home to $env:CARGO_HOME ($LASTEXITCODE)" }
}
if (-not (Test-Path -LiteralPath $cargo)) { throw "Cargo not found at $cargo" }
$localProps = Join-Path $androidProject "local.properties"
$sdkPosix = ($sdkRoot -replace '\\', '/')
Set-Content -LiteralPath $localProps -Encoding ASCII -Value "sdk.dir=$sdkPosix"
$projectGradleProps = Join-Path $androidProject "gradle.properties"
if ((Test-Path -LiteralPath $projectGradleProps) -and -not (Select-String -LiteralPath $projectGradleProps -Pattern "android.overridePathCheck" -Quiet)) {
  Add-Content -LiteralPath $projectGradleProps -Encoding ASCII -Value "android.overridePathCheck=true"
}
$gradleZip = Join-Path $EnvironmentRoot "gradle-8.14.3-bin.zip"
$wrapperProps = Join-Path $androidProject "gradle\wrapper\gradle-wrapper.properties"
if ((Test-Path -LiteralPath $gradleZip) -and (Test-Path -LiteralPath $wrapperProps) -and ((Get-Item -LiteralPath $gradleZip).Length -gt 10000000)) {
  $fileUrl = ([Uri]$gradleZip).AbsoluteUri
  $escapedUrl = $fileUrl.Replace(":", "\:")
  $wrapper = Get-Content -LiteralPath $wrapperProps
  $wrapper = $wrapper | ForEach-Object {
    if ($_ -match '^distributionUrl=') { "distributionUrl=$escapedUrl" } else { $_ }
  }
  Set-Content -LiteralPath $wrapperProps -Encoding ASCII -Value $wrapper
  Write-Host "GRADLE_DIST=$fileUrl"
}
$gradleHome = if ($GradleUserHome) { $GradleUserHome } else { Join-Path $EnvironmentRoot "gradle-home" }
New-Item -ItemType Directory -Force -Path $gradleHome | Out-Null
$env:GRADLE_USER_HOME = (Resolve-Path $gradleHome).Path
$gradleInit = Join-Path $projectRoot "gen\android\mewcode-build.init.gradle"
if (-not (Test-Path -LiteralPath $gradleInit)) { throw "Gradle init script is missing at $gradleInit" }
$gradleInitD = Join-Path $env:GRADLE_USER_HOME "init.d"
New-Item -ItemType Directory -Force -Path $gradleInitD | Out-Null
Copy-Item -LiteralPath $gradleInit -Destination (Join-Path $gradleInitD "mewcode-build.init.gradle") -Force
$gradleUserProps = Join-Path $env:GRADLE_USER_HOME "gradle.properties"
@(
  "systemProp.http.proxyHost=$($proxyUri.Host)",
  "systemProp.http.proxyPort=$($proxyUri.Port)",
  "systemProp.https.proxyHost=$($proxyUri.Host)",
  "systemProp.https.proxyPort=$($proxyUri.Port)",
  "systemProp.http.nonProxyHosts=localhost|127.0.0.1"
) | Set-Content -LiteralPath $gradleUserProps -Encoding ASCII
$toolchain = Join-Path $env:RUSTUP_HOME "toolchains\stable-x86_64-pc-windows-msvc"
$env:RUSTC = Join-Path $toolchain "bin\rustc.exe"
if (-not (Test-Path -LiteralPath $env:RUSTC)) { throw "rustc missing at $env:RUSTC" }
$env:CARGO_PROFILE_DEV_DEBUG = "0"
$env:Path = "$(Join-Path $env:JAVA_HOME 'bin');$(Join-Path $toolchain 'bin');$(Join-Path $env:CARGO_HOME 'bin');$llvmBin;$env:Path"
Write-Host "RUSTC_SYSROOT=$(& $env:RUSTC --print sysroot)"
Write-Host "ANDROID_LIBDIR=$(& $env:RUSTC --print target-libdir --target aarch64-linux-android)"
$args = @("tauri", "android", "build")
if ($DebugBuild) { $args += "--debug" }
if ($Target -ne "universal") {
  $args += @("--target", $Target, "--split-per-abi")
}
Push-Location $projectRoot
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}
try { & $cargo @args; $buildExit = $LASTEXITCODE } finally {
  $ErrorActionPreference = $prevEap
  Pop-Location
}
if ($buildExit -ne 0) { throw "Tauri Android build failed ($buildExit)" }
$apkRoot = Join-Path $androidProject "app\build\outputs\apk"
$apk = Get-ChildItem -LiteralPath $apkRoot -Recurse -Filter "*.apk" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if (-not $apk) { throw "Android build finished but no APK was found under $apkRoot" }
$distDir = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$distName = if ($DebugBuild) { "MewCode-android-arm64-debug.apk" } else { "MewCode-android-arm64.apk" }
$distPath = Join-Path $distDir $distName
Copy-Item -LiteralPath $apk.FullName -Destination $distPath -Force
Write-Host "APK_READY=$distPath"
