@echo off
chcp 65001 >nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v NetSpeedMonitor /f >nul 2>&1
if errorlevel 1 (
    echo NetSpeed Monitor не был в автозапуске.
) else (
    echo NetSpeed Monitor удалён из автозапуска.
)
pause
