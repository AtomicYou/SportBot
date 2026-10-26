param([int]$Port = 8082)
$ErrorActionPreference = 'Stop'
$rootPath = $PSScriptRoot
$pythonPath = Join-Path $rootPath '.venv\Scripts\python.exe'
$tunnelPath = Join-Path $rootPath '.tools\cloudflared.exe'
$dataPath = Join-Path $rootPath 'data'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing .venv Python.' }
if (-not (Test-Path -LiteralPath $tunnelPath)) { throw 'Missing .tools/cloudflared.exe.' }
if (-not (Test-Path -LiteralPath (Join-Path $rootPath '.env'))) { throw 'Missing .env with BOT_TOKEN.' }
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$supervisorLog = Join-Path $dataPath 'startup.log'
function Write-Status([string]$Text) {
    if ((Test-Path -LiteralPath $supervisorLog) -and (Get-Item -LiteralPath $supervisorLog).Length -gt 1MB) {
        Copy-Item -LiteralPath $supervisorLog -Destination "$supervisorLog.previous" -Force
        Clear-Content -LiteralPath $supervisorLog
    }
    Add-Content -LiteralPath $supervisorLog -Value "$(Get-Date -Format s) $Text"
}

# A second scheduled/manual launch must not create another polling bot.
$mutex = [Threading.Mutex]::new($false, 'Local\InnoSportAppSupervisor')
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
$botProcess = $null
$tunnelProcess = $null
try {
    Write-Status 'Supervisor started.'
    while ($true) {
        try {
            $portOwner = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
            if ($portOwner) { throw 'Backend port is already in use.' }
            $tunnelLog = Join-Path $dataPath 'tunnel.log'
            $tunnelProcess = Start-Process -FilePath $tunnelPath `
                -ArgumentList @('tunnel', '--no-autoupdate', '--protocol', 'http2', '--url', "http://127.0.0.1:$Port") `
                -WorkingDirectory $rootPath -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $dataPath 'tunnel-output.log') -RedirectStandardError $tunnelLog
            $publicUrl = $null
            for ($attempt = 0; $attempt -lt 90; $attempt++) {
                Start-Sleep -Seconds 1
                $tunnelProcess.Refresh()
                if ($tunnelProcess.HasExited) { throw 'Tunnel process exited before obtaining an address.' }
                if (Test-Path -LiteralPath $tunnelLog) {
                    $tunnelText = Get-Content -LiteralPath $tunnelLog -Raw
                    $urlMatch = [regex]::Match($tunnelText, 'https://(?!api\.trycloudflare\.com\b)[a-z0-9-]+\.trycloudflare\.com')
                    if ($urlMatch.Success) { $publicUrl = $urlMatch.Value; break }
                }
            }
            if (-not $publicUrl) { throw 'Tunnel address not available yet.' }
            # Child processes inherit these values. The .env containing the bot
            # token is never rewritten just to change a temporary tunnel URL.
            $env:MINIAPP_URL = $publicUrl
            $env:WEB_ENABLED = '1'
            $env:WEB_HOST = '127.0.0.1'
            $env:WEB_PORT = "$Port"
            $env:PYTHONUNBUFFERED = '1'
            $botProcess = Start-Process -FilePath $pythonPath -ArgumentList 'bot.py' `
                -WorkingDirectory $rootPath -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $dataPath 'bot-output.log') `
                -RedirectStandardError (Join-Path $dataPath 'bot-error.log')
            Set-Content -LiteralPath (Join-Path $dataPath 'miniapp-url.txt') -Value $publicUrl
            Write-Status "Bot started (PID $($botProcess.Id)); Mini App: $publicUrl"
            while ($true) {
                Start-Sleep -Seconds 5
                $botProcess.Refresh(); $tunnelProcess.Refresh()
                if ($botProcess.HasExited) { throw 'Bot process exited. Restarting.' }
                if ($tunnelProcess.HasExited) { throw 'Tunnel process exited. Restarting.' }
            }
        } catch {
            # Log only our own short status strings, never credentials or request URLs.
            Write-Status 'Startup or connection failed. Retrying in 20 seconds. See bot-error.log and tunnel.log.'
        } finally {
            foreach ($ownedProcess in @($botProcess, $tunnelProcess)) {
                if ($null -ne $ownedProcess) {
                    $ownedProcess.Refresh()
                    if (-not $ownedProcess.HasExited) { Stop-Process -Id $ownedProcess.Id -ErrorAction SilentlyContinue }
                }
            }
            $botProcess = $null; $tunnelProcess = $null
        }
        Start-Sleep -Seconds 20
    }
} finally {
    foreach ($ownedProcess in @($botProcess, $tunnelProcess)) {
        if ($null -ne $ownedProcess) {
            $ownedProcess.Refresh()
            if (-not $ownedProcess.HasExited) { Stop-Process -Id $ownedProcess.Id -ErrorAction SilentlyContinue }
        }
    }
    $mutex.ReleaseMutex(); $mutex.Dispose()
}
