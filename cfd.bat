@echo off
:: ===================================================
:: CFD - Current Folder Downloader
::
:: Callable from any folder. Downloads into the folder
:: cmd is currently opened in.
::
:: Usage:
::   cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf
::   cfd https://huggingface.co/.../resolve/main/file.gguf?download=true
::   cfd status          - show active/current/queued downloads here
::   cfd continue / c    - resume after a crash/interruption using saved queue data
::   cfd clear           - wipe the pending queue (and in-progress marker) here
::   cfd uninstall       - remove cfd from PATH and delete its files
::
:: Retry behavior per worker session:
::   * First pickup of an item gets 1 initial attempt + 3 retries (4 attempts).
::   * On failure it is requeued at the tail with an [E:n] prefix, where n
::     counts completed big cycles. Items with [E:n] get 3 attempts per cycle.
::   * Max 3 big cycles per session = 10 total attempts (1 + 3 + 3 + 3).
::   * When the 3rd big cycle ends, the item moves to a session-deferred
::     area (the lock file) and is restored to the queue when the worker
::     exits, so a later worker session tries it fresh.
::   * On startup, all [E:n] prefixes are stripped from the queue, giving
::     every item a full retry budget each session.
::   * A successful download discards any errored state.
::
:: Env overrides (mainly for tests):
::   CFD_SMALL_DELAY         seconds between retries (default 10)
::   CFD_WORKER_EXIT_DELAY   seconds to linger after the queue empties (default 5)
:: ===================================================
setlocal enabledelayedexpansion

:: Literal double-quote character, used later to wrap a
:: pasted URL in quotes when building a curl command.
set QM=^"

if /i "%~1"=="uninstall"  ( call :uninstall     & exit /b 0 )
if /i "%~1"=="status"     ( call :status        & exit /b 0 )
if /i "%~1"=="queue"      ( call :status        & exit /b 0 )
if /i "%~1"=="clear"      ( call :clearqueue    & exit /b 0 )
if /i "%~1"=="continue"   ( call :forcecontinue & exit /b 0 )
if /i "%~1"=="c"          ( call :forcecontinue & exit /b 0 )
if /i "%~1"=="__worker__" ( call :worker_loop "%~2" & exit /b 0 )

set "TARGET_DIR=%CD%"
set "USER_INPUT=%*"

if "%USER_INPUT%"=="" (
    echo ===================================================
    echo CFD - Current Folder Downloader
    echo ===================================================
    echo Usage:
    echo   cfd hf download ^<hf-url^>
    echo   cfd ^<direct-https-url^>
    echo   cfd status          - show active/current/queued downloads here
    echo   cfd continue / c    - resume after a crash/interruption
    echo   cfd clear           - wipe the pending queue here
    echo   cfd uninstall       - remove cfd
    echo.
    echo Examples:
    echo   cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf
    echo   cfd https://huggingface.co/prism-ml/Bonsai-27B-gguf/resolve/main/Bonsai-27B-Q1_0.gguf?download=true
    endlocal
    exit /b 1
)

:: Detect which of the two supported formats we were given:
::   IS_HF  = 1 -> a "hf download ..." command, used as-is
::   IS_URL = 1 -> a plain direct-download URL/link, converted to a curl command
set "IS_HF=0"
echo !USER_INPUT! | findstr /i /c:"hf download" >nul
if not errorlevel 1 set "IS_HF=1"

set "IS_URL=0"
if /i "!USER_INPUT:~0,7!"=="http://"  set "IS_URL=1"
if /i "!USER_INPUT:~0,8!"=="https://" set "IS_URL=1"

if "!IS_HF!"=="0" if "!IS_URL!"=="0" (
    echo [Error] Invalid command format.
    echo Paste either an "hf download ..." command, or a direct download
    echo URL ^(e.g. the "download" button link from a huggingface.co file page^).
    endlocal
    exit /b 1
)

if "!IS_HF!"=="0" if "!IS_URL!"=="1" (
    :: Plain download-button URL, e.g. ...file.gguf?download=true
    set "URL=!USER_INPUT!"
    :: Truncate at the first "?" - everything after is the query string.
    for /f "delims=?" %%A in ("!URL!") do set "URL=%%A"
    :: -f makes curl exit non-zero on HTTP 4xx/5xx so the worker's
    :: retry loop can see the failure. Without it curl returns 0 and
    :: writes the error body to disk as if it were content.
    set "USER_INPUT=curl -fL -C - -O !QM!!URL!!QM!"
    echo [CFD] Parsed as direct URL - will run: !USER_INPUT!
)

set "LOCK_FILE=%TARGET_DIR%\.cfd.lock"
set "QUEUE_FILE=%TARGET_DIR%\.cfd.queue"

:: Append this command as a new line in the folder's queue file
>>"%QUEUE_FILE%" echo !USER_INPUT!

:: Work out its position in the queue for feedback
set /a POS=0
for /f "usebackq delims=" %%L in ("%QUEUE_FILE%") do set /a POS+=1

if exist "%LOCK_FILE%" (
    echo [CFD] A download is already running in this folder.
    echo [CFD] Added to queue at position !POS!: !USER_INPUT!
    echo [CFD] It will start automatically once earlier downloads finish.
) else (
    echo [CFD] Queued ^(position !POS!^): !USER_INPUT!
    echo [CFD] Opening download worker window...
    start "CFD Worker" cmd /c ""%~f0" __worker__ "%TARGET_DIR%""
)
endlocal
exit /b 0


:: ===================================================
:: Worker: runs in its own window, processes the queue
:: for one folder until empty, then closes itself.
::
:: The lock file doubles as a session-deferred list:
::   Line 1 : "cfd worker running" (liveness marker)
::   Line 2+: [E:n] items that exhausted their big cycle
::            budget this session; restored to the queue
::            when the worker exits.
:: ===================================================
:worker_loop
setlocal enabledelayedexpansion
set "WDIR=%~1"
cd /d "%WDIR%"
set "LOCK_FILE=%WDIR%\.cfd.lock"
set "QUEUE_FILE=%WDIR%\.cfd.queue"
set "CURRENT_FILE=%WDIR%\.cfd.current"

set "SMALL_RETRIES=3"
set "BIG_RETRIES=3"
if not defined CFD_SMALL_DELAY set "CFD_SMALL_DELAY=10"
if not defined CFD_WORKER_EXIT_DELAY set "CFD_WORKER_EXIT_DELAY=5"
set "SMALL_DELAY=%CFD_SMALL_DELAY%"
set "EXIT_DELAY=%CFD_WORKER_EXIT_DELAY%"

echo cfd worker running> "%LOCK_FILE%"
echo ===================================================
echo CFD Worker
echo Folder: %WDIR%
echo ===================================================

:: Strip all [E:n] prefixes so every item gets a fresh
:: set of big-cycle retries for this worker session.
call :strip_error_flags

:worker_next
if not exist "%QUEUE_FILE%" goto worker_done

set "NEXT_LINE="
for /f "usebackq delims=" %%L in ("%QUEUE_FILE%") do (
    if not defined NEXT_LINE set "NEXT_LINE=%%L"
)
if not defined NEXT_LINE goto worker_done

:: Parse [E:n] prefix (n is always a single digit 1..3)
set "E_FLAG=0"
set "BIG_COUNT=0"
set "NEXT_CMD=!NEXT_LINE!"
if "!NEXT_LINE:~0,3!"=="[E:" (
    set "E_FLAG=1"
    set "BIG_COUNT=!NEXT_LINE:~3,1!"
    set "NEXT_CMD=!NEXT_LINE:~5!"
)

:: Remove the line we just picked up, keep the rest of the queue
more +1 "%QUEUE_FILE%" > "%QUEUE_FILE%.tmp" 2>nul
del /f /q "%QUEUE_FILE%" >nul 2>&1
set "TMPSIZE=0"
for %%A in ("%QUEUE_FILE%.tmp") do set "TMPSIZE=%%~zA"
if !TMPSIZE! gtr 0 (
    move /y "%QUEUE_FILE%.tmp" "%QUEUE_FILE%" >nul
) else (
    del /f /q "%QUEUE_FILE%.tmp" >nul 2>&1
)

:: Defensive: an [E:n] item already at the cycle limit shouldn't be
:: retried in this session - defer it straight away.
set "DEFER_NOW=0"
if !E_FLAG! equ 1 if !BIG_COUNT! geq %BIG_RETRIES% set "DEFER_NOW=1"
if !DEFER_NOW! equ 1 (
    >>"%LOCK_FILE%" echo [E:!BIG_COUNT!]!NEXT_CMD!
    echo [CFD] Session retry budget already exhausted. Deferring: !NEXT_CMD!
    goto worker_next
)

:: Mark this one as "currently downloading" in case of a crash
> "%CURRENT_FILE%" echo !NEXT_CMD!

echo ---------------------------------------------------
echo [CFD] Downloading: !NEXT_CMD!
echo ---------------------------------------------------

:: ---- Initial attempt (only on the first pickup of an item) ----
set "INITIAL_OK=0"
if !E_FLAG! equ 0 (
    call :run_command "!NEXT_CMD!"
    set "EXITCODE=!errorlevel!"
    if !EXITCODE! equ 0 set "INITIAL_OK=1"
)
if !INITIAL_OK! equ 1 goto small_success

:: ---- Retry loop: up to SMALL_RETRIES retries with delay ----
set /a RETRY=0
:retry_loop
if !RETRY! geq %SMALL_RETRIES% goto small_fail
set /a RETRY+=1
echo [CFD] Retry !RETRY! of %SMALL_RETRIES%. Waiting %SMALL_DELAY%s...
if %SMALL_DELAY% gtr 0 timeout /t %SMALL_DELAY% >nul
call :run_command "!NEXT_CMD!"
set "EXITCODE=!errorlevel!"
if !EXITCODE! equ 0 goto small_success
goto retry_loop

:small_success
if exist "%WDIR%\.cache" (
    echo Cleaning up .cache folder...
    rd /s /q "%WDIR%\.cache"
)
del /f /q "%CURRENT_FILE%" >nul 2>&1
echo [CFD] Finished: !NEXT_CMD!
goto worker_next

:small_fail
:: A big cycle just completed for this item
if !E_FLAG! equ 0 (
    set /a NEW_CYCLE=1
) else (
    set /a NEW_CYCLE=!BIG_COUNT!+1
)

if !NEW_CYCLE! geq %BIG_RETRIES% (
    >>"%LOCK_FILE%" echo [E:!NEW_CYCLE!]!NEXT_CMD!
    echo [CFD] Retry budget exhausted for this session. Deferring: !NEXT_CMD!
) else (
    >>"%QUEUE_FILE%" echo [E:!NEW_CYCLE!]!NEXT_CMD!
    echo [CFD] Requeued as [E:!NEW_CYCLE!]: !NEXT_CMD!
)
del /f /q "%CURRENT_FILE%" >nul 2>&1
goto worker_next

:worker_done
:: Restore session-deferred items to the queue for the next session
call :flush_deferred
del /f /q "%LOCK_FILE%" >nul 2>&1
echo ===================================================
echo [CFD] Queue empty - all downloads for this folder are complete.
echo ===================================================
if %EXIT_DELAY% gtr 0 timeout /t %EXIT_DELAY% >nul
endlocal
exit /b 0


:: ===================================================
:: :run_command  - executes a download command and
::                 returns its exit code.
:: ===================================================
:run_command
setlocal
set "CMD=%~1"
echo !CMD! | findstr /i /c:"hf download" >nul
if not errorlevel 1 (
    call !CMD! --local-dir "%WDIR%"
) else (
    call !CMD!
)
endlocal & exit /b %errorlevel%


:: ===================================================
:: :strip_error_flags  - removes all [E:n] prefixes
::                       from the queue file.
:: ===================================================
:strip_error_flags
if not exist "%QUEUE_FILE%" exit /b 0
set "TMPFILE=%QUEUE_FILE%.strip"
type nul > "%TMPFILE%"
for /f "usebackq delims=" %%L in ("%QUEUE_FILE%") do (
    set "LINE=%%L"
    set "STRIPPED=!LINE!"
    if "!LINE:~0,3!"=="[E:" set "STRIPPED=!LINE:~5!"
    >>"%TMPFILE%" echo !STRIPPED!
)
move /y "%TMPFILE%" "%QUEUE_FILE%" >nul
exit /b 0


:: ===================================================
:: :flush_deferred  - appends every line after the first
::                    from the lock file to the queue
::                    file. The first line is the lock
::                    marker and is skipped.
:: ===================================================
:flush_deferred
if not exist "%LOCK_FILE%" exit /b 0
set /a SKIPFIRST=1
for /f "usebackq delims=" %%L in ("%LOCK_FILE%") do (
    if !SKIPFIRST! equ 1 (
        set /a SKIPFIRST=0
    ) else (
        >>"%QUEUE_FILE%" echo %%L
    )
)
exit /b 0


:: ===================================================
:: Helper commands
:: ===================================================
:status
setlocal enabledelayedexpansion
set "WDIR=%CD%"
set "LOCK_FILE=%WDIR%\.cfd.lock"
set "QUEUE_FILE=%WDIR%\.cfd.queue"
set "CURRENT_FILE=%WDIR%\.cfd.current"

if exist "%LOCK_FILE%" (
    echo [CFD] A download worker is active in this folder.
) else (
    echo [CFD] No active worker in this folder.
)

if exist "%CURRENT_FILE%" (
    for /f "usebackq delims=" %%L in ("%CURRENT_FILE%") do echo [CFD] In progress: %%L
)

if exist "%QUEUE_FILE%" (
    echo [CFD] Pending items in queue:
    set /a I=0
    for /f "usebackq delims=" %%L in ("%QUEUE_FILE%") do (
        set /a I+=1
        echo !I!. %%L
    )
) else (
    echo [CFD] Queue is empty.
)
endlocal
exit /b 0


:clearqueue
setlocal enabledelayedexpansion
set "QUEUE_FILE=%CD%\.cfd.queue"
set "CURRENT_FILE=%CD%\.cfd.current"
set "FOUND=0"
if exist "%QUEUE_FILE%" (
    del /f /q "%QUEUE_FILE%" >nul 2>&1
    set "FOUND=1"
)
if exist "%CURRENT_FILE%" (
    del /f /q "%CURRENT_FILE%" >nul 2>&1
    set "FOUND=1"
)
if "!FOUND!"=="1" (
    echo [CFD] Pending queue and in-progress marker cleared for this folder.
    echo [CFD] Note: any partially downloaded .cache folder is left untouched.
) else (
    echo [CFD] Queue was already empty.
)
endlocal
exit /b 0


:forcecontinue
setlocal enabledelayedexpansion
set "WDIR=%CD%"
set "LOCK_FILE=%WDIR%\.cfd.lock"
set "QUEUE_FILE=%WDIR%\.cfd.queue"
set "CURRENT_FILE=%WDIR%\.cfd.current"

:: A stale lock can carry session-deferred items from the crashed
:: session. Flush them back to the queue before clearing the lock,
:: so a fresh worker starts them again with a full retry budget.
if exist "%LOCK_FILE%" (
    call :flush_deferred
    del /f /q "%LOCK_FILE%" >nul 2>&1
)

set "CURRENT_CMD="
if exist "%CURRENT_FILE%" (
    for /f "usebackq delims=" %%L in ("%CURRENT_FILE%") do (
        if not defined CURRENT_CMD set "CURRENT_CMD=%%L"
    )
)

if not defined CURRENT_CMD if not exist "%QUEUE_FILE%" (
    echo [CFD] Nothing to continue - no saved queue or in-progress download found here.
    endlocal
    exit /b 0
)

:: Put the interrupted download back at the front of the queue
set "TMPFILE=%QUEUE_FILE%.tmp"
> "%TMPFILE%" (
    if defined CURRENT_CMD echo !CURRENT_CMD!
    if exist "%QUEUE_FILE%" type "%QUEUE_FILE%"
)
if exist "%QUEUE_FILE%" del /f /q "%QUEUE_FILE%" >nul 2>&1
move /y "%TMPFILE%" "%QUEUE_FILE%" >nul
if exist "%CURRENT_FILE%" del /f /q "%CURRENT_FILE%" >nul 2>&1

if defined CURRENT_CMD (
    echo [CFD] Resuming interrupted download ^(existing .cache will be reused/verified^): !CURRENT_CMD!
) else (
    echo [CFD] Resuming queued downloads for this folder...
)
start "CFD Worker" cmd /c ""%~f0" __worker__ "%WDIR%""
endlocal
exit /b 0


:uninstall
setlocal enabledelayedexpansion
set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

echo ===================================================
echo CFD - Uninstalling
echo ===================================================
echo Install folder: %INSTALL_DIR%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$installDir = '%INSTALL_DIR%';" ^
  "$userPath = [Environment]::GetEnvironmentVariable('Path','User');" ^
  "$parts = $userPath -split ';' | Where-Object { $_ -ne '' -and $_ -ne $installDir };" ^
  "$newPath = $parts -join ';';" ^
  "[Environment]::SetEnvironmentVariable('Path', $newPath, 'User');" ^
  "Write-Host 'Removed' $installDir 'from PATH.'"

echo Removing installed files...
start "" cmd /c "timeout /t 1 /nobreak >nul & rmdir /s /q "%INSTALL_DIR%""
echo.
echo Done. cfd has been uninstalled.
echo Close and reopen any terminal windows for the change to take effect.
endlocal
exit /b 0