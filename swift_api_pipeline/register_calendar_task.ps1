## Register SwiftPipeline-Calendar in Task Scheduler
## Run this script as Administrator

$taskName = "SwiftPipeline-Calendar"
$batPath  = "C:\Users\admin\Desktop\Projects\ai-projects\local-pipeline\swift_api_pipeline\scheduled_calendar_pipeline.bat"
$workDir  = "C:\Users\admin\Desktop\Projects\ai-projects\local-pipeline\swift_api_pipeline"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Daily -At "12:30AM"
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 0 `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Run as 'admin' user, Interactive/Background (runs whether logged on or not)
$principal = New-ScheduledTaskPrincipal -UserId "admin" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Calendar Leave Pipeline - Daily incremental sync from Google Calendar to Supabase at 12:30 AM."

Write-Host "Task '$taskName' registered successfully."
Write-Host ""
Get-ScheduledTask -TaskName $taskName | Format-List TaskName, State, Description
