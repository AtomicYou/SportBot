@echo off
cd /d "%~dp0"
start "" /min ".venv\Scripts\pythonw.exe" monitor.py
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8085/"
