# Wrapper: start VOICEVOX if needed, then run pipeline once
# Called by Task Scheduler

$ProjectDir  = "D:\ytauto"
$VoicevoxExe = "D:\vv-engine\run.exe"
$VoicevoxUrl = "http://localhost:50021/version"
$LogFile     = "$ProjectDir\logs\scheduler_wrapper.log"

function Write-Log ($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Tee-Object -FilePath $LogFile -Append
}

Write-Log "=== run_once.ps1 started ==="

function Test-Voicevox {
    try {
        $r = Invoke-WebRequest -Uri $VoicevoxUrl -TimeoutSec 3 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-Voicevox)) {
    if (-not (Test-Path $VoicevoxExe)) {
        Write-Log "ERROR: VOICEVOX not found: $VoicevoxExe"
        exit 1
    }
    Write-Log "Starting VOICEVOX..."
    Start-Process -FilePath $VoicevoxExe -WindowStyle Minimized

    $waited = 0
    while ($waited -lt 90) {
        Start-Sleep -Seconds 3
        $waited += 3
        if (Test-Voicevox) {
            Write-Log "VOICEVOX ready (${waited}s)"
            break
        }
    }

    if (-not (Test-Voicevox)) {
        Write-Log "ERROR: VOICEVOX did not start within 90 seconds"
        exit 1
    }
} else {
    Write-Log "VOICEVOX already running"
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $candidates = @(
        "C:\Users\orang\AppData\Local\Programs\Python\Python313\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $python = $c; break }
    }
}
if (-not $python) {
    Write-Log "ERROR: python not found"
    exit 1
}
Write-Log "Python: $python"

Set-Location $ProjectDir
Write-Log "Running: main.py --once"
& $python "$ProjectDir\main.py" --once
$code = $LASTEXITCODE
Write-Log "Pipeline exited with code: $code"
Write-Log "=== run_once.ps1 finished ==="
exit $code
