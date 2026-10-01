# Ежедневный прогон сканера: scan -> watch -> report (раз в неделю), Telegram, лог в logs/.
# Запускается Планировщиком Windows (задача AccumulationScannerDaily, см. scripts/register_task.ps1).
# Ручной запуск: powershell -ExecutionPolicy Bypass -File scripts\daily_run.ps1 [-NoNotify]
param([switch]$NoNotify)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"
if (-not (Test-Path $python)) { $python = "py" }   # голый python — заглушка Windows Store
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
# PowerShell 5.1 декодирует вывод python кодировкой консоли (cp866) — кириллица в логе бьётся.
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("daily_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmm"))
$notify = if ($NoNotify) { @() } else { @("--notify") }

function Run-Step([string]$name, [string[]]$cmdArgs) {
    "=== $name $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Append -Encoding utf8
    & $python -u run.py @cmdArgs *>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8
    "=== $name exit $LASTEXITCODE ===`n" | Out-File $log -Append -Encoding utf8
    return $LASTEXITCODE
}

# watch идёт и при сбое scan: открытые позиции надо проверять независимо от воронки.
$scanCode = Run-Step "scan" (@("scan") + $notify)
$watchCode = Run-Step "watch" (@("watch") + $notify)
# Недельная сводка: report зовётся каждый день, сам решает, пора ли (--if-due, первый
# прогон недели); после watch — берёт свежие снапшоты позиций.
$reportCode = Run-Step "report" (@("report", "--if-due") + $notify)

# Логи старше 30 дней не нужны.
Get-ChildItem $logDir -Filter "daily_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

if ($scanCode -ne 0 -or $watchCode -ne 0 -or $reportCode -ne 0) { exit 1 }
exit 0
