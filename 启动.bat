@echo off
rem Launcher for merge_gui.py - no non-ASCII literals here, safe under any cmd codepage.
setlocal
set "GUI=%~dp0merge_gui.py"
set "PYDIR=%LOCALAPPDATA%\Python\pythoncore-3.14-64"
set "PYW=%PYDIR%\pythonw.exe"
set "PY=%PYDIR%\python.exe"

if exist "%PYW%" (
    start "" "%PYW%" "%GUI%"
    exit /b 0
)
if exist "%PY%" (
    "%PY%" "%GUI%"
    exit /b %errorlevel%
)

echo [ERROR] Python 3.14 with tkinter was not found at:
echo   %PYDIR%
echo Edit this .bat and point PYW / PY to a Python that has tkinter.
pause
exit /b 1
