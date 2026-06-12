# YouTube AutoPoster スケジューラー起動スクリプト
# 実行: powershell -ExecutionPolicy Bypass -File start_scheduler.ps1

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VoicevoxEngine = "D:\開発\自主\VOICEVOX\vv-engine\run.exe"

Set-Location $ProjectDir

# VOICEVOX Engine を起動（未起動の場合のみ）
$vvRunning = $null
try {
    $vvRunning = Invoke-RestMethod -Uri "http://localhost:50021/version" -TimeoutSec 2 -ErrorAction Stop
} catch {}

if ($null -eq $vvRunning) {
    if (Test-Path $VoicevoxEngine) {
        Write-Host "VOICEVOX Engine を起動しています..."
        Start-Process -FilePath $VoicevoxEngine -WindowStyle Minimized
        # 起動完了を待機（最大30秒）
        $waited = 0
        do {
            Start-Sleep -Seconds 2
            $waited += 2
            try {
                $vvRunning = Invoke-RestMethod -Uri "http://localhost:50021/version" -TimeoutSec 2 -ErrorAction Stop
            } catch {}
        } while ($null -eq $vvRunning -and $waited -lt 30)

        if ($null -eq $vvRunning) {
            Write-Host "警告: VOICEVOX Engine の起動確認ができませんでした。続行します..."
        } else {
            Write-Host "VOICEVOX Engine 起動完了 (version: $vvRunning)"
        }
    } else {
        Write-Host "警告: VOICEVOX Engine が見つかりません: $VoicevoxEngine"
    }
} else {
    Write-Host "VOICEVOX Engine は既に起動しています (version: $vvRunning)"
}

Write-Host ""
Write-Host "YouTube AutoPoster Scheduler を起動します..."
Write-Host "停止するには Ctrl+C を押してください。"
Write-Host ""

python main.py
