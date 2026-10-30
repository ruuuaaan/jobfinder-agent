@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-jobfinder.ps1"
if errorlevel 1 pause
