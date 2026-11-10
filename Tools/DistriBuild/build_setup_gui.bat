@echo off
rem Builds dist\DistriBuildSetup.exe, the Qt window that configures helper PCs and developer PCs.
rem Requires Python 3.9+ on the build machine:  pip install pyinstaller PySide6-Essentials
pushd "%~dp0"
python -m PyInstaller --noconfirm --onefile --windowed --name DistriBuildSetup setup_gui.py || goto :error
echo.
echo Done: dist\DistriBuildSetup.exe
popd
exit /b 0
:error
popd
exit /b 1
