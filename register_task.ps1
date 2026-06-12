# Windows タスクスケジューラーに YouTube AutoPoster を登録するスクリプト
# 管理者として実行してください: powershell -ExecutionPolicy Bypass -File register_task.ps1

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = (Get-Command python).Source
$TaskName = "YouTubeAutoPoster"

# 既存タスクを削除
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "既存タスクを削除しました。"
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "main.py" `
    -WorkingDirectory $ProjectDir

# ログオン時に起動
$Trigger = New-ScheduledTaskTrigger -AtLogOn

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "YouTube AutoPoster — 朝7時・夜7時に自動投稿"

Write-Host ""
Write-Host "タスク '$TaskName' を登録しました。"
Write-Host "次回ログオン時から自動起動します。"
Write-Host "今すぐ起動: Start-ScheduledTask -TaskName '$TaskName'"
