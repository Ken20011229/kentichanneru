# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File "D:\ytauto\fix_tasks_as_admin.ps1"

$dir = "D:\ytauto"

schtasks /delete /TN "YouTubeAutoPoster_Morning" /F 2>&1 | Out-Null
schtasks /delete /TN "YouTubeAutoPoster_Evening" /F 2>&1 | Out-Null

$r1 = schtasks /create /TN "YouTubeAutoPoster_Morning" /XML "$dir\task_morning.xml" /F 2>&1
Write-Host "Morning: $r1"

$r2 = schtasks /create /TN "YouTubeAutoPoster_Evening" /XML "$dir\task_evening.xml" /F 2>&1
Write-Host "Evening: $r2"

Write-Host ""
schtasks /query /TN "YouTubeAutoPoster_Morning" /FO LIST 2>&1 | Select-String "Task To Run|Next Run Time|Status|Wake To Run"
schtasks /query /TN "YouTubeAutoPoster_Evening" /FO LIST 2>&1 | Select-String "Task To Run|Next Run Time|Status|Wake To Run"
