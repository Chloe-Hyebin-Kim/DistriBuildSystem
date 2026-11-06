@echo off
rem Sets up a developer PC so UnrealBuildTool (UE 4.26 and 5.8) hands C++ builds to DistriBuild.
rem Run as administrator, after build_xgconsole.bat:
rem     install_client.bat [install folder, default C:\DistriBuild\bin]
rem
rem   1. Installs a do-nothing service named "Incredibuild Agent": XGE.IsAvailable in UBT refuses
rem      to use the XGE executor unless a service of that name is running.
rem   2. Copies xgConsole.exe to the install folder and adds that folder to the machine PATH.
rem
rem Undo with uninstall_client.bat.
setlocal
set "HERE=%~dp0"
set "BIN=%~1"
if "%BIN%"=="" set "BIN=C:\DistriBuild\bin"
set "SERVICE=Incredibuild Agent"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

net session >nul 2>&1
if errorlevel 1 (
	echo Run this from an administrator command prompt.
	exit /b 1
)
if not exist "%HERE%dist\xgConsole.exe" (
	echo %HERE%dist\xgConsole.exe not found. Run build_xgconsole.bat first.
	exit /b 1
)
rem UBT prefers an xgConsole.exe named by this key, which would bypass DistriBuild.
for %%K in ("HKLM\SOFTWARE\Xoreax\IncrediBuild\Builder" "HKLM\SOFTWARE\WOW6432Node\Xoreax\IncrediBuild\Builder") do (
	reg query %%K /v Folder >nul 2>&1 && (
		echo Incredibuild is installed on this PC ^(%%~K^). Uninstall it before using DistriBuild.
		exit /b 1
	)
)
sc query "%SERVICE%" >nul 2>&1
if not errorlevel 1 (
	sc qc "%SERVICE%" | findstr /i "DistriBuildXgeShim" >nul
	if errorlevel 1 (
		echo A service named "%SERVICE%" already exists and is not DistriBuild's. Nothing was changed.
		exit /b 1
	)
	sc stop "%SERVICE%" >nul 2>&1
	sc delete "%SERVICE%" >nul
	timeout /t 2 /nobreak >nul
)

if not exist "%BIN%" mkdir "%BIN%" || exit /b 1
copy /y "%HERE%dist\xgConsole.exe" "%BIN%\" >nul || exit /b 1
"%CSC%" /nologo /target:exe /reference:System.ServiceProcess.dll /out:"%BIN%\DistriBuildXgeShim.exe" "%HERE%service\DistriBuildXgeShim.cs" || exit /b 1

sc create "%SERVICE%" binPath= "\"%BIN%\DistriBuildXgeShim.exe\"" start= auto obj= "NT AUTHORITY\LocalService" DisplayName= "DistriBuild XGE shim (%SERVICE%)" >nul || exit /b 1
sc description "%SERVICE%" "Placeholder that makes UnrealBuildTool use its XGE executor, which then runs DistriBuild. Does no work itself." >nul
sc start "%SERVICE%" >nul || exit /b 1

powershell -NoProfile -Command "$b=$env:BIN; $p=[Environment]::GetEnvironmentVariable('Path','Machine'); if (($p -split ';') -notcontains $b) { [Environment]::SetEnvironmentVariable('Path',($p.TrimEnd(';')+';'+$b),'Machine') }" || exit /b 1

echo.
echo Done. Open a new terminal and run:  xgConsole --doctor
exit /b 0
