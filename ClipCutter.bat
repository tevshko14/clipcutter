@echo off
title ClipCutter
cd /d "%~dp0"

:: First run: install deps
python -c "import flask, webview" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install flask pywebview yt-dlp -q
)

echo Launching ClipCutter...
python clipcutter.py
