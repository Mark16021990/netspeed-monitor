@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Установка зависимостей для NetSpeed Monitor (psutil, pystray, pillow)...
echo.
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo Ошибка установки. Проверьте, что Python 3.10+ установлен и доступен как "python".
    pause
    exit /b 1
)
echo.
echo Готово. Запускайте run.bat, для автозапуска - add_to_startup.bat.
pause
