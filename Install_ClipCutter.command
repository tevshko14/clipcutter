#!/bin/bash
set -e
clear
echo ""
echo "  ✂  ClipCutter Installer"
echo "  ─────────────────────────"
echo ""

INSTALL_DIR="$HOME/.clipcutter"
APP_PATH="$HOME/Desktop/ClipCutter.app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$INSTALL_DIR/venv"

echo "  [1/5] Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo "       ✓ Found Homebrew"
    else
        echo "       Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
else
    echo "       ✓ Already installed"
fi

echo "  [2/5] Checking Python..."
if command -v python3.12 &>/dev/null; then
    PYTHON=python3.12
elif command -v python3.13 &>/dev/null; then
    PYTHON=python3.13
elif command -v python3.11 &>/dev/null; then
    PYTHON=python3.11
else
    echo "       Installing Python 3.12..."
    brew install python@3.12
    PYTHON=python3.12
fi
echo "       ✓ Using $($PYTHON --version)"

echo "  [3/5] Checking ffmpeg..."
if command -v ffmpeg &>/dev/null; then
    echo "       ✓ Already installed"
else
    echo "       Installing ffmpeg..."
    brew install ffmpeg
    echo "       ✓ ffmpeg installed"
fi

echo "  [4/5] Setting up ClipCutter environment..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/clipcutter.py" "$INSTALL_DIR/clipcutter.py"
cp "$SCRIPT_DIR/index.html" "$INSTALL_DIR/index.html"
rm -rf "$VENV_DIR"
$PYTHON -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install flask pywebview yt-dlp openai-whisper anthropic -q
echo "       ✓ All packages installed"

echo "  [5/5] Building ClipCutter.app..."
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

cat > "$APP_PATH/Contents/MacOS/launcher" << LAUNCHER
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$INSTALL_DIR"
"$VENV_DIR/bin/python" clipcutter.py
LAUNCHER
chmod +x "$APP_PATH/Contents/MacOS/launcher"

cat > "$APP_PATH/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ClipCutter</string>
    <key>CFBundleDisplayName</key>
    <string>ClipCutter</string>
    <key>CFBundleIdentifier</key>
    <string>com.clipcutter.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

echo ""
echo "  ═══════════════════════════════════════"
echo "  ✅  Done! ClipCutter.app is on your Desktop."
echo ""
echo "  Just double-click it to launch."
echo ""
echo "  First time: right-click → Open → Open"
echo "  ═══════════════════════════════════════"
echo ""
