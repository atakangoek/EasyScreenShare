@echo off
rem Starts the EasyScreenShare PC receiver (sets up Python packages on first run).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo First run: installing dependencies...
    python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    .venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
)
.venv\Scripts\python easyscreenshare.py %*
if errorlevel 1 pause
