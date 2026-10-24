param(
    [int]$InterfaceIndex = 13,
    [int]$Port = 8081,
    [switch]$ConfigureFirewall
)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python environment not found. Create .venv and install requirements.txt first.'
}
$interface = Get-NetIPConfiguration -InterfaceIndex $InterfaceIndex
$lanAddress = @($interface.IPv4Address | Where-Object { $_.IPAddress -notlike '169.254.*' })[0].IPAddress
if (-not $lanAddress) { throw 'No IPv4 address on selected interface. Connect to Wi-Fi first.' }

if ($ConfigureFirewall) {
    $ruleName = "InnoSport-LAN-Demo-$Port"
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    $settings = @{
        Direction = 'Inbound'; Action = 'Allow'; Protocol = 'TCP'; LocalPort = $Port
        LocalAddress = $lanAddress; RemoteAddress = 'LocalSubnet'
        InterfaceAlias = $interface.InterfaceAlias; Program = $pythonPath
        Profile = 'Any'; Enabled = 'True'
    }
    if ($rule) { Set-NetFirewallRule -Name $ruleName @settings | Out-Null }
    else { New-NetFirewallRule -Name $ruleName -DisplayName "Inno Sport demo (Wi-Fi port $Port)" @settings | Out-Null }
}

$listener = Get-NetTCPConnection -LocalAddress $lanAddress -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $logPath = Join-Path $PSScriptRoot 'data'
    New-Item -ItemType Directory -Path $logPath -Force | Out-Null
    Start-Process -FilePath $pythonPath -ArgumentList @('miniapp.py', '--host', $lanAddress, '--port', "$Port") `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logPath 'lan-demo-output.log') `
        -RedirectStandardError (Join-Path $logPath 'lan-demo-error.log') | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $response = Invoke-WebRequest -Uri "http://${lanAddress}:$Port/?demo=1" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw 'Server did not start. See data/lan-demo-error.log.' }
}
Write-Host "Demo: http://${lanAddress}:$Port/?demo=1"
Write-Host 'Only demo data is served. Keep the laptop awake and connected to Wi-Fi.'
