@echo off
rem Double-click this file to install Clip Factory into Desktop\Clipping Tools.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1"
if errorlevel 1 pause
