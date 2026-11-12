@echo off
rem Builds dist\DistriBuild.exe, the one program for every role: the setup window, the helper agent and
rem the xgConsole that UnrealBuildTool runs (see distribuild.py). Needs Python 3.9 or later on PATH.
rem     build.bat
rem The first run sets up build\venv with PySide6 and PyInstaller, so the Python on PATH is left as it is.
pushd "%~dp0"
set "VENV=%~dp0build\venv"
if not exist "%VENV%\Scripts\python.exe" (
	echo Setting up %VENV% ...
	python -m venv "%VENV%" || goto :error
)
"%VENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check PySide6-Essentials pyinstaller || goto :error
rem --clean: an analysis cached from another Python setup can leave Qt's plugins out.
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console --name DistriBuild distribuild.py || goto :error
rem Without Qt's platform plugin the setup window cannot open. PyInstaller drops it silently when Qt's DLLs
rem resolve from two places, as under a packaged (MSIX) app that redirects %APPDATA%.
"%VENV%\Scripts\python.exe" -c "import sys; from PyInstaller.archive.readers import CArchiveReader; sys.exit(0 if any('qwindows.dll' in n.lower() for n in CArchiveReader(r'dist\DistriBuild.exe').toc) else 'Qt platform plugin (qwindows.dll) is missing from dist\DistriBuild.exe')" || goto :error
echo.
echo Done: dist\DistriBuild.exe
echo Run it to open the setup window. Its package button copies it, with what else a PC needs, for other PCs.
popd
exit /b 0
:error
popd
exit /b 1
