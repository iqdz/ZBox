@echo off
setlocal
cd /d "%~dp0"

rem Runs ZBox from source only, against this folder's own data\.
rem A built app is never started from here: use tools\run_build.bat
rem for that, so it is always clear which copy is running and which
rem data folder it writes to.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this system.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during setup, then run this again.
    pause
    goto :eof
)

echo Checking required packages...
set NEED_INSTALL=0
python -c "import wx" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1
python -c "import imapclient" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1
python -c "import win32crypt" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1

if "%NEED_INSTALL%"=="1" (
    echo Some required packages are missing.
    echo Installing from requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed. Check the messages above.
        pause
        goto :eof
    )
)

echo Checking for Himalaya CLI...
call "tools\get_himalaya.bat"
if not exist "himalaya\himalaya.exe" (
    echo.
    echo ZBox cannot run without Himalaya. See the message above.
    pause
    goto :eof
)

echo.
echo Starting ZBox from source.
echo Data folder: "%CD%\data"
rem %* passes launcher.bat's own arguments on, so --debug and
rem --minimized work from here too.
python main.py %*
if not errorlevel 1 goto :eof

rem Logs are timestamped, zbox_debug_<date>_<time>.log, so the one
rem that describes this failure is the newest.
set "NEWEST_LOG="
for /f "delims=" %%L in ('dir /b /o-d "data\logs\zbox_debug_*.log" 2^>nul') do if not defined NEWEST_LOG set "NEWEST_LOG=%%L"
echo.
if defined NEWEST_LOG (
    echo ZBox closed with an error. See data\logs\%NEWEST_LOG% for details.
) else (
    echo ZBox closed with an error. No log was written; debug logging may be off in Settings.
)
pause

endlocal
