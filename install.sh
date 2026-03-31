#!/bin/bash
# ──────────────────────────────────────────────────────────────
# SpeakFlow — One-line installer
# Usage:  curl -sL <raw-url>/install.sh | bash
# ──────────────────────────────────────────────────────────────
set -e

REPO="tvg-invest/Speakflow"
INSTALL_DIR="$HOME/.speakflow"
APP_DIR="$HOME/Desktop/SpeakFlow.app"

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║     SpeakFlow Installer          ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ── Check Python 3.9+ ─────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "  Python 3 not found. Install it first:"
    echo "    brew install python3"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$(python3 -c 'import sys; print(sys.version_info.major')" -lt 3 ] 2>/dev/null || [ "$PY_MINOR" -lt 9 ] 2>/dev/null; then
    echo "  Python 3.9+ required (found $PY_VER)"
    exit 1
fi
echo "  [1/5] Python $PY_VER"

# ── Check git ──────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    echo "  Git not found. Install Xcode Command Line Tools:"
    echo "    xcode-select --install"
    exit 1
fi

# ── Clone or update repo ──────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  [2/5] Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --quiet
else
    echo "  [2/5] Downloading SpeakFlow..."
    if [ -d "$INSTALL_DIR" ]; then
        # Preserve config and history from existing install
        mkdir -p /tmp/speakflow_backup
        cp "$INSTALL_DIR/config.json" /tmp/speakflow_backup/ 2>/dev/null || true
        cp "$INSTALL_DIR/history.json" /tmp/speakflow_backup/ 2>/dev/null || true
        rm -rf "$INSTALL_DIR"
    fi
    git clone "https://github.com/$REPO.git" "$INSTALL_DIR"
    # Restore config/history
    cp /tmp/speakflow_backup/config.json "$INSTALL_DIR/" 2>/dev/null || true
    cp /tmp/speakflow_backup/history.json "$INSTALL_DIR/" 2>/dev/null || true
    rm -rf /tmp/speakflow_backup
fi

# ── Virtual environment ───────────────────────────────────
echo "  [3/5] Setting up Python environment..."
cd "$INSTALL_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ── Build .app bundle ─────────────────────────────────────
echo "  [4/5] Building SpeakFlow.app..."
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

cc -o "$APP_DIR/Contents/MacOS/SpeakFlow" launcher.c

cat > "$APP_DIR/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>SpeakFlow</string>
    <key>CFBundleDisplayName</key>
    <string>SpeakFlow</string>
    <key>CFBundleIdentifier</key>
    <string>com.speakflow.app</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>SpeakFlow</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>SpeakFlow</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>SpeakFlow needs microphone access to transcribe your speech.</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

cp SpeakFlow.icns "$APP_DIR/Contents/Resources/SpeakFlow.icns" 2>/dev/null || true

# ── API key ───────────────────────────────────────────────
echo "  [5/5] Checking configuration..."
CONFIG="$INSTALL_DIR/config.json"
HAS_KEY="no"
if [ -f "$CONFIG" ]; then
    HAS_KEY=$(python3 -c "import json; d=json.load(open('$CONFIG')); print('yes' if d.get('openai_api_key') else 'no')" 2>/dev/null || echo "no")
fi

if [ "$HAS_KEY" = "no" ]; then
    echo ""
    read -p "  Enter your OpenAI API key (or press Enter to skip): " API_KEY < /dev/tty
    if [ -n "$API_KEY" ]; then
        python3 -c "
import json, os
p = '$CONFIG'
d = json.load(open(p)) if os.path.exists(p) else {}
d['openai_api_key'] = '$API_KEY'
json.dump(d, open(p, 'w'), indent=2)
"
        echo "  API key saved."
    fi
fi

echo ""
echo "  ════════════════════════════════════"
echo "  SpeakFlow installed!"
echo "  ════════════════════════════════════"
echo ""
echo "  Open SpeakFlow.app on your Desktop."
echo "  Hold Ctrl → dictate"
echo "  Hold Alt  → select text + AI query"
echo ""
echo "  Update anytime:  ~/.speakflow/update.sh"
echo ""
