@echo off
setlocal
cd /d "%~dp0"

rem Builds the portable ZBox folder into its own timestamped folder,
rem dist\ZBox_<yyyyMMdd-HHmmss>\ZBox:
rem
rem     <out>\ZBox.exe
rem     <out>\apps_files\   dependencies (see zbox.spec)
rem     <out>\himalaya\     himalaya.exe
rem     <out>\data\         settings.default.json, sound themes, languages
rem
rem One folder per build, on purpose. Every build owns its own data\,
rem so a rebuild can never overwrite or read an earlier build's
rem accounts, cache or logs, and source keeps its own data\ apart from
rem all of them. The newest build's path is written to dist\latest.txt
rem for tools\run_build.bat.
rem
rem Only the two newest builds are kept: this one and the one before
rem it. Older ZBox_* folders in dist\ are deleted at the end of a
rem successful build, together with any accounts or data set up in
rem them. A build that fails deletes nothing.
rem
rem That folder is the whole app: copy it anywhere, run it from any
rem drive, and it writes nothing outside itself.
rem
rem build.bat /nopause skips every pause, for a chained command.
rem Every failure exits with code 1 either way, so a chained command
rem can tell a failed build from a good one.

set "NOPAUSE="
if /i "%~1"=="/nopause" set "NOPAUSE=1"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this system.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during setup, then run this again.
    if not defined NOPAUSE pause
    exit /b 1
)

echo Checking required packages...
set NEED_INSTALL=0
python -c "import wx" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1
python -c "import imapclient" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1
python -c "import win32crypt" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1
python -c "import keyring" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1
python -c "import accessible_output2" >nul 2>nul
if errorlevel 1 set NEED_INSTALL=1

if "%NEED_INSTALL%"=="1" (
    echo Installing required packages from requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed. Check the messages above.
        if not defined NOPAUSE pause
        exit /b 1
    )
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo PyInstaller install failed. Check the messages above.
        if not defined NOPAUSE pause
        exit /b 1
    )
)

rem zbox.spec uses contents_directory, which is what keeps the built
rem root folder clean. It arrived in PyInstaller 6.0.
python -c "import PyInstaller, sys; sys.exit(0 if int(PyInstaller.__version__.split('.')[0]) >= 6 else 1)"
if errorlevel 1 (
    echo Upgrading PyInstaller to 6.0 or newer ^(needed for the clean folder layout^)...
    python -m pip install --upgrade "pyinstaller>=6.0"
    if errorlevel 1 (
        echo PyInstaller upgrade failed. Check the messages above.
        if not defined NOPAUSE pause
        exit /b 1
    )
)

echo Checking for Himalaya CLI...
call "tools\get_himalaya.bat"
if not exist "himalaya\himalaya.exe" (
    echo.
    echo Cannot build without himalaya\himalaya.exe. See the message above.
    if not defined NOPAUSE pause
    exit /b 1
)

echo.
echo Building with PyInstaller...
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%I"
if "%STAMP%"=="" (
    echo Could not read the current date and time. Build stopped.
    if not defined NOPAUSE pause
    exit /b 1
)
set "OUTROOT=dist\ZBox_%STAMP%"
pyinstaller --noconfirm --distpath "%OUTROOT%" --workpath build_temp zbox.spec

set "OUT=%OUTROOT%\ZBox"
if not exist "%OUT%\ZBox.exe" (
    echo.
    echo Build failed. Check the messages above.
    if not defined NOPAUSE pause
    exit /b 1
)

echo.
echo Assembling the portable folder...

rem Only the files a fresh install needs are copied. accounts.json,
rem secrets.dat, himalaya.toml, settings.json, userdata and logs are
rem this machine's own data and must never end up in a build.
if not exist "%OUT%\himalaya" mkdir "%OUT%\himalaya"
copy /Y "himalaya\himalaya.exe" "%OUT%\himalaya\" >nul

if not exist "%OUT%\data\config" mkdir "%OUT%\data\config"
copy /Y "data\config\settings.default.json" "%OUT%\data\config\" >nul

xcopy /E /I /Y /Q "data\sounds" "%OUT%\data\sounds" >nul

rem Every interface language: data\lang\<code>\<code>.lang plus that
rem language's about.txt and shortcuts.txt. Without this folder a
rem build speaks only the English built into the code, the first-run
rem language page never appears and Settings lists no languages,
rem while running from source looks perfect.
xcopy /E /I /Y /Q "data\lang" "%OUT%\data\lang" >nul
if not exist "%OUT%\data\lang\en\en.lang" (
    echo.
    echo The language files were not copied into the build. Build stopped.
    if not defined NOPAUSE pause
    exit /b 1
)

if exist "apps_files\webview2" (
    echo Copying the bundled WebView2 runtime ^(this one is large^)...
    xcopy /E /I /Y /Q "apps_files\webview2" "%OUT%\apps_files\webview2" >nul
)

> "dist\latest.txt" echo %OUT%

rem Keep this build and the one before it; delete the rest. Sorted by
rem folder name, which is the timestamp, so newest first. A build
rem that is still running cannot be deleted; it is reported and left.
powershell -NoProfile -Command "Get-ChildItem -LiteralPath 'dist' -Directory -Filter 'ZBox_*' | Sort-Object Name -Descending | Select-Object -Skip 2 | ForEach-Object { Write-Host ('Removing old build ' + $_.Name); Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Continue }"

echo.
echo Build finished. The portable app is in %OUT%
echo Copy that whole folder anywhere you like.
echo This build has its own empty data folder. Set accounts up in it,
echo or copy an earlier build's data folder across by hand.
echo tools\run_build.bat starts this build; launcher.bat runs source.
if not defined NOPAUSE pause
endlocal
exit /b 0
