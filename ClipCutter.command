#!/bin/bash
# ClipCutter — Double-click to launch
# ────────────────────────────────────
cd "$(dirname "$0")"

# Prefer Homebrew Python 3.12/3.13/3.11 over system python3 (which may be 3.8)
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if command -v python3.12 &>/dev/null; then
    PYTHON=python3.12
elif command -v python3.13 &>/dev/null; then
    PYTHON=python3.13
elif command -v python3.11 &>/dev/null; then
    PYTHON=python3.11
else
    PYTHON=python3
fi

echo "✂  ClipCutter — using $($PYTHON --version)"

# First run: install deps
if ! $PYTHON -c "import flask, webview" 2>/dev/null; then
    echo "📦 First run — installing dependencies..."
    $PYTHON -m pip install flask pywebview yt-dlp openai-whisper anthropic -q 2>/dev/null
fi

echo "✂  Launching ClipCutter..."
$PYTHON clipcutter.py
