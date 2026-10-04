@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\connect.ps1" %*
