@echo off
rem Starts the EasyScreenShare PC receiver (installs Python packages on first run and when requirements.txt changes).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
)
fc /b requirements.txt .venv\requirements.txt >nul 2>&1 || (
    echo Installing dependencies...
    .venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
    copy /y requirements.txt .venv\requirements.txt >nul
)
.venv\Scripts\python easyscreenshare.py %*
if errorlevel 1 pause
