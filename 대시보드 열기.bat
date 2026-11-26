@echo off
cd /d "%~dp0"
set "PYW="
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"
if defined PYW if exist "%PYW%" (
    start "" "%PYW%" server.py
    exit /b
)
start "" python server.py