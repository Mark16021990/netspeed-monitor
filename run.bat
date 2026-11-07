@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYW="
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do if not defined PYW if exist "%%i" set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW if exist "C:\Python314\pythonw.exe" set "PYW=C:\Python314\pythonw.exe"
if not defined PYW (
    echo Не найден pythonw.exe. Установите Python и добавьте его в PATH.
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0netspeed.py"
