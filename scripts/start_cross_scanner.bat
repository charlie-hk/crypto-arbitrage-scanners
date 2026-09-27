@echo off
cd /d "%~dp0.."
title Cross-Exchange Scanner
echo Cross-exchange scanner is running. Close this window or press Ctrl+C to stop.
:loop
python arb_scanner.py cross --exchanges binance kucoin okx --loop 60
echo.
echo Scanner stopped unexpectedly. Restarting in 60 seconds...
timeout /t 60 /nobreak >nul
goto loop
