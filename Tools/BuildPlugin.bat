@echo off
rem Compiles the DistriBuild plugin against one installed engine, the way Epic packages plugins.
rem   Tools\BuildPlugin.bat "C:\Program Files\Epic Games\UE_4.26"
rem   Tools\BuildPlugin.bat "C:\Program Files\Epic Games\UE_5.8"
rem Output: Build\<engine folder name>\ (packaged plugin) and Build\<engine folder name>.log
setlocal
if "%~1"=="" (
	echo usage: %~nx0 ^<engine root^>
	exit /b 1
)
set "ENGINE=%~1"
set "ROOT=%~dp0.."
for %%I in ("%ENGINE%") do set "NAME=%%~nxI"
set "OUT=%ROOT%\Build\%NAME%"
set "LOG=%ROOT%\Build\%NAME%.log"
if not exist "%ENGINE%\Engine\Build\BatchFiles\RunUAT.bat" (
	echo Not an engine root: %ENGINE%
	exit /b 1
)
if not exist "%ROOT%\Build" mkdir "%ROOT%\Build"
if exist "%OUT%" rmdir /s /q "%OUT%"

echo Building DistriBuild with %NAME% ... (log: %LOG%)
call "%ENGINE%\Engine\Build\BatchFiles\RunUAT.bat" BuildPlugin -Plugin="%ROOT%\Plugin\DistriBuild\DistriBuild.uplugin" -Package="%OUT%" -TargetPlatforms=Win64 > "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"

if "%RESULT%"=="0" (
	echo SUCCESS - packaged plugin in %OUT%
) else (
	echo FAILED ^(exit %RESULT%^). Compiler errors:
	findstr /R /C:": error" /C:": fatal error" /C:"ERROR:" "%LOG%"
	echo.
	echo Full log: %LOG%
)
exit /b %RESULT%
