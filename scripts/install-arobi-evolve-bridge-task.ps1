$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = "Arobi ASI-Evolve Guarded Bridge"
$ScriptPath = Join-Path $Root "scripts\start-arobi-evolve-bridge.ps1"

if (-not (Test-Path $ScriptPath)) {
  throw "Missing bridge script: $ScriptPath"
}

$Action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`" -Once"
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$RepeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger @($Trigger, $RepeatTrigger) `
  -Principal $Principal `
  -Settings $Settings `
  -Description "Guarded ASI-Evolve autopilot for Arobi route health, analytics, safe local recovery, founder notifications, and approval-gated task dispatch." `
  -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
