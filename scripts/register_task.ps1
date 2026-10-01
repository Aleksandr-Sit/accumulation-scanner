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
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
if ($NoNotify) { $arg += " -NoNotify" }

# conhost --headless: окна нет вообще. На Windows 11 консоль открывается в Windows Terminal,
# он игнорирует -WindowStyle Hidden — окно висит, закрыли его — прогон убит (0xC000013A).
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
    -Argument "--headless powershell.exe $arg" -WorkingDirectory $root
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
