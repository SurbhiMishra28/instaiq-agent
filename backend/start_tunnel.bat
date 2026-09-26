@echo off
title InstaIQ Home Relay Tunnel
cd /d "%~dp0"
python tunnel.py --port 8765
echo.
echo Tunnel exited (port busy or network down?). Press any key to close.
pause >nul
