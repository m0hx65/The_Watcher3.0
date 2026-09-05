@echo off
rem The Watcher - keeps the home page fetcher running on a Windows PC. Put a
rem shortcut to this file in your Startup folder (Win+R, type  shell:startup)
rem so it starts with Windows. Close the window to stop it. Settings come from
rem home_fetcher.env next to this file.
cd /d "%~dp0"
:loop
python home_fetcher.py
echo home_fetcher exited - restarting in 10 seconds (Ctrl+C to stop)
timeout /t 10 /nobreak >nul
goto loop
