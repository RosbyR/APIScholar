# Registers a Windows Task Scheduler job that runs paperbot on weekdays.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1            # 06:00 local
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -At 07:30
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -Remove
#
# arXiv RSS refreshes around midnight US Eastern and is empty on weekends, so a weekday
# morning run in Mountain time catches the new announcements. The time is the machine's
# local time zone. If the machine is asleep at that time, the task runs when it wakes
# (StartWhenAvailable). Overlapping runs are prevented by paperbot's own lock file.

param(
    [string]$At = "06:00",
    [string]$TaskName = "paperbot-daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed scheduled task '$TaskName'."
    exit 0
}

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\run-daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "paperbot: harvest, triage, debate, digest" -Force | Out-Null
Write-Output "Registered '$TaskName': weekdays at $At (local time). Logs go to $root\logs."
Write-Output "Test it now with: Start-ScheduledTask -TaskName $TaskName"
