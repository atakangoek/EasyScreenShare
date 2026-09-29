@echo off
rem Builds a standalone dist\EasyScreenShare\EasyScreenShare.exe (no Python needed to run it).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || exit /b 1
)
fc /b requirements.txt .venv\requirements.txt >nul 2>&1 || (
    .venv\Scripts\python -m pip install -q -r requirements.txt || exit /b 1
    copy /y requirements.txt .venv\requirements.txt >nul
)
.venv\Scripts\python -m pip install -q pyinstaller || exit /b 1
.venv\Scripts\pyinstaller --noconfirm --windowed --name EasyScreenShare --collect-submodules zeroconf --hidden-import airplay easyscreenshare.py
