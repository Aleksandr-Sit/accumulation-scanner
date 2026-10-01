# Регистрирует ежедневную задачу Планировщика Windows для scripts/daily_run.ps1.
# Повторный запуск перезаписывает задачу. Удалить:
#   Unregister-ScheduledTask -TaskName AccumulationScannerDaily -Confirm:$false
param(
    [string]$At = "10:00",     # локальное время; дневная свеча закрывается в 00:00 UTC
    [switch]$NoNotify
)

$taskName = "AccumulationScannerDaily"
$script = Join-Path $PSScriptRoot "daily_run.ps1"
$root = Split-Path -Parent $PSScriptRoot
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
if ($NoNotify) { $arg += " -NoNotify" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: ноутбук спал в 10:00 — прогон стартует после пробуждения.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# Только пока пользователь вошёл в систему — без хранения пароля.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Accumulation scanner: scan + watch раз в день (crypto-strategy-research/scanner)" | Out-Null
Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime, LastRunTime, LastTaskResult
