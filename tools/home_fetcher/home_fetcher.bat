@echo off
rem The Watcher — keeps the home page fetcher running. Put a shortcut to this
rem file in your Startup folder (Win+R, type  shell:startup) so it starts with
rem Windows. Close the window to stop it.
cd /d "%~dp0"
:loop
python home_fetcher.py
echo home_fetcher exited — restarting in 5 seconds (Ctrl+C to stop)
timeout /t 5 /nobreak >nul
goto loop
