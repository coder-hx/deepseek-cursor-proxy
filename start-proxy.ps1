# DeepSeek Cursor Proxy - Startup Script
# Starts deepseek-cursor-proxy, enforces a single running instance, cleans up
# stray ngrok agents first, and auto-restarts the proxy if it crashes.

# Don't hard-stop on non-terminating errors; we manage failures via the loop.
$ErrorActionPreference = "Continue"

# Use absolute paths to avoid PATH issues on startup
$PROXY_EXE = "C:\Users\ThinkPad\AppData\Local\Programs\Python\Python312\Scripts\deepseek-cursor-proxy.exe"
$NGROK_EXE = "C:\Users\ThinkPad\AppData\Local\Microsoft\WinGet\Links\ngrok.exe"
$PROJECT_DIR = "C:\Users\ThinkPad\deepseek-cursor-proxy"
$LOG_DIR = Join-Path $PROJECT_DIR "logs"

# Ensure ngrok is in PATH so the proxy can find it
$ngrokDir = Split-Path $NGROK_EXE
$env:Path = "$ngrokDir;" + [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Keep a rolling log file so crashes are diagnosable after the fact
if (-not (Test-Path $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}
$LOG_FILE = Join-Path $LOG_DIR ("proxy-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Stamp {
    param([string]$Message, [string]$Color = "Gray")
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line -ForegroundColor $Color
    Add-Content -Path $LOG_FILE -Value $line -ErrorAction SilentlyContinue
}

function Stop-StrayNgrok {
    # ngrok's free plan allows only one simultaneous session, so a leftover
    # agent makes a fresh launch fail with ERR_NGROK_108. Clear it first.
    $stray = Get-Process ngrok -ErrorAction SilentlyContinue
    if ($stray) {
        Write-Stamp ("stopping {0} stray ngrok process(es)" -f $stray.Count) "Yellow"
        $stray | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}

# --- Single-instance guard -------------------------------------------------
# Double-clicking the shortcut while a proxy is already running used to spawn a
# second loop. Both copies fought over the reserved ngrok endpoint (ERR_NGROK_334)
# and kept killing each other's ngrok, restarting forever. A global mutex makes
# the second launch detect the first and bow out instead.
$mutex = New-Object System.Threading.Mutex($false, "Global\DeepSeekCursorProxy")
$ownsMutex = $false
try {
    try {
        $ownsMutex = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        # Previous instance crashed without releasing; we now own it.
        $ownsMutex = $true
    }

    if (-not $ownsMutex) {
        Write-Host "========================================" -ForegroundColor Yellow
        Write-Host "  DeepSeek Cursor Proxy is ALREADY running." -ForegroundColor Yellow
        Write-Host "  Use the window that's already open." -ForegroundColor Yellow
        Write-Host "  (To force a fresh start: close every proxy window," -ForegroundColor DarkYellow
        Write-Host "   then run: Get-Process ngrok | Stop-Process -Force)" -ForegroundColor DarkYellow
        Write-Host "========================================" -ForegroundColor Yellow
        Start-Sleep -Seconds 6
        exit 0
    }

    # Wait a few seconds for network to be ready after boot
    Start-Sleep -Seconds 5

    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  DeepSeek Cursor Proxy Starting...     " -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Stamp ("log file: {0}" -f $LOG_FILE) "DarkGray"

    Set-Location $PROJECT_DIR

    # Validate the proxy executable exists before entering the restart loop
    if (-not (Test-Path $PROXY_EXE)) {
        Write-Stamp ("proxy executable not found: {0}" -f $PROXY_EXE) "Red"
        Write-Stamp "install with: pip install -e . (from the project dir)" "Red"
        Read-Host "Press Enter to exit"
        exit 1
    }

    # Auto-restart loop with backoff. Rapid repeated crashes back off up to 60s;
    # a process that stayed up a while resets the backoff.
    $backoff = 3
    $maxBackoff = 60
    $restartCount = 0

    while ($true) {
        Stop-StrayNgrok

        Write-Stamp "starting proxy..." "Green"
        $startedAt = Get-Date

        # Run in foreground; show output live AND capture it for diagnosis.
        & $PROXY_EXE 2>&1 |
            Tee-Object -FilePath $LOG_FILE -Append |
            Tee-Object -Variable runOutput
        $exitCode = $LASTEXITCODE

        $uptime = (Get-Date) - $startedAt
        Write-Stamp ("proxy exited (code={0}, uptime={1:n0}s)" -f $exitCode, $uptime.TotalSeconds) "Yellow"

        # Clean exit (Ctrl+C / code 0) => stop the loop and don't restart.
        if ($exitCode -eq 0 -or $null -eq $exitCode) {
            Write-Stamp "clean shutdown; not restarting" "Cyan"
            break
        }

        # A reserved-endpoint conflict won't fix itself by retrying: something
        # else already owns the ngrok endpoint. Stop instead of ping-ponging.
        $joined = ($runOutput | Out-String)
        if ($joined -match "ERR_NGROK_334" -or $joined -match "already online") {
            Write-Stamp "reserved ngrok endpoint is already online - another proxy is already running it." "Red"
            Write-Stamp "Not restarting. Close other proxy windows and kill stray ngrok.exe, then start once." "Red"
            break
        }

        # If it ran fine for a while, treat this as a fresh failure and reset backoff
        if ($uptime.TotalSeconds -ge 60) {
            $backoff = 3
            $restartCount = 0
        } else {
            $backoff = [Math]::Min($backoff * 2, $maxBackoff)
        }

        $restartCount++
        Write-Stamp ("restarting in {0}s (restart #{1})..." -f $backoff, $restartCount) "Yellow"
        Start-Sleep -Seconds $backoff
    }

    Write-Host ""
    Write-Stamp "proxy stopped." "Cyan"
}
finally {
    if ($ownsMutex) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    if ($mutex) { $mutex.Dispose() }
}
