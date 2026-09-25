@echo off
rem Installs a WebView2 "Fixed Version" runtime into
rem apps_files\webview2 so ZBox renders HTML mail with a build it
rem controls, instead of
rem whatever Edge version Windows Update last installed. See
rem docs\webview2_fixed_runtime.md for why that matters here.
rem
rem Microsoft does not publish a stable direct download URL for the
rem fixed-version package -- the link is generated per version on
rem their download page -- so the .cab has to be fetched by hand
rem once. Drop it in this tools folder (or pass its path as the first
rem argument) and run this script; it does the rest.
setlocal
cd /d "%~dp0"

set "TARGET=%~dp0..\apps_files\webview2"

rem Already installed?
if exist "%TARGET%\msedgewebview2.exe" goto :installed
for /d %%D in ("%TARGET%\*") do (
    if exist "%%D\msedgewebview2.exe" goto :installed
)

rem Locate the package: first argument wins, otherwise any .cab
rem sitting in this tools folder.
set "PACKAGE=%~1"
if not defined PACKAGE (
    for %%F in ("%~dp0*.cab") do set "PACKAGE=%%~fF"
)

if not defined PACKAGE (
    echo.
    echo No WebView2 fixed-version package found.
    echo.
    echo 1. Open https://developer.microsoft.com/microsoft-edge/webview2
    echo 2. Under "Download the WebView2 Runtime", pick the
    echo    Fixed Version package for x64.
    echo 3. Save the .cab file into this tools folder, or pass its
    echo    full path to this script:
    echo.
    echo        get_webview2.bat C:\path\to\package.cab
    echo.
    echo Until then ZBox falls back to the system-wide runtime and
    echo still starts normally.
    exit /b 1
)

if not exist "%PACKAGE%" (
    echo Package not found: %PACKAGE%
    exit /b 1
)

echo Expanding %PACKAGE% into %TARGET% ...
if not exist "%~dp0..\apps_files" mkdir "%~dp0..\apps_files"
if not exist "%TARGET%" mkdir "%TARGET%"
expand "%PACKAGE%" -F:* "%TARGET%" >nul
if errorlevel 1 (
    echo.
    echo expand failed on %PACKAGE%.
    echo Check that the file downloaded completely and is the
    echo Fixed Version .cab, not the Evergreen bootstrapper.
    exit /b 1
)

if exist "%TARGET%\msedgewebview2.exe" goto :done
for /d %%D in ("%TARGET%\*") do (
    if exist "%%D\msedgewebview2.exe" goto :done
)

echo.
echo Expanded, but no msedgewebview2.exe turned up under %TARGET%.
echo That package was probably not the Fixed Version runtime.
exit /b 1

:installed
echo WebView2 fixed-version runtime already installed in apps_files\webview2.
goto :eof

:done
echo WebView2 fixed-version runtime installed in apps_files\webview2.
echo ZBox will use it on next start ^(Settings ^> WebView2 runtime^).
endlocal
