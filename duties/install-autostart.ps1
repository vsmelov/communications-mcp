<#
.SYNOPSIS
  Автозапуск дашборда дежурств при входе в Windows — задача планировщика «Claude Duties Dashboard».
  НИКТО НЕ ЗАПУСКАЕТ ЭТОТ СКРИПТ АВТОМАТИЧЕСКИ: регистрировать автозапуск или нет — решение владельца.

.EXAMPLE
  powershell -NoProfile -File C:\Users\v\PycharmProjects\claude-workspace\communications-mcp\duties\install-autostart.ps1
  Проверить:  Get-ScheduledTask -TaskName 'Claude Duties Dashboard'
  Запустить сейчас:  Start-ScheduledTask -TaskName 'Claude Duties Dashboard'
  Удалить:    Unregister-ScheduledTask -TaskName 'Claude Duties Dashboard' -Confirm:$false

.NOTES
  Задача запускает pythonw.exe -m duties dashboard (без окна, только 127.0.0.1:18210) от текущего пользователя,
  без повышения прав. Повторный запуск дашборда безопасен — он singleton (второй экземпляр видит первый и выходит).
#>
param([int]$Port = 18210)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot                 # communications-mcp
$py = 'C:\Python312\pythonw.exe'
if (-not (Test-Path $py)) { throw "нет $py — поправь путь к pythonw.exe в скрипте" }

$action = New-ScheduledTaskAction -Execute $py -Argument "-m duties dashboard --port $Port" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'Claude Duties Dashboard' -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Force `
  -Description 'Дашборд дежурств по Telegram-чатам (communications-mcp/duties), http://127.0.0.1:18210/' | Out-Null
Write-Output "задача 'Claude Duties Dashboard' зарегистрирована: при входе — $py -m duties dashboard (в $root)"
