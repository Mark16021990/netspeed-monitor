@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Сборка NetSpeedMonitor.exe (PyInstaller, без консоли, один файл)...
python -m pip install pyinstaller
if errorlevel 1 (
    echo Не удалось установить PyInstaller.
    pause
    exit /b 1
)
python -m PyInstaller --noconsole --onefile --name NetSpeedMonitor "%~dp0netspeed.py"
if errorlevel 1 (
    echo Ошибка сборки.
    pause
    exit /b 1
)
echo.
echo Готово: "%~dp0dist\NetSpeedMonitor.exe"
echo config.json и netspeed.log будут создаваться рядом с exe.
pause
