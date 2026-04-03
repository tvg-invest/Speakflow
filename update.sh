#!/bin/bash
# SpeakFlow — Update to latest version
set -e
cd "$HOME/.speakflow"

echo "Updating SpeakFlow..."
git pull --quiet
source venv/bin/activate
pip install --quiet -r requirements.txt

# Rebuild .app launcher and re-embed Python
APP="$HOME/Desktop/SpeakFlow.app"
if [ -f launcher.c ]; then
    cc -o "$APP/Contents/MacOS/SpeakFlow" launcher.c

    # Re-embed the Python binary (keeps Accessibility trust stable)
    REAL_PYTHON="$(python3 -c "import os,sys; print(os.path.realpath(sys.executable))")"
    if [ -f "$REAL_PYTHON" ]; then
        cp "$REAL_PYTHON" "$APP/Contents/MacOS/python3"
        chmod +x "$APP/Contents/MacOS/python3"
        PY_FWDIR="$(dirname "$(dirname "$REAL_PYTHON")")"
        if [ -f "$PY_FWDIR/Python3" ]; then
            install_name_tool -change "@executable_path/../Python3" \
                "$PY_FWDIR/Python3" "$APP/Contents/MacOS/python3" 2>/dev/null || true
        fi
    fi

    # Re-sign the bundle
    codesign --force --deep --sign - "$APP" 2>/dev/null || true
fi

echo "Updated! Restart SpeakFlow to apply changes."
