#!/bin/bash
set -e
clear
echo ""
echo "  ✂  ClipCutter Installer"
echo "  ─────────────────────────"
echo ""

INSTALL_DIR="$HOME/.clipcutter"
REPO_DIR="$INSTALL_DIR/repo"
APP_PATH="$HOME/Desktop/ClipCutter.app"
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

echo "  [3/5] Checking ffmpeg & git..."
command -v ffmpeg &>/dev/null || { echo "       Installing ffmpeg..."; brew install ffmpeg; }
command -v git &>/dev/null || { echo "       Installing git..."; brew install git; }
echo "       ✓ ffmpeg and git ready"

echo "  [4/5] Setting up ClipCutter..."
mkdir -p "$INSTALL_DIR"

# Clone or update the repo
if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR" && git pull --ff-only origin main 2>/dev/null || true
    echo "       ✓ Updated from GitHub"
else
    git clone https://github.com/tevshko14/clipcutter.git "$REPO_DIR"
    echo "       ✓ Cloned from GitHub"
fi

# Copy app files
cp -f "$REPO_DIR/clipcutter.py" "$INSTALL_DIR/clipcutter.py"
cp -f "$REPO_DIR/index.html"    "$INSTALL_DIR/index.html"

# Set up venv
if [ ! -f "$VENV_DIR/bin/python" ]; then
    rm -rf "$VENV_DIR"
    $PYTHON -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install flask pywebview yt-dlp openai-whisper anthropic faster-whisper -q
echo "       ✓ All packages installed"

echo "  [5/5] Building ClipCutter.app..."
rm -rf "$APP_PATH" 2>/dev/null || true
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

# Copy app icon
if [ -f "$REPO_DIR/icon.icns" ]; then
    cp -f "$REPO_DIR/icon.icns" "$APP_PATH/Contents/Resources/icon.icns"
    echo "       ✓ App icon installed"
fi

cat > "$APP_PATH/Contents/MacOS/launcher" << 'LAUNCHER'
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

INSTALL_DIR="$HOME/.clipcutter"
REPO_DIR="$INSTALL_DIR/repo"
VENV="$INSTALL_DIR/venv"
PYTHON="$VENV/bin/python"

exec > "$INSTALL_DIR/launch.log" 2>&1

# Auto-update from GitHub (silent, non-blocking)
if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR"
    git pull --ff-only origin main 2>/dev/null || true
    cp -f "$REPO_DIR/clipcutter.py" "$INSTALL_DIR/clipcutter.py"
    cp -f "$REPO_DIR/index.html"    "$INSTALL_DIR/index.html"
    # Update icon if it changed
    if [ -f "$REPO_DIR/icon.icns" ]; then
        APP_RES="$(find "$HOME/Desktop" -name "ClipCutter*.app" -maxdepth 1 2>/dev/null | head -1)/Contents/Resources"
        [ -d "$APP_RES" ] && cp -f "$REPO_DIR/icon.icns" "$APP_RES/icon.icns" 2>/dev/null
    fi
fi

# Ensure deps
"$VENV/bin/pip" install -q flask pywebview yt-dlp openai-whisper anthropic faster-whisper 2>/dev/null

cd "$INSTALL_DIR"
# Forward "$@" so right-click "Open With ClipCutter" passes the file path through
exec "$PYTHON" clipcutter.py "$@"
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
    <string>2.0</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIconFile</key>
    <string>icon</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>CFBundleDocumentTypes</key>
    <array>
      <dict>
        <key>CFBundleTypeName</key>
        <string>Video File</string>
        <key>CFBundleTypeRole</key>
        <string>Viewer</string>
        <key>LSItemContentTypes</key>
        <array>
          <string>public.mpeg-4</string>
          <string>public.movie</string>
          <string>public.video</string>
        </array>
        <key>LSHandlerRank</key>
        <string>Alternate</string>
      </dict>
    </array>
    <key>CFBundleURLTypes</key>
    <array>
      <dict>
        <key>CFBundleURLName</key>
        <string>ClipCutter Session</string>
        <key>CFBundleURLSchemes</key>
        <array>
          <string>clipcutter</string>
        </array>
      </dict>
    </array>
</dict>
</plist>
PLIST

# Register the .app with LaunchServices so right-click "Open With" sees it
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_PATH" 2>/dev/null || true

echo ""
echo "  ═══════════════════════════════════════"
echo "  ✅  Done! ClipCutter.app is on your Desktop."
echo ""
echo "  Just double-click it to launch."
echo "  It auto-updates from GitHub on every launch."
echo ""
echo "  First time: right-click → Open → Open"
echo "  ═══════════════════════════════════════"
echo ""
