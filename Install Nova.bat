@echo off
rem Installs Nova for this user (no administrator rights needed). Run again to update.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
