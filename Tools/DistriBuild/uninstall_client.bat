@echo off
rem Undoes install_client.bat. Run as administrator:  uninstall_client.bat [install folder]
setlocal
set "BIN=%~1"
if "%BIN%"=="" set "BIN=C:\DistriBuild\bin"
set "SERVICE=Incredibuild Agent"

net session >nul 2>&1
if errorlevel 1 (
	echo Run this from an administrator command prompt.
	exit /b 1
)
sc qc "%SERVICE%" 2>nul | findstr /i "DistriBuildXgeShim" >nul
if not errorlevel 1 (
	sc stop "%SERVICE%" >nul 2>&1
	sc delete "%SERVICE%" >nul
	timeout /t 2 /nobreak >nul
)
powershell -NoProfile -Command "$b=$env:BIN; $p=[Environment]::GetEnvironmentVariable('Path','Machine'); [Environment]::SetEnvironmentVariable('Path',((($p -split ';') | Where-Object { $_ -ne $b }) -join ';'),'Machine')"
del /q "%BIN%\xgConsole.exe" "%BIN%\DistriBuildXgeShim.exe" 2>nul
echo Done.
exit /b 0
