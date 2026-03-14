@echo off
REM build_rider_installer.bat
REM Builds ReDriveRider-Setup.exe for distribution to Windows riders.
REM
REM Requirements:
REM   pip install pyinstaller aiohttp
REM   Inno Setup installed (https://jrsoftware.org/isinfo.php)

setlocal

echo.
echo === ReDrive Rider — Windows installer build ===
echo.

REM ── Step 1: PyInstaller ──────────────────────────────────────────────────
echo [1/2] Building ReDriveRider.exe with PyInstaller...
pyinstaller rider.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed.
    echo Make sure aiohttp is installed:  pip install aiohttp pyinstaller
    pause
    exit /b 1
)
echo        Done.  dist\ReDriveRider.exe created.
echo.

REM ── Step 2: Inno Setup ───────────────────────────────────────────────────
echo [2/2] Building installer with Inno Setup...

REM Try common Inno Setup install locations
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
    set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
) else if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" (
    set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
) else if exist "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe" (
    set ISCC="%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
)

if "%ISCC%"=="" (
    echo.
    echo ERROR: Inno Setup not found.
    echo Download from https://jrsoftware.org/isinfo.php then re-run this script.
    pause
    exit /b 1
)

mkdir dist\installer 2>nul
%ISCC% installer.iss
if errorlevel 1 (
    echo.
    echo ERROR: Inno Setup build failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete:
echo  dist\installer\ReDriveRider-Setup.exe
echo ============================================================
echo.
echo Share that file with your riders.  They just double-click
echo to install — no Python needed.
echo.
pause
