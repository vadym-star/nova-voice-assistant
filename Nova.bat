@echo off
rem Start Nova without a console window.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw -m nova) || (start "" pyw -3 -m nova)
