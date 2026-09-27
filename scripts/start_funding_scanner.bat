@echo off
cd /d "%~dp0.."
title Funding Scanner
echo Funding scanner is running. Close this window or press Ctrl+C to stop.
:loop
python funding_scanner.py scan --keys --loop 1800
echo.
echo Scanner stopped unexpectedly. Restarting in 60 seconds...
timeout /t 60 /nobreak >nul
goto loop
