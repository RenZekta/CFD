@echo off
:: ===================================================
:: CFD - Current Folder Downloader  (Windows launcher)
::
:: All the logic lives in cfd-core.py. This file only
:: finds Python 3, remembers its own path, and passes
:: every argument through to the core. Keep it in the
:: same folder as cfd-core.py.
::
:: Usage:
::   cfd hf download hf://prism-ml/Bonsai-27B-gguf/Bonsai-27B-Q1_0.gguf
::   cfd https://huggingface.co/.../resolve/main/file.gguf?download=true
::   cfd status          - show active/current/queued downloads here
::   cfd continue / c    - resume after a crash/interruption using saved queue data
::   cfd clear           - wipe the pending queue (and in-progress marker) here
::   cfd log             - show the tail of the worker log (background mode)
::   cfd uninstall       - remove cfd from PATH and delete its files
::
:: Env overrides (mainly for tests):
::   CFD_SMALL_DELAY         seconds between retries (default 10)
::   CFD_WORKER_EXIT_DELAY   seconds to linger after the queue empties (default 5)
:: ===================================================
setlocal enabledelayedexpansion

set "CFD_LAUNCHER=%~f0"
set "CFD_CORE=%~dp0cfd-core.py"

if not exist "%CFD_CORE%" (
    echo [Error] cfd-core.py is missing - keep it in the same folder as cfd.bat.
    echo Install folder looked at: %CFD_CORE%
    exit /b 1
)

:: Pick a working Python 3 on PATH: py (launcher) first, then python.
set "PYCMD="
for %%P in (py python python3) do (
    if not defined PYCMD (
        where %%P >nul 2>&1
        if not errorlevel 1 set "PYCMD=%%P"
    )
)

if not defined PYCMD (
    echo [Error] Python 3 is required but was not found on PATH.
    echo Install Python 3 from https://python.org , then run cfd again.
    exit /b 1
)

:: The worker runs in its own console window; give it a recognisable title.
if /i "%~1"=="__worker__" title CFD Worker

"%PYCMD%" "%CFD_CORE%" %*
exit /b %errorlevel%
