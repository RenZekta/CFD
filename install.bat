@echo off
:: ===================================================
:: CFD Installer
:: Copies cfd.bat to a permanent folder, adds that
:: folder to your user PATH, and removes any stray
:: cfd.bat found elsewhere on PATH.
:: Run this once. Keep it in the same folder as cfd.bat.
:: ===================================================

setlocal enabledelayedexpansion

set "INSTALL_DIR=%LOCALAPPDATA%\CFD"
set "SCRIPT_SOURCE=%~dp0cfd.bat"

echo ===================================================
echo CFD - Current Folder Downloader - Installer
echo ===================================================

if not exist "%SCRIPT_SOURCE%" (
    echo [Error] cfd.bat was not found next to install.bat.
    echo Make sure both files are in the same folder, then try again.
    pause
    exit /b 1
)

if not exist "%INSTALL_DIR%" (
    mkdir "%INSTALL_DIR%"
)

echo Checking PATH for existing cfd installs and updating PATH...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$installDir = '%INSTALL_DIR%';" ^
 "$pathDirs = $env:Path -split ';' | Where-Object { $_ -ne '' };" ^
 "foreach ($d in $pathDirs) {" ^
 "  try {" ^
 "    $candidate = Join-Path $d 'cfd.bat';" ^
 "    if ((Test-Path $candidate) -and ($d.TrimEnd('\') -ne $installDir.TrimEnd('\'))) {" ^
 "      Write-Host 'Found existing cfd.bat in' $d '- removing it to avoid conflicts...';" ^
 "      Remove-Item $candidate -Force -ErrorAction Stop;" ^
 "    }" ^
 "  } catch {" ^
 "    Write-Host '  Could not remove' $candidate '(insufficient permissions) - it may shadow the new version.';" ^
 "  }" ^
 "}" ^
 "$userPath = [Environment]::GetEnvironmentVariable('Path','User');" ^
 "$parts = $userPath -split ';' | Where-Object { $_ -ne '' };" ^
 "if ($parts -notcontains $installDir) {" ^
 "  $newPath = ($parts + $installDir) -join ';';" ^
 "  [Environment]::SetEnvironmentVariable('Path', $newPath, 'User');" ^
 "  Write-Host 'Added' $installDir 'to your PATH.';" ^
 "} else {" ^
 "  Write-Host $installDir 'is already in your PATH.';" ^
 "}"

echo.
echo Installing cfd.bat to %INSTALL_DIR% (overwriting if already present)...
copy /y "%SCRIPT_SOURCE%" "%INSTALL_DIR%\cfd.bat" >nul

echo.
echo ===================================================
echo Install complete!
echo Close and reopen any cmd/terminal windows, then run
echo it from any folder, e.g.:
echo   cfd hf download hf://prism-ml/Ternary-Bonsai-27B-gguf/Ternary-Bonsai-27B-mmproj-BF16
echo.
echo To remove it later, run:  cfd uninstall
echo ===================================================
pause
