$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'start-app.ps1'
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $powershellPath `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$launcher`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'InnoSport Mini App' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'Inno Sport bot and HTTPS tunnel. Runs after user sign-in.' -Force | Out-Null
Start-ScheduledTask -TaskName 'InnoSport Mini App'
Get-ScheduledTask -TaskName 'InnoSport Mini App' | Select-Object TaskName, State
