# Windows Task Scheduler registration for YouTube AutoPoster
# Run as admin: powershell -ExecutionPolicy Bypass -File register_task.ps1

$ProjectDir    = "D:\開発\自主\youtube_autoposter"
# Task Scheduler cannot handle Japanese characters in paths — use the ASCII junction instead.
$TaskDir       = "D:\ytauto"
if (-not (Test-Path $TaskDir)) {
    New-Item -ItemType Junction -Path $TaskDir -Target $ProjectDir | Out-Null
    Write-Host "Created junction: $TaskDir -> $ProjectDir"
}
$WrapperScript = "$TaskDir\run_once.ps1"
$Python        = (Get-Command python -ErrorAction SilentlyContinue).Source
$PowerShell    = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

# --- Post tasks (morning + evening) ---
$postTasks = @(
    @{ Name = "YouTubeAutoPoster_Morning"; Hour = 7  }
    @{ Name = "YouTubeAutoPoster_Evening"; Hour = 19 }
)

foreach ($t in $postTasks) {
    if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        Write-Host "Removed existing task: $($t.Name)"
    }

    $action  = New-ScheduledTaskAction `
        -Execute $PowerShell `
        -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$WrapperScript`"" `
        -WorkingDirectory $TaskDir

    $trigger  = New-ScheduledTaskTrigger -Daily -At "$($t.Hour):00"
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10) `
        -MultipleInstances IgnoreNew -StartWhenAvailable `
        -WakeToRun -DisallowStartIfOnBatteries:$false
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Highest

    Register-ScheduledTask `
        -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "YouTube AutoPoster post at $($t.Hour):00 JST" `
        -Force | Out-Null

    Write-Host "Registered: $($t.Name) -> daily $($t.Hour):00"
}

# --- Daily stats update (23:00) ---
$statsTaskName = "YouTubeAutoPoster_StatsUpdate"
if (Get-ScheduledTask -TaskName $statsTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $statsTaskName -Confirm:$false
}
$statsAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$TaskDir\main.py`" --update-stats" `
    -WorkingDirectory $TaskDir
$statsTrigger  = New-ScheduledTaskTrigger -Daily -At "23:00"
$statsSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask `
    -TaskName $statsTaskName -Action $statsAction -Trigger $statsTrigger `
    -Settings $statsSettings -Principal $principal `
    -Description "Fetch YouTube stats for all logged videos" `
    -Force | Out-Null
Write-Host "Registered: $statsTaskName -> daily 23:00"

# --- Weekly self-improvement analysis (Sunday 00:00) ---
$analyzeTaskName = "YouTubeAutoPoster_Analyze"
if (Get-ScheduledTask -TaskName $analyzeTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $analyzeTaskName -Confirm:$false
}
$analyzeAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$TaskDir\main.py`" --analyze" `
    -WorkingDirectory $TaskDir
$analyzeTrigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "00:00"
$analyzeSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask `
    -TaskName $analyzeTaskName -Action $analyzeAction -Trigger $analyzeTrigger `
    -Settings $analyzeSettings -Principal $principal `
    -Description "Self-improvement: analyze performance and update strategy" `
    -Force | Out-Null
Write-Host "Registered: $analyzeTaskName -> every Sunday 00:00"

Write-Host ""
Write-Host "All tasks registered. Next run times:"
Get-ScheduledTask -TaskName "YouTubeAutoPoster_*" |
    Get-ScheduledTaskInfo |
    Select-Object @{N="Task";E={$_.TaskName}}, NextRunTime, LastTaskResult |
    Format-Table -AutoSize
