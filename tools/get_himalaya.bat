@echo off
rem Downloads the Himalaya CLI Windows binary into the himalaya
rem folder beside this one if it is not already there. Called by
rem launcher.bat and build.bat. Pinned to v2.1.0, the version ZBox is
rem tested against: a newer release is a deliberate, tested upgrade,
rem never something a fresh download picks up on its own.
setlocal
cd /d "%~dp0"

set "TARGET=%~dp0..\himalaya"

if exist "%TARGET%\himalaya.exe" (
    goto :eof
)

echo Himalaya CLI not found in the himalaya folder. Downloading it now...
if not exist "%TARGET%" mkdir "%TARGET%"

powershell -NoProfile -Command ^
    "$ErrorActionPreference = 'Stop';" ^
    "$url = 'https://github.com/pimalaya/himalaya/releases/download/v2.1.0/himalaya.x86_64-windows.zip';" ^
    "$zip = Join-Path $PWD 'himalaya_download.zip';" ^
    "Invoke-WebRequest -Uri $url -OutFile $zip;" ^
    "Expand-Archive -Path $zip -DestinationPath '%TARGET%' -Force;" ^
    "Remove-Item $zip"

if not exist "%TARGET%\himalaya.exe" (
    echo.
    echo Himalaya download or extraction failed.
    echo Download it manually from https://github.com/pimalaya/himalaya/releases
    echo and place himalaya.exe in the himalaya folder.
    exit /b 1
)

echo Himalaya CLI installed in the himalaya folder.
endlocal
