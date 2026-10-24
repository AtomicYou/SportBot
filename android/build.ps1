$ErrorActionPreference = 'Stop'
$project = Split-Path $PSScriptRoot -Parent
$sdk = Join-Path $project '.tools\android'
$tools = Join-Path $sdk 'android-15'
$platform = Join-Path $sdk 'android-35\android.jar'
$env:JAVA_HOME = 'C:\Program Files\Java\jdk-24'
$java = Join-Path $env:JAVA_HOME 'bin\java.exe'
if (!(Test-Path $java)) { throw 'Set JAVA_HOME in android/build.ps1 to the installed JDK.' }
$build = Join-Path $project '.tools\android-build'
$release = Join-Path $project 'releases'
$keys = Join-Path $project 'data\android-signing'
New-Item -ItemType Directory -Force $build, $release, $keys, "$build\classes", "$build\dex" | Out-Null
function Check { if ($LASTEXITCODE -ne 0) { throw "Android build failed: exit $LASTEXITCODE" } }
& "$tools\aapt.exe" package -f -M "$PSScriptRoot\AndroidManifest.xml" -S "$PSScriptRoot/res" -I $platform -F "$build\unsigned.apk"
Check
$sources = @(Get-ChildItem "$PSScriptRoot\src" -Filter '*.java' -Recurse | ForEach-Object FullName)
& "$env:JAVA_HOME\bin\javac.exe" -encoding UTF-8 -source 8 -target 8 -bootclasspath "$platform;$tools\core-lambda-stubs.jar" -d "$build\classes" @sources
Check
$classes = @(Get-ChildItem "$build\classes" -Filter '*.class' -Recurse | ForEach-Object FullName)
& $java -cp "$tools\lib\d8.jar" com.android.tools.r8.D8 --lib $platform --min-api 26 --output "$build\dex" @classes
Check
Push-Location "$build\dex"
try { & "$tools\aapt.exe" add "$build\unsigned.apk" classes.dex; Check } finally { Pop-Location }
& "$tools\zipalign.exe" -f 4 "$build\unsigned.apk" "$build\aligned.apk"
Check
$passwordFile = Join-Path $keys 'password.txt'
if (!(Test-Path $passwordFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($passwordFile, [Convert]::ToBase64String($bytes))
}
$env:INNOSPORT_SIGNING_PASSWORD = [IO.File]::ReadAllText($passwordFile)
try {
    if (!(Test-Path "$keys\release.jks")) {
        & "$env:JAVA_HOME\bin\keytool.exe" -genkeypair -keystore "$keys\release.jks" -storetype PKCS12 -storepass:env INNOSPORT_SIGNING_PASSWORD -keypass:env INNOSPORT_SIGNING_PASSWORD -alias innosport -keyalg RSA -keysize 3072 -validity 10000 -dname 'CN=Inno Sport Admin'
        Check
    }
    & $java -jar "$tools\lib\apksigner.jar" sign --ks "$keys\release.jks" --ks-key-alias innosport --ks-pass env:INNOSPORT_SIGNING_PASSWORD --key-pass env:INNOSPORT_SIGNING_PASSWORD --out "$release\InnoSport-Admin.apk" "$build\aligned.apk"
    Check
    & $java -jar "$tools\lib\apksigner.jar" verify --verbose "$release\InnoSport-Admin.apk"
    Check
    & "$tools\zipalign.exe" -c 4 "$release\InnoSport-Admin.apk"
    Check
} finally { Remove-Item Env:INNOSPORT_SIGNING_PASSWORD -ErrorAction SilentlyContinue }
Get-FileHash "$release\InnoSport-Admin.apk" -Algorithm SHA256

