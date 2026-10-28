@echo off
rem Builds xgConsole.exe (DistriBuild's XGE-compatible front end) and DistriBuildAgent.exe.
rem Requires Python 3.9+ on the build machine:  pip install pyinstaller
pushd "%~dp0"
python -m PyInstaller --onefile --console --name xgConsole xgconsole.py || goto :error
python -m PyInstaller --onefile --console --name DistriBuildAgent agent.py || goto :error
echo.
echo Done. Copy dist\xgConsole.exe to a folder on PATH (e.g. C:\DistriBuild\bin).
echo Copy dist\DistriBuildAgent.exe to every helper PC.
popd
exit /b 0
:error
popd
exit /b 1
